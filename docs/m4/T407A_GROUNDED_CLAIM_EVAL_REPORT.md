# M4 Wave 4 (`T-407A`) — Grounded Claim & Refetch Evaluation and Gate Report

> **Gate Summary (Strict Honest Governance Separation — `ADR-018`, `ADR-013`, `ADR-020`, `OD-002`, `OD-014`)**
>
> | Gate | Status | Scope & Binding Caveat |
> |---|---|---|
> | **`SYNTHETIC_CONTRACT_AND_SAFETY_GATE`** | **`PASSED`** | Frozen `0.4.0-synthetic-golden` executed against real PostgreSQL 18 (`0010..0070`, `NOBYPASSRLS` + `FORCE RLS`), real FastAPI M4 routers, and live FE↔BE `127.0.0.1` loopback HTTP: **16/16 scenarios**, **29/29 claims**, **50/50 atoms**, **27/27 refetch cases (`REF-01..08`)**, **0 Plan C leaks**, **0 unsupported `SOURCE_VERIFIED` / approvals / exports**, **0 rejected candidates promoted**, **0 bundle drift**. |
> | **`LIVE_LLM_SEMANTIC_EVAL`** | **`NOT_RUN_BLOCKED_BY_OD002_OD014`** | Live external / subscription LLM execution remained hard-OFF (`external_live_llm_calls = 0`, `live_pilot_gate_eligible = false`). **NO live LLM semantic accuracy, live pilot readiness, or population error-rate (`<1%`) claim is made or permitted from this synthetic regression set.** |
> | **External Live Provider / Connector Refetch** | **`NOT_RUN_OFFLINE_HARNESS`** | All 27 `REF-01..08` checks ran against the real `RefetchResolverService` + `PostgresProvenanceMetadataStore` on PG18 using an offline in-memory `FixtureProviderContentFetcher` (`0` external network calls). |

---

## 1. Frozen Dataset Entrypoint & Cryptographic Integrity (`ADR-018`)

- **Authoritative Single Entrypoint**: `evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json` (`0.4.0-synthetic-golden`)
- **Archived `0.3.0-synthetic-golden` / `CANDIDATE_MANIFEST.json` Guard**: Enforced by `assert_golden_approval_manifest_only()` (`GoldenDatasetMisloadError` on any attempt to load `APPROVAL_MANIFEST.json`, `CANDIDATE_MANIFEST.json`, or any alternate path directly; zero merging or double-counting).
- **Read-Only Validators Executed**:
  - `evaluation/claim-draft/tools/validate_dataset.py` (`rc=0`, read-only `-B`)
  - `evaluation/claim-draft/tools/validate_golden_approval.py` (`rc=0`, read-only `-B`)
- **Frozen Fixture Immutability**: **0 bytes** modified under `evaluation/claim-draft/**`, `evaluation/event-draft/**`, or `contracts/import-v1/openapi.yaml`.
- **Verified Cryptographic Digests**:
  - `approval_manifest_sha256`: `sha256:aee94b92ac518ab97edb24247c779c742c96be85cb15edc6f02212f3ba85ea69`
  - `candidate_manifest_sha256`: `sha256:2e9ffde13dfa619cee9035fdf2f9746adfd6b925875a7a955790e210b4a0bcea`
  - `approved_candidate_bundle_sha256`: `sha256:27b6d1eeace3e4ac5771f1abb1555a2af3c6cd9446a9f7f10005404572d7b059`
  - `immutable_openapi_blob_sha`: `07594d75425744d6149250a9f7ff95ac24659f80`
  - `t407a_eval_bundle.v1.json` `deterministic_bundle_sha256`: `sha256:0acf726438062b16b5ea64ab2f8c193e3715026f9a3003bf8cf8dfdfa95adf5d`
- **Verified Exact Approved Counts**:
  - `20` source revisions, `30` content units, `34` fact candidates, `38` fact support validations, `31` canonical facts, `35` fact supports, `16` confirmed events, `16` scenarios (`SCN-01..16`), `29` claims, `50` claim atoms, `50` claim atom supports, `27` refetch cases (`refcase-01a..08d`), `17` evaluation families (`0` overlap between `DEV` and `TEST` across families, scenarios, claims, refetch cases, and source revisions).

---

## 2. Partition Rollups (`SYNTHETIC_REGRESSION_DEV` vs `SYNTHETIC_REGRESSION_TEST`)

| Partition | Scenarios | Claims | Atoms | `SOURCE_VERIFIED` (Expected / Observed) | Refetch Cases | Plan C Leaks |
|---|---:|---:|---:|---:|---:|---:|
| `SYNTHETIC_REGRESSION_DEV` | `8 / 8` | `15 / 15` | `27 / 27` | `6 / 6` | `2 / 2` | `0` |
| `SYNTHETIC_REGRESSION_TEST` | `8 / 8` | `14 / 14` | `23 / 23` | `5 / 5` | `25 / 25` | `0` |
| **Total (`0.4.0-synthetic-golden`)** | **`16 / 16`** | **`29 / 29`** | **`50 / 50`** | **`11 / 11`** | **`27 / 27`** | **`0`** |

### Safety Invariants Summary

| Invariant | Observed | Requirement | Status |
|---|---:|---:|---|
| `unsupported_source_verified_count` | `0` | `== 0` | `PASS` |
| `unsupported_approvals_count` | `0` | `== 0` | `PASS` |
| `unsupported_or_stale_exports_count` | `0` | `== 0` | `PASS` |
| `rejected_candidates_promoted_count` | `0` | `== 0` | `PASS` |
| `total_plan_c_leaks` (41 PG18 tables + logs + HTTP + exports + cache) | `0` | `== 0` | `PASS` |
| `live_llm_hard_off_verified` (`OD-002` / `OD-014` default OFF) | `true` | `== true` | `PASS` |
| `external_live_llm_calls` | `0` | `== 0` | `PASS` |
| `population_accuracy_claim_permitted` | `false` | `== false` | `PASS (Honest)` |

---

## 3. Per-Scenario Results (`SCN-01..SCN-16` on Real PG18 + FastAPI)

Every scenario ran in an isolated PostgreSQL 18 database (`0010..0070`, `NOBYPASSRLS` runtime roles `cm_api_app` / `cm_worker_app`, `ENABLE + FORCE RLS`) through the real M4 services (`FactCandidateExtractor` -> `FactSupportValidator` -> `CanonicalFactNormalizer` + `Contributions` -> `ClaimContextBuilder` / `ClaimGenerator` -> `ClaimAtomizer` -> `ClaimSupportValidator` -> `VerificationCalculator` -> `POST /v1/claims/{id}/actions/approve` -> `POST /v1/exports` -> multi-surface Plan C audit).

| Scenario | Partition | Family | Candidates | Canonical Facts | Claims | Atoms | Key M4 Invariants Verified | Result |
|---|---|---|---:|---:|---:|---:|---|---|
| `SCN-01` | `DEV` | `family-src01` | 0 | 0 | 1 | 1 | Empty evidence draft rejected by Stage 3A & blocked at Stage 3C / approval / export (`UNVERIFIED`) | `PASS` |
| `SCN-02` | `TEST` | `family-src02` | 1 | 1 | 1 | 2 | Grounded 2-atom `ACTION` claim -> `SOURCE_VERIFIED`, approved `200`, Markdown + JSON exported `201` | `PASS` |
| `SCN-03` | `DEV` | `family-src03` | 3 | 3 | 1 | 4 | Multi-unit Korean/EN technical + `METRIC` (`p95 620ms -> 180ms`) -> 4/4 `SUPPORTED`, `SOURCE_VERIFIED` | `PASS` |
| `SCN-04` | `DEV` | `family-src04` | 2 | 2 | 2 | 2 | `DIRECT` vs `CONTEXT`-only boundary (`claim-04-1` verified; `claim-04-2` `CONTEXT`-only blocked `409`) | `PASS` |
| `SCN-05` | `TEST` | `family-src05` | 2 | 2 | 2 | 4 | Cross-unit `CONTRADICTS` marks `fact-05-1` `CONFLICTED`; passive/unattributed personal role blocked | `PASS` |
| `SCN-06` | `DEV` | `family-src06` | 3 | 2 | 2 | 4 | Plan/roadmap vs completed guard (`fcand-06-3-rejected` blocked; `claim-06-1` verified; `claim-06-2` blocked) | `PASS` |
| `SCN-07` | `TEST` | `family-src07` | 4 | 3 | 2 | 3 | Rollback/negation guard (`fcand-07-4-rejected` blocked; pre-production revert `claim-07-1` verified; `claim-07-2` blocked) | `PASS` |
| `SCN-08` | `DEV` | `family-src08` | 3 | 2 | 2 | 4 | Team vs individual attribution (`fcand-08-3-rejected` blocked; scoped `claim-08-1` verified; single-handed `claim-08-2` blocked) | `PASS` |
| `SCN-09` | `TEST` | `family-src09` | 2 | 2 | 2 | 3 | Staging vs production scope (`claim-09-1` staging sandbox verified; `claim-09-2` production escalation blocked) | `PASS` |
| `SCN-10` | `DEV` | `family-src10` | 2 | 2 | 1 | 3 | Correlation vs causation (`atom-10-1` `SUPPORTED`; causal `atom-10-2` & ungrounded `atom-10-3` `UNSUPPORTED`, claim blocked) | `PASS` |
| `SCN-11` | `TEST` | `family-src11` | 2 | 2 | 2 | 2 | `ID-04` ambiguous identity (`fact-11-1` deferred as `CANDIDATE`, `claim-11-1` blocked; verified `claim-11-2` verified) | `PASS` |
| `SCN-12` | `DEV` | `family-src12` | 1 | 1 | 4 | 6 | 4-way `ER-012` / `CLAIM-08` matrix via real `PATCH /v1/claims/{id}`: `claim-12-1` failed reverify (`UNSUPPORTED`), `claim-12-2` un-reverified (`STALE`), `claim-12-3` faithful paraphrase (`SOURCE_VERIFIED`), `claim-12-4` mixed `USER_CONFIRMED` (`MIXED`, blocked from `SOURCE_VERIFIED_ONLY` export) | `PASS` |
| `SCN-13` | `TEST` | `family-src13` | 2 | 2 | 2 | 3 | `OD-003` real `deletion-preview` + `deletion-confirm` + transactional M4 cascade purge on `src13a`: `0` retained versions of `claim-13-1` (v1) and mixed-source `claim-13-2` (v1 & v2), `404` post-purge, independent `src13b` & `fact-13-2` preserved `ACTIVE`/`SUPPORTED` | `PASS` |
| `SCN-14` | `TEST` | `family-src14` | 2 | 2 | 1 | 3 | Numeric distortion (`400ms` -> `40ms`) & cross-project entity conflation (`Alpha` vs `Borealis`) rejected (`UNSUPPORTED`) | `PASS` |
| `SCN-15` | `DEV` | `family-src15` | 2 | 2 | 2 | 3 | `CLAIM-05` verified `PERSON` identity + `contributions` (`SOURCE_SUPPORTED`) + `contribution_support` (`claim-15-1` Tech Lead verified; `claim-15-2` ungrounded Security Lead blocked; `TEAM` role `fact-15-2` preserved) | `PASS` |
| `SCN-16` | `TEST` | `family-src16` | 3 | 3 | 2 | 3 | `CLAIM-06` 6-element `METRIC` grounding (`claim-16-1` `920ms -> 210ms / 77.2%` verified; over-generalized `claim-16-2` `company-wide / annual` blocked) | `PASS` |

---

## 4. Official `REF-01..REF-08` Runtime Verification Matrix (`27 / 27`)

| Official Ref | Cases Verified | Product PG18 + FastAPI Verified | External Live Provider Verified | Key Behaviors Verified |
|---|---|---:|---:|---|
| `REF-01` | `refcase-01a` (1/1) | `true` | `false` | Exact revision hash & `cm-locator-v1` span match (`MATCHED`), approval allowed (`200`), zero auto-refresh |
| `REF-02` | `refcase-02a`, `02b`, `02c` (3/3) | `true` | `false` | Body drift (`REVISION_MISMATCH`), new revision isolation (`0` auto-mutation of historical fact/claim), same-hash corrupted span (`INVALID_LOCATOR`) — all block approval |
| `REF-03` | `refcase-03a..03e` (5/5) | `true` | `false` | HTTP `404` (`UNAVAILABLE`, non-retryable), `206` (`PARTIAL_WITH_LIMIT`), `429`/`503`/`TIMEOUT` retry-capped (`UNAVAILABLE`, retryable) — distinct from `SCN-13` deletion and historical falsity |
| `REF-04` | `refcase-04a`, `04b` (2/2) | `true` | `false` | Real canary document `rev-src_ref07_canary-v1-7e7ec30a` (`CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST`) executed through Stage 2A/2B/3A/3B/3C + Approval + Markdown/JSON Export -> **0 hits across 41 PG18 tables, JSONB keys, logs, HTTP metadata, exports, and cache**; negative leak detector verified against `negative_leaky_payload_fixture` |
| `REF-05` | `refcase-05a`, `05b` (2/2) | `true` | `false` | HTTP `403` (`POLICY_BLOCKED`) & all 6 SSRF/non-HTTPS/loopback/metadata/credential/unallowlisted URLs blocked pre-fetch (`fetch_attempted=false`) + HTTP query-param `url=` injection rejected |
| `REF-06` | `refcase-06a`, `06b`, `06c` (3/3) | `true` | `false` | Pre-commit crash rollback (`before_commit_hook`) + clean replay; post-commit idempotent replay (`1` `review_actions` row, `0` duplicates); memory-lost changed-hash replay explicit block (`REVISION_MISMATCH`, `0` raw fallback) |
| `REF-07` | `refcase-07a..07g` (7/7) | `true` | `false` | Same-op refetch on `APPROVAL` (`07a`), zero-quote `EXPORT_DOWNLOAD` (`07b`), `GET /v1/content-units/{id}/content` `200` + `Cache-Control: private, no-store` (`07c`), prior `last_verified_at` without current-op refetch blocked `409 REFETCH_REQUIRED_IN_CURRENT_OPERATION` (`07d`), `EXPORT-02` TOCTOU invalidation via `before_publish_hook` (`INVALIDATED`, `artifact_payload IS NULL`, `07e`), unapproved claim export blocked `409 EXPORT_CLAIM_NOT_APPROVED` (`07f`), version-mismatched export blocked `409 EXPORT_CLAIM_VERSION_MISMATCH` (`07g`) |
| `REF-08` | `refcase-08a..08d` (4/4) | `true` | `false` | Same-op revision dedupe (`2` units -> `1` remote fetch, `08a`), non-raw `JobRefetchManifest` handoff + Job 2 fresh refetch (`08b`), byte budget exceeded -> `PARTIAL_WITH_LIMIT` with `silent_truncation_occurred=false` (`08c`), request/retry limit exceeded -> `PARTIAL_WITH_LIMIT` with `required_evidence_dropped_silently=false` (`08d`) |

---

## 5. Genuine Production Backend & FE↔BE Wire Defects Caught & Fixed

Zero frozen oracle fixtures were modified; every mismatch uncovered during real PG18 + live FE↔BE execution was traced to a real defect in production code and fixed directly:

1. **Pre-Production Rollback False Positive (`claims/generator.py`, `claims/validator.py`)**:
   - `_PRODUCTION_TERM_RE` / `_PRODUCTION_ASSERTION_RE` matched `"before production rollout"` in `SCN-07` `claim-07-1` as if it asserted a production deployment (`INVENTED_ENVIRONMENT_SCOPE` / `STAGING_FACT_CANNOT_SUPPORT_PRODUCTION_CLAIM`).
   - **Fix**: Added `_NON_PRODUCTION_QUALIFIER_RE` to strip explicit non-production / pre-production rollback clauses (`before|prior to|not deployed to|never deployed to|without|preventing|blocked before` + `production|prod|live traffic`) before asserting positive production scope, while keeping positive escalation guards (`SCN-09`) intact.
2. **6-Element Metric Numeric Corpus Omission (`claims/validator.py`)**:
   - `_metric_grounding_matches_fact()` checked `atom_text` numeric tokens without including `fact.object_text` or `fact_metric.source_basis`, falsely flagging `SCN-16` `atom-16-2` (`920ms`, `210ms`, `77.2%` vs `metric_details.value = "920 -> 210 (-77.2%)"`) as `METRIC_NUMERIC_DISTORTION`.
   - **Fix**: Passed `fact_object_text` into `_metric_grounding_matches_fact()` and included `fact_object_text` + `fact_metric.source_basis` in `fact_all_nums`; numeric distortions (`SCN-12` `60%`, `SCN-14` `40ms`) remain strictly blocked.
3. **Team-Scoped Role Fact Identity Gate (`facts/validator.py`, `facts/normalizer.py`)**:
   - `ID-04` required a verified individual `subject_identity_id` for every `FactType.ROLE`, inadvertently keeping `TEAM`-scoped non-owner role facts (`SCN-15` `fact-15-2`) stuck in `CANDIDATE`.
   - **Fix**: Restricted the implicit `ROLE` identity requirement to `(fact_type is FactType.ROLE and semantic.scope is not FactAttributionScope.TEAM)` across both `FactSupportValidator` and `CanonicalFactNormalizer`.
4. **Edited Claim Un-Reverified Atom Initial Status (`claims/postgres_service.py`)**:
   - `atomize_claim()` inserted atoms for edited claim versions (`claim_version > 1`) with status `UNSUPPORTED` instead of `STALE` (`ER-012` / `SCN-12` `claim-12-2`).
   - **Fix**: Set initial atom status on `claim_version > 1` edits to `STALE` until Stage 3C revalidation completes, preserving `UNSUPPORTED` for initial `v1` drafts.
5. **Nested Anchor Locator Dropped in Export Renderer (`exports/renderer.py`)**:
   - Caught by live FE↔BE PG18 verification: `_sanitize_locator()` inspected only top-level keys of `content_units.anchor` instead of unwrapping nested `anchor["locator"]`, causing live Markdown & JSON exports to omit `char_start`, `char_end`, `line_start`, `line_end`, and `span_sha256`.
   - **Fix**: Unwrapped nested `raw_locator["locator"]` cleanly inside `_sanitize_locator()` while preserving `assert_no_forbidden_raw_keys`.
6. **Transactional OD-003 M4 Cascade Cleanup Helper (`claims/deletion_cascade.py`)**:
   - Added `execute_source_deletion_m4_cascade()` operating inside `workspace_scoped_transaction`, enforcing active `deletion_jobs` fence, bidirectional recursive `supersedes_*` chain purging (`0` retained versions of single- and mixed-source claims; `PARTIALLY_GROUNDED` forbidden), `SET LOCAL app.allow_revision_purge = 'on'`, `work_events` primary-anchor re-anchoring to surviving resources, and Plan C redacted audit/outbox emission.
7. **Live FE↔BE Wire Contract Normalization (`spikes/web-import/src/domain/claim-review-models.ts`, `adapters/claim-review-adapter.ts`, `adapters/claim-review-http-transport.ts`, `adapters/mock-claim-review-adapter.ts`)**:
   - Fixed `support_graph_epoch` coercion (`string | number` preserving real backend `sha256:...` strings instead of coercing to `1` and triggering HTTP `422` on `/actions/approve`).
   - Fixed `normalizeLocator` to unwrap nested `anchor.locator` and map backend `line_start`/`line_end`/`char_start`/`char_end`/`locator_spec_version`/`canonical_url`.
   - Fixed `normalizeClaimAtomDetail` (`unsupported_reason`, `role_contribution_grounding`, `metric_grounding`), `normalizeFactSupportItem` / `normalizeClaimFactSupportNode` (`support_metadata.checked_at` / `resolution_status`), `ExportStatus` (`PENDING` / `FAILED`), `normalizeExportSummary` / `normalizeExportDetail` (`claim_versions` -> `claim_ids`, flattening `rendered_json.claims[].atoms[].citations[]`), and `classifyClaimReviewError` (`EXPORT_CLAIM_VERSION_MISMATCH`).

---

## 6. Live FE↔BE PG18 Interoperability Verification

Verified via `spikes/web-import/tests/contract-http/live-backend-claim-review.test.tsx` + `run_live_claim_review_backend.py` over real `127.0.0.1` loopback HTTP against an ephemeral PostgreSQL 18 (`0010..0070`, `cm_api_app` `NOBYPASSRLS` + `FORCE RLS`) FastAPI server:

- `GET /v1/facts` + `GET /v1/facts/{id}/supports` (`private, no-store`, normalized `SpanLocatorV1` coordinates, 0 canary leakage)
- `GET /v1/claims` + `GET /v1/claims/{id}` + `GET /v1/claims/{id}/supports` (lossless SHA-256 `support_graph_epoch`, nested anchor locator unwrap)
- `GET /v1/content-units/{id}/content` Plan C ephemeral viewer `MATCHED` (`200`, `private, no-store`) and `REVISION_MISMATCH` (`409`)
- `POST /v1/claims/{id}/actions/approve` blocked fail-closed (`409 SUPPORT_INCOMPLETE`) on `UNVERIFIED` claim; succeeds (`200`) on `SOURCE_VERIFIED` claim with real SHA-256 `support_graph_epoch`
- `PATCH /v1/claims/{id}` (`ER-012` v1 preserved) -> `POST /v1/claims/{id}/actions/revalidate` -> `POST /v1/claims/{id}/actions/approve` (v2)
- `POST /v1/exports` (`MARKDOWN` + `JSON`, `SOURCE_VERIFIED_ONLY`) -> `GET /v1/exports/{id}` -> `GET /v1/exports` -> `DELETE /v1/exports/{id}` (`contains_raw_quotes = false`, structured citations with `start_line = 1`, `0` canary leakage)

---

## 7. Reproducible Verification Commands

```sh
# 1. T-407A frozen 0.4.0-synthetic-golden zero-drift bundle verification on PG18
cd apps/api && uv run python ../../scripts/run_t407a_claim_eval.py --verify-bundle

# 2. Full backend pytest suite (unit + PG18, 0 failures, 0 skipped)
cd apps/api && uv run pytest -v -m ""

# 3. Full conformance CI with PG18
cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres

# 4. B-004 fault harness (16/16 pass, 0 drift)
cd apps/api && uv run python ../../scripts/run_b004_fault_harness.py

# 5. M3 T-306A event eval regression check (21/21 pass, 0 drift)
cd apps/api && uv run python ../../scripts/run_t306a_event_eval.py --verify-bundle

# 6. Full FE typecheck, unit tests, live PG18 contract-http tests, build, and E2E
cd spikes/web-import && npm run typecheck && npm test && npm run test:contract-http && npm run build && npm run test:e2e
```
