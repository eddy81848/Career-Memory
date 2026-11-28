"""Stage 2A FactCandidateExtractor (T-402) — strictly separated from Stage 2B validation.

Enforces:
- Consumes ephemeral text ONLY from a verified ``OperationMemoryResolution``
  (``status == ResolutionStatus.MATCHED``); never reads persisted DB text.
- Gates every execution through ``career_memory_api.llm_gate.BoundedStageRunner``
  (``LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION``), preserving hard-OFF live LLM
  governance (``MANUAL_ONLY_BLOCKED`` when unapproved).
- Supports ``0..N`` candidates per ContentUnit: ``0`` candidates is a valid ``SUCCEEDED`` run;
  any schema or security violation in a batch fails the ENTIRE batch closed
  (``status='FAILED'``, ``error_code='AI_SCHEMA_INVALID'``, zero partial candidates).
- Hostile source text defense (SEC-02): enforces ``status='CANDIDATE'``, exact
  ``extractor_run_id`` / ``content_unit_id`` / ``source_object_revision_id`` bindings,
  verified ``cm-locator-v1`` bounds/hashes inside the unit, binary/oversized fail-closed,
  and Plan C zero-raw-key/zero-canary persistence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import CANONICALIZATION_VERSION, SpanLocator
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_BASE_URI,
    SCHEMA_PACKAGE_VERSION,
    validate_stage_payload,
)
from career_memory_api.facts.models import (
    DEFAULT_EXTRACTOR_MODEL,
    DEFAULT_EXTRACTOR_PROMPT_VERSION,
    DEFAULT_EXTRACTOR_VERSION,
    FactCandidateDraft,
    FactError,
    FactExtractionUnitResult,
    MetricDetailsModel,
    extract_semantic_attributes,
    verify_locator_in_unit_text,
)
from career_memory_api.llm_gate import (
    BoundedStageRunner,
    ConnectionJobReference,
    LlmGateOutcome,
    LlmProviderMode,
    LlmStageCode,
    PolicyGateRequest,
    RawProviderStageResponse,
    StageAdapterInput,
    StageBudgetPolicy,
    StageExecutionAdapterPort,
    StageExecutionRequest,
    StagePreCommitContext,
    StageRunStatus,
    assert_no_raw_text_or_secret_keys,
)
from career_memory_api.llm_gate.models import (
    ExecutionOrigin,
    STAGE_CODE_TO_SCHEMA_FILE,
)
from career_memory_api.llm_gate.stage_runner import ProviderTransportErrorKind
from career_memory_api.resolver.models import (
    OperationMemoryResolution,
    ResolutionStatus,
    map_resolution_status_to_error,
    normalize_sha256_hash,
)
from career_memory_api.schema_contracts.enums import (
    FactStatus,
    FactType,
    GenerationRunStatus,
    ResourceVisibility,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys

FACT_CANDIDATE_SCHEMA_ID = f"{SCHEMA_BASE_URI}/fact_candidate.v1.schema.json"


def compute_extraction_input_hash(
    *,
    event_id: str,
    content_unit_id: str,
    expected_revision_hash: str,
    expected_unit_hash: str,
    extractor_version: str,
    prompt_version: str,
) -> str:
    """Compute deterministic non-raw SHA-256 input_hash for Stage 2A idempotency."""
    canonical = json.dumps(
        {
            "content_unit_id": content_unit_id,
            "event_id": event_id,
            "expected_revision_hash": normalize_sha256_hash(
                expected_revision_hash, field_name="expected_revision_hash"
            ),
            "expected_unit_hash": normalize_sha256_hash(
                expected_unit_hash, field_name="expected_unit_hash"
            ),
            "extractor_version": extractor_version,
            "prompt_version": prompt_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _is_binary_or_unextractable_text(text: str) -> bool:
    """Detect NUL bytes or binary control-character payloads that must fail closed (SEC-02)."""
    if "\x00" in text:
        return True
    control_count = sum(
        1 for ch in text if ord(ch) < 32 and ch not in ("\n", "\r", "\t")
    )
    return control_count > 0 and (control_count / max(1, len(text))) > 0.05


class _SinglePayloadReplayAdapter:
    """Internal replay adapter that feeds one pre-fetched Stage 2A item into BoundedStageRunner."""

    def __init__(self, *, payload: Any, origin: ExecutionOrigin) -> None:
        self._payload = payload
        self._origin = origin

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return self._origin

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        del adapter_input
        return RawProviderStageResponse(payload=self._payload, stream_completed=True)


class _ZeroCallBlockedAdapter:
    """Guard adapter used when policy gate is expected to block before execution."""

    def __init__(self, *, origin: ExecutionOrigin) -> None:
        self._origin = origin

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return self._origin

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        del adapter_input
        raise RuntimeError("Blocked Stage 2A request unexpectedly attempted to execute adapter.")


@dataclass(frozen=True, slots=True)
class FactExtractionRequest:
    """Input for Stage 2A FactCandidateExtractor on a single verified ContentUnit."""

    access: WorkspaceAccessContext
    event_id: str
    source_resource_id: str
    visibility: ResourceVisibility | str
    resolution: OperationMemoryResolution
    expected_revision_hash: str
    expected_unit_hash: str
    budget: StageBudgetPolicy
    adapter: StageExecutionAdapterPort
    provider_mode: LlmProviderMode
    allow_synthetic_fixture_mode: bool = False
    extractor_run_id: str | None = None
    extractor_version: str = DEFAULT_EXTRACTOR_VERSION
    prompt_version: str = DEFAULT_EXTRACTOR_PROMPT_VERSION
    model: str = DEFAULT_EXTRACTOR_MODEL
    job_reference: ConnectionJobReference | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    before_commit_hook: Callable[[StagePreCommitContext], bool | None] | None = None
    verify_workspace_membership_active: Callable[[WorkspaceAccessContext], bool] | None = None
    close_resolution_on_exit: bool = True


class FactCandidateExtractor:
    """Stage 2A FactCandidateExtractor executing strictly through BoundedStageRunner."""

    def __init__(self, *, stage_runner: BoundedStageRunner) -> None:
        self._stage_runner = stage_runner

    def extract_candidates(self, request: FactExtractionRequest) -> FactExtractionUnitResult:
        """Extract 0..N CANDIDATE facts from a single verified OperationMemoryResolution."""
        run_id = (request.extractor_run_id or str(uuid.uuid4())).strip()
        meta = request.resolution.metadata
        norm_rev_hash = normalize_sha256_hash(
            request.expected_revision_hash, field_name="expected_revision_hash"
        )
        norm_unit_hash = normalize_sha256_hash(
            request.expected_unit_hash, field_name="expected_unit_hash"
        )
        input_hash = compute_extraction_input_hash(
            event_id=request.event_id,
            content_unit_id=meta.content_unit_id,
            expected_revision_hash=norm_rev_hash,
            expected_unit_hash=norm_unit_hash,
            extractor_version=request.extractor_version,
            prompt_version=request.prompt_version,
        )
        input_artifact_versions: dict[str, Any] = {
            "source_revision_ids": [meta.source_object_revision_id],
            "source_revision_hashes": [norm_rev_hash],
            "content_unit_ids": [meta.content_unit_id],
            "content_unit_hashes": [norm_unit_hash],
            "canonicalization_version": CANONICALIZATION_VERSION,
            "schema_version": SCHEMA_PACKAGE_VERSION,
        }

        def _failed(
            error_code: str,
            *,
            status: GenerationRunStatus = GenerationRunStatus.FAILED,
            extra_meta: dict[str, Any] | None = None,
        ) -> FactExtractionUnitResult:
            run_meta: dict[str, Any] = {
                "stage_code": LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION.value,
                "output_schema_id": FACT_CANDIDATE_SCHEMA_ID,
                "output_schema_version": SCHEMA_PACKAGE_VERSION,
                "resolution_status": meta.status.value,
                "candidate_count": 0,
                "error_code": error_code,
            }
            if extra_meta:
                run_meta.update(extra_meta)
            assert_no_raw_text_or_secret_keys(run_meta, context="FactCandidateExtractor.run_metadata")
            return FactExtractionUnitResult(
                extractor_run_id=run_id,
                event_id=request.event_id,
                content_unit_id=meta.content_unit_id,
                source_object_revision_id=meta.source_object_revision_id,
                status=status,
                error_code=error_code,
                candidates=(),
                input_hash=input_hash,
                extractor_version=request.extractor_version,
                prompt_version=request.prompt_version,
                model=request.model,
                input_artifact_versions=input_artifact_versions,
                run_metadata=run_meta,
            )

        unit_text: str | None = None
        unit_locator: SpanLocator | None = None
        try:
            # 1. Verify OperationMemoryResolution BEFORE touching LLM gate or adapter
            if request.resolution.closed or meta.status is not ResolutionStatus.MATCHED:
                err_code = (
                    map_resolution_status_to_error(meta).code
                    if meta.status is not ResolutionStatus.MATCHED
                    else "RESOLUTION_CLOSED"
                )
                return _failed(err_code)

            if (
                meta.expected_revision_hash != norm_rev_hash
                or meta.observed_revision_hash != norm_rev_hash
            ):
                return _failed("SOURCE_REVISION_MISMATCH")

            if (
                meta.expected_unit_hash != norm_unit_hash
                or meta.observed_unit_hash != norm_unit_hash
            ):
                return _failed("SOURCE_LOCATOR_INVALID")

            if request.resolution.ephemeral_unit is not None:
                unit_text = request.resolution.ephemeral_unit.text
                unit_locator = request.resolution.ephemeral_unit.metadata.locator
                unit_type_val = request.resolution.ephemeral_unit.metadata.unit_type.value
            elif request.resolution.ephemeral_span is not None:
                unit_text = request.resolution.ephemeral_span.text
                unit_locator = request.resolution.ephemeral_span.locator
                unit_type_val = "UNKNOWN"
            else:
                return _failed("RESOLUTION_NOT_MATCHED")

            if not unit_text:
                return _failed("EMPTY_CONTENT_UNIT")

            if _is_binary_or_unextractable_text(unit_text):
                return _failed("UNEXTRACTABLE_BINARY_UNIT")

            input_bytes = len(unit_text.encode("utf-8"))
            if input_bytes > request.budget.max_input_bytes:
                return _failed("BUDGET_MAX_INPUT_BYTES_EXCEEDED")

            # 2. Enforce central LlmPolicyGate (hard-OFF live LLM gate, consent, deletion fence, budget)
            context_meta: dict[str, Any] = {
                "extractor_run_id": run_id,
                "event_id": request.event_id,
                "extractor_version": request.extractor_version,
                "prompt_version": request.prompt_version,
                "unit_type": unit_type_val,
            }
            assert_no_raw_text_or_secret_keys(
                context_meta, context="FactCandidateExtractor.context_metadata"
            )

            # Pre-check policy gate using BoundedStageRunner's policy gate directly so 0 adapter
            # calls occur when blocked (hard-OFF live mode, BYOK, private repo, deletion fence, etc.)
            policy_gate = self._stage_runner._policy_gate  # noqa: SLF001
            conn, consent, del_fenced, _ = self._stage_runner._lookup_live_policy_state(  # noqa: SLF001
                StageExecutionRequest(
                    access=request.access,
                    stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
                    provider_mode=request.provider_mode,
                    source_resource_id=request.source_resource_id,
                    visibility=request.visibility,
                    resolution=request.resolution,
                    expected_revision_hash=norm_rev_hash,
                    expected_span_sha256=norm_unit_hash,
                    budget=request.budget,
                    adapter=_ZeroCallBlockedAdapter(origin=request.adapter.execution_origin),
                    job_reference=request.job_reference,
                    allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                    verify_workspace_membership_active=request.verify_workspace_membership_active,
                    close_resolution_on_exit=False,
                )
            )
            membership_ok = (
                True
                if request.verify_workspace_membership_active is None
                else bool(request.verify_workspace_membership_active(request.access))
            )
            decision = policy_gate.evaluate(
                PolicyGateRequest(
                    access=request.access,
                    provider_mode=request.provider_mode,
                    stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
                    source_resource_id=request.source_resource_id,
                    visibility=request.visibility,
                    input_byte_length=input_bytes,
                    budget=request.budget,
                    connection=conn,
                    consent=consent,
                    job_reference=request.job_reference,
                    deletion_fenced=del_fenced,
                    allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                    workspace_membership_active=membership_ok,
                )
            )
            if not decision.allowed:
                return _failed(
                    LlmGateOutcome.MANUAL_ONLY_BLOCKED.value,
                    extra_meta={
                        "gate_reason_code": decision.reason_code.value,
                        "external_call_count": 0,
                    },
                )

            # Enforce provider/adapter origin alignment (same as BoundedStageRunner step 2b)
            if (
                request.provider_mode is LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE
                and request.adapter.execution_origin != "SYNTHETIC_FIXTURE"
            ):
                return _failed(LlmGateOutcome.MANUAL_ONLY_BLOCKED.value)
            if (
                request.provider_mode is LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH
                and request.adapter.execution_origin != "APPROVED_SUBSCRIPTION"
            ):
                return _failed(LlmGateOutcome.MANUAL_ONLY_BLOCKED.value)

            # 3. Invoke Stage 2A adapter ONCE with bounded non-tool StageAdapterInput
            schema_file = STAGE_CODE_TO_SCHEMA_FILE[
                LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION
            ]
            adapter_input = StageAdapterInput(
                stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
                schema_file=schema_file,
                operation_id=meta.operation_id,
                workspace_id=request.access.workspace_id,
                source_object_revision_id=meta.source_object_revision_id,
                content_unit_id=meta.content_unit_id,
                expected_revision_hash=norm_rev_hash,
                span_sha256=norm_unit_hash,
                ephemeral_source_text=unit_text,
                context_metadata=context_meta,
                sanitized_headers={},
                repair_attempt=0,
                previous_validation_errors=(),
                tools_enabled=False,
            )
            raw_response = request.adapter.execute_stage(adapter_input)

            # 4. Fail closed on transport, incomplete stream, tool calls, or unsupported outputs
            if raw_response.transport_error is not ProviderTransportErrorKind.NONE:
                return _failed(raw_response.transport_error.value)
            if not raw_response.stream_completed:
                return _failed("STREAM_INCOMPLETE")
            if (
                raw_response.contains_tool_calls
                or raw_response.unsupported_output_kind is not None
            ):
                return _failed("UNSUPPORTED_OUTPUT_OR_TOOL_SIDE_EFFECT")

            raw_payload = raw_response.payload
            if isinstance(raw_payload, list):
                raw_items: list[Any] = list(raw_payload)
            elif isinstance(raw_payload, Mapping):
                raw_items = [dict(raw_payload)]
            else:
                return _failed("AI_SCHEMA_INVALID")

            # 5. Case 0 candidates (SCN-01 boilerplate/chatter): valid SUCCEEDED after pre-commit fence
            if len(raw_items) == 0:
                if request.before_commit_hook is not None:
                    hook_ok = request.before_commit_hook(
                        StagePreCommitContext(
                            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
                            operation_id=meta.operation_id,
                            workspace_id=request.access.workspace_id,
                            source_resource_id=request.source_resource_id,
                            content_unit_id=meta.content_unit_id,
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
                    )
                    if hook_ok is False:
                        return _failed(
                            "PRE_COMMIT_FENCE_REJECTED",
                            status=GenerationRunStatus.CANCELED,
                        )
                run_meta_zero = {
                    "stage_code": LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION.value,
                    "output_schema_id": FACT_CANDIDATE_SCHEMA_ID,
                    "output_schema_version": SCHEMA_PACKAGE_VERSION,
                    "execution_origin": request.adapter.execution_origin,
                    "resolution_status": meta.status.value,
                    "candidate_count": 0,
                }
                assert_no_raw_text_or_secret_keys(
                    run_meta_zero, context="FactCandidateExtractor.run_metadata"
                )
                return FactExtractionUnitResult(
                    extractor_run_id=run_id,
                    event_id=request.event_id,
                    content_unit_id=meta.content_unit_id,
                    source_object_revision_id=meta.source_object_revision_id,
                    status=GenerationRunStatus.SUCCEEDED,
                    error_code=None,
                    candidates=(),
                    input_hash=input_hash,
                    extractor_version=request.extractor_version,
                    prompt_version=request.prompt_version,
                    model=request.model,
                    input_artifact_versions=input_artifact_versions,
                    run_metadata=run_meta_zero,
                )

            # 6. Validate EVERY item through BoundedStageRunner + deterministic SEC-02 guards.
            # Any single invalid item fails the ENTIRE batch closed (zero partial candidates!).
            validated_drafts: list[FactCandidateDraft] = []
            for idx, item in enumerate(raw_items):
                if not isinstance(item, Mapping):
                    return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})

                item_budget = StageBudgetPolicy(
                    max_input_bytes=request.budget.max_input_bytes,
                    max_calls=max(1, request.budget.max_calls),
                    calls_used=0,
                    max_repair_attempts=0,
                    max_pre_spend_retries=0,
                    overage_credit_disabled=request.budget.overage_credit_disabled,
                    quota_remaining_known_and_sufficient=request.budget.quota_remaining_known_and_sufficient,
                    quota_remaining_units=request.budget.quota_remaining_units,
                    estimated_required_units=request.budget.estimated_required_units,
                    max_output_tokens_hint=request.budget.max_output_tokens_hint,
                    paid_fallback_requested=request.budget.paid_fallback_requested,
                )
                envelope = self._stage_runner.run_stage(
                    StageExecutionRequest(
                        access=request.access,
                        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
                        provider_mode=request.provider_mode,
                        source_resource_id=request.source_resource_id,
                        visibility=request.visibility,
                        resolution=request.resolution,
                        expected_revision_hash=norm_rev_hash,
                        expected_span_sha256=norm_unit_hash,
                        budget=item_budget,
                        adapter=_SinglePayloadReplayAdapter(
                            payload=dict(item),
                            origin=request.adapter.execution_origin,
                        ),
                        job_reference=request.job_reference,
                        extra_headers=request.extra_headers,
                        context_metadata=context_meta,
                        allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                        before_commit_hook=request.before_commit_hook,
                        verify_workspace_membership_active=request.verify_workspace_membership_active,
                        close_resolution_on_exit=False,
                    )
                )
                if (
                    envelope.status is not StageRunStatus.SUCCEEDED
                    or not envelope.committed_allowed
                    or envelope.validated_payload is None
                ):
                    err = (
                        "PRE_COMMIT_FENCE_REJECTED"
                        if envelope.status is StageRunStatus.CANCELED_PRE_COMMIT_FENCE
                        else (
                            LlmGateOutcome.MANUAL_ONLY_BLOCKED.value
                            if envelope.status is StageRunStatus.BLOCKED
                            else "AI_SCHEMA_INVALID"
                        )
                    )
                    run_stat = (
                        GenerationRunStatus.CANCELED
                        if envelope.status is StageRunStatus.CANCELED_PRE_COMMIT_FENCE
                        else GenerationRunStatus.FAILED
                    )
                    return _failed(
                        err,
                        status=run_stat,
                        extra_meta={
                            "failed_item_index": idx,
                            "validation_error_codes": list(envelope.validation_error_codes),
                        },
                    )

                vp = dict(envelope.validated_payload)
                # Double-check T-106 schema directly and enforce SEC-02 deterministic bindings
                if validate_stage_payload("fact_candidate", vp, raise_on_error=False):
                    return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})

                # Hostile source text defense (SEC-02): model cannot forge status/run/unit/revision!
                if vp.get("status") != FactStatus.CANDIDATE.value:
                    return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})
                if vp.get("extractor_run_id") != run_id:
                    return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})
                if vp.get("content_unit_id") != meta.content_unit_id:
                    return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})
                if vp.get("source_object_revision_id") != meta.source_object_revision_id:
                    return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})

                # Verify cm-locator-v1 bounds & span_sha256 strictly inside ephemeral unit text
                loc_ok, _span_text, loc_errs = verify_locator_in_unit_text(
                    unit_text,
                    vp["locator"],
                    unit_locator=unit_locator,
                )
                if not loc_ok:
                    return _failed(
                        "AI_SCHEMA_INVALID",
                        extra_meta={
                            "failed_item_index": idx,
                            "locator_error_codes": list(loc_errs),
                        },
                    )

                nv_raw = vp.get("normalized_value")
                if nv_raw is not None:
                    try:
                        assert_no_forbidden_raw_keys(nv_raw, field_name="normalized_value")
                        assert_no_raw_text_or_secret_keys(
                            nv_raw, context="FactCandidate.normalized_value"
                        )
                    except ValueError:
                        return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})

                metric_model: MetricDetailsModel | None = None
                if vp.get("metric_details") is not None:
                    try:
                        metric_model = MetricDetailsModel.model_validate(vp["metric_details"])
                    except Exception:  # noqa: BLE001
                        return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})

                try:
                    semantic = extract_semantic_attributes(
                        nv_raw,
                        metric_details=metric_model,
                        uncertainty_notes=vp.get("uncertainty_notes"),
                    )
                except FactError:
                    return _failed("AI_SCHEMA_INVALID", extra_meta={"failed_item_index": idx})

                validated_drafts.append(
                    FactCandidateDraft(
                        schema_version=str(vp["schema_version"]),
                        extractor_run_id=run_id,
                        extractor_version=str(vp["extractor_version"]),
                        source_object_revision_id=str(vp["source_object_revision_id"]),
                        content_unit_id=str(vp["content_unit_id"]),
                        locator=dict(vp["locator"]),
                        fact_type=FactType(str(vp["fact_type"])),
                        predicate=str(vp["predicate"]).strip(),
                        object_text=str(vp["object_text"]).strip(),
                        normalized_value=dict(nv_raw) if isinstance(nv_raw, Mapping) else None,
                        status=FactStatus.CANDIDATE,
                        subject_identity_id=(
                            str(vp["subject_identity_id"])
                            if vp.get("subject_identity_id") is not None
                            else None
                        ),
                        metric_details=metric_model,
                        uncertainty_notes=(
                            str(vp["uncertainty_notes"])
                            if vp.get("uncertainty_notes") is not None
                            else None
                        ),
                        semantic=semantic,
                    )
                )

            run_meta_ok = {
                "stage_code": LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION.value,
                "output_schema_id": FACT_CANDIDATE_SCHEMA_ID,
                "output_schema_version": SCHEMA_PACKAGE_VERSION,
                "execution_origin": request.adapter.execution_origin,
                "resolution_status": meta.status.value,
                "candidate_count": len(validated_drafts),
            }
            assert_no_raw_text_or_secret_keys(
                run_meta_ok, context="FactCandidateExtractor.run_metadata"
            )
            return FactExtractionUnitResult(
                extractor_run_id=run_id,
                event_id=request.event_id,
                content_unit_id=meta.content_unit_id,
                source_object_revision_id=meta.source_object_revision_id,
                status=GenerationRunStatus.SUCCEEDED,
                error_code=None,
                candidates=tuple(validated_drafts),
                input_hash=input_hash,
                extractor_version=request.extractor_version,
                prompt_version=request.prompt_version,
                model=request.model,
                input_artifact_versions=input_artifact_versions,
                run_metadata=run_meta_ok,
            )
        finally:
            unit_text = None
            unit_locator = None
            if request.close_resolution_on_exit:
                request.resolution.close()


__all__ = [
    "FACT_CANDIDATE_SCHEMA_ID",
    "FactCandidateExtractor",
    "FactExtractionRequest",
    "compute_extraction_input_hash",
]
