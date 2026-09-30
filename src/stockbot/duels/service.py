"""Duels: head-to-head equal-stake competitions on the league machinery.

Both players escrow real cash into GAME_ESCROW at offer/accept; on
accept a private duel season spawns (seasons.duel_id marks it, same
privacy-marker trick as sandbox_user_id) and each side gets an identical
FAUCET-funded league stake. Higher league equity at close wins the pot
minus a rake to SINK. League quarantine is the whole point: claims,
gifts, and main-portfolio wealth can't contaminate the duel -- it's
pure trading skill on identical footing.

`trade_scope` auto-pins both players to the duel season at accept so
`league:True` routes there even if a division week respawns mid-duel.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from stockbot.duels.errors import (
    DuelLimitError,
    DuelNotFoundError,
    DuelStateError,
    DuelTargetError,
)
from stockbot.feed import emit_feed
from stockbot.ledger.service import (
    get_system_account_id,
    get_user_account_id,
    post_transfer,
)
from stockbot.margin import service as margin
from stockbot.seasons import service as seasons
from stockbot.seasons.service import Season

log = logging.getLogger("stockbot.duels")

# `window` slash-arg choices (ticks; 1440/day).
WINDOW_CHOICES: dict[str, int] = {
    "4 hours": 240,
    "24 hours": 1440,
    "3 days": 4320,
    "7 days": 10080,
}


@dataclass(frozen=True)
class Duel:
    id: int
    challenger_id: int
    opponent_id: int
    stake_minor: int
    window_ticks: int
    status: str
    created_tick: int
    expires_tick: int
    season_id: int | None
    winner_id: int | None
    payout_minor: int | None
    forfeited_by: int | None

    def other(self, user_id: int) -> int:
        return self.opponent_id if user_id == self.challenger_id else self.challenger_id


def _from_row(row: dict[str, Any]) -> Duel:
    return Duel(
        id=int(row["id"]),
        challenger_id=int(row["challenger_id"]),
        opponent_id=int(row["opponent_id"]),
        stake_minor=int(row["stake_minor"]),
        window_ticks=int(row["window_ticks"]),
        status=str(row["status"]),
        created_tick=int(row["created_tick"]),
        expires_tick=int(row["expires_tick"]),
        season_id=int(row["season_id"]) if row["season_id"] is not None else None,
        winner_id=int(row["winner_id"]) if row["winner_id"] is not None else None,
        payout_minor=int(row["payout_minor"]) if row["payout_minor"] is not None else None,
        forfeited_by=(
            int(row["forfeited_by"]) if row["forfeited_by"] is not None else None
        ),
    )


async def _config_float(conn: AsyncConnection, key: str, default: float) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = await cur.fetchone()
    return float(row[0]) if row else default


async def _enabled(conn: AsyncConnection) -> bool:
    return await _config_float(conn, "duels.enabled", 1.0) != 0.0


async def get_duel(conn: AsyncConnection, duel_id: int) -> Duel | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM duels WHERE id = %s", (duel_id,))
        row = await cur.fetchone()
    return _from_row(row) if row else None


async def list_duels(conn: AsyncConnection, user_id: int) -> list[Duel]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT * FROM duels
            WHERE challenger_id = %s OR opponent_id = %s
            ORDER BY id DESC LIMIT 15
            """,
            (user_id, user_id),
        )
        return [_from_row(r) for r in await cur.fetchall()]


async def _notify(
    conn: AsyncConnection, user_id: int, kind: str, payload: dict[str, Any]
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO notifications (user_id, kind, payload) VALUES (%s, %s, %s)",
            (user_id, kind, json.dumps(payload)),
        )


async def _refund_challenger(conn: AsyncConnection, duel: Duel, reason: str) -> None:
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    challenger_account = await get_user_account_id(conn, duel.challenger_id)
    await post_transfer(
        conn,
        from_account_id=escrow_id,
        to_account_id=challenger_account,
        amount=duel.stake_minor,
        reason=reason,
        memo=f"duel {duel.id}",
    )


async def _active_count(conn: AsyncConnection, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*) FROM duels
            WHERE status IN ('OPEN', 'ACTIVE')
              AND (challenger_id = %s OR opponent_id = %s)
            """,
            (user_id, user_id),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def create_offer(
    conn: AsyncConnection,
    challenger_id: int,
    opponent_id: int,
    stake_minor: int,
    window_ticks: int,
    tick_index: int,
) -> int:
    """Escrow the challenger's stake and open the offer. Returns duel id."""
    if not await _enabled(conn):
        raise DuelStateError("duels are disabled")
    if challenger_id == opponent_id:
        raise DuelTargetError("you can't duel yourself")
    lo = int(await _config_float(conn, "duel.min_stake_minor", 100))
    hi = int(await _config_float(conn, "duel.max_stake_minor", 50_000))
    if not lo <= stake_minor <= hi:
        raise DuelTargetError(
            f"stake must be between {lo / 100:.2f} and {hi / 100:.2f} dollars"
        )

    async with conn.cursor(row_factory=dict_row) as cur:
        # No bootstrap for the opponent: challenging someone who never
        # played shouldn't mint them an account.
        await cur.execute(
            "SELECT is_bot, disabled_at FROM users WHERE id = %s", (opponent_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise DuelTargetError("they haven't started yet — they need /start first")
        if row["is_bot"]:
            raise DuelTargetError("you can't duel a bot")
        if row["disabled_at"] is not None:
            raise DuelTargetError("that account is suspended")

    cap = int(await _config_float(conn, "duel.max_active", 3))
    for uid, who in ((challenger_id, "you"), (opponent_id, "they")):
        if await _active_count(conn, uid) >= cap:
            raise DuelLimitError(f"{who} already have {cap} open/active duel(s)")

    async with conn.transaction():
        await margin.assert_spend_ok(conn, challenger_id, stake_minor)
        challenger_account = await get_user_account_id(conn, challenger_id)
        escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
        ttl = int(await _config_float(conn, "duel.offer_ttl_ticks", 1440))
        await post_transfer(
            conn,
            from_account_id=challenger_account,
            to_account_id=escrow_id,
            amount=stake_minor,
            reason="DUEL_STAKE",
        )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO duels
                    (challenger_id, opponent_id, stake_minor, window_ticks,
                     created_tick, expires_tick)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    challenger_id,
                    opponent_id,
                    stake_minor,
                    window_ticks,
                    tick_index,
                    tick_index + ttl,
                ),
            )
            id_row = await cur.fetchone()
            assert id_row is not None
            duel_id = int(id_row[0])
        await _notify(
            conn,
            opponent_id,
            "DUEL_OFFER",
            {
                "tick_index": tick_index,
                "duel_id": duel_id,
                "from": challenger_id,
                "stake": stake_minor,
                "window_ticks": window_ticks,
            },
        )
    return duel_id


async def accept(conn: AsyncConnection, duel_id: int, user_id: int, tick_index: int) -> Duel:
    """Opponent accepts: escrow their stake, spawn the duel season, pin scopes."""
    # Expired offers get refunded and marked EXPIRED -- that work must
    # commit before the state error propagates, so it's its own branch
    # flagged out of the transaction rather than a raise inside it.
    expired = False
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM duels WHERE id = %s FOR UPDATE", (duel_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise DuelNotFoundError(duel_id)
        duel = _from_row(row)
        if user_id != duel.opponent_id:
            raise DuelStateError("only the challenged player can accept")
        if duel.status != "OPEN":
            raise DuelStateError(f"that duel is already {duel.status.lower()}")
        if tick_index >= duel.expires_tick:
            await _expire_locked(conn, duel, tick_index)
            expired = True
    if expired:
        raise DuelStateError("that offer expired — challenge them again")

    async with conn.transaction():
        # Atomic claim before the multi-step work: a racing accept/cancel
        # blocks on the row lock, then sees non-OPEN and is rejected --
        # same idempotency pattern as close_season's status flip.
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE duels SET status = 'ACTIVE' "
                "WHERE id = %s AND status = 'OPEN' RETURNING id",
                (duel_id,),
            )
            if await cur.fetchone() is None:
                raise DuelStateError("that duel is no longer open")
        stake_minor = duel.stake_minor
        await margin.assert_spend_ok(conn, user_id, stake_minor)
        opponent_account = await get_user_account_id(conn, user_id)
        escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
        await post_transfer(
            conn,
            from_account_id=opponent_account,
            to_account_id=escrow_id,
            amount=stake_minor,
            reason="DUEL_STAKE",
        )

        league_stake = int(await _config_float(conn, "duel.league_stake_minor", 100_000))
        end_tick = tick_index + duel.window_ticks
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO seasons
                    (name, status, start_tick, end_tick, entry_fee_minor,
                     stake_minor, duel_id)
                VALUES (%s, 'ACTIVE', %s, %s, 0, %s, %s)
                RETURNING id
                """,
                (f"Duel #{duel.id}", tick_index, end_tick, league_stake, duel.id),
            )
            season_row = await cur.fetchone()
            assert season_row is not None
            season_id = int(season_row[0])
        # Fee 0 -> join_season skips its spend check; the real stakes are
        # already escrowed. Stakes are equal FAUCET grants -- quarantined.
        await seasons.join_season(conn, duel.challenger_id, season_id)
        await seasons.join_season(conn, duel.opponent_id, season_id)
        # Auto-pin both scopes so `league:True` trades land in the duel
        # even if a division week respawns while it's running.
        await seasons.pin_scope(conn, duel.challenger_id, season_id)
        await seasons.pin_scope(conn, duel.opponent_id, season_id)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE duels
                SET accepted_tick = %s, end_tick = %s, season_id = %s
                WHERE id = %s
                """,
                (tick_index, end_tick, season_id, duel.id),
            )
        await emit_feed(
            conn,
            "DUEL_STARTED",
            {
                "challenger": duel.challenger_id,
                "opponent": duel.opponent_id,
                "stake": stake_minor,
                "window_ticks": duel.window_ticks,
            },
            user_id=duel.challenger_id,
            tick_index=tick_index,
        )
    return await get_duel(conn, duel_id) or duel


async def _set_status(
    conn: AsyncConnection, duel_id: int, status: str, **fields: Any
) -> None:
    cols = ", ".join(f"{k} = %s" for k in fields)
    params = tuple(fields.values())
    async with conn.cursor() as cur:
        if cols:
            await cur.execute(
                f"UPDATE duels SET status = %s, {cols} WHERE id = %s",
                (status, *params, duel_id),
            )
        else:
            await cur.execute(
                "UPDATE duels SET status = %s WHERE id = %s", (status, duel_id)
            )


async def decline(conn: AsyncConnection, duel_id: int, user_id: int) -> None:
    async with conn.transaction():
        duel = await _lock_open(conn, duel_id, user_id)
        await _refund_challenger(conn, duel, "DUEL_REFUND")
        await _set_status(conn, duel_id, "DECLINED")
        await _notify(
            conn,
            duel.challenger_id,
            "DUEL_RESULT",
            {"tick_index": duel.created_tick, "duel_id": duel_id,
             "result": "declined", "opponent": user_id, "stake": duel.stake_minor},
        )


async def cancel(conn: AsyncConnection, duel_id: int, user_id: int) -> None:
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM duels WHERE id = %s FOR UPDATE", (duel_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise DuelNotFoundError(duel_id)
        duel = _from_row(row)
        if user_id != duel.challenger_id:
            raise DuelStateError("only the challenger can cancel — the opponent can decline")
        if duel.status != "OPEN":
            raise DuelStateError(f"that duel is already {duel.status.lower()}")
        await _refund_challenger(conn, duel, "DUEL_REFUND")
        await _set_status(conn, duel_id, "CANCELLED")


async def forfeit(conn: AsyncConnection, user_id: int, tick_index: int) -> Duel:
    """Concede the ACTIVE duel you're in: the other player wins outright."""
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM duels
                WHERE status = 'ACTIVE'
                  AND (challenger_id = %s OR opponent_id = %s)
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                (user_id, user_id),
            )
            row = await cur.fetchone()
        if row is None:
            raise DuelStateError("you have no active duel to forfeit")
        duel = _from_row(row)
        await _set_status(conn, duel.id, "ACTIVE", forfeited_by=user_id)
        assert duel.season_id is not None
        # close_season's atomic claim is the idempotency guard; the duel
        # branch honors forfeited_by over equity.
        await seasons.close_season(conn, duel.season_id)
    result = await get_duel(conn, duel.id)
    assert result is not None
    return result


async def _lock_open(conn: AsyncConnection, duel_id: int, user_id: int) -> Duel:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM duels WHERE id = %s FOR UPDATE", (duel_id,))
        row = await cur.fetchone()
    if row is None:
        raise DuelNotFoundError(duel_id)
    duel = _from_row(row)
    if user_id != duel.opponent_id:
        raise DuelStateError("only the challenged player can answer this offer")
    if duel.status != "OPEN":
        raise DuelStateError(f"that duel is already {duel.status.lower()}")
    return duel


async def _expire_locked(conn: AsyncConnection, duel: Duel, tick_index: int) -> None:
    """Refund an OPEN duel whose ttl passed. Call inside the caller's tx."""
    await _refund_challenger(conn, duel, "DUEL_REFUND")
    await _set_status(conn, duel.id, "EXPIRED")
    await _notify(
        conn,
        duel.challenger_id,
        "DUEL_RESULT",
        {"tick_index": tick_index, "duel_id": duel.id,
         "result": "expired", "opponent": duel.opponent_id, "stake": duel.stake_minor},
    )


async def on_tick(conn: AsyncConnection, tick_index: int) -> int:
    """Expire unanswered offers. Runs every tick in both session phases --
    cheap SELECT on the partial index."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM duels WHERE status = 'OPEN' AND expires_tick <= %s",
            (tick_index,),
        )
        due = [_from_row(r) for r in await cur.fetchall()]
    for duel in due:
        await _expire_locked(conn, duel, tick_index)
    return len(due)


async def close_duel_season(
    conn: AsyncConnection,
    season: Season,
    entries: list[dict[str, Any]],
    tick: int,
) -> None:
    """Duel settlement inside _close_season_claimed (season already CLOSED).

    Winner = higher league equity; forfeited_by forces it regardless. Pot
    = both escrowed stakes; winner-take-all minus rake -> SINK. Equity
    tie refunds each side minus its own rake share. League teardown runs
    after this in the shared tail."""
    assert season.duel_id is not None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM duels WHERE id = %s", (season.duel_id,))
        row = await cur.fetchone()
    assert row is not None
    duel = _from_row(row)

    equities: dict[int, int] = {}
    for entry in entries:
        uid = int(entry["user_id"])
        equities[uid] = await seasons.league_equity_minor(conn, season.id, uid)

    a, b = duel.challenger_id, duel.opponent_id
    if duel.forfeited_by is not None:
        winner = duel.other(duel.forfeited_by)
        loser = duel.forfeited_by
        tied = False
    else:
        ea, eb = equities.get(a, 0), equities.get(b, 0)
        tied = ea == eb
        winner = a if ea > eb else b
        loser = duel.other(winner)

    rake_pct = await _config_float(conn, "duel.rake_pct", 0.05)
    escrow_id = await get_system_account_id(conn, "GAME_ESCROW")
    sink_id = await get_system_account_id(conn, "SINK")

    if tied:
        # Push: each side's stake back minus its own rake share.
        rake_each = int(duel.stake_minor * rake_pct)
        for uid in (a, b):
            account = await get_user_account_id(conn, uid)
            back = duel.stake_minor - rake_each
            if back > 0:
                await post_transfer(
                    conn,
                    from_account_id=escrow_id,
                    to_account_id=account,
                    amount=back,
                    reason="DUEL_REFUND",
                    memo=f"duel {duel.id} tie",
                )
            if rake_each > 0:
                await post_transfer(
                    conn,
                    from_account_id=escrow_id,
                    to_account_id=sink_id,
                    amount=rake_each,
                    reason="DUEL_RAKE",
                    memo=f"duel {duel.id} tie",
                )
        payout: int | None = None
    else:
        pot = 2 * duel.stake_minor
        rake = int(pot * rake_pct)
        payout = pot - rake
        winner_account = await get_user_account_id(conn, winner)
        await post_transfer(
            conn,
            from_account_id=escrow_id,
            to_account_id=winner_account,
            amount=payout,
            reason="DUEL_PAYOUT",
            memo=f"duel {duel.id} win",
        )
        if rake > 0:
            await post_transfer(
                conn,
                from_account_id=escrow_id,
                to_account_id=sink_id,
                amount=rake,
                reason="DUEL_RAKE",
                memo=f"duel {duel.id}",
            )

    await _set_status(
        conn,
        duel.id,
        "SETTLED",
        winner_id=None if tied else winner,
        payout_minor=payout,
    )

    for uid in (a, b):
        if tied:
            result = "tied"
        elif duel.forfeited_by == uid:
            result = "forfeited"
        else:
            result = "won" if uid == winner else "lost"
        await _notify(
            conn,
            uid,
            "DUEL_RESULT",
            {
                "tick_index": tick,
                "duel_id": duel.id,
                "result": result,
                "opponent": duel.other(uid),
                "stake": duel.stake_minor,
                "payout": payout or 0,
                "my_equity": equities.get(uid, 0),
                "their_equity": equities.get(duel.other(uid), 0),
            },
        )

    await emit_feed(
        conn,
        "DUEL_SETTLED",
        {
            "winner": None if tied else winner,
            "loser": None if tied else loser,
            "stake": duel.stake_minor,
            "payout": payout or 0,
            "forfeit": duel.forfeited_by is not None,
        },
        user_id=None if tied else winner,
        tick_index=tick,
    )
