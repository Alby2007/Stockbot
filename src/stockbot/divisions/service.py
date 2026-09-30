"""Divisions: the weekly competitive ladder on top of seasons.

`division_memberships` assigns each player a tier (1 = Gold on top, N
below). Each tier runs a rolling week-long league season
(seasons.division_tier marks it); when it closes, entrants rank by
league equity (equal stakes -> pure return ordering), the top
`division.move_count` promote one tier and the bottom + non-qualifiers
relegate one. The next week's season for that tier spawns immediately
with the moved roster.

Joins mid-week queue for their tier's next season -- there's no
"unjoin" machinery, so `leave` only stops re-enrollment (the current
week still settles them). Division seasons never charge an entry fee:
the ladder is opted into once, not per week.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.divisions.errors import DivisionDisabledError, NotAMemberError
from stockbot.feed import emit_feed
from stockbot.market.engine import TICKS_PER_DAY
from stockbot.seasons import service as seasons
from stockbot.seasons.errors import SeasonError
from stockbot.seasons.service import Season
from stockbot.trading.errors import TradingError

log = logging.getLogger("stockbot.divisions")

TIER_NAMES = ("Gold", "Silver", "Bronze")


def tier_name(tier: int) -> str:
    return TIER_NAMES[tier - 1] if 1 <= tier <= len(TIER_NAMES) else f"Tier {tier}"


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _enabled(conn: AsyncConnection) -> bool:
    return await _config_float(conn, "divisions.enabled", 1.0) != 0.0


async def _notify(
    conn: AsyncConnection, user_id: int, kind: str, payload: dict[str, Any]
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO notifications (user_id, kind, payload) VALUES (%s, %s, %s)",
            (user_id, kind, json.dumps(payload)),
        )


async def membership(conn: AsyncConnection, user_id: int) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT user_id, tier, active, joined_at FROM division_memberships "
            "WHERE user_id = %s",
            (user_id,),
        )
        return await cur.fetchone()


async def join(conn: AsyncConnection, user_id: int, tick_index: int) -> int:
    """Enter the ladder at the bottom tier. Returns the assigned tier."""
    if not await _enabled(conn):
        raise DivisionDisabledError()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT is_bot, disabled_at FROM users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise TradingError("you need /start first")
        if row["is_bot"]:
            raise TradingError("bots can't join the ladder")
        if row["disabled_at"] is not None:
            raise TradingError("your account is suspended")
    tier = int(await _config_float(conn, "division.max_tier", 3))
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO division_memberships (user_id, tier, active)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (user_id) DO UPDATE
                    SET active = TRUE, joined_at = now()
                """,
                (user_id, tier),
            )
    # Bootstrap into this week's season if the tier has none running yet;
    # an already-running week means queue for next week.
    await ensure_week_seasons(conn, tick_index)
    return tier


async def leave(conn: AsyncConnection, user_id: int) -> None:
    """Stop re-enrollment. The current ACTIVE week still settles them."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE division_memberships SET active = FALSE "
                "WHERE user_id = %s AND active RETURNING user_id",
                (user_id,),
            )
            if await cur.fetchone() is None:
                raise NotAMemberError()


async def _active_division_season(conn: AsyncConnection, tier: int) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM seasons WHERE division_tier = %s AND status = 'ACTIVE' "
            "ORDER BY id DESC LIMIT 1",
            (tier,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def ensure_week_seasons(conn: AsyncConnection, tick_index: int) -> int:
    """Create (and enroll into) a week's season for every tier that has
    active members but no ACTIVE division season. Returns seasons created."""
    if not await _enabled(conn):
        return 0
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT m.tier, m.user_id
            FROM division_memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.active AND NOT u.is_bot AND u.disabled_at IS NULL
            ORDER BY m.tier, m.user_id
            """,
        )
        members: dict[int, list[int]] = {}
        for r in await cur.fetchall():
            members.setdefault(int(r["tier"]), []).append(int(r["user_id"]))

    week_ticks = int(await _config_float(conn, "division.week_ticks", 10080))
    stake = int(await _config_float(conn, "division.stake_minor", 100_000))
    created = 0
    for tier, user_ids in sorted(members.items()):
        if await _active_division_season(conn, tier) is not None:
            continue
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO seasons
                        (name, status, start_tick, end_tick, entry_fee_minor,
                         stake_minor, division_tier)
                    VALUES (%s, 'ACTIVE', %s, %s, 0, %s, %s)
                    RETURNING id
                    """,
                    (
                        f"{tier_name(tier)} Division",
                        tick_index,
                        tick_index + week_ticks,
                        stake,
                        tier,
                    ),
                )
                row = await cur.fetchone()
                assert row is not None
                season_id = int(row[0])
        created += 1
        for uid in user_ids:
            try:
                await seasons.join_season(conn, uid, season_id)
            except (SeasonError, TradingError):
                # Suspended mid-enrollment, raced a second entry -- the
                # membership row stands, next week's ensure picks them up.
                log.debug("division enroll skipped user=%s tier=%s", uid, tier)
    return created


async def close_division_season(
    conn: AsyncConnection,
    season: Season,
    entries: list[dict[str, Any]],
    tick: int,
) -> None:
    """Weekly settlement inside _close_season_claimed (season CLOSED):
    rank by league equity, move tiers, DM results, feed the digest, then
    spawn next week. League teardown runs after this in the shared tail."""
    assert season.division_tier is not None
    tier = season.division_tier
    min_trades = int(await _config_float(conn, "division.min_trades", 1))
    move = int(await _config_float(conn, "division.move_count", 2))

    equities: dict[int, int] = {}
    qualified: list[int] = []
    unqualified: list[int] = []
    for entry in entries:
        uid = int(entry["user_id"])
        equities[uid] = await seasons.league_equity_minor(conn, season.id, uid)
        if int(entry["trades_count"]) >= min_trades:
            qualified.append(uid)
        else:
            unqualified.append(uid)

    ranked = sorted(qualified, key=lambda u: equities[u], reverse=True)
    ranked += sorted(unqualified, key=lambda u: equities[u], reverse=True)
    rank_by_user = {uid: i + 1 for i, uid in enumerate(ranked)}

    qualified_set = set(qualified)
    promoted = set(ranked[:move]) & qualified_set
    # Relegation: every non-qualifier plus the bottom K of the table.
    relegated = (set(unqualified) | set(ranked[-move:])) - promoted
    if tier <= 1:
        promoted = set()  # nowhere higher to go

    async with conn.cursor() as cur:
        for uid in promoted:
            await cur.execute(
                "UPDATE division_memberships SET tier = %s WHERE user_id = %s",
                (tier - 1, uid),
            )
        for uid in relegated:
            await cur.execute(
                "UPDATE division_memberships SET tier = %s WHERE user_id = %s",
                (tier + 1, uid),
            )
        for uid, rank in rank_by_user.items():
            await cur.execute(
                "UPDATE season_entries SET final_rank = %s "
                "WHERE season_id = %s AND user_id = %s",
                (rank, season.id, uid),
            )

    for uid, rank in rank_by_user.items():
        if uid in promoted:
            moved, new_tier = "promoted", tier - 1
        elif uid in relegated:
            moved, new_tier = "relegated", tier + 1
        else:
            moved, new_tier = "stayed", tier
        await _notify(
            conn,
            uid,
            "DIVISION_RESULT",
            {
                "tick_index": tick,
                "tier_name": tier_name(tier),
                "rank": rank,
                "entrants": len(ranked),
                "equity": equities.get(uid, 0),
                "moved": moved,
                "new_tier_name": tier_name(new_tier),
            },
        )

    await emit_feed(
        conn,
        "DIVISION_WEEK",
        {
            "tier_name": tier_name(tier),
            "winner_id": ranked[0] if ranked else None,
            "entrants": len(ranked),
            "promoted": sorted(promoted),
            "relegated": sorted(relegated),
        },
        tick_index=tick,
    )

    # The moved roster spawns next week immediately in its new tiers.
    await ensure_week_seasons(conn, tick)


async def on_tick(conn: AsyncConnection, tick_index: int) -> None:
    """Day-boundary backstop for ensure_week_seasons (covers members who
    joined while seasons were mid-creation, tiers stranded by a manual
    close, etc.). The join/close paths call ensure directly."""
    if tick_index % TICKS_PER_DAY == 0:
        await ensure_week_seasons(conn, tick_index)


async def ladder(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Roster for /division standings: every active member grouped by tier,
    annotated with their live rank inside the tier's running week."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT m.user_id, m.tier, s.id AS season_id
            FROM division_memberships m
            LEFT JOIN seasons s
              ON s.division_tier = m.tier AND s.status = 'ACTIVE'
            WHERE m.active
            ORDER BY m.tier, m.user_id
            """
        )
        rows = await cur.fetchall()

    # Live equity inside each running season, for in-week ordering.
    season_uids: dict[int, list[int]] = {}
    for r in rows:
        if r["season_id"] is not None:
            season_uids.setdefault(int(r["season_id"]), []).append(int(r["user_id"]))
    equity: dict[tuple[int, int], int] = {}
    for sid, uids in season_uids.items():
        for uid in uids:
            equity[(sid, uid)] = await seasons.league_equity_minor(conn, sid, uid)

    out: list[dict[str, Any]] = []
    for r in rows:
        member_sid = int(r["season_id"]) if r["season_id"] is not None else None
        out.append(
            {
                "user_id": int(r["user_id"]),
                "tier": int(r["tier"]),
                "season_id": member_sid,
                "equity_minor": (
                    equity.get((member_sid, int(r["user_id"])))
                    if member_sid is not None
                    else None
                ),
            }
        )
    return out
