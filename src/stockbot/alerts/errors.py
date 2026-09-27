from stockbot.trading.errors import TradingError


class AlertError(TradingError):
    """Base class for price-alert errors (a TradingError so the bot layer
    formats them like any other trade failure)."""


class AlertNotFoundError(AlertError):
    def __init__(self, alert_id: int):
        self.alert_id = alert_id
        super().__init__(f"no open alert with id {alert_id}")


class AlertLimitError(AlertError):
    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"alert limit reached ({limit} open)")


class DuplicateAlertError(AlertError):
    def __init__(self, ticker: str, direction: str, target: object):
        super().__init__(
            f"you already have an open {direction.lower()} alert on "
            f"{ticker} at that price"
        )
