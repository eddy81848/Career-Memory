"""DATA-02 & Option C tests for source-aware parsing, outdated/rename anchors, and zero raw persistence."""

from __future__ import annotations

import json

from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    FORBIDDEN_PERSISTED_RAW_TEXT_KEYS,
    PARSER_VERSION,
    AnchorResolutionState,
    CommentContext,
    CommitContext,
    ContentUnitType,
    ReviewCommentContext,
    SourceObjectKind,
    SymbolChangeContext,
    canonicalize_source_bytes,
    extract_verified_span,
    parse_content_units,
)


def test_pr_body_and_issue_sections_ignore_hash_comments_inside_fenced_code_blocks() -> None:
    """DATA-02: Markdown parser must split on ATX headings while ignoring '#' inside code fences."""
    canary = "CANARY_RAW_SECRET_PR_BODY_99182"
    pr_body = (
        f"Intro preamble mentioning {canary}.\n\n"
        "## Problem Statement\n"
        "Latency spiked on worker queue.\n\n"
        "```python\n"
        "# This Python comment is NOT a heading\n"
        "## Neither is this double-hash comment\n"
        "def retry():\n"
        "    return True\n"
        "```\n\n"
        "~~~bash\n"
        "# Shell comment inside tilde fence\n"
        "echo ok\n"
        "~~~\n\n"
        "### Verification Results\n"
        "Reduced p95 latency from 620ms to 180ms.\n"
    )
    doc_res = canonicalize_source_bytes(pr_body.encode("utf-8"))
    assert doc_res.document is not None

    parsed = parse_content_units(doc_res.document, SourceObjectKind.PR_BODY)
    assert parsed.is_partial is False
    assert parsed.skip_records == ()
    assert len(parsed.units) == 3

    # Exact lossless partition of canonical text
    assert "".join(u.text for u in parsed.units) == doc_res.document.text

    types = [u.metadata.unit_type for u in parsed.units]
    assert types == [
        ContentUnitType.PR_BODY_SECTION,
        ContentUnitType.PR_BODY_SECTION,
        ContentUnitType.PR_BODY_SECTION,
    ]
    assert parsed.units[0].metadata.original_locator.section == "preamble"
    assert parsed.units[1].metadata.original_locator.section == "h2[0]"
    assert parsed.units[1].metadata.original_locator.heading_level == 2
    assert "# This Python comment is NOT a heading" in parsed.units[1].text
    assert "# Shell comment inside tilde fence" in parsed.units[1].text
    assert parsed.units[2].metadata.original_locator.section == "h3[1]"
    assert parsed.units[2].metadata.original_locator.heading_level == 3

    # Roundtrip verification + Option C zero-raw-text guarantee
    for unit in parsed.units:
        verified = extract_verified_span(
            doc_res.document,
            unit.metadata.locator,
            expected_source_content_sha256=doc_res.document.metadata.content_sha256,
        )
        assert verified.verified is True
        assert verified.span is not None
        assert verified.span.text == unit.text

        persisted = unit.metadata.to_persisted_anchor_dict()
        assert persisted["parser_version"] == PARSER_VERSION
        assert persisted["canonicalization_version"] == CANONICALIZATION_VERSION
        assert persisted["text_content"] is None
        assert persisted["content_ref"] is None
        assert persisted["retention_class"] == "NONE"
        serialized = json.dumps(persisted, ensure_ascii=False)
        assert canary not in serialized
        assert "Latency spiked" not in serialized
        assert "def retry" not in serialized
        for forbidden_key in FORBIDDEN_PERSISTED_RAW_TEXT_KEYS:
            assert forbidden_key not in persisted

    # Issue body emits ISSUE_SECTION with identical code-fence protection
    issue_parsed = parse_content_units(doc_res.document, SourceObjectKind.ISSUE_BODY)
    assert [u.metadata.unit_type for u in issue_parsed.units] == [
        ContentUnitType.ISSUE_SECTION,
        ContentUnitType.ISSUE_SECTION,
        ContentUnitType.ISSUE_SECTION,
    ]


def test_unified_diff_parses_hunks_and_tracks_renames_without_leaking_hunk_context() -> None:
    """DATA-02: Unified diff splits per file/hunk, tracks rename paths, strips code context from hunk_header."""
    diff_text = (
        "diff --git a/src/old_worker.py b/src/retry_worker.py\n"
        "similarity index 88%\n"
        "rename from src/old_worker.py\n"
        "rename to src/retry_worker.py\n"
        "--- a/src/old_worker.py\n"
        "+++ b/src/retry_worker.py\n"
        "@@ -10,4 +10,5 @@ def secret_canary_func_name():\n"
        " def process(job):\n"
        "-    run_once(job)\n"
        "+    run_with_idempotent_retry(job)\n"
        "+    record_metric(job.id)\n"
        "     return True\n"
        "diff --git a/src/metrics.py b/src/metrics.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/metrics.py\n"
        "+++ b/src/metrics.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def record_metric(job_id):\n"
        "+    emit('retry.ok', job_id)\n"
        "     return None\n"
    )
    doc_res = canonicalize_source_bytes(diff_text.encode("utf-8"))
    assert doc_res.document is not None

    parsed = parse_content_units(doc_res.document, SourceObjectKind.UNIFIED_DIFF)
    assert parsed.is_partial is False
    assert parsed.skip_records == ()
    assert len(parsed.units) == 2

    hunk0, hunk1 = parsed.units
    assert hunk0.metadata.unit_type == ContentUnitType.DIFF_HUNK
    assert hunk0.metadata.anchor_resolution_state == AnchorResolutionState.RENAMED_TRACKED
    assert hunk0.metadata.support_eligible is True
    assert hunk0.metadata.original_locator.file_path == "src/retry_worker.py"
    assert hunk0.metadata.original_locator.previous_file_path == "src/old_worker.py"
    assert hunk0.metadata.original_locator.is_renamed is True
    assert hunk0.metadata.original_locator.hunk_header == "@@ -10,4 +10,5 @@"
    assert hunk0.metadata.original_locator.old_start == 10
    assert hunk0.metadata.original_locator.old_lines == 4
    assert hunk0.metadata.original_locator.new_start == 10
    assert hunk0.metadata.original_locator.new_lines == 5

    # Trailing function context after second @@ must NEVER leak into persisted metadata
    persisted0 = json.dumps(hunk0.metadata.to_persisted_anchor_dict())
    assert "secret_canary_func_name" not in persisted0
    assert "run_with_idempotent_retry" not in persisted0

    assert hunk1.metadata.unit_type == ContentUnitType.DIFF_HUNK
    assert hunk1.metadata.anchor_resolution_state == AnchorResolutionState.RESOLVED
    assert hunk1.metadata.original_locator.file_path == "src/metrics.py"
    assert hunk1.metadata.original_locator.previous_file_path is None
    assert hunk1.metadata.original_locator.is_renamed is False
    assert hunk1.metadata.original_locator.hunk_header == "@@ -1,2 +1,3 @@"

    # Both hunks roundtrip cleanly
    for unit in parsed.units:
        verified = extract_verified_span(doc_res.document, unit.metadata.locator)
        assert verified.verified is True
        assert verified.span is not None
        assert verified.span.text == unit.text


def test_outdated_and_renamed_review_comment_anchor_resolution() -> None:
    """DATA-02: Outdated/unmatched review comments must become OUTDATED_UNRESOLVED (never silently supported)."""
    diff_text = (
        "diff --git a/src/legacy.py b/src/modern.py\n"
        "similarity index 90%\n"
        "rename from src/legacy.py\n"
        "rename to src/modern.py\n"
        "--- a/src/legacy.py\n"
        "+++ b/src/modern.py\n"
        "@@ -20,3 +20,4 @@\n"
        " def handle():\n"
        "+    validate_idempotency()\n"
        "     return 200\n"
    )
    diff_doc = canonicalize_source_bytes(diff_text.encode("utf-8")).document
    assert diff_doc is not None
    diff_parsed = parse_content_units(diff_doc, SourceObjectKind.UNIFIED_DIFF)
    current_diff_meta = diff_parsed.persisted_metadata
    hunk_sha = current_diff_meta[0].locator.span_sha256

    comment_doc = canonicalize_source_bytes(
        b"Good catch adding idempotency validation on line 21."
    ).document
    assert comment_doc is not None

    # 1. Renamed file + matching hunk hash & line range -> RENAMED_TRACKED & support_eligible=True
    renamed_ok = parse_content_units(
        comment_doc,
        SourceObjectKind.REVIEW_COMMENT,
        review_comment_context=ReviewCommentContext(
            comment_id="rc-101",
            thread_id="th-10",
            commit_sha="commit-head-2",
            original_commit_sha="commit-old-1",
            file_path="src/legacy.py",
            diff_side="RIGHT",
            line_start=21,
            line_end=21,
            is_outdated=False,
            anchored_hunk_sha256=hunk_sha,
        ),
        current_diff_units=current_diff_meta,
    )
    meta_ok = renamed_ok.units[0].metadata
    assert meta_ok.unit_type == ContentUnitType.REVIEW_COMMENT
    assert meta_ok.anchor_resolution_state == AnchorResolutionState.RENAMED_TRACKED
    assert meta_ok.support_eligible is True
    assert meta_ok.original_locator.previous_file_path == "src/legacy.py"
    assert meta_ok.original_locator.file_path == "src/modern.py"

    # 2. Renamed file BUT hunk hash drifted -> OUTDATED_UNRESOLVED & support_eligible=False
    renamed_drifted = parse_content_units(
        comment_doc,
        SourceObjectKind.REVIEW_COMMENT,
        review_comment_context=ReviewCommentContext(
            comment_id="rc-102",
            thread_id="th-11",
            commit_sha="commit-head-2",
            original_commit_sha="commit-old-1",
            file_path="src/legacy.py",
            diff_side="RIGHT",
            line_start=21,
            line_end=21,
            is_outdated=False,
            anchored_hunk_sha256="sha256:" + "a" * 64,
        ),
        current_diff_units=current_diff_meta,
    )
    meta_drifted = renamed_drifted.units[0].metadata
    assert meta_drifted.anchor_resolution_state == AnchorResolutionState.OUTDATED_UNRESOLVED
    assert meta_drifted.support_eligible is False

    # 3. Deleted file or moved line outside current hunk -> OUTDATED_UNRESOLVED & support_eligible=False
    deleted_hunk_comment = parse_content_units(
        comment_doc,
        SourceObjectKind.REVIEW_COMMENT,
        review_comment_context=ReviewCommentContext(
            comment_id="rc-103",
            thread_id="th-12",
            commit_sha="commit-head-2",
            original_commit_sha="commit-old-1",
            file_path="src/deleted_module.py",
            diff_side="RIGHT",
            line_start=50,
            line_end=55,
            is_outdated=False,
        ),
        current_diff_units=current_diff_meta,
    )
    meta_deleted = deleted_hunk_comment.units[0].metadata
    assert meta_deleted.anchor_resolution_state == AnchorResolutionState.OUTDATED_UNRESOLVED
    assert meta_deleted.support_eligible is False

    # 4. Explicit GitHub is_outdated=True -> always OUTDATED_UNRESOLVED even if line overlaps
    explicit_outdated = parse_content_units(
        comment_doc,
        SourceObjectKind.REVIEW_COMMENT,
        review_comment_context=ReviewCommentContext(
            comment_id="rc-104",
            commit_sha="commit-head-2",
            original_commit_sha="commit-old-1",
            file_path="src/modern.py",
            diff_side="RIGHT",
            line_start=21,
            line_end=21,
            is_outdated=True,
        ),
        current_diff_units=current_diff_meta,
    )
    meta_outdated = explicit_outdated.units[0].metadata
    assert meta_outdated.anchor_resolution_state == AnchorResolutionState.OUTDATED_UNRESOLVED
    assert meta_outdated.support_eligible is False


def test_all_eight_official_unit_types_are_supported() -> None:
    """All 8 official unit_type values parse cleanly and preserve Option C invariants."""
    pr_title_doc = canonicalize_source_bytes(b"feat: idempotent billing webhook queue").document
    comment_doc = canonicalize_source_bytes(b"Looks good after backoff fix.").document
    commit_doc = canonicalize_source_bytes(b"fix(billing): add retry jitter\n\nFixes #418").document
    symbol_doc = canonicalize_source_bytes(b"def enqueue(job):\n    return dedup(job)\n").document
    assert pr_title_doc and comment_doc and commit_doc and symbol_doc

    u_title = parse_content_units(pr_title_doc, SourceObjectKind.PR_TITLE).units[0]
    u_comment = parse_content_units(
        comment_doc,
        SourceObjectKind.COMMENT,
        comment_context=CommentContext(comment_id="c-1", thread_id="t-1"),
    ).units[0]
    u_commit = parse_content_units(
        commit_doc,
        SourceObjectKind.COMMIT_MESSAGE,
        commit_context=CommitContext(commit_sha="abc1234"),
    ).units[0]
    u_symbol = parse_content_units(
        symbol_doc,
        SourceObjectKind.SYMBOL_CHANGE,
        symbol_change_context=SymbolChangeContext(
            file_path="src/queue.py",
            symbol_name="enqueue",
            symbol_kind="function",
            line_start=12,
            line_end=13,
        ),
    ).units[0]

    assert u_title.metadata.unit_type == ContentUnitType.PR_TITLE
    assert u_comment.metadata.unit_type == ContentUnitType.COMMENT
    assert u_comment.metadata.original_locator.comment_id == "c-1"
    assert u_commit.metadata.unit_type == ContentUnitType.COMMIT_MESSAGE
    assert u_commit.metadata.original_locator.commit_sha == "abc1234"
    assert u_symbol.metadata.unit_type == ContentUnitType.SYMBOL_CHANGE
    assert u_symbol.metadata.original_locator.symbol_name == "enqueue"
    assert u_symbol.metadata.original_locator.symbol_kind == "function"

    # Enum completeness check
    assert {t.value for t in ContentUnitType} == {
        "PR_TITLE",
        "PR_BODY_SECTION",
        "ISSUE_SECTION",
        "COMMENT",
        "REVIEW_COMMENT",
        "COMMIT_MESSAGE",
        "DIFF_HUNK",
        "SYMBOL_CHANGE",
    }
