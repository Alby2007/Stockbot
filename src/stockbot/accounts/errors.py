"""Account-level errors. Not TradingError subclasses on purpose:
trading.errors is reached through the trading package, whose __init__
pulls in trading.service -> shop -> accounts -- an import cycle. The
command layer catches these explicitly in `_instrument_one`."""


class UserDisabledError(Exception):
    """Raised by bootstrap_user when users.disabled_at is set. Every
    user-initiated money path goes through bootstrap, so this is the
    single chokepoint; fills/liquidations bypass it on purpose (they're
    market mechanics, not user actions). `reason` is the admin-supplied
    suspension note -- surfaced in the ephemeral reply so the message
    explains itself even if the DM hasn't landed yet."""

    def __init__(self, user_id: int, reason: str | None = None):
        self.user_id = user_id
        self.reason = reason
        super().__init__(f"user {user_id} is suspended")
