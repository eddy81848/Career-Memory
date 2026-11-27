"""Stage-scoped AI JSON Schema registry, stdlib Draft 2020-12 validator, and compatibility classifier.

Implements T-106 structured I/O contracts for the 5-stage grounded Fact/Claim pipeline
plus the GenerationRun manifest:
- Stage 2A: ``fact_candidate.v1.schema.json`` (FactCandidateExtractor)
- Stage 2B: ``fact_support_validation.v1.schema.json`` (FactSupportValidator)
- Stage 3A: ``claim_draft.v1.schema.json`` (ClaimGenerator)
- Stage 3B: ``claim_atomization.v1.schema.json`` (ClaimAtomizer)
- Stage 3C: ``claim_support_validation.v1.schema.json`` (ClaimSupportValidator)
- Manifest: ``generation_run_manifest.v1.schema.json``

All validation runs strictly on the Python standard library (zero external jsonschema runtime dependency)
and enforces Plan C raw-text non-persistence, distinct stage ``run_id`` separation, read-only
``SOURCE_VERIFIED`` protection, provider-SDK response-shape rejection, CLAIM-05 role grounding,
and CLAIM-06 6-element metric grounding.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from career_memory_api.canonicalization.constants import LOCATOR_SPEC_VERSION
from career_memory_api.resolver.models import FORBIDDEN_RAW_PERSISTENCE_KEYS
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimVerification,
    ContributionRole,
    FactStatus,
    FactType,
    GenerationRunStatus,
    GenerationRunType,
    SupportType,
    SupportValidator,
)
from career_memory_api.schema_contracts.metadata_guard import (
    ALLOWED_GENERATION_RUN_INPUT_KEYS,
    FORBIDDEN_RAW_TEXT_KEYS,
)

JSON_SCHEMA_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_PACKAGE_VERSION = "1.0.0"
SCHEMA_BASE_URI = "https://career-memory.local/schemas/ai/v1"
GOLDEN_ALIGNMENT_REFERENCE = "0.4.0-synthetic-golden"

ALL_FORBIDDEN_RAW_KEYS: frozenset[str] = FORBIDDEN_RAW_TEXT_KEYS | FORBIDDEN_RAW_PERSISTENCE_KEYS

# Provider SDK envelope keys that must never leak into vendor-neutral stage artifacts (T-106).
FORBIDDEN_PROVIDER_SDK_KEYS: frozenset[str] = frozenset(
    {
        "choices",
        "candidates",
        "content_blocks",
        "tool_calls",
        "function_call",
        "openai_response",
        "anthropic_response",
        "gemini_response",
        "usage_metadata",
        "finish_reason",
        "stop_reason",
        "prompt_feedback",
        "safety_ratings",
    }
)

FORBIDDEN_DIRECT_VERIFICATION_KEYS: frozenset[str] = frozenset(
    {
        "verification",
        "verification_status",
        "claim_verification",
    }
)

_SHA256_PREFIXED_RE = r"^sha256:[0-9a-f]{64}$"
_SHA256_FLEX_RE = r"^(sha256:)?[0-9a-f]{64}$"


class SchemaCompatibilityClass(StrEnum):
    """Schema evolution compatibility classification (T-106)."""

    BACKWARD_COMPATIBLE = "BACKWARD_COMPATIBLE"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"


class FactPromotionDecision(StrEnum):
    """Stage 2B FactSupportValidator promotion outcome vocabulary."""

    PROMOTE_SUPPORTED = "PROMOTE_SUPPORTED"
    MARK_CONFLICTED = "MARK_CONFLICTED"
    REJECT_UNSUPPORTED = "REJECT_UNSUPPORTED"
    DEFER_REVIEW = "DEFER_REVIEW"


class MetricEnvironmentScope(StrEnum):
    """Structured metric environment scope aligned with CLAIM-06 and 0.4.0-synthetic-golden."""

    PRODUCTION = "PRODUCTION"
    STAGING_TEST = "STAGING_TEST"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True, slots=True)
class SchemaValidationViolation:
    """Single actionable validation failure for a stage AI payload."""

    path: str
    code: str
    message: str
    authority: str


class StageSchemaValidationError(ValueError):
    """Raised when a stage AI payload violates schema or domain invariants."""

    def __init__(
        self,
        stage_name: str,
        violations: list[SchemaValidationViolation],
    ) -> None:
        self.stage_name = stage_name
        self.violations = tuple(violations)
        summary = "; ".join(f"{v.path} [{v.code}]: {v.message}" for v in violations)
        super().__init__(f"Stage schema validation failed for {stage_name}: {summary}")


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    """Result of comparing two JSON Schema versions."""

    compatibility_class: SchemaCompatibilityClass
    breaking_reasons: tuple[str, ...]
    non_breaking_changes: tuple[str, ...]


def _cm_locator_v1_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "locator_spec_version",
            "canonicalization_version",
            "char_start",
            "char_end",
            "char_length",
            "utf8_byte_start",
            "utf8_byte_end",
            "utf8_byte_length",
            "utf16_start",
            "utf16_end",
            "line_start",
            "line_end",
            "span_sha256",
        ],
        "properties": {
            "locator_spec_version": {"type": "string", "const": LOCATOR_SPEC_VERSION},
            "canonicalization_version": {"type": "string", "minLength": 1},
            "char_start": {"type": "integer", "minimum": 0},
            "char_end": {"type": "integer", "minimum": 1},
            "char_length": {"type": "integer", "minimum": 1},
            "utf8_byte_start": {"type": "integer", "minimum": 0},
            "utf8_byte_end": {"type": "integer", "minimum": 1},
            "utf8_byte_length": {"type": "integer", "minimum": 1},
            "utf16_start": {"type": "integer", "minimum": 0},
            "utf16_end": {"type": "integer", "minimum": 1},
            "line_start": {"type": "integer", "minimum": 1},
            "line_end": {"type": "integer", "minimum": 1},
            "span_sha256": {"type": "string", "pattern": _SHA256_PREFIXED_RE},
        },
    }


def _metric_details_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "value",
            "unit",
            "period",
            "source_basis",
            "environment_scope",
            "measurement_scope",
        ],
        "properties": {
            "value": {"type": "string", "minLength": 1},
            "unit": {"type": "string", "minLength": 1},
            "period": {"type": "string", "minLength": 1},
            "source_basis": {"type": "string", "minLength": 1},
            "environment_scope": {
                "type": "string",
                "enum": [e.value for e in MetricEnvironmentScope],
            },
            "measurement_scope": {"type": "string", "minLength": 1},
        },
    }


def build_fact_candidate_schema() -> dict[str, Any]:
    """Stage 2A FactCandidateExtractor output schema (v1)."""
    return {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "$id": f"{SCHEMA_BASE_URI}/fact_candidate.v1.schema.json",
        "title": "FactCandidate Stage 2A Output (v1)",
        "description": (
            "Stage 2A FactCandidateExtractor structured output. Always starts in CANDIDATE "
            "status, carries cm-locator-v1 offsets/hashes, and strictly forbids raw source quotes "
            "or direct SUPPORTED/SOURCE_VERIFIED status assignment."
        ),
        "x-stage-code": "STAGE_2A_FACT_CANDIDATE_EXTRACTION",
        "x-pipeline-role": "FactCandidateExtractor",
        "x-schema-package-version": SCHEMA_PACKAGE_VERSION,
        "x-compatibility-class": SchemaCompatibilityClass.BACKWARD_COMPATIBLE.value,
        "x-golden-alignment": GOLDEN_ALIGNMENT_REFERENCE,
        "$defs": {
            "CmLocatorV1": _cm_locator_v1_def(),
            "MetricDetails": _metric_details_def(),
        },
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "extractor_run_id",
            "extractor_version",
            "source_object_revision_id",
            "content_unit_id",
            "locator",
            "fact_type",
            "predicate",
            "object_text",
            "normalized_value",
            "status",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": SCHEMA_PACKAGE_VERSION},
            "extractor_run_id": {"type": "string", "minLength": 1},
            "extractor_version": {"type": "string", "minLength": 1},
            "source_object_revision_id": {"type": "string", "minLength": 1},
            "content_unit_id": {"type": "string", "minLength": 1},
            "locator": {"$ref": "#/$defs/CmLocatorV1"},
            "fact_type": {"type": "string", "enum": [e.value for e in FactType]},
            "predicate": {"type": "string", "minLength": 1},
            "object_text": {"type": "string", "minLength": 1},
            "normalized_value": {"type": ["object", "null"]},
            "status": {"type": "string", "const": FactStatus.CANDIDATE.value},
            "subject_identity_id": {"type": ["string", "null"], "minLength": 1},
            "metric_details": {"$ref": "#/$defs/MetricDetails"},
            "uncertainty_notes": {"type": ["string", "null"]},
        },
    }


def build_fact_support_validation_schema() -> dict[str, Any]:
    """Stage 2B FactSupportValidator output schema (v1)."""
    return {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "$id": f"{SCHEMA_BASE_URI}/fact_support_validation.v1.schema.json",
        "title": "FactSupportValidation Stage 2B Output (v1)",
        "description": (
            "Stage 2B FactSupportValidator structured output. Requires a validator_run_id "
            "distinct from extractor_run_id, verifies cm-locator-v1 coordinates, and strictly "
            "forbids raw quote/extracted_span persistence."
        ),
        "x-stage-code": "STAGE_2B_FACT_SUPPORT_VALIDATION",
        "x-pipeline-role": "FactSupportValidator",
        "x-schema-package-version": SCHEMA_PACKAGE_VERSION,
        "x-compatibility-class": SchemaCompatibilityClass.BACKWARD_COMPATIBLE.value,
        "x-golden-alignment": GOLDEN_ALIGNMENT_REFERENCE,
        "$defs": {
            "CmLocatorV1": _cm_locator_v1_def(),
        },
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "validator_run_id",
            "extractor_run_id",
            "fact_candidate_id",
            "source_object_revision_id",
            "content_unit_id",
            "verified_locator",
            "support_type",
            "validator",
            "validator_version",
            "promotion_decision",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": SCHEMA_PACKAGE_VERSION},
            "validator_run_id": {"type": "string", "minLength": 1},
            "extractor_run_id": {"type": "string", "minLength": 1},
            "fact_candidate_id": {"type": "string", "minLength": 1},
            "source_object_revision_id": {"type": "string", "minLength": 1},
            "content_unit_id": {"type": "string", "minLength": 1},
            "verified_locator": {"$ref": "#/$defs/CmLocatorV1"},
            "support_type": {"type": "string", "enum": [e.value for e in SupportType]},
            "validator": {"type": "string", "enum": [e.value for e in SupportValidator]},
            "validator_version": {"type": "string", "minLength": 1},
            "promotion_decision": {
                "type": "string",
                "enum": [e.value for e in FactPromotionDecision],
            },
            "revision_and_locator_verified": {"type": "boolean"},
            "promoted_fact_id": {"type": ["string", "null"], "minLength": 1},
            "rejection_reason": {"type": ["string", "null"]},
        },
    }


def build_claim_draft_schema() -> dict[str, Any]:
    """Stage 3A ClaimGenerator output schema (v1)."""
    return {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "$id": f"{SCHEMA_BASE_URI}/claim_draft.v1.schema.json",
        "title": "ClaimDraft Stage 3A Output (v1)",
        "description": (
            "Stage 3A ClaimGenerator structured output. Scoped to one confirmed_event_id and "
            "non-empty same-Event input_fact_ids, fixed to initial status DRAFT, and strictly "
            "prohibiting verification or SOURCE_VERIFIED assignment."
        ),
        "x-stage-code": "STAGE_3A_CLAIM_GENERATION",
        "x-pipeline-role": "ClaimGenerator",
        "x-schema-package-version": SCHEMA_PACKAGE_VERSION,
        "x-compatibility-class": SchemaCompatibilityClass.BACKWARD_COMPATIBLE.value,
        "x-golden-alignment": GOLDEN_ALIGNMENT_REFERENCE,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "generator_run_id",
            "confirmed_event_id",
            "input_fact_ids",
            "claim_version",
            "claim_text",
            "status",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": SCHEMA_PACKAGE_VERSION},
            "generator_run_id": {"type": "string", "minLength": 1},
            "confirmed_event_id": {"type": "string", "minLength": 1},
            "input_fact_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "claim_version": {"type": "integer", "minimum": 1},
            "claim_text": {"type": "string", "minLength": 1},
            "status": {"type": "string", "const": "DRAFT"},
            "previous_claim_version": {"type": ["integer", "null"], "minimum": 1},
            "supersedes_claim_id": {"type": ["string", "null"], "minLength": 1},
        },
    }


def build_claim_atomization_schema() -> dict[str, Any]:
    """Stage 3B ClaimAtomizer output schema (v1)."""
    return {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "$id": f"{SCHEMA_BASE_URI}/claim_atomization.v1.schema.json",
        "title": "ClaimAtomization Stage 3B Output (v1)",
        "description": (
            "Stage 3B ClaimAtomizer structured output. Requires atomizer_run_id distinct from "
            "generator_run_id and decomposes a Claim version into ordered typed atoms."
        ),
        "x-stage-code": "STAGE_3B_CLAIM_ATOMIZATION",
        "x-pipeline-role": "ClaimAtomizer",
        "x-schema-package-version": SCHEMA_PACKAGE_VERSION,
        "x-compatibility-class": SchemaCompatibilityClass.BACKWARD_COMPATIBLE.value,
        "x-golden-alignment": GOLDEN_ALIGNMENT_REFERENCE,
        "$defs": {
            "ClaimAtomDraft": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ordinal", "atom_type", "atom_text"],
                "properties": {
                    "ordinal": {"type": "integer", "minimum": 0},
                    "atom_type": {
                        "type": "string",
                        "enum": [e.value for e in ClaimAtomType],
                    },
                    "atom_text": {"type": "string", "minLength": 1},
                },
            }
        },
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "atomizer_run_id",
            "generator_run_id",
            "claim_id",
            "claim_version",
            "atoms",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": SCHEMA_PACKAGE_VERSION},
            "atomizer_run_id": {"type": "string", "minLength": 1},
            "generator_run_id": {"type": "string", "minLength": 1},
            "claim_id": {"type": "string", "minLength": 1},
            "claim_version": {"type": "integer", "minimum": 1},
            "atoms": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/ClaimAtomDraft"},
            },
        },
    }


def build_claim_support_validation_schema() -> dict[str, Any]:
    """Stage 3C ClaimSupportValidator output schema (v1)."""
    return {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "$id": f"{SCHEMA_BASE_URI}/claim_support_validation.v1.schema.json",
        "title": "ClaimSupportValidation Stage 3C Output (v1)",
        "description": (
            "Stage 3C ClaimSupportValidator structured output. Requires validation_run_id "
            "distinct from both generator_run_id and atomizer_run_id, validates ROLE contribution "
            "grounding (CLAIM-05) and 6-element METRIC grounding (CLAIM-06), and emits strictly "
            "read-only computed_verification."
        ),
        "x-stage-code": "STAGE_3C_CLAIM_SUPPORT_VALIDATION",
        "x-pipeline-role": "ClaimSupportValidator",
        "x-schema-package-version": SCHEMA_PACKAGE_VERSION,
        "x-compatibility-class": SchemaCompatibilityClass.BACKWARD_COMPATIBLE.value,
        "x-golden-alignment": GOLDEN_ALIGNMENT_REFERENCE,
        "$defs": {
            "MetricDetails": _metric_details_def(),
            "AtomFactBinding": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "fact_id",
                    "support_type",
                    "fact_support_ids",
                    "source_object_revision_id",
                    "content_unit_id",
                ],
                "properties": {
                    "fact_id": {"type": "string", "minLength": 1},
                    "support_type": {
                        "type": "string",
                        "enum": [e.value for e in SupportType],
                    },
                    "fact_support_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "source_object_revision_id": {"type": "string", "minLength": 1},
                    "content_unit_id": {"type": "string", "minLength": 1},
                },
            },
            "RoleContributionGrounding": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "subject_identity_id",
                    "confirmed_event_id",
                    "role",
                    "active_contribution_support_fact_ids",
                ],
                "properties": {
                    "subject_identity_id": {"type": "string", "minLength": 1},
                    "confirmed_event_id": {"type": "string", "minLength": 1},
                    "role": {
                        "type": "string",
                        "enum": [e.value for e in ContributionRole],
                    },
                    "active_contribution_support_fact_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
            "AtomValidationResult": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "ordinal",
                    "atom_type",
                    "status",
                    "fact_bindings",
                ],
                "properties": {
                    "ordinal": {"type": "integer", "minimum": 0},
                    "atom_type": {
                        "type": "string",
                        "enum": [e.value for e in ClaimAtomType],
                    },
                    "status": {
                        "type": "string",
                        "enum": [e.value for e in ClaimAtomStatus],
                    },
                    "fact_bindings": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/AtomFactBinding"},
                    },
                    "role_contribution_grounding": {
                        "$ref": "#/$defs/RoleContributionGrounding"
                    },
                    "metric_grounding": {"$ref": "#/$defs/MetricDetails"},
                    "unsupported_reason": {"type": ["string", "null"]},
                },
            },
        },
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "validation_run_id",
            "generator_run_id",
            "atomizer_run_id",
            "claim_id",
            "claim_version",
            "atom_validations",
            "computed_verification",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": SCHEMA_PACKAGE_VERSION},
            "validation_run_id": {"type": "string", "minLength": 1},
            "generator_run_id": {"type": "string", "minLength": 1},
            "atomizer_run_id": {"type": "string", "minLength": 1},
            "claim_id": {"type": "string", "minLength": 1},
            "claim_version": {"type": "integer", "minimum": 1},
            "atom_validations": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/AtomValidationResult"},
            },
            "computed_verification": {
                "type": "string",
                "readOnly": True,
                "enum": [e.value for e in ClaimVerification],
            },
        },
    }


def build_generation_run_manifest_schema() -> dict[str, Any]:
    """GenerationRun manifest schema (v1) aligned with migration 0030 generation_runs."""
    input_ref_properties: dict[str, Any] = {}
    for key in sorted(ALLOWED_GENERATION_RUN_INPUT_KEYS):
        if key.endswith("_ids") or key.endswith("_hashes"):
            input_ref_properties[key] = {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            }
        elif key == "claim_version":
            input_ref_properties[key] = {"type": "integer", "minimum": 1}
        else:
            input_ref_properties[key] = {"type": "string", "minLength": 1}

    return {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "$id": f"{SCHEMA_BASE_URI}/generation_run_manifest.v1.schema.json",
        "title": "GenerationRun Manifest (v1)",
        "description": (
            "Non-raw provenance manifest for every AI pipeline execution run. "
            "Permits strictly ID/version/hash artifact references in input_artifact_versions "
            "and forbids raw source text or prompt bodies (Plan C / REF-04)."
        ),
        "x-stage-code": "GENERATION_RUN_MANIFEST",
        "x-pipeline-role": "GenerationRunManifest",
        "x-schema-package-version": SCHEMA_PACKAGE_VERSION,
        "x-compatibility-class": SchemaCompatibilityClass.BACKWARD_COMPATIBLE.value,
        "x-golden-alignment": GOLDEN_ALIGNMENT_REFERENCE,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "run_id",
            "workspace_id",
            "run_type",
            "input_artifact_versions",
            "input_hash",
            "output_schema_id",
            "output_schema_version",
            "compatibility_class",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": SCHEMA_PACKAGE_VERSION},
            "run_id": {"type": "string", "minLength": 1},
            "workspace_id": {"type": "string", "format": "uuid"},
            "event_id": {"type": ["string", "null"], "minLength": 1},
            "run_type": {
                "type": "string",
                "enum": [e.value for e in GenerationRunType],
            },
            "input_artifact_versions": {
                "type": "object",
                "additionalProperties": False,
                "properties": input_ref_properties,
            },
            "input_hash": {"type": "string", "pattern": _SHA256_FLEX_RE},
            "output_schema_id": {"type": "string", "minLength": 1},
            "output_schema_version": {"type": "string", "minLength": 1},
            "compatibility_class": {
                "type": "string",
                "enum": [e.value for e in SchemaCompatibilityClass],
            },
            "model": {"type": "string", "minLength": 1},
            "prompt_version": {"type": "string", "minLength": 1},
            "algorithm_version": {"type": "string", "minLength": 1},
            "status": {
                "type": "string",
                "enum": [e.value for e in GenerationRunStatus],
            },
        },
    }


AI_SCHEMA_BUILDERS: dict[str, Any] = {
    "fact_candidate.v1.schema.json": build_fact_candidate_schema,
    "fact_support_validation.v1.schema.json": build_fact_support_validation_schema,
    "claim_draft.v1.schema.json": build_claim_draft_schema,
    "claim_atomization.v1.schema.json": build_claim_atomization_schema,
    "claim_support_validation.v1.schema.json": build_claim_support_validation_schema,
    "generation_run_manifest.v1.schema.json": build_generation_run_manifest_schema,
}

STAGE_KEY_TO_FILENAME: dict[str, str] = {
    "fact_candidate": "fact_candidate.v1.schema.json",
    "fact_support_validation": "fact_support_validation.v1.schema.json",
    "claim_draft": "claim_draft.v1.schema.json",
    "claim_atomization": "claim_atomization.v1.schema.json",
    "claim_support_validation": "claim_support_validation.v1.schema.json",
    "generation_run_manifest": "generation_run_manifest.v1.schema.json",
}


def render_canonical_json(obj: Any) -> str:
    """Serialize JSON deterministically with sorted keys and trailing newline."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generate_all_ai_schemas() -> dict[str, dict[str, Any]]:
    """Generate all 6 Draft 2020-12 AI JSON Schemas deterministically."""
    return {
        filename: builder()
        for filename, builder in sorted(AI_SCHEMA_BUILDERS.items())
    }


def generate_ai_schema_manifest() -> dict[str, Any]:
    """Generate ``contracts/ai-schemas/v1/MANIFEST.json`` with SHA-256 digests."""
    schemas = generate_all_ai_schemas()
    entries: list[dict[str, Any]] = []
    for filename, schema in schemas.items():
        rendered = render_canonical_json(schema).encode("utf-8")
        entries.append(
            {
                "compatibility_class": schema["x-compatibility-class"],
                "file": filename,
                "pipeline_role": schema["x-pipeline-role"],
                "schema_id": schema["$id"],
                "sha256": f"sha256:{hashlib.sha256(rendered).hexdigest()}",
                "stage_code": schema["x-stage-code"],
                "version": schema["x-schema-package-version"],
            }
        )
    return {
        "golden_alignment_reference": GOLDEN_ALIGNMENT_REFERENCE,
        "json_schema_draft": JSON_SCHEMA_DRAFT_2020_12,
        "schema_package_version": SCHEMA_PACKAGE_VERSION,
        "schemas": entries,
    }


def generate_ai_schema_files() -> dict[str, str]:
    """Return mapping of ``filename -> canonical JSON string`` including MANIFEST.json."""
    files: dict[str, str] = {}
    for filename, schema in generate_all_ai_schemas().items():
        files[filename] = render_canonical_json(schema)
    files["MANIFEST.json"] = render_canonical_json(generate_ai_schema_manifest())
    return files


# =============================================================================
# Stdlib Deterministic JSON Schema & Domain Invariant Validator
# =============================================================================
def _check_json_type(value: Any, expected_type: str | list[str]) -> bool:
    types = [expected_type] if isinstance(expected_type, str) else expected_type
    for t in types:
        if t == "null" and value is None:
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "object" and isinstance(value, dict):
            return True
    return False


def _resolve_ref(root_schema: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"Unsupported external $ref in stdlib validator: {ref!r}")
    node: Any = root_schema
    for part in ref[2:].split("/"):
        node = node[part]
    if not isinstance(node, Mapping):
        raise ValueError(f"Resolved $ref {ref!r} is not a mapping")
    return node


def _validate_format(value: str, fmt: str) -> bool:
    if fmt == "uuid":
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            return False
        return str(parsed) == value.lower()
    if fmt == "date":
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    if fmt == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return True


def _validate_node(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    path: str,
    violations: list[SchemaValidationViolation],
) -> None:
    if "$ref" in schema:
        target = _resolve_ref(root_schema, str(schema["$ref"]))
        _validate_node(value, target, root_schema, path, violations)
        return

    if "type" in schema and not _check_json_type(value, schema["type"]):
        violations.append(
            SchemaValidationViolation(
                path=path,
                code="TYPE_MISMATCH",
                message=f"Expected type {schema['type']!r}, got {type(value).__name__}.",
                authority="Notion T-106",
            )
        )
        return

    if "const" in schema and value != schema["const"]:
        violations.append(
            SchemaValidationViolation(
                path=path,
                code="CONST_MISMATCH",
                message=f"Expected constant value {schema['const']!r}, got {value!r}.",
                authority="Notion T-106",
            )
        )

    if "enum" in schema and value not in schema["enum"]:
        violations.append(
            SchemaValidationViolation(
                path=path,
                code="ENUM_MISMATCH",
                message=f"Value {value!r} not in allowed enum {schema['enum']!r}.",
                authority="Notion Data Model / T-106",
            )
        )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            violations.append(
                SchemaValidationViolation(
                    path=path,
                    code="MIN_LENGTH",
                    message=f"String length {len(value)} < minLength {schema['minLength']}.",
                    authority="Notion T-106",
                )
            )
        if "pattern" in schema and re.search(str(schema["pattern"]), value) is None:
            violations.append(
                SchemaValidationViolation(
                    path=path,
                    code="PATTERN_MISMATCH",
                    message=f"Value {value!r} does not match pattern {schema['pattern']!r}.",
                    authority="Notion T-106",
                )
            )
        if "format" in schema and not _validate_format(value, str(schema["format"])):
            violations.append(
                SchemaValidationViolation(
                    path=path,
                    code="FORMAT_MISMATCH",
                    message=f"Value {value!r} does not satisfy format {schema['format']!r}.",
                    authority="Notion T-106",
                )
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            violations.append(
                SchemaValidationViolation(
                    path=path,
                    code="MINIMUM",
                    message=f"Value {value} < minimum {schema['minimum']}.",
                    authority="Notion T-106",
                )
            )

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            violations.append(
                SchemaValidationViolation(
                    path=path,
                    code="MIN_ITEMS",
                    message=f"Array length {len(value)} < minItems {schema['minItems']}.",
                    authority="Notion T-106",
                )
            )
        if schema.get("uniqueItems"):
            seen_serialized: set[str] = set()
            for idx, item in enumerate(value):
                marker = json.dumps(item, sort_keys=True, separators=(",", ":"))
                if marker in seen_serialized:
                    violations.append(
                        SchemaValidationViolation(
                            path=f"{path}[{idx}]",
                            code="UNIQUE_ITEMS",
                            message="Duplicate item found in uniqueItems array.",
                            authority="Notion T-106",
                        )
                    )
                seen_serialized.add(marker)
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for idx, item in enumerate(value):
                _validate_node(item, item_schema, root_schema, f"{path}[{idx}]", violations)

    if isinstance(value, dict):
        required = schema.get("required", [])
        for req_key in required:
            if req_key not in value:
                violations.append(
                    SchemaValidationViolation(
                        path=f"{path}.{req_key}",
                        code="MISSING_REQUIRED_FIELD",
                        message=f"Missing required property {req_key!r}.",
                        authority="Notion T-106",
                    )
                )
        props = schema.get("properties", {})
        additional_allowed = schema.get("additionalProperties", True)
        for key, item_val in value.items():
            child_path = f"{path}.{key}"
            if key in props:
                _validate_node(item_val, props[key], root_schema, child_path, violations)
            elif additional_allowed is False:
                violations.append(
                    SchemaValidationViolation(
                        path=child_path,
                        code="ADDITIONAL_PROPERTY_FORBIDDEN",
                        message=f"Unexpected property {key!r} is forbidden (additionalProperties: false).",
                        authority="Notion T-106",
                    )
                )


def _scan_security_and_plan_c_invariants(
    payload: Any,
    *,
    stage_key: str,
    path: str = "$",
    violations: list[SchemaValidationViolation],
) -> None:
    if isinstance(payload, Mapping):
        for raw_key, val in payload.items():
            key_str = str(raw_key)
            key_lower = key_str.strip().lower()
            child_path = f"{path}.{key_str}"

            if key_lower in ALL_FORBIDDEN_RAW_KEYS:
                violations.append(
                    SchemaValidationViolation(
                        path=child_path,
                        code="PROHIBITED_RAW_TEXT_KEY",
                        message=(
                            f"Plan C (REF-04) forbids raw source text / quote key {key_str!r} "
                            "in structured AI artifacts."
                        ),
                        authority="Notion 방안 C §7 / Data Model §3.4",
                    )
                )

            if key_lower in FORBIDDEN_PROVIDER_SDK_KEYS:
                violations.append(
                    SchemaValidationViolation(
                        path=child_path,
                        code="PROHIBITED_PROVIDER_SDK_SHAPE",
                        message=(
                            f"Provider SDK wrapper key {key_str!r} must not leak into "
                            "vendor-neutral AI stage artifacts."
                        ),
                        authority="Notion T-106",
                    )
                )

            if key_lower in FORBIDDEN_DIRECT_VERIFICATION_KEYS:
                violations.append(
                    SchemaValidationViolation(
                        path=child_path,
                        code="PROHIBITED_DIRECT_VERIFICATION_WRITE",
                        message=(
                            f"Writable verification field {key_str!r} is forbidden in stage {stage_key}."
                        ),
                        authority="Notion Data Model §3.9 / T-106",
                    )
                )

            if stage_key != "claim_support_validation" and key_lower == "computed_verification":
                violations.append(
                    SchemaValidationViolation(
                        path=child_path,
                        code="PROHIBITED_DIRECT_VERIFICATION_WRITE",
                        message="computed_verification is only permitted on Stage 3C validator output.",
                        authority="Notion T-106",
                    )
                )

            _scan_security_and_plan_c_invariants(
                val, stage_key=stage_key, path=child_path, violations=violations
            )
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            _scan_security_and_plan_c_invariants(
                item, stage_key=stage_key, path=f"{path}[{idx}]", violations=violations
            )
    elif isinstance(payload, str):
        if (
            stage_key in {"fact_candidate", "fact_support_validation", "claim_draft", "claim_atomization"}
            and payload.strip().upper() == ClaimVerification.SOURCE_VERIFIED.value
        ):
            violations.append(
                SchemaValidationViolation(
                    path=path,
                    code="PROHIBITED_SOURCE_VERIFIED_WRITE",
                    message=(
                        f"Direct assignment of SOURCE_VERIFIED is strictly forbidden in stage {stage_key}."
                    ),
                    authority="Notion Data Model §3.9 / T-106",
                )
            )


def _validate_locator_math(
    locator: Any, path: str, violations: list[SchemaValidationViolation]
) -> None:
    if not isinstance(locator, Mapping):
        return
    try:
        cs = int(locator["char_start"])
        ce = int(locator["char_end"])
        cl = int(locator["char_length"])
        bs = int(locator["utf8_byte_start"])
        be = int(locator["utf8_byte_end"])
        bl = int(locator["utf8_byte_length"])
        u16s = int(locator["utf16_start"])
        u16e = int(locator["utf16_end"])
        ls = int(locator["line_start"])
        le = int(locator["line_end"])
    except (KeyError, TypeError, ValueError):
        return
    if ce <= cs or (ce - cs) != cl:
        violations.append(
            SchemaValidationViolation(
                path=path,
                code="INVALID_LOCATOR_OFFSETS",
                message="Locator char_end must exceed char_start and equal char_start + char_length.",
                authority="Notion T-103 / 방안 C",
            )
        )
    if be <= bs or (be - bs) != bl:
        violations.append(
            SchemaValidationViolation(
                path=path,
                code="INVALID_LOCATOR_OFFSETS",
                message="Locator utf8_byte_end must exceed utf8_byte_start and match utf8_byte_length.",
                authority="Notion T-103 / 방안 C",
            )
        )
    if u16e <= u16s or le < ls:
        violations.append(
            SchemaValidationViolation(
                path=path,
                code="INVALID_LOCATOR_OFFSETS",
                message="Locator utf16 and line ranges must be non-empty and monotonic.",
                authority="Notion T-103 / 방안 C",
            )
        )


def compute_expected_claim_verification(
    atom_statuses: list[str],
) -> ClaimVerification:
    """Compute read-only ClaimVerification from atom statuses (mirrors 0030 view)."""
    if not atom_statuses:
        return ClaimVerification.UNVERIFIED
    if any(
        s not in {ClaimAtomStatus.SUPPORTED.value, ClaimAtomStatus.USER_CONFIRMED.value}
        for s in atom_statuses
    ):
        return ClaimVerification.UNVERIFIED
    if all(s == ClaimAtomStatus.SUPPORTED.value for s in atom_statuses):
        return ClaimVerification.SOURCE_VERIFIED
    if all(s == ClaimAtomStatus.USER_CONFIRMED.value for s in atom_statuses):
        return ClaimVerification.USER_CONFIRMED
    return ClaimVerification.MIXED


def _validate_stage_domain_rules(
    stage_key: str,
    payload: Mapping[str, Any],
    violations: list[SchemaValidationViolation],
) -> None:
    if stage_key == "fact_candidate":
        _validate_locator_math(payload.get("locator"), "$.locator", violations)

    elif stage_key == "fact_support_validation":
        _validate_locator_math(payload.get("verified_locator"), "$.verified_locator", violations)
        ext_id = payload.get("extractor_run_id")
        val_id = payload.get("validator_run_id")
        if isinstance(ext_id, str) and isinstance(val_id, str) and ext_id == val_id:
            violations.append(
                SchemaValidationViolation(
                    path="$.validator_run_id",
                    code="STAGE_RUN_ID_COLLISION",
                    message=(
                        "Stage 2B validator_run_id must be distinct from Stage 2A extractor_run_id."
                    ),
                    authority="Notion T-106 / ADR-018",
                )
            )

    elif stage_key == "claim_atomization":
        gen_id = payload.get("generator_run_id")
        atom_id = payload.get("atomizer_run_id")
        if isinstance(gen_id, str) and isinstance(atom_id, str) and gen_id == atom_id:
            violations.append(
                SchemaValidationViolation(
                    path="$.atomizer_run_id",
                    code="STAGE_RUN_ID_COLLISION",
                    message=(
                        "Stage 3B atomizer_run_id must be distinct from Stage 3A generator_run_id."
                    ),
                    authority="Notion T-106",
                )
            )
        atoms = payload.get("atoms")
        if isinstance(atoms, list):
            ordinals = [a.get("ordinal") for a in atoms if isinstance(a, Mapping)]
            if len(ordinals) != len(set(ordinals)):
                violations.append(
                    SchemaValidationViolation(
                        path="$.atoms",
                        code="DUPLICATE_ATOM_ORDINAL",
                        message="Claim atom ordinals must be unique within a claim version.",
                        authority="Notion Data Model §3.9",
                    )
                )

    elif stage_key == "claim_support_validation":
        gen_id = payload.get("generator_run_id")
        atom_id = payload.get("atomizer_run_id")
        val_id = payload.get("validation_run_id")
        ids = [x for x in (gen_id, atom_id, val_id) if isinstance(x, str)]
        if len(ids) == 3 and len(set(ids)) < 3:
            violations.append(
                SchemaValidationViolation(
                    path="$.validation_run_id",
                    code="STAGE_RUN_ID_COLLISION",
                    message=(
                        "Stage 3A generator_run_id, Stage 3B atomizer_run_id, and Stage 3C "
                        "validation_run_id must be pairwise distinct."
                    ),
                    authority="Notion T-106",
                )
            )

        atom_vals = payload.get("atom_validations")
        if isinstance(atom_vals, list):
            statuses: list[str] = []
            for idx, item in enumerate(atom_vals):
                if not isinstance(item, Mapping):
                    continue
                status = item.get("status")
                atom_type = item.get("atom_type")
                bindings = item.get("fact_bindings")
                if isinstance(status, str):
                    statuses.append(status)
                if status == ClaimAtomStatus.SUPPORTED.value:
                    has_direct = isinstance(bindings, list) and any(
                        isinstance(b, Mapping) and b.get("support_type") == SupportType.DIRECT.value
                        for b in bindings
                    )
                    has_contra = isinstance(bindings, list) and any(
                        isinstance(b, Mapping)
                        and b.get("support_type") == SupportType.CONTRADICTS.value
                        for b in bindings
                    )
                    if not has_direct or has_contra:
                        violations.append(
                            SchemaValidationViolation(
                                path=f"$.atom_validations[{idx}].fact_bindings",
                                code="SUPPORTED_ATOM_MISSING_DIRECT_BINDING",
                                message=(
                                    "SUPPORTED atom requires at least one DIRECT fact binding "
                                    "and zero CONTRADICTS bindings."
                                ),
                                authority="Notion Data Model §3.9",
                            )
                        )
                    if atom_type == ClaimAtomType.ROLE.value and "role_contribution_grounding" not in item:
                        violations.append(
                            SchemaValidationViolation(
                                path=f"$.atom_validations[{idx}].role_contribution_grounding",
                                code="ROLE_GROUNDING_REQUIRED",
                                message=(
                                    "SUPPORTED ROLE atom requires active role_contribution_grounding "
                                    "(CLAIM-05)."
                                ),
                                authority="Notion Data Model §3.8 / CLAIM-05",
                            )
                        )
                    if atom_type == ClaimAtomType.METRIC.value and "metric_grounding" not in item:
                        violations.append(
                            SchemaValidationViolation(
                                path=f"$.atom_validations[{idx}].metric_grounding",
                                code="METRIC_GROUNDING_REQUIRED",
                                message=(
                                    "SUPPORTED METRIC atom requires 6-element metric_grounding "
                                    "(CLAIM-06)."
                                ),
                                authority="Notion Data Model §3.7 / CLAIM-06",
                            )
                        )

            expected_verif = compute_expected_claim_verification(statuses).value
            actual_verif = payload.get("computed_verification")
            if isinstance(actual_verif, str) and actual_verif != expected_verif:
                violations.append(
                    SchemaValidationViolation(
                        path="$.computed_verification",
                        code="COMPUTED_VERIFICATION_MISMATCH",
                        message=(
                            f"computed_verification={actual_verif!r} contradicts atom statuses; "
                            f"expected {expected_verif!r}."
                        ),
                        authority="Notion Data Model §3.9",
                    )
                )


def validate_stage_payload(
    stage_key: str,
    payload: Any,
    *,
    raise_on_error: bool = True,
) -> list[SchemaValidationViolation]:
    """Validate an AI pipeline stage payload against its Draft 2020-12 schema and invariants."""
    filename = STAGE_KEY_TO_FILENAME.get(stage_key, stage_key)
    if filename not in AI_SCHEMA_BUILDERS:
        raise KeyError(f"Unknown AI stage schema: {stage_key!r}")
    normalized_stage = next(
        k for k, v in STAGE_KEY_TO_FILENAME.items() if v == filename
    )
    schema = AI_SCHEMA_BUILDERS[filename]()
    violations: list[SchemaValidationViolation] = []

    _scan_security_and_plan_c_invariants(
        payload, stage_key=normalized_stage, violations=violations
    )
    _validate_node(payload, schema, schema, "$", violations)
    if isinstance(payload, Mapping):
        _validate_stage_domain_rules(normalized_stage, payload, violations)

    if violations and raise_on_error:
        raise StageSchemaValidationError(normalized_stage, violations)
    return violations


def validate_producer_consumer_handoff(
    *,
    producer_stage: str,
    producer_payload: Mapping[str, Any],
    consumer_stage: str,
    consumer_payload: Mapping[str, Any],
) -> None:
    """Validate producer -> consumer contract compatibility across adjacent pipeline stages."""
    validate_stage_payload(producer_stage, producer_payload, raise_on_error=True)
    validate_stage_payload(consumer_stage, consumer_payload, raise_on_error=True)

    violations: list[SchemaValidationViolation] = []
    pair = (producer_stage, consumer_stage)
    if pair == ("fact_candidate", "fact_support_validation"):
        if producer_payload["extractor_run_id"] != consumer_payload["extractor_run_id"]:
            violations.append(
                SchemaValidationViolation(
                    path="$.extractor_run_id",
                    code="HANDOFF_EXTRACTOR_RUN_MISMATCH",
                    message="Stage 2B consumer must reference Stage 2A extractor_run_id.",
                    authority="Notion T-106",
                )
            )
        if producer_payload["content_unit_id"] != consumer_payload["content_unit_id"]:
            violations.append(
                SchemaValidationViolation(
                    path="$.content_unit_id",
                    code="HANDOFF_UNIT_MISMATCH",
                    message="Stage 2B consumer content_unit_id must match Stage 2A producer.",
                    authority="Notion T-106",
                )
            )
        if (
            producer_payload["source_object_revision_id"]
            != consumer_payload["source_object_revision_id"]
        ):
            violations.append(
                SchemaValidationViolation(
                    path="$.source_object_revision_id",
                    code="HANDOFF_REVISION_MISMATCH",
                    message="Stage 2B consumer source_object_revision_id must match Stage 2A producer.",
                    authority="Notion T-106",
                )
            )
    elif pair == ("claim_draft", "claim_atomization"):
        if producer_payload["generator_run_id"] != consumer_payload["generator_run_id"]:
            violations.append(
                SchemaValidationViolation(
                    path="$.generator_run_id",
                    code="HANDOFF_GENERATOR_RUN_MISMATCH",
                    message="Stage 3B consumer must reference Stage 3A generator_run_id.",
                    authority="Notion T-106",
                )
            )
        if producer_payload["claim_version"] != consumer_payload["claim_version"]:
            violations.append(
                SchemaValidationViolation(
                    path="$.claim_version",
                    code="HANDOFF_CLAIM_VERSION_MISMATCH",
                    message="Stage 3B consumer claim_version must match Stage 3A producer.",
                    authority="Notion T-106",
                )
            )
    elif pair == ("claim_atomization", "claim_support_validation"):
        for field_name in ("generator_run_id", "atomizer_run_id", "claim_id", "claim_version"):
            if producer_payload[field_name] != consumer_payload[field_name]:
                violations.append(
                    SchemaValidationViolation(
                        path=f"$.{field_name}",
                        code="HANDOFF_FIELD_MISMATCH",
                        message=f"Stage 3C {field_name} must match Stage 3B {field_name}.",
                        authority="Notion T-106",
                    )
                )
        prod_ordinals = [a["ordinal"] for a in producer_payload["atoms"]]
        cons_ordinals = [a["ordinal"] for a in consumer_payload["atom_validations"]]
        if sorted(prod_ordinals) != sorted(cons_ordinals):
            violations.append(
                SchemaValidationViolation(
                    path="$.atom_validations",
                    code="HANDOFF_ATOM_SET_MISMATCH",
                    message=(
                        "Stage 3C atom_validations must cover the exact set of Stage 3B atom ordinals."
                    ),
                    authority="Notion T-106",
                )
            )
    else:
        raise ValueError(f"Unsupported producer->consumer stage pair: {pair!r}")

    if violations:
        raise StageSchemaValidationError(f"{producer_stage}->{consumer_stage}", violations)


def classify_schema_compatibility(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
) -> CompatibilityAssessment:
    """Classify schema evolution as BACKWARD_COMPATIBLE or MIGRATION_REQUIRED (T-106)."""
    breaking: list[str] = []
    non_breaking: list[str] = []

    old_props = old_schema.get("properties", {})
    new_props = new_schema.get("properties", {})
    old_required = set(old_schema.get("required", ()))
    new_required = set(new_schema.get("required", ()))

    added_required = sorted(new_required - old_required)
    if added_required:
        breaking.append(f"Added required field(s): {', '.join(added_required)}")

    removed_props = sorted(set(old_props.keys()) - set(new_props.keys()))
    if removed_props:
        breaking.append(f"Removed property(ies): {', '.join(removed_props)}")

    added_optional = sorted((set(new_props.keys()) - set(old_props.keys())) - new_required)
    if added_optional:
        non_breaking.append(f"Added optional property(ies): {', '.join(added_optional)}")

    for prop_name in sorted(set(old_props.keys()) & set(new_props.keys())):
        old_p = old_props[prop_name]
        new_p = new_props[prop_name]
        if old_p.get("type") != new_p.get("type"):
            breaking.append(
                f"Property {prop_name!r} changed type from {old_p.get('type')!r} to {new_p.get('type')!r}"
            )
        if old_p.get("const") != new_p.get("const"):
            breaking.append(
                f"Property {prop_name!r} changed const from {old_p.get('const')!r} to {new_p.get('const')!r}"
            )
        if "enum" in old_p and "enum" in new_p:
            removed_enum = sorted(set(old_p["enum"]) - set(new_p["enum"]))
            added_enum = sorted(set(new_p["enum"]) - set(old_p["enum"]))
            if removed_enum:
                breaking.append(
                    f"Property {prop_name!r} removed enum value(s): {', '.join(map(str, removed_enum))}"
                )
            if added_enum:
                non_breaking.append(
                    f"Property {prop_name!r} added enum value(s): {', '.join(map(str, added_enum))}"
                )

    compat = (
        SchemaCompatibilityClass.MIGRATION_REQUIRED
        if breaking
        else SchemaCompatibilityClass.BACKWARD_COMPATIBLE
    )
    return CompatibilityAssessment(
        compatibility_class=compat,
        breaking_reasons=tuple(breaking),
        non_breaking_changes=tuple(non_breaking),
    )


__all__ = [
    "AI_SCHEMA_BUILDERS",
    "ALL_FORBIDDEN_RAW_KEYS",
    "CompatibilityAssessment",
    "FORBIDDEN_DIRECT_VERIFICATION_KEYS",
    "FORBIDDEN_PROVIDER_SDK_KEYS",
    "FactPromotionDecision",
    "GOLDEN_ALIGNMENT_REFERENCE",
    "JSON_SCHEMA_DRAFT_2020_12",
    "MetricEnvironmentScope",
    "SCHEMA_BASE_URI",
    "SCHEMA_PACKAGE_VERSION",
    "STAGE_KEY_TO_FILENAME",
    "SchemaCompatibilityClass",
    "SchemaValidationViolation",
    "StageSchemaValidationError",
    "build_claim_atomization_schema",
    "build_claim_draft_schema",
    "build_claim_support_validation_schema",
    "build_fact_candidate_schema",
    "build_fact_support_validation_schema",
    "build_generation_run_manifest_schema",
    "classify_schema_compatibility",
    "compute_expected_claim_verification",
    "generate_ai_schema_files",
    "generate_ai_schema_manifest",
    "generate_all_ai_schemas",
    "render_canonical_json",
    "validate_producer_consumer_handoff",
    "validate_stage_payload",
]
