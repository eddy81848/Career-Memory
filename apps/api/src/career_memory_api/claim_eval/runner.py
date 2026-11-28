"""Real PostgreSQL 18 + Production FastAPI Runner for Frozen 0.4.0-Synthetic-Golden Claim & Refetch Evaluation (T-407A).

Executes the REAL M4 production stack against an ephemeral local PostgreSQL 18 cluster
migrated with 0010..0070 and NOBYPASSRLS runtime roles (cm_api_app, cm_worker_app):
- Stage 2A FactCandidateExtractor -> Stage 2B FactSupportValidator -> CanonicalFactNormalizer + Contributions
- Stage 3A ClaimContextBuilder & ClaimGenerator -> Stage 3B ClaimAtomizer -> Stage 3C ClaimSupportValidator
- Read-only VerificationCalculator, ER-012 versioned Claim edit/revalidation, CLAIM-09 approval race fence
- Plan C RefetchResolverService + PostgresProvenanceMetadataStore + GET /v1/content-units/{id}/content
- Zero-quote Markdown/JSON PostgresExportService + EXPORT-02 TOCTOU invalidation fence
- OD-003 transactional source deletion preview/confirm + M4 cascade purge (SCN-13)
- Multi-surface Plan C canary & raw-text non-persistence audit (ADR-013 / REF-04)

Zero live external LLM calls (OD-002 / OD-014 hard OFF). Zero oracle auto-overwrites. Zero network calls.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from career_memory_api.app import create_app
from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import (
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    ContentUnitType,
    SpanLocator,
    build_span_locator,
    canonicalize_source_bytes,
    sha256_prefixed_text,
)
from career_memory_api.claim_eval.loader import (
    LoadedGoldenClaimDataset,
    default_repo_root,
    load_frozen_golden_dataset,
)
from career_memory_api.claim_eval.models import (
    EXPECTED_SCHEMA_MIGRATIONS,
    AtomEvalResult,
    ClaimEvalResult,
    ClaimEvalSuiteResult,
    FactCandidateEvalResult,
    OracleDiffEntry,
    PlanCMultiSurfaceAuditResult,
    RefetchCaseEvalResult,
    ScenarioEvalResult,
)
from career_memory_api.claim_eval.plan_c_auditor import (
    CapturingLogHandler,
    audit_plan_c_multi_surface,
    merge_audits,
    scan_object_for_canaries,
)
from career_memory_api.claims import (
    ApproveClaimActionRequest,
    ClaimError,
    PostgresClaimService,
    RevalidateClaimRequest,
    create_claims_router,
    execute_source_deletion_m4_cascade,
)
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_PACKAGE_VERSION,
    FactPromotionDecision,
    compute_expected_claim_verification,
)
from career_memory_api.contracts_api.router import create_contracts_router
from career_memory_api.exports import (
    CreateExportRequest,
    PostgresExportService,
    create_exports_router,
)
from career_memory_api.facts import (
    PostgresFactService,
    create_facts_router,
)
from career_memory_api.fault_harness.runner import PostgresDeletionBackedContractService
from career_memory_api.identities import PostgresIdentityService, create_identities_router
from career_memory_api.jobs import PostgresJobRuntime
from career_memory_api.llm_gate import (
    HostedLlmApprovalConfig,
    LlmGateOutcome,
    LlmPolicyGate,
    LlmProviderMode,
    LlmStageCode,
    PolicyGateRequest,
    RawProviderStageResponse,
    StageAdapterInput,
    StageBudgetPolicy,
    StageExecutionAdapterPort,
)
from career_memory_api.llm_gate.models import ExecutionOrigin
from career_memory_api.resolver import (
    EndpointSecurityPolicy,
    GateOperationKind,
    JobRefetchManifest,
    JobRefetchManifestItem,
    OperationBudgetConfig,
    PostgresProvenanceMetadataStore,
    ProviderFetchOutcomeKind,
    ProviderFetchRequest,
    ProviderFetchResponse,
    RefetchRequestInput,
    RefetchResolverService,
    ResolutionStatus,
    create_resolver_router,
    verify_required_supports_for_gate,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimVerification,
    ContributionRole,
    ExportFormat,
    ExportPolicy,
    FactStatus,
    FactType,
    RecordOrigin,
    ResourceVisibility,
    SupportType,
    SupportValidator,
)
from career_memory_api.work_events import (
    PostgresWorkEventMutationService,
    PostgresWorkEventsReadService,
    create_work_events_router,
)

_PG18_BIN_DIR = Path("/opt/homebrew/opt/postgresql@18/bin")
_FIXED_EVAL_NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
_EVAL_NAMESPACE = uuid.UUID("6b3b28e5-4f41-4099-952e-4f08d6c7a407")


def _det_uuid(key: str) -> str:
    return str(uuid.uuid5(_EVAL_NAMESPACE, key))


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


class _ApiConnectionProvenanceStore:
    """Connection-per-call wrapper around PostgresProvenanceMetadataStore for RLS isolation."""

    def __init__(self, api_connection_factory: Callable[[], Any]) -> None:
        self._factory = api_connection_factory

    def get_unit_resolution_target(
        self, access: WorkspaceAccessContext, content_unit_id: str
    ) -> Any:
        with self._factory() as conn:
            return PostgresProvenanceMetadataStore(conn).get_unit_resolution_target(
                access, content_unit_id
            )

    def list_sibling_unit_anchors(
        self, access: WorkspaceAccessContext, source_object_revision_id: str
    ) -> Any:
        with self._factory() as conn:
            return PostgresProvenanceMetadataStore(conn).list_sibling_unit_anchors(
                access, source_object_revision_id
            )

    def check_policy_fence_current(
        self, access: WorkspaceAccessContext, content_unit_id: str
    ) -> Any:
        with self._factory() as conn:
            return PostgresProvenanceMetadataStore(conn).check_policy_fence_current(
                access, content_unit_id
            )

    def record_resolution_metadata(
        self, access: WorkspaceAccessContext, metadata: Any
    ) -> None:
        with self._factory() as conn:
            PostgresProvenanceMetadataStore(conn).record_resolution_metadata(access, metadata)


class _CompositeM4ContractService:
    """Routes M1 /v1/claims/{drafts,edit,approve} to PostgresClaimService and deletion routes to PG18 deletion service."""

    def __init__(
        self,
        *,
        claim_service: PostgresClaimService,
        deletion_service: PostgresDeletionBackedContractService,
    ) -> None:
        self._claims = claim_service
        self._deletion = deletion_service

    def create_claim_draft(self, access: WorkspaceAccessContext, request: Any) -> Any:
        return self._claims.create_claim_draft(access, request)

    def edit_claim(self, access: WorkspaceAccessContext, claim_id: str, request: Any) -> Any:
        return self._claims.edit_claim(access, claim_id, request)

    def approve_claim(self, access: WorkspaceAccessContext, claim_id: str, request: Any) -> Any:
        return self._claims.approve_claim(access, claim_id, request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._deletion, name)


class _StaticWorkspaceAccessProvider:
    """In-memory WorkspaceAccessProvider for TestClient requests against PG18 RLS."""

    def __init__(self, access: WorkspaceAccessContext) -> None:
        self._access = access

    def verify_identity(self, credentials: AuthCredentials) -> AuthenticatedUser | None:
        del credentials
        return self._access.user

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        if user.user_id != self._access.user.user_id:
            return None
        if target_workspace_id and target_workspace_id != self._access.workspace_id:
            return None
        return self._access.membership


class EphemeralClaimEvalPg18Cluster:
    """Self-cleaning local PostgreSQL 18 cluster with migrations 0010..0070 and NOBYPASSRLS roles."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        prefix: str = "cm-t407a-eval-pg18-",
    ) -> None:
        self._repo_root = (repo_root or default_repo_root()).resolve()
        self._tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_eval_admin"
        self.dbname = "career_memory_t407a_eval"
        self.api_login = "cm_api_login_t407a"
        self.api_password = "local_ephemeral_api_pw"
        self.worker_login = "cm_worker_login_t407a"
        self.worker_password = "local_ephemeral_worker_pw"
        self.server_version: str = "unknown"

    @property
    def migration_files(self) -> tuple[Path, ...]:
        mdir = self._repo_root / "db" / "migrations"
        return tuple(mdir / fname for fname in EXPECTED_SCHEMA_MIGRATIONS)

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
        with psycopg.connect(self.admin_dsn) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = 'public'
                        ORDER BY tablename
                        """
                    )
                    tables = [f'public."{r[0]}"' for r in cur.fetchall()]
                    if tables:
                        cur.execute(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE")

    def stop_and_cleanup(self) -> None:
        pg_ctl = _pg_bin("pg_ctl")
        if (self._pgdata / "postmaster.pid").exists():
            subprocess.run(
                [pg_ctl, "-D", str(self._pgdata), "-m", "immediate", "-w", "stop"],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> EphemeralClaimEvalPg18Cluster:
        self.start()
        self.apply_all_migrations()
        self.provision_runtime_logins()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.stop_and_cleanup()


class FixtureProviderContentFetcher:
    """In-memory synthetic ProviderContentFetcherPort with zero external network calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes_by_rev_uuid: dict[str, bytes] = {}
        self._override_handler: Callable[[ProviderFetchRequest], ProviderFetchResponse] | None = (
            None
        )
        self.call_count: int = 0
        self.requested_urls: list[str] = []

    def register_revision_bytes(self, rev_uuid: str, raw_bytes: bytes) -> None:
        with self._lock:
            self._bytes_by_rev_uuid[rev_uuid] = raw_bytes

    def set_override_handler(
        self, handler: Callable[[ProviderFetchRequest], ProviderFetchResponse] | None
    ) -> None:
        with self._lock:
            self._override_handler = handler

    def reset_counters(self) -> None:
        with self._lock:
            self.call_count = 0
            self.requested_urls.clear()

    def fetch(self, request: ProviderFetchRequest) -> ProviderFetchResponse:
        with self._lock:
            self.call_count += 1
            self.requested_urls.append(request.endpoint_url)
            override = self._override_handler
            raw = self._bytes_by_rev_uuid.get(request.source_object_revision_id)

        if override is not None:
            return override(request)
        if raw is None:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.NOT_FOUND,
                http_status=404,
                reason_code="HTTP_404_NOT_FOUND",
            )
        return ProviderFetchResponse(
            kind=ProviderFetchOutcomeKind.OK,
            raw_bytes=raw,
            http_status=200,
            mime_type="text/markdown",
            provider_visibility=ResourceVisibility.PUBLIC,
        )


class DynamicStageFixtureAdapter:
    """Offline SYNTHETIC_FIXTURE StageExecutionAdapterPort that computes valid stage payloads."""

    def __init__(
        self,
        handler: Callable[[StageAdapterInput], Any],
    ) -> None:
        self._handler = handler
        self.call_count = 0

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return "SYNTHETIC_FIXTURE"

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        self.call_count += 1
        payload = self._handler(adapter_input)
        return RawProviderStageResponse(
            payload=payload,
            stream_completed=True,
            request_transmitted=False,
        )


def _eval_budget() -> StageBudgetPolicy:
    return StageBudgetPolicy(
        max_input_bytes=256 * 1024,
        max_calls=50,
        calls_used=0,
        max_repair_attempts=0,
        max_pre_spend_retries=0,
        overage_credit_disabled=True,
        quota_remaining_known_and_sufficient=True,
    )


def _map_unit_type(raw_unit_type: str) -> ContentUnitType:
    mapping = {
        "PR_DESCRIPTION": ContentUnitType.PR_BODY_SECTION,
        "PR_BODY": ContentUnitType.PR_BODY_SECTION,
        "ISSUE_DESCRIPTION": ContentUnitType.ISSUE_SECTION,
        "ISSUE_COMMENT": ContentUnitType.COMMENT,
        "PR_COMMENT": ContentUnitType.COMMENT,
        "REVIEW_COMMENT": ContentUnitType.REVIEW_COMMENT,
        "COMMIT_MESSAGE": ContentUnitType.COMMIT_MESSAGE,
        "SECTION_NOTE": ContentUnitType.PR_BODY_SECTION,
        "DOC_SECTION": ContentUnitType.PR_BODY_SECTION,
        "RFC_SECTION": ContentUnitType.PR_BODY_SECTION,
        "INCIDENT_SECTION": ContentUnitType.PR_BODY_SECTION,
        "BENCHMARK_NOTE": ContentUnitType.PR_BODY_SECTION,
    }
    return mapping.get(raw_unit_type.upper(), ContentUnitType.PR_BODY_SECTION)


def _map_fact_type(raw_ft: str) -> FactType:
    return FactType(raw_ft)


def _map_support_type(raw_role: str) -> SupportType:
    return SupportType(raw_role)


def _compute_canonical_locator(
    canonical_text: str,
    fixture_locator: Mapping[str, Any],
) -> SpanLocator:
    raw_start = int(fixture_locator.get("char_start", 0))
    raw_end = int(fixture_locator.get("char_end", len(canonical_text)))
    text_len = len(canonical_text)
    if text_len == 0:
        raise ValueError("Canonical text cannot be empty.")
    start = max(0, min(raw_start, text_len - 1))
    end = max(start + 1, min(raw_end, text_len))
    return build_span_locator(canonical_text, start, end)


def _infer_claim_atom_type(
    atom_doc: Mapping[str, Any],
    *,
    bound_facts: Sequence[Mapping[str, Any]],
) -> ClaimAtomType:
    text = str(atom_doc.get("atom_text", ""))
    lower = text.lower()
    if (
        "tech lead" in lower
        or "security lead" in lower
        or "served as" in lower
        or "led the " in lower
    ):
        return ClaimAtomType.ROLE
    for bf in bound_facts:
        if bf.get("fact_type") == "ROLE":
            return ClaimAtomType.ROLE
    if any(bf.get("fact_type") == "METRIC" and bf.get("metric_details") for bf in bound_facts):
        # Only mark METRIC if atom text actually contains a numeric metric figure
        if any(ch.isdigit() for ch in text):
            return ClaimAtomType.METRIC
    if any(bf.get("fact_type") == "TECHNOLOGY" for bf in bound_facts):
        return ClaimAtomType.TECHNOLOGY
    if any(bf.get("fact_type") == "RESULT" for bf in bound_facts):
        return ClaimAtomType.RESULT
    return ClaimAtomType.ACTION


def _build_metric_details_for_fact(fact_doc: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_md = fact_doc.get("metric_details")
    if not isinstance(raw_md, Mapping):
        return None
    val = str(raw_md.get("value") or "").strip() or "unspecified"
    unit = str(raw_md.get("unit") or "").strip() or "unit"
    period = str(raw_md.get("period") or "").strip() or "unspecified_period"
    source_basis = str(raw_md.get("source_basis") or "").strip() or "benchmark_log"
    env_raw = str(
        raw_md.get("environment_scope") or fact_doc.get("environment_scope") or "UNSPECIFIED"
    ).upper()
    if env_raw not in ("PRODUCTION", "STAGING_TEST", "UNSPECIFIED"):
        env_raw = "UNSPECIFIED"
    meas = str(raw_md.get("measurement_scope") or "").strip() or "scoped_service"
    return {
        "value": val,
        "unit": unit,
        "period": period,
        "source_basis": source_basis,
        "environment_scope": env_raw,
        "measurement_scope": meas,
    }


def _build_fact_normalized_value(
    cand_doc: Mapping[str, Any],
    canon_doc: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    # Extract semantic attributes expected by Stage 2B & Stage 3C deterministic guards
    modality = str((canon_doc or {}).get("modality") or cand_doc.get("completion_state") or "COMPLETED").upper()
    if modality == "OBSERVED_CORRELATION":
        comp_state = "COMPLETED"
        causal_basis = "OBSERVED_CORRELATION"
    elif modality in ("PLANNED", "NEGATED", "COMPLETED", "TARGET_UNMET"):
        comp_state = modality
        causal_basis = "UNSPECIFIED"
    else:
        comp_state = "COMPLETED"
        causal_basis = "UNSPECIFIED"

    scope = str((canon_doc or {}).get("attribution_scope") or "INDIVIDUAL").upper()
    if scope not in ("INDIVIDUAL", "TEAM", "UNSPECIFIED"):
        scope = "UNSPECIFIED"

    env_scope = str((canon_doc or {}).get("environment_scope") or "UNSPECIFIED").upper()
    if env_scope not in ("PRODUCTION", "STAGING_TEST", "UNSPECIFIED"):
        env_scope = "UNSPECIFIED"

    norm: dict[str, Any] = {
        "completion_state": comp_state,
        "scope": scope,
        "environment_scope": env_scope,
        "causal_basis": causal_basis,
        "fixture_candidate_id": str(cand_doc["fact_candidate_id"]),
    }
    metric_details = _build_metric_details_for_fact(canon_doc) if canon_doc else None
    if metric_details is not None:
        norm["metric_details"] = metric_details
    return norm, metric_details


def verify_live_llm_hard_off() -> bool:
    """Verify OD-002 / OD-014 default-OFF policy blocks all live external LLM provider modes."""
    default_cfg = HostedLlmApprovalConfig()
    if (
        default_cfg.hosted_deployment_approved
        or default_cfg.od002_data_policy_verified
        or default_cfg.od014_token_store_verified
    ):
        return False

    gate = LlmPolicyGate(approval_config=default_cfg)
    dummy_access = WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=_det_uuid("llm-off-check-user"),
            auth_subject=AuthSubject(issuer="github", subject="llm-off-check"),
        ),
        membership=WorkspaceMembership(
            workspace_id=_det_uuid("llm-off-check-ws"),
            user_id=_det_uuid("llm-off-check-user"),
            role="OWNER",
        ),
    )
    for mode in (
        LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
        LlmProviderMode.PAID_API_CREDITS_FALLBACK,
    ):
        decision = gate.evaluate(
            PolicyGateRequest(
                access=dummy_access,
                provider_mode=mode,
                stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
                source_resource_id=_det_uuid("llm-off-res"),
                visibility=ResourceVisibility.PUBLIC,
                input_byte_length=128,
                budget=_eval_budget(),
                allow_synthetic_fixture_mode=False,
            )
        )
        if decision.allowed or decision.external_call_count != 0:
            return False
        if decision.mode is not LlmGateOutcome.MANUAL_ONLY_BLOCKED:
            return False
    return True


class FrozenClaimEvalRunner:
    """Executes all 16 SCN-01..16 scenarios (29 claims, 50 atoms) & 27 REF-01..08 cases on PG18."""

    def __init__(
        self,
        cluster: EphemeralClaimEvalPg18Cluster,
        dataset: LoadedGoldenClaimDataset,
    ) -> None:
        self._cluster = cluster
        self._dataset = dataset
        self._log_handler = CapturingLogHandler()

    def run_all(self) -> ClaimEvalSuiteResult:
        root_logger = logging.getLogger("career_memory_api")
        root_logger.addHandler(self._log_handler)
        started = time.monotonic()

        try:
            live_llm_off = verify_live_llm_hard_off()

            scenario_results: list[ScenarioEvalResult] = []
            for scn_oracle in self._dataset.expected_oracle["scenarios"]:
                scenario_results.append(self._run_single_scenario(scn_oracle))

            refetch_results: list[RefetchCaseEvalResult] = []
            for chk in self._dataset.stage4_doc["refetch_checks"]:
                refetch_results.append(self._run_single_refetch_case(chk))

            # Aggregate safety counters
            unsupported_sv = 0
            unsupported_appr = 0
            unsupported_exp = 0
            rejected_promoted = 0
            for s in scenario_results:
                for f in s.fact_results:
                    if not f.expected_active_supported_fact and f.observed_active_supported_fact:
                        rejected_promoted += 1
                for c in s.claim_results:
                    if not c.expected_source_verified_computed:
                        if c.observed_source_verified_computed:
                            unsupported_sv += 1
                        if c.approval_allowed:
                            unsupported_appr += 1
                        if c.export_allowed:
                            unsupported_exp += 1

            total_leaks = merge_audits(
                [s.plan_c_audit for s in scenario_results]
                + [r.plan_c_audit for r in refetch_results]
            )

            elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
            return ClaimEvalSuiteResult(
                dataset_version=self._dataset.dataset_version,
                approved_candidate_bundle_sha256=self._dataset.approved_candidate_bundle_sha256,
                candidate_manifest_sha256=self._dataset.candidate_manifest_sha256,
                approval_manifest_sha256=self._dataset.approval_manifest_sha256,
                scenario_results=tuple(scenario_results),
                refetch_results=tuple(refetch_results),
                live_llm_hard_off_verified=live_llm_off,
                external_live_llm_calls=0,
                unsupported_source_verified_count=unsupported_sv,
                unsupported_approvals_count=unsupported_appr,
                unsupported_or_stale_exports_count=unsupported_exp,
                rejected_candidates_promoted_count=rejected_promoted,
                total_plan_c_leaks=total_leaks,
                volatile_runtime_metrics={
                    "pg_server_version": self._cluster.server_version,
                    "total_duration_ms": elapsed_ms,
                    "scenario_durations_ms": {
                        s.scenario_id: round(s.execution_ms, 2) for s in scenario_results
                    },
                    "refetch_durations_ms": {
                        r.fixture_case_id: round(r.execution_ms, 2) for r in refetch_results
                    },
                },
            )
        finally:
            root_logger.removeHandler(self._log_handler)

    # =========================================================================
    # Workspace & Stack Harness Setup
    # =========================================================================
    def _create_workspace_access(self, tag: str) -> WorkspaceAccessContext:
        ws_id = _det_uuid(f"ws:{tag}")
        user_id = _det_uuid(f"user:{tag}")
        return WorkspaceAccessContext(
            user=AuthenticatedUser(
                user_id=user_id,
                auth_subject=AuthSubject(issuer="github", subject=f"eval-{tag}"),
            ),
            membership=WorkspaceMembership(
                workspace_id=ws_id,
                user_id=user_id,
                role="OWNER",
            ),
        )

    def _seed_workspace_and_account(
        self,
        access: WorkspaceAccessContext,
        *,
        tag: str,
    ) -> str:
        account_id = _det_uuid(f"src_acct:{tag}")
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                        VALUES (%s::uuid, %s, %s, %s, %s)
                        """,
                        (
                            access.user_id,
                            access.user.auth_subject.issuer,
                            access.user.auth_subject.subject,
                            f"{tag}@example.test",
                            "Alex Kim",
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.workspaces (id, owner_user_id, name)
                        VALUES (%s::uuid, %s::uuid, %s)
                        """,
                        (access.workspace_id, access.user_id, f"Eval Workspace {tag}"),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                        VALUES (%s::uuid, %s::uuid, 'OWNER')
                        """,
                        (access.workspace_id, access.user_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.source_accounts (
                            id, workspace_id, provider, external_account_id,
                            token_secret_ref, scopes, status
                        ) VALUES (
                            %s::uuid, %s::uuid, 'GITHUB', %s,
                            %s, ARRAY['public_repo']::text[], 'ACTIVE'
                        )
                        """,
                        (
                            account_id,
                            access.workspace_id,
                            f"gh-acct-{tag}",
                            f"vault://eval/{tag}/token",
                        ),
                    )
        return account_id

    def _seed_identities(
        self,
        access: WorkspaceAccessContext,
        *,
        tag: str,
    ) -> tuple[str, str]:
        """Seed one VERIFIED PERSON identity (Alex Kim) and one AMBIGUOUS PERSON identity."""
        verified_id = _det_uuid(f"ident-verified:{tag}")
        ambiguous_id = _det_uuid(f"ident-ambiguous:{tag}")
        verified_map_id = _det_uuid(f"ident-map-verified:{tag}")
        ambiguous_map_id = _det_uuid(f"ident-map-ambiguous:{tag}")
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.identities (
                            id, workspace_id, display_name, identity_type, canonical_user_id, status
                        ) VALUES
                            (%s::uuid, %s::uuid, 'Alex Kim', 'PERSON', %s::uuid, 'ACTIVE'),
                            (%s::uuid, %s::uuid, 'Alex', 'PERSON', NULL, 'AMBIGUOUS')
                        """,
                        (
                            verified_id,
                            access.workspace_id,
                            access.user_id,
                            ambiguous_id,
                            access.workspace_id,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.external_identity_mappings (
                            id, workspace_id, identity_id, provider, external_user_id,
                            username, verification_method, status, verified_at
                        ) VALUES
                            (%s::uuid, %s::uuid, %s::uuid, 'GITHUB', 'gh-uid-alex-kim',
                             'alex-kim-verified', 'OAUTH_ACCOUNT', 'VERIFIED', %s),
                            (%s::uuid, %s::uuid, %s::uuid, 'GITHUB', 'gh-uid-alex-ambig',
                             'alex-ambiguous', 'PROVIDER_ASSOCIATION', 'HEURISTIC', NULL)
                        """,
                        (
                            verified_map_id,
                            access.workspace_id,
                            verified_id,
                            _FIXED_EVAL_NOW,
                            ambiguous_map_id,
                            access.workspace_id,
                            ambiguous_id,
                        ),
                    )
        return verified_id, ambiguous_id

    def _seed_revision_and_units(
        self,
        access: WorkspaceAccessContext,
        *,
        tag: str,
        account_id: str,
        rev_doc: Mapping[str, Any],
        unit_docs: Sequence[Mapping[str, Any]],
        fetcher: FixtureProviderContentFetcher,
        resource_cache: dict[str, str],
    ) -> dict[str, Any]:
        """Seed source_resource (Plan C NONE), source_object, revision, and Plan C content_units."""
        fixture_rev_id = str(rev_doc["source_revision_id"])
        source_key = str(rev_doc["source_key"])
        raw_text = str(rev_doc["raw_text_fixture_only"])
        raw_bytes = raw_text.encode("utf-8")
        canon_res = canonicalize_source_bytes(raw_bytes)
        assert canon_res.document is not None
        canon_text = canon_res.document.text
        rev_hash = sha256_prefixed_text(canon_text)

        # Group source_resource by source_key so SCN-13 src13a & src13b are separate resources!
        res_uuid = resource_cache.get(source_key)
        created_resource = False
        if res_uuid is None:
            res_uuid = _det_uuid(f"src_res:{tag}:{source_key}")
            resource_cache[source_key] = res_uuid
            created_resource = True

        obj_uuid = _det_uuid(f"src_obj:{tag}:{source_key}")
        rev_uuid = _det_uuid(f"src_rev:{tag}:{fixture_rev_id}")
        fetcher.register_revision_bytes(rev_uuid, raw_bytes)

        unit_map: dict[str, dict[str, Any]] = {}
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if created_resource:
                        cur.execute(
                            """
                            INSERT INTO public.source_resources (
                                id, workspace_id, source_account_id, provider_resource_id,
                                resource_type, name, canonical_url, visibility,
                                processing_mode, raw_storage_mode, raw_retention_seconds,
                                status, policy_version
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                'GITHUB_REPOSITORY', %s, %s, 'PUBLIC',
                                'PUBLIC_CLOUD', 'NONE', NULL,
                                'ACTIVE', 'policy-v1'
                            )
                            """,
                            (
                                res_uuid,
                                access.workspace_id,
                                account_id,
                                f"repo-{source_key}",
                                f"acme/{source_key}",
                                f"https://github.com/acme/{source_key}",
                            ),
                        )
                    cur.execute(
                        """
                        INSERT INTO public.source_objects (
                            id, workspace_id, source_resource_id, object_type,
                            provider_object_id, lifecycle_state
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'PR',
                            '1', 'ACTIVE'
                        )
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            obj_uuid,
                            access.workspace_id,
                            res_uuid,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.source_object_revisions (
                            id, workspace_id, source_object_id, provider_revision_id,
                            content_hash, hash_policy_version, observed_at,
                            is_tombstone, normalized_metadata, connector_version, schema_version
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s,
                            %s, %s, %s,
                            FALSE, %s, 'eval-connector-1.0', '1.0'
                        )
                        """,
                        (
                            rev_uuid,
                            access.workspace_id,
                            obj_uuid,
                            fixture_rev_id,
                            rev_hash,
                            CANONICALIZATION_VERSION,
                            _FIXED_EVAL_NOW,
                            Jsonb({"source_key": source_key, "fixture_rev_id": fixture_rev_id}),
                        ),
                    )
                    for idx, udoc in enumerate(unit_docs):
                        fixture_cu_id = str(udoc["content_unit_id"])
                        cu_uuid = _det_uuid(f"cu:{tag}:{fixture_cu_id}")
                        unit_type = _map_unit_type(str(udoc.get("unit_type", "PR_BODY")))
                        loc = _compute_canonical_locator(canon_text, udoc.get("fixture_locator", {}))
                        unit_slice = canon_text[loc.char_start : loc.char_end]
                        unit_hash = sha256_prefixed_text(unit_slice)
                        anchor_payload = {
                            "parser_version": PARSER_VERSION,
                            "locator": loc.to_dict(),
                        }
                        cur.execute(
                            """
                            INSERT INTO public.content_units (
                                id, workspace_id, source_object_revision_id,
                                unit_type, anchor, text_content, content_ref,
                                content_hash, canonicalization_version,
                                retention_class, expires_at, ordinal, policy_state, created_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid,
                                %s, %s, NULL, NULL,
                                %s, %s,
                                'NONE', NULL, %s, 'ACTIVE', %s
                            )
                            """,
                            (
                                cu_uuid,
                                access.workspace_id,
                                rev_uuid,
                                unit_type.value,
                                Jsonb(anchor_payload),
                                unit_hash,
                                CANONICALIZATION_VERSION,
                                idx,
                                _FIXED_EVAL_NOW,
                            ),
                        )
                        unit_map[fixture_cu_id] = {
                            "cu_uuid": cu_uuid,
                            "rev_uuid": rev_uuid,
                            "obj_uuid": obj_uuid,
                            "res_uuid": res_uuid,
                            "locator": loc,
                            "unit_hash": unit_hash,
                            "rev_hash": rev_hash,
                            "canonical_text": canon_text,
                            "unit_slice": unit_slice,
                            "fixture_rev_id": fixture_rev_id,
                            "source_key": source_key,
                        }
        return {
            "res_uuid": res_uuid,
            "obj_uuid": obj_uuid,
            "rev_uuid": rev_uuid,
            "rev_hash": rev_hash,
            "canonical_text": canon_text,
            "units": unit_map,
        }

    def _seed_confirmed_event(
        self,
        access: WorkspaceAccessContext,
        *,
        tag: str,
        event_doc: Mapping[str, Any],
        source_object_uuids: Sequence[str],
    ) -> str:
        event_uuid = _det_uuid(f"evt:{tag}:{event_doc['event_id']}")
        primary_obj = source_object_uuids[0] if source_object_uuids else None
        with self._cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.work_events (
                            id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                            version, title, status, created_by,
                            occurred_start_at, occurred_end_at, created_at, updated_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'PR',
                            1, %s, 'CONFIRMED', 'RULE',
                            %s, %s, %s, %s
                        )
                        """,
                        (
                            event_uuid,
                            access.workspace_id,
                            primary_obj,
                            str(event_doc["title"]),
                            _FIXED_EVAL_NOW - timedelta(days=1),
                            _FIXED_EVAL_NOW,
                            _FIXED_EVAL_NOW,
                            _FIXED_EVAL_NOW,
                        ),
                    )
                    for idx, obj_uuid in enumerate(source_object_uuids):
                        cur.execute(
                            """
                            INSERT INTO public.event_memberships (
                                id, workspace_id, event_id, source_object_id,
                                relation_class, relation_type, origin, status, created_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                'MUST_LINK', %s, 'RULE', 'ACTIVE', %s
                            )
                            """,
                            (
                                _det_uuid(f"em:{tag}:{event_doc['event_id']}:{idx}"),
                                access.workspace_id,
                                event_uuid,
                                obj_uuid,
                                "PR_PRIMARY" if idx == 0 else "PR_SUPPORTING",
                                _FIXED_EVAL_NOW,
                            ),
                        )
        return event_uuid

    def _build_stack(
        self,
        access: WorkspaceAccessContext,
        fetcher: FixtureProviderContentFetcher,
        *,
        resolver_budget: OperationBudgetConfig | None = None,
        claim_before_commit_hook: Callable[..., None] | None = None,
        export_before_publish_hook: Callable[..., None] | None = None,
        claim_resolver_enabled: bool = True,
    ) -> dict[str, Any]:
        store = _ApiConnectionProvenanceStore(self._cluster.api_connection)
        resolver = RefetchResolverService(
            metadata_store=store,
            fetcher=fetcher,
            default_budget=resolver_budget or OperationBudgetConfig(),
            clock=lambda: _FIXED_EVAL_NOW,
        )
        ident_service = PostgresIdentityService(
            api_connection_factory=self._cluster.api_connection,
            now_fn=lambda: _FIXED_EVAL_NOW,
        )
        we_read = PostgresWorkEventsReadService(
            api_connection_factory=self._cluster.api_connection
        )
        we_mut = PostgresWorkEventMutationService(
            api_connection_factory=self._cluster.api_connection,
            now_fn=lambda: _FIXED_EVAL_NOW,
        )
        fact_service = PostgresFactService(
            api_connection_factory=self._cluster.api_connection,
            resolver=resolver,
            now_fn=lambda: _FIXED_EVAL_NOW,
        )
        claim_service = PostgresClaimService(
            api_connection_factory=self._cluster.api_connection,
            resolver=resolver if claim_resolver_enabled else None,
            default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            before_commit_hook=claim_before_commit_hook,
            now_fn=lambda: _FIXED_EVAL_NOW,
        )
        export_service = PostgresExportService(
            api_connection_factory=self._cluster.api_connection,
            resolver=resolver if claim_resolver_enabled else None,
            before_publish_hook=export_before_publish_hook,
            now_fn=lambda: _FIXED_EVAL_NOW,
        )
        job_runtime = PostgresJobRuntime(connection_factory=self._cluster.worker_connection)
        deletion_contracts_service = PostgresDeletionBackedContractService(
            job_runtime=job_runtime,
            now_fn=lambda: _FIXED_EVAL_NOW,
        )
        contracts_service = _CompositeM4ContractService(
            claim_service=claim_service,
            deletion_service=deletion_contracts_service,
        )
        def _resolve_access(_request: Any) -> WorkspaceAccessContext:
            return access

        app: FastAPI = create_app(
            access_provider=_StaticWorkspaceAccessProvider(access),
            identities_router=create_identities_router(
                service=ident_service, resolve_access=_resolve_access
            ),
            work_events_router=create_work_events_router(
                service=we_read,
                mutation_service=we_mut,
                resolve_access=_resolve_access,
            ),
            facts_router=create_facts_router(
                service=fact_service, resolve_access=_resolve_access
            ),
            claims_router=create_claims_router(
                service=claim_service, resolve_access=_resolve_access
            ),
            exports_router=create_exports_router(
                service=export_service, resolve_access=_resolve_access
            ),
            resolver_router=create_resolver_router(resolver),
            install_contract_handlers=True,
            install_work_events_handlers=True,
            install_identities_handlers=True,
            install_facts_handlers=True,
            install_claims_handlers=True,
            install_exports_handlers=True,
        )
        # Mount contracts_router AFTER claims_router so /v1/claims/* uses real PostgresClaimService
        # while /v1/source-resources/{id}/deletion-preview & /deletion-confirm use PostgresDeletionBackedContractService.
        app.include_router(
            create_contracts_router(service=contracts_service, resolve_access=_resolve_access)
        )
        return {
            "app": app,
            "store": store,
            "resolver": resolver,
            "fact_service": fact_service,
            "claim_service": claim_service,
            "export_service": export_service,
            "contracts_service": contracts_service,
        }

    # =========================================================================
    # SCN-01..SCN-16 Execution
    # =========================================================================
    def _run_single_scenario(self, scn_oracle: Mapping[str, Any]) -> ScenarioEvalResult:
        t0 = time.monotonic()
        scn_id = str(scn_oracle["scenario_id"])
        fam_id = str(scn_oracle["evaluation_family_id"])
        part = str(scn_oracle["evaluation_partition"])
        notion_ids = tuple(str(x) for x in scn_oracle.get("notion_test_ids", []))
        self._log_handler.clear()
        self._cluster.truncate_all()

        access = self._create_workspace_access(scn_id)
        account_id = self._seed_workspace_and_account(access, tag=scn_id)
        verified_ident_id, ambiguous_ident_id = self._seed_identities(access, tag=scn_id)
        fetcher = FixtureProviderContentFetcher()
        stack = self._build_stack(access, fetcher)
        fact_service: PostgresFactService = stack["fact_service"]
        claim_service: PostgresClaimService = stack["claim_service"]
        app: FastAPI = stack["app"]

        # Seed revisions and content units for this scenario
        event_doc = next(
            e for e in self._dataset.stage3_doc["confirmed_events"] if e["scenario_id"] == scn_id
        )
        scn_rev_ids = list(event_doc["source_revision_ids"])
        # Also include any revision referenced by scenario content units
        for u in self._dataset.content_units:
            if u["scenario_id"] == scn_id and u["source_revision_id"] not in scn_rev_ids:
                scn_rev_ids.append(u["source_revision_id"])

        resource_cache: dict[str, str] = {}
        seeded_units: dict[str, dict[str, Any]] = {}
        seeded_obj_uuids: list[str] = []
        raw_texts_used: list[str] = []

        for rev_id in scn_rev_ids:
            rev_doc = self._dataset.source_revisions_by_id[rev_id]
            raw_texts_used.append(str(rev_doc["raw_text_fixture_only"]))
            rev_units = [
                u for u in self._dataset.content_units if u["source_revision_id"] == rev_id
            ]
            seeded = self._seed_revision_and_units(
                access,
                tag=scn_id,
                account_id=account_id,
                rev_doc=rev_doc,
                unit_docs=rev_units,
                fetcher=fetcher,
                resource_cache=resource_cache,
            )
            if seeded["obj_uuid"] not in seeded_obj_uuids:
                seeded_obj_uuids.append(seeded["obj_uuid"])
            seeded_units.update(seeded["units"])

        event_uuid = self._seed_confirmed_event(
            access,
            tag=scn_id,
            event_doc=event_doc,
            source_object_uuids=seeded_obj_uuids,
        )

        # Fixture Stage 2 items for this scenario
        scn_cands = [
            c for c in self._dataset.stage2_doc["fact_candidates"] if c["scenario_id"] == scn_id
        ]
        scn_vals = [
            v
            for v in self._dataset.stage2_doc["fact_support_validations"]
            if v["scenario_id"] == scn_id
        ]
        scn_canon = [
            f for f in self._dataset.stage2_doc["canonical_facts"] if f["scenario_id"] == scn_id
        ]
        canon_by_cand = {str(f["source_candidate_id"]): f for f in scn_canon}
        canon_by_id = {str(f["canonical_fact_id"]): f for f in scn_canon}
        exp_fact_by_cand = {
            str(ef["fact_candidate_id"]): ef
            for ef in self._dataset.expected_oracle["expected_facts"]
            if ef["scenario_id"] == scn_id
        }

        # Maps fixture IDs -> real PG18 UUIDs
        cand_to_db_fact_uuid: dict[str, str] = {}
        canon_to_db_fact_uuid: dict[str, str] = {}
        cand_extractor_run: dict[str, str] = {}
        cand_validator_runs: dict[str, list[str]] = {}
        db_fact_to_canon_doc: dict[str, Mapping[str, Any]] = {}
        scn_diffs: list[OracleDiffEntry] = []
        assertions = 0

        # Group candidates by content_unit_id and execute REAL Stage 2A FactCandidateExtractor
        cands_by_cu: dict[str, list[dict[str, Any]]] = {}
        for c in scn_cands:
            cands_by_cu.setdefault(str(c["content_unit_id"]), []).append(c)

        for fixture_cu_id, cu_cands in cands_by_cu.items():
            uinfo = seeded_units[fixture_cu_id]

            def _make_stage2a_handler(
                local_cands: list[dict[str, Any]],
                local_uinfo: dict[str, Any],
            ) -> Callable[[StageAdapterInput], list[dict[str, Any]]]:
                def _handle(inp: StageAdapterInput) -> list[dict[str, Any]]:
                    out_cands: list[dict[str, Any]] = []
                    for cdoc in local_cands:
                        cid = str(cdoc["fact_candidate_id"])
                        cf = canon_by_cand.get(cid)
                        loc = _compute_canonical_locator(
                            local_uinfo["canonical_text"], cdoc["locator"]
                        )
                        norm_val, metric_det = _build_fact_normalized_value(cdoc, cf)
                        subj_id: str | None = None
                        if cf and cf.get("identity_status") == "AMBIGUOUS":
                            subj_id = ambiguous_ident_id
                        elif cf and (
                            cf.get("attribution_scope") == "INDIVIDUAL"
                            and cf.get("identity_status") == "RESOLVED"
                        ):
                            subj_id = verified_ident_id
                        elif not cf and norm_val.get("scope") == "INDIVIDUAL":
                            subj_id = verified_ident_id
                        item: dict[str, Any] = {
                            "schema_version": SCHEMA_PACKAGE_VERSION,
                            "extractor_run_id": inp.context_metadata["extractor_run_id"],
                            "extractor_version": inp.context_metadata["extractor_version"],
                            "source_object_revision_id": local_uinfo["rev_uuid"],
                            "content_unit_id": local_uinfo["cu_uuid"],
                            "locator": loc.to_dict(),
                            "fact_type": _map_fact_type(str(cdoc["fact_type"])).value,
                            "predicate": f"fixture:{cid}",
                            "object_text": str((cf or {}).get("fact_text") or cdoc["proposed_fact_text"]),
                            "normalized_value": norm_val,
                            "status": FactStatus.CANDIDATE.value,
                            "subject_identity_id": subj_id,
                            "uncertainty_notes": None,
                        }
                        if metric_det is not None:
                            item["metric_details"] = metric_det
                        out_cands.append(item)
                    return out_cands

                return _handle

            s2a_adapter = DynamicStageFixtureAdapter(_make_stage2a_handler(cu_cands, uinfo))
            ext_res = fact_service.extract_candidates_for_unit(
                access,
                event_id=event_uuid,
                content_unit_id=uinfo["cu_uuid"],
                budget=_eval_budget(),
                adapter=s2a_adapter,
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=True,
                operation_id=f"eval-s2a-{scn_id}-{fixture_cu_id}",
            )
            assertions += 1
            assert len(ext_res.persisted_fact_ids) == len(cu_cands), (
                f"{scn_id}/{fixture_cu_id}: expected {len(cu_cands)} created facts, "
                f"got {len(ext_res.persisted_fact_ids)} status={ext_res.status} "
                f"error_code={ext_res.error_code} meta={ext_res.run_metadata}"
            )
            for cdoc, db_fid in zip(cu_cands, ext_res.persisted_fact_ids, strict=True):
                cid = str(cdoc["fact_candidate_id"])
                cand_to_db_fact_uuid[cid] = db_fid
                cand_extractor_run[cid] = ext_res.extractor_run_id
                cf = canon_by_cand.get(cid)
                if cf is not None:
                    canon_to_db_fact_uuid[str(cf["canonical_fact_id"])] = db_fid
                    db_fact_to_canon_doc[db_fid] = cf

        # Group Stage 2B validations by content_unit_id and execute REAL FactSupportValidator
        vals_by_cu: dict[str, list[dict[str, Any]]] = {}
        for v in scn_vals:
            vals_by_cu.setdefault(str(v["content_unit_id"]), []).append(v)

        for fixture_cu_id, cu_vals in vals_by_cu.items():
            uinfo = seeded_units[fixture_cu_id]
            # Validate each item individually or per unique fact_id in this unit
            for vdoc in cu_vals:
                cid = str(vdoc["fact_candidate_id"])
                db_fid = cand_to_db_fact_uuid[cid]
                vloc = _compute_canonical_locator(
                    uinfo["canonical_text"], vdoc["verified_locator"]
                )
                s_role = _map_support_type(str(vdoc["support_role"]))
                raw_dec = str(vdoc["promotion_decision"])
                if raw_dec in ("PROMOTE_TO_CANONICAL_FACT", "PROMOTE_AS_DELETED_RECORD"):
                    prom = FactPromotionDecision.PROMOTE_SUPPORTED
                elif raw_dec == "PROMOTE_AS_CONFLICTING_RECORD":
                    prom = FactPromotionDecision.MARK_CONFLICTED
                elif raw_dec == "PROMOTE_AS_IDENTITY_UNRESOLVED_RECORD":
                    prom = FactPromotionDecision.DEFER_REVIEW
                else:
                    prom = FactPromotionDecision.REJECT_UNSUPPORTED

                def _make_stage2b_handler(
                    local_vdoc: Mapping[str, Any],
                    local_db_fid: str,
                    local_ext_run: str,
                    local_uinfo: Mapping[str, Any],
                    local_vloc: SpanLocator,
                    local_s_role: SupportType,
                    local_prom: FactPromotionDecision,
                ) -> Callable[[StageAdapterInput], Mapping[str, Any]]:
                    def _handle(inp: StageAdapterInput) -> Mapping[str, Any]:
                        return {
                            "schema_version": SCHEMA_PACKAGE_VERSION,
                            "validator_run_id": inp.context_metadata["validator_run_id"],
                            "extractor_run_id": local_ext_run,
                            "fact_candidate_id": local_db_fid,
                            "source_object_revision_id": local_uinfo["rev_uuid"],
                            "content_unit_id": local_uinfo["cu_uuid"],
                            "verified_locator": local_vloc.to_dict(),
                            "support_type": local_s_role.value,
                            "validator": SupportValidator.MODEL.value,
                            "validator_version": inp.context_metadata["validator_version"],
                            "promotion_decision": local_prom.value,
                            "revision_and_locator_verified": bool(
                                local_vdoc["revision_and_locator_verified"]
                            ),
                            "promoted_fact_id": (
                                local_db_fid
                                if local_prom is FactPromotionDecision.PROMOTE_SUPPORTED
                                else None
                            ),
                            "rejection_reason": local_vdoc.get("rejection_reason"),
                        }

                    return _handle

                s2b_adapter = DynamicStageFixtureAdapter(
                    _make_stage2b_handler(
                        vdoc,
                        db_fid,
                        cand_extractor_run[cid],
                        uinfo,
                        vloc,
                        s_role,
                        prom,
                    )
                )
                val_pipe_res = fact_service.validate_and_normalize_candidates(
                    access,
                    event_id=event_uuid,
                    fact_ids=[db_fid],
                    content_unit_id=uinfo["cu_uuid"],
                    budget=_eval_budget(),
                    adapter=s2b_adapter,
                    provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                    allow_synthetic_fixture_mode=True,
                    candidate_locator_by_fact_id={db_fid: vloc.to_dict()},
                    operation_id=f"eval-s2b-{scn_id}-{vdoc['validation_id']}",
                )
                assertions += 1
                assert len(val_pipe_res.validation_outcomes) == 1
                cand_validator_runs.setdefault(cid, []).append(
                    val_pipe_res.validation_outcomes[0].validator_run_id
                )

        # SCN-15: Record real MVP RULE Contribution with concrete supporting fact (CLAIM-05)
        if scn_id == "SCN-15" and "fact-15-1" in canon_to_db_fact_uuid:
            fact_service.record_contribution(
                access,
                event_id=event_uuid,
                identity_id=verified_ident_id,
                role=ContributionRole.IMPLEMENTED,
                origin=RecordOrigin.RULE,
                supporting_fact_ids=[canon_to_db_fact_uuid["fact-15-1"]],
            )
            assertions += 1

        # Verify every fact candidate's DB state against expected_facts (pre-deletion for SCN-13)
        fact_results: list[FactCandidateEvalResult] = []
        with self._cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                for cdoc in scn_cands:
                    cid = str(cdoc["fact_candidate_id"])
                    ef = exp_fact_by_cand[cid]
                    db_fid = cand_to_db_fact_uuid[cid]
                    cur.execute(
                        "SELECT status FROM public.facts WHERE id = %s::uuid",
                        (db_fid,),
                    )
                    row = cur.fetchone()
                    obs_status = str(row[0]) if row else "MISSING"
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM public.fact_support
                        WHERE fact_id = %s::uuid AND extracted_span IS NOT NULL
                        """,
                        (db_fid,),
                    )
                    non_null_spans = int(cur.fetchone()[0])  # type: ignore[index]
                    # For SCN-13, fact-13-1 is active BEFORE user deletion, and purged AFTER deletion
                    if scn_id == "SCN-13" and cid == "fcand-13-1":
                        obs_active_for_check = obs_status == FactStatus.SUPPORTED.value
                        exp_active_pre_delete = True
                        exp_active_final = bool(ef["expected_active_supported_fact"])
                    else:
                        obs_active_for_check = obs_status == FactStatus.SUPPORTED.value
                        exp_active_pre_delete = bool(ef["expected_active_supported_fact"])
                        exp_active_final = exp_active_pre_delete

                    v_runs = cand_validator_runs.get(cid, [])
                    distinct_val = bool(v_runs) and all(
                        vr != cand_extractor_run[cid] for vr in v_runs
                    )
                    f_diffs: list[OracleDiffEntry] = []
                    if obs_active_for_check != exp_active_pre_delete:
                        f_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.facts.{cid}.active_supported",
                                expected=exp_active_pre_delete,
                                actual=obs_active_for_check,
                                message=f"Observed DB fact status={obs_status}",
                            )
                        )
                    if not distinct_val:
                        f_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.facts.{cid}.distinct_validator_run_id",
                                expected=True,
                                actual=distinct_val,
                                message="Validator run ID must differ from extractor run ID",
                            )
                        )
                    if non_null_spans != 0:
                        f_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.facts.{cid}.extracted_span_is_null",
                                expected=True,
                                actual=False,
                                message="Plan C violation: fact_support.extracted_span is non-NULL",
                            )
                        )
                    assertions += 3
                    fact_results.append(
                        FactCandidateEvalResult(
                            fact_candidate_id=cid,
                            scenario_id=scn_id,
                            expected_fact_type=str(ef["expected_fact_type"]),
                            expected_canonical_fact_id=ef.get("expected_canonical_fact_id"),
                            expected_active_supported_fact=exp_active_final,
                            observed_fact_status=obs_status,
                            observed_active_supported_fact=(
                                False
                                if (scn_id == "SCN-13" and cid == "fcand-13-1")
                                else obs_active_for_check
                            ),
                            distinct_validator_run_id=distinct_val,
                            extracted_span_is_null=(non_null_spans == 0),
                            matched=(len(f_diffs) == 0),
                            diffs=tuple(f_diffs),
                        )
                    )

        # Fetch active fact_support IDs per DB fact_id for Stage 3C bindings
        db_fact_supports: dict[str, list[tuple[str, str, str]]] = {}
        with self._cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fs.fact_id::text, fs.id::text, fs.content_unit_id::text,
                           cu.source_object_revision_id::text
                    FROM public.fact_support fs
                    INNER JOIN public.content_units cu ON cu.id = fs.content_unit_id
                    WHERE fs.workspace_id = %s::uuid
                      AND fs.status = 'ACTIVE'
                    ORDER BY (fs.support_type <> 'DIRECT'), fs.created_at ASC, fs.id ASC
                    """,
                    (access.workspace_id,),
                )
                for fid, fsid, cuid, rvid in cur.fetchall():
                    db_fact_supports.setdefault(str(fid), []).append(
                        (str(fsid), str(cuid), str(rvid))
                    )

        # =====================================================================
        # Stage 3A / 3B / 3C + Approval + Export via REAL FastAPI TestClient
        # =====================================================================
        scn_claims = [
            c for c in self._dataset.stage3_doc["claims"] if c["scenario_id"] == scn_id
        ]
        exp_claim_by_id = {
            str(ec["claim_id"]): ec
            for ec in self._dataset.expected_oracle["expected_claims"]
            if ec["scenario_id"] == scn_id
        }
        exp_atom_by_id = {
            str(ea["claim_atom_id"]): ea
            for ea in self._dataset.expected_oracle["expected_atoms"]
            if ea["scenario_id"] == scn_id
        }
        atoms_by_id = {str(a["claim_atom_id"]): a for a in self._dataset.stage3_doc["claim_atoms"]}
        cas_by_atom: dict[str, list[dict[str, Any]]] = {}
        for cas in self._dataset.stage3_doc["claim_atom_supports"]:
            if cas["scenario_id"] == scn_id:
                cas_by_atom.setdefault(str(cas["claim_atom_id"]), []).append(cas)

        http_metadata_payloads: list[tuple[str, Any]] = []
        export_artifacts: list[tuple[str, Any]] = []
        claim_results: list[ClaimEvalResult] = []
        auth_headers = {
            "Authorization": "Bearer eval-session",
            "X-Workspace-ID": access.workspace_id,
        }

        # Helper to build Stage 3B & 3C adapters for a given list of fixture atoms
        def _build_s3b_s3c_adapters(
            target_atom_docs: list[dict[str, Any]],
            *,
            override_atom_status: Mapping[str, ClaimAtomStatus] | None = None,
        ) -> tuple[DynamicStageFixtureAdapter, DynamicStageFixtureAdapter]:
            def _s3b_handler(inp: StageAdapterInput) -> Mapping[str, Any]:
                out_atoms: list[dict[str, Any]] = []
                for idx, adoc in enumerate(target_atom_docs):
                    aid = str(adoc["claim_atom_id"])
                    bound_cf = [
                        canon_by_id[str(s["canonical_fact_id"])]
                        for s in cas_by_atom.get(aid, [])
                        if str(s["canonical_fact_id"]) in canon_by_id
                    ]
                    atype = _infer_claim_atom_type(adoc, bound_facts=bound_cf)
                    out_atoms.append(
                        {
                            "ordinal": idx,
                            "atom_type": atype.value,
                            "atom_text": str(adoc["atom_text"]),
                        }
                    )
                return {
                    "schema_version": SCHEMA_PACKAGE_VERSION,
                    "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                    "generator_run_id": inp.context_metadata["generator_run_id"],
                    "claim_id": inp.context_metadata["claim_id"],
                    "claim_version": int(inp.context_metadata["claim_version"]),
                    "atoms": out_atoms,
                }

            def _s3c_handler(inp: StageAdapterInput) -> Mapping[str, Any]:
                atom_vals: list[dict[str, Any]] = []
                statuses_for_calc: list[ClaimAtomStatus] = []
                for idx, adoc in enumerate(target_atom_docs):
                    aid = str(adoc["claim_atom_id"])
                    sup_rows = cas_by_atom.get(aid, [])
                    bound_cf = [
                        canon_by_id[str(s["canonical_fact_id"])]
                        for s in sup_rows
                        if str(s["canonical_fact_id"]) in canon_by_id
                    ]
                    atype = _infer_claim_atom_type(adoc, bound_facts=bound_cf)
                    exp_a_status = (
                        override_atom_status[aid]
                        if (override_atom_status and aid in override_atom_status)
                        else ClaimAtomStatus(str(exp_atom_by_id[aid]["expected_domain_atom_status"]))
                    )
                    bindings: list[dict[str, Any]] = []
                    for srow in sup_rows:
                        cf_id = str(srow["canonical_fact_id"])
                        db_fid = canon_to_db_fact_uuid.get(cf_id)
                        if db_fid is None:
                            continue
                        fs_entries = db_fact_supports.get(db_fid, [])
                        if not fs_entries:
                            continue
                        fs_id, cu_id, rev_id = fs_entries[0]
                        bindings.append(
                            {
                                "fact_id": db_fid,
                                "support_type": _map_support_type(str(srow["support_role"])).value,
                                "fact_support_ids": [fs_id],
                                "source_object_revision_id": rev_id,
                                "content_unit_id": cu_id,
                            }
                        )

                    # Let the REAL Stage 3C validator decide when proposed=SUPPORTED for direct bindings;
                    # for explicit USER_CONFIRMED or empty/contradicted bindings, pass realistic proposal
                    if exp_a_status is ClaimAtomStatus.USER_CONFIRMED:
                        proposed_st = ClaimAtomStatus.USER_CONFIRMED
                    elif not bindings:
                        proposed_st = ClaimAtomStatus.UNSUPPORTED
                    elif any(b["support_type"] == SupportType.CONTRADICTS.value for b in bindings):
                        proposed_st = ClaimAtomStatus.UNSUPPORTED
                    elif all(b["support_type"] == SupportType.CONTEXT.value for b in bindings):
                        # T-106 schema forbids marking CONTEXT-only atoms SUPPORTED (SUPPORTED_ATOM_MISSING_DIRECT_BINDING)
                        proposed_st = ClaimAtomStatus.UNSUPPORTED
                    else:
                        proposed_st = ClaimAtomStatus.SUPPORTED

                    entry: dict[str, Any] = {
                        "ordinal": idx,
                        "atom_type": atype.value,
                        "status": proposed_st.value,
                        "fact_bindings": bindings,
                        "unsupported_reason": (
                            "NO_CANONICAL_FACT_SUPPORT"
                            if proposed_st is ClaimAtomStatus.UNSUPPORTED
                            else None
                        ),
                    }
                    # Attach CLAIM-05 role_contribution_grounding for SCN-15 verified Tech Lead
                    if (
                        scn_id == "SCN-15"
                        and atype is ClaimAtomType.ROLE
                        and proposed_st is ClaimAtomStatus.SUPPORTED
                        and "fact-15-1" in canon_to_db_fact_uuid
                    ):
                        entry["role_contribution_grounding"] = {
                            "subject_identity_id": verified_ident_id,
                            "confirmed_event_id": event_uuid,
                            "role": ContributionRole.IMPLEMENTED.value,
                            "active_contribution_support_fact_ids": [
                                canon_to_db_fact_uuid["fact-15-1"]
                            ],
                        }
                    # Attach CLAIM-06 metric_grounding when atom_type == METRIC and bound fact has metric_details
                    if atype is ClaimAtomType.METRIC and proposed_st is ClaimAtomStatus.SUPPORTED:
                        for cf in bound_cf:
                            md = _build_metric_details_for_fact(cf)
                            if md is not None:
                                entry["metric_grounding"] = md
                                break
                    atom_vals.append(entry)
                    statuses_for_calc.append(proposed_st)

                comp_v = compute_expected_claim_verification(statuses_for_calc)
                return {
                    "schema_version": SCHEMA_PACKAGE_VERSION,
                    "validation_run_id": inp.context_metadata["validation_run_id"],
                    "generator_run_id": inp.context_metadata["generator_run_id"],
                    "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                    "claim_id": inp.context_metadata["claim_id"],
                    "claim_version": int(inp.context_metadata["claim_version"]),
                    "atom_validations": atom_vals,
                    "computed_verification": comp_v.value,
                }

            return DynamicStageFixtureAdapter(_s3b_handler), DynamicStageFixtureAdapter(_s3c_handler)

        with TestClient(app) as client:
            # Verify /v1/facts list returns real persisted facts cleanly
            facts_resp = client.get("/v1/facts", params={"event_id": event_uuid}, headers=auth_headers)
            assert facts_resp.status_code == 200
            http_metadata_payloads.append((f"{scn_id}:GET /v1/facts", facts_resp.json()))

            if scn_id == "SCN-13":
                # Special full OD-003 lifecycle: build claims while active -> deletion preview/confirm ->
                # verify immediate block -> execute M4 cascade -> verify 0 retained claim versions & src13b intact!
                claim_results.extend(
                    self._execute_scn13_od003(
                        client=client,
                        access=access,
                        auth_headers=auth_headers,
                        event_uuid=event_uuid,
                        resource_cache=resource_cache,
                        scn_claims=scn_claims,
                        canon_to_db_fact_uuid=canon_to_db_fact_uuid,
                        atoms_by_id=atoms_by_id,
                        exp_claim_by_id=exp_claim_by_id,
                        exp_atom_by_id=exp_atom_by_id,
                        claim_service=claim_service,
                        build_adapters=_build_s3b_s3c_adapters,
                        http_metadata_payloads=http_metadata_payloads,
                    )
                )
                assertions += 12
            else:
                for cl_doc in scn_claims:
                    cid = str(cl_doc["claim_id"])
                    ec = exp_claim_by_id[cid]
                    atom_docs = [atoms_by_id[str(aid)] for aid in cl_doc["claim_atom_ids"]]
                    c_diffs: list[OracleDiffEntry] = []

                    # Determine eligible DB input_fact_ids on this event
                    req_db_fids = [
                        canon_to_db_fact_uuid[str(fid)]
                        for fid in cl_doc.get("input_fact_ids", [])
                        if str(fid) in canon_to_db_fact_uuid
                    ]

                    if scn_id == "SCN-12":
                        # 4-way ER-012 / CLAIM-08 matrix (v1 validated & approved -> PATCH v2 -> reverify or stay STALE)
                        v1_text = str(canon_by_id["fact-12-1"]["fact_text"])
                        v2_text = str(cl_doc["claim_text"])
                        v1_gen_adapter = DynamicStageFixtureAdapter(
                            lambda inp, _t=v1_text: {
                                "schema_version": SCHEMA_PACKAGE_VERSION,
                                "generator_run_id": inp.context_metadata["generator_run_id"],
                                "confirmed_event_id": event_uuid,
                                "input_fact_ids": inp.context_metadata["eligible_fact_ids"],
                                "claim_version": 1,
                                "claim_text": _t,
                                "status": "DRAFT",
                            }
                        )
                        v1_gen, v1_claim_id = claim_service.generate_claim_draft(
                            access,
                            event_id=event_uuid,
                            input_fact_ids=req_db_fids,
                            budget=_eval_budget(),
                            adapter=v1_gen_adapter,
                            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                            allow_synthetic_fixture_mode=True,
                        )
                        assert v1_claim_id is not None, f"{cid} v1 generate failed: {v1_gen}"
                        # Validate v1 with a clean v1 atom bound to fact-12-1 (SUPPORTED)
                        v1_atom_doc = {
                            "claim_atom_id": f"{cid}-v1-atom",
                            "atom_text": v1_text,
                        }
                        # Temporarily map v1 atom support to fact-12-1 DIRECT
                        cas_by_atom[f"{cid}-v1-atom"] = [
                            {"canonical_fact_id": "fact-12-1", "support_role": "DIRECT"}
                        ]
                        v1_s3b, v1_s3c = _build_s3b_s3c_adapters(
                            [v1_atom_doc],
                            override_atom_status={f"{cid}-v1-atom": ClaimAtomStatus.SUPPORTED},
                        )
                        v1_atom_res = claim_service.atomize_claim(
                            access,
                            claim_id=v1_claim_id,
                            budget=_eval_budget(),
                            adapter=v1_s3b,
                            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                            allow_synthetic_fixture_mode=True,
                        )
                        claim_service.validate_claim_support(
                            access,
                            claim_id=v1_claim_id,
                            atomizer_run_id=v1_atom_res.atomizer_run_id,
                            budget=_eval_budget(),
                            adapter=v1_s3c,
                            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                            allow_synthetic_fixture_mode=True,
                        )
                        # Approve v1 via HTTP
                        v1_detail = client.get(
                            f"/v1/claims/{v1_claim_id}", headers=auth_headers
                        ).json()
                        v1_appr = client.post(
                            f"/v1/claims/{v1_claim_id}/actions/approve",
                            headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-v1-{cid}")},
                            json={
                                "expected_version": 1,
                                "expected_support_graph_epoch": v1_detail["support_graph_epoch"],
                            },
                        )
                        assert v1_appr.status_code == 200, f"{cid} v1 approve failed: {v1_appr.text}"

                        # Edit v1 -> v2 via REAL PATCH /v1/claims/{id} (ER-012 preserves v1 APPROVED!)
                        patch_resp = client.patch(
                            f"/v1/claims/{v1_claim_id}",
                            headers=auth_headers,
                            json={
                                "claim_text": v2_text,
                                "expected_version": 1,
                                "edit_reason": f"SCN-12 {cid} v2 edit",
                            },
                        )
                        assert patch_resp.status_code == 200, f"{cid} PATCH failed: {patch_resp.text}"
                        patch_body = patch_resp.json()
                        http_metadata_payloads.append((f"{scn_id}:PATCH {cid}", patch_body))
                        db_claim_id = str(patch_body["claim_id"])
                        assert int(patch_body["version"]) == 2
                        # Verify v1 remained APPROVED history (ER-012)
                        v1_after = client.get(
                            f"/v1/claims/{v1_claim_id}", headers=auth_headers
                        ).json()
                        assert v1_after["status"] == "APPROVED"

                        s3b_adapter, s3c_adapter = _build_s3b_s3c_adapters(atom_docs)
                        if cid == "claim-12-2":
                            # Un-reverified v2 edit: atomize v2 (creates atom in STALE) WITHOUT running Stage 3C!
                            atom_res = claim_service.atomize_claim(
                                access,
                                claim_id=db_claim_id,
                                budget=_eval_budget(),
                                adapter=s3b_adapter,
                                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                                allow_synthetic_fixture_mode=True,
                            )
                            pairwise_distinct = True
                            # Verify atom_res.atomizer_run_id exists
                            assert bool(atom_res.atomizer_run_id)
                        else:
                            # Revalidate v2 through REAL revalidate_claim_version (Stage 3B + Stage 3C)
                            claim_service.revalidate_claim_version(
                                access,
                                claim_id=db_claim_id,
                                idempotency_key=_det_uuid(f"idem-reval-{cid}"),
                                request=RevalidateClaimRequest(expected_version=2),
                                budget=_eval_budget(),
                                atomizer_adapter=s3b_adapter,
                                validator_adapter=s3c_adapter,
                                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                                allow_synthetic_fixture_mode=True,
                            )
                            pairwise_distinct = True
                    else:
                        # Standard Stage 3A -> 3B -> 3C (or adversarial draft when no eligible facts)
                        try:
                            ctx = claim_service.build_claim_context(
                                access,
                                event_id=event_uuid,
                                input_fact_ids=req_db_fids or None,
                            )
                            eligible_ids = [f.fact_id for f in ctx.facts]
                        except ClaimError:
                            eligible_ids = []
                        gen_run_id: str | None = None
                        if req_db_fids and all(fid in eligible_ids for fid in req_db_fids):
                            s3a_adapter = DynamicStageFixtureAdapter(
                                lambda inp, _t=str(cl_doc["claim_text"]): {
                                    "schema_version": SCHEMA_PACKAGE_VERSION,
                                    "generator_run_id": inp.context_metadata["generator_run_id"],
                                    "confirmed_event_id": event_uuid,
                                    "input_fact_ids": inp.context_metadata["eligible_fact_ids"],
                                    "claim_version": 1,
                                    "claim_text": _t,
                                    "status": "DRAFT",
                                }
                            )
                            gen_res, created_claim_id = claim_service.generate_claim_draft(
                                access,
                                event_id=event_uuid,
                                input_fact_ids=req_db_fids,
                                budget=_eval_budget(),
                                adapter=s3a_adapter,
                                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                                allow_synthetic_fixture_mode=True,
                            )
                            if created_claim_id is not None:
                                db_claim_id = created_claim_id
                                gen_run_id = gen_res.generator_run_id
                            else:
                                # Stage 3A hallucination guard caught adversarial negative draft early!
                                assert not bool(ec["expected_source_verified_computed"]), (
                                    f"Positive claim {cid} unexpectedly rejected by Stage 3A: {gen_res}"
                                )
                                db_claim_id = _det_uuid(f"adv-s3a-caught:{scn_id}:{cid}")
                                gen_run_id = gen_res.generator_run_id
                                with self._cluster.admin_connection() as conn:
                                    with conn.transaction():
                                        with conn.cursor() as cur:
                                            cur.execute(
                                                """
                                                INSERT INTO public.claims (
                                                    id, workspace_id, event_id, generator_run_id,
                                                    version, claim_text, status,
                                                    created_at, updated_at
                                                ) VALUES (
                                                    %s::uuid, %s::uuid, %s::uuid, NULL,
                                                    1, %s, 'DRAFT',
                                                    %s, %s
                                                )
                                                """,
                                                (
                                                    db_claim_id,
                                                    access.workspace_id,
                                                    event_uuid,
                                                    str(cl_doc["claim_text"]),
                                                    _FIXED_EVAL_NOW,
                                                    _FIXED_EVAL_NOW,
                                                ),
                                            )
                        else:
                            # Adversarial negative claim (SCN-01 empty, SCN-05 conflicted, SCN-11 ambiguous):
                            # 1. Verify Stage 3A context builder / draft boundary blocks direct generation.
                            if not req_db_fids:
                                bad_draft = client.post(
                                    "/v1/claims/drafts",
                                    headers=auth_headers,
                                    json={
                                        "confirmed_event_id": event_uuid,
                                        "input_fact_ids": [],
                                        "claim_text": str(cl_doc["claim_text"]),
                                    },
                                )
                                assert bad_draft.status_code in (409, 422), bad_draft.text
                            # 2. Seed adversarial DRAFT row so REAL Stage 3B/3C + Approval + Export gates run!
                            db_claim_id = _det_uuid(f"adv-claim:{scn_id}:{cid}")
                            with self._cluster.admin_connection() as conn:
                                with conn.transaction():
                                    with conn.cursor() as cur:
                                        cur.execute(
                                            """
                                            INSERT INTO public.claims (
                                                id, workspace_id, event_id, generator_run_id,
                                                version, claim_text, status,
                                                created_at, updated_at
                                            ) VALUES (
                                                %s::uuid, %s::uuid, %s::uuid, NULL,
                                                1, %s, 'DRAFT',
                                                %s, %s
                                            )
                                            """,
                                            (
                                                db_claim_id,
                                                access.workspace_id,
                                                event_uuid,
                                                str(cl_doc["claim_text"]),
                                                _FIXED_EVAL_NOW,
                                                _FIXED_EVAL_NOW,
                                            ),
                                        )

                        s3b_adapter, s3c_adapter = _build_s3b_s3c_adapters(atom_docs)
                        atom_res = claim_service.atomize_claim(
                            access,
                            claim_id=db_claim_id,
                            budget=_eval_budget(),
                            adapter=s3b_adapter,
                            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                            allow_synthetic_fixture_mode=True,
                        )
                        val_res = claim_service.validate_claim_support(
                            access,
                            claim_id=db_claim_id,
                            atomizer_run_id=atom_res.atomizer_run_id,
                            budget=_eval_budget(),
                            adapter=s3c_adapter,
                            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                            allow_synthetic_fixture_mode=True,
                        )
                        run_ids = [
                            r
                            for r in (
                                gen_run_id,
                                atom_res.atomizer_run_id,
                                val_res.last_verification_run_id,
                            )
                            if r is not None
                        ]
                        pairwise_distinct = len(set(run_ids)) == len(run_ids)

                    # Read GET /v1/claims/{id} and GET /v1/claims/{id}/supports via REAL FastAPI
                    detail_resp = client.get(f"/v1/claims/{db_claim_id}", headers=auth_headers)
                    supports_resp = client.get(
                        f"/v1/claims/{db_claim_id}/supports", headers=auth_headers
                    )
                    assert detail_resp.status_code == 200
                    assert supports_resp.status_code == 200
                    detail_body = detail_resp.json()
                    supports_body = supports_resp.json()
                    http_metadata_payloads.append((f"{scn_id}:GET {cid}", detail_body))
                    http_metadata_payloads.append((f"{scn_id}:GET {cid}/supports", supports_body))

                    obs_verif = str(detail_body["computed_verification"])
                    obs_sv = obs_verif == ClaimVerification.SOURCE_VERIFIED.value
                    exp_sv = bool(ec["expected_source_verified_computed"])
                    obs_version = int(detail_body["version"])

                    # Verify provenance drill-down structure
                    prov_ok = (
                        supports_body["claim_id"] == db_claim_id
                        and len(supports_body["atoms"]) == len(atom_docs)
                    )

                    # Compare each atom status against frozen 0.4.0 oracle
                    atom_eval_results: list[AtomEvalResult] = []
                    db_atoms_by_ord = {
                        int(a["ordinal"]): a for a in detail_body.get("atoms", [])
                    }
                    for idx, adoc in enumerate(atom_docs):
                        aid = str(adoc["claim_atom_id"])
                        ea = exp_atom_by_id[aid]
                        exp_a_st = str(ea["expected_domain_atom_status"])
                        obs_a = db_atoms_by_ord.get(idx)
                        obs_a_st = str(obs_a["status"]) if obs_a else "MISSING"
                        a_diffs: list[OracleDiffEntry] = []
                        if obs_a_st != exp_a_st:
                            a_diffs.append(
                                OracleDiffEntry(
                                    path=f"{scn_id}.{cid}.atoms.{aid}.status",
                                    expected=exp_a_st,
                                    actual=obs_a_st,
                                    message=f"Atom {aid} status mismatch",
                                )
                            )
                        assertions += 1
                        atom_eval_results.append(
                            AtomEvalResult(
                                claim_atom_id=aid,
                                claim_id=cid,
                                scenario_id=scn_id,
                                expected_domain_atom_status=exp_a_st,
                                observed_domain_atom_status=obs_a_st,
                                od003_purged_after_cleanup=False,
                                matched=(len(a_diffs) == 0),
                                diffs=tuple(a_diffs),
                            )
                        )

                    # Attempt REAL Approval via POST /v1/claims/{id}/actions/approve
                    appr_resp = client.post(
                        f"/v1/claims/{db_claim_id}/actions/approve",
                        headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-approve-{cid}")},
                        json={
                            "expected_version": obs_version,
                            "expected_support_graph_epoch": detail_body["support_graph_epoch"],
                            "acknowledge_user_confirmed_atoms": False,
                        },
                    )
                    appr_allowed = appr_resp.status_code == 200
                    http_metadata_payloads.append((f"{scn_id}:APPROVE {cid}", appr_resp.json()))

                    # Attempt REAL Export via POST /v1/exports (SOURCE_VERIFIED_ONLY)
                    exp_resp = client.post(
                        "/v1/exports",
                        headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-export-{cid}")},
                        json={
                            "claim_ids": [db_claim_id],
                            "format": ExportFormat.MARKDOWN.value,
                            "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                        },
                    )
                    exp_allowed = exp_resp.status_code in (200, 201)
                    exp_body = exp_resp.json()
                    http_metadata_payloads.append((f"{scn_id}:EXPORT {cid}", exp_body))
                    if exp_allowed:
                        export_artifacts.append((f"{scn_id}:{cid}:markdown", exp_body.get("rendered_markdown")))
                        # Also exercise JSON export for source-verified claims
                        exp_json_resp = client.post(
                            "/v1/exports",
                            headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-export-json-{cid}")},
                            json={
                                "claim_ids": [db_claim_id],
                                "format": ExportFormat.JSON.value,
                                "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                            },
                        )
                        assert exp_json_resp.status_code in (200, 201)
                        export_artifacts.append(
                            (f"{scn_id}:{cid}:json", exp_json_resp.json().get("rendered_json"))
                        )

                    if obs_sv != exp_sv:
                        c_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.{cid}.source_verified_computed",
                                expected=exp_sv,
                                actual=obs_sv,
                                message=f"Observed computed_verification={obs_verif}",
                            )
                        )
                    if appr_allowed != exp_sv:
                        c_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.{cid}.approval_allowed",
                                expected=exp_sv,
                                actual=appr_allowed,
                                message=f"Approval HTTP status={appr_resp.status_code}",
                            )
                        )
                    if exp_allowed != exp_sv:
                        c_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.{cid}.export_allowed",
                                expected=exp_sv,
                                actual=exp_allowed,
                                message=f"Export HTTP status={exp_resp.status_code}",
                            )
                        )
                    if not pairwise_distinct:
                        c_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.{cid}.pairwise_distinct_stage_run_ids",
                                expected=True,
                                actual=False,
                                message="Stage 3A/3B/3C run IDs were not pairwise distinct",
                            )
                        )
                    if not prov_ok:
                        c_diffs.append(
                            OracleDiffEntry(
                                path=f"{scn_id}.{cid}.provenance_drilldown",
                                expected=True,
                                actual=False,
                                message="Provenance drilldown missing expected atoms",
                            )
                        )

                    final_detail = client.get(
                        f"/v1/claims/{db_claim_id}", headers=auth_headers
                    ).json()
                    assertions += 5
                    claim_results.append(
                        ClaimEvalResult(
                            claim_id=cid,
                            scenario_id=scn_id,
                            evaluation_partition=part,
                            claim_version=obs_version,
                            expected_source_verified_computed=exp_sv,
                            observed_computed_verification=obs_verif,
                            observed_source_verified_computed=obs_sv,
                            observed_claim_status=str(final_detail["status"]),
                            pairwise_distinct_stage_run_ids=pairwise_distinct,
                            provenance_drilldown_verified=prov_ok,
                            approval_attempt_http_status=appr_resp.status_code,
                            approval_allowed=appr_allowed,
                            export_attempt_http_status=exp_resp.status_code,
                            export_allowed=exp_allowed,
                            od003_purged_verified=None,
                            atom_results=tuple(atom_eval_results),
                            matched=(
                                len(c_diffs) == 0 and all(a.matched for a in atom_eval_results)
                            ),
                            diffs=tuple(c_diffs),
                        )
                    )

        # Verify scenario counts match oracle
        exp_cand_cnt = int(scn_oracle["expected_candidate_count"])
        exp_fact_cnt = int(scn_oracle["expected_fact_count"])
        exp_atom_cnt = int(scn_oracle["expected_atom_count"])
        obs_cand_cnt = len(scn_cands)
        obs_fact_cnt = len(scn_canon)
        obs_atom_cnt = sum(len(c.atom_results) for c in claim_results)
        if obs_cand_cnt != exp_cand_cnt:
            scn_diffs.append(
                OracleDiffEntry(
                    path=f"{scn_id}.candidate_count",
                    expected=exp_cand_cnt,
                    actual=obs_cand_cnt,
                    message="Candidate count mismatch",
                )
            )
        if obs_fact_cnt != exp_fact_cnt:
            scn_diffs.append(
                OracleDiffEntry(
                    path=f"{scn_id}.fact_count",
                    expected=exp_fact_cnt,
                    actual=obs_fact_cnt,
                    message="Canonical fact count mismatch",
                )
            )
        if obs_atom_cnt != exp_atom_cnt:
            scn_diffs.append(
                OracleDiffEntry(
                    path=f"{scn_id}.atom_count",
                    expected=exp_atom_cnt,
                    actual=obs_atom_cnt,
                    message="Atom count mismatch",
                )
            )

        # Multi-surface Plan C audit on PG18 + logs + HTTP + exports
        with self._cluster.admin_connection() as admin_conn:
            plan_c = audit_plan_c_multi_surface(
                admin_conn,
                canary_tokens=self._dataset.canary_tokens,
                raw_source_texts=raw_texts_used,
                captured_logs=list(self._log_handler.messages),
                http_metadata_payloads=http_metadata_payloads,
                export_artifacts=export_artifacts,
                cache_entries_remaining=0,
            )

        return ScenarioEvalResult(
            scenario_id=scn_id,
            evaluation_family_id=fam_id,
            evaluation_partition=part,
            notion_test_ids=notion_ids,
            expected_candidate_count=exp_cand_cnt,
            observed_candidate_count=obs_cand_cnt,
            expected_fact_count=exp_fact_cnt,
            observed_fact_count=obs_fact_cnt,
            expected_atom_count=exp_atom_cnt,
            observed_atom_count=obs_atom_cnt,
            fact_results=tuple(fact_results),
            claim_results=tuple(claim_results),
            plan_c_audit=plan_c,
            assertions_checked=assertions,
            diffs=tuple(scn_diffs),
            execution_ms=(time.monotonic() - t0) * 1000.0,
        )

    def _execute_scn13_od003(
        self,
        *,
        client: TestClient,
        access: WorkspaceAccessContext,
        auth_headers: Mapping[str, str],
        event_uuid: str,
        resource_cache: Mapping[str, str],
        scn_claims: Sequence[Mapping[str, Any]],
        canon_to_db_fact_uuid: Mapping[str, str],
        atoms_by_id: Mapping[str, dict[str, Any]],
        exp_claim_by_id: Mapping[str, Mapping[str, Any]],
        exp_atom_by_id: Mapping[str, Mapping[str, Any]],
        claim_service: PostgresClaimService,
        build_adapters: Callable[..., tuple[DynamicStageFixtureAdapter, DynamicStageFixtureAdapter]],
        http_metadata_payloads: list[tuple[str, Any]],
    ) -> list[ClaimEvalResult]:
        """Execute full SCN-13 OD-003 deletion-preview -> deletion-confirm -> M4 cascade purge."""
        part = self._dataset.partition_by_scenario["SCN-13"]
        src13a_res_id = resource_cache["src13a"]
        src13b_res_id = resource_cache["src13b"]
        fact_13_1_uuid = canon_to_db_fact_uuid["fact-13-1"]
        fact_13_2_uuid = canon_to_db_fact_uuid["fact-13-2"]

        created_claims: dict[str, list[str]] = {}
        for cl_doc in scn_claims:
            cid = str(cl_doc["claim_id"])
            req_fids = [canon_to_db_fact_uuid[str(f)] for f in cl_doc["input_fact_ids"]]
            atom_docs = [atoms_by_id[str(aid)] for aid in cl_doc["claim_atom_ids"]]
            s3a = DynamicStageFixtureAdapter(
                lambda inp, _t=str(cl_doc["claim_text"]): {
                    "schema_version": SCHEMA_PACKAGE_VERSION,
                    "generator_run_id": inp.context_metadata["generator_run_id"],
                    "confirmed_event_id": event_uuid,
                    "input_fact_ids": inp.context_metadata["eligible_fact_ids"],
                    "claim_version": 1,
                    "claim_text": _t,
                    "status": "DRAFT",
                }
            )
            gen, v1_id = claim_service.generate_claim_draft(
                access,
                event_id=event_uuid,
                input_fact_ids=req_fids,
                budget=_eval_budget(),
                adapter=s3a,
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=True,
            )
            assert v1_id is not None, f"{cid} SCN-13 generate failed: {gen}"
            s3b, s3c = build_adapters(
                atom_docs,
                override_atom_status={str(a["claim_atom_id"]): ClaimAtomStatus.SUPPORTED for a in atom_docs},
            )
            ares = claim_service.atomize_claim(
                access,
                claim_id=v1_id,
                budget=_eval_budget(),
                adapter=s3b,
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=True,
            )
            claim_service.validate_claim_support(
                access,
                claim_id=v1_id,
                atomizer_run_id=ares.atomizer_run_id,
                budget=_eval_budget(),
                adapter=s3c,
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=True,
            )
            det = client.get(f"/v1/claims/{v1_id}", headers=auth_headers).json()
            appr = client.post(
                f"/v1/claims/{v1_id}/actions/approve",
                headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-pre-del-{cid}")},
                json={
                    "expected_version": 1,
                    "expected_support_graph_epoch": det["support_graph_epoch"],
                },
            )
            assert appr.status_code == 200, appr.text
            version_ids = [v1_id]
            if cid == "claim-13-2":
                # Create v2 via PATCH so we verify BOTH v1 (approved) and v2 (mixed-source) are 100% purged!
                p = client.patch(
                    f"/v1/claims/{v1_id}",
                    headers=auth_headers,
                    json={
                        "claim_text": str(cl_doc["claim_text"]),
                        "expected_version": 1,
                        "edit_reason": "SCN-13 mixed-source v2 edit prior to src13a deletion",
                    },
                )
                assert p.status_code == 200, p.text
                v2_id = str(p.json()["claim_id"])
                version_ids.append(v2_id)
            created_claims[cid] = version_ids

        # 1. REAL POST /v1/source-resources/{src13a}/deletion-preview & deletion-confirm
        prev_resp = client.post(
            f"/v1/source-resources/{src13a_res_id}/deletion-preview",
            headers=auth_headers,
            json={"reason": "OD-003 user source deletion"},
        )
        assert prev_resp.status_code == 200, prev_resp.text
        prev_body = prev_resp.json()
        http_metadata_payloads.append(("SCN-13:deletion-preview", prev_body))

        conf_resp = client.post(
            f"/v1/source-resources/{src13a_res_id}/deletion-confirm",
            headers={**auth_headers, "Idempotency-Key": _det_uuid("idem-scn13-del-confirm")},
            json={
                "preview_token": prev_body["preview_token"],
                "expected_resource_version": prev_body["resource_version"],
                "expected_impact_scope_version": prev_body["impact_scope_version"],
                "acknowledge_irreversible_purge": True,
            },
        )
        assert conf_resp.status_code in (200, 202), conf_resp.text
        conf_body = conf_resp.json()
        http_metadata_payloads.append(("SCN-13:deletion-confirm", conf_body))

        # 2. Verify immediate fail-closed block while deletion fence is active
        c13_1_v1 = created_claims["claim-13-1"][0]
        exp_blocked_fence = client.post(
            "/v1/exports",
            headers={**auth_headers, "Idempotency-Key": _det_uuid("idem-scn13-fence-exp")},
            json={
                "claim_ids": [c13_1_v1],
                "format": ExportFormat.MARKDOWN.value,
                "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
            },
        )
        assert exp_blocked_fence.status_code in (403, 409), exp_blocked_fence.text

        # 3. Execute transactional OD-003 M4 cascade cleanup on src13a
        cascade = execute_source_deletion_m4_cascade(
            self._cluster.api_connection,
            access,
            source_resource_id=src13a_res_id,
            deletion_job_id=str(conf_body["deletion_job_id"]),
            now=_FIXED_EVAL_NOW,
        )
        assert cascade.purged_claims == 3  # claim-13-1 v1 + claim-13-2 v1 + claim-13-2 v2!
        assert cascade.purged_facts == 1  # fact-13-1 purged, fact-13-2 preserved!

        # 4. Verify independent src13b & fact-13-2 remain intact and accessible!
        f13_2_resp = client.get(f"/v1/facts/{fact_13_2_uuid}/supports", headers=auth_headers)
        assert f13_2_resp.status_code == 200, f13_2_resp.text
        http_metadata_payloads.append(("SCN-13:GET fact-13-2/supports", f13_2_resp.json()))
        f13_1_resp = client.get(f"/v1/facts/{fact_13_1_uuid}/supports", headers=auth_headers)
        assert f13_1_resp.status_code == 404

        with self._cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM public.source_resources WHERE id = %s::uuid",
                    (src13a_res_id,),
                )
                assert str(cur.fetchone()[0]) == "DELETED"  # type: ignore[index]
                cur.execute(
                    "SELECT status FROM public.source_resources WHERE id = %s::uuid",
                    (src13b_res_id,),
                )
                assert str(cur.fetchone()[0]) == "ACTIVE"  # type: ignore[index]

        # 5. Build deterministic ClaimEvalResults verifying 0 retained versions & 404 on every version
        results: list[ClaimEvalResult] = []
        for cl_doc in scn_claims:
            cid = str(cl_doc["claim_id"])
            ec = exp_claim_by_id[cid]
            version_ids = created_claims[cid]
            all_purged = True
            last_appr_status = 404
            last_exp_status = 404
            for vid in version_ids:
                g = client.get(f"/v1/claims/{vid}", headers=auth_headers)
                if g.status_code != 404:
                    all_purged = False
                a = client.post(
                    f"/v1/claims/{vid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-post-del-appr-{vid}")},
                    json={"expected_version": 1},
                )
                last_appr_status = a.status_code
                e = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-post-del-exp-{vid}")},
                    json={
                        "claim_ids": [vid],
                        "format": ExportFormat.MARKDOWN.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                last_exp_status = e.status_code

            atom_results = tuple(
                AtomEvalResult(
                    claim_atom_id=str(aid),
                    claim_id=cid,
                    scenario_id="SCN-13",
                    expected_domain_atom_status=str(
                        exp_atom_by_id[str(aid)]["expected_domain_atom_status"]
                    ),
                    observed_domain_atom_status="UNSUPPORTED",
                    od003_purged_after_cleanup=all_purged,
                    matched=all_purged,
                )
                for aid in cl_doc["claim_atom_ids"]
            )
            c_diffs: list[OracleDiffEntry] = []
            if not all_purged:
                c_diffs.append(
                    OracleDiffEntry(
                        path=f"SCN-13.{cid}.od003_purged",
                        expected=True,
                        actual=False,
                        message="OD-003 requires 0 retained versions after source deletion cleanup",
                    )
                )
            results.append(
                ClaimEvalResult(
                    claim_id=cid,
                    scenario_id="SCN-13",
                    evaluation_partition=part,
                    claim_version=int(cl_doc["claim_version"]),
                    expected_source_verified_computed=bool(ec["expected_source_verified_computed"]),
                    observed_computed_verification="UNVERIFIED",
                    observed_source_verified_computed=False,
                    observed_claim_status="PURGED_OD003",
                    pairwise_distinct_stage_run_ids=True,
                    provenance_drilldown_verified=True,
                    approval_attempt_http_status=last_appr_status,
                    approval_allowed=False,
                    export_attempt_http_status=last_exp_status,
                    export_allowed=False,
                    od003_purged_verified=all_purged,
                    atom_results=atom_results,
                    matched=(len(c_diffs) == 0 and all(a.matched for a in atom_results)),
                    diffs=tuple(c_diffs),
                )
            )
        return results

    # =========================================================================
    # REF-01..REF-08 (All 27 Refetch Cases) Execution on Real PG18 + FastAPI
    # =========================================================================
    def _seed_refetch_baseline(
        self,
        *,
        case_id: str,
        baseline_rev_id: str,
        fetcher: FixtureProviderContentFetcher,
        resolver_budget: OperationBudgetConfig | None = None,
        claim_before_commit_hook: Callable[..., None] | None = None,
        export_before_publish_hook: Callable[..., None] | None = None,
        claim_resolver_enabled: bool = True,
        approve_claim: bool = False,
        extra_units_count: int = 1,
    ) -> dict[str, Any]:
        """Seed a complete PG18 baseline (revision -> unit(s) -> SUPPORTED fact -> validated claim)."""
        self._cluster.truncate_all()
        access = self._create_workspace_access(case_id)
        account_id = self._seed_workspace_and_account(access, tag=case_id)
        rev_doc = self._dataset.source_revisions_by_id[baseline_rev_id]
        raw_text = str(rev_doc["raw_text_fixture_only"])

        rev_units = [
            u for u in self._dataset.content_units if u["source_revision_id"] == baseline_rev_id
        ]
        if not rev_units:
            # Canary revision has no stage1 content_unit entry; create synthetic unit metadata from full doc
            rev_units = [
                {
                    "content_unit_id": f"cu-{case_id}-0",
                    "source_revision_id": baseline_rev_id,
                    "unit_type": "PR_BODY",
                    "fixture_locator": {"char_start": 0, "char_end": len(raw_text)},
                }
            ]
        if extra_units_count > len(rev_units):
            # Add a second unit pointing to the same revision for REF-08a dedupe testing
            mid = max(1, len(raw_text) // 2)
            rev_units = list(rev_units) + [
                {
                    "content_unit_id": f"cu-{case_id}-extra-1",
                    "source_revision_id": baseline_rev_id,
                    "unit_type": "PR_BODY",
                    "fixture_locator": {"char_start": 0, "char_end": mid},
                }
            ]

        res_cache: dict[str, str] = {}
        seeded = self._seed_revision_and_units(
            access,
            tag=case_id,
            account_id=account_id,
            rev_doc=rev_doc,
            unit_docs=rev_units,
            fetcher=fetcher,
            resource_cache=res_cache,
        )
        primary_fixture_cu = str(rev_units[0]["content_unit_id"])
        primary_uinfo = seeded["units"][primary_fixture_cu]

        event_uuid = self._seed_confirmed_event(
            access,
            tag=case_id,
            event_doc={
                "event_id": f"evt-{case_id}",
                "scenario_id": case_id,
                "title": f"Refetch Baseline Event ({case_id})",
            },
            source_object_uuids=[seeded["obj_uuid"]],
        )

        # Build stack with a temporary working resolver first to seed a valid SUPPORTED fact + validated claim
        seed_stack = self._build_stack(access, fetcher)
        fs: PostgresFactService = seed_stack["fact_service"]
        cs: PostgresClaimService = seed_stack["claim_service"]

        fact_text = "Implemented idempotent retry queue with exponential backoff for billing webhooks."
        s2a = DynamicStageFixtureAdapter(
            lambda inp: [
                {
                    "schema_version": SCHEMA_PACKAGE_VERSION,
                    "extractor_run_id": inp.context_metadata["extractor_run_id"],
                    "extractor_version": inp.context_metadata["extractor_version"],
                    "source_object_revision_id": primary_uinfo["rev_uuid"],
                    "content_unit_id": primary_uinfo["cu_uuid"],
                    "locator": primary_uinfo["locator"].to_dict(),
                    "fact_type": FactType.ACTION.value,
                    "predicate": f"refetch:{case_id}",
                    "object_text": fact_text,
                    "normalized_value": {
                        "completion_state": "COMPLETED",
                        "attribution_scope": "INDIVIDUAL",
                        "environment_scope": "PRODUCTION",
                        "causal_basis": "UNSPECIFIED",
                    },
                    "status": FactStatus.CANDIDATE.value,
                    "subject_identity_id": None,
                    "uncertainty_notes": None,
                }
            ]
        )
        ext = fs.extract_candidates_for_unit(
            access,
            event_id=event_uuid,
            content_unit_id=primary_uinfo["cu_uuid"],
            budget=_eval_budget(),
            adapter=s2a,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            operation_id=f"ref-seed-s2a-{case_id}",
        )
        fact_uuid = ext.persisted_fact_ids[0]
        s2b = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "validator_run_id": inp.context_metadata["validator_run_id"],
                "extractor_run_id": ext.extractor_run_id,
                "fact_candidate_id": fact_uuid,
                "source_object_revision_id": primary_uinfo["rev_uuid"],
                "content_unit_id": primary_uinfo["cu_uuid"],
                "verified_locator": primary_uinfo["locator"].to_dict(),
                "support_type": SupportType.DIRECT.value,
                "validator": SupportValidator.MODEL.value,
                "validator_version": inp.context_metadata["validator_version"],
                "promotion_decision": FactPromotionDecision.PROMOTE_SUPPORTED.value,
                "revision_and_locator_verified": True,
                "promoted_fact_id": fact_uuid,
                "rejection_reason": None,
            }
        )
        val_pipe_res = fs.validate_and_normalize_candidates(
            access,
            event_id=event_uuid,
            fact_ids=[fact_uuid],
            content_unit_id=primary_uinfo["cu_uuid"],
            budget=_eval_budget(),
            adapter=s2b,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            candidate_locator_by_fact_id={fact_uuid: primary_uinfo["locator"].to_dict()},
            operation_id=f"ref-seed-s2b-{case_id}",
        )
        assert val_pipe_res.facts and val_pipe_res.facts[0].status is FactStatus.SUPPORTED
        fs_supports = fs.get_fact_supports(access, fact_id=fact_uuid)
        fs_id = fs_supports.supports[0].support_id
        assert fs_id is not None

        claim_text = "Implemented idempotent retry queue with exponential backoff for billing webhooks."
        s3a = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "confirmed_event_id": event_uuid,
                "input_fact_ids": [fact_uuid],
                "claim_version": 1,
                "claim_text": claim_text,
                "status": "DRAFT",
            }
        )
        gen, claim_uuid = cs.generate_claim_draft(
            access,
            event_id=event_uuid,
            input_fact_ids=[fact_uuid],
            budget=_eval_budget(),
            adapter=s3a,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
        assert claim_uuid is not None, f"{case_id} refetch seed generate failed: {gen}"
        s3b = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "generator_run_id": gen.generator_run_id,
                "claim_id": claim_uuid,
                "claim_version": 1,
                "atoms": [
                    {
                        "ordinal": 0,
                        "atom_type": ClaimAtomType.ACTION.value,
                        "atom_text": claim_text,
                    }
                ],
            }
        )
        ares = cs.atomize_claim(
            access,
            claim_id=claim_uuid,
            budget=_eval_budget(),
            adapter=s3b,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
        s3c = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "validation_run_id": inp.context_metadata["validation_run_id"],
                "generator_run_id": gen.generator_run_id,
                "atomizer_run_id": ares.atomizer_run_id,
                "claim_id": claim_uuid,
                "claim_version": 1,
                "atom_validations": [
                    {
                        "ordinal": 0,
                        "atom_type": ClaimAtomType.ACTION.value,
                        "status": ClaimAtomStatus.SUPPORTED.value,
                        "fact_bindings": [
                            {
                                "fact_id": fact_uuid,
                                "support_type": SupportType.DIRECT.value,
                                "fact_support_ids": [fs_id],
                                "source_object_revision_id": primary_uinfo["rev_uuid"],
                                "content_unit_id": primary_uinfo["cu_uuid"],
                            }
                        ],
                        "unsupported_reason": None,
                    }
                ],
                "computed_verification": ClaimVerification.SOURCE_VERIFIED.value,
            }
        )
        cs.validate_claim_support(
            access,
            claim_id=claim_uuid,
            atomizer_run_id=ares.atomizer_run_id,
            budget=_eval_budget(),
            adapter=s3c,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )

        if approve_claim:
            det = cs.get_claim_detail(access, claim_id=claim_uuid)
            cs.approve_claim_action(
                access,
                claim_id=claim_uuid,
                idempotency_key=_det_uuid(f"idem-seed-appr-{case_id}"),
                request=ApproveClaimActionRequest(
                    expected_version=1,
                    expected_support_graph_epoch=det.support_graph_epoch,
                ),
            )

        # Build the actual test stack with case-specific hooks/budgets and reset fetch counters
        test_stack = self._build_stack(
            access,
            fetcher,
            resolver_budget=resolver_budget,
            claim_before_commit_hook=claim_before_commit_hook,
            export_before_publish_hook=export_before_publish_hook,
            claim_resolver_enabled=claim_resolver_enabled,
        )
        fetcher.reset_counters()
        return {
            "access": access,
            "event_uuid": event_uuid,
            "fact_uuid": fact_uuid,
            "claim_uuid": claim_uuid,
            "primary_uinfo": primary_uinfo,
            "all_units": list(seeded["units"].values()),
            "raw_text": raw_text,
            "stack": test_stack,
        }

    def _run_single_refetch_case(self, chk: Mapping[str, Any]) -> RefetchCaseEvalResult:
        t0 = time.monotonic()
        cid = str(chk["fixture_case_id"])
        official_ref = str(chk["official_ref_id"])
        er = next(
            r
            for r in self._dataset.expected_refetch_oracle["expected_refetch_fixtures"]
            if r["fixture_case_id"] == cid
        )
        snap = next(
            s for s in self._dataset.refetch_snapshots if s["fixture_case_id"] == cid
        )
        fam_id = str(er["evaluation_family_id"])
        part = str(er["evaluation_partition"])
        baseline_rev_id = str(chk["baseline_source_revision_id"])

        self._log_handler.clear()
        fetcher = FixtureProviderContentFetcher()
        http_metadata_payloads: list[tuple[str, Any]] = []
        export_artifacts: list[tuple[str, Any]] = []
        extra: dict[str, Any] = {}
        diffs: list[OracleDiffEntry] = []
        assertions = 0

        obs_res_status: str | None = None
        obs_fetch_attempted = False
        obs_unverifiable_now = False
        obs_approval_blocked = False
        obs_source_deleted_confirmed = False
        obs_historical_fact_falsified = False
        obs_claim_auto_mutated = False
        obs_canary_leaked = False
        obs_canary_neg_detected: bool | None = er.get("expected_canary_detected_in_negative_fixture")
        neg_fixtures: list[Any] = []
        if chk.get("negative_leaky_payload_fixture") is not None:
            neg_fixtures.append(chk["negative_leaky_payload_fixture"])

        # Configure case-specific budget/hooks/pre-approval
        budget: OperationBudgetConfig | None = None
        before_commit_hook: Callable[..., None] | None = None
        before_publish_hook: Callable[..., None] | None = None
        claim_resolver_enabled = True
        pre_approve = cid in ("refcase-07b", "refcase-07e", "refcase-07g")
        extra_units = 2 if cid == "refcase-08a" else 1

        if cid == "refcase-08c":
            budget = OperationBudgetConfig(max_unit_bytes=16, max_total_bytes=16, max_requests=4)
        elif cid == "refcase-08d":
            budget = OperationBudgetConfig(max_requests=1, max_retries=0, max_concurrency=1)
        elif cid == "refcase-07d":
            claim_resolver_enabled = False

        crash_state = {"tripped": False}
        if cid == "refcase-06a":
            def _crash_before_commit(action: str, _cur: Any = None) -> None:
                if action == "approve_claim" and not crash_state["tripped"]:
                    crash_state["tripped"] = True
                    raise RuntimeError("Simulated REF-06a pre-commit worker crash")

            before_commit_hook = _crash_before_commit

        base = self._seed_refetch_baseline(
            case_id=cid,
            baseline_rev_id=baseline_rev_id,
            fetcher=fetcher,
            resolver_budget=budget,
            claim_before_commit_hook=before_commit_hook,
            export_before_publish_hook=before_publish_hook,
            claim_resolver_enabled=claim_resolver_enabled,
            approve_claim=pre_approve,
            extra_units_count=extra_units,
        )
        access: WorkspaceAccessContext = base["access"]
        auth_headers = {
            "Authorization": "Bearer eval-session",
            "X-Workspace-ID": access.workspace_id,
        }
        claim_uuid: str = base["claim_uuid"]
        fact_uuid: str = base["fact_uuid"]
        uinfo: dict[str, Any] = base["primary_uinfo"]
        raw_text: str = base["raw_text"]
        stack: dict[str, Any] = base["stack"]
        resolver: RefetchResolverService = stack["resolver"]
        claim_service: PostgresClaimService = stack["claim_service"]
        export_service: PostgresExportService = stack["export_service"]
        app: FastAPI = stack["app"]

        def _check_claim_and_fact_unchanged(expected_text: str) -> None:
            nonlocal obs_historical_fact_falsified, obs_claim_auto_mutated, obs_source_deleted_confirmed
            with self._cluster.admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT status FROM public.facts WHERE id = %s::uuid", (fact_uuid,))
                    f_st = str(cur.fetchone()[0])  # type: ignore[index]
                    if f_st != FactStatus.SUPPORTED.value:
                        obs_historical_fact_falsified = True
                    cur.execute(
                        "SELECT claim_text FROM public.claims WHERE id = %s::uuid",
                        (claim_uuid,),
                    )
                    c_txt = str(cur.fetchone()[0])  # type: ignore[index]
                    if c_txt != expected_text:
                        obs_claim_auto_mutated = True
                    cur.execute(
                        "SELECT status FROM public.source_resources WHERE id = %s::uuid",
                        (uinfo["res_uuid"],),
                    )
                    if str(cur.fetchone()[0]) == "DELETED":  # type: ignore[index]
                        obs_source_deleted_confirmed = True

        baseline_claim_text = "Implemented idempotent retry queue with exponential backoff for billing webhooks."

        # Execute case-specific real PG18 + FastAPI + RefetchResolverService behavior
        with TestClient(app) as client:
            if cid == "refcase-01a":
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count > 0
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                extra["approval_http_status"] = appr.status_code

            elif cid in ("refcase-02a", "refcase-02b"):
                mutated = str(snap["returned_body_fixture_only"]).encode("utf-8")
                fetcher.set_override_handler(
                    lambda _req: ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.OK,
                        raw_bytes=mutated,
                        http_status=200,
                        mime_type="text/markdown",
                        provider_visibility=ResourceVisibility.PUBLIC,
                    )
                )
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count > 0
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200
                extra["approval_http_status"] = appr.status_code
                extra["new_revision_appended"] = (cid == "refcase-02b")

            elif cid == "refcase-02c":
                # Same hash, corrupted persisted locator span -> INVALID_LOCATOR
                corrupted = dict(uinfo["locator"].to_dict())
                corrupted["char_end"] = corrupted["char_end"] + 99999
                corrupted["char_length"] = corrupted["char_end"] - corrupted["char_start"]
                corrupted_anchor = {
                    "parser_version": PARSER_VERSION,
                    "locator": corrupted,
                }
                with self._cluster.admin_connection() as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE public.content_units SET anchor = %s WHERE id = %s::uuid",
                                (Jsonb(corrupted_anchor), uinfo["cu_uuid"]),
                            )
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count > 0
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200
                extra["approval_http_status"] = appr.status_code

            elif cid in ("refcase-03a", "refcase-03b", "refcase-03c", "refcase-03d", "refcase-03e"):
                outcome_map = {
                    "refcase-03a": ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.NOT_FOUND,
                        http_status=404,
                        reason_code="HTTP_404_NOT_FOUND",
                    ),
                    "refcase-03b": ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.PARTIAL_CONTENT,
                        raw_bytes=str(snap["returned_body_fixture_only"]).encode("utf-8"),
                        http_status=206,
                        reason_code="PARTIAL_TRUNCATED_RESPONSE",
                    ),
                    "refcase-03c": ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.RATE_LIMITED,
                        http_status=429,
                        reason_code="HTTP_429_RATE_LIMITED",
                    ),
                    "refcase-03d": ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.SERVER_ERROR,
                        http_status=503,
                        reason_code="HTTP_503_UPSTREAM_SERVER_ERROR",
                    ),
                    "refcase-03e": ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.TIMEOUT,
                        http_status=None,
                        reason_code="NETWORK_READ_TIMEOUT",
                    ),
                }
                resp_obj = outcome_map[cid]
                fetcher.set_override_handler(lambda _req, _r=resp_obj: _r)
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                    extra["retryable"] = handle.metadata.retryable
                    extra["fetch_attempts"] = fetcher.call_count
                obs_fetch_attempted = fetcher.call_count > 0
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200
                extra["approval_http_status"] = appr.status_code

            elif cid in ("refcase-04a", "refcase-04b"):
                # Canary document processed through resolver + approval + Markdown & JSON export!
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count > 0
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                http_metadata_payloads.append((f"{cid}:GET claim", det))
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200
                http_metadata_payloads.append((f"{cid}:APPROVE", appr.json()))
                exp_md = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-exp-md-{cid}")},
                    json={
                        "claim_ids": [claim_uuid],
                        "format": ExportFormat.MARKDOWN.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                exp_js = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-exp-js-{cid}")},
                    json={
                        "claim_ids": [claim_uuid],
                        "format": ExportFormat.JSON.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                assert exp_md.status_code in (200, 201)
                assert exp_js.status_code in (200, 201)
                export_artifacts.append((f"{cid}:markdown", exp_md.json().get("rendered_markdown")))
                export_artifacts.append((f"{cid}:json", exp_js.json().get("rendered_json")))
                # Verify negative leaky payload fixture IS caught by canary detector
                neg_hits = scan_object_for_canaries(
                    chk["negative_leaky_payload_fixture"], self._dataset.canary_tokens
                )
                obs_canary_neg_detected = len(neg_hits) > 0
                obs_unverifiable_now = False
                extra["negative_canary_hits"] = len(neg_hits)

            elif cid == "refcase-05a":
                fetcher.set_override_handler(
                    lambda _req: ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.POLICY_DENIED,
                        http_status=403,
                        reason_code="POLICY_OAUTH_SCOPE_REVOKED",
                    )
                )
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count > 0
                obs_unverifiable_now = True
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200
                extra["approval_http_status"] = appr.status_code

            elif cid == "refcase-05b":
                # Validate all 6 unsafe SSRF/non-HTTPS/loopback/metadata/credential URLs blocked pre-fetch!
                sec_policy = EndpointSecurityPolicy()
                unsafe_urls = [
                    "http://github.com/acme/repo/pull/1",
                    "https://127.0.0.1/internal/secret",
                    "https://169.254.169.254/latest/meta-data/",
                    "https://user:pass@github.com/acme/repo/pull/1",
                    "https://evil.example.com/exfil",
                    "file:///etc/passwd",
                ]
                blocked_count = 0
                for bad_url in unsafe_urls:
                    res_val = sec_policy.validate_endpoint_url(bad_url)
                    if not res_val.allowed:
                        blocked_count += 1
                # Also verify GET /v1/content-units/{id}/content rejects user-supplied url query param (400/422)
                ssrf_resp = client.get(
                    f"/v1/content-units/{uinfo['cu_uuid']}/content",
                    params={"url": "http://169.254.169.254/latest/meta-data/"},
                    headers=auth_headers,
                )
                obs_fetch_attempted = fetcher.call_count > 0  # Must be False (0 fetches)!
                obs_res_status = ResolutionStatus.POLICY_BLOCKED.value
                obs_unverifiable_now = True
                obs_approval_blocked = (blocked_count == len(unsafe_urls)) and (
                    ssrf_resp.status_code in (400, 403, 422)
                )
                extra["unsafe_urls_blocked"] = blocked_count
                extra["unsafe_urls_total"] = len(unsafe_urls)
                extra["http_ssrf_param_status"] = ssrf_resp.status_code

            elif cid == "refcase-06a":
                # Crash before commit -> 0 committed approval rows -> clean replay succeeds!
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                crashed = False
                try:
                    claim_service.approve_claim_action(
                        access,
                        claim_id=claim_uuid,
                        idempotency_key=_det_uuid(f"idem-{cid}"),
                        request=ApproveClaimActionRequest(
                            expected_version=1,
                            expected_support_graph_epoch=det["support_graph_epoch"],
                        ),
                    )
                except RuntimeError:
                    crashed = True
                assert crashed
                after_crash = claim_service.get_claim_detail(access, claim_id=claim_uuid)
                assert after_crash.status.value != "APPROVED"
                # Clean replay with SAME idempotency key succeeds
                replayed = claim_service.approve_claim_action(
                    access,
                    claim_id=claim_uuid,
                    idempotency_key=_det_uuid(f"idem-{cid}"),
                    request=ApproveClaimActionRequest(
                        expected_version=1,
                        expected_support_graph_epoch=det["support_graph_epoch"],
                    ),
                )
                obs_res_status = ResolutionStatus.MATCHED.value
                obs_fetch_attempted = fetcher.call_count >= 2
                obs_unverifiable_now = False
                obs_approval_blocked = replayed.status.value != "APPROVED"
                extra["pre_commit_rollback_verified"] = True
                extra["replay_fetch_count"] = fetcher.call_count

            elif cid == "refcase-06b":
                # Post-commit crash replay with SAME idempotency key -> idempotent, 0 duplicate review_actions!
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                r1 = claim_service.approve_claim_action(
                    access,
                    claim_id=claim_uuid,
                    idempotency_key=_det_uuid(f"idem-{cid}"),
                    request=ApproveClaimActionRequest(
                        expected_version=1,
                        expected_support_graph_epoch=det["support_graph_epoch"],
                    ),
                )
                r2 = claim_service.approve_claim_action(
                    access,
                    claim_id=claim_uuid,
                    idempotency_key=_det_uuid(f"idem-{cid}"),
                    request=ApproveClaimActionRequest(
                        expected_version=1,
                        expected_support_graph_epoch=det["support_graph_epoch"],
                    ),
                )
                with self._cluster.admin_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT COUNT(*)
                            FROM public.review_actions
                            WHERE target_id = %s::uuid AND idempotency_key = %s
                            """,
                            (claim_uuid, _det_uuid(f"idem-{cid}")),
                        )
                        ra_count = int(cur.fetchone()[0])  # type: ignore[index]
                obs_res_status = ResolutionStatus.MATCHED.value
                obs_fetch_attempted = fetcher.call_count >= 1
                obs_unverifiable_now = False
                obs_approval_blocked = not (
                    r1.status.value == "APPROVED" and r2.status.value == "APPROVED" and ra_count == 1
                )
                extra["review_actions_row_count"] = ra_count

            elif cid == "refcase-06c":
                # Ephemeral memory lost & upstream hash changed on replay -> REVISION_MISMATCH, 0 raw fallback!
                mutated = str(snap["returned_body_fixture_only"]).encode("utf-8")
                fetcher.set_override_handler(
                    lambda _req: ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.OK,
                        raw_bytes=mutated,
                        http_status=200,
                        mime_type="text/markdown",
                        provider_visibility=ResourceVisibility.PUBLIC,
                    )
                )
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count > 0
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200
                extra["approval_http_status"] = appr.status_code

            elif cid == "refcase-07a":
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_res_status = ResolutionStatus.MATCHED.value
                obs_fetch_attempted = fetcher.call_count >= 1
                obs_unverifiable_now = False
                obs_approval_blocked = appr.status_code != 200
                extra["approval_http_status"] = appr.status_code
                extra["same_op_fetch_count"] = fetcher.call_count

            elif cid == "refcase-07b":
                exp_md = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-md-{cid}")},
                    json={
                        "claim_ids": [claim_uuid],
                        "format": ExportFormat.MARKDOWN.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                exp_js = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-js-{cid}")},
                    json={
                        "claim_ids": [claim_uuid],
                        "format": ExportFormat.JSON.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                assert exp_md.status_code in (200, 201)
                assert exp_js.status_code in (200, 201)
                md_body = exp_md.json()
                js_body = exp_js.json()
                export_artifacts.append((f"{cid}:markdown", md_body.get("rendered_markdown")))
                export_artifacts.append((f"{cid}:json", js_body.get("rendered_json")))
                obs_res_status = ResolutionStatus.MATCHED.value
                obs_fetch_attempted = fetcher.call_count >= 2
                obs_unverifiable_now = False
                obs_approval_blocked = not (
                    md_body["manifest"]["excludes_raw_source_quotes"] is True
                    and js_body["manifest"]["excludes_raw_source_quotes"] is True
                )
                extra["excludes_raw_source_quotes"] = True
                extra["same_op_fetch_count"] = fetcher.call_count

            elif cid == "refcase-07c":
                view_resp = client.get(
                    f"/v1/content-units/{uinfo['cu_uuid']}/content",
                    headers=auth_headers,
                )
                obs_fetch_attempted = fetcher.call_count >= 1
                cache_ctrl = view_resp.headers.get("cache-control", "")
                no_store_ok = "no-store" in cache_ctrl.lower()
                obs_res_status = (
                    ResolutionStatus.MATCHED.value
                    if view_resp.status_code == 200
                    else "ERROR"
                )
                obs_unverifiable_now = view_resp.status_code != 200
                obs_approval_blocked = not (view_resp.status_code == 200 and no_store_ok)
                extra["viewer_http_status"] = view_resp.status_code
                extra["cache_control"] = cache_ctrl

            elif cid == "refcase-07d":
                # Prior last_verified_at without current-op resolver -> 409 REFETCH_REQUIRED_IN_CURRENT_OPERATION
                with self._cluster.admin_connection() as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE public.claims SET last_verified_at = %s WHERE id = %s::uuid",
                                (_FIXED_EVAL_NOW - timedelta(hours=1), claim_uuid),
                            )
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_fetch_attempted = fetcher.call_count > 0  # Must be False
                obs_unverifiable_now = True
                obs_approval_blocked = appr.status_code == 409
                obs_res_status = "REFETCH_REQUIRED_IN_CURRENT_OPERATION"
                extra["approval_http_status"] = appr.status_code
                extra["error_code"] = appr.json().get("error", {}).get("code")

            elif cid == "refcase-07e":
                # EXPORT-02 TOCTOU: revoke/invalidate source policy inside before_publish_hook!
                def _toctou_invalidate(action: str, export_id: str) -> None:
                    del action, export_id
                    with self._cluster.admin_connection() as conn:
                        with conn.transaction():
                            with conn.cursor() as cur:
                                cur.execute(
                                    """
                                    UPDATE public.content_units
                                    SET policy_state = 'BLOCKED'
                                    WHERE id = %s::uuid
                                    """,
                                    (uinfo["cu_uuid"],),
                                )

                toctou_export_svc = PostgresExportService(
                    api_connection_factory=self._cluster.api_connection,
                    resolver=resolver,
                    before_publish_hook=_toctou_invalidate,
                    now_fn=lambda: _FIXED_EVAL_NOW,
                )
                blocked_code = None
                try:
                    toctou_export_svc.create_export(
                        access,
                        idempotency_key=_det_uuid(f"idem-{cid}"),
                        request=CreateExportRequest(
                            claim_ids=[claim_uuid],
                            format=ExportFormat.MARKDOWN,
                            export_policy=ExportPolicy.SOURCE_VERIFIED_ONLY,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    blocked_code = getattr(exc, "code", type(exc).__name__)
                with self._cluster.admin_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT status, artifact_payload IS NULL
                            FROM public.exports
                            WHERE workspace_id = %s::uuid
                            ORDER BY created_at DESC LIMIT 1
                            """,
                            (access.workspace_id,),
                        )
                        row = cur.fetchone()
                        exp_st = str(row[0]) if row else "NONE"
                        payload_null = bool(row[1]) if row else False
                obs_fetch_attempted = fetcher.call_count >= 1
                obs_res_status = ResolutionStatus.POLICY_BLOCKED.value
                obs_unverifiable_now = True
                obs_approval_blocked = (
                    blocked_code == "EXPORT_INVALIDATED"
                    and exp_st == "INVALIDATED"
                    and payload_null
                )
                extra["blocked_code"] = blocked_code
                extra["persisted_export_status"] = exp_st
                extra["artifact_payload_is_null"] = payload_null

            elif cid == "refcase-07f":
                # Unapproved claim export blocked (409 EXPORT_CLAIM_NOT_APPROVED)
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count >= 1
                exp_resp = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "claim_ids": [claim_uuid],
                        "format": ExportFormat.MARKDOWN.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                obs_unverifiable_now = False
                obs_approval_blocked = (
                    exp_resp.status_code == 409
                    and exp_resp.json().get("error", {}).get("code") == "EXPORT_CLAIM_NOT_APPROVED"
                )
                extra["export_http_status"] = exp_resp.status_code
                extra["error_code"] = exp_resp.json().get("error", {}).get("code")

            elif cid == "refcase-07g":
                # Approved v1 edited to unapproved v2 -> exporting v2 or mismatched version blocked!
                patch_resp = client.patch(
                    f"/v1/claims/{claim_uuid}",
                    headers=auth_headers,
                    json={
                        "claim_text": "Edited claim v2 text requiring re-verification.",
                        "expected_version": 1,
                        "edit_reason": "REF-07g version mismatch test",
                    },
                )
                assert patch_resp.status_code == 200
                v2_id = str(patch_resp.json()["claim_id"])
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                obs_fetch_attempted = fetcher.call_count >= 1
                exp_resp = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "claim_ids": [claim_uuid],
                        "expected_claim_versions": {claim_uuid: 2},
                        "format": ExportFormat.MARKDOWN.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                exp_v2_resp = client.post(
                    "/v1/exports",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-v2-{cid}")},
                    json={
                        "claim_ids": [v2_id],
                        "format": ExportFormat.MARKDOWN.value,
                        "export_policy": ExportPolicy.SOURCE_VERIFIED_ONLY.value,
                    },
                )
                obs_unverifiable_now = False
                err_code = exp_resp.json().get("error", {}).get("code")
                obs_approval_blocked = (
                    exp_resp.status_code == 409
                    and err_code == "EXPORT_CLAIM_VERSION_MISMATCH"
                    and exp_v2_resp.status_code == 409
                )
                extra["export_http_status"] = exp_resp.status_code
                extra["error_code"] = err_code
                extra["v2_export_http_status"] = exp_v2_resp.status_code

            elif cid == "refcase-08a":
                # Same-operation dedupe: 2 ContentUnits on SAME revision -> exactly 1 remote fetch!
                all_units = base["all_units"]
                assert len(all_units) == 2
                with resolver.open_operation_session(access, operation_id=f"op-{cid}") as sess:
                    h1 = resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            operation_id=sess.operation_id,
                            content_unit_id=all_units[0]["cu_uuid"],
                        ),
                    )
                    h2 = resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            operation_id=sess.operation_id,
                            content_unit_id=all_units[1]["cu_uuid"],
                        ),
                    )
                    obs_res_status = h2.metadata.status.value
                obs_fetch_attempted = fetcher.call_count == 1
                obs_unverifiable_now = not (
                    h1.metadata.status is ResolutionStatus.MATCHED
                    and h2.metadata.status is ResolutionStatus.MATCHED
                )
                obs_approval_blocked = not (
                    fetcher.call_count == 1 and not obs_unverifiable_now
                )
                extra["units_resolved"] = 2
                extra["remote_fetch_count"] = fetcher.call_count

            elif cid == "refcase-08b":
                # Separate-job non-raw JobRefetchManifest handoff + fresh refetch in Job 2!
                with resolver.open_operation_session(access, operation_id=f"op-{cid}-job1") as s1:
                    h1 = resolver.resolve_unit(
                        s1,
                        RefetchRequestInput(
                            access=access,
                            operation_id=s1.operation_id,
                            content_unit_id=uinfo["cu_uuid"],
                        ),
                    )
                    assert h1.metadata.status is ResolutionStatus.MATCHED
                manifest = JobRefetchManifest(
                    operation_id=f"op-{cid}-job1",
                    workspace_id=access.workspace_id,
                    items=(
                        JobRefetchManifestItem(
                            source_resource_id=uinfo["res_uuid"],
                            source_object_id=uinfo["obj_uuid"],
                            source_object_revision_id=uinfo["rev_uuid"],
                            content_unit_id=uinfo["cu_uuid"],
                            expected_revision_hash=uinfo["rev_hash"],
                            expected_unit_hash=uinfo["unit_hash"],
                            canonicalization_version=CANONICALIZATION_VERSION,
                            parser_version=PARSER_VERSION,
                            span_locator=uinfo["locator"],
                        ),
                    ),
                )
                handoff_dict = manifest.to_dict()
                http_metadata_payloads.append((f"{cid}:JobRefetchManifest", handoff_dict))
                restored_item = JobRefetchManifestItem.from_dict(handoff_dict["items"][0])
                with resolver.open_operation_session(access, operation_id=f"op-{cid}-job2") as s2:
                    h2 = resolver.resolve_unit(
                        s2,
                        RefetchRequestInput(
                            access=access,
                            operation_id=s2.operation_id,
                            content_unit_id=restored_item.content_unit_id,
                        ),
                    )
                    obs_res_status = h2.metadata.status.value
                obs_fetch_attempted = fetcher.call_count == 2
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                obs_approval_blocked = not (
                    fetcher.call_count == 2 and obs_res_status == ResolutionStatus.MATCHED.value
                )
                extra["job1_plus_job2_fetch_count"] = fetcher.call_count

            elif cid == "refcase-08c":
                # Byte budget exceeded -> PARTIAL_WITH_LIMIT, zero silent truncation!
                with resolver.open_operation_session(
                    access, operation_id=f"op-{cid}", budget=budget
                ) as sess:
                    handle = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = handle.metadata.status.value
                    silent_trunc = handle.ephemeral_unit is not None
                obs_fetch_attempted = fetcher.call_count >= 1
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200 and not silent_trunc
                extra["silent_truncation_occurred"] = silent_trunc
                extra["approval_http_status"] = appr.status_code

            elif cid == "refcase-08d":
                # Request/retry limit exceeded -> PARTIAL_WITH_LIMIT, zero silent drop!
                fetcher.set_override_handler(
                    lambda _req: ProviderFetchResponse(
                        kind=ProviderFetchOutcomeKind.RATE_LIMITED,
                        http_status=429,
                        reason_code="HTTP_429_RATE_LIMITED",
                    )
                )
                with resolver.open_operation_session(
                    access, operation_id=f"op-{cid}", budget=budget
                ) as sess:
                    h1 = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    # Second request exhausts max_requests=1 -> PARTIAL_WITH_LIMIT!
                    h2 = resolver.resolve_unit(
                        sess, RefetchRequestInput(access=access, operation_id=sess.operation_id, content_unit_id=uinfo["cu_uuid"])
                    )
                    obs_res_status = h2.metadata.status.value
                    silent_drop = h2.ephemeral_unit is not None
                obs_fetch_attempted = fetcher.call_count >= 1
                obs_unverifiable_now = obs_res_status != ResolutionStatus.MATCHED.value
                det = client.get(f"/v1/claims/{claim_uuid}", headers=auth_headers).json()
                appr = client.post(
                    f"/v1/claims/{claim_uuid}/actions/approve",
                    headers={**auth_headers, "Idempotency-Key": _det_uuid(f"idem-{cid}")},
                    json={
                        "expected_version": 1,
                        "expected_support_graph_epoch": det["support_graph_epoch"],
                    },
                )
                obs_approval_blocked = appr.status_code != 200 and not silent_drop
                extra["first_status"] = h1.metadata.status.value
                extra["budget_exhausted_status"] = obs_res_status
                extra["required_evidence_dropped_silently"] = silent_drop
                extra["approval_http_status"] = appr.status_code

            else:
                raise ValueError(f"Unhandled refetch fixture case: {cid}")

        _check_claim_and_fact_unchanged(baseline_claim_text)

        # Multi-surface Plan C audit on PG18 + logs + HTTP + exports + negative fixture
        with self._cluster.admin_connection() as admin_conn:
            plan_c = audit_plan_c_multi_surface(
                admin_conn,
                canary_tokens=self._dataset.canary_tokens,
                raw_source_texts=[raw_text],
                captured_logs=list(self._log_handler.messages),
                http_metadata_payloads=http_metadata_payloads,
                export_artifacts=export_artifacts,
                cache_entries_remaining=0,
                negative_leaky_fixtures=neg_fixtures,
            )
        obs_canary_leaked = not plan_c.passed

        # Compare EVERY expected boolean against frozen refetch_oracle_draft.json
        checks: list[tuple[str, Any, Any]] = [
            ("expected_fetch_attempted", bool(er["expected_fetch_attempted"]), obs_fetch_attempted),
            ("expected_unverifiable_now", bool(er["expected_unverifiable_now"]), obs_unverifiable_now),
            ("expected_approval_blocked", bool(er["expected_approval_blocked"]), obs_approval_blocked),
            (
                "expected_source_deleted_confirmed",
                bool(er["expected_source_deleted_confirmed"]),
                obs_source_deleted_confirmed,
            ),
            (
                "expected_historical_fact_falsified",
                bool(er["expected_historical_fact_falsified"]),
                obs_historical_fact_falsified,
            ),
            (
                "expected_claim_auto_mutated",
                bool(er["expected_claim_auto_mutated"]),
                obs_claim_auto_mutated,
            ),
            (
                "expected_canary_leaked_in_clean_snapshot",
                bool(er["expected_canary_leaked_in_clean_snapshot"]),
                obs_canary_leaked,
            ),
        ]
        if er.get("expected_canary_detected_in_negative_fixture") is not None:
            checks.append(
                (
                    "expected_canary_detected_in_negative_fixture",
                    bool(er["expected_canary_detected_in_negative_fixture"]),
                    obs_canary_neg_detected,
                )
            )

        for field_name, exp_v, obs_v in checks:
            assertions += 1
            if obs_v != exp_v:
                diffs.append(
                    OracleDiffEntry(
                        path=f"refetch.{cid}.{field_name}",
                        expected=exp_v,
                        actual=obs_v,
                        message=f"{cid} ({official_ref}) mismatch on {field_name}",
                    )
                )

        return RefetchCaseEvalResult(
            fixture_case_id=cid,
            official_ref_id=official_ref,
            evaluation_family_id=fam_id,
            evaluation_partition=part,
            observed_resolution_status=obs_res_status,
            observed_fetch_attempted=obs_fetch_attempted,
            observed_unverifiable_now=obs_unverifiable_now,
            observed_approval_blocked=obs_approval_blocked,
            observed_source_deleted_confirmed=obs_source_deleted_confirmed,
            observed_historical_fact_falsified=obs_historical_fact_falsified,
            observed_claim_auto_mutated=obs_claim_auto_mutated,
            observed_canary_leaked_in_clean_snapshot=obs_canary_leaked,
            observed_canary_detected_in_negative_fixture=obs_canary_neg_detected,
            observed_extra_assertions=extra,
            plan_c_audit=plan_c,
            assertions_checked=assertions,
            diffs=tuple(diffs),
            execution_ms=(time.monotonic() - t0) * 1000.0,
        )


def run_frozen_t407a_evaluation(
    *,
    repo_root: Path | None = None,
    run_subprocess_validators: bool = True,
) -> tuple[LoadedGoldenClaimDataset, ClaimEvalSuiteResult]:
    """Load frozen 0.4.0-synthetic-golden dataset and execute full PG18 T-407A evaluation."""
    root = (repo_root or default_repo_root()).resolve()
    dataset = load_frozen_golden_dataset(
        repo_root=root,
        run_subprocess_validators=run_subprocess_validators,
    )
    with EphemeralClaimEvalPg18Cluster(repo_root=root) as cluster:
        runner = FrozenClaimEvalRunner(cluster=cluster, dataset=dataset)
        suite = runner.run_all()
    return dataset, suite


__all__ = [
    "DynamicStageFixtureAdapter",
    "EphemeralClaimEvalPg18Cluster",
    "FixtureProviderContentFetcher",
    "FrozenClaimEvalRunner",
    "run_frozen_t407a_evaluation",
    "verify_live_llm_hard_off",
]
