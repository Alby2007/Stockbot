from stockbot.trading.errors import TradingError


class ShortError(TradingError):
    """Base class for bounded-short errors (a TradingError so the bot layer
    formats them like any other trade failure)."""


class ShortNotFoundError(ShortError):
    def __init__(self, short_id: int):
        self.short_id = short_id
        super().__init__(f"no open bounded short with id {short_id}")
