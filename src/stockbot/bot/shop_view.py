"""Interactive shop browser: category -> item select -> detail card.

Mirrors `chart_view.py`'s shape: stateless cids carry the whole
navigation state (`shop:{action}:{arg}`), so clicks survive restarts
with no process-side session. Two entry paths reach
`handle_shop_component`: the live View's callbacks and
`StockBotClient.on_interaction`'s post-restart fallback; `_INFLIGHT`
dedups the double dispatch.

Everything renders on `response.edit_message` -- the shop sends
embeds+views only, no file attachments, so unlike the chart PNG the
type-7 interaction callback supports it, and it works on the ephemeral
messages `/shop` posts (private balances shouldn't broadcast).

CID protocol (prefix `shop:`):
- `shop:home:-`         category browse
- `shop:cat:-`          home's category Select; chosen slug in values[0]
- `shop:pick:{category}` item Select inside a category; key in values[0]
- `shop:buy:{key}`      purchase button on a detail card
- `shop:equip:{key}`    equip button on an owned TITLE/theme card
- `shop:back:{category}` Back button on a detail card
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import discord
from psycopg import AsyncConnection

from stockbot import db
from stockbot.accounts.errors import UserDisabledError
from stockbot.accounts.service import bootstrap_user
from stockbot.bot.format import EMBED_GOLD, EMBED_INFO, format_money
from stockbot.ledger.errors import (
    InsufficientFundsError,
    UnknownAccountError,
)
from stockbot.ledger.service import get_balance, get_user_account_id
from stockbot.margin.errors import MarginError
from stockbot.shop.errors import ShopError
from stockbot.shop.service import (
    BASE_SLOTS,
    MARGIN_TIER_PRICES_MINOR,
    ShopItem,
    buy_item,
    deal_today,
    equip_item,
    get_slot_count,
    is_stackable,
    is_theme_item,
    list_items,
    slot_price,
)
from stockbot.trading.errors import DuplicateInteractionError

log = logging.getLogger("stockbot.bot.shop_view")

SHOP_CID_PREFIX = "shop:"

# Display order matters (browse reads top-down); each slug is a cid arg.
CATEGORIES: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("capabilities", "Capabilities", frozenset({"SLOT", "MARGIN_TIER", "PERK"})),
    ("tools", "Trading tools", frozenset({"ORDER_TYPE", "ANALYST_TOOL"})),
    ("consumables", "Consumables", frozenset({"CONSUMABLE"})),
    ("cosmetics", "Cosmetics", frozenset({"COSMETIC", "TITLE"})),
)

_KIND_EMOJI = {
    "SLOT": "\U0001f4c8",  # 📈
    "MARGIN_TIER": "\U0001f4c8",
    "PERK": "\U0001f4c8",
    "ORDER_TYPE": "\U0001f527",  # 🔧
    "ANALYST_TOOL": "\U0001f50d",  # 🔍
    "CONSUMABLE": "\U0001f3ab",  # 🎫
    "COSMETIC": "\U0001f3a8",  # 🎨
    "TITLE": "\U0001f3f7\ufe0f",  # 🏷️
    "BADGE": "\U0001f3c5",  # 🏅
}

_CATEGORY_BY_SLUG = {slug: (label, kinds) for slug, label, kinds in CATEGORIES}
_CATEGORY_FOR_KIND = {kind: slug for slug, _label, kinds in CATEGORIES for kind in kinds}


def parse_cid(custom_id: str) -> tuple[str, str] | None:
    parts = custom_id.split(":")
    if parts[0] != "shop" or len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def is_purchasable(item: ShopItem) -> bool:
    """The storefront predicate (D2): NULL-price rows are grant-only
    badges/trophies -- EXCEPT slot and margin_tier, whose prices are
    computed (`slot_price(owned)`, `MARGIN_TIER_PRICES_MINOR[tier]`).
    Same ordering buy_item/shop_item_autocomplete use."""
    return item.price_minor is not None or item.key in ("slot", "margin_tier")


@dataclass(frozen=True)
class Entitlement:
    quantity: int
    expires_at: Any  # datetime | None


@dataclass
class ShopState:
    items: list[ShopItem]
    entitlements: dict[str, Entitlement]  # live entitlements only
    expired_keys: set[str]  # owned-but-lapsed -> "renew" card state
    slot_count: int
    balance_minor: int
    equipped_title: str | None
    equipped_theme: str | None
    deal_key: str | None = None  # today's discounted item (0067)
    deal_pct: float = 0.0

    def owned(self, key: str) -> int:
        ent = self.entitlements.get(key)
        return ent.quantity if ent else 0

    def is_owned(self, key: str) -> bool:
        return key in self.entitlements


async def load_shop_state(conn: AsyncConnection, user_id: int) -> ShopState:
    """Everything a render needs: catalog, entitlements (live AND
    expired -- a lapsed renewal shows "renew", not "buy"), slots,
    balance, and the two equip slots for Equip-button state."""
    items = await list_items(conn)
    async with conn.cursor() as cur:
        # Read entitlements directly rather than get_user_entitlements:
        # the live-only filter would hide expired renewals, which the
        # card needs to distinguish "renew -- expired" from never-owned.
        await cur.execute(
            """
            SELECT item_key, quantity, expires_at,
                   (expires_at IS NULL OR expires_at > now()) AS live
            FROM entitlements WHERE user_id = %s
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        live: dict[str, Entitlement] = {}
        expired: set[str] = set()
        for key, qty, exp, is_live in rows:
            if is_live:
                live[str(key)] = Entitlement(quantity=int(qty), expires_at=exp)
            else:
                expired.add(str(key))
        await cur.execute("SELECT equipped_title FROM users WHERE id = %s", (user_id,))
        urow = await cur.fetchone()
        await cur.execute("SELECT theme FROM chart_prefs WHERE user_id = %s", (user_id,))
        trow = await cur.fetchone()
    # A stale-message clicker who never ran a command has no account --
    # render with a zero balance rather than failing the whole browse.
    try:
        account_id = await get_user_account_id(conn, user_id)
        balance = await get_balance(conn, account_id)
    except UnknownAccountError:
        balance = 0
    deal = await deal_today(conn)
    return ShopState(
        items=items,
        entitlements=live,
        expired_keys=expired,
        slot_count=await get_slot_count(conn, user_id),
        balance_minor=balance,
        equipped_title=str(urow[0]) if urow and urow[0] else None,
        equipped_theme=str(trow[0]) if trow and trow[0] else None,
        deal_key=deal[0] if deal else None,
        deal_pct=deal[1] if deal else 0.0,
    )


def _emoji(item: ShopItem) -> str:
    return _KIND_EMOJI.get(item.kind, "\U0001f6d2")  # 🛒


def _price_minor(item: ShopItem, state: ShopState) -> int | None:
    """Computed price in minor units; None when the item can't currently
    be bought (margin tier maxed)."""
    if item.key == "slot":
        return slot_price(state.slot_count - BASE_SLOTS)
    if item.key == "margin_tier":
        owned = state.owned("margin_tier")
        if owned >= len(MARGIN_TIER_PRICES_MINOR):
            return None
        return MARGIN_TIER_PRICES_MINOR[owned]
    return item.price_minor


def _deal_price(item: ShopItem, state: ShopState, price: int) -> str:
    """Strikethrough pricing when the item is today's deal."""
    if state.deal_key == item.key and state.deal_pct > 0:
        deal_price = round(price * (1 - state.deal_pct))
        return f"~~{format_money(price)}~~ **{format_money(deal_price)}** DEAL"
    return format_money(price)


def _price_text(item: ShopItem, state: ShopState) -> str:
    """The card/select price line."""
    if item.key == "slot":
        price = _price_minor(item, state)
        assert price is not None  # slot_price always returns an int
        return f"next slot {format_money(price)} · you own {state.slot_count}"
    if item.key == "margin_tier":
        owned = state.owned("margin_tier")
        if owned >= len(MARGIN_TIER_PRICES_MINOR):
            return f"max tier ({owned}) owned"
        return f"tier {owned} → {owned + 1} · {format_money(MARGIN_TIER_PRICES_MINOR[owned])}"
    price = item.price_minor
    if price is None:
        return ""
    owned = state.owned(item.key)
    tag = _deal_price(item, state, price)
    if item.duration_days is not None:
        ent = state.entitlements.get(item.key)
        if ent is not None and ent.expires_at is not None:
            until = discord.utils.format_dt(ent.expires_at, "d")
            return f"{tag} to renew · active until {until}"
        if item.key in state.expired_keys:
            return f"{tag} to renew · expired"
        return f"{tag} · renews every {item.duration_days}d"
    if is_stackable(item.kind, item.key) and owned > 0:
        return f"{tag} · you own ×{owned}"
    return tag


def _items_in(state: ShopState, slug: str) -> list[ShopItem]:
    entry = _CATEGORY_BY_SLUG.get(slug)
    if entry is None:
        return []
    _label, kinds = entry
    return [i for i in state.items if i.kind in kinds and is_purchasable(i)]


class _ShopSelect(discord.ui.Select[discord.ui.View]):
    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_shop_component(interaction)


class _ShopButton(discord.ui.Button[discord.ui.View]):
    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_shop_component(interaction)


def build_shop_home(state: ShopState) -> tuple[discord.Embed, discord.ui.View]:
    embed = discord.Embed(title="Shop", color=EMBED_GOLD)
    embed.description = f"Balance: **{format_money(state.balance_minor)}**"
    if state.deal_key is not None:
        deal_item = find_shop_item(state, state.deal_key)
        if deal_item is not None and deal_item.price_minor is not None:
            pct = round(state.deal_pct * 100)
            deal_price = round(deal_item.price_minor * (1 - state.deal_pct))
            embed.add_field(
                name="🎉 Today's deal",
                value=(
                    f"**{deal_item.name}** — ~~{format_money(deal_item.price_minor)}~~ "
                    f"**{format_money(deal_price)}** (−{pct}%) · `/shop item:{state.deal_key}`"
                ),
                inline=False,
            )
    options: list[discord.SelectOption] = []
    for slug, label, _kinds in CATEGORIES:
        items = _items_in(state, slug)
        prices = [p for i in items if (p := _price_minor(i, state)) is not None]
        price_range = ""
        if prices:
            lo, hi = format_money(min(prices)), format_money(max(prices))
            price_range = f"{lo}–{hi}" if lo != hi else lo
        stats = f"{len(items)} items · {price_range or 'n/a'}"
        embed.add_field(name=label, value=stats, inline=False)
        options.append(
            discord.SelectOption(label=label, value=slug, description=stats[:100])
        )
    view = discord.ui.View(timeout=None)
    view.add_item(
        _ShopSelect(
            custom_id=f"{SHOP_CID_PREFIX}cat:-",
            placeholder="Browse a category…",
            options=options,
        )
    )
    embed.set_footer(text="Pick a category, or /shop item:<key> to jump straight to a card.")
    return embed, view


def build_shop_category(state: ShopState, slug: str) -> tuple[discord.Embed, discord.ui.View]:
    label = _CATEGORY_BY_SLUG[slug][0]
    items = _items_in(state, slug)
    embed = discord.Embed(title=f"Shop — {label}", color=EMBED_GOLD)
    embed.description = f"Balance: **{format_money(state.balance_minor)}**"
    options: list[discord.SelectOption] = []
    for item in items:
        owned_mark = " ✓" if state.is_owned(item.key) else ""
        desc = _price_text(item, state)
        if item.description:
            tail = f" — {item.description[:60]}"
            desc = (desc + tail)[:100]
        options.append(
            discord.SelectOption(
                label=f"{item.name}{owned_mark}"[:100],
                value=item.key,
                description=desc[:100] or item.kind,
                emoji=_emoji(item),
            )
        )
    view = discord.ui.View(timeout=None)
    view.add_item(
        _ShopSelect(
            custom_id=f"{SHOP_CID_PREFIX}pick:{slug}",
            placeholder=f"Pick an item in {label}…",
            options=options,
        )
    )
    view.add_item(
        _ShopButton(
            style=discord.ButtonStyle.secondary,
            label="Back",
            custom_id=f"{SHOP_CID_PREFIX}home:-",
            row=1,
        )
    )
    return embed, view


def _buy_button_state(item: ShopItem, state: ShopState) -> tuple[str, bool]:
    """(label, disabled) for the card's primary action."""
    owned = state.is_owned(item.key)
    if item.key == "margin_tier":
        # Repeat buys are tier upgrades, not stacking -- only the cap
        # disables the button (buy_margin_tier raises the same cap).
        if state.owned("margin_tier") >= len(MARGIN_TIER_PRICES_MINOR):
            return "Max tier", True
        return "Upgrade" if owned else "Buy", False
    if item.key == "slot":
        return "Buy", False
    if owned and item.duration_days is None and not is_stackable(item.kind, item.key):
        return "Owned", True
    if owned and item.duration_days is not None:
        return "Renew", False
    if item.key in state.expired_keys:
        return "Renew", False
    return "Buy", False


def _equippable(item: ShopItem, state: ShopState) -> bool:
    return item.kind == "TITLE" or is_theme_item(item.kind, item.metadata)


def _is_equipped(item: ShopItem, state: ShopState) -> bool:
    if item.kind == "TITLE":
        return state.equipped_title == item.key
    if is_theme_item(item.kind, item.metadata):
        # chart_prefs.theme stores the item key (equip_theme upserts
        # item_key directly), not metadata["theme"].
        return state.equipped_theme == item.key
    return False


def build_shop_card(
    state: ShopState, item: ShopItem, *, note: str | None = None
) -> tuple[discord.Embed, discord.ui.View]:
    slug = _CATEGORY_FOR_KIND.get(item.kind, "capabilities")
    embed = discord.Embed(title=f"{_emoji(item)} {item.name}", color=EMBED_INFO)
    lines = [item.description, "", f"**Price:** {_price_text(item, state)}"]
    owned = state.owned(item.key)
    if owned:
        extra = f" ×{owned}" if is_stackable(item.kind, item.key) else ""
        lines.append(f"**Status:** Owned{extra}")
    elif item.key in state.expired_keys:
        lines.append("**Status:** Expired — renew to reactivate")
    if note:
        lines.append(note)
    embed.description = "\n".join(lines)

    view = discord.ui.View(timeout=None)
    if state.is_owned(item.key) and _equippable(item, state):
        equipped = _is_equipped(item, state)
        view.add_item(
            _ShopButton(
                style=discord.ButtonStyle.primary,
                label="Equipped" if equipped else "Equip",
                custom_id=f"{SHOP_CID_PREFIX}equip:{item.key}",
                disabled=equipped,
            )
        )
    else:
        label, disabled = _buy_button_state(item, state)
        view.add_item(
            _ShopButton(
                style=discord.ButtonStyle.primary,
                label=label,
                custom_id=f"{SHOP_CID_PREFIX}buy:{item.key}",
                disabled=disabled,
            )
        )
    view.add_item(
        _ShopButton(
            style=discord.ButtonStyle.secondary,
            label="Back",
            custom_id=f"{SHOP_CID_PREFIX}back:{slug}",
            row=0,
        )
    )
    return embed, view


def find_shop_item(state: ShopState, key: str) -> ShopItem | None:
    key = key.lower()
    for item in state.items:
        if item.key == key:
            return item
    return None


_INFLIGHT: set[int] = set()


async def handle_shop_component(interaction: discord.Interaction) -> None:
    # Same double-dispatch as chart components: a live View's callback and
    # on_interaction both fire on the SAME Interaction object -- the set
    # claims the id atomically so the loser no-ops.
    if interaction.response.is_done() or interaction.id in _INFLIGHT:
        return
    _INFLIGHT.add(interaction.id)
    try:
        await _handle(interaction)
    finally:
        _INFLIGHT.discard(interaction.id)


async def _edit(
    interaction: discord.Interaction,
    embed: discord.Embed,
    view: discord.ui.View,
) -> None:
    await interaction.response.edit_message(embed=embed, view=view)


async def _handle(interaction: discord.Interaction) -> None:
    data: Any = interaction.data or {}
    parsed = parse_cid(str(data.get("custom_id", "")))
    if parsed is None:
        await interaction.response.send_message(
            "That shop is stale — run `/shop` for a fresh one.", ephemeral=True
        )
        return
    async with db.connection() as conn:
        await respond_shop_action(conn, interaction, *parsed)


async def respond_shop_action(
    conn: AsyncConnection,
    interaction: discord.Interaction,
    action: str,
    arg: str,
) -> None:
    """Dispatch a parsed cid. Exported seam: tests inject the fixture
    connection (handle_shop_component itself needs the live pool)."""
    data: Any = interaction.data or {}
    values = list(data.get("values") or [])
    user_id = interaction.user.id

    if action == "buy":
        # The suspension gate runs here too -- a button can't bypass
        # what commands enforce (UserDisabledError out of bootstrap).
        try:
            await bootstrap_user(conn, user_id)
            price = await buy_item(conn, user_id, arg, interaction_id=str(interaction.id))
        except InsufficientFundsError:
            await interaction.response.send_message(
                "Insufficient funds for that purchase.", ephemeral=True
            )
            return
        except DuplicateInteractionError:
            await interaction.response.send_message(
                "That purchase was already processed.", ephemeral=True
            )
            return
        except UserDisabledError as exc:
            # Same notice shape as _instrument_one's command gate.
            notice = "This account is suspended."
            if exc.reason:
                notice += f" Reason: {exc.reason}"
            await interaction.response.send_message(notice, ephemeral=True)
            return
        except (ShopError, MarginError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        state = await load_shop_state(conn, user_id)
        item = find_shop_item(state, arg)
        if item is None:
            await interaction.response.send_message("That item no longer exists.", ephemeral=True)
            return
        balance = await get_balance(conn, await get_user_account_id(conn, user_id))
        embed, view = build_shop_card(
            state,
            item,
            note=f"✅ Purchased for {format_money(price)} — balance {format_money(balance)}",
        )
        await _edit(interaction, embed, view)
        return

    if action == "equip":
        try:
            await bootstrap_user(conn, user_id)
            slot = await equip_item(conn, user_id, arg)
        except UserDisabledError as exc:
            notice = "This account is suspended."
            if exc.reason:
                notice += f" Reason: {exc.reason}"
            await interaction.response.send_message(notice, ephemeral=True)
            return
        except ShopError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        state = await load_shop_state(conn, user_id)
        item = find_shop_item(state, arg)
        if item is None:
            await interaction.response.send_message("That item no longer exists.", ephemeral=True)
            return
        embed, view = build_shop_card(state, item, note=f"Equipped as your {slot}.")
        await _edit(interaction, embed, view)
        return

    state = await load_shop_state(conn, user_id)
    if action == "home":
        embed, view = build_shop_home(state)
    elif action == "cat":
        slug = values[0] if values else ""
        if slug not in _CATEGORY_BY_SLUG:
            await interaction.response.send_message("Unknown category.", ephemeral=True)
            return
        embed, view = build_shop_category(state, slug)
    elif action == "pick":
        key = values[0] if values else ""
        item = find_shop_item(state, key)
        if item is None or not is_purchasable(item):
            await interaction.response.send_message("That item isn't for sale.", ephemeral=True)
            return
        embed, view = build_shop_card(state, item)
    elif action == "back":
        slug = arg if arg in _CATEGORY_BY_SLUG else "capabilities"
        embed, view = build_shop_category(state, slug)
    else:
        await interaction.response.send_message(
            "That shop is stale — run `/shop` for a fresh one.",
            ephemeral=True,
        )
        return
    await _edit(interaction, embed, view)
