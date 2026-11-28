"""Real PostgreSQL 18 integration tests for M5 Permission-Safe FTS Search (T-501 / T-501A / SEARCH-01..05)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

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
from career_memory_api.contracts_api.models import (
    DEFAULT_SEARCH_INDEX_VERSION,
    DEFAULT_SEARCH_RANKING_VERSION,
    SearchCursorPayload,
    SearchEntityType,
    encode_search_cursor,
)
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.search import PostgresSearchIndexer, PostgresSearchService
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
FIXED_NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)


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
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m5-search-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0080:
            cluster.apply_sql_file(mig)
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
    visibility: str = "PUBLIC",
    status: str = "ACTIVE",
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
                    'GITHUB_REPOSITORY', %s, %s, %s,
                    'PUBLIC_CLOUD', 'NONE', NULL,
                    %s, 'v1'
                )
                """,
                (
                    res_id,
                    workspace_id,
                    account_id,
                    provider_resource_id,
                    name,
                    f"https://github.com/{name}",
                    visibility,
                    status,
                ),
            )
        conn.commit()
    return res_id


def _seed_source_unit(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    resource_id: str,
    provider_object_id: str,
    raw_text: str,
) -> dict[str, str]:
    canon = canonicalize_source_bytes(raw_text.encode("utf-8"))
    assert canon.status == "CANONICALIZED" and canon.document is not None
    doc = canon.document
    locator = build_span_locator(doc.text, 0, len(doc.text))
    obj_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    unit_id = str(uuid.uuid4())
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
    import json

    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.source_objects (
                    id, workspace_id, source_resource_id, object_type, provider_object_id
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s)
                """,
                (obj_id, workspace_id, resource_id, provider_object_id),
            )
            cur.execute(
                """
                INSERT INTO public.source_object_revisions (
                    id, workspace_id, source_object_id, content_hash,
                    hash_policy_version, connector_version, schema_version, observed_at
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, 'v1', 'gh-v1', 'v1', %s)
                """,
                (rev_id, workspace_id, obj_id, doc.metadata.content_sha256, FIXED_NOW),
            )
            cur.execute(
                """
                INSERT INTO public.content_units (
                    id, workspace_id, source_object_revision_id, unit_type,
                    anchor, text_content, content_ref, content_hash,
                    canonicalization_version, retention_class, expires_at, ordinal
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, 'PR_BODY_SECTION',
                    %s::jsonb, NULL, NULL, %s,
                    %s, 'NONE', NULL, 0
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
        conn.commit()
    return {"obj_id": obj_id, "rev_id": rev_id, "unit_id": unit_id}


def _seed_identity(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    user_id: str,
    display_name: str = "Alice Engineer",
    ext_id: str = "gh-user-100",
    mapping_status: str = "VERIFIED",
) -> tuple[str, str]:
    ident_id = str(uuid.uuid4())
    mapping_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.identities (
                    id, workspace_id, canonical_user_id, identity_type, display_name, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PERSON', %s, 'ACTIVE')
                """,
                (ident_id, workspace_id, user_id, display_name),
            )
            cur.execute(
                """
                INSERT INTO public.external_identity_mappings (
                    id, workspace_id, identity_id, provider, external_user_id,
                    username, verification_method, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'GITHUB', %s, %s, 'OAUTH_ACCOUNT', %s)
                """,
                (mapping_id, workspace_id, ident_id, ext_id, ext_id, mapping_status),
            )
        conn.commit()
    return ident_id, mapping_id


def _seed_event_fact_claim_graph(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    resource_id: str,
    identity_id: str,
    provider_object_id: str,
    event_title: str,
    fact_object_text: str,
    claim_text: str,
    event_status: str = "CONFIRMED",
    fact_status: str = "SUPPORTED",
    claim_status: str = "APPROVED",
    atom_status: str = "SUPPORTED",
) -> dict[str, str]:
    src = _seed_source_unit(
        cluster,
        workspace_id=workspace_id,
        resource_id=resource_id,
        provider_object_id=provider_object_id,
        raw_text=f"Source fixture for {provider_object_id}: {fact_object_text}",
    )
    evt_id = str(uuid.uuid4())
    fact_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    atom_id = str(uuid.uuid4())
    gen_run_id = str(uuid.uuid4())
    val_run_id = str(uuid.uuid4())

    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.work_events (
                    id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                    version, title, problem, action, outcome, status, created_by
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, 'PR',
                    1, %s, 'High latency bottleneck', 'Added bounded cache', 'Cut p95 by half',
                    %s, 'USER'
                )
                """,
                (evt_id, workspace_id, src["obj_id"], event_title, event_status),
            )
            cur.execute(
                """
                INSERT INTO public.event_memberships (
                    id, workspace_id, event_id, source_object_id,
                    relation_class, relation_type, origin, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'MUST_LINK', 'PRIMARY_ANCHOR', 'SOURCE', 'ACTIVE')
                """,
                (str(uuid.uuid4()), workspace_id, evt_id, src["obj_id"]),
            )
            cur.execute(
                """
                INSERT INTO public.facts (
                    id, workspace_id, event_id, fact_type, subject_identity_id,
                    predicate, object_text, status, extractor_version, version
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, 'ACTION', %s::uuid,
                    'implemented', %s, %s, 'fact-extractor-v1', 1
                )
                """,
                (fact_id, workspace_id, evt_id, identity_id, fact_object_text, fact_status),
            )
            cur.execute(
                """
                INSERT INTO public.fact_support (
                    id, workspace_id, fact_id, content_unit_id,
                    support_type, validator, validator_version, status
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    'DIRECT', 'RULE', 'v1', 'ACTIVE'
                )
                """,
                (str(uuid.uuid4()), workspace_id, fact_id, src["unit_id"]),
            )
            digest = "b" * 64
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
                    id, workspace_id, event_id, generator_run_id, version,
                    claim_text, status, last_verification_run_id,
                    approved_at, last_verified_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid, 1,
                    %s, %s, %s::uuid,
                    CASE WHEN %s = 'APPROVED' THEN %s ELSE NULL END, %s
                )
                """,
                (
                    claim_id,
                    workspace_id,
                    evt_id,
                    gen_run_id,
                    claim_text,
                    claim_status,
                    val_run_id,
                    claim_status,
                    FIXED_NOW,
                    FIXED_NOW,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.claim_atoms (
                    id, workspace_id, claim_id, ordinal, atom_type, atom_text, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 0, 'ACTION', %s, %s)
                """,
                (atom_id, workspace_id, claim_id, claim_text, atom_status),
            )
            cur.execute(
                """
                INSERT INTO public.claim_atom_support (
                    id, workspace_id, claim_atom_id, fact_id, support_type,
                    validation_run_id, status
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', %s::uuid, 'ACTIVE')
                """,
                (str(uuid.uuid4()), workspace_id, atom_id, fact_id, val_run_id),
            )
        conn.commit()
    return {
        **src,
        "event_id": evt_id,
        "fact_id": fact_id,
        "claim_id": claim_id,
        "atom_id": atom_id,
    }


def test_search_01_fts_filtering_composite_collapse_and_od008_scope(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """SEARCH-01 + OD-008: FTS returns CONFIRMED events (composite-collapsed), SUPPORTED/USER_CONFIRMED
    facts, and APPROVED claims; auto-selects single connected repo; rejects project_id (422)."""
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    ws_id = seed["workspace_id"]
    repo_a = seed["source_resource_id"]
    access = _access_for(seed)
    ident_id, _ = _seed_identity(pg18_cluster, workspace_id=ws_id, user_id=seed["user_id"])

    g1 = _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        identity_id=ident_id,
        provider_object_id="pr-101",
        event_title="Checkout Hypercache Optimization",
        fact_object_text="hypercache redis pipeline for checkout latency",
        claim_text="Built hypercache pipeline cutting checkout latency by 55%.",
    )

    # Seed USER_COMPOSITE collapsing two SUPERSEDED member events (single logical hit!)
    m1 = _seed_source_unit(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        provider_object_id="pr-201",
        raw_text="Composite member 1",
    )
    m2 = _seed_source_unit(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        provider_object_id="pr-202",
        raw_text="Composite member 2",
    )
    comp_id = str(uuid.uuid4())
    mem1_id = str(uuid.uuid4())
    mem2_id = str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.work_events (
                    id, workspace_id, primary_anchor_source_object_id, anchor_mode, title, status, created_by
                ) VALUES
                    (%s::uuid, %s::uuid, %s::uuid, 'PR', 'Hypercache Member Alpha', 'SUPERSEDED', 'RULE'),
                    (%s::uuid, %s::uuid, %s::uuid, 'PR', 'Hypercache Member Beta', 'SUPERSEDED', 'RULE'),
                    (%s::uuid, %s::uuid, NULL, 'USER_COMPOSITE', 'Hypercache Unified Initiative', 'CONFIRMED', 'USER')
                """,
                (
                    mem1_id,
                    ws_id,
                    m1["obj_id"],
                    mem2_id,
                    ws_id,
                    m2["obj_id"],
                    comp_id,
                    ws_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.event_composite_memberships (
                    id, workspace_id, composite_event_id, member_event_id, status
                ) VALUES
                    (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'ACTIVE'),
                    (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'ACTIVE')
                """,
                (
                    str(uuid.uuid4()),
                    ws_id,
                    comp_id,
                    mem1_id,
                    str(uuid.uuid4()),
                    ws_id,
                    comp_id,
                    mem2_id,
                ),
            )
        conn.commit()

    indexer = PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection)
    stats = indexer.rebuild_workspace(ws_id)
    assert stats.work_events_indexed == 2  # g1 event + USER_COMPOSITE (members excluded!)
    assert stats.facts_indexed == 1
    assert stats.claims_indexed == 1

    tick = 0

    def _next_now() -> datetime:
        nonlocal tick
        tick += 1
        return FIXED_NOW + timedelta(seconds=tick)

    search_service = PostgresSearchService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=_next_now,
    )
    app = create_m1_contract_app(
        search_service=search_service,
        resolve_access=lambda _req: access,
    )

    with TestClient(app) as client:
        # OD-008: project_id strictly rejected
        bad = client.get("/v1/search", params={"q": "hypercache", "project_id": "proj-forbidden"})
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "PROHIBITED_PROJECT_ID_SCOPE"

        # OD-008: single active repo auto-selected when source_resource_id omitted!
        resp = client.get("/v1/search", params={"q": "hypercache"})
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "private, no-store"
        body = resp.json()
        assert body["scope"] == {"workspace_id": ws_id, "source_resource_id": repo_a}
        hit_ids = {(item["entity_type"], item["entity_id"]) for item in body["items"]}
        assert ("WORK_EVENT", g1["event_id"]) in hit_ids
        assert ("WORK_EVENT", comp_id) in hit_ids
        assert ("WORK_EVENT", mem1_id) not in hit_ids
        assert ("WORK_EVENT", mem2_id) not in hit_ids
        assert ("FACT", g1["fact_id"]) in hit_ids
        assert ("CLAIM", g1["claim_id"]) in hit_ids

        # Filter by entity_types=CLAIM returns only CLAIM
        claims_only = client.get(
            "/v1/search",
            params={"q": "hypercache", "entity_types": ["CLAIM"]},
        )
        assert claims_only.status_code == 200
        assert [i["entity_id"] for i in claims_only.json()["items"]] == [g1["claim_id"]]

        # Add second active repo B -> omitted source_resource_id now scopes to workspace (None)
        repo_b = _add_repo(
            pg18_cluster,
            workspace_id=ws_id,
            account_id=seed["source_account_id"],
            provider_resource_id="repo-b-202",
            name="acme/repo-b",
        )
        g2 = _seed_event_fact_claim_graph(
            pg18_cluster,
            workspace_id=ws_id,
            resource_id=repo_b,
            identity_id=ident_id,
            provider_object_id="pr-301",
            event_title="Hypercache Repo B Migration",
            fact_object_text="hypercache sharding in repo b",
            claim_text="Migrated hypercache shards in repo b.",
        )
        indexer.rebuild_workspace(ws_id)

        both = client.get("/v1/search", params={"q": "hypercache"})
        assert both.status_code == 200
        assert both.json()["scope"] == {"workspace_id": ws_id, "source_resource_id": None}
        both_ids = {i["entity_id"] for i in both.json()["items"]}
        assert g1["event_id"] in both_ids and g2["event_id"] in both_ids

        # Explicit source_resource_id=repo_b isolates strictly to repo B
        only_b = client.get(
            "/v1/search",
            params={"q": "hypercache", "source_resource_id": repo_b},
        )
        assert only_b.status_code == 200
        assert only_b.json()["scope"]["source_resource_id"] == repo_b
        only_b_ids = {i["entity_id"] for i in only_b.json()["items"]}
        assert g2["event_id"] in only_b_ids
        assert g1["event_id"] not in only_b_ids


def test_search_02_excludes_rejected_stale_superseded_candidate_and_fenced_artifacts(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """SEARCH-02: REJECTED, STALE, SUPERSEDED, CANDIDATE, DRAFT, and fenced entities never appear."""
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    ws_id = seed["workspace_id"]
    repo_a = seed["source_resource_id"]
    access = _access_for(seed)
    ident_id, _ = _seed_identity(pg18_cluster, workspace_id=ws_id, user_id=seed["user_id"])

    # Seed non-searchable states on repo_a
    for idx, (ev_st, f_st, c_st) in enumerate(
        [
            ("CANDIDATE", "CANDIDATE", "DRAFT"),
            ("REJECTED", "REJECTED", "REJECTED"),
            ("STALE", "STALE", "STALE"),
            ("SUPERSEDED", "SUPERSEDED", "SUPERSEDED"),
            ("CONFIRMED", "CONFLICTED", "DRAFT_INVALID"),
        ]
    ):
        _seed_event_fact_claim_graph(
            pg18_cluster,
            workspace_id=ws_id,
            resource_id=repo_a,
            identity_id=ident_id,
            provider_object_id=f"pr-bad-{idx}",
            event_title=f"SentinelNonSearchable Event {idx}",
            fact_object_text=f"sentinelnonsearchable fact {idx}",
            claim_text=f"SentinelNonSearchable claim {idx}.",
            event_status=ev_st,
            fact_status=f_st,
            claim_status=c_st,
        )

    # Also seed a CONFIRMED/SUPPORTED/APPROVED graph on a fenced repo B
    repo_b = _add_repo(
        pg18_cluster,
        workspace_id=ws_id,
        account_id=seed["source_account_id"],
        provider_resource_id="repo-fenced-2",
        name="acme/fenced-repo",
    )
    _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_b,
        identity_id=ident_id,
        provider_object_id="pr-fenced-1",
        event_title="SentinelNonSearchable Fenced Event",
        fact_object_text="sentinelnonsearchable fenced fact",
        claim_text="SentinelNonSearchable fenced claim.",
    )
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.deletion_jobs (workspace_id, source_resource_id, status) VALUES (%s::uuid, %s::uuid, 'PENDING')",
                (ws_id, repo_b),
            )
        conn.commit()

    indexer = PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection)
    indexer.rebuild_workspace(ws_id)

    tick = 0

    def _next_now() -> datetime:
        nonlocal tick
        tick += 1
        return FIXED_NOW + timedelta(seconds=tick)

    search_service = PostgresSearchService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=_next_now,
    )
    app = create_m1_contract_app(
        search_service=search_service,
        resolve_access=lambda _req: access,
    )

    with TestClient(app) as client:
        resp = client.get("/v1/search", params={"q": "SentinelNonSearchable"})
        assert resp.status_code == 200
        # Only the 1 CONFIRMED event from idx=4 should appear; 0 facts, 0 claims, 0 fenced hits!
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["entity_type"] == "WORK_EVENT"
        assert "Event 4" in items[0]["title_or_summary"]


def test_search_03_cross_workspace_isolation_force_rls_and_revoked_membership(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """SEARCH-03: Workspace B gets identical 404 on Workspace A resource, 0 leakage, FORCE RLS enforced,
    and revoked membership is denied."""
    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/ws-a-repo")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/ws-b-repo")
    access_a = _access_for(seed_a)
    access_b = _access_for(seed_b)

    ident_a, _ = _seed_identity(
        pg18_cluster, workspace_id=seed_a["workspace_id"], user_id=seed_a["user_id"], ext_id="gh-a"
    )
    ident_b, _ = _seed_identity(
        pg18_cluster, workspace_id=seed_b["workspace_id"], user_id=seed_b["user_id"], ext_id="gh-b"
    )

    ga = _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=seed_a["workspace_id"],
        resource_id=seed_a["source_resource_id"],
        identity_id=ident_a,
        provider_object_id="pr-ws-a",
        event_title="TenantSecretAlpha Event",
        fact_object_text="tenantsecretalpha metric pipeline",
        claim_text="TenantSecretAlpha approved claim.",
    )
    gb = _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=seed_b["workspace_id"],
        resource_id=seed_b["source_resource_id"],
        identity_id=ident_b,
        provider_object_id="pr-ws-b",
        event_title="TenantSecretAlpha WorkspaceB Event",
        fact_object_text="tenantsecretalpha workspace b fact",
        claim_text="TenantSecretAlpha workspace b claim.",
    )

    indexer = PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection)
    indexer.rebuild_workspace(seed_a["workspace_id"])
    indexer.rebuild_workspace(seed_b["workspace_id"])

    tick = 0

    def _next_now() -> datetime:
        nonlocal tick
        tick += 1
        return FIXED_NOW + timedelta(seconds=tick)

    search_service = PostgresSearchService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=_next_now,
    )
    current_access = [access_b]
    app = create_m1_contract_app(
        search_service=search_service,
        resolve_access=lambda _req: current_access[0],
    )

    with TestClient(app) as client:
        # Cross-workspace resource ID returns identical 404 as nonexistent UUID
        cross_resp = client.get(
            "/v1/search",
            params={"q": "TenantSecretAlpha", "source_resource_id": seed_a["source_resource_id"]},
        )
        missing_resp = client.get(
            "/v1/search",
            params={"q": "TenantSecretAlpha", "source_resource_id": str(uuid.uuid4())},
        )
        assert cross_resp.status_code == 404
        assert missing_resp.status_code == 404
        assert cross_resp.json()["error"]["code"] == missing_resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert cross_resp.json()["error"]["message"] == missing_resp.json()["error"]["message"]

        # Broad search as Workspace B sees ONLY Workspace B hits
        b_resp = client.get("/v1/search", params={"q": "TenantSecretAlpha"})
        assert b_resp.status_code == 200
        b_ids = {i["entity_id"] for i in b_resp.json()["items"]}
        assert gb["event_id"] in b_ids
        assert ga["event_id"] not in b_ids
        assert ga["fact_id"] not in b_ids
        assert ga["claim_id"] not in b_ids

        # Direct FORCE RLS check under cm_api_app
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
                cur.execute("SELECT DISTINCT workspace_id::text FROM public.search_entries")
                assert [r[0] for r in cur.fetchall()] == [seed_b["workspace_id"]]

        # Revoke Workspace A membership -> denied with 403
        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.workspace_memberships
                    SET revoked_at = %s
                    WHERE workspace_id = %s::uuid AND user_id = %s::uuid
                    """,
                    (FIXED_NOW, seed_a["workspace_id"], seed_a["user_id"]),
                )
            conn.commit()
        current_access[0] = access_a
        revoked_resp = client.get("/v1/search", params={"q": "TenantSecretAlpha"})
        assert revoked_resp.status_code == 403


def test_search_04_metadata_only_search_runs_audit_zero_raw_query(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """SEARCH-04: public.search_runs stores query_hash and metadata only — ZERO raw query text."""
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    ws_id = seed["workspace_id"]
    access = _access_for(seed)
    ident_id, _ = _seed_identity(pg18_cluster, workspace_id=ws_id, user_id=seed["user_id"])

    canary_query = "QUANTUM_CANARY_QUERY_PHRASE_99123"
    _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=seed["source_resource_id"],
        identity_id=ident_id,
        provider_object_id="pr-canary-4",
        event_title=f"Event with {canary_query}",
        fact_object_text=f"fact matching {canary_query}",
        claim_text=f"Claim matching {canary_query}.",
    )
    PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection).rebuild_workspace(ws_id)

    search_service = PostgresSearchService(
        api_connection_factory=pg18_cluster.api_connection,
        candidate_limit=50,
        now_fn=lambda: FIXED_NOW,
    )
    resp = search_service.search(
        access,
        q=canary_query,
        source_resource_id=None,
        entity_types=[SearchEntityType.WORK_EVENT, SearchEntityType.CLAIM],
        limit=10,
        cursor=None,
    )
    assert len(resp.items) == 2

    runs = search_service.list_search_runs(access)
    assert len(runs) == 1
    run = runs[0]
    assert run.query_hash == resp.query_hash
    assert run.index_version == DEFAULT_SEARCH_INDEX_VERSION
    assert run.ranking_version == DEFAULT_SEARCH_RANKING_VERSION
    assert run.candidate_limit == 50
    assert run.page_limit == 10
    assert run.cursor_offset == 0
    assert run.raw_candidate_count == 2
    assert run.lag_filtered_count == 0
    assert run.returned_count == 2
    assert run.degraded_mode is False

    # Inspect raw DB row JSON representation: canary query string MUST NOT appear anywhere!
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT row_to_json(sr)::text FROM public.search_runs sr")
            raw_rows = [str(r[0]) for r in cur.fetchall()]
    assert len(raw_rows) == 1
    assert canary_query not in raw_rows[0]
    assert canary_query.lower() not in raw_rows[0].lower()


def test_search_05_index_lag_recheck_stale_claim_visibility_cursor_versioning_and_degraded_mode(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """SEARCH-05 / T-501A: Stale FTS rows never leak rejected/stale/identity-conflicted/private/deleted
    entities; cursor rejects cross-repo/cross-ws/version drift; degraded rebuild mode works."""
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    ws_id = seed["workspace_id"]
    repo_a = seed["source_resource_id"]
    access = _access_for(seed)
    ident_id, mapping_id = _seed_identity(
        pg18_cluster, workspace_id=ws_id, user_id=seed["user_id"], ext_id="gh-lag-1"
    )

    repo_b = _add_repo(
        pg18_cluster,
        workspace_id=ws_id,
        account_id=seed["source_account_id"],
        provider_resource_id="repo-priv-flip",
        name="acme/priv-flip",
    )

    # Graph 1 on Repo A (will have event rejected, fact made STALE -> claim becomes UNVERIFIED!)
    g_stale = _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        identity_id=ident_id,
        provider_object_id="pr-lag-1",
        event_title="LagRecheck Nebula Event",
        fact_object_text="lagrecheck nebula throughput gain",
        claim_text="LagRecheck nebula throughput improved.",
    )
    # Graph 2 on Repo B (will flip visibility to PRIVATE while leaving search_entries stale!)
    g_priv = _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_b,
        identity_id=ident_id,
        provider_object_id="pr-lag-2",
        event_title="LagRecheck PrivateFlip Event",
        fact_object_text="lagrecheck privateflip metric",
        claim_text="LagRecheck privateflip claim.",
    )
    # Graph 3 on Repo A (stays valid for pagination & cursor versioning!)
    g_ok = _seed_event_fact_claim_graph(
        pg18_cluster,
        workspace_id=ws_id,
        resource_id=repo_a,
        identity_id=ident_id,
        provider_object_id="pr-lag-ok",
        event_title="LagRecheck Healthy Event",
        fact_object_text="lagrecheck healthy fact",
        claim_text="LagRecheck healthy approved claim.",
    )

    indexer = PostgresSearchIndexer(connection_factory=pg18_cluster.worker_connection)
    indexer.rebuild_workspace(ws_id)

    # Confirm all 9 entries exist in public.search_entries BEFORE injecting lag
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.search_entries WHERE workspace_id = %s::uuid", (ws_id,))
            assert cur.fetchone() == (9,)

            # Inject intentional FTS lag (mutate authoritative tables WITHOUT updating search_entries!):
            # 1) Reject g_stale WorkEvent
            cur.execute(
                "UPDATE public.work_events SET status = 'REJECTED' WHERE id = %s::uuid",
                (g_stale["event_id"],),
            )
            # 2) Mark g_stale Fact STALE + Claim atom STALE (Claim stays status='APPROVED' in DB to test T-501A!)
            cur.execute(
                "UPDATE public.facts SET status = 'STALE' WHERE id = %s::uuid",
                (g_stale["fact_id"],),
            )
            cur.execute(
                "UPDATE public.claim_atoms SET status = 'STALE' WHERE id = %s::uuid",
                (g_stale["atom_id"],),
            )
            # 3) Flip repo_b to PRIVATE + POLICY_REVIEW_REQUIRED
            cur.execute(
                """
                UPDATE public.source_resources
                SET visibility = 'PRIVATE', status = 'POLICY_REVIEW_REQUIRED'
                WHERE id = %s::uuid
                """,
                (repo_b,),
            )
        conn.commit()

    tick = 0

    def _next_now() -> datetime:
        nonlocal tick
        tick += 1
        return FIXED_NOW + timedelta(seconds=tick)

    search_service = PostgresSearchService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=_next_now,
    )
    app = create_m1_contract_app(
        search_service=search_service,
        resolve_access=lambda _req: access,
    )

    with TestClient(app) as client:
        # Authoritative recheck MUST filter out ALL 6 stale/rejected/private candidates!
        res = client.get("/v1/search", params={"q": "LagRecheck", "limit": 2})
        assert res.status_code == 200
        body = res.json()
        # Only g_ok (3 entities total) survived -> page 1 has 2 items + next_cursor
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None
        returned_ids_p1 = {i["entity_id"] for i in body["items"]}
        for forbidden_id in (
            g_stale["event_id"],
            g_stale["fact_id"],
            g_stale["claim_id"],
            g_priv["event_id"],
            g_priv["fact_id"],
            g_priv["claim_id"],
        ):
            assert forbidden_id not in returned_ids_p1

        # Page 2 returns remaining 1 item and next_cursor == None!
        page2 = client.get(
            "/v1/search",
            params={"q": "LagRecheck", "limit": 2, "cursor": body["next_cursor"]},
        )
        assert page2.status_code == 200
        p2_body = page2.json()
        assert len(p2_body["items"]) == 1
        assert p2_body["next_cursor"] is None
        all_ok_ids = returned_ids_p1 | {p2_body["items"][0]["entity_id"]}
        assert all_ok_ids == {g_ok["event_id"], g_ok["fact_id"], g_ok["claim_id"]}

        # Verify lag_filtered_count == 3 was logged in public.search_runs (g_stale 3 entries filtered on repo_a)
        runs = search_service.list_search_runs(access)
        assert runs[0].lag_filtered_count == 3

        # Explicit search against PRIVATE-flipped repo_b MUST return 0 items even with stale search_entries!
        priv_res = client.get(
            "/v1/search",
            params={"q": "LagRecheck", "source_resource_id": repo_b},
        )
        assert priv_res.status_code == 200
        assert priv_res.json()["items"] == []

        # Cursor binding rejections: different query, different repo, forged workspace, bumped index/ranking
        bad_q = client.get(
            "/v1/search",
            params={"q": "DifferentQuery", "cursor": body["next_cursor"]},
        )
        assert bad_q.status_code == 400 and bad_q.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"

        bad_repo = client.get(
            "/v1/search",
            params={"q": "LagRecheck", "source_resource_id": repo_b, "cursor": body["next_cursor"]},
        )
        assert bad_repo.status_code == 400 and bad_repo.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"

        forged_ws_cursor = encode_search_cursor(
            SearchCursorPayload(
                workspace_id=str(uuid.uuid4()),
                source_resource_id=body["scope"]["source_resource_id"],
                query_hash=body["query_hash"],
                index_version=DEFAULT_SEARCH_INDEX_VERSION,
                ranking_version=DEFAULT_SEARCH_RANKING_VERSION,
                offset=2,
            )
        )
        bad_ws = client.get("/v1/search", params={"q": "LagRecheck", "cursor": forged_ws_cursor})
        assert bad_ws.status_code == 400 and bad_ws.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"

        search_service.index_version = "fts-index-v2"
        bad_idx = client.get("/v1/search", params={"q": "LagRecheck", "cursor": body["next_cursor"]})
        assert bad_idx.status_code == 400 and bad_idx.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"

        # Degraded mode during index rebuild (search_entries still at v1 while service expects v2!)
        degraded = client.get("/v1/search", params={"q": "LagRecheck"})
        assert degraded.status_code == 200
        deg_ids = {i["entity_id"] for i in degraded.json()["items"]}
        assert deg_ids == {g_ok["event_id"], g_ok["fact_id"], g_ok["claim_id"]}
        latest_run = search_service.list_search_runs(access)[0]
        assert latest_run.degraded_mode is True

        # Now conflict identity mapping while leaving search_entries stale -> g_ok Fact & Claim filtered!
        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.external_identity_mappings SET status = 'CONFLICTED' WHERE id = %s::uuid",
                    (mapping_id,),
                )
            conn.commit()
        after_ident = client.get("/v1/search", params={"q": "LagRecheck"})
        assert after_ident.status_code == 200
        after_ids = {i["entity_id"] for i in after_ident.json()["items"]}
        assert g_ok["fact_id"] not in after_ids
        assert g_ok["claim_id"] not in after_ids
        assert g_ok["event_id"] in after_ids
