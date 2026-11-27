"""Frozen production version identifiers and Option C persistence invariants."""

from __future__ import annotations

CANONICALIZATION_VERSION = "cm-canon-nfc-lf-v1"
PARSER_VERSION = "cm-unit-parser-v1"
LOCATOR_SPEC_VERSION = "cm-locator-v1"
RETENTION_CLASS_NONE = "NONE"

# Keys that MUST NEVER appear in any persisted ContentUnit/Anchor metadata dict.
FORBIDDEN_PERSISTED_RAW_TEXT_KEYS: frozenset[str] = frozenset(
    {
        "quote",
        "raw_body",
        "extracted_span",
        "raw_text",
        "snippet",
        "untrusted_text",
        "body_text",
    }
)
