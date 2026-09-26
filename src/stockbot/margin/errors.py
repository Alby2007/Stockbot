"""Margin-domain errors raised by the trading and liquidation paths."""

from __future__ import annotations


def _money(minor_units: int) -> str:
    """Local copy of bot.format.format_money -- the domain layer can't
    import the bot package (it sits above it), and raw minor units in
    user-facing messages were an N4 bug."""
    return f"${minor_units / 100:,.2f}"


class MarginError(Exception):
    """Base class for margin-related rejections."""


class MarginNotUnlockedError(MarginError):
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__("short selling requires a margin tier (shop: margin_tier)")


class InsufficientMarginError(MarginError):
    """Post-trade equity would be below the initial margin requirement."""

    def __init__(self, equity_minor: int, required_minor: int) -> None:
        self.equity_minor = equity_minor
        self.required_minor = required_minor
        super().__init__(
            f"insufficient margin: equity {_money(equity_minor)} < "
            f"required {_money(required_minor)}"
        )


class ShortInterestLimitError(MarginError):
    """Opening this short would exceed the instrument's short-interest cap."""

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        super().__init__(f"short interest limit reached for {ticker}")


class InstrumentNotShortableError(MarginError):
    """New margin shorts are barred until shortable_after_tick (IPO borrow
    lockout -- a fresh listing has no borrow to lend against)."""

    def __init__(self, ticker: str, until_tick: int) -> None:
        self.ticker = ticker
        self.until_tick = until_tick
        super().__init__(
            f"{ticker} can't be shorted until ~tick {until_tick} "
            "(fresh listing — no borrow yet)"
        )


class PositionLimitError(MarginError):
    """Gross notional would exceed the tier's leverage cap."""

    def __init__(self, gross_minor: int, cap_minor: int) -> None:
        self.gross_minor = gross_minor
        self.cap_minor = cap_minor
        super().__init__(
            f"gross notional {_money(gross_minor)} exceeds leverage cap "
            f"{_money(cap_minor)}"
        )


class MarginSpendBlockedError(MarginError):
    """A discretionary spend would push the account below maintenance margin."""

    def __init__(self, equity_after_minor: int, maint_req_minor: int) -> None:
        self.equity_after_minor = equity_after_minor
        self.maint_req_minor = maint_req_minor
        super().__init__(
            f"spend blocked: equity {_money(equity_after_minor)} would be "
            f"below maintenance margin {_money(maint_req_minor)}"
        )
