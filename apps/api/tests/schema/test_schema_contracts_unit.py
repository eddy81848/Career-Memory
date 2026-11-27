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
    FactType,
    RawStorageMode,
    RawTextPersistenceViolationError,
    RetentionClass,
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
    # Verify core required keys from Plan C spec are explicitly guarded.
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


def test_claims_table_ddl_has_no_writable_verification_column() -> None:
    sql_0030 = load_migration_sql("0030")
    claims_block = re.search(
        r"CREATE TABLE IF NOT EXISTS public\.claims\s*\((?P<body>.*?)\n\);",
        sql_0030,
        flags=re.DOTALL,
    )
    assert claims_block is not None
    column_names = {
        line.strip().split()[0]
        for line in claims_block.group("body").splitlines()
        if line.strip()
        and not line.strip().startswith(("--", "CONSTRAINT", ")"))
    }
    assert "verification" not in column_names
    assert "last_verified_at" in column_names
    assert "last_verification_run_id" in column_names


def test_domain_enum_invariants_match_spec() -> None:
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
    assert {e.value for e in RawStorageMode} == {"NONE"}
    assert {e.value for e in RetentionClass} == {"NONE"}
    assert len(MIGRATION_0020_TABLES) == 5
    assert len(MIGRATION_0030_TABLES) == 14
    assert len(TENANT_DOMAIN_TABLES) == 19
    assert len(COMPOSITE_FK_SPEC) == 25


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
    assert validate_token_secret_ref("secret://github/ws-1/acct-1") == (
        "secret://github/ws-1/acct-1"
    )
    assert validate_token_secret_ref("kms://projects/cm/keys/gh-token-1") == (
        "kms://projects/cm/keys/gh-token-1"
    )

    for bad_ref in (
        "",
        "ghp_1234567890abcdef1234567890abcdef1234",
        "github_pat_11ABCDEF_rawtokenvalue",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "https://example.com/token",
        "plain-secret-value",
    ):
        with pytest.raises(ValueError):
            validate_token_secret_ref(bad_ref)

    valid_hex = "0123456789abcdef" * 4
    assert validate_sha256_hash(valid_hex) == valid_hex
    assert validate_sha256_hash(f"sha256:{valid_hex}") == f"sha256:{valid_hex}"
    for bad_hash in ("", "abc123", "A" * 64, "md5:" + valid_hex):
        with pytest.raises(ValueError):
            validate_sha256_hash(bad_hash)
