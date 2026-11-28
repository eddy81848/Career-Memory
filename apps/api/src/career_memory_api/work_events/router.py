"""Mountable FastAPI router and error handlers for M3 WorkEvent Read & Mutation API (T-303 / T-304)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from career_memory_api.auth.http import install_auth_error_handlers
from career_memory_api.auth.redaction import redact_text
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.contracts_api.models import (
    ErrorEnvelope,
    assert_no_plan_c_raw_text_keys,
    assert_no_prohibited_project_id,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_workspace_access_guard,
)
from career_memory_api.schema_contracts.enums import ContributionRole, WorkEventStatus
from career_memory_api.work_events.models import (
    WORK_EVENTS_NO_STORE_HEADER,
    ConfirmWorkEventRequest,
    DissolveWorkEventRequest,
    DissolveWorkEventResponse,
    EditWorkEventRequest,
    MergeWorkEventsRequest,
    MergeWorkEventsResponse,
    RejectWorkEventRequest,
    SplitWorkEventRequest,
    SplitWorkEventResponse,
    WorkEventCounts,
    WorkEventDetailResponse,
    WorkEventError,
    WorkEventListResponse,
    WorkEventPageInfo,
    WorkEventStatusCounts,
    parse_if_match_version,
    resolve_idempotency_key,
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


class WorkEventsMutationPort(Protocol):
    """Service port for T-304 / T-304A / T-304B atomic WorkEvent curation endpoints."""

    def confirm_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: ConfirmWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse: ...

    def edit_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: EditWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse: ...

    def reject_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: RejectWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse: ...

    def split_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: SplitWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> SplitWorkEventResponse: ...

    def merge_work_events(
        self,
        access: WorkspaceAccessContext,
        *,
        request: MergeWorkEventsRequest,
        idempotency_key_header: str | None = None,
    ) -> MergeWorkEventsResponse: ...

    def dissolve_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: DissolveWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> DissolveWorkEventResponse: ...


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


class InMemoryWorkEventsMutationService:
    """Deterministic reference mutation handler for contract app & OpenAPI generation."""

    def confirm_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: ConfirmWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse:
        del access, event_id
        resolve_idempotency_key(
            header_key=idempotency_key_header, body_key=request.idempotency_key
        )
        if_match_ver = parse_if_match_version(if_match_header)
        if if_match_ver is not None and if_match_ver != request.expected_version:
            raise WorkEventError(
                status_code=422,
                code="VERSION_EXPECTATION_MISMATCH",
                message="If-Match version does not match expected_version.",
            )
        raise WorkspaceResourceNotFoundError()

    def edit_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: EditWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse:
        del access, event_id
        resolve_idempotency_key(
            header_key=idempotency_key_header, body_key=request.idempotency_key
        )
        if_match_ver = parse_if_match_version(if_match_header)
        if if_match_ver is not None and if_match_ver != request.expected_version:
            raise WorkEventError(
                status_code=422,
                code="VERSION_EXPECTATION_MISMATCH",
                message="If-Match version does not match expected_version.",
            )
        raise WorkspaceResourceNotFoundError()

    def reject_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: RejectWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse:
        del access, event_id
        resolve_idempotency_key(
            header_key=idempotency_key_header, body_key=request.idempotency_key
        )
        if_match_ver = parse_if_match_version(if_match_header)
        if if_match_ver is not None and if_match_ver != request.expected_version:
            raise WorkEventError(
                status_code=422,
                code="VERSION_EXPECTATION_MISMATCH",
                message="If-Match version does not match expected_version.",
            )
        raise WorkspaceResourceNotFoundError()

    def split_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: SplitWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> SplitWorkEventResponse:
        del access, event_id
        resolve_idempotency_key(
            header_key=idempotency_key_header, body_key=request.idempotency_key
        )
        if_match_ver = parse_if_match_version(if_match_header)
        if if_match_ver is not None and if_match_ver != request.expected_version:
            raise WorkEventError(
                status_code=422,
                code="VERSION_EXPECTATION_MISMATCH",
                message="If-Match version does not match expected_version.",
            )
        raise WorkspaceResourceNotFoundError()

    def merge_work_events(
        self,
        access: WorkspaceAccessContext,
        *,
        request: MergeWorkEventsRequest,
        idempotency_key_header: str | None = None,
    ) -> MergeWorkEventsResponse:
        del access
        resolve_idempotency_key(
            header_key=idempotency_key_header, body_key=request.idempotency_key
        )
        raise WorkspaceResourceNotFoundError()

    def dissolve_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: DissolveWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> DissolveWorkEventResponse:
        del access, event_id
        resolve_idempotency_key(
            header_key=idempotency_key_header, body_key=request.idempotency_key
        )
        if_match_ver = parse_if_match_version(if_match_header)
        if if_match_ver is not None and if_match_ver != request.expected_version:
            raise WorkEventError(
                status_code=422,
                code="VERSION_EXPECTATION_MISMATCH",
                message="If-Match version does not match expected_version.",
            )
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


async def _parse_json_object(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except ValueError as exc:
        raise WorkEventError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise WorkEventError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be a JSON object.",
        )
    return raw


def _validate_mutation_body(raw: dict[str, Any], model_cls: type[Any], *, error_code: str) -> Any:
    assert_no_prohibited_project_id(raw)
    assert_no_plan_c_raw_text_keys(raw)
    try:
        return model_cls.model_validate(raw)
    except ValidationError as exc:
        raise WorkEventError(
            status_code=422,
            code=error_code,
            message="WorkEvent mutation request failed schema validation.",
            details={"errors": exc.errors()},
        ) from exc


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
    mutation_service: WorkEventsMutationPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M3 WorkEvents Read & Mutation APIRouter (T-303 / T-304)."""
    bound_service: WorkEventsReadPort = (
        InMemoryWorkEventsReadService() if service is None else service
    )
    if mutation_service is not None:
        bound_mutation_service: WorkEventsMutationPort = mutation_service
    elif service is not None and hasattr(service, "confirm_work_event"):
        bound_mutation_service = service  # type: ignore[assignment]
    else:
        bound_mutation_service = InMemoryWorkEventsMutationService()

    access_resolver: AccessResolver = (
        _default_guard_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter(tags=["work-events"])

    # -------------------------------------------------------------------------
    # Read Endpoints (T-303)
    # -------------------------------------------------------------------------
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
            404: {
                "model": ErrorEnvelope,
                "description": "RESOURCE_NOT_FOUND (missing or cross-workspace source_resource_id).",
            },
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

    # -------------------------------------------------------------------------
    # Static-path Mutation Endpoint: MERGE (register before /{id} routes)
    # -------------------------------------------------------------------------
    @router.post(
        "/v1/work-events/actions/merge",
        status_code=200,
        response_model=MergeWorkEventsResponse,
        operation_id="mergeWorkEvents",
        summary="Manually merge >=2 active same-repository WorkEvents into a USER_COMPOSITE (T-304B)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND (missing or cross-workspace member)."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "DUPLICATE_COMPOSITE_MEMBER, CROSS_REPOSITORY_MEMBER, NESTED_COMPOSITE_NOT_ALLOWED, "
                    "MEMBER_ALREADY_COMPOSED, MEMBER_NOT_ACTIVE, EVENT_VERSION_CONFLICT, "
                    "CANNOT_LINK_CONFLICT, PR_ASSOCIATION_INCOMPLETE, DELETION_PENDING, or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "Validation error, NON_EVENT_MEMBER_NOT_ALLOWED, PROHIBITED_RAW_TEXT_PERSISTENCE, "
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
                        "schema": {"$ref": "#/components/schemas/MergeWorkEventsRequest"}
                    }
                },
            }
        },
    )
    async def merge_work_events_route(
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                description="Idempotency key header (must match body idempotency_key if both supplied).",
            ),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> MergeWorkEventsResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        raw = await _parse_json_object(request)
        parsed: MergeWorkEventsRequest = _validate_mutation_body(
            raw, MergeWorkEventsRequest, error_code="INVALID_MERGE_REQUEST"
        )
        request_id = _resolve_request_id(request)
        result = bound_mutation_service.merge_work_events(
            access,
            request=parsed,
            idempotency_key_header=idempotency_key,
        )
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = result.composite_event.etag
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
            404: {
                "model": ErrorEnvelope,
                "description": "RESOURCE_NOT_FOUND (missing or cross-workspace WorkEvent).",
            },
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

    # -------------------------------------------------------------------------
    # Single-Event Mutation Endpoints (T-304 / T-304A / T-304B)
    # -------------------------------------------------------------------------
    @router.patch(
        "/v1/work-events/{id}",
        status_code=200,
        response_model=WorkEventDetailResponse,
        operation_id="editWorkEvent",
        summary="Edit a WorkEvent into a new CANDIDATE version without overwriting historical snapshots (T-304)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": "EVENT_VERSION_CONFLICT, EVENT_NOT_ACTIVE, DELETION_PENDING, or IDEMPOTENCY_CONFLICT.",
            },
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error, PROHIBITED_RAW_TEXT_PERSISTENCE, or PROHIBITED_PROJECT_ID_SCOPE.",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/EditWorkEventRequest"}
                    }
                },
            }
        },
    )
    async def edit_work_event_route(
        id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match"),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> WorkEventDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        raw = await _parse_json_object(request)
        parsed: EditWorkEventRequest = _validate_mutation_body(
            raw, EditWorkEventRequest, error_code="INVALID_EDIT_REQUEST"
        )
        request_id = _resolve_request_id(request)
        detail = bound_mutation_service.edit_work_event(
            access,
            event_id=id,
            request=parsed,
            idempotency_key_header=idempotency_key,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = detail.etag
        return detail

    @router.post(
        "/v1/work-events/{id}/actions/confirm",
        status_code=200,
        response_model=WorkEventDetailResponse,
        operation_id="confirmWorkEvent",
        summary="Confirm (Keep) an active CANDIDATE/STALE WorkEvent (T-304)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "EVENT_VERSION_CONFLICT, EVENT_ALREADY_CONFIRMED, EVENT_NOT_ACTIVE, "
                    "DELETION_PENDING, or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error, PROHIBITED_RAW_TEXT_PERSISTENCE, or PROHIBITED_PROJECT_ID_SCOPE.",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ConfirmWorkEventRequest"}
                    }
                },
            }
        },
    )
    async def confirm_work_event_route(
        id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match"),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> WorkEventDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        raw = await _parse_json_object(request)
        parsed: ConfirmWorkEventRequest = _validate_mutation_body(
            raw, ConfirmWorkEventRequest, error_code="INVALID_CONFIRM_REQUEST"
        )
        request_id = _resolve_request_id(request)
        detail = bound_mutation_service.confirm_work_event(
            access,
            event_id=id,
            request=parsed,
            idempotency_key_header=idempotency_key,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = detail.etag
        return detail

    @router.post(
        "/v1/work-events/{id}/actions/reject",
        status_code=200,
        response_model=WorkEventDetailResponse,
        operation_id="rejectWorkEvent",
        summary="Reject (Exclude) an active WorkEvent while preserving all underlying source provenance (T-304)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "EVENT_VERSION_CONFLICT, USER_COMPOSITE_REJECT_REQUIRES_DISSOLVE, "
                    "EVENT_NOT_ACTIVE, DELETION_PENDING, or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error, PROHIBITED_RAW_TEXT_PERSISTENCE, or PROHIBITED_PROJECT_ID_SCOPE.",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/RejectWorkEventRequest"}
                    }
                },
            }
        },
    )
    async def reject_work_event_route(
        id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match"),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> WorkEventDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        raw = await _parse_json_object(request)
        parsed: RejectWorkEventRequest = _validate_mutation_body(
            raw, RejectWorkEventRequest, error_code="INVALID_REJECT_REQUEST"
        )
        request_id = _resolve_request_id(request)
        detail = bound_mutation_service.reject_work_event(
            access,
            event_id=id,
            request=parsed,
            idempotency_key_header=idempotency_key,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = detail.etag
        return detail

    @router.post(
        "/v1/work-events/{id}/actions/split",
        status_code=200,
        response_model=SplitWorkEventResponse,
        operation_id="splitWorkEvent",
        summary="Atomically split a PR or unsplit DIRECT_COMMIT into disjoint scoped segments with pairwise CANNOT_LINK (T-304A)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "EVENT_VERSION_CONFLICT, SPLIT_NOT_ALLOWED, EVENT_NOT_ACTIVE, "
                    "DELETION_PENDING, or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "INVALID_SPLIT_SCOPE, SPLIT_SCOPE_OVERLAP, PROHIBITED_RAW_TEXT_PERSISTENCE, "
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
                        "schema": {"$ref": "#/components/schemas/SplitWorkEventRequest"}
                    }
                },
            }
        },
    )
    async def split_work_event_route(
        id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match"),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> SplitWorkEventResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        raw = await _parse_json_object(request)
        parsed: SplitWorkEventRequest = _validate_mutation_body(
            raw, SplitWorkEventRequest, error_code="INVALID_SPLIT_SCOPE"
        )
        request_id = _resolve_request_id(request)
        result = bound_mutation_service.split_work_event(
            access,
            event_id=id,
            request=parsed,
            idempotency_key_header=idempotency_key,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = result.superseded_parent.etag
        return result

    @router.post(
        "/v1/work-events/{id}/actions/dissolve",
        status_code=200,
        response_model=DissolveWorkEventResponse,
        operation_id="dissolveWorkEvent",
        summary="Dissolve an active USER_COMPOSITE, mark composite downstream STALE, and re-evaluate restored member statuses (T-304B)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "EVENT_VERSION_CONFLICT, NOT_ACTIVE_USER_COMPOSITE, "
                    "DELETION_PENDING, or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error, PROHIBITED_RAW_TEXT_PERSISTENCE, or PROHIBITED_PROJECT_ID_SCOPE.",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/DissolveWorkEventRequest"}
                    }
                },
            }
        },
    )
    async def dissolve_work_event_route(
        id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match"),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> DissolveWorkEventResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        raw = await _parse_json_object(request)
        parsed: DissolveWorkEventRequest = _validate_mutation_body(
            raw, DissolveWorkEventRequest, error_code="INVALID_DISSOLVE_REQUEST"
        )
        request_id = _resolve_request_id(request)
        result = bound_mutation_service.dissolve_work_event(
            access,
            event_id=id,
            request=parsed,
            idempotency_key_header=idempotency_key,
            if_match_header=if_match,
        )
        response.headers["Cache-Control"] = WORK_EVENTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        response.headers["ETag"] = result.dissolved_composite.etag
        return result

    return router


__all__ = [
    "InMemoryWorkEventsMutationService",
    "InMemoryWorkEventsReadService",
    "WorkEventsMutationPort",
    "WorkEventsReadPort",
    "create_work_events_router",
    "install_work_events_error_handlers",
]
