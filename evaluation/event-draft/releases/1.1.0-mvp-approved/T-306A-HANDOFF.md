# T-306A Handoff — Approved 21-Case MVP Synthetic Regression Set (`1.1.0-mvp-approved`)

## 1. Authoritative Entrypoint for `T-306A`

`T-306A` workers **MUST** read ONLY the following release manifest as the single source of truth for the approved 21-case MVP synthetic regression suite:

- **Entrypoint Manifest**: `evaluation/event-draft/releases/1.1.0-mvp-approved/manifest.json`
- **Programmatic Loader**: `validate_approved_release.load_approved_21_case_set()` in `evaluation/event-draft/releases/1.1.0-mvp-approved/validate_approved_release.py`
- **Dataset Version**: `1.1.0-mvp-approved` (`approved_21_bundle_sha256: 91cac742b48ad3bebee2e84aac3f6d3c5d9202c65d2406f3828b8494bf114a4d`)
- **Base Commit / Candidate Pack**:
  - `qa/event-fixtures` HEAD `140543b983bbdf41584bc5b3a65d1ec67022f951` (base `main`: `efe251071fbee1be9a2ffee680a73f3ca3cd8084`)
  - Reviewed M0-GATE candidate pack (`13` preserved `DRAFT` files): `evaluation/event-draft/review-candidates/m0-gate-spec-delta/` (`candidate_bundle_sha256: fa8b380e09ad8891ee45d3d5d0d698323f8418a035a284f6efc34fcbeecf1730`)

### Anti-Misload Warnings (Verified Deterministically)

1. **DO NOT read `evaluation/event-draft/manifest.json` (`1.0.0-mvp-approved`) alone**: that baseline manifest contains only 16 MVP cases, omits `EV-019..EV-023`, and still points `EV-002` to the superseded baseline file.
2. **DO NOT naively union `cases/*.json` + `review-candidates/m0-gate-spec-delta/candidates/*.json`**: that duplicates `EV-002` (loading both `cases/EV-002-default-branch-direct-commit.json` and `DELTA-EV-002`) and leaks unapproved Post-MVP drafts `EV-017` and `EV-018`.
3. **ALWAYS resolve the exact 21 entries from `releases/1.1.0-mvp-approved/manifest.json -> approved_cases[]`**:
   - **15 unchanged baseline cases**: `EV-001`, `EV-003`..`EV-016` (`cases/EV-*.json`)
   - **1 replacement case**: `EV-002` → `review-candidates/m0-gate-spec-delta/candidates/DELTA-EV-002-direct-commit-confirmation-needed.json` (supersedes `cases/EV-002-default-branch-direct-commit.json`)
   - **5 new cases**: `EV-019`..`EV-023` (`review-candidates/m0-gate-spec-delta/candidates/CAND-EV-019..023*.json`)
   - **Strictly excluded**: `cases/EV-002-default-branch-direct-commit.json` (superseded), `EV-017`/`EV-018` (`POST_MVP` `B-003` drafts), `NEG-001..NEG-006` (isolated negative lint fixtures).

## 2. Scope of Owner Approval & `CAND-EV-021` Route Correction

- **Approved Scope**: Synthetic inputs and logical expected behaviors of the 21 MVP regression cases ONLY. Passing fixture checks does **NOT** approve product API response payload details or pass M3 / `T-306A` / `EVAL-EVENT` runtime evaluation.
- **`CAND-EV-021` API Route Handoff Correction**: Per **MVP API §5**, manual merge is defined at **`POST /work-events/actions/merge`**. The preserved `DRAFT` file `CAND-EV-021-active-events-cannot-link-conflict.json` (SHA-256 `6cfdd2360a7ec7be9f79be71b399b3f312edf979115db9fd594eb583952ef68d`) was intentionally left untouched to preserve review immutability; treat the earlier *"route unconfirmed"* wording in its draft note as superseded by `POST /work-events/actions/merge` recorded in `manifest.json`.
- **Confirmed Routes Summary (MVP API §5)**:
  - Manual merge (`EV-009`, `EV-013`, `EV-014`, `EV-016`, `EV-021`): `POST /work-events/actions/merge`
  - Confirm (`EV-022`): `POST /work-events/{id}/actions/confirm` (no separate `Keep` API)
  - Edit (`EV-023`): `PATCH /work-events/{id}`

## 3. Explicitly Excluded from Ground-Truth Comparison (Do NOT Assert as Truth)

- UI badge strings & unconfirmed API field names for direct-commit association confirmation (`DELTA-EV-002`, `CAND-EV-019`)
- External API `ReviewAction` response tokens (`CAND-EV-022`, `CAND-EV-023`; domain `ReviewAction` `CONFIRM`/`EDIT` IS confirmed)
- Unconfirmed HTTP status codes (`409` vs `412`, `If-Match`, `403` vs `404`, `CANNOT_LINK_CONFLICT` HTTP code, confirm/edit HTTP codes)
- Internal diagnostic record persistence format & live GitHub API lookup assumptions for out-of-window PR deduplication (`CAND-EV-019`)
- `OD-007` physical database tables / storage design
- Behavior when editing an already-`CONFIRMED` Event (`EV-023` covers active `CANDIDATE` → new `CANDIDATE` only)
- Multi-violation error precedence (`EV-014` `cmd_syn_014_nested`), non-Event ID wire error codes (`EV-013`), unauthorized cross-workspace external wire codes (`EV-016`), mixed-purpose flag literal (`EV-007`), and historical `SUPERSEDED` detail GET shape (`EV-010`)

## 4. Same-Source Evaluation Bundles (`16` Bundles Across `21` Cases)

Multi-case bundles sharing the same synthetic source repository MUST remain grouped in any evaluation split:
- `bundle_mvp_ledger_service` (`acme-syn-lab/ledger-service`): `EV-001`, `EV-015`
- `bundle_mvp_catalog_api` (`acme-syn-lab/catalog-api`): `EV-006`, `EV-009`, `EV-012`
- `bundle_mvp_reporting_core` (`acme-syn-lab/reporting-core`): `EV-007`, `EV-008`
- `bundle_cand_workflow_audit` (`acme-syn-lab/workflow-audit`): `EV-022`, `EV-023`
- Plus 12 single-case bundles (`EV-002`, `EV-003`, `EV-004`, `EV-005`, `EV-010`, `EV-011`, `EV-013`, `EV-014`, `EV-016`, `EV-019`, `EV-020`, `EV-021`).

## 5. Verification Commands (All 3 Layers)

Run from `/Users/eddy81848/Projects/Career Memory/.worktrees/qa-event-fixtures`:

```sh
# 1. Preserved 1.0.0-mvp-approved baseline linter & 20 unit tests
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft --verify-negatives
python3 -B -m unittest discover -s evaluation/event-draft/lint -v

# 2. Preserved 13-file M0-GATE DRAFT candidate pack validator & 11 unit tests
python3 -B evaluation/event-draft/review-candidates/m0-gate-spec-delta/validate_delta_candidates.py
python3 -B -m unittest discover -s evaluation/event-draft/review-candidates/m0-gate-spec-delta -v

# 3. Approved 1.1.0-mvp-approved 21-case release validator & anti-duplication/misload tests
python3 -B evaluation/event-draft/releases/1.1.0-mvp-approved/validate_approved_release.py
python3 -B -m unittest discover -s evaluation/event-draft/releases/1.1.0-mvp-approved -v
```
