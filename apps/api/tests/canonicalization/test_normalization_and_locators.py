"""DATA-04 & CLAIM-QA compatibility tests for deterministic normalization and span locators."""

from __future__ import annotations

import unicodedata

import pytest

from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    LOCATOR_SPEC_VERSION,
    SpanLocator,
    build_span_locator,
    canonicalize_source_bytes,
    extract_verified_span,
)


CLAIM_QA_GOLDEN_VECTORS = [
    {
        "scenario_id": "SCN-01",
        "text": (
            "2026-04-02 주간 스탠드업 메모\n"
            "내일 오전 10시에 커피 마시면서 아젠다를 정리합시다. 아직 확정된 작업 항목은 없습니다.\n"
            "Let's sync tomorrow after coffee; agenda TBD."
        ),
        "content_sha256": "sha256:6a0fd6360ff98e03aa0e9576b1ed81e89659e7fe6205397b7f4783b4985a89f6",
        "char_length": 118,
        "utf8_byte_length": 204,
        "spans": [
            {
                "char_start": 22,
                "char_end": 118,
                "char_length": 96,
                "utf8_byte_start": 38,
                "utf8_byte_end": 204,
                "utf8_byte_length": 166,
                "utf16_start": 22,
                "utf16_end": 118,
                "line_start": 2,
                "line_end": 3,
                "span_sha256": "sha256:4df680712cd2d63b44c74d40760a440ddf4f2530d5fd3fa1ab592258bb144194",
            },
        ],
    },
    {
        "scenario_id": "SCN-02",
        "text": (
            "PR #418 — Idempotent Retry Queue for Billing Webhooks\n"
            "Author: Alex Kim (@alexkim-synth)\n\n"
            "I implemented an idempotent retry queue with exponential backoff for billing "
            "webhooks in Project Aurora, preventing duplicate invoice notifications during "
            "transient gateway timeouts."
        ),
        "content_sha256": "sha256:2cb2537936283cbb5f7527ecbf64b9d65fc849f58004b4828104e7a53cc01c9f",
        "char_length": 271,
        "utf8_byte_length": 273,
        "spans": [
            {
                "char_start": 89,
                "char_end": 271,
                "char_length": 182,
                "utf8_byte_start": 91,
                "utf8_byte_end": 273,
                "utf8_byte_length": 182,
                "utf16_start": 89,
                "utf16_end": 271,
                "line_start": 4,
                "line_end": 4,
                "span_sha256": "sha256:e3bf184f2ae9efcd6898f9da1ab4cdf1650bad6517860c7600d5859433adebbb",
            },
        ],
    },
    {
        "scenario_id": "SCN-03",
        "text": (
            "프로젝트 오로라 2026년 5월 장애 회고 및 개선 기록\n"
            "작성자: 김알렉스 (Verified Owner)\n\n"
            "1. 검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축했다.\n"
            "2. 배포 실패 시 2분 이내로 이전 스냅샷으로 복구하는 자동 롤백 스크립트를 추가했다.\n"
            "3. 슬로우 쿼리 임계값 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다."
        ),
        "content_sha256": "sha256:6589ff1acf43749f1dfe825d739b3c4bd6b2eaf6503b1d2ae6b6bb9795f22cd3",
        "char_length": 231,
        "utf8_byte_length": 485,
        "spans": [
            {
                "char_start": 60,
                "char_end": 131,
                "char_length": 71,
                "utf8_byte_start": 110,
                "utf8_byte_end": 261,
                "utf8_byte_length": 151,
                "utf16_start": 60,
                "utf16_end": 131,
                "line_start": 4,
                "line_end": 4,
                "span_sha256": "sha256:83f3e8d7e834d2c881729be98627547e74762e0a822e3d324e8fe7a05e6a0fe6",
            },
            {
                "char_start": 132,
                "char_end": 181,
                "char_length": 49,
                "utf8_byte_start": 262,
                "utf8_byte_end": 377,
                "utf8_byte_length": 115,
                "utf16_start": 132,
                "utf16_end": 181,
                "line_start": 5,
                "line_end": 5,
                "span_sha256": "sha256:f6416b27ba0c0fcc0b2c2bdb99c64c53d7df5595efc64c535acc82239ac136df",
            },
            {
                "char_start": 182,
                "char_end": 231,
                "char_length": 49,
                "utf8_byte_start": 378,
                "utf8_byte_end": 485,
                "utf8_byte_length": 107,
                "utf16_start": 182,
                "utf16_end": 231,
                "line_start": 6,
                "line_end": 6,
                "span_sha256": "sha256:035ff3be0efbcec1c8fd4407bda81d59e546e106df4b77ba8751821379c3e394",
            },
        ],
    },
]


def test_crlf_cr_lf_and_nfd_nfc_produce_identical_canonical_hash_and_offsets() -> None:
    """DATA-04: OS line-ending differences and macOS NFD vs Linux NFC must collapse identically."""
    base_nfc_lf = "한글 커밋 메시지 🚀\n  indented line: cafe\u00e9\n세 번째 줄"
    variant_crlf_nfd = unicodedata.normalize("NFD", base_nfc_lf).replace("\n", "\r\n")
    variant_cr_nfd = unicodedata.normalize("NFD", base_nfc_lf).replace("\n", "\r")
    variant_bom_crlf_nfd = "\ufeff" + variant_crlf_nfd

    res_nfc = canonicalize_source_bytes(base_nfc_lf.encode("utf-8"))
    res_crlf = canonicalize_source_bytes(variant_crlf_nfd.encode("utf-8"))
    res_cr = canonicalize_source_bytes(variant_cr_nfd.encode("utf-8"))
    res_bom = canonicalize_source_bytes(variant_bom_crlf_nfd.encode("utf-8"))

    for res in (res_nfc, res_crlf, res_cr, res_bom):
        assert res.status == "CANONICALIZED"
        assert res.document is not None
        assert res.skip_record is None
        assert res.document.metadata.canonicalization_version == CANONICALIZATION_VERSION

    assert res_nfc.document is not None
    assert res_crlf.document is not None
    assert res_cr.document is not None
    assert res_bom.document is not None

    expected_sha = res_nfc.document.metadata.content_sha256
    assert res_crlf.document.metadata.content_sha256 == expected_sha
    assert res_cr.document.metadata.content_sha256 == expected_sha
    assert res_bom.document.metadata.content_sha256 == expected_sha

    assert res_bom.document.metadata.bom_stripped is True
    assert res_bom.document.metadata.raw_bom_bytes == 3
    assert res_nfc.document.metadata.bom_stripped is False
    assert res_nfc.document.metadata.raw_bom_bytes == 0

    # Raw byte provenance differs (BOM + CRLF + NFD), while canonical hash/length match identically
    assert res_bom.document.metadata.raw_sha256 != res_nfc.document.metadata.raw_sha256
    assert res_bom.document.metadata.char_length == res_nfc.document.metadata.char_length
    assert res_bom.document.metadata.utf8_byte_length == res_nfc.document.metadata.utf8_byte_length
    assert res_bom.document.metadata.utf16_length == res_nfc.document.metadata.utf16_length

    # Exact sub-span extraction around multilingual + emoji + indented whitespace
    target = "  indented line: cafe\u00e9"
    start = res_nfc.document.text.index(target)
    end = start + len(target)
    loc_nfc = build_span_locator(res_nfc.document.text, start, end)
    loc_crlf = build_span_locator(res_crlf.document.text, start, end)
    loc_bom = build_span_locator(res_bom.document.text, start, end)

    assert loc_nfc == loc_crlf == loc_bom
    assert loc_nfc.locator_spec_version == LOCATOR_SPEC_VERSION
    assert loc_nfc.canonicalization_version == CANONICALIZATION_VERSION

    # Emoji (🚀, U+1F680) takes 1 Python code point, 4 UTF-8 bytes, 2 UTF-16 code units
    assert loc_nfc.utf16_start == loc_nfc.char_start + 1
    assert loc_nfc.utf8_byte_start > loc_nfc.char_start

    for doc in (res_nfc.document, res_crlf.document, res_cr.document, res_bom.document):
        verified = extract_verified_span(
            doc,
            loc_nfc,
            expected_source_content_sha256=expected_sha,
        )
        assert verified.verified is True
        assert verified.span is not None
        assert verified.span.text == target


def test_whitespace_and_indentation_are_never_trimmed_or_mutated() -> None:
    """DATA-04: Leading/trailing spaces, tabs, and trailing newlines must be preserved verbatim."""
    raw = "  \tleading spaces and tabs  \n\t  code_indent()\n   "
    res = canonicalize_source_bytes(raw.encode("utf-8"))
    assert res.status == "CANONICALIZED"
    assert res.document is not None
    assert res.document.text == raw
    assert res.document.metadata.char_length == len(raw)


def test_leading_bom_is_stripped_at_most_once() -> None:
    """Only a single leading UTF-8 BOM is removed; interior/second BOM characters remain intact."""
    raw_bytes = ("\ufeff\ufeffhello").encode("utf-8")
    res = canonicalize_source_bytes(raw_bytes)
    assert res.status == "CANONICALIZED"
    assert res.document is not None
    assert res.document.metadata.bom_stripped is True
    assert res.document.metadata.raw_bom_bytes == 3
    assert res.document.text == "\ufeffhello"


@pytest.mark.parametrize("vector", CLAIM_QA_GOLDEN_VECTORS, ids=lambda v: v["scenario_id"])
def test_claim_qa_0_4_0_golden_vectors_roundtrip_compat(vector: dict) -> None:
    """Production cm-canon-nfc-lf-v1 matches CLAIM-QA 0.4.0-synthetic-golden hashes and offsets 100%."""
    # Verify both direct LF/NFC and hostile NFD+CRLF wire variants produce exact CLAIM-QA hashes/offsets
    for variant_text in (
        vector["text"],
        unicodedata.normalize("NFD", vector["text"]).replace("\n", "\r\n"),
    ):
        res = canonicalize_source_bytes(variant_text.encode("utf-8"))
        assert res.status == "CANONICALIZED"
        assert res.document is not None
        meta = res.document.metadata
        assert meta.content_sha256 == vector["content_sha256"]
        assert meta.char_length == vector["char_length"]
        assert meta.utf8_byte_length == vector["utf8_byte_length"]

        for expected_span in vector["spans"]:
            computed = build_span_locator(
                res.document.text,
                expected_span["char_start"],
                expected_span["char_end"],
            )
            assert computed.char_start == expected_span["char_start"]
            assert computed.char_end == expected_span["char_end"]
            assert computed.char_length == expected_span["char_length"]
            assert computed.utf8_byte_start == expected_span["utf8_byte_start"]
            assert computed.utf8_byte_end == expected_span["utf8_byte_end"]
            assert computed.utf8_byte_length == expected_span["utf8_byte_length"]
            assert computed.utf16_start == expected_span["utf16_start"]
            assert computed.utf16_end == expected_span["utf16_end"]
            assert computed.line_start == expected_span["line_start"]
            assert computed.line_end == expected_span["line_end"]
            assert computed.span_sha256 == expected_span["span_sha256"]

            verified = extract_verified_span(
                res.document,
                computed,
                expected_source_content_sha256=vector["content_sha256"],
            )
            assert verified.verified is True
            assert verified.span is not None
            assert verified.span.text == res.document.text[
                expected_span["char_start"] : expected_span["char_end"]
            ]


def test_extract_verified_span_fails_closed_on_hash_offset_or_version_mismatch() -> None:
    """Resolver verification must reject drifted source hash, span hash, byte offset, or version."""
    doc_res = canonicalize_source_bytes("알파 베타 🚀 감마\n두 번째 줄".encode("utf-8"))
    assert doc_res.document is not None
    doc = doc_res.document
    loc = build_span_locator(doc.text, 3, 10)

    # 1. Wrong source hash
    bad_source = extract_verified_span(
        doc,
        loc,
        expected_source_content_sha256="sha256:" + "0" * 64,
    )
    assert bad_source.verified is False
    assert bad_source.span is None
    assert "SOURCE_CONTENT_HASH_MISMATCH" in bad_source.error_codes

    # 2. Wrong span hash
    tampered_hash = SpanLocator(
        locator_spec_version=loc.locator_spec_version,
        canonicalization_version=loc.canonicalization_version,
        char_start=loc.char_start,
        char_end=loc.char_end,
        char_length=loc.char_length,
        utf8_byte_start=loc.utf8_byte_start,
        utf8_byte_end=loc.utf8_byte_end,
        utf8_byte_length=loc.utf8_byte_length,
        utf16_start=loc.utf16_start,
        utf16_end=loc.utf16_end,
        line_start=loc.line_start,
        line_end=loc.line_end,
        span_sha256="sha256:" + "f" * 64,
    )
    bad_span = extract_verified_span(doc, tampered_hash)
    assert bad_span.verified is False
    assert bad_span.span is None
    assert "SPAN_HASH_MISMATCH" in bad_span.error_codes

    # 3. Corrupted UTF-8 byte offset (mid-codepoint slice)
    bad_utf8 = SpanLocator(
        locator_spec_version=loc.locator_spec_version,
        canonicalization_version=loc.canonicalization_version,
        char_start=loc.char_start,
        char_end=loc.char_end,
        char_length=loc.char_length,
        utf8_byte_start=loc.utf8_byte_start + 1,
        utf8_byte_end=loc.utf8_byte_end,
        utf8_byte_length=loc.utf8_byte_length - 1,
        utf16_start=loc.utf16_start,
        utf16_end=loc.utf16_end,
        line_start=loc.line_start,
        line_end=loc.line_end,
        span_sha256=loc.span_sha256,
    )
    bad_byte_res = extract_verified_span(doc, bad_utf8)
    assert bad_byte_res.verified is False
    assert bad_byte_res.span is None
    assert "UTF8_BYTE_OFFSET_MISMATCH" in bad_byte_res.error_codes

    # 4. Unknown canonicalization version
    bad_ver = SpanLocator(
        locator_spec_version=loc.locator_spec_version,
        canonicalization_version="cm-canon-legacy-v0",
        char_start=loc.char_start,
        char_end=loc.char_end,
        char_length=loc.char_length,
        utf8_byte_start=loc.utf8_byte_start,
        utf8_byte_end=loc.utf8_byte_end,
        utf8_byte_length=loc.utf8_byte_length,
        utf16_start=loc.utf16_start,
        utf16_end=loc.utf16_end,
        line_start=loc.line_start,
        line_end=loc.line_end,
        span_sha256=loc.span_sha256,
    )
    bad_ver_res = extract_verified_span(doc, bad_ver)
    assert bad_ver_res.verified is False
    assert bad_ver_res.span is None
    assert "CANONICALIZATION_VERSION_MISMATCH" in bad_ver_res.error_codes
