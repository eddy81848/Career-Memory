"""Source-aware deterministic Content Unit parsers and outdated/rename anchor resolution (DATA-02)."""

from __future__ import annotations

from collections.abc import Sequence
import re

from .constants import CANONICALIZATION_VERSION, PARSER_VERSION
from .locator import OffsetCursor
from .models import (
    AnchorResolutionState,
    CommentContext,
    CommitContext,
    ContentUnitParseResult,
    ContentUnitType,
    EphemeralCanonicalDocument,
    EphemeralResolvedUnit,
    OriginalSourceLocator,
    PersistedContentUnitMetadata,
    ReviewCommentContext,
    SourceObjectKind,
    SpanLocator,
    SymbolChangeContext,
)
from .normalizer import sha256_prefixed_text
from .policy import (
    CanonicalizationBudgetConfig,
    DeterministicSkipRecord,
    ProcessedRange,
    SkipReasonCode,
    classify_generated_file,
    classify_minified_file_path,
    is_minified_text,
)

_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+.*|[ \t]*)$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _make_unit_id(
    unit_type: ContentUnitType,
    unit_index: int,
    locator: SpanLocator,
) -> str:
    short_sha = locator.span_sha256.split(":", 1)[-1][:12]
    return f"cu-{unit_type.value.lower()}-{unit_index}-{locator.char_start}_{locator.char_end}-{short_sha}"


def _build_resolved_unit(
    *,
    document: EphemeralCanonicalDocument,
    unit_index: int,
    unit_type: ContentUnitType,
    locator: SpanLocator,
    original_locator: OriginalSourceLocator,
    anchor_resolution_state: AnchorResolutionState = AnchorResolutionState.RESOLVED,
    support_eligible: bool = True,
) -> EphemeralResolvedUnit:
    unit_id = _make_unit_id(unit_type, unit_index, locator)
    metadata = PersistedContentUnitMetadata(
        unit_id=unit_id,
        unit_index=unit_index,
        unit_type=unit_type,
        source_content_sha256=document.metadata.content_sha256,
        locator=locator,
        original_locator=original_locator,
        anchor_resolution_state=anchor_resolution_state,
        support_eligible=support_eligible,
        parser_version=PARSER_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
    )
    span_text = document.text[locator.char_start : locator.char_end]
    return EphemeralResolvedUnit(metadata=metadata, _text=span_text)


def _iter_line_spans(text: str) -> list[tuple[int, int, str]]:
    """Return list of (start_char, end_char, line_without_lf) covering text verbatim."""
    lines: list[tuple[int, int, str]] = []
    pos = 0
    length = len(text)
    while pos < length:
        nl = text.find("\n", pos)
        if nl == -1:
            lines.append((pos, length, text[pos:length]))
            break
        lines.append((pos, nl + 1, text[pos:nl]))
        pos = nl + 1
    return lines


def _parse_markdown_sections(
    document: EphemeralCanonicalDocument,
    unit_type: ContentUnitType,
) -> ContentUnitParseResult:
    text = document.text
    if not text:
        return ContentUnitParseResult(
            units=(),
            persisted_metadata=(),
            skip_records=(),
            is_partial=False,
            processed_range=ProcessedRange(
                raw_bytes_processed=document.metadata.raw_byte_length,
                raw_bytes_total=document.metadata.raw_byte_length,
                chars_processed=0,
                chars_total=0,
                lines_processed=0,
                lines_total=0,
            ),
        )

    line_spans = _iter_line_spans(text)
    # Identify heading boundaries while tracking CommonMark fenced code blocks
    boundaries: list[tuple[int, int | None, str | None]] = []
    in_fence_char: str | None = None
    in_fence_len = 0

    for line_start, _line_end, line_content in line_spans:
        if in_fence_char is not None:
            stripped = line_content.lstrip(" ")
            leading_spaces = len(line_content) - len(stripped)
            if (
                leading_spaces <= 3
                and stripped.startswith(in_fence_char * in_fence_len)
                and stripped.rstrip(" \t").strip(in_fence_char) == ""
            ):
                in_fence_char = None
                in_fence_len = 0
            continue

        fence_match = _FENCE_OPEN_RE.match(line_content)
        if fence_match is not None:
            fence_seq = fence_match.group(1)
            info_string = fence_match.group(2)
            # Backtick info strings cannot contain backticks in CommonMark
            if fence_seq[0] == "~" or "`" not in info_string:
                in_fence_char = fence_seq[0]
                in_fence_len = len(fence_seq)
                continue

        heading_match = _ATX_HEADING_RE.match(line_content)
        if heading_match is not None:
            heading_level = len(heading_match.group(1))
            boundaries.append((line_start, heading_level, sha256_prefixed_text(line_content)))

    segments: list[tuple[int, int, str, int | None, str | None]] = []
    if not boundaries:
        segments.append((0, len(text), "preamble", None, None))
    else:
        first_heading_start = boundaries[0][0]
        if first_heading_start > 0:
            segments.append((0, first_heading_start, "preamble", None, None))
        for heading_idx, (h_start, h_level, h_sha) in enumerate(boundaries):
            seg_end = (
                boundaries[heading_idx + 1][0]
                if heading_idx + 1 < len(boundaries)
                else len(text)
            )
            segments.append((h_start, seg_end, f"h{h_level}[{heading_idx}]", h_level, h_sha))

    cursor = OffsetCursor(text)
    units: list[EphemeralResolvedUnit] = []
    for idx, (seg_start, seg_end, section_id, heading_level, heading_sha) in enumerate(segments):
        locator = cursor.locate(seg_start, seg_end)
        orig = OriginalSourceLocator(
            section=section_id,
            heading_level=heading_level,
            heading_sha256=heading_sha,
        )
        units.append(
            _build_resolved_unit(
                document=document,
                unit_index=idx,
                unit_type=unit_type,
                locator=locator,
                original_locator=orig,
            )
        )

    return ContentUnitParseResult(
        units=tuple(units),
        persisted_metadata=tuple(u.metadata for u in units),
        skip_records=(),
        is_partial=False,
        processed_range=ProcessedRange(
            raw_bytes_processed=document.metadata.raw_byte_length,
            raw_bytes_total=document.metadata.raw_byte_length,
            chars_processed=len(text),
            chars_total=len(text),
            lines_processed=len(line_spans),
            lines_total=len(line_spans),
        ),
    )


def _line_range_overlaps_hunk(
    hunk: OriginalSourceLocator,
    *,
    diff_side: str | None,
    line_start: int | None,
    line_end: int | None,
) -> bool:
    if line_start is None:
        return False
    end = line_end if line_end is not None else line_start
    side = (diff_side or "RIGHT").upper()
    if side == "LEFT":
        if hunk.old_start is None or hunk.old_lines is None or hunk.old_lines <= 0:
            return False
        h_start = hunk.old_start
        h_end = hunk.old_start + hunk.old_lines - 1
    else:
        if hunk.new_start is None or hunk.new_lines is None or hunk.new_lines <= 0:
            return False
        h_start = hunk.new_start
        h_end = hunk.new_start + hunk.new_lines - 1
    return not (end < h_start or line_start > h_end)


def resolve_review_comment_anchor(
    context: ReviewCommentContext | None,
    current_diff_units: Sequence[PersistedContentUnitMetadata] | None = None,
) -> tuple[AnchorResolutionState, bool, OriginalSourceLocator]:
    """Deterministically resolve a review comment anchor against current Revision diff hunks.

    Fail-closed rules (DATA-02 / T-103A Step 3):
    - Explicit ``is_outdated=True`` ALWAYS resolves to ``OUTDATED_UNRESOLVED`` (``support_eligible=False``).
    - When ``current_diff_units`` is provided, a review comment must match a current ``DIFF_HUNK``
      by file path (or tracked rename ``previous_file_path -> file_path``), line overlap on
      ``diff_side``, AND optional ``anchored_hunk_sha256`` equality; otherwise it is downgraded
      to ``OUTDATED_UNRESOLVED`` (never silently attached to an unrelated hunk).
    """
    ctx = context or ReviewCommentContext()

    base_locator_kwargs = {
        "comment_id": ctx.comment_id,
        "thread_id": ctx.thread_id,
        "commit_sha": ctx.commit_sha,
        "original_commit_sha": ctx.original_commit_sha,
        "file_path": ctx.file_path,
        "previous_file_path": ctx.previous_file_path,
        "is_renamed": bool(
            ctx.previous_file_path and ctx.file_path and ctx.previous_file_path != ctx.file_path
        ),
        "diff_side": ctx.diff_side,
        "source_line_start": ctx.line_start,
        "source_line_end": ctx.line_end,
        "original_line_start": ctx.original_line_start,
        "original_line_end": ctx.original_line_end,
    }

    if ctx.is_outdated:
        return (
            AnchorResolutionState.OUTDATED_UNRESOLVED,
            False,
            OriginalSourceLocator(**base_locator_kwargs),
        )

    if current_diff_units is not None:
        diff_hunks = [u for u in current_diff_units if u.unit_type == ContentUnitType.DIFF_HUNK]

        # First pass: direct path match or tracked rename match
        for hunk in diff_hunks:
            h_orig = hunk.original_locator
            direct_match = ctx.file_path is not None and h_orig.file_path == ctx.file_path
            rename_match = (
                ctx.file_path is not None
                and h_orig.is_renamed
                and h_orig.previous_file_path == ctx.file_path
            ) or (
                ctx.previous_file_path is not None
                and h_orig.is_renamed
                and h_orig.previous_file_path == ctx.previous_file_path
                and h_orig.file_path == ctx.file_path
            )
            if not (direct_match or rename_match):
                continue

            if not _line_range_overlaps_hunk(
                h_orig,
                diff_side=ctx.diff_side,
                line_start=ctx.line_start,
                line_end=ctx.line_end,
            ):
                continue

            if (
                ctx.anchored_hunk_sha256 is not None
                and hunk.locator.span_sha256 != ctx.anchored_hunk_sha256
            ):
                continue

            if rename_match or h_orig.is_renamed:
                return (
                    AnchorResolutionState.RENAMED_TRACKED,
                    True,
                    OriginalSourceLocator(
                        **{
                            **base_locator_kwargs,
                            "file_path": h_orig.file_path,
                            "previous_file_path": h_orig.previous_file_path or ctx.file_path,
                            "is_renamed": True,
                            "hunk_header": h_orig.hunk_header,
                            "matched_hunk_unit_id": hunk.unit_id,
                        }
                    ),
                )

            return (
                AnchorResolutionState.RESOLVED,
                True,
                OriginalSourceLocator(
                    **{
                        **base_locator_kwargs,
                        "hunk_header": h_orig.hunk_header,
                        "matched_hunk_unit_id": hunk.unit_id,
                    }
                ),
            )

        # No current hunk matched -> fail closed to OUTDATED_UNRESOLVED
        return (
            AnchorResolutionState.OUTDATED_UNRESOLVED,
            False,
            OriginalSourceLocator(**base_locator_kwargs),
        )

    # No current_diff_units supplied: rely on explicit commit/line staleness signals
    if (
        ctx.original_commit_sha
        and ctx.commit_sha
        and ctx.original_commit_sha != ctx.commit_sha
        and ctx.line_start is None
    ):
        return (
            AnchorResolutionState.OUTDATED_UNRESOLVED,
            False,
            OriginalSourceLocator(**base_locator_kwargs),
        )

    if ctx.previous_file_path and ctx.file_path and ctx.previous_file_path != ctx.file_path:
        return (
            AnchorResolutionState.RENAMED_TRACKED,
            True,
            OriginalSourceLocator(**base_locator_kwargs),
        )

    return (
        AnchorResolutionState.RESOLVED,
        True,
        OriginalSourceLocator(**base_locator_kwargs),
    )


def _strip_diff_path_prefix(raw_path: str) -> str | None:
    cleaned = raw_path.strip()
    if cleaned == "/dev/null":
        return None
    if cleaned.startswith("a/") or cleaned.startswith("b/"):
        return cleaned[2:]
    return cleaned


def _parse_unified_diff(
    document: EphemeralCanonicalDocument,
    budget: CanonicalizationBudgetConfig,
) -> ContentUnitParseResult:
    text = document.text
    line_spans = _iter_line_spans(text)
    total_chars = len(text)
    total_lines = len(line_spans)
    total_raw_bytes = document.metadata.raw_byte_length

    if not line_spans:
        return ContentUnitParseResult(
            units=(),
            persisted_metadata=(),
            skip_records=(),
            is_partial=False,
            processed_range=ProcessedRange(
                raw_bytes_processed=total_raw_bytes,
                raw_bytes_total=total_raw_bytes,
                chars_processed=0,
                chars_total=0,
                lines_processed=0,
                lines_total=0,
                files_processed=0,
                files_total=0,
                hunks_processed=0,
                hunks_total=0,
            ),
        )

    # Partition diff into per-file blocks by `diff --git` (or standalone `--- ` header)
    file_starts: list[int] = []
    for idx, (_s, _e, content) in enumerate(line_spans):
        if content.startswith("diff --git "):
            file_starts.append(idx)
    if not file_starts:
        # Support single-file diffs without `diff --git` prefix
        file_starts = [0]

    file_blocks: list[tuple[int, int]] = []
    for pos_idx, start_line_idx in enumerate(file_starts):
        end_line_idx = (
            file_starts[pos_idx + 1] if pos_idx + 1 < len(file_starts) else total_lines
        )
        file_blocks.append((start_line_idx, end_line_idx))

    total_files = len(file_blocks)
    cursor = OffsetCursor(text)
    units: list[EphemeralResolvedUnit] = []
    skip_records: list[DeterministicSkipRecord] = []
    is_partial = False
    files_processed = 0
    total_hunks_seen = 0
    chars_processed = 0
    bytes_processed = 0
    lines_processed = 0

    def _utf8_offset(char_pos: int) -> int:
        return len(text[:char_pos].encode("utf-8"))

    for block_idx, (block_start_line, block_end_line) in enumerate(file_blocks):
        block_char_start = line_spans[block_start_line][0]
        block_char_end = line_spans[block_end_line - 1][1]

        if block_idx >= budget.max_files_per_diff:
            is_partial = True
            skip_from_byte = _utf8_offset(block_char_start)
            skip_records.append(
                DeterministicSkipRecord(
                    reason_code=SkipReasonCode.DIFF_BOMB_LIMIT,
                    processed_range=ProcessedRange(
                        raw_bytes_processed=bytes_processed,
                        raw_bytes_total=total_raw_bytes,
                        chars_processed=chars_processed,
                        chars_total=total_chars,
                        lines_processed=lines_processed,
                        lines_total=total_lines,
                        files_processed=files_processed,
                        files_total=total_files,
                        hunks_processed=len(units),
                        hunks_total=None,
                        skipped_from_char=block_char_start,
                        skipped_to_char=total_chars,
                        skipped_from_byte=skip_from_byte,
                        skipped_to_byte=document.metadata.utf8_byte_length,
                    ),
                    observed_bytes=total_raw_bytes,
                    budget_limit=budget.max_files_per_diff,
                    is_partial=True,
                    detail_code="MAX_FILES_PER_DIFF_EXCEEDED",
                )
            )
            break

        files_processed += 1
        old_path: str | None = None
        new_path: str | None = None
        rename_from: str | None = None
        rename_to: str | None = None
        is_binary_patch = False
        hunk_starts: list[tuple[int, re.Match[str]]] = []

        for l_idx in range(block_start_line, block_end_line):
            _ls, _le, line_text = line_spans[l_idx]
            dg_match = _DIFF_GIT_RE.match(line_text)
            if dg_match is not None:
                old_path = dg_match.group(1)
                new_path = dg_match.group(2)
                continue
            if line_text.startswith("rename from "):
                rename_from = line_text[len("rename from ") :].strip()
                continue
            if line_text.startswith("rename to "):
                rename_to = line_text[len("rename to ") :].strip()
                continue
            if line_text.startswith("--- "):
                parsed_old = _strip_diff_path_prefix(line_text[4:])
                if parsed_old is not None:
                    old_path = parsed_old
                continue
            if line_text.startswith("+++ "):
                parsed_new = _strip_diff_path_prefix(line_text[4:])
                if parsed_new is not None:
                    new_path = parsed_new
                continue
            if line_text.startswith("Binary files ") or line_text == "GIT binary patch":
                is_binary_patch = True
                continue
            h_match = _HUNK_HEADER_RE.match(line_text)
            if h_match is not None:
                hunk_starts.append((l_idx, h_match))

        resolved_new_path = rename_to or new_path or old_path
        resolved_prev_path: str | None = None
        if rename_from and rename_to and rename_from != rename_to:
            resolved_prev_path = rename_from
        elif old_path and new_path and old_path != new_path:
            resolved_prev_path = old_path

        block_bytes = len(text[block_char_start:block_char_end].encode("utf-8"))
        block_byte_start = _utf8_offset(block_char_start)
        block_byte_end = block_byte_start + block_bytes

        # Check binary patch inside diff
        if is_binary_patch:
            is_partial = True
            skip_records.append(
                DeterministicSkipRecord(
                    reason_code=SkipReasonCode.BINARY_CONTENT,
                    processed_range=ProcessedRange(
                        raw_bytes_processed=bytes_processed,
                        raw_bytes_total=total_raw_bytes,
                        chars_processed=chars_processed,
                        chars_total=total_chars,
                        lines_processed=lines_processed,
                        lines_total=total_lines,
                        files_processed=files_processed,
                        files_total=total_files,
                        hunks_processed=len(units),
                        skipped_from_char=block_char_start,
                        skipped_to_char=block_char_end,
                        skipped_from_byte=block_byte_start,
                        skipped_to_byte=block_byte_end,
                    ),
                    observed_bytes=block_bytes,
                    budget_limit=None,
                    is_partial=True,
                    file_path=resolved_new_path,
                    detail_code="BINARY_DIFF_PATCH",
                )
            )
            chars_processed = block_char_end
            bytes_processed = block_byte_end
            lines_processed = block_end_line
            continue

        # Check generated artifact inside diff
        gen_detail = classify_generated_file(resolved_new_path, budget)
        if gen_detail is not None:
            is_partial = True
            skip_records.append(
                DeterministicSkipRecord(
                    reason_code=SkipReasonCode.GENERATED_ARTIFACT,
                    processed_range=ProcessedRange(
                        raw_bytes_processed=bytes_processed,
                        raw_bytes_total=total_raw_bytes,
                        chars_processed=chars_processed,
                        chars_total=total_chars,
                        lines_processed=lines_processed,
                        lines_total=total_lines,
                        files_processed=files_processed,
                        files_total=total_files,
                        hunks_processed=len(units),
                        skipped_from_char=block_char_start,
                        skipped_to_char=block_char_end,
                        skipped_from_byte=block_byte_start,
                        skipped_to_byte=block_byte_end,
                    ),
                    observed_bytes=block_bytes,
                    budget_limit=None,
                    is_partial=True,
                    file_path=resolved_new_path,
                    detail_code=gen_detail,
                )
            )
            chars_processed = block_char_end
            bytes_processed = block_byte_end
            lines_processed = block_end_line
            continue

        # Check minified filename inside diff
        min_detail = classify_minified_file_path(resolved_new_path, budget)
        if min_detail is not None:
            is_partial = True
            skip_records.append(
                DeterministicSkipRecord(
                    reason_code=SkipReasonCode.MINIFIED_ASSET,
                    processed_range=ProcessedRange(
                        raw_bytes_processed=bytes_processed,
                        raw_bytes_total=total_raw_bytes,
                        chars_processed=chars_processed,
                        chars_total=total_chars,
                        lines_processed=lines_processed,
                        lines_total=total_lines,
                        files_processed=files_processed,
                        files_total=total_files,
                        hunks_processed=len(units),
                        skipped_from_char=block_char_start,
                        skipped_to_char=block_char_end,
                        skipped_from_byte=block_byte_start,
                        skipped_to_byte=block_byte_end,
                    ),
                    observed_bytes=block_bytes,
                    budget_limit=None,
                    is_partial=True,
                    file_path=resolved_new_path,
                    detail_code=min_detail,
                )
            )
            chars_processed = block_char_end
            bytes_processed = block_byte_end
            lines_processed = block_end_line
            continue

        for h_idx, (h_line_idx, h_match) in enumerate(hunk_starts):
            h_end_line_idx = (
                hunk_starts[h_idx + 1][0] if h_idx + 1 < len(hunk_starts) else block_end_line
            )
            h_char_start = line_spans[h_line_idx][0]
            h_char_end = line_spans[h_end_line_idx - 1][1]
            hunk_line_count = h_end_line_idx - h_line_idx

            old_start = int(h_match.group(1))
            old_lines = int(h_match.group(2)) if h_match.group(2) is not None else 1
            new_start = int(h_match.group(3))
            new_lines = int(h_match.group(4)) if h_match.group(4) is not None else 1
            # Strictly coordinate-only hunk header (strips trailing raw code context)
            canonical_hunk_header = f"@@ -{old_start},{old_lines} +{new_start},{new_lines} @@"

            hunk_text = text[h_char_start:h_char_end]
            hunk_bytes = len(hunk_text.encode("utf-8"))
            h_byte_start = _utf8_offset(h_char_start)
            h_byte_end = h_byte_start + hunk_bytes
            total_hunks_seen += 1

            # Per-file hunk cap
            if h_idx >= budget.max_hunks_per_file:
                is_partial = True
                skip_records.append(
                    DeterministicSkipRecord(
                        reason_code=SkipReasonCode.DIFF_BOMB_LIMIT,
                        processed_range=ProcessedRange(
                            raw_bytes_processed=bytes_processed,
                            raw_bytes_total=total_raw_bytes,
                            chars_processed=chars_processed,
                            chars_total=total_chars,
                            lines_processed=lines_processed,
                            lines_total=total_lines,
                            files_processed=files_processed,
                            files_total=total_files,
                            hunks_processed=len(units),
                            hunks_total=len(hunk_starts),
                            skipped_from_char=h_char_start,
                            skipped_to_char=block_char_end,
                            skipped_from_byte=h_byte_start,
                            skipped_to_byte=block_byte_end,
                        ),
                        observed_bytes=hunk_bytes,
                        budget_limit=budget.max_hunks_per_file,
                        is_partial=True,
                        file_path=resolved_new_path,
                        hunk_header=canonical_hunk_header,
                        detail_code="MAX_HUNKS_PER_FILE_EXCEEDED",
                    )
                )
                break

            # Total diff hunk cap
            if len(units) >= budget.max_total_hunks:
                is_partial = True
                skip_records.append(
                    DeterministicSkipRecord(
                        reason_code=SkipReasonCode.DIFF_BOMB_LIMIT,
                        processed_range=ProcessedRange(
                            raw_bytes_processed=bytes_processed,
                            raw_bytes_total=total_raw_bytes,
                            chars_processed=chars_processed,
                            chars_total=total_chars,
                            lines_processed=lines_processed,
                            lines_total=total_lines,
                            files_processed=files_processed,
                            files_total=total_files,
                            hunks_processed=len(units),
                            skipped_from_char=h_char_start,
                            skipped_to_char=total_chars,
                            skipped_from_byte=h_byte_start,
                            skipped_to_byte=document.metadata.utf8_byte_length,
                        ),
                        observed_bytes=total_raw_bytes,
                        budget_limit=budget.max_total_hunks,
                        is_partial=True,
                        file_path=resolved_new_path,
                        hunk_header=canonical_hunk_header,
                        detail_code="MAX_TOTAL_HUNKS_EXCEEDED",
                    )
                )
                return ContentUnitParseResult(
                    units=tuple(units),
                    persisted_metadata=tuple(u.metadata for u in units),
                    skip_records=tuple(skip_records),
                    is_partial=True,
                    processed_range=ProcessedRange(
                        raw_bytes_processed=bytes_processed,
                        raw_bytes_total=total_raw_bytes,
                        chars_processed=chars_processed,
                        chars_total=total_chars,
                        lines_processed=lines_processed,
                        lines_total=total_lines,
                        files_processed=files_processed,
                        files_total=total_files,
                        hunks_processed=len(units),
                        hunks_total=total_hunks_seen,
                    ),
                )

            # Per-hunk line cap
            if hunk_line_count > budget.max_lines_per_hunk:
                is_partial = True
                skip_records.append(
                    DeterministicSkipRecord(
                        reason_code=SkipReasonCode.DIFF_BOMB_LIMIT,
                        processed_range=ProcessedRange(
                            raw_bytes_processed=bytes_processed,
                            raw_bytes_total=total_raw_bytes,
                            chars_processed=chars_processed,
                            chars_total=total_chars,
                            lines_processed=lines_processed,
                            lines_total=total_lines,
                            files_processed=files_processed,
                            files_total=total_files,
                            hunks_processed=len(units),
                            skipped_from_char=h_char_start,
                            skipped_to_char=h_char_end,
                            skipped_from_byte=h_byte_start,
                            skipped_to_byte=h_byte_end,
                        ),
                        observed_bytes=hunk_bytes,
                        budget_limit=budget.max_lines_per_hunk,
                        is_partial=True,
                        file_path=resolved_new_path,
                        hunk_header=canonical_hunk_header,
                        detail_code="MAX_LINES_PER_HUNK_EXCEEDED",
                    )
                )
                chars_processed = h_char_end
                bytes_processed = h_byte_end
                lines_processed = h_end_line_idx
                continue

            # Minified or overlong line inside hunk
            if is_minified_text(hunk_text, budget):
                is_partial = True
                skip_records.append(
                    DeterministicSkipRecord(
                        reason_code=SkipReasonCode.MINIFIED_ASSET,
                        processed_range=ProcessedRange(
                            raw_bytes_processed=bytes_processed,
                            raw_bytes_total=total_raw_bytes,
                            chars_processed=chars_processed,
                            chars_total=total_chars,
                            lines_processed=lines_processed,
                            lines_total=total_lines,
                            files_processed=files_processed,
                            files_total=total_files,
                            hunks_processed=len(units),
                            skipped_from_char=h_char_start,
                            skipped_to_char=h_char_end,
                            skipped_from_byte=h_byte_start,
                            skipped_to_byte=h_byte_end,
                        ),
                        observed_bytes=hunk_bytes,
                        budget_limit=budget.minified_avg_line_bytes,
                        is_partial=True,
                        file_path=resolved_new_path,
                        hunk_header=canonical_hunk_header,
                        detail_code="MINIFIED_HUNK_HEURISTIC",
                    )
                )
                chars_processed = h_char_end
                bytes_processed = h_byte_end
                lines_processed = h_end_line_idx
                continue

            max_observed_line_bytes = max(
                (len(line_spans[k][2].encode("utf-8")) for k in range(h_line_idx, h_end_line_idx)),
                default=0,
            )
            if max_observed_line_bytes > budget.max_line_bytes:
                is_partial = True
                skip_records.append(
                    DeterministicSkipRecord(
                        reason_code=SkipReasonCode.LINE_LENGTH_EXCEEDED,
                        processed_range=ProcessedRange(
                            raw_bytes_processed=bytes_processed,
                            raw_bytes_total=total_raw_bytes,
                            chars_processed=chars_processed,
                            chars_total=total_chars,
                            lines_processed=lines_processed,
                            lines_total=total_lines,
                            files_processed=files_processed,
                            files_total=total_files,
                            hunks_processed=len(units),
                            skipped_from_char=h_char_start,
                            skipped_to_char=h_char_end,
                            skipped_from_byte=h_byte_start,
                            skipped_to_byte=h_byte_end,
                        ),
                        observed_bytes=max_observed_line_bytes,
                        budget_limit=budget.max_line_bytes,
                        is_partial=True,
                        file_path=resolved_new_path,
                        hunk_header=canonical_hunk_header,
                        detail_code="MAX_LINE_BYTES_EXCEEDED",
                    )
                )
                chars_processed = h_char_end
                bytes_processed = h_byte_end
                lines_processed = h_end_line_idx
                continue

            locator = cursor.locate(h_char_start, h_char_end)
            is_renamed = resolved_prev_path is not None
            orig = OriginalSourceLocator(
                file_path=resolved_new_path,
                previous_file_path=resolved_prev_path,
                is_renamed=is_renamed,
                hunk_header=canonical_hunk_header,
                old_start=old_start,
                old_lines=old_lines,
                new_start=new_start,
                new_lines=new_lines,
            )
            units.append(
                _build_resolved_unit(
                    document=document,
                    unit_index=len(units),
                    unit_type=ContentUnitType.DIFF_HUNK,
                    locator=locator,
                    original_locator=orig,
                    anchor_resolution_state=(
                        AnchorResolutionState.RENAMED_TRACKED
                        if is_renamed
                        else AnchorResolutionState.RESOLVED
                    ),
                    support_eligible=True,
                )
            )
            chars_processed = h_char_end
            bytes_processed = h_byte_end
            lines_processed = h_end_line_idx

        chars_processed = max(chars_processed, block_char_end)
        bytes_processed = max(bytes_processed, block_byte_end)
        lines_processed = max(lines_processed, block_end_line)

    return ContentUnitParseResult(
        units=tuple(units),
        persisted_metadata=tuple(u.metadata for u in units),
        skip_records=tuple(skip_records),
        is_partial=is_partial,
        processed_range=ProcessedRange(
            raw_bytes_processed=total_raw_bytes if not is_partial else bytes_processed,
            raw_bytes_total=total_raw_bytes,
            chars_processed=chars_processed,
            chars_total=total_chars,
            lines_processed=lines_processed,
            lines_total=total_lines,
            files_processed=files_processed,
            files_total=total_files,
            hunks_processed=len(units),
            hunks_total=total_hunks_seen,
        ),
    )


def parse_content_units(
    document: EphemeralCanonicalDocument,
    source_kind: SourceObjectKind,
    *,
    budget: CanonicalizationBudgetConfig | None = None,
    comment_context: CommentContext | None = None,
    commit_context: CommitContext | None = None,
    review_comment_context: ReviewCommentContext | None = None,
    symbol_change_context: SymbolChangeContext | None = None,
    current_diff_units: Sequence[PersistedContentUnitMetadata] | None = None,
) -> ContentUnitParseResult:
    """Deterministically segment a canonicalized document into source-aware Content Units."""
    cfg = budget or CanonicalizationBudgetConfig()

    if source_kind == SourceObjectKind.PR_BODY:
        return _parse_markdown_sections(document, ContentUnitType.PR_BODY_SECTION)
    if source_kind == SourceObjectKind.ISSUE_BODY:
        return _parse_markdown_sections(document, ContentUnitType.ISSUE_SECTION)
    if source_kind == SourceObjectKind.UNIFIED_DIFF:
        return _parse_unified_diff(document, cfg)

    text = document.text
    total_lines = document.metadata.line_count
    if not text:
        return ContentUnitParseResult(
            units=(),
            persisted_metadata=(),
            skip_records=(),
            is_partial=False,
            processed_range=ProcessedRange(
                raw_bytes_processed=document.metadata.raw_byte_length,
                raw_bytes_total=document.metadata.raw_byte_length,
                chars_processed=0,
                chars_total=0,
                lines_processed=0,
                lines_total=0,
            ),
        )

    cursor = OffsetCursor(text)
    locator = cursor.locate(0, len(text))

    if source_kind == SourceObjectKind.PR_TITLE:
        unit = _build_resolved_unit(
            document=document,
            unit_index=0,
            unit_type=ContentUnitType.PR_TITLE,
            locator=locator,
            original_locator=OriginalSourceLocator(),
        )
    elif source_kind == SourceObjectKind.COMMENT:
        c_ctx = comment_context or CommentContext()
        unit = _build_resolved_unit(
            document=document,
            unit_index=0,
            unit_type=ContentUnitType.COMMENT,
            locator=locator,
            original_locator=OriginalSourceLocator(
                comment_id=c_ctx.comment_id,
                thread_id=c_ctx.thread_id,
            ),
        )
    elif source_kind == SourceObjectKind.COMMIT_MESSAGE:
        cm_ctx = commit_context or CommitContext()
        unit = _build_resolved_unit(
            document=document,
            unit_index=0,
            unit_type=ContentUnitType.COMMIT_MESSAGE,
            locator=locator,
            original_locator=OriginalSourceLocator(commit_sha=cm_ctx.commit_sha),
        )
    elif source_kind == SourceObjectKind.REVIEW_COMMENT:
        anchor_state, support_eligible, orig = resolve_review_comment_anchor(
            review_comment_context,
            current_diff_units=current_diff_units,
        )
        unit = _build_resolved_unit(
            document=document,
            unit_index=0,
            unit_type=ContentUnitType.REVIEW_COMMENT,
            locator=locator,
            original_locator=orig,
            anchor_resolution_state=anchor_state,
            support_eligible=support_eligible,
        )
    elif source_kind == SourceObjectKind.SYMBOL_CHANGE:
        if symbol_change_context is None:
            raise ValueError("symbol_change_context is required for SourceObjectKind.SYMBOL_CHANGE.")
        is_renamed = bool(
            symbol_change_context.previous_file_path
            and symbol_change_context.previous_file_path != symbol_change_context.file_path
        )
        unit = _build_resolved_unit(
            document=document,
            unit_index=0,
            unit_type=ContentUnitType.SYMBOL_CHANGE,
            locator=locator,
            original_locator=OriginalSourceLocator(
                file_path=symbol_change_context.file_path,
                previous_file_path=symbol_change_context.previous_file_path,
                is_renamed=is_renamed,
                source_line_start=symbol_change_context.line_start,
                source_line_end=symbol_change_context.line_end,
                symbol_name=symbol_change_context.symbol_name,
                symbol_kind=symbol_change_context.symbol_kind,
            ),
            anchor_resolution_state=(
                AnchorResolutionState.RENAMED_TRACKED
                if is_renamed
                else AnchorResolutionState.RESOLVED
            ),
        )
    else:
        raise ValueError(f"Unsupported source_kind: {source_kind!r}")

    return ContentUnitParseResult(
        units=(unit,),
        persisted_metadata=(unit.metadata,),
        skip_records=(),
        is_partial=False,
        processed_range=ProcessedRange(
            raw_bytes_processed=document.metadata.raw_byte_length,
            raw_bytes_total=document.metadata.raw_byte_length,
            chars_processed=len(text),
            chars_total=len(text),
            lines_processed=total_lines,
            lines_total=total_lines,
        ),
    )
