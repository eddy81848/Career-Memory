# T-007A / T-007B / T-007C / T-007E 로컬 Worker·초기가져오기·방안 C 재검증 프로토타입 (spike, 제품 코드 아님)

Cloud Run 방식 결정(OD-012), 비동기 실행·복구·재시도(`T-007A`, `T-007C`), 초기가져오기 페이지/체크포인트 원자성(`T-007B`), 그리고 **원문 비영속 참조 재검증 방안 C(`T-007E`)**를 위한 격리된 **로컬 증거**입니다.

> [!IMPORTANT]
> - 제품 API 계약, 운영 스키마/Migration, 프론트엔드, AUTHDB 테스트 DB 설정과 무관합니다. 이번 결과는 **제품 전체 경로 검증이나 `B-004` 완료가 아닙니다.**
> - 경로는 `/_prototype/t007a/*`(API)와 `/internal/t007a/*`(Worker)이고, 모든 API 응답에 `X-T007A-Prototype: not-product-api` 헤더가 붙습니다. 테이블은 일회성 DB의 별도 스키마 `t007a_spike`에만 만듭니다.
> - 클라우드 리소스(Cloud Run, Artifact Registry, Cloud Tasks, Scheduler, IAM, Supabase 프로젝트)는 하나도 만들거나 연결하지 않았으며, 실제 GitHub·LLM 호출도 하지 않습니다(고정/합성 로컬 응답만 사용). **호스팅된 Supabase PostgreSQL 연결·pooler·Cloud Run IAM·cold start·비용·승인은 모두 미검증(미실행)입니다.**

## 구성 파일

| 파일 | 역할 |
|---|---|
| [t007a/schema.sql](t007a/schema.sql) | 단일 작업(`jobs`, `outbox`, `results`, `job_attempts`) + 초기가져오기(`import_runs`, `import_stream_checkpoints`, `import_page_items`, `import_ready_outbox`) + 방안 C(`source_bindings`, `career_claims`, `claim_evidence_refs`, `claim_approvals`, `claim_exports`, `verification_events`; 원문 컬럼 없음) |
| [t007a/api.py](t007a/api.py), [t007a/worker.py](t007a/worker.py), [t007a/relay.py](t007a/relay.py), [t007a/wake.py](t007a/wake.py) | `T-007A` / `T-007C` 접수·선점·fencing 저장·만료 수거·불확실 timeout `REVIEW_REQUIRED` 격리 |
| [t007a/import_run.py](t007a/import_run.py) | `T-007B` 페이지 결과 + 체크포인트 원자 저장, `lease_token` + `checkpoint_version` 이중 fencing, 안전 재개 게이트, 단일 `IMPORT_READY` 확정 |
| [t007a/option_c.py](t007a/option_c.py) | `T-007E` 방안 C: `(revision_id, locator, expected_hash)` 참조 기반 승인·Export 매 시점 DB 트랜잭션 밖 실시간 재조회, 실패 분류, 저장 직전 내부 상태/버전 TOCTOU 재검사, 원문 표식 `0`건 스캐너 |
| [tests/test_vertical.py](tests/test_vertical.py), [tests/test_processes.py](tests/test_processes.py) | `T-007A` 16개 수직·프로세스 테스트 |
| [tests/test_t007c.py](tests/test_t007c.py) | `T-007C` 9개 만료 수거·재시도·격리 테스트 |
| [tests/test_t007b_import.py](tests/test_t007b_import.py) | `T-007B` 8개 페이지/체크포인트·늦은 쓰기·실제 `SIGKILL`·`IMPORT_READY` 경쟁 테스트 |
| [tests/test_t007e_option_c.py](tests/test_t007e_option_c.py) | `T-007E` 15개 방안 C 승인/Export 재검증·실패 분류·TOCTOU 경쟁·원문 비영속(`0`건 검출) 테스트 |
| [tests/test_r05_containers.py](tests/test_r05_containers.py) | `RUNTIME-05` 기존 [Dockerfile](Dockerfile) 단일 이미지 빌드 후 API·Worker 컨테이너 분리 기동, 정상 수렴 및 Worker 컨테이너 일시 중단·재기동·복구 검증 (`t007a-r05-*` 전용 리소스 자동 정리) |
| [T007F_HOSTED_EXPERIMENT_PLAN.md](T007F_HOSTED_EXPERIMENT_PLAN.md) | `T-007F` 최소 호스팅 실험 설계(권한·cold start·timeout/종료 재개·Supabase Pooler), Cloudflare Proxy vs Tunnel 비교, 무료 범위 안전 게이트(**현재 `NO-GO`**) |
| [evidence/t007a.json](evidence/t007a.json), [evidence/t007c.json](evidence/t007c.json), [evidence/t007b.json](evidence/t007b.json), [evidence/t007e.json](evidence/t007e.json), [evidence/latest.json](evidence/latest.json) | 단계별 보존된 실행 증거(`16` → `25` → `33` → `48` → 컨테이너 포함 최신 `49`건) |

## 로컬 재현

```bash
cd spikes/t007a-worker
uv venv .venv && uv pip install -p .venv -r requirements.txt

# 실제 로컬 일회성 PostgreSQL + 로컬 Docker 컨테이너 전체 테스트 (총 49건)
unset T007A_TEST_DSN T007A_DATABASE_URL
.venv/bin/python -m pytest -v -p no:warnings
```

## T-007E (방안 C) 설계 경계 및 REF-01 / 02 / 03 / 04 / 07 매핑

- **원문 비영속과 실시간 재검증**: Claim 근거(`claim_evidence_refs`)는 원본 `revision_id`, `locator`, 64자 `expected_hash`(SHA-256)만 저장합니다. 승인과 Export 모두 DB 트랜잭션 밖에서 합성 Provider 원문을 읽어 해시만 비교한 뒤 원문 참조를 즉시 버립니다. 과거 승인이나 직전 Export 성공이 이후 Export의 재검증을 절대 생략하지 않습니다.
- **실패 분류 및 부작용 금지**:
  - `EVIDENCE_MISMATCH`(`HASH_MISMATCH`, `LOCATOR_NOT_FOUND`): 원문 변경 또는 위치 소실.
  - `POLICY_OR_AUTH_BLOCKED`(`PROVIDER_403_FORBIDDEN`, `NON_PUBLIC_RESOURCE_BLOCKED`, `SOURCE_AUTH_REVOKED*`, `SOURCE_POLICY_BLOCKED*`): 권한 회수·비공개·정책 차단.
  - `REFETCH_UNAVAILABLE`(`PROVIDER_404_UNCONFIRMED`, `PROVIDER_429_RATE_LIMITED`, `PROVIDER_TIMEOUT`, `INCOMPLETE_PROVIDER_PAYLOAD`): 재조회 불가. **단일 `404`는 `UNVERIFIED_404`로만 기록하며 영구 삭제로 확정하지 않습니다**(이후 `200` 복구 시 Export 정상 통과 확인).
  - 필수 근거 중 하나라도 미검증이면 신규 `APPROVED` 기록과 `claim_exports` 생성·게시는 모두 `0`건이며, Claim을 거짓으로 판정하거나 `expected_hash`를 자동 갱신하지 않습니다.
- **재조회 ~ 최종 저장 사이 TOCTOU 한계 명시**: 최종 커밋 트랜잭션에서 `career_claims`와 `claim_evidence_refs`, `source_bindings`를 `FOR UPDATE`로 다시 잠가 `claim_version`, `approved_version`, `auth_status`, `policy_status`, `binding_version`을 재검사합니다. **이 검사는 재조회 도중 발생한 시스템 내부의 권한 회수·정책 차단·Claim 수정 경쟁을 막는 것이며, 재조회 직후 외부 GitHub 원문 자체가 그 찰나에 변경되지 않는다는 물리적 보장은 아닙니다.**

| 기준 ID | 검증 내용 | 스파이크 구현·테스트 근거 |
|---|---|---|
| `REF-01` | 원문 비저장 포인터(`revision_id`, `locator`, `expected_hash`) 연결 | `claim_evidence_refs` 스키마 및 `create_claim_with_refs` |
| `REF-02` | 승인 시점 DB 트랜잭션 밖 실시간 재조회 및 전체 필수 근거 일치 검증 | `verify_and_approve_claim` (`pg_stat_activity` 열린 트랜잭션 `0`개 검증) |
| `REF-03` | Export 시점 과거 승인/검증 캐시 무효, 매회 실시간 재조회 강제 및 불일치/불가 시 게시 `0`건 | `verify_and_export_claim`, 승인·1차 Export 후 원문 변조 시 2차 Export 즉시 차단 |
| `REF-04` | 실패 원인 분류(`403`/정책 vs `404`/`429`/timeout/불완전 vs 불일치), 단일 `404` 비확정, 내부 상태 변경 TOCTOU 차단, 자동 갱신·거짓 판정 금지 | `test_failure_taxonomy_*` (8종), `test_race_after_successful_refetch_before_save_*` (5종) |
| `REF-07` | DB 전체 테이블·`jobs`/`outbox` payload·로그·메모리 감사 구조·생성 파일 전체에서 런타임 합성 원문 표식 검출 `0`건 | `scan_db_for_marker_hits` + 로그/캐시/파일 스캔 (`db=0, log=0, cache=0, file=0`) |

## 남은 계약·후속 리스크 및 미검증 항목

- **제품 전체 경로 및 `B-004` 미완료**: 본 검증은 로컬 합성 Provider 기반 런타임 스파이크이며, 실제 GitHub API 연동·렌더러·감사 정책·UI/API 계약을 포함한 제품 전체 경로 검증이나 `B-004` 완료가 아닙니다.
- **제품 `Idempotency-Key` 범위·보존 기간 (`T-105` / `T-106` 승인 대기)**, **외부 부작용 송신 직후 프로세스 종료 위험 및 `REVIEW_REQUIRED` 수동 해제**는 별도 후속 항목으로 유지합니다.
- **호스팅 환경(Cloud Run + Supabase PostgreSQL) 미실행 항목 (`NO-GO` 유지)**: 로컬 Docker 단일 이미지 API/Worker 기동·중단·복구(`tests/test_r05_containers.py`)까지만 확인했으며, Artifact Registry 업로드, 호스팅된 **Supabase PostgreSQL** 실제 연결 및 Supavisor pooler(`:6543`), `T-007D` RLS 역할 결합, Cloud Run IAM/OIDC, scale-to-zero cold start, 호출자 timeout/`SIGTERM`, 계정별 잔여 무료 한도·배포 승인은 **실행하지 않았습니다(미검증, [T007F_HOSTED_EXPERIMENT_PLAN.md](T007F_HOSTED_EXPERIMENT_PLAN.md) 기준 `NO-GO`)**.
