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

from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
    record_idempotency_key,
)
from stockbot.margin import service as margin
from stockbot.shop.errors import (
    AlreadyOwnedError,
    NotEquippableError,
    NotOwnedError,
    UnknownItemError,
)

BASE_SLOTS = 5
SLOT_BASE_PRICE_MINOR = 500
SLOT_PRICE_STEP_MINOR = 250

# Phase 2: escalating margin-tier prices. Tier N unlocks shorts with a
# gross-leverage cap of min(N+1, max_gross_leverage) x equity.
MARGIN_TIER_PRICES_MINOR = (50_000, 200_000, 500_000)


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


# Theme palette keys a shop item's metadata may set -- anything else is
# ignored and unspecified keys fall back to the renderer defaults.
_PALETTE_KEYS = {
    "up", "down", "bg", "grid", "text", "accent",
    "spine", "muted", "halt_flow", "halt_model", "pill_text",
}


def palette_from_metadata(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    """Normalize a COSMETIC item's metadata into a render palette -- None
    when the item isn't a theme. Legacy `chart_color` (pre-0046 rows) maps
    onto up+accent when the full keys are absent."""
    if not isinstance(metadata, dict):
        return None
    pal = {
        k: v
        for k, v in metadata.items()
        if k in _PALETTE_KEYS and isinstance(v, str)
    }
    legacy = metadata.get("chart_color")
    if isinstance(legacy, str):
        pal.setdefault("up", legacy)
        pal.setdefault("accent", legacy)
    return pal or None


def is_theme_item(kind: str, metadata: dict[str, Any] | None) -> bool:
    return kind == "COSMETIC" and palette_from_metadata(metadata) is not None


async def owns_item(conn: AsyncConnection, user_id: int, item_key: str) -> bool:
    """Live (unexpired, quantity>0 by CHECK) entitlement check -- the gate
    for equippable/unlockable shop items."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1 FROM entitlements
            WHERE user_id = %s AND item_key = %s
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (user_id, item_key),
        )
        return await cur.fetchone() is not None


async def equip_theme(conn: AsyncConnection, user_id: int, item_key: str) -> None:
    """Write the equipped theme onto chart_prefs, creating the row with
    default view prefs when the user has never touched a chart. Caller
    owns ownership checks and the transaction."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO chart_prefs (user_id, span, axis, theme)
            VALUES (%s, 240, 'time', %s)
            ON CONFLICT (user_id) DO UPDATE
            SET theme = EXCLUDED.theme, updated_at = now()
            """,
            (user_id, item_key),
        )


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


async def _lock_user_account(conn: AsyncConnection, account_id: int) -> None:
    """Serialize this user's purchases. The entitlement row being read below
    doesn't exist on a first-ever buy, so `FOR UPDATE` on entitlements locks
    nothing and two concurrent first buys would both read owned=0 and both
    pay the cheapest price. The account row always exists; locking it
    serializes the read-modify-write (same pattern as claim_daily)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,))


async def buy_slot(
    conn: AsyncConnection, user_id: int, *, interaction_id: str | None = None
) -> int:
    """Buy one additional portfolio slot. Returns the price paid, minor units."""
    async with conn.transaction():
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        account_id = await get_user_account_id(conn, user_id)
        await _lock_user_account(conn, account_id)
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

        await margin.assert_spend_ok(conn, user_id, price)
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


async def buy_margin_tier(
    conn: AsyncConnection, user_id: int, *, interaction_id: str | None = None
) -> int:
    """Buy the next margin tier. Returns the price paid, minor units."""
    async with conn.transaction():
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        account_id = await get_user_account_id(conn, user_id)
        # Same first-buy race as buy_slot: the entitlement row may not exist.
        await _lock_user_account(conn, account_id)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT quantity FROM entitlements
                WHERE user_id = %s AND item_key = 'margin_tier'
                FOR UPDATE
                """,
                (user_id,),
            )
            row = await cur.fetchone()
        owned = row[0] if row else 0
        if owned >= len(MARGIN_TIER_PRICES_MINOR):
            raise AlreadyOwnedError("margin_tier")
        price = MARGIN_TIER_PRICES_MINOR[owned]

        await margin.assert_spend_ok(conn, user_id, price)
        sink_id = await get_system_account_id(conn, "SINK")
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=sink_id,
            amount=price,
            reason="SHOP_MARGIN_TIER",
        )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO entitlements (user_id, item_key, quantity)
                VALUES (%s, 'margin_tier', 1)
                ON CONFLICT (user_id, item_key)
                DO UPDATE SET quantity = entitlements.quantity + 1
                """,
                (user_id,),
            )
    return price


async def buy_item(
    conn: AsyncConnection, user_id: int, item_key: str, *, interaction_id: str | None = None
) -> int:
    """Buy (or renew) a shop item. Returns the price paid, minor units."""
    if item_key == "slot":
        return await buy_slot(conn, user_id, interaction_id=interaction_id)
    if item_key == "margin_tier":
        return await buy_margin_tier(conn, user_id, interaction_id=interaction_id)

    async with conn.transaction():
        if interaction_id is not None:
            await record_idempotency_key(conn, interaction_id)
        # Locking the shop_items row also serializes purchases of this item:
        # a concurrent first buy waits here, then sees the entitlement row.
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM shop_items WHERE key = %s FOR UPDATE", (item_key,))
            item = await cur.fetchone()
        if item is None:
            raise UnknownItemError(item_key)
        if item["price_minor"] is None:
            # Grant-only catalog rows (badges, trophies) aren't purchasable.
            raise UnknownItemError(item_key)

        account_id = await get_user_account_id(conn, user_id)

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM entitlements WHERE user_id = %s AND item_key = %s FOR UPDATE",
                (user_id, item_key),
            )
            already_owned = await cur.fetchone() is not None

        # CONSUMABLE/PERK stack: a repeat buy is quantity + 1, not a
        # duplicate-ownership rejection.
        stackable = item["kind"] in ("CONSUMABLE", "PERK")
        if item["duration_days"] is None and already_owned and not stackable:
            raise AlreadyOwnedError(item_key)

        sink_id = await get_system_account_id(conn, "SINK")
        price = int(item["price_minor"])
        await margin.assert_spend_ok(conn, user_id, price)
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
        elif stackable:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO entitlements (user_id, item_key)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, item_key) DO UPDATE
                    SET quantity = entitlements.quantity + 1
                    """,
                    (user_id, item_key),
                )
        else:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO entitlements (user_id, item_key) VALUES (%s, %s)",
                    (user_id, item_key),
                )

        # Equippables "just work": buying one equips it immediately.
        # /equip switches slots; /unequip clears them.
        if item["kind"] == "TITLE":
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET equipped_title = %s WHERE id = %s",
                    (item_key, user_id),
                )
        elif is_theme_item(str(item["kind"]), item["metadata"]):
            await equip_theme(conn, user_id, item_key)

    return price


async def use_consumable(
    conn: AsyncConnection, user_id: int, item_key: str
) -> int:
    """Spend one unit of a consumable; returns the remaining quantity.
    entitlements CHECK forbids a 0 row, so the last unit's spend deletes
    it. Caller owns the transaction."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE entitlements SET quantity = quantity - 1
            WHERE user_id = %s AND item_key = %s AND quantity > 1
              AND (expires_at IS NULL OR expires_at > now())
            RETURNING quantity
            """,
            (user_id, item_key),
        )
        row = await cur.fetchone()
        if row is not None:
            return int(row[0])
        await cur.execute(
            """
            DELETE FROM entitlements
            WHERE user_id = %s AND item_key = %s AND quantity = 1
              AND (expires_at IS NULL OR expires_at > now())
            RETURNING item_key
            """,
            (user_id, item_key),
        )
        if await cur.fetchone() is None:
            raise NotOwnedError(item_key)
        return 0


async def equip_item(
    conn: AsyncConnection, user_id: int, item_key: str
) -> str:
    """Equip an owned item into its slot; returns the slot name
    ('title' | 'theme'). kind->slot routing: TITLE -> users.equipped_title,
    COSMETIC-with-palette -> chart_prefs.theme."""
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT kind, metadata FROM shop_items WHERE key = %s",
                (item_key,),
            )
            item = await cur.fetchone()
        if item is None:
            raise UnknownItemError(item_key)
        if not await owns_item(conn, user_id, item_key):
            raise NotOwnedError(item_key)

        if item["kind"] == "TITLE":
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET equipped_title = %s WHERE id = %s",
                    (item_key, user_id),
                )
            return "title"
        if is_theme_item(str(item["kind"]), item["metadata"]):
            await equip_theme(conn, user_id, item_key)
            return "theme"
        raise NotEquippableError(item_key)


async def unequip(conn: AsyncConnection, user_id: int, slot: str) -> bool:
    """Clear an equip slot ('title' | 'theme'); returns whether anything
    was equipped."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            if slot == "title":
                await cur.execute(
                    "UPDATE users SET equipped_title = NULL "
                    "WHERE id = %s AND equipped_title IS NOT NULL",
                    (user_id,),
                )
            elif slot == "theme":
                await cur.execute(
                    "UPDATE chart_prefs SET theme = NULL, updated_at = now() "
                    "WHERE user_id = %s AND theme IS NOT NULL",
                    (user_id,),
                )
            else:
                raise NotEquippableError(slot)
            return cur.rowcount > 0
