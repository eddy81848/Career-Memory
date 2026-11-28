"""Mountable FastAPI router and error handlers for M4 Facts API (/v1/facts*, MVP API §6, ER-012)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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
from career_memory_api.facts.models import (
    FACTS_NO_STORE_HEADER,
    ApproveFactRequest,
    ApproveFactResponse,
    EditFactRequest,
    EditFactResponse,
    FactError,
    FactListResponse,
    FactPageInfo,
    FactSupportsResponse,
    RejectFactRequest,
    RejectFactResponse,
    assert_no_forbidden_fact_write_keys,
)
from career_memory_api.schema_contracts.enums import FactStatus, FactType
from career_memory_api.sync_runs.models import parse_idempotency_key
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

AccessResolver = Callable[[Request], WorkspaceAccessContext]


class FactsPort(Protocol):
    """Service port for /v1/facts* read and mutation endpoints."""

    def list_facts(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str | None = None,
        status: FactStatus | None = None,
        fact_type: FactType | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> FactListResponse: ...

    def get_fact_supports(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
    ) -> FactSupportsResponse: ...

    def approve_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: ApproveFactRequest,
        if_match_header: str | None = None,
    ) -> ApproveFactResponse: ...

    def reject_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: RejectFactRequest,
        if_match_header: str | None = None,
    ) -> RejectFactResponse: ...

    def edit_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: EditFactRequest,
        if_match_header: str | None = None,
    ) -> EditFactResponse: ...


class InMemoryFactsService:
    """Deterministic reference handler for contract app & OpenAPI generation."""

    def list_facts(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str | None = None,
        status: FactStatus | None = None,
        fact_type: FactType | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> FactListResponse:
        del access, event_id, status, fact_type, include_superseded, limit, cursor
        return FactListResponse(
            items=[],
            page=FactPageInfo(next_cursor=None, has_more=False),
        )

    def get_fact_supports(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
    ) -> FactSupportsResponse:
        del access, fact_id
        raise WorkspaceResourceNotFoundError()

    def approve_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: ApproveFactRequest,
        if_match_header: str | None = None,
    ) -> ApproveFactResponse:
        del access, fact_id, idempotency_key, request, if_match_header
        raise WorkspaceResourceNotFoundError()

    def reject_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: RejectFactRequest,
        if_match_header: str | None = None,
    ) -> RejectFactResponse:
        del access, fact_id, idempotency_key, request, if_match_header
        raise WorkspaceResourceNotFoundError()

    def edit_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: EditFactRequest,
        if_match_header: str | None = None,
    ) -> EditFactResponse:
        del access, fact_id, idempotency_key, request, if_match_header
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


async def _parse_json_object(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    raw_bytes = await request.body()
    if allow_empty and not raw_bytes.strip():
        return {}
    try:
        raw = await request.json()
    except ValueError as exc:
        raise FactError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise FactError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be a JSON object.",
        )
    return raw


def install_facts_error_handlers(app: FastAPI) -> None:
    """Install structured error handlers for /v1/facts* endpoints with private, no-store headers."""
    from career_memory_api.contracts_api.router import install_contract_error_handlers

    install_auth_error_handlers(app)
    install_contract_error_handlers(app)

    @app.exception_handler(FactError)
    async def handle_fact_error(request: Request, exc: FactError) -> JSONResponse:
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
                "Cache-Control": FACTS_NO_STORE_HEADER,
                "X-Request-ID": request_id,
            },
        )


def create_facts_router(
    *,
    service: FactsPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M4 Facts APIRouter (/v1/facts*)."""
    bound_service: FactsPort = InMemoryFactsService() if service is None else service
    access_resolver: AccessResolver = (
        _default_guard_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter(tags=["facts"])

    @router.get(
        "/v1/facts",
        status_code=200,
        response_model=FactListResponse,
        operation_id="listWorkspaceFacts",
        summary="List workspace facts with optional event/status/type filters and opaque pagination",
        responses={
            400: {"model": ErrorEnvelope, "description": "INVALID_FACTS_CURSOR."},
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error or PROHIBITED_PROJECT_ID_SCOPE (OD-008).",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
    )
    async def list_facts_route(
        request: Request,
        response: Response,
        event_id: Annotated[str | None, Query(description="Optional WorkEvent UUID filter.")] = None,
        status: Annotated[FactStatus | None, Query(description="Optional FactStatus filter.")] = None,
        fact_type: Annotated[FactType | None, Query(description="Optional FactType filter.")] = None,
        include_superseded: Annotated[
            bool,
            Query(description="Include SUPERSEDED fact versions (defaults to false)."),
        ] = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[
            str | None, Query(description="Opaque workspace- and filter-bound cursor.")
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> FactListResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.list_facts(
            access,
            event_id=event_id,
            status=status,
            fact_type=fact_type,
            include_superseded=include_superseded,
            limit=limit,
            cursor=cursor,
        )
        response.headers["Cache-Control"] = FACTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.get(
        "/v1/facts/{fact_id}/supports",
        status_code=200,
        response_model=FactSupportsResponse,
        operation_id="getFactSupports",
        summary="Return a Fact and its non-raw FactSupport anchors (Plan C: zero raw quotes)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {
                "model": ErrorEnvelope,
                "description": "RESOURCE_NOT_FOUND (missing or cross-workspace).",
            },
            422: {"model": ErrorEnvelope, "description": "PROHIBITED_PROJECT_ID_SCOPE (OD-008)."},
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
    )
    async def get_fact_supports_route(
        fact_id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> FactSupportsResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.get_fact_supports(access, fact_id=fact_id)
        response.headers["Cache-Control"] = FACTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = result.fact.etag
        return result

    @router.post(
        "/v1/facts/{fact_id}/actions/approve",
        status_code=200,
        response_model=ApproveFactResponse,
        operation_id="approveFact",
        summary="Approve an eligible CANDIDATE fact as USER_CONFIRMED (never SUPPORTED)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "FACT_VERSION_CONFLICT, FACT_NOT_APPROVABLE, DELETION_PENDING, "
                    "or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "INVALID_IDEMPOTENCY_KEY, MISSING_EXPECTED_VERSION, "
                    "PROHIBITED_FACT_STATUS_WRITE, PROHIBITED_RAW_TEXT_PERSISTENCE, "
                    "or PROHIBITED_PROJECT_ID_SCOPE."
                ),
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": False,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ApproveFactRequest"}
                    }
                },
            }
        },
    )
    async def approve_fact_route(
        fact_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match", description='Optional version token (e.g. "v1").'),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ApproveFactResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise FactError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request, allow_empty=True)
        assert_no_forbidden_fact_write_keys(raw)
        try:
            parsed = ApproveFactRequest.model_validate(raw)
        except ValidationError as exc:
            raise FactError(
                status_code=422,
                code="INVALID_APPROVE_FACT_REQUEST",
                message="Fact approve request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.approve_fact(
            access,
            fact_id=fact_id,
            idempotency_key=normalized_key,
            request=parsed,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = FACTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = result.fact.etag
        return result

    @router.post(
        "/v1/facts/{fact_id}/actions/reject",
        status_code=200,
        response_model=RejectFactResponse,
        operation_id="rejectFact",
        summary="Reject a fact and atomically cascade STALE/INVALIDATED downstream",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "FACT_VERSION_CONFLICT, FACT_NOT_REJECTABLE, DELETION_PENDING, "
                    "or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "INVALID_IDEMPOTENCY_KEY, INVALID_REJECT_FACT_REQUEST, "
                    "PROHIBITED_FACT_STATUS_WRITE, PROHIBITED_RAW_TEXT_PERSISTENCE, "
                    "or PROHIBITED_PROJECT_ID_SCOPE."
                ),
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/RejectFactRequest"}
                    }
                },
            }
        },
    )
    async def reject_fact_route(
        fact_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match", description='Optional version token (e.g. "v1").'),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> RejectFactResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise FactError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request, allow_empty=False)
        assert_no_forbidden_fact_write_keys(raw)
        try:
            parsed = RejectFactRequest.model_validate(raw)
        except ValidationError as exc:
            raise FactError(
                status_code=422,
                code="INVALID_REJECT_FACT_REQUEST",
                message="Fact reject request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.reject_fact(
            access,
            fact_id=fact_id,
            idempotency_key=normalized_key,
            request=parsed,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = FACTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = result.fact.etag
        return result

    @router.patch(
        "/v1/facts/{fact_id}",
        status_code=200,
        response_model=EditFactResponse,
        operation_id="editFactVersion",
        summary="Create a new CANDIDATE fact version (ER-012 non-overwrite edit) and invalidate downstream",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "FACT_VERSION_CONFLICT, FACT_NOT_EDITABLE, DELETION_PENDING, "
                    "or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "INVALID_IDEMPOTENCY_KEY, EMPTY_FACT_EDIT, INVALID_EDIT_FACT_REQUEST, "
                    "PROHIBITED_FACT_STATUS_WRITE, PROHIBITED_RAW_TEXT_PERSISTENCE, "
                    "or PROHIBITED_PROJECT_ID_SCOPE."
                ),
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/EditFactRequest"}
                    }
                },
            }
        },
    )
    async def edit_fact_route(
        fact_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match", description='Optional version token (e.g. "v1").'),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> EditFactResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise FactError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request, allow_empty=False)
        assert_no_forbidden_fact_write_keys(raw)
        try:
            parsed = EditFactRequest.model_validate(raw)
        except ValidationError as exc:
            raise FactError(
                status_code=422,
                code="INVALID_EDIT_FACT_REQUEST",
                message="Fact edit request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.edit_fact(
            access,
            fact_id=fact_id,
            idempotency_key=normalized_key,
            request=parsed,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = FACTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = result.fact.etag
        return result

    return router


__all__ = [
    "AccessResolver",
    "FactsPort",
    "InMemoryFactsService",
    "create_facts_router",
    "install_facts_error_handlers",
]
