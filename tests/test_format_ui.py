"""UI theme layer: ANSI percent cells, direction emoji, embed stripe
colors, and the /market description char budget (ANSI escapes are
invisible once rendered but still count against Discord's 4,096-char
description limit -- the budget test exists because a future column
could silently blow it)."""

from __future__ import annotations

from decimal import Decimal

from stockbot.bot.commands import _market_embed
from stockbot.bot.format import (
    EMBED_DOWN,
    EMBED_FLAT,
    EMBED_UP,
    ansi_dim,
    ansi_pct,
    format_pct,
    pct_emoji,
    stripe_for_change,
)
from stockbot.market.data import InstrumentSnapshot

ESC = ""  # ANSI escape introducer


def _snap(
    ticker: str,
    price: str,
    change: float | None,
    *,
    sector_key: str = "tech",
    market_id: int = 1,
    halted: bool = False,
) -> InstrumentSnapshot:
    day_ago = (
        Decimal(price) / Decimal(str(1 + change)) if change is not None else None
    )
    return InstrumentSnapshot(
        id=1,
        ticker=ticker,
        name=f"{ticker} Co",
        sector_key=sector_key,
        sector_name=sector_key.capitalize(),
        quoted_price=Decimal(price),
        impact=Decimal(0),
        halted_until_tick=99 if halted else None,
        day_ago_close=day_ago,
        market_id=market_id,
    )


def test_format_pct_keeps_sign() -> None:
    assert format_pct(0.0123) == "+1.23%"
    assert format_pct(-0.045) == "-4.50%"
    assert format_pct(0.0) == "+0.00%"


def test_ansi_pct_colors_and_sign() -> None:
    up = ansi_pct(0.0123)
    down = ansi_pct(-0.045)
    flat = ansi_pct(0.0)
    none = ansi_pct(None)
    assert up.startswith(ESC) and up.endswith(ESC + "[0m" + " " * 0) or "[0m" in up
    assert "[32m" in up and "+1.23%" in up
    assert "[31m" in down and "-4.50%" in down
    assert "[90m" in flat
    assert none.strip() == "n/a"
    # Visible width is padded to `width` AFTER escapes: strip codes and
    # the cell measures exactly the column width.
    import re

    visible = re.sub(r"\[\d+m", "", up)
    assert len(visible) == 9


def test_ansi_dim_wraps() -> None:
    out = ansi_dim("TECH")
    assert "[90m" in out and "TECH" in out and "[0m" in out


def test_pct_emoji_direction() -> None:
    assert pct_emoji(0.01) == "🟩"
    assert pct_emoji(-0.01) == "🟥"
    assert pct_emoji(0.0) == "⬜"
    assert pct_emoji(None) == "⬜"


def test_stripe_for_change() -> None:
    assert stripe_for_change(0.01) == EMBED_UP
    assert stripe_for_change(-0.01) == EMBED_DOWN
    assert stripe_for_change(0.0) == EMBED_FLAT
    assert stripe_for_change(None) == EMBED_FLAT


def _venues() -> dict[int, dict[str, object]]:
    return {
        1: {
            "code": "US",
            "name": "United States Exchange",
            "open_ticks": 960,
            "closed_ticks": 480,
            "offset_ticks": 0,
        },
        2: {
            "code": "AS",
            "name": "Australasia Exchange",
            "open_ticks": 600,
            "closed_ticks": 840,
            "offset_ticks": 720,
        },
    }


def test_market_embed_structure() -> None:
    snaps = [
        _snap("SBX40", "4213.00", 0.0083, sector_key="index", market_id=1),
        _snap("NORT", "59.28", 0.0153, market_id=1),
        _snap("GRFS", "27.54", -0.0149, market_id=1),
        _snap("ASX40", "1987.00", -0.004, sector_key="index", market_id=2),
        _snap("AAIR", "12.40", 0.002, market_id=2),
    ]
    embed = _market_embed(snaps, _venues(), cur_tick=100)
    desc = embed.description or ""
    assert "```ansi" in desc and desc.rstrip().endswith("```")
    # Index quotes live in the status lines, not the tables.
    assert "SBX40 $4,213.00" in desc
    assert "🟩 +0.83%" in desc
    # Index rows are excluded from the stock table.
    table = desc.split("```ansi", 1)[1]
    assert "SBX40" not in table
    # Sector sub-headers replace the per-row sector column.
    assert "TECH" in table
    assert "NORT" in table
    # Blend of +0.83% and -0.40% is positive -> green stripe.
    assert embed.color is not None and embed.color.value == EMBED_UP


def test_market_embed_char_budget() -> None:
    """82 instruments + 2 venues + sector headers must stay under the
    4,096-char embed description cap even with ANSI escapes on every
    cell -- this is the regression lock for the no-sector-column
    redesign."""
    snaps: list[InstrumentSnapshot] = []
    sectors = ["tech", "fin", "con", "ind", "eng", "mat"]
    iid = 0
    for mid in (1, 2):
        snaps.append(
            _snap(
                f"IDX{mid}",
                "4000.00",
                0.01,
                sector_key="index",
                market_id=mid,
            )
        )
        for i in range(40):
            iid += 1
            snaps.append(
                _snap(
                    f"T{iid:04d}",
                    "1234.56",
                    -0.05 if i % 2 else 0.05,
                    sector_key=sectors[i % len(sectors)],
                    market_id=mid,
                    halted=(i % 17 == 0),
                )
            )
    embed = _market_embed(snaps, _venues(), cur_tick=100)
    desc = embed.description or ""
    assert len(desc) <= 4096
    # Sanity: all 80 stock rows actually rendered.
    assert desc.count("T0001") == 1 and desc.count("T0080") == 1


def test_market_embed_no_index_rows_ok() -> None:
    """Venues without an index still render (status line only)."""
    snaps = [_snap("NORT", "59.28", 0.01, market_id=1)]
    embed = _market_embed(snaps, _venues(), cur_tick=100)
    desc = embed.description or ""
    assert "NORT" in desc
