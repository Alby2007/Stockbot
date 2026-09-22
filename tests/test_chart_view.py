from __future__ import annotations

from typing import cast

import discord
import pytest
from psycopg import AsyncConnection

from stockbot.bot.chart_view import (
    MAX_SPAN,
    MIN_SPAN,
    build_chart_view,
    encode_cid,
    handle_chart_component,
    next_window,
    parse_cid,
)


async def _seed_open_ticks(conn: AsyncConnection, instrument_id: int, n: int) -> None:
    """Open-phase ticks 0..n-1 with flat candles (no tick engine)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO market_ticks (tick_index, market_factor, sector_factors) "
            "SELECT g, 0, '{}'::jsonb FROM generate_series(0, %s) g",
            (n - 1,),
        )
        await cur.execute(
            "INSERT INTO candles "
            "(instrument_id, tick_index, open, high, low, close, volume) "
            "SELECT %s, g, 100, 101, 99, 100, 0 FROM generate_series(0, %s) g",
            (instrument_id, n - 1),
        )


async def _instrument_id(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE is_active AND kind != 'INDEX' "
            "ORDER BY id LIMIT 1"
        )
        (iid,) = await cur.fetchone()
    return int(iid)


def test_cid_roundtrip() -> None:
    cid = encode_cid("panl", 21, 1528, 240)
    assert parse_cid(cid) == ("panl", 21, 1528, 240)
    assert len(cid) < 100  # Discord's custom_id cap


@pytest.mark.parametrize(
    "cid",
    [
        "bogus",
        "other:panl:1:2:3",
        "cbt:panl:1:2",  # missing a field
        "cbt:panl:1:2:3:4",  # extra field
        "cbt:panl:x:2:3",
        "cbt:panl:1:2:x",
        "",
    ],
)
def test_parse_cid_rejects_malformed(cid: str) -> None:
    assert parse_cid(cid) is None


async def test_next_window_zoom(conn: AsyncConnection) -> None:
    iid = await _instrument_id(conn)
    await _seed_open_ticks(conn, iid, 300)

    assert await next_window(conn, "zin", iid, 299, 240) == (299, 120)
    assert await next_window(conn, "zin", iid, 299, MIN_SPAN) == (299, MIN_SPAN)
    assert await next_window(conn, "zout", iid, 299, 240) == (299, 480)
    assert await next_window(conn, "zout", iid, 299, 4000) == (299, MAX_SPAN)


async def test_next_window_timeframe_reanchors(conn: AsyncConnection) -> None:
    """Preset buttons jump the window back to 'now' at the new span."""
    iid = await _instrument_id(conn)
    await _seed_open_ticks(conn, iid, 300)

    assert await next_window(conn, "s960", iid, 150, 240) == (299, 960)
    assert await next_window(conn, "s99999999", iid, 299, 240) == (299, MAX_SPAN)
    assert await next_window(conn, "home", iid, 100, 240) == (299, 240)


async def test_next_window_pan(conn: AsyncConnection) -> None:
    iid = await _instrument_id(conn)
    await _seed_open_ticks(conn, iid, 300)

    # Half-span steps; clamped at both ends of history.
    assert await next_window(conn, "panl", iid, 299, 240) == (179, 240)
    assert await next_window(conn, "panr", iid, 179, 240) == (299, 240)
    assert await next_window(conn, "panr", iid, 269, 240) == (299, 240)
    assert await next_window(conn, "panl", iid, 100, 240) == (0, 240)


async def test_next_window_unknown_action_is_noop(conn: AsyncConnection) -> None:
    iid = await _instrument_id(conn)
    await _seed_open_ticks(conn, iid, 300)
    assert await next_window(conn, "bogus", iid, 200, 240) == (200, 240)


def test_build_chart_view_encodes_window() -> None:
    view = build_chart_view(21, 1528, 240)
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 9
    parsed = [parse_cid(str(b.custom_id)) for b in buttons]
    assert all(p is not None and p[1:] == (21, 1528, 240) for p in parsed)
    assert {p[0] for p in parsed if p} == {
        "panl",
        "panr",
        "zin",
        "zout",
        "home",
        "s60",
        "s240",
        "s960",
        "s4800",
    }


class _FakeResponse:
    def __init__(self, done: bool = False) -> None:
        self.done = done
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.edited: dict[str, object] | None = None

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.done = True
        self.sent.append((args, kwargs))

    async def edit_message(self, **kwargs: object) -> None:
        self.done = True
        self.edited = kwargs


class _FakeInteraction:
    """Duck-type stand-in for discord.Interaction on the pre-DB paths."""

    def __init__(self, custom_id: str, done: bool = False) -> None:
        self.data: dict[str, str] = {"custom_id": custom_id}
        self.response = _FakeResponse(done)


async def test_handler_noops_when_already_responded() -> None:
    """The live View's callback + on_interaction both fire per click; the
    loser of the race must return without touching the DB."""
    it = _FakeInteraction("cbt:panl:1:2:3", done=True)
    await handle_chart_component(cast(discord.Interaction, it))
    assert it.response.sent == []
    assert it.response.edited is None


async def test_handler_rejects_bad_cid_ephemerally() -> None:
    it = _FakeInteraction("cbt:not-real")
    await handle_chart_component(cast(discord.Interaction, it))
    assert len(it.response.sent) == 1
    assert it.response.sent[0][1].get("ephemeral") is True
