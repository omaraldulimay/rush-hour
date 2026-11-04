from dataclasses import dataclass

from libs.common.settings import database_url, env_int, redis_url


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    cache_ttl_s: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(database_url(), redis_url(), env_int("CACHE_TTL_S", 30))
