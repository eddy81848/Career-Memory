"""Plan C (방안 C) metadata whitelist & forbidden raw-text key validators.

Mirrors the PostgreSQL SQL guard ``public.cm_jsonb_contains_forbidden_raw_key(jsonb)``
so application producers, workers, and drift CI validate metadata payloads before
writing to PostgreSQL.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Exact 1:1 mirror of public.cm_jsonb_contains_forbidden_raw_key(jsonb) in 0020.
FORBIDDEN_RAW_TEXT_KEYS: frozenset[str] = frozenset(
    {
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
        "raw_content",
        "full_text",
        "source_text",
        "raw_diff",
        "commit_message_body",
        "prompt_text",
        "system_prompt",
        "user_prompt",
        "completion",
        "raw_response",
        "content_text",
        "excerpt",
        "snippet",
        "verbatim_quote",
        "pr_body",
        "issue_body",
        "review_body",
        "patch_text",
        "diff_text",
        "hunk_text",
    }
)

# Recommended metadata key whitelists for strict producer validation.
ALLOWED_ANCHOR_KEYS: frozenset[str] = frozenset(
    {
        "parser_version",
        "locator_version",
        "locator_type",
        "kind",
        "offset_unit",
        "char_start",
        "char_end",
        "byte_start",
        "byte_end",
        "line_start",
        "line_end",
        "start_line",
        "end_line",
        "section_index",
        "section_heading_hash",
        "heading_path_hash",
        "file_path",
        "file_path_hash",
        "hunk_index",
        "hunk_header_hash",
        "old_start",
        "old_lines",
        "new_start",
        "new_lines",
        "symbol_name",
        "symbol_kind",
        "comment_id",
        "review_id",
        "commit_sha",
        "normalization_policy",
        "canonicalization_version",
        "span_sha256",
        "locator_id",
    }
)

ALLOWED_NORMALIZED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "provider_number",
        "state",
        "merged",
        "draft",
        "base_ref",
        "head_ref",
        "base_sha",
        "head_sha",
        "merge_commit_sha",
        "author_external_id",
        "author_login",
        "committer_external_id",
        "reviewer_external_ids",
        "labels",
        "changed_files_count",
        "additions",
        "deletions",
        "commit_count",
        "comment_count",
        "review_count",
        "created_at",
        "updated_at",
        "closed_at",
        "merged_at",
        "html_url",
        "api_etag",
        "byte_length",
        "char_length",
        "unit_count",
        "canonicalization_version",
    }
)

ALLOWED_SUPPORT_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "locator_id",
        "parser_version",
        "canonicalization_version",
        "offset_unit",
        "char_start",
        "char_end",
        "byte_start",
        "byte_end",
        "line_start",
        "line_end",
        "span_sha256",
        "unit_content_hash",
        "revision_content_hash",
        "confidence",
        "match_method",
        "validator_run_id",
        "checked_at",
        "promotion_decision",
    }
)

ALLOWED_GENERATION_RUN_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "source_revision_ids",
        "source_revision_hashes",
        "content_unit_ids",
        "content_unit_hashes",
        "fact_ids",
        "contribution_ids",
        "claim_id",
        "claim_version",
        "hash_policy_version",
        "canonicalization_version",
        "schema_version",
        "ruleset_version",
    }
)

_TOKEN_SECRET_REF_PATTERN = re.compile(
    r"^(secret|vault|kms|sm|envref)://[A-Za-z0-9._:/-]+$"
)
_RAW_TOKEN_PREFIX_PATTERN = re.compile(
    r"^(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|bearer )", re.IGNORECASE
)
_SHA256_HASH_PATTERN = re.compile(r"^(sha256:)?[0-9a-f]{64}$")


class RawTextPersistenceViolationError(ValueError):
    """Raised when a metadata payload violates Plan C (방안 C) raw-text rules."""

    def __init__(self, message: str, *, violations: list[tuple[str, str]] | None = None) -> None:
        super().__init__(message)
        self.violations: list[tuple[str, str]] = violations or []


def find_forbidden_raw_keys(payload: Any, path: str = "$") -> list[tuple[str, str]]:
    """Recursively locate any forbidden raw-text JSON keys inside dicts/lists.

    Returns a list of ``(json_path, key_name)`` tuples.
    """
    found: list[tuple[str, str]] = []
    if isinstance(payload, Mapping):
        for raw_key, value in payload.items():
            key_str = str(raw_key)
            key_path = f"{path}.{key_str}"
            if key_str.lower() in FORBIDDEN_RAW_TEXT_KEYS:
                found.append((key_path, key_str))
            found.extend(find_forbidden_raw_keys(value, key_path))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(find_forbidden_raw_keys(item, f"{path}[{index}]"))
    return found


def assert_no_forbidden_raw_keys(
    payload: Mapping[str, Any], *, field_name: str = "metadata"
) -> None:
    """Validate that ``payload`` is a JSON object with zero forbidden raw-text keys."""
    if not isinstance(payload, Mapping):
        raise RawTextPersistenceViolationError(
            f"{field_name} must be a JSON object (mapping)."
        )
    violations = find_forbidden_raw_keys(payload)
    if violations:
        formatted = ", ".join(f"{path} ({key})" for path, key in violations)
        raise RawTextPersistenceViolationError(
            f"{field_name} contains forbidden Plan C raw-text key(s): {formatted}",
            violations=violations,
        )


def _assert_whitelist(
    payload: Mapping[str, Any],
    *,
    allowed_keys: frozenset[str],
    field_name: str,
) -> None:
    unknown = sorted(str(k) for k in payload.keys() if str(k) not in allowed_keys)
    if unknown:
        raise RawTextPersistenceViolationError(
            f"{field_name} contains non-whitelisted key(s): {', '.join(unknown)}"
        )


def validate_anchor_metadata(
    anchor: Mapping[str, Any], *, strict_whitelist: bool = False
) -> dict[str, Any]:
    """Validate ``content_units.anchor`` against Plan C and parser_version rules."""
    assert_no_forbidden_raw_keys(anchor, field_name="anchor")
    parser_version = anchor.get("parser_version")
    if not isinstance(parser_version, str) or not parser_version.strip():
        raise RawTextPersistenceViolationError(
            "anchor must include a non-empty string 'parser_version'."
        )
    if strict_whitelist:
        _assert_whitelist(anchor, allowed_keys=ALLOWED_ANCHOR_KEYS, field_name="anchor")
    return dict(anchor)


def validate_normalized_metadata(
    metadata: Mapping[str, Any], *, strict_whitelist: bool = False
) -> dict[str, Any]:
    """Validate ``source_object_revisions.normalized_metadata``."""
    assert_no_forbidden_raw_keys(metadata, field_name="normalized_metadata")
    if strict_whitelist:
        _assert_whitelist(
            metadata,
            allowed_keys=ALLOWED_NORMALIZED_METADATA_KEYS,
            field_name="normalized_metadata",
        )
    return dict(metadata)


def validate_support_metadata(
    metadata: Mapping[str, Any], *, strict_whitelist: bool = False
) -> dict[str, Any]:
    """Validate ``fact_support.support_metadata``."""
    assert_no_forbidden_raw_keys(metadata, field_name="support_metadata")
    if strict_whitelist:
        _assert_whitelist(
            metadata,
            allowed_keys=ALLOWED_SUPPORT_METADATA_KEYS,
            field_name="support_metadata",
        )
    return dict(metadata)


def validate_generation_run_metadata(
    input_artifact_versions: Mapping[str, Any],
    run_metadata: Mapping[str, Any] | None = None,
    *,
    strict_whitelist: bool = False,
) -> None:
    """Validate ``generation_runs.input_artifact_versions`` and ``run_metadata``."""
    assert_no_forbidden_raw_keys(
        input_artifact_versions, field_name="input_artifact_versions"
    )
    if strict_whitelist:
        _assert_whitelist(
            input_artifact_versions,
            allowed_keys=ALLOWED_GENERATION_RUN_INPUT_KEYS,
            field_name="input_artifact_versions",
        )
    if run_metadata is not None:
        assert_no_forbidden_raw_keys(run_metadata, field_name="run_metadata")


def validate_token_secret_ref(token_secret_ref: str) -> str:
    """Validate ``source_accounts.token_secret_ref`` format and reject raw tokens."""
    if not isinstance(token_secret_ref, str):
        raise ValueError("token_secret_ref must be a string.")
    trimmed = token_secret_ref.strip()
    if not trimmed or len(trimmed) > 255:
        raise ValueError("token_secret_ref must be 1..255 characters.")
    if _RAW_TOKEN_PREFIX_PATTERN.match(trimmed):
        raise ValueError("token_secret_ref must not store a raw OAuth/PAT bearer token.")
    if not _TOKEN_SECRET_REF_PATTERN.match(trimmed):
        raise ValueError(
            "token_secret_ref must use an allowed secret reference scheme "
            "(secret://, vault://, kms://, sm://, envref://)."
        )
    return trimmed


def validate_sha256_hash(content_hash: str) -> str:
    """Validate SHA-256 content hash format (64 lowercase hex chars, optional sha256: prefix)."""
    if not isinstance(content_hash, str) or not _SHA256_HASH_PATTERN.match(content_hash):
        raise ValueError(
            "content_hash must be 64 lowercase hex characters with optional 'sha256:' prefix."
        )
    return content_hash
