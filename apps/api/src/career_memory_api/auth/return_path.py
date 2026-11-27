"""Strict relative return_path validation to block open-redirect and header injection."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit


class InvalidReturnPathError(ValueError):
    """Raised when a caller-supplied return_path fails strict relative allowlist checks."""


def validate_return_path(
    return_path: str,
    *,
    allowed_prefixes: tuple[str, ...] = ("/",),
) -> str:
    """Validate that ``return_path`` is a safe same-origin relative path.

    Rejects absolute URLs, protocol-relative URLs (``//evil.example``),
    backslashes, control characters, encoded slash/backslash tricks,
    authority markers (``@``), and embedded URL schemes in query strings.
    """
    if not isinstance(return_path, str) or not return_path:
        raise InvalidReturnPathError("return_path must be a non-empty relative path.")

    if return_path != return_path.strip():
        raise InvalidReturnPathError("return_path must not contain surrounding whitespace.")

    if any(ord(ch) < 32 or ord(ch) == 127 for ch in return_path):
        raise InvalidReturnPathError("return_path contains forbidden control characters.")

    if "\\" in return_path:
        raise InvalidReturnPathError("return_path must not contain backslashes.")

    if not return_path.startswith("/") or return_path.startswith("//"):
        raise InvalidReturnPathError("return_path must start with a single '/'.")

    lowered = return_path.lower()
    if "://" in lowered or "javascript:" in lowered or "data:" in lowered:
        raise InvalidReturnPathError("return_path must not contain URL schemes.")

    if "%2f" in lowered or "%5c" in lowered:
        raise InvalidReturnPathError(
            "return_path must not contain encoded path separators."
        )

    parsed = urlsplit(return_path)
    if parsed.scheme or parsed.netloc:
        raise InvalidReturnPathError("return_path must not specify a scheme or host.")

    decoded_path = unquote(parsed.path)
    if (
        not decoded_path.startswith("/")
        or decoded_path.startswith("//")
        or "\\" in decoded_path
        or "@" in decoded_path
    ):
        raise InvalidReturnPathError("return_path path segment is not allowed.")

    if not any(parsed.path.startswith(prefix) for prefix in allowed_prefixes):
        raise InvalidReturnPathError("return_path is outside allowed prefixes.")

    return return_path
