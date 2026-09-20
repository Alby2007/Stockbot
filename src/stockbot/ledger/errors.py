class LedgerError(Exception):
    """Base class for ledger-related errors."""


class InsufficientFundsError(LedgerError):
    """Raised when a transfer would leave a user account balance negative."""

    def __init__(self, account_id: int):
        self.account_id = account_id
        super().__init__(f"account {account_id} has insufficient funds for this transfer")


class UnknownAccountError(LedgerError):
    """Raised when a referenced account (or system account name) does not exist."""

    def __init__(self, identifier: object):
        self.identifier = identifier
        super().__init__(f"unknown account: {identifier!r}")
