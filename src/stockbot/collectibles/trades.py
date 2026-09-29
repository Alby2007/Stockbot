"""Card-for-card trading: non-custodial offers.

Offers hold no escrow -- accept() re-verifies both binders under FOR
UPDATE locks, so a stale offer auto-voids instead of stranding cards.
A trade moves the whole user_cards row (frame + serial + copies) and
merges into a held row: best frame wins, lowest serial survives, copies
sum. Shards move via signed 'TRADE' shard_events, preserving
users.shards = SUM(shard_events.delta). There is no cash leg, by
design: nothing here converts back to currency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.collectibles.service import _shard_event
from stockbot.feed import emit_feed
from stockbot.shop.errors import ShopError


class TradeError(ShopError):
    """Bad offer, stale binder, expired trade, or wrong responder."""


@dataclass(frozen=True)
class TradeRow:
    id: int
    proposer: int
    counterparty: int
    give_cards: list[str]
    want_cards: list[str]
    give_shards: int
    want_shards: int
    status: str
    expires_tick: int


# SQL CASE rank for merging frames: higher rank wins the merge.
_FRAME_RANK_SQL = (
    "CASE %s WHEN 'STANDARD' THEN 0 WHEN 'SILVER' THEN 1"
    " WHEN 'GOLD' THEN 2 WHEN 'PLATINUM' THEN 3"
    " WHEN 'EPIC' THEN 4 WHEN 'LEGENDARY' THEN 5 ELSE -1 END"
)


async def _trade_config(conn: AsyncConnection) -> dict[str, float]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT key, value FROM config WHERE key LIKE 'trade.%'"
        )
        return {
            str(r[0])[len("trade."):]: float(r[1]) for r in await cur.fetchall()
        }


async def get_trade(conn: AsyncConnection, trade_id: int) -> TradeRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, proposer, counterparty, give_cards, want_cards,"
            " give_shards, want_shards, status, expires_tick"
            " FROM card_trades WHERE id = %s",
            (trade_id,),
        )
        r = await cur.fetchone()
    if r is None:
        return None
    return TradeRow(
        id=int(r["id"]),
        proposer=int(r["proposer"]),
        counterparty=int(r["counterparty"]),
        give_cards=list(r["give_cards"]),
        want_cards=list(r["want_cards"]),
        give_shards=int(r["give_shards"]),
        want_shards=int(r["want_shards"]),
        status=str(r["status"]),
        expires_tick=int(r["expires_tick"]),
    )


async def create_offer(
    conn: AsyncConnection,
    proposer: int,
    counterparty: int,
    give_cards: list[str],
    want_cards: list[str],
    give_shards: int,
    want_shards: int,
    tick_index: int | None,
) -> int:
    """Open an offer. Returns the trade id. Holdings are verified at
    accept, not now -- but a proposer offering cards they don't hold is
    rejected up front as a courtesy."""
    if proposer == counterparty:
        raise TradeError("You can't trade with yourself.")
    if not give_cards and not want_cards and not give_shards and not want_shards:
        raise TradeError("An offer needs something on at least one side.")
    cfg = await _trade_config(conn)
    ttl = int(cfg.get("ttl_ticks", 1440))
    max_open = int(cfg.get("max_open", 10))
    expires = (tick_index or 0) + ttl

    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM card_trades WHERE proposer = %s"
                " AND status = 'OPEN'",
                (proposer,),
            )
            if int((await cur.fetchone() or (0,))[0]) >= max_open:
                raise TradeError(f"Too many open offers (max {max_open}).")
            if give_cards:
                await cur.execute(
                    "SELECT count(*) FROM user_cards WHERE user_id = %s"
                    " AND card_key = ANY(%s)",
                    (proposer, give_cards),
                )
                if int((await cur.fetchone() or (0,))[0]) < len(set(give_cards)):
                    raise TradeError("You don't hold every card you're offering.")
            if give_shards:
                await cur.execute(
                    "SELECT shards FROM users WHERE id = %s", (proposer,)
                )
                row = await cur.fetchone()
                if row is None or int(row[0]) < give_shards:
                    raise TradeError("Not enough shards for that offer.")
            await cur.execute(
                """
                INSERT INTO card_trades
                    (proposer, counterparty, give_cards, want_cards,
                     give_shards, want_shards, expires_tick)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    proposer,
                    counterparty,
                    give_cards,
                    want_cards,
                    give_shards,
                    want_shards,
                    expires,
                ),
            )
            row = await cur.fetchone()
            assert row is not None  # INSERT ... RETURNING
            trade_id = int(row[0])
            await cur.execute(
                "INSERT INTO notifications (user_id, kind, payload)"
                " VALUES (%s, 'TRADE_OFFER', %s)",
                (
                    counterparty,
                    json.dumps({"trade_id": trade_id, "from": proposer}),
                ),
            )
    return trade_id


async def _transfer_card(
    conn: AsyncConnection, from_uid: int, to_uid: int, card_key: str
) -> None:
    """Move one card: delete the source row, merge into the target's
    row when held -- best frame wins, lowest serial survives, copies
    sum."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT best_frame, best_serial, copies, first_acquired_tick"
            " FROM user_cards WHERE user_id = %s AND card_key = %s FOR UPDATE",
            (from_uid, card_key),
        )
        src = await cur.fetchone()
        if src is None:
            raise TradeError("An offered card is no longer held.")
        await cur.execute(
            "DELETE FROM user_cards WHERE user_id = %s AND card_key = %s",
            (from_uid, card_key),
        )
        rank_src = _FRAME_RANK_SQL % "EXCLUDED.best_frame"
        rank_dst = _FRAME_RANK_SQL % "user_cards.best_frame"
        await cur.execute(
            f"""
            INSERT INTO user_cards
                (user_id, card_key, best_frame, best_serial, copies,
                 first_acquired_tick)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, card_key) DO UPDATE SET
                best_frame = CASE WHEN {rank_src} > {rank_dst}
                                  THEN EXCLUDED.best_frame
                                  ELSE user_cards.best_frame END,
                best_serial = LEAST(
                    COALESCE(user_cards.best_serial, EXCLUDED.best_serial),
                    COALESCE(EXCLUDED.best_serial, user_cards.best_serial)),
                copies = user_cards.copies + EXCLUDED.copies,
                upgraded_at = now()
            """,
            (
                to_uid,
                card_key,
                src["best_frame"],
                src["best_serial"],
                src["copies"],
                src["first_acquired_tick"],
            ),
        )


async def _move_shards(
    conn: AsyncConnection, from_uid: int, to_uid: int, amount: int,
    tick_index: int | None,
) -> None:
    if amount <= 0:
        return
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE users SET shards = shards - %s WHERE id = %s AND shards >= %s",
            (amount, from_uid, amount),
        )
        if cur.rowcount == 0:
            raise TradeError("A shard balance no longer covers this trade.")
        await cur.execute(
            "UPDATE users SET shards = shards + %s WHERE id = %s",
            (amount, to_uid),
        )
    await _shard_event(conn, from_uid, -amount, "TRADE", tick_index=tick_index)
    await _shard_event(conn, to_uid, amount, "TRADE", tick_index=tick_index)


async def accept(
    conn: AsyncConnection, trade_id: int, accepter_id: int,
    tick_index: int | None,
) -> TradeRow:
    """Counterparty accepts: verify both binders, swap, settle shards."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM card_trades WHERE id = %s FOR UPDATE",
                (trade_id,),
            )
            if await cur.fetchone() is None:
                raise TradeError("Unknown trade.")
        trade = await get_trade(conn, trade_id)
        assert trade is not None  # just locked above
        if trade.status != "OPEN":
            raise TradeError(f"That trade is already {trade.status.lower()}.")
        if accepter_id != trade.counterparty:
            raise TradeError("Only the counterparty can accept.")
        if tick_index is not None and tick_index > trade.expires_tick:
            raise TradeError("That trade expired.")

        # Swap cards both ways; _transfer_card raises when a binder
        # changed since the offer was posted.
        for key in trade.give_cards:
            await _transfer_card(conn, trade.proposer, trade.counterparty, key)
        for key in trade.want_cards:
            await _transfer_card(conn, trade.counterparty, trade.proposer, key)
        await _move_shards(
            conn, trade.proposer, trade.counterparty, trade.give_shards,
            tick_index,
        )
        await _move_shards(
            conn, trade.counterparty, trade.proposer, trade.want_shards,
            tick_index,
        )

        # A featured card that traded away unpins itself.
        async with conn.cursor() as cur:
            traded = trade.give_cards + trade.want_cards
            if traded:
                await cur.execute(
                    "UPDATE users SET featured_card = NULL"
                    " WHERE (id = %s AND featured_card = ANY(%s))"
                    "    OR (id = %s AND featured_card = ANY(%s))",
                    (trade.proposer, trade.give_cards,
                     trade.counterparty, trade.want_cards),
                )
            await cur.execute(
                "UPDATE card_trades SET status = 'ACCEPTED', resolved_at = now()"
                " WHERE id = %s",
                (trade_id,),
            )
            for uid, other in (
                (trade.proposer, trade.counterparty),
                (trade.counterparty, trade.proposer),
            ):
                await cur.execute(
                    "INSERT INTO notifications (user_id, kind, payload)"
                    " VALUES (%s, 'TRADE_RESULT', %s)",
                    (uid, json.dumps({"trade_id": trade_id, "with": other,
                                      "accepted": True})),
                )
        # Names in the feed payload: keys ('card_nort') aren't tape-worthy.
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT key, name FROM cards WHERE key = ANY(%s)",
                (trade.give_cards + trade.want_cards,),
            )
            key_names = {str(r["key"]): str(r["name"]) for r in await cur.fetchall()}
        await emit_feed(
            conn,
            "TRADE_COMPLETED",
            {
                "other": trade.counterparty,
                "give": [key_names.get(k, k) for k in trade.give_cards],
                "want": [key_names.get(k, k) for k in trade.want_cards],
            },
            user_id=trade.proposer,
            tick_index=tick_index,
        )
    return TradeRow(**{**trade.__dict__, "status": "ACCEPTED"})


async def _resolve(
    conn: AsyncConnection, trade_id: int, user_id: int, new_status: str
) -> TradeRow:
    """Shared cancel/decline path: actor must be a party; proposer
    cancels, counterparty declines."""
    async with conn.transaction():
        trade = await get_trade(conn, trade_id)
        if trade is None:
            raise TradeError("Unknown trade.")
        if trade.status != "OPEN":
            raise TradeError(f"That trade is already {trade.status.lower()}.")
        if new_status == "CANCELLED" and user_id != trade.proposer:
            raise TradeError("Only the proposer can cancel.")
        if new_status == "DECLINED" and user_id != trade.counterparty:
            raise TradeError("Only the counterparty can decline.")
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE card_trades SET status = %s, resolved_at = now()"
                " WHERE id = %s",
                (new_status, trade_id),
            )
            await cur.execute(
                "INSERT INTO notifications (user_id, kind, payload)"
                " VALUES (%s, 'TRADE_RESULT', %s)",
                (
                    trade.proposer if new_status == "DECLINED" else trade.counterparty,
                    json.dumps({
                        "trade_id": trade_id,
                        "with": user_id,
                        "accepted": False,
                        "result": new_status.lower(),
                    }),
                ),
            )
    return TradeRow(**{**trade.__dict__, "status": new_status})


async def decline(conn: AsyncConnection, trade_id: int, user_id: int) -> TradeRow:
    return await _resolve(conn, trade_id, user_id, "DECLINED")


async def cancel(conn: AsyncConnection, trade_id: int, user_id: int) -> TradeRow:
    return await _resolve(conn, trade_id, user_id, "CANCELLED")


async def list_trades(conn: AsyncConnection, user_id: int) -> list[TradeRow]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, proposer, counterparty, give_cards, want_cards,"
            " give_shards, want_shards, status, expires_tick"
            " FROM card_trades"
            " WHERE status = 'OPEN'"
            "   AND (proposer = %s OR counterparty = %s)"
            " ORDER BY id",
            (user_id, user_id),
        )
        return [
            TradeRow(
                id=int(r["id"]),
                proposer=int(r["proposer"]),
                counterparty=int(r["counterparty"]),
                give_cards=list(r["give_cards"]),
                want_cards=list(r["want_cards"]),
                give_shards=int(r["give_shards"]),
                want_shards=int(r["want_shards"]),
                status=str(r["status"]),
                expires_tick=int(r["expires_tick"]),
            )
            for r in await cur.fetchall()
        ]
