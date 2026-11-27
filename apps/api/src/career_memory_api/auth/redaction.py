"""Secret and credential redaction utilities for logs, errors, and audit metadata (SEC-01)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED_VALUE = "[REDACTED]"

SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "set_cookie",
        "code",
        "state",
        "code_verifier",
        "pkce_code_verifier",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "bearer_token",
        "client_secret",
        "secret",
        "password",
    }
)

_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9\-._~+/]+=*")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[opusr]_[A-Za-z0-9_]{8,}\b")
_HEADER_LINE_RE = re.compile(
    r"(?i)\b(Authorization|Cookie|Set-Cookie)\s*:\s*([^;\r\n]+)"
)
_KV_SECRET_RE = re.compile(
    r"(?i)\b(code|state|code_verifier|access_token|refresh_token|id_token|client_secret|token|secret|password)\s*=\s*([^\s&;,\"'\)\}]+)"
)


def redact_text(value: str) -> str:
    """Mask bearer tokens, JWTs, GitHub tokens, cookies, and OAuth parameters."""
    if not value:
        return value
    masked = _HEADER_LINE_RE.sub(lambda m: f"{m.group(1)}: {REDACTED_VALUE}", value)
    masked = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED_VALUE}", masked)
    masked = _JWT_RE.sub(REDACTED_VALUE, masked)
    masked = _GITHUB_TOKEN_RE.sub(REDACTED_VALUE, masked)
    masked = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED_VALUE}", masked)
    return masked


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of HTTP headers with Authorization/Cookie/Set-Cookie redacted."""
    result: dict[str, str] = {}
    for key, val in headers.items():
        if key.strip().lower() in SENSITIVE_KEY_NAMES:
            result[key] = REDACTED_VALUE
        else:
            result[key] = redact_text(str(val))
    return result


def redact_url_query(url: str) -> str:
    """Return a URL with sensitive OAuth/token query parameters redacted."""
    parts = urlsplit(url)
    if not parts.query:
        return redact_text(url)
    redacted_pairs: list[tuple[str, str]] = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        if key.strip().lower() in SENSITIVE_KEY_NAMES:
            redacted_pairs.append((key, REDACTED_VALUE))
        else:
            redacted_pairs.append((key, redact_text(val)))
    rebuilt_query = urlencode(redacted_pairs)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, rebuilt_query, "")
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys and token patterns in a mapping."""
    sanitized: dict[str, Any] = {}
    for raw_key, raw_val in data.items():
        key = str(raw_key)
        if key.strip().lower() in SENSITIVE_KEY_NAMES:
            sanitized[key] = REDACTED_VALUE
        else:
            sanitized[key] = _redact_value(raw_val)
    return sanitized


class RedactingLogFilter(logging.Filter):
    """Logging filter that strips tokens, cookies, and OAuth params from records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001
            rendered = str(record.msg)
        record.msg = redact_text(rendered)
        record.args = None
        return True
