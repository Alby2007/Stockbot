from stockbot.trading.errors import TradingError


class DuelError(TradingError):
    """Base class for duel errors. Subclasses TradingError so the bot
    layer's existing handler formats them for the user."""


class DuelNotFoundError(DuelError):
    def __init__(self, duel_id: int):
        super().__init__(f"no duel with id {duel_id}")


class DuelStateError(DuelError):
    pass


class DuelTargetError(DuelError):
    pass


class DuelLimitError(DuelError):
    pass
