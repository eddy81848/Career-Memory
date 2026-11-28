"""Unit tests for Plan C zero-quote Markdown/JSON export rendering (T-407, REF-07b)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from career_memory_api.exports import (
    EXPORT_POLICY_VERSION,
    ExportAtomCitation,
    ExportClaimRenderItem,
    ExportRenderInput,
    render_export_artifact,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ExportFormat,
    ExportPolicy,
    ExportVerificationClass,
)
from career_memory_api.schema_contracts.metadata_guard import find_forbidden_raw_keys

_HASH_REV = "sha256:" + ("1" * 64)
_HASH_SPAN = "sha256:" + ("2" * 64)


def _sample_render_input(
    *,
    fmt: ExportFormat,
    policy: ExportPolicy = ExportPolicy.SOURCE_VERIFIED_ONLY,
    verification: ExportVerificationClass = ExportVerificationClass.SOURCE_VERIFIED,
    atom_status: ClaimAtomStatus = ClaimAtomStatus.SUPPORTED,
) -> ExportRenderInput:
    ts = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
    return ExportRenderInput(
        export_id="exp-001",
        workspace_id="ws-001",
        format=fmt,
        export_policy=policy,
        policy_version=EXPORT_POLICY_VERSION,
        generated_at=ts,
        verification_calculated_at=ts,
        claims=(
            ExportClaimRenderItem(
                claim_id="claim-001",
                version=1,
                claim_text="Implemented retry queue for billing webhooks.",
                computed_verification=verification,
                atoms=(
                    ExportAtomCitation(
                        atom_id="atom-001",
                        ordinal=0,
                        atom_type=ClaimAtomType.ACTION,
                        atom_text="Implemented retry queue for billing webhooks",
                        status=atom_status,
                        citations=(
                            {
                                "canonical_url": "https://github.com/acme/billing/pull/42",
                                "source_object_revision_id": "rev-001",
                                "revision_content_hash": _HASH_REV,
                                "content_unit_id": "cu-001",
                                "unit_type": "PR_BODY_SECTION",
                                "locator": {
                                    "locator_spec_version": "cm-locator-v1",
                                    "canonicalization_version": "cm-canon-nfc-lf-v1",
                                    "char_start": 0,
                                    "char_end": 42,
                                    "char_length": 42,
                                    "utf8_byte_start": 0,
                                    "utf8_byte_end": 42,
                                    "utf8_byte_length": 42,
                                    "utf16_start": 0,
                                    "utf16_end": 42,
                                    "line_start": 1,
                                    "line_end": 1,
                                    "span_sha256": _HASH_SPAN,
                                },
                                "observed_at": "2026-11-27T10:00:00Z",
                                "checked_at": "2026-11-28T12:00:00Z",
                            },
                        ),
                    ),
                ),
            ),
        ),
    )


def test_render_markdown_and_json_zero_quote_citations_and_user_confirmed_notices() -> None:
    md_out = render_export_artifact(_sample_render_input(fmt=ExportFormat.MARKDOWN))
    assert md_out.rendered_markdown is not None
    assert md_out.rendered_json is None
    assert md_out.manifest["excludes_raw_source_quotes"] is True
    assert md_out.manifest["claim_versions"] == {"claim-001": 1}
    assert md_out.manifest["source_revision_ids"] == ["rev-001"]
    assert "https://github.com/acme/billing/pull/42" in md_out.rendered_markdown
    assert _HASH_REV in md_out.rendered_markdown
    assert _HASH_SPAN in md_out.rendered_markdown
    assert find_forbidden_raw_keys(md_out.manifest) == []
    assert find_forbidden_raw_keys(md_out.artifact_payload) == []

    json_out = render_export_artifact(
        _sample_render_input(
            fmt=ExportFormat.JSON,
            policy=ExportPolicy.ALLOW_USER_CONFIRMED,
            verification=ExportVerificationClass.MIXED,
            atom_status=ClaimAtomStatus.USER_CONFIRMED,
        )
    )
    assert json_out.rendered_json is not None
    assert json_out.rendered_markdown is None
    assert json_out.rendered_json["manifest"]["excludes_raw_source_quotes"] is True
    claim_entry = json_out.rendered_json["claims"][0]
    assert claim_entry["verification_limitation_notice"] is not None
    assert claim_entry["atoms"][0]["verification_limitation_notice"] is not None
    assert find_forbidden_raw_keys(json_out.rendered_json) == []
    assert find_forbidden_raw_keys(json_out.artifact_payload) == []
