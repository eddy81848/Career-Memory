"""Default hard-OFF hosted approval & zero-overage fail-closed policy gate (T-408 / INF-08)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.schema_contracts.enums import ResourceVisibility

from .models import (
    DEFAULT_FREE_ALTERNATIVES,
    PROHIBITED_PROVIDER_MODES,
    ConnectionJobReference,
    ExternalSendConsentRecord,
    HostedLlmApprovalConfig,
    LlmGateDecision,
    LlmGateOutcome,
    LlmGateReasonCode,
    LlmProviderMode,
    LlmStageCode,
    StageBudgetPolicy,
    SubscriptionConnectionRecord,
    SubscriptionConnectionStatus,
    utc_now,
)

DEFAULT_REQUIRED_PLAN_INFERENCE_SCOPES: frozenset[str] = frozenset(
    {"chatgpt.subscription.inference"}
)


@dataclass(frozen=True, slots=True)
class PolicyGateRequest:
    """All inputs evaluated before allowing any LLM stage execution."""

    access: WorkspaceAccessContext | None
    provider_mode: LlmProviderMode
    stage_code: LlmStageCode
    source_resource_id: str
    visibility: ResourceVisibility | str
    input_byte_length: int
    budget: StageBudgetPolicy
    connection: SubscriptionConnectionRecord | None = None
    consent: ExternalSendConsentRecord | None = None
    job_reference: ConnectionJobReference | None = None
    deletion_fenced: bool = False
    allow_synthetic_fixture_mode: bool = False
    workspace_membership_active: bool = True
    now: datetime | None = None


class LlmPolicyGate:
    """Fail-closed policy evaluator enforcing hard-OFF default, zero-overage, and consent/fences."""

    def __init__(
        self,
        *,
        approval_config: HostedLlmApprovalConfig | None = None,
        required_plan_scopes: frozenset[str] = DEFAULT_REQUIRED_PLAN_INFERENCE_SCOPES,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._approval_config = approval_config or HostedLlmApprovalConfig()
        self._required_plan_scopes = frozenset(s.strip().lower() for s in required_plan_scopes if s.strip())
        self._clock = clock

    @property
    def approval_config(self) -> HostedLlmApprovalConfig:
        return self._approval_config

    @property
    def required_plan_scopes(self) -> frozenset[str]:
        return self._required_plan_scopes

    def _block(
        self,
        reason_code: LlmGateReasonCode,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> LlmGateDecision:
        return LlmGateDecision(
            allowed=False,
            mode=LlmGateOutcome.MANUAL_ONLY_BLOCKED,
            reason_code=reason_code,
            message=message,
            retryable=False,
            external_call_count=0,
            free_alternatives=DEFAULT_FREE_ALTERNATIVES,
            policy_version=self._approval_config.policy_version,
            details=details or {},
        )

    def evaluate(self, request: PolicyGateRequest) -> LlmGateDecision:
        """Evaluate every T-408 / INF-08 gate invariant strictly before any provider call."""
        now = request.now or self._clock()

        # 1. Prohibited provider modes and paid API fallback are rejected unconditionally (0 calls).
        if request.provider_mode is LlmProviderMode.PAID_API_CREDITS_FALLBACK or request.budget.paid_fallback_requested:
            return self._block(
                LlmGateReasonCode.PAID_API_FALLBACK_FORBIDDEN,
                "Paid API credit fallback is strictly prohibited under ADR-020 zero-overage policy.",
                details={"provider_mode": request.provider_mode.value},
            )
        if request.provider_mode in PROHIBITED_PROVIDER_MODES:
            return self._block(
                LlmGateReasonCode.PROHIBITED_PROVIDER_MODE,
                f"Provider mode {request.provider_mode.value} is prohibited under T-408 / ADR-020.",
                details={"provider_mode": request.provider_mode.value},
            )

        # 2. Workspace membership must always be verified and active.
        if request.access is None or not request.workspace_membership_active:
            return self._block(
                LlmGateReasonCode.WORKSPACE_MEMBERSHIP_REQUIRED,
                "Active verified workspace membership is required before LLM stage execution.",
            )

        # 3. Source visibility (PUBLIC only) and deletion/policy fence apply in ALL modes.
        visibility_str = (
            request.visibility.value
            if isinstance(request.visibility, ResourceVisibility)
            else str(request.visibility).strip().upper()
        )
        if visibility_str != ResourceVisibility.PUBLIC.value:
            return self._block(
                LlmGateReasonCode.NON_PUBLIC_REPOSITORY_BLOCKED,
                "Only verified PUBLIC repositories are eligible for AI processing in MVP.",
                details={"visibility": visibility_str},
            )

        if request.deletion_fenced:
            return self._block(
                LlmGateReasonCode.SOURCE_POLICY_OR_DELETION_FENCED,
                "Source resource is deletion-fenced or policy-blocked; external execution denied.",
            )

        # 4. Input byte & call budget caps apply in ALL modes.
        if request.input_byte_length <= 0 or request.input_byte_length > request.budget.max_input_bytes:
            return self._block(
                LlmGateReasonCode.BUDGET_MAX_INPUT_BYTES_EXCEEDED,
                "Stage input exceeds deterministic max_input_bytes cap.",
                details={
                    "input_byte_length": request.input_byte_length,
                    "max_input_bytes": request.budget.max_input_bytes,
                },
            )
        if request.budget.calls_used >= request.budget.max_calls:
            return self._block(
                LlmGateReasonCode.BUDGET_MAX_CALLS_EXCEEDED,
                "Deterministic stage call cap exhausted.",
                details={
                    "calls_used": request.budget.calls_used,
                    "max_calls": request.budget.max_calls,
                },
            )

        # 5. Explicit offline synthetic fixture mode (dev/test/eval only; never live pilot eligible).
        if request.provider_mode is LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE:
            if not request.allow_synthetic_fixture_mode:
                return self._block(
                    LlmGateReasonCode.SYNTHETIC_MODE_NOT_ENABLED,
                    "Synthetic fixture mode must be explicitly opted in for offline test/eval runs.",
                )
            return LlmGateDecision(
                allowed=True,
                mode=LlmGateOutcome.ALLOWED,
                reason_code=LlmGateReasonCode.ALLOWED,
                message="Offline synthetic fixture execution permitted (not eligible for live AI pilot gate).",
                retryable=False,
                external_call_count=0,
                free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                policy_version=self._approval_config.policy_version,
                details={"execution_origin": "SYNTHETIC_FIXTURE"},
            )

        # 6. Live subscription mode: ALL 4 hosted approval flags MUST be explicitly True.
        cfg = self._approval_config
        if not cfg.hosted_deployment_approved:
            return self._block(
                LlmGateReasonCode.HOSTED_DEPLOYMENT_NOT_APPROVED,
                "Hosted subscription LLM deployment is OFF by default until explicitly approved.",
                details={"missing_approvals": list(cfg.missing_approvals())},
            )
        if not cfg.zero_overage_guaranteed:
            return self._block(
                LlmGateReasonCode.ZERO_OVERAGE_NOT_GUARANTEED,
                "Zero-overage guarantee is not verified; remaining in manual-only mode.",
                details={"missing_approvals": list(cfg.missing_approvals())},
            )
        if not cfg.od002_data_policy_verified:
            return self._block(
                LlmGateReasonCode.OD002_DATA_POLICY_UNVERIFIED,
                "OD-002 third-party data/training policy verification is not complete.",
                details={"missing_approvals": list(cfg.missing_approvals())},
            )
        if not cfg.od014_token_store_verified:
            return self._block(
                LlmGateReasonCode.OD014_TOKEN_STORE_UNVERIFIED,
                "OD-014 user subscription token storage policy is not verified.",
                details={"missing_approvals": list(cfg.missing_approvals())},
            )

        # 7. Separate per-repository external-send consent (OD-002 + ADR-020).
        consent = request.consent
        if consent is None:
            return self._block(
                LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED,
                "Per-repository external evidence send consent is required before transmission.",
            )
        if (
            consent.workspace_id != request.access.workspace_id
            or consent.source_resource_id != request.source_resource_id
        ):
            return self._block(
                LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH,
                "External send consent does not match target workspace or source resource.",
            )
        if consent.revoked_at is not None:
            return self._block(
                LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REVOKED,
                "External send consent for this repository has been revoked.",
            )
        if not consent.is_active:
            return self._block(
                LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED,
                "External send consent is missing required public-unmasked or non-recallable acknowledgments.",
            )

        # 8. Subscription connection ownership, workspace, status, version, scope, and plan eligibility.
        conn = request.connection
        if conn is None:
            return self._block(
                LlmGateReasonCode.CONNECTION_NOT_FOUND,
                "Active user subscription connection is required.",
            )
        if conn.workspace_id != request.access.workspace_id or conn.owner_user_id != request.access.user_id:
            return self._block(
                LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH,
                "Subscription connection belongs to a different workspace or user.",
            )
        if conn.status is not SubscriptionConnectionStatus.ACTIVE:
            return self._block(
                LlmGateReasonCode.CONNECTION_REVOKED_OR_INACTIVE,
                f"Subscription connection is not ACTIVE (status={conn.status.value}).",
            )
        if conn.token_expires_at <= now:
            return self._block(
                LlmGateReasonCode.CONNECTION_TOKEN_EXPIRED,
                "Subscription connection access token is expired and requires refresh or re-auth.",
            )

        granted_scopes = frozenset(s.strip().lower() for s in conn.granted_scopes if s.strip())
        if not self._required_plan_scopes.issubset(granted_scopes):
            return self._block(
                LlmGateReasonCode.LOGIN_ONLY_SCOPE_REJECTED,
                "Connection lacks required ChatGPT plan inference scope (login-only grant rejected).",
            )
        if not conn.plan_inference_eligible:
            return self._block(
                LlmGateReasonCode.PLAN_ELIGIBILITY_UNVERIFIED,
                "Connected account plan is not verified as eligible for subscription inference.",
            )

        # 9. ConnectionJobReference consistency check when provided.
        job_ref = request.job_reference
        if job_ref is not None:
            if (
                job_ref.workspace_id != request.access.workspace_id
                or job_ref.owner_user_id != request.access.user_id
                or job_ref.connection_id != conn.connection_id
            ):
                return self._block(
                    LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH,
                    "ConnectionJobReference ownership or workspace mismatch.",
                )
            if job_ref.connection_version != conn.connection_version:
                return self._block(
                    LlmGateReasonCode.CONNECTION_VERSION_STALE,
                    "ConnectionJobReference references a stale connection_version.",
                )
            if job_ref.consent_version != consent.consent_version:
                return self._block(
                    LlmGateReasonCode.EXTERNAL_SEND_CONSENT_VERSION_MISMATCH,
                    "ConnectionJobReference consent_version does not match current active consent.",
                )

        # 10. Zero-overage credit & quota certainty check (max_output_tokens_hint alone is NEVER sufficient).
        if not request.budget.overage_credit_disabled:
            return self._block(
                LlmGateReasonCode.OVERAGE_CREDIT_RISK_BLOCKED,
                "Cannot execute because overage/additional credit spending is not confirmed disabled.",
                details={
                    "max_output_tokens_hint_advisory_only": request.budget.max_output_tokens_hint is not None
                },
            )
        if not request.budget.quota_remaining_known_and_sufficient:
            return self._block(
                LlmGateReasonCode.QUOTA_REMAINING_UNKNOWN_OR_INSUFFICIENT,
                "Remaining subscription quota is unknown or insufficient; blocking with 0 calls.",
            )
        if (
            request.budget.quota_remaining_units is not None
            and request.budget.quota_remaining_units < request.budget.estimated_required_units
        ):
            return self._block(
                LlmGateReasonCode.QUOTA_REMAINING_UNKNOWN_OR_INSUFFICIENT,
                "Remaining subscription quota units are below estimated stage requirement.",
                details={
                    "quota_remaining_units": request.budget.quota_remaining_units,
                    "estimated_required_units": request.budget.estimated_required_units,
                },
            )

        return LlmGateDecision(
            allowed=True,
            mode=LlmGateOutcome.ALLOWED,
            reason_code=LlmGateReasonCode.ALLOWED,
            message="Subscription stage execution permitted under verified policy and budget guards.",
            retryable=False,
            external_call_count=0,
            free_alternatives=DEFAULT_FREE_ALTERNATIVES,
            policy_version=cfg.policy_version,
            details={"execution_origin": "APPROVED_SUBSCRIPTION"},
        )
