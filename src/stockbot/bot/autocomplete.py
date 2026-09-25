"""Slash-command autocomplete providers (N5).

Ticker choices come from a TTL-cached (ticker, name) list so the DB
isn't hit per keystroke. Entity pickers (orders, shorts) are per-user
and queried fresh -- they must reflect state that's seconds old, and a
scoped LIMIT-25 query is cheap.
"""

from __future__ import annotations

import time

import discord
from discord import app_commands

from stockbot import db
from stockbot.admin.service import TUNABLE_PARAMS
from stockbot.bot.format import format_price

MAX_CHOICES = 25
_TICKER_TTL_SECONDS = 30.0

_ticker_cache: tuple[float, list[tuple[str, str]]] | None = None


async def ticker_list() -> list[tuple[str, str]]:
    """(ticker, name) for every active instrument, cached for TTL."""
    global _ticker_cache
    now = time.monotonic()
    if _ticker_cache is None or now - _ticker_cache[0] > _TICKER_TTL_SECONDS:
        async with db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT ticker, name FROM instruments "
                "WHERE is_active ORDER BY ticker"
            )
            rows = await cur.fetchall()
        _ticker_cache = (now, [(str(t), str(n)) for t, n in rows])
    return _ticker_cache[1]


def _ticker_matches(
    tickers: list[tuple[str, str]], current: str
) -> list[tuple[str, str]]:
    """Prefix on the ticker first; if that underfills the picker, fall
    back to a name-prefix match so 'north' still finds NORT."""
    cur = current.strip().upper()
    if not cur:
        return tickers
    matches = [(t, n) for t, n in tickers if t.startswith(cur)]
    if len(matches) < MAX_CHOICES:
        seen = {t for t, _ in matches}
        matches += [
            (t, n)
            for t, n in tickers
            if t not in seen and n.upper().startswith(cur)
        ]
    return matches


async def ticker_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    matches = _ticker_matches(await ticker_list(), current)
    return [
        app_commands.Choice(name=f"{t} — {n}"[:100], value=t)
        for t, n in matches[:MAX_CHOICES]
    ]


async def sector_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Sector keys for /admin instrument-add (the new listing's ticker
    can't autocomplete -- it doesn't exist yet)."""
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            "SELECT DISTINCT sector_key FROM instruments ORDER BY sector_key"
        )
        keys = [str(r[0]) for r in await cursor.fetchall()]
    cur = current.strip().upper()
    return [
        app_commands.Choice(name=k, value=k)
        for k in keys
        if not cur or k.startswith(cur)
    ][:MAX_CHOICES]


async def order_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """The caller's open orders only (N5: pickers never leak others')."""
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT o.id, o.side, o.quantity - o.filled_quantity, i.ticker,
                   o.order_type, o.limit_price, o.stop_price
            FROM orders o JOIN instruments i ON i.id = o.instrument_id
            WHERE o.user_id = %s AND o.status = 'OPEN'
            ORDER BY o.id DESC LIMIT 100
            """,
            (interaction.user.id,),
        )
        rows = await cursor.fetchall()
    cur = current.strip()
    choices = []
    for oid, side, remaining, ticker, otype, limit, stop in rows:
        anchor = (
            f"@ {format_price(limit)}"
            if otype == "LIMIT"
            else (
                f"stop {format_price(stop)}"
                if otype == "STOP"
                else f"stop {format_price(stop)} lim {format_price(limit)}"
            )
        )
        label = f"#{oid} {side} {remaining:,} {ticker} {anchor}"
        if cur and cur.lstrip("#") not in str(oid) and cur.upper() not in str(ticker):
            continue
        choices.append(app_commands.Choice(name=label[:100], value=int(oid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def admin_order_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Every open order across users -- for /admin order-cancel, whose
    whole job is fixing OTHER people's orders."""
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT o.id, o.side, o.quantity - o.filled_quantity, i.ticker,
                   o.user_id
            FROM orders o JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
            ORDER BY o.id DESC LIMIT 100
            """,
        )
        rows = await cursor.fetchall()
    cur = current.strip()
    choices = []
    for oid, side, remaining, ticker, uid in rows:
        label = f"#{oid} {side} {remaining:,} {ticker} (user {uid})"
        if cur and cur.lstrip("#") not in str(oid) and cur.upper() not in str(ticker):
            continue
        choices.append(app_commands.Choice(name=label[:100], value=int(oid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def short_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """The caller's open bounded shorts."""
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT s.id, i.ticker, s.quantity, s.entry_price
            FROM bounded_shorts s JOIN instruments i ON i.id = s.instrument_id
            WHERE s.user_id = %s AND s.status = 'OPEN'
            ORDER BY s.id DESC LIMIT 100
            """,
            (interaction.user.id,),
        )
        rows = await cursor.fetchall()
    cur = current.strip()
    choices = []
    for sid, ticker, quantity, entry in rows:
        label = f"#{sid} {quantity:,} {ticker} @ {format_price(entry)}"
        if cur and cur.lstrip("#") not in str(sid) and cur.upper() not in str(ticker):
            continue
        choices.append(app_commands.Choice(name=label[:100], value=int(sid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def shop_item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute("SELECT key FROM shop_items ORDER BY key")
        keys = [str(r[0]) for r in await cursor.fetchall()]
    cur = current.strip().lower()
    return [
        app_commands.Choice(name=k, value=k)
        for k in keys
        if not cur or k.startswith(cur)
    ][:MAX_CHOICES]


async def tunable_param_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cur = current.strip().lower()
    return [
        app_commands.Choice(name=p, value=p)
        for p in TUNABLE_PARAMS
        if not cur or p.startswith(cur)
    ][:MAX_CHOICES]
