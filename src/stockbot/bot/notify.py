"""Transactional outbox delivery: poll `notifications`, coalesce rows into
one message per (user, kind, tick), and hand each off to an injectable
`deliver` callable -- tests substitute a stub, no Discord needed.

Insert sites (all inside the event's own transaction):
    margin/service.py     _liquidate_leg   -> LIQUIDATION
    margin/service.py     _liquidate_leg   -> SHORT_RECALL (kind="RECALL")
    margin/service.py     _warn_margin_risk -> MARGIN_CALL
    shorts/service.py     sweep_knockouts  -> KNOCKOUT
    orders/service.py     fill sites       -> ORDER_FILLED
    seasons/service.py    close_season     -> SEASON_RESULT
    status/service.py     evaluate_badges  -> BADGE_EARNED
    ipo/service.py        settle_due       -> IPO_SETTLED
    alerts/service.py     sweep_alerts     -> ALERT_TRIGGERED
    quests/service.py     sweep_completions -> QUEST_COMPLETED
    options/service.py    settle_expired_options -> OPTION_SETTLED
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from itertools import groupby
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.bot.format import format_money, format_price

log = logging.getLogger("stockbot.bot.notify")

MAX_ATTEMPTS = 5
POLL_BATCH = 100
# Exponential backoff in minutes, capped; index by (attempts - 1).
_BACKOFF_MINUTES = (1, 5, 15, 60, 240)


class DeliveryForbidden(Exception):
    """Raised by `deliver` when the recipient's DMs are closed -- the
    caller dead-letters every pending row for that user instead of
    retrying (which would just repeat the same failure forever)."""


Deliver = Callable[[int, str], Awaitable[None]]


async def _fetch_batch(conn: AsyncConnection) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT n.id, n.user_id, n.kind, n.payload
            FROM notifications n
            JOIN users u ON u.id = n.user_id
            WHERE n.sent_at IS NULL AND n.attempts < %s
              AND n.next_attempt_at <= now()
              AND u.dm_notifications
            ORDER BY n.user_id, n.kind, n.id
            LIMIT %s
            FOR UPDATE OF n SKIP LOCKED
            """,
            (MAX_ATTEMPTS, POLL_BATCH),
        )
        return await cur.fetchall()


def _group_key(row: dict[str, Any]) -> tuple[int, str, Any]:
    return (int(row["user_id"]), str(row["kind"]), row["payload"].get("tick_index"))


def _fmt_liquidation(items: list[dict[str, Any]]) -> str:
    parts = []
    total_penalty = 0
    for p in items:
        verb = "covered" if p["side"] == "BUY" else "sold"
        parts.append(f"{verb} {p['qty']:,} {p['ticker']} @ {format_price(p['fill'])}")
        total_penalty += int(p.get("penalty", 0))
    noun = "position" if len(items) == 1 else "positions"
    return (
        f"Liquidated on {len(items)} {noun}: " + ", ".join(parts)
        + f" — penalty {format_money(total_penalty)} total. Check `/margin`."
    )


def _fmt_margin_call(items: list[dict[str, Any]]) -> str:
    p = items[0]
    ratio = int(p["equity"]) / int(p["maint"]) if int(p["maint"]) else 0.0
    return (
        f"Margin warning: equity {format_money(p['equity'])} vs maintenance "
        f"{format_money(p['maint'])} (x{ratio:.2f}) — liquidation below "
        "x1.00. Cover shorts or add cash."
    )


def _fmt_short_recall(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['qty']:,} {p['ticker']} @ {format_price(p['fill'])}" for p in items
    ]
    return (
        "Borrow recall — crowded short forced-covered: "
        + ", ".join(parts)
        + "."
    )


def _fmt_knockout(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['ticker']} {p['qty']:,} @ entry {format_price(p['entry'])} "
        f"(KO {format_price(p['ko_price'])})"
        for p in items
    ]
    noun = "short" if len(items) == 1 else "shorts"
    return f"Bounded {noun} knocked out: " + ", ".join(parts) + "."


def _fmt_order_filled(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['side']} {p['qty']:,} {p['ticker']} @ {format_price(p['fill'])} "
        f"(#{p['order_id']}, {'maker' if p.get('maker') else 'taker'})"
        for p in items
    ]
    noun = "order" if len(items) == 1 else "orders"
    return f"{len(items)} {noun} filled: " + ", ".join(parts) + "."


def _fmt_season_result(items: list[dict[str, Any]]) -> str:
    lines = []
    for p in items:
        if p.get("rank") is None:
            lines.append(
                f"**{p['season_name']}** finished — you didn't qualify "
                "for a rank (not enough active trading days)."
            )
            continue
        lines.append(
            f"**{p['season_name']}** finished — rank #{p['rank']}, "
            f"score {p['score']:.2f}"
            + (f", prize {format_money(p['prize'])}" if p.get("prize") else "")
        )
    return "\n".join(lines)


def _fmt_account_suspended(items: list[dict[str, Any]]) -> str:
    p = items[0]
    return (
        "Your StockBot account has been suspended"
        + (f": {p['reason']}" if p.get("reason") else ".")
        + " Your open orders were cancelled; open positions are still "
        "managed by the market. Contact a moderator to appeal."
    )


def _fmt_badge_earned(items: list[dict[str, Any]]) -> str:
    names = ", ".join(f"**{p['name']}**" for p in items)
    noun = "Badge" if len(items) == 1 else "Badges"
    return f"{noun} earned: {names} — see `/profile`."


def _fmt_ipo_settled(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        part = (
            f"**{p['ticker']}** — {p['allocated']:,} shares allocated "
            f"@ {format_price(p['price'])}"
        )
        if p.get("refund"):
            part += f", {format_money(p['refund'])} refunded"
        parts.append(part)
    return "IPO settled: " + "; ".join(parts) + ". It's live on the market."


def _fmt_alert_triggered(items: list[dict[str, Any]]) -> str:
    parts = [
        f"**{p['ticker']}** crossed {p['direction'].lower()} "
        f"{format_price(p['target'])} (now {format_price(p['mark'])})"
        for p in items
    ]
    return "Price alert" + ("s" if len(items) > 1 else "") + ": " + "; ".join(parts)


def _fmt_quest_completed(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['name']} (+{format_money(p['reward_minor'])})" for p in items
    ]
    return "Quest complete: " + "; ".join(parts)


def _fmt_option_settled(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['qty']:,} **{p['ticker']}** {p['side'].lower()} "
        f"{format_price(p['strike'])} expired @ {format_price(p['settle_price'])}"
        + (f" (+{format_money(p['payout'])})" if p["payout"] else "")
        for p in items
    ]
    return "Option" + ("s" if len(items) > 1 else "") + " settled: " + "; ".join(parts)


def _fmt_moment_earned(items: list[dict[str, Any]]) -> str:
    names = ", ".join(f"**{p['name']}**" for p in items)
    noun = "Moment" if len(items) == 1 else "Moments"
    return f"{noun} witnessed: {names} — you were holding when it happened. See `/collection`."


def _fmt_trade_offer(items: list[dict[str, Any]]) -> str:
    parts = [f"**offer #{p['trade_id']}** from <@{p['from']}>" for p in items]
    noun = "offer" if len(items) == 1 else "offers"
    return f"Trade {noun}: " + "; ".join(parts) + " — `/trade list` to answer."


def _fmt_trade_result(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        if p.get("accepted"):
            parts.append(f"**#{p['trade_id']}** accepted — cards swapped")
        else:
            parts.append(f"**#{p['trade_id']}** {p.get('result', 'resolved')}")
    return "Trade" + ("s" if len(items) > 1 else "") + ": " + "; ".join(parts)


def _fmt_entitlement_expiring(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        days = int(p.get("days_left", 0))
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days}d"
        parts.append(f"**{p['name']}** ({when})")
    return "Expiring soon: " + ", ".join(parts) + " — renew in `/shop`."


def _fmt_gift_received(items: list[dict[str, Any]]) -> str:
    parts = [f"<@{p['from']}> sent you **{p['name']}**" for p in items]
    return "🎁 " + "; ".join(parts) + " — `/equip` it from the shop."


def _fmt_duel_offer(items: list[dict[str, Any]]) -> str:
    parts = [
        f"<@{p['from']}> challenged you — **{format_money(p['stake'])}** each, "
        f"answer in the channel (`/duels list` if you lost the message)"
        for p in items
    ]
    noun = "challenge" if len(items) == 1 else "challenges"
    return f"⚔️ Duel {noun}: " + "; ".join(parts)


def _fmt_duel_result(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        result = p.get("result")
        other = p.get("opponent")
        if result == "won":
            parts.append(
                f"you beat <@{other}> — **{format_money(p['payout'])}** paid out"
            )
        elif result == "lost":
            parts.append(
                f"<@{other}> took the pot — your equity "
                f"{format_money(p['my_equity'])} vs their "
                f"{format_money(p['their_equity'])}"
            )
        elif result == "forfeited":
            parts.append(f"you forfeited to <@{other}> — they take the pot")
        elif result == "tied":
            parts.append(
                f"dead heat with <@{other}> — stakes refunded minus rake"
            )
        elif result == "declined":
            parts.append(f"<@{other}> declined — stake refunded")
        elif result == "expired":
            parts.append(f"your offer to <@{other}> expired — stake refunded")
    noun = "Duel" if len(items) == 1 else "Duels"
    return f"⚔️ {noun}: " + "; ".join(parts)


def _fmt_bounty_placed(items: list[dict[str, Any]]) -> str:
    total = sum(int(p["amount"]) for p in items)
    posters = ", ".join(f"<@{p['poster']}>" for p in items)
    noun = "bounty" if len(items) == 1 else "bounties"
    return (
        f"🎯 {len(items)} open {noun} on your head — **{format_money(total)}** "
        f"total, posted by {posters}. Stay above maintenance."
    )


def _fmt_bounty_paid(items: list[dict[str, Any]]) -> str:
    p = items[0]
    if p.get("role") == "claimant":
        return (
            f"🎯 Collected: **{format_money(p['paid'])}** paid — you held the "
            f"largest position against <@{p['target']}>'s liquidation."
        )
    if p.get("claimant"):
        return (
            f"🎯 <@{p['claimant']}> collected the "
            f"{'bounty' if p['count'] == 1 else 'bounties'} on your head — "
            "biggest opposing exposure in your liquidated tickers."
        )
    return (
        "🎯 The bounties on your head went unclaimed — nobody was positioned "
        "against your liquidated tickers, so the pot burned to the sink."
    )


def _fmt_bounty_expired(items: list[dict[str, Any]]) -> str:
    parts = [
        f"`#{p['bounty_id']}` on <@{p['target']}> "
        f"({format_money(p['amount'])} refunded)"
        for p in items
    ]
    noun = "Bounty" if len(items) == 1 else "Bounties"
    return f"{noun} expired: " + "; ".join(parts) + "."


def _fmt_division_result(items: list[dict[str, Any]]) -> str:
    p = items[0]
    moved = str(p.get("moved") or "")
    tail = {
        "promoted": f"promoted to **{p['new_tier_name']}**",
        "relegated": f"relegated to **{p['new_tier_name']}**",
    }.get(moved, f"holding in **{p['new_tier_name']}**")
    return (
        f"🏆 **{p['tier_name']} Division** week settled — rank #{p['rank']}/"
        f"{p['entrants']}, equity {format_money(p['equity'])}: {tail}. "
        "Next week's season is live."
    )


def _fmt_prop_resolved(items: list[dict[str, Any]]) -> str:
    p = items[0]
    return (
        f"🎲 Prop `#{p['prop_id']}` resolved **{p['outcome']}** — "
        f"\"{p['title']}\". Your cut: **{format_money(p['paid'])}**."
    )


def _fmt_boss_beaten(items: list[dict[str, Any]]) -> str:
    p = items[0]
    return (
        f"🗡️ **Giant Slayer.** You beat {p['boss']}'s week "
        f"({p['your_ret']:+.1%} vs {p['boss_ret']:+.1%}) — "
        "hidden badge earned."
    )


_FORMATTERS: dict[str, Callable[[list[dict[str, Any]]], str]] = {
    "LIQUIDATION": _fmt_liquidation,
    "MARGIN_CALL": _fmt_margin_call,
    "SHORT_RECALL": _fmt_short_recall,
    "KNOCKOUT": _fmt_knockout,
    "ORDER_FILLED": _fmt_order_filled,
    "SEASON_RESULT": _fmt_season_result,
    "ACCOUNT_SUSPENDED": _fmt_account_suspended,
    "BADGE_EARNED": _fmt_badge_earned,
    "IPO_SETTLED": _fmt_ipo_settled,
    "ALERT_TRIGGERED": _fmt_alert_triggered,
    "QUEST_COMPLETED": _fmt_quest_completed,
    "OPTION_SETTLED": _fmt_option_settled,
    "MOMENT_EARNED": _fmt_moment_earned,
    "TRADE_OFFER": _fmt_trade_offer,
    "TRADE_RESULT": _fmt_trade_result,
    "ENTITLEMENT_EXPIRING": _fmt_entitlement_expiring,
    "GIFT_RECEIVED": _fmt_gift_received,
    "DUEL_OFFER": _fmt_duel_offer,
    "DUEL_RESULT": _fmt_duel_result,
    "BOUNTY_PLACED": _fmt_bounty_placed,
    "BOUNTY_PAID": _fmt_bounty_paid,
    "BOUNTY_EXPIRED": _fmt_bounty_expired,
    "DIVISION_RESULT": _fmt_division_result,
    "PROP_RESOLVED": _fmt_prop_resolved,
    "BOSS_BEATEN": _fmt_boss_beaten,
}


async def _mark_sent(conn: AsyncConnection, ids: list[int]) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE notifications SET sent_at = now() WHERE id = ANY(%s)", (ids,)
        )


async def _dead_letter_user(conn: AsyncConnection, user_id: int, reason: str) -> None:
    """Forbidden (DMs closed): stop retrying *every* pending row for this
    user right away, not just the ones in the current batch -- otherwise
    the next poll just rediscovers the same failure and repeats it."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE notifications SET attempts = %s, last_error = %s
            WHERE user_id = %s AND sent_at IS NULL
            """,
            (MAX_ATTEMPTS, reason, user_id),
        )


async def _retry_later(conn: AsyncConnection, ids: list[int], error: str) -> None:
    """Bump attempts and schedule the next try with exponential backoff.
    Every expression here reads the *old* row (Postgres UPDATE semantics
    -- SET targets never see each other's new values), so `attempts + 1`
    consistently means "the attempt count this failure produces"."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE notifications
            SET attempts = attempts + 1,
                last_error = %s,
                next_attempt_at = now() + (
                    CASE WHEN attempts + 1 <= array_length(%s::int[], 1)
                         THEN (%s::int[])[attempts + 1]
                         ELSE (%s::int[])[array_length(%s::int[], 1)]
                    END * interval '1 minute'
                )
            WHERE id = ANY(%s)
            """,
            (
                error,
                list(_BACKOFF_MINUTES),
                list(_BACKOFF_MINUTES),
                list(_BACKOFF_MINUTES),
                list(_BACKOFF_MINUTES),
                ids,
            ),
        )


async def poll_once(conn: AsyncConnection, deliver: Deliver) -> dict[str, int]:
    """One poll cycle: fetch pending rows, coalesce, deliver, and record
    the outcome -- all in the caller's transaction (one poll = one tx).

    Returns delivery stats for the bot heartbeat detail.
    """
    rows = await _fetch_batch(conn)
    sent = failed = dead_lettered = 0
    rows.sort(key=_group_key)
    for (user_id, kind, _tick), group_iter in groupby(rows, key=_group_key):
        group = list(group_iter)
        ids = [int(r["id"]) for r in group]
        payloads = [r["payload"] for r in group]
        formatter = _FORMATTERS.get(kind)
        message = formatter(payloads) if formatter else f"{kind} notification"
        try:
            await deliver(user_id, message)
        except DeliveryForbidden:
            await _dead_letter_user(conn, user_id, "forbidden: dm closed")
            dead_lettered += len(ids)
        except Exception as exc:  # noqa: BLE001 -- any delivery failure retries
            log.warning("notification delivery failed user=%s kind=%s: %s", user_id, kind, exc)
            await _retry_later(conn, ids, f"{type(exc).__name__}: {exc}"[:200])
            failed += len(ids)
        else:
            await _mark_sent(conn, ids)
            sent += len(ids)
    return {"sent": sent, "failed": failed, "dead_lettered": dead_lettered}
