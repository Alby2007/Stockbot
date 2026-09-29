from __future__ import annotations

from datetime import date

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import get_balance
from stockbot.shop.errors import AlreadyOwnedError, ShopError, UnknownItemError
from stockbot.shop.service import (
    BASE_SLOTS,
    _deal_keys,
    buy_item,
    daily_deal,
    deal_today,
    get_slot_count,
    get_user_entitlements,
    on_day,
    owns_item,
    use_consumable,
    warn_expiring,
)
from stockbot.trading.errors import DuplicateInteractionError, TooManyPositionsError
from stockbot.trading.service import execute_trade


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    from stockbot.ledger.service import get_system_account_id, get_user_account_id, post_transfer

    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id, amount=amount, reason="TEST"
    )


async def test_default_slot_count_is_base(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3001)
    assert await get_slot_count(conn, 3001) == BASE_SLOTS


async def test_buying_a_slot_increases_slot_count_and_costs_money(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 3002)).account_id
    await _give_cash(conn, 3002, 100_000)
    balance_before = await get_balance(conn, account_id)

    price = await buy_item(conn, 3002, "slot")

    assert await get_slot_count(conn, 3002) == BASE_SLOTS + 1
    assert await get_balance(conn, account_id) == balance_before - price


async def test_slot_price_escalates(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3003)
    await _give_cash(conn, 3003, 1_000_000)
    first = await buy_item(conn, 3003, "slot")
    second = await buy_item(conn, 3003, "slot")
    assert second > first


async def test_permanent_item_cannot_be_bought_twice(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3004)
    await _give_cash(conn, 3004, 1_000_000)
    await buy_item(conn, 3004, "theme_sunrise")
    with pytest.raises(AlreadyOwnedError):
        await buy_item(conn, 3004, "theme_sunrise")


async def test_recurring_item_can_be_renewed(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3005)
    await _give_cash(conn, 3005, 1_000_000)
    await buy_item(conn, 3005, "analyst_tools")
    await buy_item(conn, 3005, "analyst_tools")  # should not raise

    entitlements = await get_user_entitlements(conn, 3005)
    assert any(e["item_key"] == "analyst_tools" for e in entitlements)


async def test_unknown_item_raises(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3006)
    with pytest.raises(UnknownItemError):
        await buy_item(conn, 3006, "does_not_exist")


async def test_insufficient_funds_for_shop_purchase(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3007)  # only the starting grant
    with pytest.raises(InsufficientFundsError):
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")
        await buy_item(conn, 3007, "slot")


async def test_replayed_shop_buy_is_a_noop_not_a_double_charge(conn: AsyncConnection) -> None:
    """A redelivered Discord interaction (client retry) carries the same id;
    the second attempt must be a no-op, not a second charge."""
    account_id = (await bootstrap_user(conn, 3009)).account_id
    await _give_cash(conn, 3009, 1_000_000)

    await buy_item(conn, 3009, "slot", interaction_id="shop-interaction-1")
    balance_after_first = await get_balance(conn, account_id)

    with pytest.raises(DuplicateInteractionError):
        await buy_item(conn, 3009, "slot", interaction_id="shop-interaction-1")

    assert await get_balance(conn, account_id) == balance_after_first
    assert await get_slot_count(conn, 3009) == BASE_SLOTS + 1


async def test_buying_beyond_slot_count_is_rejected(conn: AsyncConnection) -> None:
    account_id = (await bootstrap_user(conn, 3008)).account_id
    await _give_cash(conn, 3008, 10_000_000)

    async with conn.cursor() as cur:
        await cur.execute("SELECT ticker FROM instruments ORDER BY id LIMIT %s", (BASE_SLOTS + 1,))
        tickers = [row[0] for row in await cur.fetchall()]

    for ticker in tickers[:BASE_SLOTS]:
        await execute_trade(conn, user_id=3008, ticker=ticker, side="BUY", quantity=1)

    with pytest.raises(TooManyPositionsError):
        await execute_trade(conn, user_id=3008, ticker=tickers[BASE_SLOTS], side="BUY", quantity=1)

    # Buying a slot should unblock the next position.
    await buy_item(conn, 3008, "slot")
    await execute_trade(conn, user_id=3008, ticker=tickers[BASE_SLOTS], side="BUY", quantity=1)
    assert account_id is not None


# ---------------------------------------------------------------------------
# Lifecycle (0067): gifts, expiry warnings, the daily deal, listing credits
# ---------------------------------------------------------------------------


async def _notification_count(conn: AsyncConnection, user_id: int, kind: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = %s AND kind = %s",
            (user_id, kind),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_gift_debits_payer_and_grants_recipient(conn: AsyncConnection) -> None:
    payer = await bootstrap_user(conn, 3100)
    await bootstrap_user(conn, 3101)
    await _give_cash(conn, 3100, 1_000_000)
    before = await get_balance(conn, payer.account_id)
    async with conn.cursor() as cur:
        # Pin the deal off -- a deal-priced gift would debit less than list.
        await cur.execute(
            "UPDATE config SET value = '0' WHERE key = 'shop.deal_enabled'"
        )

    paid = await buy_item(conn, 3100, "title_degen", gift_to=3101)

    assert paid == 1000
    assert await get_balance(conn, payer.account_id) == before - paid
    assert await owns_item(conn, 3101, "title_degen")
    assert not await owns_item(conn, 3100, "title_degen")


async def test_gift_does_not_auto_equip(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3102)
    await bootstrap_user(conn, 3103)
    await _give_cash(conn, 3102, 1_000_000)

    await buy_item(conn, 3102, "title_degen", gift_to=3103)

    async with conn.cursor() as cur:
        await cur.execute("SELECT equipped_title FROM users WHERE id = %s", (3103,))
        row = await cur.fetchone()
    assert row is not None and row[0] is None


async def test_gift_to_owner_is_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3104)
    await bootstrap_user(conn, 3105)
    await _give_cash(conn, 3104, 1_000_000)
    await buy_item(conn, 3105, "theme_sunrise")

    with pytest.raises(AlreadyOwnedError):
        await buy_item(conn, 3104, "theme_sunrise", gift_to=3105)


async def test_self_gift_is_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3106)
    await _give_cash(conn, 3106, 1_000_000)
    with pytest.raises(ShopError):
        await buy_item(conn, 3106, "title_degen", gift_to=3106)


async def test_gift_unknown_recipient_is_rejected(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3107)
    await _give_cash(conn, 3107, 1_000_000)
    with pytest.raises(ShopError):
        await buy_item(conn, 3107, "title_degen", gift_to=9_999_999)


@pytest.mark.parametrize("key", ["slot", "margin_tier"])
async def test_progression_items_cannot_be_gifted(conn: AsyncConnection, key: str) -> None:
    await bootstrap_user(conn, 3108)
    await bootstrap_user(conn, 3109)
    await _give_cash(conn, 3108, 1_000_000)
    with pytest.raises(ShopError):
        await buy_item(conn, 3108, key, gift_to=3109)


async def test_gift_writes_notification_and_feed_row(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3110)
    await bootstrap_user(conn, 3111)
    await _give_cash(conn, 3110, 1_000_000)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO feed_channels (guild_id, channel_id) VALUES (999001, 999001)"
        )

    await buy_item(conn, 3110, "title_degen", gift_to=3111)

    assert await _notification_count(conn, 3111, "GIFT_RECEIVED") == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM feed_items WHERE kind = 'GIFT' AND user_id = 3110"
        )
        row = await cur.fetchone()
    assert row is not None
    payload = row[0]
    assert payload["from"] == 3110 and payload["to"] == 3111


async def test_expiry_warning_fires_once_and_rearms_on_renewal(
    conn: AsyncConnection,
) -> None:
    await bootstrap_user(conn, 3120)
    await _give_cash(conn, 3120, 1_000_000)
    await buy_item(conn, 3120, "analyst_tools")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE entitlements SET expires_at = now() + interval '1 day' "
            "WHERE user_id = %s AND item_key = 'analyst_tools'",
            (3120,),
        )

    first = await warn_expiring(conn, 100)
    assert first >= 1
    assert await _notification_count(conn, 3120, "ENTITLEMENT_EXPIRING") == 1

    # The expiry_notice_at stamp dedups repeat sweeps.
    assert await warn_expiring(conn, 101) == 0

    # Renewal clears the stamp so a renewed item can warn again.
    await buy_item(conn, 3120, "analyst_tools")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE entitlements SET expires_at = now() + interval '1 day' "
            "WHERE user_id = %s AND item_key = 'analyst_tools'",
            (3120,),
        )
    assert await warn_expiring(conn, 102) >= 1
    assert await _notification_count(conn, 3120, "ENTITLEMENT_EXPIRING") == 2


async def test_expiry_warning_ignores_lapsed_entitlements(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3121)
    await _give_cash(conn, 3121, 1_000_000)
    await buy_item(conn, 3121, "analyst_tools")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE entitlements SET expires_at = now() - interval '1 hour' "
            "WHERE user_id = %s AND item_key = 'analyst_tools'",
            (3121,),
        )
    assert await warn_expiring(conn, 100) == 0
    assert await _notification_count(conn, 3121, "ENTITLEMENT_EXPIRING") == 0


def test_daily_deal_pick_is_deterministic() -> None:
    keys = ["a", "b", "c", "d"]
    day = date(2026, 1, 1)
    pick = daily_deal(keys, day, "seed")
    assert pick == daily_deal(keys, day, "seed")
    assert pick in keys
    assert daily_deal([], day, "seed") is None


async def test_deal_pool_excludes_computed_and_grant_only_items(
    conn: AsyncConnection,
) -> None:
    keys = await _deal_keys(conn)
    assert "slot" not in keys
    assert "margin_tier" not in keys
    assert not any(k.startswith("badge_") for k in keys)
    assert "listing_credit" in keys


async def test_deal_discount_is_charged(conn: AsyncConnection) -> None:
    deal = await deal_today(conn)
    assert deal is not None
    deal_key, pct = deal

    await bootstrap_user(conn, 3130)
    await _give_cash(conn, 3130, 10_000_000)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT price_minor FROM shop_items WHERE key = %s", (deal_key,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert await buy_item(conn, 3130, deal_key) == round(int(row[0]) * (1 - pct))

    # Everything else still charges list price.
    other = next(k for k in await _deal_keys(conn) if k != deal_key)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT price_minor FROM shop_items WHERE key = %s", (other,)
        )
        other_row = await cur.fetchone()
    assert other_row is not None
    assert await buy_item(conn, 3130, other) == int(other_row[0])


async def test_deal_disabled_by_config(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = '0' WHERE key = 'shop.deal_enabled'")
    assert await deal_today(conn) is None


async def test_on_day_warns_and_posts_deal(conn: AsyncConnection) -> None:
    from stockbot.market.engine import TICKS_PER_DAY

    await bootstrap_user(conn, 3140)
    await _give_cash(conn, 3140, 1_000_000)
    await buy_item(conn, 3140, "analyst_tools")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE entitlements SET expires_at = now() + interval '1 day' "
            "WHERE user_id = %s AND item_key = 'analyst_tools'",
            (3140,),
        )
        await cur.execute(
            "INSERT INTO feed_channels (guild_id, channel_id) VALUES (999002, 999002)"
        )

    await on_day(conn, TICKS_PER_DAY)

    assert await _notification_count(conn, 3140, "ENTITLEMENT_EXPIRING") == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT payload FROM feed_items WHERE kind = 'DEAL' "
            "AND tick_index = %s",
            (TICKS_PER_DAY,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0]["item_key"] in await _deal_keys(conn)

    # Non-boundary ticks are a no-op.
    await on_day(conn, TICKS_PER_DAY + 1)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM feed_items WHERE kind = 'DEAL' "
            "AND tick_index = %s",
            (TICKS_PER_DAY + 1,),
        )
        count_row = await cur.fetchone()
    assert count_row is not None and count_row[0] == 0


async def test_listing_credit_commissions_an_instrument(conn: AsyncConnection) -> None:
    from stockbot.admin.service import add_instrument

    await bootstrap_user(conn, 3150)
    await _give_cash(conn, 3150, 1_000_000)
    await buy_item(conn, 3150, "listing_credit")

    async with conn.transaction():
        await use_consumable(conn, 3150, "listing_credit")
        iid = await add_instrument(
            conn, ticker="ZZTEST", name="Test Co", sector_key="tech", base_price=50.0
        )

    assert not await owns_item(conn, 3150, "listing_credit")
    async with conn.cursor() as cur:
        await cur.execute("SELECT ticker FROM instruments WHERE id = %s", (iid,))
        row = await cur.fetchone()
    assert row is not None and row[0] == "ZZTEST"


async def test_failed_commission_keeps_the_credit(conn: AsyncConnection) -> None:
    from stockbot.admin.service import add_instrument

    await bootstrap_user(conn, 3151)
    await _give_cash(conn, 3151, 1_000_000)
    await buy_item(conn, 3151, "listing_credit")

    with pytest.raises(ValueError):
        async with conn.transaction():
            await use_consumable(conn, 3151, "listing_credit")
            await add_instrument(
                conn, ticker="BADX", name="x", sector_key="no_such_sector",
                base_price=1.0,
            )

    assert await owns_item(conn, 3151, "listing_credit")


async def test_purchases_ledger_shows_buys_and_gifts(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 3160)
    await bootstrap_user(conn, 3161)
    await _give_cash(conn, 3160, 1_000_000)
    await buy_item(conn, 3160, "theme_sunrise")
    await buy_item(conn, 3160, "title_degen", gift_to=3161)

    async with conn.cursor() as cur:
        # Same query /purchases runs.
        await cur.execute(
            """
            SELECT l.reason, l.memo, -l.amount AS spent
            FROM ledger_entries l
            JOIN accounts a ON a.id = l.account_id
            WHERE a.user_id = %s AND l.amount < 0 AND l.reason LIKE 'SHOP_%%'
            ORDER BY l.id DESC LIMIT 10
            """,
            (3160,),
        )
        rows = await cur.fetchall()

    by_reason = {r[0]: r[1] for r in rows}
    assert by_reason["SHOP_ITEM"] == "theme_sunrise"
    assert "gift to 3161" in by_reason["SHOP_GIFT"]
