"""T-103A Step 1 & 4 tests for binary, invalid UTF-8, oversized, diff bomb, generated, and minified inputs."""

from __future__ import annotations

import pytest

from career_memory_api.canonicalization import (
    CanonicalizationBudgetConfig,
    SkipReasonCode,
    SourceObjectKind,
    canonicalize_source_bytes,
    parse_content_units,
)


def test_invalid_utf8_is_classified_deterministically_never_silently_replaced() -> None:
    """Invalid UTF-8 byte sequences must skip with INVALID_ENCODING, never decode with errors='replace'."""
    bad_utf8 = b"valid prefix \xff\xfe invalid utf8 suffix"
    res = canonicalize_source_bytes(bad_utf8)
    assert res.status == "SKIPPED"
    assert res.document is None
    assert res.skip_record is not None
    assert res.skip_record.reason_code == SkipReasonCode.INVALID_ENCODING
    assert res.skip_record.observed_bytes == len(bad_utf8)
    assert res.skip_record.processed_range.raw_bytes_total == len(bad_utf8)
    assert res.skip_record.is_partial is False


@pytest.mark.parametrize(
    ("payload", "expected_detail_prefix"),
    [
        (b"text before\x00binary nul byte", "NUL_BYTE"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "BINARY_MAGIC"),
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "BINARY_MAGIC"),
        (b"PK\x03\x04\x14\x00\x00\x00", "BINARY_MAGIC"),
    ],
)
def test_binary_payloads_skip_with_binary_content(
    payload: bytes,
    expected_detail_prefix: str,
) -> None:
    res = canonicalize_source_bytes(payload)
    assert res.status == "SKIPPED"
    assert res.document is None
    assert res.skip_record is not None
    assert res.skip_record.reason_code == SkipReasonCode.BINARY_CONTENT
    assert res.skip_record.observed_bytes == len(payload)
    assert res.skip_record.processed_range.raw_bytes_total == len(payload)
    assert res.skip_record.detail_code is not None
    assert res.skip_record.detail_code.startswith(expected_detail_prefix)


def test_unsupported_mime_skips_deterministically() -> None:
    res = canonicalize_source_bytes(b"fake pdf payload", mime_type="application/pdf")
    assert res.status == "SKIPPED"
    assert res.skip_record is not None
    assert res.skip_record.reason_code == SkipReasonCode.UNSUPPORTED_MIME


@pytest.mark.parametrize(
    "file_path",
    [
        "package-lock.json",
        "apps/web/yarn.lock",
        "apps/api/uv.lock",
        "gen/proto/billing.pb.go",
        "dist/app.js",
        "vendor/github.com/pkg/errors.go",
    ],
)
def test_generated_artifacts_skip_explicitly_without_silent_drop(file_path: str) -> None:
    res = canonicalize_source_bytes(b'{"lockfileVersion": 3}\n', file_path=file_path)
    assert res.status == "SKIPPED"
    assert res.document is None
    assert res.skip_record is not None
    assert res.skip_record.reason_code == SkipReasonCode.GENERATED_ARTIFACT
    assert res.skip_record.file_path == file_path


def test_minified_single_line_asset_and_long_line_limits() -> None:
    """Both path-based and content-based minified bundles plus overlong lines skip explicitly."""
    # 1. Path-based minified asset
    res_path = canonicalize_source_bytes(b"var a=1;var b=2;", file_path="static/app.min.js")
    assert res_path.status == "SKIPPED"
    assert res_path.skip_record is not None
    assert res_path.skip_record.reason_code == SkipReasonCode.MINIFIED_ASSET

    # 2. Content-based single-line minified JS bundle (no .min.js extension)
    minified_js = ("function a(x){return x+1;}var b=a(2);const c=b*3;" * 120).encode("utf-8")
    res_min = canonicalize_source_bytes(minified_js, file_path="static/bundle.js")
    assert res_min.status == "SKIPPED"
    assert res_min.skip_record is not None
    assert res_min.skip_record.reason_code == SkipReasonCode.MINIFIED_ASSET
    assert res_min.skip_record.observed_bytes == len(minified_js)

    # 3. Extremely long single-line non-minified prose/log -> LINE_LENGTH_EXCEEDED
    budget = CanonicalizationBudgetConfig(max_line_bytes=200)
    long_prose = ("word " * 80).encode("utf-8")  # high whitespace ratio -> not minified, exceeds 200B line
    res_line = canonicalize_source_bytes(long_prose, budget=budget)
    assert res_line.status == "SKIPPED"
    assert res_line.skip_record is not None
    assert res_line.skip_record.reason_code == SkipReasonCode.LINE_LENGTH_EXCEEDED
    assert res_line.skip_record.budget_limit == 200


def test_10mb_synthetic_diff_bomb_is_bounded_with_explicit_processed_range() -> None:
    """10MB+ synthetic diff bomb must abort cleanly with OVERSIZED_INPUT / DIFF_BOMB_LIMIT."""
    # 10 MiB synthetic diff payload
    chunk = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " x = 1\n"
        "+y = 2\n"
    ).encode("utf-8")
    diff_bomb = chunk * ((10 * 1024 * 1024 // len(chunk)) + 1)
    assert len(diff_bomb) >= 10 * 1024 * 1024

    # Default budget rejects 10MB diff before decoding without OOM
    res = canonicalize_source_bytes(diff_bomb, is_diff=True)
    assert res.status == "SKIPPED"
    assert res.document is None
    assert res.skip_record is not None
    assert res.skip_record.reason_code == SkipReasonCode.DIFF_BOMB_LIMIT
    assert res.skip_record.observed_bytes == len(diff_bomb)
    assert res.skip_record.budget_limit == CanonicalizationBudgetConfig().max_diff_bytes
    assert res.skip_record.processed_range.raw_bytes_total == len(diff_bomb)
    assert res.skip_record.processed_range.raw_bytes_processed == 0

    # Non-diff oversized input -> OVERSIZED_INPUT
    res_doc = canonicalize_source_bytes(diff_bomb, is_diff=False)
    assert res_doc.status == "SKIPPED"
    assert res_doc.skip_record is not None
    assert res_doc.skip_record.reason_code == SkipReasonCode.OVERSIZED_INPUT
    assert res_doc.skip_record.budget_limit == CanonicalizationBudgetConfig().max_raw_bytes


def test_multi_file_diff_records_partial_processed_range_for_generated_binary_and_file_caps() -> None:
    """Multi-file diff with valid file + lockfile + binary file + file-count cap never silently drops."""
    diff_text = (
        "diff --git a/src/service.py b/src/service.py\n"
        "--- a/src/service.py\n"
        "+++ b/src/service.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def ok():\n"
        "+    return 1\n"
        "     pass\n"
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1,1 +1,2 @@\n"
        " {\n"
        '+  "lockfileVersion": 3\n'
        "diff --git a/assets/logo.png b/assets/logo.png\n"
        "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
        "diff --git a/src/extra.py b/src/extra.py\n"
        "--- a/src/extra.py\n"
        "+++ b/src/extra.py\n"
        "@@ -1,1 +1,2 @@\n"
        " a = 1\n"
        "+b = 2\n"
        "diff --git a/src/overflow.py b/src/overflow.py\n"
        "--- a/src/overflow.py\n"
        "+++ b/src/overflow.py\n"
        "@@ -1,1 +1,2 @@\n"
        " c = 1\n"
        "+d = 2\n"
    )
    budget = CanonicalizationBudgetConfig(max_files_per_diff=4)
    doc_res = canonicalize_source_bytes(diff_text.encode("utf-8"), is_diff=True, budget=budget)
    assert doc_res.document is not None

    parsed = parse_content_units(doc_res.document, SourceObjectKind.UNIFIED_DIFF, budget=budget)
    assert parsed.is_partial is True

    # Valid code files parsed: src/service.py and src/extra.py
    parsed_files = [u.metadata.original_locator.file_path for u in parsed.units]
    assert parsed_files == ["src/service.py", "src/extra.py"]

    # Explicit skip records emitted for lockfile, binary file, AND 5th file exceeding max_files_per_diff
    skip_reasons = [s.reason_code for s in parsed.skip_records]
    assert SkipReasonCode.GENERATED_ARTIFACT in skip_reasons
    assert SkipReasonCode.BINARY_CONTENT in skip_reasons
    assert SkipReasonCode.DIFF_BOMB_LIMIT in skip_reasons

    bomb_skip = [s for s in parsed.skip_records if s.reason_code == SkipReasonCode.DIFF_BOMB_LIMIT][0]
    assert bomb_skip.is_partial is True
    assert bomb_skip.processed_range.files_processed == 4
    assert bomb_skip.processed_range.files_total == 5
    assert bomb_skip.processed_range.skipped_from_char is not None
    assert bomb_skip.processed_range.skipped_to_char == len(doc_res.document.text)


def test_hunk_line_count_cap_skips_oversized_hunk_with_processed_range() -> None:
    lines = "\n".join(f"+line_{i} = {i}" for i in range(25))
    diff_text = (
        "diff --git a/src/big.py b/src/big.py\n"
        "--- a/src/big.py\n"
        "+++ b/src/big.py\n"
        "@@ -1,1 +1,26 @@\n"
        " header = True\n"
        f"{lines}\n"
    )
    budget = CanonicalizationBudgetConfig(max_lines_per_hunk=10)
    doc = canonicalize_source_bytes(diff_text.encode("utf-8"), is_diff=True, budget=budget).document
    assert doc is not None
    parsed = parse_content_units(doc, SourceObjectKind.UNIFIED_DIFF, budget=budget)
    assert parsed.units == ()
    assert parsed.is_partial is True
    assert len(parsed.skip_records) == 1
    assert parsed.skip_records[0].reason_code == SkipReasonCode.DIFF_BOMB_LIMIT
    assert parsed.skip_records[0].budget_limit == 10
    assert parsed.skip_records[0].hunk_header == "@@ -1,1 +1,26 @@"


def test_per_file_hunk_cap_and_utf8_byte_processed_range_tracking() -> None:
    """UTF-8 multibyte diff preserves exact byte vs char processed_range on partial hunk cap."""
    diff_text = (
        "diff --git a/src/korean.py b/src/korean.py\n"
        "--- a/src/korean.py\n"
        "+++ b/src/korean.py\n"
        "@@ -1,1 +1,2 @@\n"
        ' msg = "안녕하세요"\n'
        '+note = "결제 재시도 큐"\n'
        "@@ -10,1 +10,2 @@\n"
        " x = 1\n"
        "+y = 2\n"
    )
    budget = CanonicalizationBudgetConfig(max_hunks_per_file=1)
    doc = canonicalize_source_bytes(diff_text.encode("utf-8"), is_diff=True, budget=budget).document
    assert doc is not None
    parsed = parse_content_units(doc, SourceObjectKind.UNIFIED_DIFF, budget=budget)
    assert len(parsed.units) == 1
    assert parsed.is_partial is True
    assert len(parsed.skip_records) == 1
    skip = parsed.skip_records[0]
    assert skip.reason_code == SkipReasonCode.DIFF_BOMB_LIMIT
    assert skip.detail_code == "MAX_HUNKS_PER_FILE_EXCEEDED"
    assert skip.processed_range.raw_bytes_processed > skip.processed_range.chars_processed
    assert skip.processed_range.skipped_from_byte is not None
    assert skip.processed_range.skipped_to_byte == doc.metadata.utf8_byte_length

