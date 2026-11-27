"""Local ASGI entrypoint for running Career Memory API with uvicorn."""

from __future__ import annotations

import uvicorn

from career_memory_api.app import create_app
from career_memory_api.config import Settings

app = create_app()


def run() -> None:
    """Run the local development server."""
    settings = Settings.from_env()
    uvicorn.run(
        "career_memory_api.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    run()
