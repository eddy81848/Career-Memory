# T-004A Fact·Claim Synthetic Dataset Draft (`evaluation/claim-draft`)

- **Dataset Version**: `0.2.0-draft`
- **Schema Version**: `0.2.0-draft`
- **Oracle Status**: `DRAFT` (`frozen_for_golden: false`, pending human review in **T-004B**)
- **Provisional Verdict Namespace**: `DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS` (`DRAFT_CLAIM_*`, `DRAFT_ATOM_*`, `DRAFT_REFETCH_*` — **strictly separated** from finalized product state enums)
- **Branch**: `qa/claim-fixtures` (continued from `e9404841747b3ff9f5e12b8076ad0ff3d08c7202`)

## Purpose & Boundary

This directory contains **100% synthetic** fixtures and programmatic validation tools for the Career Memory Grounded Claim Pipeline:

`Source Revision → Content Unit → Fact Support → Canonical Fact → Claim Atom Support → Claim Atom → Claim` + `Refetch Verification (Official REF-01..REF-08)`

- Zero external LLMs, zero real user documents, zero real URLs/OAuth tokens/API keys, and zero production DB or product code modifications.
- All 18 synthetic `SourceRevision` fixtures, 14 Fact·Claim scenarios (`SCN-01`..`SCN-14`), and 8 refetch fixtures from `b816d01` / `e940484` are preserved (with `SCN-13` aligned to **OD-003**).
- Artificial raw texts exist **only** inside `sources/` as test fixtures. Downstream stage records (`stages/stage1..stage4`) enforce Option C (방안 C) non-persistence (`persists_raw_text: false`) and store only hashes, offsets, locators, and derived structured statements.
- **Structural validation ≠ semantic approval**: passing `validate_dataset.py` certifies schema compliance, locator/hash round-trip integrity, official `REF-01..08` coverage-gap honesty, user-edit new-version re-verification guards, OD-003 user-deletion access-block & full-purge guards, required-atom `DIRECT` support guards, and canary non-persistence — NOT T-004B golden sign-off.

## Official `REF-01..REF-08` Alignment & Honest Coverage Gaps

Missing verifications are explicitly flagged as `COVERAGE_GAP` (or `PARTIAL_COVERAGE_GAP`) rather than falsely marked verified by renumbering:

| Official ID | 공식 요구사항 | Draft Coverage Status | Linked Preserved Fixtures | Coverage Gap / Boundary Note |
|---|---|---|---|---|
| `REF-01` | 동일 버전과 정확한 근거 위치 재현 | `COVERED_SYNTHETIC_STUB` | `refcase-01a` (old `REF-01`) | Static stub & round-trip only; live external provider refetch 미실행 |
| `REF-02` | hash/locator 불일치 차단 | `COVERED_SYNTHETIC_STUB` | `refcase-02a` (old `REF-02`), `refcase-02b` (old `REF-08`) | Drift & appended-revision hash mismatch blocked; Post-MVP auto-detect/refresh disabled |
| `REF-03` | 재조회 불가와 삭제·거짓 구분 | `COVERED_SYNTHETIC_STUB` | `refcase-03a` (old `REF-03`), `refcase-03b` (old `REF-04`) | HTTP 404 & HTTP 206 distinguished from confirmed user deletion (`SCN-13` OD-003) and historical falsity |
| `REF-04` | 원문 비영속 | `PARTIAL_COVERAGE_GAP` | `refcase-04a` (old `REF-07`) | Static JSON canary verified; **coverage gap**: live DB/logs/queues/caches/disk runtime audit |
| `REF-05` | 접근 권한·허용된 URL 경계 | `PARTIAL_COVERAGE_GAP` | `refcase-05a` (old `REF-05`), `refcase-05b` (old `REF-06`) | Static HTTP 403 & URL pre-check verified; **coverage gap**: live OAuth/tenant, 30x redirect SSRF, DNS-rebinding |
| `REF-06` | 중단·재실행 시 원문 저장에 의존하지 않음 | **`COVERAGE_GAP`** | *(none — unverified)* | **coverage gap**: worker crash/checkpoint/replay integration test required |
| `REF-07` | 승인·Export·근거 열람 시 재조회 | **`COVERAGE_GAP`** | *(none — unverified)* | **coverage gap**: product Approval / Export / Evidence-Viewer E2E trigger required |
| `REF-08` | 비용·크기 한도와 단계 간 메모리 전용 처리 | **`COVERAGE_GAP`** | *(none — unverified)* | **coverage gap**: runtime byte/token/cost limit & in-memory inter-stage transport test required |

> Note: Reusable evidence from separate `RUNTIME` verification tracks does **not** close `REF-06..REF-08` as full product verification.

## Key Grounding & Versioning Guards Enforced by Validator

1. **Provisional Verdict Separation**: `expected/oracle_draft.json` and `expected/refetch_oracle_draft.json` use provisional `DRAFT_CLAIM_*`, `DRAFT_ATOM_*`, and `DRAFT_REFETCH_*` labels (`verdict_namespace: "DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS"`). Final product status mapping remains `REVIEW_REQUIRED`.
2. **User Edit Re-Verification on New Version (`SCN-12`)**: Editing a claim increments `claim_version` (`v1 → v2`). Prior `v1` support (`evaluated_on_claim_version: 1`) cannot automatically verify `v2`; `v2` must be re-verified against evidence (`claim-12-1`: v2 re-verification failed `CONTRADICTS`; `claim-12-2`: v2 pending re-verification).
3. **Strict `SOURCE_VERIFIED` Guard**: Any claim where a required atom (`is_required_atom: true`) has **only `CONTEXT` support** or **0 active `DIRECT` supports** (or `CONTRADICTS`, ambiguous identity, deleted source/fact, or un-reverified user edit) is programmatically forbidden from `source_verified_eligible: true` or `SOURCE_VERIFIED`.
4. **OD-003 User Deletion Access Block & Full Server Purge (`SCN-13`)**: When a user confirms Source deletion after a deletion preview, every dependent Claim—including mixed-source Claims (`claim-13-2` referencing deleted `src13a` + surviving `src13b`)—must have **immediate access blocked**, followed by **full purge of the Claim and all server-side edited, approved, and versioned copies** (`retained_claim_versions: []`, `retained_edited_copies: 0`, `retained_approved_copies: 0`). Independent non-deleted Source records (`src13b` / `fact-13-2`) are preserved. `DRAFT_CLAIM_PARTIALLY_GROUNDED` is programmatically forbidden as a post-deletion state, and OD-003 deletion is strictly distinguished from simple refetch failure (`REF-03`).

## Fixture-Only Normalization & Offset Assumptions (`TEST_FIXTURE_ONLY`, `REVIEW_REQUIRED`)

All normalization and offset conventions remain **unconfirmed** for production and are explicitly scoped `TEST_FIXTURE_ONLY` (`REVIEW_REQUIRED`):

1. **Text Normalization (`fixture-nfc-lf-v0-draft`, `REVIEW_REQUIRED`)**: CRLF/CR → LF, Unicode NFC, internal whitespace preserved verbatim.
2. **Offsets (`fixture-locator-v0-draft`, `REVIEW_REQUIRED`)**: Half-open `[start, end)` computed programmatically in `char` (Unicode codepoint), `utf8_byte`, `utf16` (code unit), and 1-based `line`.
3. **Hashes & Deterministic IDs (`REVIEW_REQUIRED`)**: `sha256:<64-hex>` over UTF-8 bytes of normalized text/span.

## Commands

Run from the worktree root:

```bash
# 1. Programmatically rebuild all fixtures (idempotent)
PYTHONDONTWRITEBYTECODE=1 python3 evaluation/claim-draft/tools/build_fixtures.py

# 2. Run full structural, schema, locator/hash round-trip, coverage-gap, canary, and policy validator
PYTHONDONTWRITEBYTECODE=1 python3 evaluation/claim-draft/tools/validate_dataset.py

# 3. Run unit + regression + deliberate fault-injection test suite (24 tests)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evaluation/claim-draft/tools -p "test_*.py" -v
```

## Explicitly Not Executed

- Real external provider HTTP refetch / live OAuth / live redirect & DNS-rebinding SSRF tests
- External LLM inference quality evaluation, prompt tuning, or token/cost benchmarking
- Worker crash/checkpoint/resume (`REF-06`), Approval/Export/Evidence-Viewer E2E (`REF-07`), or runtime size/cost/memory-only inter-stage tests (`REF-08`); reusable `RUNTIME` evidence is NOT treated as full product verification
- T-004B human semantic sign-off and golden dataset freeze (`oracle_status` remains `DRAFT`)
