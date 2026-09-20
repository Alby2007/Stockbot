"""Margin-domain errors raised by the trading and liquidation paths."""

from __future__ import annotations


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
        super().__init__(f"insufficient margin: equity {equity_minor} < required {required_minor}")


class ShortInterestLimitError(MarginError):
    """Opening this short would exceed the instrument's short-interest cap."""

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        super().__init__(f"short interest limit reached for {ticker}")


class PositionLimitError(MarginError):
    """Gross notional would exceed the tier's leverage cap."""

    def __init__(self, gross_minor: int, cap_minor: int) -> None:
        self.gross_minor = gross_minor
        self.cap_minor = cap_minor
        super().__init__(f"gross notional {gross_minor} exceeds leverage cap {cap_minor}")


class MarginSpendBlockedError(MarginError):
    """A discretionary spend would push the account below maintenance margin."""

    def __init__(self, equity_after_minor: int, maint_req_minor: int) -> None:
        self.equity_after_minor = equity_after_minor
        self.maint_req_minor = maint_req_minor
        super().__init__(
            f"spend blocked: equity {equity_after_minor} would be below "
            f"maintenance margin {maint_req_minor}"
        )
