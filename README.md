# Career Memory

**Career Memory**는 공개 GitHub 저장소 활동(PR, 커밋, 리뷰, 코멘트)을 **Plan C (원문 비저장 · 온디맨드 Refetch + SHA-256 검증)** 아키텍처로 구조화하여, 근거 기반의 `WorkEvent` → `Fact` → `Claim` → 무인용(Zero-Quote) Export 및 FTS 검색/삭제/쿼터 운영까지 일관되게 제공하는 커리어 기억 시스템입니다.

---

## 1. 아키텍처 개요

| 구성 요소 | 경로 | 역할 |
|---|---|---|
| **Backend API & Worker** | `apps/api` | FastAPI + PostgreSQL 18 (`cm_api_app` / `cm_worker_app` `NOBYPASSRLS` + `FORCE RLS`), ES256 JWT 검증, Plan C Refetch Resolver, M1–M5 전체 도메인 라우터 |
| **Web Application** | `apps/web` | Next.js 15 + React 19 UI (`/` Import · `/event-review` Event Review · `/claim-review` Claim Review & Plan C Viewer & Export · `/search` FTS Search, OD-003 Deletion & Quotas) |
| **Database Migrations** | `db/migrations/0010..0080` | 46개 테이블 전체 `FORCE RLS`, Plan C 비저장 제약(`text_content IS NULL`, `raw_storage_mode = 'NONE'`), FTS 인덱스, 삭제 펜스 및 쿼터 원장 |
| **Contracts & Evidence** | `contracts/` | 불변 M0 OpenAPI 스냅샷(`07594d75425744d6149250a9f7ff95ac24659f80`), 통합 OpenAPI, AI JSON Schema, B-004/T-306A/T-407A/T-408/T-504A/T-505/T-506/T-507 증빙 번들 |
| **Local Fullstack Launcher** | `scripts/run_local_fullstack.py` | PG18 + FastAPI + Background Worker + Next.js Same-Origin 프록시 원커맨드 실행 및 `--smoke-check` |

### Plan C 핵심 불변성
- DB 46개 테이블 어디에도 GitHub 원문 본문(`text_content`, `content_ref`, `extracted_span`, 금지 JSONB 키)을 영속화하지 않습니다.
- 원문 열람(`GET /v1/content-units/{id}/content`), Claim 승인(`APPROVAL`), Export 생성(`EXPORT_DOWNLOAD`) 시점에 서버 소유 Provider ID로만 온디맨드 Refetch 및 SHA-256 무결성 검증을 수행합니다.
- `career_memory_api.app:create_app()` 기본값은 항상 Fail-Closed(`UnconfiguredWorkspaceAccessProvider`)이며, 로컬 부트스트랩(`/local-dev/bootstrap`)은 로컬 개발 런처 앱(`create_local_dev_fullstack_app`)에만 마운트됩니다.

---

## 2. 원커맨드 로컬 풀스택 실행 (`₩0` · 외부 네트워크 0건)

### 사전 요구사항
- Python 3.12+ & [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL 18 로컬 바이너리 (`initdb`, `pg_ctl`, `createdb`, `pg_dump`, `pg_restore`)
- Node.js 22+ & `npm` (`cd apps/web && npm ci`)

### 1-Command 실행 (`local_sample` 모드 — 기본값)
```bash
cd apps/api
uv run python ../../scripts/run_local_fullstack.py
```
실행 후 브라우저에서 **`http://127.0.0.1:3100`** 을 엽니다:
- `/` — Initial GitHub Public Repository Import (`seoyeon-dev/ledger-engine` 완료 상태 + `seoyeon-dev/search-gateway` 실시간 Import 트리거 가능)
- `/event-review` — WorkEvent Inbox & Curation (`Keep/Edit/Reject/Split/Merge/Dissolve`)
- `/claim-review` — Grounded Fact & Claim Review, Plan C Ephemeral Source Viewer, Zero-Quote Markdown/JSON Export
- `/search` — PG18 FTS Search (`OD-008`), `OD-003` Source Deletion Preview/Confirm/Receipt, Workspace Quota Status (`T-503`)

### 주요 런처 옵션
```bash
# 전체 스택 기동 -> Same-Origin HTTP/SSR E2E 스모크 검증 -> 클린 종료
cd apps/api && uv run python ../../scripts/run_local_fullstack.py --smoke-check

# 로컬 PG18 데이터 영속화 디렉터리 지정
cd apps/api && uv run python ../../scripts/run_local_fullstack.py --pgdata-dir ~/.career-memory-pg18

# API + Worker + PG18 단독 실행
cd apps/api && uv run python ../../scripts/run_local_fullstack.py --api-only
```

---

## 3. 선택적 `live_public_github` 모드 (실제 공개 GitHub 저장소 연동)

기본값은 외부 네트워크 호출이 전혀 없는 `local_sample` 모드입니다. 실제 공개 GitHub 저장소를 읽기 전용(`read:user`)으로 연동하려면 아래 환경 변수를 설정하고 `--github-mode live`로 실행하세요:

```bash
export CAREER_MEMORY_GITHUB_PROVIDER_MODE=live
# 선택: GitHub OAuth App 사용 시
export GITHUB_CLIENT_ID="<your-oauth-app-client-id>"
export GITHUB_CLIENT_SECRET="<your-oauth-app-client-secret>"
# 또는 로컬 Public-Read 전용 토큰 사용 시 (write/repo 스코프 자동 거부)
export GITHUB_PUBLIC_READ_TOKEN="<read-only-public-token>"

cd apps/api
uv run python ../../scripts/run_local_fullstack.py --github-mode live
```

---

## 4. 전체 검증 명령어

```bash
# 1. 원커맨드 풀스택 스모크 체크 (PG18 + FastAPI + Worker + Next.js Same-Origin)
cd apps/api && uv run python ../../scripts/run_local_fullstack.py --smoke-check

# 2. 전체 백엔드 단위 + PostgreSQL 18 통합 테스트
cd apps/api && uv run pytest -v -m ""

# 3. T-106/T-106A Conformance + PG18 카탈로그 검증
cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres

# 4. T-504A Cross-Subsystem Fault Matrix & T-506 3-Tier Release Gate 검증
cd apps/api && uv run python ../../scripts/run_t504a_fault_matrix.py --verify
cd apps/api && uv run python ../../scripts/run_t506_pilot_gate.py --verify

# 5. M0 CONTRACT-06 Initial Import 회귀 테스트 (38 tests)
cd apps/api && uv run --with-requirements ../../spikes/import-contract/requirements.txt \
  python -m pytest -v ../../spikes/import-contract/test_contract.py

# 6. apps/web 타입체크, Vitest 단위/실제 PG18 Contract-HTTP, 프로덕션 빌드, Playwright E2E
cd apps/web && npm run typecheck && npm test && npm run test:contract-http && npm run build && npm run test:e2e
```

---

## 5. 3-Tier Release Gate 현황 (ADR-018 / ADR-019 / ADR-020)

| Tier / Track | 상태 | 비고 |
|---|---|---|
| **Track 1 — Local Deterministic MVP Fullstack (`LOCAL_DETERMINISTIC_MVP_GATE`)** | **GO (`PASSED`)** | PG18 + FastAPI + Worker + `apps/web` 전 여정 검증, `₩0`, Plan C 누수 0건 |
| **Track 2 — Cloud Staging / Hosted Closed Pilot (`PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE`)** | **HELD (`NO-GO`)** | `OD-013`, `OD-015` 운영자 승인 및 클라우드 Origin mTLS/IAM/백업/예산 상한 프로비저닝 대기 |
| **Track 3 — Live Subscription AI Pilot (`LIVE_SUBSCRIPTION_AI_PILOT_GATE`)** | **HELD (`NO-GO`)** | `OD-002`, `OD-014` 외부 모델 데이터 처리 동의 및 외부 암호화 토큰 볼트 프로비저닝 대기 |
