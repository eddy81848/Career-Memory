# T-004A → T-004B Synthetic Golden Baseline & Candidate Ledger (`0.4.0-synthetic-golden` / `0.4.0-owner-review-candidate`)

- **Approved Synthetic Golden Version (`T-407A` 단일 진입점)**: `0.4.0-synthetic-golden` → [`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`](GOLDEN_APPROVAL_MANIFEST_v0.4.0.json) *(프로젝트 오너가 16 시나리오 · 29 Claim · 27 재조회 케이스의 합성 입력과 논리적 기대값을 회귀 기준으로 승인; 실제 LLM·제품 파이프라인·DB/외부 커넥터·운영 한도·실제 `REF-01..08` 시험 통과는 제외)*
- **Reviewed Candidate Package (원본 불변 보존)**: `0.4.0-owner-review-candidate` (`PENDING_OWNER_REVIEW`, `human_approved: false`, `frozen_for_golden: false`, `adr_018_ai_cannot_self_approve: true` 유지; 후보 JSON 일괄 수정 및 자기 승인 차단 완화 금지)
- **Prior Baseline Preserved in Git**: `0.3.0-synthetic-golden` @ `fa6332d2dda00c59dd922465c1005349017577fe` (이전 `APPROVAL_MANIFEST.json`의 승인 상태 및 해시는 새 버전에 복사하지 않으며, `T-407A`는 이전 23건과 현재 29건을 합쳐 중복 집계하지 않음)
- **Candidate Integrity Manifest**: [`CANDIDATE_MANIFEST.json`](CANDIDATE_MANIFEST.json) · `bundle_sha256`: `sha256:27b6d1eeace3e4ac5771f1abb1555a2af3c6cd9446a9f7f10005404572d7b059` · 파일 SHA-256: `sha256:2e9ffde13dfa619cee9035fdf2f9746adfd6b925875a7a955790e210b4a0bcea` *(검토 후보 무결성 식별용 — 원본 보존)*
- **Spec Basis Disclosure**: 직접 Notion 접근 불가 환경에서 오케스트레이터가 전달한 B-002 / Grounded Claim Pipeline / DB Model (§3.7–§3.9) / 방안 C / T-004A·T-004B / `REF-06..08` / `CLAIM-05·06·08` / ADR-018 기준을 적용함. 명세에 없는 API/DB 세부 enum·컬럼명·파일 헤더·한도 수치는 추측 확정하지 않고 `확인 필요 (REVIEW_REQUIRED)`로 유지함.
- **Totals**: **20 Source Revisions** · **30 Content Units** · **34 Fact Candidates** (3 rejected at Stage 2B) · **38 Fact Support Validations** · **31 Canonical Facts** (6/6 공식 `fact_type`) · **35 Fact Supports** (`fs-15-1b` 구체 기여 근거 포함) · **16 Confirmed Events** · **118 Distinct Role Execution Runs** · **16 Scenarios** · **29 Claims** · **50 Claim Atoms** (4/4 공식 Atom 상태) · **27 Synthetic Refetch Fixtures** (`REF-01..REF-08` 전원 합성 케이스 준비 / **`product_runtime_verified: false` 전원 미실행**)

---

## 1. 공식 도메인 값 vs 테스트 전용 라벨(`TEST_FIXTURE_ONLY`) vs `확인 필요` 분류

| Artifact / Entity | 공식 도메인 필드·값 (Spec-Aligned) | 테스트 전용 라벨 (별도 격리 필드, API/DB 아님) | 미확정 / `확인 필요 (REVIEW_REQUIRED)` |
|---|---|---|---|
| `SourceRevision` / `Locator` | `source_revision_id`, `content_sha256`, `deleted_by_user` | `raw_text_fixture_only`, `is_synthetic_fixture: true`, `locator_spec_version: fixture-locator-v0-draft`, `normalization_policy: fixture-nfc-lf-v0-draft`, `convention_scope: TEST_FIXTURE_ONLY` | 운영 정규화(CRLF/NFC/공백) 및 offset 기준 단위(char vs utf8_byte vs utf16) |
| Stage 2A `FactCandidate` | `source_revision_id`, `content_unit_id`, `locator`, `fact_type` (`PROBLEM \| ACTION \| TECHNOLOGY \| RESULT \| ROLE \| METRIC`), `extractor_run_id`, `extractor_version`, `candidate_status: EXTRACTED_UNVALIDATED` | `execution_mode: SYNTHETIC_STUB_OFFLINE` | `completion_state`, `uncertainty_level`의 정확한 DB 컬럼명·enum |
| Stage 2B `FactSupportValidation` & `FactSupport` | `validator_run_id` (Stage 2A와 분리), `source_revision_id`, `content_unit_id`, `verified_locator`, `support_role` (`DIRECT \| CONTEXT \| CONTRADICTS`), `promotion_decision` | `validation_id`, `stub-fact-validator-v0.4` | 승격 거부 사유 코드 및 재추출 정책 |
| Stage 2 `CanonicalFact` | `fact_type` (`PROBLEM \| ACTION \| TECHNOLOGY \| RESULT \| ROLE \| METRIC`), `fact_support_status: SUPPORTED` (활성 시), `is_active_supported_fact`, `metric_details` | `fixture_classification.label_scope: TEST_FIXTURE_ONLY_NOT_DOMAIN_API`, `fixture_classification.fixture_fact_sha256`, `fixture_classification.legacy_test_category`, `role_attribution_grounding` (`FIXTURE_SEMANTICS_FOR_DB_MODEL_3_8_NOT_PRODUCT_TABLE`) | 비-`SUPPORTED` Fact 최종 제품 enum명, DB Model §3.8 제품 역할·기여 테이블/컬럼명 |
| Stage 3A/3B/3C `Claim` / `ClaimAtom` / `ClaimAtomSupport` | `confirmed_event_id` (`CONFIRMED`), 동일 Event 한정 `input_fact_ids`, `generator_initial_status: DRAFT`, `claim_version`, `evidence_source_kind: CANONICAL_FACT`, `support_role` (`DIRECT \| CONTEXT \| CONTRADICTS`), 역할별 분리 `run_id`, 실제 버전/해시 `input_version_refs` 및 `verified_evidence_bindings` | `fixture_event_sha256`, `fixture_classification.test_atom_kind`, `generation_fixture_origin`, `hard_negative_type` | 비-`SOURCE_VERIFIED` Claim 제품 상태 enum명, 제품 버전/해시 DB 컬럼명·포맷 |
| `expected/oracle_draft.json` | `expected_domain_atom_status` (`SUPPORTED \| USER_CONFIRMED \| UNSUPPORTED \| STALE`), `expected_source_verified_computed` (계산값; 생성기/사용자 직접 설정 금지) | `fixture_test_classification.label_scope: TEST_FIXTURE_ONLY_NOT_DOMAIN_API`, `fixture_test_classification.draft_atom_verdict`, `fixture_test_classification.draft_claim_verdict`, `product_status_proposal` | 비-`SOURCE_VERIFIED` Claim 최종 제품 상태 매핑 |
| Stage 4 & `expected/refetch_oracle_draft.json` | `official_ref_id` (`REF-01..REF-08`), `product_runtime_verified: false`, `export_precondition_input` (승인 Claim ID·정확한 버전), `checked_at`·해시·locator 검증 메타데이터, `export_excludes_raw_source_quotes: true` | `fixture_test_classification.draft_refetch_verdict`, `metadata_layout_convention_scope: TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN`, `limits_convention_scope: TEST_FIXTURE_ONLY_NUMERIC_THRESHOLDS_REVIEW_REQUIRED` | `REF-08` 운영 수치 한도(요청/바이트/토큰/재시도/동시성), `REF-07` Export 파일 헤더/API 필드 배치 규약, HTTP 410 vs 404 세부 코드 |

---

## 2. 평가 묶음 (기존 `v0.3.0` DEV/TEST 경계 100% 보존, 교차 누수 0건)

| Evaluation Partition | Families (기존 보존 + 신규) | Scenarios | Claims (편집본 포함) | Refetch Fixtures |
|---|---|---|---:|---:|
| `SYNTHETIC_REGRESSION_DEV` | 기존 8종 보존 (`family-src01`, `03`, `04`, `06`, `08`, `10`, `12`, `src_ref07_canary`) + **신규 `family-src15`** (**9**) | `SCN-01`, `03`, `04`, `06`, `08`, `10`, `12`, **`15`** (**8**) | **15** (`claim-12-1..4` 전부 `family-src12` 동시 배치; `claim-15-1..2` `family-src15`) | **2** (`refcase-04a`, **`refcase-04b`**) |
| `SYNTHETIC_REGRESSION_TEST` | 기존 7종 보존 (`family-src02`, `05`, `07`, `09`, `11`, `13`, `14`) + **신규 `family-src16`** (**8**) | `SCN-02`, `05`, `07`, `09`, `11`, `13`, `14`, **`16`** (**8**) | **14** (`claim-16-1..2` `family-src16`) | **25** (`src02` 파생 `refcase-01a..03e`, `05a..05b`, **`06a..06c`**, **`07a..07g`**, **`08a..08d`** 전원 `family-src02`) |

---

## 3. 오너 검토 큐 — Fact·Claim 시나리오 (`Case ID → 합성 원문 위치 → 추출/검증 기대값 → Notion Test ID → 검토할 쟁점`)

기존 `v0.3.0-synthetic-golden`의 23개 Claim 기대 판정은 약화·변경 없이 모두 보존되었으며, Stage 2A/2B 추출·검증·승격 분리, Stage 3A/3B/3C 역할별 실행 ID 분리, 공식 `fact_type`·Atom 상태, 그리고 **`CLAIM-05` (`SCN-15`)·`CLAIM-06` (`SCN-16`)·`CLAIM-08` (`claim-12-3`, `claim-12-4`)** 필수 케이스가 보강되었다.

| Case ID (Partition) | 합성 원문 위치 (`SourceRevision@char_start:char_end`) | 추출/검증 기대값 (Stage 2A/2B → Stage 3C / Computed `SOURCE_VERIFIED`) | Notion Test ID | 검토할 쟁점 |
|---|---|---|---|---|
| `SCN-01` / `claim-01-1` (`DEV`) | `rev-src01-v1-a43eb41e` (`cu-01-0`, boilerplate) | Stage 2A: **0 candidates** → 0 facts; `atom-01-1`: **`UNSUPPORTED`**; `SOURCE_VERIFIED=false` | `T-004A` | 비업무 잡담/아젠다 미정 메모의 0-candidate 기준 확인 |
| `SCN-02` / `claim-02-1` (`TEST`) | `rev-src02-v1-2cb25379@89:271` | Stage 2A→2B: `fcand-02-1` (`ACTION`, DIRECT) → `fact-02-1` (`SUPPORTED`); `atom-02-1`+`atom-02-2`: **`SUPPORTED`**; `SOURCE_VERIFIED=true` *(same-job refetch 시)* | `T-004A` | 구현 + 조건부 효과 2개 Atom 모두 `SUPPORTED` 기준 및 `ACTION` 분류 확인 |
| `SCN-03` / `claim-03-1` (`DEV`) | `rev-src03-v1-6589ff1a@63:131`, `@135:181`, `@185:231` | `fact-03-1` (`METRIC`), `03-2` (`ACTION`), `03-3` (`ACTION`) 전부 `SUPPORTED`; `atom-03-1..4` 4/4 **`SUPPORTED`**; `SOURCE_VERIFIED=true` | `T-004A`, `CLAIM-06` | 한국어 3-bullet → `METRIC`/`ACTION` 매핑 및 4-atom 집계 확인 |
| `SCN-04` / `claim-04-1`, `04-2` (`DEV`) | `rev-src04-v1-0dfa04ee@171:268` (DIRECT), `@44:144` (CONTEXT/`PROBLEM`) | `fact-04-1` (`ACTION`, DIRECT+CONTEXT), `fact-04-2` (`PROBLEM`, PLANNED); `claim-04-1`: `atom-04-1`=`SUPPORTED` (`SOURCE_VERIFIED=true`); `claim-04-2`: `atom-04-2`=`UNSUPPORTED` (`SOURCE_VERIFIED=false`) | `T-004A` | `CONTEXT`-only가 항상 `UNSUPPORTED`로 귀결되는지 확인 |
| `SCN-05` / `claim-05-1`, `05-2` (`TEST`) **[정정 대표]** | `rev-src05a-v1-898e609e@40:114` (초안) + `rev-src05b-v2-0539c693@58:159`, `@83:203` (정정) | Stage 2B: `fact-05-1` (`RESULT`, DIRECT+CONTRADICTS → **비활성 충돌 기록**), `fact-05-2` (`PROBLEM`, DIRECT → **`SUPPORTED`** — 정정 문장 삭제 안 함); `claim-05-1`: `atom-05-1/2`=`UNSUPPORTED` (`false`); `claim-05-2`: `atom-05-3`=`SUPPORTED`, `atom-05-4`(개인 감사)=`UNSUPPORTED` (`false`) | `T-004A`, `CLAIM-05` | 정정 문장이 `fact-05-1`을 반박하고 `fact-05-2`를 직접 뒷받침하는 구조 및 수동태 로그 감사 귀속 분리 확인 |
| `SCN-06` / `claim-06-1`, `06-2` + `fcand-06-3-rejected` (`DEV`) **[계획 대표]** | `rev-src06-v1-11685e46@38:79` (완료), `@87:147` (계획) | Stage 2B: `fact-06-1` (`ACTION`, COMPLETED, `SUPPORTED`), `fact-06-2` (`ACTION`, PLANNED, `SUPPORTED` — 계획 보존), **`fcand-06-3-rejected` (계획→완료 과장 후보) `CONTRADICTS`→`REJECT_DO_NOT_PROMOTE`**; `claim-06-1`: `SOURCE_VERIFIED=true`; `claim-06-2`: `atom-06-3/4`=`UNSUPPORTED` (`false`) | `T-004A` | 계획 문장의 비삭제 보존 및 Stage 2B에서 계획→완료 승격 거부 기대값 확인 |
| `SCN-07` / `claim-07-1`, `07-2` + `fcand-07-4-rejected` (`TEST`) **[부정 대표]** | `rev-src07-v1-84ada707@113:195` (롤백), `@58:111` (미배포), `@200:255` (미달성) | Stage 2B: `fact-07-1` (`ACTION`, COMPLETED), `fact-07-2` (`ACTION`, NEGATED), `fact-07-3` (`RESULT`, NEGATED) 보존 + **`fcand-07-4-rejected` (부정 누락 후보) `REJECT_DO_NOT_PROMOTE`**; `claim-07-1`: `true`; `claim-07-2`: `atom-07-2/3`=`UNSUPPORTED` (`false`) | `T-004A` | 부정 문장이 부정 사실 자체를 `DIRECT` 지지해 긍정 왜곡을 `CONTRADICTS` 하는 구조 확인 |
| `SCN-08` / `claim-08-1`, `08-2` + `fcand-08-3-rejected` (`DEV`) **[팀/개인 대표]** | `rev-src08-v1-699e83e2@31:169` (팀 6명), `@190:254` (개인 캐시) | Stage 2B: `fact-08-1` (`RESULT`, `TEAM`), `fact-08-2` (`ACTION`, `INDIVIDUAL`) + **`fcand-08-3-rejected` (팀→개인 승격 후보) `REJECT_DO_NOT_PROMOTE`**; `claim-08-1`: `true`; `claim-08-2`: `atom-08-2/3/4`=`UNSUPPORTED` (`false`) | `T-004A`, `CLAIM-05` | Stage 2B에서 팀 작업의 개인 기여 승격 차단 및 `claim-08-2` 3-atom 분해 확인 |
| `SCN-09` / `claim-09-1`, `09-2` (`TEST`) **[환경 대표]** | `rev-src09-v1-13804c48@31:98` (스테이징), `@99:121` (운영 미배포) | `fact-09-1` (`METRIC`, `STAGING_TEST`, `metric_details`), `fact-09-2` (`ACTION`, `NEGATED`, `PRODUCTION`); `claim-09-1`: `true`; `claim-09-2`: `atom-09-2` (CONTRADICTS) + `atom-09-3` (CONTEXT) = `UNSUPPORTED` (`false`) | `T-004A`, `CLAIM-06` | 스테이징 지표의 운영 오용 차단 및 `metric_details` 기간 미명시 표기 확인 |
| `SCN-10` / `claim-10-1` (`DEV`) | `rev-src10-v1-8fed086d@52:111`, `@112:318` | `fact-10-1` (`ACTION`), `fact-10-2` (`METRIC`, `OBSERVED_CORRELATION`); `atom-10-1`=`SUPPORTED`, `10-2`/`10-3`=`UNSUPPORTED`; `SOURCE_VERIFIED=false` | `T-004A`, `CLAIM-06` | 교란 요인 존재 시 인과 주장 `UNSUPPORTED` 처리 확인 |
| `SCN-11` / `claim-11-1`, `11-2` (`TEST`) | `rev-src11a-v1-270fe367@52:113` (모호), `rev-src11b-v1-10d2a8d0@122:184` (인증) | `fact-11-1` (`IDENTITY_UNRESOLVED_REVIEW_REQUIRED`, 비활성) → `claim-11-1` `UNSUPPORTED` (`false`); `fact-11-2` (`SUPPORTED`) → `claim-11-2` `SUPPORTED` (`true`) | `T-004A`, `CLAIM-05` | 동명이인 미연결 계정의 Fact 비활성화 규칙 확인 |
| `SCN-12` / `claim-12-1`, `12-2`, **`12-3` [NEW]**, **`12-4` [NEW]** (`DEV`) | `rev-src12-v1-54f9db4b@51:175` | `fact-12-1` (`METRIC`, checkout peak -25%): `claim-12-1` v2 범위확장 재검증 실패 (`UNSUPPORTED`, `false`); `claim-12-2` v2 미재검증 (**`STALE`**, `false`); **`claim-12-3` v2 충실 의역 + 별도 v2 Validator 재검증 통과 (`SUPPORTED`, `SOURCE_VERIFIED=true`)**; **`claim-12-4` `atom-12-6`=`USER_CONFIRMED` 단독 (`SOURCE_VERIFIED=false`)** | `T-004A`, **`CLAIM-08`** | **`CLAIM-08` 4종 매트릭스**: 편집 후 재검증 통과(`12-3`) vs 실패(`12-1`) vs 미재검증 `STALE`(`12-2`) vs `USER_CONFIRMED` 승격 금지(`12-4`) 확인 |
| `SCN-13` / `claim-13-1`, `13-2` (`TEST`) | `rev-src13a-v1-18d4db42@64:149` (삭제됨), `rev-src13b-v1-227d1587@51:126` (보존) | OD-003 삭제 미리보기·확인 → 즉시 접근 차단 → 혼합 출처 `claim-13-2` 포함 서버 편집본·승인본·버전 전량 정리 (`retained=0`, `PARTIALLY_GROUNDED` 금지), 독립 `src13b`/`fact-13-2` 보존 | `T-004A`, `OD-003` | `OD-003` 삭제 캐스케이드와 `REF-03` 단순 재조회 실패의 명확한 구분 유지 확인 |
| `SCN-14` / `claim-14-1` (`TEST`) | `rev-src14-v1-51462cb4@39:140` (Alpha), `@141:229` (Borealis) | `fact-14-1` (`METRIC`), `fact-14-2` (`ACTION`); `atom-14-1`=`SUPPORTED`, `14-2`(10x 왜곡)/`14-3`(프로젝트 혼동)=`UNSUPPORTED`; `SOURCE_VERIFIED=false` | `T-004A`, `CLAIM-06` | 수치 10배 왜곡 + 프로젝트 간 귀속 혼동 차단 확인 |
| **`SCN-15` / `claim-15-1`, `15-2` [NEW]** (`DEV`) | `rev-src15-v1-61ce37bb@76:262` (`fs-15-1` 문장) + **`@183:261` (`fs-15-1b` RFC·롤아웃 리뷰 구체 기여 근거)**, `@263:427` (Security팀 감사, 역할 없음) | `fact-15-1` (`ROLE`, `INDIVIDUAL`, `SUPPORTED`, `role_attribution_grounding`: `person-alex-kim-verified` + `evt-career-15` + `Tech Lead` + `fs-15-1b` 기여자 일치), `fact-15-2` (`ROLE`, `TEAM`, `SUPPORTED`); **`claim-15-1` (PASS)**: `atom-15-1/2`=`SUPPORTED` (`SOURCE_VERIFIED=true`); **`claim-15-2` (FAIL)**: `atom-15-3`=`UNSUPPORTED` (`CONTRADICTS`, `SOURCE_VERIFIED=false`) | **`CLAIM-05`** | DB Model §3.8 fixture 의미: 작성자 일치·`"Tech Lead"` 문구만으로는 통과 금지; 동일 `claim-15-1`에서 기여 근거 누락 또는 타인 기여 시 실패 확인 |
| **`SCN-16` / `claim-16-1`, `16-2` [NEW]** (`TEST`) | `rev-src16-v1-fc6f81e4@123:242` (`PROBLEM`), `@255:370` (`TECHNOLOGY`), `@379:535` (`METRIC`) | `fact-16-1` (`PROBLEM`), `16-2` (`TECHNOLOGY`), `16-3` (`METRIC`: 값·단위·30일 기간·Datadog APM·prod checkout-api p99); **`claim-16-1` (PASS)**: `atom-16-1/2`=`SUPPORTED` (`true`); **`claim-16-2` (FAIL)**: 전사·연간 과대확장 `atom-16-3`=`UNSUPPORTED` (`false`) | **`CLAIM-06`** | 공식 `PROBLEM`/`TECHNOLOGY`/`METRIC` 유형 및 지표 6요소(값·단위·기간·출처·환경·측정범위) 정상/실패 기준 확인 |

---

## 4. 오너 검토 큐 — 공식 `REF-01..REF-08` 합성 케이스 (`합성 케이스 준비` vs `실제 제품 시험 미실행` 엄격 구분)

> **제품 검증 경계**: 아래 27개 Refetch 케이스는 **오프라인 합성 회귀 후보**이며 `product_runtime_verified: false`가 `REF-01..REF-08` 전 항목에 고정되어 있다. 기존 `REF-04/05` 부분 정적 점검과 신규 `REF-06..08` 합성 케이스 준비는 **실제 M4 제품 시험 통과가 아니며**, 별도 `RUNTIME Spike` 결과도 참고 증거일 뿐 이 데이터셋이나 M4 제품 시험을 대신하지 않는다.

| Case ID (Partition) | 합성 원문 위치 | 추출/검증 기대값 | Notion Test ID | 검토할 쟁점 (`product_runtime_verified: false`) |
|---|---|---|---|---|
| `refcase-01a` (`TEST`) | `rev-src02-v1-2cb25379@89:271` | 동일 hash + 정확한 span 재현 (`approval_blocked=false`, Post-MVP 자동갱신 off) | `REF-01` | 실제 외부 커넥터 HTTP refetch 미실행; 운영 정규화·offset 확정 필요 |
| `refcase-02a`, `02b`, `02c` (`TEST`) | `rev-src02-v1-2cb25379@89:271` (`02c`: corrupt `@74:256`) | 본문 드리프트(`02a`), 본문 추가(`02b`), 동일 hash·locator만 오류(`02c`) 모두 차단 (`approval_blocked=true`) | `REF-02` | 본문 변경 vs 내부 locator 손상 텔레메트리 분리 규약 확인 필요 |
| `refcase-03a..03e` (`TEST`) | `rev-src02-v1-2cb25379@89:271` | `404`(`03a`), `206`(`03b`), 일시 `429`/`503`/`timeout`(`03c..03e`, 재시도 상한 3) 모두 승인 차단 + `SCN-13` 삭제·과거 거짓과 구분 | `REF-03` | 실 retry/backoff·HTTP 410 구분 미실행; 제품 상태 매핑 확인 필요 |
| `refcase-04a`, **`04b` [NEW]** (`DEV`) | `rev-src_ref07_canary-v1-7e7ec30a@147:221` + canary | DB·앱로그·Job·캐시·Export 정적 스냅샷 0 누출 + negative leak fixture 검출 | `REF-04` | **부분 정적 점검만 완료** — 실 DB·로그·Job큐·캐시·Export 런타임 감사 미실행 |
| `refcase-05a`, `05b` (`TEST`) | `rev-src02-v1-2cb25379@89:271` / unsafe 6 URL | HTTP 403 권한 회수 차단(`05a`), SSRF/비HTTPS/내부IP/자격증명 URL 사전 차단(`05b`, `fetch_attempted=false`) | `REF-05` | **부분 정적 점검만 완료** — 실 OAuth·테넌트·30x redirect SSRF·DNS rebinding 미실행 |
| **`refcase-06a` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **commit 전 중단** + 메모리 소멸 → 동일 hash 재조회 시 메타데이터+checkpoint 원자 저장, 재현 성공, 중복 0, 저장 원문 fallback 경로 없음 | **`REF-06`** | **합성 케이스 준비(런타임 미검증)** — 실 워커 트랜잭션·checkpoint 스키마 확인 필요 |
| **`refcase-06b` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **commit 후 중단** + 메모리 소멸 → 동일 작업(`idempotency_key`) 재실행 시 중복 기록 0, 동일 hash 재현, 저장 원문 fallback 경로 없음 | **`REF-06`** | **합성 케이스 준비(런타임 미검증)** — 실 idempotency key 계약 확인 필요 |
| **`refcase-06c` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` (drift body) | 중단·메모리 소멸 후 재조회 시 **hash 변경** → 명시적 차단 (`approval_blocked=true`, 저장 원문 복구 경로 없음, 삭제·거짓과 구분) | **`REF-06`** | **합성 케이스 준비(런타임 미검증)** — hash 변경 시 운영자/사용자 안내 UX 확인 필요 |
| **`refcase-07a` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **승인(`APPROVAL`)** 작업 내 재조회 통과 → `checked_at` 감사 기록, 허용 | **`REF-07`** | **합성 케이스 준비(런타임 미검증)** — 실 승인 API 트랜잭션 E2E 미실행 |
| **`refcase-07b` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **새 Export/다운로드(`EXPORT_DOWNLOAD`)**: 승인된 `claim-02-1` v1 전제(`export_precondition_input`) + 작업 내 재조회 통과 → 검증 메타데이터(`source_revision_id`, `source_content_sha256`, `locator_id`, `span_sha256`, `checked_at`) 기록 & **원문 인용 Export 비포함** | **`REF-07`** | **합성 케이스 준비(런타임 미검증)** — `확인 필요`: 검증 메타데이터의 파일 헤더·API 필드 배치 규약 미고정(`TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN`) |
| **`refcase-07c` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **근거 열람(`EVIDENCE_VIEW`)** 작업 내 재조회 통과 → 메모리 전용 일시 표시 + `checked_at` 기록 (원문 비영속) | **`REF-07`** | **합성 케이스 준비(런타임 미검증)** — 실 뷰어 `no-store` 캐시 정책 E2E 미실행 |
| **`refcase-07d` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **이전 검증 기록만 존재**(해당 작업 재조회 미실행) → 차단 (`expected_operation_allowed=false`, `approval_blocked=true`) | **`REF-07`** | **합성 케이스 준비(런타임 미검증)** — stale 검증 거부 에러 코드 확인 필요 |
| **`refcase-07e` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **TOCTOU**: 승인 `claim-02-1` v1 재조회 통과 후 저장 전 권한·유효성 변경 → 차단 (`expected_operation_allowed=false`, `export_artifact_emitted=false`) | **`REF-07`** | **합성 케이스 준비(런타임 미검증)** — 저장 직전 재확인/락 구현 규약 확인 필요 |
| **`refcase-07f` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **대조(미승인 Claim)**: 작업 내 재조회 통과(HTTP 200, hash/locator 일치)에도 요청 Claim이 **미승인(`claim_version_approved=false`)** → Export 차단 (`export_artifact_emitted=false`, `export_payload_snapshot=null`) | **`REF-07`** | **합성 케이스 준비(런타임 미검증)** — 미승인 Claim Export 차단 에러 코드 확인 필요 |
| **`refcase-07g` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **대조(승인 버전 불일치)**: 작업 내 재조회 통과(HTTP 200, hash/locator 일치)에도 요청 버전(`v2`)이 **승인 버전(`v1`)과 불일치** → Export 차단 (`export_artifact_emitted=false`, `export_payload_snapshot=null`) | **`REF-07`** | **합성 케이스 준비(런타임 미검증)** — 승인 버전 불일치 Export 차단 에러 코드 확인 필요 |
| **`refcase-08a` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | 요청·바이트·토큰·재시도·동시성 한도 내 + **동일 작업 내 중복 Unit 제거(2→1)** + 메모리 전용 처리 → 성공 | **`REF-08`** | **합성 케이스 준비(런타임 미검증)** — `확인 필요`: 운영 수치 한도는 `TEST_FIXTURE_ONLY` |
| **`refcase-08b` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **별도 Job에는 ID·hash 메타데이터만 전달**(원문 payload 0) → 다음 Job에서 재조회 통과 | **`REF-08`** | **합성 케이스 준비(런타임 미검증)** — 실 Job 큐 메시지 스키마 확인 필요 |
| **`refcase-08c` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **바이트·토큰 한도 초과** → 명시적 차단 (`silent_truncation_occurred=false`, 필수 근거 조용한 절단·성공 금지) | **`REF-08`** | **합성 케이스 준비(런타임 미검증)** — 한도 초과 에러 코드·재시도 안내 확인 필요 |
| **`refcase-08d` [NEW]** (`TEST`) | `rev-src02-v1-2cb25379@89:271` | **요청·재시도·동시성 한도 초과** → 명시적 차단 (`required_evidence_dropped_silently=false`) | **`REF-08`** | **합성 케이스 준비(런타임 미검증)** — 동시성/요청 한도 운영 수치 확인 필요 |

---

## 5. 검토 후보 무결성 해시 (`CANDIDATE_MANIFEST.json`) 및 별도 승인 매니페스트 (`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`)

> 아래 9개 파일 해시와 `bundle_sha256`은 **원본 보존된 `0.4.0-owner-review-candidate`의 무결성 해시**이며, 프로젝트 오너의 합성 회귀 기준 승인은 별도 [`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`](GOLDEN_APPROVAL_MANIFEST_v0.4.0.json) (`0.4.0-synthetic-golden`)에 기록된다. `T-407A`는 [`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`](GOLDEN_APPROVAL_MANIFEST_v0.4.0.json)을 단일 진입점으로 사용한다.

- **Approved Synthetic Golden Manifest**: [`GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`](GOLDEN_APPROVAL_MANIFEST_v0.4.0.json) (`0.4.0-synthetic-golden`, `T-004B` → `T-407A`)
- **Supersedes Baseline (Git 보존)**: `0.3.0-synthetic-golden` @ `fa6332d2dda00c59dd922465c1005349017577fe`
- **Candidate Manifest SHA-256 (`CANDIDATE_MANIFEST.json`)**: `sha256:2e9ffde13dfa619cee9035fdf2f9746adfd6b925875a7a955790e210b4a0bcea`
- **Candidate Bundle SHA-256 (`bundle_sha256`)**: `sha256:27b6d1eeace3e4ac5771f1abb1555a2af3c6cd9446a9f7f10005404572d7b059`

| Artifact Path | Exact UTF-8 SHA-256 |
|---|---|
| `expected/oracle_draft.json` | `sha256:4b1bf29f89b7456e0437cf98a45d4a471d54cbc7e1119b6b7168c284041b9378` |
| `expected/refetch_oracle_draft.json` | `sha256:4b41c6f941635919ed07154ffca26debf57b39bb3adb514bdb4069e90aeca8b4` |
| `schemas/claim_pipeline.schema.json` | `sha256:1671e4ce0e2b3bad55ede22e7f29a41e6634e0c3c266069eb7dee13128d46416` |
| `sources/refetch_snapshots.json` | `sha256:a7e557ba1cbe653d6d751a6464bc9db367c3fc8a6f3e1c82aa2f98519544ff7d` |
| `sources/source_revisions.json` | `sha256:91d8ba292d93ba1205c35091bf33ab3e3e63367902fb9237d64f0887868f5a66` |
| `stages/stage1_content_units.json` | `sha256:cbdb79ca01bfe65c995ad7f21b7423fe6e4d9dab5434b914c6ef8288d49beaaf` |
| `stages/stage2_fact_extraction.json` | `sha256:cd3459141a54a85cc2172dafa52118273a7591a9da77ab98db66580e6d8126c5` |
| `stages/stage3_claim_assembly.json` | `sha256:b61b05f8a7d1aa9223ae9ff0e647c2c9f73f92e59199b5dbb0f5b07b27af2333` |
| `stages/stage4_refetch_checks.json` | `sha256:e8a72693e8bc248e727829076c5ded0ce01b44ea3eaf309a13e48247916e37a8` |
