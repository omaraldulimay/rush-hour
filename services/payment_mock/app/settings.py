from dataclasses import dataclass

from libs.common.settings import env_float, env_int


@dataclass(frozen=True)
class Settings:
    latency_ms: int = 0
    error_rate: float = 0.0
    decline_rate: float = 0.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            env_int("PAYMENT_LATENCY_MS", 0),
            env_float("PAYMENT_ERROR_RATE", 0.0),
            env_float("PAYMENT_DECLINE_RATE", 0.0),
        )
