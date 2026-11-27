# T-004A Coverage Matrix, Evidence-Paired Review Table & T-004B Handoff

- **Dataset Version**: `0.2.0-draft`
- **Oracle Status**: `DRAFT` (`frozen_for_golden: false`)
- **Verdict Namespace**: `DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS` (provisional `DRAFT_*` labels — distinct from product state enums, which remain `REVIEW_REQUIRED`)
- **Totals**: **14 Scenarios** (≥12) · **30 Claim Atoms** (≥24) · **23 Claims** · **25 Canonical Facts** · **28 Fact Supports** · **8 Preserved Refetch Fixtures** mapped across official `REF-01..REF-05` + **3 Unverified Official Requirements (`REF-06..REF-08`: `COVERAGE_GAP`)**

---

## 1. Human Review Pairing Table: Expected Results (`DRAFT`) ↔ Source Evidence (`SCN-01`..`SCN-14`)

Each row pairs the provisional draft verdict (`DRAFT_CLAIM_*`), `SOURCE_VERIFIED` eligibility flag, and the exact synthetic evidence chain (`Source Revision @ char_start:char_end` → `Canonical Fact` → `ClaimAtomSupport`).

| Scenario | Claim (`ver`) | Provisional Verdict (`DRAFT_*`) | `SOURCE_VERIFIED` Eligible | Paired Evidence (`Atom ← Role(eval_ver) ← Fact ← SourceRevision@char_start:char_end`) | Human Review Checkpoint |
|---|---|---|---:|---|---|
| `SCN-01` | `claim-01-1` (`v1`) | `DRAFT_CLAIM_UNGROUNDED` | `false` | `atom-01-1`: **0 supports** (`rev-src01-v1-a43eb41e` boilerplate yields 0 facts) | Confirm 0-fact threshold for meeting logistics chatter |
| `SCN-02` | `claim-02-1` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-02-1 ← DIRECT(v1) ← fact-02-1 [rev-src02-v1-2cb25379@89:271]` | Confirm baseline clean individual PR candidate |
| `SCN-03` | `claim-03-1` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-03-1 ← DIRECT(v1) ← fact-03-1 [rev-src03-v1-6589ff1a@63:131]`; `atom-03-2 ← DIRECT(v1) ← fact-03-2 [@135:181]`; `atom-03-3 ← DIRECT(v1) ← fact-03-3 [@185:231]` | Confirm 3/3 required Korean atoms DIRECT-grounded |
| `SCN-04` | `claim-04-1` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-04-1 ← DIRECT(v1) ← fact-04-1 [rev-src04-v1-0dfa04ee@171:268]` + `CONTEXT(v1) ← fact-04-2 [@44:144]` | Confirm DIRECT + CONTEXT qualifies as candidate |
| `SCN-04` | `claim-04-2` (`v1`) | `DRAFT_CLAIM_UNGROUNDED` | `false` | `atom-04-2 ← CONTEXT(v1) ← fact-04-2 [rev-src04-v1-0dfa04ee@44:144]` *(CONTEXT-only)* | Confirm `CONTEXT`-only never qualifies for `SOURCE_VERIFIED` |
| `SCN-05` | `claim-05-1` (`v1`) | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-05-1 ← DIRECT(v1) ← fact-05-1 [rev-src05a-v1-898e609e@40:114]` + `CONTRADICTS(v1) ← fact-05-2 [rev-src05b-v2-0539c693@58:159]` | Confirm addendum contradiction blocks `SOURCE_VERIFIED` |
| `SCN-05` | `claim-05-2` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-05-2 ← DIRECT(v1) ← fact-05-2 [rev-src05b-v2-0539c693@58:159]` | Confirm corrected log-audit claim is grounded candidate |
| `SCN-06` | `claim-06-1` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-06-1 ← DIRECT(v1) ← fact-06-1 [rev-src06-v1-11685e46@38:79]` | Confirm completed Korean RFC item qualifies |
| `SCN-06` | `claim-06-2` (`v1`) | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-06-2 ← CONTRADICTS(v1) ← fact-06-2 [rev-src06-v1-11685e46@87:147]` | Confirm planned Q4 sharding claimed as completed maps to conflict/block |
| `SCN-07` | `claim-07-1` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-07-1 ← DIRECT(v1) ← fact-07-1 [rev-src07-v1-84ada707@113:195]` | Confirm accurate rollback claim qualifies |
| `SCN-07` | `claim-07-2` (`v1`) | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-07-2 ← CONTRADICTS(v1) ← fact-07-2 [rev-src07-v1-84ada707@58:255]` | Confirm negation reversal ("did not deploy / did not achieve 30%") is blocked |
| `SCN-08` | `claim-08-1` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-08-1 ← DIRECT(v1) ← fact-08-2 [rev-src08-v1-699e83e2@190:254]` + `CONTEXT(v1) ← fact-08-1 [@31:169]` | Confirm individual cache contribution with team context qualifies |
| `SCN-08` | `claim-08-2` (`v1`) | `DRAFT_CLAIM_UNGROUNDED` | `false` | `atom-08-2 ← CONTEXT(v1) ← fact-08-1 [rev-src08-v1-699e83e2@31:169]` *(CONTEXT-only)* | Confirm team-of-6 work overclaimed as solo never qualifies |
| `SCN-09` | `claim-09-1` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-09-1 ← DIRECT(v1) ← fact-09-1 [rev-src09-v1-13804c48@31:98]` | Confirm staging-scoped Korean benchmark claim qualifies |
| `SCN-09` | `claim-09-2` (`v1`) | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-09-2 ← CONTEXT(v1) ← fact-09-1 [@31:98]` + `CONTRADICTS(v1) ← fact-09-2 [rev-src09-v1-13804c48@99:121]` | Confirm staging result conflated with unstarted production rollout is blocked |
| `SCN-10` | `claim-10-1` (`v1`) | `DRAFT_CLAIM_PARTIALLY_GROUNDED` | `false` | `atom-10-1 ← DIRECT(v1) ← fact-10-1 [rev-src10-v1-8fed086d@52:111]`; `atom-10-2 ← CONTEXT(v1) ← fact-10-2 [@112:318]`; `atom-10-3`: **0 supports** | Confirm 1/3 required atoms supported (unverified causation + fabricated 45%) blocks `SOURCE_VERIFIED` |
| `SCN-11` | `claim-11-1` (`v1`) | `DRAFT_CLAIM_IDENTITY_AMBIGUOUS` | `false` | `atom-11-1 ← DIRECT(v1) ← fact-11-1 [rev-src11a-v1-270fe367@52:113]` (`identity_status=AMBIGUOUS`) | Confirm unmapped homonym `'Alex'` blocks `SOURCE_VERIFIED` |
| `SCN-11` | `claim-11-2` (`v1`) | `DRAFT_CLAIM_GROUNDED_CANDIDATE` | `true` *(candidate)* | `atom-11-2 ← DIRECT(v1) ← fact-11-2 [rev-src11b-v1-10d2a8d0@122:184]` (`identity_status=RESOLVED`) | Confirm verified-account PR qualifies |
| `SCN-12` | `claim-12-1` (`v2`, edited) | `DRAFT_CLAIM_REVERIFY_FAILED_AFTER_EDIT` | `false` | `atom-12-1 ← CONTRADICTS(v2) ← fact-12-1 [rev-src12-v1-54f9db4b@51:175]` (25% checkout → 60% global) | Confirm user edit to v2 re-verified against `fact-12-1` fails (`CONTRADICTS`) |
| `SCN-12` | `claim-12-2` (`v2`, edited) | `DRAFT_CLAIM_REVERIFY_REQUIRED_NEW_VERSION` | `false` | `atom-12-2 ← DIRECT(v1 stale!) ← fact-12-1 [rev-src12-v1-54f9db4b@51:175]` (minor paraphrase v2 not yet re-verified) | Confirm v1 support does NOT auto-carryover to v2; re-verification required on v2 |
| `SCN-13` | `claim-13-1` (`v1`) | `DRAFT_CLAIM_INVALIDATED_BY_USER_DELETION` | `false` | `atom-13-1 ← DIRECT(v1, removed_by_user=true) ← fact-13-1 [rev-src13a-v1-18d4db42@64:149]` (`deleted_by_user=true`) | Confirm explicit user source deletion invalidates claim (contrast with `REF-03`) |
| `SCN-13` | `claim-13-2` (`v1`) | `DRAFT_CLAIM_PARTIALLY_GROUNDED` | `false` | `atom-13-2 ← DIRECT(v1, removed_by_user=true) ← fact-13-1 [@64:149]`; `atom-13-3 ← DIRECT(v1) ← fact-13-2 [rev-src13b-v1-227d1587@51:126]` | Confirm deletion of 1 required atom blocks `SOURCE_VERIFIED` even though 2nd atom survives |
| `SCN-14` | `claim-14-1` (`v1`) | `DRAFT_CLAIM_CONFLICTING` | `false` | `atom-14-1 ← DIRECT(v1) ← fact-14-2 [rev-src14-v1-51462cb4@141:229]`; `atom-14-2 ← CONTRADICTS(v1) ← fact-14-1 [@39:140]`; `atom-14-3 ← CONTEXT(v1) ← fact-14-1, fact-14-2` | Confirm 400ms→40ms distortion + Alpha/Borealis conflation block `SOURCE_VERIFIED` |

---

## 2. Official `REF-01..REF-08` Coverage Matrix & Evidence Pairing

| Official ID | 공식 요구사항 | Draft Coverage Status | Preserved Fixture (`old ID`) | Paired Baseline Evidence & Locator | Provisional Verdict (`DRAFT_REFETCH_*`) / Coverage Gap |
|---|---|---|---|---|---|
| `REF-01` | 동일 버전과 정확한 근거 위치 재현 | `COVERED_SYNTHETIC_STUB` | `refcase-01a` (`REF-01`) | `rev-src02-v1-2cb25379@89:271` (`loc-ref-base-src02-89_271-e3bf184f`) | `DRAFT_REFETCH_EXACT_SPAN_REPRODUCED` (Post-MVP auto-detect/refresh disabled; live HTTP 미실행) |
| `REF-02` | hash/locator 불일치 차단 | `COVERED_SYNTHETIC_STUB` | `refcase-02a` (`REF-02`) | `rev-src02-v1-2cb25379@89:271` (`loc-ref-base-src02-89_271-e3bf184f`) | `DRAFT_REFETCH_HASH_LOCATOR_MISMATCH_BLOCKED` (header insert shifts offsets & hash) |
| `REF-02` | hash/locator 불일치 차단 | `COVERED_SYNTHETIC_STUB` | `refcase-02b` (`REF-08`) | `rev-src02-v1-2cb25379@89:271` (`loc-ref-base-src02-89_271-e3bf184f`) | `DRAFT_REFETCH_HASH_MISMATCH_NEW_REVISION_BLOCKED_NO_AUTO_MUTATION` (appended revision hash mismatch; Post-MVP auto-mutation disabled) |
| `REF-03` | 재조회 불가와 삭제·거짓 구분 | `COVERED_SYNTHETIC_STUB` | `refcase-03a` (`REF-03`) | `rev-src02-v1-2cb25379@89:271` (HTTP 404 stub) | `DRAFT_REFETCH_UNAVAILABLE_DISTINGUISHED_FROM_DELETED_AND_FALSE` (`unverifiable_now=true`, `deleted_confirmed=false`, `falsified=false`) |
| `REF-03` | 재조회 불가와 삭제·거짓 구분 | `COVERED_SYNTHETIC_STUB` | `refcase-03b` (`REF-04`) | `rev-src02-v1-2cb25379@89:271` (HTTP 206 partial stub) | `DRAFT_REFETCH_PARTIAL_DISTINGUISHED_FROM_DELETED_AND_FALSE` (`unverifiable_now=true`, `deleted_confirmed=false`, `falsified=false`) |
| `REF-04` | 원문 비영속 | `PARTIAL_COVERAGE_GAP` | `refcase-04a` (`REF-07`) | `rev-src_ref07_canary-v1-7e7ec30a@147:221` (`loc-ref-base-ref07-147_221-374b3bbe`) + canary `CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST` | `DRAFT_REFETCH_STATIC_CANARY_NON_PERSISTENCE_PASS` (static snapshot 0 leaks + negative leak fixture caught; **coverage gap**: live DB/logs/queues/caches/disk audit) |
| `REF-05` | 접근 권한·허용된 URL 경계 | `PARTIAL_COVERAGE_GAP` | `refcase-05a` (`REF-05`) | `rev-src02-v1-2cb25379@89:271` (HTTP 403 `POLICY_OAUTH_SCOPE_REVOKED`) | `DRAFT_REFETCH_POLICY_ACCESS_DENIED` (**coverage gap**: live OAuth/tenant boundary) |
| `REF-05` | 접근 권한·허용된 URL 경계 | `PARTIAL_COVERAGE_GAP` | `refcase-05b` (`REF-06`) | `http://169.254.169.254/...` + 5 unsafe URL vectors (`fetch_attempted=false`) | `DRAFT_REFETCH_UNSAFE_URL_BLOCKED_PREFETCH` (**coverage gap**: live 30x redirect SSRF & DNS-rebinding) |
| `REF-06` | 중단·재실행 시 원문 저장에 의존하지 않음 | **`COVERAGE_GAP`** | *(none)* | *(unverified — requires worker crash/checkpoint/resume harness)* | **`COVERAGE_GAP` (`verified_in_draft=false`)** — not faked by renumbering |
| `REF-07` | 승인·Export·근거 열람 시 재조회 | **`COVERAGE_GAP`** | *(none)* | *(unverified — requires product Approval / Export / Evidence-Viewer E2E)* | **`COVERAGE_GAP` (`verified_in_draft=false`)** — not faked by renumbering |
| `REF-08` | 비용·크기 한도와 단계 간 메모리 전용 처리 | **`COVERAGE_GAP`** | *(none)* | *(unverified — requires runtime byte/cost cap & in-memory inter-stage transport test)* | **`COVERAGE_GAP` (`verified_in_draft=false`)** — not faked by renumbering |

---

## 3. Independent Human Review Queue (`REVIEW_REQUIRED` & T-004B Handoff)

1. **Normalization & Offset Contract (`REVIEW_REQUIRED`)**: Confirm production normalization (`fixture-nfc-lf-v0-draft`) and offset unit (`char` vs `utf8_byte` vs `utf16`) against Notion specs.
2. **Product Status Enum Mapping (`REVIEW_REQUIRED`)**: Map provisional `DRAFT_CLAIM_*`, `DRAFT_ATOM_*`, and `DRAFT_REFETCH_*` verdicts onto finalized product state enums before T-004B golden freeze.
3. **User-Edit Re-Verification Lifecycle (`SCN-12`)**: Confirm how `claim-12-2` transitions once `v2` completes re-verification against `fact-12-1`, and confirm `claim-12-1` conflict handling on failed `v2` re-verification.
4. **Refetch Gap Closure Plan (`REF-04`..`REF-08`)**: Assign backend/worker/E2E QA tasks to close runtime gaps in `REF-04`, `REF-05` and full `COVERAGE_GAP`s in `REF-06`, `REF-07`, `REF-08`.
5. **Semantic Rubric Confirmation**: Review `SCN-06`/`SCN-09`/`SCN-14` conflict precedence, `SCN-04`/`SCN-08` `CONTEXT`-only ungrounded handling, `SCN-11` identity ambiguity policy, and `SCN-13` vs `REF-03` deletion-vs-unavailable UX distinction.
