"""Deterministic Content Unit canonicalization, locator contracts, and hostile-input defenses.

Implements M1-T103A (`T-103` + `T-103A`) using Python standard library only.
"""

from __future__ import annotations

from .constants import (
    CANONICALIZATION_VERSION,
    FORBIDDEN_PERSISTED_RAW_TEXT_KEYS,
    LOCATOR_SPEC_VERSION,
    PARSER_VERSION,
    RETENTION_CLASS_NONE,
)
from .isolation import (
    UntrustedDataSlot,
    UntrustedLLMDataEnvelope,
    UntrustedSourceContextBuilder,
    build_untrusted_llm_data_envelope,
    redact_for_logging,
    render_untrusted_text_safe,
)
from .locator import OffsetCursor, build_span_locator, extract_verified_span
from .models import (
    AnchorResolutionState,
    CanonicalDocumentMetadata,
    CanonicalizationResult,
    CommentContext,
    CommitContext,
    ContentUnitParseResult,
    ContentUnitType,
    EphemeralCanonicalDocument,
    EphemeralResolvedUnit,
    EphemeralVerifiedSpan,
    OriginalSourceLocator,
    PersistedContentUnitMetadata,
    ReviewCommentContext,
    SourceObjectKind,
    SpanLocator,
    SpanVerificationResult,
    SymbolChangeContext,
)
from .normalizer import (
    canonicalize_source_bytes,
    normalize_canonical_text,
    sha256_prefixed_bytes,
    sha256_prefixed_text,
)
from .parser import parse_content_units, resolve_review_comment_anchor
from .policy import (
    CanonicalizationBudgetConfig,
    DeterministicSkipRecord,
    ProcessedRange,
    SkipReasonCode,
)

__all__ = [
    "AnchorResolutionState",
    "CANONICALIZATION_VERSION",
    "CanonicalDocumentMetadata",
    "CanonicalizationBudgetConfig",
    "CanonicalizationResult",
    "CommentContext",
    "CommitContext",
    "ContentUnitParseResult",
    "ContentUnitType",
    "DeterministicSkipRecord",
    "EphemeralCanonicalDocument",
    "EphemeralResolvedUnit",
    "EphemeralVerifiedSpan",
    "FORBIDDEN_PERSISTED_RAW_TEXT_KEYS",
    "LOCATOR_SPEC_VERSION",
    "OffsetCursor",
    "OriginalSourceLocator",
    "PARSER_VERSION",
    "PersistedContentUnitMetadata",
    "ProcessedRange",
    "RETENTION_CLASS_NONE",
    "ReviewCommentContext",
    "SkipReasonCode",
    "SourceObjectKind",
    "SpanLocator",
    "SpanVerificationResult",
    "SymbolChangeContext",
    "UntrustedDataSlot",
    "UntrustedLLMDataEnvelope",
    "UntrustedSourceContextBuilder",
    "build_span_locator",
    "build_untrusted_llm_data_envelope",
    "canonicalize_source_bytes",
    "extract_verified_span",
    "normalize_canonical_text",
    "parse_content_units",
    "redact_for_logging",
    "render_untrusted_text_safe",
    "resolve_review_comment_anchor",
    "sha256_prefixed_bytes",
    "sha256_prefixed_text",
]
