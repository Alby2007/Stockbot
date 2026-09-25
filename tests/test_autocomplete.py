"""Phase N5: autocomplete — ticker resolver cap/prefix, per-user pickers."""

from __future__ import annotations

import random
import time
from decimal import Decimal
from unittest.mock import MagicMock

import discord

from stockbot import db
from stockbot.accounts.service import bootstrap_user
from stockbot.bot.autocomplete import (
    MAX_CHOICES,
    _ticker_matches,
    order_autocomplete,
    sector_autocomplete,
    short_autocomplete,
    ticker_autocomplete,
    tunable_param_autocomplete,
)
from stockbot.config import get_settings
from stockbot.orders.service import place_order
from stockbot.shorts.service import open_bounded_short


def _fresh_user() -> int:
    """A unique, comfortably >30d-old Discord snowflake: the high bits are
    ms since the Discord epoch (2015-01-01), the low 22 are random. Bare
    randrange ids land in the future and stay grant-pending forever."""
    rng = random.SystemRandom()
    age_ms = rng.randrange(31, 4000) * 86_400_000
    ts_ms = int(time.time() * 1000) - 1_420_070_400_000 - age_ms
    return (ts_ms << 22) | rng.randrange(1, 1 << 22)


def _user_interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    return interaction


def test_ticker_matches_prefix_first_then_name() -> None:
    tickers = [
        ("NORT", "Northwind Traders"),
        ("NORA", "Nordic Air"),
        ("APPL", "Apple-ish"),
        ("ACME", "Acme Corp"),
    ]
    assert [t for t, _ in _ticker_matches(tickers, "NO")] == ["NORT", "NORA"]
    # Name-prefix fallback kicks in when ticker prefixes underfill.
    assert [t for t, _ in _ticker_matches(tickers, "ACME")] == ["ACME"]
    assert [t for t, _ in _ticker_matches(tickers, "north")] == ["NORT"]
    assert _ticker_matches(tickers, "") == tickers


def test_ticker_matches_returns_all_prefix_hits() -> None:
    """The cap lives in ticker_autocomplete -- the matcher itself must
    not truncate (a name-prefix fallback would lose real matches)."""
    tickers = [(f"T{i:02d}", f"Name {i}") for i in range(100)]
    assert len(_ticker_matches(tickers, "T")) == 100


async def test_ticker_autocomplete_caps_and_prefixes() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        interaction = _user_interaction(_fresh_user())
        all_choices = await ticker_autocomplete(interaction, "")
        assert 0 < len(all_choices) <= MAX_CHOICES

        async with db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT ticker, name FROM instruments WHERE is_active "
                "ORDER BY ticker LIMIT 1"
            )
            ticker, name = await cur.fetchone()
        prefix = str(ticker)[:2]
        choices = await ticker_autocomplete(interaction, prefix)
        assert choices
        for c in choices:
            assert (
                str(c.value).startswith(prefix)
                or str(c.name).upper().find(prefix) > 0
            )
    finally:
        await db.close_pool()


async def test_order_autocomplete_only_shows_callers_orders() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        user_a, user_b = _fresh_user(), _fresh_user()
        async with db.connection() as conn, conn.transaction():
            await bootstrap_user(conn, user_a)
            await bootstrap_user(conn, user_b)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT ticker, quoted_price FROM instruments "
                    "WHERE is_active AND kind != 'INDEX' ORDER BY ticker LIMIT 1"
                )
                ticker, mark = await cur.fetchone()
            mark = Decimal(str(mark))
            mine = await place_order(
                conn, user_id=user_a, ticker=ticker, side="BUY",
                quantity=1, limit_price=mark * Decimal("0.9"),
            )
            await place_order(
                conn, user_id=user_b, ticker=ticker, side="BUY",
                quantity=1, limit_price=mark * Decimal("0.9"),
            )

        choices = await order_autocomplete(_user_interaction(user_a), "")
        assert [c.value for c in choices] == [mine.order_id]
        # The caller can filter by id or ticker text.
        filtered = await order_autocomplete(
            _user_interaction(user_a), str(mine.order_id)
        )
        assert [c.value for c in filtered] == [mine.order_id]
    finally:
        await db.close_pool()


async def test_short_autocomplete_only_shows_callers_shorts() -> None:
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        user_a, user_b = _fresh_user(), _fresh_user()
        async with db.connection() as conn, conn.transaction():
            await bootstrap_user(conn, user_a)
            await bootstrap_user(conn, user_b)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT ticker FROM instruments WHERE is_active "
                    "AND kind != 'INDEX' ORDER BY ticker LIMIT 1"
                )
                (ticker,) = await cur.fetchone()
            mine = await open_bounded_short(
                conn, user_id=user_a, ticker=ticker, quantity=1
            )
            await open_bounded_short(conn, user_id=user_b, ticker=ticker, quantity=1)

        choices = await short_autocomplete(_user_interaction(user_a), "")
        assert [c.value for c in choices] == [mine.short_id]
        assert await short_autocomplete(_user_interaction(user_a), "ZZZ") == []
    finally:
        await db.close_pool()


async def test_tunable_param_autocomplete_prefix() -> None:
    interaction = _user_interaction(_fresh_user())
    choices = await tunable_param_autocomplete(interaction, "sig")
    assert [c.value for c in choices] == ["sigma"]
    assert len(await tunable_param_autocomplete(interaction, "")) <= MAX_CHOICES


async def test_sector_autocomplete_lists_real_sector_keys() -> None:
    """Regression: the query once read instruments.sector_key, a column
    that does not exist (sector_id -> sectors.key) -- every keystroke on
    /admin instrument-add raised UndefinedColumn and showed zero choices."""
    await db.init_pool(get_settings().test_database_url, min_size=1, max_size=2)
    try:
        interaction = _user_interaction(_fresh_user())
        choices = await sector_autocomplete(interaction, "")
        values = [str(c.value) for c in choices]
        assert values, "no sector choices returned"
        assert "index" not in values  # add_instrument refuses it
        async with db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM sectors WHERE key <> 'index'"
            )
            (expected,) = await cur.fetchone()
        assert len(values) == int(expected)
        # Prefix filter works on the real key text.
        prefix = values[0][:2]
        for c in await sector_autocomplete(interaction, prefix.lower()):
            assert str(c.value).startswith(prefix)
    finally:
        await db.close_pool()
