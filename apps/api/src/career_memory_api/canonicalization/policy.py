"""Budget limits, skip reason taxonomy, and hostile/large input classifiers (T-103A)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import fnmatch
from pathlib import PurePosixPath
from typing import Any


class SkipReasonCode(StrEnum):
    """Explicit deterministic skip reason codes (zero silent drops)."""

    BINARY_CONTENT = "BINARY_CONTENT"
    INVALID_ENCODING = "INVALID_ENCODING"
    OVERSIZED_INPUT = "OVERSIZED_INPUT"
    DIFF_BOMB_LIMIT = "DIFF_BOMB_LIMIT"
    LINE_LENGTH_EXCEEDED = "LINE_LENGTH_EXCEEDED"
    GENERATED_ARTIFACT = "GENERATED_ARTIFACT"
    MINIFIED_ASSET = "MINIFIED_ASSET"
    UNSUPPORTED_MIME = "UNSUPPORTED_MIME"


DEFAULT_GENERATED_FILENAME_PATTERNS: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "poetry.lock",
    "uv.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
    "flake.lock",
    "pipfile.lock",
    "*.pb.go",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.ts",
    "*.generated.*",
    "*.g.dart",
    "*.snap",
    "*.js.map",
    "*.css.map",
)

DEFAULT_GENERATED_DIR_SEGMENTS: tuple[str, ...] = (
    "dist",
    "build",
    "vendor",
    "node_modules",
    ".next",
    "coverage",
    "__snapshots__",
)

DEFAULT_MINIFIED_FILENAME_PATTERNS: tuple[str, ...] = (
    "*.min.js",
    "*.min.mjs",
    "*.min.cjs",
    "*.min.css",
    "*.bundle.min.js",
)

ALLOWED_EXACT_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/ecmascript",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/x-sh",
        "application/sql",
        "application/graphql",
        "application/x-patch",
        "application/x-diff",
    }
)

BINARY_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "BINARY_MAGIC_PNG"),
    (b"\xff\xd8\xff", "BINARY_MAGIC_JPEG"),
    (b"GIF87a", "BINARY_MAGIC_GIF"),
    (b"GIF89a", "BINARY_MAGIC_GIF"),
    (b"%PDF-", "BINARY_MAGIC_PDF"),
    (b"PK\x03\x04", "BINARY_MAGIC_ZIP"),
    (b"PK\x05\x06", "BINARY_MAGIC_ZIP"),
    (b"\x1f\x8b", "BINARY_MAGIC_GZIP"),
    (b"\x7fELF", "BINARY_MAGIC_ELF"),
    (b"\x00asm", "BINARY_MAGIC_WASM"),
    (b"SQLite format 3\x00", "BINARY_MAGIC_SQLITE"),
    (b"\xca\xfe\xba\xbe", "BINARY_MAGIC_MACHO"),
    (b"\xcf\xfa\xed\xfe", "BINARY_MAGIC_MACHO"),
)

_CODE_PUNCTUATION = frozenset(";{}()=[],")


@dataclass(frozen=True, slots=True)
class ProcessedRange:
    """Explicit coverage metadata describing processed vs skipped input ranges."""

    raw_bytes_processed: int
    raw_bytes_total: int
    chars_processed: int = 0
    chars_total: int = 0
    lines_processed: int = 0
    lines_total: int | None = None
    files_processed: int = 0
    files_total: int | None = None
    hunks_processed: int = 0
    hunks_total: int | None = None
    skipped_from_char: int | None = None
    skipped_to_char: int | None = None
    skipped_from_byte: int | None = None
    skipped_to_byte: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_bytes_processed": self.raw_bytes_processed,
            "raw_bytes_total": self.raw_bytes_total,
            "chars_processed": self.chars_processed,
            "chars_total": self.chars_total,
            "lines_processed": self.lines_processed,
            "lines_total": self.lines_total,
            "files_processed": self.files_processed,
            "files_total": self.files_total,
            "hunks_processed": self.hunks_processed,
            "hunks_total": self.hunks_total,
            "skipped_from_char": self.skipped_from_char,
            "skipped_to_char": self.skipped_to_char,
            "skipped_from_byte": self.skipped_from_byte,
            "skipped_to_byte": self.skipped_to_byte,
        }


@dataclass(frozen=True, slots=True)
class DeterministicSkipRecord:
    """Non-raw-text audit record emitted whenever input is skipped or partially bounded."""

    reason_code: SkipReasonCode
    processed_range: ProcessedRange
    observed_bytes: int
    budget_limit: int | None
    is_partial: bool
    file_path: str | None = None
    hunk_header: str | None = None
    detail_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code.value,
            "processed_range": self.processed_range.to_dict(),
            "observed_bytes": self.observed_bytes,
            "budget_limit": self.budget_limit,
            "is_partial": self.is_partial,
            "file_path": self.file_path,
            "hunk_header": self.hunk_header,
            "detail_code": self.detail_code,
        }


@dataclass(frozen=True, slots=True)
class CanonicalizationBudgetConfig:
    """Configurable deterministic resource and content guardrails."""

    max_raw_bytes: int = 1_048_576
    max_diff_bytes: int = 2_097_152
    max_files_per_diff: int = 100
    max_hunks_per_file: int = 50
    max_total_hunks: int = 500
    max_lines_per_hunk: int = 400
    max_line_bytes: int = 16_384
    binary_sniff_bytes: int = 8_192
    max_control_byte_ratio: float = 0.30
    minified_min_bytes: int = 2_048
    minified_avg_line_bytes: int = 500
    minified_max_whitespace_ratio: float = 0.12
    skip_generated_artifacts: bool = True
    skip_minified_assets: bool = True
    generated_filename_patterns: tuple[str, ...] = DEFAULT_GENERATED_FILENAME_PATTERNS
    generated_dir_segments: tuple[str, ...] = DEFAULT_GENERATED_DIR_SEGMENTS
    minified_filename_patterns: tuple[str, ...] = DEFAULT_MINIFIED_FILENAME_PATTERNS

    def __post_init__(self) -> None:
        for field_name in (
            "max_raw_bytes",
            "max_diff_bytes",
            "max_files_per_diff",
            "max_hunks_per_file",
            "max_total_hunks",
            "max_lines_per_hunk",
            "max_line_bytes",
            "binary_sniff_bytes",
            "minified_min_bytes",
            "minified_avg_line_bytes",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, int) or val <= 0:
                raise ValueError(f"{field_name} must be a positive integer.")
        if not (0.0 < self.max_control_byte_ratio <= 1.0):
            raise ValueError("max_control_byte_ratio must be in (0.0, 1.0].")
        if not (0.0 <= self.minified_max_whitespace_ratio <= 1.0):
            raise ValueError("minified_max_whitespace_ratio must be in [0.0, 1.0].")


def is_supported_mime_type(mime_type: str) -> bool:
    """Return True if the supplied MIME type represents supported text content."""
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if not normalized:
        return True
    if normalized.startswith("text/"):
        return True
    if normalized in ALLOWED_EXACT_MIME_TYPES:
        return True
    if normalized.endswith("+json") or normalized.endswith("+xml"):
        return True
    return False


def classify_generated_file(
    file_path: str | None,
    budget: CanonicalizationBudgetConfig,
) -> str | None:
    """Return a machine-readable detail code if file_path is a generated artifact."""
    if not file_path or not budget.skip_generated_artifacts:
        return None
    normalized_path = file_path.replace("\\", "/").strip("/")
    if not normalized_path:
        return None
    posix = PurePosixPath(normalized_path)
    lower_name = posix.name.lower()

    for pattern in budget.generated_filename_patterns:
        if fnmatch.fnmatch(lower_name, pattern.lower()):
            return "GENERATED_FILENAME_PATTERN"

    dir_parts = {part.lower() for part in posix.parts[:-1]}
    generated_dirs = {seg.lower() for seg in budget.generated_dir_segments}
    if dir_parts & generated_dirs:
        return "GENERATED_DIRECTORY_SEGMENT"

    return None


def classify_minified_file_path(
    file_path: str | None,
    budget: CanonicalizationBudgetConfig,
) -> str | None:
    """Return a machine-readable detail code if file_path matches minified asset patterns."""
    if not file_path or not budget.skip_minified_assets:
        return None
    normalized_path = file_path.replace("\\", "/").strip("/")
    if not normalized_path:
        return None
    lower_name = PurePosixPath(normalized_path).name.lower()
    for pattern in budget.minified_filename_patterns:
        if fnmatch.fnmatch(lower_name, pattern.lower()):
            return "MINIFIED_FILENAME_PATTERN"
    return None


def detect_binary_magic(raw_bytes: bytes) -> str | None:
    """Detect unambiguous binary format signatures in the prefix of raw_bytes."""
    for signature, detail_code in BINARY_MAGIC_SIGNATURES:
        if raw_bytes.startswith(signature):
            return detail_code
    return None


def compute_control_char_ratio(text: str) -> float:
    """Compute ratio of non-whitespace ASCII control characters in decoded text."""
    if not text:
        return 0.0
    control_count = 0
    for ch in text:
        code = ord(ch)
        if (code < 0x20 and ch not in ("\t", "\n", "\r", "\x0c")) or code == 0x7F:
            control_count += 1
    return control_count / len(text)


def is_minified_text(
    normalized_text: str,
    budget: CanonicalizationBudgetConfig,
) -> bool:
    """Deterministically detect minified JS/CSS/bundle content without regex backtracking."""
    if not budget.skip_minified_assets or not normalized_text:
        return False
    utf8_len = len(normalized_text.encode("utf-8"))
    if utf8_len < budget.minified_min_bytes:
        return False

    line_count = normalized_text.count("\n") + 1
    avg_line_bytes = utf8_len / line_count
    if avg_line_bytes < budget.minified_avg_line_bytes:
        return False

    whitespace_count = sum(1 for ch in normalized_text if ch in (" ", "\t", "\n"))
    whitespace_ratio = whitespace_count / len(normalized_text)
    if whitespace_ratio > budget.minified_max_whitespace_ratio:
        return False

    punct_count = sum(1 for ch in normalized_text if ch in _CODE_PUNCTUATION)
    punct_ratio = punct_count / len(normalized_text)
    return punct_ratio >= 0.04
