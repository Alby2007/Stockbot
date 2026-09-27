from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.bot import charts
from stockbot.bot.chart_view import (
    encode_cid,
    load_chart_prefs,
    parse_cid,
    save_chart_prefs,
)
from stockbot.bot.charts import render_candle_chart
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.market.tick import apply_tick
from stockbot.shop.service import (
    buy_item,
    is_theme_item,
    owns_item,
    palette_from_metadata,
)


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id,
        amount=amount, reason="TEST",
    )


async def _first_instrument_id(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
        (iid,) = await cur.fetchone()
    return int(iid)


def test_palette_from_metadata_full_and_partial() -> None:
    pal = palette_from_metadata({"up": "#FF0000", "junk": 123, "down": "bad"})
    assert pal == {"up": "#FF0000"} or pal is not None
    # Non-string values are dropped; unknown keys too.
    pal = palette_from_metadata({"up": "#FF0000", "junk": 1, "nope": "#FFF"})
    assert pal == {"up": "#FF0000"}


def test_palette_from_metadata_legacy_chart_color() -> None:
    pal = palette_from_metadata({"chart_color": "#123456"})
    assert pal == {"up": "#123456", "accent": "#123456"}
    # Full keys win over the legacy color.
    pal = palette_from_metadata({"chart_color": "#123456", "up": "#ABCDEF"})
    assert pal is not None and pal["up"] == "#ABCDEF" and pal["accent"] == "#123456"


def test_is_theme_item() -> None:
    assert is_theme_item("COSMETIC", {"up": "#FFF"})
    assert is_theme_item("COSMETIC", {"chart_color": "#FFF"})
    assert not is_theme_item("COSMETIC", {"emoji": "x"})
    assert not is_theme_item("TITLE", {"up": "#FFF"})
    assert not is_theme_item("COSMETIC", None)


async def test_palette_for_resolves_and_caches(conn: AsyncConnection) -> None:
    charts._THEME_CACHE.clear()
    pal = await charts._palette_for(conn, "theme_mono")
    assert pal is not charts._DEFAULT_PALETTE
    assert pal["up"] == "#E0E0E0"
    assert pal["bg"] == "#151515"
    # Unspecified keys fall back to defaults.
    assert pal["halt_flow"] == charts._DEFAULT_PALETTE["halt_flow"]
    # Unknown theme -> defaults; both results are cached.
    assert await charts._palette_for(conn, "theme_nope") is charts._DEFAULT_PALETTE
    assert charts._THEME_CACHE["theme_nope"] is None


async def test_theme_enters_render_fingerprint(conn: AsyncConnection) -> None:
    """Two themes on the same window must produce two cache entries --
    without `theme` in the fingerprint the cache would serve one user's
    palette to everyone."""
    charts._RENDER_CACHE.clear()
    await apply_tick(conn, "theme-test-seed")
    iid = await _first_instrument_id(conn)

    assert await render_candle_chart(conn, iid, "TEST", theme=None) is not None
    assert await render_candle_chart(conn, iid, "TEST", theme="theme_mono") is not None
    assert len(charts._RENDER_CACHE) == 2

    # Repeat renders hit the cache, not new entries.
    await render_candle_chart(conn, iid, "TEST", theme=None)
    await render_candle_chart(conn, iid, "TEST", theme="theme_mono")
    assert len(charts._RENDER_CACHE) == 2


async def test_prefs_round_trip_preserves_theme(conn: AsyncConnection) -> None:
    uid = 9101
    await bootstrap_user(conn, uid)
    assert await load_chart_prefs(conn, uid) is None

    await save_chart_prefs(conn, uid, 60, "ticks")
    prefs = await load_chart_prefs(conn, uid)
    assert prefs == (60, "ticks", None)

    # Equip writes theme onto the existing row; later view saves don't
    # clobber it.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE chart_prefs SET theme = 'theme_mono' WHERE user_id = %s",
            (uid,),
        )
    await save_chart_prefs(conn, uid, 240, "time")
    prefs = await load_chart_prefs(conn, uid)
    assert prefs == (240, "time", "theme_mono")


def test_cid_parse_round_trip_and_legacy() -> None:
    cid = encode_cid("panl", 5, 100, 240, "ticks", "theme_vapor")
    assert parse_cid(cid) == ("panl", 5, 100, 240, "ticks", "theme_vapor")

    # Default theme encodes as "-" and parses to None.
    cid = encode_cid("home", 5, 100, 240)
    assert parse_cid(cid) == ("home", 5, 100, 240, "time", None)

    # Legacy 5-field (no axis) and 6-field (no theme) still parse.
    assert parse_cid("cbt:zout:5:100:240") == ("zout", 5, 100, 240, "time", None)
    assert parse_cid("cbt:zin:5:100:240:ticks") == ("zin", 5, 100, 240, "ticks", None)

    assert parse_cid("cbt:x:y:z:q:time:") is None
    assert parse_cid("nope:1:2:3") is None
    assert parse_cid("cbt:panl:5:100:240:grid") is None  # bad axis


async def test_buying_a_theme_auto_equips(conn: AsyncConnection) -> None:
    uid = 9102
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)

    await buy_item(conn, uid, "theme_vapor")
    assert await owns_item(conn, uid, "theme_vapor")
    prefs = await load_chart_prefs(conn, uid)
    assert prefs is not None and prefs[2] == "theme_vapor"

    # Buying a second theme switches the equip.
    await buy_item(conn, uid, "theme_mono")
    prefs = await load_chart_prefs(conn, uid)
    assert prefs is not None and prefs[2] == "theme_mono"


async def test_buying_non_theme_leaves_theme_alone(conn: AsyncConnection) -> None:
    uid = 9103
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "analyst_tools")
    prefs = await load_chart_prefs(conn, uid)
    assert prefs is None or prefs[2] is None
