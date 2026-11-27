"""FastAPI router and contract error handlers for the 4 initial-import routes (CONTRACT@87f1515)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthorizationBoundaryError,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceAccessContext,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.sync_runs.models import (
    CONTRACT_ERROR_MESSAGES,
    READ_NO_STORE_CACHE_CONTROL,
    SyncRunApiError,
    parse_idempotency_key,
    parse_initial_import_request,
    parse_retry_import_request,
)
from career_memory_api.sync_runs.postgres_service import PostgresSyncRunService
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

AccessResolver = Callable[[Request], WorkspaceAccessContext]
UnreadableRunsResolver = Callable[[Request], frozenset[str]]


def _build_contract_error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    existing_run_id: str | None = None,
) -> JSONResponse:
    raw_req_id = request.headers.get("X-Request-ID")
    request_id = (raw_req_id or "").strip() or str(uuid.uuid4())
    error_body: dict[str, object] = {
        "code": code,
        "message": CONTRACT_ERROR_MESSAGES[code],
        "retryable": code == "AUTH_VERIFICATION_UNAVAILABLE",
        "request_id": request_id,
    }
    if existing_run_id is not None:
        error_body["details"] = {"existing_run_id": existing_run_id}

    return JSONResponse(
        status_code=status_code,
        content={"error": error_body},
        headers={
            "X-Request-ID": request_id,
            "Cache-Control": READ_NO_STORE_CACHE_CONTROL,
        },
    )


def install_sync_runs_error_handlers(app: FastAPI) -> None:
    """Attach CONTRACT@87f1515 error envelopes and Cache-Control: private, no-store headers."""

    @app.middleware("http")
    async def echo_sync_runs_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        req_id = (request.headers.get("X-Request-ID") or "").strip()
        if req_id and "X-Request-ID" not in response.headers:
            response.headers["X-Request-ID"] = req_id
        return response

    @app.exception_handler(SyncRunApiError)
    async def handle_sync_run_api_error(
        request: Request, exc: SyncRunApiError
    ) -> Response:
        if exc.code is None:
            return Response(status_code=exc.status_code)
        return _build_contract_error_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            existing_run_id=exc.existing_run_id,
        )

    @app.exception_handler(SessionTerminatedError)
    async def handle_session_terminated(
        request: Request, _exc: SessionTerminatedError
    ) -> JSONResponse:
        return _build_contract_error_response(
            request=request,
            status_code=401,
            code="SESSION_TERMINATED",
        )

    @app.exception_handler(UnauthenticatedError)
    async def handle_unauthenticated(
        _request: Request, _exc: UnauthenticatedError
    ) -> Response:
        return Response(
            status_code=401,
            headers={"Cache-Control": READ_NO_STORE_CACHE_CONTROL},
        )

    @app.exception_handler(WorkspaceMembershipDeniedError)
    async def handle_workspace_revoked(
        request: Request, _exc: WorkspaceMembershipDeniedError
    ) -> JSONResponse:
        return _build_contract_error_response(
            request=request,
            status_code=403,
            code="WORKSPACE_SCOPE_VIOLATION",
        )

    @app.exception_handler(AccessVerificationUnavailableError)
    async def handle_auth_unavailable(
        request: Request, _exc: AccessVerificationUnavailableError
    ) -> JSONResponse:
        return _build_contract_error_response(
            request=request,
            status_code=503,
            code="AUTH_VERIFICATION_UNAVAILABLE",
        )

    @app.exception_handler(AuthorizationBoundaryError)
    async def handle_generic_authz(
        request: Request, _exc: AuthorizationBoundaryError
    ) -> JSONResponse:
        return _build_contract_error_response(
            request=request,
            status_code=403,
            code="WORKSPACE_SCOPE_VIOLATION",
        )

    @app.exception_handler(WorkspaceResourceNotFoundError)
    async def handle_resource_not_found(
        request: Request, _exc: WorkspaceResourceNotFoundError
    ) -> JSONResponse:
        return _build_contract_error_response(
            request=request,
            status_code=404,
            code="RESOURCE_NOT_FOUND",
        )


def create_sync_runs_router(
    *,
    service: PostgresSyncRunService,
    resolve_access: AccessResolver,
    resolve_unreadable_runs: UnreadableRunsResolver | None = None,
) -> APIRouter:
    """Create the mountable 4-route initial-import APIRouter without modifying app.py."""
    router = APIRouter()

    def _get_access(request: Request) -> WorkspaceAccessContext:
        return resolve_access(request)

    def _get_unreadable(request: Request) -> frozenset[str]:
        if resolve_unreadable_runs is None:
            return frozenset()
        return resolve_unreadable_runs(request)

    @router.post("/v1/sync-runs", status_code=202)
    async def create_initial_import_route(
        request: Request,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, object]:
        try:
            idem_key = parse_idempotency_key(request.headers.get("Idempotency-Key"))
        except ValueError:
            raise SyncRunApiError(status_code=422) from None
        try:
            body = await request.json()
        except ValueError:
            raise SyncRunApiError(status_code=422) from None
        try:
            parse_initial_import_request(body)
        except ValueError:
            raise SyncRunApiError(status_code=422) from None

        receipt = service.create_initial_import(
            access,
            idempotency_key=idem_key,
            request_payload=body,
            request_id=(request.headers.get("X-Request-ID") or "").strip() or None,
            unreadable_run_ids=_get_unreadable(request),
        )
        return receipt.to_contract_dict()

    @router.get("/v1/sync-runs", status_code=200)
    def recover_initial_import_route(
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, object]:
        source_resource_id = request.query_params.get("source_resource_id")
        limit = request.query_params.get("limit", "1")
        if not source_resource_id or limit != "1":
            raise SyncRunApiError(status_code=422)

        result = service.recover_latest(
            access,
            source_resource_id=source_resource_id,
            unreadable_run_ids=_get_unreadable(request),
        )
        response.headers["Cache-Control"] = READ_NO_STORE_CACHE_CONTROL
        return result

    @router.get("/v1/sync-runs/{id}", status_code=200)
    def get_initial_import_status_route(
        id: str,
        request: Request,
        response: Response,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, object]:
        snapshot = service.get_status(
            access,
            run_id=id,
            unreadable_run_ids=_get_unreadable(request),
        )
        response.headers["Cache-Control"] = READ_NO_STORE_CACHE_CONTROL
        return snapshot.to_contract_dict()

    @router.post("/v1/sync-runs/{id}/retry", status_code=202)
    async def retry_initial_import_route(
        id: str,
        request: Request,
        access: WorkspaceAccessContext = Depends(_get_access),
    ) -> dict[str, object]:
        try:
            idem_key = parse_idempotency_key(request.headers.get("Idempotency-Key"))
        except ValueError:
            raise SyncRunApiError(status_code=422) from None
        try:
            body = await request.json()
        except ValueError:
            raise SyncRunApiError(status_code=422) from None
        try:
            parsed = parse_retry_import_request(body)
        except ValueError:
            raise SyncRunApiError(status_code=422) from None

        receipt = service.retry_sync_run(
            access,
            run_id=id,
            idempotency_key=idem_key,
            expected_run_version=parsed.expected_run_version,
            request_id=(request.headers.get("X-Request-ID") or "").strip() or None,
            unreadable_run_ids=_get_unreadable(request),
        )
        return receipt.to_contract_dict()

    return router


def create_sync_runs_app(
    *,
    service: PostgresSyncRunService,
    resolve_access: AccessResolver,
    resolve_unreadable_runs: UnreadableRunsResolver | None = None,
) -> FastAPI:
    """Build a standalone FastAPI app mounting the 4 initial-import routes and error handlers."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    install_sync_runs_error_handlers(app)
    app.include_router(
        create_sync_runs_router(
            service=service,
            resolve_access=resolve_access,
            resolve_unreadable_runs=resolve_unreadable_runs,
        )
    )
    return app
