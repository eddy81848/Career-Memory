"""Additional adversarial, boundary, serialization, and leak-audit tests for T-408 / INF-08."""

from __future__ import annotations

import pickle

import pytest

from career_memory_api.llm_gate import (
    BoundedStageRunner,
    HostedLlmApprovalConfig,
    InMemorySecretVault,
    LlmGateReasonCode,
    LlmLifecycleError,
    LlmPolicyGate,
    LlmProviderMode,
    LlmStageCode,
    PolicyGatedProviderStageAdapter,
    PolicyGateRequest,
    RawProviderStageResponse,
    StageAdapterInput,
    StageBudgetPolicy,
    StageExecutionRequest,
    StageRunStatus,
    SubscriptionConnectionLifecycleManager,
    SubscriptionOAuthConfig,
    SyntheticFixtureStageAdapter,
    assert_no_raw_text_or_secret_keys,
    assert_not_synthetic_for_live_gate,
)
from career_memory_api.schema_contracts.enums import ResourceVisibility

from .conftest import (
    ALLOWED_REDIRECT_URI,
    EXPECTED_OIDC_AUDIENCE,
    EXPECTED_OIDC_ISSUER,
    PLAN_INFERENCE_SCOPE,
    SOURCE_RESOURCE_A,
    FakeClock,
    make_operation_memory_resolution,
    make_valid_stage_payloads,
)
from .test_oauth_lifecycle import MockSubscriptionExchanger


def test_expired_oauth_state_and_cross_workspace_state_rejected(
    fake_clock: FakeClock, owner_a_access, owner_b_access
) -> None:
    vault = InMemorySecretVault()
    manager = SubscriptionConnectionLifecycleManager(
        config=SubscriptionOAuthConfig(
            allowed_redirect_uris=frozenset({ALLOWED_REDIRECT_URI}),
            expected_oidc_issuer=EXPECTED_OIDC_ISSUER,
            expected_oidc_audience=EXPECTED_OIDC_AUDIENCE,
            required_plan_inference_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
            state_ttl_seconds=60,
        ),
        secret_vault=vault,
        clock=fake_clock,
    )

    # Cross-workspace state replay attempt
    start1 = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    exchanger = MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start1.nonce])
    with pytest.raises(LlmLifecycleError) as err1:
        manager.complete_callback(
            owner_b_access,
            state=start1.state,
            code="code-cross-ws",
            redirect_uri=ALLOWED_REDIRECT_URI,
            exchanger=exchanger,
        )
    assert err1.value.code is LlmGateReasonCode.OAUTH_STATE_INVALID_OR_REPLAYED
    assert exchanger.exchange_calls == 0

    # Expired state
    fake_clock.advance(seconds=65)
    with pytest.raises(LlmLifecycleError) as err2:
        manager.complete_callback(
            owner_a_access,
            state=start1.state,
            code="code-expired",
            redirect_uri=ALLOWED_REDIRECT_URI,
            exchanger=exchanger,
        )
    assert err2.value.code is LlmGateReasonCode.OAUTH_STATE_INVALID_OR_REPLAYED


def test_expired_connection_token_blocks_policy_gate_with_zero_calls(
    fake_clock: FakeClock, owner_a_access
) -> None:
    vault = InMemorySecretVault()
    manager = SubscriptionConnectionLifecycleManager(
        config=SubscriptionOAuthConfig(
            allowed_redirect_uris=frozenset({ALLOWED_REDIRECT_URI}),
            expected_oidc_issuer=EXPECTED_OIDC_ISSUER,
            expected_oidc_audience=EXPECTED_OIDC_AUDIENCE,
            required_plan_inference_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        secret_vault=vault,
        clock=fake_clock,
    )
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-exp",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce]),
    )
    consent = manager.grant_external_send_consent(
        owner_a_access,
        source_resource_id=SOURCE_RESOURCE_A,
        consent_version="v1",
        acknowledged_public_unmasked_send=True,
        acknowledged_third_party_non_recallable=True,
    )
    fake_clock.advance(seconds=4000)

    gate = LlmPolicyGate(
        approval_config=HostedLlmApprovalConfig(
            hosted_deployment_approved=True,
            zero_overage_guaranteed=True,
            od002_data_policy_verified=True,
            od014_token_store_verified=True,
        ),
        required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        clock=fake_clock,
    )
    decision = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=64,
            budget=StageBudgetPolicy(
                max_input_bytes=4096,
                max_calls=2,
                overage_credit_disabled=True,
                quota_remaining_known_and_sufficient=True,
            ),
            connection=conn,
            consent=consent,
        )
    )
    assert decision.allowed is False
    assert decision.reason_code is LlmGateReasonCode.CONNECTION_TOKEN_EXPIRED
    assert decision.external_call_count == 0


def test_budget_repair_cap_above_one_rejected_and_ephemeral_input_not_picklable() -> None:
    with pytest.raises(ValueError, match="max_repair_attempts"):
        StageBudgetPolicy(max_input_bytes=1024, max_calls=3, max_repair_attempts=2)

    inp = StageAdapterInput(
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        schema_file="fact_candidate.v1.schema.json",
        operation_id="op-1",
        workspace_id="ws-1",
        source_object_revision_id="rev-1",
        content_unit_id="unit-1",
        expected_revision_hash="sha256:" + "a" * 64,
        span_sha256="sha256:" + "b" * 64,
        ephemeral_source_text="ephemeral raw text must not serialize",
    )
    assert "ephemeral raw text" not in repr(inp)
    with pytest.raises(TypeError, match="ephemeral"):
        pickle.dumps(inp)


def test_mid_call_workspace_membership_revocation_cancels_commit_and_vault_token_in_metadata_blocked(
    fake_clock: FakeClock, owner_a_access
) -> None:
    vault = InMemorySecretVault()
    manager = SubscriptionConnectionLifecycleManager(
        config=SubscriptionOAuthConfig(
            allowed_redirect_uris=frozenset({ALLOWED_REDIRECT_URI}),
            expected_oidc_issuer=EXPECTED_OIDC_ISSUER,
            expected_oidc_audience=EXPECTED_OIDC_AUDIENCE,
            required_plan_inference_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        secret_vault=vault,
        clock=fake_clock,
    )
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-mem-race",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce]),
    )
    manager.grant_external_send_consent(
        owner_a_access,
        source_resource_id=SOURCE_RESOURCE_A,
        consent_version="v1",
        acknowledged_public_unmasked_send=True,
        acknowledged_third_party_non_recallable=True,
    )
    approval = HostedLlmApprovalConfig(
        hosted_deployment_approved=True,
        zero_overage_guaranteed=True,
        od002_data_policy_verified=True,
        od014_token_store_verified=True,
    )
    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
            clock=fake_clock,
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )

    # 1) Active vault token leaked inside context_metadata is blocked before any call
    res1, rev_hash1, span_hash1 = make_operation_memory_resolution()
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash1,
    )
    known = manager.get_known_secret_values_for_connection(conn.connection_id)
    assert len(known) == 2

    env_leak = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res1,
            expected_revision_hash=rev_hash1,
            expected_span_sha256=span_hash1,
            budget=StageBudgetPolicy(
                max_input_bytes=4096,
                max_calls=2,
                overage_credit_disabled=True,
                quota_remaining_known_and_sufficient=True,
            ),
            adapter=PolicyGatedProviderStageAdapter(
                transport=lambda _: RawProviderStageResponse(payload={}, stream_completed=True)
            ),
            job_reference=job_ref,
            context_metadata={"note": f"oops {known[0]}"},
        )
    )
    assert env_leak.status is StageRunStatus.BLOCKED
    assert env_leak.reason_code is LlmGateReasonCode.OPERATIONAL_SECRET_IN_PAYLOAD_BLOCKED
    assert env_leak.external_call_count == 0

    # 2) Workspace membership revoked mid-call -> pre-commit fence cancels commit
    res2, rev_hash2, span_hash2 = make_operation_memory_resolution()
    assert res2.ephemeral_span is not None
    valid_payload = make_valid_stage_payloads(locator_dict=res2.ephemeral_span.locator.to_dict())[
        "STAGE_2A_FACT_CANDIDATE_EXTRACTION"
    ]
    membership_state = {"active": True}

    def revoke_membership_during_call(_inp: StageAdapterInput) -> RawProviderStageResponse:
        membership_state["active"] = False
        return RawProviderStageResponse(payload=valid_payload, stream_completed=True)

    env_mem = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res2,
            expected_revision_hash=rev_hash2,
            expected_span_sha256=span_hash2,
            budget=StageBudgetPolicy(
                max_input_bytes=4096,
                max_calls=2,
                overage_credit_disabled=True,
                quota_remaining_known_and_sufficient=True,
            ),
            adapter=PolicyGatedProviderStageAdapter(transport=revoke_membership_during_call),
            job_reference=job_ref,
            verify_workspace_membership_active=lambda _acc: membership_state["active"],
        )
    )
    assert env_mem.status is StageRunStatus.CANCELED_PRE_COMMIT_FENCE
    assert env_mem.committed_allowed is False
    assert env_mem.can_promote_to_supported is False
    assert_no_raw_text_or_secret_keys(env_mem.to_audit_dict(), context="env_mem.to_audit_dict")


def test_synthetic_mode_requires_explicit_opt_in_and_live_gate_rejects_blocked_runs(
    fake_clock: FakeClock, owner_a_access
) -> None:
    res, rev_hash, span_hash = make_operation_memory_resolution()
    assert res.ephemeral_span is not None
    fixtures = make_valid_stage_payloads(locator_dict=res.ephemeral_span.locator.to_dict())
    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(approval_config=HostedLlmApprovalConfig(), clock=fake_clock),
        clock=fake_clock,
    )
    env = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=StageBudgetPolicy(max_input_bytes=4096, max_calls=2),
            adapter=SyntheticFixtureStageAdapter(fixtures_by_stage=fixtures),
            allow_synthetic_fixture_mode=False,
        )
    )
    assert env.status is StageRunStatus.BLOCKED
    assert env.reason_code is LlmGateReasonCode.SYNTHETIC_MODE_NOT_ENABLED
    assert env.live_pilot_gate_eligible is False
    with pytest.raises(ValueError):
        assert_not_synthetic_for_live_gate(env)


def test_cross_wired_adapter_and_default_off_live_runner_block_with_zero_calls(
    fake_clock: FakeClock, owner_a_access
) -> None:
    res, rev_hash, span_hash = make_operation_memory_resolution()
    assert res.ephemeral_span is not None
    fixtures = make_valid_stage_payloads(locator_dict=res.ephemeral_span.locator.to_dict())

    # 1) Default hard-OFF config blocks PolicyGatedProviderStageAdapter with 0 calls
    called = 0

    def transport(_: StageAdapterInput) -> RawProviderStageResponse:
        nonlocal called
        called += 1
        return RawProviderStageResponse(payload=fixtures["STAGE_2A_FACT_CANDIDATE_EXTRACTION"])

    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(approval_config=HostedLlmApprovalConfig(), clock=fake_clock),
        clock=fake_clock,
    )
    env_off = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=StageBudgetPolicy(
                max_input_bytes=4096,
                max_calls=2,
                overage_credit_disabled=True,
                quota_remaining_known_and_sufficient=True,
            ),
            adapter=PolicyGatedProviderStageAdapter(transport=transport),
        )
    )
    assert env_off.status is StageRunStatus.BLOCKED
    assert env_off.reason_code is LlmGateReasonCode.HOSTED_DEPLOYMENT_NOT_APPROVED
    assert called == 0
    assert env_off.external_call_count == 0

    # 2) SYNTHETIC_FIXTURE_OFFLINE cannot execute against PolicyGatedProviderStageAdapter
    res2, rev_hash2, span_hash2 = make_operation_memory_resolution()
    env_cross = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res2,
            expected_revision_hash=rev_hash2,
            expected_span_sha256=span_hash2,
            budget=StageBudgetPolicy(max_input_bytes=4096, max_calls=2),
            adapter=PolicyGatedProviderStageAdapter(transport=transport),
            allow_synthetic_fixture_mode=True,
        )
    )
    assert env_cross.status is StageRunStatus.BLOCKED
    assert env_cross.reason_code is LlmGateReasonCode.PROHIBITED_PROVIDER_MODE
    assert called == 0

