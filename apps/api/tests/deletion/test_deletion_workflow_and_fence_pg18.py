"""Real PostgreSQL 18 integration & Barrier concurrency tests for M5 Source Deletion & Fence
(T-502 / T-502A / OD-003 / DELETE-01..07 / EXPORT-02 / Migration 0080)."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    build_span_locator,
    canonicalize_source_bytes,
)
from career_memory_api.claims import (
    ApproveClaimActionRequest,
    ClaimError,
    PostgresClaimService,
)
from career_memory_api.contracts_api.models import (
    ClaimDraftCreateRequest,
    SearchEntityType,
)
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.deletion import (
    DELETION_COMPONENT_ORDER,
    EphemeralCacheRegistry,
    PostgresDeletionService,
)
from career_memory_api.exports import (
    CreateExportRequest,
    ExportError,
    PostgresExportService,
)
from career_memory_api.facts import (
    DEFAULT_EXTRACTOR_VERSION,
    DEFAULT_VALIDATOR_VERSION,
    FactError,
    PostgresFactService,
)
from career_memory_api.initial_import.derived_units import DerivedUnitIngestionProcessor
from career_memory_api.jobs.models import DeletionFenceActiveError, DeletionJobStatus
from career_memory_api.llm_gate import (
    LlmProviderMode,
    LlmStageCode,
    SyntheticFixtureStageAdapter,
)
from career_memory_api.resolver import (
    PostgresProvenanceMetadataStore,
    RefetchResolverService,
    mount_resolver_routes,
)
from career_memory_api.resolver.models import (
    ProviderFetchOutcomeKind,
    ProviderFetchResponse,
)
from career_memory_api.resolver.service import InMemoryProviderContentFetcher
from career_memory_api.schema_contracts.enums import (
    DeletionComponent,
    DeletionComponentStatus,
    ExportFormat,
    ExportPolicy,
    FactType,
    SupportType,
    SupportValidator,
)
from career_memory_api.search import PostgresSearchIndexer, PostgresSearchService
from career_memory_api.sync_runs.models import ProcessingStatus
from tests.backfill_engine.pg_harness import (
    MIGRATIONS_0010_TO_0050,
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATION_0060 = REPO_ROOT / "db" / "migrations" / "0060_m3_event_versions_and_constraints.sql"
MIGRATION_0070 = REPO_ROOT / "db" / "migrations" / "0070_m4_fact_versions_and_exports.sql"
MIGRATION_0080 = REPO_ROOT / "db" / "migrations" / "0080_m5_search_deletion_and_budgets.sql"
MIGRATIONS_0010_TO_0080 = (
    *MIGRATIONS_0010_TO_0050,
    MIGRATION_0060,
    MIGRATION_0070,
    MIGRATION_0080,
)
BASE_NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def _access_for(seed: dict[str, str]) -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=seed["user_id"],
            auth_subject=AuthSubject(
                issuer="https://auth.local/v1",
                subject=f"sub-{seed['user_id']}",
            ),
        ),
        membership=WorkspaceMembership(
            workspace_id=seed["workspace_id"],
            user_id=seed["user_id"],
            role="OWNER",
        ),
    )


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m5-deletion-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0080:
            cluster.apply_sql_file(mig)
        # Re-apply 0080 immediately to prove idempotency
        cluster.apply_sql_file(MIGRATION_0080)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def clean_tables(pg18_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _add_repo(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    account_id: str,
    provider_resource_id: str,
    name: str,
) -> str:
    res_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
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
                    name,
                    f"https://github.com/{name}",
                ),
            )
        conn.commit()
    return res_id


def _seed_full_repo_graph(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    resource_id: str,
    user_id: str,
    identity_id: str,
    tag: str,
    raw_text: str,
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
    obj_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    unit_id = str(uuid.uuid4())
    evt_id = str(uuid.uuid4())
    fact_v1_id = str(uuid.uuid4())
    fact_v2_id = str(uuid.uuid4())
    contrib_id = str(uuid.uuid4())
    claim_draft_id = str(uuid.uuid4())
    claim_v1_sup_id = str(uuid.uuid4())
    claim_v2_app_id = str(uuid.uuid4())
    export_id = str(uuid.uuid4())
    gen_run_id = str(uuid.uuid4())
    val_run_id = str(uuid.uuid4())
    digest = "c" * 64

    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.source_objects (
                    id, workspace_id, source_resource_id, object_type, provider_object_id
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s)
                """,
                (obj_id, workspace_id, resource_id, f"pr-{tag}"),
            )
            cur.execute(
                """
                INSERT INTO public.source_object_revisions (
                    id, workspace_id, source_object_id, content_hash,
                    hash_policy_version, connector_version, schema_version, observed_at
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, 'v1', 'gh-v1', 'v1', %s)
                """,
                (rev_id, workspace_id, obj_id, doc.metadata.content_sha256, BASE_NOW),
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
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', 1, %s, 'CONFIRMED', 'USER')
                """,
                (evt_id, workspace_id, obj_id, f"DeletionEvent {tag}"),
            )
            cur.execute(
                """
                INSERT INTO public.work_event_versions (
                    id, workspace_id, event_id, version, anchor_mode,
                    primary_anchor_source_object_id, title, status, created_by
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 1, 'PR', %s::uuid, %s, 'CONFIRMED', 'USER')
                """,
                (str(uuid.uuid4()), workspace_id, evt_id, obj_id, f"DeletionEvent {tag}"),
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
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'ACTION', %s::uuid, 'built', %s, 'SUPERSEDED', 'v1', 1)
                """,
                (fact_v1_id, workspace_id, evt_id, identity_id, f"old fact {tag}"),
            )
            cur.execute(
                """
                INSERT INTO public.facts (
                    id, workspace_id, event_id, fact_type, subject_identity_id,
                    predicate, object_text, status, extractor_version, version, supersedes_fact_id
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'ACTION', %s::uuid, 'built', %s, 'SUPPORTED', 'v1', 2, %s::uuid)
                """,
                (fact_v2_id, workspace_id, evt_id, identity_id, f"DeletionFact {tag}", fact_v1_id),
            )
            cur.execute(
                """
                INSERT INTO public.fact_support (
                    id, workspace_id, fact_id, content_unit_id, support_type,
                    validator, validator_version, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', 'RULE', 'v1', 'ACTIVE')
                """,
                (str(uuid.uuid4()), workspace_id, fact_v2_id, unit_id),
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
                (str(uuid.uuid4()), workspace_id, contrib_id, fact_v2_id),
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
            # Claim 1: DRAFT (unapproved)
            cur.execute(
                """
                INSERT INTO public.claims (
                    id, workspace_id, event_id, generator_run_id, version, claim_text, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 1, %s, 'DRAFT')
                """,
                (claim_draft_id, workspace_id, evt_id, gen_run_id, f"Draft claim {tag}"),
            )
            atom_draft = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.claim_atoms (
                    id, workspace_id, claim_id, ordinal, atom_type, atom_text, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 0, 'ACTION', %s, 'SUPPORTED')
                """,
                (atom_draft, workspace_id, claim_draft_id, f"Draft claim {tag}"),
            )
            cur.execute(
                """
                INSERT INTO public.claim_atom_support (
                    id, workspace_id, claim_atom_id, fact_id, support_type, validation_run_id, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', %s::uuid, 'ACTIVE')
                """,
                (str(uuid.uuid4()), workspace_id, atom_draft, fact_v2_id, val_run_id),
            )
            # Claim 2 chain: v1 SUPERSEDED -> v2 APPROVED (edited)
            cur.execute(
                """
                INSERT INTO public.claims (
                    id, workspace_id, event_id, generator_run_id, version, claim_text, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 1, %s, 'SUPERSEDED')
                """,
                (claim_v1_sup_id, workspace_id, evt_id, gen_run_id, f"Historical claim {tag}"),
            )
            cur.execute(
                """
                INSERT INTO public.claims (
                    id, workspace_id, event_id, generator_run_id, version, claim_text,
                    status, supersedes_claim_id, last_verification_run_id, approved_at, last_verified_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid, 2, %s,
                    'APPROVED', %s::uuid, %s::uuid, %s, %s
                )
                """,
                (
                    claim_v2_app_id,
                    workspace_id,
                    evt_id,
                    gen_run_id,
                    f"DeletionClaim {tag}",
                    claim_v1_sup_id,
                    val_run_id,
                    BASE_NOW,
                    BASE_NOW,
                ),
            )
            atom_app = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.claim_atoms (
                    id, workspace_id, claim_id, ordinal, atom_type, atom_text, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 0, 'ACTION', %s, 'SUPPORTED')
                """,
                (atom_app, workspace_id, claim_v2_app_id, f"DeletionClaim {tag}"),
            )
            cur.execute(
                """
                INSERT INTO public.claim_atom_support (
                    id, workspace_id, claim_atom_id, fact_id, support_type, validation_run_id, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', %s::uuid, 'ACTIVE')
                """,
                (str(uuid.uuid4()), workspace_id, atom_app, fact_v2_id, val_run_id),
            )
            # Server Export referencing claim_v2_app_id
            cur.execute(
                """
                INSERT INTO public.exports (
                    id, workspace_id, source_resource_id, requested_by_user_id,
                    format, export_policy, status, idempotency_key,
                    input_hash, policy_version, manifest, artifact_payload, completed_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    'MARKDOWN', 'SOURCE_VERIFIED_ONLY', 'READY', %s,
                    %s, 'v1', '{"ok":true}'::jsonb, '{"markdown":"# ok"}'::jsonb, %s
                )
                """,
                (
                    export_id,
                    workspace_id,
                    resource_id,
                    user_id,
                    str(uuid.uuid4()),
                    f"sha256:{digest}",
                    BASE_NOW,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.export_claims (
                    id, workspace_id, export_id, claim_id, claim_version,
                    computed_verification_at_export, ordinal
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 2, 'SOURCE_VERIFIED', 0)
                """,
                (str(uuid.uuid4()), workspace_id, export_id, claim_v2_app_id),
            )
        conn.commit()
    return {
        "obj_id": obj_id,
        "rev_id": rev_id,
        "unit_id": unit_id,
        "event_id": evt_id,
        "fact_v1_id": fact_v1_id,
        "fact_v2_id": fact_v2_id,
        "contrib_id": contrib_id,
        "claim_draft_id": claim_draft_id,
        "claim_v1_sup_id": claim_v1_sup_id,
        "claim_v2_app_id": claim_v2_app_id,
        "export_id": export_id,
        "val_run_id": val_run_id,
        "raw_text": raw_text,
        "rev_hash": doc.metadata.content_sha256,
    }


def _seed_identity(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    user_id: str,
    ext_id: str = "gh-del-owner",
) -> str:
    ident_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.identities (
                    id, workspace_id, canonical_user_id, identity_type, display_name, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PERSON', 'Owner User', 'ACTIVE')
                """,
                (ident_id, workspace_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO public.external_identity_mappings (
                    id, workspace_id, identity_id, provider, external_user_id,
                    username, verification_method, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'GITHUB', %s, %s, 'OAUTH_ACCOUNT', 'VERIFIED')
                """,
                (str(uuid.uuid4()), workspace_id, ident_id, ext_id, ext_id),
            )
        conn.commit()
    return ident_id


def test_migration_0080_idempotency_force_rls_and_plan_c_canaries(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """Verify 0080 FORCE RLS, tenant isolation, and Plan C raw/secret JSONB CHECK constraints."""
    m5_tables = (
        "search_entries",
        "search_runs",
        "deletion_component_receipts",
        "workspace_quota_ledgers",
        "runtime_pause_controls",
    )
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                INNER JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(%s)
                """,
                (list(m5_tables),),
            )
            rows = {str(r[0]): (bool(r[1]), bool(r[2])) for r in cur.fetchall()}
    assert rows == {t: (True, True) for t in m5_tables}

    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/plan-c-a")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/plan-c-b")

    # Plan C raw-text & secret key rejection on 0080 tables
    with pg18_cluster.admin_connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.search_entries (
                        workspace_id, entity_type, entity_id, entity_version,
                        status_projection, title_or_summary, search_tokens_text,
                        index_version, projection_metadata
                    ) VALUES (
                        %s::uuid, 'WORK_EVENT', %s::uuid, 1,
                        'CONFIRMED', 'Safe Title', 'safe tokens',
                        'fts-index-v1', '{"raw_body":"forbidden"}'::jsonb
                    )
                    """,
                    (seed_a["workspace_id"], str(uuid.uuid4())),
                )
        conn.rollback()

        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.workspace_quota_ledgers (
                        workspace_id, period_key, metric_kind,
                        used_value, soft_warning_limit, hard_cutoff_limit, ledger_metadata
                    ) VALUES (
                        %s::uuid, '2026-11', 'SEARCH_QUERIES',
                        0, 10, 100, '{"access_token":"gho_secret"}'::jsonb
                    )
                    """,
                    (seed_a["workspace_id"],),
                )
        conn.rollback()

        # Insert legitimate quota ledger row in WS-A and verify WS-B sees 0 under cm_api_app FORCE RLS
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.workspace_quota_ledgers (
                    workspace_id, period_key, metric_kind,
                    used_value, soft_warning_limit, hard_cutoff_limit
                ) VALUES (%s::uuid, '2026-11', 'SEARCH_QUERIES', 5, 100, 500)
                """,
                (seed_a["workspace_id"],),
            )
        conn.commit()

    with pg18_cluster.api_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.current_workspace_id', %s, false)",
                (seed_b["workspace_id"],),
            )
            cur.execute(
                "SELECT set_config('app.current_user_id', %s, false)",
                (seed_b["user_id"],),
            )
            cur.execute("SELECT COUNT(*) FROM public.workspace_quota_ledgers")
            assert cur.fetchone() == (0,)


def test_delete_01_and_02_preview_counts_scope_drift_expiry_dedupe_and_independent_repo_preservation(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """DELETE-01 & DELETE-02: Exact 10-category preview counts, scope-drift/expiry 409 with 0 mutations,
    single-job dedupe, mixed-source claim full purge (0 retained), independent repo & account preserved."""
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/repo-delete-a")
    ws_id = seed["workspace_id"]
    repo_a = seed["source_resource_id"]
    account_id = seed["source_account_id"]
    access = _access_for(seed)
    ident_id = _seed_identity(pg18_cluster, workspace_id=ws_id, user_id=seed["user_id"])

    repo_b = _add_repo(
        pg18_cluster,
        workspace_id=ws_id,
        account_id=account_id,
        provider_resource_id="repo-keep-b",
        name="acme/repo-keep-b",
    )
    ga = _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        user_id=seed["user_id"],
        identity_id=ident_id,
        tag="Alpha",
        raw_text="Raw body for repo Alpha",
    )
    gb = _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_b,
        user_id=seed["user_id"],
        identity_id=ident_id,
        tag="Beta",
        raw_text="Raw body for repo Beta",
    )

    # Seed a MIXED-SOURCE APPROVED Claim (on Repo B event, referencing BOTH Repo A fact + Repo B fact!)
    # OD-003 requires 0 retained versions of mixed-source claims when Repo A is deleted!
    mixed_claim_id = str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.claims (
                    id, workspace_id, event_id, version, claim_text, status,
                    last_verification_run_id, approved_at, last_verified_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, 1, 'Mixed-source claim Alpha+Beta', 'APPROVED',
                    %s::uuid, %s, %s
                )
                """,
                (mixed_claim_id, ws_id, gb["event_id"], gb["val_run_id"], BASE_NOW, BASE_NOW),
            )
            atom_m1 = str(uuid.uuid4())
            atom_m2 = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.claim_atoms (
                    id, workspace_id, claim_id, ordinal, atom_type, atom_text, status
                ) VALUES
                    (%s::uuid, %s::uuid, %s::uuid, 0, 'ACTION', 'Alpha atom', 'SUPPORTED'),
                    (%s::uuid, %s::uuid, %s::uuid, 1, 'RESULT', 'Beta atom', 'SUPPORTED')
                """,
                (atom_m1, ws_id, mixed_claim_id, atom_m2, ws_id, mixed_claim_id),
            )
            cur.execute(
                """
                INSERT INTO public.claim_atom_support (
                    id, workspace_id, claim_atom_id, fact_id, support_type, validation_run_id, status
                ) VALUES
                    (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', %s::uuid, 'ACTIVE'),
                    (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', %s::uuid, 'ACTIVE')
                """,
                (
                    str(uuid.uuid4()),
                    ws_id,
                    atom_m1,
                    ga["fact_v2_id"],
                    gb["val_run_id"],
                    str(uuid.uuid4()),
                    ws_id,
                    atom_m2,
                    gb["fact_v2_id"],
                    gb["val_run_id"],
                ),
            )
        conn.commit()

    # Build FTS index & populate ephemeral cache entries for Repo A and Repo B
    PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection).rebuild_workspace(ws_id)
    cache_reg = EphemeralCacheRegistry()
    cache_reg.put(ws_id, repo_a, "k1", {"slot": 1})
    cache_reg.put(ws_id, repo_a, "k2", {"slot": 2})
    cache_reg.put(ws_id, repo_b, "kb1", {"slot": 99})

    clock = _MutableClock(BASE_NOW)
    deletion_svc = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        ephemeral_cache_registry=cache_reg,
        preview_ttl_seconds=120,
        now_fn=clock,
    )
    app = create_m1_contract_app(
        deletion_service=deletion_svc,
        resolve_access=lambda _req: access,
    )

    with TestClient(app) as client:
        prev = client.post(f"/v1/source-resources/{repo_a}/deletion-preview")
        assert prev.status_code == 200
        pdata = prev.json()
        assert pdata["non_recoverable_scopes"]["already_downloaded_exports"] is True
        assert pdata["non_recoverable_scopes"]["provider_hosted_originals"] is True
        assert pdata["impact_counts"] == {
            "work_events": 1,
            "facts": 2,  # fact_v1_superseded + fact_v2_supported
            "contributions": 1,
            "claims_draft": 1,
            "claims_edited": 1,  # claim_v2_app (version=2)
            "claims_approved": 2,  # claim_v2_app + mixed_claim
            "claims_historical_versions": 1,  # claim_v1_sup
            "search_index_entries": 4,  # Alpha event + Alpha fact + Alpha approved claim + mixed claim
            "ephemeral_cache_entries": 2,
            "server_exports": 1,
        }

        # DELETE-02a: Expired preview_token -> 409 DELETION_PREVIEW_EXPIRED & 0 rows deleted
        clock.advance(timedelta(seconds=121))
        exp_resp = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_token": pdata["preview_token"],
                "expected_resource_version": pdata["resource_version"],
                "expected_impact_scope_version": pdata["impact_scope_version"],
                "acknowledge_irreversible_purge": True,
            },
        )
        assert exp_resp.status_code == 409
        assert exp_resp.json()["error"]["code"] == "DELETION_PREVIEW_EXPIRED"

        # DELETE-02b: Scope drift between preview & confirm -> 409 DELETION_PREVIEW_SCOPE_CHANGED & 0 rows deleted
        prev2 = client.post(f"/v1/source-resources/{repo_a}/deletion-preview").json()
        # Mutate scope (add a new DRAFT claim on Repo A event!)
        extra_draft_id = str(uuid.uuid4())
        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, version, claim_text, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 1, 'Late draft causing scope drift', 'DRAFT')
                    """,
                    (extra_draft_id, ws_id, ga["event_id"]),
                )
            conn.commit()
        drift_resp = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_token": prev2["preview_token"],
                "expected_resource_version": prev2["resource_version"],
                "expected_impact_scope_version": prev2["impact_scope_version"],
                "acknowledge_irreversible_purge": True,
            },
        )
        assert drift_resp.status_code == 409
        assert drift_resp.json()["error"]["code"] == "DELETION_PREVIEW_SCOPE_CHANGED"

        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.deletion_jobs WHERE workspace_id = %s::uuid", (ws_id,))
                assert cur.fetchone() == (0,)

        # DELETE-01: Fresh preview + confirm -> 202; duplicate requests (same OR different key) -> 1 job!
        prev3 = client.post(f"/v1/source-resources/{repo_a}/deletion-preview").json()
        key_1 = str(uuid.uuid4())
        confirm_body = {
            "preview_token": prev3["preview_token"],
            "expected_resource_version": prev3["resource_version"],
            "expected_impact_scope_version": prev3["impact_scope_version"],
            "acknowledge_irreversible_purge": True,
        }
        c1 = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": key_1},
            json=confirm_body,
        )
        assert c1.status_code == 202
        job_id = c1.json()["deletion_job_id"]

        # Replay with same Idempotency-Key
        c2 = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": key_1},
            json=confirm_body,
        )
        assert c2.status_code == 202
        assert c2.json()["deletion_job_id"] == job_id

        # Same Idempotency-Key with mismatched payload -> 409 IDEMPOTENCY_CONFLICT
        c_conflict = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": key_1},
            json={**confirm_body, "expected_resource_version": "sha256:" + "0" * 64},
        )
        assert c_conflict.status_code == 409
        assert c_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        # Duplicate confirm with NEW Idempotency-Key -> still deduplicated to the SAME 1 active job!
        c3 = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json=confirm_body,
        )
        assert c3.status_code == 202
        assert c3.json()["deletion_job_id"] == job_id

    # Execute 5-component deletion worker
    final_status = deletion_svc.execute_deletion_job(
        ws_id, job_id, access=access, now=clock()
    )
    assert final_status.status is DeletionJobStatus.SUCCEEDED
    assert final_status.user_visible_state == "DELETED"
    assert [c.status for c in final_status.components] == [DeletionComponentStatus.SUCCEEDED] * 5

    # Verify Repo A + mixed claim are 100% purged while Repo B + shared source_account are 100% preserved!
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.deletion_jobs WHERE workspace_id = %s::uuid", (ws_id,))
            assert cur.fetchone() == (1,)
            cur.execute("SELECT status FROM public.source_resources WHERE id = %s::uuid", (repo_a,))
            assert cur.fetchone() == ("DELETED",)
            cur.execute("SELECT status FROM public.source_resources WHERE id = %s::uuid", (repo_b,))
            assert cur.fetchone() == ("ACTIVE",)
            cur.execute("SELECT status FROM public.source_accounts WHERE id = %s::uuid", (account_id,))
            assert cur.fetchone() == ("ACTIVE",)
            cur.execute(
                "SELECT COUNT(*) FROM public.claims WHERE id = ANY(%s::uuid[])",
                ([ga["claim_draft_id"], ga["claim_v1_sup_id"], ga["claim_v2_app_id"], extra_draft_id, mixed_claim_id],),
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT COUNT(*) FROM public.work_events WHERE id = %s::uuid",
                (ga["event_id"],),
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT COUNT(*) FROM public.work_event_versions WHERE event_id = %s::uuid",
                (ga["event_id"],),
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT COUNT(*) FROM public.facts WHERE id = ANY(%s::uuid[])",
                ([ga["fact_v1_id"], ga["fact_v2_id"]],),
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT COUNT(*) FROM public.source_objects WHERE source_resource_id = %s::uuid",
                (repo_a,),
            )
            assert cur.fetchone() == (0,)
            # Repo B artifacts remain 100% intact!
            cur.execute("SELECT COUNT(*) FROM public.work_events WHERE id = %s::uuid", (gb["event_id"],))
            assert cur.fetchone() == (1,)
            cur.execute("SELECT COUNT(*) FROM public.facts WHERE id = %s::uuid", (gb["fact_v2_id"],))
            assert cur.fetchone() == (1,)
            cur.execute("SELECT COUNT(*) FROM public.claims WHERE id = %s::uuid", (gb["claim_v2_app_id"],))
            assert cur.fetchone() == (1,)
            cur.execute("SELECT COUNT(*) FROM public.exports WHERE id = %s::uuid", (gb["export_id"],))
            assert cur.fetchone() == (1,)
    assert cache_reg.count_for_resource(ws_id, repo_a) == 0
    assert cache_reg.count_for_resource(ws_id, repo_b) == 1


def test_delete_03_and_04_zero_window_immediate_block_before_worker_runs(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """DELETE-03 & DELETE-04: Immediately after 202 deletion-confirm (BEFORE worker runs!),
    /v1/search, /v1/content-units/{id}/content, /v1/exports create & download, and claim approval
    are ALL blocked immediately."""
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/zero-window-del")
    ws_id = seed["workspace_id"]
    repo_a = seed["source_resource_id"]
    access = _access_for(seed)
    ident_id = _seed_identity(pg18_cluster, workspace_id=ws_id, user_id=seed["user_id"])

    ga = _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        user_id=seed["user_id"],
        identity_id=ident_id,
        tag="ZeroWin",
        raw_text="Raw text for zero window fence test",
    )
    PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection).rebuild_workspace(ws_id)

    cache_reg = EphemeralCacheRegistry()
    cache_reg.put(ws_id, repo_a, "ephem-1", {"v": 1})

    deletion_svc = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        ephemeral_cache_registry=cache_reg,
        now_fn=lambda: BASE_NOW,
    )
    search_svc = PostgresSearchService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=lambda: BASE_NOW,
    )
    claims_svc = PostgresClaimService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=lambda: BASE_NOW,
    )
    exports_svc = PostgresExportService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=lambda: BASE_NOW,
    )
    app = create_m1_contract_app(
        search_service=search_svc,
        deletion_service=deletion_svc,
        claims_service=claims_svc,
        exports_service=exports_svc,
        resolve_access=lambda _req: access,
    )

    with TestClient(app) as client:
        # Before confirm: search returns hits!
        before = client.get("/v1/search", params={"q": "ZeroWin"})
        assert before.status_code == 200
        assert len(before.json()["items"]) == 3

        # Confirm deletion (202) — DO NOT run execute_deletion_job!
        prev = client.post(f"/v1/source-resources/{repo_a}/deletion-preview").json()
        conf = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_token": prev["preview_token"],
                "expected_resource_version": prev["resource_version"],
                "expected_impact_scope_version": prev["impact_scope_version"],
                "acknowledge_irreversible_purge": True,
            },
        )
        assert conf.status_code == 202
        assert cache_reg.count_for_resource(ws_id, repo_a) == 0

        # 1. /v1/search immediately returns 0 items (even though search_entries still exist in DB!)
        after_search = client.get("/v1/search", params={"q": "ZeroWin"})
        assert after_search.status_code == 200
        assert after_search.json()["items"] == []

        # 2. Existing export download immediately blocked (404 DELETED, artifact_payload=NULL)!
        exp_get = client.get(f"/v1/exports/{ga['export_id']}")
        assert exp_get.status_code == 404

        # 3. New export creation immediately blocked (409 DELETION_PENDING)!
        exp_create = client.post(
            "/v1/exports",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "claim_ids": [ga["claim_v2_app_id"]],
                "source_resource_id": repo_a,
                "format": "MARKDOWN",
                "export_policy": "SOURCE_VERIFIED_ONLY",
            },
        )
        assert exp_create.status_code == 409
        assert exp_create.json()["error"]["code"] == "DELETION_PENDING"

        # 4. Claim approval immediately blocked (409 DELETION_PENDING)!
        app_resp = client.post(
            f"/v1/claims/{ga['claim_draft_id']}/actions/approve",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"expected_version": 1},
        )
        assert app_resp.status_code == 409
        assert app_resp.json()["error"]["code"] == "DELETION_PENDING"

    # 5. Ephemeral content unit viewer immediately returns 403!
    class _SingleAccessProvider:
        def verify_identity(self, credentials: Any) -> AuthenticatedUser | None:
            return access.user if credentials.bearer_token == "tok-zw" else None

        def verify_workspace_membership(
            self, user: AuthenticatedUser, target_workspace_id: str
        ) -> WorkspaceMembership | None:
            if user.user_id == access.user_id and target_workspace_id == access.workspace_id:
                return access.membership
            return None

    with pg18_cluster.api_connection() as api_conn:
        fetcher = InMemoryProviderContentFetcher(
            lambda _req: ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                raw_bytes=ga["raw_text"].encode("utf-8"),
                mime_type="text/markdown",
                provider_visibility="PUBLIC",
            )
        )
        resolver = RefetchResolverService(
            metadata_store=PostgresProvenanceMetadataStore(api_conn),
            fetcher=fetcher,
            clock=lambda: BASE_NOW,
        )
        viewer_app = create_app(access_provider=_SingleAccessProvider())
        mount_resolver_routes(viewer_app, resolver=resolver)
        with TestClient(viewer_app) as vclient:
            vresp = vclient.get(
                f"/v1/content-units/{ga['unit_id']}/content",
                headers={
                    "Authorization": "Bearer tok-zw",
                    "X-Workspace-ID": ws_id,
                },
            )
            assert vresp.status_code == 403


def test_delete_05_and_06_partial_failure_visible_status_and_idempotent_retry_convergence(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """DELETE-05 & DELETE-06: Component fault leaves PARTIAL (DELETION_PENDING) with active fence;
    GET /v1/deletion-jobs/{id} exposes per-component receipts; retry skips SUCCEEDED components
    and converges idempotently to SUCCEEDED."""
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/partial-retry-repo")
    seed_other = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/other-ws-repo")
    ws_id = seed["workspace_id"]
    repo_a = seed["source_resource_id"]
    access = _access_for(seed)
    access_other = _access_for(seed_other)
    ident_id = _seed_identity(pg18_cluster, workspace_id=ws_id, user_id=seed["user_id"])

    _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        user_id=seed["user_id"],
        identity_id=ident_id,
        tag="PartialTest",
        raw_text="Raw body for partial failure test",
    )
    PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection).rebuild_workspace(ws_id)

    deletion_svc = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        now_fn=lambda: BASE_NOW,
    )
    search_svc = PostgresSearchService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=lambda: BASE_NOW,
    )
    current_access = [access]
    app = create_m1_contract_app(
        search_service=search_svc,
        deletion_service=deletion_svc,
        resolve_access=lambda _req: current_access[0],
    )

    with TestClient(app) as client:
        prev = client.post(f"/v1/source-resources/{repo_a}/deletion-preview").json()
        conf = client.post(
            f"/v1/source-resources/{repo_a}/deletion-confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_token": prev["preview_token"],
                "expected_resource_version": prev["resource_version"],
                "expected_impact_scope_version": prev["impact_scope_version"],
                "acknowledge_irreversible_purge": True,
            },
        )
        assert conf.status_code == 202
        job_id = conf.json()["deletion_job_id"]

        # Inject fault on FTS_INDEX (3rd component)
        def _fail_fts(component: str) -> None:
            if component == DeletionComponent.FTS_INDEX.value:
                raise RuntimeError("FTS_BACKEND_TRANSIENT_TIMEOUT")

        partial = deletion_svc.execute_deletion_job(
            ws_id, job_id, access=access, fault_injector=_fail_fts, now=BASE_NOW
        )
        assert partial.status is DeletionJobStatus.PARTIAL
        assert partial.user_visible_state == "DELETION_PENDING"
        assert partial.fence_active is True
        assert partial.completed_at is None
        assert "FTS_INDEX" in partial.failure_summary["failed_components"]

        # Inspect via GET /v1/deletion-jobs/{id}
        st_resp = client.get(f"/v1/deletion-jobs/{job_id}")
        assert st_resp.status_code == 200
        assert st_resp.headers["Cache-Control"] == "private, no-store"
        st_body = st_resp.json()
        assert st_body["status"] == "PARTIAL"
        assert st_body["user_visible_state"] == "DELETION_PENDING"
        comp_map = {c["component"]: c for c in st_body["components"]}
        assert comp_map["JOBS_AND_OUTBOX"]["status"] == "SUCCEEDED"
        assert comp_map["EXPORTS"]["status"] == "SUCCEEDED"
        assert comp_map["FTS_INDEX"]["status"] == "FAILED"
        assert comp_map["FTS_INDEX"]["attempt"] == 1
        assert comp_map["EPHEMERAL_CACHE"]["status"] == "PENDING"
        assert comp_map["AUTHORITATIVE_DOMAIN"]["status"] == "PENDING"

        # While PARTIAL, search MUST remain 100% blocked!
        assert client.get("/v1/search", params={"q": "PartialTest"}).json()["items"] == []

        # Cross-workspace read of deletion job returns 404!
        current_access[0] = access_other
        assert client.get(f"/v1/deletion-jobs/{job_id}").status_code == 404
        current_access[0] = access

        # Retry without fault -> skips already SUCCEEDED components (attempt stays 1!) and converges
        succeeded = deletion_svc.execute_deletion_job(
            ws_id, job_id, access=access, now=BASE_NOW + timedelta(seconds=30)
        )
        assert succeeded.status is DeletionJobStatus.SUCCEEDED
        assert succeeded.user_visible_state == "DELETED"
        assert succeeded.completed_at is not None
        after_map = {c.component: c for c in succeeded.components}
        assert after_map[DeletionComponent.JOBS_AND_OUTBOX].attempt == 1
        assert after_map[DeletionComponent.EXPORTS].attempt == 1
        assert after_map[DeletionComponent.FTS_INDEX].attempt == 2
        assert after_map[DeletionComponent.FTS_INDEX].status is DeletionComponentStatus.SUCCEEDED
        assert after_map[DeletionComponent.EPHEMERAL_CACHE].attempt == 1
        assert after_map[DeletionComponent.AUTHORITATIVE_DOMAIN].attempt == 1

        # 3rd idempotent retry does nothing and returns identical SUCCEEDED state
        replayed = deletion_svc.execute_deletion_job(
            ws_id, job_id, access=access, now=BASE_NOW + timedelta(seconds=60)
        )
        assert replayed == succeeded


def test_delete_07_and_export_02_barrier_concurrency_zero_resurrection_across_all_workers(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """DELETE-07 & EXPORT-02: Deterministic threading.Barrier concurrency proof across
    Sync/DerivedUnits, Fact Validation, Claim Draft, Search Indexer, and Export Publish —
    confirming deletion mid-flight aborts every late commit with 0 resurrected rows!"""

    def _run_confirm_deletion(
        deletion_svc: PostgresDeletionService,
        access: WorkspaceAccessContext,
        resource_id: str,
    ) -> str:
        prev = deletion_svc.preview_source_deletion(access, source_resource_id=resource_id)
        acc = deletion_svc.confirm_source_deletion(
            access,
            source_resource_id=resource_id,
            idempotency_key=str(uuid.uuid4()),
            request=type(
                "_Req",
                (),
                {
                    "preview_token": prev.preview_token,
                    "expected_resource_version": prev.resource_version,
                    "expected_impact_scope_version": prev.impact_scope_version,
                    "acknowledge_irreversible_purge": True,
                },
            )(),
        )
        return acc.deletion_job_id

    # -------------------------------------------------------------------------
    # Race 1: DerivedUnitIngestionProcessor (Sync/Import) vs Deletion Confirm
    # -------------------------------------------------------------------------
    seed1 = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/race-derived")
    ws1, res1, run1 = seed1["workspace_id"], seed1["source_resource_id"], seed1["sync_run_id"]
    access1 = _access_for(seed1)
    raw_pr = "# PR Title\n\nImplemented resilient parser for derived units race."
    canon1 = canonicalize_source_bytes(raw_pr.encode("utf-8"))
    assert canon1.document is not None
    obj1_id, rev1_id = str(uuid.uuid4()), str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.sync_runs
                SET status = 'SUCCESS', import_ready = true, processing_status = 'NOT_STARTED'
                WHERE id = %s::uuid
                """,
                (run1,),
            )
            cur.execute(
                """
                INSERT INTO public.outbox_events (
                    workspace_id, event_type, aggregate_type, aggregate_id, dedupe_key, payload, status
                ) VALUES (%s::uuid, 'IMPORT_READY', 'SYNC_RUN', %s::uuid, %s, '{}'::jsonb, 'PENDING')
                """,
                (ws1, run1, f"import_ready:{run1}"),
            )
            cur.execute(
                """
                INSERT INTO public.source_objects (
                    id, workspace_id, source_resource_id, object_type, provider_object_id
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', '501')
                """,
                (obj1_id, ws1, res1),
            )
            cur.execute(
                """
                INSERT INTO public.source_object_revisions (
                    id, workspace_id, source_object_id, content_hash,
                    hash_policy_version, connector_version, schema_version, observed_at
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, 'v1', 'gh-v1', 'v1', %s)
                """,
                (rev1_id, ws1, obj1_id, canon1.document.metadata.content_sha256, BASE_NOW),
            )
        conn.commit()

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider_resource_id FROM public.source_resources WHERE id = %s::uuid",
                (res1,),
            )
            prov_res_id1 = str(cur.fetchone()[0])  # type: ignore[index]

    fetcher1 = InMemoryProviderContentFetcher(
        lambda _req: ProviderFetchResponse(
            kind=ProviderFetchOutcomeKind.OK,
            raw_bytes=raw_pr.encode("utf-8"),
            mime_type="text/markdown",
            provider_visibility="PUBLIC",
        )
    )
    b1_enter = threading.Barrier(2, timeout=15)
    b1_done = threading.Barrier(2, timeout=15)

    def _hook_derived(_action: str, _cur: Any) -> None:
        b1_enter.wait()
        b1_done.wait()

    proc1 = DerivedUnitIngestionProcessor(
        worker_connection_factory=pg18_cluster.worker_connection,
        content_fetcher=fetcher1,
        before_commit_hook=_hook_derived,
        clock=lambda: BASE_NOW,
    )
    del_svc1 = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        now_fn=lambda: BASE_NOW,
    )

    def _confirm_race1() -> str:
        b1_enter.wait()
        jid = _run_confirm_deletion(del_svc1, access1, res1)
        b1_done.wait()
        return jid

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_worker = pool.submit(proc1.process_import_ready_run, workspace_id=ws1, sync_run_id=run1, now=BASE_NOW)
        f_del = pool.submit(_confirm_race1)
        derived_res = f_worker.result()
        _ = f_del.result()

    assert derived_res.processing_status is ProcessingStatus.BLOCKED_POLICY
    assert derived_res.reason_code == "DELETION_FENCE_ACTIVE"
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.content_units WHERE workspace_id = %s::uuid", (ws1,))
            assert cur.fetchone() == (0,)

    # -------------------------------------------------------------------------
    # Race 2: PostgresClaimService.create_claim_draft vs Deletion Confirm
    # -------------------------------------------------------------------------
    seed2 = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/race-claim")
    ws2, res2 = seed2["workspace_id"], seed2["source_resource_id"]
    access2 = _access_for(seed2)
    ident2 = _seed_identity(pg18_cluster, workspace_id=ws2, user_id=seed2["user_id"], ext_id="gh-r2")
    g2 = _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws2,
        resource_id=res2,
        user_id=seed2["user_id"],
        identity_id=ident2,
        tag="RaceClaim",
        raw_text="Raw text for claim race",
    )
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.claims WHERE workspace_id = %s::uuid", (ws2,))
            claims_before = int(cur.fetchone()[0])  # type: ignore[index]

    b2_enter = threading.Barrier(2, timeout=15)
    b2_done = threading.Barrier(2, timeout=15)

    def _hook_claim(_action: str, _cur: Any) -> None:
        b2_enter.wait()
        b2_done.wait()

    claim_svc2 = PostgresClaimService(
        api_connection_factory=pg18_cluster.api_connection,
        before_commit_hook=_hook_claim,
        now_fn=lambda: BASE_NOW,
    )
    del_svc2 = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        now_fn=lambda: BASE_NOW,
    )

    def _confirm_race2() -> str:
        b2_enter.wait()
        jid = _run_confirm_deletion(del_svc2, access2, res2)
        b2_done.wait()
        return jid

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_claim = pool.submit(
            claim_svc2.create_claim_draft,
            access2,
            ClaimDraftCreateRequest(
                confirmed_event_id=g2["event_id"],
                input_fact_ids=[g2["fact_v2_id"]],
                claim_text="Late claim must roll back on deletion fence.",
            ),
        )
        f_del2 = pool.submit(_confirm_race2)
        with pytest.raises(ClaimError) as exc_claim:
            f_claim.result()
        jid2 = f_del2.result()
    assert exc_claim.value.code == "DELETION_PENDING"
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.claims WHERE workspace_id = %s::uuid", (ws2,))
            assert int(cur.fetchone()[0]) == claims_before  # type: ignore[index]
    # Execute deletion -> 0 claims remain
    del_svc2.execute_deletion_job(ws2, jid2, access=access2, now=BASE_NOW)
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.claims WHERE workspace_id = %s::uuid", (ws2,))
            assert cur.fetchone() == (0,)

    # -------------------------------------------------------------------------
    # Race 3: PostgresSearchIndexer.sync_entity vs Deletion Confirm
    # -------------------------------------------------------------------------
    seed3 = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/race-search")
    ws3, res3 = seed3["workspace_id"], seed3["source_resource_id"]
    access3 = _access_for(seed3)
    ident3 = _seed_identity(pg18_cluster, workspace_id=ws3, user_id=seed3["user_id"], ext_id="gh-r3")
    g3 = _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws3,
        resource_id=res3,
        user_id=seed3["user_id"],
        identity_id=ident3,
        tag="RaceSearch",
        raw_text="Raw text for search race",
    )

    b3_enter = threading.Barrier(2, timeout=15)
    b3_done = threading.Barrier(2, timeout=15)

    def _hook_search(_action: str, _cur: Any) -> None:
        b3_enter.wait()
        b3_done.wait()

    indexer3 = PostgresSearchIndexer(
        connection_factory=pg18_cluster.worker_connection,
        before_commit_hook=_hook_search,
        now_fn=lambda: BASE_NOW,
    )
    del_svc3 = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        now_fn=lambda: BASE_NOW,
    )

    def _confirm_race3() -> str:
        b3_enter.wait()
        jid = _run_confirm_deletion(del_svc3, access3, res3)
        b3_done.wait()
        return jid

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_idx = pool.submit(
            indexer3.sync_entity,
            workspace_id=ws3,
            entity_type=SearchEntityType.WORK_EVENT,
            entity_id=g3["event_id"],
            now=BASE_NOW,
        )
        f_del3 = pool.submit(_confirm_race3)
        with pytest.raises(DeletionFenceActiveError):
            f_idx.result()
        _ = f_del3.result()

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.search_entries WHERE workspace_id = %s::uuid", (ws3,))
            assert cur.fetchone() == (0,)

    # -------------------------------------------------------------------------
    # Race 4: PostgresFactService.approve_fact vs Deletion Confirm
    # -------------------------------------------------------------------------
    seed4 = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/race-fact")
    ws4, res4 = seed4["workspace_id"], seed4["source_resource_id"]
    access4 = _access_for(seed4)
    ident4 = _seed_identity(pg18_cluster, workspace_id=ws4, user_id=seed4["user_id"], ext_id="gh-r4")
    g4 = _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws4,
        resource_id=res4,
        user_id=seed4["user_id"],
        identity_id=ident4,
        tag="RaceFact",
        raw_text="Raw text for fact race",
    )
    cand_fact_id = str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.facts (
                    id, workspace_id, event_id, fact_type, subject_identity_id,
                    predicate, object_text, status, extractor_version, version
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'ACTION', %s::uuid, 'tested', 'Race fact', 'CANDIDATE', 'v1', 1)
                """,
                (cand_fact_id, ws4, g4["event_id"], ident4),
            )
        conn.commit()

    b4_enter = threading.Barrier(2, timeout=15)
    b4_done = threading.Barrier(2, timeout=15)

    def _hook_fact(_action: str, _cur: Any) -> None:
        b4_enter.wait()
        b4_done.wait()

    from career_memory_api.facts import ApproveFactRequest

    fact_svc4 = PostgresFactService(
        api_connection_factory=pg18_cluster.api_connection,
        before_commit_hook=_hook_fact,
        now_fn=lambda: BASE_NOW,
    )
    del_svc4 = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        now_fn=lambda: BASE_NOW,
    )

    def _confirm_race4() -> str:
        b4_enter.wait()
        jid = _run_confirm_deletion(del_svc4, access4, res4)
        b4_done.wait()
        return jid

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_fact = pool.submit(
            fact_svc4.approve_fact,
            access4,
            fact_id=cand_fact_id,
            idempotency_key=str(uuid.uuid4()),
            request=ApproveFactRequest(expected_version=1),
        )
        f_del4 = pool.submit(_confirm_race4)
        with pytest.raises(FactError) as exc_fact:
            f_fact.result()
        _ = f_del4.result()
    assert exc_fact.value.code == "DELETION_PENDING"
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.facts WHERE id = %s::uuid", (cand_fact_id,))
            assert cur.fetchone() == ("CANDIDATE",)

    # -------------------------------------------------------------------------
    # Race 5: PostgresExportService.create_export (EXPORT-02) vs Deletion Confirm
    # -------------------------------------------------------------------------
    seed5 = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/race-export")
    ws5, res5 = seed5["workspace_id"], seed5["source_resource_id"]
    access5 = _access_for(seed5)
    ident5 = _seed_identity(pg18_cluster, workspace_id=ws5, user_id=seed5["user_id"], ext_id="gh-r5")
    g5 = _seed_full_repo_graph(
        pg18_cluster,
        workspace_id=ws5,
        resource_id=res5,
        user_id=seed5["user_id"],
        identity_id=ident5,
        tag="RaceExport",
        raw_text="Raw text for export publish race",
    )

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider_resource_id FROM public.source_resources WHERE id = %s::uuid",
                (res5,),
            )
            prov_res_id5 = str(cur.fetchone()[0])  # type: ignore[index]

    with pg18_cluster.api_connection() as resolver_conn:
        fetcher5 = InMemoryProviderContentFetcher(
            lambda _req: ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                raw_bytes=g5["raw_text"].encode("utf-8"),
                mime_type="text/markdown",
                provider_visibility="PUBLIC",
            )
        )
        resolver5 = RefetchResolverService(
            metadata_store=PostgresProvenanceMetadataStore(resolver_conn),
            fetcher=fetcher5,
            clock=lambda: BASE_NOW,
        )
        b5_enter = threading.Barrier(2, timeout=15)
        b5_done = threading.Barrier(2, timeout=15)

        def _hook_export(_action: str, _export_id: Any) -> None:
            b5_enter.wait()
            b5_done.wait()

        exp_svc5 = PostgresExportService(
            api_connection_factory=pg18_cluster.api_connection,
            resolver=resolver5,
            before_publish_hook=_hook_export,
            now_fn=lambda: BASE_NOW,
        )
        del_svc5 = PostgresDeletionService(
            api_connection_factory=pg18_cluster.api_connection,
            worker_connection_factory=pg18_cluster.worker_connection,
            now_fn=lambda: BASE_NOW,
        )

        def _confirm_race5() -> str:
            b5_enter.wait()
            jid = _run_confirm_deletion(del_svc5, access5, res5)
            b5_done.wait()
            return jid

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_exp = pool.submit(
                exp_svc5.create_export,
                access5,
                idempotency_key=str(uuid.uuid4()),
                request=CreateExportRequest(
                    claim_ids=[g5["claim_v2_app_id"]],
                    source_resource_id=res5,
                    format=ExportFormat.MARKDOWN,
                    export_policy=ExportPolicy.SOURCE_VERIFIED_ONLY,
                ),
            )
            f_del5 = pool.submit(_confirm_race5)
            with pytest.raises(ExportError) as exc_exp:
                f_exp.result()
            jid5 = f_del5.result()

    assert exc_exp.value.code in ("DELETION_PENDING", "EXPORT_INVALIDATED")
    # Verify ZERO READY exports or non-null artifact_payload exist for ws5!
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.exports
                WHERE workspace_id = %s::uuid
                  AND (status = 'READY' OR artifact_payload IS NOT NULL)
                """,
                (ws5,),
            )
            assert cur.fetchone() == (0,)
    del_svc5.execute_deletion_job(ws5, jid5, access=access5, now=BASE_NOW)
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.exports WHERE workspace_id = %s::uuid", (ws5,))
            assert cur.fetchone() == (0,)
