"""INF-08 tests for default hard-OFF hosted approval & zero-overage policy gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from career_memory_api.llm_gate.models import (
    DEFAULT_FREE_ALTERNATIVES,
    PROHIBITED_PROVIDER_MODES,
    ExternalSendConsentRecord,
    FreeAlternative,
    HostedLlmApprovalConfig,
    LlmGateOutcome,
    LlmGateReasonCode,
    LlmProviderMode,
    LlmRuntimeMode,
    LlmStageCode,
    RemoteRevocationStatus,
    SecretWipeStatus,
    StageBudgetPolicy,
    SubscriptionConnectionRecord,
    SubscriptionConnectionStatus,
)
from career_memory_api.llm_gate.policy_gate import LlmPolicyGate, PolicyGateRequest
from career_memory_api.schema_contracts.enums import ResourceVisibility

from .conftest import (
    EXPECTED_OIDC_ISSUER,
    PLAN_INFERENCE_SCOPE,
    SOURCE_RESOURCE_A,
    USER_OWNER_A,
    WORKSPACE_A,
)


def _approved_config() -> HostedLlmApprovalConfig:
    return HostedLlmApprovalConfig(
        hosted_deployment_approved=True,
        zero_overage_guaranteed=True,
        od002_data_policy_verified=True,
        od014_token_store_verified=True,
    )


def _active_connection(now: datetime) -> SubscriptionConnectionRecord:
    return SubscriptionConnectionRecord(
        connection_id="conn-001",
        connection_version=1,
        workspace_id=WORKSPACE_A,
        owner_user_id=USER_OWNER_A,
        provider=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
        oidc_issuer=EXPECTED_OIDC_ISSUER,
        oidc_subject="oidc-sub-owner-a",
        granted_scopes=("openid", PLAN_INFERENCE_SCOPE),
        plan_tier="PLUS",
        plan_inference_eligible=True,
        token_secret_ref="secret://llm-sub-token/conn-001",
        token_expires_at=now + timedelta(hours=1),
        status=SubscriptionConnectionStatus.ACTIVE,
        remote_revocation_status=RemoteRevocationStatus.NOT_REQUESTED,
        secret_wipe_status=SecretWipeStatus.ACTIVE,
        policy_version="llm-gate-policy-v1",
        created_at=now,
        updated_at=now,
    )


def _active_consent(now: datetime) -> ExternalSendConsentRecord:
    return ExternalSendConsentRecord(
        consent_id="consent-001",
        workspace_id=WORKSPACE_A,
        source_resource_id=SOURCE_RESOURCE_A,
        consented_by_user_id=USER_OWNER_A,
        consent_version="consent-v1",
        acknowledged_public_unmasked_send=True,
        acknowledged_third_party_non_recallable=True,
        granted_at=now,
    )


def _safe_budget(**overrides: object) -> StageBudgetPolicy:
    base: dict[str, object] = {
        "max_input_bytes": 16_384,
        "max_calls": 2,
        "calls_used": 0,
        "max_repair_attempts": 1,
        "overage_credit_disabled": True,
        "quota_remaining_known_and_sufficient": True,
        "quota_remaining_units": 10,
        "estimated_required_units": 1,
    }
    base.update(overrides)
    return StageBudgetPolicy(**base)  # type: ignore[arg-type]


def test_default_hosted_approval_config_is_hard_off() -> None:
    cfg = HostedLlmApprovalConfig()
    assert cfg.hosted_deployment_approved is False
    assert cfg.zero_overage_guaranteed is False
    assert cfg.od002_data_policy_verified is False
    assert cfg.od014_token_store_verified is False
    assert cfg.runtime_mode is LlmRuntimeMode.OFF_MANUAL_ONLY
    assert set(cfg.missing_approvals()) == {
        "hosted_deployment_approved",
        "zero_overage_guaranteed",
        "od002_data_policy_verified",
        "od014_token_store_verified",
    }


@pytest.mark.parametrize(
    ("flag_name", "expected_reason"),
    [
        ("hosted_deployment_approved", LlmGateReasonCode.HOSTED_DEPLOYMENT_NOT_APPROVED),
        ("zero_overage_guaranteed", LlmGateReasonCode.ZERO_OVERAGE_NOT_GUARANTEED),
        ("od002_data_policy_verified", LlmGateReasonCode.OD002_DATA_POLICY_UNVERIFIED),
        ("od014_token_store_verified", LlmGateReasonCode.OD014_TOKEN_STORE_UNVERIFIED),
    ],
)
def test_each_hosted_approval_flag_false_blocks_fail_closed(
    owner_a_access,
    flag_name: str,
    expected_reason: LlmGateReasonCode,
) -> None:
    now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
    kwargs = {
        "hosted_deployment_approved": True,
        "zero_overage_guaranteed": True,
        "od002_data_policy_verified": True,
        "od014_token_store_verified": True,
    }
    kwargs[flag_name] = False
    cfg = HostedLlmApprovalConfig(**kwargs)
    assert cfg.runtime_mode is LlmRuntimeMode.OFF_MANUAL_ONLY

    gate = LlmPolicyGate(approval_config=cfg, required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}))
    decision = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=128,
            budget=_safe_budget(),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert decision.allowed is False
    assert decision.mode is LlmGateOutcome.MANUAL_ONLY_BLOCKED
    assert decision.reason_code is expected_reason
    assert decision.retryable is False
    assert decision.external_call_count == 0
    assert set(decision.free_alternatives) == set(DEFAULT_FREE_ALTERNATIVES)
    assert FreeAlternative.REDUCE_WORK_SCOPE in decision.free_alternatives
    assert FreeAlternative.MANUAL_RESUME_AFTER_QUOTA_RESET in decision.free_alternatives
    assert FreeAlternative.NON_AI_IMPORT_AND_EVENT_CURATION in decision.free_alternatives
    assert FreeAlternative.LOCAL_SYNTHETIC_FIXTURE_DEV_ONLY in decision.free_alternatives


@pytest.mark.parametrize("prohibited_mode", sorted(PROHIBITED_PROVIDER_MODES, key=lambda m: m.value))
def test_prohibited_provider_modes_rejected_immediately_even_when_hosted_approved(
    owner_a_access,
    prohibited_mode: LlmProviderMode,
) -> None:
    now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
    gate = LlmPolicyGate(
        approval_config=_approved_config(),
        required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )
    decision = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=prohibited_mode,
            stage_code=LlmStageCode.STAGE_3A_CLAIM_GENERATION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=128,
            budget=_safe_budget(),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert decision.allowed is False
    assert decision.mode is LlmGateOutcome.MANUAL_ONLY_BLOCKED
    assert decision.external_call_count == 0
    assert decision.retryable is False
    assert decision.reason_code in {
        LlmGateReasonCode.PROHIBITED_PROVIDER_MODE,
        LlmGateReasonCode.PAID_API_FALLBACK_FORBIDDEN,
    }
    assert decision.free_alternatives == DEFAULT_FREE_ALTERNATIVES


def test_overage_credit_risk_and_unknown_quota_block_even_with_max_output_tokens_hint(
    owner_a_access,
) -> None:
    now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
    gate = LlmPolicyGate(
        approval_config=_approved_config(),
        required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )

    # Overage credit NOT disabled, even though max_output_tokens_hint is set!
    overage_decision = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=128,
            budget=_safe_budget(overage_credit_disabled=False, max_output_tokens_hint=512),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert overage_decision.allowed is False
    assert overage_decision.reason_code is LlmGateReasonCode.OVERAGE_CREDIT_RISK_BLOCKED
    assert overage_decision.external_call_count == 0

    # Quota remaining unknown -> block!
    unknown_quota_decision = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=128,
            budget=_safe_budget(
                quota_remaining_known_and_sufficient=False,
                quota_remaining_units=None,
                max_output_tokens_hint=256,
            ),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert unknown_quota_decision.allowed is False
    assert unknown_quota_decision.reason_code is LlmGateReasonCode.QUOTA_REMAINING_UNKNOWN_OR_INSUFFICIENT
    assert unknown_quota_decision.external_call_count == 0

    # Paid fallback requested -> block!
    paid_fallback_decision = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=128,
            budget=_safe_budget(paid_fallback_requested=True),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert paid_fallback_decision.allowed is False
    assert paid_fallback_decision.reason_code is LlmGateReasonCode.PAID_API_FALLBACK_FORBIDDEN
    assert paid_fallback_decision.external_call_count == 0


def test_non_public_repository_missing_consent_and_budget_caps_block(owner_a_access) -> None:
    now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
    gate = LlmPolicyGate(
        approval_config=_approved_config(),
        required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )

    priv = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PRIVATE,
            input_byte_length=100,
            budget=_safe_budget(),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert priv.allowed is False
    assert priv.reason_code is LlmGateReasonCode.NON_PUBLIC_REPOSITORY_BLOCKED

    no_consent = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=100,
            budget=_safe_budget(),
            connection=_active_connection(now),
            consent=None,
            now=now,
        )
    )
    assert no_consent.allowed is False
    assert no_consent.reason_code is LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED

    oversize = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=99_999,
            budget=_safe_budget(max_input_bytes=1024),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert oversize.allowed is False
    assert oversize.reason_code is LlmGateReasonCode.BUDGET_MAX_INPUT_BYTES_EXCEEDED

    calls_spent = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=100,
            budget=_safe_budget(max_calls=2, calls_used=2),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert calls_spent.allowed is False
    assert calls_spent.reason_code is LlmGateReasonCode.BUDGET_MAX_CALLS_EXCEEDED


def test_allowed_when_all_four_approvals_and_guards_pass(owner_a_access) -> None:
    now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
    gate = LlmPolicyGate(
        approval_config=_approved_config(),
        required_plan_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )
    decision = gate.evaluate(
        PolicyGateRequest(
            access=owner_a_access,
            provider_mode=LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            source_resource_id=SOURCE_RESOURCE_A,
            visibility=ResourceVisibility.PUBLIC,
            input_byte_length=256,
            budget=_safe_budget(),
            connection=_active_connection(now),
            consent=_active_consent(now),
            now=now,
        )
    )
    assert decision.allowed is True
    assert decision.mode is LlmGateOutcome.ALLOWED
    assert decision.reason_code is LlmGateReasonCode.ALLOWED
    assert decision.external_call_count == 0
