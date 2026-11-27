"""Structural, schema, locator/hash round-trip, anti-self-evidence, official REF-01..08 coverage-gap,
family-partition anti-leakage, approval-manifest hash, new-version user-edit re-verification,
required-atom, canary, and provisional-verdict validator.

Note:
The approved golden scope (v0.3.0-synthetic-golden) covers ONLY synthetic regression tests.
Live external LLM evaluation, external connector / HTTP refetch execution, production DB integration,
unverified REF items (REF-04/05 partial gaps, REF-06..08 coverage gaps), and finalized product
status enum / normalization specs remain explicitly outside this approval scope.
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
    "APPROVAL_MANIFEST.json": "ApprovalManifestFile",
}

APPROVED_ORACLE_STATUS = "APPROVED_SYNTHETIC_REGRESSION"
GOLDEN_SCOPE = "SYNTHETIC_REGRESSION_ONLY_NO_LIVE_LLM_NO_PROD_INTEGRATION"
VALID_EVALUATION_PARTITIONS = ("SYNTHETIC_REGRESSION_DEV", "SYNTHETIC_REGRESSION_TEST")

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
    """Load the JSON schema and all 8 dataset JSON documents into memory."""
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


def validate_bundle(
    schema: dict[str, Any],
    docs: dict[str, dict[str, Any]],
) -> tuple[list[ValidationErrorItem], dict[str, Any]]:
    """Run all schema, locator/hash, integrity, anti-self-evidence, official REF coverage-gap, and policy checks."""
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
    manifest_doc = docs["APPROVAL_MANIFEST.json"]

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

    # 4. Validate Stage 2: Canonical Facts & Fact Supports
    facts_by_id: dict[str, dict[str, Any]] = {}
    actual_fact_counts: dict[str, int] = {}
    for idx, fact in enumerate(s2_doc.get("canonical_facts", [])):
        loc_path = f"stages/stage2_fact_extraction.json.canonical_facts[{idx}]"
        fid = fact.get("canonical_fact_id", "")
        if fid in facts_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc_path, f"Duplicate canonical_fact_id '{fid}'"))
        facts_by_id[fid] = fact
        sid = fact.get("scenario_id", "")
        actual_fact_counts[sid] = actual_fact_counts.get(sid, 0) + 1

    fact_supports_by_fact: dict[str, list[dict[str, Any]]] = {fid: [] for fid in facts_by_id}
    support_roles_seen: set[str] = set()
    for idx, fs in enumerate(s2_doc.get("fact_supports", [])):
        loc_path = f"stages/stage2_fact_extraction.json.fact_supports[{idx}]"
        fid = fs.get("canonical_fact_id", "")
        rev_id = fs.get("source_revision_id", "")
        cu_id = fs.get("content_unit_id", "")
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

        if fid not in facts_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.canonical_fact_id", f"Unknown '{fid}'"))
        else:
            fact_supports_by_fact[fid].append(fs)
        if rev_id not in sources_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.source_revision_id", f"Unknown '{rev_id}'"))
            continue
        if cu_id not in units_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.content_unit_id", f"Unknown '{cu_id}'"))
            continue

        cu = units_by_id[cu_id]
        if cu.get("source_revision_id") != rev_id:
            errors.append(
                ValidationErrorItem(
                    "BROKEN_REFERENCE",
                    loc_path,
                    f"ContentUnit '{cu_id}' belongs to '{cu.get('source_revision_id')}', not '{rev_id}'",
                )
            )

        locator = fs.get("locator", {})
        locator_checks_run += 1
        extracted, rt_errors = verify_locator_roundtrip(norm_text_by_rev[rev_id], locator, context_label=loc_path)
        for code, msg in rt_errors:
            errors.append(ValidationErrorItem(code, f"{loc_path}.locator", msg))

        if extracted is not None:
            cu_loc = cu["locator"]
            if locator["char_start"] < cu_loc["char_start"] or locator["char_end"] > cu_loc["char_end"]:
                errors.append(
                    ValidationErrorItem(
                        "SPAN_CONTAINMENT_ERROR",
                        f"{loc_path}.locator",
                        f"FactSupport span [{locator['char_start']}, {locator['char_end']}) "
                        f"outside ContentUnit '{cu_id}' [{cu_loc['char_start']}, {cu_loc['char_end']})",
                    )
                )
            expected_loc_id = compute_locator_id(
                fs["fact_support_id"], locator["char_start"], locator["char_end"], locator["span_sha256"]
            )
            if locator.get("locator_id") != expected_loc_id:
                errors.append(
                    ValidationErrorItem(
                        "LOCATOR_ID_MISMATCH",
                        f"{loc_path}.locator.locator_id",
                        f"Recorded '{locator.get('locator_id')}' != computed '{expected_loc_id}'",
                    )
                )

    for sid, recorded_count in s2_doc.get("scenario_fact_counts", {}).items():
        actual_count = actual_fact_counts.get(sid, 0)
        if recorded_count != actual_count:
            errors.append(
                ValidationErrorItem(
                    "FACT_COUNT_MISMATCH",
                    f"stages/stage2_fact_extraction.json.scenario_fact_counts.{sid}",
                    f"Recorded {recorded_count} != actual {actual_count}",
                )
            )

    count_values = set(s2_doc.get("scenario_fact_counts", {}).values())
    if 0 not in count_values or 1 not in count_values or not any(v >= 3 for v in count_values):
        errors.append(
            ValidationErrorItem(
                "COVERAGE_INSUFFICIENT",
                "stages/stage2_fact_extraction.json",
                f"Must cover 0, 1, and N(>=3) facts; observed counts={sorted(count_values)}",
            )
        )
    for role in ("DIRECT", "CONTEXT", "CONTRADICTS"):
        if role not in support_roles_seen:
            errors.append(
                ValidationErrorItem(
                    "COVERAGE_INSUFFICIENT",
                    "stages/stage2_fact_extraction.json.fact_supports",
                    f"Missing FactSupport role '{role}'",
                )
            )

    # 5. Validate Stage 3: Claims, Claim Atoms, Claim Atom Supports
    claims_by_id: dict[str, dict[str, Any]] = {}
    for idx, claim in enumerate(s3_doc.get("claims", [])):
        loc_path = f"stages/stage3_claim_assembly.json.claims[{idx}]"
        cid = claim.get("claim_id", "")
        if cid in claims_by_id:
            errors.append(ValidationErrorItem("DUPLICATE_ID", loc_path, f"Duplicate claim_id '{cid}'"))
        claims_by_id[cid] = claim
        if claim.get("derived_from_claim_ids"):
            errors.append(
                ValidationErrorItem(
                    "SELF_EVIDENCE_VIOLATION",
                    f"{loc_path}.derived_from_claim_ids",
                    f"Claim '{cid}' cannot derive evidence from Claims {claim['derived_from_claim_ids']}",
                )
            )
        # User-edited claims must advance claim_version and track re-verification on new version
        if claim.get("user_edited"):
            if claim.get("claim_version", 1) < 2 or claim.get("previous_claim_version") is None:
                errors.append(
                    ValidationErrorItem(
                        "USER_EDIT_VERSIONING_ERROR",
                        loc_path,
                        f"User-edited claim '{cid}' must create a new claim_version >= 2 with previous_claim_version set",
                    )
                )
            if claim.get("reverification_state_on_new_version") == "NOT_APPLICABLE_UNEDITED":
                errors.append(
                    ValidationErrorItem(
                        "USER_EDIT_VERSIONING_ERROR",
                        loc_path,
                        f"User-edited claim '{cid}' must specify re-verification state on the new version",
                    )
                )

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
        elif aid not in claims_by_id[cid].get("claim_atom_ids", []):
            errors.append(
                ValidationErrorItem(
                    "BROKEN_REFERENCE",
                    loc_path,
                    f"Atom '{aid}' references claim '{cid}', which does not list it in claim_atom_ids",
                )
            )

    for cid, claim in claims_by_id.items():
        for aid in claim.get("claim_atom_ids", []):
            if aid not in atoms_by_id:
                errors.append(
                    ValidationErrorItem(
                        "BROKEN_REFERENCE",
                        f"stages/stage3_claim_assembly.json.claims[{cid}]",
                        f"Unknown claim_atom_id '{aid}'",
                    )
                )

    cas_by_atom: dict[str, list[dict[str, Any]]] = {aid: [] for aid in atoms_by_id}
    for idx, cas in enumerate(s3_doc.get("claim_atom_supports", [])):
        loc_path = f"stages/stage3_claim_assembly.json.claim_atom_supports[{idx}]"
        aid = cas.get("claim_atom_id", "")
        fid = cas.get("canonical_fact_id", "")
        if fid in generated_claim_namespace:
            errors.append(
                ValidationErrorItem(
                    "SELF_EVIDENCE_VIOLATION",
                    f"{loc_path}.canonical_fact_id",
                    f"ClaimAtomSupport cannot cite generated claim/atom '{fid}' as CanonicalFact",
                )
            )
            continue
        if aid not in atoms_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.claim_atom_id", f"Unknown '{aid}'"))
        else:
            cas_by_atom[aid].append(cas)
        if fid not in facts_by_id:
            errors.append(ValidationErrorItem("BROKEN_REFERENCE", f"{loc_path}.canonical_fact_id", f"Unknown '{fid}'"))

    # 6. Validate Expected Oracle (APPROVED_SYNTHETIC_REGRESSION scope, provisional verdict namespace, & strict SOURCE_VERIFIED guards)
    for doc_label, o_doc in (
        ("expected/oracle_draft.json", oracle_doc),
        ("expected/refetch_oracle_draft.json", ref_oracle_doc),
    ):
        if (
            o_doc.get("oracle_status") != APPROVED_ORACLE_STATUS
            or o_doc.get("frozen_for_golden") is not True
            or o_doc.get("golden_scope") != GOLDEN_SCOPE
        ):
            errors.append(
                ValidationErrorItem(
                    "APPROVAL_SCOPE_OVERCLAIM",
                    doc_label,
                    f"Expected oracle MUST declare oracle_status={APPROVED_ORACLE_STATUS!r}, "
                    f"frozen_for_golden=true, and golden_scope={GOLDEN_SCOPE!r} "
                    "(unqualified GOLDEN / production / live-LLM approval is forbidden)",
                )
            )
        if o_doc.get("verdict_namespace") != "DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS":
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    doc_label,
                    "Expected oracle must declare verdict_namespace='DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS'",
                )
            )

    prov = oracle_doc.get("provenance_and_freeze_preconditions", {})
    if (
        prov.get("human_approval_status") != "APPROVED_SYNTHETIC_REGRESSION_ONLY"
        or prov.get("live_llm_evaluation_in_scope") is not False
        or prov.get("production_connector_integration_in_scope") is not False
        or prov.get("synthetic_semantics_only_not_connector_evidence") is not True
    ):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_SCOPE_OVERCLAIM",
                "expected/oracle_draft.json.provenance_and_freeze_preconditions",
                "Approval scope MUST remain strictly synthetic-regression-only "
                "(live_llm_evaluation_in_scope=false, production_connector_integration_in_scope=false, "
                "synthetic_semantics_only_not_connector_evidence=true)",
            )
        )

    scenarios_in_oracle = oracle_doc.get("scenarios", [])
    if len(scenarios_in_oracle) < 12:
        errors.append(
            ValidationErrorItem(
                "COVERAGE_INSUFFICIENT",
                "expected/oracle_draft.json.scenarios",
                f"Requires >=12 scenarios, found {len(scenarios_in_oracle)}",
            )
        )
    if len(atoms_by_id) < 24:
        errors.append(
            ValidationErrorItem(
                "COVERAGE_INSUFFICIENT",
                "stages/stage3_claim_assembly.json.claim_atoms",
                f"Requires >=24 Claim Atoms, found {len(atoms_by_id)}",
            )
        )

    expected_atoms_by_id = {ea["claim_atom_id"]: ea for ea in oracle_doc.get("expected_atoms", [])}
    if set(expected_atoms_by_id.keys()) != set(atoms_by_id.keys()):
        errors.append(
            ValidationErrorItem(
                "ORACLE_ALIGNMENT_MISMATCH",
                "expected/oracle_draft.json.expected_atoms",
                "expected_atoms IDs do not match stage3 claim_atoms IDs 1-to-1",
            )
        )

    for aid, ea in expected_atoms_by_id.items():
        if not str(ea.get("draft_atom_verdict", "")).startswith("DRAFT_ATOM_"):
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_atoms[{aid}].draft_atom_verdict",
                    f"Provisional atom verdict must use DRAFT_ATOM_* namespace, got {ea.get('draft_atom_verdict')!r}",
                )
            )

    expected_claims_by_id = {ec["claim_id"]: ec for ec in oracle_doc.get("expected_claims", [])}
    if set(expected_claims_by_id.keys()) != set(claims_by_id.keys()):
        errors.append(
            ValidationErrorItem(
                "ORACLE_ALIGNMENT_MISMATCH",
                "expected/oracle_draft.json.expected_claims",
                "expected_claims IDs do not match stage3 claims IDs 1-to-1",
            )
        )

    def _required_atom_has_valid_active_direct_grounding(aid: str, claim_version: int) -> tuple[bool, str]:
        supports = [c for c in cas_by_atom.get(aid, []) if not c.get("removed_by_user")]
        if not supports:
            return False, "required atom has 0 active supports"
        if any(c.get("support_role") == "CONTRADICTS" for c in supports):
            return False, "required atom has active CONTRADICTS support"
        direct_supports = [c for c in supports if c.get("support_role") == "DIRECT"]
        if not direct_supports:
            return False, "required atom has CONTEXT-only support (no active DIRECT support)"

        # Supports must be evaluated on the current claim_version (no stale v1 carryover onto edited v2!)
        current_version_direct = [
            c for c in direct_supports if c.get("evaluated_on_claim_version") == claim_version
        ]
        if not current_version_direct:
            return False, f"required atom DIRECT support is stale (not re-verified on claim_version={claim_version})"

        valid_direct_chain = False
        for cas in current_version_direct:
            fact = facts_by_id.get(cas["canonical_fact_id"])
            if not fact or fact.get("deleted_by_user"):
                continue
            if fact.get("identity_status") != "RESOLVED":
                return False, f"supporting fact '{fact['canonical_fact_id']}' has identity_status={fact.get('identity_status')}"
            f_supports = fact_supports_by_fact.get(fact["canonical_fact_id"], [])
            if any(fs.get("support_role") == "CONTRADICTS" for fs in f_supports):
                return False, f"supporting fact '{fact['canonical_fact_id']}' has CONTRADICTS FactSupport"
            for fs in f_supports:
                if fs.get("support_role") == "DIRECT":
                    src = sources_by_id.get(fs["source_revision_id"])
                    if src and not src.get("deleted_by_user"):
                        valid_direct_chain = True
        if not valid_direct_chain:
            return False, "no active non-deleted SourceRevision -> CanonicalFact DIRECT chain"
        return True, "ok"

    for cid, ec in expected_claims_by_id.items():
        if ec.get("oracle_status") != APPROVED_ORACLE_STATUS:
            errors.append(
                ValidationErrorItem(
                    "APPROVAL_SCOPE_OVERCLAIM",
                    f"expected_claims[{cid}]",
                    f"Must be oracle_status={APPROVED_ORACLE_STATUS!r}",
                )
            )
        verdict = str(ec.get("draft_claim_verdict", ""))
        proposal = str(ec.get("product_status_proposal", ""))
        if not verdict.startswith("DRAFT_CLAIM_"):
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_claims[{cid}].draft_claim_verdict",
                    f"Provisional claim verdict must use DRAFT_CLAIM_* namespace (never raw product state), got {verdict!r}",
                )
            )
        if proposal == "SOURCE_VERIFIED":
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_claims[{cid}].product_status_proposal",
                    "DRAFT oracle must not directly assert finalized product status 'SOURCE_VERIFIED'",
                )
            )

        claim = claims_by_id.get(cid)
        if not claim:
            continue

        claims_source_verified_eligibility = (
            ec.get("source_verified_eligible") is True
            or verdict in ("SOURCE_VERIFIED", "DRAFT_CLAIM_GROUNDED_CANDIDATE")
            or proposal in ("SOURCE_VERIFIED", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW")
        )
        if claims_source_verified_eligibility:
            if claim.get("user_edited"):
                if claim.get("reverification_state_on_new_version") != "REVERIFIED_PASSED_ON_NEW_VERSION":
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"User-edited claim (v{claim.get('claim_version')}) with state "
                            f"{claim.get('reverification_state_on_new_version')!r} cannot be SOURCE_VERIFIED eligible",
                        )
                    )
            atom_ids = claim.get("claim_atom_ids", [])
            required_atoms = [aid for aid in atom_ids if atoms_by_id.get(aid, {}).get("is_required_atom")]
            if not required_atoms:
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SOURCE_VERIFIED",
                        f"expected_claims[{cid}]",
                        "Claim with 0 required atoms cannot be SOURCE_VERIFIED eligible",
                    )
                )
            for aid in required_atoms:
                ea = expected_atoms_by_id.get(aid)
                if ea and ea.get("draft_atom_verdict") not in (
                    "DRAFT_ATOM_SUPPORTED_DIRECT",
                    "DRAFT_ATOM_SUPPORTED_DIRECT_WITH_CONTEXT",
                ):
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"Required atom '{aid}' has verdict '{ea.get('draft_atom_verdict')}', blocking SOURCE_VERIFIED",
                        )
                    )
                ok, reason = _required_atom_has_valid_active_direct_grounding(aid, claim.get("claim_version", 1))
                if not ok:
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"Required atom '{aid}' fails grounding invariant ({reason}); SOURCE_VERIFIED forbidden",
                        )
                    )

        # OD-003 guard: any Claim depending on a user-deleted SourceRevision or CanonicalFact
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
                errors.append(
                    ValidationErrorItem(
                        "OD003_DELETION_POLICY_VIOLATION",
                        f"expected_claims[{cid}].draft_claim_verdict",
                        "OD-003 forbids leaving a user-deletion-dependent Claim as PARTIALLY_GROUNDED after cleanup (even when mixed with surviving sources)",
                    )
                )
            if verdict != "DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003":
                errors.append(
                    ValidationErrorItem(
                        "OD003_DELETION_POLICY_VIOLATION",
                        f"expected_claims[{cid}].draft_claim_verdict",
                        f"OD-003 dependent Claim must have verdict 'DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003', got {verdict!r}",
                    )
                )
            for aid in claim.get("claim_atom_ids", []):
                ea = expected_atoms_by_id.get(aid)
                if ea and ea.get("draft_atom_verdict") != "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003":
                    errors.append(
                        ValidationErrorItem(
                            "OD003_DELETION_POLICY_VIOLATION",
                            f"expected_atoms[{aid}].draft_atom_verdict",
                            f"Atom belonging to OD-003 purged Claim '{cid}' must be 'DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003', got {ea.get('draft_atom_verdict')!r}",
                        )
                    )
            dl = claim.get("deletion_lifecycle")
            if not isinstance(dl, dict):
                errors.append(
                    ValidationErrorItem(
                        "OD003_DELETION_POLICY_VIOLATION",
                        f"stages/stage3_claim_assembly.json.claims[{cid}].deletion_lifecycle",
                        "Dependent Claim on user-deleted source must declare OD-003 deletion_lifecycle",
                    )
                )
            else:
                if not (
                    dl.get("deletion_preview_shown") is True
                    and dl.get("explicit_user_confirmation") is True
                    and dl.get("immediate_access_blocked_after_confirmation") is True
                    and dl.get("cleanup_completed") is True
                    and dl.get("distinguished_from_refetch_failure_ref03") is True
                ):
                    errors.append(
                        ValidationErrorItem(
                            "OD003_DELETION_POLICY_VIOLATION",
                            f"stages/stage3_claim_assembly.json.claims[{cid}].deletion_lifecycle",
                            "OD-003 requires deletion_preview_shown, explicit_user_confirmation, immediate_access_blocked_after_confirmation, cleanup_completed, and REF-03 distinction",
                        )
                    )
                if (
                    dl.get("post_cleanup_retained_claim_versions") != []
                    or dl.get("post_cleanup_retained_edited_copies") != 0
                    or dl.get("post_cleanup_retained_approved_copies") != 0
                ):
                    errors.append(
                        ValidationErrorItem(
                            "OD003_DELETION_POLICY_VIOLATION",
                            f"stages/stage3_claim_assembly.json.claims[{cid}].deletion_lifecycle",
                            "OD-003 requires 0 retained Claim versions, edited copies, and approved copies on server after cleanup",
                        )
                    )
                for pres_rev in dl.get("independent_source_revisions_preserved", []):
                    src_pres = sources_by_id.get(pres_rev)
                    if not src_pres or src_pres.get("deleted_by_user") is not False:
                        errors.append(
                            ValidationErrorItem(
                                "OD003_DELETION_POLICY_VIOLATION",
                                f"stages/stage3_claim_assembly.json.claims[{cid}].deletion_lifecycle.independent_source_revisions_preserved",
                                f"Independent source '{pres_rev}' must exist and remain preserved (deleted_by_user=false)",
                            )
                        )
                for pres_fact in dl.get("independent_facts_preserved", []):
                    fact_pres = facts_by_id.get(pres_fact)
                    if not fact_pres or fact_pres.get("deleted_by_user") is not False:
                        errors.append(
                            ValidationErrorItem(
                                "OD003_DELETION_POLICY_VIOLATION",
                                f"stages/stage3_claim_assembly.json.claims[{cid}].deletion_lifecycle.independent_facts_preserved",
                                f"Independent fact '{pres_fact}' must exist and remain preserved (deleted_by_user=false)",
                            )
                        )
            od_exp = ec.get("od003_expected_post_cleanup")
            if not isinstance(od_exp, dict):
                errors.append(
                    ValidationErrorItem(
                        "OD003_DELETION_POLICY_VIOLATION",
                        f"expected_claims[{cid}].od003_expected_post_cleanup",
                        "OD-003 dependent Claim oracle must specify od003_expected_post_cleanup assertions",
                    )
                )
            else:
                if (
                    od_exp.get("immediate_access_blocked_after_confirmation") is not True
                    or od_exp.get("claim_and_server_copies_purged") is not True
                    or od_exp.get("retained_claim_versions") != []
                    or od_exp.get("retained_edited_copies") != 0
                    or od_exp.get("retained_approved_copies") != 0
                    or od_exp.get("distinguished_from_ref03_refetch_failure") is not True
                    or od_exp.get("partially_grounded_forbidden_post_deletion") is not True
                ):
                    errors.append(
                        ValidationErrorItem(
                            "OD003_DELETION_POLICY_VIOLATION",
                            f"expected_claims[{cid}].od003_expected_post_cleanup",
                            "OD-003 oracle must assert immediate block, full purge (0 versions/edits/approvals), REF-03 distinction, and PARTIALLY_GROUNDED prohibition",
                        )
                    )

    # 7. Validate Official REF-01..08 Matrix & Coverage Gaps + All Preserved Refetch Fixtures
    snaps_by_id = {r["fixture_case_id"]: r for r in ref_snap_doc.get("refetch_snapshots", [])}
    checks_by_id = {r["fixture_case_id"]: r for r in s4_doc.get("refetch_checks", [])}
    ref_oracles_by_id = {r["fixture_case_id"]: r for r in ref_oracle_doc.get("expected_refetch_fixtures", [])}

    if set(snaps_by_id.keys()) != set(checks_by_id.keys()) or set(snaps_by_id.keys()) != set(ref_oracles_by_id.keys()):
        errors.append(
            ValidationErrorItem(
                "ORACLE_ALIGNMENT_MISMATCH",
                "expected/refetch_oracle_draft.json.expected_refetch_fixtures",
                "Refetch fixture_case_ids do not align 1-to-1 across snapshots, stage4 checks, and refetch oracle",
            )
        )

    matrix_rows = ref_oracle_doc.get("official_ref_matrix", [])
    matrix_by_ref = {row["official_ref_id"]: row for row in matrix_rows}
    if set(matrix_by_ref.keys()) != set(OFFICIAL_REF_REQUIREMENTS.keys()):
        errors.append(
            ValidationErrorItem(
                "OFFICIAL_REF_MAPPING_ERROR",
                "expected/refetch_oracle_draft.json.official_ref_matrix",
                f"official_ref_matrix must cover exact REF-01..REF-08, got {sorted(matrix_by_ref.keys())}",
            )
        )

    coverage_gap_refs: list[str] = []
    partial_gap_refs: list[str] = []
    covered_stub_refs: list[str] = []
    linked_from_matrix: set[str] = set()

    for official_ref_id, expected_label in OFFICIAL_REF_REQUIREMENTS.items():
        row = matrix_by_ref.get(official_ref_id)
        if not row:
            continue
        loc = f"expected/refetch_oracle_draft.json.official_ref_matrix[{official_ref_id}]"
        if row.get("official_requirement_label") != expected_label:
            errors.append(
                ValidationErrorItem(
                    "OFFICIAL_REF_MAPPING_ERROR",
                    loc,
                    f"Label mismatch: expected {expected_label!r}, got {row.get('official_requirement_label')!r}",
                )
            )
        linked = row.get("linked_fixture_case_ids", [])
        cov_status = row.get("coverage_status")
        verified = row.get("verified_in_draft")
        row_oracle_status = row.get("oracle_status")
        gap_details = row.get("coverage_gap_details", [])

        # Anti-fake-coverage guard: unverified REF-06..08 (0 fixtures) and partially unverified REF-04..05
        # MUST NOT be marked verified/passed.
        if not linked:
            if (
                cov_status != "COVERAGE_GAP"
                or verified is not False
                or row_oracle_status != "UNVERIFIED_COVERAGE_GAP"
                or not gap_details
            ):
                errors.append(
                    ValidationErrorItem(
                        "FALSE_COVERAGE_CLAIM",
                        loc,
                        f"{official_ref_id} has 0 linked fixtures and MUST be coverage_status='COVERAGE_GAP', "
                        "verified_in_draft=false, oracle_status='UNVERIFIED_COVERAGE_GAP', with non-empty coverage_gap_details",
                    )
                )
        elif official_ref_id in ("REF-04", "REF-05") or cov_status == "PARTIAL_COVERAGE_GAP":
            if (
                cov_status != "PARTIAL_COVERAGE_GAP"
                or verified is not False
                or row_oracle_status != "PARTIAL_UNVERIFIED_COVERAGE_GAP"
                or not gap_details
            ):
                errors.append(
                    ValidationErrorItem(
                        "FALSE_COVERAGE_CLAIM",
                        loc,
                        f"{official_ref_id} has unverified runtime gaps and MUST remain coverage_status='PARTIAL_COVERAGE_GAP', "
                        "verified_in_draft=false, oracle_status='PARTIAL_UNVERIFIED_COVERAGE_GAP', with non-empty coverage_gap_details",
                    )
                )
        else:
            if cov_status == "COVERAGE_GAP" and verified is True:
                errors.append(
                    ValidationErrorItem(
                        "FALSE_COVERAGE_CLAIM",
                        loc,
                        f"{official_ref_id} marked COVERAGE_GAP cannot set verified_in_draft=true",
                    )
                )
            if cov_status == "COVERED_SYNTHETIC_STUB" and row_oracle_status != APPROVED_ORACLE_STATUS:
                errors.append(
                    ValidationErrorItem(
                        "APPROVAL_SCOPE_OVERCLAIM",
                        loc,
                        f"{official_ref_id} covered synthetic stub row must set oracle_status={APPROVED_ORACLE_STATUS!r}",
                    )
                )

        if cov_status == "COVERAGE_GAP":
            coverage_gap_refs.append(official_ref_id)
        elif cov_status == "PARTIAL_COVERAGE_GAP":
            partial_gap_refs.append(official_ref_id)
        elif cov_status == "COVERED_SYNTHETIC_STUB":
            covered_stub_refs.append(official_ref_id)

        for fcid in linked:
            linked_from_matrix.add(fcid)
            if fcid not in snaps_by_id:
                errors.append(ValidationErrorItem("BROKEN_REFERENCE", loc, f"Unknown fixture_case_id '{fcid}'"))
            elif snaps_by_id[fcid].get("official_ref_id") != official_ref_id:
                errors.append(
                    ValidationErrorItem(
                        "OFFICIAL_REF_MAPPING_ERROR",
                        loc,
                        f"Fixture '{fcid}' has official_ref_id={snaps_by_id[fcid].get('official_ref_id')!r}, not {official_ref_id!r}",
                    )
                )

    unlinked_fixtures = sorted(set(snaps_by_id.keys()) - linked_from_matrix)
    if unlinked_fixtures:
        errors.append(
            ValidationErrorItem(
                "OFFICIAL_REF_MAPPING_ERROR",
                "expected/refetch_oracle_draft.json.official_ref_matrix",
                f"Fixtures not linked in official_ref_matrix: {unlinked_fixtures}",
            )
        )

    all_canary_tokens: list[str] = []
    for snap in snaps_by_id.values():
        all_canary_tokens.extend(snap.get("canary_tokens", []))

    for fcid in sorted(snaps_by_id.keys()):
        snap = snaps_by_id[fcid]
        chk = checks_by_id.get(fcid)
        ro = ref_oracles_by_id.get(fcid)
        if not (chk and ro):
            continue

        official_ref_id = snap["official_ref_id"]
        if chk["official_ref_id"] != official_ref_id or ro["official_ref_id"] != official_ref_id:
            errors.append(
                ValidationErrorItem(
                    "OFFICIAL_REF_MAPPING_ERROR",
                    f"refetch_fixture[{fcid}]",
                    "official_ref_id mismatch across snapshot, stage4 check, and refetch oracle",
                )
            )
        if not str(ro.get("draft_refetch_verdict", "")).startswith("DRAFT_REFETCH_"):
            errors.append(
                ValidationErrorItem(
                    "VERDICT_NAMESPACE_CONFLATION",
                    f"expected_refetch_fixtures[{fcid}].draft_refetch_verdict",
                    f"Must use DRAFT_REFETCH_* provisional namespace, got {ro.get('draft_refetch_verdict')!r}",
                )
            )

        # URL safety check
        urls_to_check = [snap["target_url"], *snap.get("additional_unsafe_urls", [])]
        primary_safe, primary_reason = check_url_safety(snap["target_url"])
        if not primary_safe:
            if snap.get("url_safety_precheck") != "BLOCKED_UNSAFE_URL" or snap.get("fetch_attempted") is not False:
                errors.append(
                    ValidationErrorItem(
                        "UNSAFE_URL_POLICY_VIOLATION",
                        f"sources/refetch_snapshots.json[{fcid}]",
                        f"Unsafe URL '{snap['target_url']}' ({primary_reason}) MUST be blocked with fetch_attempted=false",
                    )
                )
            if ro.get("expected_fetch_attempted") is not False:
                errors.append(
                    ValidationErrorItem(
                        "UNSAFE_URL_POLICY_VIOLATION",
                        f"expected_refetch_fixtures[{fcid}]",
                        f"Unsafe URL '{snap['target_url']}' oracle MUST expect fetch_attempted=false",
                    )
                )
            for extra_url in urls_to_check[1:]:
                extra_safe, _ = check_url_safety(extra_url)
                if extra_safe:
                    errors.append(
                        ValidationErrorItem(
                            "UNSAFE_URL_POLICY_VIOLATION",
                            f"sources/refetch_snapshots.json[{fcid}].additional_unsafe_urls",
                            f"Expected unsafe test vector URL, got safe URL '{extra_url}'",
                        )
                    )

        # Baseline locator round-trip
        base_rev = chk["baseline_source_revision_id"]
        if base_rev not in sources_by_id:
            errors.append(
                ValidationErrorItem("BROKEN_REFERENCE", f"stage4_refetch_checks[{fcid}]", f"Unknown '{base_rev}'")
            )
        else:
            locator_checks_run += 1
            _, rt_errors = verify_locator_roundtrip(
                norm_text_by_rev[base_rev], chk["baseline_locator"], context_label=f"stage4_refetch_checks[{fcid}]"
            )
            for code, msg in rt_errors:
                errors.append(ValidationErrorItem(code, f"stage4_refetch_checks[{fcid}].baseline_locator", msg))

            ret_body = snap.get("returned_body_fixture_only")
            if ret_body is not None:
                ret_norm = normalize_text(ret_body)
                calc_ret_hash = sha256_prefixed(ret_norm)
                if snap.get("returned_content_sha256") != calc_ret_hash:
                    errors.append(
                        ValidationErrorItem(
                            "HASH_MISMATCH",
                            f"refetch_snapshots[{fcid}].returned_content_sha256",
                            f"Recorded {snap.get('returned_content_sha256')} != computed {calc_ret_hash}",
                        )
                    )
                actual_hash_match = calc_ret_hash == sources_by_id[base_rev]["content_sha256"]
                if chk.get("hash_recheck_matched") is not None and chk["hash_recheck_matched"] != actual_hash_match:
                    errors.append(
                        ValidationErrorItem(
                            "HASH_MISMATCH",
                            f"stage4_refetch_checks[{fcid}].hash_recheck_matched",
                            f"Recorded {chk['hash_recheck_matched']} != actual {actual_hash_match}",
                        )
                    )
                if not snap.get("is_partial_response"):
                    locator_to_recheck = chk.get("tested_locator") or chk["baseline_locator"]
                    _, ref_rt_errs = verify_locator_roundtrip(
                        ret_norm, locator_to_recheck, context_label=f"refetch_recheck[{fcid}]"
                    )
                    actual_loc_match = len(ref_rt_errs) == 0
                    if chk.get("locator_recheck_matched") != actual_loc_match:
                        errors.append(
                            ValidationErrorItem(
                                "OFFSET_OR_ROUNDTRIP_MISMATCH",
                                f"stage4_refetch_checks[{fcid}].locator_recheck_matched",
                                f"Recorded {chk['locator_recheck_matched']} != actual {actual_loc_match}",
                            )
                        )

        if chk.get("tested_locator") is not None:
            if (
                chk.get("hash_recheck_matched") is not True
                or chk.get("locator_recheck_matched") is not False
                or ro.get("expected_approval_blocked") is not True
            ):
                errors.append(
                    ValidationErrorItem(
                        "LOCATOR_ONLY_MISMATCH_POLICY_VIOLATION",
                        f"stage4_refetch_checks[{fcid}].tested_locator",
                        "Same-hash locator-only mismatch fixture must have hash_recheck_matched=true, "
                        "locator_recheck_matched=false, and expected_approval_blocked=true",
                    )
                )

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
                errors.append(
                    ValidationErrorItem(
                        "TRANSIENT_RETRY_POLICY_VIOLATION",
                        f"stage4_refetch_checks[{fcid}].retry_policy_stub",
                        "Transient failure fixture must enforce bounded retry (>=1), block approval while unavailable, "
                        "and distinguish transient failures from HTTP 404",
                    )
                )

        if chk["target_claim_id"] not in claims_by_id:
            errors.append(
                ValidationErrorItem(
                    "BROKEN_REFERENCE",
                    f"stage4_refetch_checks[{fcid}].target_claim_id",
                    f"Unknown claim '{chk['target_claim_id']}'",
                )
            )

        # REF-03 / unavailable / mismatch / policy denial: MUST NOT conflate with confirmed deletion or historical falsity
        if official_ref_id in ("REF-02", "REF-03", "REF-05"):
            if (
                ro.get("expected_unverifiable_now") is not True
                or ro.get("expected_source_deleted_confirmed") is not False
                or ro.get("expected_historical_fact_falsified") is not False
            ):
                errors.append(
                    ValidationErrorItem(
                        "UNVERIFIABLE_VS_DELETED_OR_FALSIFIED_CONFLATION",
                        f"expected_refetch_fixtures[{fcid}]",
                        f"{official_ref_id} fixture '{fcid}' MUST set expected_unverifiable_now=true, "
                        "expected_source_deleted_confirmed=false, and expected_historical_fact_falsified=false",
                    )
                )
            if ro.get("expected_approval_blocked") is not True:
                errors.append(
                    ValidationErrorItem(
                        "REFETCH_APPROVAL_BLOCK_VIOLATION",
                        f"expected_refetch_fixtures[{fcid}].expected_approval_blocked",
                        f"{official_ref_id} fixture '{fcid}' MUST set expected_approval_blocked=true",
                    )
                )

        # Post-MVP auto-mutation guard
        if ro.get("expected_claim_auto_mutated") is not False:
            errors.append(
                ValidationErrorItem(
                    "POST_MVP_VIOLATION",
                    f"expected_refetch_fixtures[{fcid}]",
                    "Automatic Claim mutation after refetch is Post-MVP and must remain false in MVP fixtures",
                )
            )

        # REF-04 canary non-persistence check
        leaked_in_clean = detect_canary_in_obj(chk.get("persisted_payload_snapshot", {}), all_canary_tokens)
        if leaked_in_clean:
            errors.append(
                ValidationErrorItem(
                    "CANARY_LEAK_DETECTED",
                    f"stage4_refetch_checks[{fcid}].persisted_payload_snapshot",
                    f"Raw source canary leaked into persisted payload snapshot: {leaked_in_clean}",
                )
            )
        if official_ref_id == "REF-04":
            neg_fixture = chk.get("negative_leaky_payload_fixture")
            leaked_in_neg = detect_canary_in_obj(neg_fixture, all_canary_tokens)
            if not leaked_in_neg:
                errors.append(
                    ValidationErrorItem(
                        "CANARY_NEGATIVE_FIXTURE_INVALID",
                        f"stage4_refetch_checks[{fcid}].negative_leaky_payload_fixture",
                        "REF-04 negative_leaky_payload_fixture must contain a canary token to verify detector sensitivity",
                    )
                )

    # Global Option C non-persistence audit across persisted stage files, oracle files, & approval manifest
    for label, doc in (
        ("stages/stage1_content_units.json", s1_doc),
        ("stages/stage2_fact_extraction.json", s2_doc),
        ("stages/stage3_claim_assembly.json", s3_doc),
        ("expected/oracle_draft.json", oracle_doc),
        ("expected/refetch_oracle_draft.json", ref_oracle_doc),
        ("APPROVAL_MANIFEST.json", manifest_doc),
    ):
        leaked = detect_canary_in_obj(doc, all_canary_tokens)
        if leaked:
            errors.append(
                ValidationErrorItem(
                    "CANARY_LEAK_DETECTED",
                    label,
                    f"Canary token leaked outside synthetic raw source fixture: {leaked}",
                )
            )

    # 8. Validate Family-Preserving Evaluation Partitions (anti-leakage) & APPROVAL_MANIFEST.json integrity
    family_to_partitions: dict[str, set[str]] = {}
    scenario_partition_by_id: dict[str, str] = {}
    actual_partitions: dict[str, dict[str, list[str]]] = {
        p: {
            "family_ids": [],
            "scenario_ids": [],
            "claim_ids": [],
            "refetch_fixture_case_ids": [],
        }
        for p in VALID_EVALUATION_PARTITIONS
    }

    for scn in scenarios_in_oracle:
        sid = scn.get("scenario_id", "")
        fam = scn.get("evaluation_family_id", "")
        part = scn.get("evaluation_partition", "")
        if part not in VALID_EVALUATION_PARTITIONS:
            errors.append(
                ValidationErrorItem(
                    "FAMILY_PARTITION_LEAKAGE",
                    f"expected/oracle_draft.json.scenarios[{sid}].evaluation_partition",
                    f"Invalid evaluation_partition {part!r}; expected one of {VALID_EVALUATION_PARTITIONS}",
                )
            )
            continue
        scenario_partition_by_id[sid] = part
        family_to_partitions.setdefault(fam, set()).add(part)
        actual_partitions[part]["scenario_ids"].append(sid)

    for cl in s3_doc.get("claims", []):
        cid = cl.get("claim_id", "")
        part = scenario_partition_by_id.get(cl.get("scenario_id", ""))
        if part:
            actual_partitions[part]["claim_ids"].append(cid)

    for fcid, ro in ref_oracles_by_id.items():
        if ro.get("oracle_status") != APPROVED_ORACLE_STATUS:
            errors.append(
                ValidationErrorItem(
                    "APPROVAL_SCOPE_OVERCLAIM",
                    f"expected_refetch_fixtures[{fcid}].oracle_status",
                    f"Must set oracle_status={APPROVED_ORACLE_STATUS!r}",
                )
            )
        fam = ro.get("evaluation_family_id", "")
        part = ro.get("evaluation_partition", "")
        if part not in VALID_EVALUATION_PARTITIONS:
            errors.append(
                ValidationErrorItem(
                    "FAMILY_PARTITION_LEAKAGE",
                    f"expected_refetch_fixtures[{fcid}].evaluation_partition",
                    f"Invalid evaluation_partition {part!r}; expected one of {VALID_EVALUATION_PARTITIONS}",
                )
            )
            continue
        family_to_partitions.setdefault(fam, set()).add(part)
        actual_partitions[part]["refetch_fixture_case_ids"].append(fcid)

    for fam, parts in sorted(family_to_partitions.items()):
        if len(parts) > 1:
            errors.append(
                ValidationErrorItem(
                    "FAMILY_PARTITION_LEAKAGE",
                    f"evaluation_family[{fam}]",
                    f"Cases/edits/refetch fixtures from family '{fam}' leaked across multiple partitions: {sorted(parts)}",
                )
            )
        elif len(parts) == 1:
            only_part = next(iter(parts))
            actual_partitions[only_part]["family_ids"].append(fam)

    # Verify source-revision-level anti-leakage (same raw source revision cannot appear in both DEV and TEST)
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
            errors.append(
                ValidationErrorItem(
                    "FAMILY_PARTITION_LEAKAGE",
                    f"source_revision[{rev_id}]",
                    f"Same source_revision_id '{rev_id}' leaked across multiple evaluation partitions: {sorted(parts)}",
                )
            )

    # Validate APPROVAL_MANIFEST.json scope, unverified REF list, partitions, and exact SHA-256 digests
    if (
        manifest_doc.get("approval_status") != "APPROVED_SYNTHETIC_REGRESSION_GOLDEN"
        or manifest_doc.get("golden_scope") != GOLDEN_SCOPE
    ):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_SCOPE_OVERCLAIM",
                "APPROVAL_MANIFEST.json",
                f"Manifest must declare approval_status='APPROVED_SYNTHETIC_REGRESSION_GOLDEN' and golden_scope={GOLDEN_SCOPE!r}",
            )
        )
    m_scope = manifest_doc.get("approval_scope", {})
    if (
        m_scope.get("live_llm_evaluation_in_scope") is not False
        or m_scope.get("production_connector_integration_in_scope") is not False
        or m_scope.get("synthetic_semantics_only_not_connector_evidence") is not True
    ):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_SCOPE_OVERCLAIM",
                "APPROVAL_MANIFEST.json.approval_scope",
                "Manifest approval_scope MUST exclude live LLM evaluation and production connector integration",
            )
        )
    m_unverified = manifest_doc.get("unverified_ref_items_not_passed", {})
    if m_unverified.get("partial_coverage_gap") != sorted(partial_gap_refs) or m_unverified.get(
        "coverage_gap"
    ) != sorted(coverage_gap_refs):
        errors.append(
            ValidationErrorItem(
                "FALSE_COVERAGE_CLAIM",
                "APPROVAL_MANIFEST.json.unverified_ref_items_not_passed",
                f"Manifest unverified_ref_items_not_passed mismatch: expected partial={sorted(partial_gap_refs)}, "
                f"coverage_gap={sorted(coverage_gap_refs)}, got {m_unverified!r}",
            )
        )

    m_parts = manifest_doc.get("evaluation_partitions", {})
    for part_name in VALID_EVALUATION_PARTITIONS:
        expected_entry = {k: sorted(v) for k, v in actual_partitions[part_name].items()}
        recorded_raw = m_parts.get(part_name, {})
        recorded_entry = {
            k: sorted(recorded_raw.get(k, []))
            for k in ("family_ids", "scenario_ids", "claim_ids", "refetch_fixture_case_ids")
        }
        if recorded_entry != expected_entry:
            errors.append(
                ValidationErrorItem(
                    "FAMILY_PARTITION_LEAKAGE",
                    f"APPROVAL_MANIFEST.json.evaluation_partitions.{part_name}",
                    f"Partition manifest mismatch: recorded {recorded_entry} != actual {expected_entry}",
                )
            )
    dev_fams = set(m_parts.get("SYNTHETIC_REGRESSION_DEV", {}).get("family_ids", []))
    test_fams = set(m_parts.get("SYNTHETIC_REGRESSION_TEST", {}).get("family_ids", []))
    if dev_fams & test_fams:
        errors.append(
            ValidationErrorItem(
                "FAMILY_PARTITION_LEAKAGE",
                "APPROVAL_MANIFEST.json.evaluation_partitions",
                f"Overlapping family_ids across DEV and TEST partitions: {sorted(dev_fams & test_fams)}",
            )
        )

    expected_artifact_sha256: dict[str, str] = {}
    schema_disk_path = Path(__file__).resolve().parent.parent / "schemas" / "claim_pipeline.schema.json"
    if schema_disk_path.exists():
        expected_artifact_sha256["schemas/claim_pipeline.schema.json"] = (
            "sha256:" + hashlib.sha256(schema_disk_path.read_bytes()).hexdigest()
        )
    for rel_path in sorted(p for p in DATASET_FILES if p != "APPROVAL_MANIFEST.json"):
        serialized = (json.dumps(docs[rel_path], indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        expected_artifact_sha256[rel_path] = "sha256:" + hashlib.sha256(serialized).hexdigest()

    recorded_artifact_sha256 = manifest_doc.get("artifact_sha256", {})
    if set(recorded_artifact_sha256.keys()) != set(expected_artifact_sha256.keys()):
        errors.append(
            ValidationErrorItem(
                "MANIFEST_HASH_MISMATCH",
                "APPROVAL_MANIFEST.json.artifact_sha256",
                f"Manifest file keys {sorted(recorded_artifact_sha256.keys())} != expected {sorted(expected_artifact_sha256.keys())}",
            )
        )
    for rel_path, expected_digest in sorted(expected_artifact_sha256.items()):
        rec_digest = recorded_artifact_sha256.get(rel_path)
        if rec_digest != expected_digest:
            errors.append(
                ValidationErrorItem(
                    "MANIFEST_HASH_MISMATCH",
                    f"APPROVAL_MANIFEST.json.artifact_sha256[{rel_path}]",
                    f"Recorded {rec_digest} != computed {expected_digest}",
                )
            )

    expected_manifest_lines = (
        "\n".join(f"{path}  {digest}" for path, digest in sorted(expected_artifact_sha256.items())) + "\n"
    )
    expected_bundle_sha256 = sha256_prefixed(expected_manifest_lines)
    if manifest_doc.get("bundle_sha256") != expected_bundle_sha256:
        errors.append(
            ValidationErrorItem(
                "MANIFEST_HASH_MISMATCH",
                "APPROVAL_MANIFEST.json.bundle_sha256",
                f"Recorded {manifest_doc.get('bundle_sha256')} != computed {expected_bundle_sha256}",
            )
        )

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
        "approval_status": manifest_doc.get("approval_status", "unknown"),
        "source_revisions": len(sources_by_id),
        "content_units": len(units_by_id),
        "canonical_facts": len(facts_by_id),
        "fact_supports": len(s2_doc.get("fact_supports", [])),
        "claims": len(claims_by_id),
        "claim_atoms": len(atoms_by_id),
        "claim_atom_supports": len(s3_doc.get("claim_atom_supports", [])),
        "scenarios": len(scenarios_in_oracle),
        "refetch_fixtures_preserved": len(snaps_by_id),
        "partition_counts": partition_counts,
        "official_ref_covered_stub": covered_stub_refs,
        "official_ref_partial_coverage_gap": partial_gap_refs,
        "official_ref_coverage_gap": coverage_gap_refs,
        "locator_roundtrips_checked": locator_checks_run,
        "canary_tokens_audited": len(all_canary_tokens),
        "bundle_sha256": expected_bundle_sha256,
        "error_count": len(errors),
    }
    return errors, summary


def main() -> int:
    base_dir = Path(__file__).resolve().parent.parent
    schema, docs = load_dataset_bundle(base_dir)
    errors, summary = validate_bundle(schema, docs)
    dataset_version = docs.get("sources/source_revisions.json", {}).get("dataset_version", "unknown")
    print(f"T-004A/B Claim Synthetic Regression Golden Validation Summary (v{dataset_version}):")
    for key, val in summary.items():
        print(f"  {key}: {val}")
    if errors:
        print(f"\nFAILED with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err.format()}")
        return 1
    print(
        "\nPASSED: All schema, locator/hash round-trip, anti-self-evidence, official REF coverage-gap, "
        "family-partition anti-leakage, approval-manifest hash, canary, and synthetic-regression-golden guards succeeded."
    )
    print(
        "NOTE: Approved scope is strictly SYNTHETIC_REGRESSION_ONLY (no live LLM, no production connector integration; "
        "unverified REF-04/05 partial gaps and REF-06..08 coverage gaps remain NOT passed)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
