"""Mountable FastAPI router and error handlers for M4 Wave 3A Claims API (/v1/claims*).

Preserves M1 contracts_router routes (POST /v1/claims/drafts, PATCH /v1/claims/{claim_id},
POST /v1/claims/{claim_id}/approve) without duplicate route registration.
"""

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
from career_memory_api.claims.models import (
    CLAIMS_NO_STORE_HEADER,
    ApproveClaimActionRequest,
    ClaimDetailResponse,
    ClaimError,
    ClaimGenerationStatus,
    ClaimListResponse,
    ClaimPageInfo,
    ClaimSupportsResponse,
    GenerateEventClaimsRequest,
    GenerateEventClaimsResponse,
    RejectClaimActionRequest,
    RevalidateClaimRequest,
    assert_no_forbidden_claim_write_keys,
)
from career_memory_api.contracts_api.models import (
    ErrorEnvelope,
    assert_no_prohibited_project_id,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_workspace_access_guard,
)
from career_memory_api.llm_gate.models import DEFAULT_FREE_ALTERNATIVES
from career_memory_api.schema_contracts.enums import ClaimStatus
from career_memory_api.sync_runs.models import parse_idempotency_key
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

AccessResolver = Callable[[Request], WorkspaceAccessContext]


class ClaimsPort(Protocol):
    """Service port for /v1/claims* read, generation, revalidation, approve, and reject routes."""

    def list_claims(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str | None = None,
        status: ClaimStatus | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ClaimListResponse: ...

    def generate_event_claims(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        request: GenerateEventClaimsRequest,
    ) -> GenerateEventClaimsResponse: ...

    def get_claim_detail(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
    ) -> ClaimDetailResponse: ...

    def get_claim_supports(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
    ) -> ClaimSupportsResponse: ...

    def revalidate_claim_version(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: RevalidateClaimRequest,
    ) -> ClaimDetailResponse: ...

    def approve_claim_action(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: ApproveClaimActionRequest,
    ) -> ClaimDetailResponse: ...

    def reject_claim_action(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: RejectClaimActionRequest,
    ) -> ClaimDetailResponse: ...


class InMemoryClaimsService:
    """Deterministic reference handler for OpenAPI generation & contract tests."""

    def list_claims(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str | None = None,
        status: ClaimStatus | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ClaimListResponse:
        del access, event_id, status, include_superseded, limit, cursor
        return ClaimListResponse(
            items=[],
            page=ClaimPageInfo(next_cursor=None, has_more=False),
        )

    def generate_event_claims(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        request: GenerateEventClaimsRequest,
    ) -> GenerateEventClaimsResponse:
        del access, idempotency_key
        return GenerateEventClaimsResponse(
            event_id=request.confirmed_event_id,
            generation_status=ClaimGenerationStatus.MANUAL_ONLY_BLOCKED,
            reason_code="LIVE_LLM_HARD_OFF",
            free_alternatives=[alt.value for alt in DEFAULT_FREE_ALTERNATIVES],
            claims=[],
        )

    def get_claim_detail(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
    ) -> ClaimDetailResponse:
        del access, claim_id
        raise WorkspaceResourceNotFoundError()

    def get_claim_supports(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
    ) -> ClaimSupportsResponse:
        del access, claim_id
        raise WorkspaceResourceNotFoundError()

    def revalidate_claim_version(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: RevalidateClaimRequest,
    ) -> ClaimDetailResponse:
        del access, claim_id, idempotency_key, request
        raise WorkspaceResourceNotFoundError()

    def approve_claim_action(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: ApproveClaimActionRequest,
    ) -> ClaimDetailResponse:
        del access, claim_id, idempotency_key, request
        raise WorkspaceResourceNotFoundError()

    def reject_claim_action(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: RejectClaimActionRequest,
    ) -> ClaimDetailResponse:
        del access, claim_id, idempotency_key, request
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
        raise ClaimError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise ClaimError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be a JSON object.",
        )
    return raw


def install_claims_error_handlers(app: FastAPI) -> None:
    """Install structured error handlers for /v1/claims* endpoints with private, no-store headers."""
    from career_memory_api.contracts_api.router import install_contract_error_handlers

    install_auth_error_handlers(app)
    install_contract_error_handlers(app)

    @app.exception_handler(ClaimError)
    async def handle_claim_error(request: Request, exc: ClaimError) -> JSONResponse:
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
                "Cache-Control": CLAIMS_NO_STORE_HEADER,
                "X-Request-ID": request_id,
            },
        )


def create_claims_router(
    *,
    service: ClaimsPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M4 Wave 3A Claims APIRouter."""
    bound_service: ClaimsPort = InMemoryClaimsService() if service is None else service
    access_resolver: AccessResolver = (
        _default_guard_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter(tags=["claims"])

    @router.get(
        "/v1/claims",
        status_code=200,
        response_model=ClaimListResponse,
        operation_id="listWorkspaceClaims",
        summary="List workspace claims with read-only computed verification and opaque pagination",
        responses={
            400: {"model": ErrorEnvelope, "description": "INVALID_CLAIMS_CURSOR."},
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error or PROHIBITED_PROJECT_ID_SCOPE (OD-008).",
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
    )
    async def list_claims_route(
        request: Request,
        response: Response,
        event_id: Annotated[str | None, Query(description="Optional WorkEvent UUID filter.")] = None,
        status: Annotated[ClaimStatus | None, Query(description="Optional ClaimStatus filter.")] = None,
        include_superseded: Annotated[
            bool, Query(description="Include SUPERSEDED claim versions (defaults to false).")
        ] = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[
            str | None, Query(description="Opaque workspace- and filter-bound cursor.")
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimListResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.list_claims(
            access,
            event_id=event_id,
            status=status,
            include_superseded=include_superseded,
            limit=limit,
            cursor=cursor,
        )
        response.headers["Cache-Control"] = CLAIMS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.post(
        "/v1/claims/generations",
        status_code=201,
        response_model=GenerateEventClaimsResponse,
        operation_id="generateEventClaims",
        summary="Run Stage 3A/3B/3C grounded claim pipeline for a CONFIRMED WorkEvent",
        responses={
            200: {"model": GenerateEventClaimsResponse, "description": "Blocked or DRAFT_INVALID outcome."},
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {"model": ErrorEnvelope, "description": "EVENT_NOT_CONFIRMED, DELETION_PENDING, or IDEMPOTENCY_CONFLICT."},
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error, PROHIBITED_READONLY_VERIFICATION_WRITE, PROHIBITED_RAW_TEXT_PERSISTENCE, or PROHIBITED_PROJECT_ID_SCOPE.",
            },
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/GenerateEventClaimsRequest"}
                    }
                },
            }
        },
    )
    async def generate_event_claims_route(
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> GenerateEventClaimsResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request)
        assert_no_forbidden_claim_write_keys(raw)
        try:
            parsed = GenerateEventClaimsRequest.model_validate(raw)
        except ValidationError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_CLAIM_GENERATION_REQUEST",
                message="Claim generation request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.generate_event_claims(
            access,
            idempotency_key=normalized_key,
            request=parsed,
        )
        if result.generation_status is not ClaimGenerationStatus.SUCCEEDED:
            response.status_code = 200
        response.headers["Cache-Control"] = CLAIMS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.get(
        "/v1/claims/{claim_id}",
        status_code=200,
        response_model=ClaimDetailResponse,
        operation_id="getClaimDetail",
        summary="Get a Claim version with atoms, read-only verification, and support_graph_epoch",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            422: {"model": ErrorEnvelope, "description": "PROHIBITED_PROJECT_ID_SCOPE."},
        },
    )
    async def get_claim_detail_route(
        claim_id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.get_claim_detail(access, claim_id=claim_id)
        response.headers["Cache-Control"] = CLAIMS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.get(
        "/v1/claims/{claim_id}/supports",
        status_code=200,
        response_model=ClaimSupportsResponse,
        operation_id="getClaimSupports",
        summary="Return full non-raw Claim -> Atom -> Fact -> FactSupport provenance chain",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            422: {"model": ErrorEnvelope, "description": "PROHIBITED_PROJECT_ID_SCOPE."},
        },
    )
    async def get_claim_supports_route(
        claim_id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimSupportsResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.get_claim_supports(access, claim_id=claim_id)
        response.headers["Cache-Control"] = CLAIMS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.post(
        "/v1/claims/{claim_id}/actions/revalidate",
        status_code=200,
        response_model=ClaimDetailResponse,
        operation_id="revalidateClaimVersion",
        summary="Run fresh Stage 3B atomization and Stage 3C support validation on a Claim version",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {"model": ErrorEnvelope, "description": "CLAIM_VERSION_CONFLICT, CLAIM_NOT_REVALIDATABLE, DELETION_PENDING, or IDEMPOTENCY_CONFLICT."},
            422: {"model": ErrorEnvelope, "description": "Validation error or forbidden write fields."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/RevalidateClaimRequest"}
                    }
                },
            }
        },
    )
    async def revalidate_claim_route(
        claim_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request)
        assert_no_forbidden_claim_write_keys(raw)
        try:
            parsed = RevalidateClaimRequest.model_validate(raw)
        except ValidationError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_REVALIDATE_CLAIM_REQUEST",
                message="Claim revalidate request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.revalidate_claim_version(
            access,
            claim_id=claim_id,
            idempotency_key=normalized_key,
            request=parsed,
        )
        response.headers["Cache-Control"] = CLAIMS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.post(
        "/v1/claims/{claim_id}/actions/approve",
        status_code=200,
        response_model=ClaimDetailResponse,
        operation_id="approveClaimAction",
        summary="Approve a SUPPORT_VALIDATED claim with live Plan C refetch and race-fenced epoch verification",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "WORKSPACE_SCOPE_VIOLATION or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {"model": ErrorEnvelope, "description": "CLAIM_VERSION_CONFLICT, SUPPORT_INCOMPLETE, DELETION_PENDING, SOURCE_REVISION_MISMATCH, or IDEMPOTENCY_CONFLICT."},
            422: {"model": ErrorEnvelope, "description": "Validation error or PROHIBITED_READONLY_VERIFICATION_WRITE."},
            503: {"model": ErrorEnvelope, "description": "SOURCE_UNAVAILABLE or AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ApproveClaimActionRequest"}
                    }
                },
            }
        },
    )
    async def approve_claim_action_route(
        claim_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request)
        assert_no_forbidden_claim_write_keys(raw)
        try:
            parsed = ApproveClaimActionRequest.model_validate(raw)
        except ValidationError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_APPROVE_CLAIM_REQUEST",
                message="Claim approval request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.approve_claim_action(
            access,
            claim_id=claim_id,
            idempotency_key=normalized_key,
            request=parsed,
        )
        response.headers["Cache-Control"] = CLAIMS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.post(
        "/v1/claims/{claim_id}/actions/reject",
        status_code=200,
        response_model=ClaimDetailResponse,
        operation_id="rejectClaimAction",
        summary="Reject a Claim version and atomically invalidate dependent exports",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {"model": ErrorEnvelope, "description": "CLAIM_VERSION_CONFLICT, CLAIM_NOT_REJECTABLE, DELETION_PENDING, or IDEMPOTENCY_CONFLICT."},
            422: {"model": ErrorEnvelope, "description": "Validation error or forbidden write fields."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/RejectClaimActionRequest"}
                    }
                },
            }
        },
    )
    async def reject_claim_action_route(
        claim_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request)
        assert_no_forbidden_claim_write_keys(raw)
        try:
            parsed = RejectClaimActionRequest.model_validate(raw)
        except ValidationError as exc:
            raise ClaimError(
                status_code=422,
                code="INVALID_REJECT_CLAIM_REQUEST",
                message="Claim rejection request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.reject_claim_action(
            access,
            claim_id=claim_id,
            idempotency_key=normalized_key,
            request=parsed,
        )
        response.headers["Cache-Control"] = CLAIMS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    return router


__all__ = [
    "AccessResolver",
    "ClaimsPort",
    "InMemoryClaimsService",
    "create_claims_router",
    "install_claims_error_handlers",
]
