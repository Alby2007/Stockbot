from __future__ import annotations

import asyncio
from typing import cast

import discord
import pytest
from psycopg import AsyncConnection

from stockbot.bot.chart_view import (
    _INFLIGHT,
    MAX_SPAN,
    MIN_SPAN,
    build_chart_view,
    encode_cid,
    handle_chart_component,
    load_chart_prefs,
    next_window,
    parse_cid,
    save_chart_prefs,
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
    assert parse_cid(cid) == ("panl", 21, 1528, 240, "time", None, False)
    assert len(cid) < 100  # Discord's custom_id cap

    cid2 = encode_cid("zin", 7, 300, 120, "ticks")
    assert parse_cid(cid2) == ("zin", 7, 300, 120, "ticks", None, False)


def test_cid_roundtrip_mine_flag() -> None:
    """Ephemeral /chart mine views carry the flag so clicks keep drawing
    the clicker's own marks and reply ephemerally instead of editing."""
    cid = encode_cid("panl", 21, 1528, 240, mine=True)
    assert parse_cid(cid) == ("panl", 21, 1528, 240, "time", None, True)
    cid2 = encode_cid("zin", 7, 300, 120, "ticks", "neon", mine=True)
    assert parse_cid(cid2) == ("zin", 7, 300, 120, "ticks", "neon", True)
    # Field 8 must be m or -, anything else is malformed.
    assert parse_cid("cbt:panl:1:2:3:time:-:x") is None
    assert parse_cid("cbt:panl:1:2:3:time:-:m") == (
        "panl", 1, 2, 3, "time", None, True
    )
    view = build_chart_view(21, 1528, 240, mine=True)
    parsed = [parse_cid(str(b.custom_id)) for b in view.children]
    assert all(p is not None and p[6] is True for p in parsed)


def test_parse_cid_legacy_defaults_to_time() -> None:
    """Buttons on pre-axis charts carry 5-field cids; they parse as the
    default axis so old messages keep working after the upgrade."""
    assert parse_cid("cbt:panr:5:640:240") == ("panr", 5, 640, 240, "time", None, False)


@pytest.mark.parametrize(
    "cid",
    [
        "bogus",
        "other:panl:1:2:3",
        "cbt:panl:1:2",  # missing a field
        "cbt:panl:1:2:3:4",  # unknown axis value
        "cbt:panl:1:2:3:bogus",
        "cbt:panl:1:2:3:time:extra:more",  # too many fields
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
    assert len(buttons) == 12
    parsed = [parse_cid(str(b.custom_id)) for b in buttons]
    assert all(
        p is not None and p[1:] == (21, 1528, 240, "time", None, False)
        for p in parsed
    )
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
        "s9600",
        "s19200",
        "ax",
    }


def test_build_chart_view_axis_state() -> None:
    """The axis rides every button's cid, and the toggle button labels
    the mode a click switches TO."""
    view = build_chart_view(21, 1528, 240, axis="ticks")
    buttons = {str(b.custom_id).split(":")[1]: b for b in view.children}
    assert parse_cid(str(buttons["panl"].custom_id)) == (
        "panl", 21, 1528, 240, "ticks", None, False
    )
    ax = buttons["ax"]
    # cid still encodes the CURRENT axis; the label advertises the target.
    assert parse_cid(str(ax.custom_id)) == ("ax", 21, 1528, 240, "ticks", None, False)
    assert ax.label == "Axis: time"

    view_time = build_chart_view(21, 1528, 240, axis="time")
    ax_t = {str(b.custom_id).split(":")[1]: b for b in view_time.children}["ax"]
    assert ax_t.label == "Axis: ticks"


class _FakeResponse:
    def __init__(self, done: bool = False, defer_gate: asyncio.Event | None = None) -> None:
        self.done = done
        self.defer_gate = defer_gate
        self.deferred = False
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.done = True
        self.sent.append((args, kwargs))

    async def defer(self) -> None:
        if self.defer_gate is not None:
            await self.defer_gate.wait()
        self.done = True
        self.deferred = True


class _FakeInteraction:
    """Duck-type stand-in for discord.Interaction on the pre-DB paths."""

    def __init__(
        self,
        custom_id: str,
        done: bool = False,
        interaction_id: int = 1,
        defer_gate: asyncio.Event | None = None,
    ) -> None:
        self.id = interaction_id
        self.data: dict[str, str] = {"custom_id": custom_id}
        self.response = _FakeResponse(done, defer_gate)


async def test_handler_noops_when_already_responded() -> None:
    """The live View's callback + on_interaction both fire per click; the
    loser of the race must return without touching the DB."""
    it = _FakeInteraction("cbt:panl:1:2:3", done=True)
    await handle_chart_component(cast(discord.Interaction, it))
    assert it.response.sent == []
    assert not it.response.deferred


async def test_handler_rejects_bad_cid_ephemerally() -> None:
    it = _FakeInteraction("cbt:not-real")
    await handle_chart_component(cast(discord.Interaction, it))
    assert len(it.response.sent) == 1
    assert it.response.sent[0][1].get("ephemeral") is True


async def test_handler_dedups_concurrent_dispatch() -> None:
    """View callback + on_interaction race on the same interaction id:
    the second entry must return without responding or deferring."""
    gate = asyncio.Event()
    first = _FakeInteraction("cbt:zin:1:100:240", interaction_id=42, defer_gate=gate)
    task = asyncio.create_task(
        handle_chart_component(cast(discord.Interaction, first))
    )
    await asyncio.sleep(0)  # let the first handler reach its defer() wait
    await asyncio.sleep(0)
    assert 42 in _INFLIGHT

    second = _FakeInteraction("cbt:zin:1:100:240", interaction_id=42)
    await handle_chart_component(cast(discord.Interaction, second))
    assert not second.response.deferred
    assert second.response.sent == []

    gate.set()
    # First handler proceeds past defer into db.connection(), whose pool
    # is uninitialized in tests -- it raising proves it owned the id.
    with pytest.raises(RuntimeError, match="pool is not initialized"):
        await task
    assert 42 not in _INFLIGHT


async def test_chart_prefs_roundtrip(conn: AsyncConnection) -> None:
    assert await load_chart_prefs(conn, 999_001) is None
    await save_chart_prefs(conn, 999_001, 60, "ticks")
    assert await load_chart_prefs(conn, 999_001) == (60, "ticks", None)


async def test_chart_prefs_upsert_overwrites(conn: AsyncConnection) -> None:
    await save_chart_prefs(conn, 999_002, 60, "ticks")
    await save_chart_prefs(conn, 999_002, 4800, "time")
    assert await load_chart_prefs(conn, 999_002) == (4800, "time", None)


async def test_chart_prefs_span_clamps_on_load(conn: AsyncConnection) -> None:
    """A stored span outside current zoom bounds re-clamps on read --
    bound changes can't strand a saved preference."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO chart_prefs (user_id, span, axis) "
            "VALUES (%s, %s, 'time')",
            (999_003, MAX_SPAN * 4),
        )
    assert await load_chart_prefs(conn, 999_003) == (MAX_SPAN, "time", None)
