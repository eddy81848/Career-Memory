"""FastAPI router for read-only Deletion Job status (`GET /v1/deletion-jobs/{deletion_job_id}`)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.contracts_api.models import (
    PRIVATE_NO_STORE_HEADER,
    ErrorEnvelope,
)
from career_memory_api.deletion.models import DeletionError, DeletionJobStatusResponse
from career_memory_api.dependencies import get_auth_credentials, get_workspace_access_guard

AccessResolver = Callable[[Request], WorkspaceAccessContext]


class DeletionReadPort(Protocol):
    """Read port for inspecting workspace-scoped deletion job status & component receipts."""

    def get_deletion_job(
        self, access: WorkspaceAccessContext, *, deletion_job_id: str
    ) -> DeletionJobStatusResponse: ...


class _UnconfiguredDeletionReadService:
    def get_deletion_job(
        self, access: WorkspaceAccessContext, *, deletion_job_id: str
    ) -> DeletionJobStatusResponse:
        raise DeletionError(
            status_code=404,
            code="RESOURCE_NOT_FOUND",
            message="Requested resource was not found.",
        )


def _default_access_resolver(request: Request) -> WorkspaceAccessContext:
    guard = get_workspace_access_guard(request)
    credentials = get_auth_credentials(
        authorization=request.headers.get("Authorization"),
        x_session_id=request.headers.get("X-Session-ID"),
    )
    target_ws = (request.headers.get("X-Workspace-ID") or "").strip()
    return guard.require_workspace_access(
        credentials=credentials,
        target_workspace_id=target_ws,
    )


def install_deletion_error_handlers(app: FastAPI) -> None:
    """Install structured error envelope handlers for ``DeletionError``."""

    @app.exception_handler(DeletionError)
    async def handle_deletion_error(request: Request, exc: DeletionError) -> JSONResponse:
        request_id = (request.headers.get("X-Request-ID") or "").strip() or str(uuid.uuid4())
        error_payload: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "request_id": request_id,
        }
        if exc.details is not None:
            error_payload["details"] = exc.details
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": error_payload},
            headers={
                "Cache-Control": PRIVATE_NO_STORE_HEADER,
                "X-Request-ID": request_id,
            },
        )


def create_deletion_router(
    *,
    service: DeletionReadPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M5 read-only deletion status APIRouter."""
    bound_service: DeletionReadPort = (
        _UnconfiguredDeletionReadService() if service is None else service
    )
    access_resolver: AccessResolver = (
        _default_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter()

    @router.get(
        "/v1/deletion-jobs/{deletion_job_id}",
        status_code=200,
        response_model=DeletionJobStatusResponse,
        tags=["source-resources"],
        operation_id="getDeletionJobStatus",
        summary="Read workspace-scoped source deletion job status and per-component cleanup receipts",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "Deletion job not found or concealed."},
        },
    )
    async def get_deletion_job_route(
        deletion_job_id: str,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> DeletionJobStatusResponse:
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.get_deletion_job(access, deletion_job_id=deletion_job_id)

    return router


__all__ = [
    "AccessResolver",
    "DeletionReadPort",
    "create_deletion_router",
    "install_deletion_error_handlers",
]
