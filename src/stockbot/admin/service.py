"""Admin tools: runtime instrument tuning and the ledger invariant audit.

Every tunable param lives in `instruments` and can be hot-adjusted here
without a deploy, per the design doc. `PARAM_BOUNDS` doubles as the
allow-list -- the column name is interpolated into SQL, so anything not on
this list is rejected before it ever reaches a query. The bounds matter as
much as the list: an out-of-range value doesn't fail at tune time, it fails
inside every subsequent tick or trade (`tau_ticks = 0` -> `exp(-dt/0)` in
`step_instrument`, `liquidity = 0` -> division by zero in
`apply_trade_impact`). The same bounds are enforced at the schema level by
migration 0013's `instruments_engine_params_sane` CHECK.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.alerts.service import cancel_for_instrument, cancel_for_user
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin.service import _fund_balance, _record_fund_flow
from stockbot.trading.errors import UnknownInstrumentError

# Inclusive (min, max) bounds per tunable parameter, mirrored by the
# instruments_engine_params_sane CHECK constraint -- keep them in sync.
# Finite bounds also reject NaN/Infinity, which numeric comparisons treat
# as greater than everything rather than rejecting.
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "drift": (-1.0, 1.0),
    "sigma": (0.0, 1.0),
    "beta": (-10.0, 10.0),
    "gamma": (-10.0, 10.0),
    "kappa": (0.0, 1.0),
    "fundamental_sigma": (0.0, 1.0),
    "liquidity": (0.01, 1e18),
    "lambda_impact": (0.0, 1e3),
    "tau_ticks": (0.01, 1e9),
    "max_impact": (0.0, 1.0),
    "short_knockout_pct": (0.0001, 0.9999),
    "init_margin_pct": (0.0001, 1.0),
    "maint_margin_pct": (0.0001, 1.0),
    "float_shares": (0.0, 9e18),
}

TUNABLE_PARAMS = tuple(PARAM_BOUNDS)


async def tune_instrument(conn: AsyncConnection, ticker: str, param: str, value: float) -> None:
    bounds = PARAM_BOUNDS.get(param)
    if bounds is None:
        raise ValueError(f"{param!r} is not a tunable parameter; choose from {TUNABLE_PARAMS}")
    if not math.isfinite(value):
        raise ValueError(f"{param} must be finite; got {value}")
    lo, hi = bounds
    if not lo <= value <= hi:
        raise ValueError(f"{param} must be within [{lo}, {hi}]; got {value}")
    ticker = ticker.upper()
    async with conn.transaction():
        async with conn.cursor() as cur:
            # `param` is validated against the allow-list above, so this
            # interpolation can't be used to inject arbitrary SQL.
            await cur.execute(
                f"UPDATE instruments SET {param} = %s WHERE ticker = %s",  # noqa: S608
                (value, ticker),
            )
            if cur.rowcount == 0:
                raise UnknownInstrumentError(ticker)


@dataclass(frozen=True)
class LedgerAuditReport:
    ledger_sum: int
    negative_user_accounts: int
    mismatched_accounts: list[int]
    system_balances: dict[str, int]

    @property
    def healthy(self) -> bool:
        return (
            self.ledger_sum == 0
            and self.negative_user_accounts == 0
            and not self.mismatched_accounts
        )


async def ledger_audit(conn: AsyncConnection) -> LedgerAuditReport:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        row = await cur.fetchone()
        assert row is not None
        ledger_sum = int(row[0])

        await cur.execute("SELECT COUNT(*) FROM accounts WHERE kind = 'USER' AND balance < 0")
        row = await cur.fetchone()
        assert row is not None
        negative_user_accounts = int(row[0])

        # The drift that can actually happen: accounts.balance is a
        # materialized cache over ledger_entries (post_transfer is the only
        # writer), so any divergence means a write bypassed the ledger --
        # which the two global checks above can't see.
        await cur.execute(
            """
            SELECT a.id
            FROM accounts a
            LEFT JOIN ledger_entries le ON le.account_id = a.id
            GROUP BY a.id
            HAVING a.balance <> COALESCE(SUM(le.amount), 0)
            ORDER BY a.id
            """
        )
        mismatched_accounts = [int(row[0]) for row in await cur.fetchall()]

        await cur.execute(
            "SELECT system_name, balance FROM accounts WHERE kind = 'SYSTEM' ORDER BY system_name"
        )
        system_balances = {name: int(balance) for name, balance in await cur.fetchall()}

    return LedgerAuditReport(
        ledger_sum=ledger_sum,
        negative_user_accounts=negative_user_accounts,
        mismatched_accounts=mismatched_accounts,
        system_balances=system_balances,
    )


# Allow-list + bounds for `config` keys, the same treatment PARAM_BOUNDS
# gives instrument columns. Config rows silently poison state at read
# time -- `session.open_ticks = 0` wedges every tick, a 0-divisor like
# `NULLIF(max_si, 0)` in the borrow-fee math fails *inside* a cover. The
# bounds are deliberately generous (these are ops knobs, not physics) but
# always finite and nonzero where a zero would divide.
CONFIG_BOUNDS: dict[str, tuple[float, float]] = {
    "accounts.min_discord_age_days": (0, 3650),
    "alerts.max_per_user": (1, 1e4),
    "audit.every_n_ticks": (1, 1e6),
    "claim.jackpot_pct": (0.0, 25.0),
    "claim.wheel_enabled": (0, 1),
    "cross.collar_pct": (0.0001, 1.0),
    "cross.trade_through_epsilon": (0.0, 1.0),
    "dividend.interval_ticks": (1, 1e9),
    "dividend.jitter_ticks": (0, 1e9),
    "dividend.max_yield": (0.0, 1.0),
    "dividend.min_yield": (0.0, 1.0),
    "dividend.payer_pct": (0.0, 100.0),  # percent of instruments, not a fraction
    "earnings.est_sigma": (0.0, 1.0),
    "event.halt_lead_ticks": (0.0, 120.0),
    "fee.maker_rebate_bps": (0.0, 1e4),
    "fee.tier1_bps": (0.0, 1e4),
    "fee.tier1_volume": (0.0, 1e15),
    "fee.tier2_bps": (0.0, 1e4),
    "fee.tier2_volume": (0.0, 1e15),
    "impact.participation_cap": (1e-6, 1.0),
    "ipo.short_lockout_ticks": (0, 1e6),
    "margin.borrow_fee_bps_per_tick": (0.0, 1e6),
    "margin.borrow_util_k": (0.0, 1e6),
    "margin.liquidation_penalty_bps": (0.0, 1e6),
    "margin.liquidation_target_ratio": (0.0, 1e6),
    "margin.max_gross_leverage": (0.0, 1e6),
    "margin.max_short_interest_pct": (0.0, 1e18),
    "margin.recall_fraction_per_tick": (0.0, 1.0),
    "margin.recall_si_pct": (0.0, 1e18),
    "margin.squeeze_lambda_boost": (0.0, 1e6),
    "margin.squeeze_si_threshold": (0.0, 1e18),
    "margin.warn_ratio": (0.0, 1e6),
    "order.max_fill_failures": (1, 1e6),
    "order.stop_cascade_max_iters": (1, 1e4),
    "options.enabled": (0, 1),
    "options.hedge_frac": (0.0, 4.0),
    "options.max_oi_frac": (0.0, 10.0),
    "options.min_premium_minor": (0.0, 1e6),
    "options.premium_markup": (0.0, 10.0),
    "options.sell_markdown": (0.0, 1.0),
    "options.strike_max_frac": (1.0, 100.0),
    "options.strike_min_frac": (0.01, 1.0),
    "orders.enabled": (0, 1),
    "quests.daily_count": (0, 10),
    "quests.enabled": (0, 1),
    "quests.weekly_count": (0, 10),
    "session.auction_ticks": (0, 240),
    "session.closed_ticks": (0, 1440),
    "session.open_impact_reset": (0.0, 1.0),
    "session.open_ticks": (0, 1440),
    "session.overnight_var_frac": (0.0, 1.0),
    "session.phase_offset_ticks": (0, 1440),
    "shorts.enabled": (0, 1),
    "shorts.max_knockout_pct": (0.0, 0.9999),
    "shorts.min_knockout_pct": (0.0, 0.9999),
    "spread.base_bps": (0.0, 1e4),
    "spread.event_coeff": (0.0, 1e6),
    "spread.event_window_ticks": (0, 1e9),
    "spread.halt_coeff": (0.0, 1e6),
    "spread.halt_decay_ticks": (0, 1e9),
    "spread.inv_liquidity_coeff": (0.0, 1e18),
    "spread.liquidity_ref": (0.01, 1e18),
    "spread.close_coeff": (0.0, 1e3),
    "spread.close_decay_ticks": (1.0, 1e6),
    "spread.open_coeff": (0.0, 1e3),
    "spread.open_decay_ticks": (1.0, 1e6),
    "spread.sigma_coeff": (0.0, 1e6),
    "spread.sigma_ref": (1e-9, 1e6),
    "spread.tick_min": (1e-6, 1e6),
    "spread.tick_pct": (0.0, 1.0),
    "flow.adv_mult_max": (0.0, 1e6),
    "flow.adv_mult_min": (0.0, 1e6),
    "flow.adv_ref_frac": (0.0, 1.0),
    "flow.adv_window_ticks": (1.0, 1e9),
    "flow.cross_impact_coeff": (0.0, 1.0),
    "flow.max_fundamental_move": (0.0, 1.0),
    "flow.permanent_frac": (0.0, 1.0),
    "flow.skew_coeff": (0.0, 10.0),
    "flow.skew_decay": (0.0, 0.9999),
    "flow.skew_max": (0.0, 100.0),
    "flow.skew_norm": (1e-9, 1.0),
    "flow.vol_liq_coeff": (0.0, 10.0),
    "mom.innov_frac": (0.0, 10.0),
    "mom.max_frac": (0.0, 10.0),
    "mom.rho": (0.0, 0.999999),
    "news.fizzle_pct": (0.0, 0.9),
    "trading.enabled": (0, 1),
    "vol.account_flow_cap": (0.0, 1.0),
    "vol.clip_max": (0.01, 100.0),
    "vol.clip_min": (0.01, 10.0),
    "vol.flow_halt_ticks": (0, 1e4),
    "vol.flow_ret_cap": (0.0, 1.0),
    "vol.market_weight": (0.0, 1.0),
    "vol.rho": (0.0, 0.9999),
}

TUNABLE_CONFIG_KEYS = tuple(CONFIG_BOUNDS)


async def set_config(conn: AsyncConnection, key: str, value: float) -> None:
    bounds = CONFIG_BOUNDS.get(key)
    if bounds is None:
        raise ValueError(
            f"{key!r} is not a known config key; choose from {TUNABLE_CONFIG_KEYS}"
        )
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite; got {value}")
    lo, hi = bounds
    if not lo <= value <= hi:
        raise ValueError(f"{key} must be within [{lo}, {hi}]; got {value}")
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = %s WHERE key = %s", (value, key))
        if cur.rowcount == 0:
            raise ValueError(f"config key {key!r} has no row; was the DB migrated?")


async def admin_adjust(
    conn: AsyncConnection,
    *,
    user_id: int,
    amount: int,
    memo: str,
    admin_id: int,
) -> None:
    """Ledger-respecting balance repair: a credit is a FAUCET->user
    transfer, a debit is user->SINK. Never UPDATE accounts.balance -- the
    ledger stays the only writer, so the adjustment is itself an
    explainable entry (`reason='ADMIN_ADJUST'`, memo carries the admin)."""
    if amount == 0:
        raise ValueError("adjustment amount must be nonzero")
    account_id = await get_user_account_id(conn, user_id)
    full_memo = f"admin {admin_id}: {memo}"
    if amount > 0:
        await post_transfer(
            conn,
            from_account_id=await get_system_account_id(conn, "FAUCET"),
            to_account_id=account_id,
            amount=amount,
            reason="ADMIN_ADJUST",
            memo=full_memo,
        )
    else:
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=await get_system_account_id(conn, "SINK"),
            amount=-amount,
            reason="ADMIN_ADJUST",
            memo=full_memo,
        )


async def admin_cancel_order(conn: AsyncConnection, order_id: int) -> bool:
    """Force-cancel any OPEN order regardless of owner -- the zombie-order
    repair path. Returns False if the order isn't OPEN."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE orders SET status = 'CANCELLED' WHERE id = %s AND status = 'OPEN'",
            (order_id,),
        )
        return cur.rowcount > 0


async def disable_user(
    conn: AsyncConnection, user_id: int, reason: str, admin_id: int
) -> bool:
    """Suspend a user: bootstrap_user raises UserDisabledError from now on,
    which blocks every user-initiated money path (trades, orders, claims,
    shop, league join). One transaction:

    - set disabled_at/reason/by (idempotent -- already-disabled is a no-op
      returning False)
    - cancel ALL their OPEN orders (fills never consult bootstrap, so
      without this a suspended user's resting orders would keep trading
      forever -- the gap the audit flagged)
    - enqueue an ACCOUNT_SUSPENDED outbox row so the DM explains itself

    Positions are deliberately untouched: the liquidation engine still
    manages them -- freezing those would be strictly worse.
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE users
            SET disabled_at = now(), disabled_reason = %s, disabled_by = %s
            WHERE id = %s AND disabled_at IS NULL
            """,
            (reason, admin_id, user_id),
        )
        if cur.rowcount == 0:
            await cur.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
            if await cur.fetchone() is None:
                raise ValueError(f"user {user_id} has no StockBot account")
            return False
        await cur.execute(
            "UPDATE orders SET status = 'CANCELLED' "
            "WHERE user_id = %s AND status = 'OPEN'",
            (user_id,),
        )
        cancelled = cur.rowcount
        # Same gap as resting orders: a suspended user's open alerts would
        # keep firing DMs forever -- the sweep never consults bootstrap.
        await cancel_for_user(conn, user_id)
        await cur.execute(
            """
            INSERT INTO notifications (user_id, kind, payload)
            VALUES (%s, 'ACCOUNT_SUSPENDED', %s)
            """,
            (
                user_id,
                json.dumps(
                    {"reason": reason, "cancelled_orders": cancelled}
                ),
            ),
        )
    return True


async def enable_user(conn: AsyncConnection, user_id: int) -> bool:
    """Clear a suspension. Cancelled orders stay cancelled -- the user
    can re-place them; un-cancelling would resurrect stale intents."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE users
            SET disabled_at = NULL, disabled_reason = NULL, disabled_by = NULL
            WHERE id = %s AND disabled_at IS NOT NULL
            """,
            (user_id,),
        )
        return cur.rowcount > 0


@dataclass(frozen=True)
class UserInfo:
    exists: bool
    balance_minor: int | None
    created_at: object | None
    grant_issued: bool | None
    discord_age_days: float
    open_orders: int
    open_positions: int
    open_shorts: int
    wash_flags: int
    disabled_at: object | None
    disabled_reason: str | None
    disabled_by: int | None


async def user_info(conn: AsyncConnection, user_id: int) -> UserInfo:
    """Moderation triage for /admin user-info: everything you'd check the
    first time wash-trades flags someone."""
    from stockbot.accounts.service import discord_age_days

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT u.created_at, u.grant_issued, u.disabled_at,
                   u.disabled_reason, u.disabled_by, a.balance
            FROM users u
            LEFT JOIN accounts a ON a.user_id = u.id AND a.kind = 'USER'
            WHERE u.id = %s
            """,
            (user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return UserInfo(
                exists=False,
                balance_minor=None,
                created_at=None,
                grant_issued=None,
                discord_age_days=discord_age_days(user_id),
                open_orders=0,
                open_positions=0,
                open_shorts=0,
                wash_flags=0,
                disabled_at=None,
                disabled_reason=None,
                disabled_by=None,
            )
        created_at, grant_issued, disabled_at, reason, by, balance = row
        await cur.execute(
            "SELECT count(*) FROM orders WHERE user_id = %s AND status = 'OPEN'",
            (user_id,),
        )
        open_orders_row = await cur.fetchone()
        assert open_orders_row is not None
        (open_orders,) = open_orders_row
        await cur.execute(
            "SELECT count(*) FROM positions WHERE user_id = %s AND quantity <> 0",
            (user_id,),
        )
        open_positions_row = await cur.fetchone()
        assert open_positions_row is not None
        (open_positions,) = open_positions_row
        await cur.execute(
            "SELECT count(*) FROM bounded_shorts WHERE user_id = %s AND status = 'OPEN'",
            (user_id,),
        )
        open_shorts_row = await cur.fetchone()
        assert open_shorts_row is not None
        (open_shorts,) = open_shorts_row
        await cur.execute(
            "SELECT count(*) FROM wash_trade_flags "
            "WHERE buyer_id = %s OR seller_id = %s",
            (user_id, user_id),
        )
        wash_flags_row = await cur.fetchone()
        assert wash_flags_row is not None
        (wash_flags,) = wash_flags_row
    return UserInfo(
        exists=True,
        balance_minor=int(balance) if balance is not None else 0,
        created_at=created_at,
        grant_issued=bool(grant_issued),
        discord_age_days=discord_age_days(user_id),
        open_orders=int(open_orders),
        open_positions=int(open_positions),
        open_shorts=int(open_shorts),
        wash_flags=int(wash_flags),
        disabled_at=disabled_at,
        disabled_reason=reason,
        disabled_by=int(by) if by is not None else None,
    )


async def recalc_balances(conn: AsyncConnection) -> int:
    """Rebuild the accounts.balance cache from SUM(ledger_entries) and
    return how many rows drifted. Deterministic and self-verifying: the
    ledger is the source of truth, so recomputing can only restore, never
    corrupt -- and the USER/LEAGUE non-negativity CHECK means a rebuild
    that would go negative fails loudly instead of masking real debt."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE accounts a
            SET balance = s.total
            FROM (
                SELECT a2.id, COALESCE(SUM(le.amount), 0) AS total
                FROM accounts a2
                LEFT JOIN ledger_entries le ON le.account_id = a2.id
                GROUP BY a2.id
            ) s
            WHERE a.id = s.id AND a.balance <> s.total
            """
        )
        return cur.rowcount


# --- Instrument lifecycle (Plan F) -----------------------------------------

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}$")

# Params pulled from sector medians when not given at list time. Interpolated
# into _sector_medians' SQL, so this tuple doubles as the allow-list there.
_MEDIAN_COLUMNS = (
    "drift",
    "sigma",
    "beta",
    "gamma",
    "kappa",
    "fundamental_sigma",
    "liquidity",
    "lambda_impact",
    "tau_ticks",
    "max_impact",
    "init_margin_pct",
    "maint_margin_pct",
    "short_knockout_pct",
    "float_shares",
)

# Params an admin can override at list time (validated against PARAM_BOUNDS).
_LISTABLE_OVERRIDES = ("sigma", "beta", "gamma", "liquidity")


async def _sector_medians(conn: AsyncConnection, sector_id: int) -> dict[str, float]:
    """Per-column medians over the sector's live STOCK rows, falling back to
    the whole market if the sector is empty (fresh sector, or everything in
    it delisted). Column names come from _MEDIAN_COLUMNS, not user input."""
    cols = ", ".join(
        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {c}) AS {c}"
        for c in _MEDIAN_COLUMNS
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {cols} FROM instruments "  # noqa: S608
            "WHERE kind = 'STOCK' AND is_active AND sector_id = %s",
            (sector_id,),
        )
        row = await cur.fetchone()
        if row is None or row["sigma"] is None:
            await cur.execute(
                f"SELECT {cols} FROM instruments "  # noqa: S608
                "WHERE kind = 'STOCK' AND is_active"
            )
            row = await cur.fetchone()
    assert row is not None and row["sigma"] is not None, "no listed instruments to copy"
    return {k: float(v) for k, v in row.items()}


async def add_instrument(
    conn: AsyncConnection,
    *,
    ticker: str,
    name: str,
    sector_key: str,
    base_price: float,
    sigma: float | None = None,
    beta: float | None = None,
    gamma: float | None = None,
    liquidity: float | None = None,
    market_code: str | None = None,
) -> int:
    """List a new instrument. Unspecified engine params default to the
    sector's medians (market-wide for an empty sector). New listings never
    join the SBX-40 basket -- index_member stays FALSE (0031). Candles and
    the earnings/dividend schedulers pick it up on the next open tick."""
    ticker = ticker.strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise ValueError(f"ticker {ticker!r} must be 1-10 chars of A-Z/0-9")
    if not name.strip():
        raise ValueError("name is required")
    if not math.isfinite(base_price) or base_price <= 0:
        raise ValueError(f"base_price must be positive and finite; got {base_price}")
    overrides = {"sigma": sigma, "beta": beta, "gamma": gamma, "liquidity": liquidity}
    params: dict[str, float] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        lo, hi = PARAM_BOUNDS[key]
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{key} must be within [{lo}, {hi}]; got {value}")
        params[key] = value

    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, key FROM sectors WHERE upper(key) = upper(%s)",
                (sector_key.strip(),),
            )
            sector = await cur.fetchone()
        if sector is None:
            raise ValueError(f"unknown sector {sector_key!r}")
        if sector["key"] == "index":
            raise ValueError("cannot list into the index sector")
        sector_id = int(sector["id"])

        async with conn.cursor() as cur:
            if market_code is None:
                await cur.execute(
                    "SELECT id FROM markets ORDER BY id LIMIT 1"
                )
            else:
                await cur.execute(
                    "SELECT id FROM markets WHERE upper(code) = upper(%s)",
                    (market_code.strip(),),
                )
            mkt_row = await cur.fetchone()
        if mkt_row is None:
            raise ValueError(f"unknown market {market_code!r}")
        market_id = int(mkt_row[0])

        defaults = await _sector_medians(conn, sector_id)
        defaults.update(params)
        p = defaults
        # Warm-start adv at the neutral reference (adv_mult = 1.0): a
        # zero-adv listing would open at the adv_mult_min floor (~half
        # depth) until the sliding window filled.
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT value FROM config WHERE key = 'flow.adv_ref_frac'"
            )
            ref_row = await cur.fetchone()
        adv_seed = p["liquidity"] * (float(ref_row[0]) if ref_row else 2.5e-8)
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO instruments (
                        ticker, name, sector_id, market_id, kind,
                        drift, sigma, beta, gamma, kappa, fundamental_sigma,
                        liquidity, lambda_impact, tau_ticks, max_impact,
                        fundamental_value, base_price, impact, quoted_price,
                        init_margin_pct, maint_margin_pct, float_shares,
                        index_member, short_knockout_pct, adv
                    ) VALUES (
                        %s, %s, %s, %s, 'STOCK',
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, 0, %s,
                        %s, %s, %s,
                        FALSE, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        ticker,
                        name.strip(),
                        sector_id,
                        market_id,
                        p["drift"],
                        p["sigma"],
                        p["beta"],
                        p["gamma"],
                        p["kappa"],
                        p["fundamental_sigma"],
                        p["liquidity"],
                        p["lambda_impact"],
                        p["tau_ticks"],
                        p["max_impact"],
                        base_price,
                        base_price,
                        base_price,
                        p["init_margin_pct"],
                        p["maint_margin_pct"],
                        round(p["float_shares"]),
                        p["short_knockout_pct"],
                        adv_seed,
                    ),
                )
                row = await cur.fetchone()
        except psycopg.errors.UniqueViolation:
            raise ValueError(f"ticker {ticker} is already listed") from None
    assert row is not None
    return int(row[0])


@dataclass(frozen=True)
class DelistReport:
    ticker: str
    mark_price: Decimal
    tick_index: int | None
    positions_settled: int
    shorts_covered: int
    bounded_shorts_settled: int
    options_settled: int
    orders_cancelled: int
    alerts_cancelled: int
    events_resolved: int
    fund_paid_minor: int
    mm_absorbed_minor: int


async def delist_instrument(conn: AsyncConnection, ticker: str) -> DelistReport:
    """Delist an instrument and settle every exposure at the final mark,
    in one transaction. Settlement is immediate: halted or closed-market
    delists use the current quoted_price rather than waiting for an open
    tick (a halted book is already frozen at that mark, and waiting would
    strand the cleanup on the session clock).

    Money flow mirrors _liquidate_leg minus impact/spread/fees -- this is
    a fixed-mark settlement, not a trade through depth. MARKET_MAKER pays
    longs the mark; shorts pay the cover with the insurance fund and then
    MM absorbing any shortfall. Borrow fees (SINK) and accrued dividends
    (MM) rank ahead of the cover leg. Bounded shorts settle at intrinsic
    value, open orders cancel, and pending events resolve as no-ops.
    """
    ticker = ticker.strip().upper()
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            # Lock ordering: instruments before accounts, sorted by id.
            # The INDEX row rides along for the divisor adjustment.
            await cur.execute(
                """
                SELECT id, ticker, kind, is_active, quoted_price,
                       index_member, index_divisor, base_price, market_id
                FROM instruments
                WHERE ticker = %s OR kind = 'INDEX'
                ORDER BY id
                FOR UPDATE
                """,
                (ticker,),
            )
            locked = await cur.fetchall()
        target = next((r for r in locked if r["ticker"] == ticker), None)
        if target is None:
            raise UnknownInstrumentError(ticker)
        if not target["is_active"]:
            raise ValueError(f"{ticker} is already delisted")
        iid = int(target["id"])
        mark = Decimal(target["quoted_price"])

        # Index continuity (0031): a member leaving the basket shifts the
        # level unless the divisor is re-based -- the same mechanic real
        # index providers use on fast-track deletions. Refuse to empty the
        # basket outright (a zero level would hit the positive-prices CHECK
        # on the next tick).
        if target["index_member"]:
            # The basket is venue-scoped (R4): the index for the TARGET's
            # market re-bases over that venue's members only.
            index = next(
                (
                    r
                    for r in locked
                    if r["kind"] == "INDEX"
                    and int(r["market_id"]) == int(target["market_id"])
                ),
                None,
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(i.float_shares * i.quoted_price), 0)
                    FROM instruments i
                    WHERE i.kind = 'STOCK' AND i.index_member AND i.is_active
                      AND i.id <> %s AND i.market_id = %s
                    """,
                    (iid, int(target["market_id"])),
                )
                basket_row = await cur.fetchone()
            basket_after = float(basket_row[0]) if basket_row else 0.0
            if basket_after <= 0:
                raise ValueError("cannot delist the last index constituent")
            assert index is not None and index["index_divisor"] is not None
            divisor_new = Decimal(str(basket_after)) / Decimal(index["base_price"])
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE instruments SET index_divisor = %s WHERE id = %s",
                    (divisor_new, int(index["id"])),
                )

        async with conn.cursor() as cur:
            await cur.execute("SELECT MAX(tick_index) FROM market_ticks")
            tick_row = await cur.fetchone()
            tick = int(tick_row[0]) if tick_row and tick_row[0] is not None else None

            await cur.execute(
                "UPDATE orders SET status = 'CANCELLED' "
                "WHERE instrument_id = %s AND status = 'OPEN'",
                (iid,),
            )
            orders_cancelled = cur.rowcount
            # A delisted instrument can never cross anything again.
            alerts_cancelled = await cancel_for_instrument(conn, iid)
            # resolve_due_events doesn't check is_active -- left pending, a
            # DIVIDEND would keep rescheduling onto a dead instrument.
            await cur.execute(
                "UPDATE events SET resolved = TRUE "
                "WHERE instrument_id = %s AND NOT resolved",
                (iid,),
            )
            events_resolved = cur.rowcount
            # Stale flow rows would sit until the next tick consumed them;
            # the instrument never steps again.
            await cur.execute(
                "DELETE FROM pending_flow WHERE instrument_id = %s", (iid,)
            )

        mm_id = await get_system_account_id(conn, "MARKET_MAKER")
        sink_id = await get_system_account_id(conn, "SINK")
        fund_id = await get_system_account_id(conn, "INSURANCE_FUND")

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT p.id, p.quantity, p.borrow_fees_accrued, p.dividends_accrued,
                       a.id AS account_id, a.balance
                FROM positions p
                JOIN accounts a ON a.user_id = p.user_id
                  AND a.season_id IS NOT DISTINCT FROM p.season_id
                  AND a.kind <> 'SYSTEM'
                WHERE p.instrument_id = %s AND p.quantity <> 0
                ORDER BY p.id
                FOR UPDATE OF p
                """,
                (iid,),
            )
            positions = await cur.fetchall()
            await cur.execute(
                "SELECT COUNT(*) AS n FROM positions "
                "WHERE instrument_id = %s AND quantity <> 0",
                (iid,),
            )
            total_row = await cur.fetchone()
            total_positions = int(total_row["n"]) if total_row else 0
        if len(positions) != total_positions:
            raise ValueError(
                f"{ticker}: {total_positions - len(positions)} position(s) "
                "have no matching account -- refusing to strand them"
            )

        # Lock the involved accounts in ascending id order and re-read
        # balances post-lock: the JOIN's a.balance is a statement-time
        # snapshot, and a concurrent debit committed between it and the
        # leg's post_transfer would make min(cost, cash) overshoot into
        # the nonneg CHECK -- rolling back the whole delist.
        balances: dict[int, int] = {}
        for account_id in sorted({int(p["account_id"]) for p in positions}):
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, balance FROM accounts WHERE id = %s FOR UPDATE",
                    (account_id,),
                )
                acc_row = await cur.fetchone()
            assert acc_row is not None
            balances[account_id] = int(acc_row[1])

        settled = covered = 0
        fund_paid = mm_absorbed = 0
        for pos in positions:
            qty = int(pos["quantity"])
            account_id = int(pos["account_id"])
            cash = balances[account_id]
            if qty > 0:
                payout = int(
                    (Decimal(qty) * mark * 100).quantize(Decimal("1"), ROUND_HALF_UP)
                )
                if payout > 0:
                    await post_transfer(
                        conn,
                        from_account_id=mm_id,
                        to_account_id=account_id,
                        amount=payout,
                        reason="DELIST_PAYOUT",
                        memo=ticker,
                    )
                settled += 1
            else:
                # Carry debts rank ahead of the cover leg: borrow fees to
                # SINK, accrued dividends to MARKET_MAKER, both limited by
                # cash on hand (unpaid accrual dies with the position, same
                # as the liquidation path).
                fee_minor = int(
                    Decimal(pos["borrow_fees_accrued"]).quantize(
                        Decimal("1"), ROUND_HALF_UP
                    )
                )
                paid = min(fee_minor, cash)
                if paid > 0:
                    await post_transfer(
                        conn,
                        from_account_id=account_id,
                        to_account_id=sink_id,
                        amount=paid,
                        reason="BORROW_FEE",
                        memo=ticker,
                    )
                    cash -= paid
                div_minor = int(
                    Decimal(pos["dividends_accrued"]).quantize(
                        Decimal("1"), ROUND_HALF_UP
                    )
                )
                paid = min(div_minor, cash)
                if paid > 0:
                    await post_transfer(
                        conn,
                        from_account_id=account_id,
                        to_account_id=mm_id,
                        amount=paid,
                        reason="DIVIDEND",
                        memo=ticker,
                    )
                    cash -= paid
                cost_minor = int(
                    (Decimal(-qty) * mark * 100).quantize(Decimal("1"), ROUND_HALF_UP)
                )
                user_pays = min(cost_minor, cash)
                if user_pays > 0:
                    await post_transfer(
                        conn,
                        from_account_id=account_id,
                        to_account_id=mm_id,
                        amount=user_pays,
                        reason="DELIST_COVER",
                        memo=ticker,
                    )
                shortfall = cost_minor - user_pays
                if shortfall > 0:
                    pays = min(await _fund_balance(conn, fund_id), shortfall)
                    if pays > 0:
                        await post_transfer(
                            conn,
                            from_account_id=fund_id,
                            to_account_id=mm_id,
                            amount=pays,
                            reason="DELIST_COVER_FUND",
                            memo=ticker,
                        )
                        await _record_fund_flow(
                            conn, -pays, "COVER_SHORTFALL", None, tick
                        )
                        fund_paid += pays
                    residual = shortfall - pays
                    if residual > 0:
                        # MM absorbs the rest -- the cover it was owed just
                        # isn't paid. Recorded like a liquidation ADL.
                        await _record_fund_flow(
                            conn, 0, "ADL", None, tick, mm_absorbed_minor=residual
                        )
                        mm_absorbed += residual
                covered += 1
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE positions
                    SET quantity = 0, avg_cost = 0, borrow_fees_accrued = 0,
                        dividends_accrued = 0, updated_at = now()
                    WHERE id = %s
                    """,
                    (int(pos["id"]),),
                )

        # Bounded shorts settle at intrinsic value: collateral + Q*(entry -
        # mark), floored at 0 -- the same formula a voluntary cover or a
        # knockout computes, at the final mark.
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT bs.id, bs.quantity, bs.entry_price, bs.collateral_minor,
                       a.id AS account_id
                FROM bounded_shorts bs
                JOIN accounts a ON a.user_id = bs.user_id
                  AND a.season_id IS NOT DISTINCT FROM bs.season_id
                  AND a.kind <> 'SYSTEM'
                WHERE bs.instrument_id = %s AND bs.status = 'OPEN'
                ORDER BY bs.id
                FOR UPDATE OF bs
                """,
                (iid,),
            )
            bshorts = await cur.fetchall()
        for bs in bshorts:
            intrinsic = (
                Decimal(int(bs["collateral_minor"]))
                + Decimal(int(bs["quantity"]))
                * (Decimal(bs["entry_price"]) - mark)
                * 100
            ).quantize(Decimal("1"), ROUND_HALF_UP)
            payout = max(0, int(intrinsic))
            if payout > 0:
                await post_transfer(
                    conn,
                    from_account_id=mm_id,
                    to_account_id=int(bs["account_id"]),
                    amount=payout,
                    reason="BSHORT_PAYOUT",
                    memo=ticker,
                )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE bounded_shorts
                    SET status = 'DELISTED', closed_tick = %s,
                        close_price = %s, payout_minor = %s
                    WHERE id = %s
                    """,
                    (tick, mark, payout, int(bs["id"])),
                )

        # Open options settle at intrinsic against the final mark -- the
        # same value settle_expired_options would pay at expiry. MM pays
        # in full (SYSTEM accounts may go negative -- C3: no clamp on a
        # contract the system promised to honour).
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT o.id, o.side, o.strike, o.quantity, a.id AS account_id
                FROM option_positions o
                JOIN accounts a ON a.user_id = o.user_id
                  AND a.season_id IS NOT DISTINCT FROM o.season_id
                  AND a.kind <> 'SYSTEM'
                WHERE o.instrument_id = %s AND o.status = 'OPEN'
                ORDER BY o.id
                FOR UPDATE OF o
                """,
                (iid,),
            )
            open_opts = await cur.fetchall()
        for opt in open_opts:
            intrinsic = max(
                Decimal("0"),
                (mark - Decimal(opt["strike"]))
                if opt["side"] == "CALL"
                else (Decimal(opt["strike"]) - mark),
            )
            per_share_minor = int(
                (intrinsic * 100).quantize(Decimal("1"), ROUND_HALF_UP)
            )
            payout = per_share_minor * int(opt["quantity"])
            if payout > 0:
                await post_transfer(
                    conn,
                    from_account_id=mm_id,
                    to_account_id=int(opt["account_id"]),
                    amount=payout,
                    reason="OPTION_SETTLEMENT",
                    memo=ticker,
                )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE option_positions
                    SET status = 'SETTLED', closed_tick = %s,
                        settlement_minor = %s, mark_minor = %s
                    WHERE id = %s
                    """,
                    (tick, per_share_minor, per_share_minor, int(opt["id"])),
                )

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE instruments SET
                    is_active = FALSE,
                    delisted_tick = %s,
                    circuit_halted_until_tick = NULL,
                    next_event_tick = NULL,
                    next_halting_event_tick = NULL,
                    short_interest_pct = 0
                WHERE id = %s
                """,
                (tick, iid),
            )

    return DelistReport(
        ticker=ticker,
        mark_price=mark,
        tick_index=tick,
        positions_settled=settled,
        shorts_covered=covered,
        bounded_shorts_settled=len(bshorts),
        options_settled=len(open_opts),
        orders_cancelled=orders_cancelled,
        alerts_cancelled=alerts_cancelled,
        events_resolved=events_resolved,
        fund_paid_minor=fund_paid,
        mm_absorbed_minor=mm_absorbed,
    )
