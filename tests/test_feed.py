"""Public tape: emit_feed fan-out and the feed poll loop -- per-channel
digest coalescing, dead-channel unbind, and the emit sites' gating
(league scope excluded, thresholds). Insert sites themselves live in the
service tests; this file is about what happens to a feed_items row.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import AsyncConnection

from stockbot.bot.feed import ChannelGone, poll_once
from stockbot.feed import emit_feed


@pytest.fixture(autouse=True)
async def _clean_feed(conn: AsyncConnection) -> None:
    """poll_once scans the WHOLE pending set -- service tests elsewhere
    emit real feed rows, so each test starts empty (rolled back)."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM feed_items")
        await cur.execute("DELETE FROM feed_channels")


async def _bind(conn: AsyncConnection, guild_id: int, channel_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO feed_channels (guild_id, channel_id) VALUES (%s, %s)",
            (guild_id, channel_id),
        )


async def _count(conn: AsyncConnection, **where: Any) -> int:
    clause = " AND ".join(f"{k} = %s" for k in where) if where else "TRUE"
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT COUNT(*) FROM feed_items WHERE {clause}",
            tuple(where.values()),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_emit_fans_out_to_every_bound_channel(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    await _bind(conn, 2, 102)
    n = await emit_feed(
        conn, "NEWS_LANDED", {"ticker": "NORT", "magnitude": 0.03}, tick_index=5
    )
    assert n == 2
    assert await _count(conn, channel_id=101) == 1
    assert await _count(conn, channel_id=102) == 1


async def test_emit_noop_when_disabled(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO config (key, value) VALUES ('feed.enabled', 0) "
            "ON CONFLICT (key) DO UPDATE SET value = 0"
        )
    n = await emit_feed(conn, "JACKPOT", {"amount": 100}, user_id=7)
    assert n == 0
    assert await _count(conn) == 0


async def test_emit_noop_with_no_channels(conn: AsyncConnection) -> None:
    n = await emit_feed(conn, "JACKPOT", {"amount": 100}, user_id=7)
    assert n == 0


async def test_new_binding_picks_up_only_later_items(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    await emit_feed(conn, "JACKPOT", {"amount": 100}, user_id=7)
    await _bind(conn, 2, 102)  # bound after the first item
    await emit_feed(conn, "JACKPOT", {"amount": 200}, user_id=7)
    assert await _count(conn, channel_id=102) == 1  # only the second one


async def test_poll_coalesces_legs_into_one_channel_post(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    for ticker, side, qty in (("NORT", "SELL", 5), ("WEST", "BUY", 3), ("HELX", "BUY", 2)):
        await emit_feed(
            conn,
            "LIQUIDATION",
            {"ticker": ticker, "side": side, "qty": qty, "fill": 10.0, "penalty": 50},
            user_id=9001,
            tick_index=100,
        )
    # A different user's knockout same tick -- separate line, same post.
    await emit_feed(
        conn,
        "KNOCKOUT",
        {"ticker": "NORT", "qty": 1, "entry": 10.0, "ko_price": 12.5},
        user_id=9002,
        tick_index=100,
    )

    delivered: list[tuple[int, str]] = []

    async def deliver(channel_id: int, message: str) -> None:
        delivered.append((channel_id, message))

    stats = await poll_once(conn, deliver)
    assert stats == {"sent": 4, "failed": 0, "dead_lettered": 0}
    assert len(delivered) == 1
    channel_id, message = delivered[0]
    assert channel_id == 101
    # Three legs coalesce into ONE line with a combined penalty.
    assert "liquidated" in message
    assert "NORT" in message and "WEST" in message and "HELX" in message
    assert "$1.50" in message  # 3 x 50 minor
    assert "knocked out" in message  # second line in the same post


async def test_market_mechanics_render_unattributed(conn: AsyncConnection) -> None:
    """C8 anonymity: mechanics kinds name NOBODY (NPCs can't be told
    apart from humans -- a 'no name' tell would expose every synthetic),
    while human-achievement kinds keep <@id> mentions."""
    await _bind(conn, 1, 101)
    await emit_feed(
        conn, "LIQUIDATION",
        {"ticker": "NORT", "side": "SELL", "qty": 5, "fill": 10.0, "penalty": 50},
        user_id=9001, tick_index=1,
    )
    await emit_feed(
        conn, "WHALE",
        {"ticker": "NORT", "side": "BUY", "qty": 100, "fill": 10.0,
         "notional": 100000},
        user_id=9001, tick_index=1,
    )
    await emit_feed(
        conn, "KNOCKOUT",
        {"ticker": "NORT", "qty": 1, "entry": 10.0, "ko_price": 12.5},
        user_id=9001, tick_index=1,
    )
    await emit_feed(
        conn, "OPTION_PAYOUT",
        {"ticker": "NORT", "side": "CALL", "strike": 10.0, "qty": 2,
         "payout": 60000},
        user_id=9001, tick_index=1,
    )
    await emit_feed(
        conn, "JACKPOT", {"amount": 3800}, user_id=9001, tick_index=1
    )

    delivered: list[str] = []

    async def deliver(channel_id: int, message: str) -> None:
        delivered.append(message)

    await poll_once(conn, deliver)
    message = delivered[0]
    # One mention for the jackpot; none for the four mechanics kinds.
    assert message.count("<@9001>") == 1
    assert "a trader was liquidated" in message
    assert "whale print" in message
    assert "knocked out" in message


async def test_channels_get_independent_posts(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    await _bind(conn, 2, 102)
    await emit_feed(conn, "JACKPOT", {"amount": 3800}, user_id=9001)

    delivered: list[int] = []

    async def deliver(channel_id: int, message: str) -> None:
        delivered.append(channel_id)

    await poll_once(conn, deliver)
    assert sorted(delivered) == [101, 102]


async def test_dead_channel_unbinds_and_cascades_pending(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    await _bind(conn, 2, 102)
    await emit_feed(conn, "JACKPOT", {"amount": 3800}, user_id=9001)

    async def deliver(channel_id: int, message: str) -> None:
        if channel_id == 101:
            raise ChannelGone("forbidden: channel deleted")

    stats = await poll_once(conn, deliver)
    assert stats["dead_lettered"] == 1
    assert stats["sent"] == 1

    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM feed_channels WHERE channel_id = 101")
        assert (await cur.fetchone())[0] == 0
        # Channel 2's binding and its (now-sent) row are untouched.
        await cur.execute("SELECT COUNT(*) FROM feed_channels WHERE channel_id = 102")
        assert (await cur.fetchone())[0] == 1
        await cur.execute(
            "SELECT COUNT(*) FROM feed_items WHERE channel_id = 101 AND sent_at IS NULL"
        )
        assert (await cur.fetchone())[0] == 0  # cascade removed pending rows


async def test_transient_failure_retries_with_backoff(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    await emit_feed(conn, "JACKPOT", {"amount": 100}, user_id=1)

    async def deliver(channel_id: int, message: str) -> None:
        raise RuntimeError("discord hiccup")

    stats = await poll_once(conn, deliver)
    assert stats["failed"] == 1
    async with conn.cursor() as cur:
        await cur.execute("SELECT attempts, last_error, sent_at FROM feed_items")
        attempts, last_error, sent_at = await cur.fetchone()
    assert attempts == 1 and "RuntimeError" in last_error and sent_at is None

    called = False

    async def deliver2(channel_id: int, message: str) -> None:
        nonlocal called
        called = True

    await poll_once(conn, deliver2)  # backoff hasn't elapsed
    assert not called


async def test_season_result_line(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    await emit_feed(
        conn,
        "SEASON_RESULT",
        {"season_name": "Season 3", "winner_id": 9001, "entrants": 12, "prize": 50000},
        tick_index=999,
    )
    delivered: list[str] = []

    async def deliver(channel_id: int, message: str) -> None:
        delivered.append(message)

    await poll_once(conn, deliver)
    assert "Season 3" in delivered[0]
    assert "<@9001>" in delivered[0]
    assert "$500.00" in delivered[0]


async def test_market_news_line(conn: AsyncConnection) -> None:
    await _bind(conn, 1, 101)
    await emit_feed(
        conn, "NEWS_LANDED", {"ticker": "NORT", "magnitude": 0.032}, tick_index=7
    )
    delivered: list[str] = []

    async def deliver(channel_id: int, message: str) -> None:
        delivered.append(message)

    await poll_once(conn, deliver)
    assert "NORT +3.2%" in delivered[0]


async def test_rebind_moves_pending_items(conn: AsyncConnection) -> None:
    """ON UPDATE CASCADE: /feed-setup in a new channel carries the
    unsent backlog to the new binding instead of orphaning it."""
    await _bind(conn, 1, 101)
    await emit_feed(conn, "JACKPOT", {"amount": 100}, user_id=1)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE feed_channels SET channel_id = 201 WHERE guild_id = 1"
        )
    delivered: list[int] = []

    async def deliver(channel_id: int, message: str) -> None:
        delivered.append(channel_id)

    await poll_once(conn, deliver)
    assert delivered == [201]


async def test_whale_emit_threshold(conn: AsyncConnection) -> None:
    """execute_trade emits WHALE only at/above news.whale_min_notional
    (dollars) -- a routine fill stays off the tape."""

    from stockbot.accounts.service import bootstrap_user
    from stockbot.trading.service import execute_trade

    await _bind(conn, 1, 101)
    await bootstrap_user(conn, 9101)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = 100_000_000 WHERE user_id = 9101"
        )
        await cur.execute(
            "UPDATE config SET value = 1 WHERE key = 'news.whale_min_notional'"
        )
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK' "
            "ORDER BY id LIMIT 1"
        )
        ticker = (await cur.fetchone())[0]
    # ~$1 of stock -- below any sane whale threshold.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 5000 WHERE key = 'news.whale_min_notional'"
        )
    await execute_trade(conn, user_id=9101, ticker=ticker, side="BUY", quantity=1)
    assert await _count(conn, kind="WHALE") == 0
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = 0 WHERE key = 'news.whale_min_notional'")
    await execute_trade(conn, user_id=9101, ticker=ticker, side="BUY", quantity=1)
    assert await _count(conn, kind="WHALE") == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM feed_items WHERE kind = 'WHALE'"
        )
        payload = (await cur.fetchone())[0]
    assert payload["ticker"] == ticker and payload["side"] == "BUY"


async def test_league_liquidation_stays_off_tape(conn: AsyncConnection) -> None:
    """A league-scoped liquidation is faucet-stake drama, not tape:
    emit_feed must see no row even with a bound channel."""
    from decimal import Decimal

    from stockbot.accounts.service import bootstrap_user
    from stockbot.margin.service import check_and_liquidate
    from stockbot.seasons.service import create_season, join_season, on_tick
    from stockbot.trading.service import execute_trade

    await _bind(conn, 1, 101)
    await bootstrap_user(conn, 9102)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO entitlements (user_id, item_key, quantity) "
            "VALUES (9102, 'margin_tier', 1) "
            "ON CONFLICT (user_id, item_key) DO UPDATE SET quantity = 1"
        )
        await cur.execute(
            "SELECT ticker FROM instruments WHERE is_active AND kind = 'STOCK' "
            "AND market_id = 1 ORDER BY id LIMIT 1"
        )
        ticker = (await cur.fetchone())[0]
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, maint_margin_pct = 0.3 "
            "WHERE ticker = %s",
            (ticker,),
        )
    season_id = await create_season(
        conn, name="Tape League", start_tick=0, end_tick=10_000,
        entry_fee_minor=0, stake_minor=100_000,
    )
    await on_tick(conn, 0)
    await join_season(conn, 9102, season_id)
    # ~1.8x levered league short, then a 3x gap -> undermargined.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT a.balance FROM accounts a WHERE a.user_id = 9102 "
            "AND a.season_id = %s",
            (season_id,),
        )
        cash = int((await cur.fetchone())[0])
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        price = Decimal((await cur.fetchone())[0])
    qty = max(1, int(Decimal(cash) * Decimal("1.8") / (price * 100)))
    await execute_trade(
        conn, user_id=9102, ticker=ticker, side="SELL", quantity=qty,
        season_id=season_id,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = base_price * 3, "
            "quoted_price = quoted_price * 3 WHERE ticker = %s",
            (ticker,),
        )
    legs = await check_and_liquidate(conn, 9102, season_id)
    assert legs >= 1
    assert await _count(conn, user_id=9102) == 0


async def test_season_result_aggregate_row(conn: AsyncConnection) -> None:
    """close_season emits ONE aggregate SEASON_RESULT (the league-filter
    exception), not a row per entrant."""
    from stockbot.accounts.service import bootstrap_user
    from stockbot.seasons.service import close_season, create_season, join_season, on_tick

    await _bind(conn, 1, 101)
    for uid in (9201, 9202):
        await bootstrap_user(conn, uid)
    season_id = await create_season(
        conn, name="Final Tape", start_tick=0, end_tick=10,
        entry_fee_minor=0, stake_minor=100_000,
    )
    await on_tick(conn, 0)
    await join_season(conn, 9201, season_id)
    await join_season(conn, 9202, season_id)
    # close_season is the admin path -- it doesn't gate on end_tick.
    await close_season(conn, season_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM feed_items WHERE kind = 'SEASON_RESULT'"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0]["season_name"] == "Final Tape"
