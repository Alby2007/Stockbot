from stockbot.trading.errors import TradingError


class DivisionError(TradingError):
    """Base class for division errors. Subclasses TradingError so the bot
    layer's existing handler formats them for the user."""


class DivisionDisabledError(DivisionError):
    def __init__(self) -> None:
        super().__init__("divisions are disabled")


class NotAMemberError(DivisionError):
    def __init__(self) -> None:
        super().__init__("you're not on the division ladder — `/division join` first")
