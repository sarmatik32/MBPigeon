from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = Field(..., alias="BOT_TOKEN")
    database_url: str = Field("sqlite+aiosqlite:///./mafia.db", alias="DATABASE_URL")
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")
    daily_earn_cap: int = Field(400, alias="DAILY_EARN_CAP")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
