# T-001A Event Synthetic Dataset Draft (`evaluation/event-draft/`)

This directory contains **AI-authored synthetic Event fixtures, DRAFT expected proposals, JSON Schemas, coverage/review metadata, isolated negative fixtures, and an automated integrity linter** for Career Memory **T-001A**.

## Scope & Boundaries

- **Included**:
  - 18 synthetic fixture cases (`cases/EV-001` through `cases/EV-018`: 16 `MVP` + 2 `POST_MVP` tagged `B-003`).
  - 6 isolated intentionally broken negative variants (`negative-fixtures/NEG-001` through `NEG-006`).
  - Strict separation of `synthetic_sources`, `user_commands`, `expected_proposal` (`approval_status="DRAFT"`), and `rationale` (`confirmed_spec_rules` / `unconfirmed_spec_tokens` / `truly_undecided_items`).
  - JSON Schemas (`schemas/manifest.schema.json`, `schemas/case.schema.json`).
  - Coverage matrix (`coverage-matrix.json`, `coverage-matrix.md`) and T-001B human review queue (`review-queue.json`, `review-queue.md`).
  - Zero-dependency Python fixture linter (`lint/lint_fixtures.py`) and regression test suite (`lint/test_lint_fixtures.py`).
- **Excluded (explicitly NOT in T-001A)**:
  - Event Builder runtime implementation.
  - Human ground-truth review, approval, or golden dataset freeze (**T-001B**).
  - Real Event Builder quality evaluation (**M3 / T-306A**).
  - Any CLAIM-QA files, shared schemas, CI workflows, or physical database table assumptions (**OD-007**).
  - Any real repository text, real accounts, GitHub/LLM API calls, or billable runs.

## Confirmed Rules vs. Unconfirmed Tokens

- **Confirmed official spec rules applied across all 18 cases**:
  - Merged PR is the default Event candidate.
  - Default-branch direct commit is a candidate **only** when PR association lookup completed normally (`COMPLETED`) and observed `NO_MATCH_OBSERVED`.
  - `UNKNOWN` is **never** interpreted as `NO_MATCH_OBSERVED` (no PR).
  - Issue/ticket links attach as `CONTEXT_ONLY` and **never** auto-merge.
  - User manual merge is permitted **only within the same repository** (and workspace) and is counted as **one `USER_COMPOSITE`** without double-counting members.
  - `USER_COMPOSITE` nesting is strictly forbidden (neither a `USER_COMPOSITE` root nor a subsumed member may be merged into another `USER_COMPOSITE`).
  - Dissolving a `USER_COMPOSITE` restores its constituent members and marks dependent Claims as **`stale`**.
- **Unconfirmed wire tokens (`UNCONFIRMED`, never guessed)**:
  - Because the linked Notion pages (`T-001A`, `AI 합성 Dataset 가이드`, `Snapshot Import`, `ADR-018`) require authentication and could not be accessed via HTTP, exact wire status names, error codes, and command names not explicitly confirmed are stored as `"UNCONFIRMED"` (enforced by lint rule `NO_GUESSED_UNCONFIRMED_SPEC_TOKENS`) and listed in `review-queue.md`.

## Provenance & Reproducibility Limits

- **Base commit**: `efe251071fbee1be9a2ffee680a73f3ca3cd8084` (`main`; note `AGENTS.md` was not present at this commit).
- **Branch / Worktree**: `qa/event-fixtures` (`.worktrees/qa-event-fixtures`).
- **Generation method**: Interactive AI authoring and spec-alignment pass by the EVENT-QA agent; `0` external GitHub, Notion, or scripted LLM API calls.
- **Model / Seed limitation**: The local workspace environment does not expose the underlying model build/checkpoint identifier or a deterministic sampling seed/temperature. Token-for-token regeneration via seed is **not reproducible**; draft reproducibility relies on immutable git commits and deterministic lint checks.

## Running the Fixture Lint & Regression Tests

Run from the worktree root (`.worktrees/qa-event-fixtures`):

```sh
# 1. Lint the full valid draft dataset (manifest, 18 cases, coverage matrix, review queue)
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft

# 2. Run linter against an intentionally broken negative variant (expected: non-zero exit + rule code)
python3 -B evaluation/event-draft/lint/lint_fixtures.py --case-file evaluation/event-draft/negative-fixtures/NEG-001-broken-id-ref.json

# 3. Verify all 6 isolated negative variants fail with their exact expected rule codes
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft --verify-negatives

# 4. Run full automated unit & regression test suite
python3 -B -m unittest discover -s evaluation/event-draft/lint -v
```

> **Note**: These tests validate **synthetic dataset format, referential integrity, governance rules, unconfirmed-token discipline, and Event-domain safety invariants only**. Passing lint does **not** constitute ground-truth approval / golden freeze (T-001B) or Event Builder quality verification (M3/T-306A).

## T-001B Handoff Checklist

1. Open `review-queue.md` Section 2 and fill exact official wire tokens (`spec_event_type`, `spec_lifecycle_status`, `spec_hold_status`, `spec_command_name`, `spec_error_code`, `spec_claim_status`, Requirement/Test IDs) from the canonical Notion docs.
2. Resolve the truly undecided behavioral items in `review-queue.md` Section 3 (P0/P1/P2).
3. Independently verify every `expected_proposal` against human engineering judgment before transitioning `approval_status` from `DRAFT` to frozen golden truth in T-001B.
4. Keep `negative-fixtures/` strictly isolated from the frozen evaluation set.
