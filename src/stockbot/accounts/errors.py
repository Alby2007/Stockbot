"""Account-level errors. Not TradingError subclasses on purpose:
trading.errors is reached through the trading package, whose __init__
pulls in trading.service -> shop -> accounts -- an import cycle. The
command layer catches these explicitly in `_instrument_one`."""


class UserDisabledError(Exception):
    """Raised by bootstrap_user when users.disabled_at is set. Every
    user-initiated money path goes through bootstrap, so this is the
    single chokepoint; fills/liquidations bypass it on purpose (they're
    market mechanics, not user actions)."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(f"user {user_id} is suspended")
