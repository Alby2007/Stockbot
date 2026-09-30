from stockbot.trading.errors import TradingError


class PropError(TradingError):
    """Base class for prop-contract errors. Subclasses TradingError so
    the bot layer's existing handler formats them for the user."""


class PropNotFoundError(PropError):
    def __init__(self, prop_id: int) -> None:
        super().__init__(f"no prop with id {prop_id}")
        self.prop_id = prop_id


class PropStateError(PropError):
    """Wrong lifecycle state or a disabled feature."""


class PropBetError(PropError):
    """Invalid bet: bad side, over bounds, or pool closed."""
