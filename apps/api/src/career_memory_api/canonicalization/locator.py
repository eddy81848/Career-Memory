"""Exact Unicode code point, UTF-8 byte, UTF-16 code unit, and line offset calculations."""

from __future__ import annotations

from .constants import CANONICALIZATION_VERSION, LOCATOR_SPEC_VERSION
from .models import (
    EphemeralCanonicalDocument,
    EphemeralVerifiedSpan,
    SpanLocator,
    SpanVerificationResult,
)
from .normalizer import sha256_prefixed_text


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _slice_utf16(text: str, start: int, end: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[start * 2 : end * 2].decode("utf-16-le", errors="strict")


def _line_number_at(text: str, char_index: int) -> int:
    return text.count("\n", 0, char_index) + 1


class OffsetCursor:
    """Monotonic O(N) cursor for computing exact char/UTF-8/UTF-16/line offsets."""

    __slots__ = ("_text", "_char_pos", "_utf8_pos", "_utf16_pos", "_line_no")

    def __init__(self, normalized_text: str) -> None:
        self._text = normalized_text
        self._char_pos = 0
        self._utf8_pos = 0
        self._utf16_pos = 0
        self._line_no = 1

    def _advance_to(self, target_char: int) -> None:
        if target_char < self._char_pos or target_char > len(self._text):
            raise ValueError(
                f"OffsetCursor requires monotonic index in [{self._char_pos}, {len(self._text)}]."
            )
        if target_char == self._char_pos:
            return
        delta = self._text[self._char_pos : target_char]
        self._utf8_pos += len(delta.encode("utf-8"))
        self._utf16_pos += _utf16_len(delta)
        self._line_no += delta.count("\n")
        self._char_pos = target_char

    def locate(self, char_start: int, char_end: int) -> SpanLocator:
        if char_start < 0 or char_end <= char_start or char_end > len(self._text):
            raise ValueError(
                f"Span [{char_start}, {char_end}) out of bounds for len={len(self._text)}."
            )
        self._advance_to(char_start)
        utf8_start = self._utf8_pos
        utf16_start = self._utf16_pos
        line_start = self._line_no

        span_text = self._text[char_start:char_end]
        span_utf8_len = len(span_text.encode("utf-8"))
        span_utf16_len = _utf16_len(span_text)
        # Line number of the last character in [char_start, char_end)
        internal_newlines = span_text[:-1].count("\n")
        line_end = line_start + internal_newlines

        return SpanLocator(
            locator_spec_version=LOCATOR_SPEC_VERSION,
            canonicalization_version=CANONICALIZATION_VERSION,
            char_start=char_start,
            char_end=char_end,
            char_length=char_end - char_start,
            utf8_byte_start=utf8_start,
            utf8_byte_end=utf8_start + span_utf8_len,
            utf8_byte_length=span_utf8_len,
            utf16_start=utf16_start,
            utf16_end=utf16_start + span_utf16_len,
            line_start=line_start,
            line_end=line_end,
            span_sha256=sha256_prefixed_text(span_text),
        )


def build_span_locator(
    normalized_text: str,
    char_start: int,
    char_end: int,
) -> SpanLocator:
    """Compute a deterministic SpanLocator for half-open [char_start, char_end)."""
    if char_start < 0 or char_end <= char_start or char_end > len(normalized_text):
        raise ValueError(
            f"Span [{char_start}, {char_end}) out of bounds for len={len(normalized_text)}."
        )
    prefix = normalized_text[:char_start]
    span_text = normalized_text[char_start:char_end]

    utf8_byte_start = len(prefix.encode("utf-8"))
    utf8_byte_length = len(span_text.encode("utf-8"))
    utf8_byte_end = utf8_byte_start + utf8_byte_length

    utf16_start = _utf16_len(prefix)
    utf16_end = utf16_start + _utf16_len(span_text)

    line_start = _line_number_at(normalized_text, char_start)
    line_end = _line_number_at(normalized_text, max(char_start, char_end - 1))

    return SpanLocator(
        locator_spec_version=LOCATOR_SPEC_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
        char_start=char_start,
        char_end=char_end,
        char_length=len(span_text),
        utf8_byte_start=utf8_byte_start,
        utf8_byte_end=utf8_byte_end,
        utf8_byte_length=utf8_byte_length,
        utf16_start=utf16_start,
        utf16_end=utf16_end,
        line_start=line_start,
        line_end=line_end,
        span_sha256=sha256_prefixed_text(span_text),
    )


def extract_verified_span(
    document: EphemeralCanonicalDocument | str,
    locator: SpanLocator,
    *,
    expected_source_content_sha256: str | None = None,
) -> SpanVerificationResult:
    """Fail-closed roundtrip extraction verifying char, UTF-8 byte, UTF-16, line, and SHA-256."""
    if isinstance(document, EphemeralCanonicalDocument):
        text = document.text
        actual_source_sha = document.metadata.content_sha256
    else:
        text = document
        actual_source_sha = (
            sha256_prefixed_text(text) if expected_source_content_sha256 is not None else None
        )

    errors: list[str] = []

    if (
        expected_source_content_sha256 is not None
        and actual_source_sha != expected_source_content_sha256
    ):
        errors.append("SOURCE_CONTENT_HASH_MISMATCH")

    if locator.canonicalization_version != CANONICALIZATION_VERSION:
        errors.append("CANONICALIZATION_VERSION_MISMATCH")

    if locator.char_start < 0 or locator.char_end <= locator.char_start or locator.char_end > len(text):
        errors.append("CHAR_OFFSET_OUT_OF_BOUNDS")
        return SpanVerificationResult(
            verified=False,
            status="FAILED",
            error_codes=tuple(errors),
            span=None,
        )

    char_slice = text[locator.char_start : locator.char_end]
    if len(char_slice) != locator.char_length:
        errors.append("CHAR_LENGTH_MISMATCH")

    encoded_utf8 = text.encode("utf-8")
    if (
        locator.utf8_byte_start < 0
        or locator.utf8_byte_end <= locator.utf8_byte_start
        or locator.utf8_byte_end > len(encoded_utf8)
    ):
        errors.append("UTF8_BYTE_OFFSET_MISMATCH")
    else:
        try:
            byte_slice = encoded_utf8[locator.utf8_byte_start : locator.utf8_byte_end].decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError:
            errors.append("UTF8_BYTE_OFFSET_MISMATCH")
        else:
            if byte_slice != char_slice or (
                locator.utf8_byte_end - locator.utf8_byte_start != locator.utf8_byte_length
            ):
                errors.append("UTF8_BYTE_OFFSET_MISMATCH")

    total_u16 = _utf16_len(text)
    if (
        locator.utf16_start < 0
        or locator.utf16_end <= locator.utf16_start
        or locator.utf16_end > total_u16
    ):
        errors.append("UTF16_OFFSET_MISMATCH")
    else:
        try:
            u16_slice = _slice_utf16(text, locator.utf16_start, locator.utf16_end)
        except UnicodeDecodeError:
            errors.append("UTF16_OFFSET_MISMATCH")
        else:
            if u16_slice != char_slice:
                errors.append("UTF16_OFFSET_MISMATCH")

    expected_line_start = _line_number_at(text, locator.char_start)
    expected_line_end = _line_number_at(text, max(locator.char_start, locator.char_end - 1))
    if locator.line_start != expected_line_start or locator.line_end != expected_line_end:
        errors.append("LINE_RANGE_MISMATCH")

    if sha256_prefixed_text(char_slice) != locator.span_sha256:
        errors.append("SPAN_HASH_MISMATCH")

    if errors:
        return SpanVerificationResult(
            verified=False,
            status="FAILED",
            error_codes=tuple(errors),
            span=None,
        )

    return SpanVerificationResult(
        verified=True,
        status="VERIFIED",
        error_codes=(),
        span=EphemeralVerifiedSpan(locator=locator, _text=char_slice),
    )
