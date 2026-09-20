"""Central configuration, loaded from environment variables / .env."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://stockbot:stockbot@localhost:5432/stockbot"
    test_database_url: str = "postgresql://stockbot:stockbot@localhost:5432/stockbot_test"
    discord_token: str = ""
    master_seed: str = "change-me-in-production"
    # Comma-separated Discord user ids allowed to run /admin commands.
    admin_user_ids: list[int] = []

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def _parse_admin_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
