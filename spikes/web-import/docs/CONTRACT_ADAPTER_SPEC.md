# API 계약 연결 명세 및 Adapter 변환 가이드 (UI -> CONTRACT 요청서)

> **문서 성격**: 본 문서는 `spikes/web-import/` 로컬 UI Fixture 프로토타입이 승인된 MVP API / Snapshot Import 계약과 연결될 때 필요한 **요청 필드, 상태 모델, 오류 코드, 변환(Mapping) 규칙 및 미결 질문**을 정리합니다.
>
> **경계 준수**: 이 단계에서는 임의 HTTP 경로(`/api/...`), 공용 스키마, 세션 쿠키, transport 확정 사항을 코드에 작성하지 않았습니다. 모든 화면 로직은 [`ImportServiceAdapter`](../src/adapters/import-service-adapter.ts) 포트와 [`view-models.ts`](../src/domain/view-models.ts) 뷰 모델로 격리되어 있습니다.

---

## 1. Adapter 교체 지점

현재 UI는 [`MockFixtureImportAdapter`](../src/adapters/mock-fixture-adapter.ts)를 사용합니다. 승인된 계약이 도착하면 새 구현체(예: `ApprovedContractImportAdapter`)만 작성하여 `ImportJourneyApp`에 주입하면 됩니다.

| Adapter 메서드 | 기대 역할 | 승인 계약 연결 시 변환 대상 |
| --- | --- | --- |
| `bootstrapSession()` | 현재 서비스 세션 + 별도 GitHub 수집 연결 상태 + 선택 가능한 공개 저장소 목록 + 최근 실행(`latestRun`) 조회 | 초기 부트스트랩 및 새로고침 복구 응답 -> `BootstrapPayload` |
| `signInService()` / `signOutService()` | 서비스 로그인 상태 전이 (추후 실제 인증 흐름으로 대체) | Auth 상태 응답 -> `ServiceAuthViewModel` |
| `connectGitHub()` / `disconnectGitHub()` | 별도 GitHub 수집 연결 인가 시작/해제 | GitHub 연결 상태 응답 -> `GitHubConnectionViewModel` |
| `startInitialImport(input)` | 공개 저장소 1곳에 대한 일회성 최초 가져오기 요청 (`repoId`, `idempotencyKey`) | 서버 수락 응답(run ID + 초기 상태) -> `ImportRunViewModel` |
| `pollImportRun(runId)` | 실행 중 작업 상태/준비 단계 조회 | Run 상태 조회 응답 -> `ImportRunViewModel` + 최신 `GitHubConnectionViewModel` |
| `retryImportRun(runId)` | 서버가 `retryPolicy.allowed === true`로 허용한 경우에만 호출 | 새 시도(attempt) 또는 재시도 수락 응답 -> `ImportRunViewModel` |

---

## 2. 승인된 API 계약에 요청할 필수 필드

### 2.1 서비스 로그인 상태 (`ServiceAuthViewModel`)
- `authenticated: boolean` 또는 명시적 상태 enum (`authenticated` | `signed_out` | `session_expired`)
- 사용자 표시 식별자(`userId`, `displayName` 또는 `email`)
- **세션 만료 구분**: 서비스 세션 만료 오류가 GitHub 수집 권한 회수와 **명확히 다른 오류 코드**로 구분되어야 함

### 2.2 별도 GitHub 수집 연결 상태 (`GitHubConnectionViewModel`)
- 연결 상태 enum: `disconnected` | `connected` | `permission_revoked` (선택적으로 `connecting` / `pending`)
- 연결된 GitHub 계정 핸들/표시명 (`accountHandle`) 및 연결 식별자 (`connectionId` 또는 `installationRef`)
- 허용 범위 요약 (`scopeSummary`, 공개 저장소 읽기 전용 여부)
- 권한 상실 시 사유 (`revocationReason`) 및 재연결 진입 정보 (인가 URL 또는 재연결 트리거 규약)

### 2.3 공개 저장소 선택 목록 (`PublicRepositoryOption`)
- `repoId` (안정적 내부/외부 식별자), `fullName` (`owner/repo`), `visibility` (`public` 필터 보장 여부)
- `defaultBranch`, `primaryLanguage`, `description`, `lastPushedAt`
- (선택) 규모 사전 안내용 메타데이터 또는 상한 초과 가능성 힌트

### 2.4 최초 가져오기 실행 상태 (`ImportRunViewModel`)
- `runId`, `repositoryId`, `repositoryFullName`, `attemptNumber`, `startedAt`, `updatedAt`
- **상위 실행 상태 enum**:
  - `queued` / `running` (UI는 실행 중으로 통합 표시 가능)
  - `succeeded` (전체 성공)
  - `partial` (부분 결과 — **절대 `succeeded`로 합치지 말 것**)
  - `limit_reached` (Rate Limit 또는 MVP 수집 상한 도달 — **절대 `succeeded`로 합치지 말 것**)
  - `failed` (실패)
  - `permission_revoked` (실행 중 GitHub 권한 회수)
- **준비 단계 분리 필드 (`ReadinessViewModel`)**:
  - `import_ready: boolean` (원본 스냅샷 준비 여부) + 세부 상태 (`pending` | `in_progress` | `ready` | `partial` | `failed`)
  - `derived_stages[]`: 각 파생 단계의 `key`, `status` (`not_started` | `pending` | `in_progress` | `ready` | `skipped` | `failed`), `detail`
  - **필수 요구사항**: `import_ready === true`이면서 파생 단계가 `in_progress` / `skipped` / `failed`인 상태를 서버 응답이 독립 필드로 내려주어야 함
- **서버 주도 재시도 정책 (`RetryPolicyViewModel`)**:
  - `retry_allowed: boolean` (UI가 오류 코드로 추측하지 않고 **서버가 내려준 이 값만으로 재시도 버튼 노출 여부를 결정**)
  - `retry_reason: string` (허용 또는 거부 사유 — 사용자 안내 문구/매핑 키)
  - `retry_after_at?: string | null` (Rate limit 쿨다운 시각)
- **결과 세부 필드**:
  - 성공/부분 요약: `commits_captured`, `pull_requests_captured`, `captured_at`
  - 부분 결과 상세: `skipped_commits`, `skipped_pull_requests`, `omission_reasons[]`
  - 한도 상세: `limit_kind` (`rate_limited` | `import_cap_reached`), `captured_count`, `cap_count`, `reset_at`
  - 오류 상세: 안정적 `error_code`, 사용자 안내용 메시지 또는 코드 매핑 테이블

---

## 3. CONTRACT에 요청하는 오류 코드 및 미결 질문

1. **중복 요청 / 멱등성 (`Idempotency-Key` vs 서버 단일 실행 락)**
   - 동일 사용자가 동일 저장소에 최초 가져오기를 중복 클릭하거나 두 탭에서 동시 호출할 때, 서버가 기존 `running` 작업의 `runId`를 반환(200/202)하는지 아니면 `409 CONFLICT`와 기존 `runId`를 함께 내려주는지 확정이 필요합니다.
2. **새로고침 복구용 "최근 실행 조회" 엔드포인트 형태**
   - 브라우저 새로고침 시 클라이언트가 로컬 스토리지/쿠키에 `runId`를 들고 있지 않더라도, 서버에서 "현재 사용자의 최근(또는 진행 중인) 최초 가져오기 실행"을 조회할 수 있는 읽기 계약이 있는지 확인이 필요합니다.
3. **세션 만료 vs GitHub 권한 상실 오류 코드 분리**
   - 예: `AUTH_SESSION_EXPIRED` vs `GITHUB_PERMISSION_REVOKED`가 HTTP 상태 코드 및 바디 `code`에서 명확히 구분되는지 확인이 필요합니다.
4. **부분 결과(`partial`) 및 수집 상한(`import_cap_reached`) 시 `import_ready` 의미**
   - 상한으로 절단되었거나 일부 PR이 누락된 경우 서버가 `import_ready: true` + `status: partial|limit_reached`로 내려주는지, 아니면 `import_ready: false`로 내려주는지 계약 기준 확정이 필요합니다.
5. **파생 단계 목록 및 상태 enum 확정**
   - 현재 프로토타입은 예시로 `event_normalization`, `timeline_indexing` 2단계를 두었습니다. 실제 MVP 파생 단계 key/이름/상태 enum과, `import_ready: true` 이후 파생 단계만 실패했을 때의 재시도 정책 확정이 필요합니다.
6. **상태 갱신 Transport 및 폴링 주기**
   - MVP에서 상태 조회를 단기 폴링(권장 interval 또는 `poll_after_ms` 서버 힌트 제공 여부)으로 할지, SSE/WebSocket을 사용할지 계약 확정이 필요합니다.
7. **일회성 최초 가져오기 이후 재선택/재실행 정책**
   - 이미 한 저장소가 `succeeded` 또는 `limit_reached(non-retryable)`로 끝난 뒤, 사용자가 다른 공개 저장소를 선택하는 것이 허용되는지(또는 1회 제한인지) 확인이 필요합니다.
