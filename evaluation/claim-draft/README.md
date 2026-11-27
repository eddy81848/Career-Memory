# T-004A → T-004B Fact·Claim Owner-Review Candidate Dataset (`evaluation/claim-draft`)

- **Approved Synthetic Golden Manifest (`T-407A` 단일 진입점)**: [`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`](GOLDEN_APPROVAL_MANIFEST_v0.4.0.json) (`dataset_version: 0.4.0-synthetic-golden`, `T-004B` 프로젝트 오너 승인 기록 — 아래 후보 데이터셋·해시를 수정 없이 그대로 참조)
- **Reviewed Candidate Dataset / Schema Version (원본 보존)**: `0.4.0-owner-review-candidate` (`CANDIDATE_MANIFEST.json` 및 후보 JSON의 `PENDING_OWNER_REVIEW`, `human_approved: false`, `frozen_for_golden: false` 상태와 해시를 그대로 보존)
- **Prior Baseline Preserved in Git**: `0.3.0-synthetic-golden` @ `fa6332d2dda00c59dd922465c1005349017577fe` (이전 `APPROVAL_MANIFEST.json`의 승인 상태·해시 미복사)
- **Candidate Integrity Manifest**: [`CANDIDATE_MANIFEST.json`](CANDIDATE_MANIFEST.json) (`bundle_sha256: sha256:27b6d1eeace3e4ac5771f1abb1555a2af3c6cd9446a9f7f10005404572d7b059`, 파일 SHA-256 `2e9ffde13dfa619cee9035fdf2f9746adfd6b925875a7a955790e210b4a0bcea`)
- **Branch**: `qa/claim-fixtures` (base commit `fa6332d2dda00c59dd922465c1005349017577fe`)

## `T-407A` Single Entry Point & Anti-Double-Counting Contract

- **`T-407A` 소비자는 반드시 [`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`](GOLDEN_APPROVAL_MANIFEST_v0.4.0.json) (`0.4.0-synthetic-golden`)만 단일 진입점으로 읽어야 한다.**
- `0.4.0-synthetic-golden`은 `fa6332d`의 이전 `0.3.0-synthetic-golden` 23개 Claim을 모두 보존한 상태에서 6개 Claim과 15개 재조회 케이스를 추가한 **총 16 시나리오 · 29 Claim · 27 재조회 케이스** 기준본이다.
- 이전 `0.3.0-synthetic-golden` (23건)과 `0.4.0-synthetic-golden` (29건)을 합쳐 중복 집계하거나, 이전 `0.3.0`만 읽어 추가 케이스를 누락해서는 안 된다.
- 동일 원본과 편집 버전은 기존 `SYNTHETIC_REGRESSION_DEV` / `SYNTHETIC_REGRESSION_TEST` 묶음을 그대로 유지한다.

## What Changed in `v0.4.0-owner-review-candidate` (Spec Sync)

1. **Fact 추출(`FactCandidateExtractor`) vs 검증(`FactSupportValidator`) vs 승격 분리 (Stage 2A/2B)**:
   - `stages/stage2_fact_extraction.json`에 `extractor_runs`, `fact_candidates` (34건, `candidate_status: EXTRACTED_UNVALIDATED`), `validator_runs`, `fact_support_validations` (38건), `fact_supports` (35건)을 두어 추출만으로 `SUPPORTED`가 되지 않음을 명시했다.
   - 정정(`SCN-05`)·계획(`SCN-06`)·부정(`SCN-07`)·팀 작업(`SCN-08`)·스테이징(`SCN-09`) 문장을 삭제하지 않고 정확한 의미 그대로 보존하며, 계획→완료(`fcand-06-3-rejected`)·부정 누락(`fcand-07-4-rejected`)·팀→개인(`fcand-08-3-rejected`) 과장 후보는 Stage 2B에서 `REJECT_DO_NOT_PROMOTE`로 차단한다.
2. **Claim 생성(`ClaimGenerator`)·분해(`ClaimAtomizer`)·검증(`ClaimSupportValidator`) 분리 + 실제 근거 체인 연결 (Stage 3A/3B/3C)**:
   - `stages/stage3_claim_assembly.json`에 `confirmed_events` (16건, `fixture_event_sha256`)와 5개 파이프라인 역할 전체에서 전역 유일한 `run_id`를 갖는 `role_execution_runs`를 추가해 동일 AI 응답을 생성 결과와 검증 증거로 재사용할 수 없게 했다 (`ROLE_RUN_ID_REUSE_VIOLATION`).
   - 모든 역할의 `input_version_refs`에서 상태 문자열(`CONFIRMED`/`SUPPORTED`)을 금지(`STATUS_USED_AS_VERSION_IDENTIFIER`)하고 실제 fixture 버전·해시(`fixture_event_sha256`, `fixture_fact_sha256`, Atom 버전·텍스트 해시, `SourceRevision.content_sha256`, `ContentUnit.unit_sha256`)를 연결했다.
   - `Claim.input_fact_ids`, `ClaimAtomSupport`, `ClaimSupportValidator` 입력을 동일 `ConfirmedEvent`로 한정(`CROSS_EVENT_EVIDENCE_MISMATCH`)하고, `SOURCE_VERIFIED=true` 기대값마다 현재 Claim 버전 Atom → Fact → 활성 `DIRECT` `FactSupport` → Revision/Unit hash·locator → 동일 작업 재조회 결과를 `verified_evidence_bindings`로 끝까지 검증한다 (`VALIDATOR_INPUT_EVIDENCE_MISMATCH`).
3. **공식 도메인 값과 테스트 전용 라벨 분리**:
   - 공식 Fact 유형 6종(`PROBLEM | ACTION | TECHNOLOGY | RESULT | ROLE | METRIC`)과 공식 Atom 상태 4종(`SUPPORTED | USER_CONFIRMED | UNSUPPORTED | STALE`)을 최상위 도메인 필드로 분리하고, `DRAFT_*`·`ENGINEERING_DELIVERY` 등 테스트 라벨은 `fixture_classification` / `fixture_test_classification` (`TEST_FIXTURE_ONLY_NOT_DOMAIN_API`)에 격리했다.
   - `SOURCE_VERIFIED`는 모든 필수 Atom이 활성 `SUPPORTED` Fact + 활성 `DIRECT` 근거 + 검증된 근거 바인딩을 갖고 동일 작업 내 hash·locator·권한·의미 재검증이 통과할 때만 계산된다 (`expected_source_verified_computed`). 생성기/사용자의 직접 설정은 차단된다.
4. **필수 보강 케이스 (`CLAIM-05`, `CLAIM-06`, `CLAIM-08`, `REF-04`, `REF-06..08`)**:
   - **`CLAIM-05`** (`SCN-15`): `claim-15-1` (PASS)의 Tech Lead Atom을 `person-alex-kim-verified` + `evt-career-15` + `Tech Lead` + 활성 구체 기여 근거 `fs-15-1b`(RFC 작성 및 스키마 롤아웃 리뷰 승인)에 연결(`FIXTURE_SEMANTICS_FOR_DB_MODEL_3_8_NOT_PRODUCT_TABLE`)하여 작성자 일치·역할 문구만으로는 통과하지 못하게 하고(`ROLE_EVIDENCE_VALIDATION_ERROR`), 기여 근거 누락·타인 기여 변형 실패 테스트와 `claim-15-2` (Security Lead FAIL)를 함께 유지한다.
   - **`CLAIM-06`** (`SCN-16`): 값·단위·30일 기간·Datadog APM 출처·prod checkout-api p99 범위 일치(`claim-16-1` PASS) vs 전사·연간 과대확장(`claim-16-2` FAIL), 공식 `PROBLEM`/`TECHNOLOGY`/`METRIC` 유형 포함.
   - **`CLAIM-08`** (`SCN-12`): 기존 편집 실패(`claim-12-1`)·편집 미재검증 `STALE`(`claim-12-2`)에 더해 편집 후 v2 재검증 통과(`claim-12-3` PASS)와 `USER_CONFIRMED`-only(`claim-12-4` FAIL)를 동일 `family-src12` (`DEV`) 내에 배치.
   - **`REF-04`..`REF-08`**: `refcase-04b` (DB/로그/Job/캐시/Export 5면 정적 canary), `REF-06` (`refcase-06a..06c`), `REF-07` (`refcase-07a..07g` — `07b`에 승인 `claim-02-1` v1 전제 및 검증 메타데이터/원문 인용 비포함, `07f` 재조회 성공·미승인 Export 차단, `07g` 재조회 성공·승인 버전 불일치 v2≠v1 Export 차단 포함, `REF07_EXPORT_PRECONDITION_VIOLATION`), `REF-08` (`refcase-08a..08d`) 등 총 27개 합성 Refetch 케이스를 포함한다.
5. **기존 `v0.3.0` 평가 경계 100% 보존**:
   - 기존 15개 family의 `SYNTHETIC_REGRESSION_DEV` / `SYNTHETIC_REGRESSION_TEST` 배치와 기존 23개 Claim 기대 판정·출처 연결을 100% 유지하고, 신규 독립 family `family-src15` (DEV)와 `family-src16` (TEST)를 추가했다.

## Commands

Run from the worktree root (`/Users/eddy81848/Projects/Career Memory/.worktrees/qa-claim-fixtures`):

```bash
# 1. Programmatically rebuild all 9 candidate files + CANDIDATE_MANIFEST.json (idempotent)
python3 -B evaluation/claim-draft/tools/build_fixtures.py

# 2. Run full structural, schema, round-trip, stage-separation, evidence-linkage, role/export, Option C leak, partition, & ADR-018 candidate validator
python3 -B evaluation/claim-draft/tools/validate_dataset.py

# 3. Validate the separate 0.4.0-synthetic-golden approval manifest against the untouched candidate bundle
python3 -B evaluation/claim-draft/tools/validate_golden_approval.py

# 4. Run candidate (40 tests) + approval manifest (8 tests) regression & semantic fault-injection suites (48 tests total)
python3 -B -m unittest discover -s evaluation/claim-draft/tools -p 'test_*.py' -v

# 5. Verify zero whitespace/EOF errors
git diff --check
```

## Explicitly Not Approved / Not Executed / Outside This Task Scope

- **실제 LLM 품질 평가, 제품 Claim 파이프라인 구현, 물리 DB 스키마·외부 커넥터 연동, 운영 수치 한도, 공용 CI, `EVENT-QA` 수정**: 이번 합성 회귀 기준 승인 범위가 아니며 미실행.
- **`REF-01..REF-08` 실제 런타임/M4 제품 시험**: `product_runtime_verified: false` 전 항목 유지; `REF-04/05` 부분 정적 점검 및 `REF-06..08` 합성 케이스 준비는 실제 제품 시험 통과가 아니며, `RUNTIME Spike` 결과도 M4 제품 시험을 대신하지 않음.
- **후보 JSON 원본 보존**: `CANDIDATE_MANIFEST.json` 및 후보 JSON 파일들은 `PENDING_OWNER_REVIEW` / `human_approved: false` 상태를 그대로 유지하며, 승인 기록은 별도 [`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`](GOLDEN_APPROVAL_MANIFEST_v0.4.0.json)에 분리 보관한다.
- **미확정 API/DB 세부사항 (`확인 필요`, 미고정)**: 운영 정규화/offset 규약, 비-`SUPPORTED` Fact·비-`SOURCE_VERIFIED` Claim 제품 enum명, DB Model §3.8 역할·기여 제품 테이블/컬럼명, `FactCandidate` 완료/불확실성 DB 컬럼명, `REF-08` 운영 수치 한도, `REF-07` Export 검증 메타데이터의 파일 헤더·API 필드 배치 규약 — 상세 검토 큐는 [`COVERAGE.md`](COVERAGE.md) 참조.
