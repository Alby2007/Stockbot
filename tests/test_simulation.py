from __future__ import annotations

import random

from psycopg import AsyncConnection

from stockbot.simulation.harness import build_cohort, run_simulation


def test_build_cohort_covers_all_archetypes() -> None:
    agents = build_cohort(60, random.Random(0))
    archetypes = {a.archetype for a in agents}
    assert archetypes == {
        "grinder",
        "yolo",
        "farmer",
        "wash_trader",
        "whale",
        "shorter",
        "liquidity_provider",
    }

    wash_traders = [a for a in agents if a.archetype == "wash_trader"]
    assert len(wash_traders) % 2 == 0
    for agent in wash_traders:
        partner = next(a for a in wash_traders if a.user_id == agent.wash_partner_id)
        assert partner.wash_partner_id == agent.user_id


def test_build_cohort_ids_dont_collide_with_real_discord_snowflakes() -> None:
    # Discord snowflakes are all >= ~1e17 (the Discord epoch, 2015-01-01);
    # synthetic ids must stay comfortably below that.
    agents = build_cohort(20, random.Random(1))
    assert all(0 < a.user_id < 10**17 for a in agents)


async def test_run_simulation_end_to_end_small_scale(conn: AsyncConnection) -> None:
    report = await run_simulation(
        conn,
        num_users=12,
        num_days=3,
        master_seed="test-sim-seed",
        snapshot_every_days=1,
        seed=42,
        ticks_per_day=5,  # accelerated: don't actually run 1440 ticks in a test
    )

    assert report["num_users"] == 12
    assert report["num_days"] == 3
    assert len(report["timeline"]) == 3
    assert isinstance(report["final_gini"], float)
    assert 0.0 <= report["final_gini"] <= 1.0

    by_archetype = report["by_archetype"]
    assert "farmer" in by_archetype
    for stats in by_archetype.values():
        assert "outperformed_grinder" in stats


async def test_wash_traders_dont_systematically_beat_honest_play(conn: AsyncConnection) -> None:
    """Not a hard guarantee (it's a probabilistic simulation), but over a
    short deterministic run the pump-and-dump pair shouldn't run away with
    the economy -- if this starts failing, the impact/fee model has gotten
    exploitable and the tuning needs another look.
    """
    report = await run_simulation(
        conn,
        num_users=20,
        num_days=5,
        master_seed="wash-trade-sim-seed",
        snapshot_every_days=5,
        seed=7,
        ticks_per_day=3,
    )
    wash = report["by_archetype"].get("wash_trader")
    assert wash is not None
    # The claims-only farmer is the honest floor. Pump-and-dump should net
    # the pair roughly that same claim income -- fees + impact eat the rest.
    # If wash avg gain runs well ahead of the farmer baseline, the
    # impact/fee model has gotten exploitable and tuning needs another look.
    farmer = report["by_archetype"].get("farmer")
    assert farmer is not None
    assert wash["avg_gain_minor"] <= farmer["avg_gain_minor"] * 1.5


async def test_simulated_days_drive_daily_claims(conn: AsyncConnection) -> None:
    """Every archetype claims daily; with as_of_date wired through, a
    num_days run must produce num_days of consecutive claims (max streak ==
    num_days), not ~1 real-date claim for the whole run."""
    num_days = 3
    await run_simulation(
        conn,
        num_users=12,
        num_days=num_days,
        master_seed="claim-date-seed",
        snapshot_every_days=1,
        seed=42,
        ticks_per_day=3,
    )
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(streak) FROM claims")
        (max_streak,) = await cur.fetchone()
    assert max_streak == num_days
