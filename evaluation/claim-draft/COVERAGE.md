# T-004A/B Synthetic Regression Golden Coverage Matrix, Partitioning & Approval Record

- **Dataset Version**: `0.3.0-synthetic-golden` (approved from `0.3.0-draft` @ `570067488a2d8d5b2fe249653336ec60af4a45c8`)
- **Approval Status**: `APPROVED_SYNTHETIC_REGRESSION_GOLDEN` (`oracle_status: "APPROVED_SYNTHETIC_REGRESSION"`, `frozen_for_golden: true`)
- **Golden Scope**: `SYNTHETIC_REGRESSION_ONLY_NO_LIVE_LLM_NO_PROD_INTEGRATION` (**합성 회귀 테스트용 정답에 한정 — 실제 LLM 평가·운영/외부 커넥터 연동·미검증 `REF-04..08` 항목은 승인 범위 제외**)
- **Verdict Namespace**: `DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS` (provisional `DRAFT_*` labels remain separate from unconfirmed product state enums, which stay `REVIEW_REQUIRED`)
- **Approval Manifest & Bundle Hash**: [`APPROVAL_MANIFEST.json`](APPROVAL_MANIFEST.json) · `bundle_sha256`: `sha256:881b6bb182b65c51c88a09ecf605322167ff439a4fffc37c1d77f07f1ad6b51f`
- **Totals**: **14 Scenarios** (≥12) · **41 Claim Atoms** (≥24) · **23 Claims** · **26 Canonical Facts** · **29 Fact Supports** · **12 Synthetic Refetch Fixtures** (`REF-01..REF-03` static stubs approved; `REF-04..REF-05` `PARTIAL_COVERAGE_GAP` NOT passed; `REF-06..REF-08` `COVERAGE_GAP` NOT passed)

---

## 1. Approval Scope & Explicit Exclusions

| 구분 | 항목 |
|---|---|
| **승인 포함 (`IN SCOPE`)** | `SCN-01..SCN-14` 합성 Fact·Claim 회귀 정답 (23 Claims, 41 Claim Atoms, Round-01 수정 11건 + 유지 12건 + `SCN-13` OD-003 삭제 생명주기), `REF-01..REF-03` 정적 합성 Refetch stub 9건 및 `REF-04`/`REF-05` 정적 fixture 3건의 **합성 회귀 기대값**, 프로그램 기반 locator/hash round-trip 검증, 동일 원문·편집본 family 단위 평가 묶음(`SYNTHETIC_REGRESSION_DEV` / `SYNTHETIC_REGRESSION_TEST`) |
| **승인 제외 (`OUT OF SCOPE`)** | 실제 외부 LLM 추론 품질·프롬프트·비용 평가(`live_llm_evaluation_in_scope: false`), 실제 외부 커넥터(GitHub/Notion/Slack/Jira)·실시간 HTTP refetch/retry·실시간 OAuth/테넌트·리다이렉트/DNS-rebinding SSRF·운영 DB/제품 코드 연동(`production_connector_integration_in_scope: false`), 미검증 `REF-04..REF-05` 런타임 공백(`PARTIAL_COVERAGE_GAP`, 미통과) 및 `REF-06..REF-08`(`COVERAGE_GAP`, 미통과), 운영 정규화·offset 규약 및 제품 상태 enum 최종 매핑(`REVIEW_REQUIRED`) |

---

## 2. Family-Preserving Evaluation Partitions (Same Source & Edited Versions Grouped Together)

같은 원문(`source_revision_id`)과 `evaluation_family_id`에서 파생된 모든 시나리오, 사용자 편집 버전(`v1 → v2`), Refetch fixture는 **반드시 동일한 평가 묶음**에 배치되며 `validate_dataset.py`가 교차 누수(`FAMILY_PARTITION_LEAKAGE`)를 0건으로 검증한다.

| Evaluation Partition | Families | Scenarios | Claims (incl. Edited Versions) | Refetch Fixtures | Anti-Leakage Grouping Notes |
|---|---:|---|---|---|---|
| `SYNTHETIC_REGRESSION_DEV` | **8** (`family-src01`, `src03`, `src04`, `src06`, `src08`, `src10`, `src12`, `src_ref07_canary`) | `SCN-01`, `SCN-03`, `SCN-04`, `SCN-06`, `SCN-08`, `SCN-10`, `SCN-12` (**7**) | `claim-01-1`, `03-1`, `04-1`, `04-2`, `06-1`, `06-2`, `08-1`, `08-2`, `10-1`, `12-1 (v2)`, `12-2 (v2)` (**11**) | `refcase-04a` (**1**) | `src12` 원본과 사용자 편집본 `claim-12-1 (v2)`, `claim-12-2 (v2)` 모두 `family-src12`로 `DEV`에 동시 배치; canary 전용 `src_ref07_canary`는 독립 family로 `DEV` 배치 |
| `SYNTHETIC_REGRESSION_TEST` | **7** (`family-src02`, `src05`, `src07`, `src09`, `src11`, `src13`, `src14`) | `SCN-02`, `SCN-05`, `SCN-07`, `SCN-09`, `SCN-11`, `SCN-13`, `SCN-14` (**7**) | `claim-02-1`, `05-1`, `05-2`, `07-1`, `07-2`, `09-1`, `09-2`, `11-1`, `11-2`, `13-1`, `13-2 (v2)`, `14-1` (**12**) | `refcase-01a`, `02a`, `02b`, `02c`, `03a`, `03b`, `03c`, `03d`, `03e`, `05a`, `05b` (**11**) | `SCN-02`와 `src02` 기반 Refetch fixture **11건 전부** `family-src02`로 `TEST`에 동시 배치; `src13a+src13b`와 편집본 `claim-13-2 (v2)` 모두 `family-src13`으로 `TEST`에 동시 배치 |

---

## 3. Approved Synthetic Regression Ground Truth ↔ Source Evidence (`SCN-01`..`SCN-14`)

Each row pairs the approved synthetic regression verdict (`DRAFT_CLAIM_*`), evaluation partition, `SOURCE_VERIFIED` eligibility flag, and exact synthetic evidence chain (`Source Revision @ char_start:char_end` → `Canonical Fact` → `ClaimAtomSupport`).

| Partition | Scenario | Claim (`ver`) | Approved Synthetic Verdict (`DRAFT_*`) | `SOURCE_VERIFIED` Eligible | Paired Evidence (`Atom ← Role(eval_ver) ← Fact ← SourceRevision@char_start:char_end`) | Review / Regression Anchor |
|---|---|---|---|---:|---|---|
| `DEV` | `SCN-01` | `claim-01-1` (`v1`) *(kept)* | `DRAFT_CLAIM_UNGROUNDED` | `false` | `atom-01-1`: **0 supports** (`rev-src01-v1-a43eb41e` boilerplate yields 0 facts) | 0-fact threshold for meeting logistics chatter |
| `TEST` | `SCN-02` | `claim-02-1` (`v1`) **[Round-01]** | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-02-1` (retry queue impl) + `atom-02-2` (conditional effect: preventing duplicate invoice notifications during transient gateway timeouts) ← `DIRECT(v1)` ← `fact-02-1` (`env=UNSPECIFIED`, `attr=INDIVIDUAL`) [`rev-src02-v1-2cb25379@89:271`] | BOTH implementation AND conditional effect atoms required & verified; `env=UNSPECIFIED` |
| `DEV` | `SCN-03` | `claim-03-1` (`v1`) **[Round-01]** | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-03-1` (샤드 스크립트 작성) + `atom-03-2` (운영 p95 `620ms→180ms`) ← `DIRECT(v1)` ← `fact-03-1` (`env=PRODUCTION`) [`rev-src03-v1-6589ff1a@63:131`]; `atom-03-3` (배포 실패 시 `2분 이내` 자동 롤백) ← `DIRECT(v1)` ← `fact-03-2` (`env=UNSPECIFIED`) [`@135:181`]; `atom-03-4` (`300ms 초과` 슬로우 쿼리 알림) ← `DIRECT(v1)` ← `fact-03-3` (`env=UNSPECIFIED`) [`@185:231`] | 4/4 required Korean atoms (`action`/`metric`/`rollback 2m`/`alert 300ms`) DIRECT-grounded |
| `DEV` | `SCN-04` | `claim-04-1` (`v1`) **[Round-01]** | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-04-1` ← `DIRECT(v1)` ← `fact-04-1` [`rev-src04-v1-0dfa04ee@171:268`] + `CONTEXT(v1)` ← `fact-04-2` [`@44:144`] | Unverified `"Q3 reliability work"` modifier removed; DIRECT + CONTEXT qualifies |
| `DEV` | `SCN-04` | `claim-04-2` (`v1`) *(kept)* | `DRAFT_CLAIM_UNGROUNDED` | `false` | `atom-04-2` ← `CONTEXT(v1)` ← `fact-04-2` [`rev-src04-v1-0dfa04ee@44:144`] *(CONTEXT-only)* | `CONTEXT`-only never qualifies for `SOURCE_VERIFIED` |
| `TEST` | `SCN-05` | `claim-05-1` (`v1`) **[Round-01]** | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-05-1` (personal failover execution): **0 supports** (`DRAFT_ATOM_UNSUPPORTED`); `atom-05-2` (zero dropped requests) ← `DIRECT(v1)` ← `fact-05-1` (`env=UNSPECIFIED`, `attr=UNSPECIFIED`) [`rev-src05a-v1-898e609e@40:114`] + `CONTRADICTS(v1)` ← `fact-05-2` (`env=UNSPECIFIED`, `attr=UNSPECIFIED`) [`rev-src05b-v2-0539c693@58:159`, contradict span `@83:203`] | Unsupported personal execution split from draft-vs-addendum zero-drop conflict |
| `TEST` | `SCN-05` | `claim-05-2` (`v1`) **[Round-01]** | `DRAFT_CLAIM_PARTIALLY_GROUNDED` | `false` | `atom-05-3` (log audit confirmed 412 HTTP 503s / 90s) ← `DIRECT(v1)` ← `fact-05-2` [`rev-src05b-v2-0539c693@58:159`] (`DRAFT_ATOM_SUPPORTED_DIRECT`); `atom-05-4` (user personally audited & documented) ← `CONTEXT(v1)` ← `fact-05-2` (`DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT`) | Passive log-audit fact separated from unverified personal audit/documentation attribution |
| `DEV` | `SCN-06` | `claim-06-1` (`v1`) **[Round-01]** | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-06-1` (엔드포인트 구현) + `atom-06-2` (운영 모니터링 연동) ← `DIRECT(v1)` ← `fact-06-1` [`rev-src06-v1-11685e46@38:79`] | Implementation vs production monitoring integration both DIRECT-grounded |
| `DEV` | `SCN-06` | `claim-06-2` (`v1`) **[Round-01]** | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-06-3` (샤딩 완료) + `atom-06-4` (벤치마크 완료) ← `CONTRADICTS(v1)` ← `fact-06-2` [`rev-src06-v1-11685e46@87:147`] | Sharding & benchmark completion both conflict with observation-time RFC plan |
| `TEST` | `SCN-07` | `claim-07-1` (`v1`) *(kept)* | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-07-1` ← `DIRECT(v1)` ← `fact-07-1` [`rev-src07-v1-84ada707@113:195`] | Accurate rollback claim qualifies |
| `TEST` | `SCN-07` | `claim-07-2` (`v1`) **[Round-01]** | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-07-2` (prod deploy) ← `CONTRADICTS(v1)` ← `fact-07-2` [`rev-src07-v1-84ada707@58:111`]; `atom-07-3` (30% savings) ← `CONTRADICTS(v1)` ← `fact-07-3` [`@200:255`] | Negated production deploy (`@58:111`) split from negated 30% savings (`@200:255`) |
| `DEV` | `SCN-08` | `claim-08-1` (`v1`) *(kept)* | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-08-1` ← `DIRECT(v1)` ← `fact-08-2` [`rev-src08-v1-699e83e2@190:254`] + `CONTEXT(v1)` ← `fact-08-1` [`@31:169`] | Individual cache contribution with team context qualifies |
| `DEV` | `SCN-08` | `claim-08-2` (`v1`) **[Round-01]** | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-08-2` (solo rebuild) ← `CONTRADICTS(v1)` ← `fact-08-1` [`rev-src08-v1-699e83e2@31:169`] (`CONTRADICTED`); `atom-08-3` (`from scratch`): **0 supports** (`UNSUPPORTED`); `atom-08-4` (`22m→8m` team outcome) ← `CONTEXT(v1)` ← `fact-08-1` (`CONTEXT_ONLY_INSUFFICIENT`) | Solo attribution contradicts 6-engineer team fact (`CONFLICTING`); separated from `"from scratch"` & team outcome |
| `TEST` | `SCN-09` | `claim-09-1` (`v1`) *(kept)* | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-09-1` ← `DIRECT(v1)` ← `fact-09-1` [`rev-src09-v1-13804c48@31:98`] | Staging-scoped Korean benchmark claim qualifies |
| `TEST` | `SCN-09` | `claim-09-2` (`v1`) **[Round-01]** | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-09-2` (운영 DB 배포) ← `CONTRADICTS(v1)` ← `fact-09-2` [`rev-src09-v1-13804c48@99:121`] (`CONTRADICTED`); `atom-09-3` (운영 p95 `840→95ms`) ← `CONTEXT(v1)` ← `fact-09-1` [`@31:98`] (`CONTEXT_ONLY_INSUFFICIENT`) | Unstarted prod rollout (`CONTRADICTS`) separated from staging metric misattributed to prod (`CONTEXT`-only) |
| `DEV` | `SCN-10` | `claim-10-1` (`v1`) *(kept)* | `DRAFT_CLAIM_PARTIALLY_GROUNDED` | `false` | `atom-10-1` ← `DIRECT(v1)` ← `fact-10-1` [`rev-src10-v1-8fed086d@52:111`]; `atom-10-2` ← `CONTEXT(v1)` ← `fact-10-2` [`@112:318`]; `atom-10-3`: **0 supports** | 1/3 required atoms supported (unverified causation + fabricated 45%) blocks `SOURCE_VERIFIED` |
| `TEST` | `SCN-11` | `claim-11-1` (`v1`) *(kept)* | `DRAFT_CLAIM_IDENTITY_AMBIGUOUS` | `false` | `atom-11-1` ← `DIRECT(v1)` ← `fact-11-1` [`rev-src11a-v1-270fe367@52:113`] (`identity_status=AMBIGUOUS`) | Unmapped homonym `'Alex'` blocks `SOURCE_VERIFIED` |
| `TEST` | `SCN-11` | `claim-11-2` (`v1`) *(kept)* | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-11-2` ← `DIRECT(v1)` ← `fact-11-2` [`rev-src11b-v1-10d2a8d0@122:184`] (`identity_status=RESOLVED`) | Verified-account PR qualifies |
| `DEV` | `SCN-12` | `claim-12-1` (`v2`, edited) **[Round-01]** | `DRAFT_CLAIM_REVERIFY_FAILED_AFTER_EDIT` | `false` | `atom-12-1` (global tuning scope) + `atom-12-3` (global 60% p99 cut) ← `CONTEXT(v2)` ← `fact-12-1` (`env=UNSPECIFIED`) [`rev-src12-v1-54f9db4b@51:175`] | Checkout-only peak 25% gives `CONTEXT` only (fails v2 re-verification without claiming 60% false in every scope) |
| `DEV` | `SCN-12` | `claim-12-2` (`v2`, edited) *(kept)* | `DRAFT_CLAIM_REVERIFY_REQUIRED_NEW_VERSION` | `false` | `atom-12-2` ← `DIRECT(v1 stale!)` ← `fact-12-1` [`rev-src12-v1-54f9db4b@51:175`] (minor paraphrase v2 not yet re-verified) | v1 support does NOT auto-carryover to v2; re-verification required on v2 |
| `TEST` | `SCN-13` | `claim-13-1` (`v1`) *(kept: OD-003)* | `DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003` | `false` | `atom-13-1` ← `DIRECT(v1, removed_by_user=true)` ← `fact-13-1` [`rev-src13a-v1-18d4db42@64:149`] (`deleted_by_user=true`); pre-del: `v1`+approved → post-cleanup: `versions=[]`, `edited=0`, `approved=0`; independent `rev-src13b-v1-227d1587` (`fact-13-2`) preserved | OD-003 lifecycle: deletion preview + explicit confirm → immediate access block → full purge (distinct from `REF-03`) |
| `TEST` | `SCN-13` | `claim-13-2` (`v2`, mixed `src13a+src13b`) *(kept: OD-003)* | `DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003` | `false` | `atom-13-2` ← `DIRECT(v2, removed_by_user=true)` ← `fact-13-1` [`rev-src13a-v1-18d4db42@64:149`]; `atom-13-3` ← `DIRECT(v2)` ← `fact-13-2` [`rev-src13b-v1-227d1587@51:126`] (`deleted_by_user=false`); pre-del: `v1,v2`+edited+approved → post-cleanup: `versions=[]`, `edited=0`, `approved=0` | Mixed-source dependent Claim is NEVER left as `PARTIALLY_GROUNDED` post-deletion; independent `src13b`/`fact-13-2` preserved |
| `TEST` | `SCN-14` | `claim-14-1` (`v1`) *(kept)* | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-14-1` ← `DIRECT(v1)` ← `fact-14-2` [`rev-src14-v1-51462cb4@141:229`]; `atom-14-2` ← `CONTRADICTS(v1)` ← `fact-14-1` [`@39:140`]; `atom-14-3` ← `CONTEXT(v1)` ← `fact-14-1, fact-14-2` | 400ms→40ms distortion + Alpha/Borealis conflation block `SOURCE_VERIFIED` |

---

## 4. Official `REF-01..REF-08` Coverage Matrix (Unverified Items Explicitly NOT Passed)

| Official ID | 공식 요구사항 | Coverage Status | Matrix Approval Status (`verified_in_draft`) | Partition | Fixture (`old ID`) | Paired Baseline Evidence & Verdict / Unverified Gap |
|---|---|---|---|---|---|---|
| `REF-01` | 동일 버전과 정확한 근거 위치 재현 | `COVERED_SYNTHETIC_STUB` | `APPROVED_SYNTHETIC_REGRESSION` (`true`, **stub only**) | `TEST` | `refcase-01a` (`REF-01`) | `rev-src02-v1-2cb25379@89:271` → `DRAFT_REFETCH_EXACT_SPAN_REPRODUCED` (Post-MVP auto-detect/refresh disabled; live HTTP 미실행) |
| `REF-02` | hash/locator 불일치 차단 | `COVERED_SYNTHETIC_STUB` | `APPROVED_SYNTHETIC_REGRESSION` (`true`, **stub only**) | `TEST` | `refcase-02a`, `02b`, `02c` | Body header drift (`hash=false,loc=false`), body append (`hash=false,loc=true`), and **same-hash locator-only** (`refcase-02c`, `hash=true,loc=false`) all block approval |
| `REF-03` | 재조회 불가와 삭제·거짓 구분 | `COVERED_SYNTHETIC_STUB` | `APPROVED_SYNTHETIC_REGRESSION` (`true`, **stub only**) | `TEST` | `refcase-03a..03e` | Single `404` (`03a`), partial `206` (`03b`), and transient `429`/`503`/`timeout` (`03c..03e`, bounded retry stub `max=3`) block approval while distinguishing from `404`, `SCN-13` deletion, and historical falsity (**live retry/timeout harness 미실행**) |
| `REF-04` | 원문 비영속 | **`PARTIAL_COVERAGE_GAP`** | **`PARTIAL_UNVERIFIED_COVERAGE_GAP` (`false` — NOT passed)** | `DEV` | `refcase-04a` (`REF-07`) | Static JSON canary (`CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST`) 0 leaks + negative fixture caught; **unverified runtime gap**: live DB/logs/queues/caches/disk audit |
| `REF-05` | 접근 권한·허용된 URL 경계 | **`PARTIAL_COVERAGE_GAP`** | **`PARTIAL_UNVERIFIED_COVERAGE_GAP` (`false` — NOT passed)** | `TEST` | `refcase-05a`, `refcase-05b` | Static HTTP 403 & 6 unsafe URL pre-check vectors (`fetch_attempted=false`); **unverified runtime gap**: live OAuth/tenant, 30x redirect SSRF, DNS-rebinding |
| `REF-06` | 중단·재실행 시 원문 저장에 의존하지 않음 | **`COVERAGE_GAP`** | **`UNVERIFIED_COVERAGE_GAP` (`false` — NOT passed)** | *(n/a)* | *(none)* | **Unverified**: requires worker crash/checkpoint/resume harness |
| `REF-07` | 승인·Export·근거 열람 시 재조회 | **`COVERAGE_GAP`** | **`UNVERIFIED_COVERAGE_GAP` (`false` — NOT passed)** | *(n/a)* | *(none)* | **Unverified**: requires product Approval / Export / Evidence-Viewer E2E |
| `REF-08` | 비용·크기 한도와 단계 간 메모리 전용 처리 | **`COVERAGE_GAP`** | **`UNVERIFIED_COVERAGE_GAP` (`false` — NOT passed)** | *(n/a)* | *(none)* | **Unverified**: requires runtime byte/cost cap & in-memory inter-stage transport test |

> **Strict Non-Overclaim Rule**: `REF-04..REF-05` (`PARTIAL_COVERAGE_GAP`) and `REF-06..REF-08` (`COVERAGE_GAP`) remain `verified_in_draft: false` and are **NOT** marked passed in this approval. Even if reusable partial evidence exists from separate `RUNTIME` verification tracks, it MUST NOT be counted as full product verification here.

---

## 5. Recorded Dataset Hashes (`APPROVAL_MANIFEST.json`, `v0.3.0-synthetic-golden`)

- **Approved From Draft Commit**: `570067488a2d8d5b2fe249653336ec60af4a45c8` (`0.3.0-draft`)
- **Bundle SHA-256 (`bundle_sha256`)**: `sha256:881b6bb182b65c51c88a09ecf605322167ff439a4fffc37c1d77f07f1ad6b51f`

| Artifact Path | Exact UTF-8 SHA-256 |
|---|---|
| `expected/oracle_draft.json` | `sha256:0a12ea66244f9ac4de3a4792232b820f2ad6e32315a7d7ad8d6ff8b228512dc4` |
| `expected/refetch_oracle_draft.json` | `sha256:e0e948857074b4a03123b0cbb54070c0d32f21e5774d50f1a40aa728c096dc82` |
| `schemas/claim_pipeline.schema.json` | `sha256:1cbd5c7f86a8caad31da140b27dbfd51c6cc1b09dc19a7c6b3957e3def7d58c9` |
| `sources/refetch_snapshots.json` | `sha256:5440972a84c0863f381b10a52fa1563ab31b99920ba9604562ab1f18de3ef8e3` |
| `sources/source_revisions.json` | `sha256:bdc34b62b44a97acbf544f574a7e66321605766a843ff78a572ed1efe78d5951` |
| `stages/stage1_content_units.json` | `sha256:1fd63c6b78d33c8f4caa8e2a66b8b0ab6405e3ba135aec44d5f7188f744f76fa` |
| `stages/stage2_fact_extraction.json` | `sha256:87c88681fb24d074716dfc95ce779b2b4c716c6e47658338bfdca2b992f3f224` |
| `stages/stage3_claim_assembly.json` | `sha256:751bf9498a5d6d162d7f7466e14e5afc72c8e4081798239c0f59682cbab43978` |
| `stages/stage4_refetch_checks.json` | `sha256:26de0f75d831446e010b7c43370b1372a3725cd30c1ab6de8d937e7ba4c79cc4` |

---

## 6. Remaining Open Items (`REVIEW_REQUIRED` / Post-Approval Tracks)

1. **Normalization & Offset Contract (`REVIEW_REQUIRED`)**: Confirm production normalization (`fixture-nfc-lf-v0-draft`) and offset unit (`char` vs `utf8_byte` vs `utf16`) against finalized product specs.
2. **Product Status Enum Mapping (`REVIEW_REQUIRED`)**: Map provisional `DRAFT_CLAIM_*`, `DRAFT_ATOM_*`, and `DRAFT_REFETCH_*` labels onto finalized product state enums before product code integration.
3. **Refetch Runtime & Connector Verification (`REF-01..REF-08`)**: Run live HTTP/retry-backoff tests for `REF-01..REF-03`, close live runtime gaps in `REF-04` and `REF-05`, and build full integration/E2E harnesses for `REF-06`, `REF-07`, and `REF-08`.
4. **Live LLM & Production Integration**: Separate evaluation tracks (not covered by `v0.3.0-synthetic-golden`).
