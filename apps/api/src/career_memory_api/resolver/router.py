"""FastAPI router for ephemeral raw content viewing: GET /v1/content-units/{id}/content."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from career_memory_api.auth.http import (
    AUTH_NO_STORE_CACHE_CONTROL,
    build_auth_error_response,
    install_auth_error_handlers,
)
from career_memory_api.auth.redaction import redact_text
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthCredentials,
    AuthorizationBoundaryError,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceAccessGuard,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_request_id,
    get_workspace_access_guard,
)
from career_memory_api.resolver.models import (
    RefetchRequestInput,
    ResolutionMetadata,
    ResolutionStatus,
    map_resolution_status_to_error,
)
from career_memory_api.resolver.ports import RefetchResolverPort
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

logger = logging.getLogger(__name__)

_FORBIDDEN_URL_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "url",
        "source_url",
        "target_url",
        "endpoint",
        "endpoint_url",
        "uri",
        "href",
        "redirect",
        "redirect_url",
        "raw_url",
        "fetch_url",
    }
)


def _build_no_store_headers(request_id: str | None) -> dict[str, str]:
    headers = {"Cache-Control": AUTH_NO_STORE_CACHE_CONTROL}
    resolved_id = (request_id or "").strip()
    if resolved_id:
        headers["X-Request-ID"] = resolved_id
    return headers


def _build_resolution_error_response(
    *,
    metadata: ResolutionMetadata,
    request_id: str | None,
) -> JSONResponse:
    mapping = map_resolution_status_to_error(metadata)
    resolved_id = (request_id or "").strip()
    return JSONResponse(
        status_code=mapping.http_status,
        headers=_build_no_store_headers(resolved_id),
        content={
            "error": {
                "code": mapping.code,
                "message": redact_text(mapping.message),
                "retryable": mapping.retryable,
                "request_id": resolved_id,
            },
            "source_resolution": metadata.to_http_source_resolution_dict(),
        },
    )


def _handle_boundary_exception(
    exc: Exception, request_id: str | None
) -> JSONResponse:
    if isinstance(exc, SessionTerminatedError):
        return build_auth_error_response(
            status_code=401,
            code="SESSION_TERMINATED",
            message="The session has ended.",
            retryable=False,
            request_id=request_id,
        )
    if isinstance(exc, UnauthenticatedError):
        return build_auth_error_response(
            status_code=401,
            code="UNAUTHENTICATED",
            message="Authentication is required.",
            retryable=False,
            request_id=request_id,
        )
    if isinstance(exc, WorkspaceMembershipDeniedError):
        return build_auth_error_response(
            status_code=403,
            code="WORKSPACE_SCOPE_VIOLATION",
            message="Access to the selected workspace has been revoked.",
            retryable=False,
            request_id=request_id,
        )
    if isinstance(exc, AccessVerificationUnavailableError):
        return build_auth_error_response(
            status_code=503,
            code="AUTH_VERIFICATION_UNAVAILABLE",
            message="Authentication could not be verified temporarily.",
            retryable=True,
            request_id=request_id,
        )
    if isinstance(exc, AuthorizationBoundaryError):
        return build_auth_error_response(
            status_code=403,
            code="WORKSPACE_SCOPE_VIOLATION",
            message="Access denied.",
            retryable=False,
            request_id=request_id,
        )
    if isinstance(exc, WorkspaceResourceNotFoundError):
        return build_auth_error_response(
            status_code=404,
            code="RESOURCE_NOT_FOUND",
            message="The requested resource was not found.",
            retryable=False,
            request_id=request_id,
        )
    raise exc


def create_resolver_router(resolver: RefetchResolverPort) -> APIRouter:
    """Create the Option C ephemeral content-unit viewing router without mutating app.py."""
    router = APIRouter(tags=["content-units"])

    @router.get("/v1/content-units/{content_unit_id}/content")
    async def get_content_unit_content(
        content_unit_id: str,
        request: Request,
        guard: Annotated[WorkspaceAccessGuard, Depends(get_workspace_access_guard)],
        credentials: Annotated[AuthCredentials, Depends(get_auth_credentials)],
        request_id: Annotated[str | None, Depends(get_request_id)] = None,
        workspace_id: Annotated[str | None, Query()] = None,
        include_context: Annotated[bool, Query()] = True,
        x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
    ) -> JSONResponse:
        # Reject any user-supplied URL/endpoint query parameters immediately (REF-05)
        for param_key in request.query_params.keys():
            if param_key.strip().lower() in _FORBIDDEN_URL_QUERY_PARAMS:
                return build_auth_error_response(
                    status_code=400,
                    code="INVALID_REQUEST",
                    message="Arbitrary URL parameters are not accepted; only internal content_unit_id is permitted.",
                    retryable=False,
                    request_id=request_id,
                )

        # Resolve target workspace_id from query or X-Workspace-ID header (strictly target, NEVER authority)
        query_ws = workspace_id.strip() if isinstance(workspace_id, str) and workspace_id.strip() else None
        header_ws = x_workspace_id.strip() if isinstance(x_workspace_id, str) and x_workspace_id.strip() else None
        if query_ws and header_ws and query_ws != header_ws:
            return build_auth_error_response(
                status_code=403,
                code="WORKSPACE_SCOPE_VIOLATION",
                message="Conflicting target workspace identifiers were supplied.",
                retryable=False,
                request_id=request_id,
            )
        target_workspace_id = query_ws or header_ws or ""

        try:
            access = guard.require_workspace_access(
                credentials=credentials,
                target_workspace_id=target_workspace_id,
            )
        except AuthorizationBoundaryError as exc:
            return _handle_boundary_exception(exc, request_id)

        operation_id = (request_id or "").strip() or f"http-view-{uuid.uuid4()}"

        try:
            with resolver.open_operation_session(access, operation_id=operation_id) as session:
                with resolver.resolve_unit(
                    session,
                    RefetchRequestInput(
                        access=access,
                        content_unit_id=content_unit_id,
                        operation_id=operation_id,
                        include_context=include_context,
                    ),
                ) as resolution:
                    meta = resolution.metadata
                    # Log strictly non-raw metadata only (REF-04)
                    logger.info(
                        "content_unit_refetch_resolved status=%s operation_id=%s content_unit_id=%s deduped=%s",
                        meta.status.value,
                        meta.operation_id,
                        meta.content_unit_id,
                        meta.deduped,
                    )

                    if meta.status is not ResolutionStatus.MATCHED or resolution.ephemeral_unit is None:
                        return _build_resolution_error_response(
                            metadata=meta,
                            request_id=request_id,
                        )

                    context_payload: dict[str, Any] | None = (
                        resolution.ephemeral_context.to_ephemeral_http_dict()
                        if resolution.ephemeral_context is not None
                        else None
                    )
                    payload: dict[str, Any] = {
                        "content_unit_id": meta.content_unit_id,
                        "unit_type": resolution.ephemeral_unit.metadata.unit_type.value,
                        "content": resolution.ephemeral_unit.text,
                        "context": context_payload,
                        "source_resolution": meta.to_http_source_resolution_dict(),
                    }
                    return JSONResponse(
                        status_code=200,
                        headers=_build_no_store_headers(request_id),
                        content=payload,
                    )
        except (AuthorizationBoundaryError, WorkspaceResourceNotFoundError) as exc:
            return _handle_boundary_exception(exc, request_id)

    return router


def mount_resolver_routes(
    app: FastAPI,
    *,
    resolver: RefetchResolverPort,
    install_auth_handlers: bool = True,
) -> None:
    """Mount resolver routes (and optional auth error handlers) on a FastAPI app."""
    if install_auth_handlers:
        install_auth_error_handlers(app)
    app.include_router(create_resolver_router(resolver))
