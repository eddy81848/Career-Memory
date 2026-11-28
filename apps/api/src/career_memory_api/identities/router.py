"""Mountable FastAPI router and error handlers for M4 GitHub Identities API (T-401, T-401A)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
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
from career_memory_api.identities.models import (
    IDENTITIES_NO_STORE_HEADER,
    ConfirmIdentityMappingRequest,
    ConfirmIdentityMappingResponse,
    IdentityError,
    IdentityListResponse,
    RevokeIdentityMappingRequest,
    RevokeIdentityMappingResponse,
)
from career_memory_api.sync_runs.models import parse_idempotency_key
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

AccessResolver = Callable[[Request], WorkspaceAccessContext]


class IdentitiesPort(Protocol):
    """Service port for M4 GitHub identity mapping endpoints."""

    def list_identities(self, access: WorkspaceAccessContext) -> IdentityListResponse: ...

    def confirm_identity_mapping(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        request: ConfirmIdentityMappingRequest,
    ) -> ConfirmIdentityMappingResponse: ...

    def revoke_identity_mapping(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        request: RevokeIdentityMappingRequest,
    ) -> RevokeIdentityMappingResponse: ...


class InMemoryIdentitiesService:
    """Deterministic reference handler for contract app & OpenAPI generation."""

    def list_identities(self, access: WorkspaceAccessContext) -> IdentityListResponse:
        del access
        return IdentityListResponse(items=[])

    def confirm_identity_mapping(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        request: ConfirmIdentityMappingRequest,
    ) -> ConfirmIdentityMappingResponse:
        del access, mapping_id, idempotency_key, request
        raise WorkspaceResourceNotFoundError()

    def revoke_identity_mapping(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        request: RevokeIdentityMappingRequest,
    ) -> RevokeIdentityMappingResponse:
        del access, mapping_id, idempotency_key, request
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
        raise IdentityError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise IdentityError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be a JSON object.",
        )
    return raw


def install_identities_error_handlers(app: FastAPI) -> None:
    """Install structured error handlers for Identity endpoints with private, no-store headers."""
    from career_memory_api.contracts_api.router import install_contract_error_handlers

    install_auth_error_handlers(app)
    install_contract_error_handlers(app)

    @app.exception_handler(IdentityError)
    async def handle_identity_error(request: Request, exc: IdentityError) -> JSONResponse:
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
                "Cache-Control": IDENTITIES_NO_STORE_HEADER,
                "X-Request-ID": request_id,
            },
        )


def create_identities_router(
    *,
    service: IdentitiesPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M4 Identities APIRouter (T-401 / T-401A)."""
    bound_service: IdentitiesPort = (
        InMemoryIdentitiesService() if service is None else service
    )
    access_resolver: AccessResolver = (
        _default_guard_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter(tags=["identities"])

    @router.get(
        "/v1/identities",
        status_code=200,
        response_model=IdentityListResponse,
        operation_id="listWorkspaceIdentities",
        summary="List workspace identities, redacted mappings (email_hash only), and attribution eligibility",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            422: {"model": ErrorEnvelope, "description": "PROHIBITED_PROJECT_ID_SCOPE (OD-008)."},
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
    )
    async def list_identities_route(
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> IdentityListResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.list_identities(access)
        response.headers["Cache-Control"] = IDENTITIES_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.post(
        "/v1/identities/mappings/{mapping_id}/actions/confirm",
        status_code=200,
        response_model=ConfirmIdentityMappingResponse,
        operation_id="confirmIdentityMapping",
        summary="Promote an eligible HEURISTIC PERSON mapping to USER_CONFIRMED (never VERIFIED)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND (missing or cross-workspace)."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "MAPPING_CONFLICTED, MAPPING_REVOKED, MAPPING_ALREADY_VERIFIED, "
                    "MAPPING_ALREADY_CONFIRMED, DELETION_PENDING, or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "INVALID_IDEMPOTENCY_KEY, BOT_IDENTITY_CONFIRMATION_FORBIDDEN, "
                    "PROHIBITED_RAW_TEXT_PERSISTENCE, or PROHIBITED_PROJECT_ID_SCOPE."
                ),
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": False,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ConfirmIdentityMappingRequest"}
                    }
                },
            }
        },
    )
    async def confirm_identity_mapping_route(
        mapping_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                description="Required canonical UUID idempotency key.",
            ),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ConfirmIdentityMappingResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise IdentityError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request, allow_empty=True)
        assert_no_prohibited_project_id(raw)
        assert_no_plan_c_raw_text_keys(raw)
        try:
            parsed = ConfirmIdentityMappingRequest.model_validate(raw)
        except ValidationError as exc:
            raise IdentityError(
                status_code=422,
                code="INVALID_CONFIRM_IDENTITY_REQUEST",
                message="Identity confirmation request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.confirm_identity_mapping(
            access,
            mapping_id=mapping_id,
            idempotency_key=normalized_key,
            request=parsed,
        )
        response.headers["Cache-Control"] = IDENTITIES_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.post(
        "/v1/identities/mappings/{mapping_id}/actions/revoke",
        status_code=200,
        response_model=RevokeIdentityMappingResponse,
        operation_id="revokeIdentityMapping",
        summary="Atomically revoke an identity mapping and cascade STALE/INVALIDATED downstream (ID-05)",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND (missing or cross-workspace)."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "MAPPING_ALREADY_REVOKED, DELETION_PENDING, or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {
                "model": ErrorEnvelope,
                "description": (
                    "INVALID_IDEMPOTENCY_KEY, INVALID_REVOKE_IDENTITY_REQUEST, "
                    "PROHIBITED_RAW_TEXT_PERSISTENCE, or PROHIBITED_PROJECT_ID_SCOPE."
                ),
            },
            503: {"model": ErrorEnvelope, "description": "AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/RevokeIdentityMappingRequest"}
                    }
                },
            }
        },
    )
    async def revoke_identity_mapping_route(
        mapping_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                description="Required canonical UUID idempotency key.",
            ),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> RevokeIdentityMappingResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise IdentityError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request, allow_empty=False)
        assert_no_prohibited_project_id(raw)
        assert_no_plan_c_raw_text_keys(raw)
        try:
            parsed = RevokeIdentityMappingRequest.model_validate(raw)
        except ValidationError as exc:
            raise IdentityError(
                status_code=422,
                code="INVALID_REVOKE_IDENTITY_REQUEST",
                message="Identity revocation request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.revoke_identity_mapping(
            access,
            mapping_id=mapping_id,
            idempotency_key=normalized_key,
            request=parsed,
        )
        response.headers["Cache-Control"] = IDENTITIES_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    return router


__all__ = [
    "AccessResolver",
    "IdentitiesPort",
    "InMemoryIdentitiesService",
    "create_identities_router",
    "install_identities_error_handlers",
]
