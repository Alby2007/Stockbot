"""Short-side mechanics (0043): margin-call warnings, borrow recalls,
user-chosen bounded-short knockouts, and the IPO borrow lockout."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.admin.service import add_instrument
from stockbot.ipo.service import create_offering, settle_due, subscribe
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.errors import InstrumentNotShortableError
from stockbot.margin.service import (
    compute_health,
    effective_borrow_bps_per_tick,
    refresh_short_interest,
    sweep_recalls,
    sweep_undermargined,
)
from stockbot.market.data import get_instrument_snapshot
from stockbot.orders.service import place_order
from stockbot.shorts.service import open_bounded_short
from stockbot.trading.service import execute_trade

START = 10_000_000  # $100k test funding


async def _fund(conn: AsyncConnection, user_id: int, amount: int = START) -> int:
    account_id = (await bootstrap_user(conn, user_id)).account_id
    await post_transfer(
        conn,
        from_account_id=await get_system_account_id(conn, "FAUCET"),
        to_account_id=account_id,
        amount=amount,
        reason="TEST_TOPUP",
    )
    return account_id


async def _grant_tier(conn: AsyncConnection, user_id: int, tier: int = 1) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO entitlements (user_id, item_key, quantity)
            VALUES (%s, 'margin_tier', %s)
            ON CONFLICT (user_id, item_key)
            DO UPDATE SET quantity = EXCLUDED.quantity
            """,
            (user_id, tier),
        )


async def _make_stock(
    conn: AsyncConnection, ticker: str, price: float = 10.0
) -> None:
    await add_instrument(
        conn,
        ticker=ticker,
        name=f"{ticker} Corp",
        sector_key="TECH",
        base_price=price,
    )


async def _position(conn: AsyncConnection, user_id: int, ticker: str):
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.quantity FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND i.ticker = %s AND p.season_id IS NULL
            """,
            (user_id, ticker),
        )
        return await cur.fetchone()


async def _notifications(conn: AsyncConnection, user_id: int, kind: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = %s AND kind = %s",
            (user_id, kind),
        )
        (n,) = await cur.fetchone()
    return int(n)


async def _warned(conn: AsyncConnection, user_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT margin_warned FROM accounts "
            "WHERE user_id = %s AND kind = 'USER'",
            (user_id,),
        )
        (flag,) = await cur.fetchone()
    return bool(flag)


async def _levered_short(
    conn: AsyncConnection, user_id: int, ticker: str, leverage: float
) -> None:
    """Short ~leverage*equity of `ticker` under init 50% / maint 30% margins."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET init_margin_pct = 0.5, "
            "maint_margin_pct = 0.3 WHERE ticker = %s RETURNING quoted_price",
            (ticker,),
        )
        (price,) = await cur.fetchone()
        await cur.execute(
            "SELECT balance FROM accounts WHERE user_id = %s AND kind = 'USER'",
            (user_id,),
        )
        (cash,) = await cur.fetchone()
    qty = max(1, int(Decimal(cash) * Decimal(leverage) / (Decimal(price) * 100)))
    await execute_trade(conn, user_id=user_id, ticker=ticker, side="SELL", quantity=qty)


async def test_margin_call_warns_once_latches_and_clears(
    conn: AsyncConnection,
) -> None:
    await _fund(conn, 9301)
    await _grant_tier(conn, 9301)
    await _make_stock(conn, "WRNA")
    await _levered_short(conn, 9301, "WRNA", leverage=1.4)

    # Gap up into the warning band: equity ~0.65C vs maint ~0.525C -> ~1.24x.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = quoted_price * 1.25, "
            "base_price = base_price * 1.25 WHERE ticker = 'WRNA'"
        )
    health = await compute_health(conn, 9301)
    assert health.margined and not health.undermargined
    assert Decimal(health.equity_minor) < Decimal("1.35") * Decimal(
        health.maint_req_minor
    )

    await sweep_undermargined(conn, 900)
    assert await _notifications(conn, 9301, "MARGIN_CALL") == 1
    assert await _warned(conn, 9301)

    # Latch: hovering in the band doesn't re-notify.
    await sweep_undermargined(conn, 901)
    assert await _notifications(conn, 9301, "MARGIN_CALL") == 1

    # Recovery clears the latch; a later episode warns again.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = quoted_price / 1.25, "
            "base_price = base_price / 1.25 WHERE ticker = 'WRNA'"
        )
    await sweep_undermargined(conn, 902)
    assert not await _warned(conn, 9301)

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = quoted_price * 1.25, "
            "base_price = base_price * 1.25 WHERE ticker = 'WRNA'"
        )
    await sweep_undermargined(conn, 903)
    assert await _notifications(conn, 9301, "MARGIN_CALL") == 2


async def test_margin_call_skips_undermargined(conn: AsyncConnection) -> None:
    """Liquidation is the notification for a breached account, not a warn."""
    await _fund(conn, 9302)
    await _grant_tier(conn, 9302)
    await _make_stock(conn, "WRNB")
    await _levered_short(conn, 9302, "WRNB", leverage=1.8)

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET quoted_price = quoted_price * 4, "
            "base_price = base_price * 4, impact = 0 WHERE ticker = 'WRNB'"
        )
    legs = await sweep_undermargined(conn, 910)
    assert legs >= 1
    assert await _notifications(conn, 9302, "MARGIN_CALL") == 0
    assert await _notifications(conn, 9302, "LIQUIDATION") >= 1


async def test_recall_covers_pro_rata_and_notifies(conn: AsyncConnection) -> None:
    await _fund(conn, 9310)
    await _grant_tier(conn, 9310)
    await _make_stock(conn, "RCLA")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET float_shares = 1000 WHERE ticker = 'RCLA'"
        )
    await execute_trade(conn, user_id=9310, ticker="RCLA", side="SELL", quantity=300)
    await refresh_short_interest(conn)  # SI = 0.30 > recall 0.25, at the cap

    legs = await sweep_recalls(conn, 920)
    assert legs == 1

    # recall fraction = (0.30-0.25)/0.30 * 0.10 -> 1.67%; ceil(300 * f) = 5.
    pos = await _position(conn, 9310, "RCLA")
    assert pos is not None and pos[0] == -295
    assert await _notifications(conn, 9310, "SHORT_RECALL") == 1
    # A recall is not a liquidation: no liquidations row, no penalty.
    assert await _notifications(conn, 9310, "LIQUIDATION") == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM liquidations WHERE user_id = 9310"
        )
        (n,) = await cur.fetchone()
    assert int(n) == 0


async def test_recall_noop_below_threshold(conn: AsyncConnection) -> None:
    await _fund(conn, 9311)
    await _grant_tier(conn, 9311)
    await _make_stock(conn, "RCLB", price=50.0)
    await execute_trade(conn, user_id=9311, ticker="RCLB", side="SELL", quantity=10)
    await refresh_short_interest(conn)

    assert await sweep_recalls(conn, 921) == 0
    pos = await _position(conn, 9311, "RCLB")
    assert pos is not None and pos[0] == -10


async def test_recall_cash_caps_instead_of_overdrawing(
    conn: AsyncConnection,
) -> None:
    await _fund(conn, 9312)
    await _grant_tier(conn, 9312)
    await _make_stock(conn, "RCLC")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET float_shares = 1000 WHERE ticker = 'RCLC'"
        )
    await execute_trade(conn, user_id=9312, ticker="RCLC", side="SELL", quantity=300)
    await refresh_short_interest(conn)

    # Drain every cent: the recall leg can't cover a single share, so it
    # skips rather than hitting the balance CHECK (or the fund backstop).
    account_id = await get_user_account_id(conn, 9312)
    balance = await get_balance(conn, account_id)
    await post_transfer(
        conn,
        from_account_id=account_id,
        to_account_id=await get_system_account_id(conn, "SINK"),
        amount=balance,
        reason="TEST_DRAIN",
    )

    assert await sweep_recalls(conn, 922) == 0
    pos = await _position(conn, 9312, "RCLC")
    assert pos is not None and pos[0] == -300
    assert await _notifications(conn, 9312, "SHORT_RECALL") == 0


async def test_recall_clamps_to_real_cost_fund_untouched(
    conn: AsyncConnection,
) -> None:
    """The caller's cash bound omits the half-spread, so it can oversize
    the recall. _liquidate_leg must re-clamp at the ACTUAL fill cost --
    the insurance fund must never cover a recall shortfall (there is no
    flow row for it, so fund_reconciles would break)."""
    await _fund(conn, 9314)
    await _grant_tier(conn, 9314)
    await _make_stock(conn, "RCLF")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET float_shares = 1000, max_impact = 0.001 "
            "WHERE ticker = 'RCLF'"
        )
        # 10% half-spread, every modifier coefficient off: the real cover
        # costs ~quoted*1.10 while the caller's bound is ~quoted*1.001.
        await cur.execute(
            "UPDATE config SET value = '1000' WHERE key = 'spread.base_bps'"
        )
        await cur.execute(
            "UPDATE config SET value = '0' WHERE key LIKE 'spread.%_coeff'"
        )
    # 300/1000 float is exactly at the short-interest cap (the gate is
    # strict >): SI = 0.30 -> recall ~1.67% -> 5 shares.
    await execute_trade(conn, user_id=9314, ticker="RCLF", side="SELL", quantity=300)
    await refresh_short_interest(conn)

    # Leave cash for 5 shares at the caller's bound but only 4 at the
    # real fill: the leg must close 4, not draw the fund for 5.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = 'RCLF'"
        )
        (quoted,) = await cur.fetchone()
    est_per_share = int(
        (
            Decimal(quoted) * Decimal("1.001") * Decimal("1.001") * 100
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    target_cash = est_per_share * 5 + est_per_share // 5
    account_id = await get_user_account_id(conn, 9314)
    balance = await get_balance(conn, account_id)
    await post_transfer(
        conn,
        from_account_id=account_id,
        to_account_id=await get_system_account_id(conn, "SINK"),
        amount=balance - target_cash,
        reason="TEST_DRAIN",
    )
    fund_id = await get_system_account_id(conn, "INSURANCE_FUND")
    fund_before = await get_balance(conn, fund_id)

    legs = await sweep_recalls(conn, 924)
    assert legs == 1
    pos = await _position(conn, 9314, "RCLF")
    # Fewer than the estimated 5 covered -- the real-cost clamp bit.
    assert pos is not None and pos[0] < -(300 - 5)
    assert await get_balance(conn, fund_id) == fund_before
    # And no unaudited flow: nothing was recorded against the fund.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM insurance_fund_flows "
            "WHERE tick_index = 924 AND reason = 'COVER_SHORTFALL'"
        )
        (n,) = await cur.fetchone()
    assert int(n) == 0


async def test_recall_leaves_bounded_shorts_alone(conn: AsyncConnection) -> None:
    await _fund(conn, 9313)
    await _grant_tier(conn, 9313)
    await _make_stock(conn, "RCLD")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET float_shares = 1000 WHERE ticker = 'RCLD'"
        )
    await execute_trade(conn, user_id=9313, ticker="RCLD", side="SELL", quantity=300)
    # Collateralized bounded short on the same crowded name: not a borrow.
    bs = await open_bounded_short(conn, user_id=9313, ticker="RCLD", quantity=5)
    await refresh_short_interest(conn)

    await sweep_recalls(conn, 923)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM bounded_shorts WHERE id = %s", (bs.short_id,)
        )
        (status,) = await cur.fetchone()
    assert status == "OPEN"


def test_effective_borrow_bps_mirrors_sql() -> None:
    cfg = {
        "margin.borrow_fee_bps_per_tick": Decimal("0.01"),
        "margin.max_short_interest_pct": Decimal("0.30"),
        "margin.borrow_util_k": Decimal("4"),
    }
    assert effective_borrow_bps_per_tick(Decimal("0"), cfg) == Decimal("0.01")
    # at the cap: bps * (1 + k*1) = 0.05
    assert effective_borrow_bps_per_tick(Decimal("0.30"), cfg) == Decimal("0.05")
    # utilisation is quadratic, not linear: half cap -> 0.02
    assert effective_borrow_bps_per_tick(Decimal("0.15"), cfg) == Decimal("0.02")


async def test_bounded_short_custom_knockout(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9320)
    result = await open_bounded_short(
        conn, user_id=9320, ticker="NORT", quantity=1,
        knockout_pct=Decimal("0.10"),
    )
    # 10% KO: collateral and barrier both follow the chosen pct.
    assert result.knockout_price > result.entry_price
    assert result.collateral_minor == round(
        float(result.entry_price) * 0.10 * 100
    )
    assert float(result.knockout_price) < float(result.entry_price) * 1.11


async def test_bounded_short_knockout_out_of_bounds(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 9321)
    with pytest.raises(ValueError, match="knockout"):
        await open_bounded_short(
            conn, user_id=9321, ticker="NORT", quantity=1,
            knockout_pct=Decimal("0.005"),  # below shorts.min_knockout_pct
        )


async def test_ipo_lockout_blocks_margin_shorts_only(conn: AsyncConnection) -> None:
    await _fund(conn, 9330)
    await _grant_tier(conn, 9330)
    offering_id = await create_offering(
        conn,
        ticker="IPZ",
        name="IPZ Corp",
        sector_key="TECH",
        offer_price=10.0,
        shares_offered=100,
        duration_ticks=5,
    )
    await subscribe(conn, user_id=9330, ticker="IPZ", amount_minor=10_000)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT close_tick FROM ipo_offerings WHERE id = %s", (offering_id,)
        )
        (close_tick,) = await cur.fetchone()
    await settle_due(conn, int(close_tick))

    # The listing trades, but borrow is locked until shortable_after_tick.
    await execute_trade(conn, user_id=9330, ticker="IPZ", side="BUY", quantity=1)
    with pytest.raises(InstrumentNotShortableError):
        await execute_trade(conn, user_id=9330, ticker="IPZ", side="SELL", quantity=20)
    with pytest.raises(InstrumentNotShortableError):
        await place_order(
            conn, user_id=9330, ticker="IPZ", side="SELL", quantity=1,
            limit_price=Decimal("15"), allow_short=True,
        )

    # Bounded shorts are collateralized derivatives, not borrows: allowed.
    bs = await open_bounded_short(conn, user_id=9330, ticker="IPZ", quantity=1)
    assert bs.quantity == 1

    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET shortable_after_tick = NULL "
            "WHERE ticker = 'IPZ'"
        )
    await execute_trade(conn, user_id=9330, ticker="IPZ", side="SELL", quantity=20)
    pos = await _position(conn, 9330, "IPZ")
    assert pos is not None and pos[0] < 0


async def test_snapshot_carries_short_fields(conn: AsyncConnection) -> None:
    snap = await get_instrument_snapshot(conn, "NORT")
    assert snap is not None
    assert snap.float_shares > 0
    assert float(snap.adv) >= 0
    assert snap.shortable_after_tick is None  # only IPO listings set it
