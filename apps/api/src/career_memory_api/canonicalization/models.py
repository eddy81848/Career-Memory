"""Core typed models separating persisted non-raw-text metadata from ephemeral memory slices."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from .constants import (
    CANONICALIZATION_VERSION,
    FORBIDDEN_PERSISTED_RAW_TEXT_KEYS,
    LOCATOR_SPEC_VERSION,
    PARSER_VERSION,
    RETENTION_CLASS_NONE,
)
from .policy import DeterministicSkipRecord, ProcessedRange


class ContentUnitType(StrEnum):
    """Official 8 Content Unit types (Data Model §3.4 / T-103)."""

    PR_TITLE = "PR_TITLE"
    PR_BODY_SECTION = "PR_BODY_SECTION"
    ISSUE_SECTION = "ISSUE_SECTION"
    COMMENT = "COMMENT"
    REVIEW_COMMENT = "REVIEW_COMMENT"
    COMMIT_MESSAGE = "COMMIT_MESSAGE"
    DIFF_HUNK = "DIFF_HUNK"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


class SourceObjectKind(StrEnum):
    """Supported GitHub/git source object categories for source-aware parsing."""

    PR_TITLE = "PR_TITLE"
    PR_BODY = "PR_BODY"
    ISSUE_BODY = "ISSUE_BODY"
    COMMENT = "COMMENT"
    REVIEW_COMMENT = "REVIEW_COMMENT"
    COMMIT_MESSAGE = "COMMIT_MESSAGE"
    UNIFIED_DIFF = "UNIFIED_DIFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


class AnchorResolutionState(StrEnum):
    """Deterministic anchor validity classification for diff/review comment units."""

    RESOLVED = "RESOLVED"
    RENAMED_TRACKED = "RENAMED_TRACKED"
    OUTDATED_UNRESOLVED = "OUTDATED_UNRESOLVED"
    STALE_ANCHOR = "STALE_ANCHOR"


@dataclass(frozen=True, slots=True)
class CanonicalDocumentMetadata:
    """Persisted non-raw-text metadata for a canonicalized source revision."""

    canonicalization_version: str
    raw_sha256: str
    raw_byte_length: int
    bom_stripped: bool
    raw_bom_bytes: int
    content_sha256: str
    char_length: int
    utf8_byte_length: int
    utf16_length: int
    line_count: int
    retention_class: Literal["NONE"] = RETENTION_CLASS_NONE

    def __post_init__(self) -> None:
        if self.retention_class != RETENTION_CLASS_NONE:
            raise ValueError("Option C invariant: retention_class must always be 'NONE'.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonicalization_version": self.canonicalization_version,
            "raw_sha256": self.raw_sha256,
            "raw_byte_length": self.raw_byte_length,
            "bom_stripped": self.bom_stripped,
            "raw_bom_bytes": self.raw_bom_bytes,
            "content_sha256": self.content_sha256,
            "char_length": self.char_length,
            "utf8_byte_length": self.utf8_byte_length,
            "utf16_length": self.utf16_length,
            "line_count": self.line_count,
            "retention_class": self.retention_class,
        }


@dataclass(frozen=True, slots=True)
class EphemeralCanonicalDocument:
    """In-memory canonicalized document. Raw text is strictly ephemeral and redacted in repr/str."""

    metadata: CanonicalDocumentMetadata
    _normalized_text: str = field(repr=False)

    @property
    def text(self) -> str:
        """Return ephemeral canonicalized text for in-memory parsing/verification only."""
        return self._normalized_text

    def to_persisted_metadata_dict(self) -> dict[str, Any]:
        return self.metadata.to_dict()

    def __repr__(self) -> str:
        return (
            "EphemeralCanonicalDocument("
            f"canonicalization_version={self.metadata.canonicalization_version!r}, "
            f"content_sha256={self.metadata.content_sha256!r}, "
            f"char_length={self.metadata.char_length}, "
            f"utf8_byte_length={self.metadata.utf8_byte_length}, "
            "text='<REDACTED_EPHEMERAL_TEXT>')"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class CanonicalizationResult:
    """Outcome of canonicalizing raw source bytes."""

    status: Literal["CANONICALIZED", "SKIPPED"]
    document: EphemeralCanonicalDocument | None
    skip_record: DeterministicSkipRecord | None

    def __post_init__(self) -> None:
        if self.status == "CANONICALIZED":
            if self.document is None or self.skip_record is not None:
                raise ValueError("CANONICALIZED result must have document and no skip_record.")
        elif self.status == "SKIPPED":
            if self.document is not None or self.skip_record is None:
                raise ValueError("SKIPPED result must have skip_record and no document.")
        else:
            raise ValueError(f"Unsupported canonicalization status: {self.status!r}")


@dataclass(frozen=True, slots=True)
class SpanLocator:
    """Exact half-open [start, end) span coordinates over canonicalized text."""

    locator_spec_version: str
    canonicalization_version: str
    char_start: int
    char_end: int
    char_length: int
    utf8_byte_start: int
    utf8_byte_end: int
    utf8_byte_length: int
    utf16_start: int
    utf16_end: int
    line_start: int
    line_end: int
    span_sha256: str

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("SpanLocator requires 0 <= char_start < char_end.")
        if self.char_end - self.char_start != self.char_length:
            raise ValueError("SpanLocator char_length must equal char_end - char_start.")
        if self.utf8_byte_start < 0 or self.utf8_byte_end <= self.utf8_byte_start:
            raise ValueError("SpanLocator requires 0 <= utf8_byte_start < utf8_byte_end.")
        if self.utf8_byte_end - self.utf8_byte_start != self.utf8_byte_length:
            raise ValueError("SpanLocator utf8_byte_length must equal utf8_byte_end - utf8_byte_start.")
        if self.utf16_start < 0 or self.utf16_end <= self.utf16_start:
            raise ValueError("SpanLocator requires 0 <= utf16_start < utf16_end.")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise ValueError("SpanLocator requires 1 <= line_start <= line_end.")
        if not self.span_sha256.startswith("sha256:") or len(self.span_sha256) != 71:
            raise ValueError("SpanLocator span_sha256 must be 'sha256:<64-hex>'.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator_spec_version": self.locator_spec_version,
            "canonicalization_version": self.canonicalization_version,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "char_length": self.char_length,
            "utf8_byte_start": self.utf8_byte_start,
            "utf8_byte_end": self.utf8_byte_end,
            "utf8_byte_length": self.utf8_byte_length,
            "utf16_start": self.utf16_start,
            "utf16_end": self.utf16_end,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "span_sha256": self.span_sha256,
        }

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any],
        *,
        canonicalization_version: str = CANONICALIZATION_VERSION,
        locator_spec_version: str = LOCATOR_SPEC_VERSION,
    ) -> SpanLocator:
        """Construct a SpanLocator from a persisted dict or CLAIM-QA fixture locator."""
        return cls(
            locator_spec_version=str(data.get("locator_spec_version", locator_spec_version)),
            canonicalization_version=str(
                data.get("canonicalization_version", canonicalization_version)
            ),
            char_start=int(data["char_start"]),
            char_end=int(data["char_end"]),
            char_length=int(data["char_length"]),
            utf8_byte_start=int(data["utf8_byte_start"]),
            utf8_byte_end=int(data["utf8_byte_end"]),
            utf8_byte_length=int(data["utf8_byte_length"]),
            utf16_start=int(data["utf16_start"]),
            utf16_end=int(data["utf16_end"]),
            line_start=int(data["line_start"]),
            line_end=int(data["line_end"]),
            span_sha256=str(data["span_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class OriginalSourceLocator:
    """Structured non-raw-text GitHub/git provenance coordinates for a Content Unit."""

    section: str | None = None
    heading_level: int | None = None
    heading_sha256: str | None = None
    file_path: str | None = None
    previous_file_path: str | None = None
    is_renamed: bool = False
    hunk_header: str | None = None
    old_start: int | None = None
    old_lines: int | None = None
    new_start: int | None = None
    new_lines: int | None = None
    comment_id: str | None = None
    thread_id: str | None = None
    commit_sha: str | None = None
    original_commit_sha: str | None = None
    diff_side: str | None = None
    source_line_start: int | None = None
    source_line_end: int | None = None
    original_line_start: int | None = None
    original_line_end: int | None = None
    matched_hunk_unit_id: str | None = None
    symbol_name: str | None = None
    symbol_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "heading_level": self.heading_level,
            "heading_sha256": self.heading_sha256,
            "file_path": self.file_path,
            "previous_file_path": self.previous_file_path,
            "is_renamed": self.is_renamed,
            "hunk_header": self.hunk_header,
            "old_start": self.old_start,
            "old_lines": self.old_lines,
            "new_start": self.new_start,
            "new_lines": self.new_lines,
            "comment_id": self.comment_id,
            "thread_id": self.thread_id,
            "commit_sha": self.commit_sha,
            "original_commit_sha": self.original_commit_sha,
            "diff_side": self.diff_side,
            "source_line_start": self.source_line_start,
            "source_line_end": self.source_line_end,
            "original_line_start": self.original_line_start,
            "original_line_end": self.original_line_end,
            "matched_hunk_unit_id": self.matched_hunk_unit_id,
            "symbol_name": self.symbol_name,
            "symbol_kind": self.symbol_kind,
        }


@dataclass(frozen=True, slots=True)
class PersistedContentUnitMetadata:
    """Option C persisted Content Unit metadata (strictly zero raw text fields)."""

    unit_id: str
    unit_index: int
    unit_type: ContentUnitType
    source_content_sha256: str
    locator: SpanLocator
    original_locator: OriginalSourceLocator
    anchor_resolution_state: AnchorResolutionState = AnchorResolutionState.RESOLVED
    support_eligible: bool = True
    parser_version: str = PARSER_VERSION
    canonicalization_version: str = CANONICALIZATION_VERSION
    text_content: None = None
    content_ref: None = None
    retention_class: Literal["NONE"] = RETENTION_CLASS_NONE

    def __post_init__(self) -> None:
        if self.text_content is not None:
            raise ValueError("Option C violation: text_content must always be None.")
        if self.content_ref is not None:
            raise ValueError("Option C violation: content_ref must always be None.")
        if self.retention_class != RETENTION_CLASS_NONE:
            raise ValueError("Option C violation: retention_class must always be 'NONE'.")
        if (
            self.anchor_resolution_state
            in (AnchorResolutionState.OUTDATED_UNRESOLVED, AnchorResolutionState.STALE_ANCHOR)
            and self.support_eligible
        ):
            raise ValueError(
                "DATA-02 violation: OUTDATED_UNRESOLVED/STALE_ANCHOR units cannot be support_eligible."
            )

    def to_persisted_anchor_dict(self) -> dict[str, Any]:
        """Serialize strictly non-raw-text locator & provenance metadata for persistence."""
        payload: dict[str, Any] = {
            "unit_id": self.unit_id,
            "unit_index": self.unit_index,
            "unit_type": self.unit_type.value,
            "parser_version": self.parser_version,
            "canonicalization_version": self.canonicalization_version,
            "source_content_sha256": self.source_content_sha256,
            "anchor_resolution_state": self.anchor_resolution_state.value,
            "support_eligible": self.support_eligible,
            "section": self.original_locator.section,
            "file_path": self.original_locator.file_path,
            "previous_file_path": self.original_locator.previous_file_path,
            "hunk_header": self.original_locator.hunk_header,
            "char_start": self.locator.char_start,
            "char_end": self.locator.char_end,
            "char_length": self.locator.char_length,
            "utf8_byte_start": self.locator.utf8_byte_start,
            "utf8_byte_end": self.locator.utf8_byte_end,
            "utf8_byte_length": self.locator.utf8_byte_length,
            "utf16_start": self.locator.utf16_start,
            "utf16_end": self.locator.utf16_end,
            "line_start": self.locator.line_start,
            "line_end": self.locator.line_end,
            "span_sha256": self.locator.span_sha256,
            "locator": self.locator.to_dict(),
            "original_locator": self.original_locator.to_dict(),
            "text_content": None,
            "content_ref": None,
            "retention_class": RETENTION_CLASS_NONE,
        }
        assert not (FORBIDDEN_PERSISTED_RAW_TEXT_KEYS & set(payload.keys()))
        return payload


@dataclass(frozen=True, slots=True)
class EphemeralResolvedUnit:
    """In-memory resolved Content Unit pairing persisted metadata with its ephemeral slice."""

    metadata: PersistedContentUnitMetadata
    _text: str = field(repr=False)

    @property
    def text(self) -> str:
        """Return ephemeral unit text for in-memory processing only."""
        return self._text

    def __repr__(self) -> str:
        return (
            "EphemeralResolvedUnit("
            f"unit_id={self.metadata.unit_id!r}, "
            f"unit_type={self.metadata.unit_type.value!r}, "
            f"span_sha256={self.metadata.locator.span_sha256!r}, "
            f"anchor_resolution_state={self.metadata.anchor_resolution_state.value!r}, "
            "text='<REDACTED_EPHEMERAL_TEXT>')"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class EphemeralVerifiedSpan:
    """In-memory verified span extracted by locator/hash roundtrip."""

    locator: SpanLocator
    _text: str = field(repr=False)

    @property
    def text(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return (
            "EphemeralVerifiedSpan("
            f"char_range=[{self.locator.char_start}, {self.locator.char_end}), "
            f"span_sha256={self.locator.span_sha256!r}, "
            "text='<REDACTED_EPHEMERAL_TEXT>')"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class SpanVerificationResult:
    """Fail-closed result from extract_verified_span."""

    verified: bool
    status: Literal["VERIFIED", "FAILED"]
    error_codes: tuple[str, ...]
    span: EphemeralVerifiedSpan | None

    def __post_init__(self) -> None:
        if self.verified and (self.status != "VERIFIED" or self.span is None or self.error_codes):
            raise ValueError("VERIFIED result must carry a span and no error_codes.")
        if not self.verified and (self.status != "FAILED" or self.span is not None or not self.error_codes):
            raise ValueError("FAILED result must carry error_codes and span=None.")


@dataclass(frozen=True, slots=True)
class CommentContext:
    """Structured provenance for PR/Issue comments."""

    comment_id: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class CommitContext:
    """Structured provenance for commit messages."""

    commit_sha: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewCommentContext:
    """Structured provenance and anchor coordinates for GitHub PR review comments."""

    comment_id: str | None = None
    thread_id: str | None = None
    commit_sha: str | None = None
    original_commit_sha: str | None = None
    file_path: str | None = None
    previous_file_path: str | None = None
    diff_side: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    original_line_start: int | None = None
    original_line_end: int | None = None
    is_outdated: bool = False
    anchored_hunk_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SymbolChangeContext:
    """Structured provenance for symbol-level code change units."""

    file_path: str
    symbol_name: str
    symbol_kind: str
    previous_file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True, slots=True)
class ContentUnitParseResult:
    """Complete deterministic output of source-aware content unit segmentation."""

    units: tuple[EphemeralResolvedUnit, ...]
    persisted_metadata: tuple[PersistedContentUnitMetadata, ...]
    skip_records: tuple[DeterministicSkipRecord, ...]
    is_partial: bool
    processed_range: ProcessedRange
