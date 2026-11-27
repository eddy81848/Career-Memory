"""HTTP auth/consent/permission error envelopes enforcing Cache-Control: private, no-store."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from career_memory_api.auth.oauth import (
    OAuthBoundaryError,
    OAuthPersistenceFailedError,
    OAuthStateInvalidOrReplayedError,
)
from career_memory_api.auth.redaction import redact_text
from career_memory_api.auth.return_path import InvalidReturnPathError
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthorizationBoundaryError,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

AUTH_NO_STORE_CACHE_CONTROL = "private, no-store"


def build_auth_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    request_id: str | None = None,
) -> JSONResponse:
    """Build a redacted auth/permission JSON error response with private, no-store headers."""
    headers = {"Cache-Control": AUTH_NO_STORE_CACHE_CONTROL}
    resolved_request_id = (request_id or "").strip()
    if resolved_request_id:
        headers["X-Request-ID"] = resolved_request_id

    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": redact_text(message),
                "retryable": retryable,
                "request_id": resolved_request_id,
            }
        },
    )


def install_auth_error_handlers(app: FastAPI) -> None:
    """Attach contract-aligned auth, concealment, and OAuth error handlers to a FastAPI app."""

    @app.exception_handler(SessionTerminatedError)
    async def handle_session_terminated(
        request: Request, _exc: SessionTerminatedError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=401,
            code="SESSION_TERMINATED",
            message="The session has ended.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(UnauthenticatedError)
    async def handle_unauthenticated(
        request: Request, _exc: UnauthenticatedError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=401,
            code="UNAUTHENTICATED",
            message="Authentication is required.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(WorkspaceMembershipDeniedError)
    async def handle_workspace_denied(
        request: Request, _exc: WorkspaceMembershipDeniedError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=403,
            code="WORKSPACE_SCOPE_VIOLATION",
            message="Access to the selected workspace has been revoked.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(AccessVerificationUnavailableError)
    async def handle_verification_unavailable(
        request: Request, _exc: AccessVerificationUnavailableError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=503,
            code="AUTH_VERIFICATION_UNAVAILABLE",
            message="Authentication could not be verified temporarily.",
            retryable=True,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(AuthorizationBoundaryError)
    async def handle_generic_authz(
        request: Request, _exc: AuthorizationBoundaryError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=403,
            code="WORKSPACE_SCOPE_VIOLATION",
            message="Access denied.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(WorkspaceResourceNotFoundError)
    async def handle_resource_concealed(
        request: Request, _exc: WorkspaceResourceNotFoundError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=404,
            code="RESOURCE_NOT_FOUND",
            message="The requested resource was not found.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(InvalidReturnPathError)
    async def handle_invalid_return_path(
        request: Request, _exc: InvalidReturnPathError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=400,
            code="INVALID_RETURN_PATH",
            message="The requested return path is not allowed.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(OAuthStateInvalidOrReplayedError)
    async def handle_oauth_state_invalid(
        request: Request, _exc: OAuthStateInvalidOrReplayedError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=400,
            code="OAUTH_STATE_INVALID",
            message="OAuth state is invalid, expired, or already used.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(OAuthPersistenceFailedError)
    async def handle_oauth_persistence_failed(
        request: Request, _exc: OAuthPersistenceFailedError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=503,
            code="OAUTH_CONNECTION_PERSISTENCE_FAILED",
            message="Source connection could not be saved.",
            retryable=True,
            request_id=request.headers.get("X-Request-ID"),
        )

    @app.exception_handler(OAuthBoundaryError)
    async def handle_oauth_boundary(
        request: Request, _exc: OAuthBoundaryError
    ) -> JSONResponse:
        return build_auth_error_response(
            status_code=400,
            code="OAUTH_CALLBACK_FAILED",
            message="External source authorization could not be completed.",
            retryable=False,
            request_id=request.headers.get("X-Request-ID"),
        )
