"""Unit and adversarial tests for Stage 3A/3B/3C Claim pipeline (CLAIM-04..08, T-404, T-405)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.claims import (
    ClaimAtomizer,
    ClaimAtomizationRequest,
    ClaimContextBuilder,
    ClaimDraftGenerationRequest,
    ClaimError,
    ClaimGenerator,
    ClaimSupportValidationRequest,
    ClaimSupportValidator,
    ContextBuildStatus,
    EligibleFactContextItem,
    FactEvidenceSnapshot,
    VerificationCalculator,
    compute_support_graph_epoch,
)
from career_memory_api.facts.models import (
    FactAttributionScope,
    FactCausalBasis,
    FactCompletionState,
    FactEnvironmentScope,
    FactSemanticAttributes,
    MetricDetailsModel,
)
from career_memory_api.identities.models import (
    IdentityAttributionBlockReason,
    IdentityAttributionEligibility,
)
from career_memory_api.llm_gate import (
    BoundedStageRunner,
    HostedLlmApprovalConfig,
    LlmGateOutcome,
    LlmPolicyGate,
    LlmProviderMode,
    LlmStageCode,
    StageBudgetPolicy,
    SyntheticFixtureStageAdapter,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    ContributionRole,
    ContributionStatus,
    FactStatus,
    FactType,
    IdentityStatus,
    IdentityType,
    LinkStatus,
    ResourceVisibility,
    SupportType,
    WorkEventAnchorMode,
    WorkEventStatus,
)

_HASH_A = "sha256:" + ("a" * 64)
_HASH_B = "sha256:" + ("b" * 64)


def _access() -> WorkspaceAccessContext:
    ws_id = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=uid,
            auth_subject=AuthSubject(issuer="https://auth.local", subject=f"sub-{uid}"),
        ),
        membership=WorkspaceMembership(workspace_id=ws_id, user_id=uid, role="OWNER"),
    )


def _budget() -> StageBudgetPolicy:
    return StageBudgetPolicy(
        max_input_bytes=64 * 1024,
        max_calls=3,
        calls_used=0,
        max_repair_attempts=0,
        max_pre_spend_retries=0,
        overage_credit_disabled=True,
        quota_remaining_known_and_sufficient=True,
    )


def _runner() -> BoundedStageRunner:
    return BoundedStageRunner(
        policy_gate=LlmPolicyGate(approval_config=HostedLlmApprovalConfig())
    )


def _make_fact_item(
    *,
    fact_id: str,
    event_id: str,
    fact_type: FactType = FactType.ACTION,
    predicate: str = "implemented",
    object_text: str = "Implemented webhook retry queue with exponential backoff",
    status: FactStatus = FactStatus.SUPPORTED,
    version: int = 1,
    subject_identity_id: str | None = None,
    normalized_value: dict | None = None,
    metric_details: MetricDetailsModel | None = None,
    semantic: FactSemanticAttributes | None = None,
    has_active_direct_support: bool = True,
    has_active_contradicts: bool = False,
    content_unit_id: str = "cu-1",
    revision_id: str = "rev-1",
    fact_support_id: str = "fs-1",
    unit_policy_state: str = "ACTIVE",
    revision_is_tombstone: bool = False,
) -> EligibleFactContextItem:
    return EligibleFactContextItem(
        fact_id=fact_id,
        event_id=event_id,
        version=version,
        fact_type=fact_type,
        predicate=predicate,
        object_text=object_text,
        status=status,
        subject_identity_id=subject_identity_id,
        normalized_value=normalized_value,
        metric_details=metric_details,
        semantic=semantic or FactSemanticAttributes(),
        has_active_direct_support=has_active_direct_support,
        has_active_contradicts=has_active_contradicts,
        active_direct_fact_support_ids=(fact_support_id,) if has_active_direct_support else (),
        active_content_unit_ids=(content_unit_id,) if has_active_direct_support else (),
        active_revision_ids=(revision_id,) if has_active_direct_support else (),
        unit_policy_states={content_unit_id: unit_policy_state},
        revision_tombstone_flags={revision_id: revision_is_tombstone},
        source_resource_id="res-1",
        visibility=ResourceVisibility.PUBLIC,
        input_hash=_HASH_A,
    )


def test_context_builder_filters_non_supported_cross_event_and_enforces_budget() -> None:
    evt_id = "evt-1"
    builder = ClaimContextBuilder()

    # Non-CONFIRMED event must fail closed
    with pytest.raises(ClaimError) as exc_unconfirmed:
        builder.build(
            event_id=evt_id,
            event_status=WorkEventStatus.CANDIDATE,
            anchor_mode=WorkEventAnchorMode.PR,
            is_active_composite_member=False,
            facts=[],
        )
    assert exc_unconfirmed.value.code == "EVENT_NOT_CONFIRMED"

    # Active composite member cannot generate standalone claims
    with pytest.raises(ClaimError) as exc_member:
        builder.build(
            event_id=evt_id,
            event_status=WorkEventStatus.CONFIRMED,
            anchor_mode=WorkEventAnchorMode.PR,
            is_active_composite_member=True,
            facts=[],
        )
    assert exc_member.value.code == "EVENT_IS_COMPOSITE_MEMBER"

    # Mix of eligible SUPPORTED, CANDIDATE, CONFLICTED, STALE, REJECTED, SUPERSEDED, cross-event, tombstoned
    f_ok_metric = _make_fact_item(fact_id="f-2", event_id=evt_id, fact_type=FactType.METRIC)
    f_ok_action = _make_fact_item(fact_id="f-1", event_id=evt_id, fact_type=FactType.ACTION)
    f_dup_action = _make_fact_item(fact_id="f-1", event_id=evt_id, fact_type=FactType.ACTION)
    f_cand = _make_fact_item(fact_id="f-cand", event_id=evt_id, status=FactStatus.CANDIDATE)
    f_conf = _make_fact_item(fact_id="f-conf", event_id=evt_id, status=FactStatus.CONFLICTED)
    f_stale = _make_fact_item(fact_id="f-stale", event_id=evt_id, status=FactStatus.STALE)
    f_rej = _make_fact_item(fact_id="f-rej", event_id=evt_id, status=FactStatus.REJECTED)
    f_sup = _make_fact_item(fact_id="f-sup", event_id=evt_id, status=FactStatus.SUPERSEDED)
    f_other_evt = _make_fact_item(fact_id="f-other", event_id="evt-other")
    f_tomb = _make_fact_item(fact_id="f-tomb", event_id=evt_id, revision_is_tombstone=True)
    f_no_direct = _make_fact_item(fact_id="f-nodir", event_id=evt_id, has_active_direct_support=False)

    bundle = builder.build(
        event_id=evt_id,
        event_status=WorkEventStatus.CONFIRMED,
        anchor_mode=WorkEventAnchorMode.PR,
        is_active_composite_member=False,
        facts=[
            f_ok_metric,
            f_cand,
            f_conf,
            f_stale,
            f_rej,
            f_sup,
            f_other_evt,
            f_tomb,
            f_no_direct,
            f_ok_action,
            f_dup_action,
        ],
    )
    assert bundle.status is ContextBuildStatus.READY
    # Deterministic ordering: (fact_type, fact_id), deduplicated
    assert [f.fact_id for f in bundle.facts] == ["f-1", "f-2"]

    # SCN-01: 0 eligible facts -> EMPTY
    empty_bundle = builder.build(
        event_id=evt_id,
        event_status=WorkEventStatus.CONFIRMED,
        anchor_mode=WorkEventAnchorMode.PR,
        is_active_composite_member=False,
        facts=[f_cand],
    )
    assert empty_bundle.status is ContextBuildStatus.EMPTY
    assert empty_bundle.facts == ()

    # Budget too small for any fact -> MANUAL_ONLY; fits partial subset -> PARTIAL_WITH_LIMIT (never silently drop!)
    manual_bundle = builder.build(
        event_id=evt_id,
        event_status=WorkEventStatus.CONFIRMED,
        anchor_mode=WorkEventAnchorMode.PR,
        is_active_composite_member=False,
        facts=[f_ok_action, f_ok_metric],
        max_bytes=10,
    )
    assert manual_bundle.status is ContextBuildStatus.MANUAL_ONLY
    assert manual_bundle.reason_code == "CONTEXT_BUDGET_EXCEEDED"

    partial_bundle = builder.build(
        event_id=evt_id,
        event_status=WorkEventStatus.CONFIRMED,
        anchor_mode=WorkEventAnchorMode.PR,
        is_active_composite_member=False,
        facts=[f_ok_action, f_ok_metric],
        max_bytes=500,
    )
    assert partial_bundle.status is ContextBuildStatus.PARTIAL_WITH_LIMIT
    assert partial_bundle.reason_code == "CONTEXT_BUDGET_EXCEEDED"
    assert len(partial_bundle.dropped_due_to_budget_fact_ids) == 1


def test_stage3a_generator_hard_off_live_gate_and_hallucination_guard() -> None:
    access = _access()
    evt_id = "evt-1"
    f1 = _make_fact_item(
        fact_id="f-1",
        event_id=evt_id,
        fact_type=FactType.METRIC,
        object_text="Reduced staging benchmark query time by 40% in staging environment",
        semantic=FactSemanticAttributes(
            completion_state=FactCompletionState.COMPLETED,
            environment_scope=FactEnvironmentScope.STAGING_TEST,
        ),
        metric_details=MetricDetailsModel(
            value="40%",
            unit="percent_reduction",
            period="benchmark run",
            source_basis="staging benchmark",
            environment_scope=FactEnvironmentScope.STAGING_TEST,
            measurement_scope="staging query latency",
        ),
    )
    bundle = ClaimContextBuilder().build(
        event_id=evt_id,
        event_status=WorkEventStatus.CONFIRMED,
        anchor_mode=WorkEventAnchorMode.PR,
        is_active_composite_member=False,
        facts=[f1],
    )
    generator = ClaimGenerator(stage_runner=_runner())

    # 1. Default hard-OFF live mode -> MANUAL_ONLY_BLOCKED, 0 external calls
    adapter = SyntheticFixtureStageAdapter(fixtures_by_stage={})
    blocked_res = generator.generate_draft(
        ClaimDraftGenerationRequest(
            access=access,
            context_bundle=bundle,
            budget=_budget(),
            adapter=adapter,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            allow_synthetic_fixture_mode=False,
        )
    )
    assert blocked_res.generation_status == "MANUAL_ONLY_BLOCKED"
    assert blocked_res.draft is None
    assert len(blocked_res.free_alternatives) > 0
    assert adapter.call_count == 0

    # 2. Invented production environment from staging-only facts fails closed (DRAFT_INVALID)
    bad_env_adapter = SyntheticFixtureStageAdapter(
        fixtures_by_stage={
            LlmStageCode.STAGE_3A_CLAIM_GENERATION: lambda inp: {
                "schema_version": "1.0.0",
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "confirmed_event_id": evt_id,
                "input_fact_ids": ["f-1"],
                "claim_version": 1,
                "claim_text": "Reduced production latency by 40% across checkout.",
                "status": "DRAFT",
            }
        }
    )
    inv_res = generator.generate_draft(
        ClaimDraftGenerationRequest(
            access=access,
            context_bundle=bundle,
            budget=_budget(),
            adapter=bad_env_adapter,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
    )
    assert inv_res.generation_status == "DRAFT_INVALID"
    assert inv_res.error_code == "INVENTED_ENVIRONMENT_SCOPE"

    # 3. Valid grounded DRAFT succeeds
    good_adapter = SyntheticFixtureStageAdapter(
        fixtures_by_stage={
            LlmStageCode.STAGE_3A_CLAIM_GENERATION: lambda inp: {
                "schema_version": "1.0.0",
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "confirmed_event_id": evt_id,
                "input_fact_ids": ["f-1"],
                "claim_version": 1,
                "claim_text": "Reduced staging query latency by 40% during staging benchmark.",
                "status": "DRAFT",
            }
        }
    )
    ok_res = generator.generate_draft(
        ClaimDraftGenerationRequest(
            access=access,
            context_bundle=bundle,
            budget=_budget(),
            adapter=good_adapter,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
    )
    assert ok_res.generation_status == "SUCCEEDED"
    assert ok_res.draft is not None
    assert ok_res.draft.status is ClaimStatus.DRAFT
    assert ok_res.draft.claim_version == 1


def test_stage3b_atomizer_run_id_separation_and_stage3c_claim05_claim06_guards() -> None:
    access = _access()
    runner = _runner()
    atomizer = ClaimAtomizer(stage_runner=runner)
    validator = ClaimSupportValidator(stage_runner=runner)

    # Hard guard: atomizer_run_id == generator_run_id -> 422 STAGE_RUN_ID_COLLISION
    with pytest.raises(ClaimError) as coll_3b:
        atomizer.atomize(
            ClaimAtomizationRequest(
                access=access,
                claim_id="claim-1",
                claim_version=1,
                claim_text="Led payments ledger migration and cut checkout-api p99 latency by 35%.",
                generator_run_id="run-same",
                atomizer_run_id="run-same",
                source_resource_id="res-1",
                visibility=ResourceVisibility.PUBLIC,
                budget=_budget(),
                adapter=SyntheticFixtureStageAdapter(fixtures_by_stage={}),
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=True,
            )
        )
    assert coll_3b.value.status_code == 422
    assert coll_3b.value.code == "STAGE_RUN_ID_COLLISION"

    # Valid Stage 3B produces UNSUPPORTED atoms only
    atom_adapter = SyntheticFixtureStageAdapter(
        fixtures_by_stage={
            LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION: lambda inp: {
                "schema_version": "1.0.0",
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "claim_id": "claim-1",
                "claim_version": 1,
                "atoms": [
                    {"ordinal": 0, "atom_type": "ROLE", "atom_text": "Led payments ledger migration"},
                    {"ordinal": 1, "atom_type": "METRIC", "atom_text": "Cut company-wide annual latency by 35%"},
                ],
            }
        }
    )
    atom_res = atomizer.atomize(
        ClaimAtomizationRequest(
            access=access,
            claim_id="claim-1",
            claim_version=1,
            claim_text="Led payments ledger migration and cut company-wide annual latency by 35%.",
            generator_run_id="run-3a",
            atomizer_run_id="run-3b",
            source_resource_id="res-1",
            visibility=ResourceVisibility.PUBLIC,
            budget=_budget(),
            adapter=atom_adapter,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
    )
    assert len(atom_res.atoms) == 2
    assert all(a.initial_status is ClaimAtomStatus.UNSUPPORTED for a in atom_res.atoms)

    # Hard guard Stage 3C: validation_run_id collides with atomizer_run_id -> 422
    with pytest.raises(ClaimError) as coll_3c:
        validator.validate(
            ClaimSupportValidationRequest(
                access=access,
                event_id="evt-1",
                claim_id="claim-1",
                claim_version=1,
                generator_run_id="run-3a",
                atomizer_run_id="run-3b",
                validation_run_id="run-3b",
                atoms=atom_res.atoms,
                fact_snapshots={},
                contributions=(),
                identity_eligibilities={},
                matched_content_unit_ids=frozenset(),
                source_resource_id="res-1",
                visibility=ResourceVisibility.PUBLIC,
                budget=_budget(),
                adapter=SyntheticFixtureStageAdapter(fixtures_by_stage={}),
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=True,
            )
        )
    assert coll_3c.value.status_code == 422
    assert coll_3c.value.code == "STAGE_RUN_ID_COLLISION"

    # Stage 3C adversarial guards:
    # - ROLE atom WITHOUT concrete contribution_support fact fails CLAIM-05 even if adapter claims SUPPORTED
    # - METRIC atom over-generalizing 30-day checkout-api p99 -> company-wide annual fails CLAIM-06 (SCN-16)
    fact_role = FactEvidenceSnapshot(
        fact_id="f-role",
        event_id="evt-1",
        version=1,
        fact_type=FactType.ROLE,
        status=FactStatus.SUPPORTED,
        predicate="role",
        object_text="Tech Lead on payments ledger migration",
        subject_identity_id="id-alex",
        normalized_value={"scope": "INDIVIDUAL"},
        metric_details=None,
        semantic=FactSemanticAttributes(scope=FactAttributionScope.INDIVIDUAL),
        active_direct_support_ids=("fs-role",),
        active_contradicts_support_ids=(),
        support_units={"fs-role": ("cu-1", "rev-1", "ACTIVE", False)},
    )
    fact_metric = FactEvidenceSnapshot(
        fact_id="f-metric",
        event_id="evt-1",
        version=1,
        fact_type=FactType.METRIC,
        status=FactStatus.SUPPORTED,
        predicate="latency_reduction",
        object_text="35% p99 latency reduction on checkout-api over 30 days in production",
        subject_identity_id=None,
        normalized_value={},
        metric_details=MetricDetailsModel(
            value="35%",
            unit="percent_reduction",
            period="30 days (April 2026)",
            source_basis="Datadog APM",
            environment_scope=FactEnvironmentScope.PRODUCTION,
            measurement_scope="checkout-api p99",
        ),
        semantic=FactSemanticAttributes(environment_scope=FactEnvironmentScope.PRODUCTION),
        active_direct_support_ids=("fs-metric",),
        active_contradicts_support_ids=(),
        support_units={"fs-metric": ("cu-1", "rev-1", "ACTIVE", False)},
    )
    val_adapter = SyntheticFixtureStageAdapter(
        fixtures_by_stage={
            LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION: lambda inp: {
                "schema_version": "1.0.0",
                "validation_run_id": inp.context_metadata["validation_run_id"],
                "generator_run_id": "run-3a",
                "atomizer_run_id": "run-3b",
                "claim_id": "claim-1",
                "claim_version": 1,
                "atom_validations": [
                    {
                        "ordinal": 0,
                        "atom_type": "ROLE",
                        "status": "SUPPORTED",
                        "fact_bindings": [
                            {
                                "fact_id": "f-role",
                                "support_type": "DIRECT",
                                "fact_support_ids": ["fs-role"],
                                "source_object_revision_id": "rev-1",
                                "content_unit_id": "cu-1",
                            }
                        ],
                        "role_contribution_grounding": {
                            "subject_identity_id": "id-alex",
                            "confirmed_event_id": "evt-1",
                            "role": "IMPLEMENTED",
                            "active_contribution_support_fact_ids": ["f-missing"],
                        },
                    },
                    {
                        "ordinal": 1,
                        "atom_type": "METRIC",
                        "status": "SUPPORTED",
                        "fact_bindings": [
                            {
                                "fact_id": "f-metric",
                                "support_type": "DIRECT",
                                "fact_support_ids": ["fs-metric"],
                                "source_object_revision_id": "rev-1",
                                "content_unit_id": "cu-1",
                            }
                        ],
                        "metric_grounding": {
                            "value": "35%",
                            "unit": "percent_reduction",
                            "period": "annual",
                            "source_basis": "Datadog APM",
                            "environment_scope": "PRODUCTION",
                            "measurement_scope": "company-wide latency",
                        },
                    },
                ],
                "computed_verification": "SOURCE_VERIFIED",
            }
        }
    )
    elig_ok = IdentityAttributionEligibility(
        identity_id="id-alex",
        identity_type=IdentityType.PERSON,
        identity_status=IdentityStatus.ACTIVE,
        can_support_source_verified=True,
        can_support_user_confirmed=True,
        block_reason_code=None,
        active_verified_mapping_count=1,
        active_user_confirmed_mapping_count=0,
        active_heuristic_mapping_count=0,
        active_conflicted_mapping_count=0,
        revoked_mapping_count=0,
    )
    val_res = validator.validate(
        ClaimSupportValidationRequest(
            access=access,
            event_id="evt-1",
            claim_id="claim-1",
            claim_version=1,
            generator_run_id="run-3a",
            atomizer_run_id="run-3b",
            validation_run_id="run-3c",
            atoms=atom_res.atoms,
            fact_snapshots={"f-role": fact_role, "f-metric": fact_metric},
            contributions=(),  # No SOURCE_SUPPORTED contribution!
            identity_eligibilities={"id-alex": elig_ok},
            matched_content_unit_ids=frozenset({"cu-1"}),
            source_resource_id="res-1",
            visibility=ResourceVisibility.PUBLIC,
            budget=_budget(),
            adapter=val_adapter,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
    )
    assert val_res.atom_results[0].status is ClaimAtomStatus.UNSUPPORTED
    assert val_res.atom_results[1].status is ClaimAtomStatus.UNSUPPORTED
    assert val_res.computed_verification is ClaimVerification.UNVERIFIED
    assert val_res.next_claim_status is ClaimStatus.DRAFT_INVALID


def test_support_graph_epoch_determinism_and_drift_detection() -> None:
    epoch1 = compute_support_graph_epoch(
        claim_id="c-1",
        claim_version=1,
        atoms_state=[
            {
                "atom_id": "a-1",
                "atom_status": "SUPPORTED",
                "supports": [
                    {
                        "cas_id": "cas-1",
                        "cas_status": "ACTIVE",
                        "cas_support_type": "DIRECT",
                        "fact_id": "f-1",
                        "fact_version": 1,
                        "fact_status": "SUPPORTED",
                        "fact_supports": [
                            {
                                "fs_id": "fs-1",
                                "fs_status": "ACTIVE",
                                "fs_support_type": "DIRECT",
                                "cu_id": "cu-1",
                                "cu_hash": _HASH_A,
                                "cu_policy_state": "ACTIVE",
                                "rev_id": "rev-1",
                                "rev_hash": _HASH_B,
                                "rev_is_tombstone": False,
                            }
                        ],
                    }
                ],
            }
        ],
        contribution_states=[],
        identity_states=[],
    )
    epoch2 = compute_support_graph_epoch(
        claim_id="c-1",
        claim_version=1,
        atoms_state=[
            {
                "atom_id": "a-1",
                "atom_status": "SUPPORTED",
                "supports": [
                    {
                        "cas_id": "cas-1",
                        "cas_status": "ACTIVE",
                        "cas_support_type": "DIRECT",
                        "fact_id": "f-1",
                        "fact_version": 1,
                        "fact_status": "STALE",
                        "fact_supports": [
                            {
                                "fs_id": "fs-1",
                                "fs_status": "ACTIVE",
                                "fs_support_type": "DIRECT",
                                "cu_id": "cu-1",
                                "cu_hash": _HASH_A,
                                "cu_policy_state": "ACTIVE",
                                "rev_id": "rev-1",
                                "rev_hash": _HASH_B,
                                "rev_is_tombstone": False,
                            }
                        ],
                    }
                ],
            }
        ],
        contribution_states=[],
        identity_states=[],
    )
    assert epoch1.startswith("sha256:") and len(epoch1) == 71
    assert epoch1 != epoch2


def test_stage3c_adversarial_scn04_through_scn14_matrix() -> None:
    """Explicit unit coverage of SCN-04 (CONTEXT-only), SCN-05 (CONTRADICTS), SCN-06 (PLANNED),
    SCN-07 (NEGATED), SCN-08 (TEAM), SCN-09 (STAGING_TEST), SCN-10 (CORRELATION), and SCN-14 (10x).
    """
    access = _access()
    validator = ClaimSupportValidator(stage_runner=_runner())

    def _run_case(
        *,
        atom_type: ClaimAtomType,
        atom_text: str,
        fact: FactEvidenceSnapshot,
        support_type: SupportType = SupportType.DIRECT,
        metric_grounding: dict | None = None,
    ) -> ClaimAtomStatus:
        from career_memory_api.claims import ClaimAtomDraftItem

        atom_val: dict = {
            "ordinal": 0,
            "atom_type": atom_type.value,
            "status": "SUPPORTED",
            "fact_bindings": [
                {
                    "fact_id": fact.fact_id,
                    "support_type": support_type.value,
                    "fact_support_ids": [list(fact.support_units.keys())[0]],
                    "source_object_revision_id": "rev-1",
                    "content_unit_id": "cu-1",
                }
            ],
        }
        if metric_grounding is not None:
            atom_val["metric_grounding"] = metric_grounding

        adapter = SyntheticFixtureStageAdapter(
            fixtures_by_stage={
                LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION: lambda inp: {
                    "schema_version": "1.0.0",
                    "validation_run_id": inp.context_metadata["validation_run_id"],
                    "generator_run_id": "run-3a",
                    "atomizer_run_id": "run-3b",
                    "claim_id": "claim-adv",
                    "claim_version": 1,
                    "atom_validations": [atom_val],
                    "computed_verification": "SOURCE_VERIFIED",
                }
            }
        )
        res = validator.validate(
            ClaimSupportValidationRequest(
                access=access,
                event_id="evt-1",
                claim_id="claim-adv",
                claim_version=1,
                generator_run_id="run-3a",
                atomizer_run_id="run-3b",
                validation_run_id=f"run-3c-{uuid.uuid4()}",
                atoms=(ClaimAtomDraftItem(ordinal=0, atom_type=atom_type, atom_text=atom_text),),
                fact_snapshots={fact.fact_id: fact},
                contributions=(),
                identity_eligibilities={},
                matched_content_unit_ids=frozenset({"cu-1"}),
                source_resource_id="res-1",
                visibility=ResourceVisibility.PUBLIC,
                budget=_budget(),
                adapter=adapter,
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=True,
            )
        )
        return res.atom_results[0].status

    def _base_fact(
        *,
        fact_type: FactType = FactType.ACTION,
        semantic: FactSemanticAttributes | None = None,
        metric: MetricDetailsModel | None = None,
        contra: tuple[str, ...] = (),
    ) -> FactEvidenceSnapshot:
        return FactEvidenceSnapshot(
            fact_id="f-adv",
            event_id="evt-1",
            version=1,
            fact_type=fact_type,
            status=FactStatus.SUPPORTED,
            predicate="worked_on",
            object_text="Fact text",
            subject_identity_id=None,
            normalized_value={},
            metric_details=metric,
            semantic=semantic or FactSemanticAttributes(),
            active_direct_support_ids=("fs-1",),
            active_contradicts_support_ids=contra,
            support_units={"fs-1": ("cu-1", "rev-1", "ACTIVE", False)},
        )

    # SCN-04: CONTEXT-only binding -> UNSUPPORTED
    assert (
        _run_case(
            atom_type=ClaimAtomType.ACTION,
            atom_text="Migrated billing job queue",
            fact=_base_fact(),
            support_type=SupportType.CONTEXT,
        )
        is ClaimAtomStatus.UNSUPPORTED
    )
    # SCN-05: Contradicted fact -> UNSUPPORTED
    assert (
        _run_case(
            atom_type=ClaimAtomType.RESULT,
            atom_text="Eliminated failover downtime",
            fact=_base_fact(fact_type=FactType.RESULT, contra=("fs-contra",)),
        )
        is ClaimAtomStatus.UNSUPPORTED
    )
    # SCN-06: PLANNED fact cannot support completed claim -> UNSUPPORTED
    assert (
        _run_case(
            atom_type=ClaimAtomType.ACTION,
            atom_text="Completed Q4 storage scaling migration",
            fact=_base_fact(
                semantic=FactSemanticAttributes(completion_state=FactCompletionState.PLANNED)
            ),
        )
        is ClaimAtomStatus.UNSUPPORTED
    )
    # SCN-07: NEGATED fact cannot support positive claim -> UNSUPPORTED
    assert (
        _run_case(
            atom_type=ClaimAtomType.ACTION,
            atom_text="Shipped canary autoscaler to production",
            fact=_base_fact(
                semantic=FactSemanticAttributes(completion_state=FactCompletionState.NEGATED)
            ),
        )
        is ClaimAtomStatus.UNSUPPORTED
    )
    # SCN-08: TEAM fact cannot support individual leadership claim -> UNSUPPORTED
    assert (
        _run_case(
            atom_type=ClaimAtomType.ACTION,
            atom_text="Personally led the monorepo CI pipeline rebuild",
            fact=_base_fact(semantic=FactSemanticAttributes(scope=FactAttributionScope.TEAM)),
        )
        is ClaimAtomStatus.UNSUPPORTED
    )
    # SCN-09: STAGING_TEST fact cannot support PRODUCTION claim -> UNSUPPORTED
    assert (
        _run_case(
            atom_type=ClaimAtomType.RESULT,
            atom_text="Reduced production query latency by 45%",
            fact=_base_fact(
                fact_type=FactType.RESULT,
                semantic=FactSemanticAttributes(environment_scope=FactEnvironmentScope.STAGING_TEST),
            ),
        )
        is ClaimAtomStatus.UNSUPPORTED
    )
    # SCN-10: OBSERVED_CORRELATION cannot support direct causal claim -> UNSUPPORTED
    assert (
        _run_case(
            atom_type=ClaimAtomType.RESULT,
            atom_text="Onboarding checklist directly caused a 20% conversion jump",
            fact=_base_fact(
                fact_type=FactType.RESULT,
                semantic=FactSemanticAttributes(causal_basis=FactCausalBasis.OBSERVED_CORRELATION),
            ),
        )
        is ClaimAtomStatus.UNSUPPORTED
    )
    # SCN-14: 10x numeric distortion -> UNSUPPORTED
    m14 = MetricDetailsModel(
        value="5%",
        unit="percent_reduction",
        period="H1 2026",
        source_basis="Project Alpha benchmark",
        environment_scope=FactEnvironmentScope.PRODUCTION,
        measurement_scope="Project Alpha API",
    )
    assert (
        _run_case(
            atom_type=ClaimAtomType.METRIC,
            atom_text="Reduced Project Alpha API latency by 50%",
            fact=_base_fact(fact_type=FactType.METRIC, metric=m14),
            metric_grounding=m14.model_dump(mode="json"),
        )
        is ClaimAtomStatus.UNSUPPORTED
    )

