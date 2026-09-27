"""Daily/weekly quests: rotating tasks measured from tick-stamped tables.

Quests are a read-side system -- no action site is instrumented. A
day-boundary `on_tick` rotates in a deterministic shared set of
`quest_instances` (everyone sees the same daily board), and a per-tick
`sweep_completions` finds finishers with one grouped INSERT per kind,
pays FAUCET rewards through `post_transfer`, and enqueues
QUEST_COMPLETED outbox rows -- all inside the calling transaction.

League and main activity both count (quests reward playing), but rewards
always pay the main account. `quests.enabled` is the kill switch.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import get_system_account_id, get_user_account_id, post_transfer
from stockbot.market.engine import TICKS_PER_DAY

# kind -> (source table, window column, measure expression, extra JOIN
# condition). All measure queries share one shape: join the source rows
# inside [window_start, window_end], group by user, HAVING >= target.
# COUNTs run on the window column (never a bare *) so the same expr
# works under both the sweep's inner join and list_quests' LEFT JOIN --
# and every counted source column is NULL-safe: a NULL tick never
# satisfies BETWEEN, so it never joins.
_MEASURES: dict[str, tuple[str, str, str, str]] = {
    "TRADE_COUNT": ("trades", "tick_index", "COUNT(t.tick_index)", ""),
    "DISTINCT_TICKERS": (
        "trades",
        "tick_index",
        "COUNT(DISTINCT t.instrument_id)",
        "",
    ),
    "TRADE_VOLUME": (
        "trades",
        "tick_index",
        "COALESCE(SUM(t.notional_minor), 0)",
        "",
    ),
    "ORDER_PLACED": ("orders", "opened_tick", "COUNT(t.opened_tick)", ""),
    "ORDER_FILLED": ("orders", "filled_tick", "COUNT(t.filled_tick)", ""),
    "SHORT_PROFIT": (
        "bounded_shorts",
        "closed_tick",
        "COUNT(t.closed_tick)",
        "AND t.payout_minor > t.collateral_minor",
    ),
    "ALERT_SET": ("price_alerts", "created_tick", "COUNT(t.created_tick)", ""),
    "IPO_SUBSCRIBE": (
        "ipo_subscriptions",
        "created_tick",
        "COUNT(t.created_tick)",
        "",
    ),
}


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _enabled(conn: AsyncConnection) -> bool:
    return await _config_float(conn, "quests.enabled", 1.0) != 0.0


async def on_tick(conn: AsyncConnection, tick_index: int) -> int:
    """Day-boundary housekeeping + rotation. Returns instances created.

    Runs at `tick_index % TICKS_PER_DAY == 0` (both phases -- rotation
    can land on a closed tick). The pick is deterministic
    (`hashtext(key || ':' || day_index)`) so every user sees the same
    board. IPO_SUBSCRIBE is skipped when no offering is open -- never
    hand out an impossible quest.
    """
    if tick_index % TICKS_PER_DAY != 0:
        return 0
    if not await _enabled(conn):
        return 0
    day_index = tick_index // TICKS_PER_DAY
    daily_n = int(await _config_float(conn, "quests.daily_count", 3))
    weekly_n = int(await _config_float(conn, "quests.weekly_count", 2))
    created = 0
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE quest_instances SET status = 'EXPIRED' "
            "WHERE status = 'OPEN' AND window_end < %s",
            (tick_index,),
        )
        await cur.execute(
            """
            INSERT INTO quest_instances
                (def_key, kind, period, period_index, window_start,
                 window_end, target, reward_minor)
            SELECT d.key, d.kind, 'DAILY', %s, %s, %s, d.target, d.reward_minor
            FROM quest_defs d
            WHERE d.active AND d.period = 'DAILY'
              AND (d.kind <> 'IPO_SUBSCRIBE' OR EXISTS
                   (SELECT 1 FROM ipo_offerings WHERE status = 'OPEN'))
            ORDER BY hashtext(d.key || ':' || %s::text)
            LIMIT %s
            ON CONFLICT DO NOTHING
            """,
            (
                day_index,
                tick_index,
                tick_index + TICKS_PER_DAY - 1,
                str(day_index),
                daily_n,
            ),
        )
        created += cur.rowcount
        if day_index % 7 == 0:
            await cur.execute(
                """
                INSERT INTO quest_instances
                    (def_key, kind, period, period_index, window_start,
                     window_end, target, reward_minor)
                SELECT d.key, d.kind, 'WEEKLY', %s, %s, %s,
                       d.target, d.reward_minor
                FROM quest_defs d
                WHERE d.active AND d.period = 'WEEKLY'
                  AND (d.kind <> 'IPO_SUBSCRIBE' OR EXISTS
                       (SELECT 1 FROM ipo_offerings WHERE status = 'OPEN'))
                ORDER BY hashtext(d.key || ':' || %s::text)
                LIMIT %s
                ON CONFLICT DO NOTHING
                """,
                (
                    day_index // 7,
                    tick_index,
                    tick_index + 7 * TICKS_PER_DAY - 1,
                    str(day_index // 7),
                    weekly_n,
                ),
            )
            created += cur.rowcount
    return created


async def sweep_completions(conn: AsyncConnection, tick_index: int) -> int:
    """Pay out every OPEN instance whose window the user has satisfied.

    One grouped INSERT ... ON CONFLICT DO NOTHING per present kind -- the
    (quest_instance_id, user_id) PK is the pay-once idempotency key, so a
    re-run can never double-pay. Each NEW completion posts a FAUCET
    transfer to the user's main account, bumps quests_completed, and
    enqueues a QUEST_COMPLETED outbox row. Returns completions paid.
    """
    if not await _enabled(conn):
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT DISTINCT kind FROM quest_instances WHERE status = 'OPEN'"
        )
        kinds = [str(r[0]) for r in await cur.fetchall()]

    faucet_id: int | None = None
    paid = 0
    for kind in kinds:
        table, col, measure, extra = _MEASURES[kind]
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH done AS (
                    INSERT INTO quest_completions
                        (quest_instance_id, user_id, completed_tick,
                         reward_minor)
                    SELECT qi.id, t.user_id, %s, qi.reward_minor
                    FROM quest_instances qi
                    JOIN {table} t
                      ON t.{col} BETWEEN qi.window_start AND qi.window_end
                     {extra}
                    WHERE qi.status = 'OPEN' AND qi.kind = %s
                    GROUP BY qi.id, t.user_id
                    HAVING {measure} >= qi.target
                    ON CONFLICT DO NOTHING
                    RETURNING quest_instance_id, user_id, reward_minor
                )
                SELECT done.quest_instance_id, done.user_id,
                       done.reward_minor, qd.name
                FROM done
                JOIN quest_instances qi ON qi.id = done.quest_instance_id
                JOIN quest_defs qd ON qd.key = qi.def_key
                """,
                (tick_index, kind),
            )
            completions = await cur.fetchall()
        for row in completions:
            if faucet_id is None:
                faucet_id = await get_system_account_id(conn, "FAUCET")
            account_id = await get_user_account_id(conn, int(row["user_id"]))
            await post_transfer(
                conn,
                from_account_id=faucet_id,
                to_account_id=account_id,
                amount=int(row["reward_minor"]),
                reason="QUEST_REWARD",
                memo=str(row["name"]),
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET quests_completed = quests_completed + 1 "
                    "WHERE id = %s",
                    (int(row["user_id"]),),
                )
                await cur.execute(
                    """
                    INSERT INTO notifications (user_id, kind, payload)
                    VALUES (%s, 'QUEST_COMPLETED', %s)
                    """,
                    (
                        int(row["user_id"]),
                        json.dumps(
                            {
                                "tick_index": tick_index,
                                "name": row["name"],
                                "reward_minor": int(row["reward_minor"]),
                            }
                        ),
                    ),
                )
            paid += 1
    return paid


async def list_quests(
    conn: AsyncConnection, user_id: int, tick_index: int
) -> list[dict[str, Any]]:
    """Open instances with this user's live progress. Read-only."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT qi.id, qi.kind, qd.name, qi.period, qi.target,
                   qi.reward_minor, qi.window_start, qi.window_end,
                   (qc.user_id IS NOT NULL) AS completed
            FROM quest_instances qi
            JOIN quest_defs qd ON qd.key = qi.def_key
            LEFT JOIN quest_completions qc
              ON qc.quest_instance_id = qi.id AND qc.user_id = %s
            WHERE qi.status = 'OPEN'
            ORDER BY qi.period DESC, qi.id
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        progress: dict[int, int] = {}
        for kind in {r["kind"] for r in rows}:
            table, col, measure, extra = _MEASURES[kind]
            await cur.execute(
                f"""
                SELECT qi.id, COALESCE({measure}, 0) AS progress
                FROM quest_instances qi
                LEFT JOIN {table} t
                  ON t.{col} BETWEEN qi.window_start AND qi.window_end
                 AND t.user_id = %s
                 {extra}
                WHERE qi.status = 'OPEN' AND qi.kind = %s
                GROUP BY qi.id
                """,
                (user_id, kind),
            )
            progress.update(
                {int(r["id"]): int(r["progress"]) for r in await cur.fetchall()}
            )
    for row in rows:
        row["progress"] = progress.get(int(row["id"]), 0)
        row["ticks_remaining"] = max(0, int(row["window_end"]) - tick_index)
    return rows
