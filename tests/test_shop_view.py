from __future__ import annotations

import random
import time
from typing import cast

import discord
import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.admin.service import disable_user
from stockbot.bot.format import format_money
from stockbot.bot.shop_view import (
    _INFLIGHT,
    CATEGORIES,
    build_shop_card,
    build_shop_category,
    build_shop_home,
    find_shop_item,
    handle_shop_component,
    is_purchasable,
    load_shop_state,
    parse_cid,
    respond_shop_action,
)
from stockbot.ledger.service import get_balance, get_user_account_id
from stockbot.shop.errors import AlreadyOwnedError
from stockbot.shop.service import (
    MARGIN_TIER_PRICES_MINOR,
    buy_item,
    owns_item,
)


def _snowflake() -> int:
    """A >30d-old snowflake so bootstrap grants fund the account."""
    rng = random.SystemRandom()
    ts_ms = int(time.time() * 1000) - 1_420_070_400_000 - rng.randrange(31, 4000) * 86_400_000
    return (ts_ms << 22) | rng.randrange(1, 1 << 22)


async def _give_cash(conn: AsyncConnection, user_id: int, amount: int) -> None:
    from stockbot.ledger.service import (
        get_system_account_id,
        post_transfer,
    )

    account_id = await get_user_account_id(conn, user_id)
    faucet_id = await get_system_account_id(conn, "FAUCET")
    await post_transfer(
        conn,
        from_account_id=faucet_id,
        to_account_id=account_id,
        amount=amount,
        reason="TEST",
    )


def _select_options(view: discord.ui.View) -> list[discord.SelectOption]:
    sel = next(c for c in view.children if isinstance(c, discord.ui.Select))
    return list(sel.options)


def _buttons(view: discord.ui.View) -> list[discord.ui.Button]:
    return [c for c in view.children if isinstance(c, discord.ui.Button)]


class _FakeResponse:
    def __init__(self, done: bool = False) -> None:
        self.done = done
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.edited: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.done = True
        self.sent.append((args, kwargs))

    async def edit_message(self, *args: object, **kwargs: object) -> None:
        self.done = True
        self.edited.append((args, kwargs))


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeInteraction:
    """Duck-type stand-in: `data` carries custom_id + select values,
    `response` records send_message vs edit_message separately."""

    def __init__(
        self,
        custom_id: str,
        *,
        values: list[str] | None = None,
        user_id: int = 1,
        interaction_id: int = 1,
        done: bool = False,
    ) -> None:
        self.id = interaction_id
        self.user = _FakeUser(user_id)
        self.data: dict[str, object] = {"custom_id": custom_id}
        if values is not None:
            self.data["values"] = values
        self.response = _FakeResponse(done)


# ---------------------------------------------------------------- cid


def test_parse_cid_roundtrips() -> None:
    assert parse_cid("shop:home:-") == ("home", "-")
    assert parse_cid("shop:cat:-") == ("cat", "-")
    assert parse_cid("shop:pick:cosmetics") == ("pick", "cosmetics")
    assert parse_cid("shop:buy:theme_sunrise") == ("buy", "theme_sunrise")
    assert parse_cid("shop:equip:title_oracle") == ("equip", "title_oracle")
    assert parse_cid("shop:back:tools") == ("back", "tools")


@pytest.mark.parametrize(
    "cid",
    [
        "",
        "bogus",
        "cbt:panl:1:2:3",
        "shop:buy",  # missing arg
        "shop::slot",  # empty action
        "shop:buy:",  # parses as empty arg -- still rejected
        "other:buy:slot",
        "shop:buy:slot:extra",
    ],
)
def test_parse_cid_rejects_malformed(cid: str) -> None:
    assert parse_cid(cid) is None


# ------------------------------------------------------------- catalog


async def test_home_has_four_category_options(conn: AsyncConnection) -> None:
    state = await load_shop_state(conn, _snowflake())
    _embed, view = build_shop_home(state)
    options = _select_options(view)
    assert len(options) == 4
    assert {o.value for o in options} == {slug for slug, _l, _k in CATEGORIES}


async def test_categories_cover_catalog_within_select_cap(
    conn: AsyncConnection,
) -> None:
    """Every purchasable item is reachable: home -> category -> item.
    No Select exceeds Discord's 25-option cap, and the union equals the
    purchasable catalog (currently 26 -- pinned so catalog drift is loud;
    includes the two card packs added in 0059 and listing_credit in 0067)."""
    state = await load_shop_state(conn, _snowflake())
    purchasable = {i.key for i in state.items if is_purchasable(i)}
    assert len(purchasable) == 26

    reachable: set[str] = set()
    for slug, _label, _kinds in CATEGORIES:
        _embed, view = build_shop_category(state, slug)
        options = _select_options(view)
        assert len(options) <= 25
        reachable.update(str(o.value) for o in options)
    assert reachable == purchasable


async def test_badges_never_reach_the_storefront(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT key FROM shop_items WHERE kind = 'BADGE'")
        badge_keys = {row[0] for row in await cur.fetchall()}
    assert badge_keys  # the fixture catalog has grant-only badges

    state = await load_shop_state(conn, _snowflake())
    purchasable = {i.key for i in state.items if is_purchasable(i)}
    assert not (badge_keys & purchasable)
    for slug, _label, _kinds in CATEGORIES:
        _e, view = build_shop_category(state, slug)
        assert not (badge_keys & {str(o.value) for o in _select_options(view)})


async def test_computed_price_items_appear_with_prices(
    conn: AsyncConnection,
) -> None:
    """slot/margin_tier have NULL price_minor but are purchasable --
    the card must show their computed price, not a blank."""
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    state = await load_shop_state(conn, user_id)
    for key in ("slot", "margin_tier"):
        item = find_shop_item(state, key)
        assert item is not None and is_purchasable(item)
        embed, _view = build_shop_card(state, item)
        assert "$" in str(embed.description), key


# ------------------------------------------------------------ card state


async def test_owned_sandbox_access_disables_buy(conn: AsyncConnection) -> None:
    """D3: sandbox_access is a PERK that must NOT stack -- owned reads
    'Owned', and the service rejects a repeat purchase."""
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await buy_item(conn, user_id, "sandbox_access")

    state = await load_shop_state(conn, user_id)
    item = find_shop_item(state, "sandbox_access")
    assert item is not None
    _embed, view = build_shop_card(state, item)
    buy = _buttons(view)[0]
    assert buy.label == "Owned"
    assert buy.disabled

    with pytest.raises(AlreadyOwnedError):
        await buy_item(conn, user_id, "sandbox_access")


async def test_owned_alert_pack_stays_buyable_with_quantity(
    conn: AsyncConnection,
) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await buy_item(conn, user_id, "alert_pack")
    await buy_item(conn, user_id, "alert_pack")

    state = await load_shop_state(conn, user_id)
    item = find_shop_item(state, "alert_pack")
    assert item is not None
    embed, view = build_shop_card(state, item)
    buy = _buttons(view)[0]
    assert buy.label == "Buy" and not buy.disabled
    assert "×2" in str(embed.description)


async def test_unowned_item_has_enabled_buy(conn: AsyncConnection) -> None:
    state = await load_shop_state(conn, _snowflake())
    item = find_shop_item(state, "theme_sunrise")
    assert item is not None
    _embed, view = build_shop_card(state, item)
    buy = _buttons(view)[0]
    assert buy.label == "Buy" and not buy.disabled


async def test_owned_title_shows_equip(conn: AsyncConnection) -> None:
    """buy_item auto-equips, so the enabled 'Equip' state needs a second
    title taking the slot -- oracle is owned but no longer equipped."""
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await buy_item(conn, user_id, "title_oracle")
    await buy_item(conn, user_id, "title_degen")

    state = await load_shop_state(conn, user_id)
    item = find_shop_item(state, "title_oracle")
    assert item is not None
    _embed, view = build_shop_card(state, item)
    equip = _buttons(view)[0]
    assert equip.label == "Equip" and not equip.disabled
    assert str(equip.custom_id) == "shop:equip:title_oracle"


async def test_equipped_title_disables_equip(conn: AsyncConnection) -> None:
    """A just-bought title is auto-equipped -- its card reads 'Equipped'."""
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await buy_item(conn, user_id, "title_oracle")

    state = await load_shop_state(conn, user_id)
    item = find_shop_item(state, "title_oracle")
    assert item is not None
    _embed, view = build_shop_card(state, item)
    equip = _buttons(view)[0]
    assert equip.label == "Equipped" and equip.disabled


async def test_max_margin_tier_disables_buy(conn: AsyncConnection) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 10_000_000)
    for _ in range(len(MARGIN_TIER_PRICES_MINOR)):
        await buy_item(conn, user_id, "margin_tier")

    state = await load_shop_state(conn, user_id)
    item = find_shop_item(state, "margin_tier")
    assert item is not None
    _embed, view = build_shop_card(state, item)
    buy = _buttons(view)[0]
    assert buy.label == "Max tier" and buy.disabled


async def test_expired_analyst_tool_shows_renew(conn: AsyncConnection) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await buy_item(conn, user_id, "analyst_tools")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE entitlements SET expires_at = now() - interval '1 day' "
            "WHERE user_id = %s AND item_key = 'analyst_tools'",
            (user_id,),
        )

    state = await load_shop_state(conn, user_id)
    item = find_shop_item(state, "analyst_tools")
    assert item is not None
    embed, view = build_shop_card(state, item)
    buy = _buttons(view)[0]
    assert buy.label == "Renew" and not buy.disabled
    assert "Expired" in str(embed.description)


# --------------------------------------------------------------- handler


async def test_buy_dispatches_with_interaction_id_and_edits(
    conn: AsyncConnection,
) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    account_id = await get_user_account_id(conn, user_id)
    before = await get_balance(conn, account_id)

    it = _FakeInteraction("shop:buy:theme_sunrise", user_id=user_id, interaction_id=777)
    await respond_shop_action(conn, cast(discord.Interaction, it), "buy", "theme_sunrise")

    assert await owns_item(conn, user_id, "theme_sunrise")
    assert len(it.response.edited) == 1
    embed = it.response.edited[0][1]["embed"]
    assert isinstance(embed, discord.Embed)
    assert "Purchased" in str(embed.description)
    assert it.response.sent == []
    # Price came out of the account.
    assert (await get_balance(conn, account_id)) < before
    # The interaction id is the idempotency key.
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM idempotency_keys WHERE interaction_id = '777'")
        assert await cur.fetchone() is not None


async def test_buy_insufficient_funds_is_ephemeral_no_edit(
    conn: AsyncConnection,
) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)  # $100 grant -- margin tier 1 costs $500
    it = _FakeInteraction("shop:buy:margin_tier", user_id=user_id)
    await respond_shop_action(conn, cast(discord.Interaction, it), "buy", "margin_tier")
    assert it.response.edited == []
    assert len(it.response.sent) == 1
    assert it.response.sent[0][1].get("ephemeral") is True
    assert not await owns_item(conn, user_id, "margin_tier")


async def test_buy_duplicate_interaction_is_ephemeral(
    conn: AsyncConnection,
) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    first = _FakeInteraction("shop:buy:theme_sunrise", user_id=user_id, interaction_id=888)
    await respond_shop_action(conn, cast(discord.Interaction, first), "buy", "theme_sunrise")
    assert len(first.response.edited) == 1

    second = _FakeInteraction("shop:buy:theme_sunrise", user_id=user_id, interaction_id=888)
    await respond_shop_action(conn, cast(discord.Interaction, second), "buy", "theme_sunrise")
    assert second.response.edited == []
    assert len(second.response.sent) == 1
    assert second.response.sent[0][1].get("ephemeral") is True


async def test_buy_as_suspended_user_is_ephemeral(
    conn: AsyncConnection,
) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await disable_user(conn, user_id, "audit-flag-9", 1)

    it = _FakeInteraction("shop:buy:theme_sunrise", user_id=user_id)
    await respond_shop_action(conn, cast(discord.Interaction, it), "buy", "theme_sunrise")
    assert it.response.edited == []
    assert len(it.response.sent) == 1
    assert it.response.sent[0][1].get("ephemeral") is True
    assert "audit-flag-9" in str(it.response.sent[0][0][0])
    assert not await owns_item(conn, user_id, "theme_sunrise")


async def test_equip_click_updates_title_state(conn: AsyncConnection) -> None:
    """Buy two titles (the second auto-equips), then the equip click on
    the first must switch the slot back."""
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await buy_item(conn, user_id, "title_oracle")
    await buy_item(conn, user_id, "title_degen")

    it = _FakeInteraction("shop:equip:title_oracle", user_id=user_id)
    await respond_shop_action(conn, cast(discord.Interaction, it), "equip", "title_oracle")
    assert len(it.response.edited) == 1
    async with conn.cursor() as cur:
        await cur.execute("SELECT equipped_title FROM users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
    assert row and row[0] == "title_oracle"


async def test_equip_click_updates_theme_state(conn: AsyncConnection) -> None:
    user_id = _snowflake()
    await bootstrap_user(conn, user_id)
    await _give_cash(conn, user_id, 1_000_000)
    await buy_item(conn, user_id, "theme_sunrise")
    await buy_item(conn, user_id, "theme_midnight")

    it = _FakeInteraction("shop:equip:theme_sunrise", user_id=user_id)
    await respond_shop_action(conn, cast(discord.Interaction, it), "equip", "theme_sunrise")
    assert len(it.response.edited) == 1
    async with conn.cursor() as cur:
        await cur.execute("SELECT theme FROM chart_prefs WHERE user_id = %s", (user_id,))
        row = await cur.fetchone()
    assert row and row[0] == "theme_sunrise"


async def test_category_select_renders_items(conn: AsyncConnection) -> None:
    it = _FakeInteraction("shop:cat:-", values=["cosmetics"], user_id=_snowflake())
    await respond_shop_action(conn, cast(discord.Interaction, it), "cat", "-")
    assert len(it.response.edited) == 1
    view = it.response.edited[0][1]["view"]
    assert isinstance(view, discord.ui.View)
    # 6 themes + 6 titles = 12 cosmetics, all within the select cap.
    assert len(_select_options(view)) == 12


async def test_item_select_renders_card(conn: AsyncConnection) -> None:
    it = _FakeInteraction(
        "shop:pick:consumables",
        values=["quest_reroll"],
        user_id=_snowflake(),
    )
    await respond_shop_action(conn, cast(discord.Interaction, it), "pick", "consumables")
    assert len(it.response.edited) == 1
    embed = it.response.edited[0][1]["embed"]
    assert isinstance(embed, discord.Embed)
    assert "Quest reroll" in str(embed.title)


# ------------------------------------------------------ handler guards


async def test_handler_noops_when_already_responded() -> None:
    it = _FakeInteraction("shop:home:-", done=True)
    await handle_shop_component(cast(discord.Interaction, it))
    assert it.response.sent == []
    assert it.response.edited == []


async def test_handler_rejects_bad_cid_ephemerally() -> None:
    it = _FakeInteraction("shop:not-valid-here")
    await handle_shop_component(cast(discord.Interaction, it))
    assert len(it.response.sent) == 1
    assert it.response.sent[0][1].get("ephemeral") is True


async def test_handler_dedups_concurrent_dispatch() -> None:
    """View callback + on_interaction fire on the same interaction id:
    the second entry must return without responding."""
    first = _FakeInteraction("shop:home:-", interaction_id=4242)
    _INFLIGHT.add(4242)  # simulate the live callback owning the id
    try:
        await handle_shop_component(cast(discord.Interaction, first))
        assert first.response.sent == []
        assert first.response.edited == []
    finally:
        _INFLIGHT.discard(4242)


# ------------------------------------------------------------ daily deal


async def test_home_embed_shows_todays_deal(conn: AsyncConnection) -> None:
    state = await load_shop_state(conn, _snowflake())
    assert state.deal_key is not None  # shop.deal_enabled defaults to 1
    embed, _view = build_shop_home(state)
    deal_field = next(f for f in embed.fields if "deal" in f.name.lower())
    deal_item = find_shop_item(state, state.deal_key)
    assert deal_item is not None
    assert deal_item.name in deal_field.value
    assert "~~" in deal_field.value  # strikethrough list price


async def test_deal_item_card_shows_strikethrough_price(
    conn: AsyncConnection,
) -> None:
    state = await load_shop_state(conn, _snowflake())
    assert state.deal_key is not None
    item = find_shop_item(state, state.deal_key)
    assert item is not None and item.price_minor is not None
    embed, _view = build_shop_card(state, item)
    desc = embed.description or ""
    deal_price = round(item.price_minor * (1 - state.deal_pct))
    assert f"~~{format_money(item.price_minor)}~~" in desc
    assert format_money(deal_price) in desc
    assert "DEAL" in desc


async def test_no_deal_field_when_disabled(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = '0' WHERE key = 'shop.deal_enabled'"
        )
    state = await load_shop_state(conn, _snowflake())
    assert state.deal_key is None
    embed, _view = build_shop_home(state)
    assert all("deal" not in f.name.lower() for f in embed.fields)
