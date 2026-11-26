"""Environment configuration. Deliberately has NO fault-injection settings."""

import os
import socket
from dataclasses import dataclass, field


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class Settings:
    database_url: str
    worker_wake_url: str | None = None
    wake_timeout_s: float = 2.0
    lease_s: float = 30.0
    max_jobs_per_wake: int = 5
    worker_id: str = field(default_factory=_default_worker_id)

    def __post_init__(self):
        if not 0 < self.wake_timeout_s <= 10:
            raise ValueError("wake_timeout_s must be in (0, 10]")
        if not 1 <= self.max_jobs_per_wake <= 50:
            raise ValueError("max_jobs_per_wake must be in [1, 50]")

    @classmethod
    def from_env(cls) -> "Settings":
        dsn = os.environ.get("T007A_DATABASE_URL")
        if not dsn:
            raise RuntimeError("T007A_DATABASE_URL is required")
        return cls(
            database_url=dsn,
            worker_wake_url=os.environ.get("T007A_WORKER_WAKE_URL") or None,
            wake_timeout_s=float(os.environ.get("T007A_WAKE_TIMEOUT_S", "2")),
            lease_s=float(os.environ.get("T007A_LEASE_S", "30")),
            max_jobs_per_wake=int(os.environ.get("T007A_MAX_JOBS_PER_WAKE", "5")),
            worker_id=os.environ.get("T007A_WORKER_ID") or _default_worker_id(),
        )
