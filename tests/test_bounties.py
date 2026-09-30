"""Bounties: escrow, fee burn, cancel/expiry refunds, liquidation
settlement, opposing-exposure attribution, sink fallback."""

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import STARTING_GRANT, bootstrap_user
from stockbot.bounties import service as bounties
from stockbot.bounties.errors import BountyStateError, BountyTargetError
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.service import check_and_liquidate
from stockbot.trading.service import execute_trade

AMOUNT = 3_000  # $30 pot; $1 posting fee burns on top
FEE = 100


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=faucet,
        to_account_id=await get_user_account_id(conn, user_id),
        amount=amount,
        reason="TEST_TOPUP",
    )


async def _grant_tier(conn: AsyncConnection, user_id: int, tier: int = 1) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO entitlements (user_id, item_key, quantity)
            VALUES (%s, 'margin_tier', %s)
            ON CONFLICT (user_id, item_key)
            DO UPDATE SET quantity = EXCLUDED.quantity
            """,
            (user_id, tier),
        )


async def _ticker(conn: AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK'"
            " ORDER BY id LIMIT 1"
        )
        (t,) = await cur.fetchone()
    return str(t)


async def _levered_short(conn: AsyncConnection, user_id: int, ticker: str) -> None:
    """Short ~1.8x equity so a 3x gap liquidates (test_margin's recipe)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, maint_margin_pct = 0.3 "
            "WHERE ticker = %s RETURNING quoted_price",
            (ticker,),
        )
        (price,) = await cur.fetchone()
        await cur.execute(
            "SELECT balance FROM accounts WHERE user_id = %s AND kind = 'USER'",
            (user_id,),
        )
        (cash,) = await cur.fetchone()
    qty = max(1, int(Decimal(cash) * Decimal("1.8") / (Decimal(price) * 100)))
    await execute_trade(conn, user_id=user_id, ticker=ticker, side="SELL", quantity=qty)


async def _liquidate(conn: AsyncConnection, user_id: int, ticker: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 4, "
            "quoted_price = quoted_price * 4 WHERE ticker = %s",
            (ticker,),
        )
    return await check_and_liquidate(conn, user_id)


async def test_post_escrows_amount_and_burns_fee(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4001)
    await bootstrap_user(conn, 4002)
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    sink_id = await get_system_account_id(conn, "SINK")
    esc0, sink0 = await get_balance(conn, escrow_id), await get_balance(conn, sink_id)

    bounty_id = await bounties.post(conn, 4001, 4002, AMOUNT, tick_index=0)

    poster = await get_user_account_id(conn, 4001)
    assert await get_balance(conn, poster) == STARTING_GRANT - AMOUNT - FEE
    assert await get_balance(conn, escrow_id) == esc0 + AMOUNT
    assert await get_balance(conn, sink_id) == sink0 + FEE
    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "OPEN"

    # The target knows there's a hit out on them.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM notifications WHERE user_id = 4002 AND kind = 'BOUNTY_PLACED'"
        )
        row = await cur.fetchone()
    assert row is not None and row[0]["amount"] == AMOUNT


async def test_post_rejects_self_and_unknown_and_bot(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4005)
    with pytest.raises(BountyTargetError):
        await bounties.post(conn, 4005, 4005, AMOUNT, tick_index=0)
    with pytest.raises(BountyTargetError):
        await bounties.post(conn, 4005, 999_999, AMOUNT, tick_index=0)
    # A bot target: hand-craft the NPC user row.
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO users (id, is_bot) VALUES (4007, TRUE)"
        )
    with pytest.raises(BountyTargetError):
        await bounties.post(conn, 4005, 4007, AMOUNT, tick_index=0)


async def test_cap_on_open_bounties_per_target(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4010)
    target = 4011
    await bootstrap_user(conn, target)
    for i in range(3):
        await bootstrap_user(conn, 4012 + i)
        await _give_cash(conn, 4012 + i, AMOUNT + FEE)
        await bounties.post(conn, 4012 + i, target, AMOUNT, tick_index=0)
    await bootstrap_user(conn, 4016)
    await _give_cash(conn, 4016, AMOUNT + FEE)
    with pytest.raises(BountyTargetError):
        await bounties.post(conn, 4016, target, AMOUNT, tick_index=0)


async def test_cancel_refunds_amount_not_fee(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4020)
    await bootstrap_user(conn, 4021)
    bounty_id = await bounties.post(conn, 4020, 4021, AMOUNT, tick_index=0)

    await bounties.cancel(conn, bounty_id, 4020)

    # Fee stayed burned; the pot came back.
    assert await get_balance(
        conn, await get_user_account_id(conn, 4020)
    ) == STARTING_GRANT - FEE
    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "CANCELLED"


async def test_cancel_requires_poster(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4022)
    await bootstrap_user(conn, 4023)
    bounty_id = await bounties.post(conn, 4022, 4023, AMOUNT, tick_index=0)
    with pytest.raises(BountyStateError):
        await bounties.cancel(conn, bounty_id, 4023)


async def test_expiry_refunds_via_sweep(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4024)
    await bootstrap_user(conn, 4025)
    bounty_id = await bounties.post(conn, 4024, 4025, AMOUNT, tick_index=0)

    expired = await bounties.on_tick(conn, 10_080)  # past the week ttl

    assert expired == 1
    assert await get_balance(
        conn, await get_user_account_id(conn, 4024)
    ) == STARTING_GRANT - FEE
    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "EXPIRED"


async def test_liquidation_pays_largest_opposing_claimant(conn: AsyncConnection) -> None:
    """Target's short gets force-covered (BUY legs) -> the hunters are the
    LONGS; the largest aggregate opposing exposure collects the pot."""
    await bootstrap_user(conn, 4030)          # poster
    await bootstrap_user(conn, 4031)          # target (the short)
    await _give_cash(conn, 4030, AMOUNT + FEE)
    await _give_cash(conn, 4031, 500_000)
    await _grant_tier(conn, 4031)
    ticker = await _ticker(conn)
    await _levered_short(conn, 4031, ticker)

    # Two hunters go long: 4032 holds more than 4033 -> winner by exposure.
    for uid, qty in ((4032, 5), (4033, 2)):
        await bootstrap_user(conn, uid)
        await _give_cash(conn, uid, 500_000)
        await execute_trade(conn, user_id=uid, ticker=ticker, side="BUY", quantity=qty)

    bounty_id = await bounties.post(conn, 4030, 4031, AMOUNT, tick_index=0)
    claimant_before = await get_balance(conn, await get_user_account_id(conn, 4032))

    legs = await _liquidate(conn, 4031, ticker)
    assert legs >= 1

    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "CLAIMED"
    assert bounty.claimed_by == 4032
    rake = int(AMOUNT * 0.05)
    assert bounty.paid_minor == AMOUNT - rake
    assert await get_balance(
        conn, await get_user_account_id(conn, 4032)
    ) == claimant_before + AMOUNT - rake
    # The smaller claimant got nothing.
    assert await get_balance(
        conn, await get_user_account_id(conn, 4033)
    ) <= STARTING_GRANT + 500_000


async def test_liquidation_with_no_opposing_exposure_sinks_pot(
    conn: AsyncConnection,
) -> None:
    await bootstrap_user(conn, 4040)
    await bootstrap_user(conn, 4041)
    await _give_cash(conn, 4040, AMOUNT + FEE)
    await _give_cash(conn, 4041, 500_000)
    await _grant_tier(conn, 4041)
    ticker = await _ticker(conn)
    await _levered_short(conn, 4041, ticker)

    # Nobody else holds this ticker at all -> pot burns.
    sink_id = await get_system_account_id(conn, "SINK")
    sink0 = await get_balance(conn, sink_id)
    bounty_id = await bounties.post(conn, 4040, 4041, AMOUNT, tick_index=0)

    legs = await _liquidate(conn, 4041, ticker)
    assert legs >= 1

    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "CLAIMED"
    assert bounty.claimed_by is None and bounty.paid_minor == 0
    # sink0 predates the post: +FEE burned then +AMOUNT unclaimed pot.
    assert await get_balance(conn, sink_id) == sink0 + FEE + AMOUNT


async def test_league_liquidation_never_triggers_bounty(conn: AsyncConnection) -> None:
    """Force-liquidation inside a season is league-stake play, not a real
    head -- the bounty must stay OPEN."""
    from stockbot.seasons.service import (
        close_season,
        create_season,
        join_season,
        on_tick,
    )

    await bootstrap_user(conn, 4050)
    await bootstrap_user(conn, 4051)
    await _give_cash(conn, 4050, AMOUNT + FEE)
    ticker = await _ticker(conn)
    bounty_id = await bounties.post(conn, 4050, 4051, AMOUNT, tick_index=0)

    # League-scoped short + gap -> a league liquidation. Even if the
    # sweep ran it, the season guard keeps bounties closed.
    season_id = await create_season(
        conn, name="Test Season", start_tick=0, end_tick=10_000,
        entry_fee_minor=0, stake_minor=100_000,
    )
    await on_tick(conn, 0)
    await join_season(conn, 4051, season_id)
    assert await _season_entry(conn, 4051) == season_id
    await _grant_tier(conn, 4051)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, maint_margin_pct = 0.3 "
            "WHERE ticker = %s RETURNING quoted_price",
            (ticker,),
        )
        (price,) = await cur.fetchone()
    qty = max(1, int(Decimal(100_000) * Decimal("1.8") / (Decimal(price) * 100)))
    await execute_trade(
        conn, user_id=4051, ticker=ticker, side="SELL",
        quantity=qty, season_id=season_id,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 4, "
            "quoted_price = quoted_price * 4 WHERE ticker = %s",
            (ticker,),
        )
    await check_and_liquidate(conn, 4051, season_id=season_id)
    await close_season(conn, season_id)

    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "OPEN"


async def _season_entry(conn: AsyncConnection, user_id: int) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT season_id FROM season_entries WHERE user_id = %s", (user_id,)
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def test_bot_cannot_be_claimant(conn: AsyncConnection) -> None:
    """NPC users are excluded from the claimant pool even when positioned."""
    await bootstrap_user(conn, 4060)
    await bootstrap_user(conn, 4061)
    await _give_cash(conn, 4060, AMOUNT + FEE)
    await _give_cash(conn, 4061, 500_000)
    await _grant_tier(conn, 4061)
    ticker = await _ticker(conn)
    await _levered_short(conn, 4061, ticker)

    # A bot user longs the same ticker -- biggest exposure, can't claim.
    async with conn.cursor() as cur:
        await cur.execute("INSERT INTO users (id, is_bot) VALUES (4062, TRUE)")
        await cur.execute(
            "INSERT INTO accounts (kind, user_id, balance) "
            "VALUES ('USER', 4062, 0) RETURNING id"
        )
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO positions (user_id, instrument_id, quantity, avg_cost)
            SELECT 4062, id, 100, quoted_price FROM instruments WHERE ticker = %s
            """,
            (ticker,),
        )
    sink_id = await get_system_account_id(conn, "SINK")
    sink0 = await get_balance(conn, sink_id)
    bounty_id = await bounties.post(conn, 4060, 4061, AMOUNT, tick_index=0)

    legs = await _liquidate(conn, 4061, ticker)
    assert legs >= 1

    bounty = await bounties.get_bounty(conn, bounty_id)
    assert bounty is not None and bounty.status == "CLAIMED"
    assert bounty.claimed_by is None  # bot excluded -> unclaimed -> SINK
    # sink0 predates the post: +FEE burned then +AMOUNT unclaimed pot.
    assert await get_balance(conn, sink_id) == sink0 + FEE + AMOUNT
