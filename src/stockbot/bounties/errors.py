from stockbot.trading.errors import TradingError


class BountyError(TradingError):
    """Base class for bounty errors. Subclasses TradingError so the bot
    layer's existing handler formats them for the user."""


class BountyNotFoundError(BountyError):
    def __init__(self, bounty_id: int):
        super().__init__(f"no bounty with id {bounty_id}")


class BountyStateError(BountyError):
    pass


class BountyTargetError(BountyError):
    pass
