"""Pure metric functions used by the economy simulation harness."""

from __future__ import annotations


def gini_coefficient(values: list[float]) -> float:
    """0 = perfect equality, 1 = one account holds everything.

    Negative net worth is clamped to 0 for this purpose (Gini is undefined
    with negative values, and someone in the red doesn't add "less than
    nothing" to the inequality picture in any useful sense here).
    """
    cleaned = sorted(max(0.0, v) for v in values)
    n = len(cleaned)
    total = sum(cleaned)
    if n == 0 or total == 0:
        return 0.0
    weighted_sum = sum(i * v for i, v in enumerate(cleaned, start=1))
    return (2 * weighted_sum) / (n * total) - (n + 1) / n


def faucet_sink_ratio(faucet_printed: int, sink_destroyed: int) -> float:
    """>1 means the faucet is printing faster than sinks are burning it --
    the hyperinflation warning sign the design doc dashboards against."""
    if sink_destroyed <= 0:
        return float("inf") if faucet_printed > 0 else 0.0
    return faucet_printed / sink_destroyed
