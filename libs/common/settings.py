"""Configuration comes from environment variables only."""

import os


def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def database_url() -> str:
    return env_str("DATABASE_URL", "postgresql://rushhour:rushhour@localhost:5432/rushhour")


def redis_url() -> str:
    return env_str("REDIS_URL", "redis://localhost:6379/0")
