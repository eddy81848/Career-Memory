"""Stage 3A ClaimGenerator (T-404) — strictly separated from Stage 3B Atomizer & Stage 3C Validator.

Enforces:
- Executes exclusively through ``career_memory_api.llm_gate.BoundedStageRunner``
  (``LlmStageCode.STAGE_3A_CLAIM_GENERATION``), preserving default hard-OFF live LLM gate
  (``MANUAL_ONLY_BLOCKED`` with 0 external calls & actionable ``free_alternatives``).
- Validates against ``claim_draft.v1.schema.json`` (T-106).
- Enforces non-empty same-event ``SUPPORTED`` ``input_fact_ids`` subset from ``ClaimContextBundle``.
- Guards against invented numeric metrics, invented production environment scopes, and
  invented personal role titles outside the ContextBuilder manifest.
- Always outputs initial ``status = ClaimStatus.DRAFT`` and ``claim_version = 1``;
  NEVER sets ``verification`` or ``SOURCE_VERIFIED``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import CANONICALIZATION_VERSION
from career_memory_api.claims.context_builder import ClaimContextBundle, EligibleFactContextItem
from career_memory_api.claims.models import (
    DEFAULT_CLAIM_GENERATOR_MODEL,
    DEFAULT_CLAIM_GENERATOR_PROMPT_VERSION,
    DEFAULT_CLAIM_GENERATOR_VERSION,
    ContextBuildStatus,
    build_ephemeral_manifest_resolution,
)
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_BASE_URI,
    SCHEMA_PACKAGE_VERSION,
    validate_stage_payload,
)
from career_memory_api.facts.models import (
    FactAttributionScope,
    FactEnvironmentScope,
)
from career_memory_api.llm_gate import (
    BoundedStageRunner,
    ConnectionJobReference,
    LlmProviderMode,
    LlmStageCode,
    StageBudgetPolicy,
    StageExecutionAdapterPort,
    StageExecutionRequest,
    StagePreCommitContext,
    StageRunStatus,
    assert_no_raw_text_or_secret_keys,
)
from career_memory_api.llm_gate.models import DEFAULT_FREE_ALTERNATIVES
from career_memory_api.schema_contracts.enums import (
    ClaimStatus,
    FactType,
    GenerationRunStatus,
)

CLAIM_DRAFT_SCHEMA_ID = f"{SCHEMA_BASE_URI}/claim_draft.v1.schema.json"

_PRODUCTION_TERM_RE = re.compile(r"\b(production|prod|live traffic|company-wide)\b", re.IGNORECASE)
_METRIC_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?(?:\s*%|\s*ms|\s*s|\s*x|\s*rps|\s*qps)\b", re.IGNORECASE)
_PERSONAL_LEAD_RE = re.compile(
    r"\b(tech lead|solely led|single-handedly|personally led|architected as lead|owned end-to-end)\b",
    re.IGNORECASE,
)


def compute_claim_generation_input_hash(
    *,
    event_id: str,
    bundle_input_hash: str,
    generator_version: str,
    prompt_version: str,
) -> str:
    canonical = json.dumps(
        {
            "bundle_input_hash": bundle_input_hash,
            "event_id": event_id,
            "generator_version": generator_version,
            "prompt_version": prompt_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ClaimDraftOutput:
    """Validated Stage 3A ClaimDraft payload (always DRAFT, v1, zero verification)."""

    schema_version: str
    generator_run_id: str
    confirmed_event_id: str
    input_fact_ids: tuple[str, ...]
    claim_version: int
    claim_text: str
    status: ClaimStatus
    previous_claim_version: int | None = None
    supersedes_claim_id: str | None = None

    def to_stage3a_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "generator_run_id": self.generator_run_id,
            "confirmed_event_id": self.confirmed_event_id,
            "input_fact_ids": list(self.input_fact_ids),
            "claim_version": self.claim_version,
            "claim_text": self.claim_text,
            "status": ClaimStatus.DRAFT.value,
        }
        if self.previous_claim_version is not None:
            payload["previous_claim_version"] = self.previous_claim_version
        if self.supersedes_claim_id is not None:
            payload["supersedes_claim_id"] = self.supersedes_claim_id
        return payload


@dataclass(frozen=True, slots=True)
class ClaimDraftGenerationRequest:
    """Input to Stage 3A ClaimGenerator."""

    access: WorkspaceAccessContext
    context_bundle: ClaimContextBundle
    budget: StageBudgetPolicy
    adapter: StageExecutionAdapterPort
    provider_mode: LlmProviderMode
    allow_synthetic_fixture_mode: bool = False
    generator_run_id: str | None = None
    generator_version: str = DEFAULT_CLAIM_GENERATOR_VERSION
    prompt_version: str = DEFAULT_CLAIM_GENERATOR_PROMPT_VERSION
    model: str = DEFAULT_CLAIM_GENERATOR_MODEL
    job_reference: ConnectionJobReference | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    before_commit_hook: Callable[[StagePreCommitContext], bool | None] | None = None
    verify_workspace_membership_active: Callable[[WorkspaceAccessContext], bool] | None = None


@dataclass(frozen=True, slots=True)
class ClaimDraftGenerationResult:
    """Outcome of Stage 3A ClaimGenerator."""

    generator_run_id: str
    event_id: str
    generation_status: str  # "SUCCEEDED" | "MANUAL_ONLY_BLOCKED" | "DRAFT_INVALID"
    run_status: GenerationRunStatus
    error_code: str | None
    draft: ClaimDraftOutput | None
    input_hash: str
    generator_version: str
    prompt_version: str
    model: str
    input_artifact_versions: dict[str, Any]
    run_metadata: dict[str, Any]
    free_alternatives: tuple[str, ...] = ()


_NON_PRODUCTION_QUALIFIER_RE = re.compile(
    r"\b(?:before|prior to|not deployed to|never deployed to|without|preventing|blocked before)\s+(?:production|prod|live traffic)\b",
    re.IGNORECASE,
)


def _asserts_production_scope(text: str) -> bool:
    stripped = _NON_PRODUCTION_QUALIFIER_RE.sub("", text)
    return bool(_PRODUCTION_TERM_RE.search(stripped))


def _check_manifest_hallucination_guards(
    *,
    claim_text: str,
    selected_facts: tuple[EligibleFactContextItem, ...],
) -> str | None:
    """Fail-closed guard preventing Stage 3A from inventing metrics, environments, or roles."""
    # 1. Environment escalation guard: staging-only facts cannot produce production claims
    has_prod_fact = any(
        f.semantic.environment_scope is FactEnvironmentScope.PRODUCTION
        or (
            f.metric_details is not None
            and f.metric_details.environment_scope is FactEnvironmentScope.PRODUCTION
        )
        for f in selected_facts
    )
    has_staging_fact = any(
        f.semantic.environment_scope is FactEnvironmentScope.STAGING_TEST
        or (
            f.metric_details is not None
            and f.metric_details.environment_scope is FactEnvironmentScope.STAGING_TEST
        )
        for f in selected_facts
    )
    if has_staging_fact and not has_prod_fact and _asserts_production_scope(claim_text):
        return "INVENTED_ENVIRONMENT_SCOPE"

    # 2. Invented numeric metric guard: numeric metrics in claim_text must appear in selected facts
    claim_numbers = {
        re.sub(r"\s+", "", m.group(0).lower()) for m in _METRIC_NUMBER_RE.finditer(claim_text)
    }
    if claim_numbers:
        fact_corpus = " ".join(
            [
                f"{f.object_text} {f.metric_details.value if f.metric_details else ''} "
                f"{f.metric_details.unit if f.metric_details else ''}"
                for f in selected_facts
            ]
        ).lower()
        fact_numbers = {
            re.sub(r"\s+", "", m.group(0).lower()) for m in _METRIC_NUMBER_RE.finditer(fact_corpus)
        }
        fact_metric_vals = {
            re.sub(r"\s+", "", f.metric_details.value.lower())
            for f in selected_facts
            if f.metric_details is not None
        }
        allowed_nums = fact_numbers | fact_metric_vals
        ungrounded_nums = {
            num for num in claim_numbers if num not in allowed_nums and num not in re.sub(r"\s+", "", fact_corpus)
        }
        if ungrounded_nums:
            return "INVENTED_METRIC_OUTSIDE_MANIFEST"

    # 3. Invented personal leadership role guard when no individual ROLE fact exists
    if _PERSONAL_LEAD_RE.search(claim_text):
        has_individual_role = any(
            (f.fact_type is FactType.ROLE or f.semantic.scope is FactAttributionScope.INDIVIDUAL)
            and f.subject_identity_id is not None
            for f in selected_facts
        )
        if not has_individual_role:
            return "INVENTED_ROLE_OUTSIDE_MANIFEST"

    return None


class ClaimGenerator:
    """Stage 3A ClaimGenerator executing strictly through BoundedStageRunner."""

    def __init__(self, *, stage_runner: BoundedStageRunner) -> None:
        self._stage_runner = stage_runner

    def generate_draft(self, request: ClaimDraftGenerationRequest) -> ClaimDraftGenerationResult:
        run_id = (request.generator_run_id or str(uuid.uuid4())).strip()
        bundle = request.context_bundle
        input_hash = compute_claim_generation_input_hash(
            event_id=bundle.event_id,
            bundle_input_hash=bundle.input_hash,
            generator_version=request.generator_version,
            prompt_version=request.prompt_version,
        )
        all_rev_ids = sorted({rid for f in bundle.facts for rid in f.active_revision_ids})
        all_cu_ids = sorted({cid for f in bundle.facts for cid in f.active_content_unit_ids})
        input_artifact_versions: dict[str, Any] = {
            "fact_ids": [f.fact_id for f in bundle.facts],
            "content_unit_ids": all_cu_ids,
            "source_revision_ids": all_rev_ids,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "schema_version": SCHEMA_PACKAGE_VERSION,
        }

        def _make_result(
            *,
            gen_status: str,
            run_status: GenerationRunStatus,
            error_code: str | None,
            draft: ClaimDraftOutput | None = None,
            free_alts: tuple[str, ...] = (),
            extra_meta: dict[str, Any] | None = None,
        ) -> ClaimDraftGenerationResult:
            run_meta: dict[str, Any] = {
                "stage_code": LlmStageCode.STAGE_3A_CLAIM_GENERATION.value,
                "output_schema_id": CLAIM_DRAFT_SCHEMA_ID,
                "output_schema_version": SCHEMA_PACKAGE_VERSION,
                "generation_status": gen_status,
                "eligible_fact_count": len(bundle.facts),
                "context_status": bundle.status.value,
            }
            if error_code is not None:
                run_meta["error_code"] = error_code
            if extra_meta:
                run_meta.update(extra_meta)
            assert_no_raw_text_or_secret_keys(run_meta, context="ClaimGenerator.run_metadata")
            return ClaimDraftGenerationResult(
                generator_run_id=run_id,
                event_id=bundle.event_id,
                generation_status=gen_status,
                run_status=run_status,
                error_code=error_code,
                draft=draft,
                input_hash=input_hash,
                generator_version=request.generator_version,
                prompt_version=request.prompt_version,
                model=request.model,
                input_artifact_versions=input_artifact_versions,
                run_metadata=run_meta,
                free_alternatives=free_alts,
            )

        # 1. ContextBuilder preconditions (0 eligible facts or budget exceeded -> fail closed)
        if bundle.status is ContextBuildStatus.EMPTY:
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code="NO_ELIGIBLE_SUPPORTED_FACTS",
            )
        if bundle.status in (ContextBuildStatus.PARTIAL_WITH_LIMIT, ContextBuildStatus.MANUAL_ONLY):
            return _make_result(
                gen_status="MANUAL_ONLY_BLOCKED",
                run_status=GenerationRunStatus.FAILED,
                error_code=bundle.reason_code or "CONTEXT_BUDGET_EXCEEDED",
                free_alts=tuple(alt.value for alt in DEFAULT_FREE_ALTERNATIVES),
            )

        # 2. Run strictly through BoundedStageRunner (hard-OFF live LLM gate enforced here!)
        manifest_text = bundle.to_manifest_json()
        resolution, expected_rev_hash, expected_span_hash = build_ephemeral_manifest_resolution(
            operation_id=f"stage3a-{run_id}",
            manifest_text=manifest_text,
            revision_id=all_rev_ids[0] if all_rev_ids else f"bundle-rev-{bundle.event_id}",
            content_unit_id=all_cu_ids[0] if all_cu_ids else f"bundle-cu-{bundle.event_id}",
        )
        context_meta = {
            "generator_run_id": run_id,
            "confirmed_event_id": bundle.event_id,
            "eligible_fact_ids": [f.fact_id for f in bundle.facts],
            "generator_version": request.generator_version,
            "prompt_version": request.prompt_version,
        }
        envelope = self._stage_runner.run_stage(
            StageExecutionRequest(
                access=request.access,
                stage_code=LlmStageCode.STAGE_3A_CLAIM_GENERATION,
                provider_mode=request.provider_mode,
                source_resource_id=bundle.source_resource_id,
                visibility=bundle.visibility,
                resolution=resolution,
                expected_revision_hash=expected_rev_hash,
                expected_span_sha256=expected_span_hash,
                budget=request.budget,
                adapter=request.adapter,
                job_reference=request.job_reference,
                extra_headers=request.extra_headers,
                context_metadata=context_meta,
                allow_synthetic_fixture_mode=request.allow_synthetic_fixture_mode,
                before_commit_hook=request.before_commit_hook,
                verify_workspace_membership_active=request.verify_workspace_membership_active,
                close_resolution_on_exit=True,
            )
        )

        if envelope.status is StageRunStatus.BLOCKED:
            return _make_result(
                gen_status="MANUAL_ONLY_BLOCKED",
                run_status=GenerationRunStatus.CANCELED,
                error_code=envelope.reason_code.value,
                free_alts=tuple(alt.value for alt in envelope.free_alternatives),
                extra_meta={"stage_envelope": envelope.to_audit_dict()},
            )

        if (
            envelope.status is not StageRunStatus.SUCCEEDED
            or not envelope.committed_allowed
            or not isinstance(envelope.validated_payload, Mapping)
        ):
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code=envelope.reason_code.value,
                free_alts=tuple(alt.value for alt in envelope.free_alternatives),
                extra_meta={"stage_envelope": envelope.to_audit_dict()},
            )

        payload = dict(envelope.validated_payload)
        # Re-verify T-106 claim_draft.v1.schema.json + strict domain bindings
        violations = validate_stage_payload("claim_draft", payload, raise_on_error=False)
        if violations:
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code="AI_SCHEMA_INVALID",
                extra_meta={"validation_codes": sorted({v.code for v in violations})},
            )

        if str(payload["confirmed_event_id"]) != bundle.event_id:
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code="CROSS_EVENT_CLAIM_GENERATION_FORBIDDEN",
            )
        if str(payload["generator_run_id"]) != run_id:
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code="GENERATOR_RUN_ID_MISMATCH",
            )
        if int(payload["claim_version"]) != 1:
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code="INITIAL_CLAIM_VERSION_MUST_BE_1",
            )

        eligible_map = {f.fact_id: f for f in bundle.facts}
        raw_input_fact_ids = [str(fid) for fid in payload["input_fact_ids"]]
        if not raw_input_fact_ids or any(fid not in eligible_map for fid in raw_input_fact_ids):
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code="INPUT_FACT_IDS_OUTSIDE_CONTEXT_MANIFEST",
            )

        selected_facts = tuple(eligible_map[fid] for fid in raw_input_fact_ids)
        claim_text = str(payload["claim_text"]).strip()
        if not claim_text:
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code="EMPTY_CLAIM_TEXT",
            )

        hallucination_code = _check_manifest_hallucination_guards(
            claim_text=claim_text,
            selected_facts=selected_facts,
        )
        if hallucination_code is not None:
            return _make_result(
                gen_status="DRAFT_INVALID",
                run_status=GenerationRunStatus.FAILED,
                error_code=hallucination_code,
            )

        draft = ClaimDraftOutput(
            schema_version=str(payload["schema_version"]),
            generator_run_id=run_id,
            confirmed_event_id=bundle.event_id,
            input_fact_ids=tuple(raw_input_fact_ids),
            claim_version=1,
            claim_text=claim_text,
            status=ClaimStatus.DRAFT,
            previous_claim_version=payload.get("previous_claim_version"),
            supersedes_claim_id=payload.get("supersedes_claim_id"),
        )
        return _make_result(
            gen_status="SUCCEEDED",
            run_status=GenerationRunStatus.SUCCEEDED,
            error_code=None,
            draft=draft,
            extra_meta={"stage_envelope": envelope.to_audit_dict()},
        )


__all__ = [
    "CLAIM_DRAFT_SCHEMA_ID",
    "ClaimDraftGenerationRequest",
    "ClaimDraftGenerationResult",
    "ClaimDraftOutput",
    "ClaimGenerator",
    "compute_claim_generation_input_hash",
]
