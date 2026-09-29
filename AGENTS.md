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
liquidation (`_liquidate_leg`), KO closes, AND voluntary
`cover_bounded_short` bypass the cap so positions can't be stranded --
a bounded-short cover is atomic (no partial close exists), so capping it
could trap a position opened near the cap after ordinary drift. Resting orders work over multiple ticks — pass-2 MM
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
otherwise pay index longs cash for free. User suspension
(`users.disabled_at`): `bootstrap_user` raises `UserDisabledError`
carrying `disabled_reason`, which `_instrument_one` catches into an
ephemeral reply — keep the reason on the exception or suspended users
get a bare refusal. `profile_stats.active_season_equity_minor` is full
league MTM equity via `seasons.league_equity_minor`, NOT the league
account's raw cash.

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
The same exemption is per-venue (R4): a venue debuting mid-history hits
its first `cycle_pos==0` with no OPEN candles behind it (closed ticks
write flat 'CLOSED' candles, so `_venue_has_candles` counts open-state
rows only) and steps (1,1,1) -- no synthetic gap on the debut print.
Gap variance is split (0033): stochastic terms scale by
sqrt(closed_ticks * `session.overnight_var_frac`) (default 0.125 ->
var_dt=60, ~an hour of trading), while clock-time terms (base drift,
mean reversion, impact decay) keep the full closed_ticks. Without the
split ~85% of instruments pinned the 4.5% breaker every reopen.
Regional markets (0053+0054, all four phases done): venues live in `markets`
(open/closed/offset ticks, tick_size, auction_ticks,
overnight_var_ticks = the ABSOLUTE gap horizon, 60 for US so R4's longer
close keeps var calibrated by writing 60 again, NOT rescaling the frac).
`instruments.market_id` FKs each listing to a venue; candles carry
`session_state` per row (authoritative for chart filters -- market_ticks'
is the global/any-open summary). Session reads go through
`market_cfg(market, global_cfg)` which overlays venue values onto the
`session.*`-shaped cfg dict and derives `overnight_var_frac` back from
the tick horizon; `instrument_session_cfg` does the whole lookup for
fill paths. `assert_market_open(conn, instrument_id)` is venue-scoped --
call it AFTER resolving the instrument. Tests flip session shape by
UPDATEing the `markets` row (NOT `session.*` config, which only survives
as fallback defaults for missing venues).
R2/R3 mechanics: `apply_tick` is one unified pipeline -- `venue_step`
maps instruments to their venue's (dt, var_dt) or carries them through
closed (flat candle, frozen mark); shared factor draws rescale per
instrument via `_scale_factor`. Sweeps take `open_market_ids` (a
`market_id = ANY(...)` filter, None = every venue): order matching,
KO sweep, alert sweep, recalls, and liquidation legs. Liquidation
defers per-LEG -- `_liquidate_account` skips closed-venue positions and
leaves the account undermargined for the next tick; the ADL/insurance
backstop never settles a leg whose venue is closed. Option settlement
is deliberately venue-agnostic (C5: overnight expiries settle at the
frozen pre-gap mark); only `reprice_open_options` skips closed venues.
Closing auctions are per-venue (`auction_market_ids`): a book auctions
when ITS venue is inside `markets.auction_ticks` of its own close.
R4 (0054): the AS venue (600/840, offset 720, var_ticks 60 -> 0.0714
frac on the longer close) seeds 40 stocks + ASX40, a second cap-weighted
index; index baskets and `delist_instrument`'s divisor rebase are
venue-scoped (an index's members are its own market's `index_member`
rows). The sim harness splits the cohort into one wave per distinct
venue-open tick and `_random_active_ticker` only picks open-venue
tickers (a closed-venue pick would dead-letter in the TradingError
catch). Note: `test_iceberg_*`'s old mark*1.02 limit sat exactly on the
2% cross collar after tick snapping -- tests crossing the book should
stay under ~1.5%.
Mean reversion uses the exact OU decay `log_dev*(1-exp(-kappa*dt))`
(not linear `kappa*log_dev*dt`) -- at dt=480 the linear pull is ~9x the
deviation and overshoots FV into a halt; the decay form converges onto
FV so the reopen's reversion move is bounded by the deviation itself.
"Now" = phase of `MAX(tick_index)`;
`assert_market_open` (market/data.py) gates `execute_trade`,
`place_order`, and both bounded-short endpoints with `MarketClosedError`
-- imported lazily there because `market.data -> trading.errors ->
trading.__init__ -> trading.service -> margin -> market.data` is a real
import cycle. Sim-harness gotcha: `ticks_per_day` (1440) ==
one session cycle, so agents must act right after the day's FIRST tick
(the session-open gap tick, cycle_pos 0) — acting after the last tick
lands in the closed phase and every trade/quote is silently swallowed
by `_run_agent_day`'s broad TradingError catch (the sim "ran" with zero
orders for weeks before this was noticed).

Chart UI notes: `/chart` attaches a stateless `discord.ui.View`
(`bot/chart_view.py`) -- every button's custom_id encodes
`cbt:{action}:{iid}:{end}:{span}:{axis}` so clicks need no server-side
session and survive nothing except the message itself. `axis` is `time`
(default; UTC wall-clock labels from `market_ticks.ts`, bucketed candles
label by the bucket's last tick, day boundaries get a `Mon DD` label so a
compressed close reads as an overnight gap) or `ticks` (raw tick_index);
legacy 5-field cids parse as `time` so old chart messages keep working.
The `ax` action toggles the axis in place; `/chart` also takes an `axis`
choice. Per-user view prefs live in `chart_prefs` (0034) keyed by the
Discord snowflake (no FK -- chart viewing isn't gated on an account):
`save_chart_prefs` upserts the resolved (span, axis) on EVERY button
interaction and on explicit `/chart` args, `load_chart_prefs` feeds
`/chart` when its args are absent (params are Optional so "picked" is
distinguishable from "defaulted"). `end` is deliberately not saved --
charts always open anchored at the latest tick. Two paths reach
`handle_chart_component`: the live View's item callback and
`StockBotClient.on_interaction` (discord.py fires BOTH for one click on
the SAME Interaction object -- the `_INFLIGHT` id set claims the first
entrant and the loser no-ops; post-restart the View is gone and
`on_interaction` is the only path). The handler ACKs with a
payload-free `response.defer()` (type 6) then edits via
`interaction.message.edit(attachments=[...])` -- do NOT use
`response.edit_message` with new files: the type-7 interaction callback
rejects uploaded attachments (observed in prod as 10062 Unknown
interaction); files only work on the message PATCH endpoint. Window state is in open-tick
units: `render_candle_chart(conn, iid, ticker, end=, span=)` selects the
`span` open-phase candles ending at `end` (None = last open tick),
bucketed `ceil(span/240)` ticks per candle in SQL (grp anchors at the
window's right edge so the OLDEST bucket is partial). The closed tail
appends only when `end == last_open`. `next_window` resolves
panl/panr (half-span shifts in open candles, clamped to history
bounds), zin/zout (span/2, span*2 clamped to [30, 6720]), home and
s<span> presets (re-anchor to last open). Action names starting `s`
carry the target span (`s960` = 1D at 960-tick sessions).
Render aesthetics live in `_render_png` helpers, all pure:
`_span_label(span)` maps spans to `1h`/`4h`/`1d`/`1w` (`~Nd`/`Nt`
fallbacks) for the title, and `_session_boundaries(ticks, times, bucket)`
returns candle indexes at compressed open-phase discontinuities:
`ticks[i] - ticks[i-1] > bucket` (overnights/halts/missing rows) OR a
wall-clock gap > 6x the window's median candle spacing with a 10m floor
(service downtime between ticks leaves contiguous tick_indexes that
still span days). Each draws a `_MUTED` dashed separator at `i - 0.5`
plus a `_gap_tag` duration label ("45m"/"8.5h"/"2d", tick-span fallback
when ts is NULL). The legibility matters: a same-UTC-day close teleports
the axis (07:17 → 15:44) and the `Mon DD` day labels only fire across
UTC dates, so without a visible boundary + duration the compressed gap
reads as a bad tick. The open→closed-tail transition is tick-adjacent
so the shading marks it, not a separator. The legend (`O/H/L/C Δ%` in the window's direction
color), right-edge last-price pill (`rect=(0,0,0.94,1)` reserves its
gutter), and faint ticker watermark are all derived from `rows` — no
protocol/signature change. Renders are cached in-process
(`_RENDER_CACHE`, LRU 64) keyed by a fingerprint of every row's
tick_index+close+volume — a repeat request (button spam, second user on
the same ticker) reuses the PNG bytes; the fingerprint busts on any
intra-tick fill amendment so a mid-tick trade still re-renders. PNGs
save at dpi=120 (1200px wide): upload size is the dominant
/chart latency on slow links. `/chart mine:True` renders a PRIVATE
chart (ephemeral defer) with the caller's marks drawn by `_render_png`:
position entry line + P&L band (`viewer_id` → `positions` avg_cost/qty,
signed for margin shorts) and the newest OPEN bounded short's dashed
red `knockout_price` line — gutter pills are suppressed within 3% of
`view` of the last-price pill. Marks join the render fingerprint like
`theme` (a new fill busts the key; holders of nothing share the base
render) and widen `price_hi/lo` so out-of-window entries still draw.
The `mine` flag rides the cid as field 8 (`:m`/`:-`, parsed len 5–8):
ephemeral messages have no PATCH route for re-rendered attachments, so
`handle_chart_component` answers mine-clicks with a fresh ephemeral
`followup.send` instead of `message.edit` — stale ephemeral charts keep
working, they just scroll up. In Docker, `MPLCONFIGDIR=/app/.mplconfig`
is baked with the font cache at image build — `stockbot` has no home
dir, so without it every container restart rebuilds the fontlist and
the first render takes seconds. Candle/wick/volume widths drop to 0.6 above
120 bars, else 0.8. `MARKET CLOSED` only suffixes the title when the
tail is closed candles.
Shop UI: `/shop` (replaces `/shop list|buy`) is a stateless browser in
`bot/shop_view.py` mirroring the chart component pattern —
`shop:{action}:{arg}` cids (`home`, `cat`, `pick`, `buy`, `equip`,
`back`), a module `_INFLIGHT` dedup for the View-callback +
`on_interaction` double dispatch, and an `elif` fallback branch in
`StockBotClient.on_interaction` so clicks survive restarts. Navigation
is category-first (23 purchasable items would fit one Select now, but
the 25-option cap is permanent headroom only via categories):
capabilities/tools/consumables/cosmetics -> item Select -> detail card
with Buy/Equip/Back. `is_purchasable` is the single storefront
predicate (price_minor NOT NULL OR key in slot/margin_tier, the two
computed-price rows) — share it or badges leak in. Stacking is opt-in
via `shop.service.NON_STACKABLE_ITEM_KEYS`/`is_stackable` (sandbox_access
was silently stackable because every consumer only checks `owns_item`).
`buy_item` auto-equips TITLE/theme on purchase, so an enabled "Equip"
means owned-but-not-currently-equipped. All shop replies are ephemeral
and use `response.edit_message` (no attachments, so the type-7 callback
works — unlike chart PNGs); `respond_shop_action(conn, ...)` is the
conn-injectable seam tests drive without the live pool.
Shop themes (0046): `theme_*` COSMETIC metadata IS the render palette —
`shop.service.palette_from_metadata` normalizes it over
`charts._DEFAULT_PALETTE` keys {up, down, bg, grid, text, accent, spine,
muted, halt_flow, halt_model, pill_text} (legacy `chart_color` maps to
up+accent). `render_candle_chart(theme=)` resolves via `charts._palette_for`
(cached forever in `_THEME_CACHE` — theme rows are static) and `theme` is
part of `_RENDER_CACHE`'s fingerprint, else one user's palette leaks to
everyone. The equipped key lives in `chart_prefs.theme`
(`load_chart_prefs` returns it as a third element; `save_chart_prefs`
deliberately doesn't touch it) and rides the cid as a 7th field — a shared
chart message can't repaint per-clicker; `-` = default. `/chart` gates the
theme on `owns_item`; `buy_item` auto-equips themes on purchase.
`/equip` (0047) owns the column going forward.
Titles/flair (0047): `TITLE` shop kind + `users.equipped_title` (explicit
slot column). `/equip` routes kind→slot via `shop.equip_item`
(TITLE→users.equipped_title, COSMETIC-with-palette→chart_prefs.theme),
`/unequip slot` clears; buying either auto-equips inside `buy_item`.
`status.equipped_flair_map(conn, ids)` renders "emoji name" for boards —
`leaderboard_embed(flair=)` prints `rank. <@uid> · title  $X`, and flair
joins the board content digest (equipping must repaint the board).
`profile_stats.title` feeds `/profile` + `/whois`; badge/trophy names
render `metadata.emoji` inline.
Consumables/perks (0048): `CONSUMABLE`+`PERK` kinds stack on buy
(quantity+1, not AlreadyOwnedError); `use_consumable` decrements and
deletes at 0 (CHECK forbids the 0 row) — caller owns the tx. Reroll:
`quest_instances.user_id` (NULL=global) + `quest_swaps` make a rerolled
quest genuinely stop tracking — `list_quests` and `sweep_completions`
both filter `(user_id IS NULL OR user_id = viewer) AND NOT swapped`;
the dedupe unique became `(def_key, period, period_index, user_id)
NULLS NOT DISTINCT`. `/quests` became a group: `list` + `reroll`.
Streak shield: `claim_daily` returns a `ClaimResult` (`amount_minor`,
`streak`, `shield_used`, `roll`) — gap==2 days + owned shield → consume +
streak continues; gap≥3 doesn't consume. `alert_pack` adds
`10×quantity` to `alerts.max_per_user` in `create_alert`.
Claim wheel (0051): the daily grant is a seeded draw, not the flat
`claim_amount`. `wheel_roll(user_id, day, seed, jackpot_pct)` =
HMAC(seed, `claim|{uid}|{iso-date}`) → u∈[0,1) over `WHEEL_SEGMENTS`
(50/30/12/6/2 → 0.75×/1.25×/2×/4×/10× base) — same (user, day, seed)
always rolls the same segment, so retries can't re-roll and sims
reproduce; the draw happens inside the claim txn. `claim.jackpot_pct`
replaces the jackpot weight (0 = disabled); `claim.wheel_enabled=0`
falls back to the flat formula (`roll=None`). Streak multiplies the
roll via `claim_amount(streak)/BASE` (1.0×→1.9× cap, same curve as the
old flat streak bonus); EV ≈ 1.43× base. `claims.last_segment`/
`last_amount_minor` audit the last roll. `/claim` sends "spinning" then
edits the result in (~0.8s) — cosmetic only. Harness passes
`wheel_seed=f"{master_seed}|claims"`.
Pro Terminal (0049): `pro_terminal` is a 30-day ANALYST_TOOL (renewals
reuse entitlement expiry). `/stock` gates `status.pro_terminal_stats`
(7d range, daily realized vol from close returns, 24h buy/sell flow
split, raw ADV, next-event countdown) on `owns_item`. Charts add 2w/1M
spans (`chart_view.MAX_SPAN_PRO`=19200): `/chart` choices and every
button click resolve the clicker's entitlement; `load_chart_prefs`/
`next_window` take `max_span` so an expired user clamps back to 6720.
Sandbox (0050): `sandbox_access` PERK → `seasons.sandbox_user_id`
marks a season as one user's private practice sandbox. It reuses the
LEAGUE-account quarantine verbatim (`open_sandbox` inserts an ACTIVE
season with `end_tick` ≈ +2e9 and `join_season`s — fee 0 skips the
spend check, `sandbox.stake_minor`=$100 mints FAUCET→league acct).
Every league lookup (`get_open_season`, `get_latest_season`,
`get_active_entry`, the `on_tick` snapshot insert) filters
`sandbox_user_id IS NULL` so a sandbox can never surface as "the
season"; `get_sandbox_entry` resolves it explicitly for the `sandbox`
flag on `/buy` `/sell` `/portfolio` (precedence: sandbox > league >
main). Closing a sandbox takes the early-return branch of
`_close_season_claimed` — no scoring, trophies, or SEASON_RESULT DMs,
just the SINK sweep + open-order cancel. `/sandbox open|status|reset`;
`reset_sandbox` = close + reopen. Sandbox trades are ordinary
season-scoped `trades` rows, so quests and volume badges count them.
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
did this fill cost X" is a query. Sizing UX: every exposure command
(`/buy` `/sell` `/short` `/order buy|sell`) accepts `quantity` OR
`dollars` (`/buy`/`/sell` also `all_in`), resolved by
`trading.service.shares_for_dollars`/`max_affordable_shares` via
`quote_trade` — a read-only replica of `execute_trade`'s pricing
(squeeze-boosted impact + flow-skewed spread + taker fee, no lock, no
writes). Dollar sells cap at the held position (they never flip into a
margin short); resting orders size at their anchor (limit, else stop),
not the mark. Shared `stockbot/logging.py` owns
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
`ledger_entries` is trigger-guarded append-only
(`ledger_entries_no_delete`/`_no_update`) — manual cleanup requires
`ALTER TABLE ledger_entries DISABLE TRIGGER ledger_entries_no_delete`
inside the transaction, deleting BOTH legs of every affected transfer_id
(preserves sum-zero), then rebuilding `accounts.balance` from the ledger
and re-enabling. Reverse-FK order for user deletion:
insurance_fund_flows → wash_trade_flags → trades → … → accounts → users.
`python -m stockbot.tools.doctor` checks migrations, advisory lock,
tick staleness, ledger invariants, config bounds, index presence, and
heartbeat freshness; `python -m stockbot.tools.replay --from N --to M`
re-derives each OPEN tick's market/sector factors from the master seed
and diffs them against `market_ticks`, plus checks
`quoted_price ≈ base·exp(impact)` (1e-6 tol -- the column is
NUMERIC(18,6)) and candle OHLC/CLOSED-flatness. Replay binds each
venue to ticks at/after its first OPEN candle (`_venue_debut_ticks`),
so a market seeded mid-history doesn't retroactively "open" earlier
ticks or drive union factor draws; a `markets` row that never got
instruments stays invisible to the bound. Replay caveat: sort sector
keys with Python `sorted()`, never Postgres ORDER BY -- locale
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
by sqrt(var_dt) (0033) so a reopen doesn't pin vol at clip_max. Idiosyncratic noise
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
`vol.flow_halt_ticks` instead of CIRCUIT_HALT_TICKS. INDEX instruments
skip the flow-breach check — the basket re-derives their price every
tick, so a "halt" would freeze orders without freezing the price; the
mislabel also lied about cause. A cross-triggered halt amends the
already-written candle's `halt_kind` post-match (`_settle_cross`).
Halts gate
risk-increasing trades only: closing/covering/liquidating proceeds during
a halt. `candles.model_ret` is the post-step pre-fill return — fills
amend `close` intra-tick, so replay can't recover r_model from OHLC;
`candles.flow_ret` is the bounded flow the EWMA consumed. Replay
guarantee narrowed: vol_state is flow-fed and can't be re-derived from
the seed — `replay` verifies internal consistency against stored
model_ret/flow_ret, NOT seed-determinism. F6 calibration: every measured
breach is an overnight-gap tick (intraday |r| never gets near the cap);
post-0033 the gap's stochastic horizon is closed_ticks*overnight_var_frac
(default 60) so breaches need a real tail draw, not an ordinary roll —
CIRCUIT_BREAKER_CAP = 0.045 yields ~a few halts
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
  docker-compose Postgres is mapped to **localhost-only** port **5450**
  (`127.0.0.1:5450:5432` — never publicly reachable on the VM; services use
  the compose network). If you hit `password authentication failed` or
  `role ... does not exist` against a `localhost` connection, suspect a port
  clash first — check `netstat -ano | grep <port>` on Windows. The compose
  `POSTGRES_PASSWORD` env var feeds every service's internal DATABASE_URL;
  Postgres only applies it on an empty data dir, so set it before first boot.
  `MASTER_SEED` is likewise a set-before-first-tick secret — it seeds all
  HMAC-derived price history and can't be rotated without forking it.
  Deploy (`.github/workflows/deploy.yml`) tarballs the checkout in the
  runner and scps it to the VM — the repo is private, so the VM can't pull
  it — then `docker compose up -d --build postgres bot market backup` (the
  `backup` service must be named or it never starts).

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

## Release UX (plan-1ccd460d1232ae3e.md)

**Phase N1 done** (`migrations/0035_notifications.sql`): a transactional
outbox for the events that happen server-side between commands
(liquidations, knockouts, order fills, season results) so a user isn't
finding out they were liquidated by noticing a smaller balance.
`notifications` rows are written *inside the same transaction* as the
event, at the row's insert site (`margin/service.py` `_liquidate_leg`,
`shorts/service.py` `sweep_knockouts`, `orders/service.py` both fill
sites -- cross and MM fallback -- gated on the UPDATE's `RETURNING
status` actually flipping to FILLED, not just any partial fill, and
`seasons/service.py` `_close_season_claimed`, one row per entrant
including unqualified ones) — a rollback there means the notification
never existed. `sweep_knockouts` changed from a bulk UPDATE to
`UPDATE ... RETURNING` so it has the per-row data (user, ticker, qty,
prices) to notify with; it still does all knockouts in one round trip.

`bot/notify.py` is the poller (`bot/main.py`'s `_notify_loop`, modeled on
the existing `_heartbeat_loop`, polling every 5s on its own connection —
one poll is one committed transaction, so a mid-poll crash can't
double-send or double-mark). It coalesces rows by `(user_id, kind,
payload['tick_index'])` before formatting, so a 3-leg liquidation or a
2-position knockout sweep sends one DM, not N — every insert site must
put `tick_index` in its payload or coalescing silently degenerates to
one-row groups. `deliver: Callable[[int, str], Awaitable[None]]` is
injected (tests use a stub, `bot/main.py` wires `discord.Client.send`);
raising `DeliveryForbidden` dead-letters *every* currently-pending row
for that user immediately (not just the batch's rows) so the next poll
doesn't rediscover the same Forbidden and repeat it. Other exceptions
bump `attempts` and schedule `next_attempt_at` with exponential backoff
(1/5/15/60/240 min); `attempts >= 5` is the dead-letter threshold, and
the poller's own SELECT excludes both dead-lettered and not-yet-due rows
so a full table scan every 5s stays cheap (partial index on
`next_attempt_at WHERE sent_at IS NULL AND attempts < 5`).

`/notify` toggles `users.dm_notifications`, read at *poll* time (folded
into the poller's SELECT via a JOIN), not write time — an opted-out
user's rows are never touched (not retried, not dead-lettered, `attempts`
stays 0), so opting back in surfaces the backlog instead of losing it.

**Phase N2 done** (`src/stockbot/status/service.py`,
`migrations/0036_net_worth_snapshots.sql`): `/leaderboard`, `/history`,
`/compare`, `/profile`. Net worth for a main portfolio is the same shape
as `margin.compute_health`'s equity (cash + mark-to-market positions +
bounded-short liquidation value − accrued borrow fees/dividends), but
`_NET_WORTH_EXPR`/`_NET_WORTH_JOINS` compute it for *every* USER account
at once (one query, not N) — `kind = 'USER'` alone excludes both LEAGUE
and SYSTEM accounts (the 0009 CHECK ties `kind='USER'` to
`season_id IS NULL`, so there's no separate season_id filter to forget).
Ranks are `ROW_NUMBER()` (sequential, tie-broken by user_id), not `RANK()`
— the plan's "rank 14 of 212" phrasing means a position, not a tied
placement. `leaderboard()` is a whole-table read every call, called out
in its own docstring as the thing to replace with a materialized
`leaderboard_snapshots` table if it ever gets hot; not worth the
complexity yet.

"24h change" needed net worth *history*, which nothing tracked (unlike
instruments, whose `day_change_pct` reads `candles` directly — a user's
past net worth can't be reconstructed after the fact, since positions
mutate in place with no version history). `net_worth_snapshots` fixes
that the same way `seasons.equity_snapshots` does for league accounts:
one `INSERT ... SELECT` over every USER account at the day boundary,
called from `market/tick.py` right next to `seasons.on_tick` in both the
OPEN and CLOSED branches (same unconditional-call-with-internal-modulo-
guard pattern, so it's a no-op most ticks). `day_change_pct` compares
against the latest snapshot strictly before *today's* day_index, and
returns `None` (never a synthetic 0%) when there isn't one yet — a
same-day comparison would be comparing an account to itself.

`/history` reads `trades` scoped to `season_id IS NULL` — league fills
never appear in the main-portfolio history, matching every other
main/league split in the codebase.

**Phase N3 done** (`BootstrapResult`, `/start`, `on_guild_join`):
`bootstrap_user` now returns `BootstrapResult(account_id, created)`
instead of a bare int — `created` comes from the `INSERT ... ON CONFLICT
DO NOTHING` rowcount on the `users` row, so it's exactly-once and
race-safe (two concurrent first-uses serialize on the unique index; only
the winner sees `created=True` and issues the grant). Callers that need
the account must use `.account_id`; ensure-existence callers just
discard the result. `create_user_account` returns `(account_id,
created)` the same way.

`created` drives first-use onboarding: `_welcome_suffix(created)` is
appended to whichever public response the account-creating command
produces (it's on every success path in `commands.py` — the flag is
exactly-once so the welcome can't double-fire, and a user whose first
command is `/liquidations` still gets it). It deliberately mentions the
24h first-claim gate so a day-one `/claim` rejection doesn't read like
a bug. `/start` is the explicit tour — bootstraps the account, then an
embed covering grant, market/stock/chart, buy/sell/orders, the claim
gates, bounded shorts + `margin_tier`, and leagues.

`on_guild_join` posts `guild_welcome_message()` once per guild. This
needed the one intent change since launch: `Intents.none()` +
`intents.guilds = True` — `guilds` is non-privileged (privileged ones
stay off: no members, presences, or message_content), and without it
Discord never sends GUILD_CREATE. Side benefit: the heartbeat's
`guilds` count is real now. Channel choice is probe-by-send (system
channel, then text channels, max 3 attempts) because `guild.me` isn't
reliably cached without the members intent; `Forbidden` moves to the
next candidate, other HTTP errors stop.

**Phase N4 done** (margin error formatting + explicit oversell opt-in):
`margin/errors.py` renders minor units as dollars via a local `_money`
(the domain layer can't import `bot.format` — it sits below it). The
oversell gate: `/sell` and `/order sell` take `short: bool`. A market
sell with `qty > max(held, 0)` and no flag stops at a holdings-aware
ephemeral before `execute_trade`; with it, the request reaches the
normal margin gates (tier-0 → `MarginNotUnlockedError`). `all_in`
sells can never oversell; dollar-sized sells only uncap at the
position when the flag is set. For resting orders the gate lives at
fill time, not placement: `orders.allow_short` (0037, default FALSE)
makes `_match_once` clamp a SELL's fillable size to a per-user
`sellable` pool (position floored at 0, shared across the user's asks
in the book, decremented per settle — pass 1 crosses, the closing
auction's `_volume_at` supply, and pass-2 MM fills all respect it).
An unbacked ask is *skipped*, never counted as a `fill_failure` —
position-dependent, same category as `_LimitBreach`. `place_order`
gains `allow_short`; `_place_order`'s dollars-sizing stops capping at
holdings when `short: True`, and the resting-order confirmation notes
unbacked shares. `execute_trade`'s own semantics are unchanged —
direct service callers (liquidation, seeds, tests) can still open
shorts; only the user-facing entry points gate. `/admin` is hidden
from non-admin slash pickers via `default_permissions` (the `_is_admin`
runtime check stays — Discord's filter is a display hint). Empty
states got pointers (`/portfolio`→`/market`, `/order list`→`/order`,
`/shorts`→`/short`, `/liquidations`→`/margin`) and `/market` `/movers`
`/order list` `/shorts` `/liquidations` gained column headers.

**NPC traders P1 done** (`migrations/0057_npc.sql`): `users.is_bot`
flags synthetic accounts -- a BOT account kind was deliberately rejected
because every money path resolves `kind='USER'`, so "is it human" is an
orthogonal flag, not an enum value. `npc_agents` (user_id PK, archetype,
internal `label`, enabled, quote_ticker, `died_at_tick` permadeath
marker) is created empty. Exclusions live at the aggregate sites:
`_HUMAN_ONLY_JOIN` beside `_NET_WORTH_JOINS` covers `leaderboard` and
the net-worth badge; volume/quests badge queries get `NOT u.is_bot`
(streak is naturally empty -- bots never claim); `sweep_completions`
joins `users ... AND NOT u.is_bot` at the measures table so NPC volume
can't mint FAUCET quest rewards (self-funding would defeat permadeath);
`scan_for_wash_trades` joins both sides' users and skips bots (synthetics
can't collude); `join_season` raises `BotAccountError`. Deliberately NOT
filtered: `net_worth_snapshots` (kept -- per-day equity is the runner's
half-life telemetry), `net_worth_minor`/per-user reads (the runner
values its agents through them), and `feed.enabled` stays an emit-side
switch. Feed rendering is event-class anonymous: mechanics kinds
(`LIQUIDATION`/`SQUEEZE`/`KNOCKOUT`/`WHALE`/`OPTION_PAYOUT`) name NOBODY
("a trader was liquidated") -- that's how a real tape reads, it makes
NPC prints indistinguishable (no "no-name = bot" tell), and it retires
liquidation-shaming for humans; achievement kinds (`JACKPOT`,
`SEASON_RESULT`) keep `<@id>` since bots can't reach them. `npc.*`
config is seeded with `npc.enabled=0`: P1 ships the identity layer with
zero agents acting.

**NPC runner P2 done** (`src/stockbot/npc/service.py`, `main.py`, compose
`npc` service): `python -m stockbot.npc.main` is a third singleton
process cloned from `market/main.py` -- own `pg_try_advisory_lock` key
(0x4E504354) + the `conn.commit()` right after (without it, ambient
tx state silently demotes every later `conn.transaction()` to a
never-committed savepoint; the same footgun applies inside `run_round`,
which wraps every phase -- read, per-agent action, death sweep -- in
explicit transaction blocks, with the stagger `asyncio.sleep` OUTSIDE
the action tx so pacing doesn't hold locks). The loop polls
`MAX(tick_index)` and fires `run_round` once per new tick when
`npc.enabled`. `spawn_agent` is the ONLY NPC money path: bootstrap
with `starting_grant=False` (bots burn `grant_issued` without the
STARTING_GRANT transfer -- synthetic snowflakes trivially pass the age
gate) + is_bot + one FAUCET->agent `NPC_STAKE` transfer + npc_agents
row in the caller's tx; there is deliberately no top-up function
(permadeath is the safety model). Agent selection is seeded
`master_seed|npc|uid|tick` against `npc.action_prob_per_tick`, scaled
each round by `npc.target_adv_share` feedback (bot share of trailing-
day `trades` notional, multiplier clamped [0.25x, 4x], skipped when the
window has no flow); chosen agents get a random delay across the tick
interval (the stagger keeps flow out of one pending_flow batch and off
apply_tick's locks at tick+0); aggregate round flow is bounded by
`npc.max_tick_notional` (dollars). Per-action failures are isolated:
each action is its own tx, expected errors log debug and unexpected
exceptions log a warning and score 0 -- a deterministic thrower can't
starve its successors or skip the death sweep, which runs in a
`finally`. P2 ships the grinder archetype only (ported per-tick from
the harness: venue-open ticker pick via `open_market_ids`, 5-15%-of-
balance buy, expected-error catch); whale/yolo/shorter/LP/stop_loss
land in P3. `mark_dead_agents` stamps `died_at_tick` on enabled agents
whose NET WORTH sits under `npc.death_balance_minor` with zero LIVE
INSTRUMENTS (positions + open orders + bounded shorts + open options --
a dead LP's stale quotes would otherwise keep filling for days) --
net worth, not cash, so a fully-invested agent isn't killed.

**NPC P3 done** (`src/stockbot/npc/agents.py`, `soak.py`, migration
`0058`): all six planned archetypes ported per-tick from the harness --
whale/grinder/yolo share one buy-the-mark body differing only in
balance fraction (5-20% / 5-15% / 50-95%); `shorter` covers held shorts
at 0.5 else opens real-margin shorts at 30-80% of `compute_health`
equity (spawn grants `margin_tier` free -- debiting the stake for it
just moves bounded money internally); `liquidity_provider` prunes open
quotes beyond 6 then posts a bid (and ask when holding) at
`mark +- offset` where offset is floored at `half_spread_for(...) +
npc.lp_min_spread` (0058 seeds 0.002, bounds-registered) so it can't be
scalped inside the MM's own spread; `stop_loss` re-enters flat at 0.6
then arms a 4-9% trailing SELL stop, reusing `quote_ticker` as its
pinned name. `wash_trader`/`farmer` dropped per plan; NO archetype
calls `claim_daily` -- stake-funded, P&L-sustained (the
`test_npcs_never_claim` audit joins the debit leg to FAUCET to prove
only NPC_STAKE + the universal one-time STARTING_GRANT reach bots).
Actions return executed notional for the round budget; resting orders
return 0 (depth isn't flow). `npc/soak.py` is the P3 exit harness --
scratch-DB ONLY (`--database-url` is REQUIRED; pointing it at the suite
DB was tried once and its committed agents/trades/config/regime drift
poisoned a dozen unrelated tests):
apply_tick + run_round per tick, reporting money supply, Gini,
per-archetype alive/dead, resting-order depth, and a FAUCET-leg
injection audit. Gotcha from the smoke run: soak commits real state --
leftover enabled agents on the shared test DB made other tests see 32
phantom agents, so disable/die-stamp them after a scratch-less run.

**NPC P4 done** (`/admin npc-list|npc-enable|npc-disable|npc-spawn`):
`agent_report` gives the census (per-agent equity, funded = ledger
NPC_STAKE+STARTING_GRANT, P&L = equity - funded, open positions, age);
`set_agent_enabled` toggles by label-or-user_id but refuses dead agents
(died_at_tick is permadeath, not a state to toggle back); `npc-spawn`
takes count<=10, stake override, and optional `quote_ticker` pinning.
No auto-spawn/replenishment loop -- the soak (360 ticks, 32 agents)
showed zero deaths and only ~0.03% supply drift, so population decay
is too slow to need a cap loop yet; manual spawns are the bounded
lever. Soak result for the record: money supply flat vs no-NPC
baseline, Gini 0.005->0.008 as P&L diverged, LP resting depth persisted
(~48 open orders between active ticks), all 4 shorters held real
margin shorts, FAUCET-leg audit clean (NPC_STAKE + STARTING_GRANT only).

**Collectibles done** (`migrations/0059_collectibles.sql`,
`src/stockbot/collectibles/{pull,service}.py`,
`bot/collection_view.py`, `/card` `/collection` `/open` `/craft`
`/feature`): card_sets freeze membership at release (IPOs/delistings
never mutate a released set — pull pools and completion denominators
are immutable). `cards` seeds one INSTRUMENT card per instrument
(82), 10 EPIC + 5 LEGENDARY LORE cards, and commemoratives minted by
`grant_commemorative` (backfilled for IPO subscribers / halt survivors,
never in pack pools). Pulls are tier-first: `resolve_pull` rolls the
frame tier from `pack.rate_*` weights, then draws uniformly from the
tier pool — instrument tiers share the 82-card pool and the tier IS the
frame stamped; lore tiers draw the EPIC/LEGENDARY lore pools.
`pull_seed` = HMAC(master|packs, uid|pull_seq) mirrors `tick_seed`, and
every `card_pulls` row records pull_seq + pity_count_before so any pull
replays byte-for-byte (the test does exactly this). Pity: `pity_count`
>= `pack.pity_threshold` clamps the tier to >=GOLD then resets on rare+;
`pack_premium`'s `metadata.floor="GOLD"` clamps only the LAST card of
the pack. `open_pack` is one transaction: record_idempotency_key ->
`use_consumable` -> FOR UPDATE on users (pack_pulls/pity/shards) and
each user_cards row -> classify (NEW inserts, higher frame UPGRADES in
place, equal-or-lower DUPLICATE burns to `pack.shards_*` value on
`users.shards` — off-ledger vanity material, one source one sink).
EPIC/LEGENDARY pulls emit `CARD_PULL` feed events (achievement class,
mention kept — bots never open packs). The `/open` reveal commits the
transaction BEFORE animating (C10 — a 4s staged edit never pins a
pooled conn, and a mid-reveal restart still leaves cards owned).
Shards: `craft_card` buys a missing instrument card at STANDARD,
`upgrade_frame` steps held instrument frames toward PLATINUM at
`pack.craft_*` costs; lore/commemoratives can't be crafted (the chase
stays chase). `users.featured_card` pins a held card — shows in
`profile_stats.featured_card`/`/profile` and appends after title flair
in `equipped_flair_map` (title AND card when both set). The binder is
stateless `cards:pg:{owner}:{page}` cids on the shop_view pattern with
a `cards:` branch in `on_interaction`; it resolves owners via
get_user/fetch_user fallback, NOT guild.get_member (no privileged
members intent). Both pack items are plain CONSUMABLE shop rows — they
land in `/shop` → Consumables and stack normally.

**Public tape done** (`migrations/0055_feed.sql`, `src/stockbot/feed`,
`src/stockbot/bot/feed.py`, `/feed-setup` `/feed-remove`): a per-guild
market-drama channel. `feed_channels` binds one channel per guild;
`feed_items` is a fan-out outbox — `emit_feed(conn, kind, payload,
user_id=, tick_index=)` does `INSERT ... SELECT channel_id FROM
feed_channels` inside the emitting event's own transaction, so a new
binding only picks up later events and a rollback leaves no phantom tape.
`feed.enabled` is the emit-side kill switch (checked at emit, so a
disabled feed leaves no backlog). Emit sites mirror the notifications
outbox: liquidation legs + borrow recalls (`LIQUIDATION`/`SQUEEZE`),
bounded-short KOs, whale MM fills at/above `news.whale_min_notional`
(dollars — book crosses deliberately don't post: a P2P whale print is
the collusion vector), daily-wheel jackpots, one aggregate `IPO` row per
offering, option payouts ≥ `news.option_payout_min`, landed NEWS/EARNINGS
(magnitude is already applied so it leaks nothing; dividends skipped as
too routine), and one aggregate `SEASON_RESULT` — the ONLY league event
on the tape. Every other emit is `season_id IS NULL`-gated: league stakes
are faucet-seeded and don't belong in main-economy drama.

Delivery is `bot/feed.py` cloned from notify.py's SKIP-LOCKED outbox
(`ChannelGone` maps `Forbidden`/`NotFound`), with two changes: pending
rows coalesce per channel into ONE message per poll (grouped by
(channel, kind, user_id, tick_index) → one line each, ≤1900 chars, drops
tagged "…and N more"), and a dead channel *unbinds itself* (`DELETE FROM
feed_channels`, ON DELETE CASCADE takes pending rows) instead of
dead-lettering rows. `_feed_loop` runs at 30s (tape latency is fine —
the batching is the point), posts with `AllowedMentions.none()` so `<@id>`
markup renders names without pinging people for being liquidated.
`/feed-setup` is manage_guild-gated and upserts (rebinds carry pending
items via ON UPDATE CASCADE); `/feed-remove` deletes the binding —
the feed's equivalent of `/leaderboard-setup` needs an explicit off
switch because it keeps posting, unlike a self-serve pinned board.
Test convention: `tests/test_feed.py` starts each case from
`DELETE FROM feed_items; DELETE FROM feed_channels` — service tests
elsewhere commit real feed rows whenever a channel happens to be bound.

**Phase N5 done** (`bot/autocomplete.py` + `@app_commands.autocomplete`
wiring): `ticker_autocomplete` (ticker-prefix matches first, name-prefix
fills to 25 — the matcher returns ALL prefix hits uncapped so the
fallback can still find real matches; the 25-cap is applied at choice
time), `sector_autocomplete` (for `/admin instrument-add` — its `ticker`
is deliberately NOT autocompleted since it's a new symbol),
`shop_item_autocomplete` (`shop_items.key`), `short_autocomplete` +
`order_autocomplete` (both scoped to `interaction.user.id`'s OPEN rows —
pickers never leak other users' ids; `#id` or ticker text both filter),
`admin_order_autocomplete` (unscoped — `/admin order-cancel` exists to
fix other people's orders), `tunable_param_autocomplete` (the
`TUNABLE_PARAMS` allow-list). Cursor-vs-input shadowing gotcha: the
callbacks use `cursor` for psycopg cursors and `cur` for the typed
string — mypy treats a reassigned name as one type. `/stock`'s Mark
field reads "Mark (mid)" whenever bid/ask are shown so the mark isn't
mistaken for a tradeable price.

**Release hardening done** (grant gating / disable / maintenance /
rate limits -- the `plan-1ccd460d1232ae3e.md` hardening pass):

- **Snowflake age derivation** (`accounts.service.discord_age_days`):
  `(user_id >> 22) + 1420070400000` = Discord creation ms -- pure int
  math, no API call. Every legacy test id (3001, SYNTHETIC_USER_ID_BASE
  9e14, ...) resolves to ~2015 so they're all eligible; ids from a bare
  `randrange(10**15, 2**62)` land in the FUTURE and stay grant-pending
  forever -- generate test snowflakes as `(now_ms - epoch - age) << 22
  | random22` (see `_snowflake`/`_fresh_user` helpers).
- **`BootstrapResult` fields**: `account_id, created, granted_now,
  grant_pending, min_age_days`. The grant is decoupled from creation:
  `bootstrap_user` issues `STARTING_GRANT` iff `NOT grant_issued AND
  snowflake age >= accounts.min_discord_age_days` (config, default 30,
  in CONFIG_BOUNDS so `/admin config` can tune it and doctor knows it).
  Grant claims run under a `SELECT ... FOR UPDATE` on the users row --
  concurrent pending bootstraps can't double-grant. Pending self-heals:
  the first bootstrap after aging delivers the grant, no cron.
- **Welcome surface**: `_welcome_suffix(result)` -- full welcome only on
  `created`, but the grant-pending note repeats on EVERY call while it
  applies (a pending user's $0 needs the explanation every time).
- **Claim gates moved into `claim_daily`**: `AccountTooYoungError`
  (snowflake floor) and `FirstClaimLockedError` (24h after
  users.created_at, carries `unlock_at` for the "when" message -- H5).
  The harness passes `enforce_first_claim_delay=False` (sim users'
  created_at is wall-clock, not sim time); all pre-existing claim tests
  pass it too. `/claim` renders the errors; no more inline SQL.
- **Per-user disable** (H2): `bootstrap_user` raises `UserDisabledError`
  (accounts/errors.py -- a plain Exception, NOT a TradingError
  subclass: importing trading.errors from accounts would cycle through
  trading.__init__ -> shop -> accounts). `_instrument_one` catches it ->
  ephemeral "This account is suspended." for every command. Fills and
  liquidation sweeps NEVER consult bootstrap -- positions still get
  margin-swept; that's deliberate. `/admin disable` is one transaction:
  set fields, cancel all OPEN orders (the fill path's bootstrap gap),
  enqueue `ACCOUNT_SUSPENDED` (kind added to the CHECK in 0038; notify.py
  has a formatter). `/admin enable` clears; `/admin user-info` is the
  triage view (balance, grant state, orders/positions/shorts/wash
  counts, disabled status, real Discord age).
- **Daily maintenance** (`maintenance.py`, H3): `_post_tick` calls
  `run_maintenance_if_due` at `tick_index % TICKS_PER_DAY == 0` -- same
  cadence as the net-worth snapshots, inside _post_tick's own
  transaction + try/except (prune failures can't poison a tick).
  Deletes: idempotency_keys >30d, notifications with sent_at >90d
  (unsent rows are NEVER pruned -- still deliverable), command_stats
  >90d. The notification poller's assumptions matter here: notification
  tests start from `DELETE FROM notifications` because command-level
  tests now commit real outbox rows to the shared test DB.
- **Command sync is hash-gated**: `setup_hook` hashes the serialized
  command set into `config.bot.command_hash` and skips `tree.sync()`
  when unchanged. A bulk PUT bumps Discord's global command `version`
  on EVERY call — even identical payloads — so unconditional
  sync-on-boot broke clients' cached command index for minutes after
  each deploy ("This command is outdated", interactions never reaching
  the gateway). If the registry is ever wiped server-side, delete the
  config row to force a re-publish.
- **Persistent leaderboard boards**: `leaderboard_channels` (0039) binds
  one channel per guild to a bot-owned message; `_leaderboard_loop`
  (60s, started in `on_ready`) edits it in place via
  `sync_leaderboard_boards`. The loop hashes the *ranking rows* (not the
  embed — the Updated footer would defeat it) and skips the REST edit
  when unchanged. `message_id` NULL → post + pin; `NotFound` on fetch →
  repost, but NOT inside `_REPOST_GRACE` (120s) of `updated_at` — a fresh
  board can briefly 404 while Discord propagates it, and treating that as
  "deleted" double-posts. Per-channel try/except so a deleted/locked
  channel never kills the pass; `message_id` commits per binding (a later
  channel's failure must not roll back an earlier board's stored id →
  double-post). `/leaderboard-setup` (default_permissions manage_guild)
  reads the old binding (`fetch_binding`), binds + posts immediately,
  then `delete_board_message` removes the displaced board — rebinding
  used to leave a frozen pinned orphan behind. `/leaderboard` shares
  `leaderboard_embed` with a caller-rank footer param.
- **Rate limits** (H4): `/chart` has `@app_commands.checks.cooldown(1,
  10)` (per-user default); `StockBotTree.on_error` maps
  `CommandOnCooldown` to "Slow down -- retry in Ns". `_instrument_one`
  adds a dumb global throttle (10 cmds / 10s / user, in-memory deque) --
  catches scripted bursts across all commands, not just the expensive
  one.
- **Currency sinks** (0040-0042): `shop_items.kind` now spans
  SLOT/ANALYST_TOOL/COSMETIC/TROPHY/MARGIN_TIER/BADGE/ORDER_TYPE.
  Grant-only kinds (TROPHY, BADGE, price_minor NULL) are filtered from
  the `/shop` browser and the item autocomplete; `buy_item` rejects
  NULL-price rows. Milestone badges grant in `evaluate_badges` (status/service.py) --
  a day-boundary batch pass alongside `snapshot_net_worth_if_due` in BOTH
  apply_tick branches; thresholds live in `shop_items.metadata`
  ({"metric","threshold_minor"|"threshold"}) and grants ride
  `entitlements`' PK as the idempotency key, DMing via BADGE_EARNED.
  ORDER_TYPE unlocks gate at `place_order` (iceberg=display_qty is now
  paid, `order_trailing` for trail_amount, `order_oco` via place_oco);
  a resting order survives its entitlement lapsing.
  `orders.trail_amount` drives `_ratchet_trailing_stops` once per tick at
  the top of `match_orders` (before `_trigger_due_stops`, NOT in the
  cascade loop -- same-tick fills must not re-anchor it); SELL trails
  ratchet up to mark-trail only, BUY mirrors, grid-snapped and biased one
  tick AWAY from the mark so a sub-tick trail can't park the stop on the
  mark and self-trigger; trail+limit is rejected (no trailing stop-limit).
  `orders.oco_group` links a `/order bracket` pair; `_cancel_oco_siblings`
  fires on every single-leg terminal transition (cross fill, MM fill,
  manual cancel, expiry sweep, fill_failures strike-out) -- scoped sweeps
  (season close, delist, user disable) already take both legs in one
  UPDATE. IPOs (ipo/service.py): `create_offering` wraps `add_instrument`
  then flips is_active=FALSE -- dormant instruments are invisible to the
  engine/candles/events until settlement; `subscribe` escrows cash into
  the IPO_ESCROW system account (SYSTEM_ACCOUNTS allowlist in
  ledger/service.py must include it); the subscribe window is
  `[open_tick, close_tick)` measured against the NEXT tick index
  (MAX+1) -- a commit at MAX+1 == close_tick still lands before
  settle's snapshot, so subscribe rejects only when MAX+1 > close_tick
  (the `FOR UPDATE OF o` on the offering row serializes a racing
  settle: the loser sees SETTLED, never a committed-but-missed sub);
  `settle_due` in apply_tick's
  lifecycle block allocates pro-rata-by-commitment capped at affordability
  (largest-remainder dust pass), burns proceeds escrow->SINK, refunds the
  excess, upserts positions at offer avg_cost, and activates the listing.
  Zero subscriptions -> CANCELLED, instrument stays dormant. `/ipo list`
  + `/ipo subscribe` user-side; `/admin ipo-create`.
- **Short-side mechanics** (0043): `accounts.margin_warned` latches when a
  margined account's equity drops below `margin.warn_ratio` x maint_req --
  `_warn_margin_risk` runs per candidate in `sweep_undermargined` (and at
  the tail of `check_and_liquidate`), fires one MARGIN_CALL DM per
  episode (UPDATE-first rowcount = atomic claim), clears on recovery, and
  only warns in the approaching band (undermargined accounts get
  LIQUIDATION, not warnings). `margin.sweep_recalls` sits between
  `accrue_borrow_fees` and `sweep_undermargined` in apply_tick: when
  `i.short_interest_pct` exceeds `margin.recall_si_pct` (deliberately
  below the `max_short_interest_pct` hard cap -- a hard-to-borrow band,
  not a cliff), every margin short covers ceil(qty * excess/si *
  `recall_fraction_per_tick`), CASH-CAPPED per leg so the fund/ADL
  backstop is unreachable (a cashless account keeps its short for the
  liquidation sweep). The cap is enforced TWICE: `sweep_recalls` sizes
  with a pessimistic `(1+max_impact)*(1+fee)` bound (no half-spread --
  it can undershoot), then `_liquidate_leg` re-clamps `close_qty` at the
  ACTUAL fill's per-share cost. The in-leg clamp is what makes the
  backstop unreachable in fact: before it, an undershooting estimate
  produced `shortfall > 0` and the fund paid for a recall with no
  `insurance_fund_flows` row (LIQUIDATION-gated), breaking
  `fund_reconciles`. Recalls reuse `_liquidate_leg` with
  kind="RECALL": penalty 0, ledger reason RECALL_COVER, notification
  SHORT_RECALL, no `liquidations` row. Bounded shorts are never
  recalled (collateralized derivative, not a borrow). IPO lockout:
  `instruments.shortable_after_tick` (NULL = always) is set by
  `create_offering` to close_tick + `ipo.short_lockout_ticks`; the
  authoritative gate is `_apply_fill`'s short_grew block, mirrored
  fail-fast in `place_order` for SELL+allow_short so resting shorts
  can't silently straddle the lockout (a fill-time check counts toward
  fill_failures). `(current_tick or 0)` semantics: pre-history means any
  future lockout still holds. `/short` takes optional `knockout:` pct ->
  `open_bounded_short(knockout_pct)` validated against
  `shorts.min/max_knockout_pct`; collateral + KO price follow the chosen
  pct and everything downstream reads the stored `knockout_price`.
  `effective_borrow_bps_per_tick` (margin/service.py) is the Python
  mirror of `accrue_borrow_fees`' utilization formula -- keep in sync;
  `/stock` reports borrow %/day, days-to-cover (si_shares/adv), and
  remaining capacity / lockout tick from new InstrumentSnapshot fields
  adv/float_shares/shortable_after_tick.
- **Price alerts** (0044): one-shot `/alert add|list|cancel`. OPEN rows in
  `price_alerts` flip to TRIGGERED in `apply_tick`'s `sweep_alerts` call —
  placed AFTER the liquidation sweep (the last mark-mutating op) so alerts
  see the true tick-close mark; the CLOSED branch never sweeps. Each fire
  enqueues an `ALERT_TRIGGERED` outbox row in the same transaction (the
  poller coalesces into one DM per user/tick). A partial unique index
  `price_alerts_one_open` rejects exact duplicates (user+instrument+
  direction+price); opposite directions at the same price are distinct.
  An already-crossed target is allowed at create time — it fires on the
  next tick. `alerts.max_per_user` (default 25) caps open alerts.
  `disable_user` and `delist_instrument` bulk-cancel open alerts (the
  sweep never consults bootstrap — same gap as resting orders).
- **Quests** (0045): daily/weekly rotating tasks measured entirely
  read-side — `quest_defs` catalog, `quest_instances` (deterministic
  shared pick per day via `hashtext(key||':'||day_index)`, window =
  [boundary_tick, +TICKS_PER_DAY)), `quest_completions` (PK is the
  pay-once key). `quests.on_tick` rotates at `tick % 1440 == 0` (both
  phases — rotation can land on a closed tick) and expires stale
  windows; `sweep_completions` runs EVERY tick (open and closed),
  one grouped INSERT..ON CONFLICT DO NOTHING per kind, paying FAUCET→
  main account via post_transfer + `QUEST_COMPLETED` outbox + bumping
  `users.quests_completed` (feeds the `quests` badge metric).
  `quest_instances.kind` is snapshotted with target/reward — def edits
  never mutate live quests. `IPO_SUBSCRIBE` is skipped in rotation when
  no offering is open. `quests.enabled` gates both rotation and sweep;
  counts are tuned by `quests.daily_count`/`weekly_count`. Kill-switch
  note for tests: apply_tick runs the rotation itself at tick 0, so
  quest tests zero `quests.*_count` config before driving real actions.
- **Options** (0052, Phase O2): European cash-settled CALL/PUT rows in
  `option_positions` — premium+markup flows user→MARKET_MAKER, fee→SINK,
  intrinsic settles at `expiry_tick`. Pricing is model-consistent:
  `options.pricing.total_variance` = OU stationary variance
  `(σ²/2κ)(1-e^{-2κT})` + `fundamental_sigma²·T`, blended from the EWMA
  vol back toward seed via `vol.rho` — NOT `σ_eff√T` (that formula
  overprices NORT 5-7x; `tests/test_options_pricing.py` pins the C1
  table). Post-0033 the horizon is THREE clocks via
  `session_variance_ticks` — stochastic accrual counts effective ticks
  (`open + overnight_var_frac·closed`, matching the engine's `var_dt`),
  OU mean-reversion decay keeps CALENDAR ticks (the gap step reverts
  with full `dt=closed`), and the EWMA regime blend counts open ticks
  (one update per step call). Counting calendar ticks wholesale priced
  ~29% too much variance per 960/480 cycle — a hidden extra MM edge.
  `bs_price`/`bs_delta` are zero-rate Black with intrinsic at
  T<=0. `apply_tick` calls `settle_expired_options` + `reprice_open_options`
  in BOTH branches — a contract expiring on a closed tick settles at the
  frozen pre-gap mark, and neither is gated on `options.enabled` (the
  kill switch blocks new buys only; sells and settlements always run).
  MM insolvency never aborts a tick: intrinsic is paid in full even when
  MARKET_MAKER is negative — it's a SYSTEM account, so the payout runs
  deeper negative like dividends/the ADL backstop, no balance clamp.
  OI cap: total open quantity
  per instrument must stay under `options.max_oi_frac` × liquidity —
  enforced at buy time with the instrument row locked. Strikes are
  freeform inside `options.strike_min_frac`/`strike_max_frac` × spot
  (junk-strike spam guard); the UI only offers the
  `EXPIRY_CHOICES_DAYS = (3,7,14,30)` constant. `hedge_frac` seeds 0 —
  Phase O3 adds
  the MM's delta flow back through `apply_trade_impact` with a per-user
  OI sub-cap; do NOT enable it without that sub-cap or the impact is
  unbounded. Long options count toward net worth (`_NET_WORTH_EXPR` adds
  `mark_minor*quantity`, season_id NULL for main / matching for league &
  sandbox) but are deliberately absent from `compute_health` equity —
  not collateral. `delist_instrument` settles opens at the final mark's
  intrinsic (same MM clamp) and reports `options_settled`. Sell-back
  markdown 0.12 > markup 0.10 so buy→sell round trips always lose ~20%
  — manipulation and wash hedges pay rent. League options increment
  `season_entries.trades_count` on open AND sell (MIN_TRADES scoring
  eligibility); `close_season` runs `settle_season_options` AFTER the
  final equity snapshot (marks keep residual time value for scoring) but
  BEFORE the SINK sweep — without it, post-close expiry settlements pay
  intrinsic into an already-emptied league account and strand the cash.
  `quote_option_premium` returns the UNFLOORED markup price: `buy_option`
  rejects sub-`min_premium` contracts outright, so a floored quote would
  name a fillable-looking price the buy refuses; `/options dollars:`
  guards `quote <= 0` before dividing. `opened_tick` writes
  `tick or 0` — never NULL on a pre-first-tick buy. Idempotent:
  settlement is a
  status flip on locked rows, buys key on `interaction_id`.
