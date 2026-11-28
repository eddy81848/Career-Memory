"""PostgreSQL 18 catalog introspection, ephemeral cluster management, and snapshot diffing.

Implements the T-106A database conformance layer:
- Runs Fresh (``0010 -> 0020 -> 0030 -> 0040``) and Staged Upgrade + Re-Apply Idempotency
  paths against a real ephemeral local PostgreSQL 18 cluster (always cleaned up on exit).
- Introspects ``pg_tables``, ``pg_class`` (RLS + FORCE RLS), ``pg_views``, ``pg_constraint``,
  ``pg_policies``, ``pg_indexes``, ``pg_roles``, and ``information_schema.columns``.
- Verifies zero missing FORCE RLS, zero missing Composite FKs, zero missing Plan C raw-text
  constraints, zero writable ``claims.verification`` columns, and zero Post-MVP deferred tables.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from career_memory_api.conformance.ai_schemas import render_canonical_json
from career_memory_api.schema_contracts.enums import (
    COMPOSITE_FK_SPEC,
    DEFERRED_POST_MVP_TABLES,
    MIGRATION_0050_COMPOSITE_FK_SPEC,
    MIGRATION_0060_COMPOSITE_FK_SPEC,
    MIGRATION_0070_COMPOSITE_FK_SPEC,
    MIGRATION_0080_COMPOSITE_FK_SPEC,
    READONLY_PROVENANCE_VIEWS,
)

_PG18_BIN_DIR = Path("/opt/homebrew/opt/postgresql@18/bin")

FULL_M1_MIGRATIONS: tuple[str, ...] = (
    "0010_identity_workspace_source_auth.sql",
    "0020_source_provenance_metadata.sql",
    "0030_events_facts_claims_contributions.sql",
    "0040_sync_runs_jobs_outbox_audit.sql",
    "0050_m2_backfill_observations.sql",
    "0060_m3_event_versions_and_constraints.sql",
    "0070_m4_fact_versions_and_exports.sql",
    "0080_m5_search_deletion_and_budgets.sql",
)

EXPECTED_ROLES: tuple[str, ...] = (
    "cm_migrator",
    "cm_api_app",
    "cm_worker_app",
)

# Full M1+M2+M3 Composite FK matrix across migrations 0010, 0020, 0030, 0040, 0050, and 0060.
# Format: (child_table, (child_col_1, child_col_2), parent_table, (parent_col_1, parent_col_2))
M1_REQUIRED_COMPOSITE_FKS: tuple[tuple[str, tuple[str, str], str, tuple[str, str]], ...] = (
    ("oauth_connect_states", ("workspace_id", "actor_user_id"), "workspace_memberships", ("workspace_id", "user_id")),
    *(
        (child, ("workspace_id", col), parent, ("workspace_id", "id"))
        for child, col, parent in COMPOSITE_FK_SPEC
    ),
    ("sync_runs", ("workspace_id", "source_resource_id"), "source_resources", ("workspace_id", "id")),
    ("sync_checkpoints", ("workspace_id", "sync_run_id"), "sync_runs", ("workspace_id", "id")),
    ("api_idempotency_records", ("workspace_id", "target_sync_run_id"), "sync_runs", ("workspace_id", "id")),
    ("jobs", ("workspace_id", "target_sync_run_id"), "sync_runs", ("workspace_id", "id")),
    ("jobs", ("workspace_id", "execution_parent_id"), "jobs", ("workspace_id", "id")),
    ("job_side_effect_ledger", ("workspace_id", "job_id"), "jobs", ("workspace_id", "id")),
    ("outbox_consumed_effects", ("workspace_id", "outbox_event_id"), "outbox_events", ("workspace_id", "id")),
    ("deletion_jobs", ("workspace_id", "source_resource_id"), "source_resources", ("workspace_id", "id")),
    *(
        (child, ("workspace_id", col), parent, ("workspace_id", "id"))
        for child, col, parent in MIGRATION_0050_COMPOSITE_FK_SPEC
    ),
    *(
        (child, ("workspace_id", col), parent, ("workspace_id", "id"))
        for child, col, parent in MIGRATION_0060_COMPOSITE_FK_SPEC
    ),
    *(
        (child, ("workspace_id", col), parent, ("workspace_id", "id"))
        for child, col, parent in MIGRATION_0070_COMPOSITE_FK_SPEC
    ),
    *(
        (child, ("workspace_id", col), parent, ("workspace_id", "id"))
        for child, col, parent in MIGRATION_0080_COMPOSITE_FK_SPEC
    ),
)

# Mandatory Plan C (방안 C) raw-text non-persistence CHECK constraints across 0020..0070.
REQUIRED_PLAN_C_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("source_resources", "ck_source_resources_raw_storage_mode_none"),
    ("source_resources", "ck_source_resources_raw_retention_null"),
    ("source_object_revisions", "ck_source_object_revisions_metadata_no_raw"),
    ("content_units", "ck_content_units_text_content_null"),
    ("content_units", "ck_content_units_content_ref_null"),
    ("content_units", "ck_content_units_retention_class_none"),
    ("content_units", "ck_content_units_expires_at_null"),
    ("content_units", "ck_content_units_anchor_no_raw"),
    ("external_identity_mappings", "ck_external_identity_mappings_evidence_metadata_no_raw"),
    ("work_events", "ck_work_events_anchor_scope_no_raw"),
    ("work_events", "ck_work_events_summary_metadata_no_raw"),
    ("review_actions", "ck_review_actions_metadata_no_raw"),
    ("facts", "ck_facts_normalized_value_no_raw"),
    ("facts", "ck_facts_metadata_no_raw"),
    ("fact_support", "ck_fact_support_extracted_span_null"),
    ("fact_support", "ck_fact_support_metadata_no_raw"),
    ("generation_runs", "ck_generation_runs_input_versions_no_raw"),
    ("generation_runs", "ck_generation_runs_token_usage_object_no_raw"),
    ("generation_runs", "ck_generation_runs_metadata_no_raw"),
    ("sync_runs", "ck_sync_runs_plan_spec_no_raw"),
    ("sync_checkpoints", "ck_sync_checkpoints_cursor_no_raw"),
    ("api_idempotency_records", "ck_api_idempotency_response_body_no_raw"),
    ("jobs", "ck_jobs_input_manifest_no_raw"),
    ("jobs", "ck_jobs_checkpoint_no_raw"),
    ("job_side_effect_ledger", "ck_job_side_effect_metadata_no_raw"),
    ("outbox_events", "ck_outbox_payload_no_raw"),
    ("audit_events", "ck_audit_events_metadata_no_raw"),
    ("audit_events", "ck_audit_events_metadata_no_secret"),
    ("deletion_jobs", "ck_deletion_jobs_checkpoint_no_raw"),
    ("deletion_jobs", "ck_deletion_jobs_failure_summary_no_raw"),
    ("sync_item_outcomes", "ck_sync_item_outcomes_metadata_no_raw"),
    ("sync_commit_pr_associations", "ck_sync_commit_pr_assoc_metadata_no_raw"),
    ("event_constraints", "ck_event_constraints_metadata_no_raw"),
    ("work_event_versions", "ck_work_event_versions_anchor_scope_no_raw"),
    ("work_event_versions", "ck_work_event_versions_summary_metadata_no_raw"),
    ("work_event_versions", "ck_work_event_versions_membership_snapshot_no_raw"),
    ("exports", "ck_exports_manifest_no_raw"),
    ("exports", "ck_exports_manifest_no_secret"),
    ("exports", "ck_exports_artifact_payload_no_raw"),
    ("exports", "ck_exports_artifact_payload_no_secret"),
    ("export_claims", "ck_export_claims_snapshot_no_raw"),
    ("search_entries", "ck_search_entries_metadata_no_raw"),
    ("search_entries", "ck_search_entries_metadata_no_secret"),
    ("search_runs", "ck_search_runs_metadata_no_raw"),
    ("search_runs", "ck_search_runs_metadata_no_secret"),
    ("deletion_component_receipts", "ck_deletion_component_receipts_metadata_no_raw"),
    ("deletion_component_receipts", "ck_deletion_component_receipts_metadata_no_secret"),
    ("workspace_quota_ledgers", "ck_workspace_quota_ledgers_metadata_no_raw"),
    ("workspace_quota_ledgers", "ck_workspace_quota_ledgers_metadata_no_secret"),
    ("runtime_pause_controls", "ck_runtime_pause_controls_metadata_no_raw"),
    ("runtime_pause_controls", "ck_runtime_pause_controls_metadata_no_secret"),
)

CRITICAL_PARTIAL_UNIQUE_INDEXES: tuple[str, ...] = (
    "uq_identities_active_canonical_user_per_workspace",
    "uq_ext_identity_mappings_verified_external_user",
    "uq_event_composite_memberships_active_member",
    "uq_sync_runs_one_active_per_resource",
    "uq_jobs_active_or_succeeded_logical_effect",
    "uq_outbox_events_single_import_ready_per_run",
    "uq_event_constraints_active_pair",
    "uq_deletion_jobs_single_active_per_resource",
    "uq_runtime_pause_controls_workspace_scope",
    "uq_runtime_pause_controls_resource_scope",
)


@dataclass(frozen=True, slots=True)
class DbCatalogDriftItem:
    """Single actionable PostgreSQL catalog conformance violation."""

    code: str
    object_name: str
    message: str
    authority: str


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_pg18_bin(name: str) -> str:
    """Locate a PostgreSQL 18 executable without modifying host environment."""
    candidate = _PG18_BIN_DIR / name
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(
            f"PostgreSQL 18 binary {name!r} not found in {_PG18_BIN_DIR} or PATH."
        )
    return resolved


class EphemeralConformancePg18Cluster:
    """Self-cleaning temporary PostgreSQL 18 cluster bound strictly to 127.0.0.1."""

    def __init__(self, *, prefix: str = "cm-m1-conformance-pg18-") -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_conf_admin"
        self._started = False

    def dsn(self, dbname: str) -> str:
        return f"postgresql://{self.superuser}@127.0.0.1:{self.port}/{dbname}"

    def start(self) -> None:
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
                f"max_connections = 50\n"
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
                with psycopg.connect(self.dsn("postgres"), connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SHOW server_version_num")
                        row = cur.fetchone()
                        if row is None or int(row[0]) < 180000:
                            raise RuntimeError(f"Expected PostgreSQL 18+, got {row!r}")
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("Ephemeral PostgreSQL 18 cluster failed to become ready.")

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

    def apply_sql_file(self, dbname: str, sql_path: Path) -> None:
        sql = sql_path.read_text(encoding="utf-8")
        with psycopg.connect(self.dsn(dbname), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

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

    def __enter__(self) -> EphemeralConformancePg18Cluster:
        self.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.stop()


def _parse_fk_definition(definition: str) -> dict[str, Any] | None:
    match = re.match(
        r"^FOREIGN KEY \(([^)]+)\) REFERENCES (?:public\.)?([a-zA-Z0-9_]+)\(([^)]+)\)",
        definition.strip(),
    )
    if not match:
        return None
    child_cols = [c.strip() for c in match.group(1).split(",")]
    parent_table = match.group(2).strip()
    parent_cols = [c.strip() for c in match.group(3).split(",")]
    return {
        "child_columns": child_cols,
        "parent_table": parent_table,
        "parent_columns": parent_cols,
    }


def introspect_postgres_catalog(conn: psycopg.Connection) -> dict[str, Any]:
    """Introspect live PostgreSQL 18 catalog into a deterministic JSON snapshot."""
    with conn.cursor() as cur:
        # 1. Roles
        cur.execute(
            """
            SELECT rolname, rolsuper, rolcanlogin, rolbypassrls
            FROM pg_roles
            WHERE rolname = ANY(%s)
            ORDER BY rolname
            """,
            (list(EXPECTED_ROLES),),
        )
        roles = {
            row[0]: {
                "rolsuper": bool(row[1]),
                "rolcanlogin": bool(row[2]),
                "rolbypassrls": bool(row[3]),
            }
            for row in cur.fetchall()
        }

        # 2. Public tables + RLS + FORCE RLS + owner
        cur.execute(
            """
            SELECT
                t.tablename,
                t.tableowner,
                c.relrowsecurity,
                c.relforcerowsecurity
            FROM pg_tables AS t
            INNER JOIN pg_namespace AS n
                ON n.nspname = t.schemaname
            INNER JOIN pg_class AS c
                ON c.relnamespace = n.oid
               AND c.relname = t.tablename
            WHERE t.schemaname = 'public'
            ORDER BY t.tablename
            """
        )
        tables: dict[str, dict[str, Any]] = {}
        for tablename, owner, rls, force_rls in cur.fetchall():
            tables[tablename] = {
                "owner": owner,
                "rowsecurity": bool(rls),
                "forcerowsecurity": bool(force_rls),
                "columns": [],
                "check_constraints": {},
                "foreign_keys": [],
                "policies": [],
                "indexes": [],
            }

        # 3. Columns per public table
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        )
        for table_name, column_name in cur.fetchall():
            if table_name in tables:
                tables[table_name]["columns"].append(column_name)

        # 4. CHECK & FOREIGN KEY constraints via pg_constraint
        cur.execute(
            """
            SELECT
                c.relname AS table_name,
                con.conname,
                con.contype,
                pg_get_constraintdef(con.oid) AS condef
            FROM pg_constraint AS con
            INNER JOIN pg_class AS c
                ON c.oid = con.conrelid
            INNER JOIN pg_namespace AS n
                ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND con.contype IN ('c', 'f')
            ORDER BY c.relname, con.conname
            """
        )
        for table_name, conname, contype, condef in cur.fetchall():
            if table_name not in tables:
                continue
            if contype == "c":
                tables[table_name]["check_constraints"][conname] = condef
            elif contype == "f":
                parsed_fk = _parse_fk_definition(condef)
                fk_entry: dict[str, Any] = {"name": conname}
                if parsed_fk is not None:
                    fk_entry.update(parsed_fk)
                else:
                    fk_entry["definition"] = condef
                tables[table_name]["foreign_keys"].append(fk_entry)

        # 5. Policies via pg_policies
        cur.execute(
            """
            SELECT
                tablename,
                policyname,
                cmd,
                ARRAY(SELECT unnest(roles)::text ORDER BY 1) AS sorted_roles
            FROM pg_policies
            WHERE schemaname = 'public'
            ORDER BY tablename, policyname
            """
        )
        for tablename, policyname, cmd, policy_roles in cur.fetchall():
            if tablename in tables:
                tables[tablename]["policies"].append(
                    {
                        "name": policyname,
                        "cmd": cmd,
                        "roles": list(policy_roles),
                    }
                )

        # 6. Indexes via pg_indexes
        cur.execute(
            """
            SELECT tablename, indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """
        )
        for tablename, indexname in cur.fetchall():
            if tablename in tables:
                tables[tablename]["indexes"].append(indexname)

        # 7. Views + security_invoker flag
        cur.execute(
            """
            SELECT
                v.viewname,
                COALESCE(c.reloptions, ARRAY[]::text[]) AS reloptions
            FROM pg_views AS v
            INNER JOIN pg_namespace AS n
                ON n.nspname = v.schemaname
            INNER JOIN pg_class AS c
                ON c.relnamespace = n.oid
               AND c.relname = v.viewname
            WHERE v.schemaname = 'public'
            ORDER BY v.viewname
            """
        )
        views = {
            viewname: {
                "security_invoker": "security_invoker=true" in [opt.lower() for opt in reloptions],
            }
            for viewname, reloptions in cur.fetchall()
        }

    return {
        "schema_version": "1.0.0",
        "migrations": list(FULL_M1_MIGRATIONS),
        "roles": roles,
        "views": views,
        "tables": tables,
    }


def validate_catalog_invariants(catalog: dict[str, Any]) -> list[DbCatalogDriftItem]:
    """Validate structural security, Composite FK, Plan C, and scope invariants on a catalog snapshot."""
    violations: list[DbCatalogDriftItem] = []

    roles = catalog.get("roles", {})
    for role_name in EXPECTED_ROLES:
        role_info = roles.get(role_name)
        if role_info is None:
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_DB_ROLE",
                    object_name=role_name,
                    message=f"Required least-privilege DB role {role_name!r} is missing.",
                    authority="Notion Data Model §2 / T-101",
                )
            )
            continue
        if role_info.get("rolbypassrls") or role_info.get("rolsuper") or role_info.get("rolcanlogin"):
            violations.append(
                DbCatalogDriftItem(
                    code="INSECURE_DB_ROLE_ATTRIBUTES",
                    object_name=role_name,
                    message=(
                        f"Role {role_name!r} must have NOLOGIN, NOSUPERUSER, NOBYPASSRLS; "
                        f"got {role_info!r}."
                    ),
                    authority="Notion Data Model §2 / AUTH-02",
                )
            )

    tables = catalog.get("tables", {})
    for forbidden_table in sorted(DEFERRED_POST_MVP_TABLES):
        if forbidden_table in tables:
            violations.append(
                DbCatalogDriftItem(
                    code="PROHIBITED_POST_MVP_TABLE",
                    object_name=forbidden_table,
                    message=(
                        f"Deferred Post-MVP table {forbidden_table!r} must NOT exist in M1 schema."
                    ),
                    authority="Notion Data Model §8 / OD-007",
                )
            )

    for table_name, tinfo in sorted(tables.items()):
        if not tinfo.get("rowsecurity") or not tinfo.get("forcerowsecurity"):
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_FORCE_RLS",
                    object_name=f"public.{table_name}",
                    message=(
                        f"Table public.{table_name} must enable BOTH rowsecurity=true and "
                        f"forcerowsecurity=true (got rowsecurity={tinfo.get('rowsecurity')}, "
                        f"forcerowsecurity={tinfo.get('forcerowsecurity')})."
                    ),
                    authority="Notion Data Model §2 / T-106A",
                )
            )
        if not tinfo.get("policies"):
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_RLS_POLICY",
                    object_name=f"public.{table_name}",
                    message=f"Table public.{table_name} has zero RLS policies.",
                    authority="Notion Data Model §2 / T-106A",
                )
            )

    # Composite FK check across 0010/0020/0030/0040
    for child_table, child_cols, parent_table, parent_cols in M1_REQUIRED_COMPOSITE_FKS:
        tinfo = tables.get(child_table)
        if tinfo is None:
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_TENANT_TABLE",
                    object_name=f"public.{child_table}",
                    message=f"Required tenant table public.{child_table} is missing.",
                    authority="Notion Data Model §3 / T-106A",
                )
            )
            continue
        fks = tinfo.get("foreign_keys", [])
        matched = any(
            fk.get("child_columns") == list(child_cols)
            and fk.get("parent_table") == parent_table
            and fk.get("parent_columns") == list(parent_cols)
            for fk in fks
        )
        if not matched:
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_COMPOSITE_FK",
                    object_name=f"public.{child_table}({', '.join(child_cols)})",
                    message=(
                        f"Missing Composite FK on public.{child_table}({', '.join(child_cols)}) -> "
                        f"public.{parent_table}({', '.join(parent_cols)})."
                    ),
                    authority="Notion Data Model §2-3 / T-106A",
                )
            )

    # Plan C raw-text non-persistence constraints
    for table_name, constraint_name in REQUIRED_PLAN_C_CONSTRAINTS:
        tinfo = tables.get(table_name)
        checks = tinfo.get("check_constraints", {}) if tinfo else {}
        if constraint_name not in checks:
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_PLAN_C_CONSTRAINT",
                    object_name=f"public.{table_name}.{constraint_name}",
                    message=(
                        f"Required Plan C raw-text protection CHECK constraint "
                        f"{constraint_name!r} is missing on public.{table_name}."
                    ),
                    authority="Notion 방안 C §7 / Data Model §3.4 / REF-04",
                )
            )

    # Claims must NOT have a writable verification column
    claims_info = tables.get("claims", {})
    if "verification" in claims_info.get("columns", []):
        violations.append(
            DbCatalogDriftItem(
                code="PROHIBITED_CLAIMS_VERIFICATION_COLUMN",
                object_name="public.claims.verification",
                message=(
                    "public.claims must NEVER have a writable 'verification' column; "
                    "verification is computed read-only via v_claim_verification_summary."
                ),
                authority="Notion Data Model §3.9 / T-104",
            )
        )

    # Security-invoker views
    views = catalog.get("views", {})
    for view_name in READONLY_PROVENANCE_VIEWS:
        vinfo = views.get(view_name)
        if vinfo is None or not vinfo.get("security_invoker"):
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_SECURITY_INVOKER_VIEW",
                    object_name=f"public.{view_name}",
                    message=(
                        f"Read-only provenance view public.{view_name} must exist with "
                        "security_invoker=true."
                    ),
                    authority="Notion Data Model §3.9 / T-104",
                )
            )

    # Critical partial unique indexes
    all_indexes = {
        idx
        for tinfo in tables.values()
        for idx in tinfo.get("indexes", [])
    }
    for index_name in CRITICAL_PARTIAL_UNIQUE_INDEXES:
        if index_name not in all_indexes:
            violations.append(
                DbCatalogDriftItem(
                    code="MISSING_CRITICAL_INDEX",
                    object_name=f"public.{index_name}",
                    message=f"Required partial unique index {index_name!r} is missing.",
                    authority="Notion Data Model §3 / T-105A",
                )
            )

    return violations


def diff_db_catalog_snapshots(
    expected: dict[str, Any], actual: dict[str, Any]
) -> list[DbCatalogDriftItem]:
    """Compare expected vs actual DB catalog snapshots and enforce all structural invariants."""
    violations = validate_catalog_invariants(actual)

    exp_tables = expected.get("tables", {})
    act_tables = actual.get("tables", {})
    for missing_tbl in sorted(set(exp_tables.keys()) - set(act_tables.keys())):
        violations.append(
            DbCatalogDriftItem(
                code="SNAPSHOT_MISSING_TABLE",
                object_name=f"public.{missing_tbl}",
                message=f"Expected table public.{missing_tbl} missing from live PostgreSQL catalog.",
                authority="Notion Data Model / T-106A",
            )
        )
    for extra_tbl in sorted(set(act_tables.keys()) - set(exp_tables.keys())):
        violations.append(
            DbCatalogDriftItem(
                code="SNAPSHOT_UNREGISTERED_TABLE",
                object_name=f"public.{extra_tbl}",
                message=(
                    f"Unregistered table public.{extra_tbl} found in live catalog but absent from "
                    "expected_db_catalog_snapshot.json."
                ),
                authority="Notion Data Model / T-106A",
            )
        )

    for tbl in sorted(set(exp_tables.keys()) & set(act_tables.keys())):
        exp_t = exp_tables[tbl]
        act_t = act_tables[tbl]
        if exp_t.get("columns") != act_t.get("columns"):
            violations.append(
                DbCatalogDriftItem(
                    code="SNAPSHOT_COLUMN_DRIFT",
                    object_name=f"public.{tbl}",
                    message=(
                        f"Columns drifted on public.{tbl}: expected={exp_t.get('columns')}, "
                        f"actual={act_t.get('columns')}."
                    ),
                    authority="Notion Data Model / T-106A",
                )
            )
        if exp_t.get("check_constraints") != act_t.get("check_constraints"):
            exp_ck = set(exp_t.get("check_constraints", {}).keys())
            act_ck = set(act_t.get("check_constraints", {}).keys())
            violations.append(
                DbCatalogDriftItem(
                    code="SNAPSHOT_CHECK_CONSTRAINT_DRIFT",
                    object_name=f"public.{tbl}",
                    message=(
                        f"CHECK constraints drifted on public.{tbl}: "
                        f"missing={sorted(exp_ck - act_ck)}, extra={sorted(act_ck - exp_ck)}."
                    ),
                    authority="Notion Data Model / T-106A",
                )
            )
        if exp_t.get("foreign_keys") != act_t.get("foreign_keys"):
            violations.append(
                DbCatalogDriftItem(
                    code="SNAPSHOT_FK_DRIFT",
                    object_name=f"public.{tbl}",
                    message=f"Foreign keys drifted on public.{tbl}.",
                    authority="Notion Data Model / T-106A",
                )
            )
        if exp_t.get("policies") != act_t.get("policies"):
            violations.append(
                DbCatalogDriftItem(
                    code="SNAPSHOT_POLICY_DRIFT",
                    object_name=f"public.{tbl}",
                    message=f"RLS policies drifted on public.{tbl}.",
                    authority="Notion Data Model / T-106A",
                )
            )
        if exp_t.get("indexes") != act_t.get("indexes"):
            violations.append(
                DbCatalogDriftItem(
                    code="SNAPSHOT_INDEX_DRIFT",
                    object_name=f"public.{tbl}",
                    message=f"Indexes drifted on public.{tbl}.",
                    authority="Notion Data Model / T-106A",
                )
            )

    if expected.get("views") != actual.get("views"):
        violations.append(
            DbCatalogDriftItem(
                code="SNAPSHOT_VIEW_DRIFT",
                object_name="public.views",
                message=(
                    f"Public views drifted: expected={expected.get('views')}, "
                    f"actual={actual.get('views')}."
                ),
                authority="Notion Data Model §3.9 / T-106A",
            )
        )

    return violations


def run_fresh_and_staged_pg18_catalog_smoke(
    migrations_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run BOTH Fresh (0010->0040) and Staged Upgrade + Re-Apply Idempotency on real PG18.

    Returns ``(fresh_catalog_snapshot, staged_upgrade_catalog_snapshot)``.
    Always stops and removes the temporary PostgreSQL 18 cluster in ``finally``.
    """
    migration_paths = [migrations_dir / name for name in FULL_M1_MIGRATIONS]
    for path in migration_paths:
        if not path.is_file():
            raise FileNotFoundError(f"Expected M1 migration not found: {path}")

    with EphemeralConformancePg18Cluster() as cluster:
        # Path A: Fresh sequential install 0010 -> 0020 -> 0030 -> 0040
        fresh_db = "cm_conformance_fresh"
        cluster.create_database(fresh_db)
        for path in migration_paths:
            cluster.apply_sql_file(fresh_db, path)
        with psycopg.connect(cluster.dsn(fresh_db)) as conn:
            fresh_snapshot = introspect_postgres_catalog(conn)

        # Path B: Staged upgrade (0010 -> seed -> 0020 -> seed -> 0030 -> seed -> 0040 -> seed -> re-apply 0010..0040)
        staged_db = "cm_conformance_staged"
        cluster.create_database(staged_db)

        user_id = str(uuid.uuid4())
        ws_id = str(uuid.uuid4())
        acct_id = str(uuid.uuid4())
        res_id = str(uuid.uuid4())
        obj_id = str(uuid.uuid4())
        rev_id = str(uuid.uuid4())
        unit_id = str(uuid.uuid4())
        evt_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())

        # Stage 1: 0010 + seed auth/account data
        cluster.apply_sql_file(staged_db, migration_paths[0])
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.users (id, auth_issuer, auth_subject) VALUES (%s, %s, %s)",
                    (user_id, "https://issuer.local", f"sub-{user_id}"),
                )
                cur.execute(
                    "INSERT INTO public.workspaces (id, owner_user_id, name) VALUES (%s, %s, %s)",
                    (ws_id, user_id, "Staged Upgrade Workspace"),
                )
                cur.execute(
                    "INSERT INTO public.workspace_memberships (workspace_id, user_id, role) VALUES (%s, %s, 'OWNER')",
                    (ws_id, user_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_accounts (
                        id, workspace_id, provider, external_account_id, token_secret_ref, status
                    ) VALUES (%s, %s, 'GITHUB', 'gh-staged-1', 'secret://github/staged-1', 'ACTIVE')
                    """,
                    (acct_id, ws_id),
                )
            conn.commit()

        # Stage 2: 0020 + seed source_resources / objects / revisions / content_units
        cluster.apply_sql_file(staged_db, migration_paths[1])
        digest = "a" * 64
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_resources (
                        id, workspace_id, source_account_id, provider_resource_id,
                        resource_type, name, canonical_url, visibility,
                        processing_mode, raw_storage_mode, raw_retention_seconds,
                        status, policy_version
                    ) VALUES (
                        %s, %s, %s, 'repo-staged-1',
                        'GITHUB_REPOSITORY', 'acme/staged', 'https://github.com/acme/staged', 'PUBLIC',
                        'PUBLIC_CLOUD', 'NONE', NULL,
                        'ACTIVE', 'v1'
                    )
                    """,
                    (res_id, ws_id, acct_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type, provider_object_id
                    ) VALUES (%s, %s, %s, 'PR', 'pr-1')
                    """,
                    (obj_id, ws_id, res_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, content_hash,
                        hash_policy_version, connector_version, schema_version
                    ) VALUES (%s, %s, %s, %s, 'v1', 'gh-v1', 'v1')
                    """,
                    (rev_id, ws_id, obj_id, f"sha256:{digest}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.content_units (
                        id, workspace_id, source_object_revision_id, unit_type,
                        anchor, text_content, content_ref, content_hash,
                        canonicalization_version, retention_class, expires_at, ordinal
                    ) VALUES (
                        %s, %s, %s, 'PR_TITLE',
                        '{"parser_version":"cm-unit-parser-v1"}'::jsonb,
                        NULL, NULL, %s, 'cm-canon-nfc-lf-v1', 'NONE', NULL, 0
                    )
                    """,
                    (unit_id, ws_id, rev_id, f"sha256:{digest}"),
                )
            conn.commit()

        # Stage 3: 0030 + seed work_event
        cluster.apply_sql_file(staged_db, migration_paths[2])
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode, title
                    ) VALUES (%s, %s, %s, 'PR', 'Staged Upgrade Event')
                    """,
                    (evt_id, ws_id, obj_id),
                )
            conn.commit()

        # Stage 4: 0040 + seed sync_run
        cluster.apply_sql_file(staged_db, migration_paths[3])
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.sync_runs (
                        id, workspace_id, source_resource_id, mode, status
                    ) VALUES (%s, %s, %s, 'INITIAL_BACKFILL', 'PENDING')
                    """,
                    (run_id, ws_id, res_id),
                )
            conn.commit()

        # Stage 5: 0050 + seed sync_run_page_observations
        obs_id = str(uuid.uuid4())
        cluster.apply_sql_file(staged_db, migration_paths[4])
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.sync_run_page_observations (
                        id, workspace_id, sync_run_id, stream_name,
                        page_number, request_cursor, next_cursor,
                        page_fingerprint, checkpoint_version, item_count, unique_object_count
                    ) VALUES (
                        %s, %s, %s, 'PRS',
                        1, NULL, NULL,
                        %s, 1, 1, 1
                    )
                    """,
                    (obs_id, ws_id, run_id, f"sha256:{digest}"),
                )
            conn.commit()

        # Stage 6: 0060 + seed work_event_versions & event_constraints
        evt2_id = str(uuid.uuid4())
        ver_id = str(uuid.uuid4())
        constraint_id = str(uuid.uuid4())
        left_evt_id, right_evt_id = (evt_id, evt2_id) if evt_id < evt2_id else (evt2_id, evt_id)
        cluster.apply_sql_file(staged_db, migration_paths[5])
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode, title
                    ) VALUES (%s, %s, %s, 'PR', 'Staged Upgrade Peer Event')
                    """,
                    (evt2_id, ws_id, obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.work_event_versions (
                        id, workspace_id, event_id, version, anchor_mode,
                        primary_anchor_source_object_id, title, status, created_by
                    ) VALUES (
                        %s, %s, %s, 1, 'PR',
                        %s, 'Staged Upgrade Event', 'CANDIDATE', 'RULE'
                    )
                    """,
                    (ver_id, ws_id, evt_id, obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.event_constraints (
                        id, workspace_id, left_event_id, right_event_id,
                        constraint_type, origin, status
                    ) VALUES (
                        %s, %s, %s, %s,
                        'CANNOT_LINK', 'USER', 'ACTIVE'
                    )
                    """,
                    (constraint_id, ws_id, left_evt_id, right_evt_id),
                )
            conn.commit()

        # Stage 7: 0070 + seed versioned fact, claim, export & export_claims
        fact_v1_id = str(uuid.uuid4())
        fact_v2_id = str(uuid.uuid4())
        claim_id = str(uuid.uuid4())
        export_id = str(uuid.uuid4())
        export_claim_id = str(uuid.uuid4())
        cluster.apply_sql_file(staged_db, migration_paths[6])
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, predicate,
                        object_text, status, extractor_version, version
                    ) VALUES (
                        %s, %s, %s, 'ACTION', 'implemented',
                        'Staged Fact v1', 'SUPERSEDED', 'v1', 1
                    )
                    """,
                    (fact_v1_id, ws_id, evt_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, predicate,
                        object_text, status, extractor_version, version, supersedes_fact_id
                    ) VALUES (
                        %s, %s, %s, 'ACTION', 'implemented',
                        'Staged Fact v2', 'SUPPORTED', 'v1', 2, %s
                    )
                    """,
                    (fact_v2_id, ws_id, evt_id, fact_v1_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, version, claim_text, status
                    ) VALUES (
                        %s, %s, %s, 1, 'Staged Approved Claim', 'APPROVED'
                    )
                    """,
                    (claim_id, ws_id, evt_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.exports (
                        id, workspace_id, source_resource_id, requested_by_user_id,
                        format, export_policy, status, idempotency_key, input_hash, policy_version
                    ) VALUES (
                        %s, %s, %s, %s,
                        'MARKDOWN', 'SOURCE_VERIFIED_ONLY', 'READY', %s, %s, 'v1'
                    )
                    """,
                    (
                        export_id,
                        ws_id,
                        res_id,
                        user_id,
                        str(uuid.uuid4()),
                        f"sha256:{digest}",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.export_claims (
                        id, workspace_id, export_id, claim_id, claim_version,
                        computed_verification_at_export, ordinal
                    ) VALUES (
                        %s, %s, %s, %s, 1, 'SOURCE_VERIFIED', 0
                    )
                    """,
                    (export_claim_id, ws_id, export_id, claim_id),
                )
            conn.commit()

        # Stage 8: 0080 + seed search_entries, search_runs, deletion_component_receipts, quotas & pause controls
        search_entry_id = str(uuid.uuid4())
        search_run_id = str(uuid.uuid4())
        del_job_id = str(uuid.uuid4())
        receipt_id = str(uuid.uuid4())
        quota_id = str(uuid.uuid4())
        pause_id = str(uuid.uuid4())
        cluster.apply_sql_file(staged_db, migration_paths[7])
        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.search_entries (
                        id, workspace_id, source_resource_id, entity_type, entity_id,
                        entity_version, status_projection, title_or_summary,
                        search_tokens_text, index_version
                    ) VALUES (
                        %s, %s, %s, 'WORK_EVENT', %s,
                        1, 'CONFIRMED', 'Staged Upgrade Event',
                        'staged upgrade event latency', 'fts-index-v1'
                    )
                    """,
                    (search_entry_id, ws_id, res_id, evt_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.search_runs (
                        id, workspace_id, source_resource_id, query_hash,
                        entity_types, index_version, ranking_version,
                        candidate_limit, page_limit, cursor_offset,
                        raw_candidate_count, lag_filtered_count, returned_count, degraded_mode
                    ) VALUES (
                        %s, %s, %s, %s,
                        ARRAY['WORK_EVENT']::text[], 'fts-index-v1', 'rank-bm25-v1',
                        200, 20, 0, 1, 0, 1, false
                    )
                    """,
                    (search_run_id, ws_id, res_id, f"sha256:{digest}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.deletion_jobs (
                        id, workspace_id, source_resource_id, status
                    ) VALUES (%s, %s, %s, 'PENDING')
                    """,
                    (del_job_id, ws_id, res_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.deletion_component_receipts (
                        id, workspace_id, deletion_job_id, component, status
                    ) VALUES (%s, %s, %s, 'FTS_INDEX', 'PENDING')
                    """,
                    (receipt_id, ws_id, del_job_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.workspace_quota_ledgers (
                        id, workspace_id, period_key, metric_kind,
                        used_value, soft_warning_limit, hard_cutoff_limit
                    ) VALUES (%s, %s, '2026-11', 'SEARCH_QUERIES', 1, 100, 500)
                    """,
                    (quota_id, ws_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.runtime_pause_controls (
                        id, workspace_id, scope_kind, source_resource_id, pause_state
                    ) VALUES (%s, %s, 'WORKSPACE', NULL, 'ACTIVE')
                    """,
                    (pause_id, ws_id),
                )
            conn.commit()

        # Stage 9: Re-apply 0010 -> ... -> 0080 (full idempotency) & verify data preservation
        for path in migration_paths:
            cluster.apply_sql_file(staged_db, path)

        with psycopg.connect(cluster.dsn(staged_db)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.users WHERE id = %s", (user_id,))
                assert cur.fetchone() == (1,)
                cur.execute("SELECT COUNT(*) FROM public.content_units WHERE id = %s", (unit_id,))
                assert cur.fetchone() == (1,)
                cur.execute("SELECT COUNT(*) FROM public.work_events WHERE id = %s", (evt_id,))
                assert cur.fetchone() == (1,)
                cur.execute("SELECT COUNT(*) FROM public.sync_runs WHERE id = %s", (run_id,))
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.sync_run_page_observations WHERE id = %s",
                    (obs_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.work_event_versions WHERE id = %s",
                    (ver_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.event_constraints WHERE id = %s",
                    (constraint_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.facts WHERE id = %s AND version = 2 AND supersedes_fact_id = %s",
                    (fact_v2_id, fact_v1_id),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.exports WHERE id = %s",
                    (export_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.export_claims WHERE id = %s",
                    (export_claim_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.search_entries WHERE id = %s",
                    (search_entry_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.search_runs WHERE id = %s",
                    (search_run_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.deletion_component_receipts WHERE id = %s",
                    (receipt_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.workspace_quota_ledgers WHERE id = %s",
                    (quota_id,),
                )
                assert cur.fetchone() == (1,)
                cur.execute(
                    "SELECT COUNT(*) FROM public.runtime_pause_controls WHERE id = %s",
                    (pause_id,),
                )
                assert cur.fetchone() == (1,)
            staged_snapshot = introspect_postgres_catalog(conn)

    return fresh_snapshot, staged_snapshot


def render_db_catalog_snapshot_json(snapshot: dict[str, Any]) -> str:
    """Serialize a PostgreSQL catalog snapshot deterministically."""
    return render_canonical_json(snapshot)


__all__ = [
    "CRITICAL_PARTIAL_UNIQUE_INDEXES",
    "DbCatalogDriftItem",
    "EXPECTED_ROLES",
    "EphemeralConformancePg18Cluster",
    "FULL_M1_MIGRATIONS",
    "M1_REQUIRED_COMPOSITE_FKS",
    "REQUIRED_PLAN_C_CONSTRAINTS",
    "diff_db_catalog_snapshots",
    "introspect_postgres_catalog",
    "render_db_catalog_snapshot_json",
    "resolve_pg18_bin",
    "run_fresh_and_staged_pg18_catalog_smoke",
    "validate_catalog_invariants",
]
