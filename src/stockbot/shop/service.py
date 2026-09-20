"""The shop: portfolio slots (escalating price, computed here rather than
stored), analyst tools and cosmetics (fixed price, tracked as entitlements
with an optional expiry for recurring items). Every purchase is a sink --
money paid in is destroyed, per the design doc's sink table.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.ledger.service import get_system_account_id, get_user_account_id, post_transfer
from stockbot.shop.errors import AlreadyOwnedError, UnknownItemError

BASE_SLOTS = 5
SLOT_BASE_PRICE_MINOR = 500
SLOT_PRICE_STEP_MINOR = 250


@dataclass(frozen=True)
class ShopItem:
    key: str
    name: str
    description: str
    kind: str
    price_minor: int | None
    duration_days: int | None
    metadata: dict[str, Any]


_SHOP_ITEM_FIELDS = {f.name for f in fields(ShopItem)}


async def list_items(conn: AsyncConnection) -> list[ShopItem]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM shop_items ORDER BY kind, key")
        rows = await cur.fetchall()
    return [ShopItem(**{k: v for k, v in row.items() if k in _SHOP_ITEM_FIELDS}) for row in rows]


def slot_price(owned: int) -> int:
    """Escalating price for the next portfolio slot, given how many the user
    already owns beyond the free base allotment."""
    return SLOT_BASE_PRICE_MINOR + SLOT_PRICE_STEP_MINOR * owned


async def get_slot_count(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quantity FROM entitlements WHERE user_id = %s AND item_key = 'slot'",
            (user_id,),
        )
        row = await cur.fetchone()
    return BASE_SLOTS + (row[0] if row else 0)


async def get_user_entitlements(conn: AsyncConnection, user_id: int) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT e.item_key, e.quantity, e.expires_at, s.name, s.kind
            FROM entitlements e
            JOIN shop_items s ON s.key = e.item_key
            WHERE e.user_id = %s AND (e.expires_at IS NULL OR e.expires_at > now())
            ORDER BY s.kind, e.item_key
            """,
            (user_id,),
        )
        return await cur.fetchall()


async def buy_slot(conn: AsyncConnection, user_id: int) -> int:
    """Buy one additional portfolio slot. Returns the price paid, minor units."""
    async with conn.transaction():
        account_id = await get_user_account_id(conn, user_id)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT quantity FROM entitlements
                WHERE user_id = %s AND item_key = 'slot'
                FOR UPDATE
                """,
                (user_id,),
            )
            row = await cur.fetchone()
        owned = row[0] if row else 0
        price = slot_price(owned)

        sink_id = await get_system_account_id(conn, "SINK")
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=sink_id,
            amount=price,
            reason="SHOP_SLOT",
        )

        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO entitlements (user_id, item_key, quantity)
                VALUES (%s, 'slot', 1)
                ON CONFLICT (user_id, item_key) DO UPDATE SET quantity = entitlements.quantity + 1
                """,
                (user_id,),
            )
    return price


async def buy_item(conn: AsyncConnection, user_id: int, item_key: str) -> int:
    """Buy (or renew) a shop item. Returns the price paid, minor units."""
    if item_key == "slot":
        return await buy_slot(conn, user_id)

    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM shop_items WHERE key = %s FOR UPDATE", (item_key,))
            item = await cur.fetchone()
        if item is None:
            raise UnknownItemError(item_key)

        account_id = await get_user_account_id(conn, user_id)

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM entitlements WHERE user_id = %s AND item_key = %s FOR UPDATE",
                (user_id, item_key),
            )
            already_owned = await cur.fetchone() is not None

        if item["duration_days"] is None and already_owned:
            raise AlreadyOwnedError(item_key)

        sink_id = await get_system_account_id(conn, "SINK")
        price = int(item["price_minor"])
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=sink_id,
            amount=price,
            reason="SHOP_ITEM",
            memo=item_key,
        )

        if item["duration_days"] is not None:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO entitlements (user_id, item_key, expires_at)
                    VALUES (%s, %s, now() + make_interval(days => %s))
                    ON CONFLICT (user_id, item_key) DO UPDATE
                    SET expires_at = GREATEST(entitlements.expires_at, now())
                                      + make_interval(days => %s)
                    """,
                    (user_id, item_key, item["duration_days"], item["duration_days"]),
                )
        else:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO entitlements (user_id, item_key) VALUES (%s, %s)",
                    (user_id, item_key),
                )

    return price
