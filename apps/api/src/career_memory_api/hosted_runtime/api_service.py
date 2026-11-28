"""Hosted Cloud Run API service factory (``cm-api``) with T-507 edge_guard and ``GET /v1/session``."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from career_memory_api.app import create_app
from career_memory_api.auth.jwt_verifier import (
    JwksProviderPort,
    JwtAccessTokenVerifier,
    JwtVerificationConfig,
)
from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthenticatedUser,
    AuthSubject,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.budgets import (
    DefaultQuotaPolicyConfig,
    PostgresQuotaBudgetService,
    QuotaMetricLimit,
    create_budgets_router,
)
from career_memory_api.claims import PostgresClaimService, create_claims_router
from career_memory_api.contracts_api.models import PRIVATE_NO_STORE_HEADER
from career_memory_api.contracts_api.router import (
    PostgresContractBoundaryService,
    create_contracts_router,
)
from career_memory_api.deletion import (
    EphemeralCacheRegistry,
    PostgresDeletionService,
    create_deletion_router,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_workspace_access_guard,
)
from career_memory_api.edge_guard.middleware import HmacWorkerTokenVerifier
from career_memory_api.edge_guard.models import (
    CorsCsrfConfig,
    EdgeGuardConfig,
    ManualEdgePauseController,
    OriginAuthConfig,
    WorkerAuthConfig,
)
from career_memory_api.exports import PostgresExportService, create_exports_router
from career_memory_api.facts import PostgresFactService, create_facts_router
from career_memory_api.fullstack_runtime.models import BootstrapResourceSummary
from career_memory_api.fullstack_runtime.runtime_stack import (
    PooledPostgresProvenanceMetadataStore,
)
from career_memory_api.fullstack_runtime.sample_github_provider import (
    LivePublicReadGitHubCredentialProvider,
    build_live_oauth_credential_provider,
)
from career_memory_api.github_connect import (
    PostgresGitHubConnectService,
    create_github_connect_router,
)
from career_memory_api.github_connect.ports import GitHubCredentialProviderPort
from career_memory_api.github_provider.import_client import HttpGitHubImportClient
from career_memory_api.github_provider.resolver_adapter import GitHubResolverContentFetcher
from career_memory_api.github_provider.transport import (
    GITHUB_API_ALLOWED_HOSTS,
    GitHubWireTransportPort,
    SafeGitHubHttpTransport,
    UrllibGitHubWireTransport,
)
from career_memory_api.hosted_runtime.jwks_client import (
    HostedPostgresWorkspaceAccessProvider,
    HttpsJwksProvider,
    SupabaseGoTrueLiveSessionVerifier,
)
from career_memory_api.hosted_runtime.models import (
    HostedApiServiceConfig,
    HostedSessionPayload,
)
from career_memory_api.hosted_runtime.pooler import HostedTransactionPool
from career_memory_api.hosted_runtime.worker_wake import HttpCloudRunWakeNotifier
from career_memory_api.identities import PostgresIdentityService, create_identities_router
from career_memory_api.jobs.models import NoopWakeNotifier, PostCommitWakeNotifierPort
from career_memory_api.llm_gate import LlmProviderMode
from career_memory_api.resolver import (
    EndpointSecurityPolicy,
    OperationBudgetConfig,
    RefetchResolverService,
    create_resolver_router,
)
from career_memory_api.schema_contracts.enums import QuotaMetricKind
from career_memory_api.search import PostgresSearchIndexer, PostgresSearchService
from career_memory_api.sync_runs import (
    PostgresSyncRunService,
    create_sync_runs_router,
    install_sync_runs_error_handlers,
)
from career_memory_api.work_events import (
    PostgresWorkEventMutationService,
    PostgresWorkEventsReadService,
    create_work_events_router,
)


def _session_error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
) -> JSONResponse:
    request_id = (request.headers.get("X-Request-ID") or "").strip() or str(uuid.uuid4())
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "request_id": request_id,
            }
        },
        headers={
            "Cache-Control": PRIVATE_NO_STORE_HEADER,
            "CDN-Cache-Control": "no-store",
            "X-Request-ID": request_id,
        },
    )


HOSTED_GITHUB_PUBLIC_SECRET_REF = "hosted:github-public-read:v1"
EPHEMERAL_GITHUB_PUBLIC_TOKEN_HEADER = "X-CM-GitHub-Public-Token"


def _guard_access_resolver(request: Request) -> WorkspaceAccessContext:
    guard = get_workspace_access_guard(request)
    credentials = get_auth_credentials(authorization=request.headers.get("Authorization"))
    target_ws = (request.headers.get("X-Workspace-ID") or "").strip()
    access = guard.require_workspace_access(
        credentials=credentials,
        target_workspace_id=target_ws,
    )
    runtime = getattr(request.app.state, "hosted_api_runtime", None)
    if runtime is not None:
        raw_gh_token = (request.headers.get(EPHEMERAL_GITHUB_PUBLIC_TOKEN_HEADER) or "").strip()
        if raw_gh_token and len(raw_gh_token) <= 512 and not any(ch.isspace() for ch in raw_gh_token):
            runtime.set_ephemeral_public_github_token(raw_gh_token)
        if (
            request.method == "GET"
            and request.url.path.startswith("/v1/sync-runs")
            and isinstance(runtime.wake_notifier, HttpCloudRunWakeNotifier)
        ):
            runtime.wake_notifier.maybe_nudge_recover_async(
                workspace_id=access.workspace_id
            )
    return access


class HostedApiRuntime:
    """Wired M1-M5 hosted API service stack using ``cm_api_app`` (:6543, NOBYPASSRLS)."""

    def __init__(
        self,
        config: HostedApiServiceConfig,
        *,
        jwks_provider: JwksProviderPort | None = None,
        live_session_verifier: SupabaseGoTrueLiveSessionVerifier | None = None,
        wake_notifier: PostCommitWakeNotifierPort | None = None,
        github_wire_transport: GitHubWireTransportPort | None = None,
        github_credential_provider: GitHubCredentialProviderPort | None = None,
        pause_controller: ManualEdgePauseController | None = None,
    ) -> None:
        self.config = config
        self.pool = HostedTransactionPool(config.db_pool, verify_role_on_first_checkout=True)
        api_factory = self.pool.as_connection_factory()

        resolved_jwks: JwksProviderPort = jwks_provider or HttpsJwksProvider(
            jwks_url=config.jwks_url,
            cache_ttl_seconds=config.jwks_cache_ttl_seconds,
            timeout_seconds=config.auth_http_timeout_seconds,
            allow_http_loopback_for_tests=config.allow_http_loopback_for_tests,
        )
        self.token_verifier = JwtAccessTokenVerifier(
            config=JwtVerificationConfig(
                expected_issuer=config.jwt_issuer,
                expected_audience=config.jwt_audience,
                allowed_algorithms=config.allowed_jwt_algorithms,
            ),
            jwks_provider=resolved_jwks,
        )

        resolved_live_verifier = live_session_verifier
        if (
            resolved_live_verifier is None
            and config.supabase_auth_url
            and config.supabase_anon_key
        ):
            resolved_live_verifier = SupabaseGoTrueLiveSessionVerifier(
                supabase_auth_url=config.supabase_auth_url,
                supabase_anon_key=config.supabase_anon_key,
                timeout_seconds=config.auth_http_timeout_seconds,
                allow_http_loopback_for_tests=config.allow_http_loopback_for_tests,
            )

        self.access_provider = HostedPostgresWorkspaceAccessProvider(
            pool=self.pool,
            token_verifier=self.token_verifier,
            live_session_verifier=resolved_live_verifier,
        )

        self.secret_store = InMemorySecretStore()
        self._ephemeral_public_github_token: str | None = (
            config.github_public_read_token.strip()
            if config.github_public_read_token and config.github_public_read_token.strip()
            else None
        )
        self.cache_registry = EphemeralCacheRegistry()
        self.pause_controller = pause_controller or ManualEdgePauseController()

        # Post-commit worker wake notifier
        if wake_notifier is not None:
            self.wake_notifier: PostCommitWakeNotifierPort = wake_notifier
        elif config.worker_wake_url:
            hmac_verifier = (
                HmacWorkerTokenVerifier(secret=config.worker_hmac_secret)
                if config.worker_hmac_secret
                else None
            )
            self.wake_notifier = HttpCloudRunWakeNotifier(
                worker_url=config.worker_wake_url,
                audience=config.worker_wake_audience or config.worker_wake_url,
                timeout_seconds=config.worker_wake_timeout_seconds,
                hmac_verifier=hmac_verifier,
                allow_http_loopback_for_tests=config.allow_http_loopback_for_tests,
                github_public_token_getter=lambda: self._ephemeral_public_github_token,
            )
        else:
            self.wake_notifier = NoopWakeNotifier()

        security_policy = EndpointSecurityPolicy(
            allowed_hosts=GITHUB_API_ALLOWED_HOSTS | frozenset({"raw.githubusercontent.com"}),
            allow_redirects=True,
            max_redirects=3,
        )
        wire_transport = github_wire_transport or UrllibGitHubWireTransport()
        self.github_transport = SafeGitHubHttpTransport(
            wire_transport=wire_transport,
            secret_store=self.secret_store,
            token_resolver=self._resolve_github_token,
            security_policy=security_policy,
        )
        if github_credential_provider is not None:
            self.credential_provider: GitHubCredentialProviderPort = github_credential_provider
        elif config.github_client_id and config.github_client_secret:
            self.credential_provider = build_live_oauth_credential_provider(
                client_id=config.github_client_id,
                client_secret=config.github_client_secret,
                allowed_redirect_uris=config.allowed_redirect_uris,
            )
        else:
            public_token_ref: str | None = None
            if config.github_public_read_token:
                public_token_ref = self.secret_store.store_secret(
                    "github-public-read", config.github_public_read_token
                )
            self.credential_provider = LivePublicReadGitHubCredentialProvider(
                transport=self.github_transport,
                public_read_token_ref=public_token_ref,
            )

        self.github_import_client = HttpGitHubImportClient(transport=self.github_transport)
        self.content_fetcher = GitHubResolverContentFetcher(
            transport=self.github_transport,
            require_credentials=False,
        )
        provenance_store = PooledPostgresProvenanceMetadataStore(api_factory)
        self.resolver = RefetchResolverService(
            metadata_store=provenance_store,
            fetcher=self.content_fetcher,
            default_budget=OperationBudgetConfig(
                max_requests=200,
                max_total_bytes=64 * 1024 * 1024,
                max_unit_bytes=5 * 1024 * 1024,
            ),
            security_policy=security_policy,
        )

        self.github_connect_service = PostgresGitHubConnectService(
            connection_factory=api_factory,
            credential_provider=self.credential_provider,
            secret_store=self.secret_store,
            allowed_redirect_uris=config.allowed_redirect_uris,
        )
        self.sync_run_service = PostgresSyncRunService(
            connection_factory=api_factory,
            wake_notifier=self.wake_notifier,
        )
        self.search_indexer = PostgresSearchIndexer(connection_factory=api_factory)
        self.search_service = PostgresSearchService(api_connection_factory=api_factory)
        self.work_events_read_service = PostgresWorkEventsReadService(
            api_connection_factory=api_factory
        )
        self.work_events_mutation_service = PostgresWorkEventMutationService(
            api_connection_factory=api_factory,
            search_indexer=self.search_indexer,
        )
        self.identity_service = PostgresIdentityService(api_connection_factory=api_factory)
        self.fact_service = PostgresFactService(
            api_connection_factory=api_factory,
            resolver=self.resolver,
            search_indexer=self.search_indexer,
        )
        # Subscription LLM stays strictly HARD-OFF (OFF_MANUAL_ONLY, default_adapter=None)
        # while OD-002 / OD-014 are HELD.
        self.claim_service = PostgresClaimService(
            api_connection_factory=api_factory,
            resolver=self.resolver,
            default_adapter=None,
            default_provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            allow_synthetic_fixture_mode=False,
            search_indexer=self.search_indexer,
        )
        self.export_service = PostgresExportService(
            api_connection_factory=api_factory,
            resolver=self.resolver,
        )
        self.deletion_service = PostgresDeletionService(
            api_connection_factory=api_factory,
            ephemeral_cache_registry=self.cache_registry,
        )
        quota_policy = DefaultQuotaPolicyConfig(
            limits={
                QuotaMetricKind.IMPORT_OBJECTS: QuotaMetricLimit(
                    soft_warning_limit=4000, hard_cutoff_limit=5000
                ),
                QuotaMetricKind.PROVIDER_REQUESTS: QuotaMetricLimit(
                    soft_warning_limit=400, hard_cutoff_limit=500
                ),
                QuotaMetricKind.REFETCH_BYTES: QuotaMetricLimit(
                    soft_warning_limit=50_000_000, hard_cutoff_limit=64_000_000
                ),
                QuotaMetricKind.EXPORT_BUILDS: QuotaMetricLimit(
                    soft_warning_limit=80, hard_cutoff_limit=100
                ),
                QuotaMetricKind.SEARCH_QUERIES: QuotaMetricLimit(
                    soft_warning_limit=800, hard_cutoff_limit=1000
                ),
                QuotaMetricKind.ESTIMATED_COST_KRW: QuotaMetricLimit(
                    soft_warning_limit=0, hard_cutoff_limit=0
                ),
                QuotaMetricKind.LLM_TOKENS: QuotaMetricLimit(
                    soft_warning_limit=0, hard_cutoff_limit=0
                ),
            }
        )
        self.budget_service = PostgresQuotaBudgetService(
            api_connection_factory=api_factory,
            policy_config=quota_policy,
        )
        self.contracts_service = PostgresContractBoundaryService(
            claims_service=self.claim_service,
            search_service=self.search_service,
            deletion_service=self.deletion_service,
        )

        # Public API edge guard:
        # - Direct unauthenticated *.run.app requests fail closed (403 ORIGIN_AUTH_REQUIRED).
        # - Requests from cm-web BFF / Cloudflare carrying signed X-CM-Origin-Auth pass.
        # - WorkerAuthConfig(token_verifier=None) unconditionally denies /internal/worker/**.
        self.edge_guard_config = EdgeGuardConfig(
            origin_auth=OriginAuthConfig(
                require_origin_auth=config.require_origin_auth,
                origin_hmac_secrets=config.origin_hmac_secrets,
                block_direct_run_app_hosts=True,
                allow_run_app_host_with_origin_proof=config.allow_run_app_host_with_origin_proof,
            ),
            worker_auth=WorkerAuthConfig(token_verifier=None),
            cors_csrf=CorsCsrfConfig(allowed_web_origins=config.allowed_web_origins),
            caps=config.edge_caps,
        )

    def build_session_payload(
        self,
        *,
        access: WorkspaceAccessContext,
        workspace_name: str,
        user_display_name: str,
        user_email: str,
    ) -> HostedSessionPayload:
        """Read-only SELECT query building the ``GET /v1/session`` payload under RLS."""
        accounts = self.github_connect_service.list_source_accounts(access)
        active_acct = next((a for a in accounts if a.status == "ACTIVE"), None)
        resources_raw = self.github_connect_service.list_source_resources(access)

        resource_summaries: list[BootstrapResourceSummary] = []
        for res in resources_raw:
            if res.status == "DELETED":
                continue
            latest_id: str | None = None
            latest_status: str | None = None
            latest_proc: str | None = None
            latest_ready = False
            try:
                recovered = self.sync_run_service.recover_latest(
                    access, source_resource_id=res.id
                )
                items = recovered.get("items") or []
                if items:
                    first = items[0]
                    latest_id = str(first.get("target_id") or "") or None
                    imp = first.get("import_status") or {}
                    latest_status = str(imp.get("status") or "") or None
                    latest_proc = str(imp.get("processing_status") or "") or None
                    latest_ready = bool(imp.get("import_ready"))
            except Exception:  # noqa: BLE001
                # Deleted or fenced resources conceal status; keep resource summary minimal
                pass

            resource_summaries.append(
                BootstrapResourceSummary(
                    source_resource_id=res.id,
                    source_account_id=res.source_account_id,
                    provider_resource_id=res.provider_resource_id,
                    repository_full_name=res.name,
                    canonical_url=res.canonical_url,
                    visibility=res.visibility,
                    status=res.status,
                    default_branch="main",
                    description=f"Allowlisted public GitHub repository ({res.name})",
                    primary_language="Code",
                    estimated_commits=0,
                    estimated_pull_requests=0,
                    latest_sync_run_id=latest_id,
                    latest_sync_run_status=latest_status,
                    latest_processing_status=latest_proc,
                    latest_import_ready=latest_ready,
                )
            )

        github_login = ""
        default_confirmed_event_id: str | None = None
        with self.pool.connection() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    """
                    SELECT m.username
                    FROM public.external_identity_mappings m
                    JOIN public.identities i
                      ON i.workspace_id = m.workspace_id
                     AND i.id = m.identity_id
                    WHERE m.workspace_id = %s::uuid
                      AND i.canonical_user_id = %s::uuid
                      AND m.provider = 'GITHUB'
                      AND m.status = 'VERIFIED'
                    ORDER BY m.verified_at DESC NULLS LAST, m.created_at ASC
                    LIMIT 1
                    """,
                    (access.workspace_id, access.user_id),
                )
                gh_row = cur.fetchone()
                if gh_row is not None and gh_row[0]:
                    github_login = str(gh_row[0])

                cur.execute(
                    """
                    SELECT id::text
                    FROM public.work_events
                    WHERE workspace_id = %s::uuid
                      AND status = 'CONFIRMED'
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT 1
                    """,
                    (access.workspace_id,),
                )
                ev_row = cur.fetchone()
                if ev_row is not None and ev_row[0]:
                    default_confirmed_event_id = str(ev_row[0])

        default_source_resource_id = (
            resource_summaries[0].source_resource_id if resource_summaries else None
        )

        return HostedSessionPayload(
            mode="hosted_supabase",
            workspace_id=access.workspace_id,
            workspace_name=workspace_name,
            user_id=access.user_id,
            user_display_name=user_display_name,
            user_email=user_email,
            github_login=github_login,
            connected_account_id=active_acct.id if active_acct else None,
            resources=tuple(resource_summaries),
            default_source_resource_id=default_source_resource_id,
            default_confirmed_event_id=default_confirmed_event_id,
            sample_repositories=(),
        )

    def set_ephemeral_public_github_token(self, token: str) -> None:
        cleaned = token.strip()
        if cleaned and not any(ch.isspace() for ch in cleaned):
            self._ephemeral_public_github_token = cleaned

    def _resolve_github_token(self, secret_ref: str) -> str | None:
        if secret_ref == HOSTED_GITHUB_PUBLIC_SECRET_REF:
            return self._ephemeral_public_github_token
        return self.secret_store.get_secret(secret_ref)

    def close(self) -> None:
        self.pool.close()


def create_hosted_api_app(
    config: HostedApiServiceConfig,
    *,
    jwks_provider: JwksProviderPort | None = None,
    live_session_verifier: SupabaseGoTrueLiveSessionVerifier | None = None,
    wake_notifier: PostCommitWakeNotifierPort | None = None,
    github_wire_transport: GitHubWireTransportPort | None = None,
    github_credential_provider: GitHubCredentialProviderPort | None = None,
    pause_controller: ManualEdgePauseController | None = None,
) -> FastAPI:
    """Create the production Cloud Run API FastAPI app with T-507 edge_guard & ``GET /v1/session``.

    ``career_memory_api.app:create_app()`` and ``career_memory_api.main:app`` remain
    100% untouched and fail-closed.
    """
    runtime = HostedApiRuntime(
        config,
        jwks_provider=jwks_provider,
        live_session_verifier=live_session_verifier,
        wake_notifier=wake_notifier,
        github_wire_transport=github_wire_transport,
        github_credential_provider=github_credential_provider,
        pause_controller=pause_controller,
    )

    app = create_app(
        access_provider=runtime.access_provider,
        sync_runs_router=create_sync_runs_router(
            service=runtime.sync_run_service,
            resolve_access=_guard_access_resolver,
        ),
        resolver_router=create_resolver_router(runtime.resolver),
        contracts_router=create_contracts_router(
            service=runtime.contracts_service,
            resolve_access=_guard_access_resolver,
        ),
        github_connect_router=create_github_connect_router(
            service=runtime.github_connect_service,
            resolve_access=_guard_access_resolver,
        ),
        work_events_router=create_work_events_router(
            service=runtime.work_events_read_service,
            mutation_service=runtime.work_events_mutation_service,
            resolve_access=_guard_access_resolver,
        ),
        identities_router=create_identities_router(
            service=runtime.identity_service,
            resolve_access=_guard_access_resolver,
        ),
        facts_router=create_facts_router(
            service=runtime.fact_service,
            resolve_access=_guard_access_resolver,
        ),
        claims_router=create_claims_router(
            service=runtime.claim_service,
            resolve_access=_guard_access_resolver,
        ),
        exports_router=create_exports_router(
            service=runtime.export_service,
            resolve_access=_guard_access_resolver,
        ),
        deletion_router=create_deletion_router(
            service=runtime.deletion_service,
            resolve_access=_guard_access_resolver,
        ),
        budgets_router=create_budgets_router(
            service=runtime.budget_service,
            resolve_access=_guard_access_resolver,
        ),
        install_contract_handlers=True,
        install_github_connect_handlers=True,
        install_work_events_handlers=True,
        install_identities_handlers=True,
        install_facts_handlers=True,
        install_claims_handlers=True,
        install_exports_handlers=True,
        install_deletion_handlers=True,
        install_budgets_handlers=True,
        edge_guard_config=runtime.edge_guard_config,
        edge_pause_controller=runtime.pause_controller,
    )
    install_sync_runs_error_handlers(app)
    app.state.hosted_api_runtime = runtime

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> Iterator[None]:  # type: ignore[misc]
        try:
            yield
        finally:
            runtime.close()

    app.router.lifespan_context = _lifespan

    # Post-commit wake notification on deletion-confirm 202 (non-fatal if wake fails)
    @app.middleware("http")
    async def wake_worker_on_deletion_confirm(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        if (
            request.method == "POST"
            and request.url.path.endswith("/deletion-confirm")
            and response.status_code == 202
        ):
            ws_id = (request.headers.get("X-Workspace-ID") or "").strip()
            if ws_id and isinstance(runtime.wake_notifier, HttpCloudRunWakeNotifier):
                runtime.wake_notifier.notify_deletion_job(workspace_id=ws_id)
        return response

    @app.get("/v1/session", include_in_schema=False)
    def get_hosted_session(request: Request, response: Response) -> Any:
        credentials = get_auth_credentials(authorization=request.headers.get("Authorization"))
        try:
            claims = runtime.access_provider.verify_claims(credentials)
        except SessionTerminatedError:
            return _session_error_response(
                request=request,
                status_code=401,
                code="SESSION_TERMINATED",
                message="The session has ended.",
                retryable=False,
            )
        except UnauthenticatedError:
            return _session_error_response(
                request=request,
                status_code=401,
                code="UNAUTHENTICATED",
                message="Valid bearer authentication is required.",
                retryable=False,
            )
        except AccessVerificationUnavailableError:
            return _session_error_response(
                request=request,
                status_code=503,
                code="AUTH_VERIFICATION_UNAVAILABLE",
                message="Authentication verification service is temporarily unavailable.",
                retryable=True,
            )

        try:
            profile = runtime.access_provider.lookup_user_profile(claims)
        except AccessVerificationUnavailableError:
            return _session_error_response(
                request=request,
                status_code=503,
                code="AUTH_VERIFICATION_UNAVAILABLE",
                message="Authentication verification service is temporarily unavailable.",
                retryable=True,
            )

        if profile is None:
            return _session_error_response(
                request=request,
                status_code=403,
                code="WORKSPACE_SCOPE_VIOLATION",
                message="User is not enrolled in any active workspace.",
                retryable=False,
            )

        auth_user = AuthenticatedUser(
            user_id=profile.user_id,
            auth_subject=AuthSubject(
                issuer=profile.auth_issuer,
                subject=profile.auth_subject,
            ),
        )
        requested_ws = (request.headers.get("X-Workspace-ID") or "").strip() or None
        try:
            membership_info = runtime.access_provider.resolve_active_workspace_membership(
                auth_user,
                requested_workspace_id=requested_ws,
            )
        except AccessVerificationUnavailableError:
            return _session_error_response(
                request=request,
                status_code=503,
                code="AUTH_VERIFICATION_UNAVAILABLE",
                message="Authentication verification service is temporarily unavailable.",
                retryable=True,
            )

        if membership_info is None:
            return _session_error_response(
                request=request,
                status_code=403,
                code="WORKSPACE_SCOPE_VIOLATION",
                message="No active workspace membership found for this user.",
                retryable=False,
            )

        access = WorkspaceAccessContext(
            user=auth_user,
            membership=WorkspaceMembership(
                workspace_id=membership_info.workspace_id,
                user_id=membership_info.user_id,
                role=membership_info.role,
            ),
        )
        payload = runtime.build_session_payload(
            access=access,
            workspace_name=membership_info.workspace_name,
            user_display_name=profile.display_name,
            user_email=profile.email,
        )
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        response.headers["CDN-Cache-Control"] = "no-store"
        return payload.to_dict()

    @app.get("/api/health", include_in_schema=False)
    def api_health() -> dict[str, str]:
        return {"status": "ok", "service": "career-memory-api"}

    return app


def create_hosted_api_app_from_env() -> FastAPI:
    """Build the hosted Cloud Run API app strictly from environment variables."""
    return create_hosted_api_app(HostedApiServiceConfig.from_env())


__all__ = [
    "HostedApiRuntime",
    "create_hosted_api_app",
    "create_hosted_api_app_from_env",
]
