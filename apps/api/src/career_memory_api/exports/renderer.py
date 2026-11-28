"""Deterministic Plan C Zero-Quote Markdown and JSON Export Renderer (T-407, REF-07b).

Enforces:
- Strictly ZERO raw source quotes, excerpts, snippets, or forbidden raw keys.
- Citations contain ONLY canonical_url, source_object_revision_id, revision_content_hash,
  content_unit_id, unit_type, cm-locator-v1 coordinates/span_sha256, observed_at, and checked_at.
- Embeds explicit verification limitation notices for USER_CONFIRMED / MIXED claims and atoms
  when exported under ``ExportPolicy.ALLOW_USER_CONFIRMED``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from career_memory_api.llm_gate import assert_no_raw_text_or_secret_keys
from career_memory_api.resolver.models import format_utc_timestamp
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ExportFormat,
    ExportPolicy,
    ExportVerificationClass,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys

_ALLOWED_LOCATOR_KEYS: tuple[str, ...] = (
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
)


@dataclass(frozen=True, slots=True)
class ExportAtomCitation:
    """Single ClaimAtom and its non-raw provenance citations for export rendering."""

    atom_id: str
    ordinal: int
    atom_type: ClaimAtomType
    atom_text: str
    status: ClaimAtomStatus
    citations: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ExportClaimRenderItem:
    """Single approved Claim version ready for zero-quote Markdown/JSON export rendering."""

    claim_id: str
    version: int
    claim_text: str
    computed_verification: ExportVerificationClass
    atoms: tuple[ExportAtomCitation, ...]


@dataclass(frozen=True, slots=True)
class ExportRenderInput:
    """Complete deterministic input for Plan C zero-quote export rendering."""

    export_id: str
    workspace_id: str
    format: ExportFormat
    export_policy: ExportPolicy
    policy_version: str
    generated_at: datetime
    verification_calculated_at: datetime
    claims: tuple[ExportClaimRenderItem, ...]


@dataclass(frozen=True, slots=True)
class ExportRenderOutput:
    """Deterministic zero-quote export manifest, DB-safe artifact_payload, and rendered output."""

    manifest: dict[str, Any]
    artifact_payload: dict[str, Any]
    rendered_markdown: str | None
    rendered_json: dict[str, Any] | None


def _sanitize_locator(raw_locator: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_locator, Mapping):
        return {}
    nested = raw_locator.get("locator")
    effective: Mapping[str, Any] = (
        {**raw_locator, **nested} if isinstance(nested, Mapping) else raw_locator
    )
    cleaned = {k: effective[k] for k in _ALLOWED_LOCATOR_KEYS if k in effective}
    assert_no_forbidden_raw_keys(cleaned, field_name="export.citation.locator")
    return cleaned


def _sanitize_citation(raw: Mapping[str, Any]) -> dict[str, Any]:
    assert_no_forbidden_raw_keys(raw, field_name="export.citation")
    citation: dict[str, Any] = {
        "canonical_url": str(raw["canonical_url"]),
        "source_object_revision_id": str(raw["source_object_revision_id"]),
        "revision_content_hash": str(raw["revision_content_hash"]),
        "content_unit_id": str(raw["content_unit_id"]),
        "unit_type": str(raw["unit_type"]),
        "locator": _sanitize_locator(raw.get("locator")),
        "observed_at": str(raw["observed_at"]),
        "checked_at": str(raw["checked_at"]),
    }
    assert_no_forbidden_raw_keys(citation, field_name="export.citation.sanitized")
    assert_no_raw_text_or_secret_keys(citation, context="export.citation.sanitized")
    return citation


def _claim_limitation_notice(verification: ExportVerificationClass) -> str | None:
    if verification is ExportVerificationClass.USER_CONFIRMED:
        return (
            "VERIFICATION LIMITATION NOTICE: This claim is USER_CONFIRMED only and is not "
            "backed by SOURCE_VERIFIED primary source provenance."
        )
    if verification is ExportVerificationClass.MIXED:
        return (
            "VERIFICATION LIMITATION NOTICE: This claim is MIXED — one or more atoms rely on "
            "USER_CONFIRMED attestation rather than SOURCE_VERIFIED primary source provenance."
        )
    return None


def _atom_limitation_notice(status: ClaimAtomStatus) -> str | None:
    if status is ClaimAtomStatus.USER_CONFIRMED:
        return (
            "Atom verified by USER_CONFIRMED attestation only (not SOURCE_VERIFIED by primary "
            "source provenance)."
        )
    return None


def render_export_artifact(render_input: ExportRenderInput) -> ExportRenderOutput:
    """Deterministically render a Plan C zero-quote Markdown or JSON export artifact."""
    generated_iso = format_utc_timestamp(render_input.generated_at)
    verified_iso = format_utc_timestamp(render_input.verification_calculated_at)

    claim_versions: dict[str, int] = {}
    source_revision_ids: set[str] = set()
    rendered_claims: list[dict[str, Any]] = []

    for claim in render_input.claims:
        claim_versions[claim.claim_id] = claim.version
        rendered_atoms: list[dict[str, Any]] = []
        for atom in sorted(claim.atoms, key=lambda a: (a.ordinal, a.atom_id)):
            sanitized_citations = [
                _sanitize_citation(cit)
                for cit in sorted(
                    atom.citations,
                    key=lambda c: (
                        str(c.get("source_object_revision_id", "")),
                        str(c.get("content_unit_id", "")),
                    ),
                )
            ]
            for sc in sanitized_citations:
                source_revision_ids.add(str(sc["source_object_revision_id"]))
            rendered_atoms.append(
                {
                    "atom_id": atom.atom_id,
                    "ordinal": atom.ordinal,
                    "atom_type": atom.atom_type.value,
                    "atom_text": atom.atom_text,
                    "status": atom.status.value,
                    "verification_limitation_notice": _atom_limitation_notice(atom.status),
                    "citations": sanitized_citations,
                }
            )
        rendered_claims.append(
            {
                "claim_id": claim.claim_id,
                "version": claim.version,
                "claim_text": claim.claim_text,
                "computed_verification": claim.computed_verification.value,
                "verification_limitation_notice": _claim_limitation_notice(
                    claim.computed_verification
                ),
                "atoms": rendered_atoms,
            }
        )

    manifest: dict[str, Any] = {
        "export_id": render_input.export_id,
        "format": render_input.format.value,
        "export_policy": render_input.export_policy.value,
        "policy_version": render_input.policy_version,
        "generated_at": generated_iso,
        "verification_calculated_at": verified_iso,
        "claim_versions": {k: claim_versions[k] for k in sorted(claim_versions.keys())},
        "source_revision_ids": sorted(source_revision_ids),
        "excludes_raw_source_quotes": True,
    }
    artifact_payload: dict[str, Any] = {
        "manifest": manifest,
        "claims": rendered_claims,
    }

    # Final Plan C + secret guard before returning
    assert_no_forbidden_raw_keys(manifest, field_name="exports.manifest")
    assert_no_raw_text_or_secret_keys(manifest, context="exports.manifest")
    assert_no_forbidden_raw_keys(artifact_payload, field_name="exports.artifact_payload")
    assert_no_raw_text_or_secret_keys(artifact_payload, context="exports.artifact_payload")

    if render_input.format is ExportFormat.JSON:
        return ExportRenderOutput(
            manifest=manifest,
            artifact_payload=artifact_payload,
            rendered_markdown=None,
            rendered_json=artifact_payload,
        )

    # Render deterministic Markdown with zero raw source quotes
    lines: list[str] = [
        "# Career Memory Verified Export",
        "",
        "## Export Manifest",
        f"- Export ID: `{manifest['export_id']}`",
        f"- Format: `{manifest['format']}`",
        f"- Export Policy: `{manifest['export_policy']}` (`{manifest['policy_version']}`)",
        f"- Generated At: `{manifest['generated_at']}`",
        f"- Verification Calculated At: `{manifest['verification_calculated_at']}`",
        "- Excludes Raw Source Quotes: `true`",
        f"- Source Revisions: {', '.join(f'`{r}`' for r in manifest['source_revision_ids']) or 'none'}",
        "",
        "## Approved Claims",
        "",
    ]
    for idx, c in enumerate(rendered_claims, start=1):
        lines.append(
            f"### {idx}. Claim `{c['claim_id']}` (v{c['version']}) — `{c['computed_verification']}`"
        )
        lines.append("")
        lines.append(str(c["claim_text"]))
        lines.append("")
        if c["verification_limitation_notice"] is not None:
            lines.append(f"> **Notice**: {c['verification_limitation_notice']}")
            lines.append("")
        for a in c["atoms"]:
            lines.append(
                f"- **[{a['atom_type']} · {a['status']}]** {a['atom_text']}"
            )
            if a["verification_limitation_notice"] is not None:
                lines.append(f"  - *Limitation*: {a['verification_limitation_notice']}")
            for cit in a["citations"]:
                loc = cit["locator"]
                char_range = f"chars {loc.get('char_start')}..{loc.get('char_end')}"
                line_range = f"lines {loc.get('line_start')}..{loc.get('line_end')}"
                span_hash = loc.get("span_sha256", "")
                lines.append(
                    f"  - Citation: {cit['canonical_url']} | rev=`{cit['source_object_revision_id']}` "
                    f"(`{cit['revision_content_hash']}`) | unit=`{cit['content_unit_id']}` "
                    f"(`{cit['unit_type']}`) | cm-locator-v1({char_range}, {line_range}, "
                    f"`{span_hash}`) | observed=`{cit['observed_at']}` | checked=`{cit['checked_at']}`"
                )
        lines.append("")

    rendered_md = "\n".join(lines).rstrip() + "\n"
    return ExportRenderOutput(
        manifest=manifest,
        artifact_payload=artifact_payload,
        rendered_markdown=rendered_md,
        rendered_json=None,
    )


__all__ = [
    "ExportAtomCitation",
    "ExportClaimRenderItem",
    "ExportRenderInput",
    "ExportRenderOutput",
    "render_export_artifact",
]
