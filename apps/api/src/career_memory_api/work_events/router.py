"""Mountable FastAPI router and error handlers for M3 WorkEvent Read API (T-303)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from career_memory_api.auth.http import install_auth_error_handlers
from career_memory_api.auth.redaction import redact_text
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.contracts_api.models import (
    ErrorEnvelope,
    assert_no_prohibited_project_id,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_workspace_access_guard,
)
from career_memory_api.schema_contracts.enums import ContributionRole, WorkEventStatus
from career_memory_api.work_events.models import (
    WORK_EVENTS_NO_STORE_HEADER,
    WorkEventCounts,
    WorkEventDetailResponse,
    WorkEventError,
    WorkEventListResponse,
    WorkEventPageInfo,
    WorkEventStatusCounts,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

AccessResolver = Callable[[Request], WorkspaceAccessContext]


class WorkEventsReadPort(Protocol):
    """Service port for T-303 WorkEvent list and detail endpoints."""

    def list_work_events(
        self,
        access: WorkspaceAccessContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: WorkEventStatus | None = None,
        source_resource_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        role: ContributionRole | None = None,
    ) -> WorkEventListResponse: ...

    def get_work_event_detail(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
    ) -> WorkEventDetailResponse: ...


class InMemoryWorkEventsReadService:
    """Deterministic reference read handler for contract app & OpenAPI generation."""

    def list_work_events(
        self,
        access: WorkspaceAccessContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: WorkEventStatus | None = None,
        source_resource_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        role: ContributionRole | None = None,
    ) -> WorkEventListResponse:
        del access, limit, cursor, status, source_resource_id, from_date, to_date, role
        return WorkEventListResponse(
            items=[],
            page=WorkEventPageInfo(next_cursor=None, has_more=False),
            counts=WorkEventCounts(
                active_top_level_total=0,
                by_status=WorkEventStatusCounts(CANDIDATE=0, CONFIRMED=0, STALE=0),
            ),
        )

    def get_work_event_detail(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
    ) -> WorkEventDetailResponse:
        del access, event_id
        raise WorkspaceResourceNotFoundError()


def _resolve_request_id(request: Request) -> str:
    return (request.headers.get("X-Request-ID") or "").strip() or str(uuid.uuid4())


def _default_guard_access_resolver(request: Request) -> WorkspaceAccessContext:
    guard = get_workspace_access_guard(request)
    credentials = get_auth_credentials(
        authorization=request.headers.get("Authorization"),
    )
    target_ws = (request.headers.get("X-Workspace-ID") or "").strip()
    return guard.require_workspace_access(
        credentials=credentials,
        target_workspace_id=target_ws,
    )


def install_work_events_error_handlers(app: FastAPI) -> None:
    """Install structured error handlers for WorkEvent endpoints with private, no-store headers."""
    from career_memory_api.contracts_api.router import install_contract_error_handlers

    install_auth_error_handlers(app)
    install_contract_error_handlers(app)

    @app.exception_handler(WorkEventError)
    async def handle_work_event_error(
        request: Request, exc: WorkEventError
    ) -> JSONResponse:
        request_id = _resolve_request_id(request)
        payload: dict[str, Any] = {
            "code": exc.code,
            "message": redact_text(exc.message),
            "retryable": exc.retryable,
            "request_id": request_id,
        }
        if exc.details is not None:
            payload["details"] = exc.details
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": payload},
            headers={
                "Cache-Control": WORK_EVENTS_NO_STORE_HEADER,
                "X-Request-ID": request_id,
            },
        )


def create_work_events_router(
    *,
    service: WorkEventsReadPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M3 WorkEvents Read APIRouter (T-303)."""
    bound_service: WorkEventsReadPort = (
        InMemoryWorkEventsReadService() if service is None else service
    )
    access_resolver: AccessResolver = (
        _default_guard_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter(tags=["work-events"])

    @router.get(
        "/v1/work-events",
        status_code=200,
        response_model=WorkEventListResponse,
        operation_id="listWorkEvents",
        summary="List workspace-scoped WorkEvents with opaque cursor pagination (T-303)",
        responses={
            400: {"model": ErrorEnvelope, "description": "INVALID_WORK_EVENTS_CURSOR."},
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND (missing or cross-workspace source_resource_id)."},
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error or PROHIBITED_PROJECT_ID_SCOPE (OD-008).",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
    )
    async def list_work_events_route(
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[
            str | None,
            Query(description="HMAC-signed opaque cursor bound to workspace and filter parameters."),
        ] = None,
        status: Annotated[
            WorkEventStatus | None,
            Query(description="Optional WorkEvent status filter; defaults to active top-level events."),
        ] = None,
        source_resource_id: Annotated[
            str | None,
            Query(description="Optional source_resource UUID within the authenticated workspace."),
        ] = None,
        from_date: Annotated[
            date | None,
            Query(alias="from", description="Inclusive start date (YYYY-MM-DD, UTC)."),
        ] = None,
        to_date: Annotated[
            date | None,
            Query(alias="to", description="Inclusive end date (YYYY-MM-DD, UTC)."),
        ] = None,
        role: Annotated[
            ContributionRole | None,
            Query(description="Optional contribution role filter."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> WorkEventListResponse:
        # OD-008: reject project_id / project_ids query params immediately
        assert_no_prohibited_project_id(dict(request.query_params))

        request_id = _resolve_request_id(request)
        result = bound_service.list_work_events(
            access,
            limit=limit,
            cursor=cursor,
            status=status,
            source_resource_id=source_resource_id,
            from_date=from_date,
            to_date=to_date,
            role=role,
        )
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.get(
        "/v1/work-events/{id}",
        status_code=200,
        response_model=WorkEventDetailResponse,
        operation_id="getWorkEventDetail",
        summary="Read full WorkEvent detail including separated relations, composite snapshots, and immutable history (T-303)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND (missing or cross-workspace WorkEvent)."},
            422: {
                "model": ErrorEnvelope,
                "description": "PROHIBITED_PROJECT_ID_SCOPE (OD-008).",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
    )
    async def get_work_event_detail_route(
        id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> WorkEventDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))

        request_id = _resolve_request_id(request)
        detail = bound_service.get_work_event_detail(access, event_id=id)
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = detail.etag
        return detail

    return router


__all__ = [
    "InMemoryWorkEventsReadService",
    "WorkEventsReadPort",
    "create_work_events_router",
    "install_work_events_error_handlers",
]
