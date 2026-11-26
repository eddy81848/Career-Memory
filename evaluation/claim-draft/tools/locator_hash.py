"""Programmatic normalization, hashing, span/offset computation, and round-trip verification.

IMPORTANT:
All normalization and offset conventions in this module are strictly TEST_FIXTURE_ONLY
(`fixture-nfc-lf-v0-draft`). Production parser/locator contracts remain REVIEW_REQUIRED
until confirmed against Notion specs (Grounded Claim Pipeline / Option C / T-004A).
"""

from __future__ import annotations

import hashlib
import ipaddress
import unicodedata
from typing import Any
from urllib.parse import urlparse

NORMALIZATION_POLICY_ID = "fixture-nfc-lf-v0-draft"
LOCATOR_SPEC_VERSION = "fixture-locator-v0-draft"
CONVENTION_SCOPE = "TEST_FIXTURE_ONLY"


def normalize_text(raw_text: str) -> str:
    """Normalize line endings (CRLF/CR -> LF) and Unicode form (NFC).

    Whitespaces are preserved verbatim so character/byte offsets remain transparent.
    """
    unified_newlines = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", unified_newlines)


def sha256_prefixed(text: str) -> str:
    """Return 'sha256:<hex>' digest of UTF-8 encoded text."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def short_hash(prefixed_hash: str, length: int = 8) -> str:
    """Extract the hex prefix from a 'sha256:<hex>' string."""
    raw = prefixed_hash.split(":", 1)[-1]
    return raw[:length]


def compute_version_id(source_key: str, revision_seq: int, normalized_text: str) -> str:
    """Deterministically compute a synthetic Source Revision version identifier."""
    digest = short_hash(sha256_prefixed(normalized_text), 8)
    return f"rev-{source_key}-v{revision_seq}-{digest}"


def compute_locator_id(owner_id: str, char_start: int, char_end: int, span_sha256: str) -> str:
    """Deterministically compute a locator identifier from owner, offsets, and span hash."""
    return f"loc-{owner_id}-{char_start}_{char_end}-{short_hash(span_sha256, 8)}"


def _utf16_code_unit_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _slice_by_utf16(text: str, start: int, end: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[start * 2 : end * 2].decode("utf-16-le")


def _line_number_at(text: str, char_index: int) -> int:
    return text.count("\n", 0, char_index) + 1


def locate_span(
    normalized_text: str,
    exact_substring: str,
    *,
    occurrence: int | None = None,
) -> dict[str, Any]:
    """Programmatically locate an exact substring and compute all offsets and hashes.

    Raises ValueError if the substring is missing or appears multiple times without
    an explicit `occurrence` index.
    """
    if not exact_substring:
        raise ValueError("exact_substring must be non-empty")

    matches: list[int] = []
    search_from = 0
    while True:
        idx = normalized_text.find(exact_substring, search_from)
        if idx == -1:
            break
        matches.append(idx)
        search_from = idx + 1

    if not matches:
        raise ValueError(f"Substring not found in normalized text: {exact_substring!r}")

    if occurrence is None:
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous substring ({len(matches)} matches); specify occurrence explicitly: "
                f"{exact_substring!r}"
            )
        char_start = matches[0]
    else:
        if occurrence < 0 or occurrence >= len(matches):
            raise ValueError(
                f"occurrence={occurrence} out of range for {len(matches)} matches of {exact_substring!r}"
            )
        char_start = matches[occurrence]

    char_end = char_start + len(exact_substring)
    prefix = normalized_text[:char_start]
    span_text = normalized_text[char_start:char_end]
    if span_text != exact_substring:
        raise RuntimeError("Internal slice mismatch during locate_span")

    utf8_byte_start = len(prefix.encode("utf-8"))
    utf8_byte_length = len(span_text.encode("utf-8"))
    utf8_byte_end = utf8_byte_start + utf8_byte_length

    utf16_start = _utf16_code_unit_len(prefix)
    utf16_end = utf16_start + _utf16_code_unit_len(span_text)

    line_start = _line_number_at(normalized_text, char_start)
    line_end = _line_number_at(normalized_text, max(char_start, char_end - 1))

    return {
        "locator_spec_version": LOCATOR_SPEC_VERSION,
        "normalization_policy": NORMALIZATION_POLICY_ID,
        "convention_scope": CONVENTION_SCOPE,
        "char_start": char_start,
        "char_end": char_end,
        "char_length": len(span_text),
        "utf8_byte_start": utf8_byte_start,
        "utf8_byte_end": utf8_byte_end,
        "utf8_byte_length": utf8_byte_length,
        "utf16_start": utf16_start,
        "utf16_end": utf16_end,
        "line_start": line_start,
        "line_end": line_end,
        "span_sha256": sha256_prefixed(span_text),
    }


def verify_locator_roundtrip(
    normalized_text: str,
    locator: dict[str, Any],
    *,
    context_label: str = "locator",
) -> tuple[str | None, list[tuple[str, str]]]:
    """Re-slice `normalized_text` using char, UTF-8 byte, and UTF-16 offsets and verify hashes.

    Returns (extracted_text_or_none, list_of_(error_code, message)).
    """
    errors: list[tuple[str, str]] = []
    required_int_fields = (
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
    )
    for field in required_int_fields:
        if not isinstance(locator.get(field), int):
            errors.append(("SCHEMA_VALIDATION_ERROR", f"{context_label}: missing or non-int '{field}'"))
    if not isinstance(locator.get("span_sha256"), str):
        errors.append(("SCHEMA_VALIDATION_ERROR", f"{context_label}: missing or non-str 'span_sha256'"))
    if errors:
        return None, errors

    c_start = locator["char_start"]
    c_end = locator["char_end"]
    b_start = locator["utf8_byte_start"]
    b_end = locator["utf8_byte_end"]
    u16_start = locator["utf16_start"]
    u16_end = locator["utf16_end"]

    if c_start < 0 or c_end <= c_start or c_end > len(normalized_text):
        errors.append(
            (
                "OFFSET_OR_ROUNDTRIP_MISMATCH",
                f"{context_label}: char range [{c_start}, {c_end}) out of bounds for len={len(normalized_text)}",
            )
        )
        return None, errors

    char_slice = normalized_text[c_start:c_end]
    if len(char_slice) != locator["char_length"]:
        errors.append(
            (
                "OFFSET_OR_ROUNDTRIP_MISMATCH",
                f"{context_label}: char_length={locator['char_length']} != actual {len(char_slice)}",
            )
        )

    encoded_utf8 = normalized_text.encode("utf-8")
    if b_start < 0 or b_end <= b_start or b_end > len(encoded_utf8):
        errors.append(
            (
                "BYTE_OFFSET_MISMATCH",
                f"{context_label}: utf8 byte range [{b_start}, {b_end}) out of bounds for byte len={len(encoded_utf8)}",
            )
        )
    else:
        try:
            byte_slice = encoded_utf8[b_start:b_end].decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(
                (
                    "BYTE_OFFSET_MISMATCH",
                    f"{context_label}: utf8 byte slice [{b_start}, {b_end}) splits a UTF-8 codepoint ({exc})",
                )
            )
        else:
            if byte_slice != char_slice:
                errors.append(
                    (
                        "BYTE_OFFSET_MISMATCH",
                        f"{context_label}: utf8 byte slice != char slice",
                    )
                )
            if (b_end - b_start) != locator["utf8_byte_length"]:
                errors.append(
                    (
                        "BYTE_OFFSET_MISMATCH",
                        f"{context_label}: utf8_byte_length={locator['utf8_byte_length']} != {b_end - b_start}",
                    )
                )

    total_u16 = _utf16_code_unit_len(normalized_text)
    if u16_start < 0 or u16_end <= u16_start or u16_end > total_u16:
        errors.append(
            (
                "OFFSET_OR_ROUNDTRIP_MISMATCH",
                f"{context_label}: utf16 range [{u16_start}, {u16_end}) out of bounds for len={total_u16}",
            )
        )
    else:
        try:
            u16_slice = _slice_by_utf16(normalized_text, u16_start, u16_end)
        except UnicodeDecodeError as exc:
            errors.append(
                (
                    "OFFSET_OR_ROUNDTRIP_MISMATCH",
                    f"{context_label}: utf16 slice decode error ({exc})",
                )
            )
        else:
            if u16_slice != char_slice:
                errors.append(
                    (
                        "OFFSET_OR_ROUNDTRIP_MISMATCH",
                        f"{context_label}: utf16 slice != char slice",
                    )
                )

    expected_line_start = _line_number_at(normalized_text, c_start)
    expected_line_end = _line_number_at(normalized_text, max(c_start, c_end - 1))
    if locator["line_start"] != expected_line_start or locator["line_end"] != expected_line_end:
        errors.append(
            (
                "OFFSET_OR_ROUNDTRIP_MISMATCH",
                f"{context_label}: lines ({locator['line_start']}, {locator['line_end']}) != "
                f"({expected_line_start}, {expected_line_end})",
            )
        )

    actual_hash = sha256_prefixed(char_slice)
    if actual_hash != locator["span_sha256"]:
        errors.append(
            (
                "SPAN_HASH_MISMATCH",
                f"{context_label}: span_sha256={locator['span_sha256']} != actual {actual_hash}",
            )
        )

    return char_slice, errors


def check_url_safety(url: str) -> tuple[bool, str]:
    """Evaluate synthetic refetch URL safety (HTTPS-only, no SSRF/private/loopback/credentials).

    Returns (is_safe, reason_code).
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        return False, f"FORBIDDEN_SCHEME:{scheme or 'missing'}"

    if parsed.username or parsed.password:
        return False, "CREDENTIALS_IN_URL"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "MISSING_HOSTNAME"

    blocked_hosts = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if hostname in blocked_hosts or hostname.endswith(".local") or hostname.endswith(".internal"):
        return False, f"FORBIDDEN_HOST:{hostname}"

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    if ip is not None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"FORBIDDEN_IP:{hostname}"

    return True, "ALLOWED_HTTPS_URL"
