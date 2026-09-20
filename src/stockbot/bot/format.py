"""Shared display formatting for embeds/messages.

Currency name/symbol is a placeholder per the design doc's open items.
"""

from __future__ import annotations

from decimal import Decimal


def format_money(minor_units: int) -> str:
    return f"${minor_units / 100:,.2f}"


def format_price(price: Decimal | float) -> str:
    return f"${float(price):,.2f}"


def format_pct(fraction: float) -> str:
    sign = "+" if fraction >= 0 else ""
    return f"{sign}{fraction * 100:.2f}%"
