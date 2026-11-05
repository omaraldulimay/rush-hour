from dataclasses import dataclass

from libs.common.settings import database_url, env_float, env_int, env_str, redis_url


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    catalog_url: str = "http://localhost:8001"
    inventory_url: str = "http://localhost:8002"
    payment_url: str = "http://localhost:8004"
    payment_timeout_s: float = 2.0
    http_timeout_s: float = 5.0
    breaker_failure_threshold: int = 5
    breaker_window_s: float = 30.0
    breaker_cooldown_s: float = 10.0
    cart_ttl_s: int = 86_400
    fault_error_rate: float = 0.0  # game day: share of /checkout calls answered with a 500

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=database_url(),
            redis_url=redis_url(),
            catalog_url=env_str("CATALOG_URL", cls.catalog_url),
            inventory_url=env_str("INVENTORY_URL", cls.inventory_url),
            payment_url=env_str("PAYMENT_URL", cls.payment_url),
            payment_timeout_s=env_float("PAYMENT_TIMEOUT_S", cls.payment_timeout_s),
            http_timeout_s=env_float("HTTP_TIMEOUT_S", cls.http_timeout_s),
            breaker_failure_threshold=env_int(
                "BREAKER_FAILURE_THRESHOLD", cls.breaker_failure_threshold
            ),
            breaker_window_s=env_float("BREAKER_WINDOW_S", cls.breaker_window_s),
            breaker_cooldown_s=env_float("BREAKER_COOLDOWN_S", cls.breaker_cooldown_s),
            cart_ttl_s=env_int("CART_TTL_S", cls.cart_ttl_s),
            fault_error_rate=env_float("FAULT_ERROR_RATE", cls.fault_error_rate),
        )
