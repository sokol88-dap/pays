from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: SecretStr = Field(min_length=1)
    database_url: str = "postgresql+asyncpg://pays:pays@localhost:5432/pays"
    rabbitmq_url: str = "amqp://pays:pays@localhost:5672/"
    webhook_timeout_seconds: float = Field(default=10, gt=0)
    outbox_poll_seconds: float = Field(default=1, gt=0)
    outbox_batch_size: int = Field(default=50, ge=1, le=1000)
    publish_timeout_seconds: float = Field(default=10, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings.model_validate({})
