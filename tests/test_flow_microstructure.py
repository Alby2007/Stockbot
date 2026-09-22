"""Phase H: cross-impact sector sympathy (H2), permanent impact through
the fundamental (H3), and volume-responsive liquidity (H4).

Flow is injected straight into `pending_flow` -- the accumulator is the
production plumbing every fill path already writes, and injecting it
directly keeps these tests deterministic about magnitudes.
"""

from __future__ import annotations

import math

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.accounts.service import bootstrap_user
from stockbot.market import engine
from stockbot.market.tick import apply_tick

_SEED = "flow-test"


async def _set_config(conn: AsyncConnection, key: str, value: float) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = %s WHERE key = %s", (value, key))


async def _sector_pair(conn: AsyncConnection) -> tuple[dict, dict]:
    """Two non-index instruments in the same sector."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT i.id, i.ticker, i.sector_id, i.gamma, i.impact,
                   i.fundamental_value, i.base_price, i.quoted_price, i.liquidity
            FROM instruments i
            WHERE i.is_active AND i.kind != 'INDEX'
              AND i.sector_id IN (
                  SELECT sector_id FROM instruments
                  WHERE is_active AND kind != 'INDEX'
                  GROUP BY sector_id HAVING COUNT(*) >= 2)
            ORDER BY i.sector_id, i.id
            """,
        )
        rows = await cur.fetchall()
    assert len(rows) >= 2
    return rows[0], rows[1]


async def _other_sector_stock(conn: AsyncConnection, sector_id: int) -> dict:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, ticker, sector_id, gamma, impact, fundamental_value,
                   base_price, quoted_price, liquidity
            FROM instruments
            WHERE is_active AND kind != 'INDEX' AND sector_id != %s
            ORDER BY id LIMIT 1
            """,
            (sector_id,),
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


async def _impact(conn: AsyncConnection, instrument_id: int) -> float:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT impact FROM instruments WHERE id = %s", (instrument_id,)
        )
        return float((await cur.fetchone())[0])


async def _fundamental(conn: AsyncConnection, instrument_id: int) -> float:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT fundamental_value FROM instruments WHERE id = %s",
            (instrument_id,),
        )
        return float((await cur.fetchone())[0])


# --- H2: cross-impact -------------------------------------------------------


async def test_cross_impact_moves_sector_peers_one_hop(
    conn: AsyncConnection,
) -> None:
    """A big bounded buy-flow in A bleeds cross_coeff*gamma*flow into
    same-sector peers; an out-of-sector name is untouched; and the
    sympathy move is NOT re-recorded as flow (one hop)."""
    a, b = await _sector_pair(conn)
    c = await _other_sector_stock(conn, a["sector_id"])
    # Zero the peers' impact so post-tick impact IS the cross-inflow
    # (decay of 0 is 0).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET impact = 0 WHERE id IN (%s, %s)",
            (b["id"], c["id"]),
        )
    bounded = 0.02  # single account clips at account_flow_cap
    await _inject_flow(conn, a["id"], 9101, bounded)
    await apply_tick(conn, _SEED)

    expected = (
        0.15  # flow.cross_impact_coeff seed
        * float(b["gamma"])
        * bounded
    )
    got = await _impact(conn, b["id"])
    assert got == pytest.approx(expected, rel=0.05)
    assert await _impact(conn, c["id"]) == pytest.approx(0.0, abs=1e-9)
    # One hop: nothing re-entered the accumulator for the next tick.
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM pending_flow")
        assert (await cur.fetchone())[0] == 0


async def test_cross_impact_skips_halted_peers(conn: AsyncConnection) -> None:
    """A halted peer's mark is frozen -- it receives no sympathy move."""
    a, b = await _sector_pair(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET impact = 0, circuit_halted_until_tick = 999999"
            " WHERE id = %s",
            (b["id"],),
        )
    await _inject_flow(conn, a["id"], 9101, 0.02)
    await apply_tick(conn, _SEED)
    assert await _impact(conn, b["id"]) == pytest.approx(0.0, abs=1e-9)


# --- H3: permanent impact via the fundamental -------------------------------


async def test_sustained_flow_moves_fundamental_and_unwinds_impact(
    conn: AsyncConnection,
) -> None:
    """perm_frac of own bounded flow transfers into F_i (log space) and
    out of impact -- the mark only dips by `shift`, the move becomes
    permanent instead of decaying away."""
    a, _ = await _sector_pair(conn)
    async with conn.cursor() as cur:
        # Zero A's impact (decay of 0 is 0) and its fundamental's own
        # stochastic walk so the transfer is exact.
        await cur.execute(
            "UPDATE instruments SET impact = 0, fundamental_sigma = 0"
            " WHERE id = %s",
            (a["id"],),
        )
    own = 0.02
    f0 = await _fundamental(conn, a["id"])
    await _inject_flow(conn, a["id"], 9101, own)
    await apply_tick(conn, _SEED)

    shift = 0.10 * own  # flow.permanent_frac seed
    f1 = await _fundamental(conn, a["id"])
    assert f1 / f0 == pytest.approx(math.exp(shift), rel=1e-6)
    # The transferred amount comes OUT of impact: injected flow carries no
    # pre-existing impact move, so A's post-tick impact is exactly -shift.
    impact = await _impact(conn, a["id"])
    assert impact == pytest.approx(-shift, abs=1e-9)


async def test_fundamental_move_is_hard_capped_per_tick(
    conn: AsyncConnection,
) -> None:
    """A funded group hitting the flow cap every tick still can't walk a
    fundamental faster than flow.max_fundamental_move per tick."""
    a, _ = await _sector_pair(conn)
    await _set_config(conn, "flow.max_fundamental_move", 0.001)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET fundamental_sigma = 0 WHERE id = %s",
            (a["id"],),
        )
    f0 = await _fundamental(conn, a["id"])
    # Several accounts so the bounded flow reaches flow_ret_cap (0.05) --
    # the raw flow would shift F by 0.005 without the cap.
    for uid in (9101, 9102, 9103, 9104):
        await _inject_flow(conn, a["id"], uid, 0.02)
    await apply_tick(conn, _SEED)
    f1 = await _fundamental(conn, a["id"])
    assert f1 / f0 == pytest.approx(math.exp(0.001), rel=1e-6)


# --- H4: volume-responsive liquidity ----------------------------------------


def test_effective_liquidity_clamps() -> None:
    cfg = {
        "flow.adv_mult_min": 0.5,
        "flow.adv_mult_max": 2.0,
        "flow.adv_ref_frac": 2.5e-8,
    }
    liq = 5_000_000.0
    # Dead tape -> floor (impact doubles).
    assert engine.effective_liquidity(liq, 0.0, cfg) == liq * 0.5
    # At the reference ratio -> multiplier 1.
    ref_adv = liq * 2.5e-8
    assert engine.effective_liquidity(liq, ref_adv, cfg) == pytest.approx(liq)
    # Frenzied tape -> ceiling.
    assert engine.effective_liquidity(liq, ref_adv * 10, cfg) == liq * 2.0


async def test_adv_shrinks_recorded_impact_delta(conn: AsyncConnection) -> None:
    """Same trade on a high-ADV tape moves the mark less -- and the
    recorded impact_delta proves it came through the liquidity term."""
    from stockbot.market.data import participation_cap
    from stockbot.trading.service import execute_trade

    await bootstrap_user(conn, 9105)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 100000000000 WHERE user_id = 9105"
        )
    a, _ = await _sector_pair(conn)
    cap = await participation_cap(conn)
    # The participation cap now binds on EFFECTIVE liquidity (Plan A):
    # pin adv to the reference ratio so adv_mult lands at exactly 1 and
    # the sizing below stays inside the cap.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET adv = liquidity * 2.5e-8 WHERE id = %s",
            (a["id"],),
        )
    qty = max(1, int(cap * float(a["liquidity"]) * 0.5 / float(a["quoted_price"])))

    await execute_trade(
        conn, user_id=9105, ticker=a["ticker"], side="BUY", quantity=qty
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT impact, adv FROM instruments WHERE id = %s", (a["id"],)
        )
        impact1, _ = await cur.fetchone()
        await cur.execute(
            "SELECT impact_delta FROM trades ORDER BY id DESC LIMIT 1"
        )
        (d1,) = await cur.fetchone()
        # Frenzied tape: adv far above ref -> liquidity doubles.
        await cur.execute(
            "UPDATE instruments SET impact = %s, adv = liquidity * 0.001"
            " WHERE id = %s",
            (impact1, a["id"]),
        )
    await execute_trade(
        conn, user_id=9105, ticker=a["ticker"], side="BUY", quantity=qty
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT impact FROM instruments WHERE id = %s", (a["id"],)
        )
        (impact2,) = await cur.fetchone()
        await cur.execute(
            "SELECT impact_delta FROM trades ORDER BY id DESC LIMIT 1"
        )
        (d2,) = await cur.fetchone()
    assert float(d2) < float(d1) * 0.9  # sqrt-law: 2x liquidity -> ~0.71x
    assert impact2 > impact1  # sanity: second buy still moved the mark up
