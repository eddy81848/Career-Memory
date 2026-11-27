"""Mountable FastAPI router and error handlers for M2-T201 GitHub Connect (T-201 + T-201A)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from career_memory_api.auth.http import install_auth_error_handlers
from career_memory_api.auth.oauth import OAuthTokenExchangeError
from career_memory_api.auth.redaction import redact_text
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.github_connect.models import (
    GITHUB_CONNECT_NO_STORE_HEADER,
    GitHubConnectError,
)
from career_memory_api.github_connect.postgres_service import (
    PostgresGitHubConnectService,
)

AccessResolver = Callable[[Request], WorkspaceAccessContext]


def _resolve_request_id(request: Request) -> str:
    return (request.headers.get("X-Request-ID") or "").strip() or str(uuid.uuid4())


def _build_github_connect_error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id = _resolve_request_id(request)
    payload: dict[str, Any] = {
        "code": code,
        "message": redact_text(message),
        "retryable": retryable,
        "request_id": request_id,
    }
    if details is not None:
        payload["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": payload},
        headers={
            "Cache-Control": GITHUB_CONNECT_NO_STORE_HEADER,
            "X-Request-ID": request_id,
        },
    )


async def _parse_json_object(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except ValueError as exc:
        raise GitHubConnectError(
            status_code=422,
            code="INVALID_REQUEST_BODY",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise GitHubConnectError(
            status_code=422,
            code="INVALID_REQUEST_BODY",
            message="Request body must be a JSON object.",
        )
    return raw


def install_github_connect_error_handlers(app: FastAPI) -> None:
    """Attach M2-T201 + M1 auth error handlers with Cache-Control: private, no-store."""
    install_auth_error_handlers(app)

    @app.middleware("http")
    async def attach_github_connect_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        req_id = (request.headers.get("X-Request-ID") or "").strip()
        if req_id and "X-Request-ID" not in response.headers:
            response.headers["X-Request-ID"] = req_id
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return response

    @app.exception_handler(GitHubConnectError)
    async def handle_github_connect_error(
        request: Request, exc: GitHubConnectError
    ) -> JSONResponse:
        return _build_github_connect_error_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            details=exc.details,
        )

    @app.exception_handler(OAuthTokenExchangeError)
    async def handle_oauth_token_exchange_error(
        request: Request, exc: OAuthTokenExchangeError
    ) -> JSONResponse:
        return _build_github_connect_error_response(
            request=request,
            status_code=400,
            code="OAUTH_TOKEN_EXCHANGE_FAILED",
            message=str(exc),
            retryable=False,
        )


def create_github_connect_router(
    *,
    service: PostgresGitHubConnectService,
    resolve_access: AccessResolver,
) -> APIRouter:
    """Create the mountable M2-T201 APIRouter without touching ``app.py``."""
    router = APIRouter(tags=["github-connect"])

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return resolve_access(request)

    @router.post("/v1/sources/github/oauth/start", status_code=201)
    async def start_github_oauth_route(
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, Any]:
        body = await _parse_json_object(request)
        return_path = body.get("return_path")
        redirect_uri = body.get("redirect_uri")
        ttl_seconds = body.get("ttl_seconds", 600)
        if not isinstance(return_path, str):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="return_path must be a string.",
            )
        if not isinstance(redirect_uri, str):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="redirect_uri must be a string.",
            )
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="ttl_seconds must be an integer.",
            )

        result = service.start_oauth(
            access,
            return_path=return_path,
            redirect_uri=redirect_uri,
            ttl_seconds=ttl_seconds,
        )
        response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return result.to_http_dict()

    @router.post("/v1/sources/github/oauth/callback", status_code=200)
    async def complete_github_oauth_callback_route(
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, Any]:
        body = await _parse_json_object(request)
        state = body.get("state")
        code = body.get("code")
        redirect_uri = body.get("redirect_uri")
        repository_full_name = body.get("repository_full_name")
        provider_error = body.get("error")
        policy_version = body.get("policy_version")
        consent_version = body.get("consent_version")

        if not isinstance(state, str):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="state must be a string.",
            )
        if code is not None and not isinstance(code, str):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="code must be a string when provided.",
            )
        if not isinstance(redirect_uri, str):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="redirect_uri must be a string.",
            )
        if repository_full_name is not None and not isinstance(repository_full_name, str):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="repository_full_name must be a string when provided.",
            )
        if provider_error is not None and not isinstance(provider_error, str):
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="error must be a string when provided.",
            )

        result = service.complete_oauth_callback(
            access,
            idempotency_key=idempotency_key or "",
            state=state,
            code=code,
            redirect_uri=redirect_uri,
            repository_full_name=repository_full_name,
            provider_error=provider_error,
            policy_version=policy_version if isinstance(policy_version, str) else None,
            consent_version=consent_version if isinstance(consent_version, str) else None,
        )
        response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return result.to_http_dict()

    @router.post("/v1/source-resources", status_code=201)
    async def register_source_resource_route(
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, Any]:
        body = await _parse_json_object(request)
        source_account_id = body.get("source_account_id")
        repository_full_name = body.get("repository_full_name")
        policy_version = body.get("policy_version")
        consent_version = body.get("consent_version")

        if not isinstance(source_account_id, str) or not source_account_id.strip():
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="source_account_id must be a non-empty string.",
            )
        if not isinstance(repository_full_name, str) or not repository_full_name.strip():
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="repository_full_name must be a non-empty string.",
            )

        resource = service.register_source_resource(
            access,
            source_account_id=source_account_id,
            repository_full_name=repository_full_name,
            policy_version=policy_version if isinstance(policy_version, str) else None,
            consent_version=consent_version if isinstance(consent_version, str) else None,
        )
        response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return resource.to_http_dict()

    @router.get("/v1/source-accounts", status_code=200)
    def list_source_accounts_route(
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, Any]:
        items = service.list_source_accounts(access)
        response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return {"items": [item.to_http_dict() for item in items]}

    @router.get("/v1/source-resources", status_code=200)
    def list_source_resources_route(
        response: Response,
        source_account_id: Annotated[str | None, Query()] = None,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, Any]:
        items = service.list_source_resources(
            access, source_account_id=source_account_id
        )
        response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return {"items": [item.to_http_dict() for item in items]}

    @router.post("/v1/source-resources/{source_resource_id}/verify-policy", status_code=200)
    def verify_source_resource_policy_route(
        source_resource_id: str,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, Any]:
        result = service.verify_resource_policy(
            access, source_resource_id=source_resource_id
        )
        response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return result.to_http_dict()

    @router.post("/v1/source-accounts/{source_account_id}/disconnect", status_code=200)
    def disconnect_source_account_route(
        source_account_id: str,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, Any]:
        result = service.disconnect_source_account(
            access, source_account_id=source_account_id
        )
        response.headers["Cache-Control"] = GITHUB_CONNECT_NO_STORE_HEADER
        return result.to_http_dict()

    return router


def create_github_connect_app(
    *,
    service: PostgresGitHubConnectService,
    resolve_access: AccessResolver,
) -> FastAPI:
    """Create a standalone FastAPI app mounting the M2-T201 routes and error handlers."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    install_github_connect_error_handlers(app)
    app.include_router(
        create_github_connect_router(service=service, resolve_access=resolve_access)
    )
    return app
