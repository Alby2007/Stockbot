from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.bot.leaderboard import leaderboard_embed
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.shop.errors import (
    NotEquippableError,
    NotOwnedError,
    UnknownItemError,
)
from stockbot.shop.service import buy_item, equip_item, unequip
from stockbot.status.service import (
    LeaderboardRow,
    equipped_flair_map,
    profile_stats,
)


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id,
        amount=amount, reason="TEST",
    )


async def _equipped_title(conn: AsyncConnection, user_id: int) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT equipped_title FROM users WHERE id = %s", (user_id,)
        )
        (v,) = await cur.fetchone()
    return v


async def test_buying_a_title_auto_equips(conn: AsyncConnection) -> None:
    uid = 9201
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)

    await buy_item(conn, uid, "title_oracle")
    assert await _equipped_title(conn, uid) == "title_oracle"


async def test_equip_requires_ownership(conn: AsyncConnection) -> None:
    uid = 9202
    await bootstrap_user(conn, uid)

    with pytest.raises(NotOwnedError):
        await equip_item(conn, uid, "title_degen")
    with pytest.raises(UnknownItemError):
        await equip_item(conn, uid, "no_such_item")


async def test_equip_rejects_non_equippable(conn: AsyncConnection) -> None:
    uid = 9203
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "analyst_tools")

    with pytest.raises(NotEquippableError):
        await equip_item(conn, uid, "analyst_tools")


async def test_equip_routes_theme_to_chart_prefs(conn: AsyncConnection) -> None:
    uid = 9204
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "title_fund")
    await buy_item(conn, uid, "theme_vapor")

    assert await equip_item(conn, uid, "theme_vapor") == "theme"
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT theme FROM chart_prefs WHERE user_id = %s", (uid,)
        )
        (t,) = await cur.fetchone()
    assert t == "theme_vapor"
    # Theme equip doesn't disturb the title slot.
    assert await _equipped_title(conn, uid) == "title_fund"


async def test_unequip_clears_slots(conn: AsyncConnection) -> None:
    uid = 9205
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "title_patient")

    assert await unequip(conn, uid, "title") is True
    assert await _equipped_title(conn, uid) is None
    assert await unequip(conn, uid, "title") is False  # already clear

    with pytest.raises(NotEquippableError):
        await unequip(conn, uid, "bogus")


async def test_flair_map_renders_emoji_and_name(conn: AsyncConnection) -> None:
    uid = 9206
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "title_oracle")

    flair = await equipped_flair_map(conn, [uid, 9207])
    assert flair == {uid: "🔮 The Oracle"}


async def test_leaderboard_embed_shows_flair() -> None:
    rows = [
        LeaderboardRow(user_id=1, equity_minor=5000, rank=1, total=2),
        LeaderboardRow(user_id=2, equity_minor=100, rank=2, total=2),
    ]
    embed = leaderboard_embed(rows, flair={1: "🔮 The Oracle"})
    desc = embed.description or ""
    assert "<@1> · 🔮 The Oracle" in desc
    assert "<@2>" in desc and "Oracle" not in desc.split("<@2>")[1]


async def test_profile_stats_carries_title(conn: AsyncConnection) -> None:
    uid = 9208
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "title_bagholder")

    stats = await profile_stats(conn, uid)
    assert stats is not None and stats.title == "🎒 Bagholder Prime"
