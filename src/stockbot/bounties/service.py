"""Bounties: escrowed hits on other players' heads.

`/bounty post @target amount` escrows the amount into GAME_ESCROW (a
small posting fee burns to SINK so hits aren't free). The condition is
LIQUIDATED: when the target's MAIN portfolio (season_id IS NULL) is
force-liquidated, the whole pot pays winner-take-all to the human with
the largest aggregate OPPOSING exposure in the target's liquidated
tickers -- your shorts against their sold longs, your longs against
their covered shorts. That attribution reads the `liquidations` audit
rows, not an inferred "killer": liquidation is engine-triggered, so the
claimant is whoever was positioned against the victim, not whoever
happened to act last. Nobody positioned -> SINK keeps the pot.

`settle_for_user` is called from margin._liquidate_account when legs
landed, covering both the tick sweep and post-trade check_and_liquidate.
League-account liquidations never trigger (faucet stakes aren't real
heads), and bots can be targets never claimants -- except persona NPCs
(phase 2), who are addressable targets like anyone else.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.bounties.errors import (
    BountyNotFoundError,
    BountyStateError,
    BountyTargetError,
)
from stockbot.feed import emit_feed
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin import service as margin

log = logging.getLogger("stockbot.bounties")


@dataclass(frozen=True)
class Bounty:
    id: int
    poster_id: int
    target_id: int
    amount_minor: int
    status: str
    created_tick: int
    expires_tick: int
    claimed_by: int | None
    paid_minor: int | None


def _from_row(row: dict[str, Any]) -> Bounty:
    return Bounty(
        id=int(row["id"]),
        poster_id=int(row["poster_id"]),
        target_id=int(row["target_id"]),
        amount_minor=int(row["amount_minor"]),
        status=str(row["status"]),
        created_tick=int(row["created_tick"]),
        expires_tick=int(row["expires_tick"]),
        claimed_by=int(row["claimed_by"]) if row["claimed_by"] is not None else None,
        paid_minor=int(row["paid_minor"]) if row["paid_minor"] is not None else None,
    )


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _enabled(conn: AsyncConnection) -> bool:
    return await _config_float(conn, "bounties.enabled", 1.0) != 0.0


async def get_bounty(conn: AsyncConnection, bounty_id: int) -> Bounty | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM bounties WHERE id = %s", (bounty_id,))
        row = await cur.fetchone()
    return _from_row(row) if row else None


async def list_open(conn: AsyncConnection) -> list[Bounty]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM bounties WHERE status = 'OPEN' ORDER BY id DESC LIMIT 25"
        )
        return [_from_row(r) for r in await cur.fetchall()]


async def _notify(
    conn: AsyncConnection, user_id: int, kind: str, payload: dict[str, Any]
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO notifications (user_id, kind, payload) VALUES (%s, %s, %s)",
            (user_id, kind, json.dumps(payload)),
        )


async def post(
    conn: AsyncConnection,
    poster_id: int,
    target_id: int,
    amount_minor: int,
    tick_index: int,
) -> int:
    """Escrow a bounty on a target. Returns the bounty id."""
    if not await _enabled(conn):
        raise BountyStateError("bounties are disabled")
    if poster_id == target_id:
        raise BountyTargetError("you can't put a bounty on yourself")
    lo = int(await _config_float(conn, "bounty.min_amount_minor", 100))
    if amount_minor < lo:
        raise BountyTargetError(f"minimum bounty is {lo / 100:.2f} dollars")

    persona_name: str | None = None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT is_bot, disabled_at FROM users WHERE id = %s", (target_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise BountyTargetError("they haven't started yet — they need /start first")
        if row["is_bot"]:
            # Only openly-named personas are addressable -- a bot bounty
            # needs a boss worth hitting; the anonymous tape stays
            # unaddressable.
            await cur.execute(
                "SELECT display_name FROM npc_agents "
                "WHERE user_id = %s AND is_persona",
                (target_id,),
            )
            prow = await cur.fetchone()
            if prow is None or prow["display_name"] is None:
                raise BountyTargetError("you can't put a bounty on a bot")
            persona_name = str(prow["display_name"])
        if row["disabled_at"] is not None:
            raise BountyTargetError("that account is suspended")
        cap = int(await _config_float(conn, "bounty.max_open_per_target", 3))
        await cur.execute(
            "SELECT COUNT(*) AS open_count FROM bounties "
            "WHERE target_id = %s AND status = 'OPEN'",
            (target_id,),
        )
        row2 = await cur.fetchone()
        if row2 is not None and int(row2["open_count"]) >= cap:
            raise BountyTargetError("there are already enough hits out on them")

    fee = int(await _config_float(conn, "bounty.fee_minor", 100))
    ttl = int(await _config_float(conn, "bounty.ttl_ticks", 10080))
    async with conn.transaction():
        await margin.assert_spend_ok(conn, poster_id, amount_minor + fee)
        poster_account = await get_user_account_id(conn, poster_id)
        escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
        sink_id = await get_system_account_id(conn, "SINK")
        if fee > 0:
            await post_transfer(
                conn,
                from_account_id=poster_account,
                to_account_id=sink_id,
                amount=fee,
                reason="BOUNTY_FEE",
            )
        await post_transfer(
            conn,
            from_account_id=poster_account,
            to_account_id=escrow_id,
            amount=amount_minor,
            reason="BOUNTY_POST",
        )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO bounties (poster_id, target_id, amount_minor,
                                      created_tick, expires_tick)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (poster_id, target_id, amount_minor, tick_index, tick_index + ttl),
            )
            id_row = await cur.fetchone()
            assert id_row is not None
            bounty_id = int(id_row[0])
        await _notify(
            conn,
            target_id,
            "BOUNTY_PLACED",
            {
                "tick_index": tick_index,
                "bounty_id": bounty_id,
                "poster": poster_id,
                "amount": amount_minor,
            },
        )
        await emit_feed(
            conn,
            "BOUNTY_POSTED",
            {
                "poster": poster_id,
                "target": target_id,
                "target_name": persona_name,
                "amount": amount_minor,
            },
            user_id=target_id,
            tick_index=tick_index,
        )
    return bounty_id


async def cancel(conn: AsyncConnection, bounty_id: int, user_id: int) -> None:
    """Poster withdraws an OPEN bounty: amount back, fee stays burned."""
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM bounties WHERE id = %s FOR UPDATE", (bounty_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise BountyNotFoundError(bounty_id)
        bounty = _from_row(row)
        if user_id != bounty.poster_id:
            raise BountyStateError("only the poster can cancel this bounty")
        if bounty.status != "OPEN":
            raise BountyStateError(f"that bounty is already {bounty.status.lower()}")
        escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
        poster_account = await get_user_account_id(conn, user_id)
        await post_transfer(
            conn,
            from_account_id=escrow_id,
            to_account_id=poster_account,
            amount=bounty.amount_minor,
            reason="BOUNTY_REFUND",
            memo=f"bounty {bounty.id}",
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE bounties SET status = 'CANCELLED' WHERE id = %s", (bounty_id,)
            )


async def _claimant_for_target(
    conn: AsyncConnection, target_id: int, tick_index: int | None
) -> tuple[int | None, int]:
    """Largest aggregate opposing exposure in the target's liquidated
    tickers this liquidation. Returns (claimant_user_id, notional_minor).

    Per liquidation leg: a SELL leg means the target was long -- the
    hunters are the shorts (margin shorts + open bounded shorts). A BUY
    leg covered their short -- the hunters are the longs. Main-portfolio
    exposures only (league stakes aren't bounty-hunting capital); bots
    and the target excluded."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT l.instrument_id, l.side, i.quoted_price
            FROM liquidations l
            JOIN instruments i ON i.id = l.instrument_id
            WHERE l.user_id = %s AND l.season_id IS NULL
              AND l.tick_index IS NOT DISTINCT FROM %s
            """,
            (target_id, tick_index),
        )
        legs = await cur.fetchall()
    if not legs:
        return None, 0

    exposure: dict[int, float] = {}
    for leg in legs:
        iid = int(leg["instrument_id"])
        price = float(leg["quoted_price"])
        if leg["side"] == "SELL":
            # Target was long -> hunters are shorts of any kind.
            sql = """
                SELECT user_id, SUM(qty) AS qty FROM (
                    SELECT p.user_id, -p.quantity AS qty
                    FROM positions p
                    JOIN users u ON u.id = p.user_id AND NOT u.is_bot
                    WHERE p.instrument_id = %s AND p.quantity < 0
                      AND p.season_id IS NULL AND p.user_id <> %s
                    UNION ALL
                    SELECT bs.user_id, bs.quantity
                    FROM bounded_shorts bs
                    JOIN users u ON u.id = bs.user_id AND NOT u.is_bot
                    WHERE bs.instrument_id = %s AND bs.status = 'OPEN'
                      AND bs.season_id IS NULL AND bs.user_id <> %s
                ) x GROUP BY user_id
            """
            params: tuple[object, ...] = (iid, target_id, iid, target_id)
        else:
            # Target was short -> hunters are the longs pumping it.
            sql = """
                SELECT p.user_id, SUM(p.quantity) AS qty
                FROM positions p
                JOIN users u ON u.id = p.user_id AND NOT u.is_bot
                WHERE p.instrument_id = %s AND p.quantity > 0
                  AND p.season_id IS NULL AND p.user_id <> %s
                GROUP BY p.user_id
            """
            params = (iid, target_id)
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            for uid, qty in await cur.fetchall():
                if qty and int(qty) > 0:
                    exposure[int(uid)] = exposure.get(int(uid), 0.0) + int(qty) * price * 100
    if not exposure:
        return None, 0
    claimant = max(exposure, key=lambda u: exposure[u])
    return claimant, int(exposure[claimant])


async def settle_for_user(conn: AsyncConnection, user_id: int, tick_index: int | None) -> int:
    """Pay out every OPEN bounty on a just-liquidated main portfolio.
    Called from margin._liquidate_account when legs landed and
    season_id IS NULL. Returns bounties claimed."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM bounties WHERE target_id = %s AND status = 'OPEN' FOR UPDATE",
            (user_id,),
        )
        rows = await cur.fetchall()
    if not rows:
        return 0
    open_bounties = [_from_row(r) for r in rows]

    claimant, _notional = await _claimant_for_target(conn, user_id, tick_index)
    rake_pct = await _config_float(conn, "bounty.rake_pct", 0.05)
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    sink_id = await get_system_account_id(conn, "SINK")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT display_name FROM npc_agents "
            "WHERE user_id = %s AND is_persona",
            (user_id,),
        )
        prow = await cur.fetchone()
    target_name = str(prow[0]) if prow and prow[0] is not None else None

    total_paid = 0
    for bounty in open_bounties:
        rake = int(bounty.amount_minor * rake_pct)
        paid = bounty.amount_minor - rake
        if claimant is not None:
            claimant_account = await get_user_account_id(conn, claimant)
            await post_transfer(
                conn,
                from_account_id=escrow_id,
                to_account_id=claimant_account,
                amount=paid,
                reason="BOUNTY_PAYOUT",
                memo=f"bounty {bounty.id} on {user_id}",
            )
            total_paid += paid
        else:
            paid = 0
            # Nobody was positioned against them -> SINK keeps the pot.
            await post_transfer(
                conn,
                from_account_id=escrow_id,
                to_account_id=sink_id,
                amount=bounty.amount_minor,
                reason="BOUNTY_SINK",
                memo=f"bounty {bounty.id} unclaimed",
            )
            rake = 0
        if rake > 0:
            await post_transfer(
                conn,
                from_account_id=escrow_id,
                to_account_id=sink_id,
                amount=rake,
                reason="BOUNTY_RAKE",
                memo=f"bounty {bounty.id}",
            )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE bounties
                SET status = 'CLAIMED', claimed_by = %s, claimed_tick = %s,
                    paid_minor = %s
                WHERE id = %s
                """,
                (claimant, tick_index, paid, bounty.id),
            )

    if claimant is not None:
        await _notify(
            conn,
            claimant,
            "BOUNTY_PAID",
            {
                "tick_index": tick_index,
                "role": "claimant",
                "target": user_id,
                "paid": total_paid,
                "count": len(open_bounties),
            },
        )
        await _notify(
            conn,
            user_id,
            "BOUNTY_PAID",
            {
                "tick_index": tick_index,
                "role": "target",
                "claimant": claimant,
                "count": len(open_bounties),
            },
        )
    else:
        await _notify(
            conn,
            user_id,
            "BOUNTY_PAID",
            {"tick_index": tick_index, "role": "target", "claimant": None,
             "count": len(open_bounties)},
        )
    await emit_feed(
        conn,
        "BOUNTY_PAID",
        {
            "target": user_id,
            "target_name": target_name,
            "claimant": claimant,
            "paid": total_paid,
            "count": len(open_bounties),
        },
        user_id=user_id,
        tick_index=tick_index,
    )
    return len(open_bounties)


async def on_tick(conn: AsyncConnection, tick_index: int) -> int:
    """Expire OPEN bounties past ttl: refund the amount (the posting fee
    was already burned). Session-agnostic -- runs in both tick phases."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM bounties WHERE status = 'OPEN' AND expires_tick <= %s",
            (tick_index,),
        )
        due = [_from_row(r) for r in await cur.fetchall()]
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    for bounty in due:
        poster_account = await get_user_account_id(conn, bounty.poster_id)
        await post_transfer(
            conn,
            from_account_id=escrow_id,
            to_account_id=poster_account,
            amount=bounty.amount_minor,
            reason="BOUNTY_REFUND",
            memo=f"bounty {bounty.id} expired",
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE bounties SET status = 'EXPIRED' WHERE id = %s", (bounty.id,)
            )
        await _notify(
            conn,
            bounty.poster_id,
            "BOUNTY_EXPIRED",
            {
                "tick_index": tick_index,
                "bounty_id": bounty.id,
                "target": bounty.target_id,
                "amount": bounty.amount_minor,
            },
        )
    return len(due)
