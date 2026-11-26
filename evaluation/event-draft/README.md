# T-001A Event Synthetic Dataset Draft (`evaluation/event-draft/`)

This directory contains **AI-authored synthetic Event fixtures, DRAFT expected proposals, JSON Schemas, coverage/review metadata, isolated negative fixtures, and an automated integrity linter** for Career Memory **T-001A**.

## Scope & Boundaries

- **Included**:
  - 18 synthetic fixture cases (`cases/EV-001` through `cases/EV-018`: 16 `MVP` + 2 `POST_MVP` tagged `B-003`).
  - 6 isolated intentionally broken negative variants (`negative-fixtures/NEG-001` through `NEG-006`).
  - Strict separation of `synthetic_sources`, `user_commands`, `expected_proposal` (`approval_status="DRAFT"`), and `rationale`.
  - JSON Schemas (`schemas/manifest.schema.json`, `schemas/case.schema.json`).
  - Coverage matrix (`coverage-matrix.json`, `coverage-matrix.md`) and T-001B human review queue (`review-queue.json`, `review-queue.md`).
  - Zero-dependency Python fixture linter (`lint/lint_fixtures.py`) and test suite (`lint/test_lint_fixtures.py`).
- **Excluded (explicitly NOT in T-001A)**:
  - Event Builder runtime implementation.
  - Human ground-truth review, approval, or dataset freeze (**T-001B**).
  - Real Event Builder quality evaluation (**M3 / T-306A**).
  - Any CLAIM-QA files, shared schemas, CI workflows, or physical database table assumptions (**OD-007**).
  - Any real repository text, real accounts, GitHub/LLM API calls, or billable runs.

## Provenance & Reproducibility Limits

- **Base commit**: `efe251071fbee1be9a2ffee680a73f3ca3cd8084` (`main`; note `AGENTS.md` was not present at this commit).
- **Branch / Worktree**: `qa/event-fixtures` (`.worktrees/qa-event-fixtures`).
- **Generation method**: Interactive AI authoring by the EVENT-QA agent; `0` external GitHub, Notion, or scripted LLM API calls.
- **Model / Seed limitation**: The local workspace environment does not expose the underlying model build/checkpoint identifier or a deterministic sampling seed/temperature. Token-for-token regeneration via seed is **not reproducible**; draft reproducibility relies on immutable git commits and deterministic lint checks.
- **Notion reference limitation**: The linked Notion documents (`T-001A`, `AI 합성 Dataset 가이드`, `Snapshot Import`, `ADR-018`) require authentication and could not be read via HTTP. All provisional status names, error codes, and behavioral boundaries are tagged `REVIEW_REQUIRED` and listed in `review-queue.md`.

## Running the Fixture Lint & Tests

Run from the worktree root (`.worktrees/qa-event-fixtures`):

```sh
# 1. Lint the full valid draft dataset (manifest, 18 cases, coverage matrix, review queue)
python3 evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft

# 2. Run linter against an intentionally broken negative variant (expected: non-zero exit + rule code)
python3 evaluation/event-draft/lint/lint_fixtures.py --case-file evaluation/event-draft/negative-fixtures/NEG-001-broken-id-ref.json

# 3. Verify all 6 isolated negative variants fail with their exact expected rule codes
python3 evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft --verify-negatives

# 4. Run full automated unit test suite (including duplicate case_id & manifest checks)
python3 -B -m unittest discover -s evaluation/event-draft/lint -v
```

> **Note**: These tests validate **synthetic dataset format, referential integrity, governance rules, and Event-domain safety invariants only**. Passing lint does **not** constitute ground-truth approval (T-001B) or Event Builder quality verification (M3/T-306A).

## T-001B Handoff Checklist

1. Open `review-queue.md` and resolve all P0/P1/P2 `REVIEW_REQUIRED` tokens against the canonical Notion Event Contract, Annotation Guide, ADR-018, Snapshot Import, and OD-010.
2. Update any provisional enum strings/error codes across `cases/EV-*.json` and `schemas/case.schema.json`.
3. Independently verify every `expected_proposal` against human engineering judgment before transitioning `approval_status` from `DRAFT` to frozen ground truth in T-001B.
4. Keep `negative-fixtures/` strictly isolated from the frozen evaluation set.
