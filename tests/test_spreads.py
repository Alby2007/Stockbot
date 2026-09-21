"""Phase A: dynamic half-spread shape + utilization-scaled borrow fees."""

from __future__ import annotations

from decimal import Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import get_system_account_id, post_transfer
from stockbot.margin.service import accrue_borrow_fees
from stockbot.market.data import half_spread_for, spread_config
from stockbot.market.engine import half_spread_fraction

_CFG = {
    "spread.base_bps": 5.0,
    "spread.sigma_coeff": 1.0,
    "spread.inv_liquidity_coeff": 0.5,
    "spread.halt_coeff": 3.0,
    "spread.event_coeff": 1.0,
    "spread.sigma_ref": 0.0003,
    "spread.liquidity_ref": 3_000_000.0,
    "spread.halt_decay_ticks": 60.0,
    "spread.event_window_ticks": 360.0,
}


def _spread(**kw: float | None) -> float:
    base = {
        "cfg": _CFG,
        "sigma": 0.0003,
        "liquidity": 3_000_000.0,
        "ticks_since_halt": None,
        "ticks_to_event": None,
    }
    base.update(kw)  # type: ignore[arg-type]
    return half_spread_fraction(**base)  # type: ignore[arg-type]


def test_spread_widens_with_sigma() -> None:
    assert _spread(sigma=0.0009) > _spread(sigma=0.0003) > _spread(sigma=0.0001)


def test_spread_widens_when_illiquid() -> None:
    assert _spread(liquidity=300_000.0) > _spread(liquidity=3_000_000.0)


def test_spread_elevated_after_halt_and_decays() -> None:
    just_after = _spread(ticks_since_halt=1.0)
    much_later = _spread(ticks_since_halt=600.0)
    never = _spread()
    assert just_after > much_later > never


def test_spread_elevated_near_event() -> None:
    imminent = _spread(ticks_to_event=10.0)
    far = _spread(ticks_to_event=2000.0)
    none = _spread()
    assert imminent > far > none


async def test_half_spread_for_uses_row_and_tick(conn: AsyncConnection) -> None:
    row = {
        "sigma": 0.0003,
        "liquidity": 3_000_000.0,
        "last_halt_end_tick": 90,
        "next_event_tick": 130,
    }
    cfg = await spread_config(conn)
    near = half_spread_for(row, 100, cfg)
    row_far = {**row, "last_halt_end_tick": None, "next_event_tick": None}
    plain = half_spread_for(row_far, 100, cfg)
    assert near > plain > 0
    # no ticks applied yet -> deltas are None -> plain base spread
    assert half_spread_for(row_far, None, cfg) == plain


async def test_borrow_fee_scales_with_utilization(conn: AsyncConnection) -> None:
    """Same short on two instruments: the crowded one accrues more fee."""
    await bootstrap_user(conn, 4001)
    account_id = 0  # resolved below
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM accounts WHERE user_id = %s AND kind = 'USER'", (4001,)
        )
        row = await cur.fetchone()
        assert row is not None
        account_id = int(row[0])
    faucet = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet, to_account_id=account_id,
        amount=10_000_00, reason="TEST",
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE kind != 'INDEX' "
            "AND is_active ORDER BY id LIMIT 2"
        )
        insts = await cur.fetchall()
        assert len(insts) == 2
        crowded_id, quiet_id = int(insts[0][0]), int(insts[1][0])
        # identical shorts (qty -1) on both; crank one's short_interest_pct
        for iid in (crowded_id, quiet_id):
            await cur.execute(
                "INSERT INTO positions (user_id, instrument_id, quantity, avg_cost) "
                "VALUES (%s, %s, -1, 100) ON CONFLICT DO NOTHING",
                (4001, iid),
            )
        await cur.execute(
            "UPDATE instruments SET short_interest_pct = 0.29 WHERE id = %s",
            (crowded_id,),
        )
    await accrue_borrow_fees(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT borrow_fees_accrued FROM positions WHERE instrument_id = %s",
            (crowded_id,),
        )
        crowded_fee = Decimal((await cur.fetchone())[0])
        await cur.execute(
            "SELECT borrow_fees_accrued FROM positions WHERE instrument_id = %s",
            (quiet_id,),
        )
        quiet_fee = Decimal((await cur.fetchone())[0])
    # crowded SI ~0.29 vs max 0.30 -> multiplier 1 + 4*(0.29/0.3)^2 ~ 4.74x
    assert crowded_fee > quiet_fee * 3


async def test_next_event_tick_and_last_halt_end_tracked(
    conn: AsyncConnection,
) -> None:
    from stockbot.market.tick import apply_tick

    await apply_tick(conn, "spread-test-seed")
    async with conn.cursor() as cur:
        # earnings get scheduled on first tick -> next_event_tick set
        await cur.execute(
            "SELECT COUNT(*) FROM instruments WHERE next_event_tick IS NOT NULL"
        )
        assert int((await cur.fetchone())[0]) > 0
        # halt an instrument artificially and verify the spread helper sees it
        await cur.execute(
            "UPDATE instruments SET last_halt_end_tick = 0 WHERE id = "
            "(SELECT id FROM instruments ORDER BY id LIMIT 1) RETURNING id"
        )
        row = await cur.fetchone()
        assert row is not None
        iid = int(row[0])
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sigma, liquidity, last_halt_end_tick, next_event_tick "
            "FROM instruments WHERE id = %s",
            (iid,),
        )
        inst = await cur.fetchone()
        assert inst is not None
    cfg = await spread_config(conn)
    tick = 5
    assert half_spread_for(inst, tick, cfg) > half_spread_for(
        {**inst, "last_halt_end_tick": None}, tick, cfg
    )
