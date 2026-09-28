"""Wash-trade anomaly detection.

Mitigation, not a solution, per the design doc: the impact-based fill model
already makes a same-account round trip lossy (see
`tests/test_ledger_invariant.py::test_round_trip_is_loss_making_once_a_fee_applies`).
The remaining vector is two *different* accounts trading opposite sides of
the same instrument in the same tick on illiquid stock -- classic wash
trading. This doesn't block it (that's not this job's call to make); it
flags it so an admin can look at the account pair.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row

LOW_LIQUIDITY_PERCENTILE = 0.25


@dataclass(frozen=True)
class WashTradeFlag:
    buy_trade_id: int
    sell_trade_id: int
    instrument_id: int
    buyer_id: int
    seller_id: int
    tick_index: int


async def scan_for_wash_trades(
    conn: AsyncConnection, lookback_ticks: int = 10
) -> list[WashTradeFlag]:
    """Flag (and persist) same-tick opposite-side trades between two
    different users on the bottom quartile of instruments by liquidity,
    within the last `lookback_ticks` ticks. Returns newly-flagged pairs
    (already-flagged pairs are skipped, so this is safe to call repeatedly).
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(tick_index), -1) FROM market_ticks")
            row = await cur.fetchone()
            assert row is not None
            current_tick = row[0]

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                WITH threshold AS (
                    SELECT percentile_cont(%s) WITHIN GROUP (ORDER BY liquidity) AS liq_cutoff
                    FROM instruments WHERE is_active
                )
                SELECT b.id AS buy_trade_id, s.id AS sell_trade_id, b.instrument_id,
                       b.user_id AS buyer_id, s.user_id AS seller_id, b.tick_index
                FROM trades b
                JOIN trades s
                    ON s.instrument_id = b.instrument_id
                   AND s.tick_index = b.tick_index
                   AND s.side = 'SELL'
                   AND s.user_id <> b.user_id
                JOIN instruments i ON i.id = b.instrument_id
                -- NPC-vs-NPC accidental MM pairs aren't wash trading:
                -- synthetics don't collude, and flagging them would
                -- produce admin noise with no actor to sanction (C4).
                JOIN users bu ON bu.id = b.user_id AND NOT bu.is_bot
                JOIN users su ON su.id = s.user_id AND NOT su.is_bot
                CROSS JOIN threshold
                WHERE b.side = 'BUY'
                  AND b.tick_index IS NOT NULL
                  AND b.tick_index >= %s
                  AND i.liquidity <= threshold.liq_cutoff
                  -- Book crosses look exactly like this pair (opposite
                  -- sides, same tick, two users) but are legitimate
                  -- matching: they carry counterparty_user_id. Only
                  -- MM-mediated fills (NULL counterparty) are suspicious.
                  AND b.counterparty_user_id IS NULL
                  AND s.counterparty_user_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM wash_trade_flags f
                      WHERE f.buy_trade_id = b.id AND f.sell_trade_id = s.id
                  )
                """,
                (LOW_LIQUIDITY_PERCENTILE, max(current_tick - lookback_ticks, 0)),
            )
            candidates = await cur.fetchall()

        flags = []
        for c in candidates:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO wash_trade_flags
                        (buy_trade_id, sell_trade_id, instrument_id,
                         buyer_id, seller_id, tick_index)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        c["buy_trade_id"],
                        c["sell_trade_id"],
                        c["instrument_id"],
                        c["buyer_id"],
                        c["seller_id"],
                        c["tick_index"],
                    ),
                )
            flags.append(WashTradeFlag(**c))

    return flags
