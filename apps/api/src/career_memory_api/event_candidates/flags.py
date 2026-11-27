"""Pure deterministic review-flag evaluator (Work Event Guide §7, EV-007).

Zero LLM calls, zero network calls, zero raw-body persistence. Evaluates only
non-raw metadata signals into a deterministically sorted tuple of ReviewFlagCode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from career_memory_api.event_candidates.models import ReviewFlagCode
from career_memory_api.schema_contracts.enums import PrAssociationState, WorkEventAnchorMode

# Conventional-commit / tag prefixes stripped before evaluating title specificity.
_CONVENTIONAL_PREFIX_RE = re.compile(
    r"^\s*(?:(?:feat|fix|chore|wip|docs|refactor|style|test|tests|ci|build|perf|revert|hotfix|misc)"
    r"(?:\([^)]*\))?)\s*:\s*",
    re.IGNORECASE,
)
_TICKET_OR_PR_TOKEN_RE = re.compile(r"^(?:#?\d+|[a-z]{2,10}-\d+)$", re.IGNORECASE)
_ALPHA_TOKEN_RE = re.compile(r"#?[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*")
_COMPOUND_CLAUSE_SPLIT_RE = re.compile(r"\s+(?:and|&|\+)\s+|;\s*|\s*,\s*and\s+", re.IGNORECASE)

_GENERIC_TITLE_WORDS: frozenset[str] = frozenset(
    {
        "fix",
        "fixes",
        "fixed",
        "bug",
        "bugs",
        "bugfix",
        "hotfix",
        "update",
        "updates",
        "updated",
        "wip",
        "misc",
        "chore",
        "chores",
        "cleanup",
        "clean",
        "refactor",
        "change",
        "changes",
        "stuff",
        "temp",
        "tmp",
        "test",
        "tests",
        "minor",
        "small",
        "quick",
        "work",
        "in",
        "progress",
        "file",
        "files",
        "code",
        "thing",
        "things",
        "nit",
        "nits",
        "typo",
        "typos",
        "lint",
        "format",
        "fmt",
        "patch",
        "draft",
        "tbd",
        "todo",
        "untitled",
        "merge",
        "sync",
        "initial",
        "commit",
    }
)

# Distinct work-intent verbs for detecting compound independent tasks in titles (e.g., EV-007).
_ACTION_VERB_FAMILIES: dict[str, str] = {
    "add": "ADD",
    "introduce": "ADD",
    "implement": "ADD",
    "create": "ADD",
    "build": "ADD",
    "enable": "ADD",
    "expose": "ADD",
    "support": "ADD",
    "refactor": "REFACTOR",
    "rewrite": "REFACTOR",
    "reorganize": "REFACTOR",
    "restructure": "REFACTOR",
    "cleanup": "REFACTOR",
    "clean": "REFACTOR",
    "simplify": "REFACTOR",
    "fix": "FIX",
    "resolve": "FIX",
    "patch": "FIX",
    "repair": "FIX",
    "correct": "FIX",
    "remove": "REMOVE",
    "delete": "REMOVE",
    "drop": "REMOVE",
    "deprecate": "REMOVE",
    "migrate": "MIGRATE",
    "upgrade": "MIGRATE",
    "bump": "MIGRATE",
    "optimize": "OPTIMIZE",
    "tune": "OPTIMIZE",
    "speedup": "OPTIMIZE",
    "document": "DOCS",
    "rename": "RENAME",
}

_WRAPPER_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        "src",
        "app",
        "apps",
        "packages",
        "package",
        "services",
        "service",
        "libs",
        "lib",
        "modules",
        "module",
        "pkg",
        "internal",
        "cmd",
        "crates",
        "components",
    }
)

_COMPANION_TEST_OR_DOC_ROOTS: frozenset[str] = frozenset(
    {
        "test",
        "tests",
        "__tests__",
        "spec",
        "specs",
        "doc",
        "docs",
    }
)

_CONFLICTING_LABEL_PAIRS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"bug", "bugfix", "hotfix"}), frozenset({"feature", "enhancement", "feat"})),
    (frozenset({"refactor", "chore", "tech-debt"}), frozenset({"feature", "enhancement", "feat"})),
)


@dataclass(frozen=True, slots=True)
class ReviewFlagInputs:
    """Metadata-only deterministic inputs for computing work_events review_flags."""

    anchor_mode: WorkEventAnchorMode
    title: str
    is_placeholder_title: bool = False
    changed_paths: tuple[str, ...] = ()
    commit_changed_path_groups: tuple[tuple[str, ...], ...] = ()
    top_level_dirs: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    linked_issue_keys: tuple[str, ...] = ()
    has_partial_source_skipped: bool = False
    has_ambiguous_author_identity: bool = False
    parent_count: int = 1
    pr_association_state: PrAssociationState | None = None
    explicit_mixed_purpose_hint: bool = False


def is_generic_title(title: str, *, is_placeholder: bool = False) -> bool:
    """Return True when title is overly short or composed purely of generic filler words."""
    if is_placeholder:
        return False
    cleaned = title.strip()
    if not cleaned:
        return True

    without_prefix = _CONVENTIONAL_PREFIX_RE.sub("", cleaned).strip()
    if not without_prefix:
        return True

    raw_tokens = [
        tok.lower()
        for tok in _ALPHA_TOKEN_RE.findall(without_prefix)
        if not _TICKET_OR_PR_TOKEN_RE.match(tok)
    ]
    if not raw_tokens:
        return True

    non_generic = [tok for tok in raw_tokens if tok not in _GENERIC_TITLE_WORDS]
    if not non_generic:
        return True

    # Extremely short single-word titles (e.g. "foo", "abc") provide insufficient review context.
    if len(cleaned) < 6 and len(raw_tokens) <= 1:
        return True
    return False


def extract_component_bucket(raw_path: str) -> tuple[str | None, bool]:
    """Normalize a repository file path into ``(component_bucket, is_test_or_doc_companion)``."""
    normalized = raw_path.strip().replace("\\", "/").lstrip("./")
    if not normalized:
        return None, False
    parts = [segment.lower() for segment in normalized.split("/") if segment and segment != "."]
    if not parts:
        return None, False

    first = parts[0]
    if first in _COMPANION_TEST_OR_DOC_ROOTS:
        return first, True

    if first in _WRAPPER_DIR_SEGMENTS and len(parts) >= 3:
        return f"{first}/{parts[1]}", False
    if len(parts) >= 2:
        return first, False
    return f"root:{first}", False


def _has_compound_action_title(title: str) -> bool:
    """Detect independent work clauses joined in a single title (e.g. 'Refactor X and add Y')."""
    cleaned = _CONVENTIONAL_PREFIX_RE.sub("", title.strip())
    if not cleaned:
        return False
    clauses = [c.strip() for c in _COMPOUND_CLAUSE_SPLIT_RE.split(cleaned) if c.strip()]
    if len(clauses) < 2:
        return False

    verb_families: set[str] = set()
    substantive_clauses = 0
    for clause in clauses:
        tokens = [t.lower() for t in _ALPHA_TOKEN_RE.findall(clause)]
        if len(tokens) < 2:
            continue
        lead_verb = _ACTION_VERB_FAMILIES.get(tokens[0])
        if lead_verb is not None:
            verb_families.add(lead_verb)
            substantive_clauses += 1
    return substantive_clauses >= 2 and len(verb_families) >= 2


def has_mixed_purpose_signals(inputs: ReviewFlagInputs) -> bool:
    """Detect deterministic mixed-purpose signals without ever performing auto-splits (EV-007)."""
    if inputs.explicit_mixed_purpose_hint:
        return True

    # 1. Explicit top-level directory spread (>= 3 distinct non-companion top-level dirs)
    explicit_prod_dirs = {
        d.strip().lower().strip("/")
        for d in inputs.top_level_dirs
        if d and d.strip() and d.strip().lower().strip("/") not in _COMPANION_TEST_OR_DOC_ROOTS
    }
    if len(explicit_prod_dirs) >= 3:
        return True

    # 2. Component spread derived from changed_paths and per-commit path groups
    all_paths: list[str] = list(inputs.changed_paths)
    for group in inputs.commit_changed_path_groups:
        all_paths.extend(group)

    prod_components: set[str] = set()
    top_dirs: set[str] = set()
    for path in all_paths:
        bucket, is_companion = extract_component_bucket(path)
        if bucket is None or is_companion:
            continue
        prod_components.add(bucket)
        top_dirs.add(bucket.split("/", 1)[0])

    if len(top_dirs) >= 3 or len(prod_components) >= 3:
        return True

    # Disjoint production components touched across distinct commits in the same PR (EV-007)
    if len(prod_components) >= 2 and len(inputs.commit_changed_path_groups) >= 2:
        commit_bucket_sets: list[set[str]] = []
        for group in inputs.commit_changed_path_groups:
            group_buckets = {
                b
                for p in group
                for b, is_comp in (extract_component_bucket(p),)
                if b is not None and not is_comp
            }
            if group_buckets:
                commit_bucket_sets.append(group_buckets)
        for i in range(len(commit_bucket_sets)):
            for j in range(i + 1, len(commit_bucket_sets)):
                if commit_bucket_sets[i].isdisjoint(commit_bucket_sets[j]):
                    return True

    # 3. Compound title expressing multiple independent action families
    if not inputs.is_placeholder_title and _has_compound_action_title(inputs.title):
        return True

    # 4. Conflicting independent label categories (e.g. bug + feature)
    normalized_labels = {lbl.strip().lower() for lbl in inputs.labels if lbl and lbl.strip()}
    for left_group, right_group in _CONFLICTING_LABEL_PAIRS:
        if (normalized_labels & left_group) and (normalized_labels & right_group):
            return True

    return False


def evaluate_review_flags(inputs: ReviewFlagInputs) -> tuple[ReviewFlagCode, ...]:
    """Deterministically evaluate all Work Event Guide §7 review flags."""
    flags: set[ReviewFlagCode] = set()

    if is_generic_title(inputs.title, is_placeholder=inputs.is_placeholder_title):
        flags.add(ReviewFlagCode.GENERIC_TITLE_OR_SUMMARY)

    if inputs.anchor_mode is WorkEventAnchorMode.PR and has_mixed_purpose_signals(inputs):
        flags.add(ReviewFlagCode.MIXED_PURPOSE_SIGNALS)

    distinct_issues = {k.strip().lower() for k in inputs.linked_issue_keys if k and k.strip()}
    if len(distinct_issues) >= 2:
        flags.add(ReviewFlagCode.MULTIPLE_LINKED_ISSUES)

    if inputs.has_partial_source_skipped:
        flags.add(ReviewFlagCode.PARTIAL_SOURCE_SKIPPED)

    if inputs.has_ambiguous_author_identity:
        flags.add(ReviewFlagCode.AMBIGUOUS_AUTHOR_IDENTITY)

    if inputs.anchor_mode is WorkEventAnchorMode.DIRECT_COMMIT:
        if inputs.pr_association_state is PrAssociationState.NO_MATCH_OBSERVED:
            flags.add(ReviewFlagCode.DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED)
        if inputs.parent_count > 1:
            flags.add(ReviewFlagCode.REVIEW_REQUIRED_MULTI_PARENT)

    return tuple(sorted(flags, key=lambda code: code.value))


__all__ = [
    "ReviewFlagInputs",
    "evaluate_review_flags",
    "extract_component_bucket",
    "has_mixed_purpose_signals",
    "is_generic_title",
]
