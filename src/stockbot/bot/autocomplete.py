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
from psycopg.rows import dict_row

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
    can't autocomplete -- it doesn't exist yet). Sector keys live on
    `sectors.key`, not instruments (which has sector_id); 'index' is
    excluded because add_instrument refuses it."""
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            "SELECT key FROM sectors WHERE key <> 'index' ORDER BY key"
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
    """The caller's open orders only (N5: pickers never leak others').
    The text filter runs in SQL -- a Python-side filter over a LIMIT-100
    window would under-fill the picker for users with >100 open orders."""
    cur = current.strip()
    id_like = f"%{cur.lstrip('#')}%" if cur else "%"
    tick_like = f"%{cur.upper()}%" if cur else "%"
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT o.id, o.side, o.quantity - o.filled_quantity, i.ticker,
                   o.order_type, o.limit_price, o.stop_price
            FROM orders o JOIN instruments i ON i.id = o.instrument_id
            WHERE o.user_id = %s AND o.status = 'OPEN'
              AND (CAST(o.id AS TEXT) LIKE %s OR i.ticker LIKE %s)
            ORDER BY o.id DESC LIMIT 100
            """,
            (interaction.user.id, id_like, tick_like),
        )
        rows = await cursor.fetchall()
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
        choices.append(app_commands.Choice(name=label[:100], value=int(oid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def admin_order_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Every open order across users -- for /admin order-cancel, whose
    whole job is fixing OTHER people's orders."""
    cur = current.strip()
    id_like = f"%{cur.lstrip('#')}%" if cur else "%"
    tick_like = f"%{cur.upper()}%" if cur else "%"
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT o.id, o.side, o.quantity - o.filled_quantity, i.ticker,
                   o.user_id
            FROM orders o JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'OPEN'
              AND (CAST(o.id AS TEXT) LIKE %s OR i.ticker LIKE %s)
            ORDER BY o.id DESC LIMIT 100
            """,
            (id_like, tick_like),
        )
        rows = await cursor.fetchall()
    choices = []
    for oid, side, remaining, ticker, uid in rows:
        label = f"#{oid} {side} {remaining:,} {ticker} (user {uid})"
        choices.append(app_commands.Choice(name=label[:100], value=int(oid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def short_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """The caller's open bounded shorts."""
    cur = current.strip()
    id_like = f"%{cur.lstrip('#')}%" if cur else "%"
    tick_like = f"%{cur.upper()}%" if cur else "%"
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT s.id, i.ticker, s.quantity, s.entry_price
            FROM bounded_shorts s JOIN instruments i ON i.id = s.instrument_id
            WHERE s.user_id = %s AND s.status = 'OPEN'
              AND (CAST(s.id AS TEXT) LIKE %s OR i.ticker LIKE %s)
            ORDER BY s.id DESC LIMIT 100
            """,
            (interaction.user.id, id_like, tick_like),
        )
        rows = await cursor.fetchall()
    choices = []
    for sid, ticker, quantity, entry in rows:
        label = f"#{sid} {quantity:,} {ticker} @ {format_price(entry)}"
        choices.append(app_commands.Choice(name=label[:100], value=int(sid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def alert_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """The caller's open price alerts."""
    cur = current.strip()
    id_like = f"%{cur.lstrip('#')}%" if cur else "%"
    tick_like = f"%{cur.upper()}%" if cur else "%"
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT a.id, i.ticker, a.direction, a.target_price
            FROM price_alerts a JOIN instruments i ON i.id = a.instrument_id
            WHERE a.user_id = %s AND a.status = 'OPEN'
              AND (CAST(a.id AS TEXT) LIKE %s OR i.ticker LIKE %s)
            ORDER BY a.id DESC LIMIT 100
            """,
            (interaction.user.id, id_like, tick_like),
        )
        rows = await cursor.fetchall()
    choices = []
    for aid, ticker, direction, target in rows:
        label = f"#{aid} {ticker} {direction.lower()} {format_price(target)}"
        choices.append(app_commands.Choice(name=label[:100], value=int(aid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def option_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """The caller's open option positions."""
    cur = current.strip()
    id_like = f"%{cur.lstrip('#')}%" if cur else "%"
    tick_like = f"%{cur.upper()}%" if cur else "%"
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT o.id, i.ticker, o.side, o.strike, o.quantity, o.expiry_tick
            FROM option_positions o JOIN instruments i ON i.id = o.instrument_id
            WHERE o.user_id = %s AND o.status = 'OPEN'
              AND (CAST(o.id AS TEXT) LIKE %s OR i.ticker LIKE %s)
            ORDER BY o.id DESC LIMIT 100
            """,
            (interaction.user.id, id_like, tick_like),
        )
        rows = await cursor.fetchall()
    choices = []
    for oid, ticker, side, strike, quantity, expiry in rows:
        label = (
            f"#{oid} {side} {quantity:,} {ticker} "
            f"K={format_price(strike)} exp t{expiry}"
        )
        choices.append(app_commands.Choice(name=label[:100], value=int(oid)))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


async def shop_item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with db.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(
            "SELECT key FROM shop_items WHERE price_minor IS NOT NULL "
            "OR key IN ('slot', 'margin_tier') ORDER BY key"
        )
        keys = [str(r[0]) for r in await cursor.fetchall()]
    cur = current.strip().lower()
    return [
        app_commands.Choice(name=k, value=k)
        for k in keys
        if not cur or k.startswith(cur)
    ][:MAX_CHOICES]


async def equipped_item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Owned equippables: TITLEs plus COSMETICs carrying a theme palette."""
    from stockbot.shop.service import is_theme_item

    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT s.key, s.name, s.kind, s.metadata
            FROM entitlements e
            JOIN shop_items s ON s.key = e.item_key
            WHERE e.user_id = %s
              AND (e.expires_at IS NULL OR e.expires_at > now())
              AND (s.kind = 'TITLE' OR s.kind = 'COSMETIC')
            ORDER BY s.kind, s.key
            """,
            (interaction.user.id,),
        )
        rows = await cur.fetchall()
    cur_s = current.strip().lower()
    return [
        app_commands.Choice(name=f"{r['key']} — {r['name']}", value=str(r["key"]))
        for r in rows
        if r["kind"] == "TITLE" or is_theme_item(str(r["kind"]), r["metadata"])
        if not cur_s or str(r["key"]).startswith(cur_s)
    ][:MAX_CHOICES]


async def quest_reroll_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """The clicker's incomplete visible quests -- reroll targets."""
    from stockbot.quests.service import list_quests

    async with db.connection() as conn:
        rows = await list_quests(conn, interaction.user.id, 0)
    cur = current.strip().lower()
    return [
        app_commands.Choice(
            name=f"{r['name']} ({r['period'].lower()})", value=str(r["id"])
        )
        for r in rows
        if not r["completed"]
        if not cur or cur in str(r["name"]).lower()
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


async def card_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Card picker by name or key -- 99 catalog rows, queried fresh
    (sets are frozen so a cache would only help under churn)."""
    async with db.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT key, name FROM cards ORDER BY key")
        rows = [(str(r[0]), str(r[1])) for r in await cur.fetchall()]
    q = current.strip().lower()
    return [
        app_commands.Choice(name=name, value=key)
        for key, name in rows
        if not q or q in name.lower() or key.startswith(q)
    ][:MAX_CHOICES]


async def owned_card_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """The clicker's held cards -- /craft upgrade and /feature targets."""
    async with db.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.key, c.name, u.best_frame FROM user_cards u
            JOIN cards c ON c.key = u.card_key
            WHERE u.user_id = %s ORDER BY c.key
            """,
            (interaction.user.id,),
        )
        rows = [(str(r[0]), str(r[1]), str(r[2])) for r in await cur.fetchall()]
    q = current.strip().lower()
    return [
        app_commands.Choice(name=f"{name} ({frame})", value=key)
        for key, name, frame in rows
        if not q or q in name.lower() or key.startswith(q)
    ][:MAX_CHOICES]


async def their_card_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """The `user:` option's held cards -- /trade want: targets. Falls
    back to the full catalog if the counterparty isn't resolved yet."""
    other = getattr(interaction.namespace, "user", None)
    # Namespace values for USER options may be a resolved member, a raw
    # snowflake int, or a string depending on what Discord resolved --
    # coerce whatever shows up.
    other_id = getattr(other, "id", other)
    try:
        other_id = int(other_id)
    except (TypeError, ValueError):
        return await card_autocomplete(interaction, current)
    async with db.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.key, c.name, u.best_frame FROM user_cards u
            JOIN cards c ON c.key = u.card_key
            WHERE u.user_id = %s ORDER BY c.key
            """,
            (other_id,),
        )
        rows = [(str(r[0]), str(r[1]), str(r[2])) for r in await cur.fetchall()]
    q = current.strip().lower()
    return [
        app_commands.Choice(name=f"{name} ({frame})", value=key)
        for key, name, frame in rows
        if not q or q in name.lower() or key.startswith(q)
    ][:MAX_CHOICES]


async def pack_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Openable packs the clicker holds -- /open targets."""
    async with db.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT s.key, s.name, e.quantity FROM entitlements e
            JOIN shop_items s ON s.key = e.item_key
            WHERE e.user_id = %s AND s.kind = 'CONSUMABLE'
              AND s.metadata ? 'cards'
              AND (e.expires_at IS NULL OR e.expires_at > now())
            ORDER BY s.key
            """,
            (interaction.user.id,),
        )
        rows = [(str(r[0]), str(r[1]), int(r[2])) for r in await cur.fetchall()]
    q = current.strip().lower()
    return [
        app_commands.Choice(name=f"{name} ×{qty}", value=key)
        for key, name, qty in rows
        if not q or key.startswith(q)
    ][:MAX_CHOICES]
