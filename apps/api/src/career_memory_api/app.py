"""FastAPI application factory for the Career Memory local scaffold."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from career_memory_api.authz import (
    AccessDenialReason,
    AuthorizationBoundaryError,
    UnconfiguredWorkspaceAccessProvider,
    WorkspaceAccessGuard,
    WorkspaceAccessProvider,
)
from career_memory_api.config import Settings

# Provisional non-2xx HTTP status mapping for scaffold boundary verification only.
# Final product HTTP status codes (e.g. 403 vs 404 for cross-workspace denial,
# 503 vs 500 for verification outage) are intentionally deferred to T-101.
_PROVISIONAL_DENIAL_STATUS_CODES: dict[AccessDenialReason, int] = {
    AccessDenialReason.UNAUTHENTICATED: 401,
    AccessDenialReason.MEMBERSHIP_NOT_FOUND: 403,
    AccessDenialReason.VERIFICATION_UNAVAILABLE: 503,
}


def create_app(
    settings: Settings | None = None,
    *,
    access_provider: WorkspaceAccessProvider | None = None,
    sync_runs_router: APIRouter | None = None,
    resolver_router: APIRouter | None = None,
    contracts_router: APIRouter | None = None,
    github_connect_router: APIRouter | None = None,
    install_contract_handlers: bool = False,
    install_github_connect_handlers: bool = False,
) -> FastAPI:
    """Create a FastAPI app instance.

    In normal execution ``access_provider`` is omitted and defaults to the
    fail-closed ``UnconfiguredWorkspaceAccessProvider``. No environment flag
    or request header can activate a synthetic provider.

    Optional M1/M2 domain routers (``sync_runs_router``, ``resolver_router``,
    ``contracts_router``, ``github_connect_router``) may be mounted cleanly
    without changing default scaffold behavior.
    """
    resolved_settings = Settings.from_env() if settings is None else settings
    resolved_provider: WorkspaceAccessProvider = (
        UnconfiguredWorkspaceAccessProvider()
        if access_provider is None
        else access_provider
    )

    app = FastAPI(title=resolved_settings.app_name)
    app.state.settings = resolved_settings
    app.state.workspace_access_guard = WorkspaceAccessGuard(resolved_provider)

    @app.exception_handler(AuthorizationBoundaryError)
    async def handle_authorization_boundary_error(
        _request: Request, exc: AuthorizationBoundaryError
    ) -> JSONResponse:
        status_code = _PROVISIONAL_DENIAL_STATUS_CODES.get(exc.reason, 403)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": "access_denied",
                "boundary_reason": exc.reason.value,
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if install_contract_handlers:
        from career_memory_api.contracts_api.router import install_contract_error_handlers

        install_contract_error_handlers(app)

    if install_github_connect_handlers:
        from career_memory_api.github_connect.router import (
            install_github_connect_error_handlers,
        )

        install_github_connect_error_handlers(app)

    if sync_runs_router is not None:
        app.include_router(sync_runs_router)
    if resolver_router is not None:
        app.include_router(resolver_router)
    if contracts_router is not None:
        app.include_router(contracts_router)
    if github_connect_router is not None:
        app.include_router(github_connect_router)

    return app
