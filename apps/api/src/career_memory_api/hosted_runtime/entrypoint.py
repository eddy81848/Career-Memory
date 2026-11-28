"""Multi-role Cloud Run container entrypoint (``CAREER_MEMORY_SERVICE_ROLE=api|worker``)."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn
from fastapi import FastAPI

from career_memory_api.auth.redaction import RedactingLogFilter
from career_memory_api.hosted_runtime.api_service import create_hosted_api_app_from_env
from career_memory_api.hosted_runtime.models import HostedServiceRole
from career_memory_api.hosted_runtime.worker_service import create_hosted_worker_app_from_env


def _configure_redacted_logging() -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=os.environ.get("CAREER_MEMORY_LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    redacting_filter = RedactingLogFilter()
    for handler in root_logger.handlers:
        handler.addFilter(redacting_filter)


def create_app_for_role(role: HostedServiceRole | str | None = None) -> FastAPI:
    """Instantiate either the hosted API or private IAM Worker FastAPI app by role."""
    resolved_role = (
        role
        if isinstance(role, HostedServiceRole)
        else HostedServiceRole.from_env(
            role if role is not None else os.environ.get("CAREER_MEMORY_SERVICE_ROLE")
        )
    )
    if resolved_role is HostedServiceRole.API:
        return create_hosted_api_app_from_env()
    return create_hosted_worker_app_from_env()


def main() -> int:
    _configure_redacted_logging()
    role = HostedServiceRole.from_env(os.environ.get("CAREER_MEMORY_SERVICE_ROLE"))
    host = os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("PORT", "8080"))
    limit_concurrency = int(os.environ.get("UVICORN_LIMIT_CONCURRENCY", "16"))
    timeout_keep_alive = int(os.environ.get("UVICORN_TIMEOUT_KEEP_ALIVE", "5"))
    timeout_graceful_shutdown = int(os.environ.get("UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN", "25"))

    app = create_app_for_role(role)
    uvicorn.run(
        app,
        host=host,
        port=port,
        limit_concurrency=max(1, limit_concurrency),
        timeout_keep_alive=max(1, timeout_keep_alive),
        timeout_graceful_shutdown=max(1, timeout_graceful_shutdown),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
