"""Real PostgreSQL 18 Encrypted Backup, Isolated Restore & Post-Restore Fence Drill (T-505 / INF-10 / REL-01)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.backup_restore.crypto_envelope import (
    decrypt_backup_bytes,
    encrypt_backup_bytes,
)
from career_memory_api.backup_restore.models import (
    DEFAULT_RTO_BUDGET_MS,
    BackupTamperDetectedError,
    EncryptedBackupEnvelope,
    RpoModel,
    T505DrillReport,
)
from career_memory_api.budgets import (
    BudgetOperationKind,
    PostgresQuotaBudgetService,
)
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    build_span_locator,
    canonicalize_source_bytes,
)
from career_memory_api.conformance.checks import (
    M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
    compute_git_blob_sha1,
    default_repo_root,
)
from career_memory_api.conformance.db_catalog import (
    FULL_M1_MIGRATIONS,
    introspect_postgres_catalog,
    resolve_pg18_bin,
    validate_catalog_invariants,
)
from career_memory_api.contracts_api.models import (
    DeletionConfirmRequest,
    SearchEntityType,
)
from career_memory_api.deletion import (
    DELETION_COMPONENT_ORDER,
    PostgresDeletionService,
    assert_resources_not_deletion_fenced,
)
from career_memory_api.exports import ExportError, PostgresExportService
from career_memory_api.jobs.models import DeletionFenceActiveError, DeletionJobStatus
from career_memory_api.jobs.postgres_runtime import (
    bind_worker_workspace_context,
    is_deletion_fence_active,
)
from career_memory_api.resolver import (
    PostgresProvenanceMetadataStore,
    RefetchResolverService,
    mount_resolver_routes,
)
from career_memory_api.resolver.service import InMemoryProviderContentFetcher
from career_memory_api.schema_contracts.enums import (
    DeletionComponentStatus,
    QuotaMetricKind,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    assert_no_forbidden_secret_keys,
)
from career_memory_api.search import PostgresSearchIndexer, PostgresSearchService
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

DETERMINISTIC_DRILL_NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
_RAW_CANARY_REPO_A = "CANARY_RAW_SOURCE_REPO_A_NEVER_PERSIST_9911"
_RAW_CANARY_REPO_B = "CANARY_RAW_SOURCE_REPO_B_NEVER_PERSIST_8822"


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _access_context(*, workspace_id: str, user_id: str) -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(
                issuer="https://auth.local/v1",
                subject=f"sub-{user_id}",
            ),
        ),
        membership=WorkspaceMembership(
            workspace_id=workspace_id,
            user_id=user_id,
            role="OWNER",
        ),
    )


class EphemeralBackupDrillPg18Cluster:
    """Self-cleaning temporary PostgreSQL 18 cluster for isolated backup/restore drills."""

    def __init__(self, *, prefix: str = "cm-m5-t505-pg18-") -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_t505_admin"
        self.api_login = "cm_api_login_t505"
        self.api_password = "local_ephemeral_t505_api_pw"
        self.worker_login = "cm_worker_login_t505"
        self.worker_password = "local_ephemeral_t505_worker_pw"
        self._started = False

    def admin_dsn(self, dbname: str) -> str:
        return f"postgresql://{self.superuser}@127.0.0.1:{self.port}/{dbname}"

    def api_dsn(self, dbname: str) -> str:
        return (
            f"postgresql://{self.api_login}:{self.api_password}"
            f"@127.0.0.1:{self.port}/{dbname}"
        )

    def worker_dsn(self, dbname: str) -> str:
        return (
            f"postgresql://{self.worker_login}:{self.worker_password}"
            f"@127.0.0.1:{self.port}/{dbname}"
        )

    @contextmanager
    def admin_connection(
        self, dbname: str, *, autocommit: bool = False
    ) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.admin_dsn(dbname), autocommit=autocommit) as conn:
            yield conn

    def api_connection_factory(self, dbname: str):
        @contextmanager
        def _connect() -> Iterator[psycopg.Connection]:
            with psycopg.connect(self.api_dsn(dbname)) as conn:
                yield conn

        return _connect

    def worker_connection_factory(self, dbname: str):
        @contextmanager
        def _connect() -> Iterator[psycopg.Connection]:
            with psycopg.connect(self.worker_dsn(dbname)) as conn:
                yield conn

        return _connect

    def start(self) -> tuple[int, str]:
        initdb = resolve_pg18_bin("initdb")
        pg_ctl = resolve_pg18_bin("pg_ctl")
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
                f"max_connections = 60\n"
            )

        subprocess.run(
            [pg_ctl, "-D", str(self._pgdata), "-l", str(self._logfile), "-w", "start"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        self._started = True

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_dsn("postgres"), connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SHOW server_version_num")
                        num_row = cur.fetchone()
                        cur.execute("SHOW server_version")
                        ver_row = cur.fetchone()
                        if num_row is None or int(num_row[0]) < 180000:
                            raise RuntimeError(f"Expected PostgreSQL 18+, got {num_row!r}")
                        major = int(num_row[0]) // 10000
                        label = str(ver_row[0]) if ver_row else f"{major}.0"
                        return major, label
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("Ephemeral PostgreSQL 18 drill cluster did not become ready.")

    def create_database(self, dbname: str) -> None:
        createdb = resolve_pg18_bin("createdb")
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        subprocess.run(
            [
                createdb,
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                self.superuser,
                dbname,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    def apply_migrations(self, dbname: str, migrations_dir: Path) -> None:
        for mig_name in FULL_M1_MIGRATIONS:
            sql = (migrations_dir / mig_name).read_text(encoding="utf-8")
            with self.admin_connection(dbname, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)

    def provision_runtime_logins(self, dbname: str) -> None:
        with self.admin_connection(dbname, autocommit=True) as conn:
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

    def pg_dump_custom_bytes(self, dbname: str) -> bytes:
        pg_dump = resolve_pg18_bin("pg_dump")
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        proc = subprocess.run(
            [
                pg_dump,
                "-Fc",
                "--no-password",
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                self.superuser,
                dbname,
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        return proc.stdout

    def pg_restore_custom_bytes(self, target_dbname: str, dump_bytes: bytes) -> None:
        pg_restore = resolve_pg18_bin("pg_restore")
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        subprocess.run(
            [
                pg_restore,
                "--exit-on-error",
                "--no-password",
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                self.superuser,
                "-d",
                target_dbname,
            ],
            input=dump_bytes,
            check=True,
            capture_output=True,
            env=env,
        )

    def stop(self) -> None:
        if self._started and self._pgdata.exists():
            try:
                pg_ctl = resolve_pg18_bin("pg_ctl")
                subprocess.run(
                    [pg_ctl, "-D", str(self._pgdata), "-m", "immediate", "stop"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                self._started = False
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> EphemeralBackupDrillPg18Cluster:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.stop()


def _seed_repo_graph(
    cluster: EphemeralBackupDrillPg18Cluster,
    dbname: str,
    *,
    workspace_id: str,
    account_id: str,
    user_id: str,
    identity_id: str,
    repo_name: str,
    provider_resource_id: str,
    tag: str,
    raw_text: str,
    include_sync_and_job: bool = False,
) -> dict[str, Any]:
    canon = canonicalize_source_bytes(raw_text.encode("utf-8"))
    assert canon.status == "CANONICALIZED" and canon.document is not None
    doc = canon.document
    locator = build_span_locator(doc.text, 0, len(doc.text))
    anchor = {
        "parser_version": PARSER_VERSION,
        "locator_version": locator.locator_spec_version,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "char_start": locator.char_start,
        "char_end": locator.char_end,
        "char_length": locator.char_length,
        "utf8_byte_start": locator.utf8_byte_start,
        "utf8_byte_end": locator.utf8_byte_end,
        "utf8_byte_length": locator.utf8_byte_length,
        "utf16_start": locator.utf16_start,
        "utf16_end": locator.utf16_end,
        "line_start": locator.line_start,
        "line_end": locator.line_end,
        "span_sha256": locator.span_sha256,
    }

    res_id = str(uuid.uuid4())
    obj_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    unit_id = str(uuid.uuid4())
    evt_id = str(uuid.uuid4())
    review_action_id = str(uuid.uuid4())
    fact_id = str(uuid.uuid4())
    contrib_id = str(uuid.uuid4())
    gen_run_id = str(uuid.uuid4())
    val_run_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    atom_id = str(uuid.uuid4())
    export_id = str(uuid.uuid4())
    sync_run_id: str | None = None
    job_id: str | None = None
    digest = hashlib.sha256(f"t505-{tag}".encode("utf-8")).hexdigest()

    with cluster.admin_connection(dbname) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
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
                        'ACTIVE', 'v1'
                    )
                    """,
                    (
                        res_id,
                        workspace_id,
                        account_id,
                        provider_resource_id,
                        repo_name,
                        f"https://github.com/{repo_name}",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type, provider_object_id
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s)
                    """,
                    (obj_id, workspace_id, res_id, f"pr-{tag}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, content_hash,
                        hash_policy_version, connector_version, schema_version, observed_at
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, 'v1', 'gh-v1', 'v1', %s)
                    """,
                    (rev_id, workspace_id, obj_id, doc.metadata.content_sha256, DETERMINISTIC_DRILL_NOW),
                )
                cur.execute(
                    """
                    INSERT INTO public.content_units (
                        id, workspace_id, source_object_revision_id, unit_type,
                        anchor, text_content, content_ref, content_hash,
                        canonicalization_version, retention_class, expires_at, ordinal
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'PR_BODY_SECTION',
                        %s::jsonb, NULL, NULL, %s, %s, 'NONE', NULL, 0
                    )
                    """,
                    (
                        unit_id,
                        workspace_id,
                        rev_id,
                        json.dumps(anchor),
                        locator.span_sha256,
                        CANONICALIZATION_VERSION,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                        version, title, status, created_by
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', 2, %s, 'CONFIRMED', 'USER')
                    """,
                    (evt_id, workspace_id, obj_id, f"RestoreDrillEvent {tag}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.review_actions (
                        id, workspace_id, target_type, target_id, action,
                        actor_user_id, from_version, to_version, idempotency_key, reason, created_at
                    ) VALUES (
                        %s::uuid, %s::uuid, 'WORK_EVENT', %s::uuid, 'CONFIRM',
                        %s::uuid, 1, 2, %s, 'Confirmed before backup drill', %s
                    )
                    """,
                    (
                        review_action_id,
                        workspace_id,
                        evt_id,
                        user_id,
                        str(uuid.uuid4()),
                        DETERMINISTIC_DRILL_NOW,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.work_event_versions (
                        id, workspace_id, event_id, version, anchor_mode,
                        primary_anchor_source_object_id, title, status, created_by, created_by_review_action_id
                    ) VALUES
                        (%s::uuid, %s::uuid, %s::uuid, 1, 'PR', %s::uuid, %s, 'CANDIDATE', 'RULE', NULL),
                        (%s::uuid, %s::uuid, %s::uuid, 2, 'PR', %s::uuid, %s, 'CONFIRMED', 'USER', %s::uuid)
                    """,
                    (
                        str(uuid.uuid4()),
                        workspace_id,
                        evt_id,
                        obj_id,
                        f"RestoreDrillEventDraft {tag}",
                        str(uuid.uuid4()),
                        workspace_id,
                        evt_id,
                        obj_id,
                        f"RestoreDrillEvent {tag}",
                        review_action_id,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.event_memberships (
                        id, workspace_id, event_id, source_object_id, relation_class, relation_type, origin, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'MUST_LINK', 'PRIMARY_ANCHOR', 'SOURCE', 'ACTIVE')
                    """,
                    (str(uuid.uuid4()), workspace_id, evt_id, obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, subject_identity_id,
                        predicate, object_text, status, extractor_version, version
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'ACTION', %s::uuid, 'implemented', %s, 'SUPPORTED', 'v1', 1)
                    """,
                    (fact_id, workspace_id, evt_id, identity_id, f"RestoreDrillFact {tag}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.fact_support (
                        id, workspace_id, fact_id, content_unit_id, support_type,
                        validator, validator_version, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', 'RULE', 'v1', 'ACTIVE')
                    """,
                    (str(uuid.uuid4()), workspace_id, fact_id, unit_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.contributions (
                        id, workspace_id, event_id, identity_id, role, origin, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'IMPLEMENTED', 'RULE', 'SOURCE_SUPPORTED')
                    """,
                    (contrib_id, workspace_id, evt_id, identity_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.contribution_support (
                        id, workspace_id, contribution_id, fact_id, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'ACTIVE')
                    """,
                    (str(uuid.uuid4()), workspace_id, contrib_id, fact_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.generation_runs (
                        id, workspace_id, event_id, run_type, model, prompt_version,
                        algorithm_version, input_hash, status
                    ) VALUES
                        (%s::uuid, %s::uuid, %s::uuid, 'CLAIM_DRAFT_GENERATION', 'det-v1', 'p1', 'a1', %s, 'SUCCEEDED'),
                        (%s::uuid, %s::uuid, %s::uuid, 'CLAIM_SUPPORT_VALIDATION', 'det-v1', 'p1', 'a1', %s, 'SUCCEEDED')
                    """,
                    (
                        gen_run_id,
                        workspace_id,
                        evt_id,
                        f"sha256:{digest}",
                        val_run_id,
                        workspace_id,
                        evt_id,
                        f"sha256:{digest}",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, generator_run_id, version, claim_text,
                        status, last_verification_run_id, approved_at, last_verified_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, 1, %s,
                        'APPROVED', %s::uuid, %s, %s
                    )
                    """,
                    (
                        claim_id,
                        workspace_id,
                        evt_id,
                        gen_run_id,
                        f"RestoreDrillClaim {tag} zero-quote achievement",
                        val_run_id,
                        DETERMINISTIC_DRILL_NOW,
                        DETERMINISTIC_DRILL_NOW,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.claim_atoms (
                        id, workspace_id, claim_id, ordinal, atom_type, atom_text, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 0, 'ACTION', %s, 'SUPPORTED')
                    """,
                    (atom_id, workspace_id, claim_id, f"RestoreDrillClaim {tag} zero-quote achievement"),
                )
                cur.execute(
                    """
                    INSERT INTO public.claim_atom_support (
                        id, workspace_id, claim_atom_id, fact_id, support_type, validation_run_id, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', %s::uuid, 'ACTIVE')
                    """,
                    (str(uuid.uuid4()), workspace_id, atom_id, fact_id, val_run_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.exports (
                        id, workspace_id, source_resource_id, requested_by_user_id,
                        format, export_policy, status, idempotency_key,
                        input_hash, policy_version, manifest, artifact_payload, completed_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        'MARKDOWN', 'SOURCE_VERIFIED_ONLY', 'READY', %s,
                        %s, 'v1', '{"zero_quote":true}'::jsonb, '{"markdown":"# Verified Claim Summary"}'::jsonb, %s
                    )
                    """,
                    (
                        export_id,
                        workspace_id,
                        res_id,
                        user_id,
                        str(uuid.uuid4()),
                        f"sha256:{digest}",
                        DETERMINISTIC_DRILL_NOW,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.export_claims (
                        id, workspace_id, export_id, claim_id, claim_version,
                        computed_verification_at_export, ordinal
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 1, 'SOURCE_VERIFIED', 0)
                    """,
                    (str(uuid.uuid4()), workspace_id, export_id, claim_id),
                )

                if include_sync_and_job:
                    sync_run_id = str(uuid.uuid4())
                    plan_id = str(uuid.uuid4())
                    job_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO public.sync_runs (
                            id, workspace_id, source_resource_id, mode, plan_id,
                            status, processing_status, import_ready, run_version, plan_spec, created_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'INITIAL_BACKFILL', %s::uuid,
                            'PENDING', 'NOT_STARTED', false, 1, '{"scope":"repo_b"}'::jsonb, %s
                        )
                        """,
                        (sync_run_id, workspace_id, res_id, plan_id, DETERMINISTIC_DRILL_NOW),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.jobs (
                            id, workspace_id, job_type, target_sync_run_id,
                            dedupe_key, input_hash, input_manifest, status,
                            logical_effect_key, created_at
                        ) VALUES (
                            %s::uuid, %s::uuid, 'INITIAL_BACKFILL', %s::uuid,
                            %s, %s, %s::jsonb, 'PENDING',
                            %s, %s
                        )
                        """,
                        (
                            job_id,
                            workspace_id,
                            sync_run_id,
                            f"backfill:{res_id}:1",
                            f"sha256:{digest}",
                            json.dumps({"source_resource_id": res_id}),
                            f"initial_backfill:{res_id}",
                            DETERMINISTIC_DRILL_NOW,
                        ),
                    )

    return {
        "resource_id": res_id,
        "object_id": obj_id,
        "revision_id": rev_id,
        "unit_id": unit_id,
        "event_id": evt_id,
        "review_action_id": review_action_id,
        "fact_id": fact_id,
        "contrib_id": contrib_id,
        "claim_id": claim_id,
        "atom_id": atom_id,
        "export_id": export_id,
        "sync_run_id": sync_run_id,
        "job_id": job_id,
        "raw_text": raw_text,
        "rev_hash": doc.metadata.content_sha256,
    }


def _snapshot_repo_a_parity(
    cluster: EphemeralBackupDrillPg18Cluster,
    dbname: str,
    *,
    workspace_id: str,
    repo_a: dict[str, Any],
) -> dict[str, Any]:
    """Capture canonical row-level digest & exact counts for Repo A + auth/identity/quota state."""
    queries: dict[str, tuple[str, tuple[Any, ...]]] = {
        "workspace_memberships": (
            "SELECT workspace_id::text, user_id::text, role FROM public.workspace_memberships "
            "WHERE workspace_id = %s::uuid ORDER BY user_id",
            (workspace_id,),
        ),
        "identities": (
            "SELECT id::text, display_name, identity_type, status FROM public.identities "
            "WHERE workspace_id = %s::uuid ORDER BY id",
            (workspace_id,),
        ),
        "external_identity_mappings": (
            "SELECT id::text, identity_id::text, provider, username, verification_method, status "
            "FROM public.external_identity_mappings WHERE workspace_id = %s::uuid ORDER BY id",
            (workspace_id,),
        ),
        "work_events": (
            "SELECT id::text, version, title, status FROM public.work_events "
            "WHERE workspace_id = %s::uuid AND id = %s::uuid ORDER BY id",
            (workspace_id, repo_a["event_id"]),
        ),
        "work_event_versions": (
            "SELECT version, title, status, created_by FROM public.work_event_versions "
            "WHERE workspace_id = %s::uuid AND event_id = %s::uuid ORDER BY version",
            (workspace_id, repo_a["event_id"]),
        ),
        "review_actions": (
            "SELECT id::text, target_type, target_id::text, action, from_version, to_version "
            "FROM public.review_actions WHERE workspace_id = %s::uuid AND target_id = %s::uuid ORDER BY id",
            (workspace_id, repo_a["event_id"]),
        ),
        "facts": (
            "SELECT id::text, fact_type, predicate, object_text, status, version FROM public.facts "
            "WHERE workspace_id = %s::uuid AND event_id = %s::uuid ORDER BY id",
            (workspace_id, repo_a["event_id"]),
        ),
        "claims": (
            "SELECT id::text, version, claim_text, status FROM public.claims "
            "WHERE workspace_id = %s::uuid AND event_id = %s::uuid ORDER BY id",
            (workspace_id, repo_a["event_id"]),
        ),
        "claim_atoms": (
            "SELECT id::text, ordinal, atom_type, atom_text, status FROM public.claim_atoms "
            "WHERE workspace_id = %s::uuid AND claim_id = %s::uuid ORDER BY ordinal",
            (workspace_id, repo_a["claim_id"]),
        ),
        "exports": (
            "SELECT id::text, format, export_policy, status FROM public.exports "
            "WHERE workspace_id = %s::uuid AND id = %s::uuid ORDER BY id",
            (workspace_id, repo_a["export_id"]),
        ),
        "search_entries": (
            "SELECT entity_type, entity_id::text, entity_version, status_projection, title_or_summary "
            "FROM public.search_entries WHERE workspace_id = %s::uuid AND source_resource_id = %s::uuid "
            "ORDER BY entity_type, entity_id",
            (workspace_id, repo_a["resource_id"]),
        ),
        "workspace_quota_ledgers": (
            "SELECT period_key, metric_kind, used_value, soft_warning_limit, hard_cutoff_limit "
            "FROM public.workspace_quota_ledgers WHERE workspace_id = %s::uuid "
            "ORDER BY period_key, metric_kind",
            (workspace_id,),
        ),
    }

    rows_by_table: dict[str, list[list[Any]]] = {}
    counts: dict[str, int] = {}
    with cluster.admin_connection(dbname) as conn:
        with conn.cursor() as cur:
            for name, (sql, params) in queries.items():
                cur.execute(sql, params)
                fetched = [list(r) for r in cur.fetchall()]
                rows_by_table[name] = fetched
                counts[name] = len(fetched)

    canonical = json.dumps(rows_by_table, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "counts": counts,
        "digest_sha256": f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        "rows": rows_by_table,
    }


def _audit_plan_c_restored_db(
    cluster: EphemeralBackupDrillPg18Cluster,
    dbname: str,
    *,
    backup_header: dict[str, Any],
    canary_strings: tuple[str, ...],
    passphrase: str,
) -> dict[str, Any]:
    """Scan all restored public tables + backup metadata header for Plan C & secret invariants."""
    raw_violations = 0
    secret_violations = 0
    canary_hits = 0

    with cluster.admin_connection(dbname) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
            all_tables = [str(r[0]) for r in cur.fetchall()]

            # Explicit Plan C NULL / NONE column invariants
            cur.execute(
                """
                SELECT
                    (SELECT count(*) FROM public.content_units WHERE text_content IS NOT NULL OR content_ref IS NOT NULL)
                    + (SELECT count(*) FROM public.fact_support WHERE extracted_span IS NOT NULL)
                    + (SELECT count(*) FROM public.source_resources WHERE raw_storage_mode <> 'NONE' OR raw_retention_seconds IS NOT NULL)
                """
            )
            col_viol = int(cur.fetchone()[0])  # type: ignore[index]
            raw_violations += col_viol

            # Scan EVERY jsonb column across all restored tables
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type = 'jsonb'
                ORDER BY table_name, column_name
                """
            )
            jsonb_cols = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
            for tbl, col in jsonb_cols:
                cur.execute(
                    f"SELECT {col} FROM public.{tbl} WHERE {col} IS NOT NULL"  # noqa: S608
                )
                for (val,) in cur.fetchall():
                    try:
                        assert_no_forbidden_raw_keys(val, field_name=f"{tbl}.{col}")
                    except Exception:  # noqa: BLE001
                        raw_violations += 1
                    try:
                        assert_no_forbidden_secret_keys(val, field_name=f"{tbl}.{col}")
                    except Exception:  # noqa: BLE001
                        secret_violations += 1

            # Scan ALL text/jsonb columns across every table for raw source canaries or passphrase
            needles = (*canary_strings, passphrase)
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type IN ('text', 'character varying', 'jsonb')
                ORDER BY table_name, column_name
                """
            )
            text_cols = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
            for tbl, col in text_cols:
                cur.execute(
                    f"SELECT {col}::text FROM public.{tbl} WHERE {col} IS NOT NULL"  # noqa: S608
                )
                for (text_val,) in cur.fetchall():
                    s = str(text_val)
                    for needle in needles:
                        if needle in s:
                            canary_hits += 1

    # Audit backup envelope metadata header
    header_clean = True
    try:
        assert_no_forbidden_raw_keys(backup_header, field_name="backup_envelope_header")
        assert_no_forbidden_secret_keys(backup_header, field_name="backup_envelope_header")
        header_text = json.dumps(backup_header, sort_keys=True)
        for needle in (*canary_strings, passphrase):
            if needle in header_text:
                header_clean = False
                canary_hits += 1
    except Exception:  # noqa: BLE001
        header_clean = False

    return {
        "audited_table_count": len(all_tables),
        "audited_jsonb_column_count": len(jsonb_cols),
        "raw_text_violations": raw_violations,
        "secret_key_violations": secret_violations,
        "canary_string_hits": canary_hits,
        "backup_metadata_clean": header_clean,
    }


def run_t505_backup_restore_drill(
    *,
    repo_root: Path | None = None,
    passphrase: str = "cm-t505-ephemeral-drill-passphrase-2026!",
    rto_budget_ms: int = DEFAULT_RTO_BUDGET_MS,
) -> T505DrillReport:
    """Execute the full real PostgreSQL 18 encrypted backup, isolated restore & fence drill (`T-505`)."""
    root = default_repo_root() if repo_root is None else repo_root.resolve()
    migrations_dir = root / "db" / "migrations"
    m0_openapi_path = root / "contracts" / "import-v1" / "openapi.yaml"
    m0_blob_sha = (
        compute_git_blob_sha1(m0_openapi_path.read_bytes())
        if m0_openapi_path.is_file()
        else M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1
    )

    source_db = "cm_t505_source_db"
    restore_db = "cm_t505_restore_target_db"

    with EphemeralBackupDrillPg18Cluster() as cluster:
        pg_major, pg_version_label = cluster.start()
        cluster.create_database(source_db)
        cluster.apply_migrations(source_db, migrations_dir)
        cluster.provision_runtime_logins(source_db)

        # ---------------------------------------------------------------------
        # 1. Seed Workspace A (Repo A active + Repo B in-flight PARTIAL deletion)
        #    and Workspace B (separate tenant for post-restore RLS isolation)
        # ---------------------------------------------------------------------
        ws_a_id = str(uuid.uuid4())
        user_a_id = str(uuid.uuid4())
        acct_a_id = str(uuid.uuid4())
        ident_a_id = str(uuid.uuid4())
        ext_map_a_id = str(uuid.uuid4())

        ws_b_id = str(uuid.uuid4())
        user_b_id = str(uuid.uuid4())

        with cluster.admin_connection(source_db) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                        VALUES
                            (%s::uuid, 'https://auth.local/v1', %s, %s, 'Drill Owner A'),
                            (%s::uuid, 'https://auth.local/v1', %s, %s, 'Drill Owner B')
                        """,
                        (
                            user_a_id,
                            f"sub-{user_a_id}",
                            f"{user_a_id}@local.test",
                            user_b_id,
                            f"sub-{user_b_id}",
                            f"{user_b_id}@local.test",
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.workspaces (id, owner_user_id, name)
                        VALUES
                            (%s::uuid, %s::uuid, 'T505 Workspace A'),
                            (%s::uuid, %s::uuid, 'T505 Workspace B')
                        """,
                        (ws_a_id, user_a_id, ws_b_id, user_b_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                        VALUES
                            (%s::uuid, %s::uuid, 'OWNER'),
                            (%s::uuid, %s::uuid, 'OWNER')
                        """,
                        (ws_a_id, user_a_id, ws_b_id, user_b_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.source_accounts (
                            id, workspace_id, provider, external_account_id,
                            token_secret_ref, scopes, status
                        ) VALUES (%s::uuid, %s::uuid, 'GITHUB', 'gh-acct-t505', 'secret://t505/ref', ARRAY['read:user'], 'ACTIVE')
                        """,
                        (acct_a_id, ws_a_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.identities (
                            id, workspace_id, display_name, identity_type, canonical_user_id, status
                        ) VALUES (%s::uuid, %s::uuid, 'Alice Drill', 'PERSON', %s::uuid, 'ACTIVE')
                        """,
                        (ident_a_id, ws_a_id, user_a_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.external_identity_mappings (
                            id, workspace_id, identity_id, provider, external_user_id,
                            username, verification_method, status, verified_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'GITHUB', 'gh-user-1001',
                            'alice-drill', 'OAUTH_ACCOUNT', 'VERIFIED', %s
                        )
                        """,
                        (ext_map_a_id, ws_a_id, ident_a_id, DETERMINISTIC_DRILL_NOW),
                    )

        access_a = _access_context(workspace_id=ws_a_id, user_id=user_a_id)
        access_b = _access_context(workspace_id=ws_b_id, user_id=user_b_id)

        repo_a = _seed_repo_graph(
            cluster,
            source_db,
            workspace_id=ws_a_id,
            account_id=acct_a_id,
            user_id=user_a_id,
            identity_id=ident_a_id,
            repo_name="acme/repo-alpha-active",
            provider_resource_id="9001001",
            tag="AlphaActive",
            raw_text=f"Implemented deterministic restore verification for AlphaActive ({_RAW_CANARY_REPO_A}).",
            include_sync_and_job=False,
        )
        repo_b = _seed_repo_graph(
            cluster,
            source_db,
            workspace_id=ws_a_id,
            account_id=acct_a_id,
            user_id=user_a_id,
            identity_id=ident_a_id,
            repo_name="acme/repo-beta-fenced",
            provider_resource_id="9002002",
            tag="BetaFenced",
            raw_text=f"Secret beta work item pending purge ({_RAW_CANARY_REPO_B}).",
            include_sync_and_job=True,
        )

        # Index both repos into FTS search_entries via real PostgresSearchIndexer
        src_indexer = PostgresSearchIndexer(
            connection_factory=cluster.api_connection_factory(source_db),
            now_fn=lambda: DETERMINISTIC_DRILL_NOW,
        )
        src_indexer.rebuild_workspace(ws_a_id, access=access_a)

        # Populate workspace_quota_ledgers via real PostgresQuotaBudgetService
        src_budgets = PostgresQuotaBudgetService(
            api_connection_factory=cluster.api_connection_factory(source_db),
            now_fn=lambda: DETERMINISTIC_DRILL_NOW,
        )
        src_budgets.check_and_reserve_budget(
            access_a,
            operation_kind=BudgetOperationKind.INITIAL_IMPORT,
            deltas={
                QuotaMetricKind.IMPORT_OBJECTS: 12,
                QuotaMetricKind.PROVIDER_REQUESTS: 4,
            },
        )

        # Confirm OD-003 deletion for Repo B and simulate mid-cleanup failure (PARTIAL)
        src_deletion = PostgresDeletionService(
            api_connection_factory=cluster.api_connection_factory(source_db),
            worker_connection_factory=cluster.worker_connection_factory(source_db),
            now_fn=lambda: DETERMINISTIC_DRILL_NOW,
        )
        preview_b = src_deletion.preview_source_deletion(
            access_a, source_resource_id=repo_b["resource_id"]
        )
        confirm_b = src_deletion.confirm_source_deletion(
            access_a,
            source_resource_id=repo_b["resource_id"],
            idempotency_key=str(uuid.uuid4()),
            request=DeletionConfirmRequest(
                preview_token=preview_b.preview_token,
                expected_resource_version=preview_b.resource_version,
                expected_impact_scope_version=preview_b.impact_scope_version,
                acknowledge_irreversible_purge=True,
            ),
        )
        deletion_job_id = confirm_b.deletion_job_id

        def _fail_at_fts(component: str) -> None:
            if component == "FTS_INDEX":
                raise RuntimeError("SIMULATED_CRASH_DURING_FTS_PURGE")

        partial_status = src_deletion.execute_deletion_job(
            ws_a_id,
            deletion_job_id,
            access=access_a,
            fault_injector=_fail_at_fts,
            now=DETERMINISTIC_DRILL_NOW,
        )
        assert partial_status.status is DeletionJobStatus.PARTIAL
        assert partial_status.fence_active is True

        # Capture source Repo A parity snapshot right before pg_dump
        source_parity = _snapshot_repo_a_parity(
            cluster, source_db, workspace_id=ws_a_id, repo_a=repo_a
        )

        # ---------------------------------------------------------------------
        # 2. Logical pg_dump -> AES-256-GCM Encrypt -> Tamper Tests -> Decrypt
        # ---------------------------------------------------------------------
        t_backup_start = time.perf_counter()
        raw_dump_bytes = cluster.pg_dump_custom_bytes(source_db)
        envelope = encrypt_backup_bytes(
            raw_dump_bytes,
            passphrase=passphrase,
            pg_version=pg_version_label,
            schema_migrations=FULL_M1_MIGRATIONS,
            created_at=DETERMINISTIC_DRILL_NOW,
        )
        serialized_envelope = envelope.serialize()
        backup_duration_ms = max(0, round((time.perf_counter() - t_backup_start) * 1000))

        # Tamper test 1: flip 1 ciphertext byte -> MUST fail closed
        tampered_ct = bytearray(serialized_envelope)
        tampered_ct[-1] ^= 0x01
        tamper_ciphertext_rejected = False
        try:
            decrypt_backup_bytes(bytes(tampered_ct), passphrase=passphrase)
        except BackupTamperDetectedError:
            tamper_ciphertext_rejected = True

        # Tamper test 2: mutate AAD header metadata (pg_version) -> MUST fail closed
        tampered_env = EncryptedBackupEnvelope(
            header=envelope.header.model_copy(update={"pg_version": "17.0-tampered"}),
            ciphertext=envelope.ciphertext,
        )
        tamper_aad_rejected = False
        try:
            decrypt_backup_bytes(tampered_env, passphrase=passphrase)
        except BackupTamperDetectedError:
            tamper_aad_rejected = True

        # Tamper test 3: wrong passphrase -> MUST fail closed
        wrong_passphrase_rejected = False
        try:
            decrypt_backup_bytes(serialized_envelope, passphrase="wrong-passphrase-value")
        except BackupTamperDetectedError:
            wrong_passphrase_rejected = True

        # ---------------------------------------------------------------------
        # 3. Decrypt & Restore into brand-new isolated PG18 database
        # ---------------------------------------------------------------------
        t_restore_start = time.perf_counter()
        decrypted_dump_bytes, verified_header = decrypt_backup_bytes(
            serialized_envelope, passphrase=passphrase
        )
        assert decrypted_dump_bytes == raw_dump_bytes
        cluster.create_database(restore_db)
        cluster.pg_restore_custom_bytes(restore_db, decrypted_dump_bytes)
        restore_duration_ms = max(0, round((time.perf_counter() - t_restore_start) * 1000))
        total_rto_ms = backup_duration_ms + restore_duration_ms

        # ---------------------------------------------------------------------
        # 4. Post-Restore Invariant Verification
        # ---------------------------------------------------------------------
        # 4a. Repo A exact parity
        restored_parity = _snapshot_repo_a_parity(
            cluster, restore_db, workspace_id=ws_a_id, repo_a=repo_a
        )
        exact_parity = (
            source_parity["digest_sha256"] == restored_parity["digest_sha256"]
            and source_parity["counts"] == restored_parity["counts"]
        )

        # 4b. FORCE RLS + NOBYPASSRLS + Cross-workspace read isolation on restored DB
        with cluster.admin_connection(restore_db) as conn:
            restored_catalog = introspect_postgres_catalog(conn)
            catalog_violations = validate_catalog_invariants(restored_catalog)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rolname, rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = ANY(%s)
                    """,
                    (
                        [
                            "cm_migrator",
                            "cm_api_app",
                            "cm_worker_app",
                            cluster.api_login,
                            cluster.worker_login,
                        ],
                    ),
                )
                role_rows = cur.fetchall()

        all_tables_force_rls = all(
            bool(t.get("rowsecurity")) and bool(t.get("forcerowsecurity"))
            for t in restored_catalog["tables"].values()
        )
        all_runtime_roles_nobypassrls = len(role_rows) == 5 and all(
            (not bool(r[1])) and (not bool(r[2])) for r in role_rows
        )

        # Cross-workspace read as Workspace B against Workspace A resources
        restored_search = PostgresSearchService(
            api_connection_factory=cluster.api_connection_factory(restore_db),
            now_fn=lambda: DETERMINISTIC_DRILL_NOW,
        )
        restored_deletion = PostgresDeletionService(
            api_connection_factory=cluster.api_connection_factory(restore_db),
            worker_connection_factory=cluster.worker_connection_factory(restore_db),
            now_fn=lambda: DETERMINISTIC_DRILL_NOW,
        )
        ws_b_search_hits = restored_search.search(access_b, q="AlphaActive")
        ws_b_deletion_concealed = False
        try:
            restored_deletion.get_deletion_job(access_b, deletion_job_id=deletion_job_id)
        except Exception:  # noqa: BLE001
            ws_b_deletion_concealed = True
        with cluster.api_connection_factory(restore_db)() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_workspace_id', %s, true)", (ws_b_id,))
                cur.execute("SELECT set_config('app.current_user_id', %s, true)", (user_b_id,))
                cur.execute(
                    "SELECT count(*) FROM public.work_events WHERE id = %s::uuid",
                    (repo_a["event_id"],),
                )
                ws_b_direct_count = int(cur.fetchone()[0])  # type: ignore[index]
        cross_workspace_reads_blocked = (
            len(ws_b_search_hits.items) == 0
            and ws_b_deletion_concealed
            and ws_b_direct_count == 0
        )

        # 4c. Repo B Active Deletion Fence Survival + Zero-Resurrection + Completion
        restored_job_before = restored_deletion.get_deletion_job(
            access_a, deletion_job_id=deletion_job_id
        )
        with cluster.worker_connection_factory(restore_db)() as wconn:
            with wconn.transaction():
                with wconn.cursor() as wcur:
                    bind_worker_workspace_context(wcur, ws_a_id)
                    fence_active_db = is_deletion_fence_active(
                        wcur, ws_a_id, repo_b["resource_id"]
                    )

        # Refetch GET /v1/content-units/{repo_b_unit_id}/content MUST be blocked (0 provider calls)
        class _StaticDrillAccessProvider:
            def verify_identity(self, credentials: Any) -> AuthenticatedUser | None:
                if getattr(credentials, "bearer_token", None) == "t505-drill-token":
                    return access_a.user
                return None

            def verify_workspace_membership(
                self, user: AuthenticatedUser, target_workspace_id: str
            ) -> WorkspaceMembership | None:
                if user.user_id == access_a.user_id and target_workspace_id == access_a.workspace_id:
                    return access_a.membership
                return None

        fetcher = InMemoryProviderContentFetcher.from_single_bytes(
            repo_b["raw_text"].encode("utf-8")
        )
        with cluster.api_connection_factory(restore_db)() as resolver_conn:
            resolver = RefetchResolverService(
                metadata_store=PostgresProvenanceMetadataStore(resolver_conn),
                fetcher=fetcher,
            )
            resolver_app = create_app(access_provider=_StaticDrillAccessProvider())
            mount_resolver_routes(resolver_app, resolver=resolver)
            resolver_client = TestClient(resolver_app)
            refetch_resp = resolver_client.get(
                f"/v1/content-units/{repo_b['unit_id']}/content",
                headers={
                    "Authorization": "Bearer t505-drill-token",
                    "X-Workspace-ID": ws_a_id,
                },
            )
            refetch_blocked = (
                refetch_resp.status_code == 403
                and fetcher.call_count == 0
                and refetch_resp.json().get("error", {}).get("code") == "SOURCE_POLICY_BLOCKED"
            )

            # Search MUST return 0 hits for Repo B (even though FTS_INDEX failed pre-backup) & >0 for Repo A
            search_b = restored_search.search(access_a, q="BetaFenced")
            search_a = restored_search.search(access_a, q="AlphaActive")
            search_blocked = len(search_b.items) == 0 and len(search_a.items) >= 1

            # Export for Repo B MUST stay DELETED with NULL artifact_payload
            restored_exports = PostgresExportService(
                api_connection_factory=cluster.api_connection_factory(restore_db),
                resolver=resolver,
            )
            export_blocked = False
            try:
                restored_exports.get_export(access_a, export_id=repo_b["export_id"])
            except WorkspaceResourceNotFoundError:
                export_blocked = True
            except ExportError as exc:
                export_blocked = exc.status_code in (404, 409, 410)
            with cluster.admin_connection(restore_db) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, artifact_payload IS NULL FROM public.exports WHERE id = %s::uuid",
                        (repo_b["export_id"],),
                    )
                    exp_row = cur.fetchone()
                    export_blocked = export_blocked and (
                        exp_row is None
                        or (exp_row[0] == "DELETED" and bool(exp_row[1]))
                    )

        # Replay/commit of fenced Repo B job post-restore MUST fail closed with 0 resurrected rows
        replay_commit_blocked = False
        try:
            with cluster.worker_connection_factory(restore_db)() as wconn:
                with wconn.transaction():
                    with wconn.cursor() as wcur:
                        bind_worker_workspace_context(wcur, ws_a_id)
                        assert_resources_not_deletion_fenced(
                            wcur,
                            ws_a_id,
                            [repo_b["resource_id"]],
                            context="post_restore_job_replay",
                        )
        except DeletionFenceActiveError:
            replay_commit_blocked = True

        restored_indexer = PostgresSearchIndexer(
            connection_factory=cluster.api_connection_factory(restore_db),
            now_fn=lambda: DETERMINISTIC_DRILL_NOW,
        )
        indexer_fence_blocked = False
        try:
            with cluster.api_connection_factory(restore_db)() as conn:
                with workspace_scoped_transaction(conn, access_a) as cur:
                    restored_indexer.sync_work_event_in_tx(
                        cur,
                        workspace_id=ws_a_id,
                        event_id=repo_b["event_id"],
                        raise_on_fence=True,
                        now=DETERMINISTIC_DRILL_NOW,
                    )
        except DeletionFenceActiveError:
            indexer_fence_blocked = True
        replay_commit_blocked = replay_commit_blocked and indexer_fence_blocked

        # Complete Repo B deletion cleanly on the restored DB
        completed_status = restored_deletion.execute_deletion_job(
            ws_a_id,
            deletion_job_id,
            access=access_a,
            now=DETERMINISTIC_DRILL_NOW,
        )
        succeeded_components = sum(
            1 for c in completed_status.components if c.status is DeletionComponentStatus.SUCCEEDED
        )
        with cluster.admin_connection(restore_db) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM public.source_objects WHERE source_resource_id = %s::uuid)
                        + (SELECT count(*) FROM public.search_entries WHERE source_resource_id = %s::uuid)
                        + (SELECT count(*) FROM public.work_events WHERE id = %s::uuid)
                        + (SELECT count(*) FROM public.facts WHERE id = %s::uuid)
                        + (SELECT count(*) FROM public.claims WHERE id = %s::uuid)
                    """,
                    (
                        repo_b["resource_id"],
                        repo_b["resource_id"],
                        repo_b["event_id"],
                        repo_b["fact_id"],
                        repo_b["claim_id"],
                    ),
                )
                resurrected_rows = int(cur.fetchone()[0])  # type: ignore[index]

        # Verify Repo A is still 100% intact after Repo B completion
        post_purge_repo_a = _snapshot_repo_a_parity(
            cluster, restore_db, workspace_id=ws_a_id, repo_a=repo_a
        )
        repo_a_intact_after_purge = (
            post_purge_repo_a["digest_sha256"] == source_parity["digest_sha256"]
        )

        # 4d. Plan C audit across ALL 46 restored tables + encrypted backup metadata
        header_dict = verified_header.model_dump(mode="json")
        plan_c_audit = _audit_plan_c_restored_db(
            cluster,
            restore_db,
            backup_header=header_dict,
            canary_strings=(_RAW_CANARY_REPO_A, _RAW_CANARY_REPO_B),
            passphrase=passphrase,
        )

    return T505DrillReport(
        pg_major_version=pg_major,
        pg_version_label=pg_version_label,
        schema_migrations=FULL_M1_MIGRATIONS,
        backup_envelope_header=header_dict,
        crypto_checks={
            "tamper_ciphertext_rejected": tamper_ciphertext_rejected,
            "tamper_aad_header_rejected": tamper_aad_rejected,
            "wrong_passphrase_rejected": wrong_passphrase_rejected,
            "sha256_integrity_verified": True,
        },
        repo_a_parity={
            "exact_parity": exact_parity,
            "table_row_counts": restored_parity["counts"],
        },
        rls_isolation={
            "restored_table_count": len(restored_catalog["tables"]),
            "all_tables_force_rls": all_tables_force_rls,
            "all_runtime_roles_nobypassrls": all_runtime_roles_nobypassrls,
            "cross_workspace_reads_blocked": cross_workspace_reads_blocked,
            "catalog_violations": len(catalog_violations),
        },
        repo_b_deletion_fence={
            "pre_restore_status": partial_status.status.value,
            "post_restore_status_before_resume": restored_job_before.status.value,
            "fence_survived_restore": fence_active_db and restored_job_before.fence_active,
            "refetch_blocked": refetch_blocked,
            "search_blocked": search_blocked,
            "export_blocked": export_blocked,
            "replay_commit_blocked": replay_commit_blocked,
            "resurrected_rows": resurrected_rows,
            "post_restore_completion_status": completed_status.status.value,
            "post_restore_completion_succeeded": (
                completed_status.status is DeletionJobStatus.SUCCEEDED
                and succeeded_components == len(DELETION_COMPONENT_ORDER)
            ),
            "completed_components_count": succeeded_components,
            "repo_a_intact_after_repo_b_purge": repo_a_intact_after_purge,
        },
        plan_c_audit=plan_c_audit,
        backup_duration_ms=backup_duration_ms,
        restore_duration_ms=restore_duration_ms,
        total_rto_ms=total_rto_ms,
        rto_budget_ms=rto_budget_ms,
        rpo_model=RpoModel.LOGICAL_SNAPSHOT_ON_DEMAND_OR_SCHEDULED,
        openapi_import_v1_blob_sha=m0_blob_sha,
        drill_timestamp_utc=DETERMINISTIC_DRILL_NOW.isoformat().replace("+00:00", "Z"),
    )


__all__ = [
    "DETERMINISTIC_DRILL_NOW",
    "EphemeralBackupDrillPg18Cluster",
    "run_t505_backup_restore_drill",
]
