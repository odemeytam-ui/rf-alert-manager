"""All configuration comes from environment variables (12-factor)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    stream_name: str
    consumer_group: str
    rules_file: str | None
    log_level: str


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./rfam.db"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        stream_name=os.getenv("STREAM_NAME", "rf.events"),
        consumer_group=os.getenv("CONSUMER_GROUP", "alert-service"),
        rules_file=os.getenv("RULES_FILE") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
