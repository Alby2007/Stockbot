"""Read-only market data queries shared by several slash commands.

Kept separate from `market/tick.py` (which only the tick engine writes
through) so command handlers have one obvious place to read consistent,
already-joined instrument state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from psycopg import AsyncConnection

from stockbot.market.engine import TICKS_PER_DAY


@dataclass(frozen=True)
class InstrumentSnapshot:
    id: int
    ticker: str
    name: str
    sector_key: str
    sector_name: str
    quoted_price: Decimal
    impact: Decimal
    halted_until_tick: int | None
    day_ago_close: Decimal | None
    short_interest_pct: Decimal = Decimal(0)

    @property
    def day_change_pct(self) -> float | None:
        if not self.day_ago_close:
            return None
        return float(self.quoted_price) / float(self.day_ago_close) - 1

    @property
    def is_halted(self) -> bool:
        return self.halted_until_tick is not None


async def current_tick_index(conn: AsyncConnection) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
        row = await cur.fetchone()
        return row[0] if row is not None else None


async def all_instrument_snapshots(conn: AsyncConnection) -> list[InstrumentSnapshot]:
    """One row per active instrument, plus its close price from ~1 day ago
    (or the earliest candle available, if the market hasn't run a full day
    yet) for computing a 24h % change.
    """
    current_tick = await current_tick_index(conn)
    day_ago_tick = max((current_tick or 0) - TICKS_PER_DAY, 0)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT i.id, i.ticker, i.name, s.key, s.name, i.quoted_price, i.impact,
                   i.circuit_halted_until_tick, c.close, i.short_interest_pct
            FROM instruments i
            JOIN sectors s ON s.id = i.sector_id
            LEFT JOIN LATERAL (
                SELECT close FROM candles
                WHERE candles.instrument_id = i.id AND candles.tick_index <= %s
                ORDER BY candles.tick_index DESC
                LIMIT 1
            ) c ON TRUE
            WHERE i.is_active
            ORDER BY i.ticker
            """,
            (day_ago_tick,),
        )
        rows = await cur.fetchall()

    return [
        InstrumentSnapshot(
            id=r[0],
            ticker=r[1],
            name=r[2],
            sector_key=r[3],
            sector_name=r[4],
            quoted_price=r[5],
            impact=r[6],
            halted_until_tick=r[7],
            day_ago_close=r[8],
            short_interest_pct=r[9],
        )
        for r in rows
    ]


async def get_instrument_snapshot(conn: AsyncConnection, ticker: str) -> InstrumentSnapshot | None:
    ticker = ticker.upper()
    for snapshot in await all_instrument_snapshots(conn):
        if snapshot.ticker == ticker:
            return snapshot
    return None
