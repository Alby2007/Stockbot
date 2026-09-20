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
from stockbot.market.data import (
    InstrumentSnapshot,
    all_instrument_snapshots,
    current_tick_index,
    get_instrument_snapshot,
)
from stockbot.market.engine import TICKS_PER_DAY
from stockbot.shop.errors import ShopError
from stockbot.shop.service import (
    BASE_SLOTS,
    buy_item,
    get_slot_count,
    get_user_entitlements,
    list_items,
    slot_price,
)
from stockbot.trading.errors import TradingError
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
        async with db.connection() as conn:
            snapshot = await get_instrument_snapshot(conn, ticker)
            if snapshot is None:
                await interaction.response.send_message(
                    f"No instrument found for `{ticker.upper()}`.", ephemeral=True
                )
                return
            buf = await render_candle_chart(conn, snapshot.id, snapshot.ticker, ticks)

        if buf is None:
            await interaction.response.send_message("No price history yet.", ephemeral=True)
            return

        filename = f"{snapshot.ticker}.png"
        file = discord.File(buf, filename=filename)
        embed = discord.Embed(title=f"{snapshot.ticker} \u2014 {snapshot.name}")
        embed.set_image(url=f"attachment://{filename}")
        await interaction.response.send_message(embed=embed, file=file)

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
    async def portfolio(interaction: discord.Interaction) -> None:
        async with db.connection() as conn:
            account_id = await bootstrap_user(conn, interaction.user.id)
            cash = await get_balance(conn, account_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.ticker, p.quantity, p.avg_cost, i.quoted_price
                    FROM positions p
                    JOIN instruments i ON i.id = p.instrument_id
                    WHERE p.user_id = %s AND p.quantity > 0
                    ORDER BY i.ticker
                    """,
                    (interaction.user.id,),
                )
                positions = await cur.fetchall()

        lines = [f"Cash: {format_money(cash)}"]
        holdings_value = Decimal(0)
        for ticker, quantity, avg_cost, quoted_price in positions:
            market_value = Decimal(quantity) * Decimal(quoted_price)
            holdings_value += market_value
            unrealized_pct = float(quoted_price) / float(avg_cost) - 1 if avg_cost else 0.0
            lines.append(
                f"{ticker:<6} {quantity:>6} @ {format_price(avg_cost):>10}  "
                f"now {format_price(quoted_price):>10}  {format_pct(unrealized_pct):>8}"
            )
        net_worth_minor = int(cash + holdings_value * 100)
        lines.append(f"\nNet worth: {format_money(net_worth_minor)}")

        embed = discord.Embed(title=f"{interaction.user.display_name}'s portfolio")
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        await interaction.response.send_message(embed=embed)

    @tree.command(name="buy", description="Buy shares of an instrument")
    @app_commands.describe(ticker="Instrument ticker", quantity="Number of shares")
    async def buy(
        interaction: discord.Interaction, ticker: str, quantity: app_commands.Range[int, 1, None]
    ) -> None:
        await _do_trade(interaction, ticker, "BUY", quantity)

    @tree.command(name="sell", description="Sell shares of an instrument")
    @app_commands.describe(ticker="Instrument ticker", quantity="Number of shares")
    async def sell(
        interaction: discord.Interaction, ticker: str, quantity: app_commands.Range[int, 1, None]
    ) -> None:
        await _do_trade(interaction, ticker, "SELL", quantity)

    async def _do_trade(
        interaction: discord.Interaction, ticker: str, side: str, quantity: int
    ) -> None:
        async with db.connection() as conn:
            await bootstrap_user(conn, interaction.user.id)
            try:
                result = await execute_trade(
                    conn,
                    user_id=interaction.user.id,
                    ticker=ticker,
                    side=side,  # type: ignore[arg-type]
                    quantity=quantity,
                    interaction_id=str(interaction.id),
                )
            except InsufficientFundsError:
                await interaction.response.send_message(
                    "Insufficient funds for that trade.", ephemeral=True
                )
                return
            except TradingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

        verb = "Bought" if side == "BUY" else "Sold"
        await interaction.response.send_message(
            f"{verb} **{result.quantity}** {result.ticker} @ {format_price(result.fill_price)} "
            f"(fee {format_money(result.fee_minor)})"
        )

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

        owned_keys = {row["item_key"] for row in owned}
        lines = []
        for item in items:
            if item.key == "slot":
                price = slot_price(slot_count - BASE_SLOTS)
                lines.append(f"{item.key:<15} {format_money(price):>10}  (you have {slot_count})")
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
                price = await buy_item(conn, interaction.user.id, item.lower())
            except InsufficientFundsError:
                await interaction.response.send_message(
                    "Insufficient funds for that purchase.", ephemeral=True
                )
                return
            except ShopError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Purchased **{item.lower()}** for {format_money(price)}."
        )

    tree.add_command(shop_group)

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
            "",
            "System account balances:",
        ]
        lines += [
            f"  {name:<16} {format_money(balance)}"
            for name, balance in report.system_balances.items()
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

    tree.add_command(admin_group)
