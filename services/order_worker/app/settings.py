from dataclasses import dataclass

from libs.common.settings import database_url, env_float, env_int, env_str, redis_url


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    inventory_url: str = "http://localhost:8002"
    payment_url: str = "http://localhost:8004"
    payment_timeout_s: float = 2.0
    http_timeout_s: float = 5.0
    max_attempts: int = 5
    retry_base_delay_s: float = 5.0
    retry_max_delay_s: float = 300.0
    consumer_name: str = "worker-1"
    sweep_interval_s: float = 30.0
    stale_created_after_s: float = 120.0
    orphan_pending_after_s: float = 120.0
    sweep_batch_size: int = 100

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=database_url(),
            redis_url=redis_url(),
            inventory_url=env_str("INVENTORY_URL", cls.inventory_url),
            payment_url=env_str("PAYMENT_URL", cls.payment_url),
            payment_timeout_s=env_float("PAYMENT_TIMEOUT_S", cls.payment_timeout_s),
            http_timeout_s=env_float("HTTP_TIMEOUT_S", cls.http_timeout_s),
            max_attempts=env_int("WORKER_MAX_ATTEMPTS", cls.max_attempts),
            retry_base_delay_s=env_float("RETRY_BASE_DELAY_S", cls.retry_base_delay_s),
            retry_max_delay_s=env_float("RETRY_MAX_DELAY_S", cls.retry_max_delay_s),
            consumer_name=env_str("HOSTNAME", cls.consumer_name),
            sweep_interval_s=env_float("SWEEP_INTERVAL_S", cls.sweep_interval_s),
            stale_created_after_s=env_float("STALE_CREATED_AFTER_S", cls.stale_created_after_s),
            orphan_pending_after_s=env_float("ORPHAN_PENDING_AFTER_S", cls.orphan_pending_after_s),
            sweep_batch_size=env_int("SWEEP_BATCH_SIZE", cls.sweep_batch_size),
        )
