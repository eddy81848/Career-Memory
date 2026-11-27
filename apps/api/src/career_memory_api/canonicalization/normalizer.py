"""Deterministic UTF-8 decoding, binary/budget enforcement, and cm-canon-nfc-lf-v1 normalization."""

from __future__ import annotations

import hashlib
import unicodedata

from .constants import CANONICALIZATION_VERSION
from .models import (
    CanonicalDocumentMetadata,
    CanonicalizationResult,
    EphemeralCanonicalDocument,
)
from .policy import (
    CanonicalizationBudgetConfig,
    DeterministicSkipRecord,
    ProcessedRange,
    SkipReasonCode,
    classify_generated_file,
    classify_minified_file_path,
    compute_control_char_ratio,
    detect_binary_magic,
    is_minified_text,
    is_supported_mime_type,
)


def sha256_prefixed_bytes(data: bytes) -> str:
    """Return 'sha256:<64-lowercase-hex>' digest of raw bytes."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_prefixed_text(text: str) -> str:
    """Return 'sha256:<64-lowercase-hex>' digest of UTF-8 encoded text."""
    return sha256_prefixed_bytes(text.encode("utf-8"))


def normalize_canonical_text(raw_text: str) -> tuple[str, bool]:
    """Apply frozen cm-canon-nfc-lf-v1 normalization without whitespace trimming.

    Pipeline order:
    1. Strip at most one leading UTF-8 BOM (U+FEFF).
    2. Unify line endings: CRLF ('\\r\\n') and standalone CR ('\\r') -> LF ('\\n').
    3. Apply Unicode NFC normalization.
    4. Preserve all indentation, spaces, tabs, and trailing newlines verbatim.
    """
    bom_stripped = False
    text = raw_text
    if text.startswith("\ufeff"):
        text = text[1:]
        bom_stripped = True
    unified_newlines = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = unicodedata.normalize("NFC", unified_newlines)
    return normalized, bom_stripped


def _make_skip_result(
    *,
    reason_code: SkipReasonCode,
    raw_bytes_total: int,
    budget_limit: int | None = None,
    file_path: str | None = None,
    detail_code: str | None = None,
    raw_bytes_processed: int = 0,
    chars_total: int = 0,
    lines_total: int | None = None,
    skipped_from_byte: int | None = 0,
    skipped_to_byte: int | None = None,
) -> CanonicalizationResult:
    return CanonicalizationResult(
        status="SKIPPED",
        document=None,
        skip_record=DeterministicSkipRecord(
            reason_code=reason_code,
            processed_range=ProcessedRange(
                raw_bytes_processed=raw_bytes_processed,
                raw_bytes_total=raw_bytes_total,
                chars_processed=0,
                chars_total=chars_total,
                lines_processed=0,
                lines_total=lines_total,
                skipped_from_byte=skipped_from_byte,
                skipped_to_byte=raw_bytes_total if skipped_to_byte is None else skipped_to_byte,
            ),
            observed_bytes=raw_bytes_total,
            budget_limit=budget_limit,
            is_partial=False,
            file_path=file_path,
            detail_code=detail_code,
        ),
    )


def canonicalize_source_bytes(
    raw_bytes: bytes,
    *,
    file_path: str | None = None,
    mime_type: str | None = None,
    is_diff: bool = False,
    budget: CanonicalizationBudgetConfig | None = None,
) -> CanonicalizationResult:
    """Validate, decode strictly as UTF-8, and canonicalize raw source bytes.

    Hostile, binary, invalid-UTF-8, oversized, generated, or minified payloads
    never crash and never silently drop; they return a CanonicalizationResult
    with status='SKIPPED' and an explicit DeterministicSkipRecord.
    """
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be a bytes instance.")

    cfg = budget or CanonicalizationBudgetConfig()
    total_bytes = len(raw_bytes)

    # 1. Explicit MIME policy
    if mime_type is not None and not is_supported_mime_type(mime_type):
        return _make_skip_result(
            reason_code=SkipReasonCode.UNSUPPORTED_MIME,
            raw_bytes_total=total_bytes,
            file_path=file_path,
            detail_code="UNSUPPORTED_MIME_TYPE",
        )

    # 2. Path-based generated artifact / minified asset policy (for single-file inputs)
    if not is_diff:
        gen_detail = classify_generated_file(file_path, cfg)
        if gen_detail is not None:
            return _make_skip_result(
                reason_code=SkipReasonCode.GENERATED_ARTIFACT,
                raw_bytes_total=total_bytes,
                file_path=file_path,
                detail_code=gen_detail,
            )

        min_detail = classify_minified_file_path(file_path, cfg)
        if min_detail is not None:
            return _make_skip_result(
                reason_code=SkipReasonCode.MINIFIED_ASSET,
                raw_bytes_total=total_bytes,
                file_path=file_path,
                detail_code=min_detail,
            )

    # 3. Byte budget check BEFORE decoding (prevents OOM on diff bombs / oversized payloads)
    byte_limit = cfg.max_diff_bytes if is_diff else cfg.max_raw_bytes
    if total_bytes > byte_limit:
        return _make_skip_result(
            reason_code=SkipReasonCode.DIFF_BOMB_LIMIT if is_diff else SkipReasonCode.OVERSIZED_INPUT,
            raw_bytes_total=total_bytes,
            budget_limit=byte_limit,
            file_path=file_path,
            detail_code="MAX_DIFF_BYTES_EXCEEDED" if is_diff else "MAX_RAW_BYTES_EXCEEDED",
        )

    # 4. Binary signature & NUL byte detection before decoding
    magic_detail = detect_binary_magic(raw_bytes[: cfg.binary_sniff_bytes])
    if magic_detail is not None:
        return _make_skip_result(
            reason_code=SkipReasonCode.BINARY_CONTENT,
            raw_bytes_total=total_bytes,
            file_path=file_path,
            detail_code=magic_detail,
        )

    nul_index = raw_bytes.find(b"\x00")
    if nul_index != -1:
        return _make_skip_result(
            reason_code=SkipReasonCode.BINARY_CONTENT,
            raw_bytes_total=total_bytes,
            file_path=file_path,
            detail_code="NUL_BYTE_DETECTED",
            raw_bytes_processed=nul_index,
            skipped_from_byte=nul_index,
        )

    # 5. Strict UTF-8 decoding (errors='replace' is strictly forbidden)
    try:
        decoded_text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return _make_skip_result(
            reason_code=SkipReasonCode.INVALID_ENCODING,
            raw_bytes_total=total_bytes,
            file_path=file_path,
            detail_code=f"UTF8_DECODE_ERROR_AT_BYTE_{exc.start}",
            raw_bytes_processed=exc.start,
            skipped_from_byte=exc.start,
        )

    # 6. Control-character binary heuristic on decoded text
    if compute_control_char_ratio(decoded_text) > cfg.max_control_byte_ratio:
        return _make_skip_result(
            reason_code=SkipReasonCode.BINARY_CONTENT,
            raw_bytes_total=total_bytes,
            file_path=file_path,
            detail_code="CONTROL_BYTE_RATIO_EXCEEDED",
        )

    # 7. Canonical normalization (cm-canon-nfc-lf-v1)
    raw_sha256 = sha256_prefixed_bytes(raw_bytes)
    normalized_text, bom_stripped = normalize_canonical_text(decoded_text)
    raw_bom_bytes = 3 if bom_stripped and raw_bytes.startswith(b"\xef\xbb\xbf") else 0

    canonical_utf8 = normalized_text.encode("utf-8")
    line_count = normalized_text.count("\n") + 1 if normalized_text else 0

    # 8. Content-level minified & line-length policies (for single-file non-diff inputs;
    #    multi-file diffs evaluate generated/minified/line limits per file/hunk).
    if not is_diff and normalized_text:
        if is_minified_text(normalized_text, cfg):
            return _make_skip_result(
                reason_code=SkipReasonCode.MINIFIED_ASSET,
                raw_bytes_total=total_bytes,
                budget_limit=cfg.minified_avg_line_bytes,
                file_path=file_path,
                detail_code="MINIFIED_CONTENT_HEURISTIC",
                chars_total=len(normalized_text),
                lines_total=line_count,
            )

        for line in normalized_text.split("\n"):
            line_bytes = len(line.encode("utf-8"))
            if line_bytes > cfg.max_line_bytes:
                return _make_skip_result(
                    reason_code=SkipReasonCode.LINE_LENGTH_EXCEEDED,
                    raw_bytes_total=total_bytes,
                    budget_limit=cfg.max_line_bytes,
                    file_path=file_path,
                    detail_code="MAX_LINE_BYTES_EXCEEDED",
                    chars_total=len(normalized_text),
                    lines_total=line_count,
                )

    metadata = CanonicalDocumentMetadata(
        canonicalization_version=CANONICALIZATION_VERSION,
        raw_sha256=raw_sha256,
        raw_byte_length=total_bytes,
        bom_stripped=bom_stripped,
        raw_bom_bytes=raw_bom_bytes,
        content_sha256=sha256_prefixed_bytes(canonical_utf8),
        char_length=len(normalized_text),
        utf8_byte_length=len(canonical_utf8),
        utf16_length=len(normalized_text.encode("utf-16-le")) // 2,
        line_count=line_count,
    )
    return CanonicalizationResult(
        status="CANONICALIZED",
        document=EphemeralCanonicalDocument(metadata=metadata, _normalized_text=normalized_text),
        skip_record=None,
    )
