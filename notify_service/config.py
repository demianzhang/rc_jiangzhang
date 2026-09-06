import math
import os
from dataclasses import dataclass
from pathlib import Path

from .policy import normalize_origin


@dataclass(frozen=True)
class Settings:
    api_key: str
    allowed_origins: frozenset[str]
    db_path: Path = Path("data") / "notifications.sqlite3"
    max_attempts: int = 6
    attempt_timeout_seconds: float = 10
    lease_seconds: float = 30
    poll_seconds: float = 0.5
    retry_base_seconds: float = 30
    retry_cap_seconds: float = 3600
    retry_after_cap_seconds: float = 3600

    def __post_init__(self) -> None:
        if len(self.api_key) < 32 or any(not 33 <= ord(char) <= 126 for char in self.api_key):
            raise ValueError("NOTIFY_API_KEY must contain at least 32 printable non-space ASCII characters")
        if not self.allowed_origins:
            raise ValueError("NOTIFY_ALLOWED_ORIGINS must contain explicit trusted origins")
        object.__setattr__(
            self, "allowed_origins",
            frozenset(normalize_origin(origin) for origin in self.allowed_origins),
        )
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("NOTIFY_MAX_ATTEMPTS must be between 1 and 100")
        durations = (
            self.attempt_timeout_seconds, self.lease_seconds, self.poll_seconds,
            self.retry_base_seconds, self.retry_cap_seconds, self.retry_after_cap_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in durations):
            raise ValueError("All timing settings must be finite positive numbers")
        if self.lease_seconds < self.attempt_timeout_seconds + 5:
            raise ValueError("Lease must exceed the whole-request timeout by at least 5 seconds")
        if self.retry_cap_seconds < self.retry_base_seconds:
            raise ValueError("Retry cap must be at least the retry base")

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.environ.get("NOTIFY_API_KEY", "")
        origins = frozenset(
            value.strip()
            for value in os.environ.get("NOTIFY_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        )
        return cls(
            api_key=key,
            allowed_origins=origins,
            db_path=Path(os.environ.get("NOTIFY_DB_PATH", str(cls.db_path))),
            max_attempts=int(os.environ.get("NOTIFY_MAX_ATTEMPTS", "6")),
            attempt_timeout_seconds=float(os.environ.get("NOTIFY_ATTEMPT_TIMEOUT_SECONDS", "10")),
            lease_seconds=float(os.environ.get("NOTIFY_LEASE_SECONDS", "30")),
            poll_seconds=float(os.environ.get("NOTIFY_POLL_SECONDS", "0.5")),
            retry_base_seconds=float(os.environ.get("NOTIFY_RETRY_BASE_SECONDS", "30")),
            retry_cap_seconds=float(os.environ.get("NOTIFY_RETRY_CAP_SECONDS", "3600")),
            retry_after_cap_seconds=float(os.environ.get("NOTIFY_RETRY_AFTER_CAP_SECONDS", "3600")),
        )
