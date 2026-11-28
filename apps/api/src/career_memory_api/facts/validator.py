"""Stage 2B Independent FactSupportValidator (T-403 + T-403A).

Strictly separated from Stage 2A FactCandidateExtractor:
- Enforces ``validator_run_id != extractor_run_id`` (rejects run-ID reuse).
- Executes deterministic prechecks BEFORE invoking semantic validation:
  1. Re-verified Plan C ``OperationMemoryResolution`` (``MATCHED``, exact hashes).
  2. Exact ``cm-locator-v1`` sub-span bounds & ``span_sha256`` verification.
  3. Identity attribution gate (``ID-04`` / ``SCN-11``).
  4. 6-element ``MetricDetails`` completeness gate (``CLAIM-06`` / ``SCN-09`` / ``SCN-16``).
- Executes semantic validation through ``BoundedStageRunner``
  (``LlmStageCode.STAGE_2B_FACT_SUPPORT_VALIDATION``) against
  ``fact_support_validation.v1.schema.json``.
- Applies deterministic post-validator adversarial guards (``FACT-04..08`` / ``T-403A``):
  - Plan/target vs Completed (``SCN-06``)
  - Negation/rollback/unmet target (``SCN-07``)
  - Team vs Individual attribution & passive-anonymous audit (``SCN-08``, ``SCN-05``)
  - Context-only / ``ISSUE_SECTION`` background (``SCN-04``)
  - Staging/Sandbox vs Production (``SCN-09``)
  - ``CONTRADICTS`` never promotes to ``SUPPORTED`` (``SCN-05``)
- Emits strictly non-raw ``support_metadata`` (``extracted_span`` is ALWAYS ``NULL``).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import ContentUnitType, SpanLocator
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_BASE_URI,
    SCHEMA_PACKAGE_VERSION,
    FactPromotionDecision,
    validate_stage_payload,
)
from career_memory_api.facts.models import (
    DEFAULT_VALIDATOR_MODEL,
    DEFAULT_VALIDATOR_PROMPT_VERSION,
    DEFAULT_VALIDATOR_VERSION,
    FactAttributionScope,
    FactCompletionState,
    FactEnvironmentScope,
    FactError,
    FactSemanticAttributes,
    FactValidationOutcome,
    MetricDetailsModel,
    utc_now,
    verify_locator_in_unit_text,
)
from career_memory_api.identities.models import (
    IdentityAttributionBlockReason,
    IdentityAttributionEligibility,
)
from career_memory_api.llm_gate import (
    BoundedStageRunner,
    ConnectionJobReference,
    LlmGateOutcome,
    LlmProviderMode,
    LlmStageCode,
    StageBudgetPolicy,
    StageExecutionAdapterPort,
    StageExecutionRequest,
    StagePreCommitContext,
    StageRunStatus,
    assert_no_raw_text_or_secret_keys,
)
from career_memory_api.resolver.models import (
    OperationMemoryResolution,
    ResolutionStatus,
    format_utc_timestamp,
    map_resolution_status_to_error,
    normalize_sha256_hash,
)
from career_memory_api.schema_contracts.enums import (
    FactType,
    GenerationRunStatus,
    ResourceVisibility,
    SupportType,
    SupportValidator,
)

FACT_SUPPORT_VALIDATION_SCHEMA_ID = f"{SCHEMA_BASE_URI}/fact_support_validation.v1.schema.json"

# =============================================================================
# Deterministic Adversarial Phrase Detectors (T-403A / FACT-04..08)
# =============================================================================
_PLAN_TARGET_RE = re.compile(
    r"(?i)("
    r"할\s*예정|도입\s*예정|진행\s*예정|예정이다|계획하고\s*있다|계획\s*중|계획이다|목표로\s*한다"
    r"|\btodo\b|\bplanned\s+for\b|\bplan\s+to\b|\bplanning\s+to\b|\broadmap\b"
    r"|\bstated\s+goal\b|\btarget\s+for\b|\bscheduled\s+for\b|\bwill\s+implement\b"
    r"|\bupcoming\b|\bnext\s+quarter\b"
    r")"
)

_NEGATION_ROLLBACK_RE = re.compile(
    r"(?i)("
    r"개선되지\s*않았다|미배포|미달성|시작하지\s*않았다|적용하지\s*않았다|달성하지\s*못|실패했다"
    r"|롤백|철회했다"
    r"|\bnot\s+deployed\b|\bdid\s+not\s+deploy\b|\bwas\s+not\s+deployed\b"
    r"|\bdid\s+not\s+improve\b|\bnot\s+improved\b"
    r"|\bdid\s+not\s+achieve\b|\bnot\s+achieved\b|\btarget\s+unmet\b"
    r"|\brolled\s+back\b|\breverted\b|\bfailed\s+to\b"
    r")"
)

_EXPLICIT_REVERT_CANDIDATE_RE = re.compile(
    r"(?i)(\brevert(?:ed)?\b|\broll(?:ed)?\s*back\b|롤백|철회)"
)

_POSITIVE_DEPLOYMENT_CLAIM_RE = re.compile(
    r"(?i)(\bdeployed\b|\bachieved\b|\bsavings\b|\bimproved\b|\breduced\b|배포했다|달성했다|개선했다)"
)

_TEAM_WIDE_OR_PASSIVE_RE = re.compile(
    r"(?i)("
    r"\b\d+\s*[- ]?engineer(?:s)?\s+(?:\w+\s+)?team\b"
    r"|\bteam\s*\(\s*\d+\s+engineer"
    r"|\b(?:platform|reliability|security|infrastructure|engineering)\s+team\b"
    r"|\bthe\s+team\s+(?:rebuilt|built|migrated|shipped|delivered|redesigned)\b"
    r"|팀\s*전체|팀에서\s|팀이\s|전사\s*팀"
    r"|\blog\s+audit\s+confirmed\b|\bwas\s+audited\b|\bwere\s+audited\b"
    r")"
)

_STAGING_SANDBOX_RE = re.compile(
    r"(?i)("
    r"\bstaging\b|\bsandbox\b|\blocal\s+test\b|\btest\s+environment\b|\bsynthetic\s+data\b"
    r"|스테이징|샌드박스|테스트\s*환경|합성\s*데이터|로컬\s*환경"
    r")"
)

_ISSUE_BACKGROUND_RE = re.compile(
    r"(?i)(\bbackground\b|\bgoal\b|\bproblem\s+statement\b|\binitiative\b|배경|목표)"
)


def compute_validation_input_hash(
    *,
    event_id: str,
    fact_candidate_id: str,
    extractor_run_id: str,
    content_unit_id: str,
    expected_revision_hash: str,
    expected_unit_hash: str,
    locator_span_sha256: str,
    validator_version: str,
    prompt_version: str,
) -> str:
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
            "extractor_run_id": extractor_run_id,
            "fact_candidate_id": fact_candidate_id,
            "locator_span_sha256": normalize_sha256_hash(
                locator_span_sha256, field_name="locator_span_sha256"
            ),
            "prompt_version": prompt_version,
            "validator_version": validator_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class FactSupportValidationRequest:
    """Input for Stage 2B FactSupportValidator for one candidate against one ContentUnit."""

    access: WorkspaceAccessContext
    event_id: str
    source_resource_id: str
    visibility: ResourceVisibility | str
    fact_candidate_id: str
    extractor_run_id: str
    fact_type: FactType
    predicate: str
    object_text: str
    candidate_locator: Mapping[str, Any]
    semantic: FactSemanticAttributes
    resolution: OperationMemoryResolution
    expected_revision_hash: str
    expected_unit_hash: str
    unit_type: ContentUnitType
    budget: StageBudgetPolicy
    adapter: StageExecutionAdapterPort
    provider_mode: LlmProviderMode
    allow_synthetic_fixture_mode: bool = False
    validator_run_id: str | None = None
    validator_version: str = DEFAULT_VALIDATOR_VERSION
    prompt_version: str = DEFAULT_VALIDATOR_PROMPT_VERSION
    model: str = DEFAULT_VALIDATOR_MODEL
    subject_identity_id: str | None = None
    identity_eligibility: IdentityAttributionEligibility | None = None
    metric_details: MetricDetailsModel | None = None
    job_reference: ConnectionJobReference | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    before_commit_hook: Callable[[StagePreCommitContext], bool | None] | None = None
    verify_workspace_membership_active: Callable[[WorkspaceAccessContext], bool] | None = None
    close_resolution_on_exit: bool = True


class FactSupportValidator:
    """Stage 2B Independent FactSupportValidator (T-403 + T-403A)."""

    def __init__(
        self,
        *,
        stage_runner: BoundedStageRunner,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._stage_runner = stage_runner
        self._clock = clock

    def validate_candidate(
        self, request: FactSupportValidationRequest
    ) -> FactValidationOutcome:
        """Validate one candidate fact strictly in order: prechecks -> Stage 2B model -> guards."""
        val_run_id = (request.validator_run_id or str(uuid.uuid4())).strip()
        ext_run_id = request.extractor_run_id.strip()

        # Hard Stage 2A / 2B Separation Guard: NEVER reuse Stage 2A run_id!
        if not val_run_id or not ext_run_id or val_run_id == ext_run_id:
            if request.close_resolution_on_exit:
                request.resolution.close()
            raise FactError(
                status_code=422,
                code="STAGE_RUN_ID_COLLISION",
                message=(
                    "Stage 2B validator_run_id must be non-empty and strictly distinct from "
                    "Stage 2A extractor_run_id."
                ),
            )

        meta = request.resolution.metadata
        now = self._clock()
        norm_rev_hash = normalize_sha256_hash(
            request.expected_revision_hash, field_name="expected_revision_hash"
        )
        norm_unit_hash = normalize_sha256_hash(
            request.expected_unit_hash, field_name="expected_unit_hash"
        )
        cand_locator_dict = dict(request.candidate_locator)

        def _build_support_meta(
            *,
            support_type: SupportType,
            decision: FactPromotionDecision,
            rev_loc_verified: bool,
            guard_override: bool,
            guard_reason: str | None,
            rejection_reason: str | None,
        ) -> dict[str, Any]:
            sm: dict[str, Any] = {
                "locator": dict(cand_locator_dict),
                "span_sha256": cand_locator_dict.get("span_sha256"),
                "char_start": cand_locator_dict.get("char_start"),
                "char_end": cand_locator_dict.get("char_end"),
                "line_start": cand_locator_dict.get("line_start"),
                "line_end": cand_locator_dict.get("line_end"),
                "unit_content_hash": norm_unit_hash,
                "revision_content_hash": norm_rev_hash,
                "validator_run_id": val_run_id,
                "extractor_run_id": ext_run_id,
                "checked_at": format_utc_timestamp(now),
                "resolution_status": meta.status.value,
                "revision_and_locator_verified": rev_loc_verified,
                "support_type": support_type.value,
                "promotion_decision": decision.value,
                "guard_override_applied": guard_override,
            }
            if guard_reason is not None:
                sm["guard_reason_code"] = guard_reason
            if rejection_reason is not None:
                sm["rejection_reason"] = rejection_reason
            assert_no_raw_text_or_secret_keys(sm, context="FactSupportValidator.support_metadata")
            return sm

        def _precheck_outcome(
            *,
            support_type: SupportType,
            decision: FactPromotionDecision,
            rejection_reason: str,
            rev_loc_verified: bool,
            run_status: GenerationRunStatus = GenerationRunStatus.SUCCEEDED,
            run_error_code: str | None = None,
        ) -> FactValidationOutcome:
            return FactValidationOutcome(
                validator_run_id=val_run_id,
                extractor_run_id=ext_run_id,
                fact_candidate_id=request.fact_candidate_id,
                source_object_revision_id=meta.source_object_revision_id,
                content_unit_id=meta.content_unit_id,
                verified_locator=cand_locator_dict,
                support_type=support_type,
                validator=SupportValidator.RULE,
                validator_version=request.validator_version,
                promotion_decision=decision,
                revision_and_locator_verified=rev_loc_verified,
                rejection_reason=rejection_reason,
                checked_at=now,
                resolution_status=meta.status,
                guard_override_applied=True,
                guard_reason_code=rejection_reason,
                support_metadata=_build_support_meta(
                    support_type=support_type,
                    decision=decision,
                    rev_loc_verified=rev_loc_verified,
                    guard_override=True,
                    guard_reason=rejection_reason,
                    rejection_reason=rejection_reason,
                ),
                run_status=run_status,
                run_error_code=run_error_code,
            )

        unit_text: str | None = None
        span_text: str | None = None
        unit_locator: SpanLocator | None = None
        try:
            # =================================================================
            # Step 1: Deterministic Prechecks (BEFORE semantic validation!)
            # =================================================================
            # 1a. Re-verify OperationMemoryResolution & revision/unit hashes
            if request.resolution.closed or meta.status is not ResolutionStatus.MATCHED:
                err_code = (
                    map_resolution_status_to_error(meta).code
                    if meta.status is not ResolutionStatus.MATCHED
                    else "SOURCE_LOCATOR_INVALID"
                )
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason=err_code,
                    rev_loc_verified=False,
                )

            if (
                meta.expected_revision_hash != norm_rev_hash
                or meta.observed_revision_hash != norm_rev_hash
            ):
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason="SOURCE_REVISION_MISMATCH",
                    rev_loc_verified=False,
                )

            if (
                meta.expected_unit_hash != norm_unit_hash
                or meta.observed_unit_hash != norm_unit_hash
            ):
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason="SOURCE_LOCATOR_INVALID",
                    rev_loc_verified=False,
                )

            if request.resolution.ephemeral_unit is not None:
                unit_text = request.resolution.ephemeral_unit.text
                unit_locator = request.resolution.ephemeral_unit.metadata.locator
            elif request.resolution.ephemeral_span is not None:
                unit_text = request.resolution.ephemeral_span.text
                unit_locator = request.resolution.ephemeral_span.locator
            else:
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason="SOURCE_LOCATOR_INVALID",
                    rev_loc_verified=False,
                )

            # 1b. Exact CmLocatorV1 bounds & span_sha256 verification inside unit
            loc_ok, span_text, _loc_errs = verify_locator_in_unit_text(
                unit_text,
                cand_locator_dict,
                unit_locator=unit_locator,
            )
            if not loc_ok or span_text is None:
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason="SOURCE_LOCATOR_INVALID",
                    rev_loc_verified=False,
                )

            # 1c. Identity Attribution Gate (ID-04, SCN-11)
            needs_identity_gate = (
                request.subject_identity_id is not None
                or request.fact_type is FactType.ROLE
                or request.semantic.scope is FactAttributionScope.INDIVIDUAL
            )
            if needs_identity_gate:
                elig = request.identity_eligibility
                if elig is None or not elig.can_support_source_verified:
                    if (
                        elig is not None
                        and elig.block_reason_code
                        is IdentityAttributionBlockReason.IDENTITY_IS_BOT
                    ):
                        return _precheck_outcome(
                            support_type=SupportType.CONTEXT,
                            decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                            rejection_reason=IdentityAttributionBlockReason.IDENTITY_IS_BOT.value,
                            rev_loc_verified=True,
                        )
                    return _precheck_outcome(
                        support_type=SupportType.CONTEXT,
                        decision=FactPromotionDecision.DEFER_REVIEW,
                        rejection_reason="IDENTITY_UNRESOLVED_REVIEW_REQUIRED",
                        rev_loc_verified=True,
                    )

            # 1d. 6-Element MetricDetails Completeness Gate (CLAIM-06, SCN-09, SCN-16)
            if request.fact_type is FactType.METRIC:
                if request.metric_details is None:
                    return _precheck_outcome(
                        support_type=SupportType.CONTEXT,
                        decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                        rejection_reason="METRIC_DETAILS_INCOMPLETE",
                        rev_loc_verified=True,
                    )
                try:
                    MetricDetailsModel.model_validate(request.metric_details.model_dump())
                except Exception:  # noqa: BLE001
                    return _precheck_outcome(
                        support_type=SupportType.CONTEXT,
                        decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                        rejection_reason="METRIC_DETAILS_INCOMPLETE",
                        rev_loc_verified=True,
                    )

            # =================================================================
            # Step 2: Stage 2B Model Validation via BoundedStageRunner
            # =================================================================
            context_meta: dict[str, Any] = {
                "validator_run_id": val_run_id,
                "extractor_run_id": ext_run_id,
                "fact_candidate_id": request.fact_candidate_id,
                "event_id": request.event_id,
                "fact_type": request.fact_type.value,
                "validator_version": request.validator_version,
                "unit_type": request.unit_type.value,
                "completion_state": request.semantic.completion_state.value,
                "scope": request.semantic.scope.value,
                "environment_scope": request.semantic.environment_scope.value,
            }
            assert_no_raw_text_or_secret_keys(
                context_meta, context="FactSupportValidator.context_metadata"
            )

            envelope = self._stage_runner.run_stage(
                StageExecutionRequest(
                    access=request.access,
                    stage_code=LlmStageCode.STAGE_2B_FACT_SUPPORT_VALIDATION,
                    provider_mode=request.provider_mode,
                    source_resource_id=request.source_resource_id,
                    visibility=request.visibility,
                    resolution=request.resolution,
                    expected_revision_hash=norm_rev_hash,
                    expected_span_sha256=norm_unit_hash,
                    budget=request.budget,
                    adapter=request.adapter,
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
                err_code = (
                    LlmGateOutcome.MANUAL_ONLY_BLOCKED.value
                    if envelope.status is StageRunStatus.BLOCKED
                    else envelope.reason_code.value
                )
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason=err_code,
                    rev_loc_verified=True,
                    run_status=(
                        GenerationRunStatus.CANCELED
                        if envelope.status is StageRunStatus.CANCELED_PRE_COMMIT_FENCE
                        else GenerationRunStatus.FAILED
                    ),
                    run_error_code=err_code,
                )

            vp = dict(envelope.validated_payload)
            if validate_stage_payload("fact_support_validation", vp, raise_on_error=False):
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason="AI_SCHEMA_INVALID",
                    rev_loc_verified=True,
                    run_status=GenerationRunStatus.FAILED,
                    run_error_code="AI_SCHEMA_INVALID",
                )

            # Enforce exact producer->consumer handoff bindings
            if (
                vp.get("validator_run_id") != val_run_id
                or vp.get("extractor_run_id") != ext_run_id
                or vp.get("fact_candidate_id") != request.fact_candidate_id
                or vp.get("content_unit_id") != meta.content_unit_id
                or vp.get("source_object_revision_id") != meta.source_object_revision_id
                or vp.get("verified_locator") != cand_locator_dict
            ):
                return _precheck_outcome(
                    support_type=SupportType.CONTEXT,
                    decision=FactPromotionDecision.REJECT_UNSUPPORTED,
                    rejection_reason="STAGE_2B_HANDOFF_MISMATCH",
                    rev_loc_verified=True,
                    run_status=GenerationRunStatus.FAILED,
                    run_error_code="STAGE_2B_HANDOFF_MISMATCH",
                )

            support_type = SupportType(str(vp["support_type"]))
            decision = FactPromotionDecision(str(vp["promotion_decision"]))
            validator_kind = SupportValidator(str(vp["validator"]))
            rejection_reason = (
                str(vp["rejection_reason"]) if vp.get("rejection_reason") is not None else None
            )

            # =================================================================
            # Step 3: Deterministic Post-Validator Adversarial Guards (T-403A)
            # Validator NEVER rubber-stamps extractor mistakes!
            # =================================================================
            guard_override = False
            guard_reason: str | None = None

            (
                support_type,
                decision,
                guard_override,
                guard_reason,
            ) = self._apply_adversarial_guards(
                span_text=span_text,
                request=request,
                support_type=support_type,
                decision=decision,
            )
            if guard_override and guard_reason is not None:
                rejection_reason = guard_reason

            support_meta = _build_support_meta(
                support_type=support_type,
                decision=decision,
                rev_loc_verified=True,
                guard_override=guard_override,
                guard_reason=guard_reason,
                rejection_reason=rejection_reason,
            )
            return FactValidationOutcome(
                validator_run_id=val_run_id,
                extractor_run_id=ext_run_id,
                fact_candidate_id=request.fact_candidate_id,
                source_object_revision_id=meta.source_object_revision_id,
                content_unit_id=meta.content_unit_id,
                verified_locator=cand_locator_dict,
                support_type=support_type,
                validator=validator_kind,
                validator_version=request.validator_version,
                promotion_decision=decision,
                revision_and_locator_verified=True,
                rejection_reason=rejection_reason,
                checked_at=now,
                resolution_status=meta.status,
                guard_override_applied=guard_override,
                guard_reason_code=guard_reason,
                support_metadata=support_meta,
                run_status=GenerationRunStatus.SUCCEEDED,
                run_error_code=None,
            )
        finally:
            unit_text = None
            span_text = None
            unit_locator = None
            if request.close_resolution_on_exit:
                request.resolution.close()

    @staticmethod
    def _apply_adversarial_guards(
        *,
        span_text: str,
        request: FactSupportValidationRequest,
        support_type: SupportType,
        decision: FactPromotionDecision,
    ) -> tuple[SupportType, FactPromotionDecision, bool, str | None]:
        """Deterministic semantic & scope invariant overrides (FACT-04..08 / T-403A)."""
        # 1. Context-only support NEVER promotes to SUPPORTED (SCN-04 / FACT-08)
        if support_type is SupportType.CONTEXT:
            if decision is FactPromotionDecision.PROMOTE_SUPPORTED:
                return (
                    SupportType.CONTEXT,
                    FactPromotionDecision.REJECT_UNSUPPORTED,
                    True,
                    "CONTEXT_ONLY_CANNOT_PROMOTE",
                )
            return support_type, decision, False, None

        # 2. ISSUE_SECTION background/problem unit cannot directly ground a completed achievement
        if (
            request.unit_type is ContentUnitType.ISSUE_SECTION
            and request.fact_type in (FactType.ACTION, FactType.RESULT, FactType.METRIC, FactType.ROLE)
            and request.semantic.completion_state is FactCompletionState.COMPLETED
            and _ISSUE_BACKGROUND_RE.search(span_text)
        ):
            return (
                SupportType.CONTEXT,
                FactPromotionDecision.REJECT_UNSUPPORTED,
                True,
                "ISSUE_SECTION_CONTEXT_ONLY",
            )

        # 3. Plan / Target vs Completed (SCN-06 / FACT-08)
        if (
            request.semantic.completion_state is FactCompletionState.COMPLETED
            and _PLAN_TARGET_RE.search(span_text)
        ):
            return (
                SupportType.CONTRADICTS,
                FactPromotionDecision.REJECT_UNSUPPORTED,
                True,
                "PLAN_CANNOT_PROMOTE_COMPLETED",
            )

        # 4. Negation / Rollback / Unmet Target vs Positive Achievement (SCN-07 / FACT-08)
        if (
            request.semantic.completion_state is FactCompletionState.COMPLETED
            and _NEGATION_ROLLBACK_RE.search(span_text)
        ):
            candidate_text = f"{request.predicate} {request.object_text}"
            is_truthful_revert = (
                bool(_EXPLICIT_REVERT_CANDIDATE_RE.search(candidate_text))
                and not bool(_POSITIVE_DEPLOYMENT_CLAIM_RE.search(candidate_text))
            )
            if not is_truthful_revert:
                return (
                    SupportType.CONTRADICTS,
                    FactPromotionDecision.REJECT_UNSUPPORTED,
                    True,
                    "NEGATION_CANNOT_PROMOTE_POSITIVE",
                )

        # 5. Team-wide / Passive-Anonymous Attribution vs INDIVIDUAL (SCN-08 / SCN-05 / FACT-08)
        if (
            request.semantic.scope is FactAttributionScope.INDIVIDUAL
            and _TEAM_WIDE_OR_PASSIVE_RE.search(span_text)
        ):
            return (
                SupportType.CONTRADICTS,
                FactPromotionDecision.REJECT_UNSUPPORTED,
                True,
                "TEAM_SCOPE_CANNOT_PROMOTE_INDIVIDUAL",
            )

        # 6. Staging / Sandbox Evidence vs PRODUCTION Metric/Result (SCN-09 / FACT-08)
        effective_env = (
            request.metric_details.environment_scope
            if request.metric_details is not None
            else request.semantic.environment_scope
        )
        if (
            effective_env is FactEnvironmentScope.PRODUCTION
            and _STAGING_SANDBOX_RE.search(span_text)
        ):
            return (
                SupportType.CONTRADICTS,
                FactPromotionDecision.REJECT_UNSUPPORTED,
                True,
                "STAGING_CANNOT_PROMOTE_PRODUCTION",
            )

        # 7. CONTRADICTS support NEVER promotes to SUPPORTED (SCN-05)
        if (
            support_type is SupportType.CONTRADICTS
            and decision is FactPromotionDecision.PROMOTE_SUPPORTED
        ):
            return (
                SupportType.CONTRADICTS,
                FactPromotionDecision.MARK_CONFLICTED,
                True,
                "CONTRADICTS_CANNOT_PROMOTE",
            )

        return support_type, decision, False, None


__all__ = [
    "FACT_SUPPORT_VALIDATION_SCHEMA_ID",
    "FactSupportValidationRequest",
    "FactSupportValidator",
    "compute_validation_input_hash",
]
