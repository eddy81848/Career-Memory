"""Stage 3C independent ClaimSupportValidator (T-405 + T-405A).

Enforces:
- Hard guard: ``validation_run_id`` MUST be pairwise distinct from BOTH ``generator_run_id``
  and ``atomizer_run_id`` (``422 STAGE_RUN_ID_COLLISION``).
- Executes through ``BoundedStageRunner`` (``LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION``)
  against ``claim_support_validation.v1.schema.json``.
- Re-evaluates EVERY atom deterministically against same-Event Canonical Facts, active
  provenance chains, current-operation Plan C Refetch match set, ``CLAIM-05`` Role/Contribution
  grounding, ``CLAIM-06`` 6-element Metric & causality gate, and ``USER_CONFIRMED`` rules.
- Never uses Claim drafts or AI rationale as evidence, and never trusts LLM-supplied
  ``computed_verification`` without independent deterministic recomputation.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import CANONICALIZATION_VERSION
from career_memory_api.claims.atomizer import ClaimAtomDraftItem
from career_memory_api.claims.models import (
    DEFAULT_CLAIM_VALIDATOR_MODEL,
    DEFAULT_CLAIM_VALIDATOR_PROMPT_VERSION,
    DEFAULT_CLAIM_VALIDATOR_VERSION,
    ClaimError,
    build_ephemeral_manifest_resolution,
)
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_BASE_URI,
    SCHEMA_PACKAGE_VERSION,
    compute_expected_claim_verification,
    validate_stage_payload,
)
from career_memory_api.facts.models import (
    FactAttributionScope,
    FactCausalBasis,
    FactCompletionState,
    FactEnvironmentScope,
    FactSemanticAttributes,
    MetricDetailsModel,
)
from career_memory_api.identities.models import IdentityAttributionEligibility
from career_memory_api.llm_gate import (
    BoundedStageRunner,
    ConnectionJobReference,
    LlmProviderMode,
    LlmStageCode,
    RawProviderStageResponse,
    StageAdapterInput,
    StageBudgetPolicy,
    StageExecutionAdapterPort,
    StageExecutionRequest,
    StagePreCommitContext,
    StageRunStatus,
    assert_no_raw_text_or_secret_keys,
)
from career_memory_api.llm_gate.models import ExecutionOrigin
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    ContributionRole,
    ContributionStatus,
    FactStatus,
    FactType,
    GenerationRunStatus,
    ResourceVisibility,
    SupportType,
)

CLAIM_SUPPORT_VALIDATION_SCHEMA_ID = f"{SCHEMA_BASE_URI}/claim_support_validation.v1.schema.json"

_PRODUCTION_ASSERTION_RE = re.compile(r"\b(production|prod|live traffic)\b", re.IGNORECASE)
_NON_PRODUCTION_QUALIFIER_RE = re.compile(
    r"\b(?:before|prior to|not deployed to|never deployed to|without|preventing|blocked before)\s+(?:production|prod|live traffic)\b",
    re.IGNORECASE,
)
_CAUSAL_ASSERTION_RE = re.compile(
    r"\b(caused|drove|directly resulted in|directly increased|directly reduced|solely caused)\b",
    re.IGNORECASE,
)
_INDIVIDUAL_ASSERTION_RE = re.compile(
    r"\b(i |my |personally|solely|individually|as tech lead|led the|owned the)\b",
    re.IGNORECASE,
)
_OVERGENERAL_SCOPE_RE = re.compile(
    r"\b(company-wide|all services|across the entire company|annual|yearly|all teams|org-wide)\b",
    re.IGNORECASE,
)
_PLANNED_WORDING_RE = re.compile(
    r"\b(planned|plan to|roadmap|todo|scheduled|upcoming|intend to|proposed)\b",
    re.IGNORECASE,
)
_NEGATED_WORDING_RE = re.compile(
    r"\b(not deployed|rolled back|did not|unmet|failed to|not achieved|reverted|never shipped)\b",
    re.IGNORECASE,
)
_NUMBER_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?(?:\s*%|\s*ms|\s*s|\s*x)?\b", re.IGNORECASE)


def _norm_str(val: str | None) -> str:
    return " ".join((val or "").strip().lower().split())


@dataclass(frozen=True, slots=True)
class FactEvidenceSnapshot:
    """Canonical DB Fact snapshot used by Stage 3C deterministic verification."""

    fact_id: str
    event_id: str
    version: int
    fact_type: FactType
    status: FactStatus
    predicate: str
    object_text: str
    subject_identity_id: str | None
    normalized_value: dict[str, Any] | None
    metric_details: MetricDetailsModel | None
    semantic: FactSemanticAttributes
    active_direct_support_ids: tuple[str, ...]
    active_contradicts_support_ids: tuple[str, ...]
    # fact_support_id -> (content_unit_id, revision_id, unit_policy_state, revision_is_tombstone)
    support_units: dict[str, tuple[str, str, str, bool]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContributionEvidenceSnapshot:
    """Canonical DB Contribution + active contribution_support facts (CLAIM-05)."""

    contribution_id: str
    event_id: str
    identity_id: str
    role: ContributionRole
    status: ContributionStatus
    active_supporting_fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AtomFactBindingDraft:
    """Validated atom -> fact binding persisted to public.claim_atom_support."""

    fact_id: str
    support_type: SupportType
    fact_support_ids: tuple[str, ...]
    source_object_revision_id: str
    content_unit_id: str


@dataclass(frozen=True, slots=True)
class AtomValidationOutcome:
    """Authoritative Stage 3C per-atom outcome after all deterministic adversarial guards."""

    ordinal: int
    atom_type: ClaimAtomType
    atom_text: str
    status: ClaimAtomStatus
    fact_bindings: tuple[AtomFactBindingDraft, ...]
    unsupported_reason: str | None = None
    role_contribution_grounding: dict[str, Any] | None = None
    metric_grounding: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ClaimSupportValidationRequest:
    """Input to Stage 3C ClaimSupportValidator."""

    access: WorkspaceAccessContext
    event_id: str
    claim_id: str
    claim_version: int
    generator_run_id: str
    atomizer_run_id: str
    atoms: Sequence[ClaimAtomDraftItem]
    fact_snapshots: Mapping[str, FactEvidenceSnapshot]
    contributions: Sequence[ContributionEvidenceSnapshot]
    identity_eligibilities: Mapping[str, IdentityAttributionEligibility]
    matched_content_unit_ids: frozenset[str]
    source_resource_id: str
    visibility: ResourceVisibility | str
    budget: StageBudgetPolicy
    adapter: StageExecutionAdapterPort
    provider_mode: LlmProviderMode
    allow_synthetic_fixture_mode: bool = False
    validation_run_id: str | None = None
    validator_version: str = DEFAULT_CLAIM_VALIDATOR_VERSION
    prompt_version: str = DEFAULT_CLAIM_VALIDATOR_PROMPT_VERSION
    model: str = DEFAULT_CLAIM_VALIDATOR_MODEL
    job_reference: ConnectionJobReference | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    before_commit_hook: Callable[[StagePreCommitContext], bool | None] | None = None
    verify_workspace_membership_active: Callable[[WorkspaceAccessContext], bool] | None = None


@dataclass(frozen=True, slots=True)
class ClaimSupportValidationResult:
    """Outcome of Stage 3C ClaimSupportValidator."""

    validation_run_id: str
    generator_run_id: str
    atomizer_run_id: str
    claim_id: str
    claim_version: int
    run_status: GenerationRunStatus
    error_code: str | None
    atom_results: tuple[AtomValidationOutcome, ...]
    computed_verification: ClaimVerification
    next_claim_status: ClaimStatus
    input_hash: str
    validator_version: str
    prompt_version: str
    model: str
    input_artifact_versions: dict[str, Any]
    run_metadata: dict[str, Any]


def compute_claim_validation_input_hash(
    *,
    claim_id: str,
    claim_version: int,
    atomizer_run_id: str,
    fact_ids: Sequence[str],
    validator_version: str,
    prompt_version: str,
) -> str:
    canonical = json.dumps(
        {
            "atomizer_run_id": atomizer_run_id,
            "claim_id": claim_id,
            "claim_version": claim_version,
            "fact_ids": sorted(set(fact_ids)),
            "prompt_version": prompt_version,
            "validator_version": validator_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _extract_tokens(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9가-힣%._-]+", text.lower())
        if len(tok) >= 2 and tok not in {"the", "and", "for", "with", "that", "from", "this", "into"}
    }


class DeterministicClaimValidatorAdapter:
    """Offline deterministic Stage 3C validator adapter for non-LLM revalidation of edited claims."""

    def __init__(
        self,
        *,
        fact_snapshots: Mapping[str, FactEvidenceSnapshot],
        contributions: Sequence[ContributionEvidenceSnapshot],
    ) -> None:
        self._facts = fact_snapshots
        self._contributions = contributions

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return "SYNTHETIC_FIXTURE"

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        meta = adapter_input.context_metadata
        manifest = json.loads(adapter_input.ephemeral_source_text)
        atoms = manifest.get("atoms", [])
        event_id = str(meta["event_id"])

        atom_validations: list[dict[str, Any]] = []
        statuses: list[str] = []

        for atom in atoms:
            ordinal = int(atom["ordinal"])
            atom_type = ClaimAtomType(str(atom["atom_type"]))
            atom_text = str(atom["atom_text"])
            atom_tokens = _extract_tokens(atom_text)

            # Find best matching fact(s) on this event
            best_fact: FactEvidenceSnapshot | None = None
            best_score = 0.0
            for fact in self._facts.values():
                if fact.event_id != event_id:
                    continue
                if fact.status not in (FactStatus.SUPPORTED, FactStatus.USER_CONFIRMED):
                    continue
                fact_tokens = _extract_tokens(f"{fact.predicate} {fact.object_text}")
                if not fact_tokens or not atom_tokens:
                    continue
                overlap = len(atom_tokens & fact_tokens) / max(1, min(len(atom_tokens), len(fact_tokens)))
                if fact.fact_type.value == atom_type.value:
                    overlap += 0.15
                if overlap > best_score:
                    best_score = overlap
                    best_fact = fact

            if best_fact is None or best_score < 0.35:
                atom_validations.append(
                    {
                        "ordinal": ordinal,
                        "atom_type": atom_type.value,
                        "status": ClaimAtomStatus.UNSUPPORTED.value,
                        "fact_bindings": [],
                        "unsupported_reason": "NO_MATCHING_CANONICAL_FACT",
                    }
                )
                statuses.append(ClaimAtomStatus.UNSUPPORTED.value)
                continue

            # Build binding
            bindings: list[dict[str, Any]] = []
            if best_fact.active_direct_support_ids:
                fs_id = best_fact.active_direct_support_ids[0]
                cu_id, rev_id, _, _ = best_fact.support_units[fs_id]
                bindings.append(
                    {
                        "fact_id": best_fact.fact_id,
                        "support_type": SupportType.DIRECT.value,
                        "fact_support_ids": list(best_fact.active_direct_support_ids),
                        "source_object_revision_id": rev_id,
                        "content_unit_id": cu_id,
                    }
                )

            if best_fact.status is FactStatus.USER_CONFIRMED:
                atom_validations.append(
                    {
                        "ordinal": ordinal,
                        "atom_type": atom_type.value,
                        "status": ClaimAtomStatus.USER_CONFIRMED.value,
                        "fact_bindings": bindings,
                    }
                )
                statuses.append(ClaimAtomStatus.USER_CONFIRMED.value)
                continue

            entry: dict[str, Any] = {
                "ordinal": ordinal,
                "atom_type": atom_type.value,
                "status": ClaimAtomStatus.SUPPORTED.value if bindings else ClaimAtomStatus.UNSUPPORTED.value,
                "fact_bindings": bindings,
            }
            if atom_type is ClaimAtomType.ROLE and best_fact.subject_identity_id is not None:
                matching_contrib = next(
                    (
                        c
                        for c in self._contributions
                        if c.event_id == event_id
                        and c.identity_id == best_fact.subject_identity_id
                        and c.status is ContributionStatus.SOURCE_SUPPORTED
                        and c.active_supporting_fact_ids
                    ),
                    None,
                )
                if matching_contrib is not None:
                    entry["role_contribution_grounding"] = {
                        "subject_identity_id": matching_contrib.identity_id,
                        "confirmed_event_id": event_id,
                        "role": matching_contrib.role.value,
                        "active_contribution_support_fact_ids": list(
                            matching_contrib.active_supporting_fact_ids
                        ),
                    }
                else:
                    entry["status"] = ClaimAtomStatus.UNSUPPORTED.value
                    entry["unsupported_reason"] = "ROLE_CONTRIBUTION_GROUNDING_FAILED"
            elif atom_type is ClaimAtomType.METRIC:
                if best_fact.metric_details is not None:
                    entry["metric_grounding"] = best_fact.metric_details.model_dump(mode="json")
                else:
                    entry["status"] = ClaimAtomStatus.UNSUPPORTED.value
                    entry["unsupported_reason"] = "METRIC_GROUNDING_MISSING"

            if entry["status"] == ClaimAtomStatus.UNSUPPORTED.value and "unsupported_reason" not in entry:
                entry["unsupported_reason"] = "NO_DIRECT_FACT_SUPPORT"

            atom_validations.append(entry)
            statuses.append(str(entry["status"]))

        return RawProviderStageResponse(
            payload={
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "validation_run_id": str(meta["validation_run_id"]),
                "generator_run_id": str(meta["generator_run_id"]),
                "atomizer_run_id": str(meta["atomizer_run_id"]),
                "claim_id": str(meta["claim_id"]),
                "claim_version": int(meta["claim_version"]),
                "atom_validations": atom_validations,
                "computed_verification": compute_expected_claim_verification(statuses).value,
            },
            stream_completed=True,
        )


def _metric_grounding_matches_fact(
    *,
    atom_text: str,
    proposed_metric: Mapping[str, Any] | None,
    fact_metric: MetricDetailsModel | None,
    fact_object_text: str = "",
) -> tuple[bool, str | None]:
    """CLAIM-06 6-element metric verification (value, unit, period, source_basis, env, scope)."""
    if proposed_metric is None or fact_metric is None:
        return False, "METRIC_SIX_ELEMENTS_MISSING"
    try:
        parsed = MetricDetailsModel.model_validate(proposed_metric)
    except Exception:  # noqa: BLE001
        return False, "METRIC_SIX_ELEMENTS_INVALID"

    # Strict element-by-element match against canonical SUPPORTED METRIC fact
    if _norm_str(parsed.value) != _norm_str(fact_metric.value):
        return False, "METRIC_VALUE_MISMATCH"
    if _norm_str(parsed.unit) != _norm_str(fact_metric.unit):
        return False, "METRIC_UNIT_MISMATCH"
    if _norm_str(parsed.period) != _norm_str(fact_metric.period):
        return False, "METRIC_PERIOD_MISMATCH"
    if _norm_str(parsed.source_basis) != _norm_str(fact_metric.source_basis):
        return False, "METRIC_SOURCE_BASIS_MISMATCH"
    if parsed.environment_scope is not fact_metric.environment_scope:
        return False, "METRIC_ENVIRONMENT_SCOPE_MISMATCH"
    if _norm_str(parsed.measurement_scope) != _norm_str(fact_metric.measurement_scope):
        return False, "METRIC_MEASUREMENT_SCOPE_MISMATCH"

    # Guard against over-generalized text in atom_text vs narrower fact scope/period (SCN-12, SCN-16)
    fact_scope_norm = _norm_str(f"{fact_metric.measurement_scope} {fact_metric.period}")
    if _OVERGENERAL_SCOPE_RE.search(atom_text):
        for match in _OVERGENERAL_SCOPE_RE.finditer(atom_text):
            if _norm_str(match.group(0)) not in fact_scope_norm:
                return False, "METRIC_SCOPE_OVERGENERALIZED"

    # Guard against numeric distortion in atom_text vs fact value/object_text (e.g., 10x distortion SCN-14)
    atom_nums = {
        re.sub(r"\s+", "", m.group(0).lower()) for m in _NUMBER_TOKEN_RE.finditer(atom_text)
    }
    fact_val_clean = re.sub(r"\s+", "", fact_metric.value.lower())
    fact_all_nums = {
        re.sub(r"\s+", "", m.group(0).lower())
        for m in _NUMBER_TOKEN_RE.finditer(
            f"{fact_metric.value} {fact_metric.unit} {fact_metric.period} "
            f"{fact_metric.source_basis} {fact_metric.measurement_scope} {fact_object_text}"
        )
    } | {fact_val_clean}
    for num in atom_nums:
        # Ignore pure bare digits that are part of fact numbers (e.g., "30" in "30 days")
        if num not in fact_all_nums and not any(num in fn for fn in fact_all_nums):
            return False, "METRIC_NUMERIC_DISTORTION"

    return True, None


def _semantic_fidelity_check(
    *,
    atom_type: ClaimAtomType,
    atom_text: str,
    fact: FactEvidenceSnapshot,
) -> str | None:
    """Enforce SCN-06..10 & SCN-14 semantic invariants between atom text and canonical Fact."""
    sem = fact.semantic
    # SCN-06: PLANNED fact cannot support a completed claim unless atom explicitly states planned/proposed
    if sem.completion_state is FactCompletionState.PLANNED and not _PLANNED_WORDING_RE.search(atom_text):
        return "PLANNED_FACT_CANNOT_SUPPORT_COMPLETED_CLAIM"

    # SCN-07: NEGATED / TARGET_UNMET fact cannot support a positive claim unless atom preserves negation
    if sem.completion_state in (
        FactCompletionState.NEGATED,
        FactCompletionState.TARGET_UNMET,
    ) and not _NEGATED_WORDING_RE.search(atom_text):
        return "NEGATED_OR_UNMET_FACT_CONTRADICTS_CLAIM"

    # SCN-08 / SCN-05: TEAM fact cannot support personal/individual attribution
    if sem.scope is FactAttributionScope.TEAM and (
        atom_type is ClaimAtomType.ROLE or _INDIVIDUAL_ASSERTION_RE.search(atom_text)
    ):
        return "TEAM_SCOPE_CANNOT_SUPPORT_INDIVIDUAL_ROLE"

    # SCN-09: STAGING_TEST fact cannot support positive PRODUCTION claim (excluding explicit pre-production rollbacks like SCN-07)
    if sem.environment_scope is FactEnvironmentScope.STAGING_TEST and _PRODUCTION_ASSERTION_RE.search(
        _NON_PRODUCTION_QUALIFIER_RE.sub("", atom_text)
    ):
        return "STAGING_FACT_CANNOT_SUPPORT_PRODUCTION_CLAIM"

    # SCN-10: OBSERVED_CORRELATION cannot support direct causal assertion
    if sem.causal_basis is FactCausalBasis.OBSERVED_CORRELATION and _CAUSAL_ASSERTION_RE.search(
        atom_text
    ):
        return "CORRELATION_CANNOT_SUPPORT_CAUSAL_CLAIM"

    # SCN-12 / SCN-16 scope over-generalization on RESULT/ACTION/METRIC atoms
    if _OVERGENERAL_SCOPE_RE.search(atom_text):
        fact_text_norm = _norm_str(f"{fact.predicate} {fact.object_text}")
        for match in _OVERGENERAL_SCOPE_RE.finditer(atom_text):
            if _norm_str(match.group(0)) not in fact_text_norm:
                return "CLAIM_SCOPE_OVERGENERALIZED"

    return None


class ClaimSupportValidator:
    """Stage 3C independent ClaimSupportValidator with deterministic adversarial guards."""

    def __init__(self, *, stage_runner: BoundedStageRunner) -> None:
        self._stage_runner = stage_runner

    def validate(self, request: ClaimSupportValidationRequest) -> ClaimSupportValidationResult:
        val_run_id = (request.validation_run_id or str(uuid.uuid4())).strip()
        gen_run_id = request.generator_run_id.strip()
        atom_run_id = request.atomizer_run_id.strip()

        # Hard separation guard: all 3 stage run IDs must be pairwise distinct!
        if len({gen_run_id, atom_run_id, val_run_id}) < 3 or not all(
            (gen_run_id, atom_run_id, val_run_id)
        ):
            raise ClaimError(
                status_code=422,
                code="STAGE_RUN_ID_COLLISION",
                message=(
                    "Stage 3A generator_run_id, Stage 3B atomizer_run_id, and Stage 3C "
                    "validation_run_id must be pairwise distinct."
                ),
            )

        input_hash = compute_claim_validation_input_hash(
            claim_id=request.claim_id,
            claim_version=request.claim_version,
            atomizer_run_id=atom_run_id,
            fact_ids=list(request.fact_snapshots.keys()),
            validator_version=request.validator_version,
            prompt_version=request.prompt_version,
        )
        all_cu_ids = sorted(
            {
                cu_id
                for f in request.fact_snapshots.values()
                for (cu_id, _, _, _) in f.support_units.values()
            }
        )
        all_rev_ids = sorted(
            {
                rev_id
                for f in request.fact_snapshots.values()
                for (_, rev_id, _, _) in f.support_units.values()
            }
        )
        input_artifact_versions: dict[str, Any] = {
            "claim_id": request.claim_id,
            "claim_version": request.claim_version,
            "fact_ids": sorted(request.fact_snapshots.keys()),
            "contribution_ids": sorted(c.contribution_id for c in request.contributions),
            "content_unit_ids": all_cu_ids,
            "source_revision_ids": all_rev_ids,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "schema_version": SCHEMA_PACKAGE_VERSION,
        }

        atoms_by_ordinal = {a.ordinal: a for a in request.atoms}
        if not atoms_by_ordinal:
            return ClaimSupportValidationResult(
                validation_run_id=val_run_id,
                generator_run_id=gen_run_id,
                atomizer_run_id=atom_run_id,
                claim_id=request.claim_id,
                claim_version=request.claim_version,
                run_status=GenerationRunStatus.FAILED,
                error_code="NO_CLAIM_ATOMS",
                atom_results=(),
                computed_verification=ClaimVerification.UNVERIFIED,
                next_claim_status=ClaimStatus.DRAFT_INVALID,
                input_hash=input_hash,
                validator_version=request.validator_version,
                prompt_version=request.prompt_version,
                model=request.model,
                input_artifact_versions=input_artifact_versions,
                run_metadata={"error_code": "NO_CLAIM_ATOMS"},
            )

        manifest_text = json.dumps(
            {
                "event_id": request.event_id,
                "claim_id": request.claim_id,
                "claim_version": request.claim_version,
                "atoms": [a.to_stage3b_dict() for a in request.atoms],
                "facts": [
                    {
                        "fact_id": f.fact_id,
                        "fact_type": f.fact_type.value,
                        "status": f.status.value,
                        "predicate": f.predicate,
                        "object_text": f.object_text,
                        "subject_identity_id": f.subject_identity_id,
                    }
                    for f in sorted(request.fact_snapshots.values(), key=lambda x: x.fact_id)
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        resolution, expected_rev_hash, expected_span_hash = build_ephemeral_manifest_resolution(
            operation_id=f"stage3c-{val_run_id}",
            manifest_text=manifest_text,
            revision_id=all_rev_ids[0] if all_rev_ids else f"val-rev-{request.claim_id}",
            content_unit_id=all_cu_ids[0] if all_cu_ids else f"val-cu-{request.claim_id}",
        )
        context_meta = {
            "validation_run_id": val_run_id,
            "generator_run_id": gen_run_id,
            "atomizer_run_id": atom_run_id,
            "event_id": request.event_id,
            "claim_id": request.claim_id,
            "claim_version": request.claim_version,
        }
        envelope = self._stage_runner.run_stage(
            StageExecutionRequest(
                access=request.access,
                stage_code=LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION,
                provider_mode=request.provider_mode,
                source_resource_id=request.source_resource_id,
                visibility=request.visibility,
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

        if (
            envelope.status is not StageRunStatus.SUCCEEDED
            or not envelope.committed_allowed
            or not isinstance(envelope.validated_payload, Mapping)
        ):
            fail_atoms = tuple(
                AtomValidationOutcome(
                    ordinal=a.ordinal,
                    atom_type=a.atom_type,
                    atom_text=a.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=(),
                    unsupported_reason=envelope.reason_code.value,
                )
                for a in sorted(request.atoms, key=lambda x: x.ordinal)
            )
            return ClaimSupportValidationResult(
                validation_run_id=val_run_id,
                generator_run_id=gen_run_id,
                atomizer_run_id=atom_run_id,
                claim_id=request.claim_id,
                claim_version=request.claim_version,
                run_status=GenerationRunStatus.FAILED,
                error_code=envelope.reason_code.value,
                atom_results=fail_atoms,
                computed_verification=ClaimVerification.UNVERIFIED,
                next_claim_status=ClaimStatus.DRAFT_INVALID,
                input_hash=input_hash,
                validator_version=request.validator_version,
                prompt_version=request.prompt_version,
                model=request.model,
                input_artifact_versions=input_artifact_versions,
                run_metadata={"stage_envelope": envelope.to_audit_dict(), "error_code": envelope.reason_code.value},
            )

        payload = dict(envelope.validated_payload)
        violations = validate_stage_payload("claim_support_validation", payload, raise_on_error=False)
        if violations:
            fail_atoms = tuple(
                AtomValidationOutcome(
                    ordinal=a.ordinal,
                    atom_type=a.atom_type,
                    atom_text=a.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=(),
                    unsupported_reason="AI_SCHEMA_INVALID",
                )
                for a in sorted(request.atoms, key=lambda x: x.ordinal)
            )
            return ClaimSupportValidationResult(
                validation_run_id=val_run_id,
                generator_run_id=gen_run_id,
                atomizer_run_id=atom_run_id,
                claim_id=request.claim_id,
                claim_version=request.claim_version,
                run_status=GenerationRunStatus.FAILED,
                error_code="AI_SCHEMA_INVALID",
                atom_results=fail_atoms,
                computed_verification=ClaimVerification.UNVERIFIED,
                next_claim_status=ClaimStatus.DRAFT_INVALID,
                input_hash=input_hash,
                validator_version=request.validator_version,
                prompt_version=request.prompt_version,
                model=request.model,
                input_artifact_versions=input_artifact_versions,
                run_metadata={"error_code": "AI_SCHEMA_INVALID", "validation_codes": sorted({v.code for v in violations})},
            )

        proposed_by_ord: dict[int, Mapping[str, Any]] = {
            int(item["ordinal"]): item for item in payload["atom_validations"]
        }

        outcomes: list[AtomValidationOutcome] = []
        for atom in sorted(request.atoms, key=lambda a: a.ordinal):
            proposed = proposed_by_ord.get(atom.ordinal)
            outcome = self._evaluate_single_atom(
                atom=atom,
                proposed=proposed,
                event_id=request.event_id,
                fact_snapshots=request.fact_snapshots,
                contributions=request.contributions,
                identity_eligibilities=request.identity_eligibilities,
                matched_content_unit_ids=request.matched_content_unit_ids,
            )
            outcomes.append(outcome)

        computed_verif = compute_expected_claim_verification([o.status.value for o in outcomes])
        all_supported_or_confirmed = bool(outcomes) and all(
            o.status in (ClaimAtomStatus.SUPPORTED, ClaimAtomStatus.USER_CONFIRMED)
            for o in outcomes
        )
        next_claim_status = (
            ClaimStatus.SUPPORT_VALIDATED if all_supported_or_confirmed else ClaimStatus.DRAFT_INVALID
        )

        atom_validation_details = {
            str(o.ordinal): {
                "unsupported_reason": o.unsupported_reason,
                "role_contribution_grounding": o.role_contribution_grounding,
                "metric_grounding": o.metric_grounding,
            }
            for o in outcomes
        }
        run_meta: dict[str, Any] = {
            "stage_code": LlmStageCode.STAGE_3C_CLAIM_SUPPORT_VALIDATION.value,
            "output_schema_id": CLAIM_SUPPORT_VALIDATION_SCHEMA_ID,
            "output_schema_version": SCHEMA_PACKAGE_VERSION,
            "generator_run_id": gen_run_id,
            "atomizer_run_id": atom_run_id,
            "computed_verification": computed_verif.value,
            "next_claim_status": next_claim_status.value,
            "atom_validation_details": atom_validation_details,
            "stage_envelope": envelope.to_audit_dict(),
        }
        assert_no_raw_text_or_secret_keys(run_meta, context="ClaimSupportValidator.run_metadata")
        return ClaimSupportValidationResult(
            validation_run_id=val_run_id,
            generator_run_id=gen_run_id,
            atomizer_run_id=atom_run_id,
            claim_id=request.claim_id,
            claim_version=request.claim_version,
            run_status=GenerationRunStatus.SUCCEEDED,
            error_code=None,
            atom_results=tuple(outcomes),
            computed_verification=computed_verif,
            next_claim_status=next_claim_status,
            input_hash=input_hash,
            validator_version=request.validator_version,
            prompt_version=request.prompt_version,
            model=request.model,
            input_artifact_versions=input_artifact_versions,
            run_metadata=run_meta,
        )

    def _evaluate_single_atom(
        self,
        *,
        atom: ClaimAtomDraftItem,
        proposed: Mapping[str, Any] | None,
        event_id: str,
        fact_snapshots: Mapping[str, FactEvidenceSnapshot],
        contributions: Sequence[ContributionEvidenceSnapshot],
        identity_eligibilities: Mapping[str, IdentityAttributionEligibility],
        matched_content_unit_ids: frozenset[str],
    ) -> AtomValidationOutcome:
        if proposed is None:
            return AtomValidationOutcome(
                ordinal=atom.ordinal,
                atom_type=atom.atom_type,
                atom_text=atom.atom_text,
                status=ClaimAtomStatus.UNSUPPORTED,
                fact_bindings=(),
                unsupported_reason="MISSING_ATOM_VALIDATION",
            )

        proposed_status = ClaimAtomStatus(str(proposed["status"]))
        proposed_reason = (
            str(proposed["unsupported_reason"])
            if proposed.get("unsupported_reason") is not None
            else None
        )
        raw_bindings = proposed.get("fact_bindings") or []

        # Collect structurally valid bindings strictly on the SAME event
        validated_bindings: list[AtomFactBindingDraft] = []
        has_contradicts_binding = False
        direct_supported_facts: list[FactEvidenceSnapshot] = []
        direct_user_confirmed_facts: list[FactEvidenceSnapshot] = []

        for raw_b in raw_bindings:
            fid = str(raw_b["fact_id"])
            stype = SupportType(str(raw_b["support_type"]))
            fs_ids = tuple(str(x) for x in raw_b["fact_support_ids"])
            rev_id = str(raw_b["source_object_revision_id"])
            cu_id = str(raw_b["content_unit_id"])

            fact = fact_snapshots.get(fid)
            if fact is None or fact.event_id != event_id:
                continue

            validated_bindings.append(
                AtomFactBindingDraft(
                    fact_id=fid,
                    support_type=stype,
                    fact_support_ids=fs_ids,
                    source_object_revision_id=rev_id,
                    content_unit_id=cu_id,
                )
            )
            if stype is SupportType.CONTRADICTS or fact.active_contradicts_support_ids:
                has_contradicts_binding = True
                continue
            if stype is not SupportType.DIRECT:
                # SCN-04: CONTEXT-only binding never promotes to SUPPORTED
                continue

            if fact.status is FactStatus.USER_CONFIRMED:
                direct_user_confirmed_facts.append(fact)
                continue

            if fact.status is not FactStatus.SUPPORTED:
                continue

            # Verify active DIRECT fact_support -> ACTIVE ContentUnit -> non-tombstoned Revision -> MATCHED Plan C refetch
            valid_provenance = False
            for fs_id in fs_ids:
                if fs_id not in fact.active_direct_support_ids:
                    continue
                unit_info = fact.support_units.get(fs_id)
                if unit_info is None:
                    continue
                u_cu_id, u_rev_id, u_policy, u_tombstone = unit_info
                if (
                    u_cu_id == cu_id
                    and u_rev_id == rev_id
                    and u_policy == "ACTIVE"
                    and not u_tombstone
                    and cu_id in matched_content_unit_ids
                ):
                    valid_provenance = True
                    break

            if valid_provenance:
                direct_supported_facts.append(fact)

        bindings_tuple = tuple(validated_bindings)

        # If validator itself rejected or any CONTRADICTS exists -> UNSUPPORTED
        if proposed_status in (ClaimAtomStatus.UNSUPPORTED, ClaimAtomStatus.STALE):
            return AtomValidationOutcome(
                ordinal=atom.ordinal,
                atom_type=atom.atom_type,
                atom_text=atom.atom_text,
                status=ClaimAtomStatus.UNSUPPORTED,
                fact_bindings=bindings_tuple,
                unsupported_reason=proposed_reason or "VALIDATOR_MARKED_UNSUPPORTED",
            )

        if has_contradicts_binding:
            return AtomValidationOutcome(
                ordinal=atom.ordinal,
                atom_type=atom.atom_type,
                atom_text=atom.atom_text,
                status=ClaimAtomStatus.UNSUPPORTED,
                fact_bindings=bindings_tuple,
                unsupported_reason="CONTRADICTED_BY_CANONICAL_FACT",
            )

        # SCN-12 claim-12-4: USER_CONFIRMED atom NEVER promotes to SUPPORTED
        if proposed_status is ClaimAtomStatus.USER_CONFIRMED or (
            not direct_supported_facts and direct_user_confirmed_facts
        ):
            # Ensure referenced identity is not conflicted/revoked/bot
            for ufact in direct_user_confirmed_facts:
                if ufact.subject_identity_id is not None:
                    elig = identity_eligibilities.get(ufact.subject_identity_id)
                    if elig is None or not elig.can_support_user_confirmed:
                        return AtomValidationOutcome(
                            ordinal=atom.ordinal,
                            atom_type=atom.atom_type,
                            atom_text=atom.atom_text,
                            status=ClaimAtomStatus.UNSUPPORTED,
                            fact_bindings=bindings_tuple,
                            unsupported_reason="IDENTITY_INELIGIBLE_FOR_USER_CONFIRMED",
                        )
            if not direct_user_confirmed_facts and not direct_supported_facts:
                # Explicitly user-confirmed atom without source-backed facts
                if proposed_status is ClaimAtomStatus.USER_CONFIRMED:
                    return AtomValidationOutcome(
                        ordinal=atom.ordinal,
                        atom_type=atom.atom_type,
                        atom_text=atom.atom_text,
                        status=ClaimAtomStatus.USER_CONFIRMED,
                        fact_bindings=bindings_tuple,
                    )
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="NO_DIRECT_SUPPORTED_FACT",
                )
            return AtomValidationOutcome(
                ordinal=atom.ordinal,
                atom_type=atom.atom_type,
                atom_text=atom.atom_text,
                status=ClaimAtomStatus.USER_CONFIRMED,
                fact_bindings=bindings_tuple,
            )

        # Now evaluating ClaimAtomStatus.SUPPORTED: require >= 1 verified direct SUPPORTED fact
        if not direct_supported_facts:
            return AtomValidationOutcome(
                ordinal=atom.ordinal,
                atom_type=atom.atom_type,
                atom_text=atom.atom_text,
                status=ClaimAtomStatus.UNSUPPORTED,
                fact_bindings=bindings_tuple,
                unsupported_reason="NO_VERIFIED_DIRECT_SUPPORTED_FACT",
            )

        # Semantic fidelity guards (SCN-06..10, SCN-12, SCN-14, SCN-16)
        for sfact in direct_supported_facts:
            sem_err = _semantic_fidelity_check(
                atom_type=atom.atom_type,
                atom_text=atom.atom_text,
                fact=sfact,
            )
            if sem_err is not None:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason=sem_err,
                )

            # Identity check for any fact with subject_identity_id (SCN-11)
            if sfact.subject_identity_id is not None:
                elig = identity_eligibilities.get(sfact.subject_identity_id)
                if elig is None or not elig.can_support_source_verified:
                    return AtomValidationOutcome(
                        ordinal=atom.ordinal,
                        atom_type=atom.atom_type,
                        atom_text=atom.atom_text,
                        status=ClaimAtomStatus.UNSUPPORTED,
                        fact_bindings=bindings_tuple,
                        unsupported_reason="IDENTITY_NOT_SOURCE_VERIFIED_ELIGIBLE",
                    )

        # CLAIM-05 Role / Contribution Gate (SCN-05, SCN-08, SCN-11, SCN-15)
        role_grounding_out: dict[str, Any] | None = None
        if atom.atom_type is ClaimAtomType.ROLE or _INDIVIDUAL_ASSERTION_RE.search(atom.atom_text):
            raw_rg = proposed.get("role_contribution_grounding")
            if not isinstance(raw_rg, Mapping):
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="CLAIM05_ROLE_CONTRIBUTION_GROUNDING_REQUIRED",
                )
            rg_ident = str(raw_rg.get("subject_identity_id", ""))
            rg_evt = str(raw_rg.get("confirmed_event_id", ""))
            try:
                rg_role = ContributionRole(str(raw_rg.get("role", "")))
            except ValueError:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="CLAIM05_INVALID_CONTRIBUTION_ROLE",
                )
            rg_fact_ids = tuple(str(x) for x in raw_rg.get("active_contribution_support_fact_ids", ()))

            # 1. Identity must be ACTIVE VERIFIED PERSON (0 conflicted mappings)
            elig = identity_eligibilities.get(rg_ident)
            if elig is None or not elig.can_support_source_verified:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="CLAIM05_IDENTITY_NOT_VERIFIED",
                )
            if rg_evt != event_id:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="CLAIM05_CROSS_EVENT_CONTRIBUTION",
                )

            # 2. Matching SOURCE_SUPPORTED contribution with >= 1 ACTIVE contribution_support -> concrete SUPPORTED Fact
            matching_contrib = next(
                (
                    c
                    for c in contributions
                    if c.event_id == event_id
                    and c.identity_id == rg_ident
                    and c.role is rg_role
                    and c.status is ContributionStatus.SOURCE_SUPPORTED
                ),
                None,
            )
            if matching_contrib is None:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="CLAIM05_NO_SOURCE_SUPPORTED_CONTRIBUTION",
                )

            valid_cs_facts = [
                cs_fid
                for cs_fid in rg_fact_ids
                if cs_fid in matching_contrib.active_supporting_fact_ids
                and cs_fid in fact_snapshots
                and fact_snapshots[cs_fid].event_id == event_id
                and fact_snapshots[cs_fid].status is FactStatus.SUPPORTED
                and bool(fact_snapshots[cs_fid].active_direct_support_ids)
                and not fact_snapshots[cs_fid].active_contradicts_support_ids
            ]
            if not valid_cs_facts:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="CLAIM05_MISSING_CONCRETE_CONTRIBUTION_SUPPORT_FACT",
                )

            # Ensure bound ROLE fact subject (if set) matches contribution identity
            for sfact in direct_supported_facts:
                if sfact.subject_identity_id is not None and sfact.subject_identity_id != rg_ident:
                    return AtomValidationOutcome(
                        ordinal=atom.ordinal,
                        atom_type=atom.atom_type,
                        atom_text=atom.atom_text,
                        status=ClaimAtomStatus.UNSUPPORTED,
                        fact_bindings=bindings_tuple,
                        unsupported_reason="CLAIM05_SUBJECT_IDENTITY_MISMATCH",
                    )

            role_grounding_out = {
                "subject_identity_id": rg_ident,
                "confirmed_event_id": event_id,
                "role": rg_role.value,
                "active_contribution_support_fact_ids": sorted(set(valid_cs_facts)),
            }

        # CLAIM-06 6-Element Metric Gate (SCN-03, SCN-09, SCN-10, SCN-12, SCN-14, SCN-16)
        metric_grounding_out: dict[str, Any] | None = None
        if atom.atom_type is ClaimAtomType.METRIC:
            raw_mg = proposed.get("metric_grounding")
            metric_facts = [
                f for f in direct_supported_facts if f.metric_details is not None
            ]
            if not metric_facts:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason="CLAIM06_NO_GROUNDED_METRIC_FACT",
                )
            matched_metric = False
            last_err = "CLAIM06_METRIC_MISMATCH"
            for mfact in metric_facts:
                ok, err = _metric_grounding_matches_fact(
                    atom_text=atom.atom_text,
                    proposed_metric=raw_mg if isinstance(raw_mg, Mapping) else None,
                    fact_metric=mfact.metric_details,
                    fact_object_text=mfact.object_text,
                )
                if ok:
                    matched_metric = True
                    metric_grounding_out = mfact.metric_details.model_dump(mode="json")  # type: ignore[union-attr]
                    break
                if err:
                    last_err = err
            if not matched_metric:
                return AtomValidationOutcome(
                    ordinal=atom.ordinal,
                    atom_type=atom.atom_type,
                    atom_text=atom.atom_text,
                    status=ClaimAtomStatus.UNSUPPORTED,
                    fact_bindings=bindings_tuple,
                    unsupported_reason=last_err,
                )

        return AtomValidationOutcome(
            ordinal=atom.ordinal,
            atom_type=atom.atom_type,
            atom_text=atom.atom_text,
            status=ClaimAtomStatus.SUPPORTED,
            fact_bindings=bindings_tuple,
            unsupported_reason=None,
            role_contribution_grounding=role_grounding_out,
            metric_grounding=metric_grounding_out,
        )


__all__ = [
    "CLAIM_SUPPORT_VALIDATION_SCHEMA_ID",
    "AtomFactBindingDraft",
    "AtomValidationOutcome",
    "ClaimSupportValidationRequest",
    "ClaimSupportValidationResult",
    "ClaimSupportValidator",
    "ContributionEvidenceSnapshot",
    "DeterministicClaimValidatorAdapter",
    "FactEvidenceSnapshot",
    "compute_claim_validation_input_hash",
]
