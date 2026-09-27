from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.alerts.errors import AlertLimitError
from stockbot.alerts.service import create_alert
from stockbot.claims.service import claim_daily
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.market.tick import apply_tick
from stockbot.quests.service import (
    RerollError,
    list_quests,
    reroll_quest,
    sweep_completions,
)
from stockbot.shop.errors import NotOwnedError
from stockbot.shop.service import buy_item, use_consumable
from stockbot.trading.service import execute_trade


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn, from_account_id=faucet_id, to_account_id=account_id,
        amount=amount, reason="TEST",
    )


async def _quantity(conn: AsyncConnection, user_id: int, key: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quantity FROM entitlements WHERE user_id = %s AND item_key = %s",
            (user_id, key),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _insert_instance(
    conn: AsyncConnection,
    def_key: str = "trade5",
    *,
    user_id: int | None = None,
    window: tuple[int, int] = (0, 10_000),
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO quest_instances
                (def_key, kind, period, period_index, window_start,
                 window_end, target, reward_minor, user_id)
            SELECT key, kind, period, 1, %s, %s, target, reward_minor, %s
            FROM quest_defs WHERE key = %s
            RETURNING id
            """,
            (window[0], window[1], user_id, def_key),
        )
        (iid,) = await cur.fetchone()
    return int(iid)


async def test_use_consumable_decrements_and_deletes_at_zero(
    conn: AsyncConnection,
) -> None:
    uid = 9301
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "quest_reroll")
    await buy_item(conn, uid, "quest_reroll")  # stacks
    assert await _quantity(conn, uid, "quest_reroll") == 2

    assert await use_consumable(conn, uid, "quest_reroll") == 1
    assert await _quantity(conn, uid, "quest_reroll") == 1
    assert await use_consumable(conn, uid, "quest_reroll") == 0
    assert await _quantity(conn, uid, "quest_reroll") == 0  # row deleted

    with pytest.raises(NotOwnedError):
        await use_consumable(conn, uid, "quest_reroll")


async def test_permanent_item_still_rejects_second_buy(
    conn: AsyncConnection,
) -> None:
    from stockbot.shop.errors import AlreadyOwnedError

    uid = 9302
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "theme_vapor")
    with pytest.raises(AlreadyOwnedError):
        await buy_item(conn, uid, "theme_vapor")


async def test_reroll_creates_personal_instance_and_hides_swapped(
    conn: AsyncConnection,
) -> None:
    uid = 9303
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "quest_reroll")
    iid = await _insert_instance(conn)

    new = await reroll_quest(conn, uid, iid)

    # Swap recorded + personal replacement instance exists.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT replacement_instance_id FROM quest_swaps "
            "WHERE user_id = %s AND quest_instance_id = %s",
            (uid, iid),
        )
        (repl,) = await cur.fetchone()
        await cur.execute(
            "SELECT user_id, window_start, window_end FROM quest_instances "
            "WHERE id = %s",
            (new["id"],),
        )
        prow = await cur.fetchone()
    assert repl == new["id"]
    assert prow == (uid, 0, 10_000)

    # list_quests hides the swapped quest and shows the personal one.
    rows = await list_quests(conn, uid, 0)
    ids = {r["id"] for r in rows}
    assert iid not in ids and new["id"] in ids
    # Other users still see the global instance, not the personal one.
    other = await list_quests(conn, 9304, 0)
    other_ids = {r["id"] for r in other}
    assert iid in other_ids and new["id"] not in other_ids


async def test_reroll_stops_tracking_swapped_quest(conn: AsyncConnection) -> None:
    """Reroll is real: progress on the swapped quest's measure must not
    complete it in the sweep."""
    uid = 9305
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 200_000)
    await buy_item(conn, uid, "quest_reroll")
    iid = await _insert_instance(conn, "trade5")  # 5 trades
    await reroll_quest(conn, uid, iid)

    await apply_tick(conn, "cons-seed")  # land on an OPEN tick
    for _ in range(6):
        await execute_trade(conn, user_id=uid, ticker="NORT", side="BUY", quantity=1)
    await sweep_completions(conn, 5)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM quest_completions "
            "WHERE quest_instance_id = %s AND user_id = %s",
            (iid, uid),
        )
        (n,) = await cur.fetchone()
    assert n == 0


async def test_reroll_personal_instance_completes_for_owner(
    conn: AsyncConnection,
) -> None:
    """The replacement measures its owner's window activity."""
    uid = 9306
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 200_000)
    await buy_item(conn, uid, "quest_reroll")
    iid = await _insert_instance(conn, "short1")  # DAILY, SHORT_PROFIT
    new = await reroll_quest(conn, uid, iid)

    # Whatever the replacement measures, its sweep must only see the
    # owner's rows -- a different user's trades can't complete it.
    await bootstrap_user(conn, 9307)
    await _give_cash(conn, 9307, 200_000)
    await apply_tick(conn, "cons-seed")
    for _ in range(6):
        await execute_trade(conn, user_id=9307, ticker="NORT", side="BUY", quantity=1)
    await sweep_completions(conn, 5)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM quest_completions "
            "WHERE quest_instance_id = %s",
            (new["id"],),
        )
        (n,) = await cur.fetchone()
    assert n == 0


async def test_reroll_requires_token_and_visible_quest(
    conn: AsyncConnection,
) -> None:
    uid = 9308
    await bootstrap_user(conn, uid)
    iid = await _insert_instance(conn)
    with pytest.raises(NotOwnedError):
        await reroll_quest(conn, uid, iid)
    with pytest.raises(RerollError):
        await reroll_quest(conn, uid, 999_999)


async def test_streak_shield_bridges_one_missed_day(
    conn: AsyncConnection,
) -> None:
    uid = 9309
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "streak_shield")

    d = date(2026, 9, 20)
    r1 = await claim_daily(
        conn, uid, as_of_date=d, enforce_first_claim_delay=False
    )
    assert (r1.streak, r1.shield_used) == (1, False)
    # Day 2 missed; day 3 claim consumes the shield and keeps the streak.
    r3 = await claim_daily(
        conn, uid, as_of_date=d.replace(day=22), enforce_first_claim_delay=False
    )
    assert (r3.streak, r3.shield_used) == (2, True)
    assert await _quantity(conn, uid, "streak_shield") == 0


async def test_streak_shield_not_consumed_on_longer_gap(
    conn: AsyncConnection,
) -> None:
    uid = 9310
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    await buy_item(conn, uid, "streak_shield")

    d = date(2026, 9, 20)
    await claim_daily(conn, uid, as_of_date=d, enforce_first_claim_delay=False)
    # Two missed days -> shield stays, streak resets.
    r5 = await claim_daily(
        conn, uid, as_of_date=d.replace(day=23), enforce_first_claim_delay=False
    )
    assert (r5.streak, r5.shield_used) == (1, False)
    assert await _quantity(conn, uid, "streak_shield") == 1


async def test_alert_pack_raises_cap(conn: AsyncConnection) -> None:
    uid = 9311
    await bootstrap_user(conn, uid)
    await _give_cash(conn, uid, 10_000)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 2 WHERE key = 'alerts.max_per_user'"
        )
    for i in range(2):
        await create_alert(
            conn, user_id=uid, ticker="NORT", direction="ABOVE",
            target=Decimal(10 + i),
        )
    with pytest.raises(AlertLimitError):
        await create_alert(
            conn, user_id=uid, ticker="NORT", direction="ABOVE",
            target=Decimal(30),
        )

    await buy_item(conn, uid, "alert_pack")  # +10
    for i in range(10):
        await create_alert(
            conn, user_id=uid, ticker="NORT", direction="ABOVE",
            target=Decimal(100 + i),
        )
    with pytest.raises(AlertLimitError):
        await create_alert(
            conn, user_id=uid, ticker="NORT", direction="ABOVE",
            target=Decimal(200),
        )
