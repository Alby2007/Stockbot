"""Phase N2: the status surface -- leaderboard, trade history, head-to-head
compare, and profile. The design doc's currency-value pillar ("leaderboards
and net worth flexing") had no Discord surface before this; `net_worth_by_user`
existed only in the sim harness.

Net worth for a main portfolio is cash + mark-to-market positions + bounded-
short liquidation value, minus accrued borrow fees/dividends -- the same
shape as `margin.compute_health`, but aggregated across every USER account
at once for the leaderboard instead of one account at a time. League
accounts (`season_id` set) and SYSTEM accounts are excluded by scoping to
`kind = 'USER'` (main accounts are the only USER-kind rows with
`season_id IS NULL`, per the 0009 CHECK).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.market.engine import TICKS_PER_DAY

# Reused verbatim in three queries below -- an f-string, never bare SQL
# text: the shared shape must actually get embedded, not shipped to
# Postgres as literal template syntax (the AGENTS.md gotcha that bit the
# league equity subquery). No alias here -- window functions and ORDER BY
# clauses need the raw expression, callers add "AS equity_minor" where a
# column alias is wanted.
_NET_WORTH_EXPR = """
    a.balance + COALESCE(pv.value_minor, 0) + COALESCE(bv.bshort_value, 0)
        - COALESCE(pv.accrued, 0) - COALESCE(pv.accrued_div, 0)
"""

_NET_WORTH_JOINS = """
    LEFT JOIN LATERAL (
        SELECT SUM(p.quantity * i.quoted_price * 100) AS value_minor,
               SUM(p.borrow_fees_accrued) AS accrued,
               SUM(p.dividends_accrued) AS accrued_div
        FROM positions p
        JOIN instruments i ON i.id = p.instrument_id
        WHERE p.user_id = a.user_id AND p.season_id IS NULL AND p.quantity <> 0
    ) pv ON TRUE
    LEFT JOIN LATERAL (
        SELECT SUM(GREATEST(0, bs.collateral_minor
                + CAST(bs.quantity * (bs.entry_price - i.quoted_price) * 100 AS BIGINT)))
               AS bshort_value
        FROM bounded_shorts bs
        JOIN instruments i ON i.id = bs.instrument_id
        WHERE bs.user_id = a.user_id AND bs.season_id IS NULL AND bs.status = 'OPEN'
    ) bv ON TRUE
"""


async def net_worth_minor(conn: AsyncConnection, user_id: int) -> int:
    """Live net worth for one user's main portfolio."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_NET_WORTH_EXPR} AS equity_minor
            FROM accounts a
            {_NET_WORTH_JOINS}
            WHERE a.kind = 'USER' AND a.user_id = %s
            """,
            (user_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def snapshot_net_worth_if_due(
    conn: AsyncConnection, tick_index: int, *, interval_ticks: int = TICKS_PER_DAY
) -> None:
    """Day-boundary equity snapshot for every USER account at once (one
    INSERT ... SELECT, not a per-user loop) -- mirrors seasons.on_tick's
    own day-boundary write. Feeds /compare and /profile's 24h change;
    called unconditionally every tick like seasons.on_tick, a no-op
    except on the day boundary."""
    if tick_index % interval_ticks != 0:
        return
    day_index = tick_index // interval_ticks
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            INSERT INTO net_worth_snapshots (user_id, day_index, tick_index, equity_minor)
            SELECT a.user_id, %s, %s, {_NET_WORTH_EXPR}
            FROM accounts a
            {_NET_WORTH_JOINS}
            WHERE a.kind = 'USER'
            ON CONFLICT (user_id, day_index) DO UPDATE
                SET equity_minor = EXCLUDED.equity_minor,
                    tick_index = EXCLUDED.tick_index
            """,
            (day_index, tick_index),
        )


async def evaluate_badges(
    conn: AsyncConnection, tick_index: int, *, interval_ticks: int = TICKS_PER_DAY
) -> int:
    """Day-boundary milestone grant -- one INSERT ... SELECT per metric
    class, not a per-user loop (same shape as snapshot_net_worth_if_due,
    called right after it in apply_tick's lifecycle block).

    Badge catalog rows carry {"metric": ..., "threshold_minor"|"threshold"}
    in shop_items.metadata; grants land in `entitlements` where the PK is
    the idempotency key (ON CONFLICT DO NOTHING -- re-running a day can
    never double-grant). Each NEW grant also enqueues a BADGE_EARNED
    outbox row so the DM names what was earned. Returns grants issued.
    """
    if tick_index % interval_ticks != 0:
        return 0
    granted: list[tuple[int, str]] = []
    async with conn.cursor() as cur:
        # Net worth: the exact aggregate the leaderboard ranks by.
        await cur.execute(
            f"""
            INSERT INTO entitlements (user_id, item_key)
            SELECT x.user_id, x.key FROM (
                SELECT a.user_id, s.key, {_NET_WORTH_EXPR} AS metric_value,
                       (s.metadata->>'threshold_minor')::bigint AS threshold
                FROM accounts a
                {_NET_WORTH_JOINS}
                JOIN shop_items s
                  ON s.kind = 'BADGE' AND s.metadata->>'metric' = 'net_worth'
                WHERE a.kind = 'USER'
            ) x WHERE x.metric_value >= x.threshold
            ON CONFLICT DO NOTHING
            RETURNING user_id, item_key
            """
        )
        granted += [(int(r[0]), str(r[1])) for r in await cur.fetchall()]
        # Lifetime traded notional (users.total_traded_minor, main+league).
        await cur.execute(
            """
            INSERT INTO entitlements (user_id, item_key)
            SELECT u.id, s.key
            FROM users u
            JOIN shop_items s
              ON s.kind = 'BADGE' AND s.metadata->>'metric' = 'volume'
            WHERE u.total_traded_minor >= (s.metadata->>'threshold_minor')::bigint
            ON CONFLICT DO NOTHING
            RETURNING user_id, item_key
            """
        )
        granted += [(int(r[0]), str(r[1])) for r in await cur.fetchall()]
        # Daily claim streak (claims.streak is current, not best-ever --
        # a broken streak waits for the next run of consecutive days).
        await cur.execute(
            """
            INSERT INTO entitlements (user_id, item_key)
            SELECT c.user_id, s.key
            FROM claims c
            JOIN shop_items s
              ON s.kind = 'BADGE' AND s.metadata->>'metric' = 'streak'
            WHERE c.streak >= (s.metadata->>'threshold')::int
            ON CONFLICT DO NOTHING
            RETURNING user_id, item_key
            """
        )
        granted += [(int(r[0]), str(r[1])) for r in await cur.fetchall()]
        # Lifetime quest completions (users.quests_completed, bumped by
        # the quest sweep on every reward).
        await cur.execute(
            """
            INSERT INTO entitlements (user_id, item_key)
            SELECT u.id, s.key
            FROM users u
            JOIN shop_items s
              ON s.kind = 'BADGE' AND s.metadata->>'metric' = 'quests'
            WHERE u.quests_completed >= (s.metadata->>'threshold')::int
            ON CONFLICT DO NOTHING
            RETURNING user_id, item_key
            """
        )
        granted += [(int(r[0]), str(r[1])) for r in await cur.fetchall()]
        for user_id, item_key in granted:
            await cur.execute(
                """
                INSERT INTO notifications (user_id, kind, payload)
                SELECT %s, 'BADGE_EARNED',
                       jsonb_build_object('tick_index', %s, 'item_key', %s::text,
                                          'name', s.name)
                FROM shop_items s WHERE s.key = %s
                """,
                (user_id, tick_index, item_key, item_key),
            )
    return len(granted)


async def day_change_pct(
    conn: AsyncConnection, user_id: int, current_equity_minor: int, tick_index: int
) -> float | None:
    """Change vs. the most recent snapshot strictly before today -- None
    if there isn't one yet (new user, or the market hasn't run a full day)."""
    day_index = tick_index // TICKS_PER_DAY
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT equity_minor FROM net_worth_snapshots
            WHERE user_id = %s AND day_index < %s
            ORDER BY day_index DESC LIMIT 1
            """,
            (user_id, day_index),
        )
        row = await cur.fetchone()
    if row is None or int(row[0]) <= 0:
        return None
    return current_equity_minor / int(row[0]) - 1


@dataclass(frozen=True)
class LeaderboardRow:
    user_id: int
    equity_minor: int
    rank: int
    total: int


async def leaderboard(conn: AsyncConnection) -> list[LeaderboardRow]:
    """Every main-economy USER account, ranked by net worth (ties broken
    by user_id for a stable, deterministic order). Whole-table
    aggregation -- fine at today's scale; if this ever gets hot,
    materialize a `leaderboard_snapshots` table refreshed per tick
    instead of recomputing it live on every /leaderboard call."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT a.user_id, {_NET_WORTH_EXPR} AS equity_minor,
                   ROW_NUMBER() OVER (
                       ORDER BY {_NET_WORTH_EXPR} DESC, a.user_id
                   ) AS rank,
                   COUNT(*) OVER () AS total
            FROM accounts a
            {_NET_WORTH_JOINS}
            WHERE a.kind = 'USER'
            ORDER BY rank
            """
        )
        rows = await cur.fetchall()
    return [
        LeaderboardRow(
            user_id=int(r["user_id"]),
            equity_minor=int(r["equity_minor"]) if r["equity_minor"] is not None else 0,
            rank=int(r["rank"]),
            total=int(r["total"]),
        )
        for r in rows
    ]


async def user_rank(conn: AsyncConnection, user_id: int) -> LeaderboardRow | None:
    """One user's row from the full ranking -- used by /profile so it
    doesn't have to render all N rows to answer "where do I stand"."""
    rows = await leaderboard(conn)
    for row in rows:
        if row.user_id == user_id:
            return row
    return None


async def recent_trades(
    conn: AsyncConnection, user_id: int, limit: int = 15
) -> list[dict[str, Any]]:
    """Caller's most recent main-portfolio fills (own trade rows across
    the price paths -- crosses, MM fallback, bounded shorts don't write
    to `trades` and aren't included)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT i.ticker, t.side, t.quantity, t.fill_price, t.notional_minor,
                   t.fee_minor, t.maker_taker, t.tick_index, t.created_at
            FROM trades t
            JOIN instruments i ON i.id = t.instrument_id
            WHERE t.user_id = %s AND t.season_id IS NULL
            ORDER BY t.id DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return await cur.fetchall()


@dataclass(frozen=True)
class ProfileStats:
    account_age_days: float
    net_worth_minor: int
    rank: int | None
    total_users: int
    lifetime_volume_minor: int
    quests_completed: int
    trophies: list[str]
    active_season_equity_minor: int | None


async def profile_stats(conn: AsyncConnection, user_id: int) -> ProfileStats | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT created_at, total_traded_minor, quests_completed "
            "FROM users WHERE id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    created_at, total_traded, quests_completed = row

    equity = await net_worth_minor(conn, user_id)
    rank_row = await user_rank(conn, user_id)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT s.name FROM entitlements e
            JOIN shop_items s ON s.key = e.item_key
            WHERE e.user_id = %s AND s.kind IN ('TROPHY', 'BADGE')
            ORDER BY e.item_key
            """,
            (user_id,),
        )
        trophies = [r[0] for r in await cur.fetchall()]

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT e.season_id FROM season_entries e
            JOIN seasons s ON s.id = e.season_id
            WHERE e.user_id = %s AND s.status = 'ACTIVE'
            """,
            (user_id,),
        )
        active_entry = await cur.fetchone()
    active_equity = None
    if active_entry is not None:
        # Mark-to-market league equity (cash + positions + shorts net of
        # accruals) -- the same number standings report, not raw cash.
        from stockbot.seasons.service import league_equity_minor

        active_equity = await league_equity_minor(
            conn, int(active_entry[0]), user_id
        )

    age_days = (datetime.now(UTC) - created_at).total_seconds() / 86400

    return ProfileStats(
        account_age_days=age_days,
        net_worth_minor=equity,
        rank=rank_row.rank if rank_row else None,
        total_users=rank_row.total if rank_row else 0,
        lifetime_volume_minor=int(total_traded),
        quests_completed=int(quests_completed),
        trophies=trophies,
        active_season_equity_minor=active_equity,
    )


@dataclass(frozen=True)
class CompareStats:
    user_id: int
    net_worth_minor: int
    day_change_pct: float | None
    position_count: int
    lifetime_volume_minor: int
    seasons_entered: int
    best_rank: int | None


async def compare_stats(
    conn: AsyncConnection, user_id: int, tick_index: int
) -> CompareStats:
    equity = await net_worth_minor(conn, user_id)
    change = await day_change_pct(conn, user_id, equity, tick_index)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = %s "
            "AND season_id IS NULL AND quantity <> 0",
            (user_id,),
        )
        count_row = await cur.fetchone()
        assert count_row is not None
        (position_count,) = count_row
        await cur.execute(
            "SELECT total_traded_minor FROM users WHERE id = %s", (user_id,)
        )
        vol_row = await cur.fetchone()
        await cur.execute(
            "SELECT COUNT(*), MIN(final_rank) FROM season_entries "
            "WHERE user_id = %s AND final_rank IS NOT NULL",
            (user_id,),
        )
        season_row = await cur.fetchone()
        assert season_row is not None
        seasons_entered, best_rank = season_row
    return CompareStats(
        user_id=user_id,
        net_worth_minor=equity,
        day_change_pct=change,
        position_count=int(position_count),
        lifetime_volume_minor=int(vol_row[0]) if vol_row else 0,
        seasons_entered=int(seasons_entered),
        best_rank=int(best_rank) if best_rank is not None else None,
    )
