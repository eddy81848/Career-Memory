"""Real PostgreSQL 18 integration, CLAIM-09 Barrier concurrency, SCN-12/15/16, REF-07a/d & canary tests."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    ContentUnitType,
    build_span_locator,
    canonicalize_source_bytes,
)
from career_memory_api.claims import (
    ApproveClaimActionRequest,
    ClaimError,
    ClaimGenerationStatus,
    GenerateEventClaimsRequest,
    PostgresClaimService,
    RejectClaimActionRequest,
    RevalidateClaimRequest,
)
from career_memory_api.contracts_api.models import ClaimEditRequest
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.facts import (
    DEFAULT_EXTRACTOR_VERSION,
    DEFAULT_VALIDATOR_VERSION,
    PostgresFactService,
    RejectFactRequest,
)
from career_memory_api.identities import (
    GitHubActorObservation,
    GitHubActorRole,
    PostgresIdentityService,
    RevokeIdentityMappingRequest,
)
from career_memory_api.llm_gate import (
    LlmProviderMode,
    LlmStageCode,
    StageBudgetPolicy,
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
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    ContributionRole,
    FactStatus,
    FactType,
    RecordOrigin,
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


def _budget() -> StageBudgetPolicy:
    return StageBudgetPolicy(
        max_input_bytes=128 * 1024,
        max_calls=5,
        calls_used=0,
        max_repair_attempts=0,
        max_pre_spend_retries=0,
        overage_credit_disabled=True,
        quota_remaining_known_and_sufficient=True,
    )


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m4-claims-pg18-")
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


def _seed_event_with_fact(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    source_resource_id: str,
    raw_text: str,
    fact_type: FactType = FactType.ACTION,
    predicate: str = "implemented",
    object_text: str = "Implemented retry queue for billing webhooks",
    fact_status: FactStatus = FactStatus.SUPPORTED,
    normalized_value: dict[str, Any] | None = None,
    subject_identity_id: str | None = None,
    event_id: str | None = None,
    source_object_id: str | None = None,
    provider_object_id: str | None = None,
    support_type: SupportType = SupportType.DIRECT,
) -> dict[str, Any]:
    canon = canonicalize_source_bytes(raw_text.encode("utf-8"))
    assert canon.status == "CANONICALIZED" and canon.document is not None
    doc = canon.document
    rev_hash = doc.metadata.content_sha256
    locator = build_span_locator(doc.text, 0, len(doc.text))
    unit_hash = locator.span_sha256

    obj_id = source_object_id or str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    unit_id = str(uuid.uuid4())
    evt_id = event_id or str(uuid.uuid4())
    fact_id = str(uuid.uuid4())
    fs_id = str(uuid.uuid4())
    p_obj = provider_object_id or f"pr-{obj_id[:8]}"
    p_rev = f"rev-{rev_id[:8]}"

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
                if source_object_id is None:
                    cur.execute(
                        """
                        INSERT INTO public.source_objects (
                            id, workspace_id, source_resource_id, object_type, provider_object_id
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s)
                        """,
                        (obj_id, workspace_id, source_resource_id, p_obj),
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
                    (rev_id, workspace_id, obj_id, p_rev, rev_hash),
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
                    (unit_id, workspace_id, rev_id, json.dumps(anchor), unit_hash, CANONICALIZATION_VERSION),
                )
                if event_id is None:
                    cur.execute(
                        """
                        INSERT INTO public.work_events (
                            id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                            title, status
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', 'Confirmed PR Event', 'CONFIRMED')
                        """,
                        (evt_id, workspace_id, obj_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.event_memberships (
                            workspace_id, event_id, source_object_id,
                            relation_class, relation_type, origin, status
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'MUST_LINK', 'PRIMARY_PR', 'SOURCE', 'ACTIVE')
                        """,
                        (workspace_id, evt_id, obj_id),
                    )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, subject_identity_id,
                        predicate, object_text, normalized_value, status, extractor_version
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s, %s::uuid,
                        %s, %s, %s::jsonb, %s, %s
                    )
                    """,
                    (
                        fact_id,
                        workspace_id,
                        evt_id,
                        fact_type.value,
                        subject_identity_id,
                        predicate,
                        object_text,
                        json.dumps(normalized_value) if normalized_value is not None else None,
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
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s,
                        NULL, %s::jsonb, %s, %s, 'ACTIVE'
                    )
                    """,
                    (
                        fs_id,
                        workspace_id,
                        fact_id,
                        unit_id,
                        support_type.value,
                        json.dumps({"verified_locator": anchor}),
                        SupportValidator.MODEL.value,
                        DEFAULT_VALIDATOR_VERSION,
                    ),
                )
    return {
        "source_object_id": obj_id,
        "source_object_revision_id": rev_id,
        "content_unit_id": unit_id,
        "event_id": evt_id,
        "fact_id": fact_id,
        "fact_support_id": fs_id,
        "raw_bytes": raw_text.encode("utf-8"),
        "revision_hash": rev_hash,
        "unit_hash": unit_hash,
    }


class _FactoryProvenanceStore:
    def __init__(self, cluster: EphemeralBackfillPg18Cluster) -> None:
        self._cluster = cluster

    def get_unit_resolution_target(self, access: WorkspaceAccessContext, content_unit_id: str) -> Any:
        with self._cluster.api_connection() as conn:
            return PostgresProvenanceMetadataStore(conn).get_unit_resolution_target(
                access, content_unit_id
            )

    def list_sibling_unit_anchors(
        self, access: WorkspaceAccessContext, source_object_revision_id: str
    ) -> Any:
        with self._cluster.api_connection() as conn:
            return PostgresProvenanceMetadataStore(conn).list_sibling_unit_anchors(
                access, source_object_revision_id
            )

    def check_policy_fence_current(self, access: WorkspaceAccessContext, content_unit_id: str) -> Any:
        with self._cluster.api_connection() as conn:
            return PostgresProvenanceMetadataStore(conn).check_policy_fence_current(
                access, content_unit_id
            )

    def record_resolution_metadata(self, access: WorkspaceAccessContext, metadata: Any) -> None:
        with self._cluster.api_connection() as conn:
            PostgresProvenanceMetadataStore(conn).record_resolution_metadata(access, metadata)


def _make_resolver(
    cluster: EphemeralBackfillPg18Cluster,
    bytes_by_revision: dict[str, bytes],
) -> RefetchResolverService:
    store = _FactoryProvenanceStore(cluster)
    fetcher = InMemoryProviderContentFetcher(
        handler=lambda req: ProviderFetchResponse(
            kind=ProviderFetchOutcomeKind.OK,
            http_status=200,
            raw_bytes=bytes_by_revision[req.source_object_revision_id],
            bytes_read=len(bytes_by_revision[req.source_object_revision_id]),
            provider_visibility="PUBLIC",
        )
    )
    return RefetchResolverService(metadata_store=store, fetcher=fetcher)


def test_pg18_stage3_pipeline_scn15_claim05_and_scn16_claim06(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    ident_svc = PostgresIdentityService(api_connection_factory=pg18_cluster.api_connection)
    fact_svc = PostgresFactService(api_connection_factory=pg18_cluster.api_connection)

    # Verified PERSON identity (Alex Kim)
    alex = ident_svc.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="88001",
            username="alex-kim",
            display_name="Alex Kim",
            email="alex@example.com",
            provider_associated=True,
        ),
    )

    # SCN-15: ROLE fact + concrete supporting ACTION fact (fs-15-1b pattern) + SOURCE_SUPPORTED contribution
    role_ev = _seed_event_with_fact(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        raw_text="Alex Kim served as Tech Lead for Q1 payments ledger migration and authored the RFC.",
        fact_type=FactType.ROLE,
        predicate="served_as",
        object_text="Tech Lead on payments ledger migration",
        normalized_value={"scope": "INDIVIDUAL"},
        subject_identity_id=alex.identity_id,
    )
    action_ev = _seed_event_with_fact(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        raw_text="Authored RFC and reviewed rollout checklist for payments ledger migration.",
        fact_type=FactType.ACTION,
        predicate="implemented",
        object_text="Authored RFC and rollout review for payments ledger migration",
        event_id=role_ev["event_id"],
        source_object_id=role_ev["source_object_id"],
    )
    # Record SOURCE_SUPPORTED contribution backed by concrete SUPPORTED action fact
    contrib = fact_svc.record_contribution(
        access,
        event_id=role_ev["event_id"],
        identity_id=alex.identity_id,
        role=ContributionRole.IMPLEMENTED,
        origin=RecordOrigin.RULE,
        supporting_fact_ids=(action_ev["fact_id"],),
    )
    assert contrib.status.value == "SOURCE_SUPPORTED"

    # SCN-16: 6-element METRIC fact on the same event
    metric_details = {
        "value": "35%",
        "unit": "percent_reduction",
        "period": "30 days (April 2026)",
        "source_basis": "Datadog APM",
        "environment_scope": "PRODUCTION",
        "measurement_scope": "checkout-api p99",
    }
    metric_ev = _seed_event_with_fact(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        raw_text="Reduced checkout-api p99 latency by 35% over 30 days in production (Datadog APM).",
        fact_type=FactType.METRIC,
        predicate="reduced_p99_latency",
        object_text="Reduced checkout-api p99 latency by 35% over 30 days in production",
        normalized_value={"metric_details": metric_details, "environment_scope": "PRODUCTION"},
        event_id=role_ev["event_id"],
        source_object_id=role_ev["source_object_id"],
    )

    resolver = _make_resolver(
        pg18_cluster,
        {
            role_ev["source_object_revision_id"]: role_ev["raw_bytes"],
            action_ev["source_object_revision_id"]: action_ev["raw_bytes"],
            metric_ev["source_object_revision_id"]: metric_ev["raw_bytes"],
        },
    )

    adapter = SyntheticFixtureStageAdapter(
        fixtures_by_stage={
            LlmStageCode.STAGE_3A_CLAIM_GENERATION: lambda inp: {
                "schema_version": "1.0.0",
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "confirmed_event_id": role_ev["event_id"],
                "input_fact_ids": [role_ev["fact_id"], action_ev["fact_id"], metric_ev["fact_id"]],
                "claim_version": 1,
                "claim_text": (
                    "Served as Tech Lead on payments ledger migration and reduced "
                    "checkout-api p99 latency by 35% over 30 days in production."
                ),
                "status": "DRAFT",
            },
            LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION: lambda inp: {
                "schema_version": "1.0.0",
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "claim_id": inp.context_metadata["claim_id"],
                "claim_version": inp.context_metadata["claim_version"],
                "atoms": [
                    {
                        "ordinal": 0,
                        "atom_type": "ROLE",
                        "atom_text": "Served as Tech Lead on payments ledger migration",
                    },
                    {
                        "ordinal": 1,
                        "atom_type": "METRIC",
                        "atom_text": "Reduced checkout-api p99 latency by 35% over 30 days in production",
                    },
                ],
            },
            LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION: lambda inp: {
                "schema_version": "1.0.0",
                "validation_run_id": inp.context_metadata["validation_run_id"],
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "claim_id": inp.context_metadata["claim_id"],
                "claim_version": inp.context_metadata["claim_version"],
                "atom_validations": [
                    {
                        "ordinal": 0,
                        "atom_type": "ROLE",
                        "status": "SUPPORTED",
                        "fact_bindings": [
                            {
                                "fact_id": role_ev["fact_id"],
                                "support_type": "DIRECT",
                                "fact_support_ids": [role_ev["fact_support_id"]],
                                "source_object_revision_id": role_ev["source_object_revision_id"],
                                "content_unit_id": role_ev["content_unit_id"],
                            }
                        ],
                        "role_contribution_grounding": {
                            "subject_identity_id": alex.identity_id,
                            "confirmed_event_id": role_ev["event_id"],
                            "role": "IMPLEMENTED",
                            "active_contribution_support_fact_ids": [action_ev["fact_id"]],
                        },
                    },
                    {
                        "ordinal": 1,
                        "atom_type": "METRIC",
                        "status": "SUPPORTED",
                        "fact_bindings": [
                            {
                                "fact_id": metric_ev["fact_id"],
                                "support_type": "DIRECT",
                                "fact_support_ids": [metric_ev["fact_support_id"]],
                                "source_object_revision_id": metric_ev["source_object_revision_id"],
                                "content_unit_id": metric_ev["content_unit_id"],
                            }
                        ],
                        "metric_grounding": metric_details,
                    },
                ],
                "computed_verification": "SOURCE_VERIFIED",
            },
        }
    )

    claims_svc = PostgresClaimService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=resolver,
        default_adapter=adapter,
        default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
        allow_synthetic_fixture_mode=True,
    )

    gen_resp = claims_svc.generate_event_claims(
        access,
        idempotency_key=str(uuid.uuid4()),
        request=GenerateEventClaimsRequest(confirmed_event_id=role_ev["event_id"]),
    )
    assert gen_resp.generation_status is ClaimGenerationStatus.SUCCEEDED
    assert len(gen_resp.claims) == 1
    claim = gen_resp.claims[0]
    assert claim.status is ClaimStatus.SUPPORT_VALIDATED
    assert claim.computed_verification is ClaimVerification.SOURCE_VERIFIED
    assert claim.approvable is True
    assert claim.generator_run_id is not None
    assert claim.last_verification_run_id is not None
    assert claim.generator_run_id != claim.last_verification_run_id

    # REF-07a: Approve with live Plan C refetch succeeds and records checked_at
    approved = claims_svc.approve_claim_action(
        access,
        claim_id=claim.claim_id,
        idempotency_key=str(uuid.uuid4()),
        request=ApproveClaimActionRequest(
            expected_version=1,
            expected_support_graph_epoch=claim.support_graph_epoch,
        ),
    )
    assert approved.status is ClaimStatus.APPROVED
    assert approved.computed_verification is ClaimVerification.SOURCE_VERIFIED
    assert approved.approved_at is not None

    # GET /v1/claims/{claim_id}/supports returns complete non-raw provenance chain
    supports = claims_svc.get_claim_supports(access, claim_id=claim.claim_id)
    assert supports.computed_verification is ClaimVerification.SOURCE_VERIFIED
    assert len(supports.atoms) == 2
    assert len(supports.atoms[0].supports) == 1
    assert len(supports.atoms[0].supports[0].fact_supports) == 1

    # Rejecting an approved claim marks it REJECTED via POST /v1/claims/{claim_id}/actions/reject
    rejected = claims_svc.reject_claim_action(
        access,
        claim_id=claim.claim_id,
        idempotency_key=str(uuid.uuid4()),
        request=RejectClaimActionRequest(expected_version=1, reason="withdrawn by user"),
    )
    assert rejected.status is ClaimStatus.REJECTED


def test_pg18_scn12_four_way_edit_matrix_and_er012_history_preservation(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """CLAIM-08 / ER-012 / SCN-12 4-way matrix:
    1. claim-12-2: Edited v2 unvalidated -> approvable=False (UNVALIDATED_AFTER_EDIT), prior APPROVED v1 preserved!
    2. claim-12-1: Edited v2 over-generalizing checkout peak -25% to all services -> DRAFT_INVALID (UNSUPPORTED).
    3. claim-12-3: Edited v2 faithful paraphrase + fresh Stage 3B/3C -> SUPPORT_VALIDATED & SOURCE_VERIFIED!
    4. claim-12-4: USER_CONFIRMED atom alone -> NEVER SOURCE_VERIFIED; requires explicit ack to approve.
    """
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    metric_details = {
        "value": "25%",
        "unit": "percent_reduction",
        "period": "peak checkout traffic",
        "source_basis": "pg_stat_statements",
        "environment_scope": "PRODUCTION",
        "measurement_scope": "checkout PostgreSQL pool wait time",
    }
    ev = _seed_event_with_fact(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        raw_text="Tuned checkout PostgreSQL connection pool and reduced peak wait time by 25%.",
        fact_type=FactType.METRIC,
        predicate="reduced_wait_time",
        object_text="Reduced checkout PostgreSQL pool wait time by 25% during peak traffic",
        normalized_value={"metric_details": metric_details, "environment_scope": "PRODUCTION"},
    )
    user_fact = _seed_event_with_fact(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        raw_text="Coordinated rollout checklist with checkout SRE team.",
        fact_type=FactType.ACTION,
        predicate="coordinated",
        object_text="Coordinated rollout checklist with checkout SRE team",
        fact_status=FactStatus.USER_CONFIRMED,
        event_id=ev["event_id"],
        source_object_id=ev["source_object_id"],
    )

    resolver = _make_resolver(
        pg18_cluster,
        {
            ev["source_object_revision_id"]: ev["raw_bytes"],
            user_fact["source_object_revision_id"]: user_fact["raw_bytes"],
        },
    )
    adapter = SyntheticFixtureStageAdapter(
        fixtures_by_stage={
            LlmStageCode.STAGE_3A_CLAIM_GENERATION: lambda inp: {
                "schema_version": "1.0.0",
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "confirmed_event_id": ev["event_id"],
                "input_fact_ids": [ev["fact_id"]],
                "claim_version": 1,
                "claim_text": "Reduced checkout PostgreSQL pool wait time by 25% during peak traffic.",
                "status": "DRAFT",
            },
            LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION: lambda inp: {
                "schema_version": "1.0.0",
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "claim_id": inp.context_metadata["claim_id"],
                "claim_version": inp.context_metadata["claim_version"],
                "atoms": [
                    {
                        "ordinal": 0,
                        "atom_type": "METRIC",
                        "atom_text": "Reduced checkout PostgreSQL pool wait time by 25% during peak traffic",
                    }
                ],
            },
            LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION: lambda inp: {
                "schema_version": "1.0.0",
                "validation_run_id": inp.context_metadata["validation_run_id"],
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "claim_id": inp.context_metadata["claim_id"],
                "claim_version": inp.context_metadata["claim_version"],
                "atom_validations": [
                    {
                        "ordinal": 0,
                        "atom_type": "METRIC",
                        "status": "SUPPORTED",
                        "fact_bindings": [
                            {
                                "fact_id": ev["fact_id"],
                                "support_type": "DIRECT",
                                "fact_support_ids": [ev["fact_support_id"]],
                                "source_object_revision_id": ev["source_object_revision_id"],
                                "content_unit_id": ev["content_unit_id"],
                            }
                        ],
                        "metric_grounding": metric_details,
                    }
                ],
                "computed_verification": "SOURCE_VERIFIED",
            },
        }
    )
    svc = PostgresClaimService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=resolver,
        default_adapter=adapter,
        default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
        allow_synthetic_fixture_mode=True,
    )

    # Generate and approve v1
    v1 = svc.generate_event_claims(
        access,
        idempotency_key=str(uuid.uuid4()),
        request=GenerateEventClaimsRequest(confirmed_event_id=ev["event_id"]),
    ).claims[0]
    v1_approved = svc.approve_claim_action(
        access,
        claim_id=v1.claim_id,
        idempotency_key=str(uuid.uuid4()),
        request=ApproveClaimActionRequest(expected_version=1),
    )
    assert v1_approved.status is ClaimStatus.APPROVED

    # 1. Edit APPROVED v1 via HTTP PATCH /v1/claims/{claim_id} (M1 route delegated to PG18!)
    app = create_m1_contract_app(claims_service=svc, resolve_access=lambda _r: access)
    client = TestClient(app)

    patch_resp = client.patch(
        f"/v1/claims/{v1.claim_id}",
        json={
            "expected_version": 1,
            "claim_text": "Cut all services wait time by 25% across the entire company.",
            "edit_reason": "overgeneralized edit",
        },
    )
    assert patch_resp.status_code == 200
    v2_overgen_id = patch_resp.json()["claim_id"]
    assert v2_overgen_id != v1.claim_id
    assert patch_resp.json()["version"] == 2
    assert patch_resp.json()["status"] == "DRAFT"
    assert patch_resp.json()["computed_verification"] == "UNVERIFIED"

    # Prior APPROVED v1 MUST remain preserved as APPROVED history (ER-012)
    v1_after = svc.get_claim_detail(access, claim_id=v1.claim_id)
    assert v1_after.status is ClaimStatus.APPROVED

    # claim-12-2: Unvalidated v2 cannot be approved!
    v2_unval = svc.get_claim_detail(access, claim_id=v2_overgen_id)
    assert v2_unval.approvable is False
    assert "UNVALIDATED_AFTER_EDIT" in v2_unval.approval_block_reasons
    assert v2_unval.atoms == []

    # 2. claim-12-1: Revalidate over-generalized v2 -> DRAFT_INVALID (UNSUPPORTED)
    v2_reval_fail = svc.revalidate_claim_version(
        access,
        claim_id=v2_overgen_id,
        idempotency_key=str(uuid.uuid4()),
        request=RevalidateClaimRequest(expected_version=2),
    )
    assert v2_reval_fail.status is ClaimStatus.DRAFT_INVALID
    assert v2_reval_fail.computed_verification is ClaimVerification.UNVERIFIED
    assert v2_reval_fail.atoms[0].status is ClaimAtomStatus.UNSUPPORTED

    # 3. claim-12-3: Edit into faithful paraphrase v3 -> revalidate -> SUPPORT_VALIDATED & SOURCE_VERIFIED!
    v3_edit = svc.edit_claim(
        access,
        v2_overgen_id,
        ClaimEditRequest(
            expected_version=2,
            claim_text="Reduced checkout PostgreSQL pool wait time by 25% during peak traffic.",
        ),
    )
    # Previous non-approved v2 is now SUPERSEDED
    assert svc.get_claim_detail(access, claim_id=v2_overgen_id).status is ClaimStatus.SUPERSEDED
    v3_reval = svc.revalidate_claim_version(
        access,
        claim_id=v3_edit.claim_id,
        idempotency_key=str(uuid.uuid4()),
        request=RevalidateClaimRequest(expected_version=3),
    )
    assert v3_reval.status is ClaimStatus.SUPPORT_VALIDATED
    assert v3_reval.computed_verification is ClaimVerification.SOURCE_VERIFIED
    assert v3_reval.approvable is True

    # 4. claim-12-4: Edit into USER_CONFIRMED-only text -> USER_CONFIRMED (NEVER SOURCE_VERIFIED)
    v4_edit = svc.edit_claim(
        access,
        v3_edit.claim_id,
        ClaimEditRequest(
            expected_version=3,
            claim_text="Coordinated rollout checklist with checkout SRE team.",
        ),
    )
    v4_reval = svc.revalidate_claim_version(
        access,
        claim_id=v4_edit.claim_id,
        idempotency_key=str(uuid.uuid4()),
        request=RevalidateClaimRequest(expected_version=4),
    )
    assert v4_reval.status is ClaimStatus.SUPPORT_VALIDATED
    assert v4_reval.computed_verification is ClaimVerification.USER_CONFIRMED
    assert v4_reval.atoms[0].status is ClaimAtomStatus.USER_CONFIRMED

    # Approving USER_CONFIRMED without acknowledgement -> 409 SUPPORT_INCOMPLETE
    with pytest.raises(ClaimError) as ack_exc:
        svc.approve_claim_action(
            access,
            claim_id=v4_edit.claim_id,
            idempotency_key=str(uuid.uuid4()),
            request=ApproveClaimActionRequest(
                expected_version=4, acknowledge_user_confirmed_atoms=False
            ),
        )
    assert ack_exc.value.status_code == 409
    assert ack_exc.value.code == "SUPPORT_INCOMPLETE"

    # Approving with explicit acknowledgement succeeds with USER_CONFIRMED (still never SOURCE_VERIFIED)
    v4_approved = svc.approve_claim_action(
        access,
        claim_id=v4_edit.claim_id,
        idempotency_key=str(uuid.uuid4()),
        request=ApproveClaimActionRequest(
            expected_version=4, acknowledge_user_confirmed_atoms=True
        ),
    )
    assert v4_approved.status is ClaimStatus.APPROVED
    assert v4_approved.computed_verification is ClaimVerification.USER_CONFIRMED


def test_pg18_claim09_approval_vs_invalidation_barrier_race_and_ref07d(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    """CLAIM-09 concurrency race (threading.Barrier):
    Thread A runs live refetch & pauses at before_lock_hook; Thread B rejects the supporting Fact
    (marking Claim STALE). Thread A resumes, hits FOR UPDATE epoch check, and MUST fail closed
    with 409 SUPPORT_INCOMPLETE (CLAIM_NOT_APPROVABLE_EPOCH_DRIFT), leaving Claim STALE!
    Also verifies REF-07d (historical verification without live resolver blocks approval).
    """
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    ev = _seed_event_with_fact(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        raw_text="Implemented idempotent webhook delivery worker.",
        fact_type=FactType.ACTION,
        predicate="implemented",
        object_text="Implemented idempotent webhook delivery worker",
    )
    resolver = _make_resolver(pg18_cluster, {ev["source_object_revision_id"]: ev["raw_bytes"]})
    adapter = SyntheticFixtureStageAdapter(
        fixtures_by_stage={
            LlmStageCode.STAGE_3A_CLAIM_GENERATION: lambda inp: {
                "schema_version": "1.0.0",
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "confirmed_event_id": ev["event_id"],
                "input_fact_ids": [ev["fact_id"]],
                "claim_version": 1,
                "claim_text": "Implemented idempotent webhook delivery worker.",
                "status": "DRAFT",
            },
            LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION: lambda inp: {
                "schema_version": "1.0.0",
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "claim_id": inp.context_metadata["claim_id"],
                "claim_version": 1,
                "atoms": [
                    {
                        "ordinal": 0,
                        "atom_type": "ACTION",
                        "atom_text": "Implemented idempotent webhook delivery worker",
                    }
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
                                "fact_id": ev["fact_id"],
                                "support_type": "DIRECT",
                                "fact_support_ids": [ev["fact_support_id"]],
                                "source_object_revision_id": ev["source_object_revision_id"],
                                "content_unit_id": ev["content_unit_id"],
                            }
                        ],
                    }
                ],
                "computed_verification": "SOURCE_VERIFIED",
            },
        }
    )

    setup_svc = PostgresClaimService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=resolver,
        default_adapter=adapter,
        default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
        allow_synthetic_fixture_mode=True,
    )
    claim = setup_svc.generate_event_claims(
        access,
        idempotency_key=str(uuid.uuid4()),
        request=GenerateEventClaimsRequest(confirmed_event_id=ev["event_id"]),
    ).claims[0]
    assert claim.status is ClaimStatus.SUPPORT_VALIDATED

    # REF-07d: Without live resolver (historical last_verified_at only), approval MUST fail closed!
    no_resolver_svc = PostgresClaimService(api_connection_factory=pg18_cluster.api_connection, resolver=None)
    with pytest.raises(ClaimError) as ref07d_exc:
        no_resolver_svc.approve_claim_action(
            access,
            claim_id=claim.claim_id,
            idempotency_key=str(uuid.uuid4()),
            request=ApproveClaimActionRequest(expected_version=1),
        )
    assert ref07d_exc.value.status_code == 409
    assert ref07d_exc.value.code == "REFETCH_REQUIRED_IN_CURRENT_OPERATION"

    # CLAIM-09 Barrier Race: Approver vs concurrent Fact Rejection invalidating the Claim
    reached_pre_lock = threading.Barrier(2)
    invalidation_done = threading.Barrier(2)
    approve_outcome: dict[str, Any] = {}

    def _before_lock(_action: str, _cid: str) -> None:
        reached_pre_lock.wait(timeout=5.0)
        invalidation_done.wait(timeout=5.0)

    racy_claim_svc = PostgresClaimService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=resolver,
        before_lock_hook=_before_lock,
    )
    fact_svc = PostgresFactService(api_connection_factory=pg18_cluster.api_connection)

    def _thread_approve() -> None:
        try:
            res = racy_claim_svc.approve_claim_action(
                access,
                claim_id=claim.claim_id,
                idempotency_key=str(uuid.uuid4()),
                request=ApproveClaimActionRequest(expected_version=1),
            )
            approve_outcome["ok"] = res
        except Exception as exc:  # noqa: BLE001
            approve_outcome["err"] = exc

    def _thread_invalidate() -> None:
        reached_pre_lock.wait(timeout=5.0)
        fact_svc.reject_fact(
            access,
            fact_id=ev["fact_id"],
            idempotency_key=str(uuid.uuid4()),
            request=RejectFactRequest(expected_version=1, reason="CLAIM-09 concurrent invalidation"),
        )
        invalidation_done.wait(timeout=5.0)

    t1 = threading.Thread(target=_thread_approve)
    t2 = threading.Thread(target=_thread_invalidate)
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert "ok" not in approve_outcome
    err = approve_outcome.get("err")
    assert isinstance(err, ClaimError)
    assert err.status_code == 409
    assert err.code == "SUPPORT_INCOMPLETE"
    assert err.details is not None
    assert err.details["reason_code"] == "CLAIM_NOT_APPROVABLE_EPOCH_DRIFT"

    # Claim remains STALE & UNVERIFIED in DB
    final_detail = setup_svc.get_claim_detail(access, claim_id=claim.claim_id)
    assert final_detail.status is ClaimStatus.STALE
    assert final_detail.computed_verification is ClaimVerification.UNVERIFIED
    assert final_detail.approved_at is None


def test_pg18_claims_http_guards_concealment_and_plan_c_canary_zero_leak(
    pg18_cluster: EphemeralBackfillPg18Cluster,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster)
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster)
    access_a = _access_for(seed_a)
    access_b = _access_for(seed_b)

    canary = "CANARY_SECRET_RAW_SOURCE_QUOTE_99112233"
    ev = _seed_event_with_fact(
        pg18_cluster,
        workspace_id=seed_a["workspace_id"],
        source_resource_id=seed_a["source_resource_id"],
        raw_text=f"Implemented cache warmup job. {canary}",
        fact_type=FactType.ACTION,
        predicate="implemented",
        object_text="Implemented cache warmup job",
    )
    resolver = _make_resolver(pg18_cluster, {ev["source_object_revision_id"]: ev["raw_bytes"]})
    # Default hard-OFF live service on HTTP POST /v1/claims/generations returns MANUAL_ONLY_BLOCKED
    hard_off_svc = PostgresClaimService(
        api_connection_factory=pg18_cluster.api_connection,
        resolver=resolver,
    )
    app_a = create_m1_contract_app(claims_service=hard_off_svc, resolve_access=lambda _r: access_a)
    client_a = TestClient(app_a)

    # 1. Hard-OFF live gate via HTTP
    gen_http = client_a.post(
        "/v1/claims/generations",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"confirmed_event_id": ev["event_id"]},
    )
    assert gen_http.status_code == 200
    assert gen_http.headers["Cache-Control"] == "private, no-store"
    assert gen_http.json()["generation_status"] == "MANUAL_ONLY_BLOCKED"
    assert len(gen_http.json()["free_alternatives"]) > 0

    # 2. Read-only verification write guard (422) & OD-008 project_id guard (422)
    bad_verif = client_a.post(
        f"/v1/claims/{uuid.uuid4()}/actions/approve",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"expected_version": 1, "verification": "SOURCE_VERIFIED"},
    )
    assert bad_verif.status_code == 422
    assert bad_verif.json()["error"]["code"] == "PROHIBITED_READONLY_VERIFICATION_WRITE"

    bad_proj = client_a.get("/v1/claims", params={"project_id": "proj-1"})
    assert bad_proj.status_code == 422
    assert bad_proj.json()["error"]["code"] == "PROHIBITED_PROJECT_ID_SCOPE"

    # 3. Create synthetic claim in workspace A, verify workspace B gets identical 404 concealment
    draft_resp = client_a.post(
        "/v1/claims/drafts",
        json={
            "confirmed_event_id": ev["event_id"],
            "input_fact_ids": [ev["fact_id"]],
            "claim_text": "Implemented cache warmup job.",
        },
    )
    assert draft_resp.status_code == 201
    cid = draft_resp.json()["claim_id"]

    app_b = create_m1_contract_app(claims_service=hard_off_svc, resolve_access=lambda _r: access_b)
    client_b = TestClient(app_b)
    cross_ws = client_b.get(f"/v1/claims/{cid}")
    missing_ws = client_b.get(f"/v1/claims/{uuid.uuid4()}")
    assert cross_ws.status_code == 404
    assert missing_ws.status_code == 404
    assert cross_ws.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert missing_ws.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    # 4. Zero canary leakage across HTTP, logs, and ALL persisted DB tables
    with caplog.at_level(logging.DEBUG):
        list_txt = client_a.get("/v1/claims").text
        det_txt = client_a.get(f"/v1/claims/{cid}").text
        sup_txt = client_a.get(f"/v1/claims/{cid}/supports").text
    assert canary not in caplog.text
    assert canary not in list_txt
    assert canary not in det_txt
    assert canary not in sup_txt

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            for tbl in (
                "claims",
                "claim_atoms",
                "claim_atom_support",
                "generation_runs",
                "audit_events",
                "review_actions",
                "outbox_events",
            ):
                cur.execute(
                    f"SELECT COUNT(*) FROM public.{tbl} WHERE row_to_json({tbl})::text LIKE %s",
                    (f"%{canary}%",),
                )
                assert cur.fetchone()[0] == 0, f"Canary leaked into public.{tbl}!"
