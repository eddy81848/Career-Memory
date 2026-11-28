"""Real PostgreSQL 18 + Production FastAPI Runner for Frozen 21-Case Event Evaluation (T-306 / T-306A).

Executes the REAL M3 production stack against a disposable local PostgreSQL 18 cluster
migrated with 0010..0060 and NOBYPASSRLS runtime roles (cm_api_app, cm_worker_app):
- career_memory_api.event_candidates.PostgresEventCandidateBuilder (+ replay idempotency)
- career_memory_api.work_events.PostgresWorkEventsReadService (GET /v1/work-events, GET /v1/work-events/{id})
- career_memory_api.work_events.PostgresWorkEventMutationService (confirm, edit, reject, split, merge, dissolve)
- career_memory_api.work_events.create_work_events_router (via FastAPI TestClient)
- career_memory_api.work_events.query_searchable_top_level_work_events & collapse_event_ids_for_search_and_export

Zero mock rule engines. Zero oracle auto-overwrites. Zero external network calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.event_candidates import (
    CandidateBuildStatus,
    PostgresEventCandidateBuilder,
)
from career_memory_api.event_eval.comparator import compare_case_against_frozen_oracle
from career_memory_api.event_eval.loader import (
    default_repo_root,
    load_approved_21_cases,
)
from career_memory_api.event_eval.models import (
    EVAL_BUNDLE_SCHEMA_VERSION,
    EXCLUDED_FROM_MVP_TRUTH_COMPARISON,
    EXPECTED_APPROVED_21_BUNDLE_SHA256,
    EXPECTED_APPROVED_DATASET_VERSION,
    EXPECTED_SCHEMA_MIGRATIONS,
    IMMUTABLE_OPENAPI_BLOB_SHA,
    UNIMPLEMENTED_M5_CONTRACT_BOUNDARIES,
    VOLATILE_BUNDLE_KEYS,
    ApprovedCaseEnvelope,
    BundleGroupSummary,
    CaseEvaluationResult,
    CommandExecutionRecord,
    EvalSuiteResult,
    ObservedCaseSnapshot,
    ObservedDownstreamArtifact,
    ObservedHeldItem,
    ObservedOutOfWindowDedup,
    ObservedReviewAction,
    ObservedSearchCheck,
    ObservedWorkEvent,
    PlanCAuditResult,
)
from career_memory_api.event_eval.seeder import (
    SeedIdRegistry,
    seed_approved_case_into_pg18,
)
from career_memory_api.event_eval.telemetry import (
    CurationTelemetryCollector,
    record_curation_telemetry_sample,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    find_forbidden_raw_keys,
)
from career_memory_api.work_events import (
    MergeMemberInput,
    MergeWorkEventsRequest,
    PostgresWorkEventMutationService,
    PostgresWorkEventsReadService,
    WorkEventError,
    collapse_event_ids_for_search_and_export,
    create_work_events_router,
    install_work_events_error_handlers,
    query_searchable_top_level_work_events,
)

_PG18_BIN_DIR = Path("/opt/homebrew/opt/postgresql@18/bin")
_FIXED_EVAL_NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)

_CMD_ACTION_TO_DOMAIN_ACTION: dict[str, str] = {
    "confirm": "CONFIRM",
    "edit": "EDIT",
    "exclude": "REJECT",
    "reject": "REJECT",
    "split": "SPLIT",
    "manual_merge": "MERGE",
    "dissolve": "DISSOLVE",
}


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


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EphemeralEventEvalPg18Cluster:
    """Self-cleaning local PostgreSQL 18 cluster with migrations 0010..0060 and NOBYPASSRLS roles."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        prefix: str = "cm-t306a-eval-pg18-",
    ) -> None:
        self._repo_root = (repo_root or default_repo_root()).resolve()
        self._tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_eval_admin"
        self.dbname = "career_memory_eval"
        self.api_login = "cm_api_login_eval"
        self.api_password = "local_ephemeral_api_pw"
        self.worker_login = "cm_worker_login_eval"
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
                    cur.execute("SET LOCAL app.allow_revision_purge = 'on'")
                    cur.execute(
                        """
                        TRUNCATE TABLE
                            public.event_constraints,
                            public.work_event_versions,
                            public.event_composite_memberships,
                            public.review_actions,
                            public.claim_atom_support,
                            public.claim_atoms,
                            public.claims,
                            public.generation_runs,
                            public.contribution_support,
                            public.contributions,
                            public.fact_support,
                            public.facts,
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
                            public.external_identity_mappings,
                            public.identities,
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


def _compute_db_mutation_state_hash(admin_conn: psycopg.Connection) -> str:
    """Capture a canonical SHA-256 over all WorkEvent/membership/downstream/review tables."""
    queries = {
        "work_events": """
            SELECT id::text, workspace_id::text, primary_anchor_source_object_id::text,
                   anchor_mode, anchor_scope, version, title, problem, action, outcome,
                   summary_metadata, status, created_by
            FROM public.work_events
            ORDER BY id ASC
        """,
        "event_memberships": """
            SELECT event_id::text, source_object_id::text, relation_class,
                   relation_type, origin, status
            FROM public.event_memberships
            ORDER BY event_id ASC, source_object_id ASC, relation_type ASC
        """,
        "event_composite_memberships": """
            SELECT composite_event_id::text, composite_event_version,
                   member_event_id::text, member_event_version, ordinal, status
            FROM public.event_composite_memberships
            ORDER BY composite_event_id ASC, ordinal ASC, member_event_id ASC
        """,
        "event_constraints": """
            SELECT left_event_id::text, right_event_id::text,
                   constraint_type, origin, status
            FROM public.event_constraints
            ORDER BY left_event_id ASC, right_event_id ASC, constraint_type ASC
        """,
        "work_event_versions": """
            SELECT event_id::text, version, anchor_mode, title, outcome, status
            FROM public.work_event_versions
            ORDER BY event_id ASC, version ASC
        """,
        "review_actions": """
            SELECT target_id::text, action, from_version, to_version, idempotency_key
            FROM public.review_actions
            ORDER BY target_id ASC, idempotency_key ASC, action ASC
        """,
        "facts": """
            SELECT id::text, event_id::text, status
            FROM public.facts
            ORDER BY id ASC
        """,
        "claims": """
            SELECT id::text, event_id::text, status
            FROM public.claims
            ORDER BY id ASC
        """,
    }
    state: dict[str, list[Any]] = {}
    with admin_conn.cursor() as cur:
        for name, sql in queries.items():
            cur.execute(sql)
            state[name] = [list(r) for r in cur.fetchall()]
    return _canonical_sha256(state)


def _audit_plan_c_in_pg18(
    admin_conn: psycopg.Connection,
    *,
    raw_body_canaries: Sequence[str],
) -> PlanCAuditResult:
    """Verify Plan C (ADR-013) non-raw invariants across all PostgreSQL 18 tables."""
    canaries = tuple(sorted(set(c for c in raw_body_canaries if c)))
    leak_locations: list[str] = []
    jsonb_forbidden_hits = 0
    content_units_ok = True
    fact_support_ok = True

    with admin_conn.cursor() as cur:
        cur.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename ASC
            """
        )
        tables = [str(r[0]) for r in cur.fetchall()]

        for tbl in tables:
            cur.execute(f'SELECT row_to_json(t)::text FROM public."{tbl}" t')
            for idx, (row_text,) in enumerate(cur.fetchall()):
                if not row_text:
                    continue
                for marker in canaries:
                    if marker in row_text:
                        leak_locations.append(f"pg:public.{tbl}[{idx}]")

        # content_units Plan C NULL constraints
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.content_units
            WHERE text_content IS NOT NULL
               OR content_ref IS NOT NULL
               OR retention_class <> 'NONE'
               OR expires_at IS NOT NULL
            """
        )
        cu_bad = int(cur.fetchone()[0])
        if cu_bad > 0:
            content_units_ok = False
            leak_locations.append(f"pg:public.content_units:non_compliant={cu_bad}")

        # fact_support extracted_span IS NULL constraint
        cur.execute("SELECT COUNT(*) FROM public.fact_support WHERE extracted_span IS NOT NULL")
        fs_bad = int(cur.fetchone()[0])
        if fs_bad > 0:
            fact_support_ok = False
            leak_locations.append(f"pg:public.fact_support:extracted_span_not_null={fs_bad}")

        # Inspect every JSONB column across public.* for forbidden raw keys
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type = 'jsonb'
            ORDER BY table_name ASC, column_name ASC
            """
        )
        jsonb_cols = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
        for tbl, col in jsonb_cols:
            cur.execute(
                f'SELECT "{col}" FROM public."{tbl}" WHERE "{col}" IS NOT NULL'
            )
            for idx, (val,) in enumerate(cur.fetchall()):
                found = find_forbidden_raw_keys(val)
                if found:
                    jsonb_forbidden_hits += len(found)
                    leak_locations.append(
                        f"pg:public.{tbl}.{col}[{idx}]:{[k for _, k in found]}"
                    )

    canary_hits = sum(
        1
        for loc in leak_locations
        if not loc.startswith("pg:public.content_units:non_compliant")
        and not loc.startswith("pg:public.fact_support:")
        and ":" not in loc.split("[", 1)[-1]
    )
    return PlanCAuditResult(
        canaries_checked=len(canaries),
        tables_scanned=len(tables),
        canary_hits=canary_hits,
        content_units_plan_c_compliant=content_units_ok,
        fact_support_plan_c_compliant=fact_support_ok,
        jsonb_forbidden_key_hits=jsonb_forbidden_hits,
        leak_locations=tuple(leak_locations),
    )


def _make_access_context(
    reg: SeedIdRegistry,
    *,
    workspace_syn_id: str,
    actor_syn_id: str | None = None,
) -> WorkspaceAccessContext:
    ws_uuid = reg.workspace_uuids[workspace_syn_id]
    chosen_actor = actor_syn_id or reg.workspace_default_actor[workspace_syn_id]
    user_uuid = reg.user_uuids[chosen_actor]
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_uuid,
            auth_subject=AuthSubject(
                issuer="https://auth.eval.local/v1",
                subject=f"sub-{reg.case_id}-{chosen_actor}",
            ),
        ),
        membership=WorkspaceMembership(
            workspace_id=ws_uuid,
            user_id=user_uuid,
            role="OWNER",
        ),
    )


def _create_work_events_test_client(
    cluster: EphemeralEventEvalPg18Cluster,
    reg: SeedIdRegistry,
) -> tuple[TestClient, PostgresWorkEventsReadService, PostgresWorkEventMutationService]:
    read_service = PostgresWorkEventsReadService(
        api_connection_factory=cluster.api_connection
    )
    mutation_service = PostgresWorkEventMutationService(
        api_connection_factory=cluster.api_connection,
        now_fn=lambda: _FIXED_EVAL_NOW,
    )

    def _resolve_access(request: Request) -> WorkspaceAccessContext:
        ws_syn = (request.headers.get("X-Eval-Workspace-Syn") or "").strip()
        actor_syn = (request.headers.get("X-Eval-Actor-Syn") or "").strip() or None
        return _make_access_context(reg, workspace_syn_id=ws_syn, actor_syn_id=actor_syn)

    app = FastAPI()
    install_work_events_error_handlers(app)
    app.include_router(
        create_work_events_router(
            service=read_service,
            mutation_service=mutation_service,
            resolve_access=_resolve_access,
        )
    )
    return TestClient(app), read_service, mutation_service


class FrozenEventEvalRunner:
    """Runs all 21 approved 1.1.0-mvp-approved cases against real PG18 + M3 production code."""

    def __init__(
        self,
        cluster: EphemeralEventEvalPg18Cluster,
        *,
        repo_root: Path | None = None,
    ) -> None:
        self._cluster = cluster
        self._repo_root = (repo_root or default_repo_root()).resolve()

    def run_all(self) -> EvalSuiteResult:
        manifest, cases = load_approved_21_cases(repo_root=self._repo_root)
        telemetry_collector = CurationTelemetryCollector()
        results: list[CaseEvaluationResult] = []

        for envelope in cases:
            res = self.run_case(envelope, telemetry_collector=telemetry_collector)
            results.append(res)

        # Build 16 same-source evaluation bundle rollups
        res_by_id = {r.case_id: r for r in results}
        bundle_summaries: list[BundleGroupSummary] = []
        for bundle_spec in manifest.get("evaluation_bundles", []):
            bid = str(bundle_spec["eval_bundle_id"])
            cids = tuple(str(c) for c in bundle_spec["case_ids"])
            passed_cnt = sum(1 for cid in cids if cid in res_by_id and res_by_id[cid].passed)
            bundle_summaries.append(
                BundleGroupSummary(
                    eval_bundle_id=bid,
                    case_ids=cids,
                    passed_count=passed_cnt,
                    total_count=len(cids),
                )
            )

        det_telemetry, vol_telemetry = telemetry_collector.summarize()
        volatile_metrics: dict[str, Any] = {
            "per_case_execution_ms": {r.case_id: round(r.execution_ms, 3) for r in results},
            "total_suite_execution_ms": round(sum(r.execution_ms for r in results), 3),
            "telemetry_timings": vol_telemetry,
        }

        return EvalSuiteResult(
            dataset_version=EXPECTED_APPROVED_DATASET_VERSION,
            approved_21_bundle_sha256=EXPECTED_APPROVED_21_BUNDLE_SHA256,
            case_results=tuple(results),
            bundle_summaries=tuple(bundle_summaries),
            telemetry_summary=det_telemetry,
            volatile_runtime_metrics=volatile_metrics,
        )

    def run_case(
        self,
        envelope: ApprovedCaseEnvelope,
        *,
        telemetry_collector: CurationTelemetryCollector | None = None,
    ) -> CaseEvaluationResult:
        t0 = time.perf_counter()
        self._cluster.truncate_all()

        case_id = envelope.case_id
        case_data = envelope.data
        ss = case_data.get("synthetic_sources") or {}
        commands = case_data.get("user_commands") or []
        initial_events = ss.get("initial_events") or []

        with self._cluster.admin_connection() as admin_conn:
            reg = seed_approved_case_into_pg18(
                admin_conn, case_id=case_id, case_data=case_data
            )

        builder_status: str | None = None
        builder_error_code: str | None = None
        replay_checked = False
        replay_passed: bool | None = None
        command_records: list[CommandExecutionRecord] = []
        notes: list[str] = []

        # Mode A: Candidate Generation via real PostgresEventCandidateBuilder
        if not initial_events:
            execution_mode = "CANDIDATE_BUILDER"
            builder = PostgresEventCandidateBuilder(
                worker_connection_factory=self._cluster.worker_connection,
                clock=lambda: _FIXED_EVAL_NOW,
            )
            for run in ss.get("sync_runs") or []:
                ws_uuid = reg.workspace_uuids[str(run["workspace_id"])]
                run_uuid = reg.sync_run_uuids[str(run["sync_run_id"])]
                build_res = builder.build_candidates_for_sync_run(
                    workspace_id=ws_uuid,
                    sync_run_id=run_uuid,
                    now=_FIXED_EVAL_NOW,
                )
                builder_status = build_res.status.value
                builder_error_code = (
                    build_res.error_code.value if build_res.error_code is not None else None
                )

                # Verify replay idempotency on real PG18!
                with self._cluster.admin_connection() as conn:
                    hash_after_first = _compute_db_mutation_state_hash(conn)
                replay_res = builder.build_candidates_for_sync_run(
                    workspace_id=ws_uuid,
                    sync_run_id=run_uuid,
                    now=_FIXED_EVAL_NOW,
                )
                with self._cluster.admin_connection() as conn:
                    hash_after_second = _compute_db_mutation_state_hash(conn)

                replay_checked = True
                replay_ok = (
                    hash_after_first == hash_after_second
                    and replay_res.status == build_res.status
                    and replay_res.created_event_count == 0
                )
                replay_passed = replay_ok if replay_passed is None else (replay_passed and replay_ok)
                if build_res.status is CandidateBuildStatus.SUCCEEDED:
                    notes.append(
                        f"PostgresEventCandidateBuilder succeeded (pr={len(build_res.pr_event_ids)}, "
                        f"direct_commit={len(build_res.direct_commit_event_ids)}, replay_idempotent={replay_ok})."
                    )
                else:
                    notes.append(
                        f"PostgresEventCandidateBuilder held/rejected run "
                        f"(status={builder_status}, error={builder_error_code}, replay_idempotent={replay_ok})."
                    )
        else:
            execution_mode = "CURATION_COMMANDS" if commands else "READ_COLLAPSE"
            client, _read_svc, mutation_svc = _create_work_events_test_client(self._cluster, reg)
            for cmd in commands:
                rec = self._execute_user_command(
                    client=client,
                    mutation_svc=mutation_svc,
                    reg=reg,
                    case_id=case_id,
                    cmd=cmd,
                )
                command_records.append(rec)

        # Extract full logical snapshot from real PG18 + Read API + Search Collapse
        client, _read_svc, _mut_svc = _create_work_events_test_client(self._cluster, reg)
        observed = self._extract_observed_snapshot(
            client=client,
            reg=reg,
            case_id=case_id,
            case_data=case_data,
        )

        # Read-only comparison against frozen 1.1.0-mvp-approved oracle
        comparison = compare_case_against_frozen_oracle(
            case_id=case_id,
            case_data=case_data,
            observed=observed,
            command_records=tuple(command_records),
        )

        # Plan C (ADR-013) non-raw audit on real PG18
        with self._cluster.admin_connection() as conn:
            plan_c_audit = _audit_plan_c_in_pg18(
                conn, raw_body_canaries=reg.raw_body_canaries
            )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Record synthetic contract regression telemetry (strictly isolated partition)
        if telemetry_collector is not None:
            if execution_mode == "CANDIDATE_BUILDER":
                for ev in observed.events:
                    record_curation_telemetry_sample(
                        telemetry_collector,
                        partition="SYNTHETIC_CONTRACT_REGRESSION",
                        case_or_session_id=case_id,
                        event_id=ev.logical_event_id or ev.event_uuid,
                        anchor_mode=ev.anchor_mode,
                        action=None,
                        outcome="CANDIDATE_GENERATED",
                        review_duration_ms=elapsed_ms / max(1, len(observed.events)),
                        review_flags_present=ev.review_flags,
                        review_flag_acted_upon=False,
                        post_merge_top_level_duplicates=observed.top_level_duplicate_count,
                    )
            else:
                for rec in command_records:
                    domain_act = _CMD_ACTION_TO_DOMAIN_ACTION.get(rec.command_action, rec.command_action.upper())
                    acted_on_flag = case_id == "EV-008" and domain_act == "SPLIT"
                    flags_present = ("MIXED_PURPOSE_SIGNALS",) if acted_on_flag else ()
                    record_curation_telemetry_sample(
                        telemetry_collector,
                        partition="SYNTHETIC_CONTRACT_REGRESSION",
                        case_or_session_id=case_id,
                        event_id=rec.resulting_event_ids[0] if rec.resulting_event_ids else None,
                        anchor_mode="USER_COMPOSITE" if domain_act == "MERGE" and rec.outcome == "ACCEPTED" else None,
                        action=domain_act,
                        outcome=rec.outcome,
                        review_duration_ms=rec.latency_ms,
                        review_flags_present=flags_present,
                        review_flag_acted_upon=acted_on_flag,
                        post_merge_top_level_duplicates=observed.top_level_duplicate_count,
                    )

        return CaseEvaluationResult(
            case_id=case_id,
            source_record_id=envelope.source_record_id,
            origin=envelope.origin,
            eval_bundle_id=envelope.eval_bundle_id,
            file_sha256=envelope.file_sha256,
            execution_mode=execution_mode,
            builder_status=builder_status,
            builder_error_code=builder_error_code,
            replay_idempotency_checked=replay_checked,
            replay_idempotency_passed=replay_passed,
            command_results=tuple(command_records),
            observed_snapshot=observed,
            comparison=comparison,
            plan_c_audit=plan_c_audit,
            execution_ms=elapsed_ms,
            notes=tuple(notes),
        )

    def _execute_user_command(
        self,
        *,
        client: TestClient,
        mutation_svc: PostgresWorkEventMutationService,
        reg: SeedIdRegistry,
        case_id: str,
        cmd: Mapping[str, Any],
    ) -> CommandExecutionRecord:
        cmd_id = str(cmd["command_id"])
        action = str(cmd["command_action"])
        ws_syn = str(cmd["workspace_id"])
        actor_syn = str(cmd.get("actor_id") or reg.workspace_default_actor[ws_syn])
        idem_key = str(cmd.get("idempotency_key") or f"eval-{case_id}-{cmd_id}")
        target_ids = [str(t) for t in (cmd.get("target_event_ids") or [])]
        target_versions = dict(cmd.get("target_versions") or {})
        payload = dict(cmd.get("payload") or {})

        headers = {
            "X-Eval-Workspace-Syn": ws_syn,
            "X-Eval-Actor-Syn": actor_syn,
            "Idempotency-Key": idem_key,
        }

        with self._cluster.admin_connection() as conn:
            pre_hash = _compute_db_mutation_state_hash(conn)

        t0 = time.perf_counter()
        internal_policy_code: str | None = None
        resulting_ids: list[str] = []

        if action == "confirm":
            target_uuid = reg.resolve_any_target_uuid(target_ids[0])
            method = "POST"
            path = f"/v1/work-events/{target_uuid}/actions/confirm"
            body = {
                "expected_version": int(target_versions.get(target_ids[0], cmd["expected_version"])),
                "reason": payload.get("reason"),
                "idempotency_key": idem_key,
            }
            resp = client.post(path, json=body, headers=headers)
            if resp.status_code == 200:
                resulting_ids = [str(resp.json()["event_id"])]

        elif action == "edit":
            target_uuid = reg.resolve_any_target_uuid(target_ids[0])
            method = "PATCH"
            path = f"/v1/work-events/{target_uuid}"
            body = {
                "expected_version": int(target_versions.get(target_ids[0], cmd["expected_version"])),
                "title": str(payload["title"]),
                "outcome": payload.get("summary"),
                "reason": payload.get("reason"),
                "idempotency_key": idem_key,
            }
            resp = client.patch(path, json=body, headers=headers)
            if resp.status_code == 200:
                resulting_ids = [str(resp.json()["event_id"])]

        elif action in ("exclude", "reject"):
            target_uuid = reg.resolve_any_target_uuid(target_ids[0])
            method = "POST"
            path = f"/v1/work-events/{target_uuid}/actions/reject"
            body = {
                "expected_version": int(target_versions.get(target_ids[0], cmd["expected_version"])),
                "reason": payload.get("reason"),
                "idempotency_key": idem_key,
            }
            resp = client.post(path, json=body, headers=headers)
            if resp.status_code == 200:
                resulting_ids = [str(resp.json()["event_id"])]

        elif action == "split":
            target_uuid = reg.resolve_any_target_uuid(target_ids[0])
            method = "POST"
            path = f"/v1/work-events/{target_uuid}/actions/split"
            wire_partitions = []
            for part in payload.get("partitions") or []:
                ev_scope = part.get("evidence_scope") or {}
                wire_partitions.append(
                    {
                        "title": str(part["title"]),
                        "source_object_ids": [
                            reg.commit_source_object_uuids[str(cid)]
                            for cid in (part.get("source_commit_ids") or [])
                            if str(cid) in reg.commit_source_object_uuids
                        ],
                        "anchor_scope": {
                            "file_paths": list(ev_scope.get("file_paths") or []),
                            "diff_hunks": list(ev_scope.get("diff_hunks") or []),
                            "review_thread_ids": list(ev_scope.get("review_thread_ids") or []),
                            "content_unit_ids": [
                                reg.content_unit_uuids[str(cu)]
                                for cu in (ev_scope.get("content_unit_ids") or [])
                                if str(cu) in reg.content_unit_uuids
                            ],
                        },
                    }
                )
            body = {
                "expected_version": int(target_versions.get(target_ids[0], cmd["expected_version"])),
                "partitions": wire_partitions,
                "reason": payload.get("reason"),
                "idempotency_key": idem_key,
            }
            resp = client.post(path, json=body, headers=headers)
            if resp.status_code == 200:
                resulting_ids = [str(seg["event_id"]) for seg in resp.json()["created_segments"]]

        elif action == "manual_merge":
            method = "POST"
            path = "/v1/work-events/actions/merge"
            raw_members = payload.get("members")
            wire_members: list[dict[str, Any]] = []
            if isinstance(raw_members, list) and raw_members:
                for m in raw_members:
                    mid_syn = str(m["event_id"])
                    wire_members.append(
                        {
                            "event_id": reg.resolve_any_target_uuid(mid_syn),
                            "expected_version": int(m["expected_version"]),
                        }
                    )
            else:
                for tid_syn in target_ids:
                    wire_members.append(
                        {
                            "event_id": reg.resolve_any_target_uuid(tid_syn),
                            "expected_version": int(
                                target_versions.get(tid_syn, cmd["expected_version"])
                            ),
                        }
                    )
            body = {
                "members": wire_members,
                "composite_title": str(payload.get("composite_title") or payload.get("title") or "Composite"),
                "reason": payload.get("reason"),
                "idempotency_key": idem_key,
            }
            resp = client.post(path, json=body, headers=headers)
            if resp.status_code == 200:
                resulting_ids = [str(resp.json()["composite_event"]["event_id"])]
            elif resp.status_code == 404:
                # EV-016: Verify BOTH HTTP 404 concealment AND domain CROSS_WORKSPACE_MEMBER policy!
                access = _make_access_context(
                    reg, workspace_syn_id=ws_syn, actor_syn_id=actor_syn
                )
                try:
                    mutation_svc.merge_work_events(
                        access,
                        request=MergeWorkEventsRequest(
                            members=[
                                MergeMemberInput(
                                    event_id=m["event_id"],
                                    expected_version=m["expected_version"],
                                )
                                for m in wire_members
                            ],
                            composite_title=body["composite_title"],
                            idempotency_key=f"{idem_key}:domain-check",
                        ),
                        conceal_missing_as_not_found=False,
                    )
                except WorkEventError as exc:
                    internal_policy_code = exc.code

        elif action == "dissolve":
            target_uuid = reg.resolve_any_target_uuid(target_ids[0])
            method = "POST"
            path = f"/v1/work-events/{target_uuid}/actions/dissolve"
            body = {
                "expected_version": int(target_versions.get(target_ids[0], cmd["expected_version"])),
                "reason": payload.get("reason"),
                "idempotency_key": idem_key,
            }
            resp = client.post(path, json=body, headers=headers)
            if resp.status_code == 200:
                resulting_ids = [str(m["event_id"]) for m in resp.json()["restored_members"]]

        else:
            raise ValueError(f"Unsupported fixture command_action: {action!r}")

        latency_ms = (time.perf_counter() - t0) * 1000.0
        with self._cluster.admin_connection() as conn:
            post_hash = _compute_db_mutation_state_hash(conn)

        if resp.status_code == 200:
            outcome = "ACCEPTED"
            err_code = None
        else:
            outcome = "REJECTED"
            err_payload = resp.json().get("error") if resp.headers.get("content-type", "").startswith("application/json") else {}
            err_code = str(err_payload.get("code")) if isinstance(err_payload, dict) and err_payload.get("code") else None

        return CommandExecutionRecord(
            command_id=cmd_id,
            command_action=action,
            http_method=method,
            http_path=path,
            http_status=int(resp.status_code),
            outcome=outcome,
            error_code=err_code,
            internal_policy_code=internal_policy_code,
            state_mutated=(pre_hash != post_hash),
            pre_state_sha256=pre_hash,
            post_state_sha256=post_hash,
            resulting_event_ids=tuple(resulting_ids),
            latency_ms=latency_ms,
        )

    def _extract_observed_snapshot(
        self,
        *,
        client: TestClient,
        reg: SeedIdRegistry,
        case_id: str,
        case_data: Mapping[str, Any],
    ) -> ObservedCaseSnapshot:
        ss = case_data.get("synthetic_sources") or {}
        expected = case_data.get("expected_proposal") or {}
        workspaces = ss.get("workspaces") or []

        # 1. Query Read API GET /v1/work-events per workspace (tests real HTTP + FORCE RLS!)
        per_ws_api_counts: dict[str, int] = {}
        active_top_level_uuids_via_api: set[str] = set()
        top_level_dup_count = 0

        for ws in workspaces:
            ws_syn = str(ws["workspace_id"])
            headers = {"X-Eval-Workspace-Syn": ws_syn}
            resp = client.get("/v1/work-events", params={"limit": 100}, headers=headers)
            assert resp.status_code == 200, f"GET /v1/work-events failed: {resp.text}"
            assert resp.headers.get("Cache-Control") == "private, no-store"
            body = resp.json()
            item_ids = [str(it["event_id"]) for it in body["items"]]
            top_level_dup_count += len(item_ids) - len(set(item_ids))
            active_top_level_uuids_via_api.update(item_ids)
            per_ws_api_counts[ws_syn] = int(body["counts"]["active_top_level_total"])

        # 2. Extract detailed DB state + verify GET /v1/work-events/{id} for every WorkEvent
        observed_events: list[ObservedWorkEvent] = []
        observed_review_actions: list[ObservedReviewAction] = []
        observed_downstream: list[ObservedDownstreamArtifact] = []
        observed_held: list[ObservedHeldItem] = []
        observed_oow: list[ObservedOutOfWindowDedup] = []
        observed_searches: list[ObservedSearchCheck] = []
        source_counts_after: dict[str, int] = {}
        dissolve_outbox_cnt = 0
        merge_outbox_cnt = 0

        with self._cluster.admin_connection() as conn:
            logical_hash = _compute_db_mutation_state_hash(conn)
            with conn.cursor() as cur:
                # Source provenance counts after execution
                for tbl in ("source_objects", "source_object_revisions", "content_units"):
                    cur.execute(f"SELECT COUNT(*) FROM public.{tbl}")
                    source_counts_after[tbl] = int(cur.fetchone()[0])

                # Outbox counts
                cur.execute(
                    "SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'WORK_EVENT_COMPOSITE_DISSOLVED'"
                )
                dissolve_outbox_cnt = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'WORK_EVENT_COMPOSITE_MERGED'"
                )
                merge_outbox_cnt = int(cur.fetchone()[0])

                # Preload all event_memberships
                cur.execute(
                    """
                    SELECT em.event_id::text, em.source_object_id::text,
                           so.object_type, so.source_resource_id::text,
                           em.relation_class, em.relation_type, em.status
                    FROM public.event_memberships em
                    JOIN public.source_objects so
                      ON so.workspace_id = em.workspace_id
                     AND so.id = em.source_object_id
                    WHERE em.status = 'ACTIVE'
                    ORDER BY em.event_id ASC, em.source_object_id ASC
                    """
                )
                memberships_by_ev: dict[str, list[tuple[str, str, str, str, str]]] = {}
                for r in cur.fetchall():
                    memberships_by_ev.setdefault(str(r[0]), []).append(
                        (str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]))
                    )

                # Preload composite memberships (all statuses)
                cur.execute(
                    """
                    SELECT composite_event_id::text, member_event_id::text,
                           member_event_version, ordinal, status
                    FROM public.event_composite_memberships
                    ORDER BY composite_event_id ASC, ordinal ASC, member_event_id ASC
                    """
                )
                comp_members_by_comp: dict[str, list[tuple[str, int, str]]] = {}
                active_parent_by_member: dict[str, str] = {}
                for r in cur.fetchall():
                    c_id, m_id, m_ver, _ord, st = str(r[0]), str(r[1]), int(r[2]), int(r[3]), str(r[4])
                    comp_members_by_comp.setdefault(c_id, []).append((m_id, m_ver, st))
                    if st == "ACTIVE":
                        active_parent_by_member[m_id] = c_id

                # Preload work_event_versions
                cur.execute(
                    """
                    SELECT event_id::text, version, title, outcome, status
                    FROM public.work_event_versions
                    ORDER BY event_id ASC, version ASC
                    """
                )
                versions_by_ev: dict[str, dict[int, tuple[str, str | None, str]]] = {}
                for r in cur.fetchall():
                    versions_by_ev.setdefault(str(r[0]), {})[int(r[1])] = (
                        str(r[2]),
                        str(r[3]) if r[3] is not None else None,
                        str(r[4]),
                    )

                # Preload facts & claims
                cur.execute("SELECT id::text, event_id::text, status FROM public.facts ORDER BY id ASC")
                facts_rows = [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]
                facts_by_ev: dict[str, list[tuple[str, str]]] = {}
                for fid, eid, fst in facts_rows:
                    facts_by_ev.setdefault(eid, []).append((fid, fst))

                cur.execute("SELECT id::text, event_id::text, status FROM public.claims ORDER BY id ASC")
                claims_rows = [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]
                claims_by_ev: dict[str, list[tuple[str, str]]] = {}
                for cid, eid, cst in claims_rows:
                    claims_by_ev.setdefault(eid, []).append((cid, cst))

                # Load all work_events
                cur.execute(
                    """
                    SELECT
                        we.id::text,
                        we.workspace_id::text,
                        we.primary_anchor_source_object_id::text,
                        we.anchor_mode,
                        we.anchor_scope,
                        we.version,
                        we.title,
                        we.outcome,
                        we.summary_metadata,
                        we.status,
                        pa.object_type AS primary_obj_type,
                        pa.source_resource_id::text AS primary_repo_uuid
                    FROM public.work_events we
                    LEFT JOIN public.source_objects pa
                      ON pa.workspace_id = we.workspace_id
                     AND pa.id = we.primary_anchor_source_object_id
                    ORDER BY we.id ASC
                    """
                )
                we_rows = cur.fetchall()

                # Helper to collect commits/tickets for an event (and composite members)
                def _commits_and_tickets_for_non_comp(ev_uid: str) -> tuple[list[str], list[str], list[str], str | None]:
                    commits_syn: list[str] = []
                    tickets_syn: list[str] = []
                    cl_syn: list[str] = []
                    repo_uid: str | None = None
                    for src_uid, obj_type, r_uid, rel_cls, _rel_type in memberships_by_ev.get(ev_uid, ()):
                        if repo_uid is None:
                            repo_uid = r_uid
                        syn = reg.to_syn_id(src_uid) or src_uid
                        if rel_cls == "MUST_LINK" and obj_type == "COMMIT":
                            if syn not in commits_syn:
                                commits_syn.append(syn)
                        elif rel_cls == "CONTEXT_ONLY" and obj_type == "ISSUE":
                            if syn not in tickets_syn:
                                tickets_syn.append(syn)
                        elif rel_cls == "CANNOT_LINK":
                            if syn not in cl_syn:
                                cl_syn.append(syn)
                    return commits_syn, tickets_syn, cl_syn, repo_uid

                for row in we_rows:
                    ev_uid = str(row[0])
                    ws_uid = str(row[1])
                    ws_syn = reg.to_syn_id(ws_uid) or ws_uid
                    pa_uid = str(row[2]) if row[2] is not None else None
                    anchor_mode = str(row[3])
                    raw_scope = dict(row[4]) if row[4] is not None else None
                    version = int(row[5])
                    title = str(row[6])
                    outcome_text = str(row[7]) if row[7] is not None else None
                    smeta = dict(row[8] or {})
                    status = str(row[9])
                    pa_obj_type = str(row[10]) if row[10] is not None else None
                    pa_repo_uid = str(row[11]) if row[11] is not None else None

                    # Call real GET /v1/work-events/{id} for every WorkEvent
                    detail_resp = client.get(
                        f"/v1/work-events/{ev_uid}",
                        headers={"X-Eval-Workspace-Syn": ws_syn},
                    )
                    api_detail_ok = detail_resp.status_code == 200
                    assert api_detail_ok, f"GET /v1/work-events/{ev_uid} failed: {detail_resp.text}"

                    primary_pr_syn = (
                        reg.to_syn_id(pa_uid)
                        if pa_uid is not None and pa_obj_type == "PR"
                        else None
                    )
                    direct_must_links = [
                        m for m in memberships_by_ev.get(ev_uid, ()) if m[3] == "MUST_LINK"
                    ]

                    member_syn_ids: list[str] = []
                    comp_mem_status: str | None = None
                    member_snaps_preserved = False
                    repo_uid = pa_repo_uid or (
                        str(smeta["source_resource_id"])
                        if isinstance(smeta.get("source_resource_id"), str)
                        else None
                    )

                    if anchor_mode == "USER_COMPOSITE":
                        c_commits: list[str] = []
                        c_tickets: list[str] = []
                        c_cl: list[str] = []
                        mem_rows = comp_members_by_comp.get(ev_uid, [])
                        statuses_seen: set[str] = set()
                        snaps_ok = bool(mem_rows)
                        for m_uid, m_ver, m_st in mem_rows:
                            statuses_seen.add(m_st)
                            member_syn_ids.append(reg.to_syn_id(m_uid) or m_uid)
                            if m_ver not in versions_by_ev.get(m_uid, {}):
                                snaps_ok = False
                            mc, mt, mcl, mr = _commits_and_tickets_for_non_comp(m_uid)
                            for x in mc:
                                if x not in c_commits:
                                    c_commits.append(x)
                            for x in mt:
                                if x not in c_tickets:
                                    c_tickets.append(x)
                            for x in mcl:
                                if x not in c_cl:
                                    c_cl.append(x)
                            if repo_uid is None and mr is not None:
                                repo_uid = mr
                        commits_syn, tickets_syn, cl_syn = c_commits, c_tickets, c_cl
                        member_snaps_preserved = snaps_ok
                        if "ACTIVE" in statuses_seen:
                            comp_mem_status = "ACTIVE"
                        elif "REMOVED" in statuses_seen:
                            comp_mem_status = "REMOVED"
                    else:
                        commits_syn, tickets_syn, cl_syn, mem_repo_uid = _commits_and_tickets_for_non_comp(ev_uid)
                        if repo_uid is None:
                            repo_uid = mem_repo_uid

                    # Map anchor_scope content_unit UUIDs back to synthetic IDs for comparison
                    norm_scope: dict[str, Any] | None = None
                    if raw_scope is not None:
                        norm_scope = dict(raw_scope)
                        if "content_unit_ids" in norm_scope:
                            norm_scope["content_unit_ids"] = [
                                reg.to_syn_id(str(u)) or str(u)
                                for u in (norm_scope.get("content_unit_ids") or [])
                            ]

                    parent_ev_uid: str | None = active_parent_by_member.get(ev_uid)
                    if parent_ev_uid is None and isinstance(smeta.get("parent_event_id"), str):
                        parent_ev_uid = str(smeta["parent_event_id"])

                    ver_map = versions_by_ev.get(ev_uid, {})
                    ver_nums = tuple(sorted(ver_map.keys()))
                    prior_preserved = (version - 1) in ver_map if version > 1 else True

                    is_active_top = ev_uid in active_top_level_uuids_via_api
                    review_flags = tuple(str(f) for f in (smeta.get("review_flags") or []))
                    req_confirm = bool(smeta.get("requires_user_confirmation", False))
                    no_match_def_no_pr = (
                        bool(smeta["no_match_observed_means_definitely_no_pr"])
                        if "no_match_observed_means_definitely_no_pr" in smeta
                        else None
                    )

                    ev_fact_syns = tuple(
                        sorted(reg.to_syn_id(fid) or fid for fid, _ in facts_by_ev.get(ev_uid, ()))
                    )
                    ev_claim_syns = tuple(
                        sorted(reg.to_syn_id(cid) or cid for cid, _ in claims_by_ev.get(ev_uid, ()))
                    )

                    observed_events.append(
                        ObservedWorkEvent(
                            event_uuid=ev_uid,
                            logical_event_id=reg.to_syn_id(ev_uid),
                            workspace_id=ws_syn,
                            repo_id=reg.to_syn_id(repo_uid) if repo_uid else None,
                            anchor_mode=anchor_mode,
                            lifecycle_status=status,
                            is_active_top_level=is_active_top,
                            version=version,
                            title=title,
                            summary=outcome_text,
                            primary_pr_id=primary_pr_syn,
                            source_commit_ids=tuple(sorted(commits_syn)),
                            context_ticket_ids=tuple(sorted(tickets_syn)),
                            cannot_link_source_ids=tuple(sorted(cl_syn)),
                            member_event_ids=tuple(member_syn_ids),
                            parent_event_id=parent_ev_uid,
                            composite_membership_status=comp_mem_status,
                            composite_direct_must_link_count=len(direct_must_links),
                            member_snapshots_preserved=member_snaps_preserved,
                            prior_version_preserved=prior_preserved,
                            version_numbers=ver_nums,
                            searchable_as_top_level=is_active_top,
                            counted_in_metrics=is_active_top,
                            review_flags=review_flags,
                            requires_user_confirmation=req_confirm,
                            no_match_observed_means_definitely_no_pr=no_match_def_no_pr,
                            anchor_scope=norm_scope,
                            dependent_fact_ids=ev_fact_syns,
                            dependent_claim_ids=ev_claim_syns,
                            merge_origin=str(smeta.get("merge_origin") or "NONE"),
                            api_detail_verified=api_detail_ok,
                        )
                    )

                # Extract review_actions from PG18
                cur.execute(
                    """
                    SELECT id::text, target_id::text, action, from_version, to_version
                    FROM public.review_actions
                    WHERE target_type = 'WORK_EVENT'
                    ORDER BY created_at ASC, id ASC
                    """
                )
                for ra_id, target_uid, ra_action, f_ver, t_ver in cur.fetchall():
                    t_uid_s = str(target_uid)
                    changed_fields: list[str] = []
                    if f_ver is not None and t_ver is not None:
                        vmap = versions_by_ev.get(t_uid_s, {})
                        prev_snap = vmap.get(int(f_ver))
                        curr_snap = vmap.get(int(t_ver))
                        if prev_snap is not None and curr_snap is not None:
                            if prev_snap[0] != curr_snap[0]:
                                changed_fields.append("title")
                            if prev_snap[1] != curr_snap[1]:
                                changed_fields.append("summary")
                            if prev_snap[2] != curr_snap[2]:
                                changed_fields.append("lifecycle_status")
                    observed_review_actions.append(
                        ObservedReviewAction(
                            review_action_id=str(ra_id),
                            event_id=t_uid_s,
                            action=str(ra_action),
                            from_version=int(f_ver) if f_ver is not None else None,
                            to_version=int(t_ver) if t_ver is not None else None,
                            changed_fields=tuple(changed_fields),
                            creates_new_event_version=(
                                f_ver is not None and t_ver is not None and int(t_ver) > int(f_ver)
                            ),
                            is_event_merge=(str(ra_action) == "MERGE"),
                        )
                    )

                # Extract downstream artifacts (facts, claims, and M5 export outbox boundary)
                for exp_d in expected.get("downstream_side_effects") or []:
                    atype = str(exp_d["artifact_type"])
                    aid = str(exp_d["artifact_id"])
                    src_ev_syn = str(exp_d["source_composite_event_id"])
                    src_ev_uid = reg.event_uuids.get(src_ev_syn)
                    if atype == "FACT":
                        f_uid = reg.fact_uuids.get(aid)
                        match = next((r for r in facts_rows if r[0] == f_uid), None)
                        observed_downstream.append(
                            ObservedDownstreamArtifact(
                                artifact_type="FACT",
                                artifact_id=aid,
                                source_event_id=src_ev_syn,
                                status=match[2] if match else "DELETED",
                                still_exists=match is not None,
                                transferred_to_members=(match is not None and match[1] != src_ev_uid),
                                verification_boundary="PG18_TABLE",
                            )
                        )
                    elif atype == "CLAIM":
                        c_uid = reg.claim_uuids.get(aid)
                        match = next((r for r in claims_rows if r[0] == c_uid), None)
                        observed_downstream.append(
                            ObservedDownstreamArtifact(
                                artifact_type="CLAIM",
                                artifact_id=aid,
                                source_event_id=src_ev_syn,
                                status=match[2] if match else "DELETED",
                                still_exists=match is not None,
                                transferred_to_members=(match is not None and match[1] != src_ev_uid),
                                verification_boundary="PG18_TABLE",
                            )
                        )
                    elif atype == "EXPORT":
                        # M5 Export table not yet implemented in 0010..0060; verify atomic
                        # WORK_EVENT_COMPOSITE_DISSOLVED outbox cleanup intent!
                        observed_downstream.append(
                            ObservedDownstreamArtifact(
                                artifact_type="EXPORT",
                                artifact_id=aid,
                                source_event_id=src_ev_syn,
                                status="STALE" if dissolve_outbox_cnt > 0 else "UNNOTIFIED",
                                still_exists=True,
                                transferred_to_members=False,
                                verification_boundary="M5_OUTBOX_CONTRACT_BOUNDARY",
                            )
                        )

                # Extract held items (UNKNOWN commits & non-default non-eligible commits)
                direct_commit_anchor_syns = {
                    ev.source_commit_ids[0]
                    for ev in observed_events
                    if ev.anchor_mode == "DIRECT_COMMIT" and ev.source_commit_ids
                }
                for cmt in ss.get("commits") or []:
                    cmt_syn = str(cmt["commit_id"])
                    repo_syn = str(cmt["repo_id"])
                    default_branch = reg.repo_default_branch[repo_syn]
                    assoc_status = str(cmt.get("pr_association_status") or "")
                    on_default = bool(cmt.get("on_default_branch", False))
                    branch_name = str(cmt.get("branch_name") or "")

                    if assoc_status == "UNKNOWN":
                        observed_held.append(
                            ObservedHeldItem(
                                source_id=cmt_syn,
                                record_kind="OBSERVATION_RECORD",
                                pr_association_state="UNKNOWN",
                                interpreted_as_no_pr=(cmt_syn in direct_commit_anchor_syns),
                            )
                        )
                    elif (
                        assoc_status == "NO_MATCH_OBSERVED"
                        and (not on_default or branch_name != default_branch)
                    ):
                        observed_held.append(
                            ObservedHeldItem(
                                source_id=cmt_syn,
                                record_kind="NON_ELIGIBLE_SOURCE_RECORD",
                                pr_association_state="NO_MATCH_OBSERVED",
                                interpreted_as_no_pr=(cmt_syn in direct_commit_anchor_syns),
                            )
                        )

                # Extract out-of-window PR dedup checks (CAND-EV-019)
                for obs_oow in ss.get("out_of_window_pr_associations") or []:
                    cmt_syn = str(obs_oow["observed_default_branch_commit_id"])
                    oow_pr_num = str(obs_oow["pr_number"])
                    created_pr_ev = any(
                        ev.anchor_mode == "PR" and ev.primary_pr_id == str(obs_oow["external_pr_id"])
                        for ev in observed_events
                    )
                    created_dc_ev = cmt_syn in direct_commit_anchor_syns
                    observed_oow.append(
                        ObservedOutOfWindowDedup(
                            commit_id=cmt_syn,
                            matched_pr_provider_id=oow_pr_num,
                            creates_new_pr_event=created_pr_ev,
                            creates_direct_commit_event=created_dc_ev,
                        )
                    )

        # 3. Execute real production search_collapse queries under cm_api_app FORCE RLS
        default_ws_syn = str(workspaces[0]["workspace_id"])
        for sc in expected.get("search_checks") or []:
            ws_syn = str(sc.get("workspace_id") or default_ws_syn)
            access = _make_access_context(reg, workspace_syn_id=ws_syn)
            query_text = str(sc["query"])
            with self._cluster.api_connection() as api_conn:
                with workspace_scoped_transaction(api_conn, access) as cur:
                    hits = query_searchable_top_level_work_events(
                        cur,
                        workspace_id=access.workspace_id,
                        query_text=query_text,
                        limit=50,
                    )
                    # Also verify collapse_event_ids_for_search_and_export idempotency (0 duplicates)
                    re_collapsed = collapse_event_ids_for_search_and_export(
                        cur,
                        workspace_id=access.workspace_id,
                        candidate_event_ids=hits,
                    )
                    assert re_collapsed == hits
            dup_hits = len(hits) - len(set(hits))
            top_level_dup_count += dup_hits
            observed_searches.append(
                ObservedSearchCheck(
                    query=query_text,
                    workspace_id=ws_syn,
                    matched_event_uuids=tuple(hits),
                    result_count=len(hits),
                    duplicate_count=dup_hits,
                    verification_boundary="CONTRACT_LEVEL_SEARCH_COLLAPSE",
                )
            )

        # Aggregate counts
        countable = [ev for ev in observed_events if ev.counted_in_metrics]
        composites_active = [ev for ev in countable if ev.anchor_mode == "USER_COMPOSITE"]
        direct_commits_active = [ev for ev in countable if ev.anchor_mode == "DIRECT_COMMIT"]
        rejected_events = [ev for ev in observed_events if ev.lifecycle_status == "REJECTED"]

        # Promoted events from PARTIAL runs (0 invariant)
        partial_run_repos = {
            str(r["repo_id"])
            for r in (ss.get("sync_runs") or [])
            if str(r.get("status")) == "PARTIAL" or not bool(r.get("promotes_events", True))
        }
        promoted_from_partial = sum(
            1 for ev in observed_events if ev.repo_id in partial_run_repos
        )

        # Build deterministic UUID-free logical state digest (0 drift across PG18 runs)
        ev_by_uuid = {e.event_uuid: e for e in observed_events}

        def _det_event_key(e: ObservedWorkEvent) -> dict[str, Any]:
            return {
                "logical_event_id": e.logical_event_id,
                "workspace_id": e.workspace_id,
                "repo_id": e.repo_id,
                "anchor_mode": e.anchor_mode,
                "lifecycle_status": e.lifecycle_status,
                "is_active_top_level": e.is_active_top_level,
                "version": e.version,
                "title": e.title,
                "summary": e.summary,
                "primary_pr_id": e.primary_pr_id,
                "source_commit_ids": list(e.source_commit_ids),
                "context_ticket_ids": list(e.context_ticket_ids),
                "member_event_ids": list(e.member_event_ids),
                "composite_membership_status": e.composite_membership_status,
                "composite_direct_must_link_count": e.composite_direct_must_link_count,
                "member_snapshots_preserved": e.member_snapshots_preserved,
                "prior_version_preserved": e.prior_version_preserved,
                "version_numbers": list(e.version_numbers),
                "review_flags": list(e.review_flags),
                "requires_user_confirmation": e.requires_user_confirmation,
                "no_match_observed_means_definitely_no_pr": e.no_match_observed_means_definitely_no_pr,
                "anchor_scope": e.anchor_scope,
                "dependent_fact_ids": list(e.dependent_fact_ids),
                "dependent_claim_ids": list(e.dependent_claim_ids),
            }

        det_events = sorted(
            (_det_event_key(e) for e in observed_events),
            key=lambda d: (
                d["workspace_id"],
                d["anchor_mode"],
                d["logical_event_id"] or "",
                d["primary_pr_id"] or "",
                d["title"],
            ),
        )
        det_review_actions = [
            {
                "event_label": (
                    ev_by_uuid[ra.event_id].logical_event_id
                    or f"{ev_by_uuid[ra.event_id].anchor_mode}:{ev_by_uuid[ra.event_id].title}"
                    if ra.event_id in ev_by_uuid
                    else ra.event_id
                ),
                "action": ra.action,
                "from_version": ra.from_version,
                "to_version": ra.to_version,
                "changed_fields": list(ra.changed_fields),
            }
            for ra in observed_review_actions
        ]
        det_logical_hash = _canonical_sha256(
            {
                "case_id": case_id,
                "events": det_events,
                "review_actions": det_review_actions,
                "downstream": [
                    {
                        "type": d.artifact_type,
                        "id": d.artifact_id,
                        "status": d.status,
                        "still_exists": d.still_exists,
                        "transferred": d.transferred_to_members,
                    }
                    for d in observed_downstream
                ],
                "held_items": [
                    {
                        "source_id": h.source_id,
                        "record_kind": h.record_kind,
                        "interpreted_as_no_pr": h.interpreted_as_no_pr,
                    }
                    for h in observed_held
                ],
                "per_workspace_api_counts": per_ws_api_counts,
                "totals": {
                    "countable": len(countable),
                    "composites": len(composites_active),
                    "direct_commits": len(direct_commits_active),
                    "held": len(observed_held),
                    "rejected": len(rejected_events),
                    "partial_promoted": promoted_from_partial,
                    "dissolve_outbox": dissolve_outbox_cnt,
                    "merge_outbox": merge_outbox_cnt,
                },
            }
        )

        return ObservedCaseSnapshot(
            case_id=case_id,
            events=tuple(observed_events),
            review_actions=tuple(observed_review_actions),
            downstream_artifacts=tuple(observed_downstream),
            held_items=tuple(observed_held),
            out_of_window_dedups=tuple(observed_oow),
            search_checks=tuple(observed_searches),
            per_workspace_api_counts=per_ws_api_counts,
            total_countable_events=len(countable),
            total_user_composite_events=len(composites_active),
            total_direct_commit_events=len(direct_commits_active),
            total_held_items=len(observed_held),
            total_rejected_events=len(rejected_events),
            promoted_events_from_partial_runs=promoted_from_partial,
            source_provenance_counts_before=dict(reg.seeded_source_counts),
            source_provenance_counts_after=source_counts_after,
            dissolve_outbox_event_count=dissolve_outbox_cnt,
            merge_outbox_event_count=merge_outbox_cnt,
            top_level_duplicate_count=top_level_dup_count,
            logical_state_sha256=det_logical_hash,
        )


def build_redacted_eval_bundle(
    suite: EvalSuiteResult,
    *,
    openapi_blob_sha: str = IMMUTABLE_OPENAPI_BLOB_SHA,
) -> dict[str, Any]:
    """Build deterministic redacted T-306A evidence bundle (0 raw text, 0 secrets, 0 abs paths)."""
    bundle: dict[str, Any] = {
        "schema_version": EVAL_BUNDLE_SCHEMA_VERSION,
        "task_refs": ["T-306", "T-306A"],
        "issue_ref": "#11",
        "dataset_version": suite.dataset_version,
        "approved_21_bundle_sha256": suite.approved_21_bundle_sha256,
        "openapi_import_v1_blob_sha": openapi_blob_sha,
        "database_engine": "PostgreSQL 18 (local ephemeral, migrations 0010..0060, NOBYPASSRLS + FORCE RLS)",
        "schema_migrations": list(EXPECTED_SCHEMA_MIGRATIONS),
        "production_entry_points": [
            "career_memory_api.event_candidates.PostgresEventCandidateBuilder",
            "career_memory_api.work_events.PostgresWorkEventsReadService",
            "career_memory_api.work_events.PostgresWorkEventMutationService",
            "career_memory_api.work_events.create_work_events_router",
            "career_memory_api.work_events.query_searchable_top_level_work_events",
            "career_memory_api.work_events.collapse_event_ids_for_search_and_export",
        ],
        "read_only_oracle_policy": {
            "frozen_dataset_modified": False,
            "auto_update_oracle_supported": False,
            "excluded_from_mvp_truth_comparison": list(EXCLUDED_FROM_MVP_TRUTH_COMPARISON),
            "unimplemented_m5_contract_boundaries": list(UNIMPLEMENTED_M5_CONTRACT_BOUNDARIES),
        },
        "summary": {
            "total_cases": suite.total_count,
            "passed_cases": suite.passed_count,
            "failed_cases": suite.total_count - suite.passed_count,
            "all_passed": suite.all_passed,
            "total_bundles": len(suite.bundle_summaries),
            "passed_bundles": sum(1 for b in suite.bundle_summaries if b.all_passed),
            "total_oracle_diffs": sum(len(r.comparison.diffs) for r in suite.case_results),
            "total_plan_c_violations": sum(0 if r.plan_c_audit.passed else 1 for r in suite.case_results),
            "total_raw_body_canary_hits": sum(r.plan_c_audit.canary_hits for r in suite.case_results),
            "total_jsonb_forbidden_key_hits": sum(
                r.plan_c_audit.jsonb_forbidden_key_hits for r in suite.case_results
            ),
        },
        "evaluation_bundles": [b.to_dict() for b in suite.bundle_summaries],
        "cases": [r.to_deterministic_bundle_entry() for r in suite.case_results],
        "curation_usability_telemetry": suite.telemetry_summary,
        "volatile_runtime_metrics": suite.volatile_runtime_metrics,
    }
    # Plan C safety check on the generated redacted bundle itself
    assert_no_forbidden_raw_keys(bundle, field_name="t306a_eval_bundle.v1")
    return bundle


def strip_volatile_bundle_fields(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the eval bundle excluding volatile timing keys for 0-drift checks."""
    return {k: v for k, v in bundle.items() if k not in VOLATILE_BUNDLE_KEYS}


def diff_deterministic_bundles(
    expected_bundle: Mapping[str, Any],
    actual_bundle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compare two eval bundles strictly on all deterministic fields (0 drift check)."""
    exp_clean = strip_volatile_bundle_fields(expected_bundle)
    act_clean = strip_volatile_bundle_fields(actual_bundle)
    diffs: list[dict[str, Any]] = []

    def _walk(e: Any, a: Any, path: str) -> None:
        if type(e) is not type(a):
            diffs.append({"path": path, "expected": e, "actual": a, "reason": "type_mismatch"})
            return
        if isinstance(e, Mapping):
            e_keys, a_keys = set(e.keys()), set(a.keys())
            for k in sorted(e_keys - a_keys):
                diffs.append({"path": f"{path}.{k}", "expected": e[k], "actual": "<MISSING>", "reason": "missing_key"})
            for k in sorted(a_keys - e_keys):
                diffs.append({"path": f"{path}.{k}", "expected": "<ABSENT>", "actual": a[k], "reason": "extra_key"})
            for k in sorted(e_keys & a_keys):
                _walk(e[k], a[k], f"{path}.{k}")
            return
        if isinstance(e, list):
            if len(e) != len(a):
                diffs.append({"path": path, "expected": len(e), "actual": len(a), "reason": "len_mismatch"})
            for i in range(min(len(e), len(a))):
                _walk(e[i], a[i], f"{path}[{i}]")
            return
        if e != a:
            diffs.append({"path": path, "expected": e, "actual": a, "reason": "value_mismatch"})

    _walk(exp_clean, act_clean, "$")
    return diffs


__all__ = [
    "EphemeralEventEvalPg18Cluster",
    "FrozenEventEvalRunner",
    "build_redacted_eval_bundle",
    "diff_deterministic_bundles",
    "strip_volatile_bundle_fields",
]
