# T-007D Hosted Verification Runbook (OD-006 / OD-010)

> **문서 성격 및 범위 경계**
> - 이 문서는 **OD-006**(DB 역할·Workspace 격리·풀링)과 **OD-010**(Next.js 서버 경유·HttpOnly/Secure 쿠키 후보 및 세션 경계)을 닫기 위해 **호스팅 스테이징(Supabase + Cloud Run)**에서 실행해야 할 검증 절차를 정리한다.
> - 로컬 T-007D 스파이크(`spikes/t007d-auth-db/tests/`)에서 확인된 항목과 호스팅에서 미실행인 항목은 아래 각 표에서 명시적으로 분리한다.
> - **미승인 API 오류 응답(정확한 HTTP 상태 코드, 오류 코드, JSON error envelope)은 이 문서에서 확정하지 않는다.** 로컬 스파이크의 `401`/`403`/`404`는 합성 프로토타입 관찰치이며, 실제 계약 응답 형식은 별도 API 계약 승인을 따른다.
> - **Next.js 서버 경유**와 **`HttpOnly`/`Secure` 쿠키**는 확정 사양이 아니라 **OD-010 검증 후보**로 유지한다.

---

## 0. 비용 · 무료 한도 · 외부 리소스 가드레일

1. **사전 확인 필수**: 무료 범위(Supabase 무료 프로젝트 수·동시 연결 수·egress 한도, Cloud Run 무료 할당량·과금 조건)와 계정 권한, 오케스트레이터 승인을 확인하기 전에는 **어떤 외부 리소스(호스팅 Supabase 프로젝트, Cloud Run 서비스, 외부 OAuth/GitHub App)도 생성하거나 연결하지 않는다.**
2. **중단 및 로컬 대안 원칙**: 비용 발생 가능성이나 무료 한도가 불명확하면 **호스팅 검증을 즉시 중단**하고, 아래 로컬 대안 범위만 수행·기록한다.
   - **DB 역할 · RLS · 복합 FK · 로컬 단일 연결 재사용**: 로컬 PostgreSQL 18 (`bash spikes/t007d-auth-db/scripts/run_local_verification.sh`)
   - **Data API(PostgREST) · GoTrue · 로컬 풀러 사전 점검(Docker 가용 시)**: 로컬 Supabase CLI(`supabase start`) 스택에서 무료·오프라인으로 사전 재현(현재 로컬 환경은 Docker 데몬 미기동으로 미실행)
   - **쿠키 · Origin/CSRF 흐름 사전 점검**: 로컬 BFF 프로토타입(`tests/test_bff_session_csrf.py`) 또는 로컬 자체 서명 TLS 프록시 기반 Next.js BFF(실제 Supabase Auth JWKS·Cloud Run 프록시 헤더·호스팅 Supavisor 동작은 여전히 호스팅 검증 필수)

---

## 1. Supabase 공유 Transaction Pooler 및 `psycopg` 설정 정정 (OD-006)

- **런타임 풀러 후보 (API · Worker)**: Supabase 공유 **Transaction Pooler (`:6543`)** 경로를 사용한다.
- **`psycopg` prepared statement 비활성화 규칙**:
  - `psycopg` 드라이버에서는 반드시 **`prepare_threshold=None`** 으로 서버 측 prepared statement를 끈다.
  - **`prepare_threshold=0`은 대안에서 제외한다.** (`psycopg` v3에서 `0`은 비활성화가 아니라 *첫 실행부터 모든 쿼리를 즉시 prepare*하는 설정이므로 Transaction Pooler와 충돌한다.)
  - 사용하지 않는 다른 드라이버/ORM 설정은 동등한 대안으로 제시하지 않는다.
- **세션 상태 금지**: Transaction Pooler 경로에서는 세션 수준 `SET`(`set_config(..., false)`), `LISTEN/NOTIFY`, `TEMP TABLE`, 세션 advisory lock을 금지하고, 모든 테넌트 범위는 트랜잭션 내부 `SELECT set_config('app.current_workspace_id', %s, true)`로만 지정한다.
- **마이그레이션 경로 분리**: DDL 및 스키마 마이그레이션은 런타임 풀러가 아닌 **Session Pooler (`:5432`) 또는 Direct 연결**에서 CI 전용 `migrator` 역할로만 수행한다.

---

## 2. OD-006 검증표 (DB 역할 · Workspace 격리 · 풀링 · Data API 우회 차단 · Migration 분리)

| ID · 검증 항목 | 로컬에서 확인됨 | 호스팅에서 미실행 | 선행 조건 | 실행 절차 | 통과 기준 | 실패 시 대안 | 남길 증거 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **OD006-1. API · Worker 최소권한 DB 역할** | `t007d_api_app`·`t007d_worker_app`의 `NOSUPERUSER`/`NOBYPASSRLS`/`NOINHERIT`, 테이블 비소유, `SET ROLE t007d_migrator`·`SET row_security = off`·DDL 거부, Worker의 `users`/`workspace_memberships` 비접근 및 `DELETE` 거부 확인 (`test_db_isolation_and_fk.py`) | 스테이징 Supabase DB에 실제 `api_app`/`worker_app` 역할 생성 후 Cloud Run 자격 증명으로 `pg_roles` 속성 및 권한 거부 재현 | 무료 범위·권한 확인 완료, 스테이징 Supabase 프로젝트 및 역할 SQL 준비 | 1) `api_app`·`worker_app` 각각으로 스테이징 DB 접속<br>2) `pg_roles`와 `pg_class` 소유자 조회<br>3) `SET ROLE migrator`, `SET LOCAL row_security = off`, `CREATE TABLE`, 비허가 테이블 `SELECT`/`DELETE` 시도 | 런타임 역할 모두 `rolsuper=false`, `rolbypassrls=false`, 테이블 비소유이며 금지 연산이 전부 DB 권한 오류로 거부됨 | 누락된 `REVOKE`·`NOINHERIT`·`NOBYPASSRLS` 보완 및 테이블 소유자를 `migrator`로 재지정; 호스팅 비용 불명확 시 로컬 PG 18 감사 쿼리 결과로 대기 | `pg_roles`/`pg_class` 감사 결과(계정 비밀번호 제외), 금지 SQL 실행 시 DB 오류 코드 로그 |
| **OD006-2. 트랜잭션 안에서만 유효한 Workspace 범위** | `SELECT set_config('app.current_workspace_id', %s, true)`의 트랜잭션 한정 적용 및 미설정/빈 문자열/비UUID 값 fail-closed(0행) 확인; `set_config(..., false)` 세션 누출 반례 실측 (`test_db_isolation_and_fk.py`, `test_pool_reuse.py`) | 호스팅 Supavisor Transaction Pooler(`:6543`) + `psycopg(prepare_threshold=None)` 경로에서 트랜잭션 단위 `set_config(..., true)` 동작 및 prepared statement 충돌 부재 확인 | 무료 범위·권한 확인 완료, 스테이징 API/Worker에 `psycopg` `prepare_threshold=None` 적용 | 1) `:6543` 경유 동일 파라미터화 쿼리 반복 실행<br>2) 트랜잭션 내에서만 `set_config(..., true)` 호출 후 조회<br>3) `set_config` 미호출 트랜잭션에서 동일 테이블 조회 | prepared statement 중복 오류 0건, 트랜잭션 내부에서는 해당 Workspace 행만 반환, 미설정 트랜잭션은 0행(fail-closed) | Transaction Pooler 호환 이슈 발생 시 커넥션 옵션(`prepare_threshold=None`)·트랜잭션 경계 재점검; 미해결 시 Session Pooler 전환 비용·동시성 한도를 오케스트레이터에 재보고 | `psycopg` 연결 설정 스냅샷(`prepare_threshold=None`), 반복 쿼리 성공·미설정 0행 실행 로그 |
| **OD006-3. 연결 재사용 · `ROLLBACK` 뒤 범위 초기화** | 로컬 PG 18 `psycopg_pool(max_size=1)` 단일 물리 연결(`pg_backend_pid()` 동일)에서 `WS_A → unscoped → WS_B → WS_A rollback → unscoped` 및 HTTP 순차 요청 누출 0건 확인 (`test_pool_reuse.py`) | 호스팅 Supavisor Transaction Pooler(`:6543`)에서 동시 교차 요청(WS_A/WS_B), 트랜잭션 강제 `ROLLBACK`, TLS(`sslmode=verify-full`), 연결 한도·타임아웃(`statement_timeout`, `idle_in_transaction_session_timeout`) 검증 | 무료 범위·권한 확인 완료, 스테이징 풀러 CA 번들 탑재 및 합성 Workspace A/B 시드 준비 | 1) 풀 크기를 제한한 클라이언트에서 WS_A 조회 → 강제 예외 `ROLLBACK` → unscoped 조회 → WS_B 조회를 연속·동시 실행<br>2) `sslmode=verify-full` 접속 및 `sslmode=disable` 거부 확인<br>3) 타임아웃 초과 쿼리 종료 후 다음 요청 격리 확인 | 모든 교차·롤백 이후 타 Workspace 행 누출 0건, unscoped 조회 0행, TLS 검증 성공, 풀 고갈 없음 | 롤백 후 잔존 징후 발견 시 커넥션 반환 훅에 트랜잭션 상태 검사 추가 및 세션 수준 설정 호출 제거; 로컬 대안은 로컬 PG 18 단일 연결 테스트로 한정 표기 | 동시·롤백 시나리오 테스트 리포트, TLS 검증 로그, 타임아웃·풀러 연결 수 관측치 |
| **OD006-4. 다른 Workspace의 조회 · 쓰기 · 참조 차단** | 직접/부모 경유/자식 조회·수정·삭제 RLS 차단, `WITH CHECK` 위조 `workspace_id` INSERT 차단, 복합 FK `(workspace_id, project_id) -> projects(workspace_id, id)` 교차 참조 및 존재 오라클 차단 확인(단일 FK 취약점·RLS `EXISTS`·트리거 후보 비교 완료) | 스테이징 실제 후보 스키마의 모든 부모-자식 테넌트 테이블에 `ENABLE`+`FORCE ROW LEVEL SECURITY` 및 복합 FK 적용 후 교차 Workspace 조회/쓰기/참조 차단 실측 | 스테이징 후보 스키마 마이그레이션 완료, 합성 Workspace A/B 부모·자식 데이터 준비 | 1) WS_B 컨텍스트에서 WS_A 부모·자식 직접/부모 경유 조회·수정·삭제 시도<br>2) WS_B 컨텍스트에서 `workspace_id=WS_A` INSERT 시도<br>3) WS_B 자식 행에 WS_A 부모 ID 참조 INSERT·`UPDATE` 시도(런타임 역할 + `migrator`/관리 경로 각각) | 타 Workspace 조회 0행, 수정/삭제 0행, 위조 쓰기 거부, 교차 부모 참조는 런타임·관리 경로 모두 복합 FK(`23503`)로 거부되며 미존재 ID와 구별 불가 | 단일 FK가 발견된 자식 테이블에 즉시 복합 FK 마이그레이션 추가; `FORCE ROW LEVEL SECURITY` 누락 테이블 보완 | `pg_constraint` 복합 FK 목록, `pg_class` RLS/FORCE RLS 목록, 교차 조회·쓰기·참조 차단 실행 로그 |
| **OD006-5. 브라우저의 Supabase Data API(PostgREST) 도메인 쓰기 우회 차단** | 로컬 PG 18 역할 계층(`t007d_data_api_anon`, `t007d_data_api_authenticated`)의 도메인 테이블 직접 조회/쓰기 거부·DB 행 불변·정상 서버 경유 성공 확인(`test_od006_5_data_api_isolation.py`). 단, **실제 PostgREST(`/rest/v1/*`) 게이트웨이 테스트(`test_local_supabase_integration.py`)는 로컬 Docker Engine 미기동으로 미실행(BLOCKED/SKIPPED)** | 스테이징 Supabase `/rest/v1/*` 엔드포인트에 브라우저 노출 `anon` 키 및 로그인 사용자 `authenticated` JWT로 직접 도메인 테이블 쓰기·조회 우회 시도 검증 | 무료 범위·권한 확인 완료, 스테이징 Supabase 프로젝트의 API Exposed Schemas 설정 및 `anon`/`authenticated` 역할 권한 확인 | 1) Exposed Schemas 설정과 `information_schema.role_table_grants`에서 `anon`/`authenticated` 권한 조회<br>2) 브라우저 `anon` 키로 `/rest/v1/<domain_table>` `POST`/`PATCH`/`DELETE`/`GET` 직접 호출<br>3) 실제 로그인 사용자의 `authenticated` JWT로 동일 `/rest/v1/<domain_table>` 직접 쓰기·조회 호출 | 모든 도메인 테이블에 대해 브라우저 직접 Data API 쓰기(및 정책상 비허가 직접 조회)가 거부되고 대상 테이블 행 변경 0건 | 도메인 스키마를 PostgREST Exposed Schemas에서 제외하고 `anon`/`authenticated`의 도메인 테이블 권한을 전량 `REVOKE`; 필요 시 Data API 비활성화. 로컬 Docker Desktop 기동 후 `test_local_supabase_integration.py`로 로컬 PostgREST 사전 재현 | Exposed Schemas 설정 기록, `role_table_grants` 감사 출력, 토큰·키 마스킹된 `/rest/v1` 거부 응답 로그 및 DB 행 수 불변 증적 |
| **OD006-6. 런타임 역할과 Migration 역할 분리** | `t007d_migrator` 단독 소유, 런타임 역할의 `SET ROLE t007d_migrator`·DDL 불가, 런타임 코드 경로에 `migrator` DSN 미사용 확인 (`test_db_isolation_and_fk.py`) | CI/CD 전용 `migrator`(Session `:5432` 또는 Direct 연결)와 Cloud Run 런타임(`api_app`/`worker_app`, Transaction `:6543`)의 시크릿·IAM·네트워크 경로 분리 및 배포 후 자동 감사 검증 | 무료 범위·권한 확인 완료, 스테이징 Secret Manager / CI 시크릿 분리 설계 준비 | 1) Cloud Run 서비스 환경 변수·시크릿 바인딩에 `migrator` 자격 증명 부재 확인<br>2) CI 파이프라인에서만 `migrator`로 마이그레이션 실행 후 `pg_class`/`pg_roles`/`pg_constraint` 감사 쿼리 실행<br>3) 런타임 컨테이너에서 DDL 시도 | 런타임 환경에 `migrator` 시크릿 0건, 런타임 DDL 거부, CI 마이그레이션 직후 RLS·`FORCE RLS`·복합 FK 감사 100% 통과 | 런타임 시크릿에 과권한 계정 혼입 시 즉시 시크릿 회전·IAM 범위 축소 후 재배포; 감사 실패 시 배포 파이프라인 자동 중단 | Cloud Run 시크릿 이름 목록(값 제외), CI 마이그레이션·감사 로그, 런타임 DDL 거부 로그 |

---

## 3. OD-010 검증표 (Next.js 서버 경유 · HttpOnly/Secure 쿠키 후보 및 세션 경계)

> **후보 및 계약 경계 명시**
> - **Next.js 서버 경유** 및 **`HttpOnly`/`Secure` 쿠키**는 OD-010 최종 확정안이 아니라 **검증할 후보**로 유지한다.
> - 아래 통과 기준은 미승인 API 오류 응답(특정 HTTP 상태 코드·오류 코드·JSON 스키마)을 확정하지 않으며, **접근 거부·토큰 비노출·권한 차단** 행위 여부만 검증한다.
> - **서비스 로그인(Supabase Auth)과 GitHub 저장소 수집 권한(OD-011), LLM 플랜/쿼터 권한은 별개 경계**로 검증하며 상호 혼입을 금지한다.

| ID · 검증 항목 | 로컬에서 확인됨 | 호스팅에서 미실행 | 선행 조건 | 실행 절차 | 통과 기준 | 실패 시 대안 | 남길 증거 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **OD010-1. 실제 Supabase 로그인 · 콜백 (Next.js 서버 경유 후보)** | 합성 BFF 로그인(`POST /bff/auth/synthetic-login`)이 서버 세션만 생성하고 응답 본문에 Bearer JWT를 노출하지 않음만 확인 (`test_bff_session_csrf.py`) | 스테이징 Supabase Auth + Next.js 서버 콜백 라우트에서 실제 로그인, PKCE(`code_verifier`/`code`), `state` 검증, 오픈 리다이렉트 차단 검증 | 무료 범위·권한 확인 완료, 스테이징 Supabase Auth Redirect URL 및 Next.js 서버 후보 배포(또는 승인된 HTTPS 스테이징 URL) | 1) 브라우저에서 Next.js 서버 로그인 시작 → Supabase Auth 인증 → 서버 콜백 라우트 리다이렉트 수행<br>2) 변조된 `state`/`code` 및 외부 도메인 `next` 파라미터로 콜백 재시도<br>3) 브라우저 네트워크·스토리지에 raw access/refresh 토큰 미노출 확인 | 정상 로그인 시 서버 측 세션 수립 완료; 변조 `state`/`code` 및 외부 리다이렉트는 거부되며 브라우저 JS 저장소에 raw 토큰 0건 | PKCE/콜백 리다이렉트에서 `SameSite` 충돌 시 콜백 쿠키 스코프·`SameSite=Lax` 재조정; Next.js 서버 경유 후보 부적합 판정 시 오케스트레이터에 대안 세션 전략 비교 보고 | 브라우저 HAR/네트워크 요약(토큰·쿠키 원문 제거), 변조 `state`/오픈 리다이렉트 거부 로그 |
| **OD010-2. JWT 검증 (JWKS · 클레임 · 관심사 분리)** | 합성 RS256 키쌍 기반 서명 변조·타 키·`HS256` 혼동·잘못된 `iss`/`aud`·만료 `exp`·로그인 JWT 내 GitHub/LLM 클레임 혼입 거부 확인 (`test_jwt_and_membership.py`) | 스테이징 Supabase `/.well-known/jwks.json` 비대칭 키 조회·캐시·미등록 `kid` 회전 처리, 실제 `iss`/`aud`/`exp`/`alg` 검증, `anon`/`service_role` 토큰 거부 검증 | 스테이징 Supabase 프로젝트의 JWT 서명 방식(비대칭 JWKS vs 레거시 HS256) 확인 완료 | 1) 실제 발급된 `authenticated` JWT로 백엔드 API 정상 검증<br>2) 서명 변조·잘못된 `iss`/`aud`·미등록 `kid`·`anon` 키·`service_role` 키로 동일 엔드포인트 호출<br>3) JWKS 캐시 만료/갱신 및 rate-limit 동작 확인 | 유효한 사용자 JWT만 통과; 변조·오클레임·미등록 `kid`·`anon`/`service_role` 토큰은 전부 거부되고 DB 테넌트 쿼리에 도달하지 않음 | JWKS 지연/장애 시 안전 캐시 유지 + fail-closed 정책 적용; 프로젝트가 레거시 HS256인 경우 비대칭 키 전환 일정과 검증 절차를 오케스트레이터에 보고 | JWKS 메타데이터(공개키 `kid`/`alg`만 기록, 토큰 원문 제외), 각 부정 케이스 거부 로그 |
| **OD010-3. 쿠키 · Origin / CSRF (HttpOnly / Secure 쿠키 후보)** | 로컬 HTTP 환경에서 `HttpOnly; SameSite=Lax` 쿠키 발급, 본문 JWT 미노출, `POST`의 누락/타 출처 `Origin` 및 누락/불일치 `X-CSRF-Token` 거부 확인 (`test_bff_session_csrf.py`) | 실제 HTTPS 브라우저 환경에서 `Secure` 및 `__Host-` 접두어 후보 쿠키 동작, `document.cookie`/`localStorage` 비노출, Cloud Run/CDN 뒤 `Origin`/`Host`/`X-Forwarded-Host` 정규화 및 cross-site 요청 차단 검증 | 스테이징 HTTPS 도메인·Next.js 서버 경유 후보·프록시 헤더 구성 확인 완료 | 1) 실제 브라우저에서 로그인 후 `Set-Cookie` 헤더(`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, `__Host-` 후보)와 `document.cookie`·`localStorage` 확인<br>2) 타 출처 페이지에서 `credentials: 'include'` `fetch` 및 폼 `POST` 전송<br>3) CSRF 토큰 누락/오값 요청 전송 | 브라우저 JS에서 세션·Bearer 토큰 접근 불가; 타 출처/비유효 CSRF 상태 변경 요청은 백엔드 API 도달 전 BFF 계층에서 전부 거부됨 | 프록시 뒤 `Origin`/`Host` 불일치 시 trusted proxy 헤더 화이트리스트 보정; `__Host-` 도메인 제약 충돌 시 `Secure; HttpOnly; SameSite=Lax; Path=/`로 범위 명시 후 재검토 | 마스킹된 `Set-Cookie` 속성 캡처, DevTools 스토리지 점검 기록, cross-site/CSRF 거부 로그 |
| **OD010-4. 세션 만료 (Access / Refresh 수명 및 동시 탭 갱신)** | 만료된 합성 JWT(`exp < now`)의 백엔드 API 거부만 확인 (`test_jwt_and_membership.py`); refresh 회전·만료 및 동시 탭 경합은 미실행 | 스테이징 Supabase Auth의 access JWT 만료 후 Next.js 서버(Middleware/Route Handler/RSC) 자동 갱신, 동시 다중 탭 refresh-token 회전 경합, refresh 만료 후 재인증 전환 검증 | 스테이징 Supabase Auth의 테스트용 짧은 JWT 만료 시간 설정 권한 확인 완료 | 1) 로그인 후 access JWT 만료 시점까지 대기 후 보호 페이지/API 호출<br>2) 만료 직후 2개 이상 브라우저 탭에서 동시에 요청 발사(refresh 회전 경합 확인)<br>3) refresh 토큰 만료/무효 상태에서 요청 시도 | 단일·동시 갱신 시 정상 세션 유지(오탐 로그아웃 없음), refresh 만료 시 보호 데이터 미노출 및 재인증 흐름 전환 | 동시 탭 refresh reuse-detection 충돌 발생 시 서버 측 단일 flight 잠금 또는 쿠키 갱신 경로 일원화(Middleware 단일 책임); 대안 불가 시 세션 저장소(서버 측 세션 스토어) 도입 검토 | 만료 전/후 갱신 타임라인 로그(토큰 원문 제외), 동시 탭 테스트 결과, refresh 만료 후 차단 기록 |
| **OD010-5. 로그아웃 후 기존 토큰 재사용** | 합성 BFF 로그아웃의 쿠키 삭제·refresh 재발급 거부·기존 access JWT의 `exp` 전 서명 검증 통과를 분리 확인하고, 세션 종료 또는 세션 상태 확인 불가(장애/미등록) 시 승인·Export·삭제·Source 연결/회수 민감 경로 전부 fail-closed 차단 및 로그아웃 실패/정상 구분 확인(`test_od010_5_6_session_revocation.py`). 단, **실제 로컬 GoTrue 테스트(`test_local_supabase_integration.py`)는 Docker Engine 미기동으로 미실행(BLOCKED/SKIPPED)** | 실제 `supabase.auth.signOut({ scope: 'global' | 'local' })` 실행 후 refresh 토큰 재발급 차단, 그리고 로그아웃 전 캡처된 미만료 access JWT의 직접 백엔드 API 재사용 동작 및 채택 정책 검증 | 스테이징 Supabase Auth + Next.js BFF + 백엔드 API 연동 완료, 로그아웃 정책 후보(단기 TTL vs 서버 측 revocation) 정의 | 1) 로그인 후 스테이징 테스트 하네스에서만 기존 access/refresh 상태를 추적(로그에 원문 미기록)<br>2) `signOut(scope='global')` 및 `scope='local'` 실행<br>3) 로그아웃 후 BFF 쿠키 재전송, refresh 재발급 시도, 기존 미만료 access JWT 직접 API 호출을 각각 수행 | BFF 쿠키와 refresh 재발급은 즉시 거부; 기존 미만료 access JWT 재사용은 **채택된 정책**(서버 측 세션/denylist 즉시 거부 또는 합의된 단기 TTL 내 만료 대기)과 100% 일치하게 동작 | 즉시 직접 API 차단이 필수이나 stateless 통과가 관찰되면 백엔드에 서버 측 세션 상태/revocation 검사 추가 및 access JWT TTL 단축 | `global`/`local` 로그아웃별 BFF·refresh·직접 JWT 재사용 결과표(토큰 원문 절대 미기록) |
| **OD010-6. Workspace 멤버십 회수** | DB `workspace_memberships.revoked_at` 갱신 직후 **동일한 미만료 JWT**로 회수된 Workspace 접근 즉시 거부, 동일 사용자의 타 활성 Workspace 접근 유지, 범위 밖 리소스 존재 은닉 확인 (`test_jwt_and_membership.py`) | 스테이징 환경에서 BFF 쿠키와 미만료 Supabase access JWT가 살아 있는 상태의 사용자 멤버십을 DB에서 회수했을 때 다음 요청이 즉시 거부되고 캐시/세션 쿠키로 우회되지 않음을 검증 | 스테이징 DB에 이중 Workspace 멤버십 테스트 계정 및 권한 회수 절차 준비 | 1) 사용자 로그인 후 WS_A·WS_B 정상 접근 확인<br>2) 관리자/테스트 경로로 WS_A 멤버십 회수(`revoked_at` 설정)<br>3) 로그아웃·토큰 재발급 없이 동일 브라우저 세션과 동일 access JWT로 WS_A 재요청 및 WS_B 요청 비교 | WS_A 요청은 즉시 거부되고 WS_A 데이터가 0건 반환되며, WS_B 요청은 정상 동작; 최종 API 오류 응답 형식은 승인된 계약과 일치 | 멤버십 캐시로 인해 지연 차단이 발생하면 인가 캐시 제거 또는 매 요청 DB 활성 멤버십 조회로 복귀(현재 로컬 스파이크 패턴 유지) | 멤버십 회수 시각 대비 최초 거부 시각 로그, WS_A 차단/WS_B 유지 응답 요약(토큰 원문 제외) |

---

## 4. 관심사 분리 원칙 (로그인 세션 ≠ GitHub 수집 권한 ≠ LLM 플랜 권한)

모든 로컬·호스팅 검증에서 아래 3개 권한 평면은 저장소, 토큰, 콜백, 검증 경계를 분리한다.

| 권한 평면 | 권한 주체 | 전달 · 저장 경계 (후보 포함) | 혼합 금지 대상 |
| --- | --- | --- | --- |
| **1. 서비스 로그인 세션 (OD-010)** | Supabase Auth (`sub`, `session_id`) + DB `workspace_memberships` | Next.js 서버 경유 `HttpOnly`/`Secure` 쿠키 후보 → 서버-API `Bearer` JWT | GitHub 수집 토큰, LLM 플랜/쿼터 |
| **2. GitHub 저장소 수집 권한 (OD-011)** | GitHub App 설치 또는 저장소 범위 최소 읽기 grant (Workspace/연결 단위) | 암호화된 서버 측 자격 증명 저장소, 로그인 콜백과 분리된 전용 수집 콜백 | 서비스 로그인 쿠키, Supabase Auth JWT 클레임 |
| **3. LLM 플랜 · 쿼터 권한** | 결제/구독/할당량 서비스 (Workspace 단위) | LLM 호출 게이트웨이의 서버 측 entitlement 테이블 조회 | 로그인 JWT 클레임, DB RLS 테넌트 역할 |
