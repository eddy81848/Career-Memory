# T-007D Hosted Verification Runbook (OD-006 / OD-010)

> **문서 상태 및 범위 경계**
> - **현재 게이트 상태**: 호스팅 실행 **`NO-GO`** 유지, **OD-006 / OD-010**은 **`Validation Required`** 상태를 유지한다.
> - **호스팅 실행 승인과 OD 결정 구분**: 진행 순서는 **무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인 → M0 후보 시험 → 결과를 근거로 OD-006/010 채택 검토**다. **OD-006/010 채택을 M0 후보 시험의 선행 조건으로 쓰지 않는다.**
> - 이 문서는 향후 격리 호스팅 실행 승인 시 수행할 **M0 후보 시험**(관리형 풀러·RLS·Data API·HTTPS 세션 후보) 절차를 정리하며, 그 결과를 바탕으로 OD-006/010 채택을 검토한다.
> - **M0 격리 스파이크 검증**과 **M1/M2 제품 통합 검증**(실제 제품 API·최초 가져오기 4개 경로·다중 구문 DB 쓰기·Job/outbox·멱등 기록·멤버십 회수 쓰기 경합)은 서로 다른 완료 조건이며, 제품 구현이 필요한 시험을 M0 선행 작업으로 당기지 않는다.
> - **오류 계약 범위**: `CONTRACT@87f1515cbe74bf02b0aece68904bf9a641c74c60` (`contracts/import-v1/openapi.yaml`)에서 **최초 가져오기 4개 경로에 한해** 승인된 4종 응답(`401 SESSION_TERMINATED`, `403 WORKSPACE_SCOPE_VIOLATION`, `404 RESOURCE_NOT_FOUND`, `503 AUTH_VERIFICATION_UNAVAILABLE`)만 확정 규약으로 인용한다. **일반 `401`/`403`/`422`, 승인되지 않은 기타 `409`, Source Account/Resource 응답 및 `Retry-After`는 본 문서에서 확정하지 않는다.**
> - **Next.js 서버 경유**와 **`HttpOnly`/`Secure` 쿠키**는 확정 사양이 아니라 **OD-010 검증 후보**로 유지한다.

---

## 0. 비용 · 무료 한도 · 외부 리소스 가드레일 (호스팅 `NO-GO`)

1. **사전 확인 필수 (격리 호스팅 실행 승인 ≠ OD 채택)**: 호스팅은 현재 **`NO-GO`** 상태다. **무료 잔여량**(Supabase 무료 프로젝트 수·동시 연결 수·egress 한도, Cloud Run 무료 할당량·과금 조건), **계정 권한**, **테스트 후 리소스 정리 여유**를 확인하고 **격리 호스팅 실행 승인**을 받기 전에는 어떤 외부 리소스(호스팅 Supabase 프로젝트, Cloud Run 서비스, 외부 OAuth/GitHub App)도 생성하거나 연결하지 않는다. 이 승인은 M0 후보 시험 실행을 위한 것이며, OD-006/010 채택 검토는 M0 후보 시험 결과를 근거로 그다음에 수행한다.
2. **중단 및 로컬 대안 원칙**: 비용 발생 가능성이나 무료 한도가 불명확하면 **호스팅 검증을 즉시 중단**하고, 아래 로컬 증거 범위만 인용·유지한다.
   - **DB 역할 · RLS · 복합 FK · 로컬 단일 연결 재사용**: 로컬 PostgreSQL 18 (`bash spikes/t007d-auth-db/scripts/run_local_verification.sh`, **AUTHDB-04 실행 기록 기준 27 passed / 0 failed / 0 skipped** — 본 문서 동기화에서 재실행하지 않음)
   - **Data API(PostgREST) · GoTrue · 로컬 실제 JWT→FastAPI E2E 사전 점검**: 일회성 로컬 Supabase CLI(`t007d-authdb-ephemeral`) 스택에서 AUTHDB-03/04 당시 오프라인 재현 기록(`test_local_supabase_integration.py`, `test_local_supabase_fastapi_e2e.py`; 실제 Next.js·HTTPS 로그인, 관리형 Supavisor, Cloud Run은 여전히 미검증)
   - **쿠키 · Origin/CSRF 흐름 사전 점검**: 로컬 BFF 프로토타입(`tests/test_bff_session_csrf.py`; 실제 Next.js·HTTPS 로그인·호스팅 Supabase Auth JWKS·Cloud Run 프록시 헤더·관리형 Supavisor 동작은 여전히 호스팅 미검증)

---

## 1. Supabase 공유 Transaction Pooler 및 `psycopg` 설정 후보 (OD-006)

- **런타임 풀러 후보 (API · Worker)**: Supabase 공유 **Transaction Pooler (`:6543`)** 경로를 후보로 둔다.
- **`psycopg` prepared statement 비활성화 후보 규칙**:
  - `psycopg` 드라이버에서 `:6543` Transaction Pooler를 사용할 때는 반드시 **`prepare_threshold=None`** 으로 서버 측 prepared statement를 끈다.
  - **`prepare_threshold=0`은 대안에서 제외한다.** (`psycopg` v3에서 `0`은 비활성화가 아니라 *첫 실행부터 모든 쿼리를 즉시 prepare*하는 설정이므로 Transaction Pooler와 충돌한다.)
  - 사용하지 않는 다른 드라이버/ORM 설정은 동등한 대안으로 제시하지 않는다.
- **후보 설정과 실제 호스팅 검증의 구분**:
  - 위 `prepare_threshold=None` + `:6543` 조합은 **설계·검증 후보**이며, **실제 호스팅 Supavisor에서는 아직 미검증(`NO-GO`)**이다.
  - 로컬 스파이크의 `src/db.py::make_single_connection_pool`은 로컬 PostgreSQL 18 직접 연결용이며 `prepare_threshold=None`을 아직 설정하지 않았고 Supavisor 풀러 시험이 아니다.
- **세션 상태 금지**: Transaction Pooler 경로에서는 세션 수준 `SET`(`set_config(..., false)`), `LISTEN/NOTIFY`, `TEMP TABLE`, 세션 advisory lock을 금지하고, 모든 테넌트 범위는 트랜잭션 내부 `SELECT set_config('app.current_workspace_id', %s, true)`로만 지정한다.
- **마이그레이션 경로 분리**: DDL 및 스키마 마이그레이션은 런타임 풀러가 아닌 **Session Pooler (`:5432`) 또는 Direct 연결**에서 CI 전용 `migrator` 역할로만 수행한다.

---

## 2. OD-006 검증표 (`Validation Required` — M0 격리 스파이크 vs M1/M2 제품 통합 분리)

> **단계 경계**: 아래 표의 `호스팅에서 미실행 (M0 격리 스파이크)` 항목은 **관리형 DB 역할·풀러·RLS·Data API 경계 확인**이며, 실제 최초 가져오기 4개 경로의 **다중 구문 DB 쓰기·Job/outbox·멱등 기록·쓰기 중 멤버십 회수 경합**은 §4.3의 **M1/M2 제품 통합 검증**으로 분리한다(M0 선행 조건으로 당기지 않는다).

| ID · 검증 항목 | 로컬에서 확인됨 (AUTHDB-04 기록) | 호스팅에서 미실행 (M0 격리 스파이크, `NO-GO`) | 선행 조건 | 실행 절차 | 통과 기준 | 실패 시 대안 | 남길 증거 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **OD006-1. API · Worker 최소권한 DB 역할** | `t007d_api_app`·`t007d_worker_app`의 `NOSUPERUSER`/`NOBYPASSRLS`/`NOINHERIT`, 테이블 비소유, `SET ROLE t007d_migrator`·`SET row_security = off`·DDL 거부, Worker의 `users`/`workspace_memberships` 비접근 및 `DELETE` 거부 확인 (`test_db_isolation_and_fk.py`) | 스테이징 Supabase DB에 최소권한 `api_app`/`worker_app` 역할 생성 후 Cloud Run 자격 증명으로 `pg_roles` 속성 및 권한 거부 재현 | 무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인(OD 채택 별개), 스테이징 Supabase 프로젝트 및 역할 SQL 준비 | 1) `api_app`·`worker_app` 각각으로 스테이징 DB 접속<br>2) `pg_roles`와 `pg_class` 소유자 조회<br>3) `SET ROLE migrator`, `SET LOCAL row_security = off`, `CREATE TABLE`, 비허가 테이블 `SELECT`/`DELETE` 시도 | 런타임 역할 모두 `rolsuper=false`, `rolbypassrls=false`, 테이블 비소유이며 금지 연산이 전부 DB 권한 오류로 거부됨 | 누락된 `REVOKE`·`NOINHERIT`·`NOBYPASSRLS` 보완 및 테이블 소유자를 `migrator`로 재지정; 호스팅 비용 불명확 시 로컬 PG 18 감사 기록으로 대기 | `pg_roles`/`pg_class` 감사 결과(계정 비밀번호 제외), 금지 SQL 실행 시 DB 오류 코드 로그 |
| **OD006-2. 트랜잭션 안에서만 유효한 Workspace 범위** | 로컬 PG 18 직접 연결에서 `SELECT set_config('app.current_workspace_id', %s, true)`의 트랜잭션 한정 적용 및 미설정/빈 문자열/비UUID 값 fail-closed(0행) 확인; `set_config(..., false)` 세션 누출 반례 실측 (`test_db_isolation_and_fk.py`, `test_pool_reuse.py`) | 호스팅 Supavisor Transaction Pooler(`:6543`) + `psycopg(prepare_threshold=None)` 후보 경로에서 트랜잭션 단위 `set_config(..., true)` 동작 및 prepared statement 충돌 부재 확인 | 무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인, 스테이징 API/Worker에 `psycopg` `prepare_threshold=None` 적용 | 1) `:6543` 경유 동일 파라미터화 쿼리 반복 실행<br>2) 트랜잭션 내에서만 `set_config(..., true)` 호출 후 조회<br>3) `set_config` 미호출 트랜잭션에서 동일 테이블 조회 | prepared statement 중복 오류 0건, 트랜잭션 내부에서는 해당 Workspace 행만 반환, 미설정 트랜잭션은 0행(fail-closed) | Transaction Pooler 호환 이슈 발생 시 커넥션 옵션(`prepare_threshold=None`)·트랜잭션 경계 재점검; 미해결 시 Session Pooler 전환 비용·동시성 한도를 오케스트레이터에 재보고 | `psycopg` 연결 설정 스냅샷(`prepare_threshold=None`), 반복 쿼리 성공·미설정 0행 실행 로그 |
| **OD006-3. 연결 재사용 · `ROLLBACK` 뒤 범위 초기화** | 로컬 PG 18 직접 연결 `psycopg_pool(max_size=1)` 단일 물리 연결(`pg_backend_pid()` 동일)에서 `WS_A → unscoped → WS_B → WS_A rollback → unscoped` 및 HTTP 순차 요청 누출 0건 확인 (`test_pool_reuse.py`) | 호스팅 Supavisor Transaction Pooler(`:6543`)에서 동시 교차 요청(WS_A/WS_B), 트랜잭션 강제 `ROLLBACK`, TLS(`sslmode=verify-full`), 연결 한도·타임아웃(`statement_timeout`, `idle_in_transaction_session_timeout`) 검증 | 무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인, 스테이징 풀러 CA 번들 탑재 및 합성 Workspace A/B 시드 준비 | 1) 풀 크기를 제한한 클라이언트에서 WS_A 조회 → 강제 예외 `ROLLBACK` → unscoped 조회 → WS_B 조회를 연속·동시 실행<br>2) `sslmode=verify-full` 접속 및 `sslmode=disable` 거부 확인<br>3) 타임아웃 초과 쿼리 종료 후 다음 요청 격리 확인 | 모든 교차·롤백 이후 타 Workspace 행 누출 0건, unscoped 조회 0행, TLS 검증 성공, 풀 고갈 없음 | 롤백 후 잔존 징후 발견 시 커넥션 반환 훅에 트랜잭션 상태 검사 추가 및 세션 수준 설정 호출 제거; 로컬 대안은 로컬 PG 18 단일 연결 기록으로 한정 표기 | 동시·롤백 시나리오 테스트 리포트, TLS 검증 로그, 타임아웃·풀러 연결 수 관측치 |
| **OD006-4. 다른 Workspace의 조회 · 쓰기 · 참조 차단** | 직접/부모 경유/자식 조회·수정·삭제 RLS 차단, `WITH CHECK` 위조 `workspace_id` INSERT 차단, 복합 FK `(workspace_id, project_id) -> projects(workspace_id, id)` 교차 참조 및 존재 오라클 차단 확인(단일 FK 취약점·RLS `EXISTS`·트리거 후보 비교 완료). *별도 FOUNDATION(`c1e728a`) 로컬 PG 시험은 회수 커밋 직후 보호 `SELECT` 0행만 확인* | 스테이징 격리 후보 스키마의 부모-자식 테이블에 `ENABLE`+`FORCE ROW LEVEL SECURITY` 및 복합 FK 적용 후 교차 Workspace 조회/쓰기/참조 차단 실측 *(실제 제품 스키마 전 테이블 및 다중 구문 쓰기 트랜잭션의 회수 경합은 M1/M2 제품 통합 검증에서 별도 수행)* | 격리 호스팅 실행 승인, 스테이징 격리 후보 스키마 마이그레이션 완료, 합성 Workspace A/B 부모·자식 데이터 준비 | 1) WS_B 컨텍스트에서 WS_A 부모·자식 직접/부모 경유 조회·수정·삭제 시도<br>2) WS_B 컨텍스트에서 `workspace_id=WS_A` INSERT 시도<br>3) WS_B 자식 행에 WS_A 부모 ID 참조 INSERT·`UPDATE` 시도(런타임 역할 + `migrator`/관리 경로 각각) | 타 Workspace 조회 0행, 수정/삭제 0행, 위조 쓰기 거부, 교차 부모 참조는 런타임·관리 경로 모두 복합 FK(`23503`)로 거부되며 미존재 ID와 구별 불가 | 단일 FK가 발견된 자식 테이블에 즉시 복합 FK 마이그레이션 추가; `FORCE ROW LEVEL SECURITY` 누락 테이블 보완 | `pg_constraint` 복합 FK 목록, `pg_class` RLS/FORCE RLS 목록, 교차 조회·쓰기·참조 차단 실행 로그 |
| **OD006-5. 브라우저의 Supabase Data API(PostgREST) 도메인 쓰기 우회 차단** | 로컬 PG 18 역할 계층(`test_od006_5_data_api_isolation.py`) 및 일회성 로컬 Supabase CLI PostgREST(`/rest/v1/*`, `test_local_supabase_integration.py`)에서 AUTHDB-03 당시 확인: 동일 PostgREST에서 허용 canary(`public.t007d_postgrest_healthcheck`)가 `anon`·로그인 사용자 모두 `200` 성공함을 먼저 확인한 뒤, 도메인 테이블(`projects`, `artifacts`) 직접 `GET`/`POST`/`PATCH`/`DELETE` 전부 차단, DB 행 수 및 컬럼 값 100% 불변, 정상 서버 경유(`t007d_api_app`) 쓰기 성공 확인 | 스테이징 Supabase `/rest/v1/*` 엔드포인트에 브라우저 노출 `anon` 키 및 로그인 사용자 `authenticated` JWT로 직접 도메인 테이블 쓰기·조회 우회 시도 검증 | 무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인, 스테이징 Supabase 프로젝트의 API Exposed Schemas 설정 및 `anon`/`authenticated` 역할 권한 확인 | 1) Exposed Schemas 설정과 `information_schema.role_table_grants`에서 `anon`/`authenticated` 권한 조회<br>2) 허용 canary 양성 대조(`200`) 확인 후 브라우저 `anon` 키로 `/rest/v1/<domain_table>` `POST`/`PATCH`/`DELETE`/`GET` 직접 호출<br>3) 실제 로그인 사용자의 `authenticated` JWT로 동일 `/rest/v1/<domain_table>` 직접 쓰기·조회 호출 | 모든 도메인 테이블에 대해 브라우저 직접 Data API 쓰기(및 정책상 비허가 직접 조회)가 거부되고 대상 테이블 행 수·값 변경 0건 | 도메인 스키마를 PostgREST Exposed Schemas에서 제외하고 `anon`/`authenticated`의 도메인 테이블 권한을 전량 `REVOKE`; 필요 시 Data API 비활성화 | Exposed Schemas 설정 기록, `role_table_grants` 감사 출력, 토큰·키 마스킹된 canary `200` 및 `/rest/v1` 거부 응답 로그, DB 행 수·값 불변 증적 |
| **OD006-6. 런타임 역할과 Migration 역할 분리** | `t007d_migrator` 단독 소유, 런타임 역할의 `SET ROLE t007d_migrator`·DDL 불가, 런타임 코드 경로에 `migrator` DSN 미사용 확인 (`test_db_isolation_and_fk.py`) | CI/CD 전용 `migrator`(Session `:5432` 또는 Direct 연결)와 Cloud Run 런타임(`api_app`/`worker_app`, Transaction `:6543`)의 시크릿·IAM·네트워크 경로 분리 및 배포 후 자동 감사 검증 | 무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인, 스테이징 Secret Manager / CI 시크릿 분리 설계 준비 | 1) Cloud Run 서비스 환경 변수·시크릿 바인딩에 `migrator` 자격 증명 부재 확인<br>2) CI 파이프라인에서만 `migrator`로 마이그레이션 실행 후 `pg_class`/`pg_roles`/`pg_constraint` 감사 쿼리 실행<br>3) 런타임 컨테이너에서 DDL 시도 | 런타임 환경에 `migrator` 시크릿 0건, 런타임 DDL 거부, CI 마이그레이션 직후 RLS·`FORCE RLS`·복합 FK 감사 100% 통과 | 런타임 시크릿에 과권한 계정 혼입 시 즉시 시크릿 회전·IAM 범위 축소 후 재배포; 감사 실패 시 배포 파이프라인 자동 중단 | Cloud Run 시크릿 이름 목록(값 제외), CI 마이그레이션·감사 로그, 런타임 DDL 거부 로그 |

---

## 3. OD-010 검증표 (`Validation Required` — M0 세션 후보 검증 vs M1/M2 제품 통합 분리)

> **후보 및 계약 경계 명시**
> - **Next.js 서버 경유** 및 **`HttpOnly`/`Secure` 쿠키**는 OD-010 최종 확정안이 아니라 **검증할 후보**로 유지한다.
> - 아래 M0 표는 **관리형 Supabase Auth · HTTPS 세션/쿠키/CSRF 후보 자체의 검증**에 집중한다. 실제 최초 가져오기 4개 경로의 승인 오류 계약(`401 SESSION_TERMINATED`, `403 WORKSPACE_SCOPE_VIOLATION`, `404 RESOURCE_NOT_FOUND`, `503 AUTH_VERIFICATION_UNAVAILABLE`)·Job/outbox·멱등 기록 연동은 §4.3 및 §5의 **M1/M2 제품 통합 검증**으로 분리한다.
> - 일반 `401`/`403`/`422`, 승인되지 않은 기타 `409`, Source Account/Resource 응답 및 `Retry-After`는 본 문서에서 확정하지 않는다.
> - **서비스 로그인(Supabase Auth)과 GitHub 저장소 수집 권한(OD-011), LLM 플랜/쿼터 권한은 별개 경계**로 검증하며 상호 혼입을 금지한다.

| ID · 검증 항목 | 로컬에서 확인됨 (AUTHDB-04 기록) | 호스팅에서 미실행 (M0 세션 후보, `NO-GO`) | 선행 조건 | 실행 절차 | 통과 기준 | 실패 시 대안 | 남길 증거 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **OD010-1. 실제 Supabase 로그인 · 콜백 (Next.js 서버 경유 후보)** | 합성 BFF 로그인(`POST /bff/auth/synthetic-login`)이 서버 세션만 생성하고 응답 본문에 Bearer JWT를 노출하지 않음만 확인 (`test_bff_session_csrf.py`) | 스테이징 Supabase Auth + 실제 Next.js 서버 콜백 라우트에서 실제 HTTPS 로그인, PKCE(`code_verifier`/`code`), `state` 검증, 오픈 리다이렉트 차단 검증 | 무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인(OD 채택 별개), 스테이징 Supabase Auth Redirect URL 및 Next.js 서버 후보 배포(또는 승인된 HTTPS 스테이징 URL) | 1) 브라우저에서 Next.js 서버 로그인 시작 → Supabase Auth 인증 → 서버 콜백 라우트 리다이렉트 수행<br>2) 변조된 `state`/`code` 및 외부 도메인 `next` 파라미터로 콜백 재시도<br>3) 브라우저 네트워크·스토리지에 raw access/refresh 토큰 미노출 확인 | 정상 로그인 시 서버 측 세션 수립 완료; 변조 `state`/`code` 및 외부 리다이렉트는 거부되며 브라우저 JS 저장소에 raw 토큰 0건 | PKCE/콜백 리다이렉트에서 `SameSite` 충돌 시 콜백 쿠키 스코프·`SameSite=Lax` 재조정; Next.js 서버 경유 후보 부적합 판정 시 오케스트레이터에 대안 세션 전략 비교 보고 | 브라우저 HAR/네트워크 요약(토큰·쿠키 원문 제거), 변조 `state`/오픈 리다이렉트 거부 로그 |
| **OD010-2. JWT 검증 (JWKS · 클레임 · 관심사 분리)** | 합성 RS256 키쌍 기반 부정 케이스 거부 확인 (`test_jwt_and_membership.py`); **AUTHDB-04 기록**에서 일회성 로컬 Supabase 발급 실제 사용자 JWT를 FastAPI에 투입해 기존 `RS256` 전용 검증기와의 차이 재현 후, 고정 설정(`jwks_url`, `expected_issuer`, `expected_audience`, `allowed_algorithms` — 헤더 `alg`/토큰 클레임 미신뢰)으로 정상 통과 및 서명 변조·`anon` 키·오설정 알고리즘/`iss`/`aud` 거부 확인 (`test_local_supabase_fastapi_e2e.py`). *단, 로컬 관찰치(`ES256`, `jti` 부재)는 제품 전체 가정이 아님* | 스테이징 Supabase `/.well-known/jwks.json` 비대칭 키 조회·캐시·미등록 `kid` 회전 처리, 실제 환경의 서명 알고리즘·클레임(`iss`/`aud`/`exp`/`alg`) 검증, `anon`/`service_role` 토큰 거부 및 JWKS 장애/rate-limit 동작 검증 | 스테이징 Supabase 프로젝트의 JWT 서명 방식(비대칭 JWKS vs 레거시 HS256) 및 고정 설정 확인 완료 | 1) 실제 발급된 `authenticated` JWT로 백엔드 API 정상 검증<br>2) 서명 변조·잘못된 `iss`/`aud`·미등록 `kid`·`anon` 키·`service_role` 키로 동일 엔드포인트 호출<br>3) JWKS 캐시 만료/갱신 및 rate-limit 동작 확인 | 유효한 사용자 JWT만 통과; 변조·오클레임·미등록 `kid`·`anon`/`service_role` 토큰은 전부 거부되고 DB 테넌트 쿼리에 도달하지 않음 | JWKS 지연/장애 시 안전 캐시 유지 + fail-closed 정책 적용; 프로젝트가 레거시 HS256인 경우 비대칭 키 전환 일정과 검증 절차를 오케스트레이터에 보고 | JWKS 메타데이터(공개키 `kid`/`alg`만 기록, 토큰 원문 제외), 각 부정 케이스 거부 로그 |
| **OD010-3. 쿠키 · Origin / CSRF (HttpOnly / Secure 쿠키 후보)** | 로컬 HTTP 환경에서 `HttpOnly; SameSite=Lax` 쿠키 발급, 본문 JWT 미노출, `POST`의 누락/타 출처 `Origin` 및 누락/불일치 `X-CSRF-Token` 거부 확인 (`test_bff_session_csrf.py`) | 실제 HTTPS 브라우저 환경에서 `Secure` 및 `__Host-` 접두어 후보 쿠키 동작, `document.cookie`/`localStorage` 비노출, Cloud Run/CDN 뒤 `Origin`/`Host`/`X-Forwarded-Host` 정규화 및 cross-site 요청 차단 검증 | 스테이징 HTTPS 도메인·Next.js 서버 경유 후보·프록시 헤더 구성 확인 완료 | 1) 실제 브라우저에서 로그인 후 `Set-Cookie` 헤더(`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, `__Host-` 후보)와 `document.cookie`·`localStorage` 확인<br>2) 타 출처 페이지에서 `credentials: 'include'` `fetch` 및 폼 `POST` 전송<br>3) CSRF 토큰 누락/오값 요청 전송 | 브라우저 JS에서 세션·Bearer 토큰 접근 불가; 타 출처/비유효 CSRF 상태 변경 요청은 백엔드 API 도달 전 BFF 계층에서 전부 거부됨 | 프록시 뒤 `Origin`/`Host` 불일치 시 trusted proxy 헤더 화이트리스트 보정; `__Host-` 도메인 제약 충돌 시 `Secure; HttpOnly; SameSite=Lax; Path=/`로 범위 명시 후 재검토 | 마스킹된 `Set-Cookie` 속성 캡처, DevTools 스토리지 점검 기록, cross-site/CSRF 거부 로그 |
| **OD010-4. 세션 만료 (Access / Refresh 수명 및 동시 탭 갱신)** | 만료된 합성 JWT(`exp < now`)의 백엔드 API 거부만 확인 (`test_jwt_and_membership.py`); refresh 회전·만료 및 동시 탭 경합은 미실행 | 스테이징 Supabase Auth의 access JWT 만료 후 Next.js 서버(Middleware/Route Handler/RSC) 자동 갱신, 동시 다중 탭 refresh-token 회전 경합, refresh 만료 후 재인증 전환 검증 | 스테이징 Supabase Auth의 테스트용 짧은 JWT 만료 시간 설정 권한 확인 완료 | 1) 로그인 후 access JWT 만료 시점까지 대기 후 보호 페이지/API 호출<br>2) 만료 직후 2개 이상 브라우저 탭에서 동시에 요청 발사(refresh 회전 경합 확인)<br>3) refresh 토큰 만료/무효 상태에서 요청 시도 | 단일·동시 갱신 시 정상 세션 유지(오탐 로그아웃 없음), refresh 만료 시 보호 데이터 미노출 및 재인증 흐름 전환 | 동시 탭 refresh reuse-detection 충돌 발생 시 서버 측 단일 flight 잠금 또는 쿠키 갱신 경로 일원화(Middleware 단일 책임); 대안 불가 시 세션 저장소(서버 측 세션 스토어) 도입 검토 | 만료 전/후 갱신 타임라인 로그(토큰 원문 제외), 동시 탭 테스트 결과, refresh 만료 후 차단 기록 |
| **OD010-5. 로그아웃 후 기존 토큰 재사용 및 세션 확인 장애** | 합성 BFF/민감경로 fail-closed 테스트(`test_od010_5_6_session_revocation.py`), 로컬 GoTrue `scope=local` 검증(`test_local_supabase_integration.py`), **AUTHDB-04 E2E 기록**(`test_local_supabase_fastapi_e2e.py`): 실제 로컬 JWT로 GoTrue `GET /auth/v1/user` 확인(확인 중 DB 트랜잭션 미개방 8/8 실증) 후 FastAPI 합성 삭제 경로 연동 시, 활성 Session 1·2 삭제만 성공하고 `scope=local` 로그아웃 후 미만료 JWT(stateless JWKS 통과) 및 세션 확인 timeout/503/연결 장애는 모두 차단되어 DB 행·값 100% 불변 확인 | 실제 호스팅 `supabase.auth.signOut({ scope: 'global' | 'local' })` 실행 후 refresh 토큰 재발급 차단, 로그아웃 전 캡처된 미만료 access JWT의 직접 백엔드 API 재사용 동작, 호스팅 `/auth/v1/user` 지연·rate-limit·일시 장애 시 fail-closed 및 WEB 세션 유지 동작 검증 *(최초 가져오기 4개 경로의 계약 응답·Job 생성 0건 연동은 M1/M2 제품 통합 검증에서 별도 수행)* | 스테이징 Supabase Auth + Next.js 세션 후보 + 백엔드 API 연동 완료, 로그아웃 정책 후보(단기 TTL vs 실시간 세션 확인) 정의 | 1) 로그인 후 스테이징 테스트 하네스에서만 기존 access/refresh 상태를 추적(로그에 원문 미기록)<br>2) `signOut(scope='global')` 및 `scope='local'` 실행<br>3) 로그아웃 후 BFF 쿠키 재전송, refresh 재발급 시도, 기존 미만료 access JWT 직접 API 호출, 그리고 인증 확인 일시 장애 주입을 각각 수행 | BFF 쿠키와 refresh 재발급은 즉시 거부; 기존 미만료 access JWT 재사용은 채택된 정책과 일치; 확인된 세션 종료는 재인증 유도, 일시 장애는 민감 쓰기 차단(DB 변경 0건) 및 WEB 로그인 유지 | 즉시 직접 API 차단이 필수이나 stateless 통과가 관찰되면 백엔드에 실시간 세션 확인/revocation 검사 추가 및 access JWT TTL 단축 | `global`/`local` 로그아웃별 BFF·refresh·직접 JWT 재사용·일시 장애 주입 결과표(토큰 원문 절대 미기록) |
| **OD010-6. Workspace 멤버십 회수** | 합성 JWT 기반 멤버십 회수 즉시 차단(`test_jwt_and_membership.py`, `test_od010_5_6_session_revocation.py`), **AUTHDB-04 E2E 기록**(`test_local_supabase_fastapi_e2e.py`): 동일 로컬 Supabase DB에서 `WS_A` 멤버십 회수 시 활성 Session 2의 `WS_A` 삭제 즉시 차단(DB 행·값 불변) 및 활성 `WS_B` 삭제 성공. *별도 FOUNDATION(`c1e728a`) 로컬 PG 시험은 회수 커밋 직후 보호 `SELECT` 0행 반환 확인(다중 구문 쓰기 경합은 미포함)* | 스테이징 환경에서 BFF 쿠키와 미만료 Supabase access JWT가 살아 있는 상태의 사용자 멤버십을 DB에서 회수했을 때 다음 요청이 즉시 거부되고 타 활성 Workspace와 로그인은 유지됨을 검증 *(다중 구문 쓰기 트랜잭션의 회수 경합 및 최초 가져오기 4경로 계약 응답은 M1/M2 제품 통합 검증에서 별도 수행)* | 스테이징 DB에 이중 Workspace 멤버십 테스트 계정 및 권한 회수 절차 준비 | 1) 사용자 로그인 후 WS_A·WS_B 정상 접근 확인<br>2) 관리자/테스트 경로로 WS_A 멤버십 회수(`revoked_at` 설정)<br>3) 로그아웃·토큰 재발급 없이 동일 브라우저 세션과 동일 access JWT로 WS_A 재요청 및 WS_B 요청 비교 | WS_A 요청은 즉시 거부되고 WS_A 데이터 0건 반환, 타 활성 WS_B 요청과 로그인 세션은 유지 | 멤버십 캐시로 인해 지연 차단이 발생하면 인가 캐시 제거 또는 매 요청 DB 활성 멤버십 조회로 복귀 | 멤버십 회수 시각 대비 최초 거부 시각 로그, WS_A 차단/WS_B·로그인 유지 요약(토큰 원문 제외) |

---

## 4. 로컬 통합 검증 기록 (AUTHDB-04 · FOUNDATION) 및 M0 / M1·M2 분리

- **AUTHDB-04 실행 기록 인용 (재실행하지 않음)**: `bash spikes/t007d-auth-db/scripts/run_local_verification.sh`의 AUTHDB-04 실행 기록 기준 **27 passed, 0 failed, 0 skipped** (로컬 PG 18 역할/RLS/복합 FK/풀 재사용/JWT/BFF/역할 격리/세션 회수 23건 + 일회성 로컬 Supabase PostgREST/GoTrue 2건 + **AUTHDB-04 실제 로컬 JWT → FastAPI JWKS → GoTrue 세션 확인 → DB 삭제 E2E 2건**).
- **AUTHDB-04 실제 로컬 JWT → FastAPI → 세션 확인 → DB 변경/차단 기록** (`tests/test_local_supabase_fastapi_e2e.py`, 일회성 `t007d-authdb-ephemeral` 동일 DB·제한 역할 `t007d_api_app`):
  1. **JWT 검증 경로 정합성**: 기존 `RS256` 전용 검증기(`StrictJwtVerifier`)에서 불일치(`INVALID_ALGORITHM`)를 먼저 재현한 뒤, 헤더 `alg`/클레임 `iss`를 신뢰하지 않는 고정 설정(`jwks_url`, `expected_issuer`, `expected_audience`, `allowed_algorithms`) 검증기(`FixedConfigJwksJwtVerifier`)로 정상 사용자 JWT 통과와 서명 변조·`anon` 키·오설정 알고리즘/`iss`/`aud` 거부를 확인했다.
  2. **세션 확인 중 DB 트랜잭션 미개방**: GoTrue `GET /auth/v1/user` 호출 직전 `api_pool.get_stats()`와 Supabase DB `pg_stat_activity` (`usename='t007d_api_app' AND xact_start IS NOT NULL`)를 총 8회 검사해 **8/8 전부 `checked_out=0`, `open_tx=0`**임을 확인했다.
  3. **FastAPI 합성 삭제 경로(`DELETE /api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}`) DB 변경/차단 기록**: 활성 Session 1·2 삭제만 성공했고, `scope=local` 로그아웃 후 미만료 JWT(stateless JWKS 통과), GoTrue `/auth/v1/user` timeout(`ReadTimeout`)·HTTP `503`·`ConnectError`, `WS_A` 멤버십 회수(활성 `WS_B`는 유지)는 모두 차단되어 DB 행 수·컬럼 값 100% 불변을 확인했다.
- **FOUNDATION(`c1e728a1920935cf693fe1c0d8d95746529582e1`) 로컬 PostgreSQL 멤버십 회수 시험의 정확한 범위**:
  - `apps/api/tests/test_postgres_membership_race.py`는 로컬 Docker PostgreSQL 18(`READ COMMITTED`, 테스트 전용 스키마·비-RLS)에서 가드 통과 직후 별도 트랜잭션이 `revoked_at`을 커밋했을 때 **보호 `SELECT`에서 0행을 반환하고 은닉 예외를 발생시키는 동작**을 확인한 시험이다.
  - 이는 **다중 구문 쓰기 트랜잭션(실행·Job/outbox·멱등 기록 등)의 멤버십 회수 경합까지 통과한 증거가 아니다.** 다중 구문 쓰기 경합은 M1/M2 제품 통합 검증 항목으로 남기며, 잠금 또는 조건부 쓰기 중 특정 방식을 본 문서에서 임의로 확정하지 않는다.

### 4.1 로컬 관찰치의 일반화 금지 (제품 전체 가정 불가)
- 일회성 로컬 Supabase CLI GoTrue 인스턴스에서 관찰된 **`ES256` 서명 알고리즘**과 **`jti` 클레임 부재**는 **해당 로컬 테스트 인스턴스의 관찰치일 뿐이며, 제품 전체나 호스팅 환경에 대한 가정으로 사용하지 않는다.**
- 제품 백엔드의 허용 알고리즘 목록, JWKS 엔드포인트·키 회전 전략, 필수 클레임 집합은 스테이징/운영 Supabase 프로젝트 설정을 확인한 뒤 별도 고정 설정과 계약으로 확정해야 한다.

### 4.2 미검증 범위 유지 (호스팅 `NO-GO`, 로컬 27건으로 대체 불가)
- **실제 Next.js 제품 앱 및 HTTPS 로그인/OAuth·PKCE 콜백**, **관리형 Supavisor(`:6543`, `psycopg` `prepare_threshold=None`)**, **Cloud Run 런타임·프록시 헤더·시크릿 분리**, **GitHub OAuth `state` 1회 소비 및 orphan secret 정리(OD-011)**는 모두 **미검증** 상태다.

### 4.3 M0 격리 스파이크 검증 vs M1/M2 제품 통합 검증 분리

- **게이트·결정 순서**: `무료 잔여량·권한·정리 여유 확인 및 격리 호스팅 실행 승인` → `M0 후보 시험` → `시험 결과를 근거로 OD-006/010 채택 검토` (OD 채택은 M0 후보 시험의 선행 조건이 아니다).

| 구분 | 목적 및 완료 조건 | 포함 검증 항목 | 선행·제외 경계 |
| --- | --- | --- | --- |
| **M0 격리 스파이크 검증** *(OD-006 / OD-010 `Validation Required`, 호스팅 `NO-GO`)* | 격리 호스팅 실행 승인 후 관리형 DB 역할/RLS/풀러와 HTTPS 세션 후보의 격리 경계를 시험해 **OD-006/010 채택 검토 근거**를 수집 | 관리형 Supavisor(`:6543`, `prepare_threshold=None`, `sslmode=verify-full`) 연결 재사용·`ROLLBACK`, 최소권한 역할·`FORCE RLS`·복합 FK, 호스팅 Data API(`/rest/v1/*`) 우회 차단, 실제 Next.js HTTPS PKCE/쿠키/CSRF/동시 탭 refresh 후보, 호스팅 JWKS·`/auth/v1/user` 실측 | OD 채택을 선행 조건으로 두지 않음; 제품 도메인 구현이 필요한 API·DB 쓰기·Job/outbox·멱등 기록 시험을 M0 선행 작업으로 당기지 않음 |
| **M1/M2 제품 통합 검증** *(제품 구현 이후 별도 완료 조건)* | 실제 제품 API·DB·Worker·WEB의 end-to-end 계약 및 동시성 검증 | 최초 가져오기 4개 경로(`GET/POST /v1/sync-runs`, `GET /v1/sync-runs/{id}`, `POST /v1/sync-runs/{id}/retry`)의 승인 4종 오류 계약(`Cache-Control: private, no-store` 필수, `X-Request-ID` 헤더 선택, JSON `error.request_id` 필수, `details` 금지) 검증, 실행·Job/outbox·멱등 기록의 단일 트랜잭션 커밋 및 장애 시 DB/Job 변경 0건, **다중 구문 쓰기 트랜잭션의 멤버십 회수 경합**(잠금·조건부 쓰기 등 구현 방식 미확정), `(auth_issuer, auth_subject)` 매핑, GitHub 수집 OAuth `state` 1회 소비 및 orphan secret 정리(OD-011) | M0 로컬 27건이나 FOUNDATION 보호 `SELECT` 0행 시험만으로 통과 처리하지 않음 |

---

## 5. 승인된 최초 가져오기 오류 계약 대조 및 미확정 범위 (`CONTRACT@87f1515`)

`CONTRACT@87f1515cbe74bf02b0aece68904bf9a641c74c60` (`contracts/import-v1/openapi.yaml` 0.5.0-draft)에서 **최초 가져오기 4개 경로(`GET /v1/sync-runs`, `POST /v1/sync-runs`, `GET /v1/sync-runs/{id}`, `POST /v1/sync-runs/{id}/retry`)에 한해** 아래 4종 오류 응답이 승인되었다. 로컬 스파이크(`src/api_app.py`)의 임시 출력 이름은 코드를 수정하지 않고 그대로 유지하되, 제품 계약과 표에서 명확히 구분한다.

### 5.1 최초 가져오기 4개 경로 한정 승인 계약 vs 로컬 스파이크 임시 출력

| 승인된 제품 계약 (`CONTRACT@87f1515`, 최초 가져오기 4개 경로 한정) | `retryable` | 응답 헤더 · JSON 본문 규칙 (`openapi.yaml` 근거) | Backend · WEB 처리 규약 | 로컬 스파이크 임시 출력 (`src/api_app.py`, 코드 미변경) |
| --- | --- | --- | --- | --- |
| **`401 SESSION_TERMINATED`** (`ImportSessionRequired`) | `false` | `Cache-Control: private, no-store` **필수**, `X-Request-ID` 헤더 **선택**, JSON `error.request_id` **필수**, **`details` 금지** | **확인된 세션 종료에만 사용.** 보호 요청 차단, WEB은 해당 세션을 정리하고 재로그인을 안내 | `401 SESSION_TERMINATED` (합성/로컬 하네스 관찰치; 스파이크 일반 토큰 오류 `INVALID_*` 등과 혼동 금지) |
| **`403 WORKSPACE_SCOPE_VIOLATION`** (`ImportWorkspaceAccessRevoked`) | `false` | `Cache-Control: private, no-store` **필수**, `X-Request-ID` 헤더 **선택**, JSON `error.request_id` **필수**, **`details` 금지** | **공개 가능한 Workspace 문맥에서 확인된 범위 회수.** 해당 Workspace만 닫고 **다른 활성 Workspace와 로그인은 유지** | `403 WORKSPACE_FORBIDDEN` *(로컬 스파이크 임시 코드명 — 제품 계약에 복사 금지)* |
| **`404 RESOURCE_NOT_FOUND`** (`ImportResourceConcealed`) | `false` | `Cache-Control: private, no-store` **필수**, `X-Request-ID` 헤더 **선택**, JSON `error.request_id` **필수**, **`details` 금지** | **부재 · 타 Workspace · 권한 밖 은닉 대상**에 `request_id` 제외 동일 응답 반환(대상 ID·존재 단서 비노출) | `404 RESOURCE_NOT_FOUND` (스파이크 삭제/조회 은닉 관찰치) |
| **`503 AUTH_VERIFICATION_UNAVAILABLE`** (`ImportAuthUnavailable`) | **`true`** *(4종 중 유일)* | `Cache-Control: private, no-store` **필수**, `X-Request-ID` 헤더 **선택**, JSON `error.request_id` **필수**, **`details` 금지** | **인증 확인 일시 장애**(JWKS/GoTrue 장애·timeout·rate limit). 민감 쓰기·Job 생성 0건 차단, **WEB은 세션을 유지**하고 읽기 전용 GET 복구 사용. **`503`과 `retryable: true`는 나중에 명시적으로 다시 확인할 수 있다는 뜻이며 자동 `POST` 재전송의 근거가 아님** | `503 SESSION_STATUS_UNAVAILABLE` *(로컬 스파이크 임시 코드명 — 제품 계약에 복사 금지)* |

### 5.2 본 문서에서 확정하지 않는 오류·계약 항목 (계약 승인 대기 유지)
위 4종 승인을 최초 가져오기 4개 경로 밖이나 일반 오류로 확대하지 않는다. 아래 항목은 여전히 **미확정**이다:
- **일반 `401`** (자격 정보 누락, 서명 오류, 만료 `exp` 등 확인된 세션 종료 외 미분류 인증 실패)
- **일반 `403`** (Workspace 멤버십은 유효하나 역할이 부족한 일반 RBAC 거부 등)
- **입력 오류 `422`** (누락·잘못된 본문/키/버전) 및 **승인되지 않은 기타 `409`** (성공 후 재접수, 미완료 계획 교체, 재시도 불가 실행 등)
- **Source Account / Source Resource 경로**(`/v1/source-accounts/github`, `/v1/source-accounts`, `/v1/source-resources`)의 오류 응답 매핑
- **`Retry-After` 헤더** 포함 여부 및 JWKS/GoTrue 타임아웃·백오프 운영 수치

---

## 6. 관심사 분리 원칙 (로그인 세션 ≠ GitHub 수집 권한 ≠ LLM 플랜 권한)

모든 로컬·호스팅 검증에서 아래 3개 권한 평면은 저장소, 토큰, 콜백, 검증 경계를 분리한다.

| 권한 평면 | 권한 주체 | 전달 · 저장 경계 (후보 포함) | 혼합 금지 대상 |
| --- | --- | --- | --- |
| **1. 서비스 로그인 세션 (OD-010)** | Supabase Auth (`sub`, `session_id`) + DB `workspace_memberships` | Next.js 서버 경유 `HttpOnly`/`Secure` 쿠키 후보 → 서버-API `Bearer` JWT | GitHub 수집 토큰, LLM 플랜/쿼터 |
| **2. GitHub 저장소 수집 권한 (OD-011)** | GitHub App 설치 또는 저장소 범위 최소 읽기 grant (Workspace/연결 단위) | 암호화된 서버 측 자격 증명 저장소, 로그인 콜백과 분리된 전용 수집 콜백 | 서비스 로그인 쿠키, Supabase Auth JWT 클레임 |
| **3. LLM 플랜 · 쿼터 권한** | 결제/구독/할당량 서비스 (Workspace 단위) | LLM 호출 게이트웨이의 서버 측 entitlement 테이블 조회 | 로그인 JWT 클레임, DB RLS 테넌트 역할 |
