"""INF-08 bounded stage execution, unmasked public passthrough, concurrency, and leak-audit tests."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from career_memory_api.llm_gate.models import (
    HostedLlmApprovalConfig,
    LivePilotGateEligibilityError,
    LlmGateReasonCode,
    LlmProviderMode,
    LlmStageCode,
    StageBudgetPolicy,
    StageRunStatus,
    assert_no_raw_text_or_secret_keys,
)
from career_memory_api.llm_gate.oauth_lifecycle import (
    InMemorySecretVault,
    SubscriptionConnectionLifecycleManager,
    SubscriptionOAuthConfig,
)
from career_memory_api.llm_gate.policy_gate import LlmPolicyGate
from career_memory_api.llm_gate.stage_runner import (
    BoundedStageRunner,
    PolicyGatedProviderStageAdapter,
    ProviderTransportErrorKind,
    RawProviderStageResponse,
    StageAdapterInput,
    StageExecutionRequest,
    SyntheticFixtureStageAdapter,
    assert_not_synthetic_for_live_gate,
)
from career_memory_api.resolver.models import ResolutionStatus
from career_memory_api.schema_contracts.enums import ResourceVisibility

from .conftest import (
    ALLOWED_REDIRECT_URI,
    CONTENT_UNIT_A,
    EXPECTED_OIDC_AUDIENCE,
    EXPECTED_OIDC_ISSUER,
    FAKE_PUBLIC_SECRET_MARKER,
    PLAN_INFERENCE_SCOPE,
    SOURCE_RESOURCE_A,
    SOURCE_REVISION_A,
    FakeClock,
    make_operation_memory_resolution,
    make_valid_stage_payloads,
)
from .test_oauth_lifecycle import MockSubscriptionExchanger, RecordingRemoteRevoker


def _setup_approved_subscription(
    clock: FakeClock, owner_a_access
) -> tuple[SubscriptionConnectionLifecycleManager, HostedLlmApprovalConfig, Any, Any]:
    vault = InMemorySecretVault()
    oauth_cfg = SubscriptionOAuthConfig(
        allowed_redirect_uris=frozenset({ALLOWED_REDIRECT_URI}),
        expected_oidc_issuer=EXPECTED_OIDC_ISSUER,
        expected_oidc_audience=EXPECTED_OIDC_AUDIENCE,
        required_plan_inference_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )
    manager = SubscriptionConnectionLifecycleManager(
        config=oauth_cfg, secret_vault=vault, clock=clock
    )
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-stage-001",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=clock, nonce_holder=[start.nonce]),
    )
    consent = manager.grant_external_send_consent(
        owner_a_access,
        source_resource_id=SOURCE_RESOURCE_A,
        consent_version="consent-v1",
        acknowledged_public_unmasked_send=True,
        acknowledged_third_party_non_recallable=True,
    )
    approval = HostedLlmApprovalConfig(
        hosted_deployment_approved=True,
        zero_overage_guaranteed=True,
        od002_data_policy_verified=True,
        od014_token_store_verified=True,
    )
    return manager, approval, conn, consent


def _safe_budget(**overrides: object) -> StageBudgetPolicy:
    base: dict[str, object] = {
        "max_input_bytes": 16_384,
        "max_calls": 3,
        "calls_used": 0,
        "max_repair_attempts": 1,
        "overage_credit_disabled": True,
        "quota_remaining_known_and_sufficient": True,
        "quota_remaining_units": 10,
        "estimated_required_units": 1,
    }
    base.update(overrides)
    return StageBudgetPolicy(**base)  # type: ignore[arg-type]


def test_all_five_stages_execute_on_synthetic_fixture_and_block_live_pilot_gate(
    fake_clock: FakeClock, owner_a_access
) -> None:
    resolution, rev_hash, span_hash = make_operation_memory_resolution()
    assert resolution.ephemeral_span is not None
    fixtures = make_valid_stage_payloads(locator_dict=resolution.ephemeral_span.locator.to_dict())

    manager, _, _, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    # Default hard-OFF config still allows explicit offline synthetic fixture mode!
    gate = LlmPolicyGate(
        approval_config=HostedLlmApprovalConfig(),
        required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )
    adapter = SyntheticFixtureStageAdapter(fixtures_by_stage=fixtures)
    runner = BoundedStageRunner(policy_gate=gate, lifecycle_manager=manager, clock=fake_clock)

    for stage_code in LlmStageCode:
        res_i, r_hash, s_hash = make_operation_memory_resolution()
        envelope = runner.run_stage(
            StageExecutionRequest(
                access=owner_a_access,
                stage_code=stage_code,
                provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                source_resource_id=SOURCE_RESOURCE_A,
                visibility=ResourceVisibility.PUBLIC,
                resolution=res_i,
                expected_revision_hash=r_hash,
                expected_span_sha256=s_hash,
                budget=_safe_budget(),
                adapter=adapter,
                allow_synthetic_fixture_mode=True,
            )
        )
        assert envelope.status is StageRunStatus.SUCCEEDED
        assert envelope.committed_allowed is True
        assert envelope.validated_payload == fixtures[stage_code.value]
        assert envelope.execution_origin == "SYNTHETIC_FIXTURE"
        assert envelope.live_pilot_gate_eligible is False
        assert envelope.external_call_count == 0
        # Ephemeral resolution cleared on exit
        assert res_i.closed is True

        with pytest.raises(LivePilotGateEligibilityError) as gate_err:
            assert_not_synthetic_for_live_gate(envelope)
        assert gate_err.value.code is LlmGateReasonCode.SYNTHETIC_NOT_ELIGIBLE_FOR_LIVE_GATE


def test_public_unmasked_passthrough_preserves_fake_secret_marker_and_strips_operational_creds(
    fake_clock: FakeClock, owner_a_access, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    public_pr_diff = (
        "PR #42: Rotate example fixture\n"
        f"+SAMPLE_DOC_TOKEN = \"{FAKE_PUBLIC_SECRET_MARKER}\"\n"
        "Reduced cache warm-up latency by 35%.\n"
    )
    resolution, rev_hash, span_hash = make_operation_memory_resolution(text=public_pr_diff)
    assert resolution.ephemeral_span is not None
    fixtures = make_valid_stage_payloads(locator_dict=resolution.ephemeral_span.locator.to_dict())

    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )

    observed_text: list[str] = []
    observed_headers: list[dict[str, str]] = []

    def mock_transport(adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        # Ephemeral input must not be serializable or leak in repr
        assert FAKE_PUBLIC_SECRET_MARKER not in repr(adapter_input)
        assert adapter_input.tools_enabled is False
        observed_text.append(adapter_input.ephemeral_source_text)
        observed_headers.append(dict(adapter_input.sanitized_headers))
        return RawProviderStageResponse(
            payload=fixtures["STAGE_2A_FACT_CANDIDATE_EXTRACTION"],
            stream_completed=True,
        )

    adapter = PolicyGatedProviderStageAdapter(transport=mock_transport)
    gate = LlmPolicyGate(
        approval_config=approval,
        required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )
    runner = BoundedStageRunner(policy_gate=gate, lifecycle_manager=manager, clock=fake_clock)

    envelope = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=resolution,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(),
            adapter=adapter,
            job_reference=job_ref,
            extra_headers={
                "X-Trace-Id": "trace-123",
                "Authorization": "Bearer super-secret-server-jwt",
                "Cookie": "session=secret-cookie-value",
                "Set-Cookie": "sid=other-secret",
            },
            context_metadata={"event_id": "evt-001", "unit_type": "PR_BODY_SECTION"},
        )
    )

    assert envelope.status is StageRunStatus.SUCCEEDED
    assert envelope.execution_origin == "APPROVED_SUBSCRIPTION"
    assert envelope.live_pilot_gate_eligible is True
    assert envelope.external_call_count == 1
    # 1. Public source text passed AS-IS with fake secret marker UNMASKED!
    assert observed_text == [public_pr_diff]
    assert FAKE_PUBLIC_SECRET_MARKER in observed_text[0]
    # 2. Operational credential headers strictly stripped before adapter invocation!
    assert observed_headers == [{"X-Trace-Id": "trace-123"}]
    # 3. Zero persistence of raw source text or operational secrets in envelope, audit dict, or logs!
    assert resolution.closed is True
    audit = envelope.to_audit_dict()
    assert_no_raw_text_or_secret_keys(audit, context="StageRunEnvelope.to_audit_dict")
    audit_json = json.dumps(audit, sort_keys=True)
    assert FAKE_PUBLIC_SECRET_MARKER not in audit_json
    assert "super-secret-server-jwt" not in audit_json
    assert "secret-cookie-value" not in audit_json
    assert "at_secret_exchange" not in audit_json
    assert FAKE_PUBLIC_SECRET_MARKER not in caplog.text
    assert "super-secret-server-jwt" not in caplog.text


def test_operational_secret_in_context_metadata_blocked_with_zero_calls(
    fake_clock: FakeClock, owner_a_access
) -> None:
    resolution, rev_hash, span_hash = make_operation_memory_resolution()
    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )
    called = 0

    def transport(_: StageAdapterInput) -> RawProviderStageResponse:
        nonlocal called
        called += 1
        return RawProviderStageResponse(payload={}, stream_completed=True)

    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )
    envelope = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=resolution,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(transport=transport),
            job_reference=job_ref,
            context_metadata={"db_url": "postgresql://cm_api_app:pw@localhost:5432/cm"},
        )
    )
    assert envelope.status is StageRunStatus.BLOCKED
    assert envelope.reason_code is LlmGateReasonCode.OPERATIONAL_SECRET_IN_PAYLOAD_BLOCKED
    assert called == 0
    assert envelope.external_call_count == 0


def test_plan_c_resolution_mismatch_or_non_matched_blocks_with_zero_calls(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )
    called = 0

    def transport(_: StageAdapterInput) -> RawProviderStageResponse:
        nonlocal called
        called += 1
        return RawProviderStageResponse(payload={}, stream_completed=True)

    # Case 1: Resolution status != MATCHED
    bad_res, rev_hash, span_hash = make_operation_memory_resolution(
        status=ResolutionStatus.REVISION_MISMATCH
    )
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )
    env1 = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=bad_res,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(transport=transport),
            job_reference=job_ref,
        )
    )
    assert env1.status is StageRunStatus.BLOCKED
    assert env1.reason_code is LlmGateReasonCode.RESOLUTION_NOT_MATCHED
    assert called == 0

    # Case 2: expected_span_sha256 mismatch
    good_res, rev_hash2, _ = make_operation_memory_resolution()
    env2 = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=good_res,
            expected_revision_hash=rev_hash2,
            expected_span_sha256="sha256:" + "0" * 64,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(transport=transport),
            job_reference=job_ref,
        )
    )
    assert env2.status is StageRunStatus.BLOCKED
    assert env2.reason_code is LlmGateReasonCode.RESOLUTION_HASH_MISMATCH
    assert called == 0


def test_incomplete_stream_and_unsupported_tool_calls_fail_closed(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )

    # Partial/aborted stream -> STREAM_INCOMPLETE
    res1, rev_hash, span_hash = make_operation_memory_resolution()
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )
    env_stream = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res1,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(
                transport=lambda _: RawProviderStageResponse(
                    payload={"partial": True},
                    stream_completed=False,
                    transport_error=ProviderTransportErrorKind.STREAM_TRUNCATED,
                )
            ),
            job_reference=job_ref,
        )
    )
    assert env_stream.status is StageRunStatus.FAILED_STREAM_INCOMPLETE
    assert env_stream.reason_code is LlmGateReasonCode.STREAM_INCOMPLETE
    assert env_stream.validated_payload is None
    assert env_stream.committed_allowed is False
    assert env_stream.can_promote_to_supported is False

    # Tool calls / unsupported output -> UNSUPPORTED_OUTPUT_OR_TOOL_SIDE_EFFECT
    res2, rev_hash2, span_hash2 = make_operation_memory_resolution()
    env_tool = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res2,
            expected_revision_hash=rev_hash2,
            expected_span_sha256=span_hash2,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(
                transport=lambda _: RawProviderStageResponse(
                    payload={"tool_calls": [{"name": "shell_exec"}]},
                    stream_completed=True,
                    contains_tool_calls=True,
                )
            ),
            job_reference=job_ref,
        )
    )
    assert env_tool.status is StageRunStatus.FAILED_UNSUPPORTED_OUTPUT
    assert env_tool.reason_code is LlmGateReasonCode.UNSUPPORTED_OUTPUT_OR_TOOL_SIDE_EFFECT
    assert env_tool.validated_payload is None
    assert env_tool.committed_allowed is False
    assert env_tool.can_promote_to_supported is False


def test_schema_invalid_allows_at_most_one_bounded_repair_then_fails_closed(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )

    # 1) Repair succeeds on attempt #2
    res1, rev_hash, span_hash = make_operation_memory_resolution()
    assert res1.ephemeral_span is not None
    valid_payload = make_valid_stage_payloads(locator_dict=res1.ephemeral_span.locator.to_dict())[
        "STAGE_2A_FACT_CANDIDATE_EXTRACTION"
    ]
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )
    attempts: list[int] = []

    def repair_succeeds_transport(inp: StageAdapterInput) -> RawProviderStageResponse:
        attempts.append(inp.repair_attempt)
        if inp.repair_attempt == 0:
            return RawProviderStageResponse(
                payload={"schema_version": "1.0.0", "status": "SOURCE_VERIFIED"},
                stream_completed=True,
            )
        return RawProviderStageResponse(payload=valid_payload, stream_completed=True)

    env_repaired = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res1,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(max_calls=2, max_repair_attempts=1),
            adapter=PolicyGatedProviderStageAdapter(transport=repair_succeeds_transport),
            job_reference=job_ref,
        )
    )
    assert env_repaired.status is StageRunStatus.SUCCEEDED
    assert env_repaired.repair_attempts_used == 1
    assert env_repaired.external_call_count == 2
    assert attempts == [0, 1]

    # 2) Both initial and repair invalid -> fail closed DRAFT_INVALID, never >1 repair
    res2, rev_hash2, span_hash2 = make_operation_memory_resolution()
    bad_attempts: list[int] = []

    def always_bad_transport(inp: StageAdapterInput) -> RawProviderStageResponse:
        bad_attempts.append(inp.repair_attempt)
        return RawProviderStageResponse(payload={"bad": True}, stream_completed=True)

    env_failed = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res2,
            expected_revision_hash=rev_hash2,
            expected_span_sha256=span_hash2,
            budget=_safe_budget(max_calls=5, max_repair_attempts=1),
            adapter=PolicyGatedProviderStageAdapter(transport=always_bad_transport),
            job_reference=job_ref,
        )
    )
    assert env_failed.status is StageRunStatus.DRAFT_INVALID
    assert env_failed.reason_code in {
        LlmGateReasonCode.AI_SCHEMA_INVALID,
        LlmGateReasonCode.DRAFT_INVALID,
    }
    assert env_failed.repair_attempts_used == 1
    assert env_failed.external_call_count == 2
    assert bad_attempts == [0, 1]
    assert env_failed.committed_allowed is False
    assert env_failed.can_promote_to_supported is False


def test_ambiguous_post_send_timeout_never_auto_retries_vs_pre_send_429_retries(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )

    # 1) Ambiguous post-send timeout -> MUST NOT auto-retry (external_call_count == 1)
    res1, rev_hash, span_hash = make_operation_memory_resolution()
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )
    timeout_calls = 0

    def timeout_transport(_: StageAdapterInput) -> RawProviderStageResponse:
        nonlocal timeout_calls
        timeout_calls += 1
        return RawProviderStageResponse(
            payload=None,
            stream_completed=False,
            transport_error=ProviderTransportErrorKind.AMBIGUOUS_POST_SEND_TIMEOUT,
            request_transmitted=True,
            error_message="Socket read timed out after request transmitted",
        )

    env_timeout = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res1,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(max_calls=3),
            adapter=PolicyGatedProviderStageAdapter(transport=timeout_transport),
            job_reference=job_ref,
        )
    )
    assert env_timeout.status is StageRunStatus.FAILED_AMBIGUOUS_TIMEOUT
    assert env_timeout.reason_code is LlmGateReasonCode.AMBIGUOUS_POST_SEND_TIMEOUT
    assert env_timeout.retryable is False
    assert env_timeout.auto_retry_performed is False
    assert timeout_calls == 1
    assert env_timeout.external_call_count == 1
    assert env_timeout.committed_allowed is False
    assert env_timeout.can_promote_to_supported is False

    # 2) Pre-spend 429 -> retryable within budget cap and succeeds on retry
    res2, rev_hash2, span_hash2 = make_operation_memory_resolution()
    assert res2.ephemeral_span is not None
    valid_payload = make_valid_stage_payloads(locator_dict=res2.ephemeral_span.locator.to_dict())[
        "STAGE_2A_FACT_CANDIDATE_EXTRACTION"
    ]
    throttle_calls = 0

    def throttle_then_ok(_: StageAdapterInput) -> RawProviderStageResponse:
        nonlocal throttle_calls
        throttle_calls += 1
        if throttle_calls == 1:
            return RawProviderStageResponse(
                payload=None,
                stream_completed=False,
                transport_error=ProviderTransportErrorKind.RATE_LIMITED_429,
                http_status=429,
                request_transmitted=False,
                error_message="Rate limited before spend",
            )
        return RawProviderStageResponse(payload=valid_payload, stream_completed=True)

    env_retried = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res2,
            expected_revision_hash=rev_hash2,
            expected_span_sha256=span_hash2,
            budget=_safe_budget(max_calls=3),
            adapter=PolicyGatedProviderStageAdapter(transport=throttle_then_ok),
            job_reference=job_ref,
        )
    )
    assert env_retried.status is StageRunStatus.SUCCEEDED
    assert env_retried.auto_retry_performed is True
    assert throttle_calls == 2
    assert env_retried.external_call_count == 2


def test_pre_commit_fence_rejects_commit_when_unlinked_mid_call_barrier(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    res, rev_hash, span_hash = make_operation_memory_resolution()
    assert res.ephemeral_span is not None
    valid_payload = make_valid_stage_payloads(locator_dict=res.ephemeral_span.locator.to_dict())[
        "STAGE_2A_FACT_CANDIDATE_EXTRACTION"
    ]
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )

    entered_transport = threading.Barrier(2)
    unlink_done = threading.Barrier(2)

    def slow_transport(_: StageAdapterInput) -> RawProviderStageResponse:
        entered_transport.wait(timeout=5.0)
        unlink_done.wait(timeout=5.0)
        return RawProviderStageResponse(payload=valid_payload, stream_completed=True)

    unlink_errors: list[Exception] = []

    def unlink_worker() -> None:
        try:
            entered_transport.wait(timeout=5.0)
            manager.unlink_connection(
                owner_a_access,
                connection_id=conn.connection_id,
                revoker=RecordingRemoteRevoker(manager=manager, confirmed=True),
            )
            unlink_done.wait(timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            unlink_errors.append(exc)

    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )
    t = threading.Thread(target=unlink_worker)
    t.start()
    envelope = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(transport=slow_transport),
            job_reference=job_ref,
        )
    )
    t.join(timeout=5.0)

    assert not unlink_errors
    assert envelope.status is StageRunStatus.CANCELED_PRE_COMMIT_FENCE
    assert envelope.reason_code is LlmGateReasonCode.PRE_COMMIT_FENCE_REJECTED
    assert envelope.validated_payload is None
    assert envelope.committed_allowed is False
    assert envelope.can_promote_to_supported is False
    assert envelope.live_pilot_gate_eligible is False


def test_pre_commit_fence_rejects_mid_call_consent_revocation_and_deletion_fence(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, approval, conn, _ = _setup_approved_subscription(fake_clock, owner_a_access)
    runner = BoundedStageRunner(
        policy_gate=LlmPolicyGate(
            approval_config=approval,
            required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
        ),
        lifecycle_manager=manager,
        clock=fake_clock,
    )

    # 1) Consent revoked mid-call via before_commit_hook
    res1, rev_hash, span_hash = make_operation_memory_resolution()
    assert res1.ephemeral_span is not None
    valid_payload = make_valid_stage_payloads(locator_dict=res1.ephemeral_span.locator.to_dict())[
        "STAGE_2A_FACT_CANDIDATE_EXTRACTION"
    ]
    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash,
    )

    def revoke_in_hook(_ctx: Any) -> bool:
        manager.revoke_external_send_consent(owner_a_access, source_resource_id=SOURCE_RESOURCE_A)
        return True

    env_consent = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res1,
            expected_revision_hash=rev_hash,
            expected_span_sha256=span_hash,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(
                transport=lambda _: RawProviderStageResponse(
                    payload=valid_payload, stream_completed=True
                )
            ),
            job_reference=job_ref,
            before_commit_hook=revoke_in_hook,
        )
    )
    assert env_consent.status is StageRunStatus.CANCELED_PRE_COMMIT_FENCE
    assert env_consent.committed_allowed is False
    assert env_consent.can_promote_to_supported is False

    # 2) Re-grant consent, then activate source deletion fence mid-call
    manager.grant_external_send_consent(
        owner_a_access,
        source_resource_id=SOURCE_RESOURCE_A,
        consent_version="consent-v2",
        acknowledged_public_unmasked_send=True,
        acknowledged_third_party_non_recallable=True,
    )
    res2, rev_hash2, span_hash2 = make_operation_memory_resolution()
    job_ref2 = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash=rev_hash2,
    )

    def fence_in_hook(_ctx: Any) -> bool:
        manager.mark_source_deletion_fenced(owner_a_access.workspace_id, SOURCE_RESOURCE_A, fenced=True)
        return True

    env_del = runner.run_stage(
        StageExecutionRequest(
            access=owner_a_access,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            resolution=res2,
            expected_revision_hash=rev_hash2,
            expected_span_sha256=span_hash2,
            budget=_safe_budget(),
            adapter=PolicyGatedProviderStageAdapter(
                transport=lambda _: RawProviderStageResponse(
                    payload=valid_payload, stream_completed=True
                )
            ),
            job_reference=job_ref2,
            before_commit_hook=fence_in_hook,
        )
    )
    assert env_del.status is StageRunStatus.CANCELED_PRE_COMMIT_FENCE
    assert env_del.committed_allowed is False
    assert env_del.can_promote_to_supported is False


def test_committed_inf08_evidence_bundle_matches_contract_and_has_zero_leaks() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    evidence_path = repo_root / "contracts" / "llm-gate" / "inf08_evidence.v1.json"
    assert evidence_path.exists(), f"Missing INF-08 evidence bundle at {evidence_path}"
    data = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert data["gate_id"] == "INF-08"
    assert data["task_id"] == "T-408"
    assert data["external_live_calls"] == 0
    assert data["live_ai_gate_status"] == "OFF_NOT_PASSED_PENDING_OD002_OD014"
    assert data["public_unmasked_passthrough_verified_on_synthetic"] is True
    assert data["token_or_raw_leaks"] == 0
    assert data["db_user_llm_token_tables_added"] == 0
    assert data["public_openapi_subscription_routes_added"] == 0
    assert len(data["cases"]) >= 16
    assert all(case["passed"] is True for case in data["cases"])
    assert_no_raw_text_or_secret_keys(data, context="inf08_evidence.v1.json")

    raw_text = evidence_path.read_text(encoding="utf-8")
    assert FAKE_PUBLIC_SECRET_MARKER not in raw_text
    assert "at_secret_" not in raw_text
    assert "rt_secret_" not in raw_text
