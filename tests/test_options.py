"""Options product (Phase O2): buy/sell/settle, caps, sandbox, delist."""

from decimal import Decimal

import pytest
from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.ledger.service import (
    get_balance,
    get_system_account_id,
    get_user_account_id,
)
from stockbot.margin.service import compute_health
from stockbot.market.tick import apply_tick
from stockbot.options.errors import OptionNotFoundError, StrikeOutOfBandError
from stockbot.options.service import (
    buy_option,
    list_open_options,
    sell_option,
    settle_expired_options,
)
from stockbot.seasons.service import get_sandbox_entry, open_sandbox
from stockbot.status.service import net_worth_minor
from stockbot.trading.errors import (
    DuplicateInteractionError,
    FeatureDisabledError,
    InsufficientDepthError,
)

SEED = "test-options-seed"


async def _buy(
    conn: AsyncConnection,
    user_id: int,
    *,
    side: str = "CALL",
    strike: Decimal = Decimal("60"),
    expiry_days: int = 7,
    quantity: int = 10,
    interaction_id: str | None = None,
    season_id: int | None = None,
):
    await bootstrap_user(conn, user_id)
    return await buy_option(
        conn,
        user_id=user_id,
        ticker="NORT",
        side=side,
        strike=strike,
        expiry_days=expiry_days,
        quantity=quantity,
        interaction_id=interaction_id,
        season_id=season_id,
    )


async def _mark(conn: AsyncConnection, ticker: str = "NORT") -> float:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT quoted_price FROM instruments WHERE ticker = %s", (ticker,)
        )
        row = await cur.fetchone()
    assert row is not None
    return float(row[0])


async def _set_mark(conn: AsyncConnection, price: float, ticker: str = "NORT") -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE instruments SET base_price = %s, quoted_price = %s, "
            "fundamental_value = %s WHERE ticker = %s",
            (price, price, price, ticker),
        )


async def test_buy_charges_premium_and_marks(conn: AsyncConnection) -> None:
    result = await _buy(conn, 8001)

    # Premium total (incl markup) left the account; MM received it.
    assert result.premium_minor > 0 and result.total_cost_minor > result.premium_minor
    rows = await list_open_options(conn, 8001)
    assert len(rows) == 1 and rows[0]["side"] == "CALL"
    # mark_minor is the fair value (no markup) -- always below what was paid.
    assert 0 < int(rows[0]["mark_minor"]) <= result.premium_minor
    assert int(rows[0]["expiry_tick"]) > 0


async def test_round_trip_loses_money(conn: AsyncConnection) -> None:
    """Buy -> immediate sell always loses: markup + markdown + fee (D3)."""
    bought = await _buy(conn, 8002)
    sold = await sell_option(conn, user_id=8002, option_id=bought.option_id)
    assert sold.payout_minor < bought.total_cost_minor
    # markdown 0.12 vs markup 0.10: ~20% round-trip edge at equal model marks
    assert sold.payout_minor < bought.premium_minor * bought.quantity


async def test_settle_itm_pays_full_intrinsic(conn: AsyncConnection) -> None:
    bought = await _buy(conn, 8003, strike=Decimal("60"))
    # Drive the mark to $80 -- deep ITM.
    await _set_mark(conn, 80.0)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE option_positions SET expiry_tick = 0 WHERE id = %s",
            (bought.option_id,),
        )
    n = await settle_expired_options(conn, 1)
    assert n == 1
    main = await get_user_account_id(conn, 8003)
    bal = await get_balance(conn, main)
    # $20/share intrinsic x 10 = $200 in minor units, paid in full.
    assert bal == 10_000 - bought.total_cost_minor + 20_000
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, settlement_minor FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        row = await cur.fetchone()
    assert row == ("SETTLED", 2000)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT kind FROM notifications WHERE user_id = 8003"
        )
        kinds = [r[0] for r in await cur.fetchall()]
    assert "OPTION_SETTLED" in kinds


async def test_settle_otm_pays_nothing(conn: AsyncConnection) -> None:
    bought = await _buy(conn, 8004, side="PUT", strike=Decimal("55"))
    await _set_mark(conn, 70.0)  # way OTM for a put
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE option_positions SET expiry_tick = 0 WHERE id = %s",
            (bought.option_id,),
        )
    n = await settle_expired_options(conn, 1)
    assert n == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, settlement_minor FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        assert (await cur.fetchone()) == ("SETTLED", 0)


async def test_settle_pays_when_mm_negative(conn: AsyncConnection) -> None:
    """C3: MARKET_MAKER is a SYSTEM account -- full intrinsic is paid even
    with the MM already negative, like the ADL backstop."""
    bought = await _buy(conn, 8005, strike=Decimal("60"))
    mm = await get_system_account_id(conn, "MARKET_MAKER")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE accounts SET balance = -5000000 WHERE id = %s", (mm,)
        )
    await _set_mark(conn, 80.0)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE option_positions SET expiry_tick = 0 WHERE id = %s",
            (bought.option_id,),
        )
    await settle_expired_options(conn, 1)
    main = await get_user_account_id(conn, 8005)
    # Grant minus premium plus the full $200 intrinsic, despite MM < 0.
    assert (
        await get_balance(conn, main)
        == 10_000 - bought.total_cost_minor + 20_000
    )
    async with conn.cursor() as cur:
        await cur.execute("SELECT balance FROM accounts WHERE id = %s", (mm,))
        assert (await cur.fetchone())[0] < -5000000  # went further negative


async def test_expiry_on_closed_tick_settles_at_pre_gap_mark(
    conn: AsyncConnection,
) -> None:
    """C5: an option expiring overnight settles on the CLOSED tick at the
    last open mark -- not after the overnight gap."""
    async with conn.cursor() as cur:
        # Venue session shape lives on the markets row post-0053.
        await cur.execute(
            "UPDATE markets SET open_ticks = 2, closed_ticks = 3, offset_ticks = 0"
        )
    await apply_tick(conn, SEED)  # tick 0: OPEN (open window is ticks 0-1)
    bought = await _buy(conn, 8006, strike=Decimal("60"))
    spot_at_close = await _mark(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE option_positions SET expiry_tick = 2 WHERE id = %s",
            (bought.option_id,),
        )
    await apply_tick(conn, SEED)  # tick 1: OPEN, not yet expired
    n = await apply_tick(conn, SEED)  # tick 2: CLOSED, expiry due
    assert n == 2
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, settlement_minor FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        row = await cur.fetchone()
    assert row is not None and row[0] == "SETTLED"
    # Settled at the frozen pre-gap mark from tick 1, whatever it was.
    assert float(row[1]) == pytest.approx(max(spot_at_close - 60.0, 0.0) * 100)


async def test_strike_band_rejects_junk(conn: AsyncConnection) -> None:
    await bootstrap_user(conn, 8007)
    with pytest.raises(StrikeOutOfBandError):
        await buy_option(
            conn, user_id=8007, ticker="NORT", side="CALL",
            strike=Decimal("10000"), expiry_days=7, quantity=1,
        )


async def test_oi_cap_rejects(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE instruments SET liquidity = 1 WHERE ticker = 'NORT'")
    with pytest.raises(InsufficientDepthError):
        await _buy(conn, 8008, quantity=10)


async def test_kill_switch_blocks_buy_not_sell(conn: AsyncConnection) -> None:
    bought = await _buy(conn, 8009)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = 0 WHERE key = 'options.enabled'")
    with pytest.raises(FeatureDisabledError):
        await _buy(conn, 8010)
    sold = await sell_option(conn, user_id=8009, option_id=bought.option_id)
    assert sold.payout_minor >= 0


async def test_idempotent_buy(conn: AsyncConnection) -> None:
    await _buy(conn, 8011, interaction_id="opt-1")
    with pytest.raises(DuplicateInteractionError):
        await _buy(conn, 8011, interaction_id="opt-1")


async def test_sell_rejects_foreign_option(conn: AsyncConnection) -> None:
    bought = await _buy(conn, 8012)
    with pytest.raises(OptionNotFoundError):
        await sell_option(conn, user_id=9999, option_id=bought.option_id)


async def test_options_in_net_worth_not_margin(conn: AsyncConnection) -> None:
    """C6: open option marks count toward leaderboard net worth but are
    deliberately absent from margin equity (not collateral)."""
    await bootstrap_user(conn, 8013)
    nw_before = await net_worth_minor(conn, 8013)
    assert nw_before == 10_000
    bought = await _buy(conn, 8013)
    nw_after = await net_worth_minor(conn, 8013)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT mark_minor * quantity FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        mark_total = int((await cur.fetchone())[0])
    # Net worth = cash after payment + the option's model mark.
    assert nw_after - nw_before == mark_total - bought.total_cost_minor
    # Margin equity excludes the option: only the cash debit shows.
    health = await compute_health(conn, 8013)
    assert health.equity_minor == nw_before - bought.total_cost_minor


async def test_sandbox_option_uses_sandbox_account(conn: AsyncConnection) -> None:
    """C4: the sandbox flag resolves to the sandbox LEAGUE account; the
    main portfolio never moves."""
    await bootstrap_user(conn, 8014)
    season_id = await open_sandbox(conn, 8014)
    main_before = await get_balance(conn, await get_user_account_id(conn, 8014))

    bought = await _buy(conn, 8014, season_id=season_id)
    entry = await get_sandbox_entry(conn, 8014)
    assert entry is not None
    # Premium came out of the sandbox account, not the main one.
    assert await get_balance(conn, entry[1]) == 10_000 - bought.total_cost_minor
    assert (
        await get_balance(conn, await get_user_account_id(conn, 8014))
        == main_before
    )
    # And it's season-scoped.
    rows = await list_open_options(conn, 8014, season_id)
    assert len(rows) == 1
    assert await list_open_options(conn, 8014, None) == []


async def test_delist_settles_open_options(conn: AsyncConnection) -> None:
    """Delisting pays intrinsic at the final mark -- open options are
    never stranded when an instrument dies."""
    from stockbot.admin.service import delist_instrument

    await _buy(conn, 8015, strike=Decimal("60"))
    await _set_mark(conn, 75.0)
    report = await delist_instrument(conn, "NORT")
    assert report.options_settled == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, settlement_minor FROM option_positions "
            "WHERE user_id = 8015"
        )
        assert (await cur.fetchone()) == ("SETTLED", 1500)
    main = await get_user_account_id(conn, 8015)
    # $15/share intrinsic x 10 back, on top of the $100 grant minus premium.
    assert await get_balance(conn, main) > 10_000


async def test_settle_is_replay_safe(conn: AsyncConnection) -> None:
    """Re-running the sweep after expiry is a no-op -- a duplicate tick
    replay can't double-pay."""
    bought = await _buy(conn, 8016, strike=Decimal("60"))
    await _set_mark(conn, 75.0)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE option_positions SET expiry_tick = 0 WHERE id = %s",
            (bought.option_id,),
        )
    assert await settle_expired_options(conn, 1) == 1
    assert await settle_expired_options(conn, 2) == 0
    main = await get_user_account_id(conn, 8016)
    bal = await get_balance(conn, main)
    assert await settle_expired_options(conn, 3) == 0
    assert await get_balance(conn, main) == bal


async def test_quote_returns_unfloored_premium(conn: AsyncConnection) -> None:
    """The quote must name the REAL computed premium -- flooring it at
    min_premium would quote a price buy_option then refuses to fill."""
    from stockbot.options.service import quote_option_premium

    await bootstrap_user(conn, 8017)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE config SET value = 50 WHERE key = 'options.min_premium_minor'"
        )
    # Deep OTM: model premium is a fraction of a minor unit.
    quote = await quote_option_premium(conn, "NORT", "CALL", Decimal("290"), 7)
    assert 0 <= quote < 50  # unfloored -- pre-fix this came back as 50
    with pytest.raises(ValueError, match="per-share minimum"):
        await buy_option(
            conn,
            user_id=8017,
            ticker="NORT",
            side="CALL",
            strike=Decimal("290"),
            expiry_days=7,
            quantity=1,
        )


async def test_opened_tick_set_before_first_market_tick(
    conn: AsyncConnection,
) -> None:
    """A buy before the market has ever ticked still records
    opened_tick=0 -- NULL broke expiry-age displays and backfills."""
    bought = await _buy(conn, 8018)
    rows = await list_open_options(conn, 8018)
    assert len(rows) == 1
    assert rows[0]["opened_tick"] == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT opened_tick FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        assert (await cur.fetchone())[0] == 0


async def test_sell_settlement_rounds_per_share(conn: AsyncConnection) -> None:
    """settlement_minor records the per-share proceeds rounded, not
    floored -- the stored value must match the paid amount / qty."""
    from decimal import ROUND_HALF_UP

    bought = await _buy(conn, 8019, quantity=3)
    sold = await sell_option(conn, user_id=8019, option_id=bought.option_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT settlement_minor FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        per_share = int((await cur.fetchone())[0])
    assert per_share == int(
        (Decimal(sold.payout_minor) / 3).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


async def test_league_options_count_toward_trades(conn: AsyncConnection) -> None:
    """MIN_TRADES scoring counts option opens AND sells -- a pure-options
    league player must be able to qualify."""
    from stockbot.seasons.service import create_season, join_season, on_tick

    sid = await create_season(
        conn,
        name="optleague",
        start_tick=0,
        end_tick=10_000,
        entry_fee_minor=0,
        stake_minor=50_000,
    )
    await on_tick(conn, 0)  # activate: SCHEDULED -> ACTIVE
    await bootstrap_user(conn, 8020)
    await join_season(conn, 8020, sid)
    bought = await _buy(conn, 8020, season_id=sid)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT trades_count FROM season_entries "
            "WHERE season_id = %s AND user_id = %s",
            (sid, 8020),
        )
        assert (await cur.fetchone())[0] == 1

    await sell_option(conn, user_id=8020, option_id=bought.option_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT trades_count FROM season_entries "
            "WHERE season_id = %s AND user_id = %s",
            (sid, 8020),
        )
        assert (await cur.fetchone())[0] == 2


async def test_season_close_settles_options_before_sweep(
    conn: AsyncConnection,
) -> None:
    """Options still open at close settle at intrinsic BEFORE the league
    balance sweeps to SINK -- otherwise the payout lands post-sweep in a
    dead account and strands there forever."""
    from stockbot.seasons.service import (
        close_season,
        create_season,
        join_season,
        on_tick,
    )

    sid = await create_season(
        conn,
        name="optsweep",
        start_tick=0,
        end_tick=10_000,
        entry_fee_minor=0,
        stake_minor=50_000,
    )
    await on_tick(conn, 0)
    await bootstrap_user(conn, 8021)
    await join_season(conn, 8021, sid)
    bought = await _buy(conn, 8021, strike=Decimal("60"), season_id=sid)
    await _set_mark(conn, 75.0)  # ITM -> $15/share intrinsic

    await close_season(conn, sid)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, settlement_minor FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        assert (await cur.fetchone()) == ("SETTLED", 1500)
        await cur.execute(
            "SELECT account_id FROM season_entries "
            "WHERE season_id = %s AND user_id = %s",
            (sid, 8021),
        )
        league_acct = int((await cur.fetchone())[0])
    # Intrinsic payout landed pre-sweep, then everything left to SINK.
    assert await get_balance(conn, league_acct) == 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT amount FROM ledger_entries WHERE account_id = %s "
            "AND reason = 'OPTION_SETTLEMENT'",
            (league_acct,),
        )
        assert (await cur.fetchone())[0] == 15_000  # $15/share x qty 10
    # The next global expiry sweep must NOT pay the dead account again.
    assert await settle_expired_options(conn, 10_000) == 0
    assert await get_balance(conn, league_acct) == 0


async def test_sandbox_close_settles_options_before_sweep(
    conn: AsyncConnection,
) -> None:
    """Same close-time settle for sandbox seasons (no scoring, same
    stake-sweep shape)."""
    from stockbot.seasons.service import close_season

    await bootstrap_user(conn, 8022)
    sid = await open_sandbox(conn, 8022)
    bought = await _buy(conn, 8022, strike=Decimal("60"), season_id=sid)
    await _set_mark(conn, 75.0)
    entry = await get_sandbox_entry(conn, 8022)
    assert entry is not None

    await close_season(conn, sid)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, settlement_minor FROM option_positions WHERE id = %s",
            (bought.option_id,),
        )
        assert (await cur.fetchone()) == ("SETTLED", 1500)
    assert await get_balance(conn, int(entry[1])) == 0
    assert await settle_expired_options(conn, 10_000) == 0
