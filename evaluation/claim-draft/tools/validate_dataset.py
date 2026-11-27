"""Structural, schema, locator/hash round-trip, stage-separation, role-run-ID disjointness,
official domain-vs-fixture label separation, REF-01..08 synthetic-vs-runtime honesty,
Option C inter-stage raw-text leak, family-partition anti-leakage, and ADR-018
owner-review candidate manifest validator (v0.4.0-owner-review-candidate).

Note:
v0.4.0-owner-review-candidate is strictly PENDING_OWNER_REVIEW (human_approved=false,
frozen_for_golden=false). Per ADR-018, AI cannot record human approval.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locator_hash import (
    check_url_safety,
    compute_locator_id,
    compute_version_id,
    normalize_text,
    sha256_prefixed,
    short_hash,
    verify_locator_roundtrip,
)

DATASET_FILES = {
    "sources/source_revisions.json": "SourceRevisionsFile",
    "sources/refetch_snapshots.json": "RefetchSnapshotsFile",
    "stages/stage1_content_units.json": "Stage1ContentUnitsFile",
    "stages/stage2_fact_extraction.json": "Stage2FactExtractionFile",
    "stages/stage3_claim_assembly.json": "Stage3ClaimAssemblyFile",
    "stages/stage4_refetch_checks.json": "Stage4RefetchChecksFile",
    "expected/oracle_draft.json": "OracleDraftFile",
    "expected/refetch_oracle_draft.json": "RefetchOracleDraftFile",
    "CANDIDATE_MANIFEST.json": "CandidateManifestFile",
}

PENDING_ORACLE_STATUS = "PENDING_OWNER_REVIEW"
CANDIDATE_SCOPE = "OWNER_REVIEW_CANDIDATE_SYNTHETIC_ONLY_NOT_APPROVED"
PRIOR_BASELINE_BUNDLE_SHA256 = "sha256:881b6bb182b65c51c88a09ecf605322167ff439a4fffc37c1d77f07f1ad6b51f"
VALID_EVALUATION_PARTITIONS = ("SYNTHETIC_REGRESSION_DEV", "SYNTHETIC_REGRESSION_TEST")
OFFICIAL_FACT_TYPES = ("PROBLEM", "ACTION", "TECHNOLOGY", "RESULT", "ROLE", "METRIC")
OFFICIAL_ATOM_STATUSES = ("SUPPORTED", "USER_CONFIRMED", "UNSUPPORTED", "STALE")

PRIOR_V030_FAMILY_PARTITIONS = {
    "family-src01": "SYNTHETIC_REGRESSION_DEV",
    "family-src02": "SYNTHETIC_REGRESSION_TEST",
    "family-src03": "SYNTHETIC_REGRESSION_DEV",
    "family-src04": "SYNTHETIC_REGRESSION_DEV",
    "family-src05": "SYNTHETIC_REGRESSION_TEST",
    "family-src06": "SYNTHETIC_REGRESSION_DEV",
    "family-src07": "SYNTHETIC_REGRESSION_TEST",
    "family-src08": "SYNTHETIC_REGRESSION_DEV",
    "family-src09": "SYNTHETIC_REGRESSION_TEST",
    "family-src10": "SYNTHETIC_REGRESSION_DEV",
    "family-src11": "SYNTHETIC_REGRESSION_TEST",
    "family-src12": "SYNTHETIC_REGRESSION_DEV",
    "family-src13": "SYNTHETIC_REGRESSION_TEST",
    "family-src14": "SYNTHETIC_REGRESSION_TEST",
    "family-src_ref07_canary": "SYNTHETIC_REGRESSION_DEV",
}

FORBIDDEN_INTER_STAGE_RAW_KEYS = frozenset(
    {
        "raw_text",
        "raw_body",
        "raw_text_fixture_only",
        "returned_body_fixture_only",
        "raw_excerpt",
        "source_excerpt",
        "raw_quote",
        "source_body",
        "full_document_text",
    }
)

FORBIDDEN_STATUS_VERSION_IDENTIFIERS = frozenset(
    {
        "SUPPORTED",
        "CONFIRMED",
        "DRAFT",
        "UNSUPPORTED",
        "STALE",
        "USER_CONFIRMED",
        "PENDING_OWNER_REVIEW",
        "CONFLICTING_REVIEW_REQUIRED",
        "IDENTITY_UNRESOLVED_REVIEW_REQUIRED",
        "DELETED_REVIEW_REQUIRED",
        "SOURCE_VERIFIED",
    }
)

OFFICIAL_REF_REQUIREMENTS = {
    "REF-01": "동일 버전과 정확한 근거 위치 재현",
    "REF-02": "hash/locator 불일치 차단",
    "REF-03": "재조회 불가와 삭제·거짓 구분",
    "REF-04": "원문 비영속",
    "REF-05": "접근 권한·허용된 URL 경계",
    "REF-06": "중단·재실행 시 원문 저장에 의존하지 않음",
    "REF-07": "승인·Export·근거 열람 시 재조회",
    "REF-08": "비용·크기 한도와 단계 간 메모리 전용 처리",
}


@dataclass(frozen=True)
class ValidationErrorItem:
    code: str
    location: str
    message: str

    def format(self) -> str:
        return f"[{self.code}] {self.location}: {self.message}"


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def validate_against_schema(
    instance: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
    errors: list[ValidationErrorItem],
) -> None:
    """Pure-stdlib JSON Schema Draft 2020-12 subset validator."""
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            errors.append(ValidationErrorItem("SCHEMA_VALIDATION_ERROR", path, f"Unsupported $ref: {ref}"))
            return
        def_name = ref.split("/", 2)[-1]
        target = root_schema.get("$defs", {}).get(def_name)
        if target is None:
            errors.append(ValidationErrorItem("SCHEMA_VALIDATION_ERROR", path, f"Missing $def: {def_name}"))
            return
        validate_against_schema(instance, target, root_schema, path, errors)
        return

    if "type" in schema:
        allowed_types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_matches_type(instance, t) for t in allowed_types):
            errors.append(
                ValidationErrorItem(
                    "SCHEMA_VALIDATION_ERROR",
                    path,
                    f"Expected type {allowed_types}, got {type(instance).__name__}",
                )
            )
            return

    if "const" in schema and instance != schema["const"]:
        errors.append(
            ValidationErrorItem(
                "SCHEMA_VALIDATION_ERROR",
                path,
                f"Expected const {schema['const']!r}, got {instance!r}",
            )
        )

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(
            ValidationErrorItem(
                "SCHEMA_VALIDATION_ERROR",
                path,
                f"Value {instance!r} not in enum {schema['enum']!r}",
            )
        )

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(
                ValidationErrorItem(
                    "SCHEMA_VALIDATION_ERROR",
                    path,
                    f"String length {len(instance)} < minLength {schema['minLength']}",
                )
            )
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(
                ValidationErrorItem(
                    "SCHEMA_VALIDATION_ERROR",
                    path,
                    f"String {instance!r} does not match pattern {schema['pattern']!r}",
                )
            )

    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(
                ValidationErrorItem(
                    "SCHEMA_VALIDATION_ERROR",
                    path,
                    f"Integer {instance} < minimum {schema['minimum']}",
                )
            )

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(
                    ValidationErrorItem("SCHEMA_VALIDATION_ERROR", path, f"Missing required property '{req}'")
                )
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errors.append(
                        ValidationErrorItem(
                            "SCHEMA_VALIDATION_ERROR",
                            f"{path}.{key}",
                            f"Unexpected additional property '{key}'",
                        )
                    )
        for key, prop_schema in props.items():
            if key in instance:
                validate_against_schema(instance[key], prop_schema, root_schema, f"{path}.{key}", errors)

    if isinstance(instance, list) and "items" in schema:
        for idx, item in enumerate(instance):
            validate_against_schema(item, schema["items"], root_schema, f"{path}[{idx}]", errors)


def load_dataset_bundle(base_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load the JSON schema and all 9 dataset JSON documents into memory."""
    schema_path = base_dir / "schemas" / "claim_pipeline.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    docs: dict[str, dict[str, Any]] = {}
    for rel_path in DATASET_FILES:
        docs[rel_path] = json.loads((base_dir / rel_path).read_text(encoding="utf-8"))
    return schema, docs


def detect_canary_in_obj(obj: Any, canary_tokens: list[str]) -> list[str]:
    """Return list of canary tokens found anywhere inside a JSON-serializable object."""
    if not canary_tokens:
        return []
    serialized = json.dumps(obj, ensure_ascii=False)
    return [tok for tok in canary_tokens if tok in serialized]


def find_forbidden_raw_keys(obj: Any, path: str = "", *, skip_keys: set[str] | None = None) -> list[str]:
    """Recursively locate any forbidden inter-stage raw-text keys."""
    hits: list[str] = []
    skips = skip_keys or set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in skips:
                continue
            cur = f"{path}.{k}" if path else k
            if k in FORBIDDEN_INTER_STAGE_RAW_KEYS:
                hits.append(cur)
            hits.extend(find_forbidden_raw_keys(v, cur, skip_keys=skips))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            hits.extend(find_forbidden_raw_keys(item, f"{path}[{idx}]", skip_keys=skips))
    return hits


def strip_negative_leak_fixtures(obj: Any) -> Any:
    """Return a copy of obj with deliberate negative_leaky_payload_fixture fields removed for clean leak scanning."""
    if isinstance(obj, dict):
        return {k: strip_negative_leak_fixtures(v) for k, v in obj.items() if k != "negative_leaky_payload_fixture"}
    if isinstance(obj, list):
        return [strip_negative_leak_fixtures(item) for item in obj]
    return obj


def check_no_stale_approval_manifest(base_dir: Path) -> list[ValidationErrorItem]:
    """Ensure no stale APPROVAL_MANIFEST.json sits in the working tree alongside a PENDING_OWNER_REVIEW candidate."""
    stale = base_dir / "APPROVAL_MANIFEST.json"
    if stale.exists():
        return [
            ValidationErrorItem(
                "UNAPPROVED_VERSION_MARKED_APPROVED",
                "APPROVAL_MANIFEST.json",
                "Stale APPROVAL_MANIFEST.json must NOT coexist with PENDING_OWNER_REVIEW candidate (prior v0.3.0 baseline stays in Git)",
            )
        ]
    return []


def validate_bundle(
    schema: dict[str, Any],
    docs: dict[str, dict[str, Any]],
) -> tuple[list[ValidationErrorItem], dict[str, Any]]:
    """Run all schema, locator/hash, stage-separation, domain-label, REF-01..08, Option C, partition, and ADR-018 checks."""
    errors: list[ValidationErrorItem] = []

    # 1. JSON Schema validation on every file
    for rel_path, def_name in DATASET_FILES.items():
        if rel_path not in docs:
            errors.append(ValidationErrorItem("MISSING_FILE", rel_path, "Dataset file missing"))
            continue
        root_def = schema["$defs"][def_name]
        validate_against_schema(docs[rel_path], root_def, schema, rel_path, errors)

    src_doc = docs["sources/source_revisions.json"]
    ref_snap_doc = docs["sources/refetch_snapshots.json"]
    s1_doc = docs["stages/stage1_content_units.json"]
    s2_doc = docs["stages/stage2_fact_extraction.json"]
    s3_doc = docs["stages/stage3_claim_assembly.json"]
    s4_doc = docs["stages/stage4_refetch_checks.json"]
    oracle_doc = docs["expected/oracle_draft.json"]
    ref_oracle_doc = docs["expected/refetch_oracle_draft.json"]
    manifest_doc = docs["CANDIDATE_MANIFEST.json"]

    # 2. Index and validate Source Revisions
    sources_by_id: dict[str, dict[str, Any]] = {}
    norm_text_by_rev: dict[str, str] = {}
    for idx, src in enumerate(src_doc.get("source_revisions", [])):
        loc = f"sources/source_revisions.json[{idx}]"
        rev_id = src.get("source_revision_id", "")
        if rev_id in sources_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc, f"Duplicate source_revision_id '{rev_id}'"))
        sources_by_id[rev_id] = src

        raw = src.get("raw_text_fixture_only", "")
        normalized = normalize_text(raw)
        if normalized != raw:
            errors.append(ValidationErrorItem("NORMALIZATION_MISMATCH", loc, "raw_text_fixture_only is not normalized"))
        norm_text_by_rev[rev_id] = normalized

        expected_hash = sha256_prefixed(normalized)
        if src.get("content_sha256") != expected_hash:
            errors.append(
                ValidationErrorItem(
                    "HASH_MISMATCH",
                    f"{loc}.content_sha256",
                    f"Recorded {src.get('content_sha256')} != computed {expected_hash}",
                )
            )

        expected_ver = compute_version_id(src.get("source_key", ""), src.get("revision_seq", 0), normalized)
        if rev_id != expected_ver:
            errors.append(
                ValidationErrorItem(
                    "VERSION_ID_MISMATCH",
                    f"{loc}.source_revision_id",
                    f"Recorded '{rev_id}' != computed '{expected_ver}'",
                )
            )

        if src.get("char_length") != len(normalized):
            errors.append(
                ValidationErrorItem(
                    "OFFSET_OR_ROUNDTRIP_MISMATCH",
                    f"{loc}.char_length",
                    f"char_length {src.get('char_length')} != {len(normalized)}",
                )
            )
        if src.get("utf8_byte_length") != len(normalized.encode("utf-8")):
            errors.append(
                ValidationErrorItem(
                    "BYTE_OFFSET_MISMATCH",
                    f"{loc}.utf8_byte_length",
                    f"utf8_byte_length {src.get('utf8_byte_length')} != {len(normalized.encode('utf-8'))}",
                )
            )
        safe_url, reason = check_url_safety(src.get("synthetic_uri", ""))
        if not safe_url:
            errors.append(ValidationErrorItem("UNSAFE_URL_POLICY_VIOLATION", f"{loc}.synthetic_uri", reason))

    # Pre-index Claim / Atom IDs for anti-self-evidence checks
    claim_ids_set = {c.get("claim_id") for c in s3_doc.get("claims", []) if isinstance(c.get("claim_id"), str)}
    atom_ids_set = {a.get("claim_atom_id") for a in s3_doc.get("claim_atoms", []) if isinstance(a.get("claim_atom_id"), str)}
    generated_claim_namespace = claim_ids_set | atom_ids_set

    # Global per-role execution run registry (enforces ROLE_RUN_ID_REUSE_VIOLATION across all 5 roles!)
    all_runs_by_id: dict[str, dict[str, Any]] = {}

    def _register_run(run: dict[str, Any], expected_role: str | None, loc_path: str) -> None:
        rid = run.get("run_id", "")
        role = run.get("pipeline_role", "")
        if expected_role and role != expected_role:
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    loc_path,
                    f"Expected pipeline_role={expected_role!r}, got {role!r} on run '{rid}'",
                )
            )
        if rid in all_runs_by_id:
            prev_role = all_runs_by_id[rid].get("pipeline_role")
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    loc_path,
                    f"Duplicate/reused run_id '{rid}' (already registered for role {prev_role!r}; reused by {role!r})",
                )
            )
        else:
            all_runs_by_id[rid] = run
        for r_idx, ref in enumerate(run.get("input_version_refs", [])):
            vh = ref.get("version_or_hash", "")
            if vh in FORBIDDEN_STATUS_VERSION_IDENTIFIERS:
                errors.append(
                    ValidationErrorItem(
                        "STATUS_USED_AS_VERSION_IDENTIFIER",
                        f"{loc_path}.input_version_refs[{r_idx}].version_or_hash",
                        f"Status string {vh!r} is NOT a valid version/hash identifier on run '{rid}' ({ref.get('entity_type')}:{ref.get('entity_id')})",
                    )
                )

    # 3. Validate Stage 1: Content Units (round-trip locator + hash)
    units_by_id: dict[str, dict[str, Any]] = {}
    locator_checks_run = 0
    for idx, cu in enumerate(s1_doc.get("content_units", [])):
        loc_path = f"stages/stage1_content_units.json[{idx}]"
        cu_id = cu.get("content_unit_id", "")
        if cu_id in units_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc_path, f"Duplicate content_unit_id '{cu_id}'"))
        units_by_id[cu_id] = cu

        rev_id = cu.get("source_revision_id", "")
        if rev_id in generated_claim_namespace:
            errors.append(
                ValidationErrorItem(
                    "SELF_EVIDENCE_VIOLATION",
                    f"{loc_path}.source_revision_id",
                    f"ContentUnit cannot use generated claim/atom '{rev_id}' as Source Revision",
                )
            )
        if rev_id not in sources_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.source_revision_id", f"Unknown '{rev_id}'"))
            continue

        locator = cu.get("locator", {})
        locator_checks_run += 1
        extracted, rt_errors = verify_locator_roundtrip(norm_text_by_rev[rev_id], locator, context_label=loc_path)
        for code, msg in rt_errors:
            errors.append(ValidationErrorItem(code, f"{loc_path}.locator", msg))

        if extracted is not None:
            if cu.get("unit_sha256") != locator.get("span_sha256"):
                errors.append(
                    ValidationErrorItem(
                        "SPAN_HASH_MISMATCH",
                        f"{loc_path}.unit_sha256",
                        "unit_sha256 does not match locator.span_sha256",
                    )
                )
            expected_loc_id = compute_locator_id(
                cu_id, locator["char_start"], locator["char_end"], locator["span_sha256"]
            )
            if locator.get("locator_id") != expected_loc_id:
                errors.append(
                    ValidationErrorItem(
                        "LOCATOR_ID_MISMATCH",
                        f"{loc_path}.locator.locator_id",
                        f"Recorded '{locator.get('locator_id')}' != computed '{expected_loc_id}'",
                    )
                )

    # 4. Validate Stage 2: Separated FactCandidateExtractor (2A), FactSupportValidator (2B), & CanonicalFact Promotion
    extractor_runs_by_id: dict[str, dict[str, Any]] = {}
    for idx, run in enumerate(s2_doc.get("extractor_runs", [])):
        loc_path = f"stages/stage2_fact_extraction.json.extractor_runs[{idx}]"
        _register_run(run, "FactCandidateExtractor", loc_path)
        extractor_runs_by_id[run.get("run_id", "")] = run

    validator_runs_by_id: dict[str, dict[str, Any]] = {}
    for idx, run in enumerate(s2_doc.get("validator_runs", [])):
        loc_path = f"stages/stage2_fact_extraction.json.validator_runs[{idx}]"
        _register_run(run, "FactSupportValidator", loc_path)
        validator_runs_by_id[run.get("run_id", "")] = run

    candidates_by_id: dict[str, dict[str, Any]] = {}
    actual_candidate_counts: dict[str, int] = {}
    for idx, fc in enumerate(s2_doc.get("fact_candidates", [])):
        loc_path = f"stages/stage2_fact_extraction.json.fact_candidates[{idx}]"
        cid = fc.get("fact_candidate_id", "")
        if cid in candidates_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc_path, f"Duplicate fact_candidate_id '{cid}'"))
        candidates_by_id[cid] = fc
        sid = fc.get("scenario_id", "")
        actual_candidate_counts[sid] = actual_candidate_counts.get(sid, 0) + 1

        if fc.get("candidate_status") != "EXTRACTED_UNVALIDATED":
            errors.append(
                ValidationErrorItem(
                    "EXTRACTION_CANNOT_SELF_SUPPORT",
                    f"{loc_path}.candidate_status",
                    f"FactCandidate extraction alone cannot mark status {fc.get('candidate_status')!r}; must be 'EXTRACTED_UNVALIDATED'",
                )
            )
        ext_rid = fc.get("extractor_run_id", "")
        if ext_rid not in extractor_runs_by_id:
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    f"{loc_path}.extractor_run_id",
                    f"Unknown or non-extractor run_id '{ext_rid}' on FactCandidate '{cid}'",
                )
            )

        rev_id = fc.get("source_revision_id", "")
        cu_id = fc.get("content_unit_id", "")
        if rev_id not in sources_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.source_revision_id", f"Unknown '{rev_id}'"))
            continue
        if cu_id not in units_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.content_unit_id", f"Unknown '{cu_id}'"))
            continue
        cu = units_by_id[cu_id]
        if cu.get("source_revision_id") != rev_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", loc_path, f"Unit '{cu_id}' belongs to '{cu.get('source_revision_id')}', not '{rev_id}'"))
        loc_obj = fc.get("locator", {})
        locator_checks_run += 1
        extracted, rt_errors = verify_locator_roundtrip(norm_text_by_rev[rev_id], loc_obj, context_label=loc_path)
        for code, msg in rt_errors:
            errors.append(ValidationErrorItem(code, f"{loc_path}.locator", msg))
        if extracted is not None:
            cu_loc = cu["locator"]
            if loc_obj["char_start"] < cu_loc["char_start"] or loc_obj["char_end"] > cu_loc["char_end"]:
                errors.append(ValidationErrorItem("SPAN_CONTAINMENT_ERROR", f"{loc_path}.locator", f"Candidate span outside ContentUnit '{cu_id}'"))

    validations_by_cand: dict[str, list[dict[str, Any]]] = {cid: [] for cid in candidates_by_id}
    rejected_candidates: set[str] = set()
    for idx, fval in enumerate(s2_doc.get("fact_support_validations", [])):
        loc_path = f"stages/stage2_fact_extraction.json.fact_support_validations[{idx}]"
        cand_id = fval.get("fact_candidate_id", "")
        vrun_id = fval.get("validator_run_id", "")
        if cand_id not in candidates_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.fact_candidate_id", f"Unknown '{cand_id}'"))
        else:
            validations_by_cand[cand_id].append(fval)
            if vrun_id == candidates_by_id[cand_id].get("extractor_run_id"):
                errors.append(
                    ValidationErrorItem(
                        "ROLE_RUN_ID_REUSE_VIOLATION",
                        f"{loc_path}.validator_run_id",
                        f"FactSupportValidator run_id '{vrun_id}' MUST NOT reuse FactCandidateExtractor run_id",
                    )
                )
        if vrun_id not in validator_runs_by_id:
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    f"{loc_path}.validator_run_id",
                    f"validator_run_id '{vrun_id}' must reference a FactSupportValidator run",
                )
            )
        if fval.get("promotion_decision") == "REJECT_DO_NOT_PROMOTE":
            rejected_candidates.add(cand_id)
            if fval.get("promoted_canonical_fact_id") is not None or not fval.get("rejection_reason"):
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_PROMOTION_OF_REJECTED_CANDIDATE",
                        loc_path,
                        "REJECT_DO_NOT_PROMOTE validation must have promoted_canonical_fact_id=null and non-empty rejection_reason",
                    )
                )
        rev_id = fval.get("source_revision_id", "")
        cu_id = fval.get("content_unit_id", "")
        if rev_id in sources_by_id and cu_id in units_by_id:
            locator_checks_run += 1
            extracted, rt_errors = verify_locator_roundtrip(
                norm_text_by_rev[rev_id], fval.get("verified_locator", {}), context_label=loc_path
            )
            for code, msg in rt_errors:
                errors.append(ValidationErrorItem(code, f"{loc_path}.verified_locator", msg))
            if extracted is not None:
                cu_loc = units_by_id[cu_id]["locator"]
                vloc = fval["verified_locator"]
                if vloc["char_start"] < cu_loc["char_start"] or vloc["char_end"] > cu_loc["char_end"]:
                    errors.append(ValidationErrorItem("SPAN_CONTAINMENT_ERROR", f"{loc_path}.verified_locator", f"Validation span outside ContentUnit '{cu_id}'"))

    facts_by_id: dict[str, dict[str, Any]] = {}
    actual_fact_counts: dict[str, int] = {}
    fact_types_seen: set[str] = set()
    for idx, fact in enumerate(s2_doc.get("canonical_facts", [])):
        loc_path = f"stages/stage2_fact_extraction.json.canonical_facts[{idx}]"
        fid = fact.get("canonical_fact_id", "")
        if fid in facts_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc_path, f"Duplicate canonical_fact_id '{fid}'"))
        facts_by_id[fid] = fact
        sid = fact.get("scenario_id", "")
        actual_fact_counts[sid] = actual_fact_counts.get(sid, 0) + 1
        fact_types_seen.add(str(fact.get("fact_type", "")))

        src_cand = fact.get("source_candidate_id", "")
        if src_cand not in candidates_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.source_candidate_id", f"Unknown '{src_cand}'"))
        elif src_cand in rejected_candidates:
            errors.append(
                ValidationErrorItem(
                    "ILLEGAL_PROMOTION_OF_REJECTED_CANDIDATE",
                    loc_path,
                    f"CanonicalFact '{fid}' cannot be promoted from rejected candidate '{src_cand}'",
                )
            )
        vrun_id = fact.get("validator_run_id", "")
        if vrun_id not in validator_runs_by_id:
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    f"{loc_path}.validator_run_id",
                    f"CanonicalFact '{fid}' validator_run_id '{vrun_id}' must reference a FactSupportValidator run",
                )
            )
        expected_fact_hash = sha256_prefixed(f"{fid}|{fact.get('fact_type', '')}|{fact.get('fact_text', '')}")
        recorded_fact_hash = fact.get("fixture_classification", {}).get("fixture_fact_sha256")
        if recorded_fact_hash != expected_fact_hash:
            errors.append(
                ValidationErrorItem(
                    "HASH_MISMATCH",
                    f"{loc_path}.fixture_classification.fixture_fact_sha256",
                    f"CanonicalFact '{fid}' recorded fixture_fact_sha256 {recorded_fact_hash!r} != computed {expected_fact_hash!r}",
                )
            )

    fact_supports_by_fact: dict[str, list[dict[str, Any]]] = {fid: [] for fid in facts_by_id}
    fact_supports_by_id: dict[str, dict[str, Any]] = {}
    fact_source_revs_by_fact: dict[str, set[str]] = {fid: set() for fid in facts_by_id}
    support_roles_seen: set[str] = set()
    for idx, fs in enumerate(s2_doc.get("fact_supports", [])):
        loc_path = f"stages/stage2_fact_extraction.json.fact_supports[{idx}]"
        fs_id = fs.get("fact_support_id", "")
        if fs_id:
            fact_supports_by_id[fs_id] = fs
        fid = fs.get("canonical_fact_id", "")
        rev_id = fs.get("source_revision_id", "")
        cu_id = fs.get("content_unit_id", "")
        vrun_id = fs.get("validator_run_id", "")
        support_roles_seen.add(fs.get("support_role", ""))

        if rev_id in generated_claim_namespace or cu_id in generated_claim_namespace:
            errors.append(
                ValidationErrorItem(
                    "SELF_EVIDENCE_VIOLATION",
                    loc_path,
                    f"FactSupport references generated claim/atom as evidence (rev='{rev_id}', cu='{cu_id}')",
                )
            )
            continue
        if vrun_id not in validator_runs_by_id:
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    f"{loc_path}.validator_run_id",
                    f"FactSupport '{fs.get('fact_support_id')}' must reference a FactSupportValidator run, got '{vrun_id}'",
                )
            )
        if fid not in facts_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.canonical_fact_id", f"Unknown '{fid}'"))
        else:
            fact_supports_by_fact[fid].append(fs)
            if rev_id:
                fact_source_revs_by_fact[fid].add(rev_id)
        if rev_id not in sources_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.source_revision_id", f"Unknown '{rev_id}'"))
            continue
        if cu_id not in units_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.content_unit_id", f"Unknown '{cu_id}'"))
            continue

        cu = units_by_id[cu_id]
        if cu.get("source_revision_id") != rev_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", loc_path, f"ContentUnit '{cu_id}' belongs to '{cu.get('source_revision_id')}', not '{rev_id}'"))

        locator = fs.get("locator", {})
        locator_checks_run += 1
        extracted, rt_errors = verify_locator_roundtrip(norm_text_by_rev[rev_id], locator, context_label=loc_path)
        for code, msg in rt_errors:
            errors.append(ValidationErrorItem(code, f"{loc_path}.locator", msg))

        if extracted is not None:
            cu_loc = cu["locator"]
            if locator["char_start"] < cu_loc["char_start"] or locator["char_end"] > cu_loc["char_end"]:
                errors.append(ValidationErrorItem("SPAN_CONTAINMENT_ERROR", f"{loc_path}.locator", f"FactSupport span outside ContentUnit '{cu_id}'"))
            expected_loc_id = compute_locator_id(
                fs["fact_support_id"], locator["char_start"], locator["char_end"], locator["span_sha256"]
            )
            if locator.get("locator_id") != expected_loc_id:
                errors.append(ValidationErrorItem("LOCATOR_ID_MISMATCH", f"{loc_path}.locator.locator_id", f"Recorded '{locator.get('locator_id')}' != computed '{expected_loc_id}'"))

    # Guard: active SUPPORTED CanonicalFact MUST actually have valid non-deleted DIRECT support and 0 CONTRADICTS
    for fid, fact in facts_by_id.items():
        if fact.get("is_active_supported_fact") or fact.get("fact_support_status") == "SUPPORTED":
            f_sups = fact_supports_by_fact.get(fid, [])
            has_active_direct = any(
                fs.get("support_role") == "DIRECT"
                and sources_by_id.get(fs.get("source_revision_id", ""), {}).get("deleted_by_user") is False
                for fs in f_sups
            )
            has_contra = any(fs.get("support_role") == "CONTRADICTS" for fs in f_sups)
            if (
                not has_active_direct
                or has_contra
                or fact.get("identity_status") != "RESOLVED"
                or fact.get("deleted_by_user") is True
                or fact.get("fact_support_status") != "SUPPORTED"
                or fact.get("is_active_supported_fact") is not True
            ):
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SUPPORTED_FACT",
                        f"stages/stage2_fact_extraction.json.canonical_facts[{fid}]",
                        f"Fact '{fid}' marked SUPPORTED/active without clean non-deleted DIRECT support, resolved identity, and 0 CONTRADICTS",
                    )
                )

    for sid, recorded_count in s2_doc.get("scenario_candidate_counts", {}).items():
        if recorded_count != actual_candidate_counts.get(sid, 0):
            errors.append(
                ValidationErrorItem(
                    "FACT_COUNT_MISMATCH",
                    f"stages/stage2_fact_extraction.json.scenario_candidate_counts.{sid}",
                    f"Recorded {recorded_count} != actual {actual_candidate_counts.get(sid, 0)}",
                )
            )
    for sid, recorded_count in s2_doc.get("scenario_fact_counts", {}).items():
        if recorded_count != actual_fact_counts.get(sid, 0):
            errors.append(
                ValidationErrorItem(
                    "FACT_COUNT_MISMATCH",
                    f"stages/stage2_fact_extraction.json.scenario_fact_counts.{sid}",
                    f"Recorded {recorded_count} != actual {actual_fact_counts.get(sid, 0)}",
                )
            )

    count_values = set(s2_doc.get("scenario_fact_counts", {}).values())
    if 0 not in count_values or 1 not in count_values or not any(v >= 3 for v in count_values):
        errors.append(ValidationErrorItem("COVERAGE_INSUFFICIENT", "stages/stage2_fact_extraction.json", f"Must cover 0, 1, and N(>=3) facts; observed={sorted(count_values)}"))
    for role in ("DIRECT", "CONTEXT", "CONTRADICTS"):
        if role not in support_roles_seen:
            errors.append(ValidationErrorItem("COVERAGE_INSUFFICIENT", "stages/stage2_fact_extraction.json.fact_supports", f"Missing FactSupport role '{role}'"))
    missing_fact_types = sorted(set(OFFICIAL_FACT_TYPES) - fact_types_seen)
    if missing_fact_types:
        errors.append(ValidationErrorItem("COVERAGE_INSUFFICIENT", "stages/stage2_fact_extraction.json.canonical_facts", f"Missing official Fact types: {missing_fact_types}"))

    # 5. Validate Stage 3: Confirmed Events, Role Execution Runs (3A/3B/3C), Claims, Atoms, Supports
    events_by_id: dict[str, dict[str, Any]] = {}
    for idx, evt in enumerate(s3_doc.get("confirmed_events", [])):
        loc_path = f"stages/stage3_claim_assembly.json.confirmed_events[{idx}]"
        eid = evt.get("event_id", "")
        events_by_id[eid] = evt
        if evt.get("event_status") != "CONFIRMED":
            errors.append(ValidationErrorItem("EVENT_NOT_CONFIRMED", loc_path, f"Event '{eid}' must be CONFIRMED"))
        for rev_id in evt.get("source_revision_ids", []):
            if rev_id not in sources_by_id:
                errors.append(ValidationErrorItem("BROKEN_REFERENCE", loc_path, f"Unknown source_revision_id '{rev_id}'"))
        expected_evt_hash = sha256_prefixed(
            f"{eid}|{evt.get('title', '')}|" + ",".join(sorted(evt.get("source_revision_ids", [])))
        )
        if evt.get("fixture_event_sha256") != expected_evt_hash:
            errors.append(
                ValidationErrorItem(
                    "HASH_MISMATCH",
                    f"{loc_path}.fixture_event_sha256",
                    f"ConfirmedEvent '{eid}' recorded fixture_event_sha256 {evt.get('fixture_event_sha256')!r} != computed {expected_evt_hash!r}",
                )
            )

    def _fact_belongs_to_event(fid: str, event_id: str) -> bool:
        evt_obj = events_by_id.get(event_id)
        if not evt_obj or fid not in facts_by_id:
            return False
        evt_revs = set(evt_obj.get("source_revision_ids", []))
        f_revs = fact_source_revs_by_fact.get(fid, set())
        return bool(f_revs) and f_revs.issubset(evt_revs)

    stage3_runs_by_id: dict[str, dict[str, Any]] = {}
    for idx, run in enumerate(s3_doc.get("role_execution_runs", [])):
        loc_path = f"stages/stage3_claim_assembly.json.role_execution_runs[{idx}]"
        _register_run(run, None, loc_path)
        stage3_runs_by_id[run.get("run_id", "")] = run
        role = run.get("pipeline_role")
        if role == "ClaimGenerator":
            if run.get("output_initial_status") != "DRAFT":
                errors.append(ValidationErrorItem("ILLEGAL_SOURCE_VERIFIED", loc_path, "ClaimGenerator output_initial_status MUST be 'DRAFT'"))
        elif role == "ClaimSupportValidator":
            if run.get("uses_claim_draft_or_ai_explanation_as_evidence") is not False:
                errors.append(
                    ValidationErrorItem(
                        "SELF_EVIDENCE_VIOLATION",
                        loc_path,
                        "ClaimSupportValidator MUST set uses_claim_draft_or_ai_explanation_as_evidence=false",
                    )
                )
            for ref in run.get("input_version_refs", []):
                if ref.get("entity_type") == "CLAIM_VERSION" or ref.get("entity_id", "").startswith("run-"):
                    errors.append(
                        ValidationErrorItem(
                            "SELF_EVIDENCE_VIOLATION",
                            loc_path,
                            f"ClaimSupportValidator cannot use Claim draft or AI run output ({ref}) as validation input",
                        )
                    )

    claims_by_id: dict[str, dict[str, Any]] = {}
    for idx, claim in enumerate(s3_doc.get("claims", [])):
        loc_path = f"stages/stage3_claim_assembly.json.claims[{idx}]"
        cid = claim.get("claim_id", "")
        if cid in claims_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc_path, f"Duplicate claim_id '{cid}'"))
        claims_by_id[cid] = claim

        if claim.get("confirmed_event_id") not in events_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.confirmed_event_id", f"Unknown event '{claim.get('confirmed_event_id')}'"))

        gen_rid = claim.get("generator_run_id", "")
        atom_rid = claim.get("atomizer_run_id", "")
        val_rid = claim.get("validator_run_id")

        if stage3_runs_by_id.get(gen_rid, {}).get("pipeline_role") != "ClaimGenerator":
            errors.append(ValidationErrorItem("ROLE_RUN_ID_REUSE_VIOLATION", f"{loc_path}.generator_run_id", f"'{gen_rid}' is not a ClaimGenerator run"))
        if stage3_runs_by_id.get(atom_rid, {}).get("pipeline_role") != "ClaimAtomizer":
            errors.append(ValidationErrorItem("ROLE_RUN_ID_REUSE_VIOLATION", f"{loc_path}.atomizer_run_id", f"'{atom_rid}' is not a ClaimAtomizer run"))
        if val_rid is not None and stage3_runs_by_id.get(val_rid, {}).get("pipeline_role") != "ClaimSupportValidator":
            errors.append(ValidationErrorItem("ROLE_RUN_ID_REUSE_VIOLATION", f"{loc_path}.validator_run_id", f"'{val_rid}' is not a ClaimSupportValidator run"))

        present_rids = [r for r in (gen_rid, atom_rid, val_rid) if r is not None]
        if len(set(present_rids)) != len(present_rids):
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    loc_path,
                    f"Claim '{cid}' reuses run_id across generator/atomizer/validator roles: {present_rids}",
                )
            )

        if claim.get("generator_initial_status") != "DRAFT" or claim.get("user_can_directly_set_source_verified") is not False:
            errors.append(ValidationErrorItem("ILLEGAL_SOURCE_VERIFIED", loc_path, "Generator/user cannot directly set SOURCE_VERIFIED"))

        for fid in claim.get("input_fact_ids", []):
            if fid in generated_claim_namespace:
                errors.append(ValidationErrorItem("SELF_EVIDENCE_VIOLATION", f"{loc_path}.input_fact_ids", f"Cannot cite claim/atom '{fid}'"))
            elif fid not in facts_by_id:
                errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.input_fact_ids", f"Unknown fact '{fid}'"))
            else:
                if claim.get("confirmed_event_id") in events_by_id and not _fact_belongs_to_event(
                    fid, claim["confirmed_event_id"]
                ):
                    errors.append(
                        ValidationErrorItem(
                            "CROSS_EVENT_EVIDENCE_MISMATCH",
                            f"{loc_path}.input_fact_ids",
                            f"Claim '{cid}' (event '{claim.get('confirmed_event_id')}') input_fact_ids cites Fact '{fid}' from a different ConfirmedEvent",
                        )
                    )
                if claim.get("generation_fixture_origin") == "GENERATED_FROM_SUPPORTED_FACTS" and not facts_by_id[fid].get("is_active_supported_fact"):
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SUPPORTED_FACT",
                            f"{loc_path}.input_fact_ids",
                            f"GENERATED_FROM_SUPPORTED_FACTS claim '{cid}' passed non-active/non-SUPPORTED fact '{fid}'",
                        )
                    )

        if claim.get("derived_from_claim_ids"):
            errors.append(ValidationErrorItem("SELF_EVIDENCE_VIOLATION", f"{loc_path}.derived_from_claim_ids", f"Claim '{cid}' cannot derive evidence from Claims {claim['derived_from_claim_ids']}"))

        if claim.get("user_edited"):
            if claim.get("claim_version", 1) < 2 or claim.get("previous_claim_version") is None:
                errors.append(ValidationErrorItem("USER_EDIT_VERSIONING_ERROR", loc_path, f"User-edited claim '{cid}' must create claim_version >= 2"))
            if claim.get("reverification_state_on_new_version") == "NOT_APPLICABLE_UNEDITED":
                errors.append(ValidationErrorItem("USER_EDIT_VERSIONING_ERROR", loc_path, f"User-edited claim '{cid}' must specify re-verification state"))

    atoms_by_id: dict[str, dict[str, Any]] = {}
    for idx, atom in enumerate(s3_doc.get("claim_atoms", [])):
        loc_path = f"stages/stage3_claim_assembly.json.claim_atoms[{idx}]"
        aid = atom.get("claim_atom_id", "")
        cid = atom.get("claim_id", "")
        if aid in atoms_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc_path, f"Duplicate claim_atom_id '{aid}'"))
        atoms_by_id[aid] = atom
        if cid not in claims_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.claim_id", f"Unknown claim '{cid}'"))
        else:
            parent = claims_by_id[cid]
            if aid not in parent.get("claim_atom_ids", []):
                errors.append(ValidationErrorItem("BROKEN_REFERENCE", loc_path, f"Atom '{aid}' missing from parent claim '{cid}'"))
            if atom.get("atomizer_run_id") != parent.get("atomizer_run_id"):
                errors.append(ValidationErrorItem("ROLE_RUN_ID_REUSE_VIOLATION", f"{loc_path}.atomizer_run_id", f"Atom '{aid}' atomizer_run_id mismatch"))
            if atom.get("validator_run_id") != parent.get("validator_run_id"):
                errors.append(ValidationErrorItem("ROLE_RUN_ID_REUSE_VIOLATION", f"{loc_path}.validator_run_id", f"Atom '{aid}' validator_run_id mismatch"))
            if atom.get("claim_version") != parent.get("claim_version"):
                errors.append(ValidationErrorItem("USER_EDIT_VERSIONING_ERROR", f"{loc_path}.claim_version", f"Atom '{aid}' claim_version mismatch"))

    for cid, claim in claims_by_id.items():
        for aid in claim.get("claim_atom_ids", []):
            if aid not in atoms_by_id:
                errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"stages/stage3_claim_assembly.json.claims[{cid}]", f"Unknown claim_atom_id '{aid}'"))

    cas_by_atom: dict[str, list[dict[str, Any]]] = {aid: [] for aid in atoms_by_id}
    cas_by_vrun: dict[str, list[dict[str, Any]]] = {}
    for idx, cas in enumerate(s3_doc.get("claim_atom_supports", [])):
        loc_path = f"stages/stage3_claim_assembly.json.claim_atom_supports[{idx}]"
        aid = cas.get("claim_atom_id", "")
        fid = cas.get("canonical_fact_id", "")
        vrun_id = cas.get("validator_run_id", "")
        if vrun_id:
            cas_by_vrun.setdefault(vrun_id, []).append(cas)
        if fid in generated_claim_namespace:
            errors.append(ValidationErrorItem("SELF_EVIDENCE_VIOLATION", f"{loc_path}.canonical_fact_id", f"ClaimAtomSupport cannot cite generated claim/atom '{fid}'"))
            continue
        if cas.get("evidence_source_kind") != "CANONICAL_FACT":
            errors.append(ValidationErrorItem("SELF_EVIDENCE_VIOLATION", f"{loc_path}.evidence_source_kind", "evidence_source_kind must be CANONICAL_FACT"))
        if stage3_runs_by_id.get(vrun_id, {}).get("pipeline_role") != "ClaimSupportValidator":
            errors.append(
                ValidationErrorItem(
                    "ROLE_RUN_ID_REUSE_VIOLATION",
                    f"{loc_path}.validator_run_id",
                    f"ClaimAtomSupport must reference a ClaimSupportValidator run, got '{vrun_id}'",
                )
            )
        parent_claim = None
        if aid not in atoms_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.claim_atom_id", f"Unknown '{aid}'"))
        else:
            parent_claim = claims_by_id.get(atoms_by_id[aid].get("claim_id", ""))
            if parent_claim and vrun_id in (parent_claim.get("generator_run_id"), parent_claim.get("atomizer_run_id")):
                errors.append(
                    ValidationErrorItem(
                        "ROLE_RUN_ID_REUSE_VIOLATION",
                        f"{loc_path}.validator_run_id",
                        f"ClaimAtomSupport reuses ClaimGenerator/ClaimAtomizer run_id '{vrun_id}'",
                    )
                )
            cas_by_atom[aid].append(cas)
        if fid not in facts_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.canonical_fact_id", f"Unknown '{fid}'"))
        elif parent_claim and parent_claim.get("confirmed_event_id") in events_by_id:
            if not _fact_belongs_to_event(fid, parent_claim["confirmed_event_id"]):
                errors.append(
                    ValidationErrorItem(
                        "CROSS_EVENT_EVIDENCE_MISMATCH",
                        f"{loc_path}.canonical_fact_id",
                        f"ClaimAtomSupport '{cas.get('claim_atom_support_id')}' on atom '{aid}' (claim '{parent_claim.get('claim_id')}', event '{parent_claim.get('confirmed_event_id')}') cites Fact '{fid}' from a different ConfirmedEvent",
                    )
                )

    # Validate Stage 3 role_execution_runs input_version_refs semantic linkage (same-Event scoping + non-empty + real hashes)
    validator_input_errors_by_claim: set[str] = set()
    for idx, run in enumerate(s3_doc.get("role_execution_runs", [])):
        loc_path = f"stages/stage3_claim_assembly.json.role_execution_runs[{idx}]"
        rid = run.get("run_id", "")
        role = run.get("pipeline_role")
        cid = run.get("target_claim_id", "")
        cver = run.get("target_claim_version")
        claim = claims_by_id.get(cid)
        if not claim:
            continue
        evt_id = claim.get("confirmed_event_id", "")
        evt = events_by_id.get(evt_id)
        evt_revs = set(evt.get("source_revision_ids", [])) if evt else set()
        refs = run.get("input_version_refs", [])

        if role == "ClaimGenerator":
            for r_idx, ref in enumerate(refs):
                et = ref.get("entity_type", "")
                eid = ref.get("entity_id", "")
                vh = ref.get("version_or_hash", "")
                if et == "CONFIRMED_EVENT":
                    if eid != evt_id:
                        errors.append(ValidationErrorItem("CROSS_EVENT_EVIDENCE_MISMATCH", f"{loc_path}.input_version_refs[{r_idx}]", f"ClaimGenerator run '{rid}' event '{eid}' != claim event '{evt_id}'"))
                    elif evt and vh != evt.get("fixture_event_sha256"):
                        errors.append(ValidationErrorItem("VALIDATOR_INPUT_EVIDENCE_MISMATCH", f"{loc_path}.input_version_refs[{r_idx}]", f"ClaimGenerator run '{rid}' event hash mismatch for '{eid}'"))
                elif et == "CANONICAL_FACT":
                    if eid not in facts_by_id:
                        errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.input_version_refs[{r_idx}]", f"Unknown fact '{eid}'"))
                    else:
                        if evt and not _fact_belongs_to_event(eid, evt_id):
                            errors.append(ValidationErrorItem("CROSS_EVENT_EVIDENCE_MISMATCH", f"{loc_path}.input_version_refs[{r_idx}]", f"ClaimGenerator run '{rid}' cites Fact '{eid}' from a different ConfirmedEvent than '{evt_id}'"))
                        exp_fh = facts_by_id[eid].get("fixture_classification", {}).get("fixture_fact_sha256")
                        if vh != exp_fh:
                            errors.append(ValidationErrorItem("VALIDATOR_INPUT_EVIDENCE_MISMATCH", f"{loc_path}.input_version_refs[{r_idx}]", f"ClaimGenerator run '{rid}' fact hash mismatch for '{eid}'"))
        elif role == "ClaimSupportValidator":
            run_has_error = any(ref.get("version_or_hash", "") in FORBIDDEN_STATUS_VERSION_IDENTIFIERS for ref in refs)
            if not refs:
                errors.append(
                    ValidationErrorItem(
                        "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                        f"{loc_path}.input_version_refs",
                        f"ClaimSupportValidator run '{rid}' (claim '{cid}') has empty input_version_refs; validation MUST bind to real Atom/Fact/Revision/Unit refs",
                    )
                )
                run_has_error = True
            refs_map: dict[tuple[str, str], str] = {}
            for r_idx, ref in enumerate(refs):
                et = ref.get("entity_type", "")
                eid = ref.get("entity_id", "")
                vh = ref.get("version_or_hash", "")
                refs_map[(et, eid)] = vh
                if et == "CANONICAL_FACT":
                    if eid not in facts_by_id:
                        errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.input_version_refs[{r_idx}]", f"Unknown fact '{eid}'"))
                        run_has_error = True
                    else:
                        if evt and not _fact_belongs_to_event(eid, evt_id):
                            errors.append(
                                ValidationErrorItem(
                                    "CROSS_EVENT_EVIDENCE_MISMATCH",
                                    f"{loc_path}.input_version_refs[{r_idx}]",
                                    f"ClaimSupportValidator run '{rid}' (claim '{cid}', event '{evt_id}') cites Fact '{eid}' belonging to another ConfirmedEvent",
                                )
                            )
                            run_has_error = True
                        exp_fh = facts_by_id[eid].get("fixture_classification", {}).get("fixture_fact_sha256")
                        if vh != exp_fh:
                            errors.append(
                                ValidationErrorItem(
                                    "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                                    f"{loc_path}.input_version_refs[{r_idx}]",
                                    f"ClaimSupportValidator run '{rid}' Fact '{eid}' version_or_hash {vh!r} != fixture_fact_sha256 {exp_fh!r}",
                                )
                            )
                            run_has_error = True
                elif et == "SOURCE_REVISION":
                    if eid not in sources_by_id:
                        errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.input_version_refs[{r_idx}]", f"Unknown source_revision_id '{eid}'"))
                        run_has_error = True
                    else:
                        if evt and eid not in evt_revs:
                            errors.append(
                                ValidationErrorItem(
                                    "CROSS_EVENT_EVIDENCE_MISMATCH",
                                    f"{loc_path}.input_version_refs[{r_idx}]",
                                    f"ClaimSupportValidator run '{rid}' (event '{evt_id}') cites SourceRevision '{eid}' from another ConfirmedEvent",
                                )
                            )
                            run_has_error = True
                        if vh != sources_by_id[eid].get("content_sha256"):
                            errors.append(
                                ValidationErrorItem(
                                    "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                                    f"{loc_path}.input_version_refs[{r_idx}]",
                                    f"ClaimSupportValidator run '{rid}' SourceRevision '{eid}' content_sha256 mismatch",
                                )
                            )
                            run_has_error = True
                elif et == "CONTENT_UNIT":
                    if eid not in units_by_id:
                        errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.input_version_refs[{r_idx}]", f"Unknown content_unit_id '{eid}'"))
                        run_has_error = True
                    else:
                        cu_rev = units_by_id[eid].get("source_revision_id", "")
                        if evt and cu_rev not in evt_revs:
                            errors.append(
                                ValidationErrorItem(
                                    "CROSS_EVENT_EVIDENCE_MISMATCH",
                                    f"{loc_path}.input_version_refs[{r_idx}]",
                                    f"ClaimSupportValidator run '{rid}' (event '{evt_id}') cites ContentUnit '{eid}' (rev '{cu_rev}') from another ConfirmedEvent",
                                )
                            )
                            run_has_error = True
                        if vh != units_by_id[eid].get("unit_sha256"):
                            errors.append(
                                ValidationErrorItem(
                                    "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                                    f"{loc_path}.input_version_refs[{r_idx}]",
                                    f"ClaimSupportValidator run '{rid}' ContentUnit '{eid}' unit_sha256 mismatch",
                                )
                            )
                            run_has_error = True

            for aid in claim.get("claim_atom_ids", []):
                if ("CLAIM_ATOM", aid) not in refs_map:
                    errors.append(
                        ValidationErrorItem(
                            "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                            f"{loc_path}.input_version_refs",
                            f"ClaimSupportValidator run '{rid}' missing CLAIM_ATOM input ref for '{aid}'",
                        )
                    )
                    run_has_error = True
                else:
                    atom_obj = atoms_by_id.get(aid)
                    if atom_obj and cver == atom_obj.get("claim_version"):
                        exp_atom_vh = f"v{cver}:{short_hash(sha256_prefixed(atom_obj.get('atom_text', '')), 12)}"
                        if refs_map[("CLAIM_ATOM", aid)] != exp_atom_vh:
                            errors.append(
                                ValidationErrorItem(
                                    "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                                    f"{loc_path}.input_version_refs",
                                    f"ClaimSupportValidator run '{rid}' CLAIM_ATOM '{aid}' hash {refs_map[('CLAIM_ATOM', aid)]!r} != {exp_atom_vh!r}",
                                )
                            )
                            run_has_error = True

            for cas in cas_by_vrun.get(rid, []):
                cfid = cas.get("canonical_fact_id", "")
                if ("CANONICAL_FACT", cfid) not in refs_map:
                    errors.append(
                        ValidationErrorItem(
                            "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                            f"{loc_path}.input_version_refs",
                            f"ClaimSupportValidator run '{rid}' emitted ClaimAtomSupport '{cas.get('claim_atom_support_id')}' citing Fact '{cfid}' absent from input_version_refs",
                        )
                    )
                    run_has_error = True
                for fs in fact_supports_by_fact.get(cfid, []):
                    fs_rev = fs.get("source_revision_id", "")
                    fs_cu = fs.get("content_unit_id", "")
                    if ("SOURCE_REVISION", fs_rev) not in refs_map:
                        errors.append(
                            ValidationErrorItem(
                                "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                                f"{loc_path}.input_version_refs",
                                f"ClaimSupportValidator run '{rid}' missing backing SOURCE_REVISION '{fs_rev}' for Fact '{cfid}'",
                            )
                        )
                        run_has_error = True
                    if ("CONTENT_UNIT", fs_cu) not in refs_map:
                        errors.append(
                            ValidationErrorItem(
                                "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                                f"{loc_path}.input_version_refs",
                                f"ClaimSupportValidator run '{rid}' missing backing CONTENT_UNIT '{fs_cu}' for Fact '{cfid}'",
                            )
                        )
                        run_has_error = True

            if run_has_error and rid == claim.get("validator_run_id"):
                validator_input_errors_by_claim.add(cid)

    # 6. Validate Expected Oracle (ADR-018 PENDING_OWNER_REVIEW, domain vs fixture label separation, strict SUPPORTED & SOURCE_VERIFIED guards)
    for doc_label, o_doc in (
        ("expected/oracle_draft.json", oracle_doc),
        ("expected/refetch_oracle_draft.json", ref_oracle_doc),
    ):
        if (
            o_doc.get("oracle_status") != PENDING_ORACLE_STATUS
            or o_doc.get("frozen_for_golden") is not False
            or o_doc.get("human_approved") is not False
            or o_doc.get("candidate_scope") != CANDIDATE_SCOPE
        ):
            errors.append(
                ValidationErrorItem(
                    "UNAPPROVED_VERSION_MARKED_APPROVED",
                    doc_label,
                    f"v0.4.0 candidate MUST remain oracle_status={PENDING_ORACLE_STATUS!r}, "
                    "frozen_for_golden=false, human_approved=false (ADR-018: AI cannot mark new version approved)",
                )
            )

    prov = oracle_doc.get("provenance_and_freeze_preconditions", {})
    if (
        prov.get("human_approval_status") != "PENDING_OWNER_REVIEW_NOT_APPROVED"
        or prov.get("human_approved") is not False
        or prov.get("adr_018_ai_cannot_self_approve") is not True
    ):
        errors.append(
            ValidationErrorItem(
                "UNAPPROVED_VERSION_MARKED_APPROVED",
                "expected/oracle_draft.json.provenance_and_freeze_preconditions",
                "Candidate provenance MUST declare human_approval_status='PENDING_OWNER_REVIEW_NOT_APPROVED', human_approved=false, adr_018_ai_cannot_self_approve=true",
            )
        )
    if (
        prov.get("live_llm_evaluation_in_scope") is not False
        or prov.get("production_connector_integration_in_scope") is not False
        or prov.get("synthetic_semantics_only_not_connector_evidence") is not True
    ):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_SCOPE_OVERCLAIM",
                "expected/oracle_draft.json.provenance_and_freeze_preconditions",
                "Candidate scope MUST exclude live LLM evaluation and production connector integration",
            )
        )

    scenarios_in_oracle = oracle_doc.get("scenarios", [])
    if len(scenarios_in_oracle) < 12:
        errors.append(ValidationErrorItem("COVERAGE_INSUFFICIENT", "expected/oracle_draft.json.scenarios", f"Requires >=12 scenarios, found {len(scenarios_in_oracle)}"))
    if len(atoms_by_id) < 24:
        errors.append(ValidationErrorItem("COVERAGE_INSUFFICIENT", "stages/stage3_claim_assembly.json.claim_atoms", f"Requires >=24 Claim Atoms, found {len(atoms_by_id)}"))

    for scn in scenarios_in_oracle:
        if scn.get("oracle_status") != PENDING_ORACLE_STATUS:
            errors.append(ValidationErrorItem("UNAPPROVED_VERSION_MARKED_APPROVED", f"scenarios[{scn.get('scenario_id')}]", "Scenario must remain PENDING_OWNER_REVIEW"))

    # Validate expected_facts aligns 1-to-1 with fact_candidates
    expected_facts_by_cand = {ef["fact_candidate_id"]: ef for ef in oracle_doc.get("expected_facts", [])}
    if set(expected_facts_by_cand.keys()) != set(candidates_by_id.keys()):
        errors.append(
            ValidationErrorItem(
                "ORACLE_ALIGNMENT_MISMATCH",
                "expected/oracle_draft.json.expected_facts",
                "expected_facts fact_candidate_ids do not align 1-to-1 with stage2 fact_candidates",
            )
        )
    for cand_id, ef in expected_facts_by_cand.items():
        if ef.get("oracle_status") != PENDING_ORACLE_STATUS:
            errors.append(ValidationErrorItem("UNAPPROVED_VERSION_MARKED_APPROVED", f"expected_facts[{cand_id}]", "Must be PENDING_OWNER_REVIEW"))
        cand = candidates_by_id.get(cand_id)
        if cand and ef.get("expected_fact_type") != cand.get("fact_type"):
            errors.append(ValidationErrorItem("ORACLE_ALIGNMENT_MISMATCH", f"expected_facts[{cand_id}]", "expected_fact_type mismatch with stage2 candidate"))
        if ef.get("expected_promotion_decision") == "REJECT_DO_NOT_PROMOTE" and ef.get("expected_active_supported_fact") is True:
            errors.append(ValidationErrorItem("ILLEGAL_SUPPORTED_FACT", f"expected_facts[{cand_id}]", "Rejected candidate cannot be expected_active_supported_fact=true"))

    expected_atoms_by_id = {ea["claim_atom_id"]: ea for ea in oracle_doc.get("expected_atoms", [])}
    if set(expected_atoms_by_id.keys()) != set(atoms_by_id.keys()):
        errors.append(ValidationErrorItem("ORACLE_ALIGNMENT_MISMATCH", "expected/oracle_draft.json.expected_atoms", "expected_atoms IDs do not match stage3 claim_atoms 1-to-1"))

    expected_claims_by_id = {ec["claim_id"]: ec for ec in oracle_doc.get("expected_claims", [])}
    if set(expected_claims_by_id.keys()) != set(claims_by_id.keys()):
        errors.append(ValidationErrorItem("ORACLE_ALIGNMENT_MISMATCH", "expected/oracle_draft.json.expected_claims", "expected_claims IDs do not match stage3 claims 1-to-1"))

    def _required_atom_has_valid_active_direct_grounding(
        aid: str, claim_version: int, claim_event_id: str | None = None
    ) -> tuple[bool, str]:
        supports = [c for c in cas_by_atom.get(aid, []) if not c.get("removed_by_user")]
        if not supports:
            return False, "atom has 0 active supports"
        if any(c.get("support_role") == "CONTRADICTS" for c in supports):
            return False, "atom has active CONTRADICTS support"
        direct_supports = [c for c in supports if c.get("support_role") == "DIRECT"]
        if not direct_supports:
            return False, "atom has CONTEXT-only support (no active DIRECT support)"

        current_version_direct = [c for c in direct_supports if c.get("evaluated_on_claim_version") == claim_version]
        if not current_version_direct:
            return False, f"atom DIRECT support is stale (not re-verified on claim_version={claim_version})"

        evt_revs = (
            set(events_by_id.get(claim_event_id, {}).get("source_revision_ids", []))
            if claim_event_id and claim_event_id in events_by_id
            else None
        )
        valid_direct_chain = False
        for cas in current_version_direct:
            cfid = cas["canonical_fact_id"]
            if claim_event_id and not _fact_belongs_to_event(cfid, claim_event_id):
                return False, f"supporting fact '{cfid}' does not belong to ConfirmedEvent '{claim_event_id}'"
            fact = facts_by_id.get(cfid)
            if not fact or fact.get("deleted_by_user") or not fact.get("is_active_supported_fact"):
                continue
            if fact.get("fact_support_status") != "SUPPORTED":
                return False, f"supporting fact '{fact['canonical_fact_id']}' status={fact.get('fact_support_status')} != SUPPORTED"
            if fact.get("identity_status") != "RESOLVED":
                return False, f"supporting fact '{fact['canonical_fact_id']}' has identity_status={fact.get('identity_status')}"
            f_supports = fact_supports_by_fact.get(fact["canonical_fact_id"], [])
            if any(fs.get("support_role") == "CONTRADICTS" for fs in f_supports):
                return False, f"supporting fact '{fact['canonical_fact_id']}' has CONTRADICTS FactSupport"
            for fs in f_supports:
                if fs.get("support_role") == "DIRECT":
                    if evt_revs is not None and fs["source_revision_id"] not in evt_revs:
                        continue
                    src = sources_by_id.get(fs["source_revision_id"])
                    if src and not src.get("deleted_by_user"):
                        valid_direct_chain = True
        if not valid_direct_chain:
            return False, "no active non-deleted SourceRevision -> active SUPPORTED CanonicalFact DIRECT chain"
        return True, "ok"

    def _validate_role_atom_grounding(
        aid: str,
        parent_claim: dict[str, Any],
        verified_binding_fs_ids: set[str] | None = None,
    ) -> list[str]:
        """CLAIM-05 / DB Model §3.8 fixture semantics: ROLE atoms MUST bind subject person + Event + role + active concrete contribution evidence by that same person (document author match or role string alone is insufficient)."""
        atom_obj = atoms_by_id.get(aid)
        if not atom_obj:
            return []
        if atom_obj.get("fixture_classification", {}).get("test_atom_kind") != "ROLE":
            return []
        c_ver = parent_claim.get("claim_version", 1)
        c_evt = parent_claim.get("confirmed_event_id", "")
        evt_revs = set(events_by_id.get(c_evt, {}).get("source_revision_ids", []))
        direct_cas = [
            c
            for c in cas_by_atom.get(aid, [])
            if not c.get("removed_by_user")
            and c.get("support_role") == "DIRECT"
            and c.get("evaluated_on_claim_version") == c_ver
        ]
        if not direct_cas:
            return [f"ROLE atom '{aid}' has no active current-version DIRECT ClaimAtomSupport"]

        role_issues: list[str] = []
        valid_role_fact_found = False
        for cas in direct_cas:
            fid = cas.get("canonical_fact_id", "")
            fact = facts_by_id.get(fid)
            if not fact:
                role_issues.append(f"ROLE atom '{aid}' cites unknown Fact '{fid}'")
                continue
            if fact.get("fact_type") != "ROLE":
                role_issues.append(f"ROLE atom '{aid}' DIRECT Fact '{fid}' has fact_type={fact.get('fact_type')!r} != 'ROLE'")
                continue
            rag = fact.get("role_attribution_grounding")
            if not isinstance(rag, dict):
                role_issues.append(f"ROLE Fact '{fid}' lacks role_attribution_grounding (DB Model §3.8 fixture semantics: role string/author match alone is insufficient)")
                continue
            if rag.get("representation_scope") != "FIXTURE_SEMANTICS_FOR_DB_MODEL_3_8_NOT_PRODUCT_TABLE":
                role_issues.append(f"ROLE Fact '{fid}' invalid representation_scope")
                continue
            if rag.get("confirmed_event_id") != c_evt:
                role_issues.append(f"ROLE Fact '{fid}' role_attribution_grounding.confirmed_event_id '{rag.get('confirmed_event_id')}' != claim event '{c_evt}'")
                continue
            if rag.get("author_match_or_role_string_alone_insufficient") is not True:
                role_issues.append(f"ROLE Fact '{fid}' must declare author_match_or_role_string_alone_insufficient=true")
                continue
            subj = rag.get("subject_person_ref")
            contrib_person = rag.get("contribution_contributor_person_ref")
            if not subj or not contrib_person or contrib_person != subj:
                role_issues.append(
                    f"ROLE Fact '{fid}' contribution_contributor_person_ref ({contrib_person!r}) does not match subject_person_ref ({subj!r}); another person's contribution cannot verify role"
                )
                continue
            contrib_fs_ids = rag.get("active_contribution_fact_support_ids", [])
            if not contrib_fs_ids:
                role_issues.append(
                    f"ROLE Fact '{fid}' has 0 active_contribution_fact_support_ids; document author match or role string alone cannot verify ROLE atom '{aid}'"
                )
                continue
            all_contrib_valid = True
            for cfs_id in contrib_fs_ids:
                cfs = fact_supports_by_id.get(cfs_id)
                if not cfs:
                    role_issues.append(f"ROLE Fact '{fid}' references unknown contribution FactSupport '{cfs_id}'")
                    all_contrib_valid = False
                    continue
                if cfs.get("canonical_fact_id") != fid or cfs.get("support_role") != "DIRECT":
                    role_issues.append(f"ROLE Fact '{fid}' contribution '{cfs_id}' must be DIRECT FactSupport on '{fid}'")
                    all_contrib_valid = False
                    continue
                cfs_rev = cfs.get("source_revision_id", "")
                cfs_src = sources_by_id.get(cfs_rev)
                if not cfs_src or cfs_src.get("deleted_by_user") is not False or cfs_rev not in evt_revs:
                    role_issues.append(f"ROLE Fact '{fid}' contribution '{cfs_id}' source revision '{cfs_rev}' deleted or outside event '{c_evt}'")
                    all_contrib_valid = False
                    continue
                if verified_binding_fs_ids is not None and cfs_id not in verified_binding_fs_ids:
                    role_issues.append(f"ROLE Fact '{fid}' contribution '{cfs_id}' missing from same-job verified_evidence_bindings")
                    all_contrib_valid = False
            if all_contrib_valid:
                valid_role_fact_found = True

        if valid_role_fact_found:
            return []
        return role_issues or [f"ROLE atom '{aid}' failed concrete personal contribution evidence check"]

    atom_statuses_seen: set[str] = set()
    for aid, ea in expected_atoms_by_id.items():
        if ea.get("oracle_status") != PENDING_ORACLE_STATUS:
            errors.append(ValidationErrorItem("UNAPPROVED_VERSION_MARKED_APPROVED", f"expected_atoms[{aid}]", "Must be PENDING_OWNER_REVIEW"))
        dom_status = str(ea.get("expected_domain_atom_status", ""))
        atom_statuses_seen.add(dom_status)
        if dom_status not in OFFICIAL_ATOM_STATUSES:
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_atoms[{aid}].expected_domain_atom_status",
                    f"Must use official Atom status {OFFICIAL_ATOM_STATUSES}, got {dom_status!r}",
                )
            )
        ftc = ea.get("fixture_test_classification", {})
        if ftc.get("label_scope") != "TEST_FIXTURE_ONLY_NOT_DOMAIN_API" or not str(ftc.get("draft_atom_verdict", "")).startswith("DRAFT_ATOM_"):
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_atoms[{aid}].fixture_test_classification",
                    "Test-only DRAFT_ATOM_* verdict must live inside fixture_test_classification with TEST_FIXTURE_ONLY_NOT_DOMAIN_API",
                )
            )
        atom_rec = atoms_by_id.get(aid)
        if atom_rec:
            parent_claim = claims_by_id.get(atom_rec.get("claim_id", ""))
            c_ver = parent_claim.get("claim_version", 1) if parent_claim else 1
            c_evt = parent_claim.get("confirmed_event_id") if parent_claim else None
            if dom_status == "SUPPORTED":
                ok, reason = _required_atom_has_valid_active_direct_grounding(aid, c_ver, c_evt)
                if not ok:
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SUPPORTED_ATOM",
                            f"expected_atoms[{aid}].expected_domain_atom_status",
                            f"Atom '{aid}' cannot be SUPPORTED ({reason})",
                        )
                    )
                if parent_claim:
                    role_errs = _validate_role_atom_grounding(aid, parent_claim)
                    for rmsg in role_errs:
                        errors.append(ValidationErrorItem("ROLE_EVIDENCE_VALIDATION_ERROR", f"expected_atoms[{aid}]", rmsg))
                        errors.append(ValidationErrorItem("ILLEGAL_SUPPORTED_ATOM", f"expected_atoms[{aid}]", f"ROLE atom '{aid}' failed role evidence rule ({rmsg})"))
            elif dom_status == "USER_CONFIRMED" and not atom_rec.get("user_confirmed_by_owner"):
                errors.append(
                    ValidationErrorItem(
                        "ORACLE_ALIGNMENT_MISMATCH",
                        f"expected_atoms[{aid}]",
                        "USER_CONFIRMED atom must have user_confirmed_by_owner=true in stage3",
                    )
                )

    missing_atom_statuses = sorted(set(OFFICIAL_ATOM_STATUSES) - atom_statuses_seen)
    if missing_atom_statuses:
        errors.append(ValidationErrorItem("COVERAGE_INSUFFICIENT", "expected/oracle_draft.json.expected_atoms", f"Missing official Atom statuses: {missing_atom_statuses}"))

    notion_test_pass_fail: dict[str, set[bool]] = {}
    for cid, ec in expected_claims_by_id.items():
        if ec.get("oracle_status") != PENDING_ORACLE_STATUS:
            errors.append(ValidationErrorItem("UNAPPROVED_VERSION_MARKED_APPROVED", f"expected_claims[{cid}]", "Must be PENDING_OWNER_REVIEW"))
        ftc = ec.get("fixture_test_classification", {})
        verdict = str(ftc.get("draft_claim_verdict", ""))
        proposal = str(ftc.get("product_status_proposal", ""))
        if ftc.get("label_scope") != "TEST_FIXTURE_ONLY_NOT_DOMAIN_API" or not verdict.startswith("DRAFT_CLAIM_"):
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_claims[{cid}].fixture_test_classification",
                    f"Provisional claim verdict must live in fixture_test_classification with DRAFT_CLAIM_* prefix, got {verdict!r}",
                )
            )
        if proposal == "SOURCE_VERIFIED":
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_claims[{cid}].fixture_test_classification.product_status_proposal",
                    "Test proposal must not directly assert finalized product status 'SOURCE_VERIFIED'",
                )
            )

        for ntid in ec.get("notion_test_ids", []):
            notion_test_pass_fail.setdefault(ntid, set()).add(bool(ec.get("expected_source_verified_computed")))

        claim = claims_by_id.get(cid)
        if not claim:
            continue

        claims_source_verified_computed = (
            ec.get("expected_source_verified_computed") is True
            or verdict in ("SOURCE_VERIFIED", "DRAFT_CLAIM_GROUNDED_CANDIDATE")
            or proposal in ("SOURCE_VERIFIED", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW")
        )
        if claims_source_verified_computed:
            if claim.get("user_edited") and claim.get("reverification_state_on_new_version") != "REVERIFIED_PASSED_ON_NEW_VERSION":
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SOURCE_VERIFIED",
                        f"expected_claims[{cid}]",
                        f"User-edited claim (v{claim.get('claim_version')}) with state {claim.get('reverification_state_on_new_version')!r} cannot be SOURCE_VERIFIED",
                    )
                )
            val_rid = claim.get("validator_run_id")
            val_run = stage3_runs_by_id.get(val_rid or "")
            ctx = val_run.get("reverification_job_context") if val_run else None
            if cid in validator_input_errors_by_claim:
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SOURCE_VERIFIED",
                        f"expected_claims[{cid}]",
                        f"Claim '{cid}' ClaimSupportValidator run '{val_rid}' failed input_version_refs evidence linkage; SOURCE_VERIFIED forbidden",
                    )
                )
            if not (
                isinstance(ctx, dict)
                and ctx.get("refetch_checked_in_same_job") is True
                and ctx.get("hash_and_locator_verified") is True
                and ctx.get("permission_verified") is True
                and ctx.get("semantics_verified") is True
            ):
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SOURCE_VERIFIED",
                        f"expected_claims[{cid}]",
                        "SOURCE_VERIFIED requires same-job hash, locator, permission, and semantics verification in ClaimSupportValidator run",
                    )
                )
            # Verify end-to-end verified_evidence_bindings: Atom -> Fact -> active DIRECT FactSupport -> Revision/Unit/Locator -> same-job refetch
            verified_binding_keys: set[tuple[str, str, str]] = set()
            verified_binding_fs_ids: set[str] = set()
            c_ver = claim.get("claim_version", 1)
            c_evt = claim.get("confirmed_event_id", "")
            evt_revs_claim = set(events_by_id.get(c_evt, {}).get("source_revision_ids", []))
            raw_bindings = ctx.get("verified_evidence_bindings", []) if isinstance(ctx, dict) else []
            if not isinstance(raw_bindings, list) or not raw_bindings:
                errors.append(
                    ValidationErrorItem(
                        "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                        f"expected_claims[{cid}].verified_evidence_bindings",
                        f"SOURCE_VERIFIED claim '{cid}' requires non-empty verified_evidence_bindings on validator run '{val_rid}'",
                    )
                )
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SOURCE_VERIFIED",
                        f"expected_claims[{cid}]",
                        f"Claim '{cid}' has no verified_evidence_bindings; SOURCE_VERIFIED forbidden",
                    )
                )
            else:
                for b_idx, bnd in enumerate(raw_bindings):
                    b_loc = f"claims[{cid}].validator[{val_rid}].verified_evidence_bindings[{b_idx}]"
                    b_aid = bnd.get("claim_atom_id", "")
                    b_fid = bnd.get("canonical_fact_id", "")
                    b_fsid = bnd.get("fact_support_id", "")
                    b_rev = bnd.get("source_revision_id", "")
                    b_cu = bnd.get("content_unit_id", "")
                    b_fact = facts_by_id.get(b_fid)
                    b_fs = fact_supports_by_id.get(b_fsid)
                    b_src = sources_by_id.get(b_rev)
                    b_unit = units_by_id.get(b_cu)
                    b_ok = (
                        bnd.get("claim_version") == c_ver
                        and bnd.get("support_role") == "DIRECT"
                        and bnd.get("same_job_refetch_result") == "VERIFIED_EXACT_MATCH"
                        and b_aid in claim.get("claim_atom_ids", [])
                        and b_fact is not None
                        and b_fact.get("is_active_supported_fact") is True
                        and b_fact.get("deleted_by_user") is False
                        and _fact_belongs_to_event(b_fid, c_evt)
                        and bnd.get("fact_fixture_sha256") == b_fact.get("fixture_classification", {}).get("fixture_fact_sha256")
                        and b_fs is not None
                        and b_fs.get("canonical_fact_id") == b_fid
                        and b_fs.get("support_role") == "DIRECT"
                        and b_fs.get("source_revision_id") == b_rev
                        and b_rev in evt_revs_claim
                        and b_fs.get("content_unit_id") == b_cu
                        and b_fs.get("locator", {}).get("locator_id") == bnd.get("locator_id")
                        and b_fs.get("locator", {}).get("span_sha256") == bnd.get("span_sha256")
                        and b_src is not None
                        and b_src.get("deleted_by_user") is False
                        and b_src.get("content_sha256") == bnd.get("source_content_sha256")
                        and b_unit is not None
                        and b_unit.get("unit_sha256") == bnd.get("unit_sha256")
                    )
                    if not b_ok:
                        errors.append(
                            ValidationErrorItem(
                                "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                                b_loc,
                                f"Evidence binding broken/unmatched on SOURCE_VERIFIED claim '{cid}' (atom='{b_aid}', fact='{b_fid}', fs='{b_fsid}')",
                            )
                        )
                        errors.append(
                            ValidationErrorItem(
                                "ILLEGAL_SOURCE_VERIFIED",
                                f"expected_claims[{cid}]",
                                f"Claim '{cid}' has broken verified_evidence_bindings entry [{b_idx}]; SOURCE_VERIFIED forbidden",
                            )
                        )
                    else:
                        verified_binding_keys.add((b_aid, b_fid, b_fsid))
                        verified_binding_fs_ids.add(b_fsid)

            atom_ids = claim.get("claim_atom_ids", [])
            required_atoms = [aid for aid in atom_ids if atoms_by_id.get(aid, {}).get("is_required_atom")]
            if not required_atoms:
                errors.append(ValidationErrorItem("ILLEGAL_SOURCE_VERIFIED", f"expected_claims[{cid}]", "Claim with 0 required atoms cannot be SOURCE_VERIFIED"))
            for aid in required_atoms:
                ea = expected_atoms_by_id.get(aid)
                if ea and ea.get("expected_domain_atom_status") != "SUPPORTED":
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"Required atom '{aid}' has domain status {ea.get('expected_domain_atom_status')!r} (only SUPPORTED qualifies; USER_CONFIRMED/UNSUPPORTED/STALE forbidden)",
                        )
                    )
                ok, reason = _required_atom_has_valid_active_direct_grounding(aid, c_ver, c_evt)
                if not ok:
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"Required atom '{aid}' fails grounding invariant ({reason}); SOURCE_VERIFIED forbidden",
                        )
                    )
                # Ensure required atom has at least one verified end-to-end binding matching its active DIRECT ClaimAtomSupport -> FactSupport
                atom_direct_cas = [
                    c
                    for c in cas_by_atom.get(aid, [])
                    if not c.get("removed_by_user")
                    and c.get("support_role") == "DIRECT"
                    and c.get("evaluated_on_claim_version") == c_ver
                    and c.get("validator_run_id") == val_rid
                ]
                has_verified_binding_for_atom = any(
                    (aid, c["canonical_fact_id"], fs["fact_support_id"]) in verified_binding_keys
                    for c in atom_direct_cas
                    for fs in fact_supports_by_fact.get(c["canonical_fact_id"], [])
                    if fs.get("support_role") == "DIRECT"
                )
                if not has_verified_binding_for_atom:
                    errors.append(
                        ValidationErrorItem(
                            "VALIDATOR_INPUT_EVIDENCE_MISMATCH",
                            f"expected_claims[{cid}].required_atom[{aid}]",
                            f"Required atom '{aid}' lacks verified end-to-end Atom->Fact->FactSupport->Revision/Unit binding on run '{val_rid}'",
                        )
                    )
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"Required atom '{aid}' lacks verified_evidence_bindings linkage; SOURCE_VERIFIED forbidden",
                        )
                    )
                role_errs = _validate_role_atom_grounding(aid, claim, verified_binding_fs_ids)
                for rmsg in role_errs:
                    errors.append(ValidationErrorItem("ROLE_EVIDENCE_VALIDATION_ERROR", f"expected_claims[{cid}].required_atom[{aid}]", rmsg))
                    errors.append(ValidationErrorItem("ILLEGAL_SOURCE_VERIFIED", f"expected_claims[{cid}]", f"ROLE atom '{aid}' failed CLAIM-05 role evidence rule ({rmsg}); SOURCE_VERIFIED forbidden"))

        # OD-003 guard: user-deleted SourceRevision / CanonicalFact dependent Claims
        depends_on_user_deleted_source = False
        for aid in claim.get("claim_atom_ids", []):
            for cas in cas_by_atom.get(aid, []):
                fact = facts_by_id.get(cas.get("canonical_fact_id", ""))
                if not fact:
                    continue
                if fact.get("deleted_by_user"):
                    depends_on_user_deleted_source = True
                for fs in fact_supports_by_fact.get(fact["canonical_fact_id"], []):
                    src = sources_by_id.get(fs.get("source_revision_id", ""))
                    if src and src.get("deleted_by_user"):
                        depends_on_user_deleted_source = True

        if depends_on_user_deleted_source:
            if verdict == "DRAFT_CLAIM_PARTIALLY_GROUNDED":
                errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"expected_claims[{cid}]", "OD-003 forbids leaving a deletion-dependent Claim as PARTIALLY_GROUNDED"))
            if verdict != "DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003":
                errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"expected_claims[{cid}]", f"OD-003 dependent Claim must be DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003, got {verdict!r}"))
            for aid in claim.get("claim_atom_ids", []):
                ea = expected_atoms_by_id.get(aid)
                if ea and ea.get("fixture_test_classification", {}).get("draft_atom_verdict") != "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003":
                    errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"expected_atoms[{aid}]", "Atom of OD-003 purged Claim must be DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003"))
            dl = claim.get("deletion_lifecycle")
            if not isinstance(dl, dict):
                errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"claims[{cid}].deletion_lifecycle", "Missing OD-003 deletion_lifecycle"))
            else:
                if not (
                    dl.get("deletion_preview_shown") is True
                    and dl.get("explicit_user_confirmation") is True
                    and dl.get("immediate_access_blocked_after_confirmation") is True
                    and dl.get("cleanup_completed") is True
                    and dl.get("distinguished_from_refetch_failure_ref03") is True
                ):
                    errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"claims[{cid}].deletion_lifecycle", "OD-003 requires preview, explicit confirmation, immediate block, cleanup, and REF-03 distinction"))
                if (
                    dl.get("post_cleanup_retained_claim_versions") != []
                    or dl.get("post_cleanup_retained_edited_copies") != 0
                    or dl.get("post_cleanup_retained_approved_copies") != 0
                ):
                    errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"claims[{cid}].deletion_lifecycle", "OD-003 requires 0 retained versions/edited/approved copies"))
                for pres_rev in dl.get("independent_source_revisions_preserved", []):
                    src_pres = sources_by_id.get(pres_rev)
                    if not src_pres or src_pres.get("deleted_by_user") is not False:
                        errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"claims[{cid}].deletion_lifecycle", f"Independent source '{pres_rev}' must remain preserved"))
                for pres_fact in dl.get("independent_facts_preserved", []):
                    fact_pres = facts_by_id.get(pres_fact)
                    if not fact_pres or fact_pres.get("deleted_by_user") is not False:
                        errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"claims[{cid}].deletion_lifecycle", f"Independent fact '{pres_fact}' must remain preserved"))
            od_exp = ec.get("od003_expected_post_cleanup")
            if not isinstance(od_exp, dict) or not (
                od_exp.get("immediate_access_blocked_after_confirmation") is True
                and od_exp.get("claim_and_server_copies_purged") is True
                and od_exp.get("retained_claim_versions") == []
                and od_exp.get("retained_edited_copies") == 0
                and od_exp.get("retained_approved_copies") == 0
                and od_exp.get("distinguished_from_ref03_refetch_failure") is True
                and od_exp.get("partially_grounded_forbidden_post_deletion") is True
            ):
                errors.append(ValidationErrorItem("OD003_DELETION_POLICY_VIOLATION", f"expected_claims[{cid}].od003_expected_post_cleanup", "OD-003 expected assertions incomplete"))

    # Require PASS + FAIL expectations for CLAIM-05, CLAIM-06, CLAIM-08
    for req_claim_rule in ("CLAIM-05", "CLAIM-06", "CLAIM-08"):
        outcomes = notion_test_pass_fail.get(req_claim_rule, set())
        if outcomes != {True, False}:
            errors.append(
                ValidationErrorItem(
                    "COVERAGE_INSUFFICIENT",
                    "expected/oracle_draft.json.expected_claims",
                    f"Required rule '{req_claim_rule}' must link both PASS (true) and FAIL (false) claim expectations, got {outcomes}",
                )
            )

    # 7. Validate Official REF-01..REF-08 Matrix (ALL 8 mapped; product_runtime_verified=false for ALL 8)
    snaps_by_id = {r["fixture_case_id"]: r for r in ref_snap_doc.get("refetch_snapshots", [])}
    checks_by_id = {r["fixture_case_id"]: r for r in s4_doc.get("refetch_checks", [])}
    ref_oracles_by_id = {r["fixture_case_id"]: r for r in ref_oracle_doc.get("expected_refetch_fixtures", [])}

    if set(snaps_by_id.keys()) != set(checks_by_id.keys()) or set(snaps_by_id.keys()) != set(ref_oracles_by_id.keys()):
        errors.append(ValidationErrorItem("ORACLE_ALIGNMENT_MISMATCH", "expected/refetch_oracle_draft.json", "Refetch fixture_case_ids do not align 1-to-1 across snapshots, stage4 checks, and refetch oracle"))

    matrix_rows = ref_oracle_doc.get("official_ref_matrix", [])
    matrix_by_ref = {row["official_ref_id"]: row for row in matrix_rows}
    if set(matrix_by_ref.keys()) != set(OFFICIAL_REF_REQUIREMENTS.keys()):
        errors.append(ValidationErrorItem("OFFICIAL_REF_MAPPING_ERROR", "expected/refetch_oracle_draft.json.official_ref_matrix", f"Must cover exact REF-01..REF-08, got {sorted(matrix_by_ref.keys())}"))

    synthetic_stubs_refs: list[str] = []
    partial_static_refs: list[str] = []
    synthetic_cases_runtime_unverified_refs: list[str] = []
    linked_from_matrix: set[str] = set()

    for official_ref_id, expected_label in OFFICIAL_REF_REQUIREMENTS.items():
        row = matrix_by_ref.get(official_ref_id)
        if not row:
            continue
        loc = f"expected/refetch_oracle_draft.json.official_ref_matrix[{official_ref_id}]"
        if row.get("official_requirement_label") != expected_label:
            errors.append(ValidationErrorItem("OFFICIAL_REF_MAPPING_ERROR", loc, f"Label mismatch: expected {expected_label!r}, got {row.get('official_requirement_label')!r}"))
        linked = row.get("linked_fixture_case_ids", [])
        cov_status = row.get("synthetic_fixture_coverage_status")
        prod_verified = row.get("product_runtime_verified")
        row_oracle_status = row.get("oracle_status")
        gap_details = row.get("coverage_gap_details", [])

        if not linked:
            errors.append(
                ValidationErrorItem(
                    "MISSING_OFFICIAL_REF_CASE_MAPPING",
                    loc,
                    f"{official_ref_id} has 0 linked synthetic fixtures (REF-01..REF-08 must all have synthetic cases prepared)",
                )
            )
        if prod_verified is not False or not gap_details:
            errors.append(
                ValidationErrorItem(
                    "FALSE_PRODUCT_VERIFICATION_CLAIM",
                    loc,
                    f"{official_ref_id} MUST set product_runtime_verified=false with non-empty coverage_gap_details (synthetic cases != M4 product test pass)",
                )
            )
        if row_oracle_status != PENDING_ORACLE_STATUS:
            errors.append(ValidationErrorItem("UNAPPROVED_VERSION_MARKED_APPROVED", loc, f"{official_ref_id} must remain {PENDING_ORACLE_STATUS}"))

        if official_ref_id in ("REF-04", "REF-05") and cov_status != "PARTIAL_STATIC_FIXTURE_PREPARED":
            errors.append(ValidationErrorItem("FALSE_PRODUCT_VERIFICATION_CLAIM", loc, f"{official_ref_id} must be PARTIAL_STATIC_FIXTURE_PREPARED"))
        if official_ref_id in ("REF-06", "REF-07", "REF-08") and cov_status != "SYNTHETIC_CASE_PREPARED_RUNTIME_UNVERIFIED":
            errors.append(ValidationErrorItem("FALSE_PRODUCT_VERIFICATION_CLAIM", loc, f"{official_ref_id} must be SYNTHETIC_CASE_PREPARED_RUNTIME_UNVERIFIED"))

        if cov_status == "SYNTHETIC_STUB_PREPARED":
            synthetic_stubs_refs.append(official_ref_id)
        elif cov_status == "PARTIAL_STATIC_FIXTURE_PREPARED":
            partial_static_refs.append(official_ref_id)
        elif cov_status == "SYNTHETIC_CASE_PREPARED_RUNTIME_UNVERIFIED":
            synthetic_cases_runtime_unverified_refs.append(official_ref_id)

        for fcid in linked:
            linked_from_matrix.add(fcid)
            if fcid not in snaps_by_id:
                errors.append(ValidationErrorItem("BROKEN_REFERENCE", loc, f"Unknown fixture_case_id '{fcid}'"))
            elif snaps_by_id[fcid].get("official_ref_id") != official_ref_id:
                errors.append(ValidationErrorItem("OFFICIAL_REF_MAPPING_ERROR", loc, f"Fixture '{fcid}' official_ref_id mismatch"))

    unlinked_fixtures = sorted(set(snaps_by_id.keys()) - linked_from_matrix)
    if unlinked_fixtures:
        errors.append(ValidationErrorItem("OFFICIAL_REF_MAPPING_ERROR", "official_ref_matrix", f"Unlinked fixtures: {unlinked_fixtures}"))

    all_canary_tokens: list[str] = []
    for snap in snaps_by_id.values():
        all_canary_tokens.extend(snap.get("canary_tokens", []))

    ref06_timings_seen: set[str] = set()
    ref06_changed_hash_blocked_seen = False
    ref07_ops_seen: set[str] = set()
    ref07_prior_only_blocked_seen = False
    ref07_toctou_blocked_seen = False
    ref07_export_unapproved_blocked_seen = False
    ref07_export_version_mismatch_blocked_seen = False
    ref08_dedup_seen = False
    ref08_sep_job_handoff_seen = False
    ref08_limit_breaches_seen: set[str] = set()

    for fcid in sorted(snaps_by_id.keys()):
        snap = snaps_by_id[fcid]
        chk = checks_by_id.get(fcid)
        ro = ref_oracles_by_id.get(fcid)
        if not (chk and ro):
            continue

        official_ref_id = snap["official_ref_id"]
        if chk["official_ref_id"] != official_ref_id or ro["official_ref_id"] != official_ref_id:
            errors.append(ValidationErrorItem("OFFICIAL_REF_MAPPING_ERROR", f"refetch_fixture[{fcid}]", "official_ref_id mismatch"))

        ftc = ro.get("fixture_test_classification", {})
        if ftc.get("label_scope") != "TEST_FIXTURE_ONLY_NOT_DOMAIN_API" or not str(ftc.get("draft_refetch_verdict", "")).startswith("DRAFT_REFETCH_"):
            errors.append(ValidationErrorItem("VERDICT_NAMESPACE_CONFLATION", f"expected_refetch_fixtures[{fcid}].fixture_test_classification", "Must place DRAFT_REFETCH_* in TEST_FIXTURE_ONLY_NOT_DOMAIN_API"))
        if ro.get("oracle_status") != PENDING_ORACLE_STATUS:
            errors.append(ValidationErrorItem("UNAPPROVED_VERSION_MARKED_APPROVED", f"expected_refetch_fixtures[{fcid}]", "Must remain PENDING_OWNER_REVIEW"))

        # URL safety check
        urls_to_check = [snap["target_url"], *snap.get("additional_unsafe_urls", [])]
        primary_safe, primary_reason = check_url_safety(snap["target_url"])
        if not primary_safe:
            if snap.get("url_safety_precheck") != "BLOCKED_UNSAFE_URL" or snap.get("fetch_attempted") is not False:
                errors.append(ValidationErrorItem("UNSAFE_URL_POLICY_VIOLATION", f"refetch_snapshots[{fcid}]", f"Unsafe URL '{snap['target_url']}' ({primary_reason}) MUST be blocked with fetch_attempted=false"))
            if ro.get("expected_fetch_attempted") is not False:
                errors.append(ValidationErrorItem("UNSAFE_URL_POLICY_VIOLATION", f"expected_refetch_fixtures[{fcid}]", "Unsafe URL oracle MUST expect fetch_attempted=false"))
            for extra_url in urls_to_check[1:]:
                extra_safe, _ = check_url_safety(extra_url)
                if extra_safe:
                    errors.append(ValidationErrorItem("UNSAFE_URL_POLICY_VIOLATION", f"refetch_snapshots[{fcid}].additional_unsafe_urls", f"Expected unsafe vector, got '{extra_url}'"))

        # Baseline locator round-trip & returned body check
        base_rev = chk["baseline_source_revision_id"]
        if base_rev not in sources_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"stage4_refetch_checks[{fcid}]", f"Unknown '{base_rev}'"))
        else:
            locator_checks_run += 1
            _, rt_errors = verify_locator_roundtrip(norm_text_by_rev[base_rev], chk["baseline_locator"], context_label=f"stage4_refetch_checks[{fcid}]")
            for code, msg in rt_errors:
                errors.append(ValidationErrorItem(code, f"stage4_refetch_checks[{fcid}].baseline_locator", msg))

            ret_body = snap.get("returned_body_fixture_only")
            if ret_body is not None:
                ret_norm = normalize_text(ret_body)
                calc_ret_hash = sha256_prefixed(ret_norm)
                if snap.get("returned_content_sha256") != calc_ret_hash:
                    errors.append(ValidationErrorItem("HASH_MISMATCH", f"refetch_snapshots[{fcid}].returned_content_sha256", f"Recorded {snap.get('returned_content_sha256')} != computed {calc_ret_hash}"))
                actual_hash_match = calc_ret_hash == sources_by_id[base_rev]["content_sha256"]
                if chk.get("hash_recheck_matched") is not None and chk["hash_recheck_matched"] != actual_hash_match:
                    errors.append(ValidationErrorItem("HASH_MISMATCH", f"stage4_refetch_checks[{fcid}].hash_recheck_matched", f"Recorded {chk['hash_recheck_matched']} != actual {actual_hash_match}"))
                if not snap.get("is_partial_response"):
                    locator_to_recheck = chk.get("tested_locator") or chk["baseline_locator"]
                    _, ref_rt_errs = verify_locator_roundtrip(ret_norm, locator_to_recheck, context_label=f"refetch_recheck[{fcid}]")
                    actual_loc_match = len(ref_rt_errs) == 0
                    if chk.get("locator_recheck_matched") != actual_loc_match:
                        errors.append(ValidationErrorItem("OFFSET_OR_ROUNDTRIP_MISMATCH", f"stage4_refetch_checks[{fcid}].locator_recheck_matched", f"Recorded {chk['locator_recheck_matched']} != actual {actual_loc_match}"))

        if chk.get("tested_locator") is not None:
            if chk.get("hash_recheck_matched") is not True or chk.get("locator_recheck_matched") is not False or ro.get("expected_approval_blocked") is not True:
                errors.append(ValidationErrorItem("LOCATOR_ONLY_MISMATCH_POLICY_VIOLATION", f"stage4_refetch_checks[{fcid}].tested_locator", "Same-hash locator-only mismatch must block approval"))

        retry_stub = chk.get("retry_policy_stub")
        if retry_stub is not None:
            if (
                not isinstance(retry_stub.get("max_retry_attempts"), int)
                or retry_stub["max_retry_attempts"] < 1
                or not isinstance(retry_stub.get("attempts_exhausted_in_stub"), int)
                or retry_stub["attempts_exhausted_in_stub"] < 1
                or retry_stub.get("approval_blocked_while_unavailable") is not True
                or retry_stub.get("distinguished_from_404") is not True
                or ro.get("expected_approval_blocked") is not True
            ):
                errors.append(ValidationErrorItem("TRANSIENT_RETRY_POLICY_VIOLATION", f"stage4_refetch_checks[{fcid}].retry_policy_stub", "Transient failure must enforce bounded retry, block approval, and distinguish from 404"))

        if chk["target_claim_id"] not in claims_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"stage4_refetch_checks[{fcid}].target_claim_id", f"Unknown claim '{chk['target_claim_id']}'"))

        if official_ref_id in ("REF-02", "REF-03", "REF-05"):
            if ro.get("expected_unverifiable_now") is not True or ro.get("expected_source_deleted_confirmed") is not False or ro.get("expected_historical_fact_falsified") is not False:
                errors.append(ValidationErrorItem("UNVERIFIABLE_VS_DELETED_OR_FALSIFIED_CONFLATION", f"expected_refetch_fixtures[{fcid}]", f"{official_ref_id} must set unverifiable_now=true, deleted_confirmed=false, falsified=false"))
            if ro.get("expected_approval_blocked") is not True:
                errors.append(ValidationErrorItem("REFETCH_APPROVAL_BLOCK_VIOLATION", f"expected_refetch_fixtures[{fcid}]", f"{official_ref_id} must block approval"))

        if ro.get("expected_claim_auto_mutated") is not False:
            errors.append(ValidationErrorItem("POST_MVP_VIOLATION", f"expected_refetch_fixtures[{fcid}]", "Automatic Claim mutation is Post-MVP and must stay false"))

        # REF-04 canary checks
        leaked_in_clean = detect_canary_in_obj(chk.get("persisted_payload_snapshot", {}), all_canary_tokens)
        if leaked_in_clean:
            errors.append(ValidationErrorItem("CANARY_LEAK_DETECTED", f"stage4_refetch_checks[{fcid}].persisted_payload_snapshot", f"Canary leaked: {leaked_in_clean}"))
        if official_ref_id == "REF-04":
            neg_fixture = chk.get("negative_leaky_payload_fixture")
            if not detect_canary_in_obj(neg_fixture, all_canary_tokens):
                errors.append(ValidationErrorItem("CANARY_NEGATIVE_FIXTURE_INVALID", f"stage4_refetch_checks[{fcid}].negative_leaky_payload_fixture", "REF-04 negative fixture must contain canary token"))

        # REF-06 crash/replay checks
        if official_ref_id == "REF-06":
            cr = chk.get("crash_replay_stub")
            exp6 = ro.get("expected_ref06_assertions")
            if not isinstance(cr, dict) or not isinstance(exp6, dict):
                errors.append(ValidationErrorItem("REF06_CRASH_REPLAY_POLICY_VIOLATION", f"refetch[{fcid}]", "REF-06 requires crash_replay_stub and expected_ref06_assertions"))
            else:
                ref06_timings_seen.add(str(cr.get("crash_timing")))
                if cr.get("raw_text_fallback_path_exists") is not False or cr.get("atomic_metadata_checkpoint_commit") is not True or cr.get("duplicate_records_created") != 0 or cr.get("in_memory_raw_text_wiped") is not True:
                    errors.append(ValidationErrorItem("REF06_CRASH_REPLAY_POLICY_VIOLATION", f"stage4_refetch_checks[{fcid}].crash_replay_stub", "REF-06 requires atomic metadata+checkpoint commit, 0 duplicates, wiped memory, and NO stored raw-text fallback"))
                if chk.get("hash_recheck_matched") is False:
                    ref06_changed_hash_blocked_seen = True
                    if ro.get("expected_approval_blocked") is not True or exp6.get("expected_replay_reproduced") is not False:
                        errors.append(ValidationErrorItem("REF06_CRASH_REPLAY_POLICY_VIOLATION", f"expected_refetch_fixtures[{fcid}]", "REF-06 changed hash on replay must explicitly block"))

        # REF-07 Approval / Export / Evidence-View checks
        if official_ref_id == "REF-07":
            op = chk.get("operation_refetch_stub")
            exp7 = ro.get("expected_ref07_assertions")
            if not isinstance(op, dict) or not isinstance(exp7, dict):
                errors.append(ValidationErrorItem("REF07_OPERATION_REFETCH_POLICY_VIOLATION", f"refetch[{fcid}]", "REF-07 requires operation_refetch_stub and expected_ref07_assertions"))
            else:
                op_type = str(op.get("operation_type"))
                ref07_ops_seen.add(op_type)
                if op.get("prior_verification_record_only"):
                    ref07_prior_only_blocked_seen = True
                    if exp7.get("expected_operation_allowed") is not False or ro.get("expected_approval_blocked") is not True:
                        errors.append(ValidationErrorItem("REF07_OPERATION_REFETCH_POLICY_VIOLATION", f"expected_refetch_fixtures[{fcid}]", "Prior verification record ONLY must block operation"))
                if op.get("permission_or_validity_changed_before_save"):
                    ref07_toctou_blocked_seen = True
                    if exp7.get("expected_operation_allowed") is not False or ro.get("expected_approval_blocked") is not True:
                        errors.append(ValidationErrorItem("REF07_OPERATION_REFETCH_POLICY_VIOLATION", f"expected_refetch_fixtures[{fcid}]", "TOCTOU state change before save must block operation"))
                if op_type == "EXPORT_DOWNLOAD":
                    if op.get("export_excludes_raw_source_quotes") is not True:
                        errors.append(ValidationErrorItem("REF07_OPERATION_REFETCH_POLICY_VIOLATION", f"stage4_refetch_checks[{fcid}]", "Export MUST exclude raw source quotes"))
                    pre = op.get("export_precondition_input")
                    if not isinstance(pre, dict):
                        errors.append(
                            ValidationErrorItem(
                                "REF07_EXPORT_PRECONDITION_VIOLATION",
                                f"stage4_refetch_checks[{fcid}].operation_refetch_stub.export_precondition_input",
                                "EXPORT_DOWNLOAD fixture MUST specify export_precondition_input (approved Claim ID & exact version)",
                            )
                        )
                    else:
                        req_cid = pre.get("requested_claim_id")
                        req_ver = pre.get("requested_claim_version")
                        is_approved = pre.get("claim_version_approved")
                        app_ver = pre.get("approved_claim_version")
                        ver_matches = pre.get("requested_version_matches_approved_version")
                        emitted = pre.get("export_artifact_emitted")
                        expected_ver_match = (is_approved is True) and (app_ver == req_ver)

                        if req_cid != chk.get("target_claim_id") or req_cid not in claims_by_id:
                            errors.append(ValidationErrorItem("REF07_EXPORT_PRECONDITION_VIOLATION", f"stage4_refetch_checks[{fcid}].export_precondition_input", "requested_claim_id must match target_claim_id and exist"))
                        if ver_matches != expected_ver_match:
                            errors.append(
                                ValidationErrorItem(
                                    "REF07_EXPORT_PRECONDITION_VIOLATION",
                                    f"stage4_refetch_checks[{fcid}].export_precondition_input",
                                    f"requested_version_matches_approved_version={ver_matches!r} inconsistent with claim_version_approved={is_approved!r}, requested={req_ver!r}, approved={app_ver!r}",
                                )
                            )
                        if exp7.get("requires_approved_exact_claim_version") is not True:
                            errors.append(ValidationErrorItem("REF07_EXPORT_PRECONDITION_VIOLATION", f"expected_refetch_fixtures[{fcid}].expected_ref07_assertions", "EXPORT_DOWNLOAD must set requires_approved_exact_claim_version=true"))
                        if exp7.get("expected_export_artifact_emitted") != emitted:
                            errors.append(ValidationErrorItem("REF07_EXPORT_PRECONDITION_VIOLATION", f"expected_refetch_fixtures[{fcid}].expected_ref07_assertions", "expected_export_artifact_emitted mismatch with export_precondition_input.export_artifact_emitted"))

                        refetch_passed = (
                            op.get("same_job_refetch_executed") is True
                            and chk.get("hash_recheck_matched") is True
                            and chk.get("locator_recheck_matched") is True
                        )
                        if refetch_passed and is_approved is False and app_ver is None:
                            ref07_export_unapproved_blocked_seen = True
                        if refetch_passed and app_ver is not None and req_ver != app_ver:
                            ref07_export_version_mismatch_blocked_seen = True

                        preconditions_met = (
                            is_approved is True
                            and app_ver == req_ver
                            and ver_matches is True
                            and refetch_passed
                            and op.get("permission_or_validity_changed_before_save") is False
                        )
                        if not preconditions_met:
                            if (
                                emitted is not False
                                or op.get("export_payload_snapshot") is not None
                                or exp7.get("expected_operation_allowed") is not False
                                or exp7.get("expected_export_artifact_emitted") is not False
                                or ro.get("expected_approval_blocked") is not True
                            ):
                                errors.append(
                                    ValidationErrorItem(
                                        "REF07_EXPORT_PRECONDITION_VIOLATION",
                                        f"refetch[{fcid}]",
                                        "Export MUST NOT be allowed or emit export_payload_snapshot when Claim is unapproved, version mismatches approved version, or refetch/TOCTOU fails",
                                    )
                                )
                        elif emitted is True or exp7.get("expected_operation_allowed") is True:
                            exp_snap = op.get("export_payload_snapshot")
                            vm = exp_snap.get("verification_metadata_fixture_only") if isinstance(exp_snap, dict) else None
                            base_src = sources_by_id.get(chk["baseline_source_revision_id"], {})
                            audit_checked_at = op.get("verification_audit_record", {}).get("checked_at")
                            if not (
                                isinstance(exp_snap, dict)
                                and exp_snap.get("approved_claim_id") == req_cid
                                and exp_snap.get("approved_claim_version") == app_ver
                                and exp_snap.get("raw_source_quote_included") is False
                                and isinstance(vm, dict)
                                and vm.get("layout_scope") == "TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN"
                                and vm.get("source_revision_id") == chk["baseline_source_revision_id"]
                                and vm.get("source_content_sha256") == base_src.get("content_sha256")
                                and vm.get("locator_id") == chk["baseline_locator"]["locator_id"]
                                and vm.get("span_sha256") == chk["baseline_locator"]["span_sha256"]
                                and isinstance(vm.get("checked_at"), str)
                                and len(vm["checked_at"]) >= 10
                                and vm.get("checked_at") == audit_checked_at
                            ):
                                errors.append(
                                    ValidationErrorItem(
                                        "REF07_EXPORT_PRECONDITION_VIOLATION",
                                        f"stage4_refetch_checks[{fcid}].export_payload_snapshot",
                                        "Allowed Export payload MUST bind approved_claim_id/approved_claim_version and verification metadata (source_revision_id, source_content_sha256, locator_id, span_sha256, checked_at) with non-frozen layout marker and raw_source_quote_included=false",
                                    )
                                )
                if op.get("same_job_refetch_executed") and not op.get("verification_audit_record", {}).get("checked_at"):
                    errors.append(ValidationErrorItem("REF07_OPERATION_REFETCH_POLICY_VIOLATION", f"stage4_refetch_checks[{fcid}]", "Same-job refetch must record checked_at in verification_audit_record"))

        # REF-08 limits & memory boundary checks
        if official_ref_id == "REF-08":
            lim = chk.get("limits_and_boundary_stub")
            exp8 = ro.get("expected_ref08_assertions")
            if not isinstance(lim, dict) or not isinstance(exp8, dict):
                errors.append(ValidationErrorItem("REF08_LIMITS_MEMORY_BOUNDARY_VIOLATION", f"refetch[{fcid}]", "REF-08 requires limits_and_boundary_stub and expected_ref08_assertions"))
            else:
                if lim.get("silent_truncation_occurred") is not False or lim.get("required_evidence_dropped_silently") is not False:
                    errors.append(ValidationErrorItem("REF08_LIMITS_MEMORY_BOUNDARY_VIOLATION", f"stage4_refetch_checks[{fcid}]", "Silent truncation / silent drop of required evidence is forbidden"))
                if lim.get("duplicate_units_removed_in_job", 0) >= 1:
                    ref08_dedup_seen = True
                if lim.get("transport_mode") == "SEPARATE_JOB_METADATA_ONLY_HANDOFF":
                    if not isinstance(lim.get("inter_job_handoff_payload"), dict) or lim.get("next_job_refetch_executed") is not True:
                        errors.append(ValidationErrorItem("REF08_LIMITS_MEMORY_BOUNDARY_VIOLATION", f"stage4_refetch_checks[{fcid}]", "Separate job handoff requires metadata-only payload and next_job_refetch_executed=true"))
                    else:
                        ref08_sep_job_handoff_seen = True
                breach = lim.get("limit_breached")
                if breach is not None:
                    ref08_limit_breaches_seen.add(str(breach))
                    if exp8.get("expected_operation_allowed") is not False or ro.get("expected_approval_blocked") is not True:
                        errors.append(ValidationErrorItem("REF08_LIMITS_MEMORY_BOUNDARY_VIOLATION", f"expected_refetch_fixtures[{fcid}]", "Limit breach must fail/block and never return success"))

    # Verify REF-06, REF-07, REF-08 sub-case completeness
    if ref06_timings_seen != {"BEFORE_COMMIT", "AFTER_COMMIT"} or not ref06_changed_hash_blocked_seen:
        errors.append(ValidationErrorItem("MISSING_OFFICIAL_REF_CASE_MAPPING", "REF-06", "REF-06 must cover BEFORE_COMMIT, AFTER_COMMIT, and memory-lost changed-hash block"))
    if (
        ref07_ops_seen != {"APPROVAL", "EXPORT_DOWNLOAD", "EVIDENCE_VIEW"}
        or not ref07_prior_only_blocked_seen
        or not ref07_toctou_blocked_seen
        or not ref07_export_unapproved_blocked_seen
        or not ref07_export_version_mismatch_blocked_seen
    ):
        errors.append(
            ValidationErrorItem(
                "MISSING_OFFICIAL_REF_CASE_MAPPING",
                "REF-07",
                "REF-07 must cover APPROVAL, EXPORT_DOWNLOAD, EVIDENCE_VIEW, prior-check-only block, TOCTOU block, unapproved-Claim Export block, and approved-version-mismatch Export block",
            )
        )
    if not ref08_dedup_seen or not ref08_sep_job_handoff_seen or len(ref08_limit_breaches_seen) < 2:
        errors.append(ValidationErrorItem("MISSING_OFFICIAL_REF_CASE_MAPPING", "REF-08", "REF-08 must cover same-job dedup, separate-job metadata-only handoff, and limit-breach blocks"))

    # Global Option C non-persistence & inter-stage raw-text payload leak audit
    all_raw_texts = [t for t in norm_text_by_rev.values() if len(t) >= 30]
    src02_full_span = "I implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts."

    for label, doc in (
        ("stages/stage1_content_units.json", s1_doc),
        ("stages/stage2_fact_extraction.json", s2_doc),
        ("stages/stage3_claim_assembly.json", s3_doc),
        ("stages/stage4_refetch_checks.json", s4_doc),
        ("expected/oracle_draft.json", oracle_doc),
        ("expected/refetch_oracle_draft.json", ref_oracle_doc),
        ("CANDIDATE_MANIFEST.json", manifest_doc),
    ):
        cleaned = strip_negative_leak_fixtures(doc)
        leaked_canaries = detect_canary_in_obj(cleaned, all_canary_tokens)
        if leaked_canaries:
            errors.append(ValidationErrorItem("CANARY_LEAK_DETECTED", label, f"Canary leaked outside raw fixture: {leaked_canaries}"))
        raw_key_hits = find_forbidden_raw_keys(cleaned)
        if raw_key_hits:
            errors.append(ValidationErrorItem("INTER_STAGE_RAW_TEXT_LEAK", label, f"Forbidden raw-text keys found in downstream artifact: {raw_key_hits}"))
        serialized_clean = json.dumps(cleaned, ensure_ascii=False)
        for raw_doc_text in all_raw_texts:
            if raw_doc_text in serialized_clean:
                errors.append(ValidationErrorItem("INTER_STAGE_RAW_TEXT_LEAK", label, "Full raw source document text leaked into downstream artifact"))
                break

    # Extra strict scan on inter-stage run objects, checkpoints, inter-job handoffs, and Export payloads
    strict_metadata_payloads: list[tuple[str, Any]] = [
        ("stage2.extractor_runs", s2_doc.get("extractor_runs", [])),
        ("stage2.validator_runs", s2_doc.get("validator_runs", [])),
        ("stage3.role_execution_runs", s3_doc.get("role_execution_runs", [])),
    ]
    for chk in s4_doc.get("refetch_checks", []):
        fcid = chk.get("fixture_case_id", "")
        if chk.get("crash_replay_stub"):
            strict_metadata_payloads.append((f"stage4[{fcid}].checkpoint_payload", chk["crash_replay_stub"].get("checkpoint_payload")))
        if chk.get("operation_refetch_stub") and chk["operation_refetch_stub"].get("export_payload_snapshot"):
            strict_metadata_payloads.append((f"stage4[{fcid}].export_payload_snapshot", chk["operation_refetch_stub"].get("export_payload_snapshot")))
        if chk.get("limits_and_boundary_stub") and chk["limits_and_boundary_stub"].get("inter_job_handoff_payload"):
            strict_metadata_payloads.append((f"stage4[{fcid}].inter_job_handoff_payload", chk["limits_and_boundary_stub"].get("inter_job_handoff_payload")))

    for loc_label, payload in strict_metadata_payloads:
        s_payload = json.dumps(payload, ensure_ascii=False)
        if src02_full_span in s_payload:
            errors.append(
                ValidationErrorItem(
                    "INTER_STAGE_RAW_TEXT_LEAK",
                    loc_label,
                    "Verbatim raw source excerpt leaked into metadata-only inter-stage / checkpoint / handoff / Export payload",
                )
            )

    # 8. Validate Family-Preserving Evaluation Partitions & CANDIDATE_MANIFEST.json (ADR-018)
    family_to_partitions: dict[str, set[str]] = {}
    scenario_partition_by_id: dict[str, str] = {}
    actual_partitions: dict[str, dict[str, list[str]]] = {
        p: {"family_ids": [], "scenario_ids": [], "claim_ids": [], "refetch_fixture_case_ids": []}
        for p in VALID_EVALUATION_PARTITIONS
    }

    for scn in scenarios_in_oracle:
        sid = scn.get("scenario_id", "")
        fam = scn.get("evaluation_family_id", "")
        part = scn.get("evaluation_partition", "")
        if part not in VALID_EVALUATION_PARTITIONS:
            errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", f"scenarios[{sid}]", f"Invalid partition {part!r}"))
            continue
        if fam in PRIOR_V030_FAMILY_PARTITIONS and PRIOR_V030_FAMILY_PARTITIONS[fam] != part:
            errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", f"scenarios[{sid}]", f"Existing family '{fam}' moved from {PRIOR_V030_FAMILY_PARTITIONS[fam]} to {part}"))
        scenario_partition_by_id[sid] = part
        family_to_partitions.setdefault(fam, set()).add(part)
        actual_partitions[part]["scenario_ids"].append(sid)

    for cl in s3_doc.get("claims", []):
        part = scenario_partition_by_id.get(cl.get("scenario_id", ""))
        if part:
            actual_partitions[part]["claim_ids"].append(cl.get("claim_id", ""))

    for fcid, ro in ref_oracles_by_id.items():
        fam = ro.get("evaluation_family_id", "")
        part = ro.get("evaluation_partition", "")
        if part not in VALID_EVALUATION_PARTITIONS:
            errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", f"expected_refetch_fixtures[{fcid}]", f"Invalid partition {part!r}"))
            continue
        if fam in PRIOR_V030_FAMILY_PARTITIONS and PRIOR_V030_FAMILY_PARTITIONS[fam] != part:
            errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", f"expected_refetch_fixtures[{fcid}]", f"Existing family '{fam}' moved from {PRIOR_V030_FAMILY_PARTITIONS[fam]} to {part}"))
        family_to_partitions.setdefault(fam, set()).add(part)
        actual_partitions[part]["refetch_fixture_case_ids"].append(fcid)

    for fam, parts in sorted(family_to_partitions.items()):
        if len(parts) > 1:
            errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", f"evaluation_family[{fam}]", f"Family '{fam}' leaked across partitions: {sorted(parts)}"))
        elif len(parts) == 1:
            actual_partitions[next(iter(parts))]["family_ids"].append(fam)

    source_rev_to_partitions: dict[str, set[str]] = {}
    for cu in s1_doc.get("content_units", []):
        part = scenario_partition_by_id.get(cu.get("scenario_id", ""))
        if part and cu.get("source_revision_id"):
            source_rev_to_partitions.setdefault(cu["source_revision_id"], set()).add(part)
    for fs in s2_doc.get("fact_supports", []):
        part = scenario_partition_by_id.get(fs.get("scenario_id", ""))
        if part and fs.get("source_revision_id"):
            source_rev_to_partitions.setdefault(fs["source_revision_id"], set()).add(part)
    for chk in s4_doc.get("refetch_checks", []):
        ro = ref_oracles_by_id.get(chk.get("fixture_case_id", ""))
        part = ro.get("evaluation_partition") if ro else None
        if part in VALID_EVALUATION_PARTITIONS and chk.get("baseline_source_revision_id"):
            source_rev_to_partitions.setdefault(chk["baseline_source_revision_id"], set()).add(part)

    for rev_id, parts in sorted(source_rev_to_partitions.items()):
        if len(parts) > 1:
            errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", f"source_revision[{rev_id}]", f"Source '{rev_id}' leaked across partitions: {sorted(parts)}"))

    # Validate CANDIDATE_MANIFEST.json (ADR-018 non-approval + scope + REF honesty + exact hashes)
    if (
        manifest_doc.get("candidate_status") != PENDING_ORACLE_STATUS
        or manifest_doc.get("candidate_scope") != CANDIDATE_SCOPE
        or manifest_doc.get("human_approved") is not False
        or manifest_doc.get("human_approval_recorded_by") is not None
        or manifest_doc.get("adr_018_ai_cannot_self_approve") is not True
        or manifest_doc.get("frozen_for_golden") is not False
    ):
        errors.append(
            ValidationErrorItem(
                "UNAPPROVED_VERSION_MARKED_APPROVED",
                "CANDIDATE_MANIFEST.json",
                "CANDIDATE_MANIFEST.json MUST remain PENDING_OWNER_REVIEW, human_approved=false, frozen_for_golden=false (ADR-018)",
            )
        )
    if manifest_doc.get("bundle_sha256") == PRIOR_BASELINE_BUNDLE_SHA256:
        errors.append(
            ValidationErrorItem(
                "UNAPPROVED_VERSION_MARKED_APPROVED",
                "CANDIDATE_MANIFEST.json.bundle_sha256",
                "Prior v0.3.0-synthetic-golden hash MUST NOT be copied into v0.4.0 candidate manifest",
            )
        )
    m_scope = manifest_doc.get("scope_statement", {})
    if (
        m_scope.get("live_llm_evaluation_in_scope") is not False
        or m_scope.get("production_connector_integration_in_scope") is not False
        or m_scope.get("product_e2e_verified") is not False
        or m_scope.get("synthetic_semantics_only_not_connector_evidence") is not True
    ):
        errors.append(ValidationErrorItem("APPROVAL_SCOPE_OVERCLAIM", "CANDIDATE_MANIFEST.json.scope_statement", "Candidate scope must exclude live LLM, production connectors, and product E2E"))

    m_ref = manifest_doc.get("ref_verification_status", {})
    if (
        m_ref.get("synthetic_stubs_prepared") != sorted(synthetic_stubs_refs)
        or m_ref.get("partial_static_fixtures_prepared") != sorted(partial_static_refs)
        or m_ref.get("synthetic_cases_prepared_runtime_unverified") != sorted(synthetic_cases_runtime_unverified_refs)
        or m_ref.get("product_runtime_unverified_all") != sorted(OFFICIAL_REF_REQUIREMENTS.keys())
    ):
        errors.append(ValidationErrorItem("FALSE_PRODUCT_VERIFICATION_CLAIM", "CANDIDATE_MANIFEST.json.ref_verification_status", "Manifest REF status mismatch with official_ref_matrix"))

    m_parts = manifest_doc.get("evaluation_partitions", {})
    for part_name in VALID_EVALUATION_PARTITIONS:
        expected_entry = {k: sorted(v) for k, v in actual_partitions[part_name].items()}
        recorded_raw = m_parts.get(part_name, {})
        recorded_entry = {k: sorted(recorded_raw.get(k, [])) for k in ("family_ids", "scenario_ids", "claim_ids", "refetch_fixture_case_ids")}
        if recorded_entry != expected_entry:
            errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", f"CANDIDATE_MANIFEST.json.evaluation_partitions.{part_name}", f"Partition mismatch: {recorded_entry} != {expected_entry}"))
    dev_fams = set(m_parts.get("SYNTHETIC_REGRESSION_DEV", {}).get("family_ids", []))
    test_fams = set(m_parts.get("SYNTHETIC_REGRESSION_TEST", {}).get("family_ids", []))
    if dev_fams & test_fams:
        errors.append(ValidationErrorItem("FAMILY_PARTITION_LEAKAGE", "CANDIDATE_MANIFEST.json.evaluation_partitions", f"Overlapping families: {sorted(dev_fams & test_fams)}"))

    expected_artifact_sha256: dict[str, str] = {}
    schema_disk_path = Path(__file__).resolve().parent.parent / "schemas" / "claim_pipeline.schema.json"
    if schema_disk_path.exists():
        expected_artifact_sha256["schemas/claim_pipeline.schema.json"] = (
            "sha256:" + hashlib.sha256(schema_disk_path.read_bytes()).hexdigest()
        )
    for rel_path in sorted(p for p in DATASET_FILES if p != "CANDIDATE_MANIFEST.json"):
        serialized = (json.dumps(docs[rel_path], indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        expected_artifact_sha256[rel_path] = "sha256:" + hashlib.sha256(serialized).hexdigest()

    recorded_artifact_sha256 = manifest_doc.get("artifact_sha256", {})
    if set(recorded_artifact_sha256.keys()) != set(expected_artifact_sha256.keys()):
        errors.append(ValidationErrorItem("MANIFEST_HASH_MISMATCH", "CANDIDATE_MANIFEST.json.artifact_sha256", "Manifest file key mismatch"))
    for rel_path, expected_digest in sorted(expected_artifact_sha256.items()):
        if recorded_artifact_sha256.get(rel_path) != expected_digest:
            errors.append(ValidationErrorItem("MANIFEST_HASH_MISMATCH", f"CANDIDATE_MANIFEST.json.artifact_sha256[{rel_path}]", f"Recorded {recorded_artifact_sha256.get(rel_path)} != computed {expected_digest}"))

    expected_manifest_lines = "\n".join(f"{path}  {digest}" for path, digest in sorted(expected_artifact_sha256.items())) + "\n"
    expected_bundle_sha256 = sha256_prefixed(expected_manifest_lines)
    if manifest_doc.get("bundle_sha256") != expected_bundle_sha256:
        errors.append(ValidationErrorItem("MANIFEST_HASH_MISMATCH", "CANDIDATE_MANIFEST.json.bundle_sha256", f"Recorded {manifest_doc.get('bundle_sha256')} != computed {expected_bundle_sha256}"))

    partition_counts = {
        part_name: {
            "families": len(actual_partitions[part_name]["family_ids"]),
            "scenarios": len(actual_partitions[part_name]["scenario_ids"]),
            "claims": len(actual_partitions[part_name]["claim_ids"]),
            "refetch_fixtures": len(actual_partitions[part_name]["refetch_fixture_case_ids"]),
        }
        for part_name in VALID_EVALUATION_PARTITIONS
    }

    summary = {
        "dataset_version": src_doc.get("dataset_version", "unknown"),
        "candidate_status": manifest_doc.get("candidate_status", "unknown"),
        "human_approved": manifest_doc.get("human_approved"),
        "source_revisions": len(sources_by_id),
        "content_units": len(units_by_id),
        "fact_candidates": len(candidates_by_id),
        "fact_support_validations": len(s2_doc.get("fact_support_validations", [])),
        "canonical_facts": len(facts_by_id),
        "fact_supports": len(s2_doc.get("fact_supports", [])),
        "official_fact_types_covered": sorted(fact_types_seen),
        "confirmed_events": len(events_by_id),
        "distinct_role_execution_runs": len(all_runs_by_id),
        "claims": len(claims_by_id),
        "claim_atoms": len(atoms_by_id),
        "claim_atom_supports": len(s3_doc.get("claim_atom_supports", [])),
        "official_atom_statuses_covered": sorted(atom_statuses_seen),
        "scenarios": len(scenarios_in_oracle),
        "refetch_fixtures_prepared": len(snaps_by_id),
        "partition_counts": partition_counts,
        "official_ref_synthetic_stubs": synthetic_stubs_refs,
        "official_ref_partial_static": partial_static_refs,
        "official_ref_synthetic_cases_runtime_unverified": synthetic_cases_runtime_unverified_refs,
        "product_runtime_verified_refs": [],
        "locator_roundtrips_checked": locator_checks_run,
        "canary_tokens_audited": len(all_canary_tokens),
        "bundle_sha256": expected_bundle_sha256,
        "error_count": len(errors),
    }
    return errors, summary


def main() -> int:
    base_dir = Path(__file__).resolve().parent.parent
    stale_errors = check_no_stale_approval_manifest(base_dir)
    schema, docs = load_dataset_bundle(base_dir)
    errors, summary = validate_bundle(schema, docs)
    errors = [*stale_errors, *errors]
    summary["error_count"] = len(errors)
    dataset_version = docs.get("sources/source_revisions.json", {}).get("dataset_version", "unknown")
    print(f"T-004A/B Owner-Review Candidate Dataset Validation Summary (v{dataset_version}):")
    for key, val in summary.items():
        print(f"  {key}: {val}")
    if errors:
        print(f"\nFAILED with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err.format()}")
        return 1
    print(
        "\nPASSED: All schema, locator/hash round-trip, stage-separation, role-run-ID disjointness, "
        "domain-vs-fixture label, REF-01..08 synthetic-mapping, Option C raw-text non-leakage, "
        "family-partition anti-leakage, and ADR-018 non-self-approval guards succeeded."
    )
    print(
        "NOTE: Candidate status is PENDING_OWNER_REVIEW (human_approved=false, frozen_for_golden=false). "
        "All REF-01..08 product runtime/E2E tests, live LLM evaluation, and product integration remain UNEXECUTED."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
