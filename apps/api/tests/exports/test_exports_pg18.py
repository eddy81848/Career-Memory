"""Real PostgreSQL 18 integration, EXPORT-02 Barrier concurrency, REF-07b/d/e/f/g & zero-quote canary tests."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Iterator
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
from career_memory_api.claims import (
    ApproveClaimActionRequest,
    GenerateEventClaimsRequest,
    PostgresClaimService,
)
from career_memory_api.contracts_api.models import ClaimEditRequest
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.exports import (
    CreateExportRequest,
    ExportError,
    PostgresExportService,
)
from career_memory_api.facts import (
    DEFAULT_EXTRACTOR_VERSION,
    DEFAULT_VALIDATOR_VERSION,
    PostgresFactService,
    RejectFactRequest,
)
from career_memory_api.llm_gate import (
    LlmProviderMode,
    LlmStageCode,
    SyntheticFixtureStageAdapter,
)
from career_memory_api.resolver.models import (
    ProviderFetchOutcomeKind,
    ProviderFetchResponse,
)
from career_memory_api.resolver.postgres_store import PostgresProvenanceMetadataStore
from career_memory_api.resolver.service import (
    InMemoryProviderContentFetcher,
    RefetchResolverService,
)
from career_memory_api.schema_contracts.enums import (
    ClaimStatus,
    ClaimVerification,
    ExportFormat,
    ExportPolicy,
    ExportStatus,
    FactStatus,
    FactType,
    SupportType,
    SupportValidator,
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
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m4-exports-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0070:
            cluster.apply_sql_file(mig)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def clean_tables(pg18_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _seed_approved_claim_chain(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    seed: dict[str, str],
    access: WorkspaceAccessContext,
    raw_text: str,
    claim_text: str,
    fact_status: FactStatus = FactStatus.SUPPORTED,
    approve: bool = True,
) -> dict[str, Any]:
    canon = canonicalize_source_bytes(raw_text.encode("utf-8"))
    assert canon.status == "CANONICALIZED" and canon.document is not None
    doc = canon.document
    rev_hash = doc.metadata.content_sha256
    locator = build_span_locator(doc.text, 0, len(doc.text))
    unit_hash = locator.span_sha256

    obj_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    unit_id = str(uuid.uuid4())
    evt_id = str(uuid.uuid4())
    fact_id = str(uuid.uuid4())
    fs_id = str(uuid.uuid4())

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

    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type, provider_object_id
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s)
                    """,
                    (obj_id, seed["workspace_id"], seed["source_resource_id"], f"pr-{obj_id[:8]}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, provider_revision_id,
                        content_hash, hash_policy_version, connector_version, schema_version,
                        observed_at, is_tombstone
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'cm-canon-nfc-lf-v1', 'github-connector-v1', '1.0.0',
                        now(), false
                    )
                    """,
                    (rev_id, seed["workspace_id"], obj_id, f"rev-{rev_id[:8]}", rev_hash),
                )
                cur.execute(
                    """
                    INSERT INTO public.content_units (
                        id, workspace_id, source_object_revision_id, unit_type, ordinal,
                        anchor, content_hash, canonicalization_version,
                        text_content, content_ref, retention_class, policy_state
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'PR_BODY_SECTION', 0,
                        %s::jsonb, %s, %s,
                        NULL, NULL, 'NONE', 'ACTIVE'
                    )
                    """,
                    (unit_id, seed["workspace_id"], rev_id, json.dumps(anchor), unit_hash, CANONICALIZATION_VERSION),
                )
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                        title, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', 'Export Event', 'CONFIRMED')
                    """,
                    (evt_id, seed["workspace_id"], obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.event_memberships (
                        workspace_id, event_id, source_object_id,
                        relation_class, relation_type, origin, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'MUST_LINK', 'PRIMARY_PR', 'SOURCE', 'ACTIVE')
                    """,
                    (seed["workspace_id"], evt_id, obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type,
                        predicate, object_text, status, extractor_version
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'ACTION',
                        'implemented', %s, %s, %s
                    )
                    """,
                    (
                        fact_id,
                        seed["workspace_id"],
                        evt_id,
                        claim_text,
                        fact_status.value,
                        DEFAULT_EXTRACTOR_VERSION,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.fact_support (
                        id, workspace_id, fact_id, content_unit_id, support_type,
                        extracted_span, support_metadata, validator, validator_version, status
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT',
                        NULL, %s::jsonb, %s, %s, 'ACTIVE'
                    )
                    """,
                    (
                        fs_id,
                        seed["workspace_id"],
                        fact_id,
                        unit_id,
                        json.dumps({"verified_locator": anchor}),
                        SupportValidator.MODEL.value,
                        DEFAULT_VALIDATOR_VERSION,
                    ),
                )

    raw_bytes = raw_text.encode("utf-8")
    bytes_by_rev: dict[str, bytes] = {rev_id: raw_bytes}

    class _FactoryStore:
        def get_unit_resolution_target(self, acc: WorkspaceAccessContext, cu_id: str) -> Any:
            with cluster.api_connection() as conn:
                return PostgresProvenanceMetadataStore(conn).get_unit_resolution_target(acc, cu_id)

        def list_sibling_unit_anchors(self, acc: WorkspaceAccessContext, r_id: str) -> Any:
            with cluster.api_connection() as conn:
                return PostgresProvenanceMetadataStore(conn).list_sibling_unit_anchors(acc, r_id)

        def check_policy_fence_current(self, acc: WorkspaceAccessContext, cu_id: str) -> Any:
            with cluster.api_connection() as conn:
                return PostgresProvenanceMetadataStore(conn).check_policy_fence_current(acc, cu_id)

        def record_resolution_metadata(self, acc: WorkspaceAccessContext, meta: Any) -> None:
            with cluster.api_connection() as conn:
                PostgresProvenanceMetadataStore(conn).record_resolution_metadata(acc, meta)

    fetcher = InMemoryProviderContentFetcher(
        handler=lambda req: ProviderFetchResponse(
            kind=ProviderFetchOutcomeKind.OK,
            http_status=200,
            raw_bytes=bytes_by_rev[req.source_object_revision_id],
            bytes_read=len(bytes_by_rev[req.source_object_revision_id]),
            provider_visibility="PUBLIC",
        )
    )
    resolver = RefetchResolverService(metadata_store=_FactoryStore(), fetcher=fetcher)

    if fact_status is FactStatus.SUPPORTED:
        adapter = SyntheticFixtureStageAdapter(
            fixtures_by_stage={
                LlmStageCode.STAGE_3A_CLAIM_GENERATION: lambda inp: {
                    "schema_version": "1.0.0",
                    "generator_run_id": inp.context_metadata["generator_run_id"],
                    "confirmed_event_id": evt_id,
                    "input_fact_ids": [fact_id],
                    "claim_version": 1,
                    "claim_text": claim_text,
                    "status": "DRAFT",
                },
                LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION: lambda inp: {
                    "schema_version": "1.0.0",
                    "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                    "generator_run_id": inp.context_metadata["generator_run_id"],
                    "claim_id": inp.context_metadata["claim_id"],
                    "claim_version": 1,
                    "atoms": [
                        {"ordinal": 0, "atom_type": "ACTION", "atom_text": claim_text}
                    ],
                },
                LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION: lambda inp: {
                    "schema_version": "1.0.0",
                    "validation_run_id": inp.context_metadata["validation_run_id"],
                    "generator_run_id": inp.context_metadata["generator_run_id"],
                    "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                    "claim_id": inp.context_metadata["claim_id"],
                    "claim_version": 1,
                    "atom_validations": [
                        {
                            "ordinal": 0,
                            "atom_type": "ACTION",
                            "status": "SUPPORTED",
                            "fact_bindings": [
                                {
                                    "fact_id": fact_id,
                                    "support_type": "DIRECT",
                                    "fact_support_ids": [fs_id],
                                    "source_object_revision_id": rev_id,
                                    "content_unit_id": unit_id,
                                }
                            ],
                        }
                    ],
                    "computed_verification": "SOURCE_VERIFIED",
                },
            }
        )
        claim_svc = PostgresClaimService(
            api_connection_factory=cluster.api_connection,
            resolver=resolver,
            default_adapter=adapter,
            default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
        detail = claim_svc.generate_event_claims(
            access,
            idempotency_key=str(uuid.uuid4()),
            request=GenerateEventClaimsRequest(confirmed_event_id=evt_id),
        ).claims[0]
        if approve:
            detail = claim_svc.approve_claim_action(
                access,
                claim_id=detail.claim_id,
                idempotency_key=str(uuid.uuid4()),
                request=ApproveClaimActionRequest(expected_version=1),
            )
    else:
        # USER_CONFIRMED fact path
        claim_svc = PostgresClaimService(
            api_connection_factory=cluster.api_connection,
            resolver=resolver,
        )
        from career_memory_api.contracts_api.models import ClaimDraftCreateRequest
        from career_memory_api.claims import RevalidateClaimRequest

        draft = claim_svc.create_claim_draft(
            access,
            ClaimDraftCreateRequest(
                confirmed_event_id=evt_id,
                input_fact_ids=[fact_id],
                claim_text=claim_text,
            ),
        )
        detail = claim_svc.revalidate_claim_version(
            access,
            claim_id=draft.claim_id,
            idempotency_key=str(uuid.uuid4()),
            request=RevalidateClaimRequest(expected_version=1),
        )
        if approve:
            detail = claim_svc.approve_claim_action(
                access,
                claim_id=detail.claim_id,
                idempotency_key=str(uuid.uuid4()),
                request=ApproveClaimActionRequest(
                    expected_version=1,
                    acknowledge_user_confirmed_atoms=True,
                ),
            )

    return {
        "claim_id": detail.claim_id,
        "claim_version": detail.version,
        "fact_id": fact_id,
        "content_unit_id": unit_id,
        "source_object_revision_id": rev_id,
        "event_id": evt_id,
        "resolver": resolver,
        "bytes_by_rev": bytes_by_rev,
        "claim_svc": claim_svc,
    }


def test_pg18_exports_ref07b_d_f_g_policy_gating_and_zero_quote_canary(
    pg18_cluster: EphemeralBackfillPg18Cluster,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)

    canary = "EXPORT_ZERO_QUOTE_CANARY_TOKEN_778899"
    sv_chain = _seed_approved_claim_chain(
        pg18_cluster,
        seed=seed,
        access=access,
        raw_text=f"Implemented billing webhook retry queue. {canary}",
        claim_text="Implemented billing webhook retry queue.",
        fact_status=FactStatus.SUPPORTED,
        approve=True,
    )
    unapproved_chain = _seed_approved_claim_chain(
        pg18_cluster,
        seed=seed,
        access=access,
        raw_text="Implemented draft metrics dashboard.",
        claim_text="Implemented draft metrics dashboard.",
        fact_status=FactStatus.SUPPORTED,
        approve=False,
    )
    uc_chain = _seed_approved_claim_chain(
        pg18_cluster,
        seed=seed,
        access=access,
        raw_text="Led customer onboarding workshop.",
        claim_text="Led customer onboarding workshop.",
        fact_status=FactStatus.USER_CONFIRMED,
        approve=True,
    )

    export_svc = PostgresExportService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=sv_chain["resolver"],
    )
    app = create_m1_contract_app(
        claims_service=sv_chain["claim_svc"],
        exports_service=export_svc,
        resolve_access=lambda _r: access,
    )
    client = TestClient(app)

    # REF-07f: Unapproved claim rejected with 409 EXPORT_CLAIM_NOT_APPROVED
    r_unapp = client.post(
        "/v1/exports",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"format": "MARKDOWN", "claim_ids": [unapproved_chain["claim_id"]]},
    )
    assert r_unapp.status_code == 409
    assert r_unapp.json()["error"]["code"] == "EXPORT_CLAIM_NOT_APPROVED"

    # REF-07g: Version mismatch rejected with 409 EXPORT_CLAIM_VERSION_MISMATCH
    r_ver = client.post(
        "/v1/exports",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "format": "JSON",
            "claim_ids": [sv_chain["claim_id"]],
            "expected_claim_versions": {sv_chain["claim_id"]: 2},
        },
    )
    assert r_ver.status_code == 409
    assert r_ver.json()["error"]["code"] == "EXPORT_CLAIM_VERSION_MISMATCH"

    # SOURCE_VERIFIED_ONLY rejects USER_CONFIRMED claim (409 EXPORT_POLICY_DISALLOWS_VERIFICATION_LEVEL)
    r_pol_block = client.post(
        "/v1/exports",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "format": "JSON",
            "claim_ids": [uc_chain["claim_id"]],
            "export_policy": "SOURCE_VERIFIED_ONLY",
        },
    )
    assert r_pol_block.status_code == 409
    assert r_pol_block.json()["error"]["code"] == "EXPORT_POLICY_DISALLOWS_VERIFICATION_LEVEL"

    # Opt-in ALLOW_USER_CONFIRMED succeeds with explicit verification limitation notices
    r_uc_ok = client.post(
        "/v1/exports",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "format": "JSON",
            "claim_ids": [uc_chain["claim_id"]],
            "export_policy": "ALLOW_USER_CONFIRMED",
        },
    )
    assert r_uc_ok.status_code == 201
    uc_body = r_uc_ok.json()
    assert uc_body["rendered_json"]["claims"][0]["verification_limitation_notice"] is not None

    # REF-07b: SOURCE_VERIFIED Markdown export succeeds with zero-quote citations and idempotency
    idem_key = str(uuid.uuid4())
    with caplog.at_level(logging.DEBUG):
        r_md = client.post(
            "/v1/exports",
            headers={"Idempotency-Key": idem_key},
            json={
                "format": "MARKDOWN",
                "claim_ids": [sv_chain["claim_id"]],
                "expected_claim_versions": {sv_chain["claim_id"]: 1},
            },
        )
        assert r_md.status_code == 201
        assert r_md.headers["Cache-Control"] == "private, no-store"
        md_body = r_md.json()
        assert md_body["status"] == "READY"
        assert md_body["manifest"]["excludes_raw_source_quotes"] is True
        assert md_body["rendered_markdown"] is not None
        assert sv_chain["source_object_revision_id"] in md_body["rendered_markdown"]

        # Idempotent replay returns same export_id
        r_replay = client.post(
            "/v1/exports",
            headers={"Idempotency-Key": idem_key},
            json={
                "format": "MARKDOWN",
                "claim_ids": [sv_chain["claim_id"]],
                "expected_claim_versions": {sv_chain["claim_id"]: 1},
            },
        )
        assert r_replay.status_code == 201
        assert r_replay.json()["export_id"] == md_body["export_id"]

        # GET /v1/exports/{export_id} re-runs live EXPORT_DOWNLOAD refetch and returns 200
        r_get = client.get(f"/v1/exports/{md_body['export_id']}")
        assert r_get.status_code == 200
        assert r_get.json()["rendered_markdown"] is not None

    # Plan C zero-quote canary audit: 0 occurrences in HTTP responses, logs, exports, export_claims, audit_events
    assert canary not in r_md.text
    assert canary not in r_get.text
    assert canary not in caplog.text
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            for tbl in ("exports", "export_claims", "audit_events", "outbox_events"):
                cur.execute(
                    f"SELECT COUNT(*) FROM public.{tbl} WHERE row_to_json({tbl})::text LIKE %s",
                    (f"%{canary}%",),
                )
                assert cur.fetchone()[0] == 0, f"Canary leaked into public.{tbl}!"

    # Simulate remote revision drift on GET /v1/exports/{export_id} -> transitions to INVALIDATED & returns 409 SOURCE_REVISION_MISMATCH!
    sv_chain["bytes_by_rev"][sv_chain["source_object_revision_id"]] = b"Drifted remote source body!"
    r_drift = client.get(f"/v1/exports/{md_body['export_id']}")
    assert r_drift.status_code == 409
    assert r_drift.json()["error"]["code"] == "SOURCE_REVISION_MISMATCH"

    # Subsequent GET fails closed with 409 EXPORT_INVALIDATED
    r_after = client.get(f"/v1/exports/{md_body['export_id']}")
    assert r_after.status_code == 409
    assert r_after.json()["error"]["code"] == "EXPORT_INVALIDATED"

    # DELETE /v1/exports/{export_id} marks DELETED
    r_del = client.delete(
        f"/v1/exports/{md_body['export_id']}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "DELETED"
    assert client.get(f"/v1/exports/{md_body['export_id']}").status_code == 404

    # REF-07d on exports: without live resolver, POST /v1/exports fails closed
    no_res_svc = PostgresExportService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=None,
    )
    with pytest.raises(ExportError) as exp_ref07d:
        no_res_svc.create_export(
            access,
            idempotency_key=str(uuid.uuid4()),
            request=CreateExportRequest(
                format=ExportFormat.JSON,
                claim_ids=[sv_chain["claim_id"]],
            ),
        )
    assert exp_ref07d.value.status_code == 409
    assert exp_ref07d.value.code == "REFETCH_REQUIRED_IN_CURRENT_OPERATION"

    # Cross-workspace concealment (404) & OD-008 project_id guard (422) on /v1/exports
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster)
    access_b = _access_for(seed_b)
    app_b = create_m1_contract_app(exports_service=export_svc, resolve_access=lambda _r: access_b)
    client_b = TestClient(app_b)
    assert client_b.get(f"/v1/exports/{uc_body['export_id']}").status_code == 404
    assert client.get("/v1/exports", params={"project_id": "proj-1"}).status_code == 422
    assert len(client.get("/v1/exports").json()["items"]) >= 1


def test_pg18_export02_ref07e_build_vs_invalidation_barrier_race(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """EXPORT-02 / REF-07e TOCTOU Barrier Race:
    Thread A creates export, passes live refetch, renders, and pauses at before_publish_hook.
    Thread B concurrently rejects the supporting Fact (invalidating claim -> STALE & PENDING export).
    Thread A resumes publish fence, detects invalidation, commits status='INVALIDATED' with
    artifact_payload=NULL, and fails closed with 409 EXPORT_INVALIDATED!
    """
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    chain = _seed_approved_claim_chain(
        pg18_cluster,
        seed=seed,
        access=access,
        raw_text="Implemented distributed rate limiter for public API.",
        claim_text="Implemented distributed rate limiter for public API.",
        fact_status=FactStatus.SUPPORTED,
        approve=True,
    )

    reached_publish = threading.Barrier(2)
    invalidation_done = threading.Barrier(2)
    export_outcome: dict[str, Any] = {}
    captured_export_id: list[str] = []

    def _before_publish(_action: str, exp_id: str) -> None:
        captured_export_id.append(exp_id)
        reached_publish.wait(timeout=5.0)
        invalidation_done.wait(timeout=5.0)

    racy_export_svc = PostgresExportService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=chain["resolver"],
        before_publish_hook=_before_publish,
    )
    fact_svc = PostgresFactService(api_connection_factory=pg18_cluster.api_connection)

    def _thread_export() -> None:
        try:
            res = racy_export_svc.create_export(
                access,
                idempotency_key=str(uuid.uuid4()),
                request=CreateExportRequest(
                    format=ExportFormat.MARKDOWN,
                    claim_ids=[chain["claim_id"]],
                    expected_claim_versions={chain["claim_id"]: 1},
                ),
            )
            export_outcome["ok"] = res
        except Exception as exc:  # noqa: BLE001
            export_outcome["err"] = exc

    def _thread_invalidate() -> None:
        reached_publish.wait(timeout=5.0)
        fact_svc.reject_fact(
            access,
            fact_id=chain["fact_id"],
            idempotency_key=str(uuid.uuid4()),
            request=RejectFactRequest(expected_version=1, reason="EXPORT-02 build-time invalidation"),
        )
        invalidation_done.wait(timeout=5.0)

    t1 = threading.Thread(target=_thread_export)
    t2 = threading.Thread(target=_thread_invalidate)
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert "ok" not in export_outcome
    err = export_outcome.get("err")
    assert isinstance(err, ExportError)
    assert err.status_code == 409
    assert err.code == "EXPORT_INVALIDATED"
    assert len(captured_export_id) == 1

    # Verify DB state: status = 'INVALIDATED', artifact_payload IS NULL, completed_at IS NULL!
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, invalidation_reason, artifact_payload, completed_at
                FROM public.exports
                WHERE id = %s::uuid
                """,
                (captured_export_id[0],),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == ExportStatus.INVALIDATED.value
            assert row[1] is not None
            assert row[2] is None
            assert row[3] is None

    # Attempting to GET the invalidated export fails closed with 409 EXPORT_INVALIDATED
    with pytest.raises(ExportError) as get_exc:
        racy_export_svc.get_export(access, export_id=captured_export_id[0])
    assert get_exc.value.status_code == 409
    assert get_exc.value.code == "EXPORT_INVALIDATED"
