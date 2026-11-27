"""Unit & HTTP contract tests for T-106 AI JSON Schemas, Claim write guard, OD-008, and OD-003."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.conformance.ai_schemas import (
    GOLDEN_ALIGNMENT_REFERENCE,
    SCHEMA_PACKAGE_VERSION,
    SchemaCompatibilityClass,
    StageSchemaValidationError,
    build_fact_candidate_schema,
    classify_schema_compatibility,
    generate_ai_schema_manifest,
    generate_all_ai_schemas,
    validate_producer_consumer_handoff,
    validate_stage_payload,
)
from career_memory_api.contracts_api.models import (
    SearchCursorPayload,
    encode_search_cursor,
)
from career_memory_api.contracts_api.router import (
    InMemoryContractBoundaryService,
    create_contracts_router,
    create_m1_contract_app,
)

_SAMPLE_HASH = "sha256:" + ("a" * 64)


def _sample_locator() -> dict[str, object]:
    return {
        "locator_spec_version": "cm-locator-v1",
        "canonicalization_version": "cm-canon-nfc-lf-v1",
        "char_start": 0,
        "char_end": 25,
        "char_length": 25,
        "utf8_byte_start": 0,
        "utf8_byte_end": 25,
        "utf8_byte_length": 25,
        "utf16_start": 0,
        "utf16_end": 25,
        "line_start": 1,
        "line_end": 1,
        "span_sha256": _SAMPLE_HASH,
    }


def _sample_stage2a() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_PACKAGE_VERSION,
        "extractor_run_id": "run-fact-extract-001",
        "extractor_version": "fact-extractor-v1",
        "source_object_revision_id": "rev-001",
        "content_unit_id": "cu-001",
        "locator": _sample_locator(),
        "fact_type": "METRIC",
        "predicate": "reduced_p95_latency",
        "object_text": "Reduced p95 latency from 420ms to 180ms in production.",
        "normalized_value": {"from_ms": 420, "to_ms": 180},
        "status": "CANDIDATE",
        "metric_details": {
            "value": "57%",
            "unit": "percent_reduction",
            "period": "2026-09-01/2026-09-30",
            "source_basis": "PR benchmark log",
            "environment_scope": "PRODUCTION",
            "measurement_scope": "checkout API p95",
        },
    }


def _sample_stage2b() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_PACKAGE_VERSION,
        "validator_run_id": "run-fact-validate-001",
        "extractor_run_id": "run-fact-extract-001",
        "fact_candidate_id": "fcand-001",
        "source_object_revision_id": "rev-001",
        "content_unit_id": "cu-001",
        "verified_locator": _sample_locator(),
        "support_type": "DIRECT",
        "validator": "MODEL",
        "validator_version": "fact-validator-v1",
        "promotion_decision": "PROMOTE_SUPPORTED",
        "revision_and_locator_verified": True,
        "promoted_fact_id": "fact-001",
        "rejection_reason": None,
    }


def _sample_stage3a() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_PACKAGE_VERSION,
        "generator_run_id": "run-claim-gen-001",
        "confirmed_event_id": "evt-001",
        "input_fact_ids": ["fact-001", "fact-002"],
        "claim_version": 1,
        "claim_text": "Implemented checkout caching and reduced production p95 latency by 57%.",
        "status": "DRAFT",
    }


def _sample_stage3b() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_PACKAGE_VERSION,
        "atomizer_run_id": "run-claim-atomize-001",
        "generator_run_id": "run-claim-gen-001",
        "claim_id": "claim-001",
        "claim_version": 1,
        "atoms": [
            {"ordinal": 0, "atom_type": "ROLE", "atom_text": "Implemented checkout caching"},
            {"ordinal": 1, "atom_type": "METRIC", "atom_text": "Reduced production p95 latency by 57%"},
        ],
    }


def _sample_stage3c() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_PACKAGE_VERSION,
        "validation_run_id": "run-claim-validate-001",
        "generator_run_id": "run-claim-gen-001",
        "atomizer_run_id": "run-claim-atomize-001",
        "claim_id": "claim-001",
        "claim_version": 1,
        "atom_validations": [
            {
                "ordinal": 0,
                "atom_type": "ROLE",
                "status": "SUPPORTED",
                "fact_bindings": [
                    {
                        "fact_id": "fact-002",
                        "support_type": "DIRECT",
                        "fact_support_ids": ["fs-002"],
                        "source_object_revision_id": "rev-001",
                        "content_unit_id": "cu-001",
                    }
                ],
                "role_contribution_grounding": {
                    "subject_identity_id": "ident-owner-1",
                    "confirmed_event_id": "evt-001",
                    "role": "IMPLEMENTED",
                    "active_contribution_support_fact_ids": ["fact-002"],
                },
            },
            {
                "ordinal": 1,
                "atom_type": "METRIC",
                "status": "SUPPORTED",
                "fact_bindings": [
                    {
                        "fact_id": "fact-001",
                        "support_type": "DIRECT",
                        "fact_support_ids": ["fs-001"],
                        "source_object_revision_id": "rev-001",
                        "content_unit_id": "cu-001",
                    }
                ],
                "metric_grounding": {
                    "value": "57%",
                    "unit": "percent_reduction",
                    "period": "2026-09-01/2026-09-30",
                    "source_basis": "PR benchmark log",
                    "environment_scope": "PRODUCTION",
                    "measurement_scope": "checkout API p95",
                },
            },
        ],
        "computed_verification": "SOURCE_VERIFIED",
    }


def _sample_manifest() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_PACKAGE_VERSION,
        "run_id": "run-claim-validate-001",
        "workspace_id": str(uuid.uuid4()),
        "event_id": "evt-001",
        "run_type": "CLAIM_SUPPORT_VALIDATION",
        "input_artifact_versions": {
            "source_revision_ids": ["rev-001"],
            "source_revision_hashes": [_SAMPLE_HASH],
            "content_unit_ids": ["cu-001"],
            "fact_ids": ["fact-001", "fact-002"],
            "claim_id": "claim-001",
            "claim_version": 1,
            "canonicalization_version": "cm-canon-nfc-lf-v1",
        },
        "input_hash": _SAMPLE_HASH,
        "output_schema_id": "https://career-memory.local/schemas/ai/v1/claim_support_validation.v1.schema.json",
        "output_schema_version": SCHEMA_PACKAGE_VERSION,
        "compatibility_class": "BACKWARD_COMPATIBLE",
        "model": "structured-validator-v1",
        "prompt_version": "p-v1",
        "algorithm_version": "v1",
        "status": "SUCCEEDED",
    }


def _fake_access(workspace_id: str | None = None) -> WorkspaceAccessContext:
    ws_id = workspace_id or str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer="https://auth.local", subject=f"sub-{user_id}"),
        ),
        membership=WorkspaceMembership(
            workspace_id=ws_id,
            user_id=user_id,
            role="OWNER",
        ),
    )


# =============================================================================
# 1. AI JSON Schema Validation, Run Separation & Compatibility Tests (T-106)
# =============================================================================
def test_all_six_ai_stage_schemas_accept_valid_payloads_and_handoffs() -> None:
    s2a = _sample_stage2a()
    s2b = _sample_stage2b()
    s3a = _sample_stage3a()
    s3b = _sample_stage3b()
    s3c = _sample_stage3c()
    manifest = _sample_manifest()

    assert validate_stage_payload("fact_candidate", s2a) == []
    assert validate_stage_payload("fact_support_validation", s2b) == []
    assert validate_stage_payload("claim_draft", s3a) == []
    assert validate_stage_payload("claim_atomization", s3b) == []
    assert validate_stage_payload("claim_support_validation", s3c) == []
    assert validate_stage_payload("generation_run_manifest", manifest) == []

    validate_producer_consumer_handoff(
        producer_stage="fact_candidate",
        producer_payload=s2a,
        consumer_stage="fact_support_validation",
        consumer_payload=s2b,
    )
    validate_producer_consumer_handoff(
        producer_stage="claim_draft",
        producer_payload=s3a,
        consumer_stage="claim_atomization",
        consumer_payload=s3b,
    )
    validate_producer_consumer_handoff(
        producer_stage="claim_atomization",
        producer_payload=s3b,
        consumer_stage="claim_support_validation",
        consumer_payload=s3c,
    )

    schema_manifest = generate_ai_schema_manifest()
    assert schema_manifest["schema_package_version"] == SCHEMA_PACKAGE_VERSION
    assert schema_manifest["golden_alignment_reference"] == GOLDEN_ALIGNMENT_REFERENCE
    assert len(schema_manifest["schemas"]) == 6


def test_ai_schemas_align_with_golden_040_official_enums_when_available() -> None:
    golden_schema_path = Path(
        "/Users/eddy81848/Projects/Career Memory/.worktrees/qa-claim-fixtures/"
        "evaluation/claim-draft/schemas/claim_pipeline.schema.json"
    )
    if not golden_schema_path.is_file():
        return
    golden = json.loads(golden_schema_path.read_text(encoding="utf-8"))
    defs = golden["$defs"]
    schemas = generate_all_ai_schemas()

    assert (
        schemas["fact_candidate.v1.schema.json"]["properties"]["fact_type"]["enum"]
        == defs["OfficialFactType"]["enum"]
    )
    assert (
        schemas["claim_support_validation.v1.schema.json"]["$defs"]["AtomValidationResult"][
            "properties"
        ]["status"]["enum"]
        == defs["OfficialAtomStatus"]["enum"]
    )
    assert (
        schemas["fact_support_validation.v1.schema.json"]["properties"]["support_type"]["enum"]
        == defs["SupportRole"]["enum"]
    )
    assert set(
        schemas["fact_candidate.v1.schema.json"]["$defs"]["MetricDetails"]["required"]
    ) == set(defs["MetricDetails"]["required"])


def test_ai_stage_validator_rejects_run_id_reuse_across_stages() -> None:
    s2b = _sample_stage2b()
    s2b["validator_run_id"] = s2b["extractor_run_id"]
    with pytest.raises(StageSchemaValidationError) as exc_2b:
        validate_stage_payload("fact_support_validation", s2b)
    assert any(v.code == "STAGE_RUN_ID_COLLISION" for v in exc_2b.value.violations)

    s3b = _sample_stage3b()
    s3b["atomizer_run_id"] = s3b["generator_run_id"]
    with pytest.raises(StageSchemaValidationError) as exc_3b:
        validate_stage_payload("claim_atomization", s3b)
    assert any(v.code == "STAGE_RUN_ID_COLLISION" for v in exc_3b.value.violations)

    s3c = _sample_stage3c()
    s3c["validation_run_id"] = s3c["atomizer_run_id"]
    with pytest.raises(StageSchemaValidationError) as exc_3c:
        validate_stage_payload("claim_support_validation", s3c)
    assert any(v.code == "STAGE_RUN_ID_COLLISION" for v in exc_3c.value.violations)


@pytest.mark.parametrize(
    ("stage", "mutator", "expected_code"),
    [
        (
            "fact_candidate",
            lambda p: p.__setitem__("status", "SUPPORTED"),
            "CONST_MISMATCH",
        ),
        (
            "fact_candidate",
            lambda p: p.__setitem__("quote", "raw verbatim quote from PR"),
            "PROHIBITED_RAW_TEXT_KEY",
        ),
        (
            "fact_support_validation",
            lambda p: p.__setitem__("extracted_span", "raw extracted source span"),
            "PROHIBITED_RAW_TEXT_KEY",
        ),
        (
            "claim_draft",
            lambda p: p.__setitem__("verification", "SOURCE_VERIFIED"),
            "PROHIBITED_DIRECT_VERIFICATION_WRITE",
        ),
        (
            "claim_draft",
            lambda p: p.__setitem__("status", "SOURCE_VERIFIED"),
            "PROHIBITED_SOURCE_VERIFIED_WRITE",
        ),
        (
            "claim_draft",
            lambda p: p.__setitem__("choices", [{"message": {"content": "raw"}}]),
            "PROHIBITED_PROVIDER_SDK_SHAPE",
        ),
        (
            "generation_run_manifest",
            lambda p: p["input_artifact_versions"].__setitem__("system_prompt", "secret"),
            "PROHIBITED_RAW_TEXT_KEY",
        ),
    ],
)
def test_ai_stage_validator_blocks_prohibited_mutations(
    stage: str, mutator, expected_code: str
) -> None:
    builders = {
        "fact_candidate": _sample_stage2a,
        "fact_support_validation": _sample_stage2b,
        "claim_draft": _sample_stage3a,
        "generation_run_manifest": _sample_manifest,
    }
    payload = builders[stage]()
    mutator(payload)
    with pytest.raises(StageSchemaValidationError) as exc_info:
        validate_stage_payload(stage, payload)
    assert any(v.code == expected_code for v in exc_info.value.violations)


def test_stage3c_enforces_role_metric_grounding_and_computed_verification_math() -> None:
    missing_role = _sample_stage3c()
    del missing_role["atom_validations"][0]["role_contribution_grounding"]
    with pytest.raises(StageSchemaValidationError) as exc_role:
        validate_stage_payload("claim_support_validation", missing_role)
    assert any(v.code == "ROLE_GROUNDING_REQUIRED" for v in exc_role.value.violations)

    missing_metric = _sample_stage3c()
    del missing_metric["atom_validations"][1]["metric_grounding"]
    with pytest.raises(StageSchemaValidationError) as exc_metric:
        validate_stage_payload("claim_support_validation", missing_metric)
    assert any(v.code == "METRIC_GROUNDING_REQUIRED" for v in exc_metric.value.violations)

    bad_verif = _sample_stage3c()
    bad_verif["atom_validations"][1]["status"] = "USER_CONFIRMED"
    # Keeping computed_verification == SOURCE_VERIFIED must fail (expected MIXED)
    with pytest.raises(StageSchemaValidationError) as exc_verif:
        validate_stage_payload("claim_support_validation", bad_verif)
    assert any(v.code == "COMPUTED_VERIFICATION_MISMATCH" for v in exc_verif.value.violations)


def test_schema_compatibility_classifier_distinguishes_non_breaking_vs_breaking() -> None:
    base = build_fact_candidate_schema()

    additive = copy.deepcopy(base)
    additive["properties"]["optional_hint"] = {"type": ["string", "null"]}
    res_ok = classify_schema_compatibility(base, additive)
    assert res_ok.compatibility_class is SchemaCompatibilityClass.BACKWARD_COMPATIBLE
    assert not res_ok.breaking_reasons

    breaking = copy.deepcopy(base)
    breaking["required"].append("new_mandatory_field")
    breaking["properties"]["new_mandatory_field"] = {"type": "string"}
    res_break = classify_schema_compatibility(base, breaking)
    assert res_break.compatibility_class is SchemaCompatibilityClass.MIGRATION_REQUIRED
    assert res_break.breaking_reasons


# =============================================================================
# 2. FastAPI Contract Boundary Tests (Claims, OD-008 Search, OD-003 Deletion)
# =============================================================================
def test_claim_write_routes_reject_verification_and_source_verified() -> None:
    access = _fake_access()
    app = create_m1_contract_app(resolve_access=lambda _req: access)

    with TestClient(app) as client:
        ok = client.post(
            "/v1/claims/drafts",
            json={
                "confirmed_event_id": "evt-1",
                "input_fact_ids": ["fact-1"],
                "claim_text": "Built deterministic locator parser.",
            },
        )
        assert ok.status_code == 201
        assert ok.headers["Cache-Control"] == "private, no-store"
        assert ok.json()["status"] == "DRAFT"
        assert ok.json()["computed_verification"] == "UNVERIFIED"

        # Inject writable verification field -> 422 PROHIBITED_READONLY_VERIFICATION_WRITE
        bad_field = client.post(
            "/v1/claims/drafts",
            json={
                "confirmed_event_id": "evt-1",
                "input_fact_ids": ["fact-1"],
                "claim_text": "Built deterministic locator parser.",
                "verification": "SOURCE_VERIFIED",
            },
        )
        assert bad_field.status_code == 422
        assert bad_field.json()["error"]["code"] == "PROHIBITED_READONLY_VERIFICATION_WRITE"

        # Inject SOURCE_VERIFIED on PATCH edit -> 422 PROHIBITED_READONLY_VERIFICATION_WRITE
        bad_patch = client.patch(
            "/v1/claims/claim-1",
            json={
                "expected_version": 1,
                "claim_text": "Edited claim",
                "computed_verification": "SOURCE_VERIFIED",
            },
        )
        assert bad_patch.status_code == 422
        assert bad_patch.json()["error"]["code"] == "PROHIBITED_READONLY_VERIFICATION_WRITE"

        # Inject SOURCE_VERIFIED on approve -> 422 PROHIBITED_READONLY_VERIFICATION_WRITE
        bad_approve = client.post(
            "/v1/claims/claim-1/approve",
            json={
                "expected_version": 1,
                "verification": "SOURCE_VERIFIED",
            },
        )
        assert bad_approve.status_code == 422
        assert bad_approve.json()["error"]["code"] == "PROHIBITED_READONLY_VERIFICATION_WRITE"

        # Inject Plan C raw quote key -> 422 PROHIBITED_RAW_TEXT_PERSISTENCE
        bad_raw = client.post(
            "/v1/claims/drafts",
            json={
                "confirmed_event_id": "evt-1",
                "input_fact_ids": ["fact-1"],
                "claim_text": "Built parser",
                "quote": "raw text from PR",
            },
        )
        assert bad_raw.status_code == 422
        assert bad_raw.json()["error"]["code"] == "PROHIBITED_RAW_TEXT_PERSISTENCE"


def test_od008_search_rejects_project_id_and_enforces_bound_cursor() -> None:
    access_a = _fake_access()
    service = InMemoryContractBoundaryService()
    app = create_m1_contract_app(
        contracts_service=service,
        resolve_access=lambda _req: access_a,
    )

    with TestClient(app) as client:
        # project_id is strictly forbidden (OD-008)
        rejected = client.get("/v1/search", params={"q": "latency", "project_id": "proj-1"})
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "PROHIBITED_PROJECT_ID_SCOPE"

        # Valid Workspace + optional source_resource_id search
        ok = client.get(
            "/v1/search",
            params={"q": "latency", "source_resource_id": "res-1", "limit": 10},
        )
        assert ok.status_code == 200
        assert ok.headers["Cache-Control"] == "private, no-store"
        body = ok.json()
        assert body["scope"] == {
            "workspace_id": access_a.workspace_id,
            "source_resource_id": "res-1",
        }
        assert "project_id" not in body
        cursor = body["next_cursor"]
        assert isinstance(cursor, str) and "." in cursor

        # Same binding cursor works
        page2 = client.get(
            "/v1/search",
            params={"q": "latency", "source_resource_id": "res-1", "limit": 10, "cursor": cursor},
        )
        assert page2.status_code == 200

        # Changing query invalidates cursor binding
        bad_query = client.get(
            "/v1/search",
            params={"q": "different", "source_resource_id": "res-1", "cursor": cursor},
        )
        assert bad_query.status_code == 400
        assert bad_query.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"

        # Changing source_resource_id invalidates cursor binding
        bad_res = client.get(
            "/v1/search",
            params={"q": "latency", "source_resource_id": "res-2", "cursor": cursor},
        )
        assert bad_res.status_code == 400
        assert bad_res.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"

        # Cursor forged for another workspace is rejected
        other_cursor = encode_search_cursor(
            SearchCursorPayload(
                workspace_id=str(uuid.uuid4()),
                source_resource_id="res-1",
                query_hash=body["query_hash"],
                index_version=service.index_version,
                ranking_version=service.ranking_version,
                offset=10,
            )
        )
        cross_ws = client.get(
            "/v1/search",
            params={"q": "latency", "source_resource_id": "res-1", "cursor": other_cursor},
        )
        assert cross_ws.status_code == 400
        assert cross_ws.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"

        # Changing index_version invalidates existing cursor
        service.index_version = "fts-index-v2"
        stale_index = client.get(
            "/v1/search",
            params={"q": "latency", "source_resource_id": "res-1", "cursor": cursor},
        )
        assert stale_index.status_code == 400
        assert stale_index.json()["error"]["code"] == "INVALID_SEARCH_CURSOR"


def test_od003_source_deletion_preview_and_confirm_contract() -> None:
    now_box = [datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)]
    access = _fake_access()
    service = InMemoryContractBoundaryService(
        now_fn=lambda: now_box[0],
        preview_ttl_seconds=120,
    )
    app = create_m1_contract_app(
        contracts_service=service,
        resolve_access=lambda _req: access,
    )
    res_id = "res-del-1"

    with TestClient(app) as client:
        preview = client.post(f"/v1/source-resources/{res_id}/deletion-preview")
        assert preview.status_code == 200
        assert preview.headers["Cache-Control"] == "private, no-store"
        pdata = preview.json()
        assert pdata["source_resource_id"] == res_id
        assert pdata["non_recoverable_scopes"]["already_downloaded_exports"] is True
        assert pdata["non_recoverable_scopes"]["provider_hosted_originals"] is True
        for count_key in (
            "work_events",
            "facts",
            "contributions",
            "claims_draft",
            "claims_edited",
            "claims_approved",
            "claims_historical_versions",
            "search_index_entries",
            "ephemeral_cache_entries",
            "server_exports",
        ):
            assert count_key in pdata["impact_counts"]

        # Missing Idempotency-Key -> 422
        no_key = client.post(
            f"/v1/source-resources/{res_id}/deletion-confirm",
            json={
                "preview_token": pdata["preview_token"],
                "expected_resource_version": pdata["resource_version"],
                "expected_impact_scope_version": pdata["impact_scope_version"],
                "acknowledge_irreversible_purge": True,
            },
        )
        assert no_key.status_code == 422

        # Valid confirm -> 202 + idempotent replay returns same deletion_job_id
        idem_key = str(uuid.uuid4())
        confirm_payload = {
            "preview_token": pdata["preview_token"],
            "expected_resource_version": pdata["resource_version"],
            "expected_impact_scope_version": pdata["impact_scope_version"],
            "acknowledge_irreversible_purge": True,
        }
        ok1 = client.post(
            f"/v1/source-resources/{res_id}/deletion-confirm",
            headers={"Idempotency-Key": idem_key},
            json=confirm_payload,
        )
        assert ok1.status_code == 202
        assert ok1.json()["fence_active"] is True
        ok2 = client.post(
            f"/v1/source-resources/{res_id}/deletion-confirm",
            headers={"Idempotency-Key": idem_key},
            json=confirm_payload,
        )
        assert ok2.status_code == 202
        assert ok2.json()["deletion_job_id"] == ok1.json()["deletion_job_id"]

        # Scope version changed -> 409 DELETION_PREVIEW_SCOPE_CHANGED
        service.set_resource_scope_state(
            access.workspace_id,
            res_id,
            resource_version="res-v2",
            impact_scope_version="scope-v2",
        )
        changed = client.post(
            f"/v1/source-resources/{res_id}/deletion-confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json=confirm_payload,
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "DELETION_PREVIEW_SCOPE_CHANGED"

        # Fresh preview then expire token -> 409 DELETION_PREVIEW_EXPIRED
        preview2 = client.post(f"/v1/source-resources/{res_id}/deletion-preview").json()
        now_box[0] = now_box[0] + timedelta(seconds=121)
        expired = client.post(
            f"/v1/source-resources/{res_id}/deletion-confirm",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "preview_token": preview2["preview_token"],
                "expected_resource_version": preview2["resource_version"],
                "expected_impact_scope_version": preview2["impact_scope_version"],
                "acknowledge_irreversible_purge": True,
            },
        )
        assert expired.status_code == 409
        assert expired.json()["error"]["code"] == "DELETION_PREVIEW_EXPIRED"


def test_create_app_supports_optional_router_mounting_without_breaking_defaults() -> None:
    access = _fake_access()
    app = create_app(
        contracts_router=create_contracts_router(resolve_access=lambda _req: access),
        install_contract_handlers=True,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/search", params={"q": "ok"}).status_code == 200
