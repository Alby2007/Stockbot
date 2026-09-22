"""Wires the pure math in `market/engine.py` to Postgres: one call per tick."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import time

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from stockbot.margin import service as margin
from stockbot.market import data, engine, events
from stockbot.observability import (
    audit_every_n_ticks,
    run_periodic_audit,
    write_heartbeat,
)
from stockbot.orders import service as orders
from stockbot.seasons import service as seasons
from stockbot.shorts import service as shorts

log = logging.getLogger("stockbot.market.tick")


async def apply_tick(conn: AsyncConnection, master_seed: str) -> int:
    """Advance the market by exactly one tick. Returns the tick index applied.

    Must only ever be called by the singleton market process (see
    `market/main.py`'s advisory lock) -- there is no locking here against a
    concurrent second tick, only against concurrent trades touching the same
    instruments.

    Session structure: ticks cycle through `session.open_ticks` open ticks
    then `session.closed_ticks` closed ticks (derived from the tick index,
    so every process agrees). Closed ticks do no factor-model work -- the
    market row and flat zero-volume candles are still written, borrow fees
    keep accruing (real markets charge calendar days), and season
    snapshots still land, but no matching, knockouts, or liquidation run.
    The first open tick after a close steps once with dt = closed_ticks,
    producing the overnight gap (bounded by the circuit breaker), resets a
    fraction of accumulated impact, then runs the full open-tick pipeline
    -- events due during the close resolve into that gap, and the
    knockout/margin sweeps fire immediately after it.
    """
    started = time.perf_counter()
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(tick_index), -1) + 1 FROM market_ticks")
            next_tick_row = await cur.fetchone()
            assert next_tick_row is not None
            tick_index: int = next_tick_row[0]

        session_cfg = await data.session_config(conn)
        open_ticks, closed_ticks, offset = data.session_parts(session_cfg)
        phase = engine.session_phase(tick_index, open_ticks, closed_ticks, offset)

        if phase == "CLOSED":
            # Flat candles keep the chart's x-axis continuous; volume 0 marks
            # them as non-trading ticks. No instrument writes at all, so no
            # FOR UPDATE is needed (trades are gated on assert_market_open).
            async with conn.cursor() as cur:
                # Vol state is frozen across the close (no returns, no
                # information): carry forward the previous tick's value.
                await cur.execute(
                    """
                    INSERT INTO market_ticks
                        (tick_index, market_factor, sector_factors,
                         session_state, vol_state)
                    VALUES (%s, 0, '{}', 'CLOSED',
                            (SELECT vol_state FROM market_ticks
                             ORDER BY tick_index DESC LIMIT 1))
                    """,
                    (tick_index,),
                )
                await cur.execute(
                    """
                    INSERT INTO candles
                        (instrument_id, tick_index, open, high, low, close,
                         volume, vol_state, flow_ret, model_ret, halt_kind)
                    SELECT id, %s, quoted_price, quoted_price, quoted_price,
                           quoted_price, 0, vol_state, NULL, NULL, NULL
                    FROM instruments WHERE is_active
                    ORDER BY id
                    """,
                    (tick_index,),
                )
            # Calendar-day accruals and lifecycle still run; nothing that
            # depends on prices moving does.
            await margin.accrue_borrow_fees(conn)
            await seasons.on_tick(conn, tick_index)
            duration_ms = (time.perf_counter() - started) * 1000
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE market_ticks SET duration_ms = %s WHERE tick_index = %s",
                    (round(duration_ms, 3), tick_index),
                )
            await write_heartbeat(
                conn, "market", {"tick": tick_index, "phase": "CLOSED"}
            )
            log.info(
                "tick=%d phase=CLOSED steps=0 events=0 crosses=0 mm_fills=0 "
                "stops=0 kos=0 liqs=0 ms=%.1f",
                tick_index,
                duration_ms,
            )
            await _post_tick(conn, tick_index)
            return tick_index

        # OPEN tick. Position in the cycle 0 means this is the first tick
        # after the close: step with dt = closed_ticks so the overnight
        # drift/vol lands as one gap (breaker-bounded like any move).
        # tick_index 0 is the exception: the market never closed, so the
        # first-ever tick must not "reopen" with a closed_ticks gap.
        cycle_pos = (tick_index - offset) % (open_ticks + closed_ticks)
        gap_dt = (
            float(closed_ticks)
            if cycle_pos == 0 and closed_ticks > 0 and tick_index > 0
            else 1.0
        )
        impact_reset = (
            float(session_cfg.get("session.open_impact_reset", 1.0))
            if gap_dt > 1.0
            else 1.0
        )

        async with conn.cursor(row_factory=dict_row) as cur:
            # Lock ordering rule: instruments before accounts, sorted by id.
            # No accounts are touched here, so this is just instruments-by-id.
            await cur.execute(
                """
                SELECT i.id, i.ticker, i.kind, s.key AS sector_key, i.drift, i.sigma,
                       i.beta, i.gamma,
                       i.kappa, i.fundamental_sigma, i.tau_ticks, i.base_price,
                       i.fundamental_value, i.impact, i.circuit_halted_until_tick,
                       i.float_shares, i.index_divisor, i.dividend_drift_offset,
                       i.index_member,
                       i.vol_state, i.sigma_eff, i.drift_state, i.flow_skew
                FROM instruments i
                JOIN sectors s ON s.id = i.sector_id
                WHERE i.is_active
                ORDER BY i.id
                FOR UPDATE OF i
                """
            )
            instrument_rows = await cur.fetchall()

        # Vol-state inputs (Phase F): last tick's candle closes are the
        # pre-trade marks the interval's flow is measured against; the
        # previous market vol state seeds the shared EWMA; pending_flow
        # holds every impact-moving write since the last step. Every flow
        # writer locks its instrument row first, so the table is quiescent
        # while this tick holds all of them.
        prev_closes: dict[int, float] = {}
        v_mkt_prev = 1.0
        if tick_index > 0:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT instrument_id, close FROM candles WHERE tick_index = %s",
                    (tick_index - 1,),
                )
                prev_closes = {int(r[0]): float(r[1]) for r in await cur.fetchall()}
                await cur.execute(
                    "SELECT vol_state FROM market_ticks WHERE tick_index = %s",
                    (tick_index - 1,),
                )
                vol_row = await cur.fetchone()
                if vol_row and vol_row[0] is not None:
                    v_mkt_prev = float(vol_row[0])
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM pending_flow "
                "RETURNING instrument_id, user_id, delta_impact"
            )
            flow_rows = await cur.fetchall()
        flow_delta: dict[int, dict[int, float]] = {}
        for fr in flow_rows:
            flow_delta.setdefault(int(fr[0]), {})[int(fr[1])] = float(fr[2])
        vol_cfg = await data.vol_config(conn)
        flow_cfg = await data.flow_config(conn)
        mom_cfg = await data.mom_config(conn)

        # Phase H flow decomposition, computed up front because H2's
        # cross-impact needs every instrument's bounded flow before any
        # of them step. `bounded_own` is the F5-clipped per-instrument
        # flow (each account clipped at account_flow_cap, total at
        # flow_ret_cap); `cross_inflow` is the sector-sympathy term --
        # each instrument's bounded flow bleeds cross_impact_coeff into
        # same-sector peers, scaled by the PEER's gamma loading. One hop
        # by construction: the inflow lands on impact directly and is
        # never re-recorded into pending_flow.
        flow_cap = float(vol_cfg.get("vol.flow_ret_cap", 0.05))
        acct_cap = float(vol_cfg.get("vol.account_flow_cap", 0.02))
        bounded_own: dict[int, float] = {}
        sector_flow: dict[str, float] = {}
        sector_of = {int(r["id"]): str(r["sector_key"]) for r in instrument_rows}
        for iid, deltas in flow_delta.items():
            bf, _raw = data.bound_flow(deltas, acct_cap, flow_cap)
            bounded_own[iid] = bf
            skey = sector_of.get(iid)
            if skey is not None:
                sector_flow[skey] = sector_flow.get(skey, 0.0) + bf
        cross_coeff = float(flow_cfg.get("flow.cross_impact_coeff", 0.15))
        cross_inflow: dict[int, float] = {
            int(r["id"]): cross_coeff
            * float(r["gamma"])
            * (sector_flow.get(str(r["sector_key"]), 0.0)
               - bounded_own.get(int(r["id"]), 0.0))
            for r in instrument_rows
        }

        sector_keys = sorted({row["sector_key"] for row in instrument_rows})
        rng = engine.rng_for_tick(master_seed, tick_index)
        market_factor = engine.draw_market_factor(rng, dt=gap_dt)
        sector_factors = engine.draw_sector_factors(rng, sector_keys, dt=gap_dt)

        # Shared market vol state: EWMA of the normalized market factor.
        # draw_market_factor already folds in sqrt(dt), so dividing it back
        # out leaves |z|/sqrt(2/pi) -- stationary mean ~1.
        rho = float(vol_cfg.get("vol.rho", 0.94))
        v_mkt = engine.ewma_vol_update(
            v_mkt_prev,
            abs(market_factor)
            / (engine.MARKET_SIGMA * math.sqrt(gap_dt) * engine.SQRT_2_OVER_PI),
            rho,
        )

        # Events (earnings + news) use their own deterministic RNG stream so
        # they can't perturb the price engine's own draw sequence.
        events_rng = engine.rng_for_tick(f"{master_seed}|events", tick_index)
        await events.schedule_initial_earnings(conn, events_rng, tick_index)
        await events.schedule_initial_dividends(conn, events_rng, tick_index)
        stats: dict[str, int] = {}
        fundamentals = {row["id"]: float(row["fundamental_value"]) for row in instrument_rows}
        fundamentals, dividend_drops = await events.resolve_due_events(
            conn, events_rng, tick_index, fundamentals, stats=stats
        )
        await events.maybe_create_news(conn, events_rng, tick_index, instrument_rows)
        await events.refresh_next_event_ticks(conn)

        results: list[engine.InstrumentTickResult] = []
        halts: dict[int, int | None] = {}
        opens: dict[int, float] = {}

        index_rows = [row for row in instrument_rows if row["kind"] == "INDEX"]
        for row in instrument_rows:
            if row["kind"] == "INDEX":
                continue  # priced from components below
            # Ex-date drop: the dividend subtracts from the base (immediate,
            # permanent) rather than the decaying impact term. This drop is
            # the *entire* funding mechanism -- dividend_drift_offset is
            # always 0 now (a pre-bleed would double-suppress the payout).
            div_drop = float(dividend_drops.get(row["id"], 0))
            # sigma_eff is last tick's regime-scaled sigma; NULL before the
            # first vol update means "use raw sigma" (v=1 -> multiplier 1).
            sigma_eff = (
                float(row["sigma_eff"])
                if row["sigma_eff"] is not None
                else float(row["sigma"])
            )
            state = engine.InstrumentState(
                id=row["id"],
                sector_key=row["sector_key"],
                drift=float(row["drift"]) - float(row["dividend_drift_offset"]),
                sigma=sigma_eff,
                beta=float(row["beta"]),
                gamma=float(row["gamma"]),
                kappa=float(row["kappa"]),
                fundamental_sigma=float(row["fundamental_sigma"]),
                tau_ticks=float(row["tau_ticks"]),
                base_price=max(
                    events.MIN_POST_DIV_PRICE, float(row["base_price"]) - div_drop
                ),
                fundamental_value=fundamentals[row["id"]],
                impact=float(row["impact"]),
                drift_state=float(row["drift_state"]),
            )
            opens[state.id] = state.base_price * math.exp(state.impact)

            # `>=`: halted_until is the *last* frozen tick, so a breach at T
            # freezes exactly CIRCUIT_HALT_TICKS ticks (T+1 .. T+N) and the
            # instrument steps again at T+N+1.
            still_halted = (
                row["circuit_halted_until_tick"] is not None
                and row["circuit_halted_until_tick"] >= tick_index
            )
            if still_halted:
                result = engine.freeze_instrument(state, dt=gap_dt)
                halts[state.id] = row["circuit_halted_until_tick"]
            else:
                result = engine.step_instrument(
                    rng,
                    state,
                    market_factor,
                    sector_factors[state.sector_key],
                    dt=gap_dt,
                    mom_rho=float(mom_cfg.get("mom.rho", 1.0)),
                    mom_innov_frac=float(mom_cfg.get("mom.innov_frac", 0.0)),
                    mom_max_frac=float(mom_cfg.get("mom.max_frac", 0.0)),
                )
                halts[state.id] = (
                    tick_index + engine.CIRCUIT_HALT_TICKS if result.circuit_breached else None
                )
            # Overnight open: the book absorbs part of the stale impact
            # (new_impact already decayed by dt inside the step/freeze).
            if impact_reset != 1.0:
                new_impact = result.impact * impact_reset
                result = engine.InstrumentTickResult(
                    id=result.id,
                    base_price=result.base_price,
                    fundamental_value=result.fundamental_value,
                    impact=new_impact,
                    quoted_price=result.base_price * math.exp(new_impact),
                    circuit_breached=result.circuit_breached,
                    drift_state=result.drift_state,
                )
            results.append(result)

        # Index instruments: level = cap-weighted component basket / divisor.
        # They draw no factor-model randomness (rng stream untouched) but do
        # carry their own trade impact, which decays like everyone else's.
        quoted_by_id = {res.id: res.quoted_price for res in results}
        for row in index_rows:
            opens[row["id"]] = float(row["base_price"]) * math.exp(float(row["impact"]))
            divisor = float(row["index_divisor"])
            level = (
                sum(
                    float(r["float_shares"]) * quoted_by_id[r["id"]]
                    for r in instrument_rows
                    # index_member is the fixed v1 basket (0031): listings
                    # never join it, and a delisted member is divisor-
                    # adjusted out by admin.delist_instrument.
                    if r["kind"] != "INDEX" and r["index_member"]
                )
                / divisor
            )
            decayed = (
                float(row["impact"])
                * math.exp(-gap_dt / float(row["tau_ticks"]))
                * impact_reset
            )
            results.append(
                engine.InstrumentTickResult(
                    id=row["id"],
                    base_price=level,
                    fundamental_value=level,
                    impact=decayed,
                    quoted_price=level * math.exp(decayed),
                    circuit_breached=False,
                )
            )
            halts[row["id"]] = None

        # Vol-state update (F1) + flow-bounded breaker extension (F5). The
        # tick's return decomposes into the model move r_model = ln(close /
        # open) and the flow move carried in from this interval's trades.
        # Flow enters the EWMA numerator only after the F5 bounds: each
        # account's contribution clips at vol.account_flow_cap, the total
        # at vol.flow_ret_cap. The breaker checks r_model + bounded flow --
        # a lone account is capped below any sane cap, so a whale alone can
        # never manufacture a halt, while crowd-scale flow still can (and
        # gets the shorter flow_halt_ticks instead of CIRCUIT_HALT_TICKS).
        clip_lo = float(vol_cfg.get("vol.clip_min", 0.5))
        clip_hi = float(vol_cfg.get("vol.clip_max", 4.0))
        mkt_w = float(vol_cfg.get("vol.market_weight", 0.5))
        flow_halt = int(vol_cfg.get("vol.flow_halt_ticks", 2))
        perm_frac = float(flow_cfg.get("flow.permanent_frac", 0.10))
        perm_cap = float(flow_cfg.get("flow.max_fundamental_move", 0.005))
        skew_decay = float(flow_cfg.get("flow.skew_decay", 0.9))
        sqrt_dt = math.sqrt(gap_dt)
        rows_by_id = {int(r["id"]): r for r in instrument_rows}
        vol_new: dict[int, float] = {}
        sigma_eff_new: dict[int, float] = {}
        flow_ret: dict[int, float] = {}
        model_ret: dict[int, float] = {}
        skew_new: dict[int, float] = {}
        flow_breached: set[int] = set()
        for res_idx, result in enumerate(results):
            row = rows_by_id[result.id]
            sigma_i = float(row["sigma"])
            v_prev = (
                float(row["vol_state"]) if row["vol_state"] is not None else 1.0
            )
            open_mark = opens[result.id]
            prev_close = prev_closes.get(result.id)
            own_flow = bounded_own.get(result.id, 0.0)
            is_index = row["kind"] == "INDEX"
            # A halted instrument's mark is frozen: it keeps its own flow
            # for vol accounting but receives no sector sympathy move and
            # no permanent transfer.
            frozen = halts[result.id] is not None
            xflow = (
                0.0
                if frozen or is_index
                else cross_inflow.get(result.id, 0.0)
            )
            bounded_flow = max(-flow_cap, min(flow_cap, own_flow + xflow))
            # model_ret is the pure step return (post-step, pre-fill AND
            # pre-flow-application): fills amend the candle close later, so
            # it must be persisted now or the replay vol check can't
            # reconstruct it.
            r_model = (
                math.log(result.quoted_price / open_mark) if open_mark > 0 else 0.0
            )
            model_ret[result.id] = r_model
            v_i = v_prev
            if prev_close is not None and prev_close > 0 and open_mark > 0:
                if sigma_i > 0:
                    numerator = (abs(r_model) / sqrt_dt + abs(bounded_flow)) / (
                        sigma_i * engine.SQRT_2_OVER_PI
                    )
                    v_i = engine.ewma_vol_update(v_prev, numerator, rho)
                if (
                    halts[result.id] is None
                    and abs(r_model + bounded_flow) > engine.CIRCUIT_BREAKER_CAP
                ):
                    # Reached only when the model alone did NOT breach --
                    # by construction this is a flow-attributed breach.
                    halts[result.id] = tick_index + flow_halt
                    flow_breached.add(result.id)
            # H2: sector sympathy -- bounded peer flow moves this mark.
            # Direct impact write, never re-recorded as flow (one hop).
            new_impact = result.impact + xflow
            new_fundamental = result.fundamental_value
            # H3: permanent impact -- move a capped fraction of OWN flow out
            # of the decaying impact term into the fundamental, where kappa
            # pulls the base toward it over ~1/kappa ticks. The mark drops
            # by `shift` now and recovers permanently -- no double count.
            shift = (
                0.0
                if frozen or is_index
                else max(-perm_cap, min(perm_cap, perm_frac * own_flow))
            )
            if shift:
                new_fundamental *= math.exp(shift)
                new_impact -= shift
            if xflow or shift:
                results[res_idx] = dataclasses.replace(
                    result,
                    fundamental_value=new_fundamental,
                    impact=new_impact,
                    quoted_price=result.base_price * math.exp(new_impact),
                )
            vol_new[result.id] = v_i
            sigma_eff_new[result.id] = sigma_i * engine.vol_multiplier(
                v_mkt, v_i, mkt_w, clip_lo, clip_hi
            )
            flow_ret[result.id] = bounded_flow
            # Plan A: decaying EWMA of bounded OWN flow -- the adverse-
            # selection signal fill paths use to skew the half-spread.
            # Own flow only (cross-impact is peer sympathy, not this name's
            # tape); decays toward 0 when the tape goes quiet.
            skew_new[result.id] = skew_decay * float(
                row["flow_skew"] or 0.0
            ) + (1.0 - skew_decay) * own_flow

        # Single-statement writes: executemany still round-trips per row,
        # which dominates tick latency (~100ms -> ~15ms measured on a local
        # Docker Postgres). One UPDATE ... FROM (VALUES ...) and one
        # multi-row INSERT collapse ~80 waits into 2.
        async with conn.cursor() as cur:
            if results:
                update_rows = sql.SQL(", ").join(
                    sql.SQL("({})").format(
                        sql.SQL(", ").join(sql.Placeholder() for _ in range(11))
                    )
                    for _ in results
                )
                await cur.execute(
                    sql.SQL(
                        """
                        UPDATE instruments i
                        SET base_price = v.base_price::numeric,
                            fundamental_value = v.fundamental_value::numeric,
                            impact = v.impact::numeric,
                            quoted_price = v.quoted_price::numeric,
                            circuit_halted_until_tick = v.circuit_halted_until_tick::bigint,
                            last_halt_end_tick = COALESCE(
                                v.new_halt_end::bigint, i.last_halt_end_tick),
                            vol_state = v.vol_state::numeric,
                            sigma_eff = v.sigma_eff::numeric,
                            drift_state = v.drift_state::numeric,
                            flow_skew = v.flow_skew::numeric
                        FROM (VALUES {}) AS v(
                            base_price, fundamental_value, impact, quoted_price,
                            circuit_halted_until_tick, new_halt_end,
                            vol_state, sigma_eff, drift_state, flow_skew, id
                        )
                        WHERE i.id = v.id::int
                        """
                    ).format(update_rows),
                    [
                        param
                        for result in results
                        for param in (
                            result.base_price,
                            result.fundamental_value,
                            result.impact,
                            result.quoted_price,
                            halts[result.id],
                            (
                                halts[result.id]
                                if result.circuit_breached
                                or result.id in flow_breached
                                else None
                            ),
                            vol_new[result.id],
                            sigma_eff_new[result.id],
                            result.drift_state,
                            skew_new[result.id],
                            result.id,
                        )
                    ],
                )

            await cur.execute(
                """
                INSERT INTO market_ticks
                    (tick_index, market_factor, sector_factors, session_state,
                     vol_state)
                VALUES (%s, %s, %s, 'OPEN', %s)
                """,
                (tick_index, market_factor, json.dumps(sector_factors), v_mkt),
            )

            if results:
                candle_rows = sql.SQL(", ").join(
                    sql.SQL("({}, 0, {})").format(
                        sql.SQL(", ").join(sql.Placeholder() for _ in range(6)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in range(4)),
                    )
                    for _ in results
                )
                await cur.execute(
                    sql.SQL(
                        """
                        INSERT INTO candles
                            (instrument_id, tick_index, open, high, low, close,
                             volume, vol_state, flow_ret, model_ret, halt_kind)
                        VALUES {}
                        """
                    ).format(candle_rows),
                    [
                        param
                        for result in results
                        for param in (
                            result.id,
                            tick_index,
                            opens[result.id],
                            max(opens[result.id], result.quoted_price),
                            min(opens[result.id], result.quoted_price),
                            result.quoted_price,
                            vol_new[result.id],
                            flow_ret[result.id],
                            model_ret[result.id],
                            (
                                "FLOW"
                                if result.id in flow_breached
                                else "MODEL" if result.circuit_breached else None
                            ),
                        )
                    ],
                )

        # Bounded shorts: knock out any position whose instrument reached its
        # knockout price this tick. Before season snapshots so closed shorts
        # stop contributing to equity.
        kos = await shorts.sweep_knockouts(conn, tick_index)

        # Resting limit orders: fill any whose limit the new marks satisfy.
        # Before the margin sweep so a fill's impact and equity change land
        # in this tick's margin state, not next tick's. Skipped entirely when
        # the orders.enabled kill switch is off -- resting orders wait.
        # On the session's last `session.auction_ticks` open ticks the
        # continuous book clears at a uniform price (Plan C closing
        # auction); the MM fallback is skipped -- the print is the auction.
        if await data.feature_enabled_flag(conn, "orders.enabled"):
            auction_window = int(session_cfg.get("session.auction_ticks", 30))
            closing_auction = (
                auction_window > 0
                and engine.ticks_until_close(
                    tick_index, open_ticks, closed_ticks, offset
                )
                <= auction_window
            )
            await orders.match_orders(
                conn, tick_index, stats=stats, closing_auction=closing_auction
            )

        # Phase 2 margin maintenance, all inside the tick transaction where
        # every instrument is already locked: refresh published short
        # interest, accrue borrow fees on shorts, then liquidate any account
        # that fell below maintenance margin at the new marks. Note the SI
        # refresh runs AFTER match_orders -- order fills this tick see last
        # tick's short_interest_pct (one-tick lag on the squeeze boost,
        # benign).
        await margin.refresh_short_interest(conn)
        await margin.accrue_borrow_fees(conn)
        liqs = await margin.sweep_undermargined(conn, tick_index)

        # H4: trailing ADV refresh -- a sliding-window SMA of per-tick
        # notional volume (candles.volume * close). Once per tick, never
        # per-trade (finding 7): add this tick's notional, subtract the
        # tick falling off the window edge. adv feeds effective liquidity
        # for impact on the NEXT interval's fills.
        adv_window = max(1, int(flow_cfg.get("flow.adv_window_ticks", 7200)))
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE instruments i
                SET adv = GREATEST(0, COALESCE(i.adv, 0)
                        + (COALESCE(nw.n, 0) - COALESCE(ew.n, 0)) / %s)
                FROM (SELECT instrument_id, volume * close AS n
                      FROM candles WHERE tick_index = %s) nw
                FULL JOIN (SELECT instrument_id, volume * close AS n
                           FROM candles WHERE tick_index = %s) ew
                    ON ew.instrument_id = nw.instrument_id
                WHERE i.id = COALESCE(nw.instrument_id, ew.instrument_id)
                """,
                (adv_window, tick_index, tick_index - adv_window),
            )

        # Season lifecycle: activate due seasons, write day-boundary equity
        # snapshots, close finished seasons (all inside this tick's tx).
        await seasons.on_tick(conn, tick_index)

        duration_ms = (time.perf_counter() - started) * 1000
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE market_ticks SET
                    duration_ms = %s, fills = %s, crosses = %s,
                    stops_triggered = %s, knockouts = %s, liquidations = %s,
                    events_resolved = %s, auction_fills = %s
                WHERE tick_index = %s
                """,
                (
                    round(duration_ms, 3),
                    stats.get("crosses", 0) + stats.get("mm_fills", 0),
                    stats.get("crosses", 0),
                    stats.get("stops_triggered", 0),
                    kos,
                    liqs,
                    stats.get("events_resolved", 0),
                    stats.get("auction_fills", 0),
                    tick_index,
                ),
            )
        await write_heartbeat(
            conn, "market", {"tick": tick_index, "phase": "OPEN"}
        )

    log.info(
        "tick=%d phase=OPEN steps=%d events=%d crosses=%d mm_fills=%d "
        "stops=%d kos=%d liqs=%d auction=%d ms=%.1f",
        tick_index,
        len(results),
        stats.get("events_resolved", 0),
        stats.get("crosses", 0),
        stats.get("mm_fills", 0),
        stats.get("stops_triggered", 0),
        kos,
        liqs,
        stats.get("auction_fills", 0),
        duration_ms,
    )
    await _post_tick(conn, tick_index)
    return tick_index


async def _post_tick(conn: AsyncConnection, tick_index: int) -> None:
    """Post-commit tick work: heartbeat-adjacent, non-price tasks that
    must not roll back the tick if they fail. Currently the periodic
    invariant audit (every `audit.every_n_ticks`, own transaction)."""
    try:
        async with conn.transaction():
            # The config read must live INSIDE this transaction: a bare
            # SELECT on this long-lived connection would open an implicit
            # transaction that never commits, silently demoting every
            # subsequent apply_tick's conn.transaction() to a savepoint --
            # ticks would "succeed" while writing nothing (the
            # bootstrap_user trap from AGENTS.md, but for the market).
            if tick_index > 0 and tick_index % await audit_every_n_ticks(conn) == 0:
                await run_periodic_audit(conn, tick_index)
    except Exception:
        # The audit must never poison the tick loop -- a failed audit
        # *query* is itself the signal.
        log.exception("periodic audit failed at tick %d", tick_index)
