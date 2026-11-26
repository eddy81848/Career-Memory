# T-106 최초 가져오기 WEB 검토본

기존 CONTRACT 기준은 `b31a670cd500d32a4f5c3feaa0297a9f28d15a0c`다. [OpenAPI 0.2.0-draft](openapi.yaml)는 아래 세 경로를 대상으로 한 WEB 검토용 후보이며 제품 계약 승인은 대기 중이다.

## WEB에서 사용할 응답

| 경로 | 검토본에서 사용할 요청·응답 | 승인 대기 |
| --- | --- | --- |
| `POST /v1/sync-runs` | UUID `Idempotency-Key`와 JSON `source_resource_id`, `mode: INITIAL_BACKFILL` 필수. `backfill_from`과 `limits.max_objects`는 선택. `202`의 필수 필드는 `target_id`, `status_url` | 기간·한도 기본값과 경계, 키 보존 기간 |
| `GET /v1/sync-runs/{id}` | 접수 응답의 상대 경로 `status_url`로 조회. `200`에서 가져오기 상태, 파생 처리 상태, 재시도 가능 여부를 읽는다. `PARTIAL_WITH_LIMIT`도 이 응답에 포함된다. | 전체 실패 이유와 stream enum, 구조화된 범위의 필요 여부 |
| `POST /v1/sync-runs/{id}/retry` | UUID `Idempotency-Key` 필수. 요청 body 생략. `202`는 원래 run의 `target_id`, `status_url`을 반환한다. | Worker의 작업 단위와 `job_id` 발급 관계 |

`target_id`는 가져오기 실행 ID이고 `status_url`은 해당 실행을 조회하는 상대 경로다. WEB은 이 두 필드로 조회와 재시도를 이어간다. `job_id`는 선택 사항이며 stub에서는 생략한다. 두 mutation은 키 누락·잘못된 키·유효하지 않은 요청에 빈 `422`를 반환하는 리뷰 후보를 사용한다. `X-Request-ID`는 선택 사항이며 전달하면 stub이 응답 헤더에 돌려준다. [MVP API Specification](https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa)

- 같은 동작을 재전송할 때는 같은 키와 요청을 사용한다. 접수는 JSON 값, 재시도는 대상 run으로 동일 요청을 판정한다. Stub은 Workspace와 동작별로 키를 관리하고 접수·조회·재전송 시 현재 권한을 확인한다.
- 같은 키로 요청을 바꾸면 `409 IDEMPOTENCY_CONFLICT`다. 별도의 요청이 `PENDING` 또는 `RUNNING` 실행에 부딪히면 `409 SYNC_ALREADY_RUNNING`이다. 코드는 `error.code`로 읽고, `error.details.existing_run_id`가 있을 때만 해당 run으로 이동한다. 이 ID는 현재 읽기 권한이 있는 경우에만 반환된다.
- 기존 완료 실행의 재접수, 계획 폐기가 필요한 재접수, 재개가 불가능한 재시도의 stub 응답은 빈 `409`다. 이 경우 WEB은 조회된 상태와 `retry.allowed`를 사용한다. 별도 오류 코드와 실제 범위 밖 객체의 응답 매핑은 승인 대기다.

## 상태별 필수 필드와 생략 규칙

모든 상태에 `status`, `plan_id`, `import_ready`, `processing_status`, `source_drift`, `retry.allowed`가 필요하다. 사용 가능한 경우 `observed_at`, `streams`, `processed_range`, `remaining_range`를 제공한다. 아래 필수 조건에 해당하지 않는 선택 필드는 값이 없을 때 생략한다. 알려진 필드에 JSON `null`은 사용하지 않는다. 각 stream을 반환할 때는 `type`과 `status`가 필요하고, 집계할 수 있는 checkpoint와 실패 수만 넣는다. 범위는 화면에 표시하는 문자열이다.

| `status` | 추가 필수·생략 규칙 | 파생 처리와 재시도 |
| --- | --- | --- |
| `PENDING`, `RUNNING` | `failure_reason` 생략. 재시도 접수 후에도 기존 checkpoint와 범위를 조회할 수 있다. | `import_ready=false`, `processing_status=NOT_STARTED`, `retry.allowed=false` |
| `PARTIAL` | `failure_reason` 필수. `PLAN_DRIFT`일 때만 `source_drift=true`. | `import_ready=false`, `processing_status=NOT_STARTED`. 일시적 provider 오류에 안전한 checkpoint가 있을 때만 `retry.allowed=true`와 `retry.from_safe_checkpoint=true`. drift·정책 차단·구조적 불완전 결과는 같은 계획에서 재개할 수 없다. |
| `PARTIAL_WITH_LIMIT` | `failure_reason`, `remaining_range` 필수. 처리한 범위가 있으면 `processed_range` 제공. | `import_ready=false`, `processing_status=NOT_STARTED`, `retry.allowed=false` |
| `FAILED`, `CANCELED` | `FAILED`에는 `failure_reason` 필수. `CANCELED`에는 생략. | `import_ready=false`, `processing_status=NOT_STARTED`, `retry.allowed=false` |
| `SUCCESS` | `failure_reason` 생략. | `import_ready=true`, `retry.allowed=false`. 나중에 공개 정책이 바뀌면 성공 이력은 유지되고 파생 `processing_status`는 `BLOCKED_POLICY`가 될 수 있다. |

`retry.allowed=false`이면 `retry.from_safe_checkpoint`를 생략한다. `import_ready`는 성공한 가져오기가 단 한 번의 `IMPORT_READY` 의도를 기록했음을 나타내며, `processing_status`는 그 이후 파생 처리와 현재 정책을 반영한다. [Snapshot Import 계약](https://app.notion.com/p/3f0f86591b5f8170940fe98da87609aa)

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

테스트는 OpenAPI 문법, 내부 참조, 예시, 상태별 필드 규칙을 검사한다. FastAPI TestClient는 실제 요청을 보내고 반환 상태와 JSON을 OpenAPI의 해당 HTTP 응답에 대조한다. 주체·Workspace·권한과 provider 관찰 결과는 인프로세스 테스트가 직접 주입한다. 권한 밖이거나 찾을 수 없는 fixture는 빈 `403`으로 처리한다. 실제 인증·권한 통합, Worker·PostgreSQL 장애 검증 및 공통 CI는 후속 범위다. [T-106](https://app.notion.com/p/3e3f86591b5f8148908dd84514f1fbee)
