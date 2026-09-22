"""Plan A: flow-responsive market making.

Vol-coupled effective liquidity (`flow.vol_liq_coeff` on
`engine.effective_liquidity`, with the participation cap now computed on
the effective value) and adverse-selection flow skew (`instruments.
flow_skew` EWMA maintained by apply_tick; `engine.flow_skew_mult` widens
the crowded side's half-spread and discounts the contra side).
"""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.market import engine
from stockbot.market.data import book_depth
from stockbot.market.tick import apply_tick
from stockbot.trading.errors import InsufficientDepthError
from stockbot.trading.service import execute_trade

_SEED = "flow-mm-test"


async def _set_config(conn: AsyncConnection, key: str, value: float) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = %s WHERE key = %s", (value, key))


async def _instrument(conn: AsyncConnection) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, ticker, liquidity, quoted_price
            FROM instruments
            WHERE is_active AND kind != 'INDEX'
            ORDER BY id LIMIT 1
            """
        )
        row = await cur.fetchone()
    assert row is not None
    return row


async def _inject_flow(
    conn: AsyncConnection, instrument_id: int, user_id: int, delta: float
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO pending_flow
                (instrument_id, user_id, tick_index, delta_impact,
                 signed_notional_minor)
            VALUES (%s, %s, 0, %s, 1000000)
            ON CONFLICT (instrument_id, user_id) DO UPDATE SET
                delta_impact = pending_flow.delta_impact + EXCLUDED.delta_impact
            """,
            (instrument_id, user_id, delta),
        )


async def _flow_skew(conn: AsyncConnection, instrument_id: int) -> float:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT flow_skew FROM instruments WHERE id = %s", (instrument_id,)
        )
        return float((await cur.fetchone())[0])


async def _set_flow_state(
    conn: AsyncConnection,
    instrument_id: int,
    *,
    flow_skew: float = 0.0,
    vol_state: float = 1.0,
    adv_ratio: float | None = None,
) -> None:
    """Directly set the MM-relevant state columns. `adv_ratio` is expressed
    in units of `flow.adv_ref_frac` so adv=liquidity*ref*ratio lands the
    ADV multiplier at exactly `ratio` (pre-clip)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE instruments
            SET flow_skew = %s, vol_state = %s,
                adv = CASE WHEN %s::float IS NULL THEN adv
                           ELSE liquidity * 2.5e-8 * %s::float END
            WHERE id = %s
            """,
            (flow_skew, vol_state, adv_ratio, adv_ratio, instrument_id),
        )


# --- unit: effective_liquidity vol coupling ---------------------------------


def test_effective_liquidity_vol_state_coupling() -> None:
    cfg = {
        "flow.adv_mult_min": 0.5,
        "flow.adv_mult_max": 2.0,
        "flow.adv_ref_frac": 2.5e-8,
        "flow.vol_liq_coeff": 1.0,
    }
    liq = 5_000_000.0
    ref_adv = liq * 2.5e-8  # ADV multiplier exactly 1
    # vol_state 1 (or below): unchanged -- calm tapes don't deepen.
    assert engine.effective_liquidity(liq, ref_adv, cfg, 1.0) == pytest.approx(liq)
    assert engine.effective_liquidity(liq, ref_adv, cfg, 0.5) == pytest.approx(liq)
    # Storm: liquidity thins by 1/vol_state.
    assert engine.effective_liquidity(liq, ref_adv, cfg, 4.0) == pytest.approx(
        liq / 4.0
    )
    # The knob: exponent 0 disables the coupling entirely.
    assert engine.effective_liquidity(
        liq, ref_adv, {**cfg, "flow.vol_liq_coeff": 0.0}, 4.0
    ) == pytest.approx(liq)
    # Fractional coefficient softens it: sqrt(1/4) = 1/2.
    assert engine.effective_liquidity(
        liq, ref_adv, {**cfg, "flow.vol_liq_coeff": 0.5}, 4.0
    ) == pytest.approx(liq / 2.0)
    # Backward compatible: omitted vol_state behaves as vol_state = 1.
    assert engine.effective_liquidity(liq, ref_adv, cfg) == pytest.approx(liq)


# --- unit: flow_skew_mult ----------------------------------------------------


def test_flow_skew_mult_sides_and_clips() -> None:
    cfg = {"flow.skew_coeff": 0.5, "flow.skew_norm": 0.05, "flow.skew_max": 3.0}
    # Quiet tape: symmetric, no adjustment.
    assert engine.flow_skew_mult(1_000.0, 0.0, cfg) == 1.0
    assert engine.flow_skew_mult(-1_000.0, 0.0, cfg) == 1.0
    # Buy-pressure tape (skew = +norm): a buy pays 1 + 0.5*1 = 1.5x,
    # a sell is discounted to 0.5x.
    assert engine.flow_skew_mult(1_000.0, 0.05, cfg) == pytest.approx(1.5)
    assert engine.flow_skew_mult(-1_000.0, 0.05, cfg) == pytest.approx(0.5)
    # Sell-pressure tape mirrors.
    assert engine.flow_skew_mult(1_000.0, -0.05, cfg) == pytest.approx(0.5)
    assert engine.flow_skew_mult(-1_000.0, -0.05, cfg) == pytest.approx(1.5)
    # Saturation clips at skew_max; the contra side floors at 0.
    assert engine.flow_skew_mult(1_000.0, 1.0, cfg) == 3.0
    assert engine.flow_skew_mult(-1_000.0, 1.0, cfg) == 0.0
    # skew_norm 0 disables normalization rather than dividing by zero.
    assert engine.flow_skew_mult(1_000.0, 0.05, {**cfg, "flow.skew_norm": 0.0}) == 1.0


# --- tick: flow_skew EWMA ----------------------------------------------------


async def test_flow_skew_ewma_tracks_own_flow_and_decays(
    conn: AsyncConnection,
) -> None:
    """Buy flow leaves a positive skew, sell flow a negative one, and a
    quiet tape decays it toward zero (decay 0.9, innovation 0.1)."""
    inst = await _instrument(conn)

    await _inject_flow(conn, inst["id"], 9201, 0.02)
    await apply_tick(conn, _SEED)
    skew = await _flow_skew(conn, inst["id"])
    assert skew == pytest.approx(0.002, rel=1e-3)

    # Sell pressure flips the sign: 0.9*0.002 + 0.1*(-0.02) = -0.0002.
    await _inject_flow(conn, inst["id"], 9202, -0.02)
    await apply_tick(conn, _SEED)
    skew = await _flow_skew(conn, inst["id"])
    assert skew < 0

    # Two quiet ticks: pure decay.
    await apply_tick(conn, _SEED)
    s1 = await _flow_skew(conn, inst["id"])
    await apply_tick(conn, _SEED)
    s2 = await _flow_skew(conn, inst["id"])
    assert s2 == pytest.approx(0.9 * s1, rel=1e-3)
    assert abs(s2) < abs(skew)


async def test_flow_skew_uses_own_flow_not_sector_sympathy(
    conn: AsyncConnection,
) -> None:
    """Cross-impact moves the mark but is NOT this instrument's tape --
    a peer's flow must not skew the MM's quote here."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT i.id, i.ticker
            FROM instruments i
            WHERE i.is_active AND i.kind != 'INDEX'
              AND i.sector_id IN (
                  SELECT sector_id FROM instruments
                  WHERE is_active AND kind != 'INDEX'
                  GROUP BY sector_id HAVING COUNT(*) >= 2)
            ORDER BY i.sector_id, i.id
            """
        )
        rows = await cur.fetchall()
    assert len(rows) >= 2
    a, b = rows[0], rows[1]

    await _inject_flow(conn, a["id"], 9203, 0.02)
    await apply_tick(conn, _SEED)
    assert await _flow_skew(conn, a["id"]) > 0
    assert await _flow_skew(conn, b["id"]) == pytest.approx(0.0, abs=1e-12)


# --- fills: skew on the half-spread ------------------------------------------


async def test_crowded_side_pays_wider_half_spread(
    conn: AsyncConnection,
) -> None:
    """trades.half_spread records the skewed spread: with the tape at
    +skew_norm the buy pays 1.5x the unskewed half-spread."""
    await bootstrap_user(conn, 9204)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 100000000000 WHERE user_id = 9204"
        )
    inst = await _instrument(conn)
    qty = 1

    await execute_trade(
        conn, user_id=9204, ticker=inst["ticker"], side="BUY", quantity=qty
    )
    async with conn.cursor() as cur:
        await cur.execute("SELECT half_spread FROM trades ORDER BY id DESC LIMIT 1")
        (h0,) = await cur.fetchone()

    await _set_flow_state(conn, inst["id"], flow_skew=0.05)  # = skew_norm
    await execute_trade(
        conn, user_id=9204, ticker=inst["ticker"], side="BUY", quantity=qty
    )
    async with conn.cursor() as cur:
        await cur.execute("SELECT half_spread FROM trades ORDER BY id DESC LIMIT 1")
        (h1,) = await cur.fetchone()
    assert float(h1) == pytest.approx(1.5 * float(h0), rel=1e-3)

    # The contra side is discounted: a sell pays 0.5x.
    await execute_trade(
        conn, user_id=9204, ticker=inst["ticker"], side="SELL", quantity=qty
    )
    async with conn.cursor() as cur:
        await cur.execute("SELECT half_spread FROM trades ORDER BY id DESC LIMIT 1")
        (h2,) = await cur.fetchone()
    assert float(h2) == pytest.approx(0.5 * float(h0), rel=1e-3)


# --- fills: participation cap on effective liquidity -------------------------


async def test_participation_cap_shrinks_in_high_vol(
    conn: AsyncConnection,
) -> None:
    """A storm-thinned book rejects a fill that fits under the calm-tape
    cap: cap = participation_cap * liquidity * adv_mult / vol_state."""
    await bootstrap_user(conn, 9205)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 100000000000 WHERE user_id = 9205"
        )
    inst = await _instrument(conn)
    # adv at the reference ratio -> adv multiplier exactly 1, so
    # effective liquidity = liquidity / vol_state.
    await _set_flow_state(conn, inst["id"], adv_ratio=1.0)

    liq = float(inst["liquidity"])
    price = float(inst["quoted_price"])
    # ~5% of static liquidity: under the 10% cap when vol_state is calm,
    # 5x over it at vol_state = 10.
    qty = max(1, int(0.05 * liq / price))

    await execute_trade(
        conn, user_id=9205, ticker=inst["ticker"], side="BUY", quantity=qty
    )

    await _set_flow_state(conn, inst["id"], vol_state=10.0)
    with pytest.raises(InsufficientDepthError):
        await execute_trade(
            conn, user_id=9205, ticker=inst["ticker"], side="BUY", quantity=qty
        )


async def test_book_depth_mm_rungs_thin_in_storm(conn: AsyncConnection) -> None:
    """Displayed MM depth is the effective per-tick budget -- a vol spike
    shrinks the synthetic rungs, not just the fill cap."""
    # Most-liquid name: keep mm_qty well above the max(1, ...) floor so the
    # shrink is measurable rather than clamped.
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, ticker, liquidity, quoted_price
            FROM instruments
            WHERE is_active AND kind != 'INDEX'
            ORDER BY liquidity DESC LIMIT 1
            """
        )
        inst = await cur.fetchone()
    assert inst is not None
    await _set_flow_state(conn, inst["id"], adv_ratio=1.0, vol_state=1.0)
    bids_calm, _ = await book_depth(conn, inst["id"])
    calm_qty = sum(lvl.quantity for lvl in bids_calm if lvl.synthetic)

    await _set_flow_state(conn, inst["id"], adv_ratio=1.0, vol_state=8.0)
    bids_storm, _ = await book_depth(conn, inst["id"])
    storm_qty = sum(lvl.quantity for lvl in bids_storm if lvl.synthetic)

    assert calm_qty > 0
    assert storm_qty > 0
    assert storm_qty <= calm_qty // 4  # 8x thinner, minus int() rounding
