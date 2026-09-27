from __future__ import annotations

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.bot.chart_view import (
    MAX_SPAN,
    MAX_SPAN_PRO,
    load_chart_prefs,
    next_window,
    save_chart_prefs,
)
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.market.tick import apply_tick
from stockbot.shop.service import buy_item, owns_item
from stockbot.status.service import pro_terminal_stats
from stockbot.trading.service import execute_trade


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id,
        amount=amount, reason="TEST",
    )


async def _iid(conn: AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM instruments WHERE ticker = 'NORT'"
        )
        (iid,) = await cur.fetchone()
    return int(iid)


async def test_pro_stats_empty_without_history(conn: AsyncConnection) -> None:
    stats = await pro_terminal_stats(conn, await _iid(conn), 0)
    assert stats.week_high is None and stats.week_low is None
    assert stats.realized_vol is None
    assert stats.buy_notional_24h == 0 and stats.sell_notional_24h == 0
    assert stats.adv_shares > 0  # seeded instrument


async def test_pro_stats_populate_after_trading(conn: AsyncConnection) -> None:
    uid = 9401
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 200_000)
    await apply_tick(conn, "pro-seed")
    iid = await _iid(conn)
    await execute_trade(conn, user_id=uid, ticker="NORT", side="BUY", quantity=3)
    await execute_trade(conn, user_id=uid, ticker="NORT", side="SELL", quantity=1)

    stats = await pro_terminal_stats(conn, iid, 1)
    assert stats.week_high is not None and stats.week_high > 0
    assert stats.week_low is not None and stats.week_low <= stats.week_high
    assert stats.buy_notional_24h > 0
    assert stats.sell_notional_24h > 0


async def test_pro_terminal_is_renewable_entitlement(
    conn: AsyncConnection,
) -> None:
    uid = 9402
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 100_000)
    assert not await owns_item(conn, uid, "pro_terminal")
    await buy_item(conn, uid, "pro_terminal")
    assert await owns_item(conn, uid, "pro_terminal")
    # 30-day duration: a second buy renews, never AlreadyOwnedError.
    await buy_item(conn, uid, "pro_terminal")


async def test_span_bound_gate(conn: AsyncConnection) -> None:
    """zout/the s<> presets clamp at MAX_SPAN for everyone and at
    MAX_SPAN_PRO for the entitled -- the bound is passed in, resolved
    per clicker."""
    iid = await _iid(conn)
    await apply_tick(conn, "pro-seed")

    assert await next_window(conn, "zout", iid, 0, 6000) == (0, MAX_SPAN)
    assert await next_window(conn, "zout", iid, 0, 6000, max_span=MAX_SPAN_PRO) == (
        0,
        12000,
    )
    assert await next_window(
        conn, "s19200", iid, 0, 240, max_span=MAX_SPAN
    ) == (0, MAX_SPAN)
    assert await next_window(
        conn, "s19200", iid, 0, 240, max_span=MAX_SPAN_PRO
    ) == (0, MAX_SPAN_PRO)


async def test_load_prefs_clamps_to_bound(conn: AsyncConnection) -> None:
    uid = 9403
    await bootstrap_user(conn, uid)
    await save_chart_prefs(conn, uid, MAX_SPAN_PRO, "time")
    # Expired/absent entitlement: the stored 1M view clamps to MAX_SPAN.
    assert (await load_chart_prefs(conn, uid))[0] == MAX_SPAN
    # Entitled bound keeps it.
    assert (
        await load_chart_prefs(conn, uid, max_span=MAX_SPAN_PRO)
    )[0] == MAX_SPAN_PRO
