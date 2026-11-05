from dataclasses import dataclass

from libs.common.settings import database_url, env_float, env_str


@dataclass(frozen=True)
class Settings:
    database_url: str
    prometheus_url: str = "http://localhost:9090"
    catalog_url: str = "http://localhost:8001"
    checkout_url: str = "http://localhost:8003"
    payment_url: str = "http://localhost:8004"
    http_timeout_s: float = 2.0
    gauge_interval_s: float = 5.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=database_url(),
            prometheus_url=env_str("PROMETHEUS_URL", cls.prometheus_url),
            catalog_url=env_str("CATALOG_URL", cls.catalog_url),
            checkout_url=env_str("CHECKOUT_URL", cls.checkout_url),
            payment_url=env_str("PAYMENT_URL", cls.payment_url),
            http_timeout_s=env_float("HTTP_TIMEOUT_S", cls.http_timeout_s),
        )
