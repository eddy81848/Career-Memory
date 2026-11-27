"""T-005B + T-005C Real PostgreSQL 18 B-004 Fault Harness Runner for all 16 scenarios (S01..S16).

Executes the REAL M2 production initial-import stack against a real disposable PostgreSQL 18
cluster migrated with `0010`..`0050` and `NOBYPASSRLS` runtime roles (`cm_api_app`, `cm_worker_app`).

Zero mock/shadow importer models. Zero external network calls. Zero automatic snapshot overwriting.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.authz import (
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.backfill_engine.models import (
    BackfillStreamName,
    NormalizedPageItem,
    PageCommitRequest,
    PageCommitStatus,
)
from career_memory_api.backfill_engine.pagination_guard import compute_page_fingerprint
from career_memory_api.backfill_engine.postgres_page_committer import (
    PostgresBackfillPageCommitter,
)
from career_memory_api.contracts_api.models import (
    ContractBoundaryError,
    DeletionConfirmAcceptedResponse,
    DeletionConfirmRequest,
    DeletionPreviewResponse,
)
from career_memory_api.contracts_api.router import (
    InMemoryContractBoundaryService,
    create_contracts_router,
)
from career_memory_api.fault_harness.comparator import (
    ComparisonResult,
    compare_logical_snapshots,
    load_expected_snapshot,
)
from career_memory_api.fault_harness.fake_provider import (
    DeterministicFakeCredentialProvider,
    DeterministicFakeGitHubProvider,
    build_10page_canonical_provider,
    build_compact_multipage_provider,
)
from career_memory_api.fault_harness.fault_hooks import (
    B004LifecycleFaultHook,
    CanaryAuditResult,
    CapturingLogHandler,
    InjectedFaultCrash,
    InsidePageTxFaultInjector,
    audit_canaries_and_plan_c_compliance,
)
from career_memory_api.fault_harness.scenario_schema import (
    DEFAULT_OBSERVED_AT_ISO,
    EVIDENCE_BUNDLE_SCHEMA_VERSION,
    EXPECTED_SNAPSHOT_SCHEMA_VERSION,
    SCENARIO_SCHEMA_VERSION,
    FaultInjectionPoint,
    ScenarioSpec,
    validate_scenario_spec,
)
from career_memory_api.fault_harness.scenarios import SCENARIO_SPECS, get_scenario_spec
from career_memory_api.fault_harness.snapshot import (
    GateCheckObservations,
    InvariantViolation,
    LogicalDbSnapshot,
    canonical_json_sha256,
    extract_logical_db_snapshot,
    verify_b004_invariants,
)
from career_memory_api.github_connect import (
    GitHubConnectError,
    PolicyGuardedOperation,
    PostgresGitHubConnectService,
    create_github_connect_router,
)
from career_memory_api.github_provider import (
    GitHubResolverContentFetcher,
    HttpGitHubImportClient,
    InMemoryWireTransport,
    SafeGitHubHttpTransport,
)
from career_memory_api.initial_import import (
    DerivedUnitIngestionProcessor,
    FinalizeHookContext,
    InitialBackfillJobWorker,
    InitialBackfillOrchestrator,
    PageHookContext,
)
from career_memory_api.jobs import JobStatus, PostgresJobRuntime
from career_memory_api.resolver import (
    ExportGatePrecondition,
    GateOperationKind,
    PostgresProvenanceMetadataStore,
    RefetchRequestInput,
    RefetchResolverService,
    ResolutionStatus,
    mount_resolver_routes,
    verify_required_supports_for_gate,
)
from career_memory_api.schema_contracts.enums import SourceObjectType
from career_memory_api.sync_runs import (
    PostgresSyncRunService,
    ProcessingStatus,
    SyncRunApiError,
    SyncRunStatus,
    create_sync_runs_router,
    install_sync_runs_error_handlers,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

_PG18_BIN_DIR = Path("/opt/homebrew/opt/postgresql@18/bin")
FIXED_NOW = datetime.fromisoformat(DEFAULT_OBSERVED_AT_ISO[:-1] + "+00:00")


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pg_bin(name: str) -> str:
    candidate = _PG18_BIN_DIR / name
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(
            f"PostgreSQL 18 binary {name!r} not found in {_PG18_BIN_DIR} or PATH."
        )
    return resolved


class EphemeralB004Pg18Cluster:
    """Self-cleaning local PostgreSQL 18 cluster bound strictly to 127.0.0.1."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        prefix: str = "cm-b004-pg18-",
    ) -> None:
        self._repo_root = (repo_root or _default_repo_root()).resolve()
        self._tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_b004_admin"
        self.dbname = "career_memory_b004"
        self.api_login = "cm_api_login_b004"
        self.api_password = "local_ephemeral_api_pw"
        self.worker_login = "cm_worker_login_b004"
        self.worker_password = "local_ephemeral_worker_pw"
        self.server_version: str = "unknown"

    @property
    def migrations_dir(self) -> Path:
        return self._repo_root / "db" / "migrations"

    @property
    def migration_files(self) -> tuple[Path, ...]:
        return (
            self.migrations_dir / "0010_identity_workspace_source_auth.sql",
            self.migrations_dir / "0020_source_provenance_metadata.sql",
            self.migrations_dir / "0030_events_facts_claims_contributions.sql",
            self.migrations_dir / "0040_sync_runs_jobs_outbox_audit.sql",
            self.migrations_dir / "0050_m2_backfill_observations.sql",
        )

    @property
    def admin_dsn(self) -> str:
        return f"postgresql://{self.superuser}@127.0.0.1:{self.port}/{self.dbname}"

    @property
    def api_dsn(self) -> str:
        return (
            f"postgresql://{self.api_login}:{self.api_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    @property
    def worker_dsn(self) -> str:
        return (
            f"postgresql://{self.worker_login}:{self.worker_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    @contextmanager
    def api_connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.api_dsn) as conn:
            yield conn

    @contextmanager
    def worker_connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.worker_dsn) as conn:
            yield conn

    @contextmanager
    def admin_connection(self, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.admin_dsn, autocommit=autocommit) as conn:
            yield conn

    def start(self) -> None:
        initdb = _pg_bin("initdb")
        pg_ctl = _pg_bin("pg_ctl")
        createdb = _pg_bin("createdb")

        env = os.environ.copy()
        env["LC_ALL"] = "C"

        subprocess.run(
            [
                initdb,
                "-D",
                str(self._pgdata),
                "-U",
                self.superuser,
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        with (self._pgdata / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write(
                f"\nlisten_addresses = '127.0.0.1'\n"
                f"port = {self.port}\n"
                f"unix_socket_directories = '{self._tmpdir}'\n"
                f"fsync = off\n"
                f"synchronous_commit = off\n"
                f"max_connections = 100\n"
            )

        subprocess.run(
            [pg_ctl, "-D", str(self._pgdata), "-l", str(self._logfile), "-w", "start"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        subprocess.run(
            [
                createdb,
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                self.superuser,
                self.dbname,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_dsn, connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SHOW server_version_num")
                        row = cur.fetchone()
                        assert row is not None and int(row[0]) >= 180000
                        cur.execute("SHOW server_version")
                        ver_row = cur.fetchone()
                        self.server_version = str(ver_row[0]).split()[0] if ver_row else "18"
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("Ephemeral PG18 cluster did not become ready within 10s.")

    def apply_all_migrations(self) -> None:
        for path in self.migration_files:
            sql = path.read_text(encoding="utf-8")
            with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)

    def provision_runtime_logins(self) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.api_login}') THEN
                            CREATE ROLE {self.api_login}
                                LOGIN PASSWORD '{self.api_password}'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.worker_login}') THEN
                            CREATE ROLE {self.worker_login}
                                LOGIN PASSWORD '{self.worker_password}'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        END IF;
                    END
                    $$;
                    GRANT cm_api_app TO {self.api_login};
                    GRANT cm_worker_app TO {self.worker_login};
                    """
                )

    def truncate_all(self) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.allow_revision_purge = 'on'")
                cur.execute(
                    """
                    TRUNCATE TABLE
                        public.sync_run_event_promotions,
                        public.sync_run_page_observations,
                        public.sync_commit_pr_associations,
                        public.sync_item_outcomes,
                        public.event_memberships,
                        public.work_events,
                        public.outbox_consumed_effects,
                        public.job_side_effect_ledger,
                        public.audit_events,
                        public.outbox_events,
                        public.api_idempotency_records,
                        public.deletion_jobs,
                        public.jobs,
                        public.sync_checkpoints,
                        public.sync_runs,
                        public.content_units,
                        public.source_object_revisions,
                        public.source_objects,
                        public.source_resources,
                        public.source_accounts,
                        public.workspace_memberships,
                        public.workspaces,
                        public.users
                    CASCADE
                    """
                )

    def stop(self) -> None:
        pg_ctl = _pg_bin("pg_ctl")
        if self._pgdata.exists():
            subprocess.run(
                [pg_ctl, "-D", str(self._pgdata), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class _SingleActorAccessProvider:
    """Minimal WorkspaceAccessProvider for mounting real resolver HTTP route in-process."""

    def __init__(self, access: WorkspaceAccessContext, bearer_token: str) -> None:
        self._access = access
        self._token = bearer_token

    def verify_identity(self, credentials: AuthCredentials) -> AuthenticatedUser | None:
        if credentials.bearer_token == self._token:
            return self._access.user
        return None

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        if user.user_id == self._access.user_id and target_workspace_id == self._access.workspace_id:
            return self._access.membership
        return None


class PostgresDeletionBackedContractService(InMemoryContractBoundaryService):
    """Bridges OD-003 `deletion-preview` / `deletion-confirm` onto real PG18 `deletion_jobs` fence."""

    def __init__(
        self,
        *,
        job_runtime: PostgresJobRuntime,
        now_fn: Any,
    ) -> None:
        super().__init__(now_fn=now_fn)
        self._job_runtime = job_runtime

    def confirm_source_deletion(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        idempotency_key: str,
        request: DeletionConfirmRequest,
    ) -> DeletionConfirmAcceptedResponse:
        accepted = super().confirm_source_deletion(
            access,
            source_resource_id=source_resource_id,
            idempotency_key=idempotency_key,
            request=request,
        )
        del_job = self._job_runtime.request_source_deletion(
            access.workspace_id,
            source_resource_id=source_resource_id,
            requested_by=access.user_id,
            now=self._now_fn(),
        )
        return DeletionConfirmAcceptedResponse(
            deletion_job_id=del_job.id,
            source_resource_id=source_resource_id,
        )


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    """Full execution result for one B-004 scenario."""

    spec: ScenarioSpec
    logical_snapshot: LogicalDbSnapshot
    canary_audit: CanaryAuditResult
    intermediate_violations: tuple[InvariantViolation, ...]
    final_violations: tuple[InvariantViolation, ...]
    comparison: ComparisonResult | None
    baseline_equivalence_matched: bool | None
    notes: tuple[str, ...] = ()

    @property
    def all_violations(self) -> tuple[InvariantViolation, ...]:
        return (*self.intermediate_violations, *self.final_violations)

    @property
    def passed(self) -> bool:
        if self.all_violations:
            return False
        if not self.canary_audit.passed:
            return False
        if self.comparison is not None and not self.comparison.matched:
            return False
        if self.baseline_equivalence_matched is False:
            return False
        return True

    def to_evidence_entry(self) -> dict[str, Any]:
        return {
            "scenario_id": self.spec.scenario_id,
            "title": self.spec.title,
            "requirements": list(self.spec.requirements),
            "default_branch": self.spec.default_branch,
            "injection_points": [p.value for p in self.spec.injection_points],
            "passed": self.passed,
            "logical_snapshot_sha256": self.logical_snapshot.snapshot_sha256,
            "core_convergence_sha256": self.logical_snapshot.core_convergence_sha256,
            "baseline_equivalence_target": self.spec.baseline_equivalence_target,
            "baseline_equivalence_matched": self.baseline_equivalence_matched,
            "expected_snapshot_matched": (
                self.comparison.matched if self.comparison is not None else None
            ),
            "expected_snapshot_diff_count": (
                len(self.comparison.diffs) if self.comparison is not None else 0
            ),
            "invariant_violation_count": len(self.all_violations),
            "canary_audit": self.canary_audit.to_dict(),
            "summary_counts": {
                "sync_runs": len(self.logical_snapshot.data["sync_runs"]),
                "source_objects": len(self.logical_snapshot.data["source_objects"]),
                "source_object_revisions": len(self.logical_snapshot.data["source_object_revisions"]),
                "work_events": len(self.logical_snapshot.data["work_events"]),
                "event_memberships": self.logical_snapshot.data["event_memberships_count"],
                "content_units": len(self.logical_snapshot.data["content_units"]),
                "import_ready_outbox": self.logical_snapshot.data["outbox_summary"][
                    "import_ready_count"
                ],
                "outbox_consumed_effects": self.logical_snapshot.data["outbox_summary"][
                    "outbox_consumed_effects_count"
                ],
            },
            "review_comment_drift_note": self.spec.review_comment_drift_note,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class _ScenarioStack:
    access: WorkspaceAccessContext
    bearer_token: str
    secret_store: InMemorySecretStore
    connect_service: PostgresGitHubConnectService
    resource_id: str
    provider: DeterministicFakeGitHubProvider
    transport: SafeGitHubHttpTransport
    github_client: HttpGitHubImportClient
    content_fetcher: GitHubResolverContentFetcher
    sync_runs_service: PostgresSyncRunService
    job_runtime: PostgresJobRuntime
    page_committer: PostgresBackfillPageCommitter
    orchestrator: InitialBackfillOrchestrator
    derived_processor: DerivedUnitIngestionProcessor
    worker: InitialBackfillJobWorker
    app: Any
    hook: B004LifecycleFaultHook
    captured_api_texts: list[str] = field(default_factory=list)


class B004PostgresScenarioRunner:
    """Executes all 16 B-004 scenarios on real PostgreSQL 18 + real M2 production services."""

    def __init__(
        self,
        cluster: EphemeralB004Pg18Cluster,
        *,
        expected_snapshots_dir: Path | None = None,
    ) -> None:
        self._cluster = cluster
        self._expected_snapshots_dir = expected_snapshots_dir

    def _seed_workspace(self, *, label: str) -> WorkspaceAccessContext:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                        VALUES (%s::uuid, 'https://auth.career-memory.local/v1', %s, %s, %s)
                        """,
                        (user_id, f"sub-{user_id}", f"{user_id}@local.test", f"B004 {label}"),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.workspaces (id, owner_user_id, name)
                        VALUES (%s::uuid, %s::uuid, %s)
                        """,
                        (workspace_id, user_id, f"B004 Workspace {label}"),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                        VALUES (%s::uuid, %s::uuid, 'OWNER')
                        """,
                        (workspace_id, user_id),
                    )
        return WorkspaceAccessContext(
            user=AuthenticatedUser(
                user_id=user_id,
                auth_subject=AuthSubject(
                    issuer="https://auth.career-memory.local/v1",
                    subject=f"sub-{user_id}",
                ),
            ),
            membership=WorkspaceMembership(
                workspace_id=workspace_id,
                user_id=user_id,
                role="OWNER",
            ),
        )

    def _build_stack(
        self,
        provider: DeterministicFakeGitHubProvider,
        *,
        hook: B004LifecycleFaultHook | None = None,
        page_fault_injector: Any = None,
        after_lock_hook: Any = None,
    ) -> _ScenarioStack:
        active_hook = hook or B004LifecycleFaultHook()
        secret_store = InMemorySecretStore()
        access = self._seed_workspace(label=provider.scenario_id)
        bearer_token = f"b004-session-token-{provider.scenario_id.lower()}"

        connect_service = PostgresGitHubConnectService(
            connection_factory=self._cluster.api_connection,
            credential_provider=DeterministicFakeCredentialProvider(provider),
            secret_store=secret_store,
            allowed_redirect_uris=("https://app.career-memory.local/oauth/callback",),
            clock=lambda: FIXED_NOW,
        )
        start_res = connect_service.start_oauth(
            access,
            return_path="/settings/sources",
            redirect_uri="https://app.career-memory.local/oauth/callback",
        )
        cb_res = connect_service.complete_oauth_callback(
            access,
            idempotency_key=str(uuid.uuid4()),
            state=start_res.state,
            code=f"gh-code-{provider.scenario_id.lower()}",
            redirect_uri="https://app.career-memory.local/oauth/callback",
            repository_full_name=provider.full_name,
        )
        assert cb_res.source_resource is not None
        resource_id = cb_res.source_resource.id

        wire = InMemoryWireTransport(provider.handle_wire_request)
        transport = SafeGitHubHttpTransport(
            wire_transport=wire,
            secret_store=secret_store,
            clock=lambda: FIXED_NOW,
        )
        github_client = HttpGitHubImportClient(transport=transport, clock=lambda: FIXED_NOW)
        content_fetcher = GitHubResolverContentFetcher(
            transport=transport, require_credentials=True
        )
        sync_runs_service = PostgresSyncRunService(
            connection_factory=self._cluster.api_connection,
            worker_connection_factory=self._cluster.worker_connection,
            clock=lambda: FIXED_NOW,
        )
        job_runtime = PostgresJobRuntime(connection_factory=self._cluster.worker_connection)
        page_committer = PostgresBackfillPageCommitter(
            worker_connection_factory=self._cluster.worker_connection,
            clock=lambda: FIXED_NOW,
            fault_injector=page_fault_injector,
            after_lock_hook=after_lock_hook,
        )
        orchestrator = InitialBackfillOrchestrator(
            worker_connection_factory=self._cluster.worker_connection,
            github_client=github_client,
            page_committer=page_committer,
            github_connect_service=connect_service,
            lifecycle_hook=active_hook,
            clock=lambda: FIXED_NOW,
        )
        derived_processor = DerivedUnitIngestionProcessor(
            worker_connection_factory=self._cluster.worker_connection,
            content_fetcher=content_fetcher,
            clock=lambda: FIXED_NOW,
        )
        worker = InitialBackfillJobWorker(
            job_runtime=job_runtime,
            orchestrator=orchestrator,
            derived_unit_processor=derived_processor,
            clock=lambda: FIXED_NOW,
        )
        contracts_svc = PostgresDeletionBackedContractService(
            job_runtime=job_runtime,
            now_fn=lambda: FIXED_NOW,
        )
        app = create_app(
            access_provider=_SingleActorAccessProvider(access, bearer_token),
            sync_runs_router=create_sync_runs_router(
                service=sync_runs_service, resolve_access=lambda _req: access
            ),
            github_connect_router=create_github_connect_router(
                service=connect_service, resolve_access=lambda _req: access
            ),
            contracts_router=create_contracts_router(
                service=contracts_svc, resolve_access=lambda _req: access
            ),
            install_github_connect_handlers=True,
            install_contract_handlers=True,
        )
        install_sync_runs_error_handlers(app)

        return _ScenarioStack(
            access=access,
            bearer_token=bearer_token,
            secret_store=secret_store,
            connect_service=connect_service,
            resource_id=resource_id,
            provider=provider,
            transport=transport,
            github_client=github_client,
            content_fetcher=content_fetcher,
            sync_runs_service=sync_runs_service,
            job_runtime=job_runtime,
            page_committer=page_committer,
            orchestrator=orchestrator,
            derived_processor=derived_processor,
            worker=worker,
            app=app,
            hook=active_hook,
        )

    def _find_first_pr_content_unit_id(
        self, workspace_id: str, *, pr_number: str | None = None
    ) -> str | None:
        with self._cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                if pr_number is not None:
                    cur.execute(
                        """
                        SELECT cu.id::text
                        FROM public.content_units cu
                        JOIN public.source_object_revisions rev ON rev.id = cu.source_object_revision_id
                        JOIN public.source_objects so ON so.id = rev.source_object_id
                        WHERE cu.workspace_id = %s::uuid
                          AND so.object_type = 'PR'
                          AND so.provider_object_id = %s
                        ORDER BY cu.ordinal ASC
                        LIMIT 1
                        """,
                        (workspace_id, pr_number),
                    )
                else:
                    cur.execute(
                        """
                        SELECT cu.id::text
                        FROM public.content_units cu
                        JOIN public.source_object_revisions rev ON rev.id = cu.source_object_revision_id
                        JOIN public.source_objects so ON so.id = rev.source_object_id
                        WHERE cu.workspace_id = %s::uuid
                          AND so.object_type = 'PR'
                        ORDER BY so.provider_object_id ASC, cu.ordinal ASC
                        LIMIT 1
                        """,
                        (workspace_id,),
                    )
                row = cur.fetchone()
                return str(row[0]) if row is not None else None

    def _check_policy_gates(
        self,
        stack: _ScenarioStack,
        *,
        content_unit_id: str | None,
        sync_run_id: str | None = None,
        check_http_viewer: bool = True,
        project_resolver_status: bool = False,
        concurrent_stale_fence_discarded: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> GateCheckObservations:
        # 1. External AI & Export & Raw Refetch resource policy checks via real PostgresGitHubConnectService
        ai_allowed = False
        export_policy_allowed = False
        refetch_policy_allowed = False
        try:
            stack.connect_service.assert_resource_processing_allowed(
                stack.access,
                source_resource_id=stack.resource_id,
                operation=PolicyGuardedOperation.AI_TRANSMISSION,
            )
            ai_allowed = True
        except (GitHubConnectError, WorkspaceResourceNotFoundError):
            ai_allowed = False

        try:
            stack.connect_service.assert_resource_processing_allowed(
                stack.access,
                source_resource_id=stack.resource_id,
                operation=PolicyGuardedOperation.EXPORT,
            )
            export_policy_allowed = True
        except (GitHubConnectError, WorkspaceResourceNotFoundError):
            export_policy_allowed = False

        try:
            stack.connect_service.assert_resource_processing_allowed(
                stack.access,
                source_resource_id=stack.resource_id,
                operation=PolicyGuardedOperation.RAW_REFETCH,
            )
            refetch_policy_allowed = True
        except (GitHubConnectError, WorkspaceResourceNotFoundError):
            refetch_policy_allowed = False

        refetch_allowed: bool | None = refetch_policy_allowed if content_unit_id is None else None
        refetch_status: str | None = None
        claim_approval_allowed: bool | None = None
        export_gate_allowed: bool | None = export_policy_allowed

        if content_unit_id is not None:
            with self._cluster.api_connection() as api_conn:
                resolver = RefetchResolverService(
                    metadata_store=PostgresProvenanceMetadataStore(api_conn),
                    fetcher=stack.content_fetcher,
                    clock=lambda: FIXED_NOW,
                )
                if project_resolver_status and sync_run_id is not None:
                    proc_with_resolver = DerivedUnitIngestionProcessor(
                        worker_connection_factory=self._cluster.worker_connection,
                        content_fetcher=stack.content_fetcher,
                        resolver_service=resolver,
                        clock=lambda: FIXED_NOW,
                    )
                    res_st, _proj = proc_with_resolver.verify_unit_and_project_status(
                        access=stack.access,
                        sync_run_id=sync_run_id,
                        content_unit_id=content_unit_id,
                        now=FIXED_NOW,
                    )
                    refetch_status = res_st.value

                # Live approval & export gates via verify_required_supports_for_gate
                with resolver.open_operation_session(
                    stack.access, operation_id=f"b004-gate-{stack.provider.scenario_id}"
                ) as session:
                    try:
                        app_gate = verify_required_supports_for_gate(
                            session,
                            [content_unit_id],
                            operation_kind=GateOperationKind.APPROVAL,
                            resolver=resolver,
                            clock=lambda: FIXED_NOW,
                        )
                        claim_approval_allowed = app_gate.allowed
                        if refetch_status is None and app_gate.resolutions:
                            refetch_status = app_gate.resolutions[0].status.value
                    except WorkspaceResourceNotFoundError:
                        claim_approval_allowed = False
                        if refetch_status is None:
                            refetch_status = "POLICY_BLOCKED"

                    try:
                        exp_gate = verify_required_supports_for_gate(
                            session,
                            [content_unit_id],
                            operation_kind=GateOperationKind.EXPORT_DOWNLOAD,
                            resolver=resolver,
                            export_precondition=ExportGatePrecondition(
                                claim_version_approved=True,
                                approved_claim_version=1,
                                requested_claim_version=1,
                            ),
                            clock=lambda: FIXED_NOW,
                        )
                        export_gate_allowed = bool(export_policy_allowed and exp_gate.allowed)
                    except WorkspaceResourceNotFoundError:
                        export_gate_allowed = False

                if check_http_viewer:
                    viewer_app = create_app(
                        access_provider=_SingleActorAccessProvider(
                            stack.access, stack.bearer_token
                        ),
                    )
                    mount_resolver_routes(viewer_app, resolver=resolver)
                    with TestClient(viewer_app) as vclient:
                        vresp = vclient.get(
                            f"/v1/content-units/{content_unit_id}/content",
                            headers={
                                "Authorization": f"Bearer {stack.bearer_token}",
                                "X-Workspace-ID": stack.access.workspace_id,
                            },
                        )
                        refetch_allowed = vresp.status_code == 200
                        if vresp.status_code != 200:
                            # Error responses MUST NEVER leak raw/secret canaries!
                            stack.captured_api_texts.append(vresp.text)
                        else:
                            # Authorized 200 ephemeral viewer MUST still never leak OAuth token!
                            assert stack.provider.canaries.oauth_token not in vresp.text
                            assert "no-store" in vresp.headers.get("cache-control", "").lower()

        return GateCheckObservations(
            refetch_content_allowed=refetch_allowed,
            refetch_resolution_status=refetch_status,
            external_ai_gate_allowed=ai_allowed,
            export_gate_allowed=export_gate_allowed,
            claim_approval_gate_allowed=claim_approval_allowed,
            concurrent_stale_fence_discarded=concurrent_stale_fence_discarded,
            extra=extra,
        )

    def _create_run_http(
        self,
        stack: _ScenarioStack,
        *,
        backfill_from: str = "2026-08-01",
        limits: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_status: int = 202,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_resource_id": stack.resource_id,
            "mode": "INITIAL_BACKFILL",
            "backfill_from": backfill_from,
        }
        if limits is not None:
            payload["limits"] = limits
        with TestClient(stack.app) as client:
            resp = client.post(
                "/v1/sync-runs",
                headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
                json=payload,
            )
            stack.captured_api_texts.append(resp.text)
            assert resp.status_code == expected_status, (
                f"Expected HTTP {expected_status}, got {resp.status_code}: {resp.text}"
            )
            return resp.json() if resp.content else {}

    def _retry_run_http(
        self,
        stack: _ScenarioStack,
        *,
        run_id: str,
        expected_run_version: int,
        expected_status: int = 202,
    ) -> dict[str, Any]:
        with TestClient(stack.app) as client:
            resp = client.post(
                f"/v1/sync-runs/{run_id}/retry",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={"expected_run_version": expected_run_version},
            )
            stack.captured_api_texts.append(resp.text)
            assert resp.status_code == expected_status, (
                f"Expected retry HTTP {expected_status}, got {resp.status_code}: {resp.text}"
            )
            return resp.json() if resp.content else {}

    def _get_run_status_http(self, stack: _ScenarioStack, run_id: str) -> dict[str, Any]:
        with TestClient(stack.app) as client:
            resp = client.get(f"/v1/sync-runs/{run_id}")
            stack.captured_api_texts.append(resp.text)
            assert resp.status_code == 200, f"GET /v1/sync-runs/{run_id} failed: {resp.text}"
            return resp.json()

    def run_scenario(
        self,
        scenario_id: str,
        *,
        baseline_snapshot: LogicalDbSnapshot | None = None,
    ) -> ScenarioRunResult:
        """Run one B-004 scenario end-to-end on a clean PostgreSQL 18 state."""
        spec = get_scenario_spec(scenario_id)
        validate_scenario_spec(spec)
        self._cluster.truncate_all()

        log_handler = CapturingLogHandler()
        cm_logger = logging.getLogger("career_memory_api")
        cm_logger.addHandler(log_handler)
        try:
            dispatch = getattr(self, f"_run_{spec.scenario_id.lower()}")
            (
                workspace_id,
                source_resource_id,
                sync_run_id,
                canaries_to_scan,
                api_texts,
                intermediate_violations,
                gate_checks,
                duplicate_race_writes,
                notes,
            ) = dispatch(spec)

            with self._cluster.admin_connection() as admin_conn:
                canary_audit = audit_canaries_and_plan_c_compliance(
                    admin_conn,
                    canaries=canaries_to_scan,
                    log_records=log_handler.records,
                    api_response_texts=api_texts,
                )
                logical_snapshot = extract_logical_db_snapshot(
                    admin_conn,
                    workspace_id=workspace_id,
                    source_resource_id=source_resource_id,
                    sync_run_id=sync_run_id,
                    gate_checks=gate_checks,
                )
                final_violations = verify_b004_invariants(
                    admin_conn,
                    workspace_id=workspace_id,
                    phase="final",
                    canary_audit=canary_audit,
                    gate_checks=gate_checks,
                    duplicate_race_writes=duplicate_race_writes,
                    source_resource_id=source_resource_id,
                )
        finally:
            cm_logger.removeHandler(log_handler)

        comparison: ComparisonResult | None = None
        if self._expected_snapshots_dir is not None:
            snap_path = self._expected_snapshots_dir / f"{spec.scenario_id}.json"
            expected_doc = load_expected_snapshot(snap_path)
            comparison = compare_logical_snapshots(
                scenario_id=spec.scenario_id,
                injection_points=spec.injection_points,
                expected=expected_doc["logical_snapshot"],
                actual=logical_snapshot.data,
            )

        baseline_equiv: bool | None = None
        if spec.baseline_equivalence_target is not None and baseline_snapshot is not None:
            baseline_equiv = (
                logical_snapshot.core_convergence_sha256
                == baseline_snapshot.core_convergence_sha256
            )

        return ScenarioRunResult(
            spec=spec,
            logical_snapshot=logical_snapshot,
            canary_audit=canary_audit,
            intermediate_violations=tuple(intermediate_violations),
            final_violations=tuple(final_violations),
            comparison=comparison,
            baseline_equivalence_matched=baseline_equiv,
            notes=tuple(notes),
        )

    def run_all(self) -> list[ScenarioRunResult]:
        """Run all 16 B-004 scenarios S01..S16 in canonical order with S01 baseline comparison."""
        results: list[ScenarioRunResult] = []
        s01_snapshot: LogicalDbSnapshot | None = None
        for spec in SCENARIO_SPECS:
            res = self.run_scenario(
                spec.scenario_id,
                baseline_snapshot=s01_snapshot if spec.baseline_equivalence_target == "S01" else None,
            )
            if spec.scenario_id == "S01":
                s01_snapshot = res.logical_snapshot
            results.append(res)
        return results

    # =========================================================================
    # S01: 10-page clean initial import completion (baseline)
    # =========================================================================
    def _run_s01(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_10page_canonical_provider(
            scenario_id="S01", canary_id="S01", default_branch=spec.default_branch
        )
        stack = self._build_stack(provider)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01", limits={"max_objects": 500})
        run_id = receipt["target_id"]

        step = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s01-worker",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step is not None
        assert step.execution.status is SyncRunStatus.SUCCESS
        assert step.execution.import_ready is True
        assert step.derived_units is not None
        assert step.derived_units.processing_status is ProcessingStatus.COMPLETE

        unit_id = self._find_first_pr_content_unit_id(stack.access.workspace_id, pr_number="101")
        assert unit_id is not None
        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=unit_id,
            sync_run_id=run_id,
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            [],
            gate_checks,
            0,
            ["Baseline 10-page clean import on default_branch='trunk'."],
        )

    # =========================================================================
    # S02: 8/10 page transient 503 failure -> safe checkpoint resume == S01
    # =========================================================================
    def _run_s02(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_10page_canonical_provider(
            scenario_id="S02", canary_id="S01", default_branch=spec.default_branch
        )
        provider.status_fault_queues[f"/repositories/{provider.repo_id}/pulls?page=8"] = [503]
        stack = self._build_stack(provider)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01", limits={"max_objects": 500})
        run_id = receipt["target_id"]

        # Attempt 1 -> stops at PRS page 8 with last_committed_page == 7
        step1 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s02-w1",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step1 is not None
        assert step1.execution.status is SyncRunStatus.PARTIAL
        assert step1.execution.retry_allowed is True
        assert step1.execution.has_safe_checkpoint is True
        assert step1.derived_units is None

        cp_mid = stack.page_committer.get_stream_checkpoint(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            stream_name=BackfillStreamName.PRS,
        )
        assert cp_mid is not None and cp_mid.last_committed_page == 7

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack.access.workspace_id,
                    phase="s02_after_page8_503",
                    source_resource_id=stack.resource_id,
                )
            )

        # Capture request counts for pages 1..7 before resume
        p1_to_7_before = {
            p: provider.request_counts[f"/repositories/{provider.repo_id}/pulls?page={p}"]
            for p in range(1, 8)
        }
        assert all(v == 1 for v in p1_to_7_before.values())

        st1 = self._get_run_status_http(stack, run_id)
        self._retry_run_http(stack, run_id=run_id, expected_run_version=st1["run_version"])

        step2 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s02-w2",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step2 is not None
        assert step2.execution.status is SyncRunStatus.SUCCESS
        assert step2.execution.resumed_from_checkpoint is True

        # Pages 1..7 fetched strictly 1 (initial) + 1 (2nd-pass completion gate) = 2 total (0 resume re-fetches)
        p1_to_7_after = {
            p: provider.request_counts[f"/repositories/{provider.repo_id}/pulls?page={p}"]
            for p in range(1, 8)
        }
        assert all(v == 2 for v in p1_to_7_after.values())

        unit_id = self._find_first_pr_content_unit_id(stack.access.workspace_id, pr_number="101")
        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=unit_id,
            sync_run_id=run_id,
            extra={
                "partial_last_committed_page": 7,
                "pages_1_to_7_resume_refetches": 0,
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            mid_violations,
            gate_checks,
            0,
            ["PRS page 8/10 returned 503; resumed from page 8 with 0 re-fetches of pages 1..7."],
        )

    # =========================================================================
    # S03: Crash inside PostgreSQL page TX before COMMIT -> rollback -> resume == S01
    # =========================================================================
    def _run_s03(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_10page_canonical_provider(
            scenario_id="S03", canary_id="S01", default_branch=spec.default_branch
        )
        hook = B004LifecycleFaultHook()
        tx_injector = InsidePageTxFaultInjector(
            hook=hook,
            target_stream=BackfillStreamName.PRS,
            target_page=5,
            target_stage="after_checkpoint_update",
        )
        stack = self._build_stack(provider, hook=hook, page_fault_injector=tx_injector)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01", limits={"max_objects": 500})
        run_id = receipt["target_id"]

        crashed = False
        try:
            stack.worker.claim_and_execute_next(
                stack.access.workspace_id,
                worker_id="b004-s03-w1",
                access=stack.access,
                run_derived_units=True,
                now=FIXED_NOW,
            )
        except InjectedFaultCrash as exc:
            assert exc.point is FaultInjectionPoint.INSIDE_PAGE_TX_BEFORE_COMMIT
            crashed = True
        assert crashed and tx_injector.triggered_count == 1

        # Verify PostgreSQL rolled back ALL page-5 writes (PR #106 on page 5 MUST NOT exist!)
        cp_after_crash = stack.page_committer.get_stream_checkpoint(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            stream_name=BackfillStreamName.PRS,
        )
        assert cp_after_crash is not None and cp_after_crash.last_committed_page == 4

        with self._cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.source_objects
                    WHERE workspace_id = %s::uuid AND object_type = 'PR' AND provider_object_id = '106'
                    """,
                    (stack.access.workspace_id,),
                )
                assert int(cur.fetchone()[0]) == 0
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.sync_run_page_observations
                    WHERE workspace_id = %s::uuid AND sync_run_id = %s::uuid
                      AND stream_name = 'PRS' AND page_number = 5
                    """,
                    (stack.access.workspace_id, run_id),
                )
                assert int(cur.fetchone()[0]) == 0

            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack.access.workspace_id,
                    phase="s03_after_inside_tx_rollback",
                    source_resource_id=stack.resource_id,
                )
            )

        # Simulate lease expiry + worker reclaim at FIXED_NOW + 120s
        step2 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s03-w2",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW + timedelta(seconds=120),
        )
        assert step2 is not None
        assert step2.execution.status is SyncRunStatus.SUCCESS
        assert step2.execution.resumed_from_checkpoint is True

        unit_id = self._find_first_pr_content_unit_id(stack.access.workspace_id, pr_number="101")
        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=unit_id,
            sync_run_id=run_id,
            extra={
                "rolled_back_page": 5,
                "post_rollback_last_committed_page": 4,
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            mid_violations,
            gate_checks,
            0,
            ["PG18 rolled back PRS page 5 atomically on pre-COMMIT crash; reclaimed worker converged to S01."],
        )

    # =========================================================================
    # S04: Crash after PostgreSQL page COMMIT before worker ack -> resume == S01
    # =========================================================================
    def _run_s04(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_10page_canonical_provider(
            scenario_id="S04", canary_id="S01", default_branch=spec.default_branch
        )
        crashed_flag = [False]

        def _crash_after_page6(ctx: PageHookContext) -> None:
            if (
                not crashed_flag[0]
                and ctx.stream_name is BackfillStreamName.PRS
                and ctx.page_number == 6
            ):
                crashed_flag[0] = True
                raise InjectedFaultCrash(
                    point=FaultInjectionPoint.AFTER_PAGE_COMMIT_BEFORE_ACK,
                    stream_name=ctx.stream_name,
                    page_number=ctx.page_number,
                    message="Simulated crash after PRS page 6 DB COMMIT and before worker ack.",
                )

        hook = B004LifecycleFaultHook(on_after_page=_crash_after_page6)
        stack = self._build_stack(provider, hook=hook)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01", limits={"max_objects": 500})
        run_id = receipt["target_id"]

        crashed = False
        try:
            stack.worker.claim_and_execute_next(
                stack.access.workspace_id,
                worker_id="b004-s04-w1",
                access=stack.access,
                run_derived_units=True,
                now=FIXED_NOW,
            )
        except InjectedFaultCrash as exc:
            assert exc.point is FaultInjectionPoint.AFTER_PAGE_COMMIT_BEFORE_ACK
            crashed = True
        assert crashed

        # Page 6 IS committed in PG18 even though worker died before heartbeat/ack!
        cp_after_crash = stack.page_committer.get_stream_checkpoint(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            stream_name=BackfillStreamName.PRS,
        )
        assert cp_after_crash is not None and cp_after_crash.last_committed_page == 6

        # Prove a stale retry attempting to re-commit page 6 with old version 5 is discarded
        stale_retry = stack.page_committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=stack.access.workspace_id,
                sync_run_id=run_id,
                source_resource_id=stack.resource_id,
                stream_name=BackfillStreamName.PRS,
                page_number=6,
                expected_checkpoint_version=5,
                expected_cursor=None,
                next_cursor="page:7",
                page_fingerprint=compute_page_fingerprint(BackfillStreamName.PRS, (), "page:7"),
                items=(),
            ),
            now=FIXED_NOW,
        )
        assert stale_retry.status is PageCommitStatus.STALE_CHECKPOINT_VERSION

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack.access.workspace_id,
                    phase="s04_after_commit_before_ack_crash",
                    source_resource_id=stack.resource_id,
                )
            )

        # Lease expires -> worker 2 reclaims job and resumes from PRS page 7 cleanly
        step2 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s04-w2",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW + timedelta(seconds=120),
        )
        assert step2 is not None
        assert step2.execution.status is SyncRunStatus.SUCCESS
        assert step2.execution.resumed_from_checkpoint is True

        unit_id = self._find_first_pr_content_unit_id(stack.access.workspace_id, pr_number="101")
        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=unit_id,
            sync_run_id=run_id,
            concurrent_stale_fence_discarded=1,
            extra={
                "committed_page_before_crash": 6,
                "stale_page6_recommit_discarded": True,
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            mid_violations,
            gate_checks,
            0,
            ["Post-COMMIT pre-ack crash preserved page 6; stale recommit discarded; converged to S01."],
        )

    # =========================================================================
    # S05: Concurrent duplicate POST /v1/sync-runs + 2-worker checkpoint race
    # =========================================================================
    def _run_s05(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_compact_multipage_provider(
            scenario_id="S05", default_branch=spec.default_branch
        )
        stack = self._build_stack(provider)

        # Part A: Two concurrent create_initial_import calls with distinct Idempotency-Keys
        barrier_req = threading.Barrier(2)
        req_outcomes: list[tuple[str, Any]] = []
        req_lock = threading.Lock()

        def _invoke_create(idem_key: str) -> None:
            barrier_req.wait(timeout=5.0)
            try:
                rec = stack.sync_runs_service.create_initial_import(
                    stack.access,
                    idempotency_key=idem_key,
                    request_payload={
                        "source_resource_id": stack.resource_id,
                        "mode": "INITIAL_BACKFILL",
                        "backfill_from": "2026-08-01",
                    },
                )
                with req_lock:
                    req_outcomes.append(("ACCEPTED", rec.target_id))
            except SyncRunApiError as err:
                with req_lock:
                    req_outcomes.append(("CONFLICT", (err.status_code, err.code)))

        k1, k2 = str(uuid.uuid4()), str(uuid.uuid4())
        t1 = threading.Thread(target=_invoke_create, args=(k1,))
        t2 = threading.Thread(target=_invoke_create, args=(k2,))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        accepted = [val for kind, val in req_outcomes if kind == "ACCEPTED"]
        conflicts = [val for kind, val in req_outcomes if kind == "CONFLICT"]
        assert len(accepted) == 1
        assert len(conflicts) == 1 and conflicts[0] == (409, "SYNC_ALREADY_RUNNING")
        run_id = accepted[0]

        # Idempotent replay of the winning key returns 202 with same target_id
        winning_key = k1 if any(k == "ACCEPTED" for k, _ in req_outcomes[:1]) else k2
        # Verify whichever key succeeded replays cleanly
        replayed_id = None
        for cand_key in (k1, k2):
            try:
                rec = stack.sync_runs_service.create_initial_import(
                    stack.access,
                    idempotency_key=cand_key,
                    request_payload={
                        "source_resource_id": stack.resource_id,
                        "mode": "INITIAL_BACKFILL",
                        "backfill_from": "2026-08-01",
                    },
                )
                replayed_id = rec.target_id
                break
            except SyncRunApiError:
                continue
        assert replayed_id == run_id

        # Part B: Two workers read checkpoint_version=0 for PRS and race commit_page_batch
        # First let real orchestrator freeze plan & execute PRS page 1? Even better:
        # We run a controlled 2-thread race on PRS page 1 using the real PR #203 item from provider,
        # OR even cleaner: use two PostgresBackfillPageCommitter instances racing on PRS page 1
        # right after plan_spec is persisted, then let orchestrator resume cleanly from page 2!
        snap_start = stack.github_client.fetch_repository_snapshot(
            owner=provider.owner,
            repo=provider.repo,
            token_secret_ref=stack.connect_service.resolve_active_credential_for_resource(
                stack.access, source_resource_id=stack.resource_id
            ).source_resource.source_account_id
            if False
            else None,
            provider_resource_id=provider.repo_id,
        )
        # Execute real page 1 fetch via HttpGitHubImportClient + build frozen plan
        from career_memory_api.github_provider.plan import build_frozen_import_plan
        from career_memory_api.initial_import.orchestrator import (
            _revision_to_envelope_item,
            _serialize_cue,
        )

        plan = build_frozen_import_plan(
            source_resource_id=stack.resource_id,
            repo_snapshot=snap_start,
            cutoff_at=FIXED_NOW,
            backfill_from="2026-08-01",
        )
        stack.orchestrator._mark_run_running(  # noqa: SLF001
            stack.access.workspace_id,
            run_id,
            plan_spec=plan.to_plan_spec_dict(),
            ts=FIXED_NOW,
        )
        p1 = stack.github_client.fetch_merged_pull_requests_page(plan, page=1)
        p1_item_dict = _revision_to_envelope_item(p1.items[0])
        p1_item = NormalizedPageItem(
            object_type=SourceObjectType.PR,
            provider_object_id=p1_item_dict["provider_object_id"],
            content_hash=p1_item_dict["content_hash"],
            normalized_metadata=p1_item_dict["normalized_metadata"],
            hash_policy_version=p1_item_dict["hash_policy_version"],
            connector_version=p1_item_dict["connector_version"],
            schema_version=p1_item_dict["schema_version"],
            provider_revision_id=p1_item_dict["provider_revision_id"],
            provider_updated_at=p1_item_dict["provider_updated_at"],
            observed_at=p1_item_dict["observed_at"],
        )
        next_cur = p1.next_url or "page:2"
        p1_fp = compute_page_fingerprint(BackfillStreamName.PRS, (p1_item,), next_cur)
        race_req = PageCommitRequest(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            source_resource_id=stack.resource_id,
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=next_cur,
            page_fingerprint=p1_fp,
            items=(p1_item,),
            stream_completed=False,
            cursor_metadata_extra={
                "merged_pr_cues": [_serialize_cue(c) for c in p1.cues],
                "referenced_issue_numbers": [],
                "safeguard_requests_total": 1,
                "safeguard_bytes_total": p1.bytes_read,
            },
        )

        barrier_race = threading.Barrier(2)
        race_results: list[PageCommitStatus] = []
        race_lock = threading.Lock()

        def _worker_race() -> None:
            committer = PostgresBackfillPageCommitter(
                worker_connection_factory=self._cluster.worker_connection,
                clock=lambda: FIXED_NOW,
            )
            barrier_race.wait(timeout=5.0)
            res = committer.commit_page_batch(race_req, now=FIXED_NOW)
            with race_lock:
                race_results.append(res.status)

        w1 = threading.Thread(target=_worker_race)
        w2 = threading.Thread(target=_worker_race)
        w1.start()
        w2.start()
        w1.join(timeout=10.0)
        w2.join(timeout=10.0)

        committed_cnt = race_results.count(PageCommitStatus.COMMITTED)
        stale_cnt = race_results.count(PageCommitStatus.STALE_CHECKPOINT_VERSION)
        assert committed_cnt == 1 and stale_cnt == 1, f"Unexpected race outcomes: {race_results}"

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack.access.workspace_id,
                    phase="s05_after_2worker_checkpoint_race",
                    duplicate_race_writes=max(0, committed_cnt - 1),
                    source_resource_id=stack.resource_id,
                )
            )

        # Now let the real worker finish remaining pages (PRS page 2..3 + remaining streams) to SUCCESS!
        step = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s05-finisher",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step is not None and step.execution.status is SyncRunStatus.SUCCESS

        unit_id = self._find_first_pr_content_unit_id(stack.access.workspace_id, pr_number="201")
        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=unit_id,
            sync_run_id=run_id,
            concurrent_stale_fence_discarded=stale_cnt,
            extra={
                "concurrent_create_accepted": 1,
                "concurrent_create_409_sync_already_running": 1,
                "idempotent_replay_matched": True,
                "checkpoint_race_committed": committed_cnt,
                "checkpoint_race_stale_discarded": stale_cnt,
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            mid_violations,
            gate_checks,
            max(0, committed_cnt - 1),
            ["Concurrent create -> 1x 202 + 1x 409 SYNC_ALREADY_RUNNING; 2-worker checkpoint race -> 1 COMMITTED + 1 STALE_CHECKPOINT_VERSION."],
        )

    # =========================================================================
    # S06: HTTP 200 Content-Length truncated partial response + missing required fields
    # =========================================================================
    def _run_s06(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        # Sub-case A: HTTP 200 Content-Length truncation on PRS page 2
        provider = build_compact_multipage_provider(
            scenario_id="S06", default_branch=spec.default_branch
        )
        provider.truncated_content_length_keys.add(f"/repositories/{provider.repo_id}/pulls?page=2")
        stack = self._build_stack(provider)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01")
        run_id = receipt["target_id"]

        step = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s06-w1",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step is not None
        assert step.execution.status is SyncRunStatus.PARTIAL
        assert step.execution.failure_reason_code == "MALFORMED_OR_PARTIAL_PAGE"
        assert step.execution.retry_allowed is False
        assert step.derived_units is None

        # Sub-case B: Verify missing required fields on HTTP 200 also fails closed as MALFORMED_OR_PARTIAL_PAGE
        provider_b = build_compact_multipage_provider(
            scenario_id="S06",
            default_branch=spec.default_branch,
            repo_id="900400026",
            repo_name="career-memory-s06b",
        )
        provider_b.malformed_pr_page_numbers.add(2)
        stack_b = self._build_stack(provider_b)
        rec_b = self._create_run_http(stack_b, backfill_from="2026-08-01")
        step_b = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s06b-w1",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_b is not None
        assert step_b.execution.status is SyncRunStatus.PARTIAL
        assert step_b.execution.failure_reason_code == "MALFORMED_OR_PARTIAL_PAGE"
        assert step_b.execution.retry_allowed is False

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_b.access.workspace_id,
                    phase="s06_subcase_b_missing_fields",
                    source_resource_id=stack_b.resource_id,
                )
            )

        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=None,
            extra={
                "subcase_a_content_length_truncated_reason": step.execution.failure_reason_code,
                "subcase_b_missing_fields_reason": step_b.execution.failure_reason_code,
                "subcase_b_retry_allowed": step_b.execution.retry_allowed,
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            (*provider.canaries.all_tokens(), *provider_b.canaries.all_tokens()),
            [*stack.captured_api_texts, *stack_b.captured_api_texts],
            mid_violations,
            gate_checks,
            0,
            ["Both HTTP 200 Content-Length truncation and missing required fields halted in PARTIAL/MALFORMED_OR_PARTIAL_PAGE."],
        )

    # =========================================================================
    # S07: Repeated / cyclic pagination cursor detection
    # =========================================================================
    def _run_s07(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_compact_multipage_provider(
            scenario_id="S07", default_branch=spec.default_branch
        )
        provider.cursor_cycle_pr_page_map[2] = 2  # Self-loop A -> A on PRS page 2
        stack = self._build_stack(provider)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01")
        run_id = receipt["target_id"]

        step = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s07-w1",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step is not None
        assert step.execution.status is SyncRunStatus.PARTIAL
        assert step.execution.failure_reason_code == "CURSOR_CYCLE_DETECTED"
        assert step.execution.retry_allowed is False
        assert step.derived_units is None
        assert provider.request_counts[f"/repositories/{provider.repo_id}/pulls?page=2"] == 1

        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=None,
            extra={
                "cycle_detected_within_bound": True,
                "prs_page2_requests": provider.request_counts[
                    f"/repositories/{provider.repo_id}/pulls?page=2"
                ],
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            [],
            gate_checks,
            0,
            ["Cyclic cursor on PRS page 2 halted immediately with CURSOR_CYCLE_DETECTED (1 request, 0 loops)."],
        )

    # =========================================================================
    # S08: Rate limit 429 Retry-After and timeout exhaustion
    # =========================================================================
    def _run_s08(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_compact_multipage_provider(
            scenario_id="S08", default_branch=spec.default_branch
        )
        prs_p2_key = f"/repositories/{provider.repo_id}/pulls?page=2"
        provider.status_fault_queues[prs_p2_key] = [429]
        provider.retry_after_by_key[prs_p2_key] = "30"
        provider.timeout_fault_counts[f"/repositories/{provider.repo_id}/commits?page=2"] = 3

        stack = self._build_stack(provider)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01")
        run_id = receipt["target_id"]

        # Attempt 1: PRS page 1 commits, PRS page 2 hits 429 (Retry-After: 30) -> PARTIAL retryable
        step1 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s08-w1",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step1 is not None
        assert step1.execution.status is SyncRunStatus.PARTIAL
        assert step1.execution.failure_reason_code == "TRANSIENT_PROVIDER_ERROR"
        assert step1.execution.retry_allowed is True
        assert step1.execution.has_safe_checkpoint is True

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack.access.workspace_id,
                    phase="s08_after_429_retry_after",
                    source_resource_id=stack.resource_id,
                )
            )

        # Retry -> PRS completes, commits page 1 commits, commits page 2 exhausts timeouts -> PARTIAL
        st1 = self._get_run_status_http(stack, run_id)
        self._retry_run_http(stack, run_id=run_id, expected_run_version=st1["run_version"])
        step2 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s08-w2",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW + timedelta(seconds=35),
        )
        assert step2 is not None
        assert step2.execution.status is SyncRunStatus.PARTIAL
        assert step2.execution.failure_reason_code == "TRANSIENT_PROVIDER_ERROR"
        assert step2.execution.retry_allowed is True
        assert step2.execution.has_safe_checkpoint is True
        assert step2.derived_units is None

        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=None,
            extra={
                "attempt1_429_retry_after_status": step1.execution.status.value,
                "attempt2_timeout_status": step2.execution.status.value,
                "attempt2_safe_checkpoint_preserved": step2.execution.has_safe_checkpoint,
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            mid_violations,
            gate_checks,
            0,
            ["429 Retry-After and Timeout exhaustion halted cleanly in PARTIAL with safe checkpoint and 0 promotions."],
        )

    # =========================================================================
    # S09: Mid-run PR cue drift AND default branch / HEAD drift -> PLAN_DRIFT
    # =========================================================================
    def _run_s09(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        # Sub-case A: PR cue mutation (updated_at & head_sha) between 1st and 2nd pass
        provider_a = build_compact_multipage_provider(
            scenario_id="S09", default_branch=spec.default_branch
        )

        def _mutate_pr_cue(ctx: PageHookContext) -> None:
            if ctx.stream_name is BackfillStreamName.DEFAULT_BRANCH_COMMITS and ctx.page_number == 2:
                provider_a.pr_pages[2][0]["updated_at"] = "2026-09-19T00:00:00Z"
                provider_a.pr_pages[2][0]["head"]["sha"] = "9" * 40

        hook_a = B004LifecycleFaultHook(on_after_page=_mutate_pr_cue)
        stack_a = self._build_stack(provider_a, hook=hook_a)
        rec_a = self._create_run_http(stack_a, backfill_from="2026-08-01")
        run_a = rec_a["target_id"]

        step_a = stack_a.worker.claim_and_execute_next(
            stack_a.access.workspace_id,
            worker_id="b004-s09a-w1",
            access=stack_a.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_a is not None
        assert step_a.execution.status is SyncRunStatus.PARTIAL
        assert step_a.execution.failure_reason_code == "PLAN_DRIFT"
        assert step_a.execution.source_drift is True
        assert step_a.execution.retry_allowed is False
        assert step_a.derived_units is None

        # Retry on PLAN_DRIFT MUST be rejected with 409!
        st_a = self._get_run_status_http(stack_a, run_a)
        self._retry_run_http(
            stack_a, run_id=run_a, expected_run_version=st_a["run_version"], expected_status=409
        )

        # Sub-case B: Default branch & HEAD SHA mutation between start and completion gate
        provider_b = build_compact_multipage_provider(
            scenario_id="S09",
            default_branch=spec.default_branch,
            repo_id="900400029",
            repo_name="career-memory-s09b",
        )

        def _mutate_branch_and_head(ctx: PageHookContext) -> None:
            if ctx.stream_name is BackfillStreamName.DEFAULT_BRANCH_COMMITS and ctx.page_number == 2:
                provider_b.default_branch = "release"
                provider_b.head_sha = "8" * 40

        hook_b = B004LifecycleFaultHook(on_after_page=_mutate_branch_and_head)
        stack_b = self._build_stack(provider_b, hook=hook_b)
        rec_b = self._create_run_http(stack_b, backfill_from="2026-08-01")
        step_b = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s09b-w1",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_b is not None
        assert step_b.execution.status is SyncRunStatus.PARTIAL
        assert step_b.execution.failure_reason_code == "PLAN_DRIFT"
        assert step_b.execution.source_drift is True
        assert step_b.execution.retry_allowed is False

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_b.access.workspace_id,
                    phase="s09_subcase_b_branch_head_drift",
                    source_resource_id=stack_b.resource_id,
                )
            )

        gate_checks = self._check_policy_gates(
            stack_a,
            content_unit_id=None,
            extra={
                "subcase_a_pr_cue_drift_reason": step_a.execution.failure_reason_code,
                "subcase_a_retry_rejected_409": True,
                "subcase_b_branch_head_drift_reason": step_b.execution.failure_reason_code,
                "global_review_comment_drift_assumed": False,
            },
        )
        return (
            stack_a.access.workspace_id,
            stack_a.resource_id,
            run_a,
            (*provider_a.canaries.all_tokens(), *provider_b.canaries.all_tokens()),
            [*stack_a.captured_api_texts, *stack_b.captured_api_texts],
            mid_violations,
            gate_checks,
            0,
            [
                "PR cue drift & default-branch/HEAD drift both halted in PARTIAL/PLAN_DRIFT (409 on retry).",
                "Global review/comment mutation detection is explicitly not assumed.",
            ],
        )

    # =========================================================================
    # S10: UNKNOWN PR association blocks SUCCESS + clean NO_MATCH_OBSERVED / exclusions
    # =========================================================================
    def _run_s10(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        # Sub-case A: UNKNOWN commit-to-PR association blocks SUCCESS with 0 DIRECT_COMMITs
        provider_a = build_compact_multipage_provider(
            scenario_id="S10", default_branch=spec.default_branch
        )
        direct_sha = next(sha for sha, prs in provider_a.commit_pr_associations.items() if prs == [])
        provider_a.commit_assoc_status_override[direct_sha] = 404  # Non-retryable -> UNKNOWN
        stack_a = self._build_stack(provider_a)
        rec_a = self._create_run_http(stack_a, backfill_from="2026-08-01")
        run_a = rec_a["target_id"]

        step_a = stack_a.worker.claim_and_execute_next(
            stack_a.access.workspace_id,
            worker_id="b004-s10a-w1",
            access=stack_a.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_a is not None
        assert step_a.execution.status is SyncRunStatus.PARTIAL
        assert step_a.execution.failure_reason_code == "INCOMPLETE_PR_ASSOCIATION"
        assert step_a.execution.retry_allowed is False
        assert step_a.derived_units is None

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_a.access.workspace_id,
                    phase="s10_subcase_a_unknown_association",
                    source_resource_id=stack_a.resource_id,
                )
            )

        # Sub-case B: Clean completion with NO_MATCH_OBSERVED + out-of-window PR + squash commit
        provider_b = build_compact_multipage_provider(
            scenario_id="S10",
            default_branch=spec.default_branch,
            repo_id="900400030",
            repo_name="career-memory-s10b",
            include_direct_commit=True,
            include_out_of_window_commit=True,
            include_squash_commit=True,
        )
        stack_b = self._build_stack(provider_b)
        rec_b = self._create_run_http(stack_b, backfill_from="2026-08-01")
        run_b = rec_b["target_id"]

        step_b = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s10b-w1",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_b is not None
        assert step_b.execution.status is SyncRunStatus.SUCCESS
        assert len(step_b.execution.promoted_direct_commit_event_ids) == 1
        assert len(step_b.execution.promoted_pr_event_ids) == 3

        unit_id = self._find_first_pr_content_unit_id(stack_b.access.workspace_id, pr_number="201")
        gate_checks = self._check_policy_gates(
            stack_b,
            content_unit_id=unit_id,
            sync_run_id=run_b,
            extra={
                "subcase_a_unknown_blocked_status": step_a.execution.status.value,
                "subcase_a_unknown_reason": step_a.execution.failure_reason_code,
                "subcase_b_direct_commit_count": len(
                    step_b.execution.promoted_direct_commit_event_ids
                ),
                "subcase_b_out_of_window_and_squash_direct_commits": 0,
            },
        )
        return (
            stack_b.access.workspace_id,
            stack_b.resource_id,
            run_b,
            (*provider_a.canaries.all_tokens(), *provider_b.canaries.all_tokens()),
            [*stack_a.captured_api_texts, *stack_b.captured_api_texts],
            mid_violations,
            gate_checks,
            0,
            ["UNKNOWN blocked SUCCESS (0 DIRECT_COMMIT); clean run promoted 1 NO_MATCH_OBSERVED DIRECT_COMMIT (requires_user_confirmation=True) and excluded out-of-window/squash commits."],
        )

    # =========================================================================
    # S11: PR-linked commits deduplicated (0 DIRECT_COMMITs; default_branch='master')
    # =========================================================================
    def _run_s11(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_compact_multipage_provider(
            scenario_id="S11",
            default_branch=spec.default_branch,  # 'master' (verifies non-hardcoding!)
            include_direct_commit=False,
            include_out_of_window_commit=False,
            include_squash_commit=True,
        )
        stack = self._build_stack(provider)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01")
        run_id = receipt["target_id"]

        step = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s11-w1",
            access=stack.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step is not None
        assert step.execution.status is SyncRunStatus.SUCCESS
        assert len(step.execution.promoted_pr_event_ids) == 3
        assert len(step.execution.promoted_direct_commit_event_ids) == 0

        unit_id = self._find_first_pr_content_unit_id(stack.access.workspace_id, pr_number="201")
        gate_checks = self._check_policy_gates(
            stack,
            content_unit_id=unit_id,
            sync_run_id=run_id,
            extra={
                "default_branch_verified": spec.default_branch,
                "duplicate_direct_commit_events": 0,
            },
        )
        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            [],
            gate_checks,
            0,
            ["All PR-linked commits (head_sha, merge_commit_sha, squash) attached only to PR MUST_LINK memberships; 0 DIRECT_COMMITs."],
        )

    # =========================================================================
    # S12: Max objects / requests / bytes budget exceeded -> PARTIAL_WITH_LIMIT
    # =========================================================================
    def _run_s12(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        # Sub-case A: max_objects exceeded
        provider_a = build_compact_multipage_provider(
            scenario_id="S12", default_branch=spec.default_branch
        )
        stack_a = self._build_stack(provider_a)
        rec_a = self._create_run_http(
            stack_a, backfill_from="2026-08-01", limits={"max_objects": 2}
        )
        run_a = rec_a["target_id"]
        step_a = stack_a.worker.claim_and_execute_next(
            stack_a.access.workspace_id,
            worker_id="b004-s12a-w1",
            access=stack_a.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_a is not None
        assert step_a.execution.status is SyncRunStatus.PARTIAL_WITH_LIMIT
        assert step_a.execution.failure_reason_code == "LIMIT_EXCEEDED"
        assert step_a.execution.retry_allowed is False
        assert step_a.execution.processed_range and step_a.execution.remaining_range
        assert step_a.derived_units is None

        # Sub-case B: max_requests exceeded (configured in sync_runs.plan_spec.limits before start)
        provider_b = build_compact_multipage_provider(
            scenario_id="S12",
            default_branch=spec.default_branch,
            repo_id="900400032",
            repo_name="career-memory-s12b",
        )
        stack_b = self._build_stack(provider_b)
        rec_b = self._create_run_http(stack_b, backfill_from="2026-08-01")
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET plan_spec = jsonb_set(plan_spec, '{limits}', '{"max_requests": 2}'::jsonb)
                        WHERE workspace_id = %s::uuid AND id = %s::uuid
                        """,
                        (stack_b.access.workspace_id, rec_b["target_id"]),
                    )
        step_b = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s12b-w1",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_b is not None and step_b.execution.status is SyncRunStatus.PARTIAL_WITH_LIMIT

        # Sub-case C: max_total_bytes exceeded (configured in sync_runs.plan_spec.limits before start)
        provider_c = build_compact_multipage_provider(
            scenario_id="S12",
            default_branch=spec.default_branch,
            repo_id="900400033",
            repo_name="career-memory-s12c",
        )
        stack_c = self._build_stack(provider_c)
        rec_c = self._create_run_http(stack_c, backfill_from="2026-08-01")
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET plan_spec = jsonb_set(
                            plan_spec,
                            '{limits}',
                            '{"max_total_bytes": 500, "max_item_bytes": 500}'::jsonb
                        )
                        WHERE workspace_id = %s::uuid AND id = %s::uuid
                        """,
                        (stack_c.access.workspace_id, rec_c["target_id"]),
                    )
        step_c = stack_c.worker.claim_and_execute_next(
            stack_c.access.workspace_id,
            worker_id="b004-s12c-w1",
            access=stack_c.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_c is not None and step_c.execution.status is SyncRunStatus.PARTIAL_WITH_LIMIT

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_b.access.workspace_id,
                    phase="s12_subcase_b_max_requests",
                    source_resource_id=stack_b.resource_id,
                )
            ) + list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_c.access.workspace_id,
                    phase="s12_subcase_c_max_bytes",
                    source_resource_id=stack_c.resource_id,
                )
            )

        gate_checks = self._check_policy_gates(
            stack_a,
            content_unit_id=None,
            extra={
                "max_objects_status": step_a.execution.status.value,
                "max_requests_status": step_b.execution.status.value,
                "max_bytes_status": step_c.execution.status.value,
            },
        )
        return (
            stack_a.access.workspace_id,
            stack_a.resource_id,
            run_a,
            (
                *provider_a.canaries.all_tokens(),
                *provider_b.canaries.all_tokens(),
                *provider_c.canaries.all_tokens(),
            ),
            [*stack_a.captured_api_texts, *stack_b.captured_api_texts, *stack_c.captured_api_texts],
            mid_violations,
            gate_checks,
            0,
            ["max_objects, max_requests, and max_total_bytes all halted cleanly in PARTIAL_WITH_LIMIT with 0 promotions."],
        )

    # =========================================================================
    # S13: Mid-run PRIVATE flip + 401 token revocation + policy API 503 failure
    # =========================================================================
    def _run_s13(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        # Sub-case A: Public -> Private flip mid-run
        provider_a = build_compact_multipage_provider(
            scenario_id="S13", default_branch=spec.default_branch
        )

        def _flip_private(ctx: PageHookContext) -> None:
            if ctx.stream_name is BackfillStreamName.DEFAULT_BRANCH_COMMITS and ctx.page_number == 2:
                provider_a.private = True
                provider_a.visibility = "private"

        stack_a = self._build_stack(
            provider_a, hook=B004LifecycleFaultHook(on_after_page=_flip_private)
        )
        rec_a = self._create_run_http(stack_a, backfill_from="2026-08-01")
        run_a = rec_a["target_id"]
        step_a = stack_a.worker.claim_and_execute_next(
            stack_a.access.workspace_id,
            worker_id="b004-s13a-w1",
            access=stack_a.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_a is not None
        assert step_a.execution.status in (SyncRunStatus.FAILED, SyncRunStatus.CANCELED)
        assert step_a.execution.import_ready is False
        assert step_a.execution.retry_allowed is False
        assert step_a.derived_units is None

        # Sub-case B: 401 token revocation mid-run
        provider_b = build_compact_multipage_provider(
            scenario_id="S13",
            default_branch=spec.default_branch,
            repo_id="900400034",
            repo_name="career-memory-s13b",
        )

        def _revoke_401(ctx: PageHookContext) -> None:
            if ctx.stream_name is BackfillStreamName.PRS and ctx.page_number == 1:
                provider_b.auth_revoked = True

        stack_b = self._build_stack(
            provider_b, hook=B004LifecycleFaultHook(on_after_page=_revoke_401)
        )
        self._create_run_http(stack_b, backfill_from="2026-08-01")
        step_b = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s13b-w1",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_b is not None
        assert step_b.execution.import_ready is False
        assert stack_b.connect_service.list_source_accounts(stack_b.access)[0].status == "REVOKED"
        b_gates = self._check_policy_gates(stack_b, content_unit_id=None)
        assert b_gates.external_ai_gate_allowed is False
        assert b_gates.export_gate_allowed is False

        # Sub-case C: Policy verification API 503 failure -> fail-closed
        provider_c = build_compact_multipage_provider(
            scenario_id="S13",
            default_branch=spec.default_branch,
            repo_id="900400035",
            repo_name="career-memory-s13c",
        )

        def _break_policy_api(ctx: PageHookContext) -> None:
            if ctx.stream_name is BackfillStreamName.DEFAULT_BRANCH_COMMITS and ctx.page_number == 2:
                provider_c.policy_check_http_status_override = 503

        stack_c = self._build_stack(
            provider_c, hook=B004LifecycleFaultHook(on_after_page=_break_policy_api)
        )
        self._create_run_http(stack_c, backfill_from="2026-08-01")
        step_c = stack_c.worker.claim_and_execute_next(
            stack_c.access.workspace_id,
            worker_id="b004-s13c-w1",
            access=stack_c.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_c is not None
        assert step_c.execution.status is SyncRunStatus.PARTIAL
        assert step_c.execution.import_ready is False

        policy_api_503_code: str | None = None
        try:
            stack_c.connect_service.verify_resource_policy(
                stack_c.access, source_resource_id=stack_c.resource_id
            )
        except GitHubConnectError as exc:
            assert exc.status_code == 503
            policy_api_503_code = exc.code
        assert policy_api_503_code == "GITHUB_PROVIDER_UNAVAILABLE"

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_b.access.workspace_id,
                    phase="s13_subcase_b_401_revoked",
                    gate_checks=b_gates,
                    source_resource_id=stack_b.resource_id,
                )
            ) + list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_c.access.workspace_id,
                    phase="s13_subcase_c_policy_api_503",
                    source_resource_id=stack_c.resource_id,
                )
            )

        gate_checks = self._check_policy_gates(
            stack_a,
            content_unit_id=None,
            extra={
                "subcase_a_private_flip_status": step_a.execution.status.value,
                "subcase_b_401_account_status": "REVOKED",
                "subcase_b_ai_blocked": not b_gates.external_ai_gate_allowed,
                "subcase_c_policy_api_503_code": policy_api_503_code,
                "subcase_c_import_ready": step_c.execution.import_ready,
            },
        )
        assert gate_checks.external_ai_gate_allowed is False
        assert gate_checks.export_gate_allowed is False
        return (
            stack_a.access.workspace_id,
            stack_a.resource_id,
            run_a,
            (
                *provider_a.canaries.all_tokens(),
                *provider_b.canaries.all_tokens(),
                *provider_c.canaries.all_tokens(),
            ),
            [*stack_a.captured_api_texts, *stack_b.captured_api_texts, *stack_c.captured_api_texts],
            mid_violations,
            gate_checks,
            0,
            ["PRIVATE flip, 401 revocation, and policy API 503 all halted with 0 tombstones, 0 promotions, and full AI/Export/refetch blocking."],
        )

    # =========================================================================
    # S14: Transient retry vs PLAN_DRIFT abandoned isolation vs post-SUCCESS 409
    # =========================================================================
    def _run_s14(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        # Sub-case A: Transient 503 -> retry succeeds -> post-SUCCESS create & retry return 409
        provider_a = build_compact_multipage_provider(
            scenario_id="S14", default_branch=spec.default_branch
        )
        provider_a.status_fault_queues[f"/repositories/{provider_a.repo_id}/pulls?page=2"] = [503]
        stack_a = self._build_stack(provider_a)
        rec_a = self._create_run_http(stack_a, backfill_from="2026-08-01")
        run_a = rec_a["target_id"]
        step_a1 = stack_a.worker.claim_and_execute_next(
            stack_a.access.workspace_id,
            worker_id="b004-s14a-w1",
            access=stack_a.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_a1 is not None and step_a1.execution.status is SyncRunStatus.PARTIAL
        assert step_a1.execution.retry_allowed is True
        fp_before = step_a1.execution.frozen_plan_fingerprint

        st_a1 = self._get_run_status_http(stack_a, run_a)
        self._retry_run_http(stack_a, run_id=run_a, expected_run_version=st_a1["run_version"])
        step_a2 = stack_a.worker.claim_and_execute_next(
            stack_a.access.workspace_id,
            worker_id="b004-s14a-w2",
            access=stack_a.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_a2 is not None and step_a2.execution.status is SyncRunStatus.SUCCESS
        assert step_a2.execution.frozen_plan_fingerprint == fp_before

        # Post-SUCCESS retry & new import MUST BOTH return 409!
        st_a2 = self._get_run_status_http(stack_a, run_a)
        self._retry_run_http(
            stack_a, run_id=run_a, expected_run_version=st_a2["run_version"], expected_status=409
        )
        self._create_run_http(stack_a, backfill_from="2026-08-01", expected_status=409)

        # Sub-case B: PLAN_DRIFT -> 409 on retry -> no HTTP abandon endpoint -> DB abandon -> clean new plan
        provider_b = build_compact_multipage_provider(
            scenario_id="S14",
            default_branch=spec.default_branch,
            repo_id="900400036",
            repo_name="career-memory-s14b",
        )
        drifted_once = [False]

        def _drift_once(ctx: PageHookContext) -> None:
            if (
                not drifted_once[0]
                and ctx.stream_name is BackfillStreamName.DEFAULT_BRANCH_COMMITS
                and ctx.page_number == 2
            ):
                drifted_once[0] = True
                provider_b.head_sha = "7" * 40

        stack_b = self._build_stack(
            provider_b, hook=B004LifecycleFaultHook(on_after_page=_drift_once)
        )
        rec_b1 = self._create_run_http(stack_b, backfill_from="2026-08-01")
        old_run_id = rec_b1["target_id"]
        step_b1 = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s14b-w1",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_b1 is not None
        assert step_b1.execution.status is SyncRunStatus.PARTIAL
        assert step_b1.execution.failure_reason_code == "PLAN_DRIFT"
        assert step_b1.execution.retry_allowed is False

        st_b1 = self._get_run_status_http(stack_b, old_run_id)
        self._retry_run_http(
            stack_b, run_id=old_run_id, expected_run_version=st_b1["run_version"], expected_status=409
        )
        # Verify HTTP API does NOT expose an arbitrary abandon endpoint (must be 404/405)
        with TestClient(stack_b.app) as client:
            abandon_resp = client.post(f"/v1/sync-runs/{old_run_id}/abandon")
            stack_b.captured_api_texts.append(abandon_resp.text)
            assert abandon_resp.status_code in (404, 405)

        # Operator/internal DB abandonment of old incomplete run -> new plan starts cleanly & isolated
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET is_abandoned = true,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid AND id = %s::uuid
                        """,
                        (FIXED_NOW, stack_b.access.workspace_id, old_run_id),
                    )
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_b.access.workspace_id,
                    phase="s14_after_plan_drift_abandoned",
                    source_resource_id=stack_b.resource_id,
                )
            )

        rec_b2 = self._create_run_http(stack_b, backfill_from="2026-08-01", expected_status=202)
        new_run_id = rec_b2["target_id"]
        assert new_run_id != old_run_id

        step_b2 = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s14b-w2",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW + timedelta(seconds=10),
        )
        assert step_b2 is not None and step_b2.execution.status is SyncRunStatus.SUCCESS
        assert step_b2.execution.plan_id != step_b1.execution.plan_id

        # Confirm old abandoned run STILL has import_ready=False and 0 IMPORT_READY outbox rows!
        with self._cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT import_ready, is_abandoned, status
                    FROM public.sync_runs
                    WHERE workspace_id = %s::uuid AND id = %s::uuid
                    """,
                    (stack_b.access.workspace_id, old_run_id),
                )
                old_row = cur.fetchone()
                assert old_row == (False, True, "PARTIAL")
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.outbox_events
                    WHERE workspace_id = %s::uuid AND aggregate_id = %s::uuid
                      AND event_type = 'IMPORT_READY'
                    """,
                    (stack_b.access.workspace_id, old_run_id),
                )
                assert int(cur.fetchone()[0]) == 0

        unit_id = self._find_first_pr_content_unit_id(stack_b.access.workspace_id, pr_number="201")
        gate_checks = self._check_policy_gates(
            stack_b,
            content_unit_id=unit_id,
            sync_run_id=new_run_id,
            extra={
                "subcase_a_transient_retry_same_plan": True,
                "subcase_a_post_success_retry_409": True,
                "subcase_a_post_success_create_409": True,
                "subcase_b_plan_drift_retry_409": True,
                "subcase_b_http_abandon_exposed": False,
                "subcase_b_old_abandoned_run_isolated": True,
            },
        )
        return (
            stack_b.access.workspace_id,
            stack_b.resource_id,
            None,  # Include BOTH abandoned + succeeded runs in sync_runs snapshot!
            (*provider_a.canaries.all_tokens(), *provider_b.canaries.all_tokens()),
            [*stack_a.captured_api_texts, *stack_b.captured_api_texts],
            mid_violations,
            gate_checks,
            0,
            ["Verified transient retry convergence, post-SUCCESS 409 rejections, no HTTP abandon route, and isolated new plan after PLAN_DRIFT."],
        )

    # =========================================================================
    # S15: Pre-IMPORT_READY block + duplicate completion idempotency + refetch match/mismatch
    # =========================================================================
    def _run_s15(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        provider = build_compact_multipage_provider(
            scenario_id="S15", default_branch=spec.default_branch
        )
        provider.status_fault_queues[f"/repositories/{provider.repo_id}/pulls?page=2"] = [503]
        stack = self._build_stack(provider)
        receipt = self._create_run_http(stack, backfill_from="2026-08-01")
        run_id = receipt["target_id"]

        # Part A: Pause in PARTIAL -> DerivedUnitIngestionProcessor MUST refuse to run!
        step1 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s15-w1",
            access=stack.access,
            run_derived_units=False,
            now=FIXED_NOW,
        )
        assert step1 is not None and step1.execution.status is SyncRunStatus.PARTIAL

        premature_derived = stack.derived_processor.process_import_ready_run(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            now=FIXED_NOW,
        )
        assert premature_derived.executed is False
        assert premature_derived.reason_code == "RUN_NOT_IMPORT_READY"

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack.access.workspace_id,
                    phase="s15_before_import_ready",
                    source_resource_id=stack.resource_id,
                )
            )

        # Part B: Retry -> SUCCESS + duplicate completion signal idempotency
        st1 = self._get_run_status_http(stack, run_id)
        self._retry_run_http(stack, run_id=run_id, expected_run_version=st1["run_version"])
        step2 = stack.worker.claim_and_execute_next(
            stack.access.workspace_id,
            worker_id="b004-s15-w2",
            access=stack.access,
            run_derived_units=False,
            now=FIXED_NOW,
        )
        assert step2 is not None and step2.execution.status is SyncRunStatus.SUCCESS

        # Duplicate completion signal #1: re-invoke execute_sync_run -> 0 extra fetches
        reqs_before_dup = len(provider.request_log)
        dup_exec = stack.orchestrator.execute_sync_run(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            access=stack.access,
            now=FIXED_NOW,
        )
        assert dup_exec.status is SyncRunStatus.SUCCESS
        assert len(provider.request_log) == reqs_before_dup

        # Duplicate completion signal #2: consume IMPORT_READY outbox event TWICE -> 1st True, 2nd False
        published_events: list[Any] = []

        class _CapturePublisher:
            def publish(self, event: Any) -> None:
                published_events.append(event)

        stack.job_runtime.dispatch_pending_outbox(
            stack.access.workspace_id,
            publisher=_CapturePublisher(),
            now=FIXED_NOW,
        )
        import_ready_events = [e for e in published_events if e.event_type == "IMPORT_READY"]
        assert len(import_ready_events) == 1
        effect_counter = [0]
        first_consume = stack.job_runtime.consume_outbox_event_once(
            stack.access.workspace_id,
            consumer_name="b004_derived_unit_consumer",
            event=import_ready_events[0],
            on_effect=lambda _ev: effect_counter.__setitem__(0, effect_counter[0] + 1),
            now=FIXED_NOW,
        )
        second_consume = stack.job_runtime.consume_outbox_event_once(
            stack.access.workspace_id,
            consumer_name="b004_derived_unit_consumer",
            event=import_ready_events[0],
            on_effect=lambda _ev: effect_counter.__setitem__(0, effect_counter[0] + 1),
            now=FIXED_NOW,
        )
        assert first_consume is True and second_consume is False and effect_counter[0] == 1

        # Duplicate completion signal #3: DerivedUnitIngestionProcessor called TWICE -> 0 duplicate units
        d1 = stack.derived_processor.process_import_ready_run(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            now=FIXED_NOW,
        )
        d2 = stack.derived_processor.process_import_ready_run(
            workspace_id=stack.access.workspace_id,
            sync_run_id=run_id,
            now=FIXED_NOW,
        )
        assert d1.executed is True and d1.content_units_inserted > 0
        assert d2.executed is True and d2.content_units_inserted == 0

        # Part C-1: Refetch hash MATCHED branch
        unit_id = self._find_first_pr_content_unit_id(stack.access.workspace_id, pr_number="201")
        assert unit_id is not None
        matched_gates = self._check_policy_gates(
            stack, content_unit_id=unit_id, sync_run_id=run_id
        )
        assert matched_gates.refetch_resolution_status == ResolutionStatus.MATCHED.value
        assert matched_gates.refetch_content_allowed is True
        assert matched_gates.claim_approval_gate_allowed is True
        assert matched_gates.export_gate_allowed is True

        # Part C-2: Mutate upstream PR #201 body -> REVISION_MISMATCH -> processing_status=BLOCKED_SOURCE
        # while preserving historical sync_runs.status='SUCCESS' & blocking approval/export gates!
        provider.pr_body_override_by_number[201] = "## Mutated\nUpstream PR body edited after SUCCESS.\n"
        mismatch_gates = self._check_policy_gates(
            stack,
            content_unit_id=unit_id,
            sync_run_id=run_id,
            project_resolver_status=True,
            extra={
                "premature_derived_blocked": True,
                "outbox_consume_first": first_consume,
                "outbox_consume_second": second_consume,
                "derived_second_run_inserted": d2.content_units_inserted,
                "pre_mutation_refetch_status": matched_gates.refetch_resolution_status,
                "pre_mutation_export_allowed": matched_gates.export_gate_allowed,
                "post_mutation_refetch_status": ResolutionStatus.REVISION_MISMATCH.value,
            },
        )
        assert mismatch_gates.refetch_resolution_status == ResolutionStatus.REVISION_MISMATCH.value
        assert mismatch_gates.refetch_content_allowed is False
        assert mismatch_gates.claim_approval_gate_allowed is False
        assert mismatch_gates.export_gate_allowed is False

        st_final = self._get_run_status_http(stack, run_id)
        assert st_final["status"] == "SUCCESS"
        assert st_final["import_ready"] is True
        assert st_final["processing_status"] == "BLOCKED_SOURCE"

        return (
            stack.access.workspace_id,
            stack.resource_id,
            run_id,
            provider.canaries.all_tokens(),
            stack.captured_api_texts,
            mid_violations,
            mismatch_gates,
            0,
            ["Blocked pre-IMPORT_READY processing; verified duplicate completion idempotency (outbox/derived=1x); proved MATCHED vs REVISION_MISMATCH -> BLOCKED_SOURCE preserving status='SUCCESS'."],
        )

    # =========================================================================
    # S16: Post-SUCCESS OD-003 deletion fence & pre-resume deletion/PRIVATE cancellation
    # =========================================================================
    def _run_s16(self, spec: ScenarioSpec) -> tuple[Any, ...]:
        # Part A: Post-SUCCESS OD-003 deletion-preview -> deletion-confirm -> blocks raw/AI/Export
        provider_a = build_compact_multipage_provider(
            scenario_id="S16", default_branch=spec.default_branch
        )
        stack_a = self._build_stack(provider_a)
        rec_a = self._create_run_http(stack_a, backfill_from="2026-08-01")
        run_a = rec_a["target_id"]
        step_a = stack_a.worker.claim_and_execute_next(
            stack_a.access.workspace_id,
            worker_id="b004-s16a-w1",
            access=stack_a.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_a is not None and step_a.execution.status is SyncRunStatus.SUCCESS
        unit_id = self._find_first_pr_content_unit_id(stack_a.access.workspace_id, pr_number="201")
        assert unit_id is not None

        with TestClient(stack_a.app) as client:
            prev_resp = client.post(
                f"/v1/source-resources/{stack_a.resource_id}/deletion-preview"
            )
            stack_a.captured_api_texts.append(prev_resp.text)
            assert prev_resp.status_code == 200
            prev = prev_resp.json()

            conf_resp = client.post(
                f"/v1/source-resources/{stack_a.resource_id}/deletion-confirm",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={
                    "preview_token": prev["preview_token"],
                    "expected_resource_version": prev["resource_version"],
                    "expected_impact_scope_version": prev["impact_scope_version"],
                    "acknowledge_irreversible_purge": True,
                },
            )
            stack_a.captured_api_texts.append(conf_resp.text)
            assert conf_resp.status_code == 202

        # After OD-003 deletion fence, derived unit processing projects BLOCKED_POLICY (preserving SUCCESS)
        post_del_derived = stack_a.derived_processor.process_import_ready_run(
            workspace_id=stack_a.access.workspace_id,
            sync_run_id=run_a,
            now=FIXED_NOW + timedelta(seconds=5),
        )
        assert post_del_derived.sync_run_status is SyncRunStatus.SUCCESS
        assert post_del_derived.processing_status is ProcessingStatus.BLOCKED_POLICY

        # Part B: Pre-resume OD-003 deletion fence on PARTIAL run cancels queued resume (0 resurrection)
        provider_b = build_compact_multipage_provider(
            scenario_id="S16",
            default_branch=spec.default_branch,
            repo_id="900400037",
            repo_name="career-memory-s16b",
        )
        provider_b.status_fault_queues[f"/repositories/{provider_b.repo_id}/pulls?page=2"] = [503]
        stack_b = self._build_stack(provider_b)
        rec_b = self._create_run_http(stack_b, backfill_from="2026-08-01")
        run_b = rec_b["target_id"]
        step_b1 = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s16b-w1",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_b1 is not None and step_b1.execution.status is SyncRunStatus.PARTIAL
        st_b1 = self._get_run_status_http(stack_b, run_b)
        self._retry_run_http(stack_b, run_id=run_b, expected_run_version=st_b1["run_version"])

        # Activate OD-003 deletion fence BEFORE resumed worker runs!
        with TestClient(stack_b.app) as client:
            prev_b = client.post(
                f"/v1/source-resources/{stack_b.resource_id}/deletion-preview"
            ).json()
            conf_b = client.post(
                f"/v1/source-resources/{stack_b.resource_id}/deletion-confirm",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={
                    "preview_token": prev_b["preview_token"],
                    "expected_resource_version": prev_b["resource_version"],
                    "expected_impact_scope_version": prev_b["impact_scope_version"],
                    "acknowledge_irreversible_purge": True,
                },
            )
            assert conf_b.status_code == 202

        # Worker finds 0 runnable jobs (canceled by fence); direct orchestrator call also sees CANCELED
        step_b2 = stack_b.worker.claim_and_execute_next(
            stack_b.access.workspace_id,
            worker_id="b004-s16b-w2",
            access=stack_b.access,
            run_derived_units=True,
            now=FIXED_NOW + timedelta(seconds=5),
        )
        assert step_b2 is None
        exec_b_check = stack_b.orchestrator.execute_sync_run(
            workspace_id=stack_b.access.workspace_id,
            sync_run_id=run_b,
            access=stack_b.access,
            now=FIXED_NOW + timedelta(seconds=6),
        )
        assert exec_b_check.status is SyncRunStatus.CANCELED
        assert exec_b_check.import_ready is False

        # Part C: Pre-resume PRIVATE flip on PARTIAL run blocks resume immediately (0 resurrection)
        provider_c = build_compact_multipage_provider(
            scenario_id="S16",
            default_branch=spec.default_branch,
            repo_id="900400038",
            repo_name="career-memory-s16c",
        )
        provider_c.status_fault_queues[f"/repositories/{provider_c.repo_id}/pulls?page=2"] = [503]
        stack_c = self._build_stack(provider_c)
        rec_c = self._create_run_http(stack_c, backfill_from="2026-08-01")
        run_c = rec_c["target_id"]
        step_c1 = stack_c.worker.claim_and_execute_next(
            stack_c.access.workspace_id,
            worker_id="b004-s16c-w1",
            access=stack_c.access,
            run_derived_units=True,
            now=FIXED_NOW,
        )
        assert step_c1 is not None and step_c1.execution.status is SyncRunStatus.PARTIAL
        st_c1 = self._get_run_status_http(stack_c, run_c)
        self._retry_run_http(stack_c, run_id=run_c, expected_run_version=st_c1["run_version"])

        # Flip to PRIVATE right before resumed job runs!
        provider_c.private = True
        provider_c.visibility = "private"
        step_c2 = stack_c.worker.claim_and_execute_next(
            stack_c.access.workspace_id,
            worker_id="b004-s16c-w2",
            access=stack_c.access,
            run_derived_units=True,
            now=FIXED_NOW + timedelta(seconds=5),
        )
        assert step_c2 is not None
        assert step_c2.execution.status in (SyncRunStatus.FAILED, SyncRunStatus.CANCELED)
        assert step_c2.execution.import_ready is False
        assert step_c2.execution.retry_allowed is False

        with self._cluster.admin_connection() as conn:
            mid_violations = list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_b.access.workspace_id,
                    phase="s16_part_b_pre_resume_deletion_fence",
                    source_resource_id=stack_b.resource_id,
                )
            ) + list(
                verify_b004_invariants(
                    conn,
                    workspace_id=stack_c.access.workspace_id,
                    phase="s16_part_c_pre_resume_private_flip",
                    source_resource_id=stack_c.resource_id,
                )
            )

        gate_checks = self._check_policy_gates(
            stack_a,
            content_unit_id=unit_id,
            sync_run_id=run_a,
            extra={
                "part_a_post_success_deletion_processing_status": post_del_derived.processing_status.value,
                "part_b_pre_resume_deletion_run_status": exec_b_check.status.value,
                "part_b_pre_resume_resurrection_events": 0,
                "part_c_pre_resume_private_run_status": step_c2.execution.status.value,
                "part_c_pre_resume_private_reason": step_c2.execution.failure_reason_code,
            },
        )
        assert gate_checks.refetch_content_allowed is False
        assert gate_checks.external_ai_gate_allowed is False
        assert gate_checks.export_gate_allowed is False
        assert gate_checks.claim_approval_gate_allowed is False

        return (
            stack_a.access.workspace_id,
            stack_a.resource_id,
            run_a,
            (
                *provider_a.canaries.all_tokens(),
                *provider_b.canaries.all_tokens(),
                *provider_c.canaries.all_tokens(),
            ),
            [*stack_a.captured_api_texts, *stack_b.captured_api_texts, *stack_c.captured_api_texts],
            mid_violations,
            gate_checks,
            0,
            ["Post-SUCCESS OD-003 deletion blocked raw/AI/Export (0 exposures); pre-resume deletion fence canceled run (0 resurrection); pre-resume PRIVATE flip halted resume (0 tombstones, 0 promotions)."],
        )


def build_redacted_evidence_bundle(
    results: Sequence[ScenarioRunResult],
    *,
    openapi_blob_sha: str,
) -> dict[str, Any]:
    """Build a strictly deterministic, zero-canary, zero-UUID redacted evidence bundle for T-005D."""
    entries = [res.to_evidence_entry() for res in results]
    s01_core_hash = next(
        (e["core_convergence_sha256"] for e in entries if e["scenario_id"] == "S01"),
        None,
    )
    bundle: dict[str, Any] = {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "scenario_schema_version": SCENARIO_SCHEMA_VERSION,
        "expected_snapshot_schema_version": EXPECTED_SNAPSHOT_SCHEMA_VERSION,
        "gate_id": "B-004",
        "tracks": ["T-005A", "T-005B", "T-005C", "T-005D"],
        "observed_at_iso": DEFAULT_OBSERVED_AT_ISO,
        "openapi_import_v1_blob_sha": openapi_blob_sha,
        "database_engine": "PostgreSQL 18 (local ephemeral, migrations 0010..0050, NOBYPASSRLS)",
        "production_entry_points": [
            "career_memory_api.initial_import.InitialBackfillOrchestrator",
            "career_memory_api.initial_import.InitialBackfillJobWorker",
            "career_memory_api.initial_import.DerivedUnitIngestionProcessor",
            "career_memory_api.backfill_engine.PostgresBackfillPageCommitter",
            "career_memory_api.sync_runs.PostgresSyncRunService",
            "career_memory_api.jobs.PostgresJobRuntime",
            "career_memory_api.github_connect.PostgresGitHubConnectService",
            "career_memory_api.github_provider.HttpGitHubImportClient",
            "career_memory_api.resolver.RefetchResolverService",
            "career_memory_api.contracts_api.create_contracts_router",
        ],
        "hosted_staging_and_live_github_status": (
            "Deferred / Waived for current gate by owner decision (not executed in local offline B-004 harness)."
        ),
        "review_comment_global_drift_assumption": (
            "Not assumed: completion gate detects default-branch/HEAD changes and 2nd-pass merged PR cue drift only."
        ),
        "snapshot_auto_update_supported": False,
        "summary": {
            "total_scenarios": len(entries),
            "passed_scenarios": sum(1 for e in entries if e["passed"]),
            "failed_scenarios": sum(1 for e in entries if not e["passed"]),
            "total_invariant_violations": sum(e["invariant_violation_count"] for e in entries),
            "total_canary_hits": sum(e["canary_audit"]["canary_hits"] for e in entries),
            "all_content_units_plan_c_compliant": all(
                e["canary_audit"]["content_units_plan_c_compliant"] for e in entries
            ),
            "s01_baseline_core_convergence_sha256": s01_core_hash,
            "s02_s03_s04_converged_to_s01": all(
                e["baseline_equivalence_matched"] is True
                for e in entries
                if e["scenario_id"] in ("S02", "S03", "S04")
            ),
        },
        "scenarios": entries,
    }
    # Defense-in-depth: guarantee zero canary strings in the redacted evidence bundle!
    serialized = json.dumps(bundle, sort_keys=True)
    if "CANARY_B004_" in serialized:
        raise RuntimeError("Redacted evidence bundle unexpectedly contained a canary marker!")
    return bundle


__all__ = [
    "B004PostgresScenarioRunner",
    "EphemeralB004Pg18Cluster",
    "FIXED_NOW",
    "PostgresDeletionBackedContractService",
    "ScenarioRunResult",
    "build_redacted_evidence_bundle",
]
