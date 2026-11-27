# Career Memory — 연결 및 일회성 최초 가져오기 UI 프로토타입 (`spikes/web-import`)

이 폴더는 **Next.js 기반 일회성 최초 가져오기 사용자 여정**을 검증하기 위한 격리된 프론트엔드 프로토타입입니다. 화면 자체는 로컬 UI Fixture로 동작하며, 별도 테스트 스위트에서 **로컬 FastAPI stub과의 실제 루프백(`127.0.0.1`) HTTP 연동**까지 검증합니다.

## 검증 계층과 구현 경계

이 워크스페이스의 구현과 검증은 아래 **네 구분**(모의 화면·E2E / 이전 `1d754a5` 기록 / 최신 `87f1515` 루프백 교차 검증 / 미검증 제품 연동)으로 명확히 나뉩니다.

### 1. 모의 화면 및 브라우저 E2E (로컬 UI Fixture — 루프백 HTTP와 별개)
- **코드**: `MockFixtureImportAdapter` + `SyntheticOpenApiTransport` + `ImportJourneyApp` (`src/`), Playwright E2E (`e2e/import-journey.spec.ts`), Vitest 단위/UI 테스트 (`src/**/*.test.ts(x)`).
- **검증 대상**:
  - 모의 서비스 로그인 상태와 별도 GitHub 수집 연결 상태 분리 표시
  - 합성 공개 저장소 1곳 선택 및 일회성 최초 가져오기 요청
  - 요청 중(서버 미확정) · 실행 중 · 성공 · 부분 결과(`PARTIAL`) · 한도 도달(`PARTIAL_WITH_LIMIT`) · 오류 · 권한 상실 · 세션 만료 · 네트워크 실패 · 새로고침 복구 경로
  - `import_ready`와 후속 처리 상태(`processing_status` / 파생 단계) 분리 표시 (`PARTIAL`·`PARTIAL_WITH_LIMIT`는 `import_ready=false`)
  - 서버 `retryPolicy.allowed === true`일 때만 새 재시도 버튼 노출, 미확정 이전 재시도는 별도 명시적 재전송 패널/버튼으로 분리
  - 실행 중 중복 클릭 방지, **이 세션에서 성공이 확인된 동일 저장소의 재요청 차단(`A 성공 → B 선택 → A 재선택` 포함, 메모리 기반 UI UX 방어)**, 계정/세션 전환 시 이전 범위 상태 정리, 키보드 전용 조작
- **한계**: 브라우저 E2E(`npm run test:e2e`)와 `npm test`는 로컬 UI Fixture / 합성 HTTP 전송만 사용하며 실제 루프백 FastAPI stub·백엔드·DB·인증·GitHub API는 호출하지 않습니다.

### 2. 로컬 FastAPI stub 실제 루프백 HTTP 테스트 — 이전 기록 vs 현재 통과 검증

#### A. 이전 검증 기록 (`1d754a5bc33cf166d92b1c1ce8568031859ff0a4`, `OpenAPI 0.3.0-draft`)
- 이전 `WEB-06` / `WEB-SPEC-SYNC` 구간에서는 CONTRACT 커밋 `1d754a5` (`OpenAPI 0.3.0-draft`)의 body 없는 `POST /v1/sync-runs/{id}/retry` 및 빈 `403`/`409`/`422` 응답을 기준으로 루프백 HTTP 회귀를 검증했습니다.

#### B. 이번에 실제로 통과한 교차 검증 (`87f1515cbe74bf02b0aece68904bf9a641c74c60`, `OpenAPI 0.5.0-draft`)
- **고정 계약 및 추출 방식**:
  - CONTRACT 커밋 **`87f1515cbe74bf02b0aece68904bf9a641c74c60`** (`OpenAPI 0.5.0-draft`, `contracts/import-v1/openapi.yaml` Git blob **`07594d75425744d6149250a9f7ff95ac24659f80`**)에서 `git archive`로 `contracts/import-v1/openapi.yaml`과 `spikes/import-contract/`만 임시 폴더에 추출해 `CONTRACT_IMPORT_DIR` + `UV_OFFLINE=1`로 고정 실행했습니다.
  - `127.0.0.1` 에페메럴 포트에서 CONTRACT `create_app` + Python 주입 `FixtureActor`/`FixtureAuthFailure`에 **sync-run 4개 경로만** 연결합니다. 브라우저 헤더나 요청 본문으로 사용자·Workspace·인증 결과를 결정하지 않습니다.
    - `POST /v1/sync-runs`
    - `GET /v1/sync-runs?source_resource_id=...&limit=1`
    - `GET /v1/sync-runs/{id}` (`target_id`/`status_url` 일치 검증)
    - `POST /v1/sync-runs/{id}/retry` (`Idempotency-Key` UUID + JSON `{"expected_run_version": <int >= 1>}`)
- **실제 통과한 `127.0.0.1` 루프백 검증 항목 (18/18 통과)**:
  - **버전 전달 및 방어**: 상세 GET의 `run_version`과 복구 GET의 `items[0].import_status.run_version`을 `target_id`별로 귀속해 `{"expected_run_version": N}`으로 전송. 버전 누락·무효·대상 불일치 시 **POST 0건**, 의도적 bodyless 재시도는 stub이 **`422`로 거절**(추가 작업 0건).
  - **응답 유실 → GET → 명시적 재전송 vs 충돌 vs 새 재시도**: 실패 A 재시도 수락 후 `202` 유실 → 서버가 실패 B(최신 버전)로 진행 → 복구는 **GET만** 수행(자동 POST 0건) → 실패 A 명시 재전송(원래 키 + 원래 버전 본문)은 기존 `202`만 복구(**추가 작업 0건**) → 다른 키 + 구버전 A는 **`409 RESOURCE_VERSION_CONFLICT`**(추가 작업 0건, 자동 POST 0건) → 같은 키 + 바뀐 본문은 **`409 IDEMPOTENCY_CONFLICT`**(추가 작업 0건, 자동 POST 0건) → 실패 B에서 새 재시도(새 키 + 최신 버전)만 **추가 작업 +1건** 생성.
  - **승인 오류·권한·장애**: `401 SESSION_TERMINATED`(보호 상태 제거·재인증 안내), `403 WORKSPACE_SCOPE_VIOLATION`(Workspace 저장소·실행 상세·폴링만 닫고 로그인 유지), `404 RESOURCE_NOT_FOUND`(대상 부재/권한 없음 비구별 중립 안내·이전 상세 숨김), `503 AUTH_VERIFICATION_UNAVAILABLE`(`retryable: true`여도 로그인 유지·자동 POST 0건), 일반 빈 `401`/`403`/`409`/`422` 중립 처리, 스푸핑 인증/Workspace 헤더 무시.
  - **기존 회귀 유지**: 정상 `202 -> 200`, `PARTIAL_WITH_LIMIT`(`import_ready=false`, 재시도 비활성), `SUCCESS` + `BLOCKED_POLICY`/`BLOCKED_SOURCE` 분리, `409 SYNC_ALREADY_RUNNING`(`existing_run_id` 유무 분기), 응답 유실 후 새로고침 활성/종료 복구, 제어 시계(+25h) `409 IDEMPOTENCY_KEY_EXPIRED`, `target_id`/`status_url` 불일치 거부, 로그아웃·세션·저장소 전환 후 늦은 응답 폐기.

### 3. 아직 미검증인 제품 연동 (의도적 범위 밖)
- 실제 제품 백엔드 HTTP 연동, Supabase Auth/JWT·세션 쿠키·호스팅 로그아웃, 실제 GitHub OAuth·토큰·공개 저장소 목록 API
- 실제 PostgreSQL 트랜잭션·서버 재시작 후 24시간 영속 응답 복구·Worker 실행·운영 폴링 주기(`poll_after_ms` 등)/전송 방식
- Workspace 관리 화면, 계획 폐기(Abandon) API/UI, 새로고침을 넘는 `Idempotency-Key`/재시도 본문 영속화, 삭제·Claim 생성·LLM 연동·Event 편집
- **중요**: UI의 저장소별 세션 내 성공 기억 및 재요청 차단은 클라이언트 UX 방어일 뿐이며, **제품 서버의 중복·권한·상태 제약 집행을 UI가 대신하지 않습니다**.

## 로컬 실행 방법

```bash
cd "spikes/web-import"
npm install
npm run dev
```

브라우저에서 `http://localhost:3100` 접속.

프로덕션 빌드 후 실행:

```bash
npm run build
npm run start
```

## 테스트 명령

```bash
# 타입 검사
npm run typecheck

# 1) 단위 및 UI 통합 테스트 (Vitest + Testing Library, 로컬 UI Fixture + 합성 HTTP)
npm test

# 2) 로컬 FastAPI stub 실제 루프백 HTTP 통합 테스트 (127.0.0.1, OpenAPI 0.5.0-draft)
#    선행 조건: spikes/web-import/node_modules 설치, uv 사용 가능,
#    CONTRACT 커밋 87f1515cbe74bf02b0aece68904bf9a641c74c60 추출본을 CONTRACT_IMPORT_DIR로 지정
CONTRACT_IMPORT_DIR="<extracted>/spikes/import-contract" UV_OFFLINE=1 npm run test:contract-http

# 3) 프로덕션 빌드 및 브라우저 E2E 테스트 (Playwright, 로컬 UI Fixture 전용)
npm run build
npm run test:e2e
```

## 주요 화면 확인 방법 (모의 UI Fixture)

페이지 상단의 **로컬 UI Fixture 시나리오 제어판**에서 아래 시나리오를 선택하고 "최초 가져오기 요청"을 눌러 주요 UI 상태를 확인할 수 있습니다.

1. `정상 성공 (import_ready + 파생 완료)`: 요청 중(미확정) → 실행 중(`import_ready=true`, 파생 진행 중) → 완료. 완료 후 같은 저장소의 새 최초 가져오기 CTA가 비활성화되며 수동 상태 확인과 다른 저장소 선택은 유지됨을 확인.
2. `스냅샷만 준비 (import_ready=true, 파생 미완료)` / `성공 이력 + 정책·원본 차단`: `import_ready=true`와 현재 후속 처리 상태(`BLOCKED_POLICY`/`BLOCKED_SOURCE` 포함) 구분 배너 확인.
3. `부분 결과 (재시도 허용 / 거부)`: 성공 배지가 아닌 경고 배너, `import_ready=false`, `retryPolicy.allowed`에 따른 재시도 버튼 노출/숨김 확인.
4. `한도 도달 (PARTIAL_WITH_LIMIT 등)`: 한도 상세, `import_ready=false`, 재시도 거부 확인.
5. `오류 (재시도 허용 / 거부)`: 오류 상세 및 서버 정책 기반 재시도 버튼 노출/숨김 확인.
6. `권한 상실 (GitHub 수집 권한 회수)`: 서비스 로그인은 유지되면서 GitHub 연결만 `권한 상실`로 전이, 재시도 대신 재연결 버튼 노출 확인.
7. `세션 만료 / 네트워크 실패 (요청 시점)`: 작업 확정으로 표시하지 않고 미확정 안내 및 복구 버튼 노출 확인.
8. `장기 실행 (새로고침 복구 테스트용)`: 실행 중 버튼 중복 클릭 시 차단 안내 확인 후, 브라우저 실제 새로고침(`Cmd+R` / `F5`) 또는 `새로고침 복구 확인` 버튼으로 중복 요청 없는 상태 복구 확인.
9. **키보드 전용 조작**: `Tab`으로 이동, 저장소 라디오에서 `↑`/`↓`로 선택, `Enter`로 요청 실행 확인.

## 계약 연동 문서

구현된 테스트 전용 HTTP 클라이언트 범위, 확정된 UI 동작, `1d754a5` 이전 기록과 `87f1515` (`0.5.0-draft`) 교차 검증 결과 및 미검증 제품 연동 경계는 [docs/CONTRACT_ADAPTER_SPEC.md](./docs/CONTRACT_ADAPTER_SPEC.md)를 참고하세요.
