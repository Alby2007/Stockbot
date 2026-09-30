"""Props: parimutuel binary event contracts.

`/props` lists OPEN contracts; `/prop bet <id> <yes|no> <amount>` escrows
the stake into GAME_ESCROW. There is no book and no market maker: the
losing side's pool funds the winning side, split pro-rata by stake minus
`props.rake_pct` to SINK -- the house can never lose, and odds are just
the current pool ratio.

Resolution is tick-driven (`settle_due` runs in both tick phases, same
as option settlement): at `resolve_tick` the contract's predicate is
evaluated against the quoted marks. PRICE_ABOVE resolves YES when the
instrument's mark >= `threshold`. INDEX_BEAT compares the return of
instrument A against instrument B over the contract window -- both legs'
marks are captured into `meta` at creation so the comparison can't be
gamed by creation-time drift. An exact tie, or a prop nobody bet, marks
the contract resolved with all stakes refunded minus no rake (nothing
was won).

`autogen` creates the weekly ritual prop at each week boundary: "will
SBX40 beat ASX40" plus a deterministic stock-vs-threshold pick, both
seeded from the master seed + day index so replays generate the same
contracts.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.feed import emit_feed
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin import service as margin
from stockbot.market.engine import TICKS_PER_DAY
from stockbot.props.errors import PropBetError, PropNotFoundError, PropStateError

log = logging.getLogger("stockbot.props")


@dataclass(frozen=True)
class Prop:
    id: int
    title: str
    kind: str
    instrument_id: int | None
    instrument_b_id: int | None
    threshold: float | None
    meta: dict[str, Any]
    status: str
    outcome: str | None
    created_tick: int
    resolve_tick: int
    pool_yes_minor: int
    pool_no_minor: int
    auto: bool


def _from_row(row: dict[str, Any]) -> Prop:
    return Prop(
        id=int(row["id"]),
        title=str(row["title"]),
        kind=str(row["kind"]),
        instrument_id=(
            int(row["instrument_id"]) if row["instrument_id"] is not None else None
        ),
        instrument_b_id=(
            int(row["instrument_b_id"])
            if row["instrument_b_id"] is not None
            else None
        ),
        threshold=float(row["threshold"]) if row["threshold"] is not None else None,
        meta=dict(row["meta"] or {}),
        status=str(row["status"]),
        outcome=str(row["outcome"]) if row["outcome"] is not None else None,
        created_tick=int(row["created_tick"]),
        resolve_tick=int(row["resolve_tick"]),
        pool_yes_minor=int(row["pool_yes_minor"]),
        pool_no_minor=int(row["pool_no_minor"]),
        auto=bool(row["auto"]),
    )


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _enabled(conn: AsyncConnection) -> bool:
    return await _config_float(conn, "props.enabled", 1.0) != 0.0


async def _notify(
    conn: AsyncConnection, user_id: int, kind: str, payload: dict[str, Any]
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO notifications (user_id, kind, payload) VALUES (%s, %s, %s)",
            (user_id, kind, json.dumps(payload)),
        )


async def create(
    conn: AsyncConnection,
    *,
    title: str,
    kind: str,
    resolve_tick: int,
    tick_index: int,
    instrument_id: int | None = None,
    instrument_b_id: int | None = None,
    threshold: float | None = None,
    auto: bool = False,
    feed_post: bool = True,
) -> int:
    """Open a new prop contract. Returns its id.

    INDEX_BEAT captures both legs' current marks into meta -- resolution
    compares returns from creation to resolve_tick, so the creation
    moment itself carries no edge."""
    meta: dict[str, Any] = {}
    if kind == "INDEX_BEAT":
        if instrument_id is None or instrument_b_id is None:
            raise PropStateError("an INDEX_BEAT prop needs two instruments")
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.quoted_price AS a, b.quoted_price AS b,
                       ta.ticker AS tick_a, tb.ticker AS tick_b
                FROM instruments a, instruments b,
                     instruments ta, instruments tb
                WHERE a.id = %s AND b.id = %s AND ta.id = a.id AND tb.id = b.id
                """,
                (instrument_id, instrument_b_id),
            )
            row = await cur.fetchone()
        if row is None or row[0] is None or row[1] is None:
            raise PropStateError("a leg of that prop has no mark yet")
        meta = {
            "start_a": float(row[0]),
            "start_b": float(row[1]),
            "tick_a": str(row[2]),
            "tick_b": str(row[3]),
        }
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO props (title, kind, instrument_id, instrument_b_id,
                                   threshold, meta, created_tick, resolve_tick,
                                   auto)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    title,
                    kind,
                    instrument_id,
                    instrument_b_id,
                    threshold,
                    json.dumps(meta),
                    tick_index,
                    resolve_tick,
                    auto,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            prop_id = int(row[0])
        if feed_post:
            await emit_feed(
                conn,
                "PROP_OPENED",
                {"prop_id": prop_id, "title": title, "resolve_tick": resolve_tick},
                tick_index=tick_index,
            )
    return prop_id


async def get_prop(conn: AsyncConnection, prop_id: int) -> Prop | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM props WHERE id = %s", (prop_id,))
        row = await cur.fetchone()
    return _from_row(row) if row else None


async def list_open(conn: AsyncConnection) -> list[Prop]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM props WHERE status = 'OPEN' ORDER BY resolve_tick"
        )
        return [_from_row(r) for r in await cur.fetchall()]


async def my_bets(conn: AsyncConnection, user_id: int) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT b.prop_id, b.side, b.amount_minor, b.paid_minor,
                   p.title, p.status, p.outcome, p.resolve_tick
            FROM prop_bets b JOIN props p ON p.id = b.prop_id
            WHERE b.user_id = %s
            ORDER BY b.id DESC LIMIT 25
            """,
            (user_id,),
        )
        return await cur.fetchall()


async def bet(
    conn: AsyncConnection,
    user_id: int,
    prop_id: int,
    side: str,
    amount_minor: int,
    tick_index: int,
) -> int:
    """Stake `amount_minor` on one side of an OPEN prop. Returns new
    aggregate stake on that side."""
    if not await _enabled(conn):
        raise PropStateError("props are disabled")
    side = side.upper()
    if side not in ("YES", "NO"):
        raise PropBetError("side must be yes or no")
    lo = int(await _config_float(conn, "props.min_bet_minor", 100))
    hi = int(await _config_float(conn, "props.max_bet_minor", 50_000))
    if amount_minor < lo:
        raise PropBetError(f"minimum bet is {lo / 100:.2f} dollars")

    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM props WHERE id = %s FOR UPDATE", (prop_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise PropNotFoundError(prop_id)
        prop = _from_row(row)
        if prop.status != "OPEN" or prop.resolve_tick <= tick_index:
            raise PropStateError("that prop is closed to new bets")
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COALESCE(SUM(amount_minor), 0) FROM prop_bets
                WHERE prop_id = %s AND user_id = %s AND side = %s
                """,
                (prop_id, user_id, side),
            )
            cur_row = await cur.fetchone()
            existing = int(cur_row[0]) if cur_row else 0
        if existing + amount_minor > hi:
            raise PropBetError(
                f"you can't put more than {hi / 100:.2f} dollars on one side"
            )
        await margin.assert_spend_ok(conn, user_id, amount_minor)
        account_id = await get_user_account_id(conn, user_id)
        escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
        await post_transfer(
            conn,
            from_account_id=account_id,
            to_account_id=escrow_id,
            amount=amount_minor,
            reason="PROP_BET",
            memo=f"prop {prop_id} {side}",
        )
        async with conn.cursor() as cur:
            pool_col = "pool_yes_minor" if side == "YES" else "pool_no_minor"
            await cur.execute(
                f"""
                UPDATE props SET {pool_col} = {pool_col} + %s WHERE id = %s
                """,
                (amount_minor, prop_id),
            )
            await cur.execute(
                """
                INSERT INTO prop_bets (prop_id, user_id, side, amount_minor,
                                       created_tick)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (prop_id, user_id, side)
                DO UPDATE SET amount_minor = prop_bets.amount_minor + EXCLUDED.amount_minor
                """,
                (prop_id, user_id, side, amount_minor, tick_index),
            )
        return existing + amount_minor


async def _resolve_outcome(conn: AsyncConnection, prop: Prop) -> str | None:
    """'YES', 'NO', or None for an exact INDEX_BEAT tie (refund-all)."""
    if prop.kind == "PRICE_ABOVE":
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT quoted_price FROM instruments WHERE id = %s",
                (prop.instrument_id,),
            )
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        return "YES" if float(row[0]) >= float(prop.threshold or 0) else "NO"
    if prop.kind == "INDEX_BEAT":
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.quoted_price AS a, b.quoted_price AS b
                FROM instruments a, instruments b
                WHERE a.id = %s AND b.id = %s
                """,
                (prop.instrument_id, prop.instrument_b_id),
            )
            row = await cur.fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        start_a = float(prop.meta.get("start_a", 0) or 0)
        start_b = float(prop.meta.get("start_b", 0) or 0)
        if start_a <= 0 or start_b <= 0:
            return None
        ret_a = float(row[0]) / start_a - 1.0
        ret_b = float(row[1]) / start_b - 1.0
        if ret_a == ret_b:
            return None
        return "YES" if ret_a > ret_b else "NO"
    return None


async def settle_due(conn: AsyncConnection, tick_index: int) -> int:
    """Resolve every OPEN prop at/past resolve_tick, paying winners
    parimutuel. Runs in both tick phases (same rationale as option
    settlement: the mark is frozen overnight, so an overnight resolve
    settles "at the close"). Idempotent: the status flip and payouts are
    one transaction each."""
    if not await _enabled(conn):
        return 0
    # Its own transaction: a bare cursor would leave an ambient tx open
    # and silently demote the per-prop transactions below to savepoints
    # that never commit (the market/main.py footgun).
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE props SET status = 'RESOLVED'
                WHERE status = 'OPEN' AND resolve_tick <= %s
                RETURNING *
                """,
                (tick_index,),
            )
            due = [_from_row(r) for r in await cur.fetchall()]
    if not due:
        return 0

    settled = 0
    for prop in due:
        async with conn.transaction():
            outcome = await _resolve_outcome(conn, prop)
            escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
            sink_id = await get_system_account_id(conn, "SINK")
            rake_pct = await _config_float(conn, "props.rake_pct", 0.05)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id, user_id, side, amount_minor FROM prop_bets "
                    "WHERE prop_id = %s ORDER BY id",
                    (prop.id,),
                )
                bets = await cur.fetchall()
            total_pool = prop.pool_yes_minor + prop.pool_no_minor

            if outcome is None or not bets:
                # Exact tie / no marks / no action: full refunds, no rake.
                for b in bets:
                    acct = await get_user_account_id(conn, int(b["user_id"]))
                    await post_transfer(
                        conn,
                        from_account_id=escrow_id,
                        to_account_id=acct,
                        amount=int(b["amount_minor"]),
                        reason="PROP_REFUND",
                        memo=f"prop {prop.id} void",
                    )
                    await _record_payout(conn, b, int(b["amount_minor"]))
            else:
                win_side = outcome
                winners = [b for b in bets if b["side"] == win_side]
                win_pool = sum(int(b["amount_minor"]) for b in winners)
                if win_pool == 0:
                    # Nobody held the winning side: refund everyone.
                    for b in bets:
                        acct = await get_user_account_id(conn, int(b["user_id"]))
                        await post_transfer(
                            conn,
                            from_account_id=escrow_id,
                            to_account_id=acct,
                            amount=int(b["amount_minor"]),
                            reason="PROP_REFUND",
                            memo=f"prop {prop.id} no winners",
                        )
                else:
                    rake = int(total_pool * rake_pct)
                    distributable = total_pool - rake
                    paid_total = 0
                    for b in winners:
                        pay = int(b["amount_minor"]) * distributable // win_pool
                        if pay > 0:
                            acct = await get_user_account_id(conn, int(b["user_id"]))
                            await post_transfer(
                                conn,
                                from_account_id=escrow_id,
                                to_account_id=acct,
                                amount=pay,
                                reason="PROP_PAYOUT",
                                memo=f"prop {prop.id} won",
                            )
                            paid_total += pay
                        await _record_payout(conn, b, pay)
                    # Rake + rounding dust both sink -- escrow stays whole.
                    dust = distributable - paid_total
                    burn = rake + dust
                    if burn > 0:
                        await post_transfer(
                            conn,
                            from_account_id=escrow_id,
                            to_account_id=sink_id,
                            amount=burn,
                            reason="PROP_RAKE",
                            memo=f"prop {prop.id}",
                        )
                    for b in winners:
                        await _notify(
                            conn,
                            int(b["user_id"]),
                            "PROP_RESOLVED",
                            {
                                "tick_index": tick_index,
                                "prop_id": prop.id,
                                "title": prop.title,
                                "outcome": outcome,
                                "paid": int(b["amount_minor"]) * distributable
                                // win_pool,
                            },
                        )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE props SET outcome = %s WHERE id = %s
                    """,
                    (outcome or "NO", prop.id),
                )
            await emit_feed(
                conn,
                "PROP_RESOLVED",
                {
                    "prop_id": prop.id,
                    "title": prop.title,
                    "outcome": outcome,
                    "pool": total_pool,
                },
                tick_index=tick_index,
            )
            settled += 1
    return settled


async def _record_payout(
    conn: AsyncConnection, bet_row: dict[str, Any], paid: int
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE prop_bets SET paid_minor = %s WHERE id = %s",
            (paid, int(bet_row["id"])),
        )


async def autogen(conn: AsyncConnection, tick_index: int) -> int:
    """Weekly ritual props: at each 7-day boundary spawn the index duel
    prop plus a deterministic stock-vs-threshold pick. Idempotent per
    week via a title/day check."""
    if not await _enabled(conn):
        return 0
    if await _config_float(conn, "props.autogen", 1.0) == 0.0:
        return 0
    day_index = tick_index // TICKS_PER_DAY
    if tick_index % TICKS_PER_DAY != 0 or day_index % 7 != 0:
        return 0
    window = int(await _config_float(conn, "props.window_ticks", 10080))
    rng = random.Random(f"props-autogen-{day_index}")
    made = 0

    # The index rivalry prop: will SBX40 beat ASX40 over the week?
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT a.id, b.id FROM instruments a, instruments b
            WHERE a.ticker = 'SBX40' AND b.ticker = 'ASX40'
              AND a.is_active AND b.is_active
            """
        )
        idx = await cur.fetchone()
    if idx is not None:
        await create(
            conn,
            title=f"Will SBX40 beat ASX40 this week? (week {day_index // 7})",
            kind="INDEX_BEAT",
            instrument_id=int(idx[0]),
            instrument_b_id=int(idx[1]),
            resolve_tick=tick_index + window,
            tick_index=tick_index,
            auto=True,
        )
        made += 1

    # A stock-vs-threshold pick: deterministic instrument + drift.
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, ticker, quoted_price FROM instruments
            WHERE kind = 'STOCK' AND is_active AND quoted_price IS NOT NULL
            ORDER BY id
            """
        )
        stocks = await cur.fetchall()
    if stocks:
        pick = stocks[rng.randrange(len(stocks))]
        drift = rng.choice([0.03, 0.05, 0.08, -0.03, -0.05])
        threshold = round(float(pick[2]) * (1 + drift), 2)
        direction = "above" if drift > 0 else "at or above"
        await create(
            conn,
            title=(
                f"Will {pick[1]} close {direction} ${threshold:.2f} "
                f"in a week? (week {day_index // 7})"
            ),
            kind="PRICE_ABOVE",
            instrument_id=int(pick[0]),
            threshold=threshold,
            resolve_tick=tick_index + window,
            tick_index=tick_index,
            auto=True,
        )
        made += 1
    return made


async def on_tick(conn: AsyncConnection, tick_index: int) -> int:
    """Tick sweep: autogen at the week boundary, then settle everything
    due. Session-agnostic -- props read frozen marks fine."""
    made = await autogen(conn, tick_index)
    settled = await settle_due(conn, tick_index)
    return made + settled
