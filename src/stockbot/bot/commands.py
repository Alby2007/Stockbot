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
from stockbot.bot.format import format_money, format_pct, format_price
from stockbot.claims.errors import AlreadyClaimedTodayError
from stockbot.claims.service import claim_daily
from stockbot.ledger.errors import InsufficientFundsError
from stockbot.ledger.service import get_balance
from stockbot.market.engine import TICKS_PER_DAY
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
        async with db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT i.ticker, s.key AS sector_key, i.quoted_price, i.circuit_halted_until_tick
                FROM instruments i
                JOIN sectors s ON s.id = i.sector_id
                WHERE i.is_active
                ORDER BY s.key, i.ticker
                """
            )
            rows = await cur.fetchall()

        lines = []
        for ticker, sector_key, quoted_price, halted_until in rows:
            marker = " (halted)" if halted_until is not None else ""
            lines.append(f"{ticker:<6} {sector_key:<11} {format_price(quoted_price):>12}{marker}")

        embed = discord.Embed(
            title="Market",
            description="```\n" + "\n".join(lines) + "\n```",
        )
        await interaction.response.send_message(embed=embed)

    @tree.command(name="stock", description="Show detail for one instrument")
    @app_commands.describe(ticker="Instrument ticker, e.g. NORT")
    async def stock(interaction: discord.Interaction, ticker: str) -> None:
        ticker = ticker.upper()
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.id, i.name, s.name AS sector_name, i.quoted_price,
                           i.impact, i.circuit_halted_until_tick
                    FROM instruments i
                    JOIN sectors s ON s.id = i.sector_id
                    WHERE i.ticker = %s AND i.is_active
                    """,
                    (ticker,),
                )
                instrument = await cur.fetchone()
            if instrument is None:
                await interaction.response.send_message(
                    f"No instrument found for `{ticker}`.", ephemeral=True
                )
                return
            instrument_id, name, sector_name, quoted_price, impact, halted_until = instrument

            async with conn.cursor() as cur:
                await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
                row = await cur.fetchone()
                current_tick = row[0] if row else None

            day_ago_close = None
            if current_tick is not None:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT close FROM candles
                        WHERE instrument_id = %s AND tick_index <= %s
                        ORDER BY tick_index DESC
                        LIMIT 1
                        """,
                        (instrument_id, max(current_tick - TICKS_PER_DAY, 0)),
                    )
                    row = await cur.fetchone()
                    if row:
                        day_ago_close = row[0]

        embed = discord.Embed(title=f"{ticker} \u2014 {name}")
        embed.add_field(name="Sector", value=sector_name)
        embed.add_field(name="Price", value=format_price(quoted_price))
        if day_ago_close:
            change = float(quoted_price) / float(day_ago_close) - 1
            embed.add_field(name="24h change", value=format_pct(change))
        embed.add_field(name="Impact", value=format_pct(float(impact)))
        if halted_until is not None:
            embed.add_field(name="Status", value="Halted (circuit breaker)")
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
