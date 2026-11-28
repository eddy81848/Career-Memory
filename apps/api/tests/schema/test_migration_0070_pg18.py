"""Real PostgreSQL 18 integration tests for Migration 0070 (facts versioning + exports + RLS + Plan C)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from career_memory_api.schema_contracts.enums import (
    ALL_TENANT_DOMAIN_TABLES,
    MIGRATION_0070_COMPOSITE_FK_SPEC,
    MIGRATION_0070_TABLES,
)
from tests.backfill_engine.pg_harness import (
    MIGRATIONS_0010_TO_0050,
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATION_0060 = REPO_ROOT / "db" / "migrations" / "0060_m3_event_versions_and_constraints.sql"
MIGRATION_0070 = REPO_ROOT / "db" / "migrations" / "0070_m4_fact_versions_and_exports.sql"
MIGRATIONS_0010_TO_0070 = (*MIGRATIONS_0010_TO_0050, MIGRATION_0060, MIGRATION_0070)


def _sha256(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m4-0070-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0070:
            cluster.apply_sql_file(mig)
        # Re-apply 0070 a second time to verify full idempotency
        cluster.apply_sql_file(MIGRATION_0070)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def clean_tables(pg18_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _seed_event_fact_and_claim(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    source_resource_id: str,
) -> dict[str, str]:
    obj_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    fact_v1_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type, provider_object_id
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s)
                    """,
                    (obj_id, workspace_id, source_resource_id, f"pr-{obj_id[:8]}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode, title
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', 'PR Event')
                    """,
                    (event_id, workspace_id, obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, predicate,
                        object_text, status, extractor_version
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'ACTION', 'implemented',
                        'Implemented token bucket limiter', 'SUPPORTED', 'v1'
                    )
                    """,
                    (fact_v1_id, workspace_id, event_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, version, claim_text, status
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 1,
                        'Implemented token bucket rate limiter in checkout API.', 'APPROVED'
                    )
                    """,
                    (claim_id, workspace_id, event_id),
                )
    return {
        "source_object_id": obj_id,
        "event_id": event_id,
        "fact_v1_id": fact_v1_id,
        "claim_id": claim_id,
    }


def test_0070_facts_versioning_supersedes_and_superseded_status(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    assert MIGRATION_0070_TABLES == ("exports", "export_claims")
    assert set(MIGRATION_0070_TABLES).issubset(set(ALL_TENANT_DOMAIN_TABLES))
    assert len(MIGRATION_0070_COMPOSITE_FK_SPEC) == 4

    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/repo-a")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="beta/repo-b")
    items_a = _seed_event_fact_and_claim(
        pg18_cluster,
        workspace_id=seed_a["workspace_id"],
        source_resource_id=seed_a["source_resource_id"],
    )
    items_b = _seed_event_fact_and_claim(
        pg18_cluster,
        workspace_id=seed_b["workspace_id"],
        source_resource_id=seed_b["source_resource_id"],
    )

    fact_v2_id = str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # Default version = 1
                cur.execute(
                    "SELECT version, supersedes_fact_id FROM public.facts WHERE id = %s::uuid",
                    (items_a["fact_v1_id"],),
                )
                assert cur.fetchone() == (1, None)

                # Mark v1 SUPERSEDED and insert v2 superseding v1
                cur.execute(
                    "UPDATE public.facts SET status = 'SUPERSEDED' WHERE id = %s::uuid",
                    (items_a["fact_v1_id"],),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, predicate,
                        object_text, status, extractor_version, version, supersedes_fact_id
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'ACTION', 'implemented',
                        'Implemented distributed token bucket limiter', 'USER_CONFIRMED',
                        'v1', 2, %s::uuid
                    )
                    """,
                    (
                        fact_v2_id,
                        seed_a["workspace_id"],
                        items_a["event_id"],
                        items_a["fact_v1_id"],
                    ),
                )

        # Reject version < 1
        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.facts (
                            workspace_id, event_id, fact_type, predicate,
                            object_text, status, extractor_version, version
                        ) VALUES (
                            %s::uuid, %s::uuid, 'ACTION', 'p', 'o', 'CANDIDATE', 'v1', 0
                        )
                        """,
                        (seed_a["workspace_id"], items_a["event_id"]),
                    )

        # Reject self-supersede
        self_id = str(uuid.uuid4())
        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.facts (
                            id, workspace_id, event_id, fact_type, predicate,
                            object_text, status, extractor_version, version, supersedes_fact_id
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'ACTION', 'p', 'o', 'CANDIDATE', 'v1', 2, %s::uuid
                        )
                        """,
                        (self_id, seed_a["workspace_id"], items_a["event_id"], self_id),
                    )

        # Reject cross-workspace supersedes_fact_id via Composite FK
        with pytest.raises(pg_errors.ForeignKeyViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.facts (
                            workspace_id, event_id, fact_type, predicate,
                            object_text, status, extractor_version, version, supersedes_fact_id
                        ) VALUES (
                            %s::uuid, %s::uuid, 'ACTION', 'p', 'o', 'CANDIDATE', 'v1', 2, %s::uuid
                        )
                        """,
                        (seed_a["workspace_id"], items_a["event_id"], items_b["fact_v1_id"]),
                    )


def test_0070_exports_and_export_claims_plan_c_secret_guards_and_rls(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/exp-a")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="beta/exp-b")
    items_a = _seed_event_fact_and_claim(
        pg18_cluster,
        workspace_id=seed_a["workspace_id"],
        source_resource_id=seed_a["source_resource_id"],
    )
    items_b = _seed_event_fact_and_claim(
        pg18_cluster,
        workspace_id=seed_b["workspace_id"],
        source_resource_id=seed_b["source_resource_id"],
    )

    export_a_id = str(uuid.uuid4())
    export_claim_a_id = str(uuid.uuid4())

    # 1. Insert via cm_api_app with active membership + workspace GUC
    with pg18_cluster.api_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        set_config('app.current_workspace_id', %s, true),
                        set_config('app.current_user_id', %s, true)
                    """,
                    (seed_a["workspace_id"], seed_a["user_id"]),
                )
                cur.execute(
                    """
                    INSERT INTO public.exports (
                        id, workspace_id, source_resource_id, requested_by_user_id,
                        format, export_policy, status, idempotency_key, input_hash,
                        policy_version, manifest, artifact_payload
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        'MARKDOWN', 'SOURCE_VERIFIED_ONLY', 'READY', %s, %s,
                        'v1', '{"claim_count": 1}'::jsonb, '{"sections": []}'::jsonb
                    )
                    """,
                    (
                        export_a_id,
                        seed_a["workspace_id"],
                        seed_a["source_resource_id"],
                        seed_a["user_id"],
                        str(uuid.uuid4()),
                        _sha256("export-a-input"),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.export_claims (
                        id, workspace_id, export_id, claim_id, claim_version,
                        computed_verification_at_export, support_manifest_snapshot, ordinal
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, 1,
                        'SOURCE_VERIFIED', '{"fact_ids": []}'::jsonb, 0
                    )
                    """,
                    (
                        export_claim_a_id,
                        seed_a["workspace_id"],
                        export_a_id,
                        items_a["claim_id"],
                    ),
                )

        # 2. Plan C forbidden raw key blocked on manifest, artifact_payload, and support_manifest_snapshot
        for col, bad_json in (
            ("manifest", {"nested": {"raw_text": "CANARY_RAW"}}),
            ("manifest", {"nested": {"access_token": "gho_secret"}}),
            ("artifact_payload", {"items": [{"diff": "CANARY_DIFF"}]}),
            ("artifact_payload", {"headers": {"authorization": "Bearer x"}}),
        ):
            with pytest.raises(pg_errors.CheckViolation):
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT
                                set_config('app.current_workspace_id', %s, true),
                                set_config('app.current_user_id', %s, true)
                            """,
                            (seed_a["workspace_id"], seed_a["user_id"]),
                        )
                        cur.execute(
                            f"""
                            INSERT INTO public.exports (
                                workspace_id, format, export_policy, status,
                                idempotency_key, input_hash, policy_version, {col}
                            ) VALUES (
                                %s::uuid, 'JSON', 'SOURCE_VERIFIED_ONLY', 'PENDING',
                                %s, %s, 'v1', %s
                            )
                            """,
                            (
                                seed_a["workspace_id"],
                                str(uuid.uuid4()),
                                _sha256(f"bad-{col}-{uuid.uuid4()}"),
                                Jsonb(bad_json),
                            ),
                        )

        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            set_config('app.current_workspace_id', %s, true),
                            set_config('app.current_user_id', %s, true)
                        """,
                        (seed_a["workspace_id"], seed_a["user_id"]),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.export_claims (
                            workspace_id, export_id, claim_id, claim_version,
                            computed_verification_at_export, support_manifest_snapshot
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 1,
                            'SOURCE_VERIFIED', '{"quote": "CANARY_QUOTE"}'::jsonb
                        )
                        """,
                        (seed_a["workspace_id"], export_a_id, items_a["claim_id"]),
                    )

        # 3. Cross-workspace RLS isolation (ws B sees 0 rows of ws A) & fail-closed without GUC
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.exports")
                assert cur.fetchone() == (0,)
                cur.execute("SELECT COUNT(*) FROM public.export_claims")
                assert cur.fetchone() == (0,)

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        set_config('app.current_workspace_id', %s, true),
                        set_config('app.current_user_id', %s, true)
                    """,
                    (seed_b["workspace_id"], seed_b["user_id"]),
                )
                cur.execute("SELECT COUNT(*) FROM public.exports")
                assert cur.fetchone() == (0,)
                cur.execute("SELECT COUNT(*) FROM public.export_claims")
                assert cur.fetchone() == (0,)

    # 4. cm_worker_app has SELECT/INSERT/UPDATE but NO DELETE on exports & export_claims
    with pg18_cluster.worker_connection() as wconn:
        with wconn.transaction():
            with wconn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_workspace_id', %s, true)",
                    (seed_a["workspace_id"],),
                )
                cur.execute("SELECT COUNT(*) FROM public.exports")
                assert cur.fetchone() == (1,)
                cur.execute("SELECT COUNT(*) FROM public.export_claims")
                assert cur.fetchone() == (1,)
        with pytest.raises(pg_errors.InsufficientPrivilege):
            with wconn.transaction():
                with wconn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('app.current_workspace_id', %s, true)",
                        (seed_a["workspace_id"],),
                    )
                    cur.execute("DELETE FROM public.exports WHERE id = %s::uuid", (export_a_id,))

    # 5. Composite FK blocks cross-workspace export_claims -> claims even as admin
    with pg18_cluster.admin_connection() as aconn:
        with pytest.raises(pg_errors.ForeignKeyViolation):
            with aconn.transaction():
                with aconn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.export_claims (
                            workspace_id, export_id, claim_id, claim_version,
                            computed_verification_at_export
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 1, 'SOURCE_VERIFIED'
                        )
                        """,
                        (seed_a["workspace_id"], export_a_id, items_b["claim_id"]),
                    )
