"""Shared display formatting for embeds/messages.

Currency name/symbol is a placeholder per the design doc's open items.

Color language: ANSI escapes inside fenced ```ansi blocks render real
red/green text on desktop AND mobile Discord; embed `color=` stripes
carry per-command semantics (margin health, day change, leaderboard
gold). Color is always decoration on top of a +/- sign -- nothing is
color-only (red/green alone is colorblind-hostile).
"""

from __future__ import annotations

from decimal import Decimal

# Embed stripe palette (discord.Embed color= ints).
EMBED_UP = 0x2ECC71
EMBED_DOWN = 0xE74C3C
EMBED_WARN = 0xE67E22
EMBED_GOLD = 0xF1C40F
EMBED_FLAT = 0x95A5A6
EMBED_INFO = 0x3498DB

# ANSI codes for ```ansi fenced blocks.
_A_RESET = "\u001b[0m"
_A_GREEN = "\u001b[32m"
_A_RED = "\u001b[31m"
_A_GREY = "\u001b[90m"

# |move| below this renders flat -- sub-bp jitter isn't a direction.
_FLAT_EPS = 0.00005


def format_money(minor_units: int) -> str:
    return f"${minor_units / 100:,.2f}"


def format_price(price: Decimal | float) -> str:
    return f"${float(price):,.2f}"


def format_pct(fraction: float) -> str:
    sign = "+" if fraction >= 0 else ""
    return f"{sign}{fraction * 100:.2f}%"


def ansi_pct(fraction: float | None, width: int = 9) -> str:
    """Fixed-width percent cell with ANSI color for ```ansi blocks.

    The escape bytes are invisible once rendered, so padding is applied
    AFTER the escapes -- `width` is the visible column width.
    """
    if fraction is None:
        return f"{'n/a':>{width}}"
    cell = format_pct(fraction)
    pad = " " * max(0, width - len(cell))
    if fraction >= _FLAT_EPS:
        return f"{_A_GREEN}{cell}{_A_RESET}{pad}"
    if fraction <= -_FLAT_EPS:
        return f"{_A_RED}{cell}{_A_RESET}{pad}"
    return f"{_A_GREY}{cell}{_A_RESET}{pad}"


def ansi_dim(text: str) -> str:
    """Grey/dim text for table sub-headers inside ```ansi blocks."""
    return f"{_A_GREY}{text}{_A_RESET}"


def pct_emoji(fraction: float | None) -> str:
    """Direction marker for surfaces where ANSI doesn't fit."""
    if fraction is None:
        return "⬜"
    if fraction >= _FLAT_EPS:
        return "🟩"
    if fraction <= -_FLAT_EPS:
        return "🟥"
    return "⬜"


def stripe_for_change(fraction: float | None) -> int:
    """Embed color for a signed move: green up, red down, grey flat."""
    if fraction is None:
        return EMBED_FLAT
    if fraction >= _FLAT_EPS:
        return EMBED_UP
    if fraction <= -_FLAT_EPS:
        return EMBED_DOWN
    return EMBED_FLAT
