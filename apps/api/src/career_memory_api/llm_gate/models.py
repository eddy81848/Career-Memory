"""Typed domain models, enums, and redaction/non-raw guards for T-408 Subscription LLM Gate."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from career_memory_api.auth.redaction import SENSITIVE_KEY_NAMES, redact_mapping, redact_text
from career_memory_api.resolver.models import (
    FORBIDDEN_RAW_PERSISTENCE_KEYS,
    format_utc_timestamp,
    normalize_sha256_hash,
)
from career_memory_api.schema_contracts.metadata_guard import (
    FORBIDDEN_RAW_TEXT_KEYS,
    assert_no_forbidden_raw_keys,
    validate_token_secret_ref,
)

DEFAULT_LLM_POLICY_VERSION = "llm-gate-policy-v1"

ExecutionOrigin = Literal["SYNTHETIC_FIXTURE", "APPROVED_SUBSCRIPTION"]


class LlmRuntimeMode(StrEnum):
    """Runtime activation state for hosted subscription LLM execution (OD-002 / OD-014)."""

    OFF_MANUAL_ONLY = "OFF_MANUAL_ONLY"
    APPROVED_SUBSCRIPTION = "APPROVED_SUBSCRIPTION"
    SYNTHETIC_FIXTURE_OFFLINE = "SYNTHETIC_FIXTURE_OFFLINE"


class LlmProviderMode(StrEnum):
    """Provider / credential modes evaluated by the fail-closed LLM policy gate."""

    CHATGPT_SUBSCRIPTION_OAUTH = "CHATGPT_SUBSCRIPTION_OAUTH"
    SYNTHETIC_FIXTURE_OFFLINE = "SYNTHETIC_FIXTURE_OFFLINE"

    # Strictly prohibited modes (ADR-020 / T-408) — always rejected with 0 calls.
    BYOK_API_KEY = "BYOK_API_KEY"
    SHARED_OPERATOR_KEY = "SHARED_OPERATOR_KEY"
    BROWSER_COOKIE_IMPORT = "BROWSER_COOKIE_IMPORT"
    CLI_CREDENTIAL_IMPORT = "CLI_CREDENTIAL_IMPORT"
    GEMINI_SUBSCRIPTION_UNSUPPORTED = "GEMINI_SUBSCRIPTION_UNSUPPORTED"
    CLAUDE_SUBSCRIPTION_UNSUPPORTED = "CLAUDE_SUBSCRIPTION_UNSUPPORTED"
    PAID_API_CREDITS_FALLBACK = "PAID_API_CREDITS_FALLBACK"


PROHIBITED_PROVIDER_MODES: frozenset[LlmProviderMode] = frozenset(
    {
        LlmProviderMode.BYOK_API_KEY,
        LlmProviderMode.SHARED_OPERATOR_KEY,
        LlmProviderMode.BROWSER_COOKIE_IMPORT,
        LlmProviderMode.CLI_CREDENTIAL_IMPORT,
        LlmProviderMode.GEMINI_SUBSCRIPTION_UNSUPPORTED,
        LlmProviderMode.CLAUDE_SUBSCRIPTION_UNSUPPORTED,
        LlmProviderMode.PAID_API_CREDITS_FALLBACK,
    }
)


class LlmGateOutcome(StrEnum):
    """Deterministic binary gate outcome; every block degrades cleanly to manual-only."""

    ALLOWED = "ALLOWED"
    MANUAL_ONLY_BLOCKED = "MANUAL_ONLY_BLOCKED"


class FreeAlternative(StrEnum):
    """Actionable zero-cost user alternatives emitted on every blocked decision."""

    REDUCE_WORK_SCOPE = "REDUCE_WORK_SCOPE"
    MANUAL_RESUME_AFTER_QUOTA_RESET = "MANUAL_RESUME_AFTER_QUOTA_RESET"
    NON_AI_IMPORT_AND_EVENT_CURATION = "NON_AI_IMPORT_AND_EVENT_CURATION"
    LOCAL_SYNTHETIC_FIXTURE_DEV_ONLY = "LOCAL_SYNTHETIC_FIXTURE_DEV_ONLY"


DEFAULT_FREE_ALTERNATIVES: tuple[FreeAlternative, ...] = (
    FreeAlternative.REDUCE_WORK_SCOPE,
    FreeAlternative.MANUAL_RESUME_AFTER_QUOTA_RESET,
    FreeAlternative.NON_AI_IMPORT_AND_EVENT_CURATION,
    FreeAlternative.LOCAL_SYNTHETIC_FIXTURE_DEV_ONLY,
)


class LlmGateReasonCode(StrEnum):
    """Stable machine-readable reason codes for T-408 / INF-08 policy & runtime decisions."""

    ALLOWED = "ALLOWED"
    HOSTED_DEPLOYMENT_NOT_APPROVED = "HOSTED_DEPLOYMENT_NOT_APPROVED"
    ZERO_OVERAGE_NOT_GUARANTEED = "ZERO_OVERAGE_NOT_GUARANTEED"
    OD002_DATA_POLICY_UNVERIFIED = "OD002_DATA_POLICY_UNVERIFIED"
    OD014_TOKEN_STORE_UNVERIFIED = "OD014_TOKEN_STORE_UNVERIFIED"
    PROHIBITED_PROVIDER_MODE = "PROHIBITED_PROVIDER_MODE"
    PAID_API_FALLBACK_FORBIDDEN = "PAID_API_FALLBACK_FORBIDDEN"
    SYNTHETIC_MODE_NOT_ENABLED = "SYNTHETIC_MODE_NOT_ENABLED"
    WORKSPACE_MEMBERSHIP_REQUIRED = "WORKSPACE_MEMBERSHIP_REQUIRED"
    WORKSPACE_OR_OWNER_MISMATCH = "WORKSPACE_OR_OWNER_MISMATCH"
    INVALID_REDIRECT_URI = "INVALID_REDIRECT_URI"
    OAUTH_STATE_INVALID_OR_REPLAYED = "OAUTH_STATE_INVALID_OR_REPLAYED"
    PKCE_VERIFICATION_FAILED = "PKCE_VERIFICATION_FAILED"
    OIDC_CLAIMS_INVALID = "OIDC_CLAIMS_INVALID"
    CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"
    CONNECTION_REVOKED_OR_INACTIVE = "CONNECTION_REVOKED_OR_INACTIVE"
    CONNECTION_VERSION_STALE = "CONNECTION_VERSION_STALE"
    CONNECTION_TOKEN_EXPIRED = "CONNECTION_TOKEN_EXPIRED"
    LOGIN_ONLY_SCOPE_REJECTED = "LOGIN_ONLY_SCOPE_REJECTED"
    PLAN_ELIGIBILITY_UNVERIFIED = "PLAN_ELIGIBILITY_UNVERIFIED"
    EXTERNAL_SEND_CONSENT_REQUIRED = "EXTERNAL_SEND_CONSENT_REQUIRED"
    EXTERNAL_SEND_CONSENT_REVOKED = "EXTERNAL_SEND_CONSENT_REVOKED"
    EXTERNAL_SEND_CONSENT_VERSION_MISMATCH = "EXTERNAL_SEND_CONSENT_VERSION_MISMATCH"
    NON_PUBLIC_REPOSITORY_BLOCKED = "NON_PUBLIC_REPOSITORY_BLOCKED"
    SOURCE_POLICY_OR_DELETION_FENCED = "SOURCE_POLICY_OR_DELETION_FENCED"
    OVERAGE_CREDIT_RISK_BLOCKED = "OVERAGE_CREDIT_RISK_BLOCKED"
    QUOTA_REMAINING_UNKNOWN_OR_INSUFFICIENT = "QUOTA_REMAINING_UNKNOWN_OR_INSUFFICIENT"
    BUDGET_MAX_INPUT_BYTES_EXCEEDED = "BUDGET_MAX_INPUT_BYTES_EXCEEDED"
    BUDGET_MAX_CALLS_EXCEEDED = "BUDGET_MAX_CALLS_EXCEEDED"
    RESOLUTION_NOT_MATCHED = "RESOLUTION_NOT_MATCHED"
    RESOLUTION_HASH_MISMATCH = "RESOLUTION_HASH_MISMATCH"
    OPERATIONAL_SECRET_IN_PAYLOAD_BLOCKED = "OPERATIONAL_SECRET_IN_PAYLOAD_BLOCKED"
    STREAM_INCOMPLETE = "STREAM_INCOMPLETE"
    UNSUPPORTED_OUTPUT_OR_TOOL_SIDE_EFFECT = "UNSUPPORTED_OUTPUT_OR_TOOL_SIDE_EFFECT"
    AI_SCHEMA_INVALID = "AI_SCHEMA_INVALID"
    DRAFT_INVALID = "DRAFT_INVALID"
    AMBIGUOUS_POST_SEND_TIMEOUT = "AMBIGUOUS_POST_SEND_TIMEOUT"
    RETRYABLE_PRE_SEND_OR_THROTTLED_FAILURE = "RETRYABLE_PRE_SEND_OR_THROTTLED_FAILURE"
    UPSTREAM_AUTH_OR_QUOTA_TERMINATED = "UPSTREAM_AUTH_OR_QUOTA_TERMINATED"
    PRE_COMMIT_FENCE_REJECTED = "PRE_COMMIT_FENCE_REJECTED"
    SYNTHETIC_NOT_ELIGIBLE_FOR_LIVE_GATE = "SYNTHETIC_NOT_ELIGIBLE_FOR_LIVE_GATE"


class LlmStageCode(StrEnum):
    """The 5 bounded AI pipeline stages aligned 1:1 with T-106 Draft 2020-12 schemas."""

    STAGE_2A_FACT_CANDIDATE_EXTRACTION = "STAGE_2A_FACT_CANDIDATE_EXTRACTION"
    STAGE_2B_FACT_SUPPORT_VALIDATION = "STAGE_2B_FACT_SUPPORT_VALIDATION"
    STAGE_3A_CLAIM_GENERATION = "STAGE_3A_CLAIM_GENERATION"
    STAGE_3B_CLAIM_ATOMIZATION = "STAGE_3B_CLAIM_ATOMIZATION"
    STAGE_3C_CLAIM_SUPPORT_VALIDATION = "STAGE_3C_CLAIM_SUPPORT_VALIDATION"


STAGE_CODE_TO_SCHEMA_FILE: dict[LlmStageCode, str] = {
    LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION: "fact_candidate.v1.schema.json",
    LlmStageCode.STAGE_2B_FACT_SUPPORT_VALIDATION: "fact_support_validation.v1.schema.json",
    LlmStageCode.STAGE_3A_CLAIM_GENERATION: "claim_draft.v1.schema.json",
    LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION: "claim_atomization.v1.schema.json",
    LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION: "claim_support_validation.v1.schema.json",
}


class SubscriptionConnectionStatus(StrEnum):
    """Lifecycle status of an in-memory / port-backed subscription OAuth connection."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


class RemoteRevocationStatus(StrEnum):
    """Explicit remote revocation confirmation status (never falsely claiming cancellation)."""

    NOT_REQUESTED = "NOT_REQUESTED"
    CONFIRMED_REMOTE = "CONFIRMED_REMOTE"
    UNCONFIRMED_REMOTE = "UNCONFIRMED_REMOTE"


class SecretWipeStatus(StrEnum):
    """Vault secret wipe status tracked across unlink/revocation."""

    ACTIVE = "ACTIVE"
    WIPED = "WIPED"
    SECRET_WIPE_RETRY_PENDING = "SECRET_WIPE_RETRY_PENDING"


class StageRunStatus(StrEnum):
    """Execution status of a bounded LLM stage invocation."""

    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    DRAFT_INVALID = "DRAFT_INVALID"
    FAILED_STREAM_INCOMPLETE = "FAILED_STREAM_INCOMPLETE"
    FAILED_UNSUPPORTED_OUTPUT = "FAILED_UNSUPPORTED_OUTPUT"
    FAILED_AMBIGUOUS_TIMEOUT = "FAILED_AMBIGUOUS_TIMEOUT"
    FAILED_RETRYABLE_PRE_SPEND = "FAILED_RETRYABLE_PRE_SPEND"
    FAILED_UPSTREAM_TERMINATED = "FAILED_UPSTREAM_TERMINATED"
    CANCELED_PRE_COMMIT_FENCE = "CANCELED_PRE_COMMIT_FENCE"


# =============================================================================
# Secret & Plan C Non-Raw Audit Guards
# =============================================================================
FORBIDDEN_SECRET_KEYS: frozenset[str] = frozenset(
    set(SENSITIVE_KEY_NAMES)
    | {
        "api_key",
        "private_key",
        "oauth_token",
        "github_token",
        "openai_token",
        "chatgpt_token",
        "secret_value",
        "session_token",
        "database_url",
        "db_url",
        "dsn",
        "proxy_authorization",
        "proxy-authorization",
        "x-api-key",
        "x_api_key",
        "x-auth-token",
        "x_auth_token",
        "x-session-token",
        "x_session_token",
        "token_secret_ref",
        "pkce_verifier_ref",
    }
)

FORBIDDEN_OPERATIONAL_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-session-token",
    }
)

ALL_FORBIDDEN_METADATA_KEYS: frozenset[str] = (
    FORBIDDEN_RAW_TEXT_KEYS | FORBIDDEN_RAW_PERSISTENCE_KEYS | FORBIDDEN_SECRET_KEYS
)

_OPERATIONAL_SECRET_VALUE_RE = re.compile(
    r"(?i)("
    r"\b(?:postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?)://[^\s]+"
    r"|\bbearer\s+[A-Za-z0-9\-._~+/]+=*"
    r"|\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*"
    r"|\b(?:authorization|cookie|set-cookie)\s*:"
    r"|\bsk-[A-Za-z0-9_-]{12,}"
    r"|\b(?:at|rt)_secret_[A-Za-z0-9_-]+"
    r")"
)


def redact_llm_text(value: str) -> str:
    """Redact OAuth tokens, JWTs, headers, DB DSNs, and operational secrets from logs/errors."""
    if not value:
        return value
    masked = redact_text(value)
    return _OPERATIONAL_SECRET_VALUE_RE.sub("[REDACTED]", masked)


def contains_operational_secret_value(
    value: Any,
    *,
    known_secret_values: tuple[str, ...] = (),
) -> bool:
    """Return True if a metadata/header value embeds server/API operational credentials."""
    if isinstance(value, str):
        if _OPERATIONAL_SECRET_VALUE_RE.search(value):
            return True
        for secret in known_secret_values:
            if secret and secret in value:
                return True
        return False
    if isinstance(value, Mapping):
        for k, v in value.items():
            if str(k).strip().lower() in FORBIDDEN_SECRET_KEYS:
                return True
            if contains_operational_secret_value(v, known_secret_values=known_secret_values):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(
            contains_operational_secret_value(item, known_secret_values=known_secret_values)
            for item in value
        )
    return False


def assert_no_raw_text_or_secret_keys(
    payload: Mapping[str, Any],
    *,
    context: str,
    known_secret_values: tuple[str, ...] = (),
) -> None:
    """Fail closed if ``payload`` contains Plan C raw-text keys, secret keys, or secret values."""
    assert_no_forbidden_raw_keys(payload, field_name=context)
    stack: list[tuple[str, Any]] = [("$", payload)]
    while stack:
        path, current = stack.pop()
        if isinstance(current, Mapping):
            for raw_key, val in current.items():
                key = str(raw_key)
                lowered = key.strip().lower()
                child_path = f"{path}.{key}"
                if lowered in ALL_FORBIDDEN_METADATA_KEYS:
                    raise ValueError(
                        f"Forbidden raw-text or secret key {key!r} at {child_path} in {context}."
                    )
                if isinstance(val, str) and contains_operational_secret_value(
                    val, known_secret_values=known_secret_values
                ):
                    raise ValueError(
                        f"Operational secret value detected at {child_path} in {context}."
                    )
                stack.append((child_path, val))
        elif isinstance(current, (list, tuple)):
            for idx, item in enumerate(current):
                child_path = f"{path}[{idx}]"
                if isinstance(item, str) and contains_operational_secret_value(
                    item, known_secret_values=known_secret_values
                ):
                    raise ValueError(
                        f"Operational secret value detected at {child_path} in {context}."
                    )
                stack.append((child_path, item))


# =============================================================================
# Core Domain Exceptions
# =============================================================================
class LlmLifecycleError(Exception):
    """Fail-closed redacted domain error for subscription OAuth / consent / unlink lifecycle."""

    def __init__(
        self,
        *,
        code: LlmGateReasonCode,
        message: str,
        retryable: bool = False,
    ) -> None:
        safe_message = redact_llm_text(message)
        super().__init__(safe_message)
        self.code = code
        self.message = safe_message
        self.retryable = retryable


class LivePilotGateEligibilityError(ValueError):
    """Raised when a synthetic or non-approved run is submitted as live pilot gate evidence."""

    def __init__(self, message: str) -> None:
        super().__init__(redact_llm_text(message))
        self.code = LlmGateReasonCode.SYNTHETIC_NOT_ELIGIBLE_FOR_LIVE_GATE


# =============================================================================
# Core Domain Records
# =============================================================================
@dataclass(frozen=True, slots=True)
class HostedLlmApprovalConfig:
    """Default hard-OFF hosted subscription LLM approval flags (OD-002 / OD-014 / ADR-020)."""

    hosted_deployment_approved: bool = False
    zero_overage_guaranteed: bool = False
    od002_data_policy_verified: bool = False
    od014_token_store_verified: bool = False
    policy_version: str = DEFAULT_LLM_POLICY_VERSION

    @property
    def runtime_mode(self) -> LlmRuntimeMode:
        if (
            self.hosted_deployment_approved
            and self.zero_overage_guaranteed
            and self.od002_data_policy_verified
            and self.od014_token_store_verified
        ):
            return LlmRuntimeMode.APPROVED_SUBSCRIPTION
        return LlmRuntimeMode.OFF_MANUAL_ONLY

    def missing_approvals(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.hosted_deployment_approved:
            missing.append("hosted_deployment_approved")
        if not self.zero_overage_guaranteed:
            missing.append("zero_overage_guaranteed")
        if not self.od002_data_policy_verified:
            missing.append("od002_data_policy_verified")
        if not self.od014_token_store_verified:
            missing.append("od014_token_store_verified")
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class StageBudgetPolicy:
    """Deterministic zero-overage & bounded call/input budget contract.

    Note: ``max_output_tokens_hint`` is advisory ONLY and NEVER sufficient by itself
    to satisfy zero-overage guarantees; ``overage_credit_disabled`` and
    ``quota_remaining_known_and_sufficient`` MUST both be explicitly True.
    """

    max_input_bytes: int
    max_calls: int
    calls_used: int = 0
    max_repair_attempts: int = 1
    max_pre_spend_retries: int = 1
    overage_credit_disabled: bool = False
    quota_remaining_known_and_sufficient: bool = False
    quota_remaining_units: int | None = None
    estimated_required_units: int = 1
    max_output_tokens_hint: int | None = None
    paid_fallback_requested: bool = False

    def __post_init__(self) -> None:
        if self.max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive.")
        if self.max_calls <= 0:
            raise ValueError("max_calls must be positive.")
        if self.calls_used < 0:
            raise ValueError("calls_used cannot be negative.")
        if self.max_repair_attempts < 0 or self.max_repair_attempts > 1:
            raise ValueError("max_repair_attempts must be 0 or 1 (bounded repair cap).")
        if self.max_pre_spend_retries < 0:
            raise ValueError("max_pre_spend_retries cannot be negative.")
        if self.estimated_required_units <= 0:
            raise ValueError("estimated_required_units must be positive.")


@dataclass(frozen=True, slots=True)
class LlmGateDecision:
    """Deterministic fail-closed policy gate decision (0 external calls on block)."""

    allowed: bool
    mode: LlmGateOutcome
    reason_code: LlmGateReasonCode
    message: str
    retryable: bool = False
    external_call_count: int = 0
    free_alternatives: tuple[FreeAlternative, ...] = DEFAULT_FREE_ALTERNATIVES
    policy_version: str = DEFAULT_LLM_POLICY_VERSION
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", redact_llm_text(self.message))
        sanitized = redact_mapping(dict(self.details))
        assert_no_raw_text_or_secret_keys(sanitized, context="LlmGateDecision.details")
        object.__setattr__(self, "details", sanitized)
        if not self.allowed and self.mode is not LlmGateOutcome.MANUAL_ONLY_BLOCKED:
            raise ValueError("Blocked LlmGateDecision must use LlmGateOutcome.MANUAL_ONLY_BLOCKED.")
        if self.allowed and self.mode is not LlmGateOutcome.ALLOWED:
            raise ValueError("Allowed LlmGateDecision must use LlmGateOutcome.ALLOWED.")

    def to_audit_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "allowed": self.allowed,
            "mode": self.mode.value,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "retryable": self.retryable,
            "external_call_count": self.external_call_count,
            "free_alternatives": [alt.value for alt in self.free_alternatives],
            "policy_version": self.policy_version,
            "details": dict(self.details),
        }
        assert_no_raw_text_or_secret_keys(payload, context="LlmGateDecision.to_audit_dict")
        return payload


@dataclass(frozen=True, slots=True)
class SubscriptionConnectionRecord:
    """In-memory / port-backed subscription OAuth connection record (ZERO plaintext tokens)."""

    connection_id: str
    connection_version: int
    workspace_id: str
    owner_user_id: str
    provider: LlmProviderMode
    oidc_issuer: str
    oidc_subject: str
    granted_scopes: tuple[str, ...]
    plan_tier: str
    plan_inference_eligible: bool
    token_secret_ref: str | None
    token_expires_at: datetime
    status: SubscriptionConnectionStatus
    remote_revocation_status: RemoteRevocationStatus
    secret_wipe_status: SecretWipeStatus
    policy_version: str
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.token_secret_ref is not None:
            validate_token_secret_ref(self.token_secret_ref)
        if self.connection_version < 1:
            raise ValueError("connection_version must be >= 1.")

    def to_safe_dict(self) -> dict[str, Any]:
        """Serialize strictly non-secret, non-raw connection metadata for audit/UI."""
        payload: dict[str, Any] = {
            "connection_id": self.connection_id,
            "connection_version": self.connection_version,
            "workspace_id": self.workspace_id,
            "owner_user_id": self.owner_user_id,
            "provider": self.provider.value,
            "oidc_issuer": self.oidc_issuer,
            "oidc_subject": self.oidc_subject,
            "granted_scopes": list(self.granted_scopes),
            "plan_tier": self.plan_tier,
            "plan_inference_eligible": self.plan_inference_eligible,
            "token_expires_at": format_utc_timestamp(self.token_expires_at),
            "status": self.status.value,
            "remote_revocation_status": self.remote_revocation_status.value,
            "secret_wipe_status": self.secret_wipe_status.value,
            "policy_version": self.policy_version,
            "created_at": format_utc_timestamp(self.created_at),
            "updated_at": format_utc_timestamp(self.updated_at),
            "revoked_at": (
                format_utc_timestamp(self.revoked_at) if self.revoked_at is not None else None
            ),
        }
        assert_no_raw_text_or_secret_keys(payload, context="SubscriptionConnectionRecord")
        return payload

    def __repr__(self) -> str:
        return (
            "SubscriptionConnectionRecord("
            f"connection_id={self.connection_id!r}, "
            f"connection_version={self.connection_version}, "
            f"workspace_id={self.workspace_id!r}, "
            f"owner_user_id={self.owner_user_id!r}, "
            f"status={self.status.value!r}, "
            f"plan_tier={self.plan_tier!r}, "
            f"plan_inference_eligible={self.plan_inference_eligible}, "
            f"remote_revocation_status={self.remote_revocation_status.value!r}, "
            f"secret_wipe_status={self.secret_wipe_status.value!r})"
        )


@dataclass(frozen=True, slots=True)
class ExternalSendConsentRecord:
    """Separate per-repository external AI transmission consent (OD-002 + ADR-020)."""

    consent_id: str
    workspace_id: str
    source_resource_id: str
    consented_by_user_id: str
    consent_version: str
    acknowledged_public_unmasked_send: bool
    acknowledged_third_party_non_recallable: bool
    granted_at: datetime
    revoked_at: datetime | None = None
    revoked_by_user_id: str | None = None
    revocation_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return (
            self.revoked_at is None
            and self.acknowledged_public_unmasked_send is True
            and self.acknowledged_third_party_non_recallable is True
        )

    def to_safe_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "consent_id": self.consent_id,
            "workspace_id": self.workspace_id,
            "source_resource_id": self.source_resource_id,
            "consented_by_user_id": self.consented_by_user_id,
            "consent_version": self.consent_version,
            "acknowledged_public_unmasked_send": self.acknowledged_public_unmasked_send,
            "acknowledged_third_party_non_recallable": self.acknowledged_third_party_non_recallable,
            "granted_at": format_utc_timestamp(self.granted_at),
            "revoked_at": (
                format_utc_timestamp(self.revoked_at) if self.revoked_at is not None else None
            ),
            "revoked_by_user_id": self.revoked_by_user_id,
            "revocation_reason": self.revocation_reason,
        }
        assert_no_raw_text_or_secret_keys(payload, context="ExternalSendConsentRecord")
        return payload


_ALLOWED_JOB_REFERENCE_KEYS: frozenset[str] = frozenset(
    {
        "connection_id",
        "connection_version",
        "owner_user_id",
        "workspace_id",
        "consent_version",
        "policy_version",
        "stage_code",
        "input_hash",
    }
)


@dataclass(frozen=True, slots=True)
class ConnectionJobReference:
    """Strictly non-raw, non-secret job/generation_run reference for subscription execution."""

    connection_id: str
    connection_version: int
    owner_user_id: str
    workspace_id: str
    consent_version: str
    policy_version: str
    stage_code: LlmStageCode
    input_hash: str

    def __post_init__(self) -> None:
        if not self.connection_id.strip():
            raise ValueError("connection_id must be non-empty.")
        if self.connection_version < 1:
            raise ValueError("connection_version must be >= 1.")
        if not self.owner_user_id.strip():
            raise ValueError("owner_user_id must be non-empty.")
        if not self.workspace_id.strip():
            raise ValueError("workspace_id must be non-empty.")
        if not self.consent_version.strip():
            raise ValueError("consent_version must be non-empty.")
        if not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty.")
        object.__setattr__(
            self,
            "input_hash",
            normalize_sha256_hash(self.input_hash, field_name="input_hash"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "connection_id": self.connection_id,
            "connection_version": self.connection_version,
            "owner_user_id": self.owner_user_id,
            "workspace_id": self.workspace_id,
            "consent_version": self.consent_version,
            "policy_version": self.policy_version,
            "stage_code": self.stage_code.value,
            "input_hash": self.input_hash,
        }
        assert_no_raw_text_or_secret_keys(payload, context="ConnectionJobReference")
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConnectionJobReference:
        if not isinstance(data, Mapping):
            raise ValueError("ConnectionJobReference payload must be a mapping.")
        assert_no_raw_text_or_secret_keys(data, context="ConnectionJobReference")
        extra_keys = sorted(set(str(k) for k in data.keys()) - _ALLOWED_JOB_REFERENCE_KEYS)
        if extra_keys:
            raise ValueError(
                f"ConnectionJobReference contains unexpected key(s): {', '.join(extra_keys)}"
            )
        missing_keys = sorted(_ALLOWED_JOB_REFERENCE_KEYS - set(str(k) for k in data.keys()))
        if missing_keys:
            raise ValueError(
                f"ConnectionJobReference is missing required key(s): {', '.join(missing_keys)}"
            )
        return cls(
            connection_id=str(data["connection_id"]),
            connection_version=int(data["connection_version"]),
            owner_user_id=str(data["owner_user_id"]),
            workspace_id=str(data["workspace_id"]),
            consent_version=str(data["consent_version"]),
            policy_version=str(data["policy_version"]),
            stage_code=LlmStageCode(str(data["stage_code"])),
            input_hash=str(data["input_hash"]),
        )


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)
