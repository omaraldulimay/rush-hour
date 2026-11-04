from dataclasses import dataclass

from libs.common.settings import database_url


@dataclass(frozen=True)
class Settings:
    database_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(database_url())
