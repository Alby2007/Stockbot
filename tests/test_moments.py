"""Moments: market-minted commemoratives. `sweep_moments` reads only
in-memory tick state (results / opens / flow_breached / rows_by_id) --
the tests drive it with SimpleNamespace fakes, no real tick needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from psycopg import AsyncConnection

from stockbot.accounts.service import bootstrap_user
from stockbot.collectibles.moments import sweep_moments

_USER = 7101


def _res(iid: int, quoted: float, breached: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=iid, quoted_price=quoted, circuit_breached=breached)


async def _instrument(conn: AsyncConnection) -> tuple[int, str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE kind = 'STOCK'"
            " ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        return int(row[0]), str(row[1])


async def _hold(conn: AsyncConnection, user_id: int, iid: int, qty: int = 5) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO positions (user_id, instrument_id, quantity)"
            " VALUES (%s, %s, %s)",
            (user_id, iid, qty),
        )


def _ctx(iid: int, ticker: str, hw: float | None = None) -> dict:
    return {iid: {"ticker": ticker, "name": f"{ticker} Corp",
                  "high_water_price": hw}}


async def test_mover_mints_and_awards_witnesses(conn: AsyncConnection) -> None:
    iid, ticker = await _instrument(conn)
    await bootstrap_user(conn, _USER)
    await _hold(conn, _USER, iid)
    minted = await sweep_moments(
        conn,
        500,
        results=[_res(iid, 115.0)],
        opens={iid: 100.0},  # +15% > 12% mover bar
        flow_breached=set(),
        rows_by_id=_ctx(iid, ticker, hw=200.0),  # 115 < 2x200: no RECORD
    )
    assert minted == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT rule, awarded_count FROM moments"
            " WHERE instrument_id = %s AND tick_index = 500",
            (iid,),
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "MOVER" and int(row[1]) == 1
        await cur.execute(
            "SELECT 1 FROM user_cards uc JOIN cards c ON c.key = uc.card_key"
            " WHERE uc.user_id = %s AND c.kind = 'COMMEMORATIVE'",
            (_USER,),
        )
        assert await cur.fetchone() is not None
        await cur.execute(
            "SELECT 1 FROM notifications WHERE user_id = %s"
            " AND kind = 'MOMENT_EARNED'",
            (_USER,),
        )
        assert await cur.fetchone() is not None


async def test_mover_crater_word(conn: AsyncConnection) -> None:
    iid, ticker = await _instrument(conn)
    minted = await sweep_moments(
        conn,
        501,
        results=[_res(iid, 80.0)],
        opens={iid: 100.0},  # -20%: crater, still a mover
        flow_breached=set(),
        rows_by_id=_ctx(iid, ticker),
    )
    assert minted == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT c.name FROM moments m JOIN cards c ON c.key = m.card_key"
            " WHERE m.tick_index = 501",
        )
        assert "Crater" in str((await cur.fetchone())[0])


async def test_record_break_mints(conn: AsyncConnection) -> None:
    iid, ticker = await _instrument(conn)
    minted = await sweep_moments(
        conn,
        502,
        results=[_res(iid, 250.0)],
        opens={iid: 245.0},  # +2%: no MOVER
        flow_breached=set(),
        rows_by_id=_ctx(iid, ticker, hw=100.0),  # 250 >= 2x100 -> RECORD
    )
    assert minted == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT rule FROM moments WHERE tick_index = 502"
        )
        assert (await cur.fetchone())[0] == "RECORD"


async def test_mass_halt_awards_all_holders(conn: AsyncConnection) -> None:
    other = 7102
    await bootstrap_user(conn, _USER)
    await bootstrap_user(conn, other)
    iids: list[int] = []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE kind = 'STOCK'"
            " ORDER BY id LIMIT 6"
        )
        rows = await cur.fetchall()
    results = []
    rows_by_id = {}
    for r in rows[:5]:
        iid = int(r[0])
        iids.append(iid)
        results.append(_res(iid, 50.0, breached=True))
        rows_by_id[iid] = {"ticker": str(r[1]), "name": str(r[1]),
                           "high_water_price": None}
    await _hold(conn, _USER, iids[0])
    await _hold(conn, other, iids[3])
    minted = await sweep_moments(
        conn,
        503,
        results=results,
        opens={i: 50.0 for i in iids},  # flat: no MOVER triggers
        flow_breached=set(),
        rows_by_id=rows_by_id,
    )
    assert minted == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT awarded_count FROM moments WHERE rule = 'MASS_HALT'"
            " AND tick_index = 503"
        )
        assert int((await cur.fetchone())[0]) == 2  # both holders got it


async def test_moment_dedup_same_tick(conn: AsyncConnection) -> None:
    iid, ticker = await _instrument(conn)
    args = dict(
        results=[_res(iid, 115.0)],
        opens={iid: 100.0},
        flow_breached=set(),
        rows_by_id=_ctx(iid, ticker),
    )
    assert await sweep_moments(conn, 504, **args) == 1
    assert await sweep_moments(conn, 504, **args) == 0  # same tick: no remint


async def test_moment_cap_prioritizes(conn: AsyncConnection) -> None:
    """max_per_tick=3: MASS_HALT + RECORD outrank MOVER spam."""
    iids: list[int] = []
    results = []
    rows_by_id = {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, ticker FROM instruments WHERE kind = 'STOCK'"
            " ORDER BY id LIMIT 6"
        )
        rows = await cur.fetchall()
    for r in rows[:5]:
        iid = int(r[0])
        iids.append(iid)
        results.append(_res(iid, 50.0, breached=True))
        rows_by_id[iid] = {"ticker": str(r[1]), "name": str(r[1]),
                           "high_water_price": None}
    # + a record-breaker and two movers on the sixth/others
    rid = int(rows[5][0])
    results.append(_res(rid, 300.0))
    rows_by_id[rid] = {"ticker": str(rows[5][1]), "name": "x",
                       "high_water_price": 100.0}
    opens = {i: 50.0 for i in iids}
    opens[rid] = 295.0  # no MOVER on the record breaker
    minted = await sweep_moments(
        conn,
        505,
        results=results,
        opens=opens,
        flow_breached=set(),
        rows_by_id=rows_by_id,
    )
    assert minted == 2  # MASS_HALT + RECORD; under the cap
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT rule FROM moments WHERE tick_index = 505 ORDER BY rule"
        )
        assert [r[0] for r in await cur.fetchall()] == ["MASS_HALT", "RECORD"]


async def test_kill_switch(conn: AsyncConnection) -> None:
    iid, ticker = await _instrument(conn)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE config SET value = 0 WHERE key = 'moment.enabled'")
    assert (
        await sweep_moments(
            conn,
            506,
            results=[_res(iid, 200.0)],
            opens={iid: 100.0},
            flow_breached=set(),
            rows_by_id=_ctx(iid, ticker),
        )
        == 0
    )


async def test_league_holders_are_not_witnesses(conn: AsyncConnection) -> None:
    """season_id IS NULL on the position row gates witness eligibility."""
    iid, ticker = await _instrument(conn)
    await bootstrap_user(conn, _USER)
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM seasons LIMIT 1")
        season = await cur.fetchone()
        if season is None:
            return  # fixture DB has no seasons: nothing to prove
        await cur.execute(
            "INSERT INTO positions (user_id, instrument_id, quantity, season_id)"
            " VALUES (%s, %s, 5, %s)",
            (_USER, iid, int(season[0])),
        )
    minted = await sweep_moments(
        conn,
        507,
        results=[_res(iid, 115.0)],
        opens={iid: 100.0},
        flow_breached=set(),
        rows_by_id=_ctx(iid, ticker),
    )
    assert minted == 1  # minted regardless
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT awarded_count FROM moments WHERE tick_index = 507"
        )
        assert int((await cur.fetchone())[0]) == 0  # but nobody earned it
