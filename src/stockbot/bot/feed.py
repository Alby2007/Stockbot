"""Public tape delivery: poll `feed_items`, coalesce pending rows into ONE
message per channel per poll, and hand it to an injectable `deliver`
callable -- tests substitute a stub, no Discord needed.

Cloned from notify.py's outbox mechanics (SKIP LOCKED fetch, MAX_ATTEMPTS,
backoff table) with two deliberate changes:

- Grouping is (channel, kind, user_id, tick_index) -> one LINE; all lines
  join into a single channel post. A liquidation cascade produces one
  digest, not ten posts -- inherently rate-safe and reads like a tape.
- A dead channel (Forbidden/NotFound -> `ChannelGone`) deletes the
  `feed_channels` row itself: the feed analogue of `_dead_letter_user`,
  but self-healing at the binding level. ON DELETE CASCADE drops its
  pending items.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from itertools import groupby
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.bot.format import format_money, format_price

log = logging.getLogger("stockbot.bot.feed")

MAX_ATTEMPTS = 5
POLL_BATCH = 200  # rows, not messages -- a poll gathers a burst into one post
# Exponential backoff in minutes, capped; index by (attempts - 1).
_BACKOFF_MINUTES = (1, 5, 15, 60, 240)
_MAX_MESSAGE = 1900  # under the 2000 Discord cap with headroom
_MAX_LINES = 20


class ChannelGone(Exception):
    """Raised by `deliver` when the bound channel is gone or the bot lost
    access (Forbidden/NotFound) -- the poll unbinds the channel instead of
    retrying a delivery that can never succeed."""


Deliver = Callable[[int, str], Awaitable[None]]


async def _fetch_batch(conn: AsyncConnection) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT f.id, f.channel_id, f.kind, f.user_id, f.tick_index, f.payload
            FROM feed_items f
            WHERE f.sent_at IS NULL AND f.attempts < %s
              AND f.next_attempt_at <= now()
            ORDER BY f.channel_id, f.id
            LIMIT %s
            FOR UPDATE OF f SKIP LOCKED
            """,
            (MAX_ATTEMPTS, POLL_BATCH),
        )
        return await cur.fetchall()


def _group_key(row: dict[str, Any]) -> tuple[int, str, Any, Any]:
    return (
        int(row["channel_id"]),
        str(row["kind"]),
        row["user_id"],
        row["tick_index"],
    )


def _mention(user_id: Any) -> str:
    return f"<@{int(user_id)}>" if user_id is not None else "someone"


# Event-class anonymity (NPC plan C8): market-mechanics kinds render
# UNATTRIBUTED for everyone -- that's how a real tape works, it makes
# NPC prints indistinguishable from human ones (no "no name = bot"
# tell), and it retires the liquidation-shaming vector as a bonus.
# Human-achievement kinds (jackpot, season results) keep <@id> mentions:
# NPCs can't reach them (no claims, no seasons) so there's no leak.
def _fmt_liquidation(items: list[dict[str, Any]]) -> str:
    parts = []
    total_penalty = 0
    for p in items:
        verb = "covered" if p["side"] == "BUY" else "sold"
        parts.append(f"{verb} {p['qty']:,} {p['ticker']}")
        total_penalty += int(p.get("penalty", 0))
    return (
        "💀 a trader was liquidated: " + ", ".join(parts)
        + (f" — {format_money(total_penalty)} penalty" if total_penalty else "")
    )


def _fmt_squeeze(items: list[dict[str, Any]]) -> str:
    parts = [f"{p['qty']:,} {p['ticker']}" for p in items]
    return (
        "📣 borrow recall forced a crowded short to cover: "
        + ", ".join(parts)
    )


def _fmt_knockout(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['ticker']} {p['qty']:,} @ {format_price(p['entry'])} "
        f"(KO {format_price(p['ko_price'])})"
        for p in items
    ]
    return "🥊 bounded short knocked out: " + ", ".join(parts)


def _fmt_whale(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['side']} {p['qty']:,} {p['ticker']} @ {format_price(p['fill'])} "
        f"({format_money(p['notional'])})"
        for p in items
    ]
    return "🐋 whale print: " + ", ".join(parts)


def _fmt_jackpot(items: list[dict[str, Any]]) -> str:
    parts = [format_money(p["amount"]) for p in items]
    return (
        f"🎰 {_mention(items[0].get('subject'))} hit the daily jackpot: "
        + ", ".join(parts)
    )


def _fmt_ipo(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['ticker']} IPO'd @ {format_price(p['price'])} — "
        f"{p['shares']:,} shares across {p['holders']} holder"
        + ("s" if p["holders"] != 1 else "")
        for p in items
    ]
    return "📈 " + "; ".join(parts)


def _fmt_option_payout(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['ticker']} {p['side'].lower()} {format_price(p['strike'])} "
        f"x{p['qty']:,} paid {format_money(p['payout'])}"
        for p in items
    ]
    return "💰 option payout: " + ", ".join(parts)


def _fmt_season_result(items: list[dict[str, Any]]) -> str:
    p = items[0]
    head = f"🏆 {p['season_name']} ended"
    if p.get("winner_id") is not None:
        return (
            f"{head} — winner {_mention(p['winner_id'])}"
            + (f", prize {format_money(p['prize'])}" if p.get("prize") else "")
            + f" ({p['entrants']} entrants)"
        )
    return f"{head} — no qualifiers ({p['entrants']} entrants)"


def _fmt_market_news(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        pct = float(p["magnitude"]) * 100
        parts.append(f"{p['ticker']} {pct:+.1f}%")
    return "📰 " + ", ".join(parts)


def _fmt_card_pull(items: list[dict[str, Any]]) -> str:
    # Achievement class (like JACKPOT): the mention stays -- NPCs can't
    # reach packs, so attribution is safe. Serial shown when the pull
    # was broadcast for being a low mint (#1-#10 chase prints).
    parts = [
        f"{_mention(p['subject'])} pulled {p['tier']} {p['name']}"
        + (f" #{p['serial']}" if p.get("serial") else "")
        for p in items
    ]
    return "🃏 " + "; ".join(parts)


def _fmt_trade_completed(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        gave = ", ".join(p.get("give") or []) or "shards"
        want = ", ".join(p.get("want") or []) or "shards"
        parts.append(f"{_mention(p['subject'])} ⇄ <@{p['other']}>: {gave} for {want}")
    return "🔄 " + "; ".join(parts)


def _fmt_top_pull(items: list[dict[str, Any]]) -> str:
    p = items[0]
    serial = f" #{p['serial']}" if p.get("serial") else ""
    return (
        f"🏆 pull of the day: {_mention(p['subject'])} — "
        f"{p['tier']} **{p['name']}**{serial}"
    )


def _fmt_moment_minted(items: list[dict[str, Any]]) -> str:
    # Market history, not an achievement: no subject mention, just the
    # minted moment and how many holders witnessed it.
    parts = [
        f"**{p['name']}** minted — {p.get('awarded', 0)} witnesses"
        for p in items
    ]
    return "📸 " + "; ".join(parts)


def _fmt_gift(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{_mention(p['from'])} sent {p['name']} to {_mention(p['to'])}"
        for p in items
    ]
    return "🎁 " + "; ".join(parts)


def _fmt_listing(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{p['ticker']} commissioned by {_mention(p['subject'])}"
        for p in items
    ]
    return "📈 " + "; ".join(parts)


def _fmt_deal(items: list[dict[str, Any]]) -> str:
    p = items[0]
    pct = round(float(p["pct"]) * 100)
    return f"🎉 Today's deal: {p['name']} −{pct}% — `/shop item:{p['item_key']}`"


def _fmt_duel_started(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{_mention(p['challenger'])} vs {_mention(p['opponent'])} — "
        f"{format_money(p['stake'])} each on the line"
        for p in items
    ]
    return "⚔️ duel" + ("s" if len(items) > 1 else "") + " on: " + "; ".join(parts)


def _fmt_duel_settled(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        if p.get("winner") is None:
            parts.append(
                f"{_mention(p['loser'])}'s duel ended in a dead heat — stakes refunded"
            )
        elif p.get("forfeit"):
            parts.append(
                f"{_mention(p['winner'])} took the pot after "
                f"{_mention(p['loser'])} forfeited — {format_money(p['payout'])}"
            )
        else:
            parts.append(
                f"{_mention(p['winner'])} took {_mention(p['loser'])}'s "
                f"{format_money(p['stake'])} — {format_money(p['payout'])} paid"
            )
    return "⚔️ " + "; ".join(parts)


def _fmt_bounty_posted(items: list[dict[str, Any]]) -> str:
    parts = [
        f"{format_money(p['amount'])} on "
        f"{p['target_name'] + ' (bot)' if p.get('target_name') else _mention(p['target'])}'s head"
        for p in items
    ]
    return "🎯 new bount" + ("ies" if len(items) > 1 else "y") + ": " + "; ".join(parts)


def _fmt_bounty_paid(items: list[dict[str, Any]]) -> str:
    p = items[0]
    count = p.get("count", 1)
    noun = "bounty" if count == 1 else f"{count} bounties"
    target = (
        p["target_name"] + " (bot)"
        if p.get("target_name")
        else _mention(p["target"])
    )
    if p.get("claimant") is not None:
        return (
            f"🎯 {_mention(p['claimant'])} collected the {noun} on "
            f"{target}'s head — {format_money(p['paid'])} paid"
        )
    return (
        f"🎯 {noun.capitalize()} on {target}'s head went "
        "unclaimed — nobody was positioned against them; the pot burned"
    )


def _fmt_division_week(items: list[dict[str, Any]]) -> str:
    parts = []
    for p in items:
        head = f"**{p['tier_name']}** week settled"
        if p.get("winner_id") is not None:
            head += f" — {_mention(p['winner_id'])} on top ({p['entrants']} entrants)"
        moves = []
        if p.get("promoted"):
            moves.append("promoted " + ", ".join(_mention(u) for u in p["promoted"]))
        if p.get("relegated"):
            moves.append("relegated " + ", ".join(_mention(u) for u in p["relegated"]))
        parts.append(head + ("; " + "; ".join(moves) if moves else ""))
    return "🏆 " + " · ".join(parts)


def _fmt_prop_opened(items: list[dict[str, Any]]) -> str:
    p = items[0]
    return (
        f"🎲 new prop `#{p['prop_id']}`: \"{p['title']}\" — "
        "take a side with /props bet"
    )


def _fmt_prop_resolved(items: list[dict[str, Any]]) -> str:
    p = items[0]
    if p.get("outcome"):
        return (
            f"🎲 prop `#{p['prop_id']}` resolved **{p['outcome']}** — "
            f"\"{p['title']}\" ({format_money(p['pool'])} in the pool)"
        )
    return (
        f"🎲 prop `#{p['prop_id']}` voided — \"{p['title']}\", stakes refunded"
    )


def _fmt_chaos_crash(items: list[dict[str, Any]]) -> str:
    p = items[0]
    return (
        f"💥 **FLASH CRASH** — every stock just gapped "
        f"{float(p['pct']):.1%} lower. Check your margin."
    )


def _fmt_chaos_vol(items: list[dict[str, Any]]) -> str:
    p = items[0]
    return (
        f"🌪️ **VOLATILITY STORM** — sigma ×{float(p['mult']):.1f} "
        f"until tick {p['end_tick']}."
    )


def _fmt_chaos_fee(items: list[dict[str, Any]]) -> str:
    p = items[0]
    label = p.get("label") or "fee surge"
    return (
        f"📞 **{label.upper()}** — borrow costs ×{float(p['mult']):.1f} "
        f"until tick {p['end_tick']}. Shorts bleed double."
    )


def _fmt_chaos_ended(items: list[dict[str, Any]]) -> str:
    p = items[0]
    names = {
        "FLASH_CRASH": "the dust settles after the flash crash",
        "VOL_STORM": "the volatility storm passes",
        "FEE_SURGE": "borrow rates normalize",
    }
    return f"🌤️ {names.get(str(p['kind']), 'the chaos passes')}."


def _fmt_persona_taunt(items: list[dict[str, Any]]) -> str:
    p = items[0]
    return f"🤖 {p['line']}"


def _fmt_boss_beaten(items: list[dict[str, Any]]) -> str:
    p = items[0]
    who = ", ".join(_mention(u) for u in p.get("beaten_by", []))
    return (
        f"🗡️ {who} out-traded **{p['boss']}** all week "
        f"({p['boss_ret']:+.1%} for the boss). Giant Slayer badges out."
    )


_FORMATTERS: dict[str, Callable[[list[dict[str, Any]]], str]] = {
    "LIQUIDATION": _fmt_liquidation,
    "SQUEEZE": _fmt_squeeze,
    "KNOCKOUT": _fmt_knockout,
    "WHALE": _fmt_whale,
    "JACKPOT": _fmt_jackpot,
    "IPO": _fmt_ipo,
    "OPTION_PAYOUT": _fmt_option_payout,
    "SEASON_RESULT": _fmt_season_result,
    "NEWS_LANDED": _fmt_market_news,
    "EARNINGS": _fmt_market_news,
    "CARD_PULL": _fmt_card_pull,
    "MOMENT_MINTED": _fmt_moment_minted,
    "TOP_PULL": _fmt_top_pull,
    "TRADE_COMPLETED": _fmt_trade_completed,
    "GIFT": _fmt_gift,
    "LISTING": _fmt_listing,
    "DEAL": _fmt_deal,
    "DUEL_STARTED": _fmt_duel_started,
    "DUEL_SETTLED": _fmt_duel_settled,
    "BOUNTY_POSTED": _fmt_bounty_posted,
    "BOUNTY_PAID": _fmt_bounty_paid,
    "DIVISION_WEEK": _fmt_division_week,
    "PROP_OPENED": _fmt_prop_opened,
    "PROP_RESOLVED": _fmt_prop_resolved,
    "CHAOS_FLASH_CRASH": _fmt_chaos_crash,
    "CHAOS_VOL_STORM": _fmt_chaos_vol,
    "CHAOS_FEE_SURGE": _fmt_chaos_fee,
    "CHAOS_ENDED": _fmt_chaos_ended,
    "PERSONA_TAUNT": _fmt_persona_taunt,
    "BOSS_BEATEN": _fmt_boss_beaten,
}


async def _mark_sent(conn: AsyncConnection, ids: list[int]) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE feed_items SET sent_at = now() WHERE id = ANY(%s)", (ids,)
        )


async def _unbind_channel(conn: AsyncConnection, channel_id: int, reason: str) -> None:
    """The channel is gone: drop the binding. ON DELETE CASCADE takes its
    pending items with it -- self-healing at the binding level, so a
    guild that deletes the tape channel stops generating work instantly."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM feed_channels WHERE channel_id = %s", (channel_id,)
        )
    log.info("feed channel %s unbound (%s)", channel_id, reason)


async def _retry_later(conn: AsyncConnection, ids: list[int], error: str) -> None:
    """Same backoff semantics as notify.py: every expression reads the old
    row, so `attempts + 1` is the count this failure produces."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE feed_items
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


def _build_message(
    groups: list[tuple[tuple[int, str, Any, Any], list[dict[str, Any]]]],
) -> str:
    """Render grouped rows into a single tape post, truncated to the
    message cap with an "…and N more" overflow tag."""
    lines: list[str] = []
    for (_channel_id, kind, _user_id, _tick), group in groups:
        payloads = [dict(r["payload"], subject=r["user_id"]) for r in group]
        formatter = _FORMATTERS.get(kind)
        lines.append(
            formatter(payloads) if formatter else f"{kind} x{len(group)}"
        )
    shown = len(lines)
    if len(lines) > _MAX_LINES:
        shown = _MAX_LINES
        lines = lines[:_MAX_LINES]
    message = "\n".join(lines)
    while len(message) > _MAX_MESSAGE and len(lines) > 1:
        shown -= 1
        lines = lines[:shown]
        message = "\n".join(lines)
    if len(message) > _MAX_MESSAGE:
        # A single group rendered past the cap (e.g. a huge liquidation
        # cascade): hard-truncate the line rather than drop the drama.
        message = message[: _MAX_MESSAGE - 1] + "…"
    elif shown < len(groups):
        message += f"\n…and {len(groups) - shown} more"
    return message


async def poll_once(conn: AsyncConnection, deliver: Deliver) -> dict[str, int]:
    """One poll cycle: fetch pending rows, coalesce per channel into ONE
    message, deliver, and record the outcome -- all in the caller's
    transaction (one poll = one tx, same as notify.py).

    Returns delivery stats for the bot heartbeat detail.
    """
    rows = await _fetch_batch(conn)
    sent = failed = dead_lettered = 0
    rows.sort(key=lambda r: int(r["channel_id"]))
    for channel_id, channel_iter in groupby(rows, key=lambda r: int(r["channel_id"])):
        channel_rows = sorted(list(channel_iter), key=_group_key)
        groups = [(k, list(g)) for k, g in groupby(channel_rows, key=_group_key)]
        ids = [int(r["id"]) for r in channel_rows]
        if not groups:
            continue
        message = _build_message(groups)
        # Overflow groups are dropped from the post (tagged "…and N more")
        # but still marked sent: the per-poll single message IS the
        # anti-spam cap, and a backed-up feed shouldn't lag the tape.
        try:
            await deliver(channel_id, message)
        except ChannelGone as exc:
            await _unbind_channel(conn, channel_id, str(exc))
            dead_lettered += len(ids)
        except Exception as exc:  # noqa: BLE001 -- transient failures retry
            log.warning("feed delivery failed channel=%s: %s", channel_id, exc)
            await _retry_later(conn, ids, f"{type(exc).__name__}: {exc}"[:200])
            failed += len(ids)
        else:
            await _mark_sent(conn, ids)
            sent += len(ids)
    return {"sent": sent, "failed": failed, "dead_lettered": dead_lettered}
