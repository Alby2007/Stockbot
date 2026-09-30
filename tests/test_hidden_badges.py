"""Hidden/rare badges: new evaluate_badges metric blocks (duel wins,
bounty claims, liquidations survived, prop wins) and the catalog's
hidden flag semantics."""

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.props import service as props
from stockbot.status.service import evaluate_badges


async def _entitled(conn: AsyncConnection, user_id: int, key: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM entitlements WHERE user_id = %s AND item_key = %s",
            (user_id, key),
        )
        return await cur.fetchone() is not None


async def test_duel_wins_badge(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4301)
    await bootstrap_user(conn, 4302)
    async with conn.cursor() as cur:
        for _i in range(3):
            await cur.execute(
                """
                INSERT INTO duels (challenger_id, opponent_id, stake_minor,
                                   window_ticks, status, created_tick,
                                   expires_tick, winner_id)
                VALUES (4301, 4302, 100, 10, 'SETTLED', 0, 10, 4301)
                """,
            )
    granted = await evaluate_badges(conn, 1440, interval_ticks=60)
    assert granted > 0
    assert await _entitled(conn, 4301, "badge_duelist")
    # The loser gets nothing.
    assert not await _entitled(conn, 4302, "badge_duelist")
    # 10-win tier needs more.
    assert not await _entitled(conn, 4301, "badge_gladiator")


async def test_bounty_claims_badge(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4310)
    await bootstrap_user(conn, 4311)
    await bootstrap_user(conn, 4312)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO bounties (poster_id, target_id, amount_minor, status,
                                  created_tick, expires_tick, claimed_by)
            VALUES (4310, 4312, 1000, 'CLAIMED', 0, 10, 4311)
            """
        )
    await evaluate_badges(conn, 1440, interval_ticks=60)
    assert await _entitled(conn, 4311, "badge_headhunter")


async def test_liquidations_survived_badge(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4320)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE is_active LIMIT 1"
        )
        (iid,) = await cur.fetchone()
        await cur.execute(
            "SELECT id FROM accounts WHERE user_id = 4320 AND kind = 'USER'"
        )
        (acct,) = await cur.fetchone()
        await cur.execute(
            """
            INSERT INTO liquidations (user_id, account_id, instrument_id,
                                      season_id, side, quantity_closed,
                                      fill_price, notional_minor, penalty_minor,
                                      equity_before_minor, maint_req_before_minor,
                                      tick_index)
            VALUES (4320, %s, %s, NULL, 'SELL', 1, 1.0, 100, 0, 0, 1, 5)
            """,
            (acct, iid),
        )
    await evaluate_badges(conn, 1440, interval_ticks=60)
    assert await _entitled(conn, 4320, "badge_singed")


async def test_prop_wins_badge(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 4330)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, quoted_price FROM instruments "
            "WHERE kind = 'STOCK' AND is_active LIMIT 1"
        )
        iid, price = await cur.fetchone()
    prop_id = await props.create(
        conn,
        title="t",
        kind="PRICE_ABOVE",
        instrument_id=int(iid),
        threshold=float(price) * 0.5,
        resolve_tick=10,
        tick_index=0,
        feed_post=False,
    )
    await props.bet(conn, 4330, prop_id, "YES", 1000, tick_index=0)
    await props.settle_due(conn, 10)
    await evaluate_badges(conn, 1440, interval_ticks=60)
    assert await _entitled(conn, 4330, "badge_sharpie")


async def test_hidden_badges_are_grant_only(conn: AsyncConnection) -> None:
    """`metadata.hidden` rows never appear in the purchasable catalog --
    they exist only as grants. price_minor NULL already keeps them out
    of /shop; the flag names the intent."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT key, price_minor, metadata->>'hidden' FROM shop_items
            WHERE kind = 'BADGE' AND metadata->>'hidden' = 'true'
            """
        )
        rows = await cur.fetchall()
    assert len(rows) >= 4
    for _key, price, hidden in rows:
        assert price is None and hidden == "true"
    # And bots can't earn them even if the metric rows exist.
    await bootstrap_user(conn, 4398)
    async with conn.cursor() as cur:
        await cur.execute("INSERT INTO users (id, is_bot) VALUES (4399, TRUE)")
        await cur.execute(
            """
            INSERT INTO duels (challenger_id, opponent_id, stake_minor,
                               window_ticks, status, created_tick,
                               expires_tick, winner_id)
            VALUES (4399, 4398, 100, 10, 'SETTLED', 0, 10, 4399),
                   (4399, 4398, 100, 10, 'SETTLED', 0, 10, 4399),
                   (4399, 4398, 100, 10, 'SETTLED', 0, 10, 4399)
            """
        )
    await evaluate_badges(conn, 1440, interval_ticks=60)
    assert not await _entitled(conn, 4399, "badge_duelist")
