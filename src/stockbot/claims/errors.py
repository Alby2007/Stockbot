from datetime import datetime


class AlreadyClaimedTodayError(Exception):
    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(f"user {user_id} already claimed today")


class AccountTooYoungError(Exception):
    """The Discord snowflake itself is younger than the configured
    minimum -- defense-in-depth inside claim_daily so no non-command
    caller can bypass the anti-farm gate the command layer enforces."""

    def __init__(self, min_age_days: int):
        self.min_age_days = min_age_days
        super().__init__(
            f"Discord account must be at least {min_age_days} days old to claim"
        )


class FirstClaimLockedError(Exception):
    """A brand-new bot account can't claim for 24h. Carries the unlock
    instant so the command layer can say *when*, not just that it's
    locked (H5 -- a bare "wait 24h" reads as a bug)."""

    def __init__(self, unlock_at: datetime):
        self.unlock_at = unlock_at
        super().__init__(f"first claim unlocks at {unlock_at.isoformat()}")
