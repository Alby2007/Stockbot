"""Slash commands: registered onto a client's `app_commands.CommandTree`.

Kept deliberately thin -- all the actual logic lives in the service modules
(`accounts`, `claims`, `trading`, `ledger`); commands just parse Discord
input, call a service inside one DB connection, and format the result.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import discord
from discord import app_commands
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot import db
from stockbot.accounts.errors import UserDisabledError
from stockbot.accounts.service import (
    STARTING_GRANT,
    BootstrapResult,
    bootstrap_user,
    min_discord_age_days,
)
from stockbot.admin.service import (
    TUNABLE_CONFIG_KEYS,
    TUNABLE_PARAMS,
    add_instrument,
    admin_adjust,
    admin_cancel_order,
    delist_instrument,
    disable_user,
    enable_user,
    ledger_audit,
    recalc_balances,
    set_config,
    tune_instrument,
    user_info,
)
from stockbot.alerts.service import cancel_alert, create_alert, list_alerts
from stockbot.bot.autocomplete import (
    admin_order_autocomplete,
    alert_autocomplete,
    card_autocomplete,
    equipped_item_autocomplete,
    option_autocomplete,
    order_autocomplete,
    owned_card_autocomplete,
    pack_autocomplete,
    quest_reroll_autocomplete,
    sector_autocomplete,
    shop_item_autocomplete,
    short_autocomplete,
    their_card_autocomplete,
    ticker_autocomplete,
    tunable_param_autocomplete,
)
from stockbot.bot.chart_view import (
    MAX_SPAN,
    MAX_SPAN_PRO,
    TIMEFRAME_SPANS,
    build_chart_view,
    load_chart_prefs,
    save_chart_prefs,
)
from stockbot.bot.charts import render_candle_chart
from stockbot.bot.collection_view import (
    STAMP_LABEL,
    stamp_glyphs,
)
from stockbot.bot.collection_view import (
    build_page as build_collection_page,
)
from stockbot.bot.collection_view import (
    frame_glyph as _frame_glyph,
)
from stockbot.bot.format import format_money, format_pct, format_price
from stockbot.bot.leaderboard import (
    bind_leaderboard_channel,
    delete_board_message,
    fetch_binding,
    leaderboard_embed,
    post_leaderboard_board,
)
from stockbot.bot.shop_view import (
    build_shop_card,
    build_shop_home,
    find_shop_item,
    is_purchasable,
    load_shop_state,
)
from stockbot.bot.trade_view import render_trade, trade_view
from stockbot.claims.errors import (
    AccountTooYoungError,
    AlreadyClaimedTodayError,
    FirstClaimLockedError,
)
from stockbot.claims.service import claim_daily
from stockbot.collectibles.pull import FRAME_RANK
from stockbot.collectibles.service import (
    PullOutcome,
    assemble_card,
    collection_stats,
    collector_leaderboard,
    craft_card,
    find_card,
    list_collection,
    recipe_for,
    set_featured_card,
    upgrade_frame,
)
from stockbot.collectibles.service import (
    open_pack as open_card_pack,
)
from stockbot.collectibles.trades import (
    cancel as trades_cancel,
)
from stockbot.collectibles.trades import (
    create_offer,
    get_trade,
    list_trades,
)
from stockbot.compliance.wash_trade import scan_for_wash_trades
from stockbot.config import get_settings
from stockbot.feed import emit_feed
from stockbot.ipo import service as ipo_svc
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import get_balance, record_idempotency_key
from stockbot.margin.errors import MarginError
from stockbot.margin.service import (
    check_and_liquidate,
    compute_health,
    effective_borrow_bps_per_tick,
    list_liquidations,
    margin_config,
    margin_tier,
)
from stockbot.market.data import (
    InstrumentSnapshot,
    all_instrument_snapshots,
    book_depth,
    current_tick_index,
    get_instrument_snapshot,
    market_phase,
    markets_map,
)
from stockbot.market.engine import TICKS_PER_DAY, ticks_until_close, ticks_until_open
from stockbot.npc.agents import ACTIONS as NPC_ARCHETYPES
from stockbot.npc.service import (
    agent_report,
    set_agent_enabled,
    spawn_agent,
)
from stockbot.options.service import (
    EXPIRY_CHOICES_DAYS,
    buy_option,
    list_open_options,
    option_chain,
    quote_option_premium,
    sell_option,
)
from stockbot.orders.service import (
    cancel_order,
    list_open_orders,
    place_oco,
    place_order,
)
from stockbot.quests.service import RerollError, list_quests, reroll_quest
from stockbot.seasons.errors import SandboxAlreadyOpenError, SeasonError
from stockbot.seasons.service import (
    close_season,
    create_season,
    current_tick,
    get_active_entry,
    get_latest_season,
    get_open_season,
    get_sandbox_entry,
    get_season,
    join_season,
    league_equity_minor,
    open_sandbox,
    reset_sandbox,
    standings,
)
from stockbot.shop.errors import NotOwnedError, ShopError
from stockbot.shop.service import (
    buy_item,
    equip_item,
    get_user_entitlements,
    owns_item,
    use_consumable,
)
from stockbot.shop.service import (
    unequip as unequip_item,
)
from stockbot.shorts.service import (
    cover_bounded_short,
    list_open_shorts,
    open_bounded_short,
)
from stockbot.status import service as status_svc
from stockbot.trading.errors import DuplicateInteractionError, TradingError
from stockbot.trading.service import (
    execute_trade,
    max_affordable_shares,
    quote_trade,
    shares_for_dollars,
    taker_fee_bps,
)

log = logging.getLogger("stockbot.bot.commands")


def guild_welcome_message() -> str:
    """Posted once per guild on join (N3.2). Points everyone at /start --
    accounts stay per-user and are still only created on first use."""
    return (
        "**StockBot is live.** One shared synthetic stock market — fake "
        "money, real market mechanics.\n"
        f"• `/start` — the 60-second tour (creates your account with a "
        f"{format_money(STARTING_GRANT)} grant)\n"
        "• `/market` — what's tradeable · `/chart <ticker>` — candles\n"
        "• `/buy` `/sell` `/order` — trade · `/portfolio` `/leaderboard` — track it\n"
        "Slash commands only — I never read your messages."
    )


_WHEEL_LABELS = {
    "cold": "❄️ Cold",
    "even": "⚪ Even",
    "warm": "🔥 Warm",
    "hot": "☄️ Hot",
    "jackpot": "💎 JACKPOT",
}


def _welcome_suffix(result: BootstrapResult | None) -> str:
    # League paths never bootstrap (season-scoped accounts) -> None.
    if result is None:
        return ""
    """Appended to a command's response. Fires the full welcome exactly
    once -- on the command that actually created the account (N3.1,
    `bootstrap_user`'s `created` flag is exactly-once) -- but repeats the
    grant-pending note on EVERY call while it applies (H1): a pending
    user's $0 balance needs the explanation every time, not just once.
    Explicitly calls out the claim-age gate: without it, a first-time
    user's day-one `/claim` rejection reads like a bug."""
    if result.grant_pending:
        note = (
            f"**Your starting grant is pending** — it unlocks once your "
            f"Discord account is {result.min_age_days} days old "
            f"(anti-abuse). Everything else works now."
        )
        if not result.created:
            return f"\n\n{note}"
        return (
            f"\n\n**Welcome to the market!** {note} Try `/market` to see "
            "what's tradeable, or `/start` for a full walkthrough. Your "
            "first `/claim` unlocks 24h after your account is created."
        )
    if not result.created:
        return ""
    return (
        f"\n\n**Welcome to the market!** You've been seeded with "
        f"{format_money(STARTING_GRANT)} to start. Try `/market` to see "
        "what's tradeable, or `/start` for a full walkthrough. Your first "
        "`/claim` unlocks 24h after your account is created."
    )


def _frame_glyph_for(card: Any) -> str:
    """Card-row glyph when no held frame applies: rarity for lore,
    medal for commemoratives, white circle for instruments."""
    if card.kind == "LORE" and card.rarity:
        return _frame_glyph(card.rarity)
    if card.kind == "COMMEMORATIVE":
        return "🏅"
    return _frame_glyph("STANDARD")


def _pull_embed(outcome: PullOutcome, index: int, total: int) -> discord.Embed:
    """One staged-reveal card: frame glyph + name + mint serial + print
    stamps + outcome, duplicates carrying their shard award (C6 -- a
    visible 'rare' dupe must never read as a bare loss)."""
    dup = f" — duplicate, +{outcome.shards} shards" if outcome.outcome == "DUPLICATE" else ""
    upgrade = " — upgraded frame!" if outcome.outcome == "UPGRADE" else ""
    stamp_names = ", ".join(
        STAMP_LABEL.get(s, s) for s in outcome.stamps
    )
    embed = discord.Embed(
        title="🧩 Bonus part" if outcome.tier == "PART"
              else f"{_frame_glyph(outcome.tier)} {outcome.tier}",
        description=f"**{outcome.name}** #{outcome.serial}{dup}{upgrade}"
        + (f"\n*{stamp_names}*" if stamp_names else ""),
    )
    embed.set_footer(text=f"Card {index}/{total}")
    return embed


async def _held_shares(
    conn: AsyncConnection, user_id: int, ticker: str, season_id: int | None
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.quantity
            FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.user_id = %s AND i.ticker = %s
              AND p.season_id IS NOT DISTINCT FROM %s
            """,
            (user_id, ticker.upper(), season_id),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _resolve_quantity(
    conn: AsyncConnection,
    *,
    user_id: int,
    ticker: str,
    side: str,
    account_id: int,
    quantity: int | None,
    dollars: float | None,
    all_in: bool,
    season_id: int | None,
    allow_short: bool = False,
) -> tuple[int, bool]:
    """Resolve the user's sizing input to a share count.

    Returns (quantity, capped) -- capped means a dollar-sized SELL ran
    into the position's size (the whole position went out the door).
    Raises TradingError with a user-readable message on bad input."""
    ticker = ticker.upper()
    provided = (quantity is not None) + (dollars is not None) + bool(all_in)
    if provided != 1:
        raise TradingError("give exactly one of `quantity`, `dollars`, or `all_in`")
    if quantity is not None:
        return quantity, False
    if all_in:
        if side == "SELL":
            held = await _held_shares(conn, user_id, ticker, season_id)
            if held <= 0:
                raise TradingError(f"you don't hold any {ticker}")
            return held, True
        cash = await get_balance(conn, account_id)
        qty = await max_affordable_shares(conn, user_id=user_id, ticker=ticker, cash_minor=cash)
        if qty <= 0:
            raise TradingError(
                f"your {format_money(cash)} can't cover one {ticker} share "
                f"— check the mark with `/stock {ticker}`"
            )
        return qty, False
    assert dollars is not None
    dollars_minor = int(Decimal(str(dollars)) * 100)
    if side == "SELL":
        held = await _held_shares(conn, user_id, ticker, season_id)
        if held <= 0 and not allow_short:
            raise TradingError(f"you don't hold any {ticker}")
        qty = await shares_for_dollars(
            conn,
            user_id=user_id,
            ticker=ticker,
            side="SELL",
            dollars_minor=dollars_minor,
        )
        if qty <= 0:
            quote = await quote_trade(conn, user_id=user_id, ticker=ticker, side="SELL", quantity=1)
            raise TradingError(
                f"1 {ticker} share nets ~{format_money(quote.unit_minor)} — too small an amount"
            )
        if allow_short:
            # Opted in to margin shorting: a dollar-sized sell means the
            # whole amount, not just the held part.
            return qty, False
        return min(qty, held), qty > held
    qty = await shares_for_dollars(
        conn,
        user_id=user_id,
        ticker=ticker,
        side="BUY",
        dollars_minor=dollars_minor,
    )
    if qty <= 0:
        quote = await quote_trade(conn, user_id=user_id, ticker=ticker, side="BUY", quantity=1)
        raise TradingError(
            f"1 {ticker} share costs ~{format_money(quote.unit_minor)} all-in — bump the amount"
        )
    return qty, False


def register_commands(tree: app_commands.CommandTree) -> None:
    @tree.command(
        name="start",
        description="New here? Create your account and get the 60-second tour",
    )
    async def start(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            min_age = await min_discord_age_days(conn)
        embed = discord.Embed(
            title="Welcome to the market" if bootstrap.created else "Quick start",
            description=(
                "One shared synthetic stock market — everyone trades the "
                "same instruments, priced by a factor model that reacts to "
                "order flow. Fake money, real mechanics."
            ),
        )
        if bootstrap.grant_pending:
            account_line = (
                f"Your **{format_money(STARTING_GRANT)}** starting grant "
                f"unlocks once your Discord account is "
                f"{bootstrap.min_age_days} days old (anti-abuse) — "
                "everything else works now."
            )
        else:
            account_line = (
                f"{'You were just seeded with' if bootstrap.created else 'You started with'} "
                f"**{format_money(STARTING_GRANT)}**. `/balance` shows cash, "
                "`/portfolio` shows holdings, `/profile` your track record."
            )
        embed.add_field(name="Your account", value=account_line, inline=False)
        embed.add_field(
            name="Find trades",
            value=(
                "`/market` lists everything tradeable · `/stock <ticker>` "
                "quotes one name · `/chart <ticker>` draws candles · "
                "`/movers` shows what's moving."
            ),
            inline=False,
        )
        embed.add_field(
            name="Trade",
            value=(
                "`/buy <ticker>` / `/sell <ticker>` — size with `quantity:` "
                "or `dollars:`. Resting orders: `/order buy|sell` with a "
                "`limit` or `stop`."
            ),
            inline=False,
        )
        embed.add_field(
            name="Daily claim",
            value=(
                "`/claim` pays a daily stipend — your first one unlocks "
                "24h after account creation (anti-farm), and Discord "
                f"accounts under {min_age} days can't claim at all."
            ),
            inline=False,
        )
        embed.add_field(
            name="Shorts & margin",
            value=(
                "`/short` opens a bounded short — fixed collateral, "
                "auto-knockout above entry. True margin shorts unlock via "
                "`margin_tier` in `/shop`; `/margin` tracks account health."
            ),
            inline=False,
        )
        embed.add_field(
            name="Compete",
            value=(
                "`/league` seasons are opt-in equal-stake competitions · "
                "`/leaderboard` ranks everyone by net worth · `/quests list` "
                "pays faucet rewards for daily/weekly tasks."
            ),
            inline=False,
        )
        embed.set_footer(text="/notify toggles DM alerts · no real money, ever.")
        await interaction.response.send_message(embed=embed)

    @tree.command(name="balance", description="Check your cash balance")
    async def balance(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            cash = await get_balance(conn, bootstrap.account_id)
            fee_bps = await taker_fee_bps(conn, interaction.user.id)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT total_traded_minor FROM users WHERE id = %s",
                    (interaction.user.id,),
                )
                row = await cur.fetchone()
            total_traded = int(row[0]) if row else 0
        await interaction.response.send_message(
            f"Cash balance: **{format_money(cash)}**\n"
            f"Lifetime volume {format_money(total_traded)} — "
            f"taker fee {fee_bps}bps (makers earn a rebate on crosses)" + _welcome_suffix(bootstrap)
        )

    @tree.command(
        name="notify",
        description="Toggle DM notifications for liquidations, knockouts, fills, league results",
    )
    @app_commands.describe(enabled="On or off")
    async def notify(interaction: discord.Interaction, enabled: bool) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET dm_notifications = %s WHERE id = %s",
                    (enabled, interaction.user.id),
                )
        await interaction.response.send_message(
            (
                "DM notifications are now **on**."
                if enabled
                else "DM notifications are now **off**. Events still happen — "
                "you just won't hear about them until you turn this back on."
            )
            + _welcome_suffix(bootstrap),
            ephemeral=True,
        )

    @tree.command(name="claim", description="Claim your daily faucet grant")
    async def claim(interaction: discord.Interaction) -> None:
        user = interaction.user
        # Both age gates live in claim_daily now (H1 defense-in-depth);
        # the command just renders them. Bootstrapping first is fine --
        # an under-age user gets a pending-grant account, not a rejection.
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, user.id)
            try:
                result = await claim_daily(conn, user.id)
            except AccountTooYoungError:
                await interaction.response.send_message(
                    f"Your Discord account needs to be at least "
                    f"{bootstrap.min_age_days} days old to claim." + _welcome_suffix(bootstrap),
                    ephemeral=True,
                )
                return
            except FirstClaimLockedError as exc:
                # Say WHEN it unlocks -- a bare "wait 24h" reads as a bug
                # on a brand-new user's very first command (H5).
                await interaction.response.send_message(
                    f"Your first claim unlocks <t:{int(exc.unlock_at.timestamp())}:R>."
                    + _welcome_suffix(bootstrap),
                    ephemeral=True,
                )
                return
            except AlreadyClaimedTodayError:
                await interaction.response.send_message(
                    "You already claimed today. Come back tomorrow!" + _welcome_suffix(bootstrap),
                    ephemeral=True,
                )
                return

        shield_note = " 🛡 Streak shield consumed — streak saved." if result.shield_used else ""
        if result.roll is None:
            # claim.wheel_enabled=0 -- flat formula, same message as always.
            await interaction.response.send_message(
                f"Claimed **{format_money(result.amount_minor)}**! "
                f"Current streak: **{result.streak}** day(s)."
                + shield_note
                + _welcome_suffix(bootstrap)
            )
            return

        # The draw is already committed -- this edit is presentation, not
        # the roll itself, so a client retry can't re-roll the payout.
        await interaction.response.send_message("🎡 Spinning the daily wheel…")
        await asyncio.sleep(0.8)
        reveal = (
            f"🎡 The wheel lands on **{_WHEEL_LABELS[result.roll.segment]}** — "
            f"**{format_money(result.roll.roll_minor)}**"
        )
        if result.streak > 1:
            reveal += f" × **{result.amount_minor / result.roll.roll_minor:.2f}** streak"
        reveal += (
            f"\nClaimed **{format_money(result.amount_minor)}**! "
            f"Current streak: **{result.streak}** day(s)."
            + shield_note
            + _welcome_suffix(bootstrap)
        )
        await interaction.edit_original_response(content=reveal)

    @tree.command(name="market", description="List all tradeable instruments")
    async def market(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            snapshots = await all_instrument_snapshots(conn)
            venues = await markets_map(conn)
            cur_tick = await current_tick_index(conn)

        # One status line + one section per venue, in market-id order.
        status_lines: list[str] = []
        for m in venues.values():
            label = f"**{m['code']}** {m['name']}"
            if cur_tick is not None:
                open_t = int(m["open_ticks"])
                closed_t = int(m["closed_ticks"])
                off = int(m["offset_ticks"])
                if market_phase(m, cur_tick) == "OPEN":
                    until = ticks_until_close(cur_tick, open_t, closed_t, off)
                    status_lines.append(f"{label}: OPEN — closes in ~{until} min")
                else:
                    until = ticks_until_open(cur_tick, open_t, closed_t, off)
                    status_lines.append(f"{label}: CLOSED — reopens in ~{until} min")
            else:
                status_lines.append(f"{label}: not yet open")

        sections: list[str] = []
        for mid in venues:
            rows = sorted(
                (s for s in snapshots if s.market_id == mid),
                key=lambda s: (s.sector_key, s.ticker),
            )
            if not rows:
                continue
            lines = [f"── {venues[mid]['code']} ──", "ticker  sector           price       24h"]
            for s in rows:
                marker = " (halted)" if s.is_halted else ""
                change = format_pct(s.day_change_pct) if s.day_change_pct is not None else "   n/a"
                lines.append(
                    f"{s.ticker:<6} {s.sector_key:<11} {format_price(s.quoted_price):>12} "
                    f"{change:>9}{marker}"
                )
            sections.append("\n".join(lines))

        embed = discord.Embed(
            title="Market",
            description="\n".join(status_lines) + "```\n" + "\n\n".join(sections) + "\n```",
        )
        await interaction.response.send_message(embed=embed)

    @tree.command(name="stock", description="Show detail for one instrument")
    @app_commands.describe(ticker="Instrument ticker, e.g. NORT")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def stock(interaction: discord.Interaction, ticker: str) -> None:
        async with db.connection() as conn:
            snapshot = await get_instrument_snapshot(conn, ticker)
            depth = await book_depth(conn, snapshot.id) if snapshot is not None else ([], [])
            quote10 = (
                await quote_trade(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side="BUY",
                    quantity=10,
                )
                if snapshot is not None
                else None
            )
            mcfg = await margin_config(conn)
            tick_now = await current_tick_index(conn)
            venues = await markets_map(conn)
            # Pro Terminal: deeper stats gated on the entitlement (the
            # analyst_tools ownership pattern from /calendar).
            pro = await owns_item(conn, interaction.user.id, "pro_terminal")
            pro_stats = (
                await status_svc.pro_terminal_stats(conn, snapshot.id, tick_now or 0)
                if snapshot is not None and pro
                else None
            )
        if snapshot is None:
            await interaction.response.send_message(
                f"No instrument found for `{ticker.upper()}`.", ephemeral=True
            )
            return

        embed = discord.Embed(title=f"{snapshot.ticker} \u2014 {snapshot.name}")
        embed.add_field(name="Sector", value=snapshot.sector_name)
        venue = venues.get(snapshot.market_id)
        if venue is not None:
            venue_str = f"{venue['name']} ({venue['code']})"
            if tick_now is not None:
                open_t = int(venue["open_ticks"])
                closed_t = int(venue["closed_ticks"])
                off = int(venue["offset_ticks"])
                if market_phase(venue, tick_now) == "OPEN":
                    until = ticks_until_close(tick_now, open_t, closed_t, off)
                    venue_str += f" — OPEN, closes ~{until}m"
                else:
                    until = ticks_until_open(tick_now, open_t, closed_t, off)
                    venue_str += f" — CLOSED, reopens ~{until}m"
            embed.add_field(name="Venue", value=venue_str)
        # "Mark (mid)" reads better once bid/ask are on screen: the mark
        # IS the mid, and saying so keeps users from reading it as a
        # tradeable price.
        embed.add_field(
            name=(
                "Mark (mid)" if snapshot.bid is not None and snapshot.ask is not None else "Mark"
            ),
            value=format_price(snapshot.quoted_price),
        )
        if snapshot.bid is not None and snapshot.ask is not None:
            embed.add_field(
                name="Bid / Ask",
                value=f"{format_price(snapshot.bid)} / {format_price(snapshot.ask)}",
            )
        if snapshot.day_change_pct is not None:
            embed.add_field(name="24h change", value=format_pct(snapshot.day_change_pct))
        embed.add_field(name="Impact", value=format_pct(float(snapshot.impact)))
        bids, asks = depth
        if bids or asks:
            lines = ["`  size    px    cum | cum     px   size `"]
            for i in range(max(len(bids), len(asks))):
                b = (
                    f"{bids[i].quantity:>6,} {float(bids[i].price):>7.2f}"
                    f"{bids[i].cumulative:>7,}" + ("~" if bids[i].synthetic else " ")
                    if i < len(bids)
                    else " " * 22
                )
                a = (
                    f"{asks[i].cumulative:>5,} {float(asks[i].price):>7.2f}"
                    f" {asks[i].quantity:<6,}" + ("~" if asks[i].synthetic else "")
                    if i < len(asks)
                    else ""
                )
                lines.append(f"`{b}|{a}`")
                if i == 0:
                    # Inside market: the first row on each side is the
                    # best resting level -- a divider separates it from
                    # the depth behind it like a terminal ladder.
                    lines.append("`   ─── inside ── | ── inside ───    `")
            embed.add_field(name="Book depth (~ = MM)", value="\n".join(lines), inline=False)
        if quote10 is not None:
            embed.add_field(
                name="Est. 10 shares",
                value=f"~{format_money(quote10.notional_minor + quote10.fee_minor)} all-in",
            )
        embed.add_field(name="24h volume", value=f"{snapshot.day_volume:,} shares")
        si_pct = float(snapshot.short_interest_pct)
        si_shares = si_pct * snapshot.float_shares
        si_line1 = f"{si_pct * 100:.1f}% of float"
        if float(snapshot.adv) > 0:
            si_line1 += f" · {si_shares / float(snapshot.adv):.1f}d to cover"
        borrow_pct_day = (
            float(effective_borrow_bps_per_tick(snapshot.short_interest_pct, mcfg))
            * TICKS_PER_DAY
            / 100
        )
        si_line2 = f"borrow ~{borrow_pct_day:.2f}%/day"
        if (
            snapshot.shortable_after_tick is not None
            and (tick_now or 0) < snapshot.shortable_after_tick
        ):
            si_line2 += f" · no borrow until ~tick {snapshot.shortable_after_tick}"
        else:
            capacity = max(
                0.0,
                float(mcfg["margin.max_short_interest_pct"]) * snapshot.float_shares - si_shares,
            )
            si_line2 += (
                f" · {capacity:,.0f} sh capacity" if capacity > 0 else " · borrow closed (at cap)"
            )
        embed.add_field(name="Short interest", value=si_line1 + "\n" + si_line2)
        if pro_stats is not None:
            if pro_stats.week_high is not None and pro_stats.week_low is not None:
                embed.add_field(
                    name="7d range Ⓟ",
                    value=(
                        f"{format_price(pro_stats.week_low)} – {format_price(pro_stats.week_high)}"
                    ),
                )
            if pro_stats.realized_vol is not None:
                embed.add_field(
                    name="Realized vol Ⓟ",
                    value=f"{pro_stats.realized_vol * 100:.2f}%/day",
                )
            embed.add_field(
                name="24h flow Ⓟ",
                value=(
                    f"buy {format_money(pro_stats.buy_notional_24h)} · "
                    f"sell {format_money(pro_stats.sell_notional_24h)}"
                ),
            )
            embed.add_field(name="ADV Ⓟ", value=f"{pro_stats.adv_shares:,.0f} shares/day")
            if pro_stats.next_event_in is not None:
                embed.add_field(
                    name="Next event Ⓟ",
                    value=(f"in ~{pro_stats.next_event_in} ticks (~{pro_stats.next_event_in}min)"),
                )
        if snapshot.is_halted:
            embed.add_field(name="Status", value="Halted (circuit breaker)")
        elif snapshot.event_halted:
            embed.add_field(name="Status", value="Halted pending earnings (T1)")
        await interaction.response.send_message(embed=embed)

    @tree.command(name="chart", description="Show an interactive price chart")
    @app_commands.describe(
        ticker="Instrument ticker, e.g. NORT",
        timeframe="Window (default: your saved view, else 4h)",
        axis="X-axis labels (default: your saved view, else real time)",
        mine="Private chart with your entry + knockout lines drawn (ephemeral)",
    )
    @app_commands.choices(
        timeframe=[
            app_commands.Choice(name="1 hour", value="1h"),
            app_commands.Choice(name="4 hours", value="4h"),
            app_commands.Choice(name="1 day", value="1d"),
            app_commands.Choice(name="1 week", value="1w"),
            app_commands.Choice(name="2 weeks (Pro)", value="2w"),
            app_commands.Choice(name="1 month (Pro)", value="1M"),
        ],
        axis=[
            app_commands.Choice(name="Time (UTC)", value="time"),
            app_commands.Choice(name="Ticks", value="ticks"),
        ],
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    # H4: the ~100ms matplotlib render is the one command worth a real
    # cooldown -- everything else is sub-10ms SQL.
    @app_commands.checks.cooldown(1, 10.0)  # default bucket: per user
    async def chart(
        interaction: discord.Interaction,
        ticker: str,
        timeframe: str | None = None,
        axis: str | None = None,
        mine: bool = False,
    ) -> None:
        # Defer up front: the render is slow enough to risk blowing the 3s
        # response window on a cold pool. mine => ephemeral, so position
        # marks are never visible to anyone else.
        await interaction.response.defer(ephemeral=mine)
        async with db.connection() as conn:
            snapshot = await get_instrument_snapshot(conn, ticker)
            if snapshot is None:
                await interaction.followup.send(
                    f"No instrument found for `{ticker.upper()}`.", ephemeral=True
                )
                return
            # Explicit args win and become the new saved view; absent
            # args fall back to saved prefs, then system defaults. Pro
            # Terminal lifts the saved/requested span bound to 1M.
            pro = await owns_item(conn, interaction.user.id, "pro_terminal")
            prefs = await load_chart_prefs(
                conn,
                interaction.user.id,
                max_span=MAX_SPAN_PRO if pro else MAX_SPAN,
            )
            if timeframe is None:
                span = prefs[0] if prefs else 240
            else:
                span = TIMEFRAME_SPANS.get(timeframe, 240)
            if span > MAX_SPAN and not pro:
                await interaction.followup.send(
                    "The 2w and 1M spans need **Pro Terminal** — `/shop item:pro_terminal`.",
                    ephemeral=True,
                )
                return
            if axis is None:
                resolved_axis = prefs[1] if prefs else "time"
            else:
                resolved_axis = axis if axis in ("time", "ticks") else "time"
            # Equipped theme renders only while the entitlement is live.
            theme = prefs[2] if prefs else None
            if theme is not None and not await owns_item(conn, interaction.user.id, theme):
                theme = None
            if timeframe is not None or axis is not None:
                await save_chart_prefs(conn, interaction.user.id, span, resolved_axis)
            result = await render_candle_chart(
                conn,
                snapshot.id,
                snapshot.ticker,
                span=span,
                axis=resolved_axis,
                theme=theme,
                viewer_id=interaction.user.id if mine else None,
            )

        if result is None:
            await interaction.followup.send("No price history yet.", ephemeral=True)
            return
        buf, end, end_ts = result

        filename = f"{snapshot.ticker}.png"
        file = discord.File(buf, filename=filename)
        embed = discord.Embed(title=f"{snapshot.ticker} \u2014 {snapshot.name}")
        embed.set_image(url=f"attachment://{filename}")
        if resolved_axis == "time" and end_ts is not None:
            ending = end_ts.astimezone(UTC).strftime("%b %d %H:%M UTC")
        else:
            ending = str(end)
        embed.set_footer(text=f"{span} open ticks ending {ending} · pan/zoom buttons below")
        await interaction.followup.send(
            embed=embed,
            file=file,
            view=build_chart_view(snapshot.id, end, span, resolved_axis, theme, mine=mine),
        )

    @tree.command(name="movers", description="Show today's biggest gainers and losers")
    async def movers(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            snapshots = await all_instrument_snapshots(conn)
        ranked = sorted(
            (s for s in snapshots if s.day_change_pct is not None),
            key=lambda s: s.day_change_pct,  # type: ignore[arg-type,return-value]
        )
        if not ranked:
            await interaction.response.send_message("Not enough price history yet.", ephemeral=True)
            return

        def _lines(items: list[InstrumentSnapshot]) -> str:
            return "ticker  v    price       24h\n" + "\n".join(
                f"{s.ticker:<6} {s.market_code:<3} {format_price(s.quoted_price):>10} "
                f"{format_pct(s.day_change_pct):>9}"  # type: ignore[arg-type]
                for s in items
            )

        embed = discord.Embed(title="Movers (24h)")
        embed.add_field(
            name="Top gainers", value=f"```\n{_lines(ranked[-5:][::-1])}\n```", inline=False
        )
        embed.add_field(name="Top losers", value=f"```\n{_lines(ranked[:5])}\n```", inline=False)
        await interaction.response.send_message(embed=embed)

    @tree.command(name="sectors", description="Show average performance by sector")
    async def sectors(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            snapshots = await all_instrument_snapshots(conn)

        # Venue x sector: shared sector factors mean the same key can
        # appear on multiple venues with different constituents.
        by_sector: dict[tuple[str, str], list[InstrumentSnapshot]] = {}
        for s in snapshots:
            by_sector.setdefault((s.market_code, s.sector_key), []).append(s)

        lines = []
        for (market_code, sector_key), members in sorted(by_sector.items()):
            changes = [s.day_change_pct for s in members if s.day_change_pct is not None]
            avg_change = sum(changes) / len(changes) if changes else None
            change_str = format_pct(avg_change) if avg_change is not None else "   n/a"
            venue = f"{market_code:<4}" if len({s.market_code for s in snapshots}) > 1 else ""
            lines.append(f"{venue}{sector_key:<11} {members[0].sector_name:<16} {change_str:>9}")

        embed = discord.Embed(title="Sectors (avg 24h change)")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @tree.command(name="calendar", description="Show upcoming earnings dates")
    async def calendar(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            current_tick = await current_tick_index(conn) or 0
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, e.resolve_tick, e.estimate, m.code
                    FROM events e
                    JOIN instruments i ON i.id = e.instrument_id
                    JOIN markets m ON m.id = i.market_id
                    WHERE e.kind = 'EARNINGS' AND NOT e.resolved
                    ORDER BY e.resolve_tick
                    LIMIT 20
                    """
                )
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No earnings scheduled yet -- check back after the market has ticked a bit.",
                ephemeral=True,
            )
            return

        multi_venue = len({r[3] for r in rows}) > 1
        lines = [
            f"{ticker + ('·' + code if multi_venue else ''):<9} in "
            f"{(resolve_tick - current_tick) / TICKS_PER_DAY:>5.1f} day(s)"
            + (f"   street {format_pct(float(estimate))}" if estimate is not None else "")
            for ticker, resolve_tick, estimate, code in rows
        ]
        embed = discord.Embed(title="Earnings calendar")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @tree.command(name="news", description="Show pending news hints and recent resolutions")
    async def news(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            current_tick = await current_tick_index(conn) or 0
            # analyst_tools holders get a noisy size read on pending news
            # (the magnitude exists pre-resolution but is hidden otherwise).
            entitlements = await get_user_entitlements(conn, interaction.user.id)
            has_desk = any(
                e["item_key"] == "analyst_tools" and e["quantity"] > 0 for e in entitlements
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, e.headline, e.resolve_tick, e.magnitude
                    FROM events e
                    JOIN instruments i ON i.id = e.instrument_id
                    WHERE e.kind = 'NEWS' AND NOT e.resolved AND e.scheduled_tick <= %s
                    ORDER BY e.resolve_tick
                    LIMIT 10
                    """,
                    (current_tick,),
                )
                pending = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT i.ticker, e.headline, e.magnitude, e.fizzled
                    FROM events e
                    JOIN instruments i ON i.id = e.instrument_id
                    WHERE e.kind = 'NEWS' AND e.resolved AND e.resolve_tick > %s
                    ORDER BY e.resolve_tick DESC
                    LIMIT 5
                    """,
                    (current_tick - TICKS_PER_DAY,),
                )
                resolved = await cur.fetchall()

        def _desk_read(magnitude: float | None) -> str:
            """analyst_tools size hint: bucketed |magnitude| -- a fizzle
            reads 'minor', indistinguishable from a small real move."""
            if magnitude is None:
                return ""
            m = abs(float(magnitude))
            bucket = "minor" if m < 0.01 else ("sizable" if m < 0.03 else "major")
            return f" · desk: {bucket}"

        embed = discord.Embed(title="News")
        if pending:
            lines = [
                f"[{ticker}] {headline} (effect lands in "
                f"{(resolve_tick - current_tick) / 60:.1f}h)"
                + (_desk_read(float(magnitude)) if has_desk else "")
                for ticker, headline, resolve_tick, magnitude in pending
            ]
            embed.add_field(name="Pending", value="\n".join(lines)[:1024], inline=False)
        if resolved:
            lines = [
                f"[{ticker}] {headline} \u2014 landed {format_pct(float(magnitude))}"
                + (" (rumor died)" if fizzled else "")
                for ticker, headline, magnitude, fizzled in resolved
            ]
            embed.add_field(name="Recently resolved", value="\n".join(lines)[:1024], inline=False)
        if not pending and not resolved:
            embed.description = "No news right now."
        await interaction.response.send_message(embed=embed)

    @tree.command(name="portfolio", description="Show your positions and net worth")
    @app_commands.describe(
        league="Show your league portfolio instead of your main one",
        sandbox="Show your sandbox portfolio instead of your main one",
    )
    async def portfolio(
        interaction: discord.Interaction, league: bool = False, sandbox: bool = False
    ) -> None:
        async with db.connection() as conn:
            season_id: int | None = None
            bootstrap: BootstrapResult | None = None
            scope = "sandbox" if sandbox else "league" if league else ""
            if sandbox or league:
                entry = await (
                    get_sandbox_entry(conn, interaction.user.id)
                    if sandbox
                    else get_active_entry(conn, interaction.user.id)
                )
                if entry is None:
                    await interaction.response.send_message(
                        "You don't have a sandbox running. `/sandbox open` first."
                        if sandbox
                        else "You're not entered in an active season. `/league join` first.",
                        ephemeral=True,
                    )
                    return
                season_id, account_id = entry
            else:
                bootstrap = await bootstrap_user(conn, interaction.user.id)
                account_id = bootstrap.account_id
            cash = await get_balance(conn, account_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, p.quantity, p.avg_cost, i.quoted_price,
                           p.borrow_fees_accrued, p.dividends_accrued
                    FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s AND p.quantity <> 0
                      AND p.season_id IS NOT DISTINCT FROM %s
                    ORDER BY i.ticker
                    """,
                    (interaction.user.id, season_id),
                )
                positions = await cur.fetchall()

        lines = [f"Cash: {format_money(cash)}"]
        holdings_value = Decimal(0)
        accrued_minor = Decimal(0)
        for ticker, quantity, avg_cost, quoted_price, fees_acc, divs_acc in positions:
            market_value = Decimal(quantity) * Decimal(quoted_price)
            holdings_value += market_value
            accrued_minor += Decimal(fees_acc) + Decimal(divs_acc)
            is_short = quantity < 0
            unrealized_pct = (
                (
                    1 - float(quoted_price) / float(avg_cost)
                    if is_short
                    else float(quoted_price) / float(avg_cost) - 1
                )
                if avg_cost
                else 0.0
            )
            tag = "SHORT " if is_short else ""
            lines.append(
                f"{tag}{ticker:<6} {quantity:>6} @ {format_price(avg_cost):>10}  "
                f"now {format_price(quoted_price):>10}  {format_pct(unrealized_pct):>8}"
            )
        # Net worth nets out accrued borrow fees / dividend obligations,
        # same as the margin health and league scoring paths.
        net_worth_minor = int(cash + holdings_value * 100 - accrued_minor)
        lines.append(f"\nNet worth: {format_money(net_worth_minor)}")

        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s {scope + ' ' if scope else ''}portfolio"
        )
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        if not positions:
            embed.set_footer(
                text=(
                    "No sandbox positions — /sandbox status shows the account."
                    if sandbox
                    else "No league positions — /league standings shows the board."
                    if league
                    else "No positions yet — /market shows what's tradeable."
                )
            )
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(name="leaderboard", description="Top net worth across the whole server economy")
    async def leaderboard_cmd(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            rows = await status_svc.leaderboard(conn)
            flair = await status_svc.equipped_flair_map(conn, [r.user_id for r in rows[:15]])
        if not rows:
            await interaction.response.send_message("No accounts yet.", ephemeral=True)
            return
        caller = next((r for r in rows if r.user_id == interaction.user.id), None)
        embed = leaderboard_embed(rows, caller=caller, flair=flair)
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(
        name="leaderboard-setup",
        description="Pin a live net-worth leaderboard board in this channel",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def leaderboard_setup(interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if interaction.guild_id is None or not isinstance(
            channel, (discord.TextChannel, discord.Thread)
        ):
            await interaction.response.send_message(
                "Run this in the channel that should host the board.",
                ephemeral=True,
            )
            return
        async with db.connection() as conn, conn.transaction():
            old = await fetch_binding(conn, interaction.guild_id)
            await bind_leaderboard_channel(conn, interaction.guild_id, channel.id)
            message = await post_leaderboard_board(conn, interaction.guild_id, channel)
        if old is not None and old.message_id is not None:
            await delete_board_message(interaction.client, old.channel_id, old.message_id)
        await interaction.response.send_message(
            f"Leaderboard board live in {channel.mention} — it refreshes "
            "every minute. "
            f"[Jump to it]({message.jump_url})",
            ephemeral=True,
        )

    @tree.command(
        name="feed-setup",
        description="Post the market-drama tape (liquidations, whales, news) in this channel",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def feed_setup(interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if interaction.guild_id is None or not isinstance(
            channel, (discord.TextChannel, discord.Thread)
        ):
            await interaction.response.send_message(
                "Run this in the channel that should host the tape.",
                ephemeral=True,
            )
            return
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                # One feed per channel AND one per guild: the UNIQUE
                # channel_id plus the guild PK make a rebind an upsert --
                # pending items follow the binding via ON UPDATE CASCADE.
                await cur.execute(
                    """
                    INSERT INTO feed_channels (guild_id, channel_id)
                    VALUES (%s, %s)
                    ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id
                    """,
                    (interaction.guild_id, channel.id),
                )
        await interaction.response.send_message(
            f"📡 The market tape will post in {channel.mention} — "
            "liquidations, whale prints, knockouts, jackpots, IPOs, season "
            "results, and landed news, batched into one digest.",
            ephemeral=True,
        )

    @tree.command(
        name="feed-remove",
        description="Stop the market-drama tape in this server",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def feed_remove(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Run this in a server channel.", ephemeral=True)
            return
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM feed_channels WHERE guild_id = %s",
                    (interaction.guild_id,),
                )
                removed = cur.rowcount
        await interaction.response.send_message(
            "Tape unbound — no more posts." if removed else "No tape was bound here.",
            ephemeral=True,
        )

    @tree.command(name="history", description="Show your recent trades")
    async def history(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            trades = await status_svc.recent_trades(conn, interaction.user.id)
        if not trades:
            await interaction.response.send_message(
                "No trades yet. Try `/market` to see what's tradeable."
                + _welcome_suffix(bootstrap),
                ephemeral=True,
            )
            return
        lines = [
            f"{t['side']:<4} {t['quantity']:>6} {t['ticker']:<6} @ "
            f"{format_price(t['fill_price']):>10}  fee {format_money(t['fee_minor']):>8}  "
            f"tick {t['tick_index']}"
            + (f" ({t['maker_taker'].lower()})" if t["maker_taker"] else "")
            for t in trades
        ]
        embed = discord.Embed(title="Recent trades")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    quest_group = app_commands.Group(name="quests", description="Daily and weekly quests")

    @quest_group.command(
        name="list", description="Today's and this week's quests and your progress"
    )
    async def quests_list(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            tick_index = await current_tick_index(conn)
            rows = await list_quests(conn, interaction.user.id, tick_index or 0)
        if not rows:
            await interaction.response.send_message(
                "No active quests — the board rotates at the next day "
                "boundary." + _welcome_suffix(bootstrap),
                ephemeral=True,
            )
            return

        def _quest_line(q: dict[str, Any]) -> str:
            done = "✓ " if q["completed"] else ""
            return (
                f"{done}**{q['name']}** — {q['progress']}/{q['target']} · "
                f"+{format_money(q['reward_minor'])}"
            )

        embed = discord.Embed(title="Quests")
        daily = [q for q in rows if q["period"] == "DAILY"]
        weekly = [q for q in rows if q["period"] == "WEEKLY"]
        if daily:
            hours = daily[0]["ticks_remaining"] / 60
            embed.add_field(
                name=f"Today — resets in ~{hours:.0f}h",
                value="\n".join(_quest_line(q) for q in daily),
                inline=False,
            )
        if weekly:
            embed.add_field(
                name="This week",
                value="\n".join(_quest_line(q) for q in weekly),
                inline=False,
            )
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @quest_group.command(
        name="reroll",
        description="Swap one active quest for a different one (uses a reroll token)",
    )
    @app_commands.describe(quest="The quest to swap out")
    @app_commands.autocomplete(quest=quest_reroll_autocomplete)
    async def quests_reroll(interaction: discord.Interaction, quest: str) -> None:
        try:
            instance_id = int(quest)
        except ValueError:
            instance_id = -1
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            try:
                new = await reroll_quest(conn, interaction.user.id, instance_id)
            except NotOwnedError:
                await interaction.response.send_message(
                    "You don't own a quest reroll — `/shop item:quest_reroll`.",
                    ephemeral=True,
                )
                return
            except RerollError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Rerolled into **{new['name']}** — target {new['target']}, "
            f"+{format_money(new['reward_minor'])}." + _welcome_suffix(bootstrap),
            ephemeral=True,
        )

    tree.add_command(quest_group)

    @tree.command(name="compare", description="Head-to-head comparison with another user")
    @app_commands.describe(user="Discord user to compare against")
    async def compare(interaction: discord.Interaction, user: discord.User) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            tick = await current_tick_index(conn) or 0
            me = await status_svc.compare_stats(conn, interaction.user.id, tick)
            them = await status_svc.compare_stats(conn, user.id, tick)

        def _fmt(stat: status_svc.CompareStats) -> str:
            change = format_pct(stat.day_change_pct) if stat.day_change_pct is not None else "n/a"
            record = (
                f"{stat.seasons_entered} season(s), best rank {stat.best_rank}"
                if stat.seasons_entered
                else "no league history"
            )
            return (
                f"Net worth: {format_money(stat.net_worth_minor)}\n"
                f"24h change: {change}\n"
                f"Positions: {stat.position_count}\n"
                f"Lifetime volume: {format_money(stat.lifetime_volume_minor)}\n"
                f"League: {record}"
            )

        embed = discord.Embed(title=f"{interaction.user.display_name} vs {user.display_name}")
        embed.add_field(name=interaction.user.display_name, value=_fmt(me))
        embed.add_field(name=user.display_name, value=_fmt(them))
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(name="profile", description="Your account summary: net worth, rank, trophies")
    async def profile(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            stats = await status_svc.profile_stats(conn, interaction.user.id)
        assert stats is not None
        embed = discord.Embed(title=f"{interaction.user.display_name}'s profile")
        if stats.title:
            embed.description = stats.title
        embed.add_field(name="Account age", value=f"{stats.account_age_days:.1f} day(s)")
        embed.add_field(name="Net worth", value=format_money(stats.net_worth_minor))
        embed.add_field(
            name="Rank",
            value=(f"{stats.rank} of {stats.total_users}" if stats.rank else "unranked"),
        )
        embed.add_field(name="Lifetime volume", value=format_money(stats.lifetime_volume_minor))
        embed.add_field(name="Quests", value=str(stats.quests_completed))
        if stats.featured_card:
            embed.add_field(name="Featured card", value=stats.featured_card)
        if stats.trophies:
            embed.add_field(
                name="Trophies & badges",
                value="\n".join(stats.trophies),
                inline=False,
            )
        if stats.active_season_equity_minor is not None:
            embed.add_field(
                name="Active season equity",
                value=format_money(stats.active_season_equity_minor),
            )
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(
        name="whois",
        description="Flex card: any player's net worth, rank, badges and trophies",
    )
    @app_commands.describe(user="Discord user to look up")
    async def whois(interaction: discord.Interaction, user: discord.User) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            stats = await status_svc.profile_stats(conn, user.id)
        if stats is None:
            await interaction.response.send_message(
                f"**{user.display_name}** hasn't joined StockBot — `/start` creates an account."
            )
            return
        embed = discord.Embed(title=user.display_name)
        if stats.title:
            # Equipped title sits under the name -- the flex reads first.
            embed.description = stats.title
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Net worth", value=format_money(stats.net_worth_minor))
        embed.add_field(
            name="Rank",
            value=(f"#{stats.rank} of {stats.total_users}" if stats.rank else "unranked"),
        )
        embed.add_field(
            name="Lifetime volume",
            value=format_money(stats.lifetime_volume_minor),
        )
        embed.add_field(name="Quests", value=str(stats.quests_completed))
        embed.add_field(name="StockBot age", value=f"{stats.account_age_days:.1f}d")
        if stats.active_season_equity_minor is not None:
            embed.add_field(
                name="Active season",
                value=format_money(stats.active_season_equity_minor),
            )
        if stats.trophies:
            embed.add_field(
                name="Badges & trophies",
                value="\n".join(stats.trophies),
                inline=False,
            )
        # Public by design -- the flex is the point.
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(
        name="equip",
        description="Equip an owned title or chart theme",
    )
    @app_commands.describe(item="Owned item key, e.g. title_oracle, theme_vapor")
    @app_commands.autocomplete(item=equipped_item_autocomplete)
    async def equip(interaction: discord.Interaction, item: str) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            try:
                slot = await equip_item(conn, interaction.user.id, item.lower())
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        label = "title" if slot == "title" else "chart theme"
        await interaction.response.send_message(
            f"Equipped **{item.lower()}** as your {label}." + _welcome_suffix(bootstrap),
            ephemeral=True,
        )

    @tree.command(name="unequip", description="Clear an equip slot")
    @app_commands.describe(slot="Which slot to clear")
    @app_commands.choices(
        slot=[
            app_commands.Choice(name="title", value="title"),
            app_commands.Choice(name="theme", value="theme"),
        ]
    )
    async def unequip(interaction: discord.Interaction, slot: str) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            try:
                cleared = await unequip_item(conn, interaction.user.id, slot)
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Cleared your {slot}." if cleared else f"No {slot} equipped.",
            ephemeral=True,
        )

    @tree.command(name="buy", description="Buy shares of an instrument")
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        dollars="Spend ~this many dollars instead of giving a share count",
        all_in="Spend your entire cash balance on this instrument",
        league="Trade from your season league stake instead of your main portfolio",
        sandbox="Trade from your sandbox stake instead of your main portfolio",
        slippage="Max slippage vs the mark, in percent (e.g. 2.5)",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def buy(
        interaction: discord.Interaction,
        ticker: str,
        # A bounded quantity keeps absurd inputs from overflowing BIGINT
        # notional_minor into an uncaught NumericValueOutOfRange.
        quantity: app_commands.Range[int, 1, 1_000_000_000] | None = None,
        dollars: app_commands.Range[float, 0.01, 1_000_000_000.0] | None = None,
        all_in: bool = False,
        league: bool = False,
        sandbox: bool = False,
        slippage: app_commands.Range[float, 0.01, 100.0] | None = None,
    ) -> None:
        await _do_trade(
            interaction, ticker, "BUY", quantity, dollars, all_in, league, sandbox, slippage
        )

    @tree.command(name="sell", description="Sell shares of an instrument")
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        dollars="Sell ~this many dollars' worth instead of giving a share count",
        all_in="Sell your entire position",
        league="Trade from your season league stake instead of your main portfolio",
        sandbox="Trade from your sandbox stake instead of your main portfolio",
        slippage="Max slippage vs the mark, in percent (e.g. 2.5)",
        short="Opt in to selling past your position (opens a margin short)",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def sell(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000] | None = None,
        dollars: app_commands.Range[float, 0.01, 1_000_000_000.0] | None = None,
        all_in: bool = False,
        league: bool = False,
        sandbox: bool = False,
        slippage: app_commands.Range[float, 0.01, 100.0] | None = None,
        short: bool = False,
    ) -> None:
        await _do_trade(
            interaction,
            ticker,
            "SELL",
            quantity,
            dollars,
            all_in,
            league,
            sandbox,
            slippage,
            short,
        )

    async def _do_trade(
        interaction: discord.Interaction,
        ticker: str,
        side: str,
        quantity: int | None,
        dollars: float | None,
        all_in: bool,
        league: bool,
        sandbox: bool,
        slippage: float | None,
        short: bool = False,
    ) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            account_id = bootstrap.account_id
            season_id: int | None = None
            if sandbox or league:
                entry = await (
                    get_sandbox_entry(conn, interaction.user.id)
                    if sandbox
                    else get_active_entry(conn, interaction.user.id)
                )
                if entry is None:
                    await interaction.response.send_message(
                        "You don't have a sandbox running. `/sandbox open` first."
                        if sandbox
                        else "You're not entered in an active season. `/league join` first.",
                        ephemeral=True,
                    )
                    return
                season_id, account_id = entry
            try:
                qty, capped = await _resolve_quantity(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,
                    account_id=account_id,
                    quantity=quantity,
                    dollars=dollars,
                    all_in=all_in,
                    season_id=season_id,
                    allow_short=short,
                )
            except TradingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            if side == "SELL" and not short:
                # N4.2: a sell that would push the position below zero is a
                # margin short in disguise -- refuse with a holdings-aware
                # message unless the user opted in with `short: True`.
                held = await _held_shares(conn, interaction.user.id, ticker, season_id)
                if qty > max(held, 0):
                    pos_note = (
                        f"You hold **{held:,}** {ticker.upper()} — "
                        if held > 0
                        else f"You're already short **{-held:,}** {ticker.upper()} — "
                    )
                    await interaction.response.send_message(
                        f"{pos_note}selling {qty:,} would open a margin "
                        "short. Re-run with `short: True` to confirm "
                        "(needs a margin tier from `/shop`).",
                        ephemeral=True,
                    )
                    return
            try:
                result = await execute_trade(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,  # type: ignore[arg-type]
                    quantity=qty,
                    interaction_id=str(interaction.id),
                    season_id=season_id,
                    max_slippage=(None if slippage is None else Decimal(str(slippage)) / 100),
                )
            except InsufficientFundsError:
                # Show the math: what the size needs vs. the balance. On a
                # SELL the shortfall is the fee leg, not the notional.
                try:
                    quote = await quote_trade(
                        conn,
                        user_id=interaction.user.id,
                        ticker=ticker,
                        side=side,  # type: ignore[arg-type]
                        quantity=qty,
                    )
                    balance = await get_balance(conn, account_id)
                    need = (
                        quote.notional_minor + quote.fee_minor if side == "BUY" else quote.fee_minor
                    )
                    msg = (
                        f"{qty:,} {ticker.upper()} needs "
                        f"~{format_money(need)} cash "
                        f"({'price+fee' if side == 'BUY' else 'fee'}) — "
                        f"you have {format_money(balance)}."
                    )
                except (TradingError, ValueError):
                    msg = "Insufficient funds for that trade."
                await interaction.response.send_message(msg, ephemeral=True)
                return
            except (TradingError, MarginError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            legs = await check_and_liquidate(conn, interaction.user.id, season_id)
            cash_after = await get_balance(conn, account_id)

        verb = "Bought" if side == "BUY" else "Sold"
        moved = (
            f"**{format_money(result.notional_minor + result.fee_minor)}** spent"
            if side == "BUY"
            else f"**{format_money(result.notional_minor - result.fee_minor)}** proceeds"
        )
        suffix = f" ({legs} liquidation leg(s) fired)" if legs else ""
        closed = " — position closed" if capped else ""
        await interaction.response.send_message(
            f"{verb} **{result.quantity:,}** {result.ticker} @ "
            f"{format_price(result.fill_price)} — {moved} "
            f"(fee {format_money(result.fee_minor)}). "
            f"Cash: **{format_money(cash_after)}**{closed}{suffix}" + _welcome_suffix(bootstrap)
        )

    @tree.command(
        name="short",
        description="Open a bounded short: capped downside, auto-knockout above entry",
    )
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares to short",
        dollars="Short ~this many dollars of notional instead of a share count",
        knockout="Knockout % above entry — tighter is cheaper, wider is safer "
        "(default: instrument's own)",
        league="Use your season league stake instead of your main portfolio",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def short(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000] | None = None,
        dollars: app_commands.Range[float, 0.01, 1_000_000_000.0] | None = None,
        knockout: float | None = None,
        league: bool = False,
    ) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            account_id = bootstrap.account_id
            season_id: int | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season. `/league join` first.",
                        ephemeral=True,
                    )
                    return
                season_id, account_id = entry
            if (quantity is not None) == (dollars is not None):
                await interaction.response.send_message(
                    "Give exactly one of `quantity` or `dollars`.", ephemeral=True
                )
                return
            if dollars is not None:
                qty = await shares_for_dollars(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side="SELL",
                    dollars_minor=int(Decimal(str(dollars)) * 100),
                )
                if qty <= 0:
                    await interaction.response.send_message(
                        f"That amount shorts less than one {ticker.upper()} "
                        "share — bump `dollars`.",
                        ephemeral=True,
                    )
                    return
                quantity = qty
            assert quantity is not None
            try:
                result = await open_bounded_short(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    quantity=quantity,
                    interaction_id=str(interaction.id),
                    season_id=season_id,
                    knockout_pct=(Decimal(str(knockout)) / 100 if knockout is not None else None),
                )
            except InsufficientFundsError:
                bal = await get_balance(conn, account_id)
                await interaction.response.send_message(
                    f"Collateral for {quantity:,} {ticker.upper()} exceeds "
                    f"your balance ({format_money(bal)}).",
                    ephemeral=True,
                )
                return
            # MarginError: assert_spend_ok can block the fee spend.
            # ValueError: "collateral rounds to zero" on tiny shorts.
            except (TradingError, MarginError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Opened bounded short **#{result.short_id}**: {result.quantity} {result.ticker} "
            f"@ {format_price(result.entry_price)} — collateral "
            f"{format_money(result.collateral_minor)}, knocks out at "
            f"{format_price(result.knockout_price)}." + _welcome_suffix(bootstrap)
        )

    @tree.command(name="shorts", description="List your open bounded shorts")
    @app_commands.describe(league="Show league shorts instead of main-portfolio ones")
    async def shorts(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            season_id: int | None = None
            bootstrap: BootstrapResult | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season.", ephemeral=True
                    )
                    return
                season_id = entry[0]
            else:
                bootstrap = await bootstrap_user(conn, interaction.user.id)
            rows = await list_open_shorts(conn, interaction.user.id, season_id)

        if not rows:
            await interaction.response.send_message(
                "No open bounded shorts — `/short <ticker>` opens one.",
                ephemeral=True,
            )
            return
        lines = ["id    ticker      qty          entry            KO       P&L"]
        for s in rows:
            pnl_pct = float(s["entry_price"]) / float(s["quoted_price"]) - 1
            lines.append(
                f"#{s['id']:<4} {s['ticker']:<6} {s['quantity']:>6} @ "
                f"{format_price(s['entry_price']):>10}  "
                f"KO {format_price(s['knockout_price']):>10}  "
                f"{format_pct(pnl_pct):>8}"
            )
        embed = discord.Embed(title="Open bounded shorts")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        embed.set_footer(text="/cover <id> to close early")
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(name="cover", description="Close a bounded short at market")
    @app_commands.describe(short_id="Bounded short id from /shorts")
    @app_commands.autocomplete(short_id=short_autocomplete)
    async def cover(interaction: discord.Interaction, short_id: int) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            try:
                result = await cover_bounded_short(
                    conn, user_id=interaction.user.id, short_id=short_id
                )
            except TradingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        outcome = "profit" if result.payoff_minor >= 0 else "loss"
        await interaction.response.send_message(
            f"Covered **#{result.short_id}** {result.ticker} @ "
            f"{format_price(result.close_price)} — {outcome} "
            f"{format_money(abs(result.payoff_minor))}, paid out "
            f"{format_money(result.payout_minor)}." + _welcome_suffix(bootstrap)
        )

    @tree.command(name="margin", description="Show your margin account health")
    @app_commands.describe(league="Show your league account's margin instead")
    async def margin_cmd(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            season_id: int | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season. `/league join` first.",
                        ephemeral=True,
                    )
                    return
                season_id = entry[0]
            health = await compute_health(conn, interaction.user.id, season_id)
            tier = await margin_tier(conn, interaction.user.id, season_id)

        embed = discord.Embed(
            title=f"{'League m' if league else 'M'}argin — {interaction.user.display_name}"
        )
        embed.add_field(name="Equity", value=format_money(health.equity_minor))
        embed.add_field(name="Cash", value=format_money(health.cash_minor))
        embed.add_field(name="Positions value", value=format_money(health.positions_value_minor))
        embed.add_field(name="Maintenance req", value=format_money(health.maint_req_minor))
        embed.add_field(name="Initial req", value=format_money(health.init_req_minor))
        ratio = health.ratio
        embed.add_field(
            name="Margin ratio",
            value="not margined" if ratio is None else f"{float(ratio):.2f}",
        )
        embed.add_field(name="Margin tier", value=str(tier))
        if health.accrued_fees_minor:
            embed.add_field(
                name="Accrued borrow fees", value=format_money(health.accrued_fees_minor)
            )
        if health.accrued_dividends_minor:
            embed.add_field(
                name="Accrued dividends",
                value=format_money(health.accrued_dividends_minor),
            )
        if health.undermargined:
            embed.set_footer(text="Below maintenance — liquidation may fire on the next tick.")
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(
        name="collateral",
        description="Show per-position margin requirements for your shorts",
    )
    @app_commands.describe(league="Show league positions instead")
    async def collateral(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            season_id: int | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season. `/league join` first.",
                        ephemeral=True,
                    )
                    return
                season_id = entry[0]
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, p.quantity, i.quoted_price, i.maint_margin_pct,
                           i.init_margin_pct, i.short_interest_pct,
                           p.borrow_fees_accrued, p.dividends_accrued
                    FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s AND p.quantity < 0
                      AND p.season_id IS NOT DISTINCT FROM %s
                    ORDER BY i.ticker
                    """,
                    (interaction.user.id, season_id),
                )
                rows = await cur.fetchall()
        if not rows:
            await interaction.response.send_message("No open margin shorts.", ephemeral=True)
            return
        lines = []
        for r in rows:
            notional = -int(r[1]) * Decimal(r[2])
            fees = int(Decimal(r[6]).quantize(Decimal("1")))
            divs = int(Decimal(r[7]).quantize(Decimal("1")))
            lines.append(
                f"{r[0]:<6} {r[1]:>6}  notional {format_price(notional):>10}  "
                f"maint {format_money(int(notional * Decimal(r[3]) * 100)):>9}  "
                f"SI {float(r[5]) * 100:.1f}%  fees {format_money(fees)}"
                + (f"  divs {format_money(divs)}" if divs else "")
            )
        embed = discord.Embed(title="Collateral — open shorts")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @tree.command(name="liquidations", description="Show your recent liquidation events")
    async def liquidations(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            rows = await list_liquidations(conn, interaction.user.id)
        if not rows:
            await interaction.response.send_message(
                "No liquidations — `/margin` shows your current health.",
                ephemeral=True,
            )
            return
        lines = ["id    ticker  side      qty           fill      penalty"] + [
            f"#{r['id']:<4} {r['ticker']:<6} {r['side']:<4} {r['quantity_closed']:>6} @ "
            f"{format_price(r['fill_price']):>10}  {format_money(r['penalty_minor']):>9}"
            for r in rows
        ]
        embed = discord.Embed(title="Liquidations")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    order_group = app_commands.Group(
        name="order", description="Resting limit orders, matched once per tick"
    )

    async def _place_order(
        interaction: discord.Interaction,
        ticker: str,
        side: str,
        quantity: int | None,
        dollars: float | None,
        limit: float | None,
        stop: float | None,
        hours: float | None,
        league: bool,
        display: int | None,
        short: bool = False,
        trail: float | None = None,
    ) -> None:
        if limit is None and stop is None and trail is None:
            await interaction.response.send_message(
                "Give a `limit` price, a `stop` price, a `trail` distance, "
                "or limit+stop (stop-limit).",
                ephemeral=True,
            )
            return
        if (quantity is not None) == (dollars is not None):
            await interaction.response.send_message(
                "Give exactly one of `quantity` or `dollars`.", ephemeral=True
            )
            return
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            season_id: int | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season. `/league join` first.",
                        ephemeral=True,
                    )
                    return
                season_id = entry[0]
            if dollars is not None:
                # Size against the order's anchor: the limit for
                # limit/stop-limit orders, the stop for pure stops, and
                # the live mark for a trail-only stop (it has no resting
                # price yet -- the stop anchors off the mark anyway).
                anchor_arg = limit if limit is not None else stop
                if anchor_arg is None:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT quoted_price FROM instruments WHERE ticker = %s",
                            (ticker.upper(),),
                        )
                        mark_row = await cur.fetchone()
                    if mark_row is None:
                        await interaction.response.send_message(
                            f"Unknown instrument {ticker.upper()}.", ephemeral=True
                        )
                        return
                    anchor = Decimal(mark_row[0])
                else:
                    anchor = Decimal(str(anchor_arg))
                qty = await shares_for_dollars(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,  # type: ignore[arg-type]
                    dollars_minor=int(Decimal(str(dollars)) * 100),
                    anchor_price=anchor,
                )
                if qty <= 0:
                    verb = "buys" if side == "BUY" else "sells"
                    await interaction.response.send_message(
                        f"~{format_money(int(Decimal(str(dollars)) * 100))} "
                        f"{verb} less than one {ticker.upper()} share at "
                        f"{format_price(anchor)} — bump `dollars`.",
                        ephemeral=True,
                    )
                    return
                capped = False
                if side == "SELL" and not short:
                    held = await _held_shares(conn, interaction.user.id, ticker, season_id)
                    if held <= 0:
                        await interaction.response.send_message(
                            f"You don't hold any {ticker.upper()}. "
                            "Pass `short: True` to sell past your position.",
                            ephemeral=True,
                        )
                        return
                    capped = qty > held
                    qty = min(qty, held)
                quantity = qty
            else:
                capped = False
            unbacked = 0
            if side == "SELL" and not short:
                held = await _held_shares(conn, interaction.user.id, ticker, season_id)
                unbacked = max(0, quantity or 0) - max(held, 0)
            assert quantity is not None
            try:
                result = await place_order(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,  # type: ignore[arg-type]
                    quantity=quantity,
                    limit_price=None if limit is None else Decimal(str(limit)),
                    season_id=season_id,
                    expires_in_ticks=(None if hours is None else int(hours * TICKS_PER_DAY / 24)),
                    interaction_id=str(interaction.id),
                    stop_price=None if stop is None else Decimal(str(stop)),
                    display_qty=display,
                    allow_short=short,
                    trail_amount=None if trail is None else Decimal(str(trail)),
                )
            except (TradingError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        expiry = "GTC" if result.expires_tick is None else f"expires tick {result.expires_tick}"
        if result.order_type == "STOP":
            price_part = f"stop {format_price(result.stop_price or 0)}"
            if result.trail_amount is not None:
                price_part += f" (trails {format_price(result.trail_amount)})"
        elif result.order_type == "STOP_LIMIT":
            price_part = (
                f"stop {format_price(result.stop_price or 0)} "
                f"limit {format_price(result.limit_price or 0)}"
            )
        else:
            price_part = f"@ {format_price(result.limit_price or 0)}"
        iceberg = (
            f" (iceberg — showing {result.display_qty:,})" if result.display_qty is not None else ""
        )
        order_anchor = result.limit_price if result.limit_price is not None else result.stop_price
        notional = (
            f" — ~{format_money(result.quantity * int(order_anchor * 100))} "
            f"{'cost' if result.side == 'BUY' else 'proceeds'} if it fills"
            if order_anchor is not None
            else ""
        )
        position_capped = " (capped at your position)" if capped else ""
        unbacked_note = (
            f" — {unbacked:,} share(s) rest unbacked and can't fill unless "
            "you buy more or re-place with `short: True`"
            if unbacked > 0
            else ""
        )
        await interaction.response.send_message(
            f"Order **#{result.order_id}** resting: {result.side} "
            f"**{result.quantity:,}** {result.ticker} {price_part}{iceberg}"
            f"{notional}{position_capped}{unbacked_note} "
            f"({expiry}). Fills when the market reaches it." + _welcome_suffix(bootstrap)
        )

    @order_group.command(name="buy", description="Rest a limit/stop buy")
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        dollars="Size the order for ~this many dollars instead of a share count",
        limit="Max price you'll pay (omit for a pure stop order)",
        stop="Trigger once the mark rises to this price",
        trail="Paid unlock: stop trails this many dollars below the mark",
        hours="Auto-expire after this many hours (omit for GTC)",
        league="Place the order in your league portfolio",
        display="Iceberg (paid unlock): show only this many shares in the book",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def order_buy(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000] | None = None,
        dollars: app_commands.Range[float, 0.01, 1_000_000_000.0] | None = None,
        # NUMERIC(18, 6) tops out just under 1e12; keep the bound inside it.
        limit: app_commands.Range[float, 0.0001, 999_999_999_999.0] | None = None,
        stop: app_commands.Range[float, 0.0001, 999_999_999_999.0] | None = None,
        trail: app_commands.Range[float, 0.0001, 999_999_999_999.0] | None = None,
        hours: app_commands.Range[float, 0.02, 8760.0] | None = None,
        league: bool = False,
        display: app_commands.Range[int, 1, 1_000_000_000] | None = None,
    ) -> None:
        await _place_order(
            interaction,
            ticker,
            "BUY",
            quantity,
            dollars,
            limit,
            stop,
            hours,
            league,
            display,
            False,
            trail,
        )

    @order_group.command(
        name="sell",
        description="Rest a limit/stop sell (stops double as stop-losses)",
    )
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        dollars="Size the order for ~this many dollars instead of a share count",
        limit="Min price you'll accept (omit for a pure stop order)",
        stop="Trigger once the mark falls to this price (stop-loss)",
        trail="Paid unlock: stop trails this many dollars behind the mark",
        hours="Auto-expire after this many hours (omit for GTC)",
        league="Place the order in your league portfolio",
        display="Iceberg (paid unlock): show only this many shares in the book",
        short="Opt in to selling past your position (opens a margin short)",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def order_sell(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000] | None = None,
        dollars: app_commands.Range[float, 0.01, 1_000_000_000.0] | None = None,
        limit: app_commands.Range[float, 0.0001, 999_999_999_999.0] | None = None,
        stop: app_commands.Range[float, 0.0001, 999_999_999_999.0] | None = None,
        trail: app_commands.Range[float, 0.0001, 999_999_999_999.0] | None = None,
        hours: app_commands.Range[float, 0.02, 8760.0] | None = None,
        league: bool = False,
        display: app_commands.Range[int, 1, 1_000_000_000] | None = None,
        short: bool = False,
    ) -> None:
        await _place_order(
            interaction,
            ticker,
            "SELL",
            quantity,
            dollars,
            limit,
            stop,
            hours,
            league,
            display,
            short,
            trail,
        )

    @order_group.command(
        name="bracket",
        description="Paid unlock: take-profit + stop-loss pair — first hit cancels the other",
    )
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Shares on each leg",
        take_profit="Limit price of the profit leg",
        stop_loss="Trigger price of the stop leg",
        side="SELL exits a long (default); BUY exits a short",
        hours="Auto-expire after this many hours (omit for GTC)",
        league="Place the bracket in your league portfolio",
        short="Allow the legs to sell past your position (margin short)",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def order_bracket(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000],
        take_profit: app_commands.Range[float, 0.0001, 999_999_999_999.0],
        stop_loss: app_commands.Range[float, 0.0001, 999_999_999_999.0],
        side: str = "SELL",
        hours: app_commands.Range[float, 0.02, 8760.0] | None = None,
        league: bool = False,
        short: bool = False,
    ) -> None:
        side = side.upper()
        if side not in ("SELL", "BUY"):
            await interaction.response.send_message("`side` must be SELL or BUY.", ephemeral=True)
            return
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            season_id: int | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season. `/league join` first.",
                        ephemeral=True,
                    )
                    return
                season_id = entry[0]
            try:
                tp_leg, sl_leg = await place_oco(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,  # type: ignore[arg-type]
                    quantity=quantity,
                    take_profit=Decimal(str(take_profit)),
                    stop_price=Decimal(str(stop_loss)),
                    season_id=season_id,
                    expires_in_ticks=(None if hours is None else int(hours * TICKS_PER_DAY / 24)),
                    interaction_id=str(interaction.id),
                    allow_short=short,
                )
            except (TradingError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Bracket resting on **{ticker.upper()}**: "
            f"**#{tp_leg.order_id}** {side} {quantity:,} @ "
            f"{format_price(tp_leg.limit_price or 0)} take-profit, "
            f"**#{sl_leg.order_id}** stop {format_price(sl_leg.stop_price or 0)} "
            "— whichever resolves first cancels the other." + _welcome_suffix(bootstrap)
        )

    @order_group.command(name="list", description="Show your resting orders")
    @app_commands.describe(league="Show league orders instead of main-portfolio ones")
    async def order_list(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            season_id: int | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season.", ephemeral=True
                    )
                    return
                season_id = entry[0]
            rows = await list_open_orders(conn, interaction.user.id, season_id)
        if not rows:
            await interaction.response.send_message(
                "No open orders — `/order buy` or `/order sell` to rest one.",
                ephemeral=True,
            )
            return

        def _order_line(r: dict[str, Any]) -> str:
            remaining = r["quantity"] - r["filled_quantity"]
            if r["order_type"] == "STOP":
                price = f"stop {format_price(r['stop_price'])}"
            elif r["order_type"] == "STOP_LIMIT":
                price = f"stop {format_price(r['stop_price'])} lim {format_price(r['limit_price'])}"
            else:
                price = f"@ {format_price(r['limit_price'])}"
            triggered = " [TRIGGERED]" if r["triggered_tick"] is not None else ""
            iceberg = (
                f" [iceberg->{min(r['display_qty'], remaining):,}]"
                if r["display_qty"] is not None
                else ""
            )
            trail = (
                f" [trail {format_price(r['trail_amount'])}]"
                if r["trail_amount"] is not None
                else ""
            )
            oco = " [OCO]" if r["oco_group"] is not None else ""
            return (
                f"#{r['id']:<4} {r['side']:<4} {remaining:>5} {r['ticker']:<6} "
                f"{price:>24} (mark {format_price(r['quoted_price'])})"
                f"{triggered}{iceberg}{trail}{oco}"
            )

        lines = ["id    side    qty ticker                     price"] + [
            _order_line(r) for r in rows
        ]
        embed = discord.Embed(title="Open orders")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @order_group.command(name="cancel", description="Cancel a resting order")
    @app_commands.describe(order_id="Order id from /order list")
    @app_commands.autocomplete(order_id=order_autocomplete)
    async def order_cancel(interaction: discord.Interaction, order_id: int) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            cancelled = await cancel_order(conn, user_id=interaction.user.id, order_id=order_id)
        if cancelled:
            await interaction.response.send_message(
                f"Cancelled order **#{order_id}**." + _welcome_suffix(bootstrap)
            )
        else:
            await interaction.response.send_message(
                f"No open order **#{order_id}** on your account.", ephemeral=True
            )

    tree.add_command(order_group)

    alert_group = app_commands.Group(
        name="alert", description="One-shot price alerts (DM on cross)"
    )

    @alert_group.command(name="add", description="DM me when a ticker crosses a price")
    @app_commands.describe(
        ticker="Instrument ticker",
        direction="above or below",
        price="Target price in dollars",
    )
    @app_commands.choices(
        direction=[
            app_commands.Choice(name="above", value="ABOVE"),
            app_commands.Choice(name="below", value="BELOW"),
        ]
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def alert_add(
        interaction: discord.Interaction,
        ticker: str,
        direction: str,
        price: app_commands.Range[float, 0.0001, 999_999_999_999.0],
    ) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            try:
                alert_id = await create_alert(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    direction=direction,
                    target=Decimal(str(price)),
                )
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT quoted_price FROM instruments WHERE ticker = %s",
                        (ticker.strip().upper(),),
                    )
                    mark_row = await cur.fetchone()
            except TradingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        assert mark_row is not None
        await interaction.response.send_message(
            f"Alert **#{alert_id}** set — **{ticker.strip().upper()}** "
            f"{direction.lower()} {format_price(price)} "
            f"(now {format_price(mark_row[0])}). I'll DM you when it crosses."
            + _welcome_suffix(bootstrap)
        )

    @alert_group.command(name="list", description="Your open and recent alerts")
    async def alert_list(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            rows = await list_alerts(conn, interaction.user.id)
        if not rows["open"] and not rows["closed"]:
            await interaction.response.send_message(
                "No alerts — `/alert add` one and I'll DM you at the cross.",
                ephemeral=True,
            )
            return
        lines = [
            f"**#{a['id']}** {a['ticker']} {a['direction'].lower()} "
            f"{format_price(a['target_price'])} "
            f"(now {format_price(a['quoted_price'])})"
            for a in rows["open"]
        ]
        if rows["closed"]:
            lines += ["— recent —"] + [
                f"~~#{a['id']} {a['ticker']} {a['direction'].lower()} "
                f"{format_price(a['target_price'])}~~ {a['status'].lower()}"
                + (
                    f" at {format_price(a['triggered_price'])}"
                    if a["triggered_price"] is not None
                    else ""
                )
                for a in rows["closed"]
            ]
        await interaction.response.send_message(
            "\n".join(lines) + _welcome_suffix(bootstrap), ephemeral=True
        )

    @alert_group.command(name="cancel", description="Cancel an open alert")
    @app_commands.describe(alert_id="Alert id from /alert list")
    @app_commands.autocomplete(alert_id=alert_autocomplete)
    async def alert_cancel(interaction: discord.Interaction, alert_id: int) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            cancelled = await cancel_alert(conn, user_id=interaction.user.id, alert_id=alert_id)
        if cancelled:
            await interaction.response.send_message(
                f"Cancelled alert **#{alert_id}**." + _welcome_suffix(bootstrap)
            )
        else:
            await interaction.response.send_message(
                f"No open alert **#{alert_id}** on your account.", ephemeral=True
            )

    tree.add_command(alert_group)

    @tree.command(
        name="shop",
        description="Browse and buy slots, tools, consumables, and cosmetics",
    )
    @app_commands.describe(item="Jump straight to an item card, e.g. pro_terminal")
    @app_commands.autocomplete(item=shop_item_autocomplete)
    async def shop(interaction: discord.Interaction, item: str | None = None) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            state = await load_shop_state(conn, interaction.user.id)
            if item is not None:
                target = find_shop_item(state, item)
                if target is None or not is_purchasable(target):
                    await interaction.response.send_message(
                        f"**{item}** isn't for sale — browse `/shop` instead.",
                        ephemeral=True,
                    )
                    return
                embed, view = build_shop_card(state, target)
            else:
                embed, view = build_shop_home(state)
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None,
            embed=embed,
            view=view,
            ephemeral=True,
        )

    @tree.command(name="card", description="Look up a collectible card and your copy")
    @app_commands.describe(card="Card name or key, e.g. card_nort or The Whale")
    @app_commands.autocomplete(card=card_autocomplete)
    async def card_lookup(interaction: discord.Interaction, card: str) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            c = await find_card(conn, card)
            if c is None:
                await interaction.response.send_message(
                    f"No card named **{card}** — browse `/collection`.",
                    ephemeral=True,
                )
                return
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT uc.best_frame, uc.copies, uc.best_serial,"
                    " COALESCE(bp.stamps, '{}') AS best_stamps"
                    " FROM user_cards uc"
                    " LEFT JOIN card_pulls bp"
                    "  ON bp.user_id = uc.user_id"
                    " AND bp.card_key = uc.card_key"
                    " AND bp.serial = uc.best_serial"
                    " WHERE uc.user_id = %s AND uc.card_key = %s",
                    (interaction.user.id, c.key),
                )
                held = await cur.fetchone()
        embed = discord.Embed(
            title=f"{_frame_glyph_for(c)} {c.name}",
            description=c.flavor,
        )
        kind_label = (
            f"{c.kind.lower().capitalize()} · {c.set_key} set"
            + (f" · {c.rarity}" if c.rarity else "")
        )
        embed.add_field(name="Card", value=kind_label)
        if held:
            yours = f"{_frame_glyph(str(held[0]))} {held[0]}"
            if held[2] is not None:
                yours += f" · mint #{held[2]}"
            if int(held[1]) > 1:
                yours += f" ×{held[1]}"
            stamp_names = [STAMP_LABEL.get(s, s) for s in held[3] or []]
            if stamp_names:
                yours += "\n*" + "; ".join(stamp_names) + "*"
            embed.add_field(name="Yours", value=yours)
        else:
            embed.add_field(name="Yours", value="Not collected")
        if c.kind == "ASSEMBLED":
            async with db.connection() as conn:
                recipe = await recipe_for(conn, c.key)
                prog: dict[str, int] = {}
                if recipe:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT card_key, copies FROM user_cards"
                            " WHERE user_id = %s AND card_key = ANY(%s)",
                            (interaction.user.id, [r["part_key"] for r in recipe]),
                        )
                        prog = {str(r[0]): int(r[1]) for r in await cur.fetchall()}
            if recipe:
                embed.add_field(
                    name="Recipe",
                    value="\n".join(
                        f"{min(prog.get(r['part_key'], 0), r['qty'])}/{r['qty']} {r['name']}"
                        for r in recipe
                    ),
                    inline=False,
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="collection", description="Browse a card binder")
    @app_commands.describe(user="Whose binder to view (default: yours)")
    async def collection(
        interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        target = user or interaction.user
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            stats = await collection_stats(conn, target.id)
            rows = await list_collection(conn, target.id)
        embed, view = build_collection_page(
            target.display_name, stats, rows, target.id, 0
        )
        await interaction.response.send_message(embed=embed, view=view)

    @tree.command(name="collectors", description="Top collectors by card score")
    async def collectors(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            rows = await collector_leaderboard(conn, limit=15)
            mine = await collector_leaderboard(
                conn, limit=None, user_id=interaction.user.id
            )
        lines = []
        for idx, r in enumerate(rows, start=1):
            top = (
                f" · {r['top_frame']} {r['top_name']}"
                + (f" #{r['top_serial']}" if r.get("top_serial") else "")
                if r.get("top_name")
                else ""
            )
            lines.append(
                f"{idx:>3}. <@{r['user_id']}> — **{int(r['score'])}** "
                f"({int(r['cards'])} cards){top}"
            )
        embed = discord.Embed(
            title="Collector leaderboard",
            description="\n".join(lines)
            if lines
            else "No cards yet — `/open` a pack to start a binder.",
        )
        if mine:
            embed.set_footer(
                text=f"You: {int(mine[0]['score'])} pts · {int(mine[0]['cards'])} cards"
            )
        await interaction.response.send_message(embed=embed)

    @tree.command(name="open", description="Open a card pack with a staged reveal")
    @app_commands.describe(pack="Which pack to open")
    @app_commands.autocomplete(pack=pack_autocomplete)
    async def open_pack_cmd(interaction: discord.Interaction, pack: str) -> None:
        # C10: the pull transaction COMMITS and releases the connection
        # before the reveal starts -- a ~4s animation never pins a pooled
        # connection, and a restart mid-reveal leaves cards correctly
        # owned (/collection is the source of truth).
        await interaction.response.defer()
        try:
            async with db.connection() as conn:
                await bootstrap_user(conn, interaction.user.id)
                outcomes = await open_card_pack(
                    conn,
                    interaction.user.id,
                    pack,
                    interaction_id=str(interaction.id),
                    master_seed=get_settings().master_seed,
                )
        except ShopError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except DuplicateInteractionError:
            await interaction.followup.send(
                "That pack was already opened.", ephemeral=True
            )
            return

        # Bonus parts open first (the wrapper), then best lands last --
        # ascending pulled-tier order for the card reveals.
        reveals = sorted(
            outcomes,
            key=lambda o: (o.tier != "PART", FRAME_RANK.get(o.tier, 0)),
        )
        if reveals:
            # First card on the deferred response; the rest edit in.
            embed = _pull_embed(reveals[0], 1, len(reveals))
            await interaction.edit_original_response(embed=embed)
            for idx, outcome in enumerate(reveals[1:], start=2):
                await asyncio.sleep(1.2)
                embed = _pull_embed(outcome, idx, len(reveals))
                await interaction.edit_original_response(embed=embed)
        summary = "\n".join(
            f"{_frame_glyph(o.tier)} **{o.name}** #{o.serial} — {o.outcome.lower()}"
            + (f" (+{o.shards} shards)" if o.shards else "")
            + stamp_glyphs(o.stamps)
            for o in reveals
        )
        await asyncio.sleep(0.6)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title=f"Pack opened — {len(reveals)} cards", description=summary
            )
        )

    @tree.command(
        name="craft",
        description="Craft a missing card, upgrade a frame, or assemble parts",
    )
    @app_commands.describe(card="Card to craft (missing) or frame-upgrade (held)")
    @app_commands.autocomplete(card=card_autocomplete)
    async def craft(interaction: discord.Interaction, card: str) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            c = await find_card(conn, card)
            if c is None:
                await interaction.response.send_message(
                    f"No card named **{card}**.", ephemeral=True
                )
                return
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM user_cards WHERE user_id = %s AND card_key = %s",
                    (interaction.user.id, c.key),
                )
                held = await cur.fetchone() is not None
            try:
                if c.kind == "ASSEMBLED":
                    _k, serial = await assemble_card(
                        conn, interaction.user.id, c.key
                    )
                    message = f"Assembled **{c.name}** — mint #{serial}."
                elif held:
                    _k, frame, cost = await upgrade_frame(conn, interaction.user.id, c.key)
                    message = (
                        f"Upgraded **{c.name}** to {_frame_glyph(frame)} {frame} "
                        f"for {cost} shards."
                    )
                else:
                    _k, cost = await craft_card(conn, interaction.user.id, c.key)
                    message = f"Crafted **{c.name}** (⚪ STANDARD) for {cost} shards."
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(message)

    @tree.command(
        name="feature",
        description="Pin a held card on your profile and leaderboard flair",
    )
    @app_commands.describe(card="Card to feature; omit to clear")
    @app_commands.autocomplete(card=owned_card_autocomplete)
    async def feature(interaction: discord.Interaction, card: str | None = None) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            c = await find_card(conn, card) if card else None
            if card and c is None:
                await interaction.response.send_message(
                    f"No card named **{card}**.", ephemeral=True
                )
                return
            try:
                key = await set_featured_card(
                    conn, interaction.user.id, c.key if c else None
                )
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        if key and c:
            await interaction.response.send_message(
                f"Now featuring **{c.name}** on your profile."
            )
        else:
            await interaction.response.send_message("Featured card cleared.")

    @tree.command(name="gift", description="Buy a shop item for another player")
    @app_commands.describe(user="Who receives it", item="What to buy for them")
    @app_commands.autocomplete(item=shop_item_autocomplete)
    async def gift(
        interaction: discord.Interaction, user: discord.User, item: str
    ) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            try:
                paid = await buy_item(
                    conn,
                    interaction.user.id,
                    item,
                    interaction_id=str(interaction.id),
                    gift_to=user.id,
                )
            except (ShopError, InsufficientFundsError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            except DuplicateInteractionError:
                await interaction.response.send_message(
                    "That gift already went through.", ephemeral=True
                )
                return
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT name FROM shop_items WHERE key = %s", (item,)
                )
                row = await cur.fetchone()
        name = str(row[0]) if row else item
        await interaction.response.send_message(
            f"🎁 Sent **{name}** to <@{user.id}> for {format_money(paid)}.",
            ephemeral=True,
        )

    @tree.command(
        name="commission",
        description="Spend a listing credit to list a custom instrument",
    )
    @app_commands.describe(
        ticker="1-10 chars, A-Z/0-9",
        name="Company name",
        sector="Sector key",
        market="Venue code (default: first market)",
        base_price="Opening price in dollars (default: 50)",
    )
    @app_commands.autocomplete(sector=sector_autocomplete)
    async def commission(
        interaction: discord.Interaction,
        ticker: str,
        name: str,
        sector: str,
        market: str | None = None,
        base_price: float = 50.0,
    ) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            try:
                async with conn.transaction():
                    # Consume and list in one tx: a bad ticker/sector or a
                    # duplicate listing rolls the credit spend back too.
                    await record_idempotency_key(conn, str(interaction.id))
                    await use_consumable(conn, interaction.user.id, "listing_credit")
                    try:
                        await add_instrument(
                            conn,
                            ticker=ticker,
                            name=name,
                            sector_key=sector,
                            base_price=base_price,
                            market_code=market,
                        )
                    except ValueError as exc:
                        raise ShopError(str(exc)) from exc
                    await emit_feed(
                        conn,
                        "LISTING",
                        {"ticker": ticker.strip().upper(), "name": name},
                        user_id=interaction.user.id,
                    )
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            except DuplicateInteractionError:
                await interaction.response.send_message(
                    "That commission already went through.", ephemeral=True
                )
                return
        await interaction.response.send_message(
            f"📈 **{ticker.strip().upper()}** ({name}) commissioned — it lists "
            "on the next tick. Find it with `/stock`."
        )

    @tree.command(name="purchases", description="Your recent shop purchases")
    async def purchases(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT l.reason, l.memo, -l.amount AS spent, l.created_at
                    FROM ledger_entries l
                    JOIN accounts a ON a.id = l.account_id
                    WHERE a.user_id = %s AND l.amount < 0
                      AND l.reason LIKE 'SHOP_%%'
                    ORDER BY l.id DESC LIMIT 10
                    """,
                    (interaction.user.id,),
                )
                rows = await cur.fetchall()
        if not rows:
            await interaction.response.send_message(
                "No purchases yet — `/shop` is the storefront.", ephemeral=True
            )
            return
        lines = []
        for reason, memo, spent, ts in rows:
            label = str(memo or reason).split(" gift ")[0]
            gifted = " 🎁" if reason == "SHOP_GIFT" else ""
            lines.append(
                f"`{discord.utils.format_dt(ts, 'd')}` **{label}**{gifted}"
                f" — {format_money(int(spent))}"
            )
        embed = discord.Embed(title="Recent purchases", description="\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    trade_group = app_commands.Group(
        name="trade", description="Trade cards and shards with other players"
    )

    @trade_group.command(name="offer", description="Offer a card/shard swap to another player")
    @app_commands.describe(
        user="Who you're offering to",
        give="Card you give (optional if you give shards)",
        want="Card you want from them (optional if you ask shards)",
        give_shards="Shards you add to your side",
        want_shards="Shards you ask from their side",
    )
    @app_commands.autocomplete(give=owned_card_autocomplete, want=their_card_autocomplete)
    async def trade_offer(
        interaction: discord.Interaction,
        user: discord.User,
        give: str | None = None,
        want: str | None = None,
        give_shards: int = 0,
        want_shards: int = 0,
    ) -> None:
        if give_shards < 0 or want_shards < 0:
            await interaction.response.send_message(
                "Shard amounts can't be negative.", ephemeral=True
            )
            return
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            async with conn.cursor() as cur:
                # No bootstrap for the counterparty: offering to someone
                # who never played shouldn't mint them an account.
                await cur.execute("SELECT 1 FROM users WHERE id = %s", (user.id,))
                if await cur.fetchone() is None:
                    await interaction.response.send_message(
                        f"<@{user.id}> hasn't started yet — they need `/start` first.",
                        ephemeral=True,
                    )
                    return
            give_c = await find_card(conn, give) if give else None
            want_c = await find_card(conn, want) if want else None
            if give and give_c is None:
                await interaction.response.send_message(
                    f"No card named **{give}**.", ephemeral=True
                )
                return
            if want and want_c is None:
                await interaction.response.send_message(
                    f"No card named **{want}**.", ephemeral=True
                )
                return
            give_cards = [give_c.key] if give_c else []
            want_cards = [want_c.key] if want_c else []
            tick = await current_tick_index(conn)
            try:
                trade_id = await create_offer(
                    conn,
                    interaction.user.id,
                    user.id,
                    give_cards,
                    want_cards,
                    give_shards,
                    want_shards,
                    tick,
                )
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            trade = await get_trade(conn, trade_id)
            assert trade is not None
            embed = await render_trade(conn, trade)
        await interaction.response.send_message(
            embed=embed, view=trade_view(trade_id)
        )

    @trade_group.command(name="list", description="Your open trade offers")
    async def trade_list(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            offers = await list_trades(conn, interaction.user.id)
        if not offers:
            await interaction.response.send_message(
                "No open offers — `/trade offer` to start one.", ephemeral=True
            )
            return
        lines = []
        for t in offers:
            direction = "→" if t.proposer == interaction.user.id else "←"
            other = t.counterparty if t.proposer == interaction.user.id else t.proposer
            side = (
                f"{', '.join(t.give_cards)}"
                + (f" +{t.give_shards}sh" if t.give_shards else "")
            ) or "nothing"
            gets = (
                f"{', '.join(t.want_cards)}"
                + (f" +{t.want_shards}sh" if t.want_shards else "")
            ) or "nothing"
            lines.append(f"`#{t.id}` {direction} <@{other}> — {side} ⇄ {gets}")
        await interaction.response.send_message(
            embed=discord.Embed(title="Open trades", description="\n".join(lines)),
            ephemeral=True,
        )

    @trade_group.command(name="cancel", description="Cancel an open offer you made")
    @app_commands.describe(trade_id="Offer id from /trade list")
    async def trade_cancel(interaction: discord.Interaction, trade_id: int) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            try:
                await trades_cancel(conn, trade_id, interaction.user.id)
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Offer `#{trade_id}` cancelled.", ephemeral=True
        )

    ipo_group = app_commands.Group(
        name="ipo", description="IPO subscriptions — commit cash before listing day"
    )

    @ipo_group.command(name="list", description="Open and recently settled IPO offerings")
    async def ipo_list(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            tick = await current_tick_index(conn)
            rows = await ipo_svc.list_offerings(conn, interaction.user.id)
        if not rows:
            await interaction.response.send_message("No IPO offerings right now.", ephemeral=True)
            return
        lines = []
        for r in rows:
            head = (
                f"{r['ticker']:<6} {format_price(r['offer_price_minor'] / 100):>10}"
                f" × {r['shares_offered']:,} sh"
            )
            if r["status"] == "OPEN":
                remaining = max(0, int(r["close_tick"]) - (tick or 0))
                mine = (
                    f" · yours {format_money(r['my_committed_minor'])}"
                    if r["my_committed_minor"]
                    else ""
                )
                lines.append(
                    f"{head} · committed {format_money(r['committed_minor'])}"
                    f"{mine} · closes in ~{remaining} tick(s)"
                )
            else:
                alloc = (
                    f" · you got {r['my_allocated_qty']:,} sh"
                    f" ({format_money(r['my_refund_minor'])} back)"
                    if r["my_allocated_qty"] is not None
                    else ""
                )
                lines.append(
                    f"{head} · {r['status'].lower()}"
                    + (
                        f" — {r['allocated_qty']:,} sh allocated"
                        if r["status"] == "SETTLED"
                        else ""
                    )
                    + alloc
                )
        embed = discord.Embed(title="IPO offerings")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        embed.set_footer(text="/ipo subscribe <ticker> <dollars> — pro-rata at the offer price")
        await interaction.response.send_message(
            content=_welcome_suffix(bootstrap) or None, embed=embed
        )

    @ipo_group.command(name="subscribe", description="Commit dollars to an open IPO window")
    @app_commands.describe(
        ticker="IPO ticker",
        dollars="Cash to commit — buys shares at the offer price, excess refunds",
    )
    async def ipo_subscribe(interaction: discord.Interaction, ticker: str, dollars: float) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            try:
                result = await ipo_svc.subscribe(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    amount_minor=int(Decimal(str(dollars)) * 100),
                    interaction_id=str(interaction.id),
                )
            except InsufficientFundsError:
                await interaction.response.send_message(
                    "Insufficient funds for that subscription.", ephemeral=True
                )
                return
            except (TradingError, ValueError, MarginError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Committed **{format_money(result.committed_minor)}** to the "
            f"**{result.ticker}** IPO — book total "
            f"{format_money(result.total_committed_minor)}. Allocation "
            "settles at the window close (pro-rata, excess refunded)." + _welcome_suffix(bootstrap)
        )

    tree.add_command(ipo_group)

    league_group = app_commands.Group(
        name="league",
        description="Season league: opt-in competitive track with an equal stake",
    )

    @league_group.command(name="info", description="Show the current or upcoming season")
    async def league_info(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            season = await get_open_season(conn)
            if season is None:
                await interaction.response.send_message(
                    "No league season is scheduled right now.", ephemeral=True
                )
                return
            tick = await current_tick(conn)
            entry = await get_active_entry(conn, interaction.user.id, season.id)
            equity = (
                await league_equity_minor(conn, season.id, interaction.user.id) if entry else None
            )

        embed = discord.Embed(title=f"League — {season.name}")
        embed.add_field(name="Status", value=season.status)
        embed.add_field(
            name="Window",
            value=(
                f"day {(max(tick - season.start_tick, 0)) // TICKS_PER_DAY + 1} of "
                f"{(season.end_tick - season.start_tick) // TICKS_PER_DAY}"
                if season.status == "ACTIVE"
                else f"starts in {(season.start_tick - tick) / TICKS_PER_DAY:.1f} day(s)"
            ),
        )
        embed.add_field(name="Entry fee", value=format_money(season.entry_fee_minor))
        embed.add_field(name="Stake", value=format_money(season.stake_minor))
        embed.add_field(
            name="You",
            value=(
                f"entered — equity {format_money(equity)}" if equity is not None else "not entered"
            ),
        )
        await interaction.response.send_message(embed=embed)

    @league_group.command(name="join", description="Enter the open league season")
    async def league_join(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            season = await get_open_season(conn)
            try:
                await join_season(conn, interaction.user.id)
            except InsufficientFundsError:
                fee = season.entry_fee_minor if season else 0
                await interaction.response.send_message(
                    f"Insufficient funds for the {format_money(fee)} entry fee.",
                    ephemeral=True,
                )
                return
            # MarginError: the entry fee is a gated discretionary spend.
            except (SeasonError, MarginError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        if season is None:
            # Rare race: the season was created between our read and
            # join_season's own lookup -- confirm the join anyway.
            await interaction.response.send_message(
                "Entered the league season." + _welcome_suffix(bootstrap)
            )
            return
        await interaction.response.send_message(
            f"Entered **{season.name}** — your league stake is "
            f"**{format_money(season.stake_minor)}**. Trade it with "
            "`/buy <ticker> quantity:<shares>` or `dollars:<$>` — add "
            "`league:True`." + _welcome_suffix(bootstrap)
        )

    @league_group.command(name="standings", description="Show league standings")
    async def league_standings(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            season = await get_latest_season(conn)
            if season is None:
                await interaction.response.send_message(
                    "No league season exists yet.", ephemeral=True
                )
                return
            rows = await standings(conn, season.id)

        if not rows:
            await interaction.response.send_message(
                f"**{season.name}** has no entrants yet.", ephemeral=True
            )
            return

        closed = season.status == "CLOSED"
        lines = []
        for i, s in enumerate(rows[:15], start=1):
            rank = s.final_rank if closed else i
            score = f"{s.final_score:>6.2f}" if s.final_score is not None else "    --"
            prize = f" +{format_money(s.prize_minor)}" if s.prize_minor else ""
            lines.append(
                f"{rank:>3}. <@{s.user_id}>  eq {format_money(s.equity_minor):>12}  "
                f"score {score}{prize}"
            )
        embed = discord.Embed(title=f"League standings — {season.name} ({season.status})")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)

    tree.add_command(league_group)

    sandbox_group = app_commands.Group(
        name="sandbox",
        description="Private practice portfolio — resets free, never touches net worth",
    )

    @sandbox_group.command(
        name="open", description="Open your sandbox account (needs Sandbox access)"
    )
    async def sandbox_open(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            if not await owns_item(conn, interaction.user.id, "sandbox_access"):
                await interaction.response.send_message(
                    "Sandbox requires **Sandbox access** — `/shop item:sandbox_access`.",
                    ephemeral=True,
                )
                return
            try:
                season_id = await open_sandbox(conn, interaction.user.id)
            except SandboxAlreadyOpenError:
                await interaction.response.send_message(
                    "Your sandbox is already running — `/sandbox status` or "
                    "`/sandbox reset` to start over.",
                    ephemeral=True,
                )
                return
            season = await get_season(conn, season_id)
        stake = season.stake_minor if season else 0
        await interaction.response.send_message(
            f"Sandbox open — **{format_money(stake)}** practice stake, "
            "quarantined from your real portfolio. Trade with "
            "`/buy` `/sell` `sandbox:True` and watch it with "
            "`/portfolio sandbox:True`." + _welcome_suffix(bootstrap)
        )

    @sandbox_group.command(name="status", description="Show your sandbox account")
    async def sandbox_status(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            entry = await get_sandbox_entry(conn, interaction.user.id)
            if entry is None:
                await interaction.response.send_message(
                    "No sandbox running — `/sandbox open` starts one "
                    "(needs Sandbox access from `/shop`).",
                    ephemeral=True,
                )
                return
            season_id, _account_id = entry
            season = await get_season(conn, season_id)
            equity = await league_equity_minor(conn, season_id, interaction.user.id)
            cash = await get_balance(conn, entry[1])
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*), COUNT(*) FILTER (WHERE p.quantity < 0)
                    FROM positions p
                    WHERE p.user_id = %s AND p.season_id = %s AND p.quantity <> 0
                    """,
                    (interaction.user.id, season_id),
                )
                row = await cur.fetchone()
                assert row is not None
                pos_count, short_count = int(row[0]), int(row[1])
            assert season is not None
            tick_age = max((await current_tick(conn)) - season.start_tick, 0)
        embed = discord.Embed(title="Sandbox")
        embed.add_field(name="Equity", value=format_money(equity))
        embed.add_field(name="Cash", value=format_money(cash))
        embed.add_field(
            name="Positions",
            value=(f"{pos_count} open" + (f" ({short_count} short)" if short_count else "")),
        )
        embed.set_footer(
            text=(
                f"Opened {tick_age} ticks ago · /sandbox reset starts over · "
                "sandbox trades count toward quests and volume but never net worth"
            )
        )
        await interaction.response.send_message(embed=embed)

    @sandbox_group.command(name="reset", description="Wipe your sandbox and start a fresh stake")
    async def sandbox_reset(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            if not await owns_item(conn, interaction.user.id, "sandbox_access"):
                await interaction.response.send_message(
                    "Sandbox requires **Sandbox access** — `/shop item:sandbox_access`.",
                    ephemeral=True,
                )
                return
            had = await get_sandbox_entry(conn, interaction.user.id)
            season_id = await reset_sandbox(conn, interaction.user.id)
            season = await get_season(conn, season_id)
        stake = season.stake_minor if season else 0
        await interaction.response.send_message(
            ("Sandbox reset — old positions and cash wiped, " if had else "Sandbox opened — ")
            + f"fresh **{format_money(stake)}** stake."
            + _welcome_suffix(bootstrap)
        )

    tree.add_command(sandbox_group)

    options_group = app_commands.Group(
        name="options",
        description="Cash-settled calls/puts vs the market maker",
    )

    async def _options_scope(
        conn: AsyncConnection,
        interaction: discord.Interaction,
        league: bool,
        sandbox: bool,
    ) -> int | None | str:
        """Three-way account resolution shared by /options subcommands:
        sandbox > league > main. Returns the season_id to scope to, or a
        user-facing error string (already-safe to send verbatim)."""
        if sandbox:
            entry = await get_sandbox_entry(conn, interaction.user.id)
            if entry is None:
                return "You don't have a sandbox running. `/sandbox open` first."
            return entry[0]
        if league:
            entry = await get_active_entry(conn, interaction.user.id)
            if entry is None:
                return "You're not entered in an active season. `/league join` first."
            return entry[0]
        return None

    @options_group.command(name="chain", description="Premium grid for an instrument's options")
    @app_commands.describe(ticker="Instrument ticker")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def options_chain(interaction: discord.Interaction, ticker: str) -> None:
        async with db.connection() as conn:
            try:
                chain = await option_chain(conn, ticker)
            except TradingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        spot = chain["spot"]
        strikes = chain["strikes"]
        days = EXPIRY_CHOICES_DAYS
        header = f"{'K':>8} " + " ".join(f"{f'{d}d prem':>8} {'Δ':>5}" for d in days)
        lines = [f"spot {format_price(spot)}", header]
        for side in ("CALL", "PUT"):
            lines.append(f"{side.lower()}s:")
            for k in strikes:
                row = f"{format_price(k):>8} "
                for d in days:
                    c = chain["cells"][side][(d, k)]
                    row += f"{format_price(c['premium']):>8} {c['delta']:>5.2f} "
                lines.append(row)
        embed = discord.Embed(title=f"Options chain — {chain['ticker']}")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @options_group.command(name="buy", description="Buy calls or puts from the market maker")
    @app_commands.describe(
        ticker="Instrument ticker",
        side="call or put",
        strike="Strike price in dollars",
        expiry="Days to expiry",
        quantity="Number of contracts",
        dollars="Spend ~this many dollars of premium instead of a count",
        league="Buy with your season league stake",
        sandbox="Buy with your sandbox stake",
    )
    @app_commands.choices(
        side=[
            app_commands.Choice(name="call", value="CALL"),
            app_commands.Choice(name="put", value="PUT"),
        ]
    )
    @app_commands.choices(
        expiry=[app_commands.Choice(name=f"{d}d", value=d) for d in EXPIRY_CHOICES_DAYS]
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def options_buy(
        interaction: discord.Interaction,
        ticker: str,
        side: str,
        strike: app_commands.Range[float, 0.0001, 999_999_999_999.0],
        expiry: int,
        quantity: app_commands.Range[int, 1, 1_000_000] | None = None,
        dollars: app_commands.Range[float, 0.01, 1_000_000_000.0] | None = None,
        league: bool = False,
        sandbox: bool = False,
    ) -> None:
        async with db.connection() as conn:
            bootstrap = await bootstrap_user(conn, interaction.user.id)
            scope = await _options_scope(conn, interaction, league, sandbox)
            if isinstance(scope, str):
                await interaction.response.send_message(scope, ephemeral=True)
                return
            if (quantity is not None) == (dollars is not None):
                await interaction.response.send_message(
                    "Give exactly one of `quantity` or `dollars`.", ephemeral=True
                )
                return
            qty = quantity
            try:
                if qty is None:
                    assert dollars is not None
                    premium_each = await quote_option_premium(
                        conn, ticker, side, Decimal(str(strike)), expiry
                    )
                    if premium_each <= 0:
                        await interaction.response.send_message(
                            "That contract prices at ~$0/share — try a nearer "
                            "strike or shorter expiry.",
                            ephemeral=True,
                        )
                        return
                    qty = int(Decimal(str(dollars)) * 100) // premium_each
                    if qty < 1:
                        await interaction.response.send_message(
                            f"One contract costs ~{format_money(premium_each)} — "
                            f"`dollars:{dollars}` can't cover one.",
                            ephemeral=True,
                        )
                        return
                result = await buy_option(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,
                    strike=Decimal(str(strike)),
                    expiry_days=expiry,
                    quantity=qty,
                    interaction_id=str(interaction.id),
                    season_id=scope,
                )
            except (TradingError, MarginError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        breakeven = (
            float(result.strike) + result.premium_minor / 100
            if result.side == "CALL"
            else float(result.strike) - result.premium_minor / 100
        )
        await interaction.response.send_message(
            f"Bought **{result.quantity:,}** {result.ticker} "
            f"{result.side.lower()} **{format_price(result.strike)}** "
            f"exp t{result.expiry_tick} — premium {format_price(result.premium_minor)}"
            f"/sh × {result.quantity:,} = **{format_money(result.total_cost_minor)}** "
            f"(fee {format_money(result.fee_minor)}). "
            f"Δ {result.delta:+.2f} · breakeven {format_price(breakeven)}"
            + _welcome_suffix(bootstrap)
        )

    @options_group.command(name="positions", description="Your open option positions")
    @app_commands.describe(
        league="Show league-scoped options",
        sandbox="Show sandbox-scoped options",
    )
    async def options_positions(
        interaction: discord.Interaction,
        league: bool = False,
        sandbox: bool = False,
    ) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            scope = await _options_scope(conn, interaction, league, sandbox)
            if isinstance(scope, str):
                await interaction.response.send_message(scope, ephemeral=True)
                return
            rows = await list_open_options(conn, interaction.user.id, scope)
            tick = await current_tick(conn)
        if not rows:
            await interaction.response.send_message(
                "No open options — `/options chain` shows the grid, `/options buy` opens one.",
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            days_left = max(0, int(r["expiry_tick"]) - tick) / TICKS_PER_DAY
            pnl = (int(r["mark_minor"]) - int(r["premium_minor"])) / int(r["premium_minor"])
            lines.append(
                f"#{r['id']:<4} {r['ticker']:<5} {r['side']:<4} "
                f"K={format_price(r['strike']):>9} {days_left:>5.1f}d "
                f"×{int(r['quantity']):>5}  "
                f"{format_money(r['mark_minor'])}/sh ({format_pct(pnl)})"
            )
        embed = discord.Embed(title=f"{interaction.user.display_name}'s options")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @options_group.command(name="sell", description="Sell an open option back to the market maker")
    @app_commands.describe(option="Open option position id")
    @app_commands.autocomplete(option=option_autocomplete)
    async def options_sell(interaction: discord.Interaction, option: int) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            try:
                result = await sell_option(conn, user_id=interaction.user.id, option_id=option)
            except (TradingError, MarginError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        pnl = result.payout_minor - result.premium_paid_minor
        await interaction.response.send_message(
            f"Sold **{result.quantity:,}** {result.ticker} "
            f"{result.side.lower()} #{result.option_id} for "
            f"**{format_money(result.payout_minor)}** "
            f"(paid {format_money(result.premium_paid_minor)}, "
            f"{format_money(abs(pnl))} {'gain' if pnl >= 0 else 'loss'})."
        )

    tree.add_command(options_group)

    # default_permissions hides the whole group from the slash picker
    # for non-admins (N4.5); _is_admin stays as the runtime gate --
    # Discord's permission filter is a display hint, not authorization.
    admin_group = app_commands.Group(
        name="admin",
        description="Bot admin tools",
        default_permissions=discord.Permissions(administrator=True),
    )

    def _is_admin(interaction: discord.Interaction) -> bool:
        return interaction.user.id in get_settings().admin_user_ids

    @admin_group.command(name="tune", description="Hot-tune an instrument's price engine parameter")
    @app_commands.describe(
        ticker="Instrument ticker",
        param=f"One of: {', '.join(TUNABLE_PARAMS)}",
        value="New value",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete, param=tunable_param_autocomplete)
    async def admin_tune(
        interaction: discord.Interaction, ticker: str, param: str, value: float
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                await tune_instrument(conn, ticker, param, value)
            except (ValueError, TradingError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Set {ticker.upper()}.{param} = {value}", ephemeral=True
        )

    @admin_group.command(name="ledger-audit", description="Run the ledger sum-zero invariant audit")
    async def admin_ledger_audit(interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            report = await ledger_audit(conn)

        status = "HEALTHY" if report.healthy else "\u26a0 UNHEALTHY"
        lines = [
            f"Status: {status}",
            f"Ledger sum (should be 0): {report.ledger_sum}",
            f"Negative user accounts (should be 0): {report.negative_user_accounts}",
            f"Accounts with balance != ledger sum (should be 0): {len(report.mismatched_accounts)}",
            "",
            "System account balances:",
        ]
        lines += [
            f"  {name:<16} {format_money(balance)}"
            for name, balance in report.system_balances.items()
        ]
        if report.mismatched_accounts:
            lines += [
                "",
                f"Drifted account ids: {report.mismatched_accounts[:20]}",
            ]
        await interaction.response.send_message(
            "```\n" + "\n".join(lines) + "\n```", ephemeral=True
        )

    @admin_group.command(
        name="wash-trades", description="Scan for and list flagged same-tick opposing trades"
    )
    async def admin_wash_trades(interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            flags = await scan_for_wash_trades(conn)
        if not flags:
            await interaction.response.send_message("No new flags.", ephemeral=True)
            return
        lines = [
            f"tick {f.tick_index}: instrument {f.instrument_id}, "
            f"buyer {f.buyer_id} / seller {f.seller_id}"
            for f in flags
        ]
        await interaction.response.send_message(
            "```\n" + "\n".join(lines) + "\n```", ephemeral=True
        )

    @admin_group.command(
        name="health",
        description="Service liveness, last tick, latest invariant audit, pool stats",
    )
    async def admin_health(interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT service, beat_at, detail FROM service_heartbeats ORDER BY service"
                )
                heartbeats = await cur.fetchall()
                await cur.execute(
                    "SELECT tick_index, ts, session_state, duration_ms, fills, "
                    "       crosses, knockouts, liquidations, events_resolved "
                    "FROM market_ticks ORDER BY tick_index DESC LIMIT 1"
                )
                last_tick = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT DISTINCT ON (check_name) check_name, ok, detail, created_at
                    FROM audit_results ORDER BY check_name, created_at DESC
                    """
                )
                audits = await cur.fetchall()

        lines = ["Heartbeats:"]
        now = datetime.now(UTC)
        seen = {hb["service"] for hb in heartbeats}
        for hb in heartbeats:
            age = (now - hb["beat_at"]).total_seconds()
            lines.append(f"  {hb['service']:<8} {age:>6.0f}s ago  {hb['detail']}")
        for service in ("market", "bot"):
            if service not in seen:
                lines.append(f"  {service:<8} never reported")
        lines.append("")
        if last_tick:
            tick_age = (now - last_tick["ts"]).total_seconds()
            lines.append(
                f"Last tick: {last_tick['tick_index']} "
                f"({last_tick['session_state']}, {tick_age:.0f}s ago) "
                f"{last_tick['duration_ms']}ms fills={last_tick['fills']} "
                f"crosses={last_tick['crosses']} kos={last_tick['knockouts']} "
                f"liqs={last_tick['liquidations']} ev={last_tick['events_resolved']}"
            )
        lines.append("")
        lines.append("Latest invariant audit:")
        if not audits:
            lines.append("  (no audit rows yet)")
        for a in audits:
            mark = "ok" if a["ok"] else "FAIL"
            lines.append(f"  [{mark}] {a['check_name']}: {a['detail']}")
        stats = db.pool_stats()
        if stats:
            lines.append("")
            lines.append(f"Pool: {stats}")
        await interaction.response.send_message(
            "```\n" + "\n".join(lines) + "\n```", ephemeral=True
        )

    @admin_group.command(
        name="config", description="Set a global config knob (bounded, allow-listed)"
    )
    @app_commands.describe(
        key=f"One of: {', '.join(TUNABLE_CONFIG_KEYS[:8])}… (see CONFIG_BOUNDS)",
        value="New value",
    )
    async def admin_config(interaction: discord.Interaction, key: str, value: float) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                await set_config(conn, key, value)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(f"Set config `{key}` = {value}", ephemeral=True)

    @admin_group.command(
        name="adjust",
        description="Ledger repair: credit (Faucet→user) or debit (user→Sink) an account",
    )
    @app_commands.describe(
        user="Discord user",
        amount="Signed amount, minor units (+ credits, - debits)",
        memo="Why this adjustment exists (required for the audit trail)",
    )
    async def admin_adjust_cmd(
        interaction: discord.Interaction,
        user: discord.User,
        amount: app_commands.Range[int, -9_000_000_000_000_000, 9_000_000_000_000_000],
        memo: str,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                await admin_adjust(
                    conn,
                    user_id=user.id,
                    amount=amount,
                    memo=memo,
                    admin_id=interaction.user.id,
                )
            except (ValueError, InsufficientFundsError, TradingError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Adjusted {user.display_name} by {format_money(amount)} (ledger `ADMIN_ADJUST`).",
            ephemeral=True,
        )

    @admin_group.command(
        name="order-cancel", description="Force-cancel any OPEN order (zombie repair)"
    )
    @app_commands.describe(order_id="Order id")
    @app_commands.autocomplete(order_id=admin_order_autocomplete)
    async def admin_order_cancel(interaction: discord.Interaction, order_id: int) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            cancelled = await admin_cancel_order(conn, order_id)
        msg = (
            f"Order {order_id} cancelled."
            if cancelled
            else (f"Order {order_id} is not OPEN (already filled/cancelled, or no such order).")
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @admin_group.command(
        name="disable",
        description="Suspend a user: blocks commands, cancels their open orders, DMs them",
    )
    @app_commands.describe(user="Discord user", reason="Why (shown to the user)")
    async def admin_disable(
        interaction: discord.Interaction, user: discord.User, reason: str
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                changed = await disable_user(conn, user.id, reason, interaction.user.id)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            (
                f"**{user.display_name}** suspended — open orders cancelled, "
                "DM enqueued. Their open positions still follow market "
                "mechanics (liquidation sweeps run as usual)."
            )
            if changed
            else f"**{user.display_name}** is already suspended.",
            ephemeral=True,
        )

    @admin_group.command(name="enable", description="Lift a user's suspension")
    @app_commands.describe(user="Discord user")
    async def admin_enable(interaction: discord.Interaction, user: discord.User) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            changed = await enable_user(conn, user.id)
        await interaction.response.send_message(
            (f"**{user.display_name}** re-enabled.")
            if changed
            else f"**{user.display_name}** isn't suspended.",
            ephemeral=True,
        )

    @admin_group.command(
        name="user-info", description="Moderation triage: balances, grants, flags, status"
    )
    @app_commands.describe(user="Discord user")
    async def admin_user_info(interaction: discord.Interaction, user: discord.User) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            info = await user_info(conn, user.id)
        if not info.exists:
            await interaction.response.send_message(
                f"**{user.display_name}** has no StockBot account "
                f"(Discord account age: {info.discord_age_days:.0f} days).",
                ephemeral=True,
            )
            return
        disabled = (
            f"suspended by {info.disabled_by}: {info.disabled_reason}"
            if info.disabled_at is not None
            else "active"
        )
        await interaction.response.send_message(
            f"**{user.display_name}** (`{user.id}`) — {disabled}\n"
            f"Discord age: {info.discord_age_days:.0f}d · "
            f"created {info.created_at:%Y-%m-%d} · "
            f"grant {'issued' if info.grant_issued else 'PENDING'}\n"
            f"balance {format_money(info.balance_minor or 0)} · "
            f"{info.open_positions} position(s) · {info.open_shorts} "
            f"short(s) · {info.open_orders} open order(s) · "
            f"**{info.wash_flags}** wash flag(s)",
            ephemeral=True,
        )

    @admin_group.command(
        name="recalc-balances",
        description="Rebuild accounts.balance from SUM(ledger_entries); reports drift fixed",
    )
    async def admin_recalc(interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                fixed = await recalc_balances(conn)
            except Exception as exc:
                # A rebuild that would push a USER/LEAGUE balance negative
                # fails on the CHECK -- that failure IS the signal.
                await interaction.response.send_message(f"Rebuild failed: {exc}", ephemeral=True)
                return
        await interaction.response.send_message(
            f"Recalculated balances: **{fixed}** account(s) had drifted and were repaired.",
            ephemeral=True,
        )

    @admin_group.command(name="season-create", description="Create a league season")
    @app_commands.describe(
        name="Season name",
        days="Length in days (default 42)",
        start_in_days="Days until the season opens for entries (default 0 = next tick)",
        entry_fee="Entry fee, minor units (default 1000 = $10)",
        stake="Equal league stake, minor units (default 100000 = $1,000)",
    )
    async def admin_season_create(
        interaction: discord.Interaction,
        name: str,
        days: app_commands.Range[int, 1, 365] = 42,
        start_in_days: app_commands.Range[int, 0, 365] = 0,
        entry_fee: app_commands.Range[int, 0, 9_000_000_000_000_000] = 1_000,
        stake: app_commands.Range[int, 1, 9_000_000_000_000_000] = 100_000,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            tick = await current_tick(conn)
            start = tick + start_in_days * TICKS_PER_DAY + 1
            season_id = await create_season(
                conn,
                name=name,
                start_tick=start,
                end_tick=start + days * TICKS_PER_DAY,
                entry_fee_minor=entry_fee,
                stake_minor=stake,
            )
        await interaction.response.send_message(
            f"Created season **{name}** (id {season_id}): starts tick {start}, "
            f"runs {days} day(s), fee {format_money(entry_fee)}, "
            f"stake {format_money(stake)}.",
            ephemeral=True,
        )

    @admin_group.command(
        name="season-close", description="Finalize a season now: score, rank, pay prizes"
    )
    async def admin_season_close(interaction: discord.Interaction, season_id: int) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                await close_season(conn, season_id)
            except SeasonError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(f"Season {season_id} closed.", ephemeral=True)

    @admin_group.command(
        name="instrument-add",
        description="List a new instrument (sector-median params; never joins SBX-40)",
    )
    @app_commands.describe(
        ticker="New ticker (<=10 chars, A-Z/0-9)",
        name="Display name",
        sector="Sector key, e.g. TECH",
        base_price="Listing price in dollars",
        sigma="Per-tick idiosyncratic vol (default: sector median)",
        beta="Market-factor loading (default: sector median)",
        gamma="Sector-factor loading (default: sector median)",
        liquidity="Notional depth for impact scaling (default: sector median)",
    )
    @app_commands.autocomplete(sector=sector_autocomplete)
    async def admin_instrument_add(
        interaction: discord.Interaction,
        ticker: str,
        name: str,
        sector: str,
        base_price: app_commands.Range[float, 0.0001, 1_000_000_000.0],
        sigma: float | None = None,
        beta: float | None = None,
        gamma: float | None = None,
        liquidity: float | None = None,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                instrument_id = await add_instrument(
                    conn,
                    ticker=ticker,
                    name=name,
                    sector_key=sector,
                    base_price=base_price,
                    sigma=sigma,
                    beta=beta,
                    gamma=gamma,
                    liquidity=liquidity,
                )
            except (ValueError, TradingError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Listed **{ticker.upper()}** ({name}) at {format_price(Decimal(str(base_price)))} "
            f"— instrument id {instrument_id}, active next tick.",
            ephemeral=True,
        )

    @admin_group.command(
        name="delist",
        description="Delist an instrument: settles all positions/orders/shorts at the final mark",
    )
    @app_commands.describe(ticker="Instrument ticker")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def admin_delist(interaction: discord.Interaction, ticker: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                report = await delist_instrument(conn, ticker)
            except (ValueError, TradingError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        lines = [
            f"**{report.ticker}** delisted at mark {format_price(report.mark_price)}"
            + (f" (tick {report.tick_index})" if report.tick_index is not None else ""),
            f"Long positions settled: {report.positions_settled}",
            f"Shorts covered: {report.shorts_covered}",
            f"Bounded shorts settled: {report.bounded_shorts_settled}",
            f"Open orders cancelled: {report.orders_cancelled}",
            f"Price alerts cancelled: {report.alerts_cancelled}",
            f"Pending events resolved: {report.events_resolved}",
        ]
        if report.fund_paid_minor or report.mm_absorbed_minor:
            lines.append(
                f"Cover shortfalls: fund paid {format_money(report.fund_paid_minor)}, "
                f"MM absorbed {format_money(report.mm_absorbed_minor)}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @admin_group.command(
        name="ipo-create",
        description="Create an IPO: lists dormant at the offer price, settles at close",
    )
    @app_commands.describe(
        ticker="Instrument ticker",
        name="Display name",
        sector="Sector key, e.g. TECH",
        offer_price="Offer price in dollars",
        shares="Shares offered (the float subscribers allocate pro-rata)",
        days="Subscription window length in days",
    )
    @app_commands.autocomplete(sector=sector_autocomplete)
    async def admin_ipo_create(
        interaction: discord.Interaction,
        ticker: str,
        name: str,
        sector: str,
        offer_price: app_commands.Range[float, 0.0001, 1_000_000_000.0],
        shares: app_commands.Range[int, 1, 1_000_000_000],
        days: app_commands.Range[float, 0.02, 30.0] = 1.0,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                offering_id = await ipo_svc.create_offering(
                    conn,
                    ticker=ticker,
                    name=name,
                    sector_key=sector,
                    offer_price=offer_price,
                    shares_offered=shares,
                    duration_ticks=int(days * TICKS_PER_DAY),
                )
            except (ValueError, TradingError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"IPO **{ticker.upper()}** created — {shares:,} shares at "
            f"{format_price(Decimal(str(offer_price)))}, offering #{offering_id}. "
            f"Window is open for ~{days} day(s); `/ipo subscribe`.",
            ephemeral=True,
        )

    @admin_group.command(
        name="npc-list",
        description="List NPC agents: archetype, status, equity, P&L vs stake",
    )
    async def admin_npc_list(interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            agents = await agent_report(conn)
        if not agents:
            await interaction.response.send_message("No NPC agents.", ephemeral=True)
            return
        alive = sum(1 for a in agents if a.died_at_tick is None)
        enabled = sum(1 for a in agents if a.enabled)
        total_pnl = sum(a.pnl_minor for a in agents)
        lines = [
            f"NPC census: {len(agents)} agents -- {enabled} enabled, "
            f"{alive} alive, {len(agents) - alive} dead | "
            f"aggregate P&L {format_money(total_pnl)}",
            "```",
            f"{'agent':<20}{'type':<18}{'state':<9}{'equity':>10}{'P&L':>10}{'inst':>5}{'age':>6}",
        ]
        now = discord.utils.utcnow()
        for a in agents:
            state = "dead" if a.died_at_tick is not None else ("enabled" if a.enabled else "off")
            age_hours = (now - a.created_at).total_seconds() / 3600
            age = f"{age_hours / 24:.0f}d" if age_hours >= 24 else f"{age_hours:.0f}h"
            lines.append(
                f"{a.label[:19]:<20}{a.archetype:<18}{state:<9}"
                f"{format_money(a.equity_minor):>10}{format_money(a.pnl_minor):>10}"
                f"{a.live_instruments:>5}{age:>6}"
            )
        lines.append("```")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    _npc_agent_desc = "Agent label or user_id (see /admin npc-list)"

    @admin_group.command(name="npc-enable", description="Enable one NPC agent")
    @app_commands.describe(agent=_npc_agent_desc)
    async def admin_npc_enable(interaction: discord.Interaction, agent: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            found = await set_agent_enabled(conn, label=agent, enabled=True)
        if found is None:
            await interaction.response.send_message(
                f"No live NPC agent matches `{agent}` (dead agents stay dead).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"NPC `{found.label}` ({found.archetype}) enabled.", ephemeral=True
        )

    @admin_group.command(
        name="npc-disable", description="Disable one NPC agent (positions stay open)"
    )
    @app_commands.describe(agent=_npc_agent_desc)
    async def admin_npc_disable(interaction: discord.Interaction, agent: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            found = await set_agent_enabled(conn, label=agent, enabled=False)
        if found is None:
            await interaction.response.send_message(
                f"No live NPC agent matches `{agent}`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"NPC `{found.label}` ({found.archetype}) disabled.", ephemeral=True
        )

    @admin_group.command(
        name="npc-spawn",
        description="Spawn NPC agents (one bounded stake each; funded count is capped)",
    )
    @app_commands.describe(
        archetype="Trading style",
        count="Agents to spawn (max 10 per call)",
        stake_dollars="Override per-agent stake in dollars (default npc.stake_minor)",
        ticker="Pin a ticker (LP quote target / stop_loss name)",
    )
    @app_commands.choices(
        archetype=[app_commands.Choice(name=k, value=k) for k in sorted(NPC_ARCHETYPES)]
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def admin_npc_spawn(
        interaction: discord.Interaction,
        archetype: str,
        count: app_commands.Range[int, 1, 10] = 1,
        stake_dollars: app_commands.Range[float, 100.0, 10_000_000.0] | None = None,
        ticker: str | None = None,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        stake_minor = int(Decimal(str(stake_dollars)) * 100) if stake_dollars is not None else None
        try:
            async with db.connection() as conn:
                async with conn.transaction():
                    spawned = [
                        await spawn_agent(
                            conn,
                            archetype,
                            quote_ticker=ticker.upper() if ticker else None,
                            stake_minor=stake_minor,
                        )
                        for _ in range(count)
                    ]
        except ValueError as exc:
            await interaction.response.send_message(
                f"Spawn refused: {exc}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Spawned {len(spawned)} `{archetype}` agent(s)"
            + (f" pinned to `{ticker.upper()}`" if ticker else "")
            + ".",
            ephemeral=True,
        )

    tree.add_command(admin_group)

    _instrument_commands(tree)


def _instrument_commands(tree: app_commands.CommandTree) -> None:
    """Wrap every registered callback with one structured log line
    (command, user, interaction id = correlation key) and a
    fire-and-forget `command_stats` row. Applied once at registration so
    command bodies stay oblivious."""
    for cmd in tree.get_commands():
        _instrument_one(cmd)


# H4: cheap in-memory per-user throttle against scripted command spam --
# one dict, no table. Discord's own cooldowns cover the expensive
# commands (/chart); this catches bursts across ALL commands.
_THROTTLE_RATE, _THROTTLE_WINDOW = 10, 10.0  # commands / seconds
_throttle: dict[int, deque[float]] = {}


def _instrument_one(cmd: Any) -> None:
    if isinstance(cmd, app_commands.Group):
        for sub in cmd.commands:
            _instrument_one(sub)
        return
    if not isinstance(cmd, app_commands.Command):
        return
    original = cmd.callback

    @functools.wraps(original)
    async def wrapper(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> Any:
        name = cmd.qualified_name
        started = time.perf_counter()
        ok, error = True, None
        try:
            # Cheap global throttle: N commands per window per user.
            hits = _throttle.setdefault(interaction.user.id, deque())
            now = time.monotonic()
            while hits and now - hits[0] > _THROTTLE_WINDOW:
                hits.popleft()
            if len(hits) >= _THROTTLE_RATE:
                ok, error = False, "throttled"
                await interaction.response.send_message(
                    "Slow down — too many commands. Try again in a few seconds.",
                    ephemeral=True,
                )
                return None
            hits.append(now)
            return await original(interaction, *args, **kwargs)
        except UserDisabledError as exc:
            # H2: the account-level chokepoint raised -- one ephemeral
            # for every command, no per-handler edits.
            ok, error = False, "UserDisabledError"
            notice = "This account is suspended."
            if exc.reason:
                notice += f" Reason: {exc.reason}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(notice, ephemeral=True)
                else:
                    await interaction.response.send_message(notice, ephemeral=True)
            except discord.HTTPException:
                pass
            return None
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:200]
            raise
        finally:
            ms = (time.perf_counter() - started) * 1000
            log.info(
                "cmd=%s user=%s iid=%s ok=%s ms=%.1f",
                name,
                interaction.user.id,
                interaction.id,
                ok,
                ms,
            )
            # Stats write is best-effort on its own connection -- it must
            # never surface to the user or delay the response.
            asyncio.get_running_loop().create_task(_record_command_stat(name, ok, ms, error))

    # Command.callback is a read-only property; _do_call invokes the
    # private backing attribute, which is what must be replaced.
    cmd._callback = wrapper  # noqa: SLF001


async def _record_command_stat(command: str, ok: bool, ms: float, error: str | None) -> None:
    try:
        async with db.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO command_stats (command, ok, duration_ms, error) "
                    "VALUES (%s, %s, %s, %s)",
                    (command, ok, round(ms, 3), error),
                )
    except Exception:
        log.debug("command_stats write failed for %s", command)
