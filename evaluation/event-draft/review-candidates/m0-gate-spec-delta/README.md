# M0-GATE Spec-Delta Review Candidate Pack (`evaluation/event-draft/review-candidates/m0-gate-spec-delta/`)

This isolated directory holds **unapproved DRAFT review candidates** addressing the four confirmed M0-GATE spec gaps in the `T-001A/B` Event synthetic dataset **without modifying or overwriting `1.0.0-mvp-approved`** (`qa/event-fixtures@140543b983bbdf41584bc5b3a65d1ec67022f951`).

## Isolation & Scope Boundaries

- **Untouched baseline (`1.0.0-mvp-approved`)**:
  - `manifest.json`, `cases/EV-001..EV-016.json`, `cases/EV-017..EV-018.json` (Post-MVP `B-003` drafts), `negative-fixtures/NEG-001..NEG-006.json`, `schemas/*.json`, `coverage-matrix.*`, `review-queue.*`, and `lint/*` are **not modified**.
- **Isolated candidate contents (`DRAFT_REVIEW_CANDIDATE`, `approved_by: null`)**:
  - `DELTA-EV-002` — Amendment candidate for `EV-002` (Gap 1: `NO_MATCH_OBSERVED != definitely no PR` + user association-confirmation-needed expectation; API field name & badge string kept `UNCONFIRMED`).
  - `CAND-EV-019` — New MVP candidate (Gap 2: provider-reported `default_branch="trunk"` vs non-default `"main"`, squash/rebase PR dedup, fixture-premise `out_of_window_pr_associations` dedup for an otherwise eligible in-window commit without creating a new PR Event or prescribing diagnostic persistence, and no timestamp/message heuristic PR linking).
  - `CAND-EV-020` — New MVP candidate (Gap 2: provider-reported `default_branch="master"` vs non-default `"main"` + `UNKNOWN` -> `PARTIAL` hold preserving 0 promoted Events).
  - `CAND-EV-021` — New MVP candidate (Gap 3: explicit `CANNOT_LINK` between two active, same-workspace, same-repo, version-matched Events -> `CANNOT_LINK_CONFLICT` with zero mutation; separate from `EV-013`).
  - `CAND-EV-022` — New MVP candidate (Gap 4a: `MVP API §5` `POST /work-events/{id}/actions/confirm` — no separate `Keep` API invented — transitioning `CANDIDATE` v1 → `CONFIRMED` v2 + `DB Model` domain `ReviewAction` `CONFIRM`, separated from unconfirmed external response tokens).
  - `CAND-EV-023` — New MVP candidate (Gap 4b: `MVP API §5` `PATCH /work-events/{id}` + `Event Guide §6` active `CANDIDATE` v1 edit → new `CANDIDATE` v2 + `DB Model` domain `ReviewAction` `EDIT`, strictly distinct from `manual_merge`; grouped in the same evaluation bundle `bundle_cand_workflow_audit` as `CAND-EV-022`; editing already-`CONFIRMED` Events excluded from expected truth).
- **Explicitly NOT included**:
  - Owner approval / dataset version bump / golden freeze.
  - Product/Event Builder runtime code, `EVAL-EVENT` quality evaluation, CLAIM-QA files, shared CI, shared schema edits, OD-007 physical tables, or unconfirmed HTTP status / external response tokens.

## Verification Commands & Exact Scope Separation

> **Important**: The existing linter (`evaluation/event-draft/lint/lint_fixtures.py`) validates ONLY the untouched `1.0.0-mvp-approved` dataset (`manifest.json`, `EV-001..EV-018`, `NEG-001..NEG-006`) and does **NOT** read this `review-candidates/` directory. The new candidates are validated separately by `validate_delta_candidates.py` and `test_delta_candidates.py`.

Run from `/Users/eddy81848/Projects/Career Memory/.worktrees/qa-event-fixtures`:

```sh
# 1. Existing 1.0.0-mvp-approved verification (does NOT read review-candidates/)
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft --verify-negatives
python3 -B -m unittest discover -s evaluation/event-draft/lint -v
git diff --check

# 2. Dedicated deterministic validation & negative regression tests for M0-GATE review candidates
python3 -B evaluation/event-draft/review-candidates/m0-gate-spec-delta/validate_delta_candidates.py
python3 -B -m unittest discover -s evaluation/event-draft/review-candidates/m0-gate-spec-delta -v
```
