"""Central configuration, loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://stockbot:stockbot@localhost:5432/stockbot"
    test_database_url: str = "postgresql://stockbot:stockbot@localhost:5432/stockbot_test"
    discord_token: str = ""
    master_seed: str = "change-me-in-production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
