class TradingError(Exception):
    """Base class for trading errors."""


class UnknownInstrumentError(TradingError):
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"unknown instrument: {ticker!r}")


class InstrumentHaltedError(TradingError):
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"{ticker} is halted by the circuit breaker")


class InsufficientSharesError(TradingError):
    def __init__(self, ticker: str, requested: int, held: int):
        self.ticker = ticker
        self.requested = requested
        self.held = held
        super().__init__(f"cannot sell {requested} shares of {ticker}; only {held} held")


class InsufficientDepthError(TradingError):
    """A single marketable fill exceeds the participation cap -- the
    instrument's liquidity can't absorb that much notional at once."""

    def __init__(self, ticker: str, notional_minor: int, cap_minor: int):
        self.ticker = ticker
        self.notional_minor = notional_minor
        self.cap_minor = cap_minor
        super().__init__(
            f"order for {ticker} is too large to fill at once "
            f"(~${notional_minor / 100:,.2f} notional vs a "
            f"~${cap_minor / 100:,.2f} depth cap) -- split it into smaller "
            "trades or rest a limit order"
        )


class TooManyPositionsError(TradingError):
    def __init__(self, open_positions: int, slot_count: int):
        self.open_positions = open_positions
        self.slot_count = slot_count
        super().__init__(
            f"you already hold {open_positions} position(s), the max your "
            f"{slot_count} portfolio slot(s) allow. Buy a slot in /shop or "
            "close a position first."
        )


class DuplicateInteractionError(TradingError):
    """Raised when an interaction id has already been processed (idempotent replay)."""

    def __init__(self, interaction_id: str):
        self.interaction_id = interaction_id
        super().__init__(f"interaction {interaction_id} was already processed")


class MarketClosedError(TradingError):
    """Trading while the market is in its closed session."""

    def __init__(self, ticks_until_open: int):
        self.ticks_until_open = ticks_until_open
        super().__init__(
            f"the market is closed -- it reopens in ~{ticks_until_open} min"
        )


class NotInLeagueError(TradingError):
    """League-scoped trade attempted without an entry in an ACTIVE season.

    Lives here (not seasons/errors.py) because execute_trade raises it;
    keeping it in the trading module avoids a circular import through
    trading/__init__ -> trading.service -> seasons.service.
    """

    def __init__(self, user_id: int, season_id: int):
        self.user_id = user_id
        self.season_id = season_id
        super().__init__(f"not entered in season {season_id}, or season is not active")
