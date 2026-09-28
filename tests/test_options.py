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
        await cur.execute("UPDATE config SET value = 2 WHERE key = 'session.open_ticks'")
        await cur.execute("UPDATE config SET value = 3 WHERE key = 'session.closed_ticks'")
        await cur.execute("UPDATE config SET value = 0 WHERE key = 'session.phase_offset_ticks'")
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
