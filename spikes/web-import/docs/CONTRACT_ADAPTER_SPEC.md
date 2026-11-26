# API 계약 연결 명세 및 Adapter 변환 가이드 (UI -> CONTRACT)

> **문서 성격**: 본 문서는 `spikes/web-import/` 로컬 UI Fixture 프로토타입이 승인된 MVP API / Snapshot Import 계약과 연결될 때의 **확정된 동작**, **Adapter 변환 기준**, 그리고 **계약 승인 대기 / 보류 질문**을 구분해 정리합니다.
>
> **경계 준수**: 이 단계에서는 실제 HTTP 어댑터, 임의 HTTP 경로(`/api/...`), 공용 스키마, 세션 쿠키, transport 확정 사항을 코드에 작성하지 않았습니다. 모든 화면 로직은 [`ImportServiceAdapter`](../src/adapters/import-service-adapter.ts) 포트와 [`view-models.ts`](../src/domain/view-models.ts) 뷰 모델로 격리되어 있습니다.

---

## 1. 확정된 동작 (Confirmed Behaviors)

아래 동작은 UI 및 로컬 Fixture 프로토타입에 반영되어 있으며, 추후 계약 어댑터 구현 시 그대로 유지합니다.

1. **`PARTIAL` 및 `PARTIAL_WITH_LIMIT`는 `import_ready=false`다**
   - 부분 수집(`PARTIAL`) 또는 수집 상한에 의한 절단(`PARTIAL_WITH_LIMIT`) 결과는 원본 스냅샷 준비 완료(`import_ready=true`)로 간주하지 않으며, UI에서 성공 배지로 표시하지 않는다.
2. **`import_ready`와 후속 처리 상태의 구분 (단, `derived_stages[]`는 서버 필수 필드가 아님)**
   - UI는 `import_ready`와 후속(파생) 처리 상태를 구분해 표시한다.
   - 다만 로컬 모의 화면의 `derived_stages[]` 배열은 **UI 표현용 뷰 모델 구조**이며, 서버 계약에 `derived_stages[]` 배열을 필수 필드로 요구하지 않는다. 서버가 단일 후속 처리 상태 필드나 요약 필드를 내려주더라도 Adapter 계층에서 UI 뷰 모델로 변환한다.
3. **서버 주도 재시도 게이팅**
   - UI는 오류 코드를 보고 임의로 재시도 가능 여부를 추측하지 않으며, 서버가 재시도 허용(`retryPolicy.allowed === true`)을 명시할 때만 재시도 버튼을 노출한다.
4. **응답 불변 시에도 멈추지 않는 단일 동시 조회(Single-Flight Polling) 및 중단 규칙**
   - 실행 중(`running`)에는 서버가 동일한 `runId`·`status`·`updatedAt`을 연속 반환하더라도 다음 상태 조회를 예약한다.
   - 상태 조회 요청은 한 번에 하나만 진행하며, 최종 상태(`succeeded`/`partial`/`limit_reached`/`failed`/`permission_revoked`)·세션 만료·권한 상실·화면 종료(unmount) 시 즉시 중단한다.
   - 이전 작업의 늦은 응답이 현재 화면을 덮어쓰지 않도록 작업 토큰과 `runId`를 검증해 폐기한다.
   - 조회 오류 발생 시 새 가져오기 요청(`startInitialImport`)으로 자동 전환하지 않는다.
5. **서비스 세션 만료와 GitHub 수집 권한 상실 분리**
   - 서비스 로그인 세션 만료와 별도 GitHub 수집 권한 회수는 독립 상태·안내·복구 CTA로 구분한다.

---

## 2. Adapter 교체 지점 및 최소 변환 대상

| Adapter 메서드 | 기대 역할 | 승인 계약 연결 시 변환 대상 |
| --- | --- | --- |
| `bootstrapSession()` | 현재 연결 상태 및 최근/진행 중 실행 복구 | 부트스트랩/복구 응답 -> `BootstrapPayload` |
| `startInitialImport(input)` | 공개 저장소 1곳에 대한 일회성 최초 가져오기 요청 (`repoId`, `idempotencyKey`) | 서버 수락 응답(`runId` + 초기 상태) -> `ImportRunViewModel` |
| `pollImportRun(runId)` | 실행 중 작업 상태 및 `import_ready` / 후속 처리 상태 조회 | Run 상태 조회 응답 -> `ImportRunViewModel` (+ 권한 변화 반영) |
| `retryImportRun(runId)` | 서버가 재시도를 허용한 경우에만 호출 | 재시도 수락 응답 -> `ImportRunViewModel` |

> **최소 핵심 실행 응답 기대 필드 (Snapshot Import 계약 중심)**
> - `runId`, 대상 저장소 식별자, `status` (`running` / `succeeded` / `partial` / `partial_with_limit` 또는 `limit_reached` / `failed` / `permission_revoked`), `attemptNumber`, `updatedAt`
> - `import_ready: boolean` (`PARTIAL`, `PARTIAL_WITH_LIMIT`, `FAILED`, `PERMISSION_REVOKED`에서는 `false`)
> - 후속 처리 준비 여부/상태를 구분할 수 있는 서버 상태 필드 (형식 자유 — `derived_stages[]` 배열 필수 아님)
> - 재시도 허용 여부(`retry_allowed: boolean`) 및 거부/허용 사유(`retry_reason`, 선택적 `retry_after_at`)
> - 부분/한도/오류 결과 사유 및 안정적 오류 코드

---

## 3. 계약 승인 대기 질문 (CONTRACT 확인 필요)

아래 항목만 현재 Snapshot Import 계약 승인 과정에서 확정이 필요합니다.

1. **중복 요청(동일 사용자·저장소 동시 제출) 응답 규약**
   - 동일 저장소 최초 가져오기가 이미 실행 중일 때 서버가 기존 `running` 작업의 `runId`를 멱등 반환(200/202)하는지, 아니면 `409 CONFLICT`와 함께 기존 `runId`를 내려주는지 확정 필요.
2. **`import_ready=true` 이후 후속 처리 상태 표현 필드 및 실패 시 재시도 정책**
   - 서버가 후속 처리 상태를 어떤 필드/enum으로 내려주는지(단일 상태/요약 필드 포함, `derived_stages[]` 비필수)와, 원본 스냅샷(`import_ready=true`) 완료 후 후속 처리만 지연·실패했을 때 재시도 허용 여부 확정 필요.
3. **비성공 종료(`PARTIAL`, `PARTIAL_WITH_LIMIT`, `FAILED`, `PERMISSION_REVOKED`) 세부 필드 및 오류 코드 매핑**
   - 누락/한도 사유, `reset_at`(또는 `retry_after_at`), 재시도 거부 사유를 내려주는 정확한 필드명 및 오류 코드 규약 확정 필요.
4. **일회성 최초 가져오기 종료 후 다른 공개 저장소 재선택 정책**
   - 한 저장소가 `succeeded` 또는 재시도 불가 한도(`PARTIAL_WITH_LIMIT`)로 종료된 뒤 다른 공개 저장소를 선택해 새 최초 가져오기를 요청할 수 있는지 여부 확정 필요.

---

## 4. 보류 항목 (Deferred — 이번 계약 질문에서 제외)

아래 항목은 현재 Snapshot Import 계약 승인 범위에서 **보류**하며, 후속 인증·운영 설계 단계에서 별도 확정합니다.

- **새로고침 시 `runId`가 없는 경우의 최근 실행 조회 API**: 클라이언트가 `runId`를 보유하지 않은 상태에서 최근/진행 중 실행을 찾는 서버 조회 계약.
- **운영 환경 상태 조회 주기 및 Transport**: 실제 폴링 간격(`poll_after_ms` 서버 힌트 포함) 및 폴링 vs SSE/WebSocket 선택.
- **인증 상태 및 공개 저장소 목록 응답 필드 상세**: 서비스 로그인 세션 응답 스키마, 별도 GitHub 연결 메타데이터, 공개 저장소 목록 조회 API 필드.
