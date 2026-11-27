# CONTRACT-06 최초 가져오기 인수인계

[OpenAPI 0.5.0-draft](openapi.yaml)(Git blob `07594d75425744d6149250a9f7ff95ac24659f80`)는 직전 CONTRACT-04 기준(`54a8906aaf460e6a5c32a76f8079734411b4d0d6`) 위에 CONTRACT-06 체크포인트(`87f1515cbe74bf02b0aece68904bf9a641c74c60`)로 승인 고정한 최초 가져오기 네 경로 계약을 담는다. CONTRACT-06의 승인 항목은 JSON `expected_run_version` 전달과 `409 RESOURCE_VERSION_CONFLICT` 응답이다.

## 승인 반영

| 경로 | 인수인계 |
| --- | --- |
| `GET /v1/sync-runs?source_resource_id=…&limit=1` | 실행 ID를 잃었을 때 사용한다. 현재 Workspace·저장소·실행 읽기 권한을 먼저 적용한다. 읽을 수 있는 활성 실행을 우선하고 그 외에는 마지막 종료 전이가 가장 최근인 실행을 최대 한 건 반환한다. 동률은 늦게 생성된 실행이 우선이다. 각 항목의 `import_status.run_version`은 해당 실행의 버전이다. 빈 목록은 그 시점에 읽을 수 있는 실행이 없다는 뜻이다. 직전 접수 여부는 계속 미확정이며, 반환된 실행도 그 요청의 접수를 증명하지 않는다. |
| `GET /v1/sync-runs/{id}` | ID를 알고 있을 때 페이지 재진입이나 사용자 새로고침에 사용한다. 조회마다 읽기 권한을 확인하고 `run_version`을 반환한다. 종료 후 자동 폴링을 멈추고 수동 재조회에서 이전 `SUCCESS`와 최신 `processing_status`를 함께 읽는다. |
| `POST /v1/sync-runs` | 동일 Workspace·동작·키·요청은 최초 커밋된 접수부터 24시간 동안 현재 권한 확인 후 원래 `202`의 실행 ID·조회 경로를 복구한다. 성공한 저장소의 재접수와 미완료 계획의 교체는 차단한다. |
| `POST /v1/sync-runs/{id}/retry` | 안전 checkpoint가 있는 일시 실패의 같은 run·계획만 재개한다. JSON `expected_run_version`이 필수다. 신규 접수에서는 요청 버전과 재시도 가능 여부를 같은 상태 변경 안에서 확인한다. 버전이 다르면 `409 RESOURCE_VERSION_CONFLICT`를 반환하고 실행·계획·checkpoint·Job을 유지한다. 접수가 끝난 동일 키·동일 run·동일 버전 요청은 현재 권한 확인 후 저장된 `202`만 복구한다. |

두 GET은 Job·GitHub 호출·재시도 상태 변경 없이 조회한다. `target_id`와 `status_url`은 같은 실행을 가리킨다. 응답 유실 또는 분류되지 않은 mutation `5xx`에서는 WEB이 GET으로만 자동 복구한다. 원래 키와 동일 요청을 사용자가 직접 다시 보내는 것은 별도의 POST다. 처음 요청이 접수되지 않았다면, 현재 조건을 만족할 때 이 POST가 최초 접수와 작업 생성을 일으킬 수 있다.

재전송은 24시간을 연장하지 않는다. 알려진 만료 키는 새 작업 없이 `409 IDEMPOTENCY_KEY_EXPIRED`를 반환한다. 시간 기반 만료 표식 정리는 보류한다. Source 삭제 정책은 계속 적용되며 정리 보류는 별도 보존 권한을 부여하지 않는다. 표식 정리 시점과 제거 후 지연 재전송의 차단 방식은 별도 결정이 필요하다.

MVP API §3의 공통 형식으로 `error.code`, `message`, `retryable`, `request_id`를 반환한다. `details`는 권한이 확인된 정보에 한해 선택 필드다. [MVP API Specification](https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa)

| 상태와 코드 | Backend·WEB 처리 |
| --- | --- |
| `401 SESSION_TERMINATED` | 신뢰된 확인 결과로 세션 종료가 확정된 경우에만 해당한다. WEB은 해당 세션을 정리하고 재인증을 안내한다. |
| `403 WORKSPACE_SCOPE_VIOLATION` | 공개 가능한 Workspace 문맥에서 확인된 권한 회수다. 해당 범위를 닫고 다른 활성 Workspace와 로그인은 유지한다. |
| `404 RESOURCE_NOT_FOUND` | 부재·타 Workspace·은닉 대상에는 추적값을 제외하고 동일한 응답을 반환한다. 해당 ID나 존재 단서는 제공하지 않는다. |
| `503 AUTH_VERIFICATION_UNAVAILABLE` | 인증 확인 일시 장애에서 민감 작업의 쓰기와 Job 생성은 0건이다. WEB은 세션을 유지하고 일시 장애를 안내한다. `retryable`은 나중에 다시 확인할 수 있다는 뜻이다. |

Stub의 인증 결과는 테스트가 주입하는 신뢰된 의존성에서만 온다.

## 미확정 오류

| 로컬 fixture의 빈 응답 | 제품·WEB 결정 대기 |
| --- | --- |
| 일반 `401` | 자격 정보 누락이나 분류되지 않은 인증 실패의 응답 매핑. 확인된 세션 종료에는 승인된 `SESSION_TERMINATED`를 사용한다. |
| 입력 오류 `422` | 본문·버전·키가 누락되거나 잘못됐을 때의 제품 오류 코드·본문과 WEB 안내. 기존 bodyless 요청도 여기에서 거절된다. |
| 기타 `409` | 안전한 재개 조건을 만족하지 않거나 미완료 계획을 교체하려는 경우의 제품 응답 매핑. 버전 충돌에는 승인된 공통 오류 봉투를 사용한다. |

## WEB 인수인계

상세 조회의 `run_version` 또는 복구 조회의 해당 항목에서 받은 `import_status.run_version`을 같은 실행의 재시도 JSON `expected_run_version`으로 보낸다. 응답 유실은 GET으로 확인한다. 사용자가 기존 접수를 명시적으로 다시 보내면 같은 키와 같은 본문을 사용한다. 최신 버전으로 새 재시도를 시작하려면 사용자가 다시 선택해야 한다. 기존 WEB의 bodyless 호출을 JSON 호출로 바꾸는 작업과 WEB HTTP 회귀 검증이 남아 있다.

| `error.code` | 표시와 조회 | 자동으로 전송하지 않을 요청 |
| --- | --- | --- |
| `RESOURCE_VERSION_CONFLICT` | 실행 상태가 바뀌었음을 안내하고 GET으로 최신 상태와 버전만 갱신한다. | 옛 요청 재전송, 새 키·새 버전의 재시도. 새 시도는 사용자 선택 후에만 요청한다. |
| `IDEMPOTENCY_CONFLICT` | 같은 키에 이전과 다른 요청이 사용됐음을 안내한다. 재시도에서는 실행 ID와 예상 버전의 일치 여부를 확인한다. 현재 상태는 GET으로 확인한다. | 기존 키에 변경한 본문을 재전송하거나 새 키로 자동 대체하는 POST. |
| `IDEMPOTENCY_KEY_EXPIRED` | 접수 응답 복구 기간이 끝났음을 안내하고, 실행 ID가 있으면 상세 GET, 없으면 복구 GET으로 확인한다. | 만료 키의 자동 재전송 또는 새 키로 자동 전환한 접수·재시도. |

## Notion MVP API 인수인계

아래 문구는 최초 가져오기 경로의 문서 갱신에 사용할 수 있다. [MVP API §3·4·9](https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa)

| 절 | 반영 문구 |
| --- | --- |
| §3 공통 오류 | 신규 재시도의 실행 버전이 다르면 `409 RESOURCE_VERSION_CONFLICT`를 공통 `error` 봉투로 반환한다. `retryable=false`이며 WEB은 GET으로 실행 상태를 갱신한다. |
| §4 Source / Sync API | 상세 GET과 복구 GET 결과는 해당 실행의 `run_version`을 제공한다. `POST /v1/sync-runs/{id}/retry` 요청의 JSON에는 `expected_run_version`이 필수다. 신규 요청은 현재 읽기 권한 확인 뒤 버전과 재시도 가능 여부를 같은 상태 변경 안에서 검사한다. 버전이 다르면 실행·계획·checkpoint·Job을 유지한다. |
| §9 Async Job | 동일 키·실행·예상 버전으로 24시간 이내에 재전송된 접수는 현재 권한 확인 뒤 원래 `202`를 복구한다. 현재 실행 버전이 달라져도 추가 재시도는 시작하지 않는다. 보존 기간 안에 같은 키에 다른 버전을 제공하면 `IDEMPOTENCY_CONFLICT`, 알려진 만료 키는 `IDEMPOTENCY_KEY_EXPIRED`를 반환한다. |

검증 근거는 [FastAPI 계약 테스트](../../spikes/import-contract/test_contract.py)다. 기존 37건의 범위를 유지해 38건이 통과했다. 버전 A의 저장 응답 복구, 다른 키의 오래된 A 요청에서 오류 봉투와 상태·작업 수 불변, 버전 B 명시 요청의 1회 접수 및 재전송, bodyless 거절을 HTTP 응답으로 확인했다. OpenAPI 검증도 통과했다. 증거는 로컬 stub 범위다.

## 범위 밖

계획 폐기 API·UI는 첫 구간에서 보류한다. 사용자는 실행 상태와 남은 범위를 조회하고 `retry.allowed`가 참인 경우 같은 계획을 재개할 수 있다. `PLAN_DRIFT`·한도 도달·정책 차단 상태에서 새 키나 기간으로 계획을 교체할 수 없다. `simulate_future_abandonment`는 복구 정렬과 미래 이력을 만드는 테스트 함수로, 공개 HTTP 경로는 네 개다.

제품 백엔드·DB 마이그레이션, 실제 Supabase·GitHub·Cloud Run 연동, 배포·Notion 상태, Source Account·Resource 연결과 OAuth 결정, 공용 CI 및 T-106 전체 완료는 별도 작업이다.

## 실제 DB·호스팅 검증 대기

- PostgreSQL에서 실행 생성·재개 상태 전이, Job/outbox, 멱등 키 기록과 원래 `202`를 한 트랜잭션에 커밋하고 Worker 알림은 커밋 뒤에 발생하는지 확인한다. 커밋 전 실패에서 모두 롤백되고, 커밋 후 응답 유실 및 실제 API 재시작 뒤에는 동일 `202`가 복구돼야 한다.
- 동시에 실행한 접수·재시도에서 DB 제약과 잠금 또는 원자적 조건부 갱신이 중복을 막는지 확인한다. 실행 버전과 재시도 가능 여부를 함께 검사한다. 체크포인트 위치가 같은 실패 A·B에서 A의 재전송은 저장 응답만 복구하고, A의 미접수 요청은 `409 RESOURCE_VERSION_CONFLICT`로 쓰기 없이 거부되며, B의 명시 요청은 한 번만 새 작업을 생성해야 한다.
- 실제 테넌트 격리와 권한 회수 경합, 24시간 경계, 만료 키와 Source 삭제·표식 정리의 상호작용을 검증한다. 호스팅 인증의 종료·회수·장애와 WEB 세션 유지를 검증한다.
- 로컬 [FastAPI stub](../../spikes/import-contract/stub.py)은 테스트 시계, 잠금과 인메모리 기록으로만 위 동작을 모의한다. 로컬 통과 증거의 범위는 HTTP 계약과 상태·작업 수 변화다.

---

아래는 `0.3.0-draft`와 CONTRACT-04 당시 기록이다. 현재 계약은 위 CONTRACT-06 인수인계를 따른다. CONTRACT-04 인수인계의 커밋은 `54a8906aaf460e6a5c32a76f8079734411b4d0d6`이다.

# CONTRACT-03 최초 가져오기 복구 검토본

기준은 CONTRACT 커밋 `a0314f2cbcb33f1d34ed486db7b1009dcb4adc59`다. WEB 커밋 `c80008dd28ccf65e03847ebf1763f8a618138d33`의 세 경로 대응을 참고했다. 당시 `0.3.0-draft` 스키마는 `git show 1d754a5bc33cf166d92b1c1ce8568031859ff0a4:contracts/import-v1/openapi.yaml`로 조회한다.

## WEB에서 사용할 응답

| 경로 | 검토본에서 사용할 요청·응답 | 승인 대기 |
| --- | --- | --- |
| `POST /v1/sync-runs` | UUID `Idempotency-Key`와 JSON `source_resource_id`, `mode: INITIAL_BACKFILL` 필수. `backfill_from`과 `limits.max_objects`는 선택. `202`의 필수 필드는 `target_id`, `status_url` | 기간·한도 기본값과 경계, 키 보존 기간 |
| `GET /v1/sync-runs?source_resource_id=…&limit=1` | 실행 ID 없이 복구한다. `200`의 `items`는 최대 한 건이다. | 후보 경로·응답, 선택 순서, 권한 오류 |
| `GET /v1/sync-runs/{id}` | ID를 알고 있다면 페이지 재진입이나 사용자의 새로고침에서 접수 응답의 `status_url`로 현재 상태를 조회. `200`에서 가져오기 상태, 파생 처리 상태, 재시도 가능 여부를 읽는다. `PARTIAL_WITH_LIMIT`도 이 응답에 포함된다. | 전체 실패 이유와 stream enum, 구조화된 범위의 필요 여부 |
| `POST /v1/sync-runs/{id}/retry` | UUID `Idempotency-Key` 필수. 요청 body 생략. `202`는 원래 run의 `target_id`, `status_url`을 반환한다. | Worker의 작업 단위와 `job_id` 발급 관계 |

`target_id`는 실행 ID다. `status_url`은 `/v1/sync-runs/`에 같은 `target_id`를 한 경로 세그먼트로 인코딩해 붙인 상대 경로다. 접수·재전송·복구 결과에서 두 값이 일치할 때 해당 경로를 조회한다. 재시도 접수는 요청 경로의 실행 ID와도 같아야 한다. `job_id`는 선택 사항이며 stub에서는 생략한다. 두 mutation의 키 누락·잘못된 키·유효하지 않은 요청은 빈 `422`다. `X-Request-ID`는 선택 응답 헤더로 돌려준다. [MVP API Specification](https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa)

## 실행 ID 없이 복구

`source_resource_id`는 필수다. `limit`은 생략 시 `1`이고 값 `1`만 허용한다. 현재 Workspace에서 저장소 권한을 확인한 뒤 읽을 수 있는 실행만 남긴다. `PENDING` 또는 `RUNNING` 실행이 있으면 우선 선택한다. 그 외에는 마지막 종료 전이 시각이 가장 최근인 실행을 반환하고, 시각이 같으면 더 늦게 생성된 실행을 선택한다. `PARTIAL`과 `PARTIAL_WITH_LIMIT`도 종료된 시도의 결과로 조회할 수 있다. 같은 run이 재시도를 거쳐 다시 종료되면 종료 시각을 갱신하며, 후속 처리 상태만 바뀌면 종료 순서는 유지된다.

- 항목이 있는 `200` 응답은 `items[0]`에 `target_id`, `status_url`과 상세 조회와 같은 구조의 `import_status`를 담는다. 조회 가능한 실행이 없거나 실행이 전부 읽기 범위 밖이면 같은 `200 {"items": []}`를 반환한다.
- 저장소가 없거나 다른 Workspace에 있거나 현재 접근할 수 없으면 모두 빈 `403`이다. 인증되지 않은 호출은 빈 `401`, 잘못된 조회 인자는 빈 `422`다.
- 두 조회의 `200` 응답은 `Cache-Control: private, no-store`를 사용한다. 복구 GET은 저장된 상태를 읽는 동작이다. Job 생성, GitHub 호출, 가져오기 시작·재시도와는 독립적이다.

## 재전송·충돌·새 계획

같은 키의 범위는 현재 Workspace와 동작이다. 생성과 재시도는 각각 별도의 키 범위를 갖는다. 접수는 파싱한 JSON 값을, 재시도는 대상 run ID를 비교한다. 권장 보존 기간은 **최초 접수 후 24시간**이며 재전송으로 연장하지 않는다. 이 기간 안에 같은 키와 요청을 명시적으로 재전송하면 현재 권한을 확인한 뒤 원래 `202`의 실행 ID와 조회 경로를 반환한다. 현재 권한은 충돌·만료 정보를 반환하기 전에도 확인한다. 운영에서는 접수와 키 기록을 함께 확정해 서버 재시작 뒤에도 같은 응답을 돌려주는 방안을 검토한다. Stub은 같은 저장소 객체를 사용하는 새 클라이언트로 재접속을 재현한다.

알려진 키가 24시간에 도달하면 실행 이력과 함께 보존하는 만료 표시를 확인하고 `409 IDEMPOTENCY_KEY_EXPIRED`를 반환한다. 이 표시가 보존되는 동안 같은 키로 새 작업을 시작할 수 없다. 운영 제거·보존 기간은 승인 대상이며, 표시가 제거된 키는 새 키와 구분할 수 없다. WEB은 만료 키에서 조회로 돌아가고 새 키로 자동 대체하지 않는다.

| 상황 | 응답과 WEB 동작 |
| --- | --- |
| 네트워크 단절 또는 mutation `5xx` | 접수 여부 미확정. ID가 있으면 상세 GET, 없으면 저장소 복구 GET을 사용한다. 빈 조회도 앞선 요청의 실패를 확정하지 않는다. 같은 키를 보유한 기간 안에는 사용자가 직접 같은 요청을 재전송할 수 있다. 자동 `POST`는 하지 않는다. |
| 보존 기간 안에 같은 키로 요청 내용을 변경 | `409 IDEMPOTENCY_CONFLICT`. 요청 내용을 확인한다. |
| 별도의 요청과 활성 run 충돌 | `409 SYNC_ALREADY_RUNNING`. `error.details.existing_run_id`가 있으면 해당 run을 조회하고, ID가 없으면 복구 GET 또는 중립 안내를 사용한다. 이 ID는 현재 실행 읽기 권한이 있을 때만 반환된다. |
| 이미 성공한 저장소의 재접수 | 빈 `409`. 현재 권한으로 조회한 `SUCCESS`를 표시한다. |
| 부분 실패의 안전한 재시도 | `retry.allowed=true`일 때만 사용자가 같은 run의 재시도를 요청한다. 안전한 checkpoint에서 같은 계획을 재개한다. 허용되지 않는 재시도는 기존 빈 `409`를 유지하고 조회 상태를 사용한다. |
| 기존 미완료 계획을 바꿀 때 | 명시적 폐기 확인 전 새 접수는 빈 `409`다. 활성·성공 실행은 이 폐기 후보의 대상에서 제외한다. 폐기 확인 후에만 새 키로 명시 접수해 새 실행과 계획을 만든다. 이전 실행은 조회 이력에 남고 재시도는 비활성화된다. 폐기 확인의 HTTP 계약은 승인 대기이며 stub에서는 테스트 함수로만 모의한다. |

## 승인 대상

- 복구 GET의 경로, 한 건 제한, 권한별 응답과 선택 순서, 두 GET의 캐시 지시.
- 24시간 재전송 기간, 만료 표시의 수명과 범위, 새 `IDEMPOTENCY_KEY_EXPIRED` 응답. 기존 빈 `409`는 WEB 기준 커밋과 호환되도록 유지한다.
- 폐기 확인의 권한, 의사 확인 및 별도 mutation 계약. 기존 실행 생성과 폐기 확인은 별도 동작으로 남긴다.

## 상태별 필수 필드와 생략 규칙

모든 상태에 `status`, `plan_id`, `import_ready`, `processing_status`, `source_drift`, `retry.allowed`가 필요하다. 사용 가능한 경우 `observed_at`, `streams`, `processed_range`, `remaining_range`를 제공한다. 아래 필수 조건에 해당하지 않는 선택 필드는 값이 없을 때 생략한다. 알려진 필드에 JSON `null`은 사용하지 않는다. 각 stream을 반환할 때는 `type`과 `status`가 필요하고, 집계할 수 있는 checkpoint와 실패 수만 넣는다. 범위는 화면에 표시하는 문자열이다.

| `status` | 추가 필수·생략 규칙 | 파생 처리와 재시도 |
| --- | --- | --- |
| `PENDING`, `RUNNING` | `failure_reason` 생략. 재시도 접수 후에도 기존 checkpoint와 범위를 조회할 수 있다. | `import_ready=false`, `processing_status=NOT_STARTED`, `retry.allowed=false` |
| `PARTIAL` | `failure_reason` 필수. `PLAN_DRIFT`일 때만 `source_drift=true`. | `import_ready=false`, `processing_status=NOT_STARTED`. 일시적 provider 오류에 안전한 checkpoint가 있을 때만 `retry.allowed=true`와 `retry.from_safe_checkpoint=true`. drift·정책 차단·구조적 불완전 결과는 같은 계획에서 재개할 수 없다. |
| `PARTIAL_WITH_LIMIT` | `failure_reason`, `remaining_range` 필수. 처리한 범위가 있으면 `processed_range` 제공. | `import_ready=false`, `processing_status=NOT_STARTED`, `retry.allowed=false` |
| `FAILED`, `CANCELED` | `FAILED`에는 `failure_reason` 필수. `CANCELED`에는 생략. | `import_ready=false`, `processing_status=NOT_STARTED`, `retry.allowed=false` |
| `SUCCESS` | `failure_reason` 생략. | `import_ready=true`, `retry.allowed=false`. 나중에 공개 정책이 바뀌면 성공 이력은 유지되고 파생 `processing_status`는 `BLOCKED_POLICY`가 될 수 있다. |

`retry.allowed=false`이면 `retry.from_safe_checkpoint`를 생략한다. `import_ready`는 성공한 가져오기가 단 한 번의 `IMPORT_READY` 의도를 기록했음을 나타내며, `processing_status`는 그 이후 파생 처리와 현재 정책을 반영한다. WEB은 `failure_reason` 문자열과 무관하게 `retry.allowed`로 버튼을 제어한다. 자동 가져오기 폴링은 종료 상태에서 끝낼 수 있다. 이후 페이지 재진입이나 사용자 새로고침에서 상세 GET을 호출하면 과거 `SUCCESS`와 최신 파생 처리 상태를 함께 읽는다. [Snapshot Import 계약](https://app.notion.com/p/3f0f86591b5f8170940fe98da87609aa)

## 연결·권한·저장소 승인 대기

| 영역 | 문서화된 역할 | 승인 대기 |
| --- | --- | --- |
| `POST /v1/source-accounts/github` | 서비스 로그인과 별도인 GitHub 수집 연결과 scope 확인 | OD-011 수집 방식, 시작·콜백 경로, 요청·응답 필드 |
| `GET /v1/source-accounts` | 현재 Workspace의 연결 계정 조회 | 계정·상태 필드, 목록·페이지 규칙 |
| `POST /v1/source-resources`, `GET /v1/source-resources` | 공개 저장소 allowlist 등록, 저장소·정책 상태 조회 | 등록 입력, 저장소 목록 필드·정책 enum·페이지 규칙 |
| 인증과 Workspace | Backend는 Supabase access JWT와 현재 membership·대상 접근 권한을 확인 | OD-010 브라우저 세션, Workspace 전달 위치, 권한 매트릭스와 범위 밖 객체 응답 |

[Open Decisions](https://app.notion.com/p/3e3f86591b5f81fcbc89d50d3f591dc4) · [OD-010](https://app.notion.com/p/3e3f86591b5f81d692f8f1cdd5fed345) · [OD-011](https://app.notion.com/p/3e3f86591b5f81d89e60c0deeff3838f) · [Supabase Auth·Workspace 권한 계약](https://app.notion.com/p/41df86591b5f81948ab8f08667e9d5da)

## CONTRACT-04 제품 인수인계

| 경로·영역 | 제품 구현에 넘길 후보 동작 | 승인 대기 | 실환경 검증 필요 |
| --- | --- | --- | --- |
| T-106 상세 `GET /v1/sync-runs/{id}` | 현재 세션·Workspace·저장소 범위에서 실행 ID를 알고 있으면 페이지 재진입과 사용자 새로고침에 사용한다. 현재 멤버십·저장소·실행 읽기 권한을 매 요청에서 확인하고, Job이나 동기화 작업 없이 상태를 읽는다. 종료 시 자동 폴링을 끝낸다. 이후 상세 조회는 과거 `SUCCESS`·`import_ready`를 유지하면서 최신 `processing_status`를 반영한다. 재시도 버튼은 `retry.allowed`를 따른다. | 권한 부족·부재·은닉의 제품 HTTP 상태, 오류 코드와 본문. | 실제 DB와 세션에서 권한 회수 직후 데이터 비노출, 정책 변경 후 최신 상태 조회, 캐시 경유 동작 및 조회 중 Job·provider 호출 0건. |
| T-106 복구 `GET /v1/sync-runs?source_resource_id=…&limit=1` | 같은 세션·저장소 범위에 유효한 실행 ID가 없을 때 사용한다. 현재 Workspace에서 저장소 권한을 확인한 다음, 읽을 수 있는 실행만 대상으로 `PENDING`·`RUNNING`을 우선하고 그 외에는 마지막 종료 전이 시각, 동률이면 생성 순서로 최신 한 건을 고른다. `200 {"items": []}`는 현재 읽을 수 있는 실행이 없다는 뜻이며 이전 접수 여부는 계속 미확정으로 처리한다. 조회 중 Job·GitHub 호출이나 새 `POST`가 발생하지 않는다. 반환된 `status_url`과 `target_id`는 같은 실행을 가리킨다. | 허용·부재·은닉의 제품 응답. Stub의 빈 `403`은 로컬 fixture 응답으로만 사용한다. | 실제 권한과 DB에서 활성 우선·종료 정렬·동률 처리·권한 밖 실행 비노출을 검증한다. 응답 유실 후 재접속에서 실행·Job·재시도 기록이 그대로인지 확인한다. |
| T-106 접수 `POST /v1/sync-runs` → T-105 수용 기준 | `INITIAL_BACKFILL`을 명시하고 현재 권한과 저장소 상태를 확인해 동일한 최초 실행 요청을 한 번만 접수한다. `202`는 같은 실행을 가리키는 `target_id`·`status_url`을 반환한다. 같은 키·다른 요청의 충돌과 별도 요청·활성 실행의 충돌을 구분하고 기존 실행 ID는 현재 읽기 권한이 있을 때만 제공한다. `5xx`·응답 유실 시 접수 여부는 미확정이다. WEB은 조회로 확인하고 자동 `POST`를 보내지 않는다. | 기간·한도 기본값, 제품 충돌·거절 응답 매핑. 계획 폐기 API·UI는 별도 범위다. | 동시 접수에서 저장소당 활성 실행 최대 한 건, 성공한 저장소 재접수 차단, 권한 회수 뒤 접수·재전송 차단. |
| T-106 재시도 `POST /v1/sync-runs/{id}/retry` → T-105 수용 기준 | 현재 권한과 재시도 가능 여부를 확인한다. 안전한 checkpoint가 있는 일시 실패에 한해 같은 계획과 실행을 한 번 재개한다. `202`는 원래 실행의 `target_id`·`status_url`을 반환한다. 한도 도달·drift·정책 차단은 같은 계획에서 재개할 수 없다. 재전송은 기존 접수를 복구하는 동작이며 새 재개를 만들지 않는다. | Worker의 `job_id` 수명, 제품 재시도 거절 응답 매핑. | 실제 checkpoint와 재개 Job의 일관성, 재시도 동시 호출과 중복 전달에서 같은 재개가 한 번만 반영되는지 검증. |
| T-105 접수·재시도 공통 수용 기준 | 실행 생성·재개 전이, Job 및 필요한 outbox, Workspace·동작별 재전송 기록과 원래 `202`의 응답 본문을 같은 DB 트랜잭션에 저장한다. 커밋 뒤에만 접수 응답과 Worker 알림을 내보낸다. 서버 재시작 후에도 보존 기간 내 동일 키·동일 요청은 현재 권한을 다시 확인한 뒤 같은 접수 응답을 복구한다. 보존 기간은 **24시간 후보**다. 만료 뒤에도 같은 키가 새 Job이나 실행을 만들지 못하도록 보존된 만료 표식과 실행 상태 제약으로 차단한다. [T-105](https://app.notion.com/p/3e3f86591b5f8124944ed58ea829d546) | 최종 보존 기간과 만료 응답. **만료 표식을 언제 삭제할 것인가? 삭제한 뒤 지연 재전송을 어떻게 식별·차단할 것인가?** | 실제 DB에서 커밋 전 실패 시 전부 롤백, 커밋 후 응답 유실 및 API 재시작 시 같은 `202` 복구, 만료·동시 재전송 시 중복 실행·Job 0건을 검증한다. T-105A·REL-02 완료 증거는 별도 확인한다. |
| AUTHDB-05 확인된 세션 종료 | 인증 서비스가 세션 종료를 확인하면 미만료 JWT가 있어도 보호 요청을 차단한다. WEB은 해당 세션을 정리하고 재인증으로 안내한다. | 확정 종료를 전달할 HTTP 상태, 오류 코드와 본문. | 호스팅 로그아웃 범위, 쿠키·refresh·기존 미만료 JWT 재사용 차단을 채택한 정책에 맞춰 검증. |
| AUTHDB-05 Workspace 권한 회수 | 활성 세션이라도 해당 Workspace 접근을 즉시 거부하고 그 범위의 보호 데이터를 숨긴다. WEB은 접근 거부·Workspace 전환을 안내하고 다른 활성 Workspace와 로그인은 유지한다. | 멤버십 회수와 리소스 은닉의 경계, HTTP 상태·오류 코드·본문. | 실제 멤버십 회수 직후 같은 세션에서 회수 범위 차단과 다른 Workspace 정상 동작을 확인한다. |
| AUTHDB-05 인증 서비스 일시 장애 | JWKS 또는 세션 확인의 지연·연결 오류·rate limit으로 확인할 수 없으면 민감 작업을 차단하고 DB 변경·Job 생성을 막는다. WEB은 세션을 유지하며 일시적으로 확인할 수 없음을 안내한다. | 실제 HTTP 상태, 오류 코드·본문, `Retry-After` 등 재시도 안내. | 호스팅 장애 주입으로 민감 작업 차단, DB 변경·Job 생성 0건, WEB 세션 유지와 서비스 복구 후 정상 진행을 검증. |
| 검증 근거 | WEB-06 `da6f4980de0b81bdffe5d1774c2737a7ee0f6011`의 `spikes/web-import/tests/contract-http/contract-stub-http.test.tsx`는 로컬 루프백 stub 증거다. AUTHDB-05 `13522c2af04250a7b755d7bee026189a0701137d`의 `spikes/t007d-auth-db/HOSTED_VERIFICATION_RUNBOOK.md` §4–5는 로컬 인증·DB 증거와 인수인계 제안이다. | 각 후보의 제품 계약 승인. | 실제 제품 백엔드·DB·WEB 연동과 호스팅 검증은 별도로 수행한다. |

## 로컬 확인

워크트리 루트에서 실행한다.

```sh
import_contract_venv="$(mktemp -d)"
python3.13 -m venv "$import_contract_venv"
"$import_contract_venv/bin/python" -m pip install -r spikes/import-contract/requirements.txt
"$import_contract_venv/bin/python" -B -m unittest discover -s spikes/import-contract -v
```

테스트는 OpenAPI 문법, 내부 참조, 예시, 상태별 필드 규칙을 검사한다. FastAPI TestClient는 실제 요청을 보내고 반환 상태와 JSON을 OpenAPI의 해당 HTTP 응답에 대조한다. 주체·Workspace·권한, provider 관찰 결과와 보존 기간 검증용 시계는 인프로세스 테스트가 주입한다. 조회 전에 응답이 유실되는 경우와 접수 후 502가 반환되는 경우도 모의한다. 조회가 run과 재전송 기록을 바꾸지 않는지 확인한다. 권한 밖이거나 찾을 수 없는 fixture는 빈 `403`으로 처리한다. 실제 인증·권한 통합, Worker·PostgreSQL 장애 검증 및 공통 CI는 후속 범위다. [T-106](https://app.notion.com/p/3e3f86591b5f8148908dd84514f1fbee)
