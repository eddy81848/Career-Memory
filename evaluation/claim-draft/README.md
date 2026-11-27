# T-004A/B Fact·Claim Synthetic Regression Golden Dataset (`evaluation/claim-draft`)

- **Dataset Version**: `0.3.0-synthetic-golden` (approved from `0.3.0-draft` @ `570067488a2d8d5b2fe249653336ec60af4a45c8`)
- **Schema Version**: `0.3.0-synthetic-golden`
- **Approval Status**: `APPROVED_SYNTHETIC_REGRESSION_GOLDEN` (`oracle_status: "APPROVED_SYNTHETIC_REGRESSION"`, `frozen_for_golden: true`)
- **Golden Scope**: `SYNTHETIC_REGRESSION_ONLY_NO_LIVE_LLM_NO_PROD_INTEGRATION`
- **Provisional Verdict Namespace**: `DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS` (`DRAFT_CLAIM_*`, `DRAFT_ATOM_*`, `DRAFT_REFETCH_*` — **strictly separated** from unconfirmed product state enums)
- **Approval Manifest**: [`APPROVAL_MANIFEST.json`](APPROVAL_MANIFEST.json) (`bundle_sha256: sha256:881b6bb182b65c51c88a09ecf605322167ff439a4fffc37c1d77f07f1ad6b51f`)
- **Branch**: `qa/claim-fixtures` (continued from `570067488a2d8d5b2fe249653336ec60af4a45c8`)

## Approval Scope & Strict Boundary

This directory contains the **approved synthetic regression test ground truth** and programmatic validation tools for the Career Memory Grounded Claim Pipeline:

`Source Revision → Content Unit → Fact Support → Canonical Fact → Claim Atom Support → Claim Atom → Claim` + `Refetch Verification (Official REF-01..REF-08)`

- **Approved for synthetic regression tests ONLY**: covers all 18 synthetic `SourceRevision` fixtures, 14 Fact·Claim scenarios (`SCN-01`..`SCN-14`), 23 Claims (11 Round-01 revisions + 12 kept Claims including `SCN-13` **OD-003**), 41 Claim Atoms, 26 Canonical Facts, 29 Fact Supports, and 12 static synthetic refetch fixtures (`refcase-01a`..`refcase-05b`).
- **Explicitly OUT of approval scope**:
  - **No live LLM evaluation** (`live_llm_evaluation_in_scope: false`): zero external LLM inference, prompt tuning, or cost/latency benchmarking.
  - **No production / external connector integration** (`production_connector_integration_in_scope: false`, `synthetic_semantics_only_not_connector_evidence: true`): fixtures do NOT prove real GitHub/Notion/Slack/Jira connectors, live OAuth/tenant isolation, live HTTP refetch/retry-backoff, or production DB/product code behavior.
  - **Unverified REF items are NOT passed**: `REF-04` and `REF-05` remain `PARTIAL_COVERAGE_GAP` (`verified_in_draft: false`, `oracle_status: PARTIAL_UNVERIFIED_COVERAGE_GAP`), and `REF-06`, `REF-07`, `REF-08` remain `COVERAGE_GAP` (`verified_in_draft: false`, `oracle_status: UNVERIFIED_COVERAGE_GAP`).
- **Option C (방안 C) non-persistence**: artificial raw texts exist **only** inside `sources/` as test fixtures. Downstream stage files (`stages/stage1..stage4`) and `APPROVAL_MANIFEST.json` enforce `persists_raw_text: false` and canary-leak auditing.

## Family-Preserving Evaluation Partitions (Anti-Leakage)

Every case, edited Claim version (`v1 → v2`), and Refetch fixture derived from the same raw source (`evaluation_family_id` / `source_revision_id`) is locked into the exact same partition, enforced programmatically by `validate_dataset.py` (`FAMILY_PARTITION_LEAKAGE`):

- **`SYNTHETIC_REGRESSION_DEV`** (8 families · 7 scenarios · 11 claims · 1 refetch fixture): `family-src01`, `src03`, `src04`, `src06`, `src08`, `src10`, `src12` (original `src12` + user-edited `claim-12-1` v2 & `claim-12-2` v2 together), `src_ref07_canary` (`refcase-04a`).
- **`SYNTHETIC_REGRESSION_TEST`** (7 families · 7 scenarios · 12 claims · 11 refetch fixtures): `family-src02` (`SCN-02` + all 11 `src02`-derived refetch fixtures `refcase-01a`..`05b` together), `src05`, `src07`, `src09`, `src11`, `src13` (`src13a`+`src13b` + edited `claim-13-2` v2 together), `src14`.

## Official `REF-01..REF-08` Alignment & Unverified Coverage Gaps

Unverified requirements are explicitly NOT marked passed:

| Official ID | 공식 요구사항 | Coverage Status | Approval / Verified Flag | Linked Fixtures | Boundary / Coverage Gap Note |
|---|---|---|---|---|---|
| `REF-01` | 동일 버전과 정확한 근거 위치 재현 | `COVERED_SYNTHETIC_STUB` | `APPROVED_SYNTHETIC_REGRESSION` (`true`, stub only) | `refcase-01a` | Static stub & round-trip only; live external provider refetch 미실행 |
| `REF-02` | hash/locator 불일치 차단 | `COVERED_SYNTHETIC_STUB` | `APPROVED_SYNTHETIC_REGRESSION` (`true`, stub only) | `refcase-02a`, `02b`, `02c` | Body drift (`02a`), body append (`02b`), and same-hash locator-only mismatch (`02c`) separated; approval blocked & Post-MVP auto-mutation disabled |
| `REF-03` | 재조회 불가와 삭제·거짓 구분 | `COVERED_SYNTHETIC_STUB` | `APPROVED_SYNTHETIC_REGRESSION` (`true`, stub only) | `refcase-03a` (`404`), `03b` (`206`), `03c` (`429`), `03d` (`503`), `03e` (`timeout`) | `404`/`206` vs transient `429`/`5xx`/`timeout` (bounded retry stub max=3, approval blocked) distinguished from `SCN-13` OD-003 deletion and historical falsity; **live retry/backoff harness 미실행** |
| `REF-04` | 원문 비영속 | **`PARTIAL_COVERAGE_GAP`** | **`PARTIAL_UNVERIFIED_COVERAGE_GAP` (`false`, NOT passed)** | `refcase-04a` | Static JSON canary verified; **coverage gap**: live DB/logs/queues/caches/disk runtime audit |
| `REF-05` | 접근 권한·허용된 URL 경계 | **`PARTIAL_COVERAGE_GAP`** | **`PARTIAL_UNVERIFIED_COVERAGE_GAP` (`false`, NOT passed)** | `refcase-05a`, `refcase-05b` | Static HTTP 403 & URL pre-check verified; **coverage gap**: live OAuth/tenant, 30x redirect SSRF, DNS-rebinding |
| `REF-06` | 중단·재실행 시 원문 저장에 의존하지 않음 | **`COVERAGE_GAP`** | **`UNVERIFIED_COVERAGE_GAP` (`false`, NOT passed)** | *(none — unverified)* | **coverage gap**: worker crash/checkpoint/replay integration test required |
| `REF-07` | 승인·Export·근거 열람 시 재조회 | **`COVERAGE_GAP`** | **`UNVERIFIED_COVERAGE_GAP` (`false`, NOT passed)** | *(none — unverified)* | **coverage gap**: product Approval / Export / Evidence-Viewer E2E trigger required |
| `REF-08` | 비용·크기 한도와 단계 간 메모리 전용 처리 | **`COVERAGE_GAP`** | **`UNVERIFIED_COVERAGE_GAP` (`false`, NOT passed)** | *(none — unverified)* | **coverage gap**: runtime byte/token/cost limit & in-memory inter-stage transport test required |

> Note: Reusable evidence from separate `RUNTIME` verification tracks does **not** close `REF-04..REF-08` as full product verification.

## Key Guards Enforced by Validator (`validate_dataset.py`)

1. **Synthetic-Only Approval Scope & Tamper Hash Guard**: Verifies `oracle_status == "APPROVED_SYNTHETIC_REGRESSION"`, `live_llm_evaluation_in_scope == false`, `production_connector_integration_in_scope == false`, and exact UTF-8 SHA-256 digests of all 9 files + `bundle_sha256` in `APPROVAL_MANIFEST.json`.
2. **Family & Source-Revision Anti-Leakage Guard**: Rejects any split of an `evaluation_family_id` or `source_revision_id` across `SYNTHETIC_REGRESSION_DEV` and `SYNTHETIC_REGRESSION_TEST` (`FAMILY_PARTITION_LEAKAGE`).
3. **Unverified REF Non-Pass Guard**: Rejects marking `REF-04..REF-05` (`PARTIAL_COVERAGE_GAP`) or `REF-06..REF-08` (`COVERAGE_GAP`) as `verified_in_draft: true` or `APPROVED_SYNTHETIC_REGRESSION` (`FALSE_COVERAGE_CLAIM`).
4. **Provisional Verdict Separation**: Enforces `DRAFT_CLAIM_*`, `DRAFT_ATOM_*`, and `DRAFT_REFETCH_*` namespace (`verdict_namespace: "DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS"`) while product status enum mapping remains `REVIEW_REQUIRED`.
5. **User Edit Re-Verification (`SCN-12`), Strict `SOURCE_VERIFIED`, OD-003 User Deletion (`SCN-13`), Same-Hash Locator-Only (`REF-02`), & Transient Retry (`REF-03`)**: All prior structural & semantic regression guards remain active and verified by 30 unit/fault-injection tests.

## Fixture-Only Normalization & Offset Assumptions (`TEST_FIXTURE_ONLY`, `REVIEW_REQUIRED`)

1. **Text Normalization (`fixture-nfc-lf-v0-draft`, `REVIEW_REQUIRED`)**: CRLF/CR → LF, Unicode NFC, internal whitespace preserved verbatim.
2. **Offsets (`fixture-locator-v0-draft`, `REVIEW_REQUIRED`)**: Half-open `[start, end)` computed programmatically in `char` (Unicode codepoint), `utf8_byte`, `utf16` (code unit), and 1-based `line`.
3. **Hashes & Deterministic IDs (`REVIEW_REQUIRED`)**: `sha256:<64-hex>` over UTF-8 bytes of normalized text/span.

## Commands

Run from the worktree root:

```bash
# 1. Programmatically rebuild all fixtures + APPROVAL_MANIFEST.json (idempotent)
PYTHONDONTWRITEBYTECODE=1 python3 evaluation/claim-draft/tools/build_fixtures.py

# 2. Run full structural, schema, locator/hash round-trip, partition anti-leakage, manifest hash, coverage-gap, & canary validator
PYTHONDONTWRITEBYTECODE=1 python3 evaluation/claim-draft/tools/validate_dataset.py

# 3. Run unit + regression + deliberate fault-injection test suite (30 tests)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evaluation/claim-draft/tools -p "test_*.py" -v
```

## Explicitly Not Executed / Outside Approval Scope

- Real external provider HTTP refetch / live retry-backoff / live OAuth / live redirect & DNS-rebinding SSRF tests
- External LLM inference quality evaluation, prompt tuning, or token/cost benchmarking
- Worker crash/checkpoint/resume (`REF-06`), Approval/Export/Evidence-Viewer E2E (`REF-07`), or runtime size/cost/memory-only inter-stage tests (`REF-08`); reusable `RUNTIME` evidence is NOT treated as full product verification
- Production code integration, shared CI wiring, EVENT-QA edits, or finalized product enum / normalization sign-off (`REVIEW_REQUIRED`)
