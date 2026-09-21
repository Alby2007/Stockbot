"""Slash commands: registered onto a client's `app_commands.CommandTree`.

Kept deliberately thin -- all the actual logic lives in the service modules
(`accounts`, `claims`, `trading`, `ledger`); commands just parse Discord
input, call a service inside one DB connection, and format the result.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import discord
from discord import app_commands

from stockbot import db
from stockbot.accounts.service import bootstrap_user
from stockbot.admin.service import TUNABLE_PARAMS, ledger_audit, tune_instrument
from stockbot.bot.charts import render_candle_chart
from stockbot.bot.format import format_money, format_pct, format_price
from stockbot.claims.errors import AlreadyClaimedTodayError
from stockbot.claims.service import claim_daily
from stockbot.compliance.wash_trade import scan_for_wash_trades
from stockbot.config import get_settings
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import get_balance
from stockbot.margin.errors import MarginError
from stockbot.margin.service import (
    check_and_liquidate,
    compute_health,
    list_liquidations,
    margin_tier,
)
from stockbot.market.data import (
    InstrumentSnapshot,
    all_instrument_snapshots,
    current_tick_index,
    get_instrument_snapshot,
)
from stockbot.market.engine import TICKS_PER_DAY
from stockbot.orders.service import cancel_order, list_open_orders, place_order
from stockbot.seasons.errors import SeasonError
from stockbot.seasons.service import (
    close_season,
    create_season,
    current_tick,
    get_active_entry,
    get_latest_season,
    get_open_season,
    join_season,
    league_equity_minor,
    standings,
)
from stockbot.shop.errors import ShopError
from stockbot.shop.service import (
    BASE_SLOTS,
    MARGIN_TIER_PRICES_MINOR,
    buy_item,
    get_slot_count,
    get_user_entitlements,
    list_items,
    slot_price,
)
from stockbot.shorts.service import (
    cover_bounded_short,
    list_open_shorts,
    open_bounded_short,
)
from stockbot.trading.errors import DuplicateInteractionError, TradingError
from stockbot.trading.service import execute_trade

log = logging.getLogger("stockbot.bot.commands")

MIN_DISCORD_ACCOUNT_AGE = timedelta(days=30)
MIN_BOT_ACCOUNT_AGE_BEFORE_FIRST_CLAIM = timedelta(hours=24)


def register_commands(tree: app_commands.CommandTree) -> None:
    @tree.command(name="balance", description="Check your cash balance")
    async def balance(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            account_id = await bootstrap_user(conn, interaction.user.id)
            cash = await get_balance(conn, account_id)
        await interaction.response.send_message(f"Cash balance: **{format_money(cash)}**")

    @tree.command(name="claim", description="Claim your daily faucet grant")
    async def claim(interaction: discord.Interaction) -> None:
        user = interaction.user
        account_age = datetime.now(UTC) - discord.utils.snowflake_time(user.id)
        if account_age < MIN_DISCORD_ACCOUNT_AGE:
            await interaction.response.send_message(
                "Your Discord account needs to be at least 30 days old to claim.",
                ephemeral=True,
            )
            return

        async with db.connection() as conn:
            await bootstrap_user(conn, user.id)
            async with conn.cursor() as cur:
                await cur.execute("SELECT created_at FROM users WHERE id = %s", (user.id,))
                row = await cur.fetchone()
                assert row is not None
                (created_at,) = row
                await cur.execute("SELECT 1 FROM claims WHERE user_id = %s", (user.id,))
                has_claimed_before = await cur.fetchone() is not None

            if (
                not has_claimed_before
                and (datetime.now(UTC) - created_at) < MIN_BOT_ACCOUNT_AGE_BEFORE_FIRST_CLAIM
            ):
                await interaction.response.send_message(
                    "New accounts need to wait 24h before their first claim.",
                    ephemeral=True,
                )
                return

            try:
                amount, streak = await claim_daily(conn, user.id)
            except AlreadyClaimedTodayError:
                await interaction.response.send_message(
                    "You already claimed today. Come back tomorrow!", ephemeral=True
                )
                return

        await interaction.response.send_message(
            f"Claimed **{format_money(amount)}**! Current streak: **{streak}** day(s)."
        )

    @tree.command(name="market", description="List all tradeable instruments")
    async def market(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            snapshots = await all_instrument_snapshots(conn)

        lines = []
        for s in sorted(snapshots, key=lambda s: (s.sector_key, s.ticker)):
            marker = " (halted)" if s.is_halted else ""
            change = format_pct(s.day_change_pct) if s.day_change_pct is not None else "   n/a"
            lines.append(
                f"{s.ticker:<6} {s.sector_key:<11} {format_price(s.quoted_price):>12} "
                f"{change:>9}{marker}"
            )

        embed = discord.Embed(
            title="Market",
            description="```\n" + "\n".join(lines) + "\n```",
        )
        await interaction.response.send_message(embed=embed)

    @tree.command(name="stock", description="Show detail for one instrument")
    @app_commands.describe(ticker="Instrument ticker, e.g. NORT")
    async def stock(interaction: discord.Interaction, ticker: str) -> None:
        async with db.connection() as conn:
            snapshot = await get_instrument_snapshot(conn, ticker)
        if snapshot is None:
            await interaction.response.send_message(
                f"No instrument found for `{ticker.upper()}`.", ephemeral=True
            )
            return

        embed = discord.Embed(title=f"{snapshot.ticker} \u2014 {snapshot.name}")
        embed.add_field(name="Sector", value=snapshot.sector_name)
        embed.add_field(name="Price", value=format_price(snapshot.quoted_price))
        if snapshot.day_change_pct is not None:
            embed.add_field(name="24h change", value=format_pct(snapshot.day_change_pct))
        embed.add_field(name="Impact", value=format_pct(float(snapshot.impact)))
        embed.add_field(name="24h volume", value=f"{snapshot.day_volume:,} shares")
        embed.add_field(
            name="Short interest",
            value=f"{float(snapshot.short_interest_pct) * 100:.1f}% of float",
        )
        if snapshot.is_halted:
            embed.add_field(name="Status", value="Halted (circuit breaker)")
        await interaction.response.send_message(embed=embed)

    @tree.command(name="chart", description="Show a price chart for an instrument")
    @app_commands.describe(
        ticker="Instrument ticker, e.g. NORT",
        ticks="How many recent ticks to show (default 240, ~4h)",
    )
    async def chart(
        interaction: discord.Interaction,
        ticker: str,
        ticks: app_commands.Range[int, 10, 1440] = 240,
    ) -> None:
        # Defer up front: the render is slow enough to risk blowing the 3s
        # response window on a cold pool.
        await interaction.response.defer()
        async with db.connection() as conn:
            snapshot = await get_instrument_snapshot(conn, ticker)
            if snapshot is None:
                await interaction.followup.send(
                    f"No instrument found for `{ticker.upper()}`.", ephemeral=True
                )
                return
            buf = await render_candle_chart(conn, snapshot.id, snapshot.ticker, ticks)

        if buf is None:
            await interaction.followup.send("No price history yet.", ephemeral=True)
            return

        filename = f"{snapshot.ticker}.png"
        file = discord.File(buf, filename=filename)
        embed = discord.Embed(title=f"{snapshot.ticker} \u2014 {snapshot.name}")
        embed.set_image(url=f"attachment://{filename}")
        await interaction.followup.send(embed=embed, file=file)

    @tree.command(name="movers", description="Show today's biggest gainers and losers")
    async def movers(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            snapshots = await all_instrument_snapshots(conn)
        ranked = sorted(
            (s for s in snapshots if s.day_change_pct is not None),
            key=lambda s: s.day_change_pct,  # type: ignore[arg-type,return-value]
        )
        if not ranked:
            await interaction.response.send_message(
                "Not enough price history yet.", ephemeral=True
            )
            return

        def _lines(items: list[InstrumentSnapshot]) -> str:
            return "\n".join(
                f"{s.ticker:<6} {format_price(s.quoted_price):>10} "
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

        by_sector: dict[str, list[InstrumentSnapshot]] = {}
        for s in snapshots:
            by_sector.setdefault(s.sector_key, []).append(s)

        lines = []
        for sector_key in sorted(by_sector):
            members = by_sector[sector_key]
            changes = [s.day_change_pct for s in members if s.day_change_pct is not None]
            avg_change = sum(changes) / len(changes) if changes else None
            change_str = format_pct(avg_change) if avg_change is not None else "   n/a"
            lines.append(f"{sector_key:<11} {members[0].sector_name:<16} {change_str:>9}")

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
                    SELECT i.ticker, e.resolve_tick
                    FROM events e
                    JOIN instruments i ON i.id = e.instrument_id
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

        lines = [
            f"{ticker:<6} in {(resolve_tick - current_tick) / TICKS_PER_DAY:>5.1f} day(s)"
            for ticker, resolve_tick in rows
        ]
        embed = discord.Embed(title="Earnings calendar")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @tree.command(name="news", description="Show pending news hints and recent resolutions")
    async def news(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            current_tick = await current_tick_index(conn) or 0
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, e.headline, e.resolve_tick
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
                    SELECT i.ticker, e.headline, e.magnitude
                    FROM events e
                    JOIN instruments i ON i.id = e.instrument_id
                    WHERE e.kind = 'NEWS' AND e.resolved AND e.resolve_tick > %s
                    ORDER BY e.resolve_tick DESC
                    LIMIT 5
                    """,
                    (current_tick - TICKS_PER_DAY,),
                )
                resolved = await cur.fetchall()

        embed = discord.Embed(title="News")
        if pending:
            lines = [
                f"[{ticker}] {headline} (effect lands in "
                f"{(resolve_tick - current_tick) / 60:.1f}h)"
                for ticker, headline, resolve_tick in pending
            ]
            embed.add_field(name="Pending", value="\n".join(lines)[:1024], inline=False)
        if resolved:
            lines = [
                f"[{ticker}] {headline} \u2014 landed {format_pct(float(magnitude))}"
                for ticker, headline, magnitude in resolved
            ]
            embed.add_field(name="Recently resolved", value="\n".join(lines)[:1024], inline=False)
        if not pending and not resolved:
            embed.description = "No news right now."
        await interaction.response.send_message(embed=embed)

    @tree.command(name="portfolio", description="Show your positions and net worth")
    @app_commands.describe(league="Show your league portfolio instead of your main one")
    async def portfolio(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
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
            else:
                account_id = await bootstrap_user(conn, interaction.user.id)
            cash = await get_balance(conn, account_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, p.quantity, p.avg_cost, i.quoted_price
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
        for ticker, quantity, avg_cost, quoted_price in positions:
            market_value = Decimal(quantity) * Decimal(quoted_price)
            holdings_value += market_value
            is_short = quantity < 0
            unrealized_pct = (
                1 - float(quoted_price) / float(avg_cost) if is_short
                else float(quoted_price) / float(avg_cost) - 1
            ) if avg_cost else 0.0
            tag = "SHORT " if is_short else ""
            lines.append(
                f"{tag}{ticker:<6} {quantity:>6} @ {format_price(avg_cost):>10}  "
                f"now {format_price(quoted_price):>10}  {format_pct(unrealized_pct):>8}"
            )
        net_worth_minor = int(cash + holdings_value * 100)
        lines.append(f"\nNet worth: {format_money(net_worth_minor)}")

        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s {'league' if league else ''} portfolio"
        )
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @tree.command(name="buy", description="Buy shares of an instrument")
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        league="Trade from your season league stake instead of your main portfolio",
    )
    async def buy(
        interaction: discord.Interaction,
        ticker: str,
        # A bounded quantity keeps absurd inputs from overflowing BIGINT
        # notional_minor into an uncaught NumericValueOutOfRange.
        quantity: app_commands.Range[int, 1, 1_000_000_000],
        league: bool = False,
    ) -> None:
        await _do_trade(interaction, ticker, "BUY", quantity, league)

    @tree.command(name="sell", description="Sell shares of an instrument")
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        league="Trade from your season league stake instead of your main portfolio",
    )
    async def sell(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000],
        league: bool = False,
    ) -> None:
        await _do_trade(interaction, ticker, "SELL", quantity, league)

    async def _do_trade(
        interaction: discord.Interaction, ticker: str, side: str, quantity: int, league: bool
    ) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
                result = await execute_trade(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,  # type: ignore[arg-type]
                    quantity=quantity,
                    interaction_id=str(interaction.id),
                    season_id=season_id,
                )
            except InsufficientFundsError:
                await interaction.response.send_message(
                    "Insufficient funds for that trade.", ephemeral=True
                )
                return
            except (TradingError, MarginError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            legs = await check_and_liquidate(conn, interaction.user.id, season_id)

        verb = "Bought" if side == "BUY" else "Sold"
        suffix = f" ({legs} liquidation leg(s) fired)" if legs else ""
        await interaction.response.send_message(
            f"{verb} **{result.quantity}** {result.ticker} @ {format_price(result.fill_price)} "
            f"(fee {format_money(result.fee_minor)}){suffix}"
        )

    @tree.command(
        name="short",
        description="Open a bounded short: capped downside, auto-knockout above entry",
    )
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares to short",
        league="Use your season league stake instead of your main portfolio",
    )
    async def short(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000],
        league: bool = False,
    ) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
                result = await open_bounded_short(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    quantity=quantity,
                    interaction_id=str(interaction.id),
                    season_id=season_id,
                )
            except InsufficientFundsError:
                await interaction.response.send_message(
                    "Insufficient funds for the collateral.", ephemeral=True
                )
                return
            except TradingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Opened bounded short **#{result.short_id}**: {result.quantity} {result.ticker} "
            f"@ {format_price(result.entry_price)} — collateral "
            f"{format_money(result.collateral_minor)}, knocks out at "
            f"{format_price(result.knockout_price)}."
        )

    @tree.command(name="shorts", description="List your open bounded shorts")
    @app_commands.describe(league="Show league shorts instead of main-portfolio ones")
    async def shorts(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            season_id: int | None = None
            if league:
                entry = await get_active_entry(conn, interaction.user.id)
                if entry is None:
                    await interaction.response.send_message(
                        "You're not entered in an active season.", ephemeral=True
                    )
                    return
                season_id = entry[0]
            else:
                await bootstrap_user(conn, interaction.user.id)
            rows = await list_open_shorts(conn, interaction.user.id, season_id)

        if not rows:
            await interaction.response.send_message(
                "No open bounded shorts.", ephemeral=True
            )
            return
        lines = []
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
        await interaction.response.send_message(embed=embed)

    @tree.command(name="cover", description="Close a bounded short at market")
    @app_commands.describe(short_id="Bounded short id from /shorts")
    async def cover(interaction: discord.Interaction, short_id: int) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
            f"{format_money(result.payout_minor)}."
        )

    @tree.command(name="margin", description="Show your margin account health")
    @app_commands.describe(league="Show your league account's margin instead")
    async def margin_cmd(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
        embed.add_field(
            name="Cash", value=format_money(health.cash_minor)
        )
        embed.add_field(
            name="Positions value", value=format_money(health.positions_value_minor)
        )
        embed.add_field(
            name="Maintenance req", value=format_money(health.maint_req_minor)
        )
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
        if health.undermargined:
            embed.set_footer(text="Below maintenance — liquidation may fire on the next tick.")
        await interaction.response.send_message(embed=embed)

    @tree.command(
        name="collateral",
        description="Show per-position margin requirements for your shorts",
    )
    @app_commands.describe(league="Show league positions instead")
    async def collateral(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
                           i.init_margin_pct, i.short_interest_pct, p.borrow_fees_accrued
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
            await interaction.response.send_message(
                "No open margin shorts.", ephemeral=True
            )
            return
        lines = []
        for r in rows:
            notional = -int(r[1]) * Decimal(r[2])
            fees = int(Decimal(r[6]).quantize(Decimal("1")))
            lines.append(
                f"{r[0]:<6} {r[1]:>6}  notional {format_price(notional):>10}  "
                f"maint {format_money(int(notional * Decimal(r[3]) * 100)):>9}  "
                f"SI {float(r[5]) * 100:.1f}%  fees {format_money(fees)}"
            )
        embed = discord.Embed(title="Collateral — open shorts")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @tree.command(name="liquidations", description="Show your recent liquidation events")
    async def liquidations(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            rows = await list_liquidations(conn, interaction.user.id)
        if not rows:
            await interaction.response.send_message("No liquidations.", ephemeral=True)
            return
        lines = [
            f"#{r['id']:<4} {r['ticker']:<6} {r['side']:<4} {r['quantity_closed']:>6} @ "
            f"{format_price(r['fill_price']):>10}  penalty {format_money(r['penalty_minor'])}"
            for r in rows
        ]
        embed = discord.Embed(title="Liquidations")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    order_group = app_commands.Group(
        name="order", description="Resting limit orders, matched once per tick"
    )

    async def _place_order(
        interaction: discord.Interaction,
        ticker: str,
        side: str,
        quantity: int,
        limit: float,
        hours: float | None,
        league: bool,
    ) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
                result = await place_order(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,  # type: ignore[arg-type]
                    quantity=quantity,
                    limit_price=Decimal(str(limit)),
                    season_id=season_id,
                    expires_in_ticks=(
                        None if hours is None else int(hours * TICKS_PER_DAY / 24)
                    ),
                    interaction_id=str(interaction.id),
                )
            except (TradingError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        expiry = (
            "GTC"
            if result.expires_tick is None
            else f"expires tick {result.expires_tick}"
        )
        await interaction.response.send_message(
            f"Order **#{result.order_id}** resting: {result.side} "
            f"**{result.quantity}** {result.ticker} @ {format_price(result.limit_price)} "
            f"({expiry}). Fills when the market reaches it."
        )

    @order_group.command(name="buy", description="Rest a limit buy")
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        limit="Max price you'll pay",
        hours="Auto-expire after this many hours (omit for GTC)",
        league="Place the order in your league portfolio",
    )
    async def order_buy(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000],
        # NUMERIC(18, 6) tops out just under 1e12; keep the bound inside it.
        limit: app_commands.Range[float, 0.0001, 999_999_999_999],
        hours: app_commands.Range[float, 0.02, 8760] | None = None,
        league: bool = False,
    ) -> None:
        await _place_order(interaction, ticker, "BUY", quantity, limit, hours, league)

    @order_group.command(
        name="sell",
        description="Rest a limit sell (past your holdings opens a short — needs margin)",
    )
    @app_commands.describe(
        ticker="Instrument ticker",
        quantity="Number of shares",
        limit="Min price you'll accept",
        hours="Auto-expire after this many hours (omit for GTC)",
        league="Place the order in your league portfolio",
    )
    async def order_sell(
        interaction: discord.Interaction,
        ticker: str,
        quantity: app_commands.Range[int, 1, 1_000_000_000],
        limit: app_commands.Range[float, 0.0001, 999_999_999_999],
        hours: app_commands.Range[float, 0.02, 8760] | None = None,
        league: bool = False,
    ) -> None:
        await _place_order(interaction, ticker, "SELL", quantity, limit, hours, league)

    @order_group.command(name="list", description="Show your resting orders")
    @app_commands.describe(league="Show league orders instead of main-portfolio ones")
    async def order_list(interaction: discord.Interaction, league: bool = False) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
            await interaction.response.send_message("No open orders.", ephemeral=True)
            return
        lines = [
            f"#{r['id']:<4} {r['side']:<4} {r['quantity'] - r['filled_quantity']:>5} "
            f"{r['ticker']:<6} "
            f"@ {format_price(r['limit_price']):>10} "
            f"(mark {format_price(r['quoted_price'])})"
            for r in rows
        ]
        embed = discord.Embed(title="Open orders")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @order_group.command(name="cancel", description="Cancel a resting order")
    @app_commands.describe(order_id="Order id from /order list")
    async def order_cancel(interaction: discord.Interaction, order_id: int) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            cancelled = await cancel_order(
                conn, user_id=interaction.user.id, order_id=order_id
            )
        if cancelled:
            await interaction.response.send_message(f"Cancelled order **#{order_id}**.")
        else:
            await interaction.response.send_message(
                f"No open order **#{order_id}** on your account.", ephemeral=True
            )

    tree.add_command(order_group)

    shop_group = app_commands.Group(
        name="shop", description="Portfolio slots, analyst tools, and cosmetics"
    )

    @shop_group.command(name="list", description="List items available in the shop")
    async def shop_list(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            items = await list_items(conn)
            slot_count = await get_slot_count(conn, interaction.user.id)
            owned = await get_user_entitlements(conn, interaction.user.id)

        owned_keys = {row["item_key"]: row["quantity"] for row in owned}
        lines = []
        for item in items:
            if item.key == "slot":
                price = slot_price(slot_count - BASE_SLOTS)
                lines.append(f"{item.key:<15} {format_money(price):>10}  (you have {slot_count})")
                continue
            if item.key == "margin_tier":
                tier = owned_keys.get("margin_tier", 0)
                if tier >= len(MARGIN_TIER_PRICES_MINOR):
                    lines.append(f"{item.key:<15}  max tier ({tier}) owned")
                else:
                    price = MARGIN_TIER_PRICES_MINOR[tier]
                    lines.append(
                        f"{item.key:<15} {format_money(price):>10}  "
                        f"(tier {tier} -> {tier + 1})"
                    )
                continue
            marker = " (owned)" if item.key in owned_keys and item.duration_days is None else ""
            recurring = f" every {item.duration_days}d" if item.duration_days else ""
            assert item.price_minor is not None
            lines.append(
                f"{item.key:<15} {format_money(item.price_minor):>10}{recurring}{marker}"
            )

        embed = discord.Embed(title="Shop")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        embed.set_footer(text="/shop buy <item>")
        await interaction.response.send_message(embed=embed)

    @shop_group.command(name="buy", description="Buy an item from the shop")
    @app_commands.describe(item="Item key, e.g. slot, analyst_tools, theme_sunrise")
    async def shop_buy(interaction: discord.Interaction, item: str) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            try:
                price = await buy_item(
                    conn,
                    interaction.user.id,
                    item.lower(),
                    interaction_id=str(interaction.id),
                )
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
            except (ShopError, MarginError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Purchased **{item.lower()}** for {format_money(price)}."
        )

    tree.add_command(shop_group)

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
                await league_equity_minor(conn, season.id, interaction.user.id)
                if entry
                else None
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
                f"entered — equity {format_money(equity)}"
                if equity is not None
                else "not entered"
            ),
        )
        await interaction.response.send_message(embed=embed)

    @league_group.command(name="join", description="Enter the open league season")
    async def league_join(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
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
            except SeasonError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        if season is None:
            return  # join_season already reported "no open season"
        await interaction.response.send_message(
            f"Entered **{season.name}** — your league stake is "
            f"**{format_money(season.stake_minor)}**. Trade it with "
            "`/buy <ticker> <qty> league:True`."
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

    admin_group = app_commands.Group(name="admin", description="Bot admin tools")

    def _is_admin(interaction: discord.Interaction) -> bool:
        return interaction.user.id in get_settings().admin_user_ids

    @admin_group.command(name="tune", description="Hot-tune an instrument's price engine parameter")
    @app_commands.describe(
        ticker="Instrument ticker",
        param=f"One of: {', '.join(TUNABLE_PARAMS)}",
        value="New value",
    )
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
            "Accounts with balance != ledger sum (should be 0): "
            f"{len(report.mismatched_accounts)}",
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
    async def admin_season_close(
        interaction: discord.Interaction, season_id: int
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with db.connection() as conn:
            try:
                await close_season(conn, season_id)
            except SeasonError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Season {season_id} closed.", ephemeral=True
        )

    tree.add_command(admin_group)
