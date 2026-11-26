"""Local runtime configuration for Career Memory API.

Intentionally contains no flags to enable synthetic or bypass auth providers.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_APP_NAME = "Career Memory API"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_LOG_LEVEL = "info"


@dataclass(frozen=True, slots=True)
class Settings:
    """Minimal local server settings for the FastAPI scaffold."""

    app_name: str = DEFAULT_APP_NAME
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL

    def __post_init__(self) -> None:
        if not self.app_name.strip():
            raise ValueError("app_name must be non-empty.")
        if not self.host.strip():
            raise ValueError("host must be non-empty.")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be between 1 and 65535, got {self.port}.")
        if not self.log_level.strip():
            raise ValueError("log_level must be non-empty.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Load local server settings from environment variables."""
        env = os.environ if environ is None else environ
        raw_port = env.get("CAREER_MEMORY_API_PORT", str(DEFAULT_PORT)).strip()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError(
                f"CAREER_MEMORY_API_PORT must be an integer, got {raw_port!r}."
            ) from exc

        return cls(
            app_name=env.get("CAREER_MEMORY_API_NAME", DEFAULT_APP_NAME).strip(),
            host=env.get("CAREER_MEMORY_API_HOST", DEFAULT_HOST).strip(),
            port=port,
            log_level=env.get("CAREER_MEMORY_API_LOG_LEVEL", DEFAULT_LOG_LEVEL)
            .strip()
            .lower(),
        )
