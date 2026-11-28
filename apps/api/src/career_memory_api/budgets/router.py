"""FastAPI router for Workspace Quota Status (`GET /v1/quota-status`, T-503 / INF-06)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.budgets.models import BudgetError, QuotaStatusResponse
from career_memory_api.budgets.postgres_service import (
    InMemoryQuotaBudgetService,
    QuotaBudgetPort,
)
from career_memory_api.contracts_api.models import (
    PRIVATE_NO_STORE_HEADER,
    ErrorEnvelope,
)
from career_memory_api.dependencies import get_auth_credentials, get_workspace_access_guard

AccessResolver = Callable[[Request], WorkspaceAccessContext]


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


def install_budgets_error_handlers(app: FastAPI) -> None:
    """Install structured error envelope handlers for ``BudgetError`` / ``BudgetCutoffError``."""

    @app.exception_handler(BudgetError)
    async def handle_budget_error(request: Request, exc: BudgetError) -> JSONResponse:
        request_id = (request.headers.get("X-Request-ID") or "").strip() or str(uuid.uuid4())
        error_payload: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "request_id": request_id,
            "paid_fallback_allowed": False,
            "free_alternatives": list(exc.free_alternatives),
        }
        if exc.retry_after_seconds is not None:
            error_payload["retry_after_seconds"] = int(exc.retry_after_seconds)
        if exc.details is not None:
            error_payload["details"] = exc.details
        headers = {
            "Cache-Control": PRIVATE_NO_STORE_HEADER,
            "X-Request-ID": request_id,
        }
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(int(exc.retry_after_seconds))
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": error_payload},
            headers=headers,
        )


def create_budgets_router(
    *,
    service: QuotaBudgetPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M5 quota status APIRouter (`GET /v1/quota-status`)."""
    bound_service: QuotaBudgetPort = (
        InMemoryQuotaBudgetService() if service is None else service
    )
    access_resolver: AccessResolver = (
        _default_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter()

    @router.get(
        "/v1/quota-status",
        status_code=200,
        response_model=QuotaStatusResponse,
        tags=["quotas"],
        operation_id="getWorkspaceQuotaStatus",
        summary="Read workspace quota meters, admission state, pause controls, and circuit breaker state",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            503: {"model": ErrorEnvelope, "description": "Auth or budget verification unavailable."},
        },
    )
    async def get_workspace_quota_status_route(
        response: Response,
        source_resource_id: Annotated[
            str | None,
            Query(description="Optional source resource UUID to include resource-level pause controls."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> QuotaStatusResponse:
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.get_quota_status(
            access, source_resource_id=source_resource_id
        )

    return router


__all__ = [
    "AccessResolver",
    "create_budgets_router",
    "install_budgets_error_handlers",
]
