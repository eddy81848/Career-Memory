# CONTRACT-03 최초 가져오기 복구 검토본

기준은 CONTRACT 커밋 `a0314f2cbcb33f1d34ed486db7b1009dcb4adc59`다. WEB 커밋 `c80008dd28ccf65e03847ebf1763f8a618138d33`의 세 경로 대응을 참고했다. [OpenAPI 0.3.0-draft](openapi.yaml)는 복구 조회와 재전송 정책의 승인 후보다.

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

## 로컬 확인

워크트리 루트에서 실행한다.

```sh
import_contract_venv="$(mktemp -d)"
python3.13 -m venv "$import_contract_venv"
"$import_contract_venv/bin/python" -m pip install -r spikes/import-contract/requirements.txt
"$import_contract_venv/bin/python" -B -m unittest discover -s spikes/import-contract -v
```

테스트는 OpenAPI 문법, 내부 참조, 예시, 상태별 필드 규칙을 검사한다. FastAPI TestClient는 실제 요청을 보내고 반환 상태와 JSON을 OpenAPI의 해당 HTTP 응답에 대조한다. 주체·Workspace·권한, provider 관찰 결과와 보존 기간 검증용 시계는 인프로세스 테스트가 주입한다. 조회 전에 응답이 유실되는 경우와 접수 후 502가 반환되는 경우도 모의한다. 조회가 run과 재전송 기록을 바꾸지 않는지 확인한다. 권한 밖이거나 찾을 수 없는 fixture는 빈 `403`으로 처리한다. 실제 인증·권한 통합, Worker·PostgreSQL 장애 검증 및 공통 CI는 후속 범위다. [T-106](https://app.notion.com/p/3e3f86591b5f8148908dd84514f1fbee)
