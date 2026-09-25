from __future__ import annotations

import random

import psycopg
import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import get_balance, get_system_account_id, post_transfer


async def _ledger_sum(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        (total,) = await cur.fetchone()
    return total


async def _cached_balance_matches_ledger(conn: AsyncConnection, account_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE account_id = %s",
            (account_id,),
        )
        (ledger_total,) = await cur.fetchone()
    cached = await get_balance(conn, account_id)
    return cached == ledger_total


async def _all_account_ids(conn: AsyncConnection) -> list[int]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM accounts")
        return [row[0] for row in await cur.fetchall()]


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_ledger_sum_is_always_zero_under_fuzzing(conn: AsyncConnection, seed: int) -> None:
    rng = random.Random(seed)
    num_users = 8
    user_ids = list(range(1_000 + seed * 100, 1_000 + seed * 100 + num_users))
    account_ids = [
        (await bootstrap_user(conn, uid)).account_id for uid in user_ids
    ]

    sink_id = await get_system_account_id(conn, "SINK")

    for _ in range(300):
        op = rng.choice(["transfer", "sink_fee", "overdraw_attempt"])
        src, dst = rng.sample(account_ids, 2)

        if op == "transfer":
            balance = await get_balance(conn, src)
            if balance <= 0:
                continue
            amount = rng.randint(1, balance)
            await post_transfer(
                conn, from_account_id=src, to_account_id=dst, amount=amount, reason="TEST_TRANSFER"
            )

        elif op == "sink_fee":
            balance = await get_balance(conn, src)
            if balance <= 0:
                continue
            amount = rng.randint(1, min(balance, 50))
            await post_transfer(
                conn, from_account_id=src, to_account_id=sink_id, amount=amount, reason="TEST_FEE"
            )

        else:  # overdraw_attempt: deliberately try to move more than the account has
            balance = await get_balance(conn, src)
            amount = balance + rng.randint(1, 1000)
            try:
                await post_transfer(
                    conn,
                    from_account_id=src,
                    to_account_id=dst,
                    amount=amount,
                    reason="TEST_OVERDRAW",
                )
            except InsufficientFundsError:
                pass

        # The invariant must hold after every single operation, not just at the end.
        assert await _ledger_sum(conn) == 0

    assert await _ledger_sum(conn) == 0

    for account_id in await _all_account_ids(conn):
        assert await _cached_balance_matches_ledger(conn, account_id)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM accounts WHERE kind = 'USER' AND balance < 0"
        )
        (negative_count,) = await cur.fetchone()
    assert negative_count == 0


async def test_overdraft_is_rejected_and_atomic(conn: AsyncConnection) -> None:
    payer = (await bootstrap_user(conn, 42)).account_id
    payee = (await bootstrap_user(conn, 43)).account_id

    balance_before = await get_balance(conn, payer)
    payee_balance_before = await get_balance(conn, payee)
    ledger_sum_before = await _ledger_sum(conn)

    with pytest.raises(InsufficientFundsError):
        await post_transfer(
            conn,
            from_account_id=payer,
            to_account_id=payee,
            amount=balance_before + 1,
            reason="TEST",
        )

    assert await get_balance(conn, payer) == balance_before
    assert await get_balance(conn, payee) == payee_balance_before
    assert await _ledger_sum(conn) == ledger_sum_before


async def test_round_trip_is_loss_making_once_a_fee_applies(conn: AsyncConnection) -> None:
    """A wash-trade defense sanity check at the ledger level: moving money out
    and back via a sink fee always leaves the account strictly poorer.
    """
    account_id = (await bootstrap_user(conn, 99)).account_id
    sink_id = await get_system_account_id(conn, "SINK")
    other_id = (await bootstrap_user(conn, 100)).account_id

    start_balance = await get_balance(conn, account_id)
    fee = 10
    round_trip_amount = 500

    await post_transfer(
        conn,
        from_account_id=account_id,
        to_account_id=other_id,
        amount=round_trip_amount,
        reason="TEST",
    )
    await post_transfer(
        conn, from_account_id=account_id, to_account_id=sink_id, amount=fee, reason="TEST_FEE"
    )
    await post_transfer(
        conn,
        from_account_id=other_id,
        to_account_id=account_id,
        amount=round_trip_amount,
        reason="TEST",
    )

    end_balance = await get_balance(conn, account_id)
    assert end_balance == start_balance - fee


async def test_ledger_entries_are_immutable(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 7)).account_id
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id, amount=1, reason="TEST"
    )

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM ledger_entries WHERE account_id = %s LIMIT 1", (account_id,)
        )
        (entry_id,) = await cur.fetchone()

    with pytest.raises(psycopg.errors.RaiseException):
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE ledger_entries SET amount = 999 WHERE id = %s", (entry_id,)
                )

    with pytest.raises(psycopg.errors.RaiseException):
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM ledger_entries WHERE id = %s", (entry_id,))
