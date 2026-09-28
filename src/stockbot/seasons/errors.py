from stockbot.trading.errors import TradingError


class SeasonError(TradingError):
    """Base class for league/season errors. Subclasses TradingError so the
    bot layer's existing TradingError handler formats them for the user."""


class SeasonNotFoundError(SeasonError):
    def __init__(self, season_id: int):
        self.season_id = season_id
        super().__init__(f"no season with id {season_id}")


class NoOpenSeasonError(SeasonError):
    def __init__(self) -> None:
        super().__init__("no season is open for entries right now")


class SeasonNotOpenError(SeasonError):
    def __init__(self, season_id: int, status: str):
        self.season_id = season_id
        self.status = status
        super().__init__(f"season {season_id} is {status.lower()}, not open for entries")


class AlreadyEnteredError(SeasonError):
    def __init__(self, season_id: int, user_id: int):
        self.season_id = season_id
        self.user_id = user_id
        super().__init__(f"already entered season {season_id}")


class SandboxAlreadyOpenError(SeasonError):
    def __init__(self, season_id: int):
        self.season_id = season_id
        super().__init__(f"sandbox season {season_id} is already running")


class BotAccountError(SeasonError):
    """Synthetic (NPC) accounts can't enter seasons: the stake is
    faucet-seeded and seasons are a human competition."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__("bot accounts can't join seasons")
