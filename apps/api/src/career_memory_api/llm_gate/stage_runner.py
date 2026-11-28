"""Bounded 5-stage AI execution runner, public unmasked passthrough, and pre-commit fences."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.conformance.ai_schemas import validate_stage_payload
from career_memory_api.resolver.models import (
    OperationMemoryResolution,
    ResolutionStatus,
    format_utc_timestamp,
    normalize_sha256_hash,
)
from career_memory_api.schema_contracts.enums import ResourceVisibility

from .models import (
    DEFAULT_FREE_ALTERNATIVES,
    FORBIDDEN_OPERATIONAL_HEADER_NAMES,
    STAGE_CODE_TO_SCHEMA_FILE,
    ConnectionJobReference,
    ExecutionOrigin,
    FreeAlternative,
    LivePilotGateEligibilityError,
    LlmGateDecision,
    LlmGateOutcome,
    LlmGateReasonCode,
    LlmProviderMode,
    LlmRuntimeMode,
    LlmStageCode,
    StageBudgetPolicy,
    StageRunStatus,
    assert_no_raw_text_or_secret_keys,
    contains_operational_secret_value,
    redact_llm_text,
    utc_now,
)
from .oauth_lifecycle import SubscriptionConnectionLifecycleManager
from .policy_gate import LlmPolicyGate, PolicyGateRequest

logger = logging.getLogger(__name__)


class ProviderTransportErrorKind(StrEnum):
    """Deterministic classification of stage adapter transport outcomes."""

    NONE = "NONE"
    PRE_SEND_CONNECTION_ERROR = "PRE_SEND_CONNECTION_ERROR"
    RATE_LIMITED_429 = "RATE_LIMITED_429"
    SERVICE_UNAVAILABLE_503 = "SERVICE_UNAVAILABLE_503"
    AMBIGUOUS_POST_SEND_TIMEOUT = "AMBIGUOUS_POST_SEND_TIMEOUT"
    STREAM_TRUNCATED = "STREAM_TRUNCATED"
    UPSTREAM_AUTH_REVOKED = "UPSTREAM_AUTH_REVOKED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class StageAdapterInput:
    """Ephemeral in-memory input passed to a StageExecutionAdapterPort.

    - ``ephemeral_source_text`` carries consented PUBLIC GitHub source text **as-is**
      without PII/sensitivity scanning or masking (ADR-020 / PM-SEC-01 Post-MVP).
    - ``ephemeral_source_text`` is strictly redacted from ``repr()`` / ``str()`` and
      cannot be pickled or persisted.
    - ``sanitized_headers`` and ``context_metadata`` are strictly scrubbed of server
      operational credentials.
    """

    stage_code: LlmStageCode
    schema_file: str
    operation_id: str
    workspace_id: str
    source_object_revision_id: str
    content_unit_id: str
    expected_revision_hash: str
    span_sha256: str
    ephemeral_source_text: str = field(repr=False)
    context_metadata: Mapping[str, Any] = field(default_factory=dict)
    sanitized_headers: Mapping[str, str] = field(default_factory=dict)
    repair_attempt: int = 0
    previous_validation_errors: tuple[str, ...] = ()
    tools_enabled: Literal[False] = False

    def __post_init__(self) -> None:
        if self.tools_enabled is not False:
            raise ValueError("tools_enabled must always be False (no tool/function side effects).")

    def __getstate__(self) -> Any:
        raise TypeError("StageAdapterInput is strictly ephemeral and cannot be serialized.")

    def __repr__(self) -> str:
        return (
            "StageAdapterInput("
            f"stage_code={self.stage_code.value!r}, "
            f"schema_file={self.schema_file!r}, "
            f"operation_id={self.operation_id!r}, "
            f"content_unit_id={self.content_unit_id!r}, "
            f"repair_attempt={self.repair_attempt}, "
            f"tools_enabled={self.tools_enabled}, "
            "ephemeral_source_text='<REDACTED_EPHEMERAL_TEXT>')"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class RawProviderStageResponse:
    """Raw stage adapter outcome evaluated for stream completeness, tools, and T-106 schema."""

    payload: Any = None
    stream_completed: bool = True
    contains_tool_calls: bool = False
    unsupported_output_kind: str | None = None
    transport_error: ProviderTransportErrorKind = ProviderTransportErrorKind.NONE
    http_status: int | None = None
    request_transmitted: bool = True
    error_message: str | None = None


class StageExecutionAdapterPort(Protocol):
    """Port implemented by SyntheticFixtureStageAdapter and PolicyGatedProviderStageAdapter."""

    @property
    def execution_origin(self) -> ExecutionOrigin:
        ...

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        ...


class SyntheticFixtureStageAdapter:
    """Offline deterministic fixture adapter for Wave 2/3/4 synthetic tests (0 external calls)."""

    def __init__(
        self,
        *,
        fixtures_by_stage: Mapping[str | LlmStageCode, Any],
        on_invoke: Callable[[StageAdapterInput], None] | None = None,
    ) -> None:
        self._fixtures: dict[str, Any] = {
            (k.value if isinstance(k, LlmStageCode) else str(k)): v
            for k, v in fixtures_by_stage.items()
        }
        self._on_invoke = on_invoke
        self.call_count = 0

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return "SYNTHETIC_FIXTURE"

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        self.call_count += 1
        if self._on_invoke is not None:
            self._on_invoke(adapter_input)
        fixture = self._fixtures.get(adapter_input.stage_code.value)
        if callable(fixture):
            result = fixture(adapter_input)
            if isinstance(result, RawProviderStageResponse):
                return result
            return RawProviderStageResponse(payload=result, stream_completed=True)
        return RawProviderStageResponse(payload=fixture, stream_completed=True)


class PolicyGatedProviderStageAdapter:
    """Injectable mock/provider transport wrapper (ZERO live network calls in T-408)."""

    def __init__(
        self,
        *,
        transport: Callable[[StageAdapterInput], RawProviderStageResponse],
    ) -> None:
        self._transport = transport
        self.external_call_count = 0

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return "APPROVED_SUBSCRIPTION"

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        self.external_call_count += 1
        return self._transport(adapter_input)


@dataclass(frozen=True, slots=True)
class StagePreCommitContext:
    """Non-raw context handed to ``before_commit_hook`` immediately before stage commit."""

    stage_code: LlmStageCode
    operation_id: str
    workspace_id: str
    source_resource_id: str
    content_unit_id: str
    execution_origin: ExecutionOrigin
    connection_id: str | None
    connection_version: int | None


@dataclass(frozen=True, slots=True)
class StageExecutionRequest:
    """Caller request to execute one bounded AI pipeline stage through T-408 gates."""

    access: WorkspaceAccessContext
    stage_code: LlmStageCode
    provider_mode: LlmProviderMode
    source_resource_id: str
    visibility: ResourceVisibility | str
    resolution: OperationMemoryResolution
    expected_revision_hash: str
    expected_span_sha256: str
    budget: StageBudgetPolicy
    adapter: StageExecutionAdapterPort
    job_reference: ConnectionJobReference | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    context_metadata: Mapping[str, Any] = field(default_factory=dict)
    allow_synthetic_fixture_mode: bool = False
    before_commit_hook: Callable[[StagePreCommitContext], bool | None] | None = None
    verify_workspace_membership_active: Callable[[WorkspaceAccessContext], bool] | None = None
    close_resolution_on_exit: bool = True


@dataclass(frozen=True, slots=True)
class StageRunEnvelope:
    """Non-raw, redacted stage execution result separating synthetic vs live provenance."""

    stage_code: LlmStageCode
    schema_file: str
    status: StageRunStatus
    reason_code: LlmGateReasonCode
    execution_origin: ExecutionOrigin
    live_pilot_gate_eligible: bool
    committed_allowed: bool
    can_promote_to_supported: bool
    retryable: bool
    auto_retry_performed: bool
    external_call_count: int
    repair_attempts_used: int
    checked_at: datetime
    decision: LlmGateDecision
    validated_payload: Mapping[str, Any] | None = None
    validation_error_codes: tuple[str, ...] = ()
    free_alternatives: tuple[FreeAlternative, ...] = DEFAULT_FREE_ALTERNATIVES
    job_reference: ConnectionJobReference | None = None

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialize strictly non-raw, non-secret audit metadata for generation_runs/logs."""
        payload: dict[str, Any] = {
            "stage_code": self.stage_code.value,
            "schema_file": self.schema_file,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "execution_origin": self.execution_origin,
            "live_pilot_gate_eligible": self.live_pilot_gate_eligible,
            "committed_allowed": self.committed_allowed,
            "can_promote_to_supported": self.can_promote_to_supported,
            "retryable": self.retryable,
            "auto_retry_performed": self.auto_retry_performed,
            "external_call_count": self.external_call_count,
            "repair_attempts_used": self.repair_attempts_used,
            "checked_at": format_utc_timestamp(self.checked_at),
            "decision": self.decision.to_audit_dict(),
            "validation_error_codes": list(self.validation_error_codes),
            "free_alternatives": [alt.value for alt in self.free_alternatives],
            "job_reference": self.job_reference.to_dict() if self.job_reference is not None else None,
        }
        assert_no_raw_text_or_secret_keys(payload, context="StageRunEnvelope.to_audit_dict")
        return payload


def assert_not_synthetic_for_live_gate(envelope: StageRunEnvelope) -> None:
    """Hard guard preventing Wave 2/3/4 from mistaking synthetic pass for live AI pilot pass."""
    if envelope.execution_origin == "SYNTHETIC_FIXTURE" or not envelope.live_pilot_gate_eligible:
        raise LivePilotGateEligibilityError(
            "Synthetic or non-approved LLM stage execution cannot satisfy the live AI pilot gate."
        )


def _sanitize_operational_headers(
    raw_headers: Mapping[str, str],
    *,
    known_secret_values: tuple[str, ...],
) -> dict[str, str]:
    """Strip Authorization, Cookie, Set-Cookie, API key, and secret-bearing headers."""
    cleaned: dict[str, str] = {}
    for raw_key, raw_val in raw_headers.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if key.lower() in FORBIDDEN_OPERATIONAL_HEADER_NAMES:
            continue
        val_str = str(raw_val)
        if contains_operational_secret_value(val_str, known_secret_values=known_secret_values):
            continue
        cleaned[key] = val_str
    return cleaned


def _extract_verified_ephemeral_text(
    *,
    resolution: OperationMemoryResolution,
    expected_revision_hash: str,
    expected_span_sha256: str,
) -> tuple[str | None, LlmGateReasonCode | None, str | None]:
    """Verify Plan C OperationMemoryResolution match before building model input."""
    meta = resolution.metadata
    if resolution.closed or meta.status is not ResolutionStatus.MATCHED:
        return (
            None,
            LlmGateReasonCode.RESOLUTION_NOT_MATCHED,
            f"OperationMemoryResolution must be open and MATCHED (got {meta.status.value}).",
        )

    norm_expected_rev = normalize_sha256_hash(expected_revision_hash, field_name="expected_revision_hash")
    norm_expected_span = normalize_sha256_hash(expected_span_sha256, field_name="expected_span_sha256")
    if (
        meta.expected_revision_hash != norm_expected_rev
        or meta.observed_revision_hash != norm_expected_rev
    ):
        return (
            None,
            LlmGateReasonCode.RESOLUTION_HASH_MISMATCH,
            "OperationMemoryResolution revision hash does not match expected_revision_hash.",
        )

    if meta.expected_unit_hash != norm_expected_span or meta.observed_unit_hash != norm_expected_span:
        return (
            None,
            LlmGateReasonCode.RESOLUTION_HASH_MISMATCH,
            "OperationMemoryResolution unit hash does not match expected_span_sha256.",
        )

    if resolution.ephemeral_span is not None:
        if resolution.ephemeral_span.locator.span_sha256 != norm_expected_span:
            return (
                None,
                LlmGateReasonCode.RESOLUTION_HASH_MISMATCH,
                "EphemeralVerifiedSpan span_sha256 mismatch.",
            )
        text = resolution.ephemeral_span.text
    elif resolution.ephemeral_unit is not None:
        if resolution.ephemeral_unit.metadata.locator.span_sha256 != norm_expected_span:
            return (
                None,
                LlmGateReasonCode.RESOLUTION_HASH_MISMATCH,
                "EphemeralResolvedUnit span_sha256 mismatch.",
            )
        text = resolution.ephemeral_unit.text
    else:
        return (
            None,
            LlmGateReasonCode.RESOLUTION_NOT_MATCHED,
            "OperationMemoryResolution contains no ephemeral unit or span text.",
        )

    if not text:
        return (
            None,
            LlmGateReasonCode.RESOLUTION_NOT_MATCHED,
            "Resolved ephemeral source text is empty.",
        )
    return text, None, None


class BoundedStageRunner:
    """Executes the 5 bounded AI stages with strict pre-flight, runtime, and pre-commit fences."""

    def __init__(
        self,
        *,
        policy_gate: LlmPolicyGate,
        lifecycle_manager: SubscriptionConnectionLifecycleManager | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._policy_gate = policy_gate
        self._lifecycle = lifecycle_manager
        self._clock = clock

    def _is_membership_active(self, request: StageExecutionRequest) -> bool:
        if request.verify_workspace_membership_active is None:
            return True
        try:
            return bool(request.verify_workspace_membership_active(request.access))
        except Exception:  # noqa: BLE001
            return False

    def _lookup_live_policy_state(
        self, request: StageExecutionRequest
    ) -> tuple[Any, Any, bool, tuple[str, ...]]:
        if self._lifecycle is None:
            return None, None, False, ()
        conn_id = request.job_reference.connection_id if request.job_reference is not None else None
        conn = self._lifecycle.get_connection(conn_id) if conn_id is not None else None
        consent = self._lifecycle.get_external_send_consent(
            workspace_id=request.access.workspace_id,
            source_resource_id=request.source_resource_id,
        )
        deletion_fenced = self._lifecycle.is_source_deletion_fenced(
            request.access.workspace_id,
            request.source_resource_id,
        )
        known_secrets = (
            self._lifecycle.get_known_secret_values_for_connection(conn_id)
            if conn_id is not None
            else ()
        )
        return conn, consent, deletion_fenced, known_secrets

    def _make_blocked_envelope(
        self,
        *,
        request: StageExecutionRequest,
        schema_file: str,
        decision: LlmGateDecision,
        now: datetime,
    ) -> StageRunEnvelope:
        return StageRunEnvelope(
            stage_code=request.stage_code,
            schema_file=schema_file,
            status=StageRunStatus.BLOCKED,
            reason_code=decision.reason_code,
            execution_origin=request.adapter.execution_origin,
            live_pilot_gate_eligible=False,
            committed_allowed=False,
            can_promote_to_supported=False,
            retryable=False,
            auto_retry_performed=False,
            external_call_count=0,
            repair_attempts_used=0,
            checked_at=now,
            decision=decision,
            validated_payload=None,
            free_alternatives=decision.free_alternatives,
            job_reference=request.job_reference,
        )

    def run_stage(self, request: StageExecutionRequest) -> StageRunEnvelope:
        """Execute a single bounded AI stage following the exact 8-step T-408 / INF-08 sequence."""
        schema_file = STAGE_CODE_TO_SCHEMA_FILE[request.stage_code]
        now = self._clock()
        ephemeral_text: str | None = None
        adapter_input: StageAdapterInput | None = None

        try:
            conn, consent, deletion_fenced, known_secrets = self._lookup_live_policy_state(request)

            # 0. Reject operational secrets in context_metadata immediately (0 calls).
            if request.context_metadata:
                try:
                    assert_no_raw_text_or_secret_keys(
                        request.context_metadata,
                        context="StageExecutionRequest.context_metadata",
                        known_secret_values=known_secrets,
                    )
                except ValueError:
                    blocked = LlmGateDecision(
                        allowed=False,
                        mode=LlmGateOutcome.MANUAL_ONLY_BLOCKED,
                        reason_code=LlmGateReasonCode.OPERATIONAL_SECRET_IN_PAYLOAD_BLOCKED,
                        message="Server operational secret or raw-text key blocked from adapter metadata.",
                        retryable=False,
                        external_call_count=0,
                        free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                        policy_version=self._policy_gate.approval_config.policy_version,
                    )
                    return self._make_blocked_envelope(
                        request=request,
                        schema_file=schema_file,
                        decision=blocked,
                        now=now,
                    )

            # 1. Verify Plan C OperationMemoryResolution before sending anything.
            ephemeral_text, res_err_code, res_err_msg = _extract_verified_ephemeral_text(
                resolution=request.resolution,
                expected_revision_hash=request.expected_revision_hash,
                expected_span_sha256=request.expected_span_sha256,
            )
            if res_err_code is not None or ephemeral_text is None:
                blocked = LlmGateDecision(
                    allowed=False,
                    mode=LlmGateOutcome.MANUAL_ONLY_BLOCKED,
                    reason_code=res_err_code or LlmGateReasonCode.RESOLUTION_NOT_MATCHED,
                    message=res_err_msg or "OperationMemoryResolution failed verification.",
                    retryable=False,
                    external_call_count=0,
                    free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                    policy_version=self._policy_gate.approval_config.policy_version,
                )
                return self._make_blocked_envelope(
                    request=request,
                    schema_file=schema_file,
                    decision=blocked,
                    now=now,
                )

            input_byte_length = len(ephemeral_text.encode("utf-8"))

            # 2. Evaluate central fail-closed policy & zero-overage budget gate (0 calls on block).
            decision = self._policy_gate.evaluate(
                PolicyGateRequest(
                    access=request.access,
                    provider_mode=request.provider_mode,
                    stage_code=request.stage_code,
                    source_resource_id=request.source_resource_id,
                    visibility=request.visibility,
                    input_byte_length=input_byte_length,
                    budget=request.budget,
                    connection=conn,
                    consent=consent,
                    job_reference=request.job_reference,
                    deletion_fenced=deletion_fenced,
                    allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                    workspace_membership_active=self._is_membership_active(request),
                    now=now,
                )
            )
            if not decision.allowed:
                return self._make_blocked_envelope(
                    request=request,
                    schema_file=schema_file,
                    decision=decision,
                    now=now,
                )

            # 2b. Prevent cross-wiring synthetic adapter with live mode or vice versa (0 calls).
            if (
                request.provider_mode is LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE
                and request.adapter.execution_origin != "SYNTHETIC_FIXTURE"
            ):
                blocked = LlmGateDecision(
                    allowed=False,
                    mode=LlmGateOutcome.MANUAL_ONLY_BLOCKED,
                    reason_code=LlmGateReasonCode.PROHIBITED_PROVIDER_MODE,
                    message="SYNTHETIC_FIXTURE_OFFLINE mode requires a SYNTHETIC_FIXTURE adapter.",
                    retryable=False,
                    external_call_count=0,
                    free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                    policy_version=self._policy_gate.approval_config.policy_version,
                )
                return self._make_blocked_envelope(
                    request=request,
                    schema_file=schema_file,
                    decision=blocked,
                    now=now,
                )
            if (
                request.provider_mode is LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH
                and request.adapter.execution_origin != "APPROVED_SUBSCRIPTION"
            ):
                blocked = LlmGateDecision(
                    allowed=False,
                    mode=LlmGateOutcome.MANUAL_ONLY_BLOCKED,
                    reason_code=LlmGateReasonCode.SYNTHETIC_NOT_ELIGIBLE_FOR_LIVE_GATE,
                    message="CHATGPT_SUBSCRIPTION_OAUTH mode cannot execute on a SYNTHETIC_FIXTURE adapter.",
                    retryable=False,
                    external_call_count=0,
                    free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                    policy_version=self._policy_gate.approval_config.policy_version,
                )
                return self._make_blocked_envelope(
                    request=request,
                    schema_file=schema_file,
                    decision=blocked,
                    now=now,
                )

            # 3. Strip operational credentials from headers; pass PUBLIC ephemeral source text AS-IS!
            sanitized_headers = _sanitize_operational_headers(
                request.extra_headers,
                known_secret_values=known_secrets,
            )

            is_external = request.adapter.execution_origin == "APPROVED_SUBSCRIPTION"
            remaining_calls = max(0, request.budget.max_calls - request.budget.calls_used)
            calls_made = 0
            external_calls_made = 0
            repair_attempts_used = 0
            pre_spend_retries_used = 0
            auto_retry_performed = False
            previous_errors: tuple[str, ...] = ()

            validated_payload: dict[str, Any] | None = None

            while calls_made < remaining_calls:
                adapter_input = StageAdapterInput(
                    stage_code=request.stage_code,
                    schema_file=schema_file,
                    operation_id=request.resolution.metadata.operation_id,
                    workspace_id=request.access.workspace_id,
                    source_object_revision_id=request.resolution.metadata.source_object_revision_id,
                    content_unit_id=request.resolution.metadata.content_unit_id,
                    expected_revision_hash=request.resolution.metadata.expected_revision_hash,
                    span_sha256=request.resolution.metadata.expected_unit_hash,
                    ephemeral_source_text=ephemeral_text,
                    context_metadata=dict(request.context_metadata),
                    sanitized_headers=sanitized_headers,
                    repair_attempt=repair_attempts_used,
                    previous_validation_errors=previous_errors,
                    tools_enabled=False,
                )

                calls_made += 1
                if is_external:
                    external_calls_made += 1

                response = request.adapter.execute_stage(adapter_input)

                # 4. Handle transport & timeout outcomes.
                err_kind = response.transport_error
                if err_kind is ProviderTransportErrorKind.AMBIGUOUS_POST_SEND_TIMEOUT:
                    # MUST NOT auto-retry ambiguous post-send timeouts (prevent double quota spend)!
                    return StageRunEnvelope(
                        stage_code=request.stage_code,
                        schema_file=schema_file,
                        status=StageRunStatus.FAILED_AMBIGUOUS_TIMEOUT,
                        reason_code=LlmGateReasonCode.AMBIGUOUS_POST_SEND_TIMEOUT,
                        execution_origin=request.adapter.execution_origin,
                        live_pilot_gate_eligible=False,
                        committed_allowed=False,
                        can_promote_to_supported=False,
                        retryable=False,
                        auto_retry_performed=False,
                        external_call_count=external_calls_made,
                        repair_attempts_used=repair_attempts_used,
                        checked_at=self._clock(),
                        decision=decision,
                        validated_payload=None,
                        free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                        job_reference=request.job_reference,
                    )

                if err_kind in {
                    ProviderTransportErrorKind.PRE_SEND_CONNECTION_ERROR,
                    ProviderTransportErrorKind.RATE_LIMITED_429,
                    ProviderTransportErrorKind.SERVICE_UNAVAILABLE_503,
                }:
                    # Retryable ONLY when confirmed pre-spend/not-transmitted and within caps.
                    can_retry_pre_spend = (
                        not response.request_transmitted
                        and pre_spend_retries_used < request.budget.max_pre_spend_retries
                        and calls_made < remaining_calls
                    )
                    if can_retry_pre_spend:
                        # Re-check policy & live fences before retrying.
                        re_conn, re_consent, re_del, _ = self._lookup_live_policy_state(request)
                        re_decision = self._policy_gate.evaluate(
                            PolicyGateRequest(
                                access=request.access,
                                provider_mode=request.provider_mode,
                                stage_code=request.stage_code,
                                source_resource_id=request.source_resource_id,
                                visibility=request.visibility,
                                input_byte_length=input_byte_length,
                                budget=StageBudgetPolicy(
                                    max_input_bytes=request.budget.max_input_bytes,
                                    max_calls=request.budget.max_calls,
                                    calls_used=request.budget.calls_used + calls_made,
                                    max_repair_attempts=request.budget.max_repair_attempts,
                                    max_pre_spend_retries=request.budget.max_pre_spend_retries,
                                    overage_credit_disabled=request.budget.overage_credit_disabled,
                                    quota_remaining_known_and_sufficient=request.budget.quota_remaining_known_and_sufficient,
                                    quota_remaining_units=request.budget.quota_remaining_units,
                                    estimated_required_units=request.budget.estimated_required_units,
                                    max_output_tokens_hint=request.budget.max_output_tokens_hint,
                                    paid_fallback_requested=request.budget.paid_fallback_requested,
                                ),
                                connection=re_conn,
                                consent=re_consent,
                                job_reference=request.job_reference,
                                deletion_fenced=re_del,
                                allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                                workspace_membership_active=self._is_membership_active(request),
                                now=self._clock(),
                            )
                        )
                        if re_decision.allowed:
                            pre_spend_retries_used += 1
                            auto_retry_performed = True
                            continue

                    return StageRunEnvelope(
                        stage_code=request.stage_code,
                        schema_file=schema_file,
                        status=StageRunStatus.FAILED_RETRYABLE_PRE_SPEND,
                        reason_code=LlmGateReasonCode.RETRYABLE_PRE_SEND_OR_THROTTLED_FAILURE,
                        execution_origin=request.adapter.execution_origin,
                        live_pilot_gate_eligible=False,
                        committed_allowed=False,
                        can_promote_to_supported=False,
                        retryable=(not response.request_transmitted),
                        auto_retry_performed=auto_retry_performed,
                        external_call_count=external_calls_made,
                        repair_attempts_used=repair_attempts_used,
                        checked_at=self._clock(),
                        decision=decision,
                        validated_payload=None,
                        free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                        job_reference=request.job_reference,
                    )

                if err_kind in {
                    ProviderTransportErrorKind.UPSTREAM_AUTH_REVOKED,
                    ProviderTransportErrorKind.QUOTA_EXHAUSTED,
                }:
                    return StageRunEnvelope(
                        stage_code=request.stage_code,
                        schema_file=schema_file,
                        status=StageRunStatus.FAILED_UPSTREAM_TERMINATED,
                        reason_code=LlmGateReasonCode.UPSTREAM_AUTH_OR_QUOTA_TERMINATED,
                        execution_origin=request.adapter.execution_origin,
                        live_pilot_gate_eligible=False,
                        committed_allowed=False,
                        can_promote_to_supported=False,
                        retryable=False,
                        auto_retry_performed=auto_retry_performed,
                        external_call_count=external_calls_made,
                        repair_attempts_used=repair_attempts_used,
                        checked_at=self._clock(),
                        decision=decision,
                        validated_payload=None,
                        free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                        job_reference=request.job_reference,
                    )

                # 5. Validate complete stream termination (truncated stream fails closed).
                if (
                    not response.stream_completed
                    or err_kind is ProviderTransportErrorKind.STREAM_TRUNCATED
                ):
                    return StageRunEnvelope(
                        stage_code=request.stage_code,
                        schema_file=schema_file,
                        status=StageRunStatus.FAILED_STREAM_INCOMPLETE,
                        reason_code=LlmGateReasonCode.STREAM_INCOMPLETE,
                        execution_origin=request.adapter.execution_origin,
                        live_pilot_gate_eligible=False,
                        committed_allowed=False,
                        can_promote_to_supported=False,
                        retryable=False,
                        auto_retry_performed=auto_retry_performed,
                        external_call_count=external_calls_made,
                        repair_attempts_used=repair_attempts_used,
                        checked_at=self._clock(),
                        decision=decision,
                        validated_payload=None,
                        free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                        job_reference=request.job_reference,
                    )

                # 6. Reject tool/function-calling side effects or unsupported output shapes.
                has_tool_keys = isinstance(response.payload, Mapping) and any(
                    str(k).strip().lower() in {"tool_calls", "function_call"}
                    for k in response.payload.keys()
                )
                if (
                    response.contains_tool_calls
                    or response.unsupported_output_kind is not None
                    or has_tool_keys
                ):
                    return StageRunEnvelope(
                        stage_code=request.stage_code,
                        schema_file=schema_file,
                        status=StageRunStatus.FAILED_UNSUPPORTED_OUTPUT,
                        reason_code=LlmGateReasonCode.UNSUPPORTED_OUTPUT_OR_TOOL_SIDE_EFFECT,
                        execution_origin=request.adapter.execution_origin,
                        live_pilot_gate_eligible=False,
                        committed_allowed=False,
                        can_promote_to_supported=False,
                        retryable=False,
                        auto_retry_performed=auto_retry_performed,
                        external_call_count=external_calls_made,
                        repair_attempts_used=repair_attempts_used,
                        checked_at=self._clock(),
                        decision=decision,
                        validated_payload=None,
                        free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                        job_reference=request.job_reference,
                    )

                # 7. Validate JSON payload against T-106 Draft 2020-12 schema & Plan C invariants.
                violations = validate_stage_payload(
                    schema_file,
                    response.payload,
                    raise_on_error=False,
                )
                if not violations and isinstance(response.payload, Mapping):
                    validated_payload = dict(response.payload)
                    break

                error_codes = tuple(sorted({v.code for v in violations})) or ("INVALID_JSON_OBJECT",)
                # Allow AT MOST 1 bounded repair call ONLY if budget & live policy still allow.
                if (
                    repair_attempts_used < request.budget.max_repair_attempts
                    and calls_made < remaining_calls
                ):
                    re_conn, re_consent, re_del, _ = self._lookup_live_policy_state(request)
                    repair_decision = self._policy_gate.evaluate(
                        PolicyGateRequest(
                            access=request.access,
                            provider_mode=request.provider_mode,
                            stage_code=request.stage_code,
                            source_resource_id=request.source_resource_id,
                            visibility=request.visibility,
                            input_byte_length=input_byte_length,
                            budget=StageBudgetPolicy(
                                max_input_bytes=request.budget.max_input_bytes,
                                max_calls=request.budget.max_calls,
                                calls_used=request.budget.calls_used + calls_made,
                                max_repair_attempts=request.budget.max_repair_attempts,
                                max_pre_spend_retries=request.budget.max_pre_spend_retries,
                                overage_credit_disabled=request.budget.overage_credit_disabled,
                                quota_remaining_known_and_sufficient=request.budget.quota_remaining_known_and_sufficient,
                                quota_remaining_units=request.budget.quota_remaining_units,
                                estimated_required_units=request.budget.estimated_required_units,
                                max_output_tokens_hint=request.budget.max_output_tokens_hint,
                                paid_fallback_requested=request.budget.paid_fallback_requested,
                            ),
                            connection=re_conn,
                            consent=re_consent,
                            job_reference=request.job_reference,
                            deletion_fenced=re_del,
                            allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                            workspace_membership_active=self._is_membership_active(request),
                            now=self._clock(),
                        )
                    )
                    if repair_decision.allowed:
                        repair_attempts_used += 1
                        previous_errors = tuple(
                            redact_llm_text(f"{v.path} [{v.code}]") for v in violations[:5]
                        )
                        continue

                return StageRunEnvelope(
                    stage_code=request.stage_code,
                    schema_file=schema_file,
                    status=StageRunStatus.DRAFT_INVALID,
                    reason_code=LlmGateReasonCode.AI_SCHEMA_INVALID,
                    execution_origin=request.adapter.execution_origin,
                    live_pilot_gate_eligible=False,
                    committed_allowed=False,
                    can_promote_to_supported=False,
                    retryable=False,
                    auto_retry_performed=auto_retry_performed,
                    external_call_count=external_calls_made,
                    repair_attempts_used=repair_attempts_used,
                    checked_at=self._clock(),
                    decision=decision,
                    validated_payload=None,
                    validation_error_codes=error_codes,
                    free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                    job_reference=request.job_reference,
                )

            if validated_payload is None:
                return StageRunEnvelope(
                    stage_code=request.stage_code,
                    schema_file=schema_file,
                    status=StageRunStatus.DRAFT_INVALID,
                    reason_code=LlmGateReasonCode.DRAFT_INVALID,
                    execution_origin=request.adapter.execution_origin,
                    live_pilot_gate_eligible=False,
                    committed_allowed=False,
                    can_promote_to_supported=False,
                    retryable=False,
                    auto_retry_performed=auto_retry_performed,
                    external_call_count=external_calls_made,
                    repair_attempts_used=repair_attempts_used,
                    checked_at=self._clock(),
                    decision=decision,
                    validated_payload=None,
                    free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                    job_reference=request.job_reference,
                )

            # 8. Pre-Commit Re-Verification Fence (before_commit_hook + live connection/consent/deletion check).
            if request.before_commit_hook is not None:
                hook_ctx = StagePreCommitContext(
                    stage_code=request.stage_code,
                    operation_id=request.resolution.metadata.operation_id,
                    workspace_id=request.access.workspace_id,
                    source_resource_id=request.source_resource_id,
                    content_unit_id=request.resolution.metadata.content_unit_id,
                    execution_origin=request.adapter.execution_origin,
                    connection_id=(
                        request.job_reference.connection_id
                        if request.job_reference is not None
                        else None
                    ),
                    connection_version=(
                        request.job_reference.connection_version
                        if request.job_reference is not None
                        else None
                    ),
                )
                try:
                    hook_ok = request.before_commit_hook(hook_ctx)
                except Exception:  # noqa: BLE001
                    hook_ok = False
                if hook_ok is False:
                    return StageRunEnvelope(
                        stage_code=request.stage_code,
                        schema_file=schema_file,
                        status=StageRunStatus.CANCELED_PRE_COMMIT_FENCE,
                        reason_code=LlmGateReasonCode.PRE_COMMIT_FENCE_REJECTED,
                        execution_origin=request.adapter.execution_origin,
                        live_pilot_gate_eligible=False,
                        committed_allowed=False,
                        can_promote_to_supported=False,
                        retryable=False,
                        auto_retry_performed=auto_retry_performed,
                        external_call_count=external_calls_made,
                        repair_attempts_used=repair_attempts_used,
                        checked_at=self._clock(),
                        decision=decision,
                        validated_payload=None,
                        free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                        job_reference=request.job_reference,
                    )

            post_conn, post_consent, post_del_fenced, _ = self._lookup_live_policy_state(request)
            post_decision = self._policy_gate.evaluate(
                PolicyGateRequest(
                    access=request.access,
                    provider_mode=request.provider_mode,
                    stage_code=request.stage_code,
                    source_resource_id=request.source_resource_id,
                    visibility=request.visibility,
                    input_byte_length=input_byte_length,
                    budget=request.budget,
                    connection=post_conn,
                    consent=post_consent,
                    job_reference=request.job_reference,
                    deletion_fenced=post_del_fenced,
                    allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                    workspace_membership_active=self._is_membership_active(request),
                    now=self._clock(),
                )
            )
            if not post_decision.allowed:
                return StageRunEnvelope(
                    stage_code=request.stage_code,
                    schema_file=schema_file,
                    status=StageRunStatus.CANCELED_PRE_COMMIT_FENCE,
                    reason_code=LlmGateReasonCode.PRE_COMMIT_FENCE_REJECTED,
                    execution_origin=request.adapter.execution_origin,
                    live_pilot_gate_eligible=False,
                    committed_allowed=False,
                    can_promote_to_supported=False,
                    retryable=False,
                    auto_retry_performed=auto_retry_performed,
                    external_call_count=external_calls_made,
                    repair_attempts_used=repair_attempts_used,
                    checked_at=self._clock(),
                    decision=post_decision,
                    validated_payload=None,
                    free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                    job_reference=request.job_reference,
                )

            # 9. Synthetic vs Live provenance separation.
            live_pilot_eligible = (
                request.adapter.execution_origin == "APPROVED_SUBSCRIPTION"
                and self._policy_gate.approval_config.runtime_mode
                is LlmRuntimeMode.APPROVED_SUBSCRIPTION
                and validated_payload is not None
            )

            return StageRunEnvelope(
                stage_code=request.stage_code,
                schema_file=schema_file,
                status=StageRunStatus.SUCCEEDED,
                reason_code=LlmGateReasonCode.ALLOWED,
                execution_origin=request.adapter.execution_origin,
                live_pilot_gate_eligible=live_pilot_eligible,
                committed_allowed=True,
                can_promote_to_supported=True,
                retryable=False,
                auto_retry_performed=auto_retry_performed,
                external_call_count=external_calls_made,
                repair_attempts_used=repair_attempts_used,
                checked_at=self._clock(),
                decision=post_decision,
                validated_payload=validated_payload,
                free_alternatives=DEFAULT_FREE_ALTERNATIVES,
                job_reference=request.job_reference,
            )
        finally:
            # Release ephemeral in-memory raw text references immediately on exit.
            ephemeral_text = None
            adapter_input = None
            if request.close_resolution_on_exit:
                request.resolution.close()
