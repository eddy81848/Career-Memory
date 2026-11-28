"""Mountable FastAPI router and error handlers for M4 Wave 3A Exports API (/v1/exports*)."""

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
from career_memory_api.exports.models import (
    EXPORTS_NO_STORE_HEADER,
    CreateExportRequest,
    DeleteExportResponse,
    ExportDetailResponse,
    ExportError,
    ExportListResponse,
    assert_no_forbidden_export_write_keys,
)
from career_memory_api.sync_runs.models import parse_idempotency_key
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

AccessResolver = Callable[[Request], WorkspaceAccessContext]


class ExportsPort(Protocol):
    """Service port for /v1/exports* endpoints."""

    def list_exports(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str | None = None,
        include_deleted: bool = False,
    ) -> ExportListResponse: ...

    def create_export(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        request: CreateExportRequest,
    ) -> ExportDetailResponse: ...

    def get_export(
        self,
        access: WorkspaceAccessContext,
        *,
        export_id: str,
    ) -> ExportDetailResponse: ...

    def delete_export(
        self,
        access: WorkspaceAccessContext,
        *,
        export_id: str,
        idempotency_key: str,
    ) -> DeleteExportResponse: ...


class InMemoryExportsService:
    """Deterministic reference handler for OpenAPI generation & contract tests."""

    def list_exports(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str | None = None,
        include_deleted: bool = False,
    ) -> ExportListResponse:
        del access, source_resource_id, include_deleted
        return ExportListResponse(items=[])

    def create_export(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        request: CreateExportRequest,
    ) -> ExportDetailResponse:
        del access, idempotency_key, request
        raise WorkspaceResourceNotFoundError()

    def get_export(
        self,
        access: WorkspaceAccessContext,
        *,
        export_id: str,
    ) -> ExportDetailResponse:
        del access, export_id
        raise WorkspaceResourceNotFoundError()

    def delete_export(
        self,
        access: WorkspaceAccessContext,
        *,
        export_id: str,
        idempotency_key: str,
    ) -> DeleteExportResponse:
        del access, export_id, idempotency_key
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
        raise ExportError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise ExportError(
            status_code=422,
            code="INVALID_JSON_BODY",
            message="Request body must be a JSON object.",
        )
    return raw


def install_exports_error_handlers(app: FastAPI) -> None:
    """Install structured error handlers for /v1/exports* endpoints with private, no-store headers."""
    from career_memory_api.contracts_api.router import install_contract_error_handlers

    install_auth_error_handlers(app)
    install_contract_error_handlers(app)

    @app.exception_handler(ExportError)
    async def handle_export_error(request: Request, exc: ExportError) -> JSONResponse:
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
                "Cache-Control": EXPORTS_NO_STORE_HEADER,
                "X-Request-ID": request_id,
            },
        )


def create_exports_router(
    *,
    service: ExportsPort | None = None,
    resolve_access: AccessResolver | None = None,
) -> APIRouter:
    """Create the mountable M4 Wave 3A Exports APIRouter (/v1/exports*)."""
    bound_service: ExportsPort = InMemoryExportsService() if service is None else service
    access_resolver: AccessResolver = (
        _default_guard_access_resolver if resolve_access is None else resolve_access
    )

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return access_resolver(request)

    router = APIRouter(tags=["exports"])

    @router.get(
        "/v1/exports",
        status_code=200,
        response_model=ExportListResponse,
        operation_id="listWorkspaceExports",
        summary="List workspace exports with optional source_resource_id filter",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            422: {"model": ErrorEnvelope, "description": "Validation error or PROHIBITED_PROJECT_ID_SCOPE."},
        },
    )
    async def list_exports_route(
        request: Request,
        response: Response,
        source_resource_id: Annotated[
            str | None, Query(description="Optional SourceResource UUID filter.")
        ] = None,
        include_deleted: Annotated[
            bool, Query(description="Include DELETED exports (defaults to false).")
        ] = False,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ExportListResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.list_exports(
            access,
            source_resource_id=source_resource_id,
            include_deleted=include_deleted,
        )
        response.headers["Cache-Control"] = EXPORTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.post(
        "/v1/exports",
        status_code=201,
        response_model=ExportDetailResponse,
        operation_id="createWorkspaceExport",
        summary="Create a Plan C zero-quote Markdown or JSON export with live refetch and race fence",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "WORKSPACE_SCOPE_VIOLATION or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {
                "model": ErrorEnvelope,
                "description": (
                    "EXPORT_CLAIM_NOT_APPROVED, EXPORT_CLAIM_VERSION_MISMATCH, "
                    "EXPORT_POLICY_DISALLOWS_VERIFICATION_LEVEL, SUPPORT_INCOMPLETE, "
                    "EXPORT_INVALIDATED, SOURCE_REVISION_MISMATCH, DELETION_PENDING, "
                    "or IDEMPOTENCY_CONFLICT."
                ),
            },
            422: {"model": ErrorEnvelope, "description": "Validation error or forbidden write fields."},
            503: {"model": ErrorEnvelope, "description": "SOURCE_UNAVAILABLE or AUTH_VERIFICATION_UNAVAILABLE."},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/CreateExportRequest"}
                    }
                },
            }
        },
    )
    async def create_export_route(
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ExportDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise ExportError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        raw = await _parse_json_object(request)
        assert_no_forbidden_export_write_keys(raw)
        try:
            parsed = CreateExportRequest.model_validate(raw)
        except ValidationError as exc:
            raise ExportError(
                status_code=422,
                code="INVALID_CREATE_EXPORT_REQUEST",
                message="Export creation request failed schema validation.",
                details={"errors": exc.errors(include_context=False)},
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.create_export(
            access,
            idempotency_key=normalized_key,
            request=parsed,
        )
        response.headers["Cache-Control"] = EXPORTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.get(
        "/v1/exports/{export_id}",
        status_code=200,
        response_model=ExportDetailResponse,
        operation_id="getWorkspaceExport",
        summary="Fetch a READY export after live Plan C EXPORT_DOWNLOAD refetch re-verification",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "WORKSPACE_SCOPE_VIOLATION or SOURCE_POLICY_BLOCKED."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            409: {"model": ErrorEnvelope, "description": "EXPORT_INVALIDATED or SOURCE_REVISION_MISMATCH."},
            422: {"model": ErrorEnvelope, "description": "PROHIBITED_PROJECT_ID_SCOPE or SOURCE_FETCH_LIMIT."},
            503: {"model": ErrorEnvelope, "description": "SOURCE_UNAVAILABLE."},
        },
    )
    async def get_export_route(
        export_id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> ExportDetailResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        request_id = _resolve_request_id(request)
        result = bound_service.get_export(access, export_id=export_id)
        response.headers["Cache-Control"] = EXPORTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    @router.delete(
        "/v1/exports/{export_id}",
        status_code=200,
        response_model=DeleteExportResponse,
        operation_id="deleteWorkspaceExport",
        summary="Delete an export and clear stored artifact payload",
        responses={
            401: {"model": ErrorEnvelope, "description": "Unauthenticated or session terminated."},
            403: {"model": ErrorEnvelope, "description": "Workspace scope violation."},
            404: {"model": ErrorEnvelope, "description": "RESOURCE_NOT_FOUND."},
            422: {"model": ErrorEnvelope, "description": "INVALID_IDEMPOTENCY_KEY or PROHIBITED_PROJECT_ID_SCOPE."},
        },
    )
    async def delete_export_route(
        export_id: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", description="Required canonical UUID idempotency key."),
        ] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> DeleteExportResponse:
        assert_no_prohibited_project_id(dict(request.query_params))
        try:
            normalized_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise ExportError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        request_id = _resolve_request_id(request)
        result = bound_service.delete_export(
            access,
            export_id=export_id,
            idempotency_key=normalized_key,
        )
        response.headers["Cache-Control"] = EXPORTS_NO_STORE_HEADER
        response.headers["X-Request-ID"] = request_id
        return result

    return router


__all__ = [
    "AccessResolver",
    "ExportsPort",
    "InMemoryExportsService",
    "create_exports_router",
    "install_exports_error_handlers",
]
