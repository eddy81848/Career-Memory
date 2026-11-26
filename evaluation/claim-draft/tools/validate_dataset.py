"""Structural, schema, locator/hash round-trip, anti-self-evidence, canary, and policy validator.

Note:
Structural validation is strictly separate from human semantic ground-truth approval (T-004B).
Passing this validator certifies structural integrity, locator/hash round-trip accuracy,
stage separation, and safety invariants — NOT golden semantic sign-off.
"""

from __future__ import annotations

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
    """Run all schema, locator/hash, integrity, anti-self-evidence, canary, and policy checks."""
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

    # Check 0 / 1 / N fact coverage & DIRECT/CONTEXT/CONTRADICTS coverage
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

    # 6. Validate Expected Oracle (DRAFT & Grounding Invariants)
    for doc_label, o_doc in (
        ("expected/oracle_draft.json", oracle_doc),
        ("expected/refetch_oracle_draft.json", ref_oracle_doc),
    ):
        if o_doc.get("oracle_status") != "DRAFT" or o_doc.get("frozen_for_golden") is not False:
            errors.append(
                ValidationErrorItem(
                    "PREMATURE_GOLDEN_FREEZE",
                    doc_label,
                    "Expected oracle MUST remain oracle_status='DRAFT' and frozen_for_golden=false before T-004B review",
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

    expected_claims_by_id = {ec["claim_id"]: ec for ec in oracle_doc.get("expected_claims", [])}
    if set(expected_claims_by_id.keys()) != set(claims_by_id.keys()):
        errors.append(
            ValidationErrorItem(
                "ORACLE_ALIGNMENT_MISMATCH",
                "expected/oracle_draft.json.expected_claims",
                "expected_claims IDs do not match stage3 claims IDs 1-to-1",
            )
        )

    # Critical guard: NEVER allow SOURCE_VERIFIED on edited / unsupported / partial / conflicting / ambiguous / deleted claims
    def _atom_has_valid_active_direct_grounding(aid: str) -> tuple[bool, str]:
        atom = atoms_by_id[aid]
        if atom.get("user_edited"):
            return False, "atom is user_edited"
        supports = [c for c in cas_by_atom.get(aid, []) if not c.get("removed_by_user")]
        if not supports:
            return False, "atom has 0 active supports"
        if any(c.get("support_role") == "CONTRADICTS" for c in supports):
            return False, "atom has active CONTRADICTS support"
        direct_supports = [c for c in supports if c.get("support_role") == "DIRECT"]
        if not direct_supports:
            return False, "atom has no active DIRECT support (e.g., CONTEXT-only)"

        valid_direct_chain = False
        for cas in direct_supports:
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
        if ec.get("oracle_status") != "DRAFT":
            errors.append(
                ValidationErrorItem("PREMATURE_GOLDEN_FREEZE", f"expected_claims[{cid}]", "Must be oracle_status='DRAFT'")
            )
        claim = claims_by_id.get(cid)
        if not claim:
            continue
        if ec.get("expected_claim_status") == "SOURCE_VERIFIED":
            if claim.get("user_edited"):
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SOURCE_VERIFIED",
                        f"expected_claims[{cid}]",
                        "User-edited Claim cannot be automatically set to SOURCE_VERIFIED",
                    )
                )
            atom_ids = claim.get("claim_atom_ids", [])
            if not atom_ids:
                errors.append(
                    ValidationErrorItem(
                        "ILLEGAL_SOURCE_VERIFIED",
                        f"expected_claims[{cid}]",
                        "Claim with 0 atoms cannot be SOURCE_VERIFIED",
                    )
                )
            for aid in atom_ids:
                ea = expected_atoms_by_id.get(aid)
                if ea and ea.get("expected_atom_status") not in ("SUPPORTED_DIRECT", "SUPPORTED_DIRECT_WITH_CONTEXT"):
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"Atom '{aid}' has expected status '{ea.get('expected_atom_status')}', blocking SOURCE_VERIFIED",
                        )
                    )
                ok, reason = _atom_has_valid_active_direct_grounding(aid)
                if not ok:
                    errors.append(
                        ValidationErrorItem(
                            "ILLEGAL_SOURCE_VERIFIED",
                            f"expected_claims[{cid}]",
                            f"Atom '{aid}' fails grounding invariant ({reason}); Claim cannot be SOURCE_VERIFIED",
                        )
                    )

    # 7. Validate Refetch (REF-01..08), Unsafe URL guard, Unverifiable vs Falsified, Canary Non-Persistence, Post-MVP tags
    expected_ref_ids = {f"REF-0{i}" for i in range(1, 9)}
    snaps_by_id = {r["refetch_case_id"]: r for r in ref_snap_doc.get("refetch_snapshots", [])}
    checks_by_id = {r["refetch_case_id"]: r for r in s4_doc.get("refetch_checks", [])}
    ref_oracles_by_id = {r["refetch_case_id"]: r for r in ref_oracle_doc.get("expected_refetch_cases", [])}

    for label, mapping in (
        ("sources/refetch_snapshots.json", snaps_by_id),
        ("stages/stage4_refetch_checks.json", checks_by_id),
        ("expected/refetch_oracle_draft.json", ref_oracles_by_id),
    ):
        missing = sorted(expected_ref_ids - set(mapping.keys()))
        if missing:
            errors.append(ValidationErrorItem("COVERAGE_INSUFFICIENT", label, f"Missing refetch cases: {missing}"))

    all_canary_tokens: list[str] = []
    for snap in snaps_by_id.values():
        all_canary_tokens.extend(snap.get("canary_tokens", []))

    for ref_id in sorted(expected_ref_ids):
        snap = snaps_by_id.get(ref_id)
        chk = checks_by_id.get(ref_id)
        ro = ref_oracles_by_id.get(ref_id)
        if not (snap and chk and ro):
            continue

        # URL safety check
        urls_to_check = [snap["target_url"], *snap.get("additional_unsafe_urls", [])]
        primary_safe, primary_reason = check_url_safety(snap["target_url"])
        if not primary_safe:
            if snap.get("url_safety_precheck") != "BLOCKED_UNSAFE_URL" or snap.get("fetch_attempted") is not False:
                errors.append(
                    ValidationErrorItem(
                        "UNSAFE_URL_POLICY_VIOLATION",
                        f"sources/refetch_snapshots.json[{ref_id}]",
                        f"Unsafe URL '{snap['target_url']}' ({primary_reason}) MUST be blocked with fetch_attempted=false",
                    )
                )
            if ro.get("expected_fetch_attempted") is not False:
                errors.append(
                    ValidationErrorItem(
                        "UNSAFE_URL_POLICY_VIOLATION",
                        f"expected/refetch_oracle_draft.json[{ref_id}]",
                        f"Unsafe URL '{snap['target_url']}' oracle MUST expect fetch_attempted=false",
                    )
                )
            for extra_url in urls_to_check[1:]:
                extra_safe, _ = check_url_safety(extra_url)
                if extra_safe:
                    errors.append(
                        ValidationErrorItem(
                            "UNSAFE_URL_POLICY_VIOLATION",
                            f"sources/refetch_snapshots.json[{ref_id}].additional_unsafe_urls",
                            f"Expected unsafe test vector URL, got safe URL '{extra_url}'",
                        )
                    )

        # Baseline locator round-trip
        base_rev = chk["baseline_source_revision_id"]
        if base_rev not in sources_by_id:
            errors.append(
                ValidationErrorItem("BROKEN_REFERENCE", f"stage4_refetch_checks[{ref_id}]", f"Unknown '{base_rev}'")
            )
        else:
            locator_checks_run += 1
            _, rt_errors = verify_locator_roundtrip(
                norm_text_by_rev[base_rev], chk["baseline_locator"], context_label=f"stage4_refetch_checks[{ref_id}]"
            )
            for code, msg in rt_errors:
                errors.append(ValidationErrorItem(code, f"stage4_refetch_checks[{ref_id}].baseline_locator", msg))

            # Verify refetch snapshot body hash & re-check consistency
            ret_body = snap.get("returned_body_fixture_only")
            if ret_body is not None:
                ret_norm = normalize_text(ret_body)
                calc_ret_hash = sha256_prefixed(ret_norm)
                if snap.get("returned_content_sha256") != calc_ret_hash:
                    errors.append(
                        ValidationErrorItem(
                            "HASH_MISMATCH",
                            f"refetch_snapshots[{ref_id}].returned_content_sha256",
                            f"Recorded {snap.get('returned_content_sha256')} != computed {calc_ret_hash}",
                        )
                    )
                actual_hash_match = calc_ret_hash == sources_by_id[base_rev]["content_sha256"]
                if chk.get("hash_recheck_matched") is not None and chk["hash_recheck_matched"] != actual_hash_match:
                    errors.append(
                        ValidationErrorItem(
                            "HASH_MISMATCH",
                            f"stage4_refetch_checks[{ref_id}].hash_recheck_matched",
                            f"Recorded {chk['hash_recheck_matched']} != actual {actual_hash_match}",
                        )
                    )
                if not snap.get("is_partial_response"):
                    _, ref_rt_errs = verify_locator_roundtrip(
                        ret_norm, chk["baseline_locator"], context_label=f"refetch_recheck[{ref_id}]"
                    )
                    actual_loc_match = len(ref_rt_errs) == 0
                    if chk.get("locator_recheck_matched") != actual_loc_match:
                        errors.append(
                            ValidationErrorItem(
                                "OFFSET_OR_ROUNDTRIP_MISMATCH",
                                f"stage4_refetch_checks[{ref_id}].locator_recheck_matched",
                                f"Recorded {chk['locator_recheck_matched']} != actual {actual_loc_match}",
                            )
                        )

        if chk["target_claim_id"] not in claims_by_id:
            errors.append(
                ValidationErrorItem(
                    "BROKEN_REFERENCE",
                    f"stage4_refetch_checks[{ref_id}].target_claim_id",
                    f"Unknown claim '{chk['target_claim_id']}'",
                )
            )

        # Unverifiable-now vs historical-fact-falsified separation
        if chk["stub_outcome"] in (
            "CONTENT_CHANGED_LOCATOR_DRIFT",
            "SOURCE_UNAVAILABLE",
            "PARTIAL_RESPONSE",
            "POLICY_DENIED",
            "BLOCKED_UNSAFE_URL",
        ):
            if ro.get("expected_unverifiable_now") is not True or ro.get("expected_historical_fact_falsified") is not False:
                errors.append(
                    ValidationErrorItem(
                        "UNVERIFIABLE_VS_FALSIFIED_CONFLATION",
                        f"expected/refetch_oracle_draft.json[{ref_id}]",
                        "Current unavailability/drift/policy failure MUST set expected_unverifiable_now=true "
                        "and expected_historical_fact_falsified=false",
                    )
                )

        # Post-MVP auto-mutation guard
        if ro.get("expected_claim_auto_mutated") is not False:
            errors.append(
                ValidationErrorItem(
                    "POST_MVP_VIOLATION",
                    f"expected/refetch_oracle_draft.json[{ref_id}]",
                    "Automatic Claim mutation after refetch is Post-MVP and must remain false in MVP fixtures",
                )
            )

        # Canary non-persistence check on clean persisted snapshot + negative leak fixture verification
        leaked_in_clean = detect_canary_in_obj(chk.get("persisted_payload_snapshot", {}), all_canary_tokens)
        if leaked_in_clean:
            errors.append(
                ValidationErrorItem(
                    "CANARY_LEAK_DETECTED",
                    f"stage4_refetch_checks[{ref_id}].persisted_payload_snapshot",
                    f"Raw source canary leaked into persisted payload snapshot: {leaked_in_clean}",
                )
            )
        if ref_id == "REF-07":
            neg_fixture = chk.get("negative_leaky_payload_fixture")
            leaked_in_neg = detect_canary_in_obj(neg_fixture, all_canary_tokens)
            if not leaked_in_neg:
                errors.append(
                    ValidationErrorItem(
                        "CANARY_NEGATIVE_FIXTURE_INVALID",
                        "stage4_refetch_checks[REF-07].negative_leaky_payload_fixture",
                        "REF-07 negative_leaky_payload_fixture must contain a canary token to verify detector sensitivity",
                    )
                )

    # Global Option C non-persistence audit across persisted stage files & oracle files
    for label, doc in (
        ("stages/stage1_content_units.json", s1_doc),
        ("stages/stage2_fact_extraction.json", s2_doc),
        ("stages/stage3_claim_assembly.json", s3_doc),
        ("expected/oracle_draft.json", oracle_doc),
        ("expected/refetch_oracle_draft.json", ref_oracle_doc),
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

    summary = {
        "source_revisions": len(sources_by_id),
        "content_units": len(units_by_id),
        "canonical_facts": len(facts_by_id),
        "fact_supports": len(s2_doc.get("fact_supports", [])),
        "claims": len(claims_by_id),
        "claim_atoms": len(atoms_by_id),
        "claim_atom_supports": len(s3_doc.get("claim_atom_supports", [])),
        "scenarios": len(scenarios_in_oracle),
        "refetch_cases": len(snaps_by_id),
        "locator_roundtrips_checked": locator_checks_run,
        "canary_tokens_audited": len(all_canary_tokens),
        "error_count": len(errors),
    }
    return errors, summary


def main() -> int:
    base_dir = Path(__file__).resolve().parent.parent
    schema, docs = load_dataset_bundle(base_dir)
    errors, summary = validate_bundle(schema, docs)
    print("T-004A Claim Draft Dataset Structural Validation Summary:")
    for key, val in summary.items():
        print(f"  {key}: {val}")
    if errors:
        print(f"\nFAILED with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err.format()}")
        return 1
    print("\nPASSED: All schema, locator/hash round-trip, anti-self-evidence, canary, and draft-oracle guards succeeded.")
    print("NOTE: Structural PASS is separate from T-004B human semantic ground-truth approval (oracle_status=DRAFT).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
