# API 계약 연결 명세 및 Adapter 변환 가이드 (UI -> CONTRACT)

> **문서 성격**: 본 문서는 `spikes/web-import/`의 **검증 계층 구분**, **이전 `1d754a5` 기록**, **현재 `87f1515` (`OpenAPI 0.5.0-draft`) 실제 루프백 교차 검증 결과**, **확정된 UI 동작 및 Adapter 경계**, 그리고 **미검증 제품 연동 범위**를 분리해 기록합니다.
>
> **계약·검증 기준 구분**:
> - **이전 검증 기록 (`1d754a5bc33cf166d92b1c1ce8568031859ff0a4`, `OpenAPI 0.3.0-draft`)**: `WEB-06` / `WEB-SPEC-SYNC` 구간에서 body 없는 재시도(`POST /v1/sync-runs/{id}/retry`)와 빈 `403`/`409`/`422` 응답을 기준으로 검증한 과거 기록입니다.
> - **현재 통과한 교차 검증 기준 (`87f1515cbe74bf02b0aece68904bf9a641c74c60`, `OpenAPI 0.5.0-draft`)**: `contracts/import-v1/openapi.yaml` Git blob **`07594d75425744d6149250a9f7ff95ac24659f80`**. 해당 커밋만 `git archive`로 임시 추출해 `CONTRACT_IMPORT_DIR` + `UV_OFFLINE=1` 환경에서 `127.0.0.1` 실제 루프백 HTTP 테스트 18/18 통과를 확인했습니다.
> - **아직 미검증인 제품 연동**: 실제 제품 백엔드, Supabase Auth/JWT·세션 쿠키·호스팅 로그아웃, GitHub OAuth·저장소 목록 API, PostgreSQL 트랜잭션·재시작 영속 24시간 복구, Worker 실행, 운영 폴링 주기/Transport, Workspace 관리 화면, 계획 폐기(Abandon) API/UI는 본 스파이크에 포함하지 않습니다.

---

## 1. 구현·검증 계층 구분

| 계층 | 실제 파일·명령 | 검증 내용 | 미검증·경계 |
| --- | --- | --- | --- |
| **모의 화면 / 단위·UI·브라우저 E2E (로컬 UI Fixture)** | `MockFixtureImportAdapter`, `SyntheticOpenApiTransport`, `ImportJourneyApp`, `npm test` (71/71), `npm run build`, `npm run test:e2e` (7/7) | UI 상태 전이, 키보드 조작, 중복 클릭 방지, 성공 저장소 재요청 UX 차단(`A→B→A` 포함), 계정/세션 전환 범위 정리, `import_ready`/후속 처리 분리, `retryPolicy.allowed` 게이팅, 이전 재시도 재전송 vs 새 재시도 버튼 분리, 브라우저 새로고침 복구 | 로컬 UI Fixture / 합성 HTTP 기반; 실제 루프백 FastAPI stub·백엔드·인증·GitHub API 미호출 (루프백 HTTP 검증과 별개 계층) |
| **로컬 FastAPI stub 실제 루프백 HTTP 교차 검증 (`87f1515`, `OpenAPI 0.5.0-draft`)** | `src/adapters/contract-sync-run-client.ts`, `tests/contract-http/loopback-http-transport.ts`, `tests/contract-http/run_contract_stub.py`, `tests/contract-http/contract-stub-http.test.tsx`, `CONTRACT_IMPORT_DIR=<87f1515 archive> UV_OFFLINE=1 npm run test:contract-http` (18/18) | `127.0.0.1` 에페메럴 포트에서 CONTRACT `create_app` + Python 주입 `FixtureActor`/`FixtureAuthFailure`에 **sync-run 4개 경로만** 실제 HTTP 연결. 상세/복구 GET `run_version` → `{"expected_run_version": N}` 전달, 버전 누락/대상 불일치 시 POST 0건, 의도적 bodyless 재시도 `422` 거절, 실패 A 응답 유실 → 실패 B GET(자동 POST 0건) → A 명시 재전송(추가 작업 0건) → 구버전 `409 RESOURCE_VERSION_CONFLICT`·키 충돌 `409 IDEMPOTENCY_CONFLICT`(추가 작업 0건) → B 새 재시도(+1 작업), `401 SESSION_TERMINATED`·`403 WORKSPACE_SCOPE_VIOLATION`·`404 RESOURCE_NOT_FOUND`·`503 AUTH_VERIFICATION_UNAVAILABLE`·일반 빈 `401/403/409/422` 처리, 새로고침 복구 및 제어 시계(+25h) `IDEMPOTENCY_KEY_EXPIRED`, 늦은 응답 폐기 | 테스트 전용 루프백 연결(프로덕션 UI 미연결). 브라우저 헤더/본문으로 사용자·Workspace·인증 결과 미전달. 실제 DB·Worker·인증·GitHub API 미포함 |
| **미검증 제품 API·Auth 연동** | (미구현 — 추후 정식 앱·제품 백엔드 연동 단계) | 실제 제품 API 베이스 URL, Supabase Auth/JWT·세션 쿠키·호스팅 로그아웃, 실제 GitHub OAuth·저장소 목록 API, 실제 PostgreSQL 영속 24시간 복구·Worker 실행, 운영 폴링 주기/Transport | 본 스파이크 범위 밖. 서버 측 최종 권한·중복·버전 제약 집행은 반드시 백엔드에서 담당 |

---

## 2. 확정된 UI·클라이언트 동작 (Confirmed Behaviors)

아래 동작은 UI 상태 머신(`src/domain/state-machine.ts`), 컴포넌트, 그리고 어댑터/클라이언트 계층에 반영되어 단위·React·루프백 HTTP 테스트로 검증되었습니다.

1. **버전 기반 재시도 (`run_version` → `expected_run_version`)**
   - 상세 GET(`GET /v1/sync-runs/{id}`)의 `run_version`과 복구 GET(`GET /v1/sync-runs?source_resource_id=...&limit=1`)의 `items[0].import_status.run_version`을 반드시 `target_id`에 귀속해 기록한다.
   - `POST /v1/sync-runs/{id}/retry`는 항상 `Idempotency-Key`(UUID)와 JSON 본문 `{"expected_run_version": <int >= 1>}`을 전송한다. body 없는 재시도는 보내지 않는다.
   - `run_version`이 없거나 유효하지 않거나(0 이하·비정수), 화면 실행과 대상 `runId`가 불일치하면 **POST를 0건**만 보내고 GET 상태 갱신을 안내한다. 타임스탬프·체크포인트·다른 실행의 버전·임의 기본값(`1`)을 대입하지 않는다.
2. **이전 미확정 재시도 재전송 vs 최신 상태 새 재시도 분리**
   - 응답 유실·필수 필드 누락 `202`·미분류 `5xx`는 성공이나 확정 실패로 표시하지 않으며, 자동 복구는 **GET만** 사용한다(자동 POST 0건).
   - **이전 미확정 재시도 재전송**(`mode: "resend_pending"`, 별도 `resend-pending-retry-button`): 같은 `runId`·같은 `Idempotency-Key`·같은 최초 `expected_run_version` 본문을 그대로 재전송한다. 중간 GET에서 실패 B의 더 최신 버전을 읽더라도 저장된 실패 A의 키·본문을 덮어쓰지 않는다. UI는 이 동작이 읽기 전용 조회가 아니라 POST이며 원래 요청이 미접수였다면 새 작업의 최초 접수가 될 수 있음을 명시적으로 경고한다.
   - **최신 상태로 새 재시도**(`mode: "new_retry"`, `retry-import-button`): `retry.allowed === true`인 최신 관측 상태에 대해 **새 UUID 키와 최신 `run_version`**으로 요청한다.
   - 대기 키와 재시도 본문은 현재 세션 메모리에만 두고 새로고침 이후 영속화하지 않으며, 인증·세션·Workspace·연결·대상 저장소가 바뀌면 즉시 비운다. 이전 범위의 늦은 응답은 비워진 캐시나 대기 키를 되살리지 못한다.
3. **승인된 오류 응답 (`OpenAPI 0.5.0-draft`) 및 일반 빈 오류 처리**
   - `409 RESOURCE_VERSION_CONFLICT`: 실행 상태 변경을 알리고 GET 갱신을 안내하며 자동 POST(이전 재전송·새 키/버전 재시도 모두)를 보내지 않는다.
   - `409 IDEMPOTENCY_CONFLICT` / `409 IDEMPOTENCY_KEY_EXPIRED`: 각각 사유를 구분해 GET 조회를 안내하고 자동 POST·새 키 자동 발급을 하지 않는다.
   - `401 SESSION_TERMINATED`: 확정된 세션 종료로 처리해 이전 범위의 저장소·실행 상세·성공 목록·복구 안내·대기 키를 정리하고 재로그인을 안내한다.
   - `403 WORKSPACE_SCOPE_VIOLATION`: 해당 Workspace의 저장소 목록·실행 상세·자동 폴링을 닫되 **로그인 상태(`serviceAuth.status === "authenticated"`)는 유지**한다. Workspace 관리 화면은 만들지 않는다.
   - `404 RESOURCE_NOT_FOUND`: 대상 부재와 권한 없음을 구별하지 않는 중립 안내를 표시하고 이전 대상 상세 표시를 중단한다.
   - `503 AUTH_VERIFICATION_UNAVAILABLE`: 로그인 상태를 유지하고 일시적 인증 검증 장애로 안내한다. `retryable: true`를 자동 POST 근거로 삼지 않는다.
   - 계약 오류 코드 없는 일반 빈 `401`/`403`/`409`/`422`: 세션 종료·실행 성공·미접수로 단정하지 않는 일반 오류로 처리하며 자동 POST는 0건이다.
4. **성공이 확인된 동일 저장소의 새 최초 가져오기 요청 차단 (저장소 단위 UI UX 방어)**
   - [MVP API §4](https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa)와 [Snapshot Import §2–3](https://app.notion.com/p/3f0f86591b5f8170940fe98da87609aa) 기준에 따라, 현재 화면 `activeRun`과 세션 내 성공 목록(`succeededRepoIds`, 메모리 전용·비영속)을 함께 확인해 `A 성공 → B 선택 → A 재선택`에서도 A의 새 최초 가져오기 요청을 차단하고 B 요청과 수동 GET은 허용한다.
   - 로그아웃·세션 만료·계정 전환·Workspace 회수·GitHub 연결 변경 시 이전 범위의 실행 상세·성공 목록·복구 안내·대기 키를 즉시 제거한다.
5. **`PARTIAL`/`PARTIAL_WITH_LIMIT` = `import_ready=false`, `import_ready`/후속 처리 분리, 단일 동시 폴링**
   - `PARTIAL` 및 `PARTIAL_WITH_LIMIT`는 성공으로 표시하지 않으며 `PARTIAL_WITH_LIMIT`는 재시도를 비활성화한다.
   - 과거 `SUCCESS`(`import_ready=true`) 이력과 현재 후속 처리 차단(`BLOCKED_POLICY`/`BLOCKED_SOURCE`)을 분리 표시한다.
   - 실행 중에는 응답이 동일해도 다음 단일 동시 GET을 예약하며, 종료 상태·세션 만료·Workspace 회수·권한 상실·화면 종료 시 즉시 중단한다.

---

## 3. Adapter 경계 및 테스트 전용 HTTP 클라이언트 매핑 (`OpenAPI 0.5.0-draft`)

모든 화면 로직은 [`ImportServiceAdapter`](../src/adapters/import-service-adapter.ts) 포트와 [`view-models.ts`](../src/domain/view-models.ts) 뷰 모델로 격리되어 있으며, sync-run 4개 경로의 HTTP 매핑은 [`ContractSyncRunClient`](../src/adapters/contract-sync-run-client.ts)에 캡슐화되어 있습니다.

| Adapter 메서드 | 기대 역할 | `ContractSyncRunClient` 연결 경로 (`OpenAPI 0.5.0-draft`) |
| --- | --- | --- |
| `bootstrapSession()` / `recoverRepositoryRun()` | 현재 세션·저장소의 알려진 실행 상세 조회 또는 ID 부재 시 복구 조회 (`run_version` 수집) | ID 있음: `GET /v1/sync-runs/{id}` / ID 없음: `GET /v1/sync-runs?source_resource_id=...&limit=1` |
| `startInitialImport(input)` | 공개 저장소 1곳 일회성 최초 가져오기 접수 (`Idempotency-Key` UUID) | `POST /v1/sync-runs` (`202` → `target_id`, `status_url` 검증) |
| `pollImportRun(runId)` | 실행 중 폴링 및 종료 실행 수동 상세 재조회 (`run_version` 갱신, `pendingRetry` 비덮어쓰기) | `GET <validated status_url>` (`GET /v1/sync-runs/{id}`) |
| `retryImportRun(runId, { mode, expectedRunVersion })` | `mode="new_retry"`: 최신 `run_version` + 새 UUID 키 / `mode="resend_pending"`: 이전 미확정 키·최초 본문 그대로 재전송 | `POST /v1/sync-runs/{id}/retry` (JSON `{"expected_run_version": <int >= 1>}`, `202` → 원래 `target_id`/`status_url` 일치 검증) |

---

## 4. 아직 미검증인 제품 연동 항목 (범위 밖)

1. **서비스 세션·인증·GitHub OAuth·저장소 목록 제품 API**: 실제 Supabase Auth/JWT·세션 쿠키·호스팅 로그아웃, 실제 Workspace 멤버십 회수, 실제 GitHub OAuth 연결/회수 및 공개 저장소 목록 API는 미연동입니다.
2. **실제 백엔드 영속성·Worker·운영 폴링**: PostgreSQL 트랜잭션 및 서버 재시작 후 24시간 응답 복구, 실제 Worker 실행과 `job_id` 수명 관계, 운영 폴링 주기(`poll_after_ms` 등) 및 Transport 선택, `backfill_from`·`limits.max_objects` 제품 경계는 실제 백엔드 연동 단계에서 검증합니다.
3. **의도적 제외 기능**: Workspace 관리 화면, 계획 폐기(Abandon) API/UI, 새로고침을 넘는 `Idempotency-Key`/재시도 본문 영속화, 삭제·Claim 생성·LLM 연동·Event 편집은 이번 범위에 포함하지 않습니다.
