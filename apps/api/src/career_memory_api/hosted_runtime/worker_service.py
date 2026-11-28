"""Private IAM Cloud Run Worker service factory (``cm-worker``, OD-012 / T-007A..F)."""

from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.auth.redaction import redact_text
from career_memory_api.backfill_engine.postgres_page_committer import (
    PostgresBackfillPageCommitter,
)
from career_memory_api.deletion import EphemeralCacheRegistry, PostgresDeletionService
from career_memory_api.edge_guard.middleware import HmacWorkerTokenVerifier, install_edge_guard
from career_memory_api.edge_guard.models import (
    EdgeGuardConfig,
    ManualEdgePauseController,
    OriginAuthConfig,
    WorkerAuthConfig,
    WorkerIdentity,
)
from career_memory_api.event_candidates.postgres_builder import (
    PostgresEventCandidateBuilder,
)
from career_memory_api.fullstack_runtime.runtime_stack import (
    PooledPostgresProvenanceMetadataStore,
)
from career_memory_api.github_provider.import_client import HttpGitHubImportClient
from career_memory_api.github_provider.resolver_adapter import GitHubResolverContentFetcher
from career_memory_api.github_provider.transport import (
    GITHUB_API_ALLOWED_HOSTS,
    GitHubWireTransportPort,
    SafeGitHubHttpTransport,
    UrllibGitHubWireTransport,
)
from career_memory_api.hosted_runtime.models import HostedWorkerServiceConfig
from career_memory_api.hosted_runtime.pooler import HostedTransactionPool
from career_memory_api.hosted_runtime.worker_wake import GoogleOidcWorkerTokenVerifier
from career_memory_api.initial_import.derived_units import DerivedUnitIngestionProcessor
from career_memory_api.initial_import.orchestrator import InitialBackfillOrchestrator
from career_memory_api.initial_import.worker_runner import InitialBackfillJobWorker
from career_memory_api.jobs import PostgresJobRuntime
from career_memory_api.jobs.models import OutboxEventRecord
from career_memory_api.jobs.postgres_runtime import bind_worker_workspace_context
from career_memory_api.resolver import (
    EndpointSecurityPolicy,
    OperationBudgetConfig,
    RefetchResolverService,
)

logger = logging.getLogger(__name__)


class WorkerWakeRequest(BaseModel):
    """Redacted JSON request payload for ``POST /internal/worker/wake``."""

    workspace_id: str | None = None
    job_id: str | None = None
    job_type: str | None = None
    target_sync_run_id: str | None = None
    deletion_job_id: str | None = None
    outbox_event_id: str | None = None
    max_slices: int | None = Field(default=None, ge=1, le=20)


class WorkerRecoverRequest(BaseModel):
    """Redacted JSON request payload for ``POST /internal/worker/recover``."""

    workspace_id: str | None = None
    workspace_ids: list[str] = Field(default_factory=list)
    max_slices: int | None = Field(default=None, ge=1, le=20)
    outbox_limit: int = Field(default=20, ge=1, le=100)


class _RecoveryOutboxPublisher:
    """Durable recovery outbox sink marking wake/deletion outbox rows published after drain."""

    def __init__(self) -> None:
        self.published_event_ids: list[str] = []

    def publish(self, event: OutboxEventRecord) -> None:
        self.published_event_ids.append(event.id)


def _normalize_workspace_id(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    try:
        return str(uuid.UUID(raw.strip()))
    except ValueError:
        return None


class HostedWorkerRuntime:
    """Private ``cm_worker_app`` (NOBYPASSRLS, :6543, max_size <= 2) bounded slice runner."""

    def __init__(
        self,
        config: HostedWorkerServiceConfig,
        *,
        github_wire_transport: GitHubWireTransportPort | None = None,
        pause_controller: ManualEdgePauseController | None = None,
    ) -> None:
        self.config = config
        self.pool = HostedTransactionPool(config.db_pool, verify_role_on_first_checkout=True)
        worker_factory = self.pool.as_connection_factory()

        self.shutdown_event = threading.Event()
        self.pause_controller = pause_controller or ManualEdgePauseController()
        self.secret_store = InMemorySecretStore()
        self._ephemeral_public_github_token: str | None = (
            config.github_public_read_token.strip()
            if config.github_public_read_token and config.github_public_read_token.strip()
            else None
        )
        self.cache_registry = EphemeralCacheRegistry()

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
        self.github_import_client = HttpGitHubImportClient(transport=self.github_transport)
        self.content_fetcher = GitHubResolverContentFetcher(
            transport=self.github_transport,
            require_credentials=False,
        )
        provenance_store = PooledPostgresProvenanceMetadataStore(worker_factory)
        worker_op_budget = OperationBudgetConfig(
            max_requests=1000,
            max_total_bytes=64 * 1024 * 1024,
            max_unit_bytes=5 * 1024 * 1024,
        )
        self.resolver = RefetchResolverService(
            metadata_store=provenance_store,
            fetcher=self.content_fetcher,
            default_budget=worker_op_budget,
            security_policy=security_policy,
        )

        self.job_runtime = PostgresJobRuntime(connection_factory=worker_factory)
        page_committer = PostgresBackfillPageCommitter(worker_connection_factory=worker_factory)
        self.backfill_orchestrator = InitialBackfillOrchestrator(
            worker_connection_factory=worker_factory,
            github_client=self.github_import_client,
            page_committer=page_committer,
            github_connect_service=None,
        )
        self.derived_unit_processor = DerivedUnitIngestionProcessor(
            worker_connection_factory=worker_factory,
            content_fetcher=self.content_fetcher,
            resolver_service=self.resolver,
            security_policy=security_policy,
            budget=worker_op_budget,
        )
        self.event_candidate_builder = PostgresEventCandidateBuilder(
            worker_connection_factory=worker_factory
        )
        self.backfill_worker = InitialBackfillJobWorker(
            job_runtime=self.job_runtime,
            orchestrator=self.backfill_orchestrator,
            derived_unit_processor=self.derived_unit_processor,
            event_candidate_builder=self.event_candidate_builder,
        )
        # Worker connects ONLY as cm_worker_app (NOBYPASSRLS).
        self.deletion_service = PostgresDeletionService(
            api_connection_factory=worker_factory,
            worker_connection_factory=worker_factory,
            ephemeral_cache_registry=self.cache_registry,
        )

    def request_shutdown(self) -> None:
        """Signal graceful cancellation (e.g. SIGTERM) without corrupting committed checkpoints."""
        self.shutdown_event.set()

    def _list_pending_deletion_jobs(self, workspace_id: str, *, limit: int) -> list[str]:
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.deletion_jobs
                        WHERE workspace_id = %s::uuid
                          AND status IN ('PENDING', 'PARTIAL')
                        ORDER BY requested_at ASC
                        LIMIT %s
                        """,
                        (workspace_id, max(1, int(limit))),
                    )
                    return [str(r[0]) for r in cur.fetchall()]

    def _list_unfinished_derived_runs(self, workspace_id: str, *, limit: int) -> list[str]:
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        """
                        SELECT count(*)
                        FROM public.jobs
                        WHERE workspace_id = %s::uuid
                          AND status = 'RUNNING'
                        """,
                        (workspace_id,),
                    )
                    if int(cur.fetchone()[0]) > 0:
                        return []
                    cur.execute(
                        """
                        SELECT sr.id::text
                        FROM public.sync_runs sr
                        JOIN public.source_resources res
                          ON res.workspace_id = sr.workspace_id
                         AND res.id = sr.source_resource_id
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.status = 'SUCCESS'
                          AND sr.import_ready = true
                          AND sr.processing_status IN ('NOT_STARTED', 'RUNNING')
                          AND res.status = 'ACTIVE'
                        ORDER BY sr.updated_at ASC
                        LIMIT %s
                        """,
                        (workspace_id, max(1, int(limit))),
                    )
                    return [str(r[0]) for r in cur.fetchall()]

    def process_workspace_slices(
        self,
        workspace_id: str,
        *,
        max_slices: int | None = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Claim and execute bounded SYNC_RUN + deletion_jobs slices for ``workspace_id``."""
        ws_uuid = _normalize_workspace_id(workspace_id)
        if ws_uuid is None:
            return {
                "status": "ok",
                "workspace_id": None,
                "processed_slices": 0,
                "sync_jobs_processed": 0,
                "deletion_jobs_processed": 0,
                "deletion_jobs_partial": 0,
                "shutdown_requested": self.shutdown_event.is_set(),
            }

        cap = min(max_slices or self.config.max_slices_per_wake, 20)
        budget_s = float(
            deadline_seconds
            if deadline_seconds is not None
            else self.config.worker_request_slice_timeout_seconds
        )
        deadline = time.monotonic() + max(0.5, budget_s)
        lease_duration = timedelta(seconds=self.config.lease_duration_seconds)

        processed_slices = 0
        sync_jobs_processed = 0
        deletion_jobs_processed = 0
        deletion_jobs_partial = 0
        sync_job_outcomes: list[dict[str, Any]] = []
        deletion_job_outcomes: list[dict[str, Any]] = []

        # 1. Bounded SYNC_RUN slices
        while (
            processed_slices < cap
            and not self.shutdown_event.is_set()
            and time.monotonic() < deadline
        ):
            step = self.backfill_worker.claim_and_execute_next(
                ws_uuid,
                worker_id=self.config.worker_id,
                access=None,
                lease_duration=lease_duration,
                run_derived_units=True,
                run_event_candidate_builder=True,
            )
            if step is None:
                break
            processed_slices += 1
            sync_jobs_processed += 1
            sync_job_outcomes.append(
                {
                    "job_id": step.claimed_job.job.id,
                    "sync_run_id": step.execution.sync_run_id,
                    "job_status": step.job_final_status.value,
                    "sync_run_status": step.execution.status.value,
                    "import_ready": step.execution.import_ready,
                }
            )

        # 2. Bounded deletion_jobs slices (OD-003)
        remaining = cap - processed_slices
        if (
            remaining > 0
            and not self.shutdown_event.is_set()
            and time.monotonic() < deadline
        ):
            pending_del_ids = self._list_pending_deletion_jobs(ws_uuid, limit=remaining)
            for del_job_id in pending_del_ids:
                if self.shutdown_event.is_set() or time.monotonic() >= deadline:
                    break
                try:
                    del_res = self.deletion_service.execute_deletion_job(
                        ws_uuid,
                        del_job_id,
                    )
                    processed_slices += 1
                    deletion_jobs_processed += 1
                    if del_res.status.value == "PARTIAL":
                        deletion_jobs_partial += 1
                    deletion_job_outcomes.append(
                        {
                            "deletion_job_id": del_res.deletion_job_id,
                            "status": del_res.status.value,
                            "fence_active": del_res.fence_active,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Worker deletion job %s encountered error: %s",
                        del_job_id,
                        redact_text(str(exc)),
                    )
                    deletion_jobs_partial += 1

        return {
            "status": "ok",
            "workspace_id": ws_uuid,
            "processed_slices": processed_slices,
            "sync_jobs_processed": sync_jobs_processed,
            "deletion_jobs_processed": deletion_jobs_processed,
            "deletion_jobs_partial": deletion_jobs_partial,
            "sync_job_outcomes": sync_job_outcomes,
            "deletion_job_outcomes": deletion_job_outcomes,
            "shutdown_requested": self.shutdown_event.is_set(),
        }

    def process_recover_pass(
        self,
        workspace_ids: Sequence[str],
        *,
        max_slices: int | None = None,
        outbox_limit: int = 20,
    ) -> dict[str, Any]:
        """Reclaim expired job leases + drain due jobs, deletion_jobs, and outbox (1 bounded pass)."""
        normalized_ids: list[str] = []
        for raw_ws in workspace_ids:
            ws = _normalize_workspace_id(raw_ws)
            if ws is not None and ws not in normalized_ids:
                normalized_ids.append(ws)

        total_reclaimed = 0
        total_slices = 0
        total_sync = 0
        total_del = 0
        total_outbox = 0
        workspace_reports: list[dict[str, Any]] = []

        for ws_uuid in normalized_ids:
            if self.shutdown_event.is_set():
                break
            reclaimed = self.job_runtime.reclaim_expired_leases(ws_uuid)
            total_reclaimed += len(reclaimed)

            slice_report = self.process_workspace_slices(
                ws_uuid,
                max_slices=max_slices,
            )
            total_slices += int(slice_report["processed_slices"])
            total_sync += int(slice_report["sync_jobs_processed"])
            total_del += int(slice_report["deletion_jobs_processed"])

            publisher = _RecoveryOutboxPublisher()
            dispatched = self.job_runtime.dispatch_pending_outbox(
                ws_uuid,
                publisher=publisher,
                limit=outbox_limit,
            )
            total_outbox += len(dispatched)

            # Self-heal any IMPORT_READY run whose post-commit derived unit / candidate pass
            # was interrupted by a previous slice timeout (only when no job is RUNNING).
            derived_resumed = 0
            if int(slice_report["sync_jobs_processed"]) == 0 and not self.shutdown_event.is_set():
                pending_derived_run_ids = self._list_unfinished_derived_runs(ws_uuid, limit=2)
                derived_deadline = time.monotonic() + max(
                    10.0, float(self.config.worker_request_slice_timeout_seconds) - 40.0
                )
                for run_id_to_finish in pending_derived_run_ids:
                    if self.shutdown_event.is_set() or time.monotonic() >= derived_deadline:
                        break
                    self.event_candidate_builder.build_candidates_for_sync_run(
                        workspace_id=ws_uuid,
                        sync_run_id=run_id_to_finish,
                    )
                    self.derived_unit_processor.process_import_ready_run(
                        workspace_id=ws_uuid,
                        sync_run_id=run_id_to_finish,
                        skip_already_ingested=True,
                        deadline_monotonic=derived_deadline,
                    )
                    derived_resumed += 1

            workspace_reports.append(
                {
                    "workspace_id": ws_uuid,
                    "reclaimed_leases": len(reclaimed),
                    "processed_slices": slice_report["processed_slices"],
                    "sync_jobs_processed": slice_report["sync_jobs_processed"],
                    "deletion_jobs_processed": slice_report["deletion_jobs_processed"],
                    "outbox_dispatched": len(dispatched),
                    "derived_runs_resumed": derived_resumed,
                }
            )

        return {
            "status": "ok",
            "workspaces_scanned": len(workspace_reports),
            "reclaimed_leases": total_reclaimed,
            "processed_slices": total_slices,
            "sync_jobs_processed": total_sync,
            "deletion_jobs_processed": total_del,
            "outbox_dispatched": total_outbox,
            "workspaces": workspace_reports,
            "shutdown_requested": self.shutdown_event.is_set(),
        }

    def set_ephemeral_public_github_token(self, token: str) -> None:
        cleaned = token.strip()
        if cleaned and not any(ch.isspace() for ch in cleaned):
            self._ephemeral_public_github_token = cleaned

    def _resolve_github_token(self, secret_ref: str) -> str | None:
        if secret_ref == "hosted:github-public-read:v1":
            return self._ephemeral_public_github_token
        return self.secret_store.get_secret(secret_ref)

    def close(self) -> None:
        self.request_shutdown()
        self.pool.close()


def _install_sigterm_handler(runtime: HostedWorkerRuntime) -> None:
    """Install non-invasive SIGTERM handler when running on main thread."""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)

        def _handle_sigterm(signum: int, frame: Any) -> None:
            runtime.request_shutdown()
            if callable(previous):
                previous(signum, frame)

        signal.signal(signal.SIGTERM, _handle_sigterm)
    except Exception:  # noqa: BLE001
        return


def create_hosted_worker_app(
    config: HostedWorkerServiceConfig,
    *,
    worker_token_verifier: Callable[[str], WorkerIdentity | None] | None = None,
    github_wire_transport: GitHubWireTransportPort | None = None,
    pause_controller: ManualEdgePauseController | None = None,
) -> FastAPI:
    """Create the private IAM Cloud Run Worker FastAPI app (``cm-worker``).

    - Connects ONLY as ``cm_worker_app`` (``NOBYPASSRLS``, ``:6543``, ``prepare_threshold=None``).
    - Allows ONLY ``GET /healthz`` and authenticated ``/internal/worker/**`` routes.
    - Denies ALL ``/v1/**`` routes fail-closed (``allowed_api_prefixes=frozenset()``).
    """
    runtime = HostedWorkerRuntime(
        config,
        github_wire_transport=github_wire_transport,
        pause_controller=pause_controller,
    )

    resolved_verifier: Callable[[str], WorkerIdentity | None]
    if worker_token_verifier is not None:
        resolved_verifier = worker_token_verifier
    elif config.worker_hmac_secret:
        resolved_verifier = HmacWorkerTokenVerifier(secret=config.worker_hmac_secret)
    else:
        resolved_verifier = GoogleOidcWorkerTokenVerifier(
            allowed_worker_audiences=config.allowed_worker_audiences,
            allowed_worker_issuers=config.allowed_worker_issuers,
            allowed_worker_service_accounts=config.allowed_worker_service_accounts,
            jwks_url=config.google_jwks_url,
            allow_http_loopback_for_tests=config.allow_http_loopback_for_tests,
        )

    edge_guard_config = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(require_origin_auth=True, block_direct_run_app_hosts=True),
        worker_auth=WorkerAuthConfig(
            allowed_worker_audiences=config.allowed_worker_audiences,
            allowed_worker_issuers=config.allowed_worker_issuers,
            allowed_worker_service_accounts=config.allowed_worker_service_accounts,
            token_verifier=resolved_verifier,
        ),
        allowed_api_prefixes=frozenset(),
        caps=config.edge_caps,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> Iterator[None]:  # type: ignore[misc]
        _install_sigterm_handler(runtime)
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="Career Memory Hosted Worker", lifespan=_lifespan)
    app.state.hosted_worker_runtime = runtime

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "role": "worker"}

    @app.get("/api/health", include_in_schema=False)
    def api_health() -> dict[str, str]:
        return {"status": "ok", "role": "worker"}

    @app.post("/internal/worker/wake")
    def internal_worker_wake(
        request: Request, payload: WorkerWakeRequest | None = None
    ) -> dict[str, Any]:
        raw_gh_token = (request.headers.get("X-CM-GitHub-Public-Token") or "").strip()
        if raw_gh_token and len(raw_gh_token) <= 512 and not any(ch.isspace() for ch in raw_gh_token):
            runtime.set_ephemeral_public_github_token(raw_gh_token)
        req = payload or WorkerWakeRequest()
        target_ws = req.workspace_id or (
            config.default_workspace_ids[0] if config.default_workspace_ids else None
        )
        if target_ws is None:
            return {
                "status": "ok",
                "workspace_id": None,
                "processed_slices": 0,
                "sync_jobs_processed": 0,
                "deletion_jobs_processed": 0,
                "deletion_jobs_partial": 0,
                "shutdown_requested": runtime.shutdown_event.is_set(),
            }
        return runtime.process_workspace_slices(
            target_ws,
            max_slices=req.max_slices,
        )

    @app.post("/internal/worker/recover")
    def internal_worker_recover(
        request: Request, payload: WorkerRecoverRequest | None = None
    ) -> dict[str, Any]:
        raw_gh_token = (request.headers.get("X-CM-GitHub-Public-Token") or "").strip()
        if raw_gh_token and len(raw_gh_token) <= 512 and not any(ch.isspace() for ch in raw_gh_token):
            runtime.set_ephemeral_public_github_token(raw_gh_token)
        req = payload or WorkerRecoverRequest()
        targets: list[str] = []
        if req.workspace_id:
            targets.append(req.workspace_id)
        targets.extend(req.workspace_ids)
        if not targets:
            targets.extend(config.default_workspace_ids)
        return runtime.process_recover_pass(
            targets,
            max_slices=req.max_slices,
            outbox_limit=req.outbox_limit,
        )

    install_edge_guard(app, edge_guard_config, controller=runtime.pause_controller)
    return app


def create_hosted_worker_app_from_env() -> FastAPI:
    """Build the private IAM Cloud Run Worker app strictly from environment variables."""
    return create_hosted_worker_app(HostedWorkerServiceConfig.from_env())


__all__ = [
    "HostedWorkerRuntime",
    "WorkerRecoverRequest",
    "WorkerWakeRequest",
    "create_hosted_worker_app",
    "create_hosted_worker_app_from_env",
]
