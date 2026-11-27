"""Unit & static contract tests for M1-T102 and M1-T104 schema contracts.

Runs under default ``uv run pytest -v`` without requiring a database server.
"""

from __future__ import annotations

import re

import pytest

from career_memory_api.schema_contracts import (
    COMPOSITE_FK_SPEC,
    DEFERRED_POST_MVP_TABLES,
    FORBIDDEN_RAW_TEXT_KEYS,
    MIGRATION_0020_TABLES,
    MIGRATION_0030_TABLES,
    MVP_RULE_CONTRIBUTION_ROLES,
    SCHEMA_MIGRATION_FILES,
    TENANT_DOMAIN_TABLES,
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    ContributionRole,
    EventMembershipOrigin,
    EventMembershipRelationClass,
    ExternalIdentityMappingStatus,
    FactType,
    GenerationRunStatus,
    GenerationRunType,
    IdentityStatus,
    IdentityType,
    IdentityVerificationMethod,
    RawStorageMode,
    RawTextPersistenceViolationError,
    RetentionClass,
    ReviewActionType,
    WorkEventAnchorMode,
    WorkEventStatus,
    assert_no_forbidden_raw_keys,
    find_forbidden_raw_keys,
    list_schema_migrations,
    load_migration_sql,
    validate_anchor_metadata,
    validate_generation_run_metadata,
    validate_normalized_metadata,
    validate_sha256_hash,
    validate_support_metadata,
    validate_token_secret_ref,
)


def _extract_sql_forbidden_keys(sql_0020: str) -> set[str]:
    match = re.search(
        r"AND lower\(key_name\) IN \((?P<keys>.*?)\)\s*\);",
        sql_0020,
        flags=re.DOTALL,
    )
    assert match is not None, "Could not locate forbidden key list in 0020 SQL."
    return set(re.findall(r"'([a-z0-9_]+)'", match.group("keys")))


def _extract_table_columns(sql_text: str, table_name: str) -> set[str]:
    block = re.search(
        rf"CREATE TABLE IF NOT EXISTS public\.{table_name}\s*\((?P<body>.*?)\n\);",
        sql_text,
        flags=re.DOTALL,
    )
    assert block is not None, f"Could not locate CREATE TABLE for public.{table_name}"
    return {
        line.strip().split()[0]
        for line in block.group("body").splitlines()
        if line.strip()
        and not line.strip().startswith(("--", "CONSTRAINT", "PRIMARY", "UNIQUE", ")"))
    }


def test_migration_files_exist_in_expected_order() -> None:
    migrations = list_schema_migrations()
    assert tuple(m.filename for m in migrations) == SCHEMA_MIGRATION_FILES
    assert [m.version for m in migrations] == ["0020", "0030"]
    for migration in migrations:
        assert len(migration.sha256) == 64
        assert load_migration_sql(migration.version) == load_migration_sql(
            migration.filename
        )

    with pytest.raises(KeyError):
        load_migration_sql("9999")


def test_python_and_sql_forbidden_raw_key_sets_match_exactly() -> None:
    sql_0020 = load_migration_sql("0020")
    sql_keys = _extract_sql_forbidden_keys(sql_0020)
    assert sql_keys == set(FORBIDDEN_RAW_TEXT_KEYS)
    required_core = {
        "body",
        "raw_body",
        "raw_text",
        "text_content",
        "diff",
        "patch",
        "comment_body",
        "message_body",
        "quote",
        "extracted_span",
        "prompt",
        "raw_payload",
    }
    assert required_core.issubset(sql_keys)


def test_migrations_contain_no_hardcoded_passwords_and_no_deferred_tables() -> None:
    sql_0020 = load_migration_sql("0020")
    sql_0030 = load_migration_sql("0030")
    combined = f"{sql_0020}\n{sql_0030}"

    assert re.search(r"\bPASSWORD\s+'", combined, flags=re.IGNORECASE) is None

    for deferred_table in DEFERRED_POST_MVP_TABLES:
        assert (
            re.search(
                rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?{deferred_table}\b",
                combined,
                flags=re.IGNORECASE,
            )
            is None
        ), f"Deferred table {deferred_table!r} must not be created in M1."


def test_ddl_columns_match_notion_data_model_spec_1_to_1() -> None:
    sql_0020 = load_migration_sql("0020")
    sql_0030 = load_migration_sql("0030")

    # 0020 aligned with 0010 base tables
    assert {"id", "auth_issuer", "auth_subject", "email", "name", "created_at"} == _extract_table_columns(
        sql_0020, "users"
    )
    assert {"id", "owner_user_id", "name", "created_at"} == _extract_table_columns(
        sql_0020, "workspaces"
    )
    assert {"workspace_id", "user_id", "role", "revoked_at", "created_at"} == _extract_table_columns(
        sql_0020, "workspace_memberships"
    )

    # 0030 §3.5 identities & external_identity_mappings (no plaintext external_email)
    ident_cols = _extract_table_columns(sql_0030, "identities")
    assert {"display_name", "identity_type", "canonical_user_id", "status"}.issubset(ident_cols)
    assert "canonical_name" not in ident_cols
    assert "identity_kind" not in ident_cols

    ext_cols = _extract_table_columns(sql_0030, "external_identity_mappings")
    assert {"email_hash", "username", "verification_method", "status", "confidence_reason", "evidence_metadata", "revoked_at"}.issubset(ext_cols)
    assert "external_email" not in ext_cols

    # 0030 §3.6 work_events, event_memberships, event_composite_memberships, review_actions
    ev_cols = _extract_table_columns(sql_0030, "work_events")
    assert {"anchor_mode", "anchor_scope", "version", "problem", "action", "outcome", "created_by", "reviewed_at"}.issubset(ev_cols)
    assert "event_kind" not in ev_cols

    em_cols = _extract_table_columns(sql_0030, "event_memberships")
    assert {"relation_class", "relation_type", "origin", "rationale_code"}.issubset(em_cols)

    ecm_cols = _extract_table_columns(sql_0030, "event_composite_memberships")
    assert {"composite_event_version", "member_event_version", "added_by_review_action_id", "removed_at"}.issubset(ecm_cols)

    ra_cols = _extract_table_columns(sql_0030, "review_actions")
    assert {"from_version", "to_version", "reason", "idempotency_key"}.issubset(ra_cols)

    # 0030 §3.7 facts
    fact_cols = _extract_table_columns(sql_0030, "facts")
    assert {"subject_identity_id", "predicate", "object_text", "normalized_value"}.issubset(fact_cols)
    assert "statement" not in fact_cols

    # 0030 §3.9-3.10 generation_runs, claims (no writable verification), claim_atom_support
    gr_cols = _extract_table_columns(sql_0030, "generation_runs")
    assert {"run_type", "model", "algorithm_version", "input_hash", "token_usage", "cost_estimate", "latency_ms", "error_code"}.issubset(gr_cols)

    claim_cols = _extract_table_columns(sql_0030, "claims")
    assert "verification" not in claim_cols
    assert {"version", "generator_run_id", "approved_at", "last_verified_at", "last_verification_run_id"}.issubset(claim_cols)
    assert "claim_version" not in claim_cols

    cas_cols = _extract_table_columns(sql_0030, "claim_atom_support")
    assert "validation_run_id" in cas_cols


def test_domain_enum_invariants_match_notion_data_model_spec() -> None:
    assert {e.value for e in IdentityType} == {"PERSON", "BOT", "UNKNOWN"}
    assert {e.value for e in IdentityStatus} == {"ACTIVE", "AMBIGUOUS", "MERGED", "REVOKED"}
    assert {e.value for e in IdentityVerificationMethod} == {
        "OAUTH_ACCOUNT",
        "PROVIDER_ASSOCIATION",
        "USER_CONFIRMATION",
    }
    assert {e.value for e in ExternalIdentityMappingStatus} == {
        "VERIFIED",
        "USER_CONFIRMED",
        "HEURISTIC",
        "CONFLICTED",
        "REVOKED",
    }
    assert {e.value for e in WorkEventAnchorMode} == {
        "PR",
        "PR_SEGMENT",
        "DIRECT_COMMIT",
        "USER_COMPOSITE",
    }
    assert {e.value for e in WorkEventStatus} == {
        "CANDIDATE",
        "CONFIRMED",
        "REJECTED",
        "STALE",
        "SUPERSEDED",
    }
    assert {e.value for e in EventMembershipRelationClass} == {
        "MUST_LINK",
        "CONTEXT_ONLY",
        "CANNOT_LINK",
    }
    assert {e.value for e in EventMembershipOrigin} == {"SOURCE", "RULE", "USER"}
    assert {e.value for e in ReviewActionType} == {
        "CONFIRM",
        "EDIT",
        "SPLIT",
        "MERGE",
        "DISSOLVE",
        "REJECT",
        "REOPEN",
    }
    assert {e.value for e in FactType} == {
        "PROBLEM",
        "ACTION",
        "TECHNOLOGY",
        "RESULT",
        "ROLE",
        "METRIC",
    }
    assert {e.value for e in ClaimAtomType} == {e.value for e in FactType}
    assert {e.value for e in ClaimAtomStatus} == {
        "SUPPORTED",
        "USER_CONFIRMED",
        "UNSUPPORTED",
        "STALE",
    }
    assert {e.value for e in ClaimVerification} == {
        "SOURCE_VERIFIED",
        "USER_CONFIRMED",
        "MIXED",
        "UNVERIFIED",
    }
    assert {e.value for e in ClaimStatus} == {
        "DRAFT",
        "DRAFT_INVALID",
        "SUPPORT_VALIDATED",
        "APPROVED",
        "REJECTED",
        "STALE",
        "SUPERSEDED",
    }
    assert {e.value for e in ContributionRole} == {
        "IMPLEMENTED",
        "REVIEWED",
        "PROPOSED",
        "DESIGNED",
        "OPERATED",
        "MENTORED",
        "MEASURED",
    }
    assert MVP_RULE_CONTRIBUTION_ROLES == frozenset(
        {ContributionRole.IMPLEMENTED, ContributionRole.REVIEWED}
    )
    assert {e.value for e in GenerationRunType} == {
        "FACT_CANDIDATE_EXTRACTION",
        "FACT_SUPPORT_VALIDATION",
        "CLAIM_DRAFT_GENERATION",
        "CLAIM_ATOMIZATION",
        "CLAIM_SUPPORT_VALIDATION",
        "REFETCH_VERIFICATION",
    }
    assert {e.value for e in GenerationRunStatus} == {
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELED",
    }
    assert {e.value for e in RawStorageMode} == {"NONE"}
    assert {e.value for e in RetentionClass} == {"NONE"}
    assert len(MIGRATION_0020_TABLES) == 5
    assert len(MIGRATION_0030_TABLES) == 14
    assert len(TENANT_DOMAIN_TABLES) == 19
    assert len(COMPOSITE_FK_SPEC) == 28


def test_metadata_guard_blocks_top_level_nested_and_array_canary_keys() -> None:
    nested_payload = {
        "parser_version": "anchor-v1",
        "meta": {
            "items": [
                {"safe": 1},
                {"nested": {"RAW_TEXT": "CANARY_SECRET_RAW_BODY"}},
            ]
        },
    }
    hits = find_forbidden_raw_keys(nested_payload)
    assert hits == [("$.meta.items[1].nested.RAW_TEXT", "RAW_TEXT")]

    with pytest.raises(RawTextPersistenceViolationError) as exc_info:
        assert_no_forbidden_raw_keys(nested_payload, field_name="anchor")
    assert exc_info.value.violations == hits


def test_anchor_and_metadata_validators() -> None:
    valid_anchor = {
        "parser_version": "gh-md-v1.0",
        "offset_unit": "char",
        "char_start": 10,
        "char_end": 120,
        "span_sha256": "a" * 64,
    }
    assert validate_anchor_metadata(valid_anchor, strict_whitelist=True) == valid_anchor

    with pytest.raises(RawTextPersistenceViolationError):
        validate_anchor_metadata({"char_start": 0, "char_end": 10})

    with pytest.raises(RawTextPersistenceViolationError):
        validate_anchor_metadata({"parser_version": "  "})

    with pytest.raises(RawTextPersistenceViolationError):
        validate_anchor_metadata(
            {"parser_version": "v1", "quote": "CANARY_QUOTE_SHOULD_FAIL"}
        )

    with pytest.raises(RawTextPersistenceViolationError):
        validate_anchor_metadata(
            {"parser_version": "v1", "unknown_extra_key": 123},
            strict_whitelist=True,
        )

    assert validate_normalized_metadata(
        {"provider_number": 42, "merged": True}, strict_whitelist=True
    ) == {"provider_number": 42, "merged": True}

    with pytest.raises(RawTextPersistenceViolationError):
        validate_normalized_metadata({"pr": {"body": "CANARY_PR_BODY"}})

    assert validate_support_metadata(
        {"locator_id": "loc-1", "char_start": 0, "char_end": 12},
        strict_whitelist=True,
    ) == {"locator_id": "loc-1", "char_start": 0, "char_end": 12}

    with pytest.raises(RawTextPersistenceViolationError):
        validate_support_metadata({"extracted_span": "CANARY_SPAN"})

    validate_generation_run_metadata(
        {"fact_ids": ["f1"], "schema_version": "1.0"},
        {"duration_ms": 120},
        strict_whitelist=True,
    )

    with pytest.raises(RawTextPersistenceViolationError):
        validate_generation_run_metadata(
            {"fact_ids": ["f1"]},
            {"debug": [{"prompt": "CANARY_SYSTEM_PROMPT"}]},
        )


def test_token_secret_ref_and_sha256_validators() -> None:
    # Both URI-style secret refs and 0010-style opaque refs are accepted.
    assert validate_token_secret_ref("secret://github/ws-1/acct-1") == (
        "secret://github/ws-1/acct-1"
    )
    assert validate_token_secret_ref("kms://projects/cm/keys/gh-token-1") == (
        "kms://projects/cm/keys/gh-token-1"
    )
    assert validate_token_secret_ref("vault/github/ws-1/acct-ref-001") == (
        "vault/github/ws-1/acct-ref-001"
    )

    for bad_ref in (
        "",
        "ghp_1234567890abcdef1234567890abcdef1234",
        "gho_1234567890abcdef",
        "github_pat_11ABCDEF_rawtokenvalue",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "has spaces in ref",
        "ref?with=query",
    ):
        with pytest.raises(ValueError):
            validate_token_secret_ref(bad_ref)

    valid_hex = "0123456789abcdef" * 4
    assert validate_sha256_hash(valid_hex) == valid_hex
    assert validate_sha256_hash(f"sha256:{valid_hex}") == f"sha256:{valid_hex}"
    for bad_hash in ("", "abc123", "A" * 64, "md5:" + valid_hex):
        with pytest.raises(ValueError):
            validate_sha256_hash(bad_hash)
