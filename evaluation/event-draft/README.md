# Event Synthetic Dataset (`evaluation/event-draft/`, `1.0.0-mvp-approved`)

This directory contains **synthetic Event fixtures, T-001B human-approved MVP synthetic regression ground truth (`EV-001`..`EV-016`), unapproved Post-MVP `B-003` drafts (`EV-017`..`EV-018`), JSON Schemas, same-source evaluation bundle grouping, SHA-256 integrity hashes, isolated negative fixtures (`NEG-001`..`NEG-006`), and an automated linter + regression test suite** for Career Memory.

## Dataset Version & Approval Scope

- **Dataset ID / Version**: `career-memory-event-synthetic-mvp-v1.0.0` / **`1.0.0-mvp-approved`** (`task_ref: T-001B`, `approval_status: PARTIALLY_APPROVED_MVP_REGRESSION`).
- **Approval Baseline**: `qa/event-fixtures@0773ac5740b358f4168777fda1e578231cc6dff9` (base `main`: `efe251071fbee1be9a2ffee680a73f3ca3cd8084`).
- **Approved for MVP Synthetic Regression (`16` cases)**: `EV-001`..`EV-016` (`eval_partition: MVP_SYNTHETIC_REGRESSION`) — confirmed logical expectations approved by explicit human review.
- **Retained as Unapproved Post-MVP Drafts (`2` cases)**: `EV-017`, `EV-018` (`scope: POST_MVP`, `backlog_ref: B-003`, `approval_status: DRAFT`, `approved_by: null`, `eval_partition: POST_MVP_DRAFT_EXCLUDED`).
- **Isolated Negative Fixtures (`6` variants)**: `negative-fixtures/NEG-001`..`NEG-006` (`eval_excluded: true`) — intentionally broken fixtures for linter verification only; never included in `cases/` or `evaluation_bundles`.
- **Excluded from This Package**:
  - Event Builder runtime implementation or product API changes.
  - Real Event Builder runtime quality evaluation (**M3 / T-306A / EVAL-EVENT** — never marked passed by fixture lint).
  - Freeze of unconfirmed HTTP status codes, multi-violation error precedence, or unconfirmed wire tokens.
  - CLAIM-QA files, shared schemas, CI workflows, OD-007 physical database table assumptions, Notion/cloud edits, or `main` branch merge/push.

## Truth Comparison Policy

- **Enforced in MVP synthetic regression (`EV-001`..`EV-016`)**:
  - Confirmed logical expectations (anchor modes, `PARTIAL` import hold, lifecycle statuses, disjoint split, same-repo manual merge, single-count search/metrics roll-up, exclude, dissolve + `STALE` propagation, workspace/repository/version isolation).
  - Unambiguous official Event API error codes (`NESTED_COMPOSITE_NOT_ALLOWED`, `MEMBER_NOT_ACTIVE`, `EVENT_VERSION_CONFLICT`, `CROSS_REPOSITORY_MEMBER`).
  - **Zero state mutation on every rejected command**: `command_results[*].outcome == "REJECTED"`, `state_mutated == false`, `resulting_event_ids == []`, and unmutated `events`.
- **Excluded from truth comparison**:
  - Unconfirmed HTTP status codes (`409` vs `412`, `If-Match`, unauthorized `403` vs `404`).
  - Unconfirmed multi-violation error precedence (`EV-014` `cmd_syn_014_nested`: `MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE`).
  - Unconfirmed wire error codes for non-Event IDs (`EV-013`) and unauthorized cross-workspace external responses (`EV-016`).
  - Unconfirmed wire command names (`spec_command_name`), exact mixed-purpose review flag string (`EV-007`), diagnostic record status tokens (`EV-003`, `EV-004`, `EV-013`), ticket-only keyword search ranking (`EV-005`, `EV-006`), and direct detail GET shape for historical `SUPERSEDED` members (`EV-010`).

## Same-Source Evaluation Bundles & Isolation

Cases derived from the same synthetic source repository are grouped into the same evaluation bundle in `manifest.json` (`evaluation_bundles`) and each case's `eval_grouping` block (enforced automatically by `EVAL_BUNDLE_LINEAGE_ISOLATION`):

- **Multi-case MVP bundles (same synthetic source)**:
  - `bundle_mvp_ledger_service` (`acme-syn-lab/ledger-service`): `EV-001`, `EV-015` *(Post-MVP sibling `EV-017` isolated in `bundle_post_mvp_b003_draft`)*.
  - `bundle_mvp_catalog_api` (`acme-syn-lab/catalog-api`): `EV-006`, `EV-009`, `EV-012`.
  - `bundle_mvp_reporting_core` (`acme-syn-lab/reporting-core`): `EV-007`, `EV-008`.
- **Single-case MVP bundles (`9` bundles)**:
  - `bundle_mvp_edge_gateway` (`EV-002`), `bundle_mvp_billing_engine` (`EV-003`; *Post-MVP sibling `EV-018` isolated in `bundle_post_mvp_b003_draft`*), `bundle_mvp_search_indexer` (`EV-004`), `bundle_mvp_notification_hub` (`EV-005`), `bundle_mvp_queue_dispatcher` (`EV-010`), `bundle_mvp_web_portal` (`EV-011`), `bundle_mvp_auth_services` (`EV-013`), `bundle_mvp_analytics_pipe` (`EV-014`), `bundle_mvp_multi_workspace` (`EV-016`).
- **Post-MVP draft exclusion bundle (`1` bundle)**:
  - `bundle_post_mvp_b003_draft` (`POST_MVP_DRAFT_EXCLUDED`): `EV-017`, `EV-018`.
- **Negative variants (`eval_excluded: true`)**:
  - `NEG-001`..`NEG-006` live strictly under `negative-fixtures/` and are rejected by the linter if referenced in `evaluation_bundles`.

## Integrity Hashes (`SHA-256`)

All per-file and bundle SHA-256 digests are recorded in `manifest.json` (`integrity_hashes`) and verified on every lint run (`INTEGRITY_HASH_MISMATCH`):

- `approved_mvp_bundle_sha256` (`EV-001`..`EV-016`, current approved files): `e51878f094d77b2f32675ffc6b48bab013347620277ac91eed0996ae1ee2328e`
- `approved_mvp_baseline_bundle_sha256` (`EV-001`..`EV-016` at `0773ac5740b358f4168777fda1e578231cc6dff9`): `b1bdffa8bbec1e47f2401d383ae874466193598e2b664cbd415cac7372d1d8f6`
- `post_mvp_draft_bundle_sha256` (`EV-017`..`EV-018`, unapproved Post-MVP drafts): `1adb920c9df98ea18edbebffef5c5708521c18ef40a0fc4cb796ecb10fb4a17d`
- `schemas/case.schema.json`: `d97e2b04fb7527809776c59e780605b0a5d2d7e3d598182f854faf1a0cf5ed51`
- `schemas/manifest.schema.json`: `8eef5122b79dccbe80fa2e8b05baacff53a3d250150e7486050047d25247d770`

## Provenance & Reproducibility Limits

- **Base `main` commit**: `efe251071fbee1be9a2ffee680a73f3ca3cd8084` (`AGENTS.md` was not present at this commit).
- **Approval baseline commit**: `0773ac5740b358f4168777fda1e578231cc6dff9` on `qa/event-fixtures` (`.worktrees/qa-event-fixtures`).
- **External API calls**: `0` GitHub, Notion, or scripted LLM API calls.
- **Model / Seed limitation**: Interactive agent authoring does not expose a model checkpoint identifier or deterministic sampling seed. Reproducibility of this approved regression dataset relies on immutable git commits, SHA-256 digests in `manifest.json`, and deterministic lint verification.

## Running the Fixture Lint & Regression Tests

Run from the worktree root (`.worktrees/qa-event-fixtures`):

```sh
# 1. Lint the full dataset (manifest, 18 cases, bundles, SHA-256 hashes, coverage matrix, review queue)
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft

# 2. Verify all 6 isolated negative variants fail with their exact expected rule codes
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft --verify-negatives

# 3. Run full automated unit & regression test suite (20 tests)
python3 -B -m unittest discover -s evaluation/event-draft/lint -v

# 4. Check whitespace cleanliness
git diff --check
```

> **Note**: These tests validate **synthetic dataset format, referential integrity, approval/bundle governance, SHA-256 integrity, and Event-domain safety invariants only**. Passing lint does **not** execute or pass Event Builder runtime quality evaluation (**M3 / T-306A / EVAL-EVENT**).
