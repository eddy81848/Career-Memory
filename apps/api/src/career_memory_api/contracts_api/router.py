"""FastAPI routers for M1 contract boundaries (Claims write guard, OD-008 Search, OD-003 Deletion).

Also exposes ``create_m1_contract_app`` to assemble the full integrated M1 route surface
(healthz + 4 initial-import routes + Plan C content-unit viewer + Claim write boundary +
OD-008 search + OD-003 deletion preview/confirm) for deterministic OpenAPI generation and
contract verification.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from career_memory_api.auth.http import install_auth_error_handlers
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceAccessProvider
from career_memory_api.config import Settings
from career_memory_api.contracts_api.models import (
    DEFAULT_SEARCH_INDEX_VERSION,
    DEFAULT_SEARCH_RANKING_VERSION,
    PRIVATE_NO_STORE_HEADER,
    ClaimApproveRequest,
    ClaimBoundaryResponse,
    ClaimDraftCreateRequest,
    ClaimEditRequest,
    ContractBoundaryError,
    DeletionConfirmAcceptedResponse,
    DeletionConfirmRequest,
    DeletionImpactCounts,
    DeletionPreviewResponse,
    DeletionPreviewTokenPayload,
    ErrorEnvelope,
    NonRecoverableScopes,
    SearchCursorPayload,
    SearchEntityType,
    SearchResponse,
    SearchScope,
    assert_no_plan_c_raw_text_keys,
    assert_no_prohibited_project_id,
    assert_no_readonly_verification_write,
    compute_query_hash,
    decode_and_verify_deletion_preview_token,
    decode_and_verify_search_cursor,
    encode_deletion_preview_token,
    encode_search_cursor,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_request_id,
    get_workspace_access_guard,
)
from career_memory_api.schema_contracts.enums import ClaimStatus, ClaimVerification
from career_memory_api.sync_runs.models import parse_idempotency_key

AccessResolver = Callable[[Request], WorkspaceAccessContext]


class ContractBoundaryPort(Protocol):
    """Backend service port for M1 contract boundary endpoints."""

    def create_claim_draft(
        self, access: WorkspaceAccessContext, request: ClaimDraftCreateRequest
    ) -> ClaimBoundaryResponse: ...

    def edit_claim(
        self, access: WorkspaceAccessContext, claim_id: str, request: ClaimEditRequest
    ) -> ClaimBoundaryResponse: ...

    def approve_claim(
        self, access: WorkspaceAccessContext, claim_id: str, request: ClaimApproveRequest
    ) -> ClaimBoundaryResponse: ...

    def search(
        self,
        access: WorkspaceAccessContext,
        *,
        q: str,
        source_resource_id: str | None,
        entity_types: list[SearchEntityType] | None,
        limit: int,
        cursor: str | None,
    ) -> SearchResponse: ...

    def preview_source_deletion(
        self, access: WorkspaceAccessContext, *, source_resource_id: str
    ) -> DeletionPreviewResponse: ...

    def confirm_source_deletion(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        idempotency_key: str,
        request: DeletionConfirmRequest,
    ) -> DeletionConfirmAcceptedResponse: ...


class InMemoryContractBoundaryService:
    """Deterministic contract-boundary reference handler for M1 contract testing.

    Actual FTS execution (M5 T-501), deletion engine (M5 T-502), and Claim pipeline (M4)
    are implemented in later milestones; this reference service enforces every M1 wire
    invariant (read-only verification guard, OD-008 cursor & no-project_id, OD-003
    version/expiry-bound preview_token & idempotency).
    """

    def __init__(
        self,
        *,
        now_fn: Callable[[], datetime] | None = None,
        preview_ttl_seconds: int = 300,
    ) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._preview_ttl_seconds = preview_ttl_seconds
        self._resource_versions: dict[tuple[str, str], tuple[str, str]] = {}
        self._confirmed_keys: dict[tuple[str, str, str], DeletionConfirmAcceptedResponse] = {}
        self.index_version = DEFAULT_SEARCH_INDEX_VERSION
        self.ranking_version = DEFAULT_SEARCH_RANKING_VERSION

    def set_resource_scope_state(
        self,
        workspace_id: str,
        source_resource_id: str,
        *,
        resource_version: str,
        impact_scope_version: str,
    ) -> None:
        self._resource_versions[(workspace_id, source_resource_id)] = (
            resource_version,
            impact_scope_version,
        )

    def _current_resource_state(
        self, workspace_id: str, source_resource_id: str
    ) -> tuple[str, str]:
        return self._resource_versions.get(
            (workspace_id, source_resource_id), ("res-v1", "scope-v1")
        )

    def create_claim_draft(
        self, access: WorkspaceAccessContext, request: ClaimDraftCreateRequest
    ) -> ClaimBoundaryResponse:
        return ClaimBoundaryResponse(
            claim_id=str(uuid.uuid4()),
            workspace_id=access.workspace_id,
            event_id=request.confirmed_event_id,
            version=1,
            claim_text=request.claim_text,
            status=ClaimStatus.DRAFT,
            computed_verification=ClaimVerification.UNVERIFIED,
        )

    def edit_claim(
        self, access: WorkspaceAccessContext, claim_id: str, request: ClaimEditRequest
    ) -> ClaimBoundaryResponse:
        return ClaimBoundaryResponse(
            claim_id=claim_id,
            workspace_id=access.workspace_id,
            event_id="evt-bound",
            version=request.expected_version + 1,
            claim_text=request.claim_text,
            status=ClaimStatus.DRAFT,
            computed_verification=ClaimVerification.UNVERIFIED,
        )

    def approve_claim(
        self, access: WorkspaceAccessContext, claim_id: str, request: ClaimApproveRequest
    ) -> ClaimBoundaryResponse:
        return ClaimBoundaryResponse(
            claim_id=claim_id,
            workspace_id=access.workspace_id,
            event_id="evt-bound",
            version=request.expected_version,
            claim_text="approved-claim-boundary",
            status=ClaimStatus.APPROVED,
            computed_verification=ClaimVerification.UNVERIFIED,
        )

    def search(
        self,
        access: WorkspaceAccessContext,
        *,
        q: str,
        source_resource_id: str | None,
        entity_types: list[SearchEntityType] | None,
        limit: int,
        cursor: str | None,
    ) -> SearchResponse:
        query_hash = compute_query_hash(q, entity_types)
        offset = 0
        if cursor is not None:
            decoded = decode_and_verify_search_cursor(
                cursor,
                expected_workspace_id=access.workspace_id,
                expected_source_resource_id=source_resource_id,
                expected_query_hash=query_hash,
                expected_index_version=self.index_version,
                expected_ranking_version=self.ranking_version,
            )
            offset = decoded.offset
        next_cursor = encode_search_cursor(
            SearchCursorPayload(
                workspace_id=access.workspace_id,
                source_resource_id=source_resource_id,
                query_hash=query_hash,
                index_version=self.index_version,
                ranking_version=self.ranking_version,
                offset=offset + limit,
            )
        )
        return SearchResponse(
            scope=SearchScope(
                workspace_id=access.workspace_id,
                source_resource_id=source_resource_id,
            ),
            query_hash=query_hash,
            index_version=self.index_version,
            ranking_version=self.ranking_version,
            items=[],
            next_cursor=next_cursor,
        )

    def preview_source_deletion(
        self, access: WorkspaceAccessContext, *, source_resource_id: str
    ) -> DeletionPreviewResponse:
        resource_version, impact_scope_version = self._current_resource_state(
            access.workspace_id, source_resource_id
        )
        expires_at = self._now_fn() + timedelta(seconds=self._preview_ttl_seconds)
        token = encode_deletion_preview_token(
            DeletionPreviewTokenPayload(
                workspace_id=access.workspace_id,
                source_resource_id=source_resource_id,
                resource_version=resource_version,
                impact_scope_version=impact_scope_version,
                expires_at=expires_at,
            )
        )
        return DeletionPreviewResponse(
            source_resource_id=source_resource_id,
            resource_version=resource_version,
            impact_scope_version=impact_scope_version,
            impact_counts=DeletionImpactCounts(
                work_events=0,
                facts=0,
                contributions=0,
                claims_draft=0,
                claims_edited=0,
                claims_approved=0,
                claims_historical_versions=0,
                search_index_entries=0,
                ephemeral_cache_entries=0,
                server_exports=0,
            ),
            non_recoverable_scopes=NonRecoverableScopes(),
            preview_token=token,
            expires_at=expires_at,
        )

    def confirm_source_deletion(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        idempotency_key: str,
        request: DeletionConfirmRequest,
    ) -> DeletionConfirmAcceptedResponse:
        cache_key = (access.workspace_id, source_resource_id, idempotency_key)
        if cache_key in self._confirmed_keys:
            return self._confirmed_keys[cache_key]

        resource_version, impact_scope_version = self._current_resource_state(
            access.workspace_id, source_resource_id
        )
        if (
            request.expected_resource_version != resource_version
            or request.expected_impact_scope_version != impact_scope_version
        ):
            raise ContractBoundaryError(
                status_code=409,
                code="DELETION_PREVIEW_SCOPE_CHANGED",
                message=(
                    "Expected resource_version or impact_scope_version does not match current state."
                ),
            )
        decode_and_verify_deletion_preview_token(
            request.preview_token,
            expected_workspace_id=access.workspace_id,
            expected_source_resource_id=source_resource_id,
            current_resource_version=resource_version,
            current_impact_scope_version=impact_scope_version,
            now=self._now_fn(),
        )
        accepted = DeletionConfirmAcceptedResponse(
            deletion_job_id=str(uuid.uuid4()),
            source_resource_id=source_resource_id,
        )
        self._confirmed_keys[cache_key] = accepted
        return accepted


def install_contract_error_handlers(app: FastAPI) -> None:
    """Install structured error envelope handlers for M1 contract boundary errors."""

    @app.exception_handler(ContractBoundaryError)
    async def handle_contract_boundary_error(
        request: Request, exc: ContractBoundaryError
    ) -> JSONResponse:
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


def _default_guard_access_resolver(request: Request) -> WorkspaceAccessContext:
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


async def _parse_json_object(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except ValueError as exc:
        raise ContractBoundaryError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise ContractBoundaryError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be a JSON object.",
        )
    return raw


def create_contracts_router(
    *,
    service: ContractBoundaryPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M1 contract-boundary APIRouter."""
    bound_service: ContractBoundaryPort = (
        InMemoryContractBoundaryService() if service is None else service
    )
    access_resolver: AccessResolver = (
        _default_guard_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter()

    # -------------------------------------------------------------------------
    # Claim Write Boundary Routes (Read-Only Verification Enforced)
    # -------------------------------------------------------------------------
    @router.post(
        "/v1/claims/drafts",
        status_code=201,
        response_model=ClaimBoundaryResponse,
        tags=["claims"],
        operation_id="createClaimDraft",
        summary="Create a DRAFT claim without writable verification fields",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "Validation error, PROHIBITED_READONLY_VERIFICATION_WRITE, "
                    "or PROHIBITED_RAW_TEXT_PERSISTENCE."
                ),
            },
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ClaimDraftCreateRequest"}
                    }
                },
            }
        },
    )
    async def create_claim_draft_route(
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimBoundaryResponse:
        raw = await _parse_json_object(request)
        assert_no_prohibited_project_id(raw)
        assert_no_readonly_verification_write(raw)
        assert_no_plan_c_raw_text_keys(raw)
        try:
            parsed = ClaimDraftCreateRequest.model_validate(raw)
        except ValidationError as exc:
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_CLAIM_DRAFT_REQUEST",
                message="Claim draft request failed schema validation.",
                details={"errors": exc.errors()},
            ) from exc
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.create_claim_draft(access, parsed)

    @router.patch(
        "/v1/claims/{claim_id}",
        status_code=200,
        response_model=ClaimBoundaryResponse,
        tags=["claims"],
        operation_id="editClaimVersion",
        summary="Edit a claim into a new version without writable verification fields",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "Claim not found or concealed."},
            409: {"model": ErrorEnvelope, "description": "Version conflict."},
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "Validation error, PROHIBITED_READONLY_VERIFICATION_WRITE, "
                    "or PROHIBITED_RAW_TEXT_PERSISTENCE."
                ),
            },
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ClaimEditRequest"}
                    }
                },
            }
        },
    )
    async def edit_claim_route(
        claim_id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimBoundaryResponse:
        raw = await _parse_json_object(request)
        assert_no_prohibited_project_id(raw)
        assert_no_readonly_verification_write(raw)
        assert_no_plan_c_raw_text_keys(raw)
        try:
            parsed = ClaimEditRequest.model_validate(raw)
        except ValidationError as exc:
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_CLAIM_EDIT_REQUEST",
                message="Claim edit request failed schema validation.",
                details={"errors": exc.errors()},
            ) from exc
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.edit_claim(access, claim_id, parsed)

    @router.post(
        "/v1/claims/{claim_id}/approve",
        status_code=200,
        response_model=ClaimBoundaryResponse,
        tags=["claims"],
        operation_id="approveClaimVersion",
        summary="Approve a claim version without direct SOURCE_VERIFIED writes",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "Claim not found or concealed."},
            409: {"model": ErrorEnvelope, "description": "Version conflict or refetch verification block."},
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error or PROHIBITED_READONLY_VERIFICATION_WRITE.",
            },
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ClaimApproveRequest"}
                    }
                },
            }
        },
    )
    async def approve_claim_route(
        claim_id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ClaimBoundaryResponse:
        raw = await _parse_json_object(request)
        assert_no_prohibited_project_id(raw)
        assert_no_readonly_verification_write(raw)
        assert_no_plan_c_raw_text_keys(raw)
        try:
            parsed = ClaimApproveRequest.model_validate(raw)
        except ValidationError as exc:
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_CLAIM_APPROVE_REQUEST",
                message="Claim approval request failed schema validation.",
                details={"errors": exc.errors()},
            ) from exc
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.approve_claim(access, claim_id, parsed)

    # -------------------------------------------------------------------------
    # OD-008 Search Contract (Workspace + optional source_resource_id, NO project_id)
    # -------------------------------------------------------------------------
    @router.get(
        "/v1/search",
        status_code=200,
        response_model=SearchResponse,
        tags=["search"],
        operation_id="searchWorkspaceMemory",
        summary="Search non-raw workspace entities scoped by workspace and optional source_resource_id (OD-008)",
        responses={
            400: {"model": ErrorEnvelope, "description": "INVALID_SEARCH_CURSOR."},
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            422: {
                "model": ErrorEnvelope,
                "description": "Validation error or PROHIBITED_PROJECT_ID_SCOPE (OD-008).",
            },
        },
    )
    async def search_route(
        request: Request,
        response: Response,
        q: Annotated[str, Query(min_length=1, description="Search query text.")],
        source_resource_id: Annotated[
            str | None,
            Query(description="Optional source resource UUID within the authenticated Workspace."),
        ] = None,
        entity_types: Annotated[
            list[SearchEntityType] | None,
            Query(description="Optional filter over WORK_EVENT, FACT, or CLAIM."),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[
            str | None,
            Query(
                description=(
                    "Versioned opaque cursor bound to (workspace_id, source_resource_id, "
                    "query_hash, index_version, ranking_version)."
                )
            ),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> SearchResponse:
        # OD-008: Explicitly reject any project_id query parameter regardless of case
        assert_no_prohibited_project_id(dict(request.query_params))
        if not q.strip():
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_SEARCH_QUERY",
                message="Query parameter 'q' must not be blank.",
            )
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.search(
            access,
            q=q,
            source_resource_id=source_resource_id,
            entity_types=entity_types,
            limit=limit,
            cursor=cursor,
        )

    # -------------------------------------------------------------------------
    # OD-003 Source Deletion Preview & Confirm Contract
    # -------------------------------------------------------------------------
    @router.post(
        "/v1/source-resources/{source_resource_id}/deletion-preview",
        status_code=200,
        response_model=DeletionPreviewResponse,
        tags=["source-resources"],
        operation_id="previewSourceResourceDeletion",
        summary="Preview cascade deletion impact and issue a version-bound preview_token (OD-003)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "Source resource not found or concealed."},
        },
    )
    async def preview_source_deletion_route(
        source_resource_id: str,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> DeletionPreviewResponse:
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.preview_source_deletion(
            access, source_resource_id=source_resource_id
        )

    @router.post(
        "/v1/source-resources/{source_resource_id}/deletion-confirm",
        status_code=202,
        response_model=DeletionConfirmAcceptedResponse,
        tags=["source-resources"],
        operation_id="confirmSourceResourceDeletion",
        summary="Confirm irreversible source deletion with Idempotency-Key and matching preview_token (OD-003)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "Source resource not found or concealed."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "DELETION_PREVIEW_EXPIRED, DELETION_PREVIEW_SCOPE_CHANGED, "
                    "or DELETION_PREVIEW_INVALID."
                ),
            },
            422: {"model": ErrorEnvelope, "description": "Missing/invalid Idempotency-Key or body."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/DeletionConfirmRequest"}
                    }
                },
            }
        },
    )
    async def confirm_source_deletion_route(
        source_resource_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                description="Required canonical UUID idempotency key for deletion confirmation.",
            ),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> DeletionConfirmAcceptedResponse:
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request)
        assert_no_prohibited_project_id(raw)
        assert_no_plan_c_raw_text_keys(raw)
        try:
            parsed = DeletionConfirmRequest.model_validate(raw)
        except ValidationError as exc:
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_DELETION_CONFIRM_REQUEST",
                message="Deletion confirmation request failed schema validation.",
                details={"errors": exc.errors()},
            ) from exc

        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return bound_service.confirm_source_deletion(
            access,
            source_resource_id=source_resource_id,
            idempotency_key=normalized_key,
            request=parsed,
        )

    return router


def create_m1_contract_app(
    *,
    settings: Settings | None = None,
    access_provider: WorkspaceAccessProvider | None = None,
    contracts_service: ContractBoundaryPort | None = None,
    work_events_service: Any | None = None,
    work_events_mutation_service: Any | None = None,
    resolve_access: AccessResolver | None = None,
) -> FastAPI:
    """Create a FastAPI app exposing the full integrated contract surface (no BYOK / LLM callbacks)."""
    from career_memory_api.app import create_app
    from career_memory_api.work_events.router import create_work_events_router

    contracts_router = create_contracts_router(
        service=contracts_service,
        resolve_access=resolve_access,
    )
    work_events_router = create_work_events_router(
        service=work_events_service,
        mutation_service=work_events_mutation_service,
        resolve_access=resolve_access,
    )
    app = create_app(
        settings=settings,
        access_provider=access_provider,
        contracts_router=contracts_router,
        work_events_router=work_events_router,
        install_contract_handlers=True,
        install_work_events_handlers=True,
    )
    install_auth_error_handlers(app)
    return app


__all__ = [
    "ContractBoundaryPort",
    "InMemoryContractBoundaryService",
    "create_contracts_router",
    "create_m1_contract_app",
    "install_contract_error_handlers",
]
