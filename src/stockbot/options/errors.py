from stockbot.trading.errors import TradingError


class OptionError(TradingError):
    """Base class for options errors (a TradingError so the bot layer
    formats them like any other trade failure)."""


class OptionNotFoundError(OptionError):
    def __init__(self, option_id: int):
        self.option_id = option_id
        super().__init__(f"no open option position with id {option_id}")


class StrikeOutOfBandError(OptionError):
    def __init__(self, strike: float, lo: float, hi: float):
        super().__init__(
            f"strike {strike:g} outside the allowed band "
            f"{lo:g}–{hi:g} (20%–500% of the mark)"
        )
