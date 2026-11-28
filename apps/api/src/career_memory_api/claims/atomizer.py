"""Stage 3B ClaimAtomizer (T-405) — strictly separated from Stage 3A Generator & Stage 3C Validator.

Enforces:
- Hard guard: ``atomizer_run_id != generator_run_id`` (``422 STAGE_RUN_ID_COLLISION``).
- Executes through ``career_memory_api.llm_gate.BoundedStageRunner``
  (``LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION``) against ``claim_atomization.v1.schema.json``.
- Decomposes one ``(claim_id, claim_version)`` into contiguous ordered atoms
  (``ordinal = 0..K-1``) starting strictly in ``status = ClaimAtomStatus.UNSUPPORTED``.
- NEVER assigns ``SUPPORTED`` or ``claim_atom_support``.
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
from career_memory_api.claims.models import (
    DEFAULT_CLAIM_ATOMIZER_MODEL,
    DEFAULT_CLAIM_ATOMIZER_PROMPT_VERSION,
    DEFAULT_CLAIM_ATOMIZER_VERSION,
    ClaimError,
    build_ephemeral_manifest_resolution,
)
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_BASE_URI,
    SCHEMA_PACKAGE_VERSION,
    validate_stage_payload,
)
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
    GenerationRunStatus,
    ResourceVisibility,
)

CLAIM_ATOMIZATION_SCHEMA_ID = f"{SCHEMA_BASE_URI}/claim_atomization.v1.schema.json"

_METRIC_HINT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|x|rps|qps|mb|gb|hours?|days?)\b", re.IGNORECASE)
_ROLE_HINT_RE = re.compile(r"\b(led|lead|owned|architected|reviewed|authored|designed|operated|mentored)\b", re.IGNORECASE)
_PROBLEM_HINT_RE = re.compile(r"\b(bottleneck|outage|incident|timeout|failure|issue|bug|latency spike|problem)\b", re.IGNORECASE)
_RESULT_HINT_RE = re.compile(r"\b(reduced|improved|increased|eliminated|prevented|accelerated|achieved|cut)\b", re.IGNORECASE)
_TECH_HINT_RE = re.compile(r"\b(postgres|postgresql|redis|kafka|fastapi|react|kubernetes|docker|datadog|sql|python|typescript)\b", re.IGNORECASE)


def compute_atomization_input_hash(
    *,
    claim_id: str,
    claim_version: int,
    claim_text: str,
    atomizer_version: str,
    prompt_version: str,
) -> str:
    canonical = json.dumps(
        {
            "atomizer_version": atomizer_version,
            "claim_id": claim_id,
            "claim_text_sha256": hashlib.sha256(claim_text.encode("utf-8")).hexdigest(),
            "claim_version": claim_version,
            "prompt_version": prompt_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def deterministic_classify_atom_type(clause: str) -> ClaimAtomType:
    """Deterministic rule classifier for offline revalidation atomization."""
    if _METRIC_HINT_RE.search(clause):
        return ClaimAtomType.METRIC
    if _ROLE_HINT_RE.search(clause):
        return ClaimAtomType.ROLE
    if _RESULT_HINT_RE.search(clause):
        return ClaimAtomType.RESULT
    if _PROBLEM_HINT_RE.search(clause):
        return ClaimAtomType.PROBLEM
    if _TECH_HINT_RE.search(clause):
        return ClaimAtomType.TECHNOLOGY
    return ClaimAtomType.ACTION


def deterministic_split_claim_atoms(claim_text: str) -> list[dict[str, Any]]:
    """Deterministically decompose claim text into 1..N ordered typed clauses."""
    parts = [
        p.strip(" .;")
        for p in re.split(r"(?:\.\s+|;\s+|\s+and\s+(?=(?:reduced|improved|cut|led|implemented|migrated|achieved|owned)))", claim_text.strip())
        if p and p.strip(" .;")
    ]
    if not parts:
        parts = [claim_text.strip()]
    return [
        {
            "ordinal": idx,
            "atom_type": deterministic_classify_atom_type(part).value,
            "atom_text": part,
        }
        for idx, part in enumerate(parts)
    ]


class DeterministicClaimAtomizerAdapter:
    """Offline deterministic Stage 3B atomizer adapter (used for non-LLM revalidation)."""

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return "SYNTHETIC_FIXTURE"

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        meta = adapter_input.context_metadata
        parsed_manifest = json.loads(adapter_input.ephemeral_source_text)
        claim_text = str(parsed_manifest.get("claim_text", ""))
        atoms = deterministic_split_claim_atoms(claim_text)
        return RawProviderStageResponse(
            payload={
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "atomizer_run_id": str(meta["atomizer_run_id"]),
                "generator_run_id": str(meta["generator_run_id"]),
                "claim_id": str(meta["claim_id"]),
                "claim_version": int(meta["claim_version"]),
                "atoms": atoms,
            },
            stream_completed=True,
        )


@dataclass(frozen=True, slots=True)
class ClaimAtomDraftItem:
    """Single Stage 3B atom draft — ALWAYS initialized as UNSUPPORTED."""

    ordinal: int
    atom_type: ClaimAtomType
    atom_text: str
    initial_status: ClaimAtomStatus = ClaimAtomStatus.UNSUPPORTED

    def to_stage3b_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "atom_type": self.atom_type.value,
            "atom_text": self.atom_text,
        }


@dataclass(frozen=True, slots=True)
class ClaimAtomizationRequest:
    """Input for Stage 3B ClaimAtomizer."""

    access: WorkspaceAccessContext
    claim_id: str
    claim_version: int
    claim_text: str
    generator_run_id: str
    source_resource_id: str
    visibility: ResourceVisibility | str
    budget: StageBudgetPolicy
    adapter: StageExecutionAdapterPort
    provider_mode: LlmProviderMode
    allow_synthetic_fixture_mode: bool = False
    atomizer_run_id: str | None = None
    atomizer_version: str = DEFAULT_CLAIM_ATOMIZER_VERSION
    prompt_version: str = DEFAULT_CLAIM_ATOMIZER_PROMPT_VERSION
    model: str = DEFAULT_CLAIM_ATOMIZER_MODEL
    job_reference: ConnectionJobReference | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    before_commit_hook: Callable[[StagePreCommitContext], bool | None] | None = None
    verify_workspace_membership_active: Callable[[WorkspaceAccessContext], bool] | None = None


@dataclass(frozen=True, slots=True)
class ClaimAtomizationResult:
    """Outcome of Stage 3B ClaimAtomizer."""

    atomizer_run_id: str
    generator_run_id: str
    claim_id: str
    claim_version: int
    status: GenerationRunStatus
    error_code: str | None
    atoms: tuple[ClaimAtomDraftItem, ...]
    input_hash: str
    atomizer_version: str
    prompt_version: str
    model: str
    input_artifact_versions: dict[str, Any]
    run_metadata: dict[str, Any]


class ClaimAtomizer:
    """Stage 3B ClaimAtomizer enforcing distinct run_id and UNSUPPORTED initial state."""

    def __init__(self, *, stage_runner: BoundedStageRunner) -> None:
        self._stage_runner = stage_runner

    def atomize(self, request: ClaimAtomizationRequest) -> ClaimAtomizationResult:
        run_id = (request.atomizer_run_id or str(uuid.uuid4())).strip()
        gen_run_id = request.generator_run_id.strip()

        # Hard separation guard (T-106 / T-405)
        if not gen_run_id or run_id == gen_run_id:
            raise ClaimError(
                status_code=422,
                code="STAGE_RUN_ID_COLLISION",
                message="Stage 3B atomizer_run_id must be distinct from Stage 3A generator_run_id.",
            )

        input_hash = compute_atomization_input_hash(
            claim_id=request.claim_id,
            claim_version=request.claim_version,
            claim_text=request.claim_text,
            atomizer_version=request.atomizer_version,
            prompt_version=request.prompt_version,
        )
        input_artifact_versions: dict[str, Any] = {
            "claim_id": request.claim_id,
            "claim_version": request.claim_version,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "schema_version": SCHEMA_PACKAGE_VERSION,
        }

        def _failed(error_code: str, *, extra_meta: dict[str, Any] | None = None) -> ClaimAtomizationResult:
            run_meta: dict[str, Any] = {
                "stage_code": LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION.value,
                "output_schema_id": CLAIM_ATOMIZATION_SCHEMA_ID,
                "output_schema_version": SCHEMA_PACKAGE_VERSION,
                "generator_run_id": gen_run_id,
                "error_code": error_code,
            }
            if extra_meta:
                run_meta.update(extra_meta)
            assert_no_raw_text_or_secret_keys(run_meta, context="ClaimAtomizer.run_metadata")
            return ClaimAtomizationResult(
                atomizer_run_id=run_id,
                generator_run_id=gen_run_id,
                claim_id=request.claim_id,
                claim_version=request.claim_version,
                status=GenerationRunStatus.FAILED,
                error_code=error_code,
                atoms=(),
                input_hash=input_hash,
                atomizer_version=request.atomizer_version,
                prompt_version=request.prompt_version,
                model=request.model,
                input_artifact_versions=input_artifact_versions,
                run_metadata=run_meta,
            )

        manifest_text = json.dumps(
            {
                "claim_id": request.claim_id,
                "claim_version": request.claim_version,
                "claim_text": request.claim_text,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        resolution, expected_rev_hash, expected_span_hash = build_ephemeral_manifest_resolution(
            operation_id=f"stage3b-{run_id}",
            manifest_text=manifest_text,
            revision_id=f"claim-rev-{request.claim_id}-v{request.claim_version}",
            content_unit_id=f"claim-cu-{request.claim_id}-v{request.claim_version}",
        )
        context_meta = {
            "atomizer_run_id": run_id,
            "generator_run_id": gen_run_id,
            "claim_id": request.claim_id,
            "claim_version": request.claim_version,
        }
        envelope = self._stage_runner.run_stage(
            StageExecutionRequest(
                access=request.access,
                stage_code=LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION,
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
            return _failed(
                envelope.reason_code.value,
                extra_meta={"stage_envelope": envelope.to_audit_dict()},
            )

        payload = dict(envelope.validated_payload)
        violations = validate_stage_payload("claim_atomization", payload, raise_on_error=False)
        if violations:
            return _failed(
                "AI_SCHEMA_INVALID",
                extra_meta={"validation_codes": sorted({v.code for v in violations})},
            )

        if (
            str(payload["atomizer_run_id"]) != run_id
            or str(payload["generator_run_id"]) != gen_run_id
            or str(payload["claim_id"]) != request.claim_id
            or int(payload["claim_version"]) != request.claim_version
        ):
            return _failed("ATOMIZATION_HANDOFF_MISMATCH")

        raw_atoms = list(payload["atoms"])
        raw_atoms.sort(key=lambda item: int(item["ordinal"]))
        expected_ordinals = list(range(len(raw_atoms)))
        actual_ordinals = [int(item["ordinal"]) for item in raw_atoms]
        if actual_ordinals != expected_ordinals:
            return _failed("NON_CONTIGUOUS_ATOM_ORDINALS")

        atoms = tuple(
            ClaimAtomDraftItem(
                ordinal=int(item["ordinal"]),
                atom_type=ClaimAtomType(str(item["atom_type"])),
                atom_text=str(item["atom_text"]).strip(),
                initial_status=ClaimAtomStatus.UNSUPPORTED,
            )
            for item in raw_atoms
        )
        run_meta: dict[str, Any] = {
            "stage_code": LlmStageCode.STAGE_3B_CLAIM_ATOMIZATION.value,
            "output_schema_id": CLAIM_ATOMIZATION_SCHEMA_ID,
            "output_schema_version": SCHEMA_PACKAGE_VERSION,
            "generator_run_id": gen_run_id,
            "atom_count": len(atoms),
            "stage_envelope": envelope.to_audit_dict(),
        }
        assert_no_raw_text_or_secret_keys(run_meta, context="ClaimAtomizer.run_metadata")
        return ClaimAtomizationResult(
            atomizer_run_id=run_id,
            generator_run_id=gen_run_id,
            claim_id=request.claim_id,
            claim_version=request.claim_version,
            status=GenerationRunStatus.SUCCEEDED,
            error_code=None,
            atoms=atoms,
            input_hash=input_hash,
            atomizer_version=request.atomizer_version,
            prompt_version=request.prompt_version,
            model=request.model,
            input_artifact_versions=input_artifact_versions,
            run_metadata=run_meta,
        )


__all__ = [
    "CLAIM_ATOMIZATION_SCHEMA_ID",
    "ClaimAtomDraftItem",
    "ClaimAtomizationRequest",
    "ClaimAtomizationResult",
    "ClaimAtomizer",
    "DeterministicClaimAtomizerAdapter",
    "compute_atomization_input_hash",
    "deterministic_classify_atom_type",
    "deterministic_split_claim_atoms",
]
