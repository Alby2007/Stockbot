"""stockbot replay: verify recorded ticks against the deterministic engine.

The price engine is deterministic: `rng_for_tick(master_seed, tick)` fully
determines each tick's market/sector factors, and every instrument write
must satisfy `quoted_price = base_price * exp(impact)`. This tool replays
the factor draws for stored ticks and diffs them against `market_ticks`,
plus consistency-checks the stored marks and candles -- turning "the
price looks wrong at tick 4532" into a bisect:

- factors diverge  -> seed/draw-order drift (RNG layer)
- factors match but marks/candles are inconsistent -> a write path bug

Limitations by design: instrument *state* (base/impact/fundamental) is
not snapshotted per tick, so individual price paths can't be re-derived
historically; sector keys and session config are read from their current
rows, so a deactivated sector or a mid-range session-config change shows
as factor divergence starting at the change tick. Venue participation is
bounded by each market's first OPEN candle, so a venue seeded mid-history
(0054) replays as absent before its debut -- a venue added WITHOUT
instruments ever stepping (a phantom `markets` row) stays invisible to
the bound and can read as a session_state mismatch.

Narrowed guarantee since Phase F (vol regimes): vol_state is flow-fed --
order flow moves it, and order flow is not re-derivable from the seed.
The vol check therefore verifies *internal consistency* only: it
recomputes each tick's EWMA from the stored candle open/close and the
stored bounded flow_ret column, not from a re-derived model return. A
clean vol replay means "the recorded state obeys the EWMA update rule",
NOT "the flow that produced it was correct". Treat it as weaker evidence
than the factor replay.

Usage:
    python -m stockbot.tools.replay --from 4000 --to 5000 \
        --database-url postgresql://... --master-seed <seed>

Exit code 0 = clean, 1 = divergences found.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from typing import Any

import psycopg

from stockbot.config import get_settings
from stockbot.market import engine


@dataclass
class Report:
    factor_mismatches: list[str] = field(default_factory=list)
    mark_inconsistencies: list[str] = field(default_factory=list)
    candle_inconsistencies: list[str] = field(default_factory=list)
    vol_mismatches: list[str] = field(default_factory=list)
    ticks_checked: int = 0
    ticks_skipped_closed: int = 0

    @property
    def clean(self) -> bool:
        return not (
            self.factor_mismatches
            or self.mark_inconsistencies
            or self.candle_inconsistencies
            or self.vol_mismatches
        )


def _load_markets(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Venue rows as plain dicts -- post-0053 the session shape lives on
    `markets`, not `session.*` config."""
    cur.execute(
        "SELECT id, code, open_ticks, closed_ticks, offset_ticks, "
        "auction_ticks, tick_size, overnight_var_ticks FROM markets ORDER BY id"
    )
    cols = [d.name for d in cur.description or ()]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _venue_debut_ticks(cur: psycopg.Cursor) -> dict[int, int]:
    """First OPEN-state candle tick per market_id -- the venue's actual
    debut. The CURRENT venue set can't be replayed onto history predating
    a venue's debut: a market seeded mid-history (0054) was not open --
    or even listed -- before its instruments produced marks, so counting
    it would manufacture session_state/factor mismatches that aren't
    corruption. OPEN candles specifically: closed ticks write flat
    'CLOSED' candles for every active instrument, so a venue seeded
    mid-close accumulates rows without ever stepping."""
    cur.execute(
        "SELECT i.market_id, MIN(c.tick_index) FROM candles c "
        "JOIN instruments i ON i.id = c.instrument_id "
        "WHERE c.session_state = 'OPEN' GROUP BY i.market_id"
    )
    return {int(mid): int(t) for mid, t in cur.fetchall()}


def _venue_live_at(
    markets: list[dict[str, Any]], debuts: dict[int, int], tick_index: int
) -> list[dict[str, Any]]:
    """Venues with instruments producing marks by `tick_index`."""
    return [
        m
        for m in markets
        if tick_index >= debuts.get(int(m["id"]), 1 << 62)
    ]


def _overnight_var_frac(cur: psycopg.Cursor) -> float:
    """The global fallback frac live `apply_tick` uses when a venue's
    `overnight_var_ticks` is NULL (tick.py falls back to
    `closed * session.overnight_var_frac`, default 1.0)."""
    cur.execute(
        "SELECT value FROM config WHERE key = 'session.overnight_var_frac'"
    )
    row = cur.fetchone()
    return float(row[0]) if row else 1.0


def _venue_gap_dt(
    market: dict[str, Any],
    tick_index: int,
    debut_tick: int | None = None,
    var_frac: float = 1.0,
) -> tuple[float, float]:
    """(clock dt, variance dt) for one venue's step at `tick_index` -- the
    reopen scales stochastic terms by the venue's absolute overnight
    variance horizon (`overnight_var_ticks`; NULL falls back to
    `closed * var_frac` like live), clock terms by closed_ticks. Non-gap
    ticks return (1.0, 1.0). A venue's debut tick is exempt from the gap
    like global tick 0 (no candles behind it = never closed)."""
    if debut_tick is not None and tick_index <= debut_tick:
        return 1.0, 1.0
    open_ticks = int(market["open_ticks"])
    closed_ticks = int(market["closed_ticks"])
    offset = int(market["offset_ticks"])
    cycle = open_ticks + closed_ticks
    if cycle <= 0:
        return 1.0, 1.0
    cycle_pos = (tick_index - offset) % cycle
    if cycle_pos == 0 and closed_ticks > 0 and tick_index > 0:
        var_ticks = market.get("overnight_var_ticks")
        vdt = (
            float(var_ticks)
            if var_ticks is not None
            else float(closed_ticks) * var_frac
        )
        return float(closed_ticks), max(1.0, vdt)
    return 1.0, 1.0


def _union_gap_dt(
    markets: list[dict[str, Any]],
    tick_index: int,
    debuts: dict[int, int] | None = None,
    var_frac: float = 1.0,
) -> tuple[float, float]:
    """(clock dt, variance dt) for the shared factor draw at `tick_index`:
    the largest gap across venues reopening this tick, else (1.0, 1.0).
    Per-instrument steps rescale the shared draw by sqrt(their own var_dt
    / the draw's var_dt), so this only sets the recorded draw scale."""
    dt, vdt = 1.0, 1.0
    for m in markets:
        mid = int(m["id"])
        if debuts is not None and tick_index < debuts.get(mid, 1 << 62):
            continue  # venue not yet live at this tick
        mdt, mvdt = _venue_gap_dt(
            m,
            tick_index,
            None if debuts is None else debuts.get(mid),
            var_frac,
        )
        if mvdt > vdt:
            dt, vdt = mdt, mvdt
    return dt, vdt


def _any_venue_open(
    markets: list[dict[str, Any]],
    tick_index: int,
    debuts: dict[int, int] | None = None,
) -> bool:
    if debuts is not None:
        markets = _venue_live_at(markets, debuts, tick_index)
    return any(
        engine.session_phase(
            tick_index,
            int(m["open_ticks"]),
            int(m["closed_ticks"]),
            int(m["offset_ticks"]),
        )
        == "OPEN"
        for m in markets
    )


def replay_factors(
    cur: psycopg.Cursor,
    *,
    master_seed: str,
    tick_from: int,
    tick_to: int,
    report: Report,
) -> None:
    cur.execute(
        "SELECT tick_index, market_factor, sector_factors, session_state "
        "FROM market_ticks WHERE tick_index BETWEEN %s AND %s ORDER BY tick_index",
        (tick_from, tick_to),
    )
    rows = cur.fetchall()

    markets = _load_markets(cur)
    debuts = _venue_debut_ticks(cur)
    var_frac = _overnight_var_frac(cur)

    cur.execute(
        "SELECT DISTINCT s.key FROM instruments i "
        "JOIN sectors s ON s.id = i.sector_id WHERE i.is_active"
    )
    # The engine sorts sector keys with Python's ASCII `sorted()` -- the
    # DB's ORDER BY uses locale collation (puts 'index' before
    # 'INDUSTRIAL'), which would shift every draw by one.
    sector_keys = sorted(row[0] for row in cur.fetchall())

    for tick_index, stored_mf, stored_sf, phase in rows:
        # Cross-check the recorded phase: post-0053 the market_ticks row is
        # an "any venue open" summary -- a wrong stored session_state is
        # itself a bug (a CLOSED row would silently skip factor replay).
        expected_phase = (
            "OPEN" if _any_venue_open(markets, tick_index, debuts) else "CLOSED"
        )
        if phase != expected_phase:
            report.factor_mismatches.append(
                f"tick {tick_index}: session_state stored={phase} "
                f"expected={expected_phase} (any-venue-open summary)"
            )
        if phase == "CLOSED":
            report.ticks_skipped_closed += 1
            continue
        report.ticks_checked += 1
        rng = engine.rng_for_tick(master_seed, tick_index)
        dt, vdt = _union_gap_dt(markets, tick_index, debuts, var_frac)
        expected_mf = engine.draw_market_factor(rng, dt=dt, var_dt=vdt)
        expected_sf = engine.draw_sector_factors(
            rng, sector_keys, dt=dt, var_dt=vdt
        )

        if not math.isclose(expected_mf, float(stored_mf), rel_tol=0, abs_tol=1e-9):
            report.factor_mismatches.append(
                f"tick {tick_index}: market_factor stored={float(stored_mf):.10f} "
                f"expected={expected_mf:.10f}"
            )
        for key in sector_keys:
            stored_val = (stored_sf or {}).get(key)
            expected_val = expected_sf[key]
            if stored_val is None:
                report.factor_mismatches.append(
                    f"tick {tick_index}: sector {key} missing from stored factors"
                )
            elif not math.isclose(
                float(stored_val), expected_val, rel_tol=0, abs_tol=1e-9
            ):
                report.factor_mismatches.append(
                    f"tick {tick_index}: sector {key} stored={float(stored_val):.10f} "
                    f"expected={expected_val:.10f}"
                )


def check_mark_consistency(cur: psycopg.Cursor, report: Report) -> None:
    cur.execute(
        "SELECT ticker, base_price, impact, quoted_price FROM instruments WHERE is_active"
    )
    for ticker, base_price, impact, quoted in cur.fetchall():
        expected = float(base_price) * math.exp(float(impact))
        actual = float(quoted)
        # quoted_price is NUMERIC(18,6): the stored value is rounded to 6
        # decimals, so the invariant holds only up to column precision.
        if not math.isclose(expected, actual, rel_tol=1e-6):
            report.mark_inconsistencies.append(
                f"{ticker}: quoted={actual:.8f} != base*exp(impact)={expected:.8f} "
                f"(impact={float(impact):.8f})"
            )


def check_candles(
    cur: psycopg.Cursor, tick_from: int, tick_to: int, report: Report
) -> None:
    cur.execute(
        """
        SELECT i.ticker, c.tick_index, c.open, c.high, c.low, c.close, c.volume,
               c.session_state
        FROM candles c
        JOIN instruments i ON i.id = c.instrument_id
        WHERE c.tick_index BETWEEN %s AND %s
        """,
        (tick_from, tick_to),
    )
    for ticker, tick, o, h, lo, c, vol, phase in cur.fetchall():
        o, h, lo, c, vol = (
            float(o), float(h), float(lo), float(c), float(vol)
        )
        prefix = f"{ticker}@{tick}"
        if h < max(o, c) or lo > min(o, c):
            report.candle_inconsistencies.append(
                f"{prefix}: OHLC unordered o={o} h={h} l={lo} c={c}"
            )
        if vol < 0:
            report.candle_inconsistencies.append(f"{prefix}: negative volume {vol}")
        if phase == "CLOSED" and (o != c or vol != 0):
            report.candle_inconsistencies.append(
                f"{prefix}: CLOSED tick not flat o={o} c={c} vol={vol}"
            )


def check_vol_state(
    cur: psycopg.Cursor, tick_from: int, tick_to: int, report: Report
) -> None:
    """Recompute the vol-state EWMAs from stored candle opens/closes and
    the stored flow_ret column, and diff against the stored vol_state.

    This is an internal-consistency check, NOT seed-determinism: flow_ret
    is consumed-and-gone once apply_tick uses it, so it must be trusted
    from the record (see the module docstring's narrowed guarantee). The
    first stored candle per instrument in the range self-seeds the EWMA,
    so transitions are verified, not absolute levels.
    """
    cur.execute("SELECT key, value FROM config WHERE key LIKE 'vol.%'")
    cfg = {str(k): float(v) for k, v in cur.fetchall()}
    rho = cfg.get("vol.rho", 0.94)
    markets = _load_markets(cur)
    markets_by_id = {int(m["id"]): m for m in markets}
    debuts = _venue_debut_ticks(cur)
    var_frac = _overnight_var_frac(cur)

    cur.execute(
        """
        SELECT c.instrument_id, c.tick_index, c.open, c.close,
               c.vol_state, c.flow_ret, c.model_ret, i.sigma, c.session_state,
               i.market_id
        FROM candles c
        JOIN instruments i ON i.id = c.instrument_id
        WHERE c.tick_index BETWEEN %s AND %s
        ORDER BY c.instrument_id, c.tick_index
        """,
        (tick_from, tick_to),
    )
    v_prev: dict[int, float] = {}
    for iid, tick, o, c, vs, fr, mr, sigma, phase, market_id in cur.fetchall():
        if vs is None:
            continue  # pre-0022 candle: nothing stored to verify
        iid = int(iid)
        stored = float(vs)
        if phase == "CLOSED" or iid not in v_prev:
            # Closed ticks freeze vol state; the first sighting seeds.
            if iid in v_prev and stored != v_prev[iid]:
                report.vol_mismatches.append(
                    f"instrument {iid}@{tick}: CLOSED-tick vol_state "
                    f"{stored:.6f} != carried {v_prev[iid]:.6f}"
                )
            v_prev[iid] = stored
            continue
        market = markets_by_id.get(int(market_id))
        _dt, vdt = (
            _venue_gap_dt(
                market,
                int(tick),
                debuts.get(int(market_id)),
                var_frac,
            )
            if market is not None
            else (1.0, 1.0)
        )
        # model_ret is the pre-fill step return; the candle close already
        # contains this tick's fills, so ln(close/open) would double-count
        # flow. Fall back to it only for rows predating the column.
        r_model = float(mr) if mr is not None else math.log(float(c) / float(o))
        flow = float(fr) if fr is not None else 0.0
        if float(sigma) <= 0:
            # e.g. the index basket: the tick leaves vol_state untouched.
            expected = v_prev[iid]
        else:
            numerator = (abs(r_model) / math.sqrt(vdt) + abs(flow)) / (
                float(sigma) * engine.SQRT_2_OVER_PI
            )
            expected = engine.ewma_vol_update(v_prev[iid], numerator, rho)
        if not math.isclose(expected, stored, rel_tol=1e-4, abs_tol=1e-4):
            report.vol_mismatches.append(
                f"instrument {iid}@{tick}: vol_state stored={stored:.6f} "
                f"expected={expected:.6f} (r_model={r_model:.6f} flow={flow:.6f})"
            )
        v_prev[iid] = stored

    # Same check for the shared market-wide EWMA on market_ticks.
    cur.execute(
        "SELECT tick_index, market_factor, session_state, vol_state "
        "FROM market_ticks WHERE tick_index BETWEEN %s AND %s "
        "ORDER BY tick_index",
        (tick_from, tick_to),
    )
    v_mkt: float | None = None
    for tick, mf, phase, vs in cur.fetchall():
        if vs is None:
            continue
        stored = float(vs)
        if phase == "CLOSED" or v_mkt is None:
            if v_mkt is not None and stored != v_mkt:
                report.vol_mismatches.append(
                    f"market@{tick}: CLOSED-tick vol_state {stored:.6f} "
                    f"!= carried {v_mkt:.6f}"
                )
            v_mkt = stored
            continue
        _dt, vdt = _union_gap_dt(markets, int(tick), debuts, var_frac)
        numerator = abs(float(mf)) / (
            engine.MARKET_SIGMA * math.sqrt(vdt) * engine.SQRT_2_OVER_PI
        )
        expected = engine.ewma_vol_update(v_mkt, numerator, rho)
        if not math.isclose(expected, stored, rel_tol=1e-4, abs_tol=1e-4):
            report.vol_mismatches.append(
                f"market@{tick}: vol_state stored={stored:.6f} "
                f"expected={expected:.6f}"
            )
        v_mkt = stored


def run(
    database_url: str,
    master_seed: str,
    tick_from: int,
    tick_to: int,
) -> Report:
    report = Report()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        replay_factors(
            cur,
            master_seed=master_seed,
            tick_from=tick_from,
            tick_to=tick_to,
            report=report,
        )
        check_mark_consistency(cur, report)
        check_candles(cur, tick_from, tick_to, report)
        check_vol_state(cur, tick_from, tick_to, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--master-seed", default=None)
    parser.add_argument("--from", dest="tick_from", type=int, required=True)
    parser.add_argument("--to", dest="tick_to", type=int, required=True)
    parser.add_argument(
        "--show", type=int, default=20, help="max divergences printed per check"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    database_url = args.database_url or settings.database_url
    master_seed = args.master_seed or settings.master_seed

    report = run(database_url, master_seed, args.tick_from, args.tick_to)
    print(
        f"Replayed ticks {args.tick_from}..{args.tick_to}: "
        f"{report.ticks_checked} open checked, "
        f"{report.ticks_skipped_closed} closed skipped"
    )
    for label, items in (
        ("factor mismatches", report.factor_mismatches),
        ("mark inconsistencies", report.mark_inconsistencies),
        ("candle inconsistencies", report.candle_inconsistencies),
        ("vol mismatches", report.vol_mismatches),
    ):
        if items:
            print(f"\n{len(items)} {label}:")
            for item in items[: args.show]:
                print(f"  {item}")
            if len(items) > args.show:
                print(f"  ... and {len(items) - args.show} more")
        else:
            print(f"{label}: none")
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
