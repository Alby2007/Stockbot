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


class DuplicateInteractionError(TradingError):
    """Raised when an interaction id has already been processed (idempotent replay)."""

    def __init__(self, interaction_id: str):
        self.interaction_id = interaction_id
        super().__init__(f"interaction {interaction_id} was already processed")
