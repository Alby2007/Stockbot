class ShopError(Exception):
    """Base class for shop errors."""


class UnknownItemError(ShopError):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"unknown shop item: {key!r}")


class AlreadyOwnedError(ShopError):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"you already own {key!r}")
