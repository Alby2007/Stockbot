# Agent notes

Project plan: see the design doc (Phase 0 → Phase 2). Current status:
**Phase 0 complete** (ledger, accounts, migrations, process skeleton,
docker-compose). **Phase 1 complete**: tick engine, 40 seeded instruments,
trading (`/buy` `/sell`), `/balance` `/portfolio` `/market` `/stock` `/claim`
`/movers` `/sectors` `/chart` `/news` `/calendar`, shop, admin
(`/admin tune` `/admin ledger-audit` `/admin wash-trades` `/admin
season-create` `/admin season-close`), wash-trade detection, the economy
simulation harness (`python -m stockbot.simulation.harness`, use a scratch
DB), seasons/league (`/league info|join|standings`, `league` flag on
`/buy` `/sell` `/portfolio`), Phase 1.5 bounded shorts (`/short`
`/shorts` `/cover`, `stockbot.shorts`, knockout sweep inside `apply_tick`),
and Phase 2 true margin (`stockbot.margin`, signed positions, liquidation
engine, insurance fund, short interest + squeeze, SBX-40 index, `/margin`
`/collateral` `/liquidations`, `margin_tier` shop unlock) are all done.
Post-Phase-2: limit orders (`orders` table, `/order buy|sell|list|cancel`,
matched inside `apply_tick` before the margin sweep; fills run through
`execute_trade` in a savepoint so unfillable orders stay OPEN; league
orders are cancelled on season close) and `shorter`/`liquidity_provider`
archetypes in the sim harness are done.

Liquidity plan (phases A–E) status: **Phase A done** (dynamic
`spread.*` half-spreads at all four fill paths; utilization-scaled borrow
fees; `next_event_tick`/`last_halt_end_tick`; chart volume; `0013` param
bounds + `0014` spread config). **Phase B done** (crossing book).
**Phase C done** (participation cap + sqrt impact + fill epsilon).
**Phase D done** (stop orders + dividends).
**Phase E done** (sessions + overnight gaps).

Crossing-book design notes: `match_orders` runs two passes inside
`apply_tick`. Pass 1 walks each (instrument, season_id) book in
price-time priority; crossing pairs settle via `_settle_cross` — one
buyer→seller `TRADE_CROSS` transfer shared by both `_apply_fill` legs
(`cash_leg="none"` records the shared transfer id on both trade rows;
both sides still pay their own fee to SINK). The maker is the earlier
order by `(opened_tick, id)` and the cross prints at the maker's price;
the mark then moves to it (`impact = ln(cross/base)` clamped by
`max_impact` and `CIRCUIT_BREAKER_CAP`, breach → halt + `last_halt_end_tick`
like a tick-time breach). Crosses are collared to `cross.collar_pct`
around the **tick-open mark** (`open_marks`, anchored once per
`match_orders` call — NOT the live mark, which each cross moves; a live
anchor would let colluding pairs ratchet the price collar-width per
cross). A stale maker is skipped, its taker tries the next counterparty —
the collar gates book prints only; off-market orders can still MM-fill at
the mark. Self-matches (same user_id, any scope) are banned; settlement
failures advance the taker pointer. Both fill paths re-check
`status = 'OPEN'` on the order UPDATE — a `cancel_order` committed
between the book snapshot and the fill write rolls the fill back rather
than overwriting 'CANCELLED'. Pass 2 is the old MM fallback on remaining
`quantity - filled_quantity`, now recording `trades.order_id`.
`execute_trade` was refactored: the counterparty-
agnostic settlement core is `_apply_fill` (position upsert, borrow-fee
settle, fee leg, trade row, margin gates, season trade counter);
`execute_trade` = instrument lock + impact/spread + `_apply_fill` vs
MARKET_MAKER + mark/candle update. Cross legs do NOT call
`update_candle_with_fill` individually (would double-count volume) — the
cross prints once. Deterministic fill failures (funds, margin, league
state) increment `orders.fill_failures` and auto-cancel at
`order.max_fill_failures` — without it a parked unfillable order retries
every tick forever; the mark-dependent limit breach (`_LimitBreach`)
deliberately does NOT count.

Participation/impact notes: impact is concave — `Δimpact =
λ·sign(N)·√(|N|/L)` (N = signed fill notional, L = instrument liquidity);
migration 0016 rescaled `lambda_impact` to the new units anchored at a
$500 typical order so typical-size fills are unchanged. Known calibration
consequence: at `participation_cap` notional (~10% of liquidity) the
concave impact is ~1bp vs ~10–20bp under the old linear model — whales
move the market nearly for free via the MM, which materially weakens
impact as the anti-manipulation lever for large trades. User-initiated
fills are capped at `liquidity.participation_cap`·liquidity notional
(`InsufficientDepthError`, atomic — no partial market fills); forced
liquidation (`_liquidate_leg`) and KO closes bypass the cap so positions
can't be stranded. Resting orders work over multiple ticks — pass-2 MM
fills clamp to the cap and leave `filled_quantity` short. MM limit fills
also need the mark to cross the limit by `order_book.fill_epsilon_bps`,
not merely touch it (no free trade-throughs). When resizing tests: a
failing-depth order raises before funds/margin checks — keep test sizes
under the cap when testing other rejections.

Stops/dividends notes: `orders.order_type` LIMIT/STOP/STOP_LIMIT with
`stop_price` and `triggered_tick`; untriggered stops are invisible to the
book, `_trigger_due_stops` flips them when the mark crosses, and a
triggered STOP is marketable (never the maker — a marketable maker defers
to the counterparty's limit, two marketable orders cross at the mark).
`match_orders` is an outer loop of `_match_once` bounded by
`order.stop_cascade_max_iters` with `depth_used` carrying the per-tick
participation budget across cascade iterations — post-0016 lambdas are
~0.01, so one fill moves an illiquid mark ≪1%; cascades need several
orders or several ticks to matter. `depth_used` is keyed by **order id**,
not instrument: N resting orders on one instrument get N×cap notional per
tick (consistent with `execute_trade`'s per-fill cap — splitting a big
order multiplies throughput). Dividends: `events.kind='DIVIDEND'`
(scheduled like earnings for a hash-selected payer subset). The ex-date
drop is the *entire* funding mechanism: MARKET_MAKER pays longs
(`reason='DIVIDEND'`) and is short the aggregate book, so its mark-to-
market on the drop offsets the payout — self-funding. The old per-tick
`drift_offset` pre-bleed was retired (0020): it plus the ex-drop
suppressed ~2×div per cycle — a short-side arb. `drift_offset`/
`instruments.dividend_drift_offset` remain as vestigial columns that
always evaluate to NULL/0. `base_price` and `fundamental_value` drop by
the dividend (permanent, not the decaying impact term), and shorts accrue
`positions.dividends_accrued` (minor units) settled to MARKET_MAKER on
cover/liquidation — the cash CHECK means shorts can never be debited
directly. Bounded shorts are NOT charged (collateralized derivative, not
borrowed stock). Events on INDEX instruments resolve as no-ops (marked
resolved, nothing applied) — a hand-inserted index DIVIDEND would
otherwise pay index longs cash for free.

Sessions notes: `session_phase(tick, open, closed, offset)` in engine.py
is pure -- session state derives from the tick index, no shared flag.
Closed ticks still write `market_ticks` (`session_state='CLOSED'`) and
flat volume-0 candles, accrue borrow fees, and run `seasons.on_tick` --
everything else (events, steps, KOs, matching, margin sweep) is open-only.
The first open tick after a close (`cycle_pos == 0` and tick_index > 0)
steps once with `dt=closed_ticks` (the overnight gap, breaker-bounded
like any move) and applies `session.open_impact_reset`. Tick 0 is the
exception: the market never closed, so the first-ever tick steps with
dt=1 -- otherwise it "reopens" with a closed_ticks gap it never had.
"Now" = phase of `MAX(tick_index)`;
`assert_market_open` (market/data.py) gates `execute_trade`,
`place_order`, and both bounded-short endpoints with `MarketClosedError`
-- imported lazily there because `market.data -> trading.errors ->
trading.__init__ -> trading.service -> margin -> market.data` is a real
import cycle. Tests flip `session.*` config (open=2, closed=4) to cycle
phases in a handful of ticks; the phase offset shifts where the cycle
lands relative to tick 0. Sim-harness gotcha: `ticks_per_day` (1440) ==
one session cycle, so agents must act right after the day's FIRST tick
(the session-open gap tick, cycle_pos 0) — acting after the last tick
lands in the closed phase and every trade/quote is silently swallowed
by `_run_agent_day`'s broad TradingError catch (the sim "ran" with zero
orders for weeks before this was noticed).

Observability notes: every `apply_tick` writes per-tick stats on the
`market_ticks` row itself (duration_ms, fills, crosses, stops_triggered,
knockouts, liquidations, events_resolved) plus one structured log line
(`tick=N phase=… crosses=… ms=…`) — tick 4532 is fully re-describable
from one row + one log line. `match_orders`/`resolve_due_events` take an
optional `stats` dict out-param so the tick counts outcomes without
changing their return types. `_post_tick` runs the periodic invariant
audit every `audit.every_n_ticks` (default 60) in its own transaction —
ledger_sum_zero, no_negative_balances, balance_matches_ledger,
fund_reconciles → `audit_results` rows + log.critical on drift; audit
failures are swallowed (logged) so a broken audit can never poison a
tick. `service_heartbeats` is upserted per service per interval (market:
every tick incl. the CLOSED branch; bot: periodic loop) — "alive but
wedged" = heartbeat older than ~2 intervals, surfaced by `/admin health`
alongside `db.pool_stats()`. `market/main.py` tracks consecutive tick
failures and escalates to log.critical at 3. Kill switches live in
`config` (`trading.enabled`, `orders.enabled`, `shorts.enabled`):
service entry points raise `FeatureDisabledError` while
`match_orders` in the tick just no-ops (checked via
`data.feature_enabled_flag`, the non-raising variant). Trade rows carry
fill provenance: `trades.half_spread` and `trades.impact_delta` — "why
did this fill cost X" is a query. Shared `stockbot/logging.py` owns
handler setup for both service mains. Every registered command callback
is wrapped once at the end of `register_commands`
(`_instrument_commands`) -- one `cmd=… user=… iid=… ok=… ms=…` log line
(the interaction id is the correlation key) plus a fire-and-forget
`command_stats` row on its own connection. The wrap replaces
`cmd._callback` because `Command.callback` is a read-only property and
`_do_call` invokes the private attr. Ops surface: `/admin config`
(CONFIG_BOUNDS allow-list + ranges on all 35 config keys),
`/admin adjust` (FAUCET/SINK transfers, never a balance UPDATE),
`/admin order-cancel` (any OPEN order), `/admin recalc-balances`
(rebuild the balance cache from SUM(ledger_entries) -- a rebuild that
would go negative fails on the CHECK, which IS the signal).
`python -m stockbot.tools.doctor` checks migrations, advisory lock,
tick staleness, ledger invariants, config bounds, index presence, and
heartbeat freshness; `python -m stockbot.tools.replay --from N --to M`
re-derives each OPEN tick's market/sector factors from the master seed
and diffs them against `market_ticks`, plus checks
`quoted_price ≈ base·exp(impact)` (1e-6 tol -- the column is
NUMERIC(18,6)) and candle OHLC/CLOSED-flatness. Replay caveat: sort
sector keys with Python `sorted()`, never Postgres ORDER BY -- locale
collation puts 'index' before 'INDUSTRIAL', ASCII sort puts it last,
and that shifts every sector draw by one. A `backup` compose service
pg_dumps daily into the `stockbot-backups` volume (14-day retention).

Volatility-regimes notes (Phase F): `instruments.vol_state` is an EWMA of
|r| normalized by sigma_i (idiosyncratic sigma, rho `vol.rho`) -- for
factor-dominated names total vol >> sigma_i so the state equilibrates
well above 1 (avg ~3 observed in harness runs; the index basket's sigma=0
skips the EWMA entirely). What is bounded is the multiplier:
`sigma_eff = sigma * clip(vol.market_weight*v_mkt + (1-w)*v_i,
clip_min..clip_max)`, persisted per tick and read by every fill path via
`COALESCE(sigma_eff, sigma)` — spread, impact and margin queries must all
use the COALESCE'd value, never raw `sigma`. The EWMA numerator is
(|r_model|/sqrt(dt) + |bounded_flow|) / (sigma_i * sqrt(2/pi)) — total
returns including trade impact, with the sqrt(2/pi) normalization because
E|r|/sigma = sqrt(2/pi) for Gaussian returns, and gap returns normalized
by sqrt(dt) so a reopen doesn't pin vol at clip_max. Idiosyncratic noise
is Student-t(5) rescaled to unit variance; draw counts are unconditional —
never add branch-dependent RNG draws (replay determinism depends on draw
order). sigma=0 instruments (the index basket) skip the EWMA entirely.
Flow enters through `pending_flow` (instrument_id, user_id; user_id 0 =
system/liquidation): every impact-moving path upserts its delta_impact
inside the per-trade instrument lock, apply_tick consumes the whole table
while holding all instrument locks — lock-free by deferral, no new locks.
F5 guards: per-account flow clips at `vol.account_flow_cap`, the bounded
total at `vol.flow_ret_cap`, and the breaker evaluates
|r_model + bounded_flow| — a lone account is capped below the cap so a
whale alone can never manufacture a halt, while crowd-scale flow still
can (halt_kind 'FLOW' on the candle) and gets the shorter
`vol.flow_halt_ticks` instead of CIRCUIT_HALT_TICKS. Halts gate
risk-increasing trades only: closing/covering/liquidating proceeds during
a halt. `candles.model_ret` is the post-step pre-fill return — fills
amend `close` intra-tick, so replay can't recover r_model from OHLC;
`candles.flow_ret` is the bounded flow the EWMA consumed. Replay
guarantee narrowed: vol_state is flow-fed and can't be re-derived from
the seed — `replay` verifies internal consistency against stored
model_ret/flow_ret, NOT seed-determinism. F6 calibration: every measured
breach is an overnight-gap tick (intraday |r| never gets near the cap);
the gap's dt=480 step plus I1's drift_state·480 term make the realized
gap distribution wide — CIRCUIT_BREAKER_CAP = 0.045 yields ~a few halts
per instrument-week (measured 5.2/wk at 0.035, 5.4/wk at 0.03 pre-I1
measured 2.4/wk before drift_state existed). Because the clamp censors
candles.model_ret at the cap, the breach distribution can't be
re-measured above the cap from stored candles — calibrate against a
harness run, not SQL.

Microstructure notes (Phase G): fills print on a price grid —
`engine.tick_size(price)` is a 1-2-5 multiple of 10^n at or above
`price*spread.tick_pct`, floored at `spread.tick_min` ($100 → $0.10,
$5 → $0.01). `round_to_tick` ties away from zero, same as
`place_order`'s ROUND_HALF_UP snap of resting limit/stop prices — a
resting price is always on the grid fills print on. Crosses print at
the maker's (snapped) limit; the two-marketable-orders case prints at
the mark snapped to the grid. `quote_ticks(mark, half, tick)` rounds
the displayed bid DOWN and ask UP so the quote is the worst case a
market fill can land at. U-shaped intraday spread: `half_spread_fraction`
adds `spread.open_coeff·exp(−t/open_decay)` +
`close_coeff·exp(−(T−t)/close_decay)` terms when the caller merges
`session.*` keys into the spread cfg (all fill paths and the `/stock`
snapshot do); `half_spread_for` derives ticks_since_open/to_close from
the tick index + session geometry. New cfg keys are `.get()`-defaulted
in `half_spread_fraction` so hand-built cfg dicts in tests stay valid.
Gotcha: `execute_trade` computes session position from the *real*
MAX(tick_index) while `match_orders(tick_index)` uses the caller's
index — tests calling match_orders with a synthetic tick must
neutralize the U-shape (`session.open_ticks` huge) or the est/actual
fills diverge. Fill-side tick rounding quantizes `impact` deltas to
~tick/price — tests asserting small impact deltas must flatten the
grid (`spread.tick_pct=0`, `tick_min=1e-7`) or the quantum swamps them.

Flow-driven microstructure notes (Phase H): the tick-boundary flow
accumulator is `pending_flow` (richer than the plan's single-column
sketch — it keeps per-account rows for the F5 caps). `apply_tick`
consumes it before stepping, computes each instrument's bounded own
flow (per-account clip `vol.account_flow_cap`, total `vol.flow_ret_cap`),
then aggregates per sector. H2 cross-impact: each instrument's bounded
flow bleeds `flow.cross_impact_coeff`·γ_peer into same-sector peers as
a direct `impact` write — one hop by construction (the inflow is never
re-recorded into pending_flow), skipped for halted and INDEX
instruments, and included in the peer's `flow_ret` for the
EWMA/breaker (it IS a price move). H3 permanent impact:
`flow.permanent_frac` of OWN bounded flow moves into
`fundamental_value` (`F *= exp(shift)`, `impact -= shift`, per-tick cap
`flow.max_fundamental_move`) — the mark dips by shift now and kappa
pulls the base up to the new F over ~1/κ ticks, so flow genuinely
reprices without an unanchored permanent-impact term (finding 5).
`InstrumentTickResult` is frozen — mutate via `dataclasses.replace`
and write back into `results[i]` (rebinding the loop variable alone
silently drops the change). H4 ADV: `instruments.adv` is a sliding-
window SMA of per-tick notional volume (candles volume*close over
`flow.adv_window_ticks`), updated once per tick after all fill paths
by adding the newest tick's notional and subtracting the window-edge
tick's. `engine.effective_liquidity` scales liquidity by
clip(adv/(L·flow.adv_ref_frac), mult_min, mult_max) — dead tape
halves L (2x impact), frenzied doubles it — and (Plan A) by
`(1/max(1, vol_state)) ** flow.vol_liq_coeff`: storm regimes thin the
book, calm tapes never deepen past the ADV term. adv_ref_frac ≈ 2.5e-8
was measured as the sim's mean ADV/liquidity ratio so the multiplier
centers near 1. adv is WARM-STARTED, not zero-seeded: 0032 backfills
existing rows to `liquidity·adv_ref_frac` (neutral mult = 1.0) and
`add_instrument` seeds the same — a cold start otherwise sits at the
adv_mult_min floor (~half depth) for the whole 7,200-tick window.

Flow-responsive MM notes (Plan A, migration 0026): the participation
cap binds on EFFECTIVE liquidity everywhere now (execute_trade,
_assert_depth, pass-2 order sizing, book_depth MM rungs) — storms
shrink both impact liquidity AND the per-tick fill budget, replacing
finding 7's static-cap guard (the death spiral it prevented is now
bounded by vol.clip_max: liq_eff >= L·adv_min/4). Forced liquidation
still bypasses the cap entirely. `instruments.flow_skew` is an EWMA
(decay `flow.skew_decay`, innovation 1-decay) of each instrument's
bounded OWN flow written per tick by apply_tick — own flow only, not
cross-impact (peer sympathy isn't this name's tape). Every MM fill
path multiplies its half-spread by `engine.flow_skew_mult(signed,
skew, cfg)`: the side matching the tape pays up to
flow.skew_coeff·(skew/flow.skew_norm) extra, contra side discounts
(floor 0, ceiling flow.skew_max). /stock's bid/ask and book_depth
quote legs are asymmetric via `engine.quote_ticks_skewed` (bid leg =
sell-side half, ask leg = buy-side half). Margin liquidation legs pay
the skew + storm liquidity like any MM fill but are never capped.
adv is warm-started to the ref ratio (mult 1) — see the H4 note — but
tests sizing fills against the cap should still pin `adv` explicitly
(= liquidity·2.5e-8) so they don't break if the window's real flow has
already moved it; sustained-buy tests must recompute size per fill
because the whale's own flow raises vol_state and shrinks the cap
mid-loop (see _cap_size_qty in test_vol_regimes.py).

Momentum/book notes (Phase I): `instruments.drift_state` is an AR(1)
additive drift regime (I1) -- `step_instrument` now makes THREE sampler
calls per tick (momentum normal, idiosyncratic Student-t, fundamental
normal), all unconditional. The regime update `d = clip(rho*d +
innov_frac*sigma_eff*sqrt(dt)*eps, +-max_frac*sigma_eff)` lands BEFORE
this tick's step, so the innovation applies immediately; scales with
sigma_eff so momentum and vol clustering compound. rho=0.995 (half-life
~138 ticks) is deliberately a few times the seeded kappa half-lives
(25-75): much faster and the regime overwhelms the fundamental anchor,
much slower and it washes out. Frozen/halted names carry drift_state
unchanged (no draw); INDEX names have sigma=0 so theirs stays 0. With
mom.* absent the max_frac=0 default wipes drift_state -- "disabled" is
a reset, not a freeze. Tests resetting instrument state for re-tick
comparisons must restore drift_state too (same trap as vol_state in
test_tick.py). I2: `book_depth()` in market/data.py renders the /stock
book -- real LIMIT + triggered levels (season_id IS NULL only; league
books are a separate book entirely), best-first, padded outward with
synthetic MM rungs sized at
participation_cap*effective_liquidity/mark/levels per rung (Plan A:
storm-thinned effective liquidity, not static). Untriggered stops are
invisible to the matcher and so to the book.

Fee notes (Plan E, migration 0027): `_apply_fill` prices the fee off
`maker_taker` — MAKER legs pay 0 and TAKER legs split their tiered fee
into a MAKER_REBATE transfer (counterparty account = the maker's) plus
the TRADE_FEE SINK remainder; `trades.maker_rebate_minor` records the
split. MM fills (maker_taker NULL) and liquidation legs keep the flat
FEE_BPS — liquidation is a safety net, not a fee tier user. Taker rate
is `taker_fee_bps(user_id)` — lifetime `users.total_traded_minor` across
main+league accounts vs `fee.tier{1,2}_volume` → `fee.tier{1,2}_bps`,
read BEFORE the fill's volume accrues so the crossing fill itself pays
the old rate. `record_volume` accrues notional per fill; bounded shorts
call it directly (they settle outside _apply_fill). Rebate is clamped
to the taker's own bps — the maker can never be paid more than was
collected (at clamp, fee_transfer_id is NULL and SINK sees nothing).
`/balance` shows the tier.

Information notes (Plan D, migration 0028): `events.estimate` is the
public street consensus for an EARNINGS event — drawn at scheduling
(`schedule_initial_earnings` and the rolling reschedule both draw
`N(0, earnings.est_sigma)` AFTER the timing draw, ordered by
instrument id) and shown on `/calendar`. At resolution the pre-existing
`N(0, EARNINGS_SHOCK_SIGMA)` draw is now the SURPRISE term: realized
`magnitude = estimate + surprise` is stored back onto the resolved row —
earnings move on surprise-vs-consensus, and `/calendar` tells you what's
priced in. `news.fizzle_pct` (default 0.25, bounds 0–0.9): in
`maybe_create_news` the fizzle uniform is drawn BEFORE the magnitude
normal (unconditional draw count) — a fizzled rumor keeps its headline
and resolution timing, magnitude is shrunk 10× (not zeroed — a fizzle
should read like a tiny real move, not a detectable null), and
`events.fizzled` records it for post-resolution `/news` labeling
("rumor died"); pre-resolution reads never select the flag.
`analyst_tools` holders get a bucketed desk-read ("minor/sizable/major")
on pending `/news` magnitude — it leaks size but can't distinguish a
fizzle from a small real move, which is the intended edge.

Execution notes (Plan B, migration 0029): `execute_trade` takes an
optional `max_slippage` fraction — the fill is rejected
(SlippageExceededError, carrying the would-be fill and mark) when
|fill/mark − 1| exceeds it; the mark is base·exp(impact_before), i.e.
the pre-fill displayed mark, not the submit-time quote. Only `/buy`
`/sell` pass it (`slippage:` percent option); matching and liquidation
paths leave it unset — resting orders have their own limit checks and
forced flows must fill. `orders.display_qty` (NULL = fully displayed)
caps the size the `/stock` ladder shows per level — the aggregation is
`SUM(LEAST(display_qty, remaining))` so the visible slice refills as
fills drain it, while the matcher works real remaining quantity.
`DepthLevel.cumulative` is the running displayed-size total per side;
the `/stock` render shows size/price/cum per side with an "inside"
divider under the top row. `display_qty` is validated 1..quantity at
placement and by a column CHECK.

Session-auction & event-halt notes (Plan C, migration 0030):
`session.auction_ticks` (0 disables) -- a real window: on EACH of the
last N open ticks (`ticks_until_close <= N`)
`match_orders(closing_auction=True)` runs `_auction_clear` per book
instead of the continuous loop and restricts the MM fallback (pass 2)
to books that had no auction: event-halted names keep their
position-aware reduce-only path via execute_trade's gate, so a holder's
resting SELL still MM-fills mid-halt on a closing tick (previously pass
2 was skipped entirely -- resting orders on halted names froze for the
whole window while marketable sells still worked). Auctioned books
never reach pass 2 -- the print IS the close.
Clearing price = the candidate maximizing executable volume over the
union of resting limits + the snapped open_mark; ties break toward
open_mark then lower. A maximizing price outside `cross.collar_pct` of
the open mark is clamped to the collar boundary (grid-snapped, then one
tick back toward the mark if nearest-grid rounding overshot the
boundary), not abandoned. All pairs print at the SAME price through
`_settle_cross` (maker/taker + rebate still apply -- the earlier order
is the maker even though nobody "takes"). Stops triggered by the print
can clear in a later cascade-loop auction iteration. Per-tick auction
fill count persists to `market_ticks.auction_fills` (0032).
`instruments.next_halting_event_tick` = MIN resolve_tick over
unresolved EARNINGS only, maintained by refresh_next_event_ticks in the
same UPDATE as next_event_tick (dividends are mechanical, news is
unscheduled -- neither halts). `event_halted(tick, now, lead)` +
`event_halt_lead_ticks()` in market/data.py are the shared gate:
`event.halt_lead_ticks` (0 disables). execute_trade applies the same
risk-reducing exemption as circuit halts (EventHaltedError only when
the fill grows exposure); place_order rejects outright (consistent with
closed-market placement); shorts' `_lock_instrument` gates opens while
cover stays ungated. In match_orders, pass-1 skips event-halted books
entirely (frozen book, like circuit halts) while pass-2 lets
execute_trade decide per fill -- a resting order that reduces risk can
still MM-fill inside the window, and EventHaltedError joins the
transient (no-strike) catch. The gate self-opens on resolution: the
aggregate rolls to the next earnings ~30d out.
The two halt types deliberately differ on RESTING orders (intentional,
not inherited): circuit/flow halts queue them like a real LULD pause --
pass-1 skips AND pass-2 filters `circuit_halted_until_tick IS NULL`, so
the book freezes outright -- while T1 event halts are lenient (pass-2
reaches the position-aware gate). The asymmetry is bounded to resting
orders: under EITHER halt a marketable reduce-only /sell still fills via
execute_trade -- the escape hatch during an emergency is the deliberate
marketable order, not a resting one. To unify later: drop the pass-2
`circuit_halted_until_tick IS NULL` predicate and add
InstrumentHaltedError to the transient catch.

Lifecycle notes (Plan F, migration 0031): `instruments.index_member` is
the fixed SBX-40 basket -- the tick's index level is computed from live
rows (`kind != 'INDEX' AND index_member` among the is_active set), so
without the flag a listing would silently join the index and a delisted
member would silently leave a hole. Seeded STOCK rows got TRUE; listings
stay FALSE in v1. `admin/service.py` owns both ends:
`add_instrument` defaults unspecified params to the sector's medians
(`percentile_cont`, market-wide fallback for an empty sector), validates
overrides against PARAM_BOUNDS, and refuses the `index` sector (stored
lowercase -- sector lookup is case-insensitive because of it).
`delist_instrument` settles at the current quoted_price IMMEDIATELY in
one transaction -- halted/closed-market delists don't wait for an open
tick (a halted book is already frozen at that mark anyway). The sweep:
cancel OPEN orders (all scopes), mark unresolved events resolved
(required, not cosmetic -- `resolve_due_events` never checks is_active,
so a pending DIVIDEND would otherwise keep rescheduling onto a dead
instrument), delete pending_flow rows, then positions: longs get
MM->account 'DELIST_PAYOUT' of qty*mark; shorts pay carry debts first
(borrow_fees -> SINK, dividends -> MM, cash-limited like liquidation)
then the cover (account -> MM 'DELIST_COVER', shortfall -> fund pays
'COVER_SHORTFALL' flow, residual -> 'ADL' mm_absorbed). Bounded shorts
settle at intrinsic (collateral + Q*(entry-mark), floored 0) under a new
'DELISTED' status -- NOT 'KNOCKED_OUT' (that status means wiped).
Positions are zeroed, not deleted (matches the upsert convention). No
fee, spread, or impact anywhere in the sweep -- it's settlement, not a
trade through depth. Member delists re-base `index_divisor =
basket_after / index.base_price` for level continuity (the real index
mechanic); delisting the last constituent is refused outright since an
empty basket would produce level 0 and trip the positive-prices CHECK
next tick. `/admin instrument-add` and `/admin delist` expose both; the
delist report counts every settlement path.

Margin design notes: cash stays >= 0 (the USER/LEAGUE balance CHECK is
preserved -- short proceeds credit to cash and are spendable; leverage is
bounded by post-trade margin gates, not negative cash). Maintenance applies
to SHORT notional only, so `sweep_undermargined` only scans accounts with
`quantity < 0`. `check_and_liquidate` opens its own tx (locks the user's
position instruments id-ordered, then the account) because post-trade
liquidation can't take new instrument locks while holding the account lock
without inverting the ordering. The tick path calls `_liquidate_account`
inline (instruments already locked). Backstop chain on negative equity:
INSURANCE_FUND pays what it can -> MARKET_MAKER absorbs the residual;
every leg is recorded in `liquidations` / `insurance_fund_flows`
(reconciliation: fund balance == SUM(flows.amount_minor)). Borrow fees
accrue fractionally on `positions.borrow_fees_accrued` and settle to SINK
on cover/liquidation. Shorts consume portfolio slots (`quantity <> 0`).
League accounts get effective margin tier 1 (equal start). Bounded shorts
do NOT count toward short interest (they're collateralized, not borrowed).

Bounded-shorts design notes: `bounded_shorts` rows are separate from
long-only `positions` (defined-risk product: collateral = Q·entry·
knockout_pct posted to MARKET_MAKER upfront, payout = max(0, collateral +
Q·(entry − close)), KO at entry·(1+knockout_pct) → payout 0).
`sweep_knockouts` runs inside `apply_tick` after instrument updates.
`seasons` equity adds open-short value via `_SHORT_VALUE_SUBQUERY` — when
embedding it in a raw query string, the query must be an f-string (a plain
`"""` query shipped `{_SHORT_VALUE_SUBQUERY}` literally to Postgres once).

Seasons design notes: LEAGUE accounts are `accounts` rows with
`season_id` set; positions/trades carry `season_id` (NULL = main portfolio,
via `NULLS NOT DISTINCT` unique key). `seasons.on_tick` runs inside
`apply_tick`'s transaction: activation, day-boundary equity snapshots,
close (score/rank/prizes/sweep-to-SINK). Note: `ALTER TYPE ... ADD VALUE`
can't be referenced in its own transaction -- that's why the enum lives in
0008 and everything that touches 'LEAGUE' is in 0009.

## Gotchas already hit once -- don't re-debug these

- **Every long-lived connection must commit before looping.** Any code that
  runs a bare `cur.execute(...)` on a connection (not wrapped in
  `conn.transaction()`) leaves that connection sitting in an open,
  uncommitted implicit transaction (autocommit=False is the pool default).
  If you then loop and call service functions that each open their own
  `async with conn.transaction()`, those nest as *savepoints* of that
  never-committed outer transaction instead of real commits -- writes look
  like they succeed (no exception, logs look fine) but never reach disk and
  hold row locks forever, silently wedging every other connection that
  touches the same rows. This bit `market/main.py`'s tick loop: it checks
  `pg_try_advisory_lock` with a bare `execute()` before starting the loop.
  Fix is one explicit `await conn.commit()` right after acquiring the lock
  (`pg_try_advisory_lock`, not `pg_try_advisory_xact_lock`, is session-scoped
  so this doesn't release it). If you add another long-running loop that
  reuses one connection, do the same audit: every bare statement before the
  first `conn.transaction()` block needs an explicit commit (or just don't
  run bare statements on a connection you intend to reuse across a loop).
- `bootstrap_user` was the live instance of that trap: a bare
  `SELECT 1 FROM users` pre-read both opened the poison transaction AND
  raced as a check-then-act (two concurrent first-uses could both grant).
  Fix pattern: never pre-read for check-then-insert — derive "was I
  first?" from `cur.rowcount` on `INSERT ... ON CONFLICT DO NOTHING`
  itself (concurrent inserts serialize on the unique index; only the
  winner sees rowcount 1), and run the create + one-time grant inside a
  single `conn.transaction()`.
- Short-lived per-command connections (`async with db.connection() as conn:`
  then one or more `service_function(conn, ...)` calls, as in
  `bot/commands.py`) don't have this problem: the pool rolls back any
  leftover open transaction when the connection is released, and each
  top-level `conn.transaction()` call still commits for real as long as nothing
  upstream nested it.
- The simulation harness hit the same bug differently: `run_simulation` calls
  `bootstrap_user`/`net_worth_by_user` (bare SELECTs) before the tick loop, so
  the whole run silently became one never-committed transaction full of
  savepoints -- progressively slower, holding `instruments` row locks,
  invisible to other connections. Fix: `simulation/harness.py::_main_async`
  sets `conn.set_autocommit(True)`, which also mirrors production (each
  service call = one real transaction). Tests keep using rollback-wrapped
  conns; do NOT add `conn.commit()` inside `run_simulation` or it would break
  that isolation.
- `executemany()` is not a batch: psycopg3 still waits per row (~186 socket
  waits per 40-instrument tick measured, ~96ms/tick). `market/tick.py` uses a
  single `UPDATE ... FROM (VALUES ...)` and one multi-row `INSERT` instead --
  ~8ms/tick. Note VALUES columns with mixed None/int rows infer as `text`;
  cast explicitly (`v.col::bigint`). For real bulk loads, consider COPY.
- `rng.choice(rows)` is only deterministic if the SQL has `ORDER BY` --
  Postgres row order shifts as UPDATEs rearrange tuples, which made the
  wash-trader sim test flaky until `_random_active_ticker` got `ORDER BY
  ticker`. Same deal for `resolve_due_events`: earnings magnitudes and
  reschedule jitter are drawn in row order, so its `FOR UPDATE` scan is
  `ORDER BY id`.
- The market tick loop's advisory lock is session-scoped: if the connection
  dies, the lock is already gone and a second instance may hold it.
  `_tick_loop` returns on `psycopg.OperationalError` (or a closed/broken
  conn) and `run()` re-checks out a fresh conn and re-acquires the lock;
  never keep ticking on a dead conn -- the pool won't heal it.
- Shop buys take `FOR UPDATE` on the user's *account* row before reading
  entitlements (same pattern as `claim_daily`): the entitlement row doesn't
  exist on a first buy, so locking it directly locks nothing and two racing
  first buys both price at `owned=0`.
- Tunable engine params are bounded twice: `admin.service.PARAM_BOUNDS` and
  the `instruments_engine_params_sane` CHECK (0013). Bounds are finite
  ranges on purpose -- Postgres numeric comparisons treat NaN as greater
  than everything, so `x >= 0` does NOT reject NaN.
- `candles.volume`/high/low are extended by fills between ticks
  (`trading.service.update_candle_with_fill`, also called by bounded shorts
  and liquidation legs); candle T is written during tick T then amended by
  intra-interval prints until T+1.
- `claim_daily(..., as_of_date=...)` exists for the sim harness -- sim days
  map to consecutive fake dates so faucet income is per-sim-day, not
  per-real-day. Don't "fix" it back to CURRENT_DATE only.
- Circuit halt semantics: `circuit_halted_until_tick` is the *last* frozen
  tick (compared with `>=`), so a breach at T freezes T+1..T+CIRCUIT_HALT_TICKS.
- `close_season` claims the ACTIVE/SCHEDULED -> CLOSED flip atomically
  (`UPDATE ... WHERE status <> 'CLOSED' RETURNING`) inside its own
  transaction -- a bare-statement version committed nothing outside the
  tick path, and an unguarded version could double-pay prizes on a racing
  close. `join_season` re-checks status under `FOR SHARE` for the
  join-vs-close race and maps the league-account UniqueViolation to
  AlreadyEnteredError (check-then-insert can't win a concurrent join).
- Insurance-fund reconciliation: `fund_balance == SUM(flows.amount_minor)`
  holds because ADL rows write `amount_minor = 0` -- the absorbed residual
  is MARKET_MAKER's loss and lives on `mm_absorbed_minor`. Never record an
  MM absorption as a negative amount_minor.
- `match_orders` MM fills re-check `result.fill_price` against the limit
  inside the savepoint: the candidate pre-check sees a stale instrument
  snapshot (earlier fills moved `impact`) and doesn't model the squeeze
  boost execute_trade adds on BUYs. A breaching fill rolls back and the
  order stays OPEN.
- `place_order` validates `season_id` at placement (ACTIVE + entered,
  same rule execute_trade enforces at fill); a league order placed
  against a dead/unjoined season would rest OPEN forever otherwise.
- Squeeze-boost SI is one tick stale for order fills by design
  (refresh_short_interest runs after match_orders). Bounded shorts accrue
  no borrow fees -- strictly cheaper carry than margin shorts, trading it
  off against the KO + capped-risk payoff. SBX40 has float_shares=0 so
  its short-interest cap is vacuous (the one instrument with unbounded
  shorting; margin gates still bound it).
- Backlog: `idempotency_keys`, resolved `events`, `command_stats`,
  `audit_results`, and `market_ticks` grow forever -- add a retention
  cleanup when they get big.
- `_post_tick`'s config read lives inside its `conn.transaction()` on
  purpose: the first version ran `audit_every_n_ticks(conn)` as a bare
  SELECT on the market's long-lived connection, opening the never-
  committed implicit transaction from the bootstrap_user trap -- every
  subsequent apply_tick nested as a savepoint and committed nothing
  from tick 2 onward. Rule: on a reused long-lived connection, EVERY
  statement goes inside `conn.transaction()`; tests can't see this
  because the fixture's outer rollback makes nesting normal (the
  regression tests in test_concurrency.py use plain/scratch conns).
- Kill-switch semantics: `trading.enabled`/`orders.enabled`/
  `shorts.enabled` halt NEW exposure only. `FeatureDisabledError` is a
  global transient -- match_orders exempts it from fill_failures strikes
  (a trading halt must not auto-cancel the book), and
  `cover_bounded_short` is intentionally ungated so a shorts halt can't
  trap users in open positions (same reason `cancel_order` is ungated).

## Environment

- Requires Python 3.13, not 3.14 (discord.py 2.7.1 has open issues on 3.14).
  On a machine where the default `python` is 3.14, create the venv with
  `py -3.13 -m venv .venv` instead.
- Windows only: psycopg's async mode refuses to run under the default
  `ProactorEventLoop`. `tests/conftest.py` and both service `main()` entrypoints
  already switch to `WindowsSelectorEventLoopPolicy` on `sys.platform ==
  "win32"`. This is a no-op on Linux (where the Docker image runs), so it's
  safe to leave in.
- This dev machine already has native Postgres services bound to ports 5432
  and 5433 (`postgresql-x64-13`, `postgresql-x64-18` Windows services). The
  docker-compose Postgres is mapped to host port **5450** instead. If you hit
  `password authentication failed` or `role ... does not exist` against a
  `localhost` connection, suspect a port clash first — check
  `netstat -ano | grep <port>` on Windows.

## Commands

```bash
# one-time
py -3.13 -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
cp .env.example .env

# local Postgres
docker compose up -d postgres
docker exec stockbot-postgres-1 psql -U stockbot -d stockbot -c "CREATE DATABASE stockbot_test;"
.venv/Scripts/python -m stockbot.migrate

# verification (run all three after any change)
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy src

# full stack smoke test
docker compose up -d --build
docker logs stockbot-migrate-1   # should say "No pending migrations." on repeat runs
docker logs stockbot-bot-1       # idles without DISCORD_TOKEN (expected pre-Phase-1)
docker logs stockbot-market-1    # "acquired singleton advisory lock..."
```

Tests need a real Postgres (`TEST_DATABASE_URL`) — no SQLite fallback, by
design (row-locking/concurrency behavior can't be expressed there).

Ops gotcha: `migrate.py`/`doctor.py` resolve `MIGRATIONS_DIR` from
`__file__` (repo layout) — inside the Docker image the package is
pip-installed under site-packages, so the Dockerfile sets
`MIGRATIONS_DIR=/app/migrations`. Both tools now fail loudly when the
dir is missing; before that guard the migrate service silently printed
"No pending migrations" and exited 0 while the prod schema stalled
several migrations behind the code.
