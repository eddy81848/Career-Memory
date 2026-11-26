# T-106 최초 가져오기 API 후보

기준 SHA는 `efe251071fbee1be9a2ffee680a73f3ca3cd8084`다. 기준 트리에는 `README.md`만 있었고 적용되는 `AGENTS.md`, 이전 로컬 설계, 기존 계약 테스트 도구는 없었다. 이 후보는 오케스트레이터 검토본이다. 승인 후 클라이언트 생성과 다른 스레드 전달에 사용한다.

## 명세에서 확인한 범위

| 경로 | 근거가 있는 역할과 필드 | 승인 대기 |
| --- | --- | --- |
| `POST /v1/source-accounts/github` | 별도 GitHub 수집 계정 연결·scope 확인. 서비스 로그인과 별도 grant | 시작·콜백의 메서드 및 경로, HTTP 결과, 요청·응답 필드 |
| `GET /v1/source-accounts` | 현재 Workspace에서 연결 계정 조회 | 항목 식별자 및 상태, 목록 래퍼와 pagination 적용 여부 |
| `POST /v1/source-resources` | 공개 GitHub 저장소를 allowlist에 등록 | 연결 계정·provider 저장소 식별 입력, 응답 상태·필드, mutation 전제조건 |
| `GET /v1/source-resources` | 현재 Workspace에서 저장소와 정책 상태 조회. 제품 저장소 식별자는 `source_resource_id` | 항목 필드, 정책 상태 enum, 목록 래퍼와 pagination 적용 여부 |
| `POST /v1/sync-runs` | 대상 `source_resource_id` 필수. `mode`는 값이 있으면 `INITIAL_BACKFILL`. `backfill_from` 날짜와 `limits.max_objects` 정수가 문서 예시에 있다. 접수는 `202` 및 run ID(`target_id`). 공통 Job 예시의 `job_id`, `status`, `target_type=SYNC_RUN`, `status_url`을 후보 필드로 제시한다. | `mode` 생략, 기간·한도 기본값 및 필수 여부, Job 추가 필드의 필수 여부, 키 누락 처리, 재전송 성공·충돌 wire |
| `GET /v1/sync-runs/{id}` | 필수 경로 `id`. 조회에 `status`, `import_ready`, `processing_status`, `retry.allowed`를 요구한다. `plan_id`, `failure_reason`, `streams`, `observed_at`, `source_drift`, `processed_range`, `remaining_range`, `retry.from_safe_checkpoint`와 stream 예시는 문서에 있다. 실패 상태에는 실패 이유를 함께 알린다. | 정확한 성공 HTTP 상태, stage별 필드 필수·nullable 여부, 기간·누락 범위 형식, reason과 stream enum 전체 |
| `POST /v1/sync-runs/{id}/retry` | 필수 경로 `id`. 자격을 다시 확인하고 허용된 경우 같은 run과 plan을 안전 checkpoint에서 재개 | 키 적용·필수 여부, 요청 body 여부, 성공 상태·payload와 job ID 관계, 재시도 거절 HTTP·payload |
| 전체 | Backend 입력은 Supabase access JWT Bearer. 서버가 현재 Workspace membership·role 및 대상 접근 권한을 확인한다. 인증 실패 `401`, membership/role 부족 `403`, 민감한 객체 존재 여부 concealment 원칙이 문서화되어 있다. `X-Request-ID`는 선택 사항이며 공통 문서는 echo를 명시한다. | Workspace 선택 값의 전달 위치, 역할 매트릭스, 브라우저 세션 transport, concealment의 정확한 상태·코드, 오류 필드별 필수 여부 |

후보 YAML은 문서 예시에 있는 필드를 우선 수록한다. `required`에서 빠진 필드의 생략 동작과 null 처리는 승인을 기다린다. 상태 미확정 성공 응답은 `2XX`로, 연결 시작 결과는 `default`로 기록했다. 공통 오류 예시는 단독 component에 두었고 `SYNC_PARTIAL_WITH_LIMIT`의 HTTP 매핑은 승인을 기다린다. Job의 문서화된 `status_url` 필드는 서버 내부 상태 조회 경로를 나타낸다. Job 및 오류 예시는 합성 ID와 메타데이터만 사용한다. [MVP API Specification](https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa) · [OpenAPI 3.1.0 Responses Object](https://spec.openapis.org/oas/v3.1.0.html#responses-object)

## 가져오기 동작

- `PENDING`과 `RUNNING`은 활성 상태다. 같은 요청의 키 재전송은 같은 논리 작업을 가리킨다. 키가 다른 요청에 재사용되는 경우와 별개 요청이 활성 저장소에 들어오는 경우를 구분한다. 활성 중복에는 `SYNC_ALREADY_RUNNING`과 기존 run ID가 필요하다. 성공 후 새 자동 가져오기는 거부한다.
- 일시적 provider 장애로 `PARTIAL`이 된 run은 안전 checkpoint에서 같은 계획으로 재개할 수 있다. 필수 자료가 구조적으로 불완전하거나 정책이 차단된 경우, 또는 `PLAN_DRIFT`로 중단한 경우에는 새 계획이 필요하다. 드리프트는 run 실패 이유 `PLAN_DRIFT`와 문서의 오류 코드 `IMPORT_SOURCE_DRIFT`를 구분한다. 기간을 바꾸려면 미완료 계획을 명시적으로 폐기해야 한다.
- 항목·요청·크기 한도에 걸리면 `PARTIAL_WITH_LIMIT`이며 처리·누락 범위를 조회할 수 있다. 일부 metadata/hash를 보존하더라도 승격하지 않는다. `import_ready`는 최종 `SUCCESS`와 한 번의 `IMPORT_READY` 기록을 나타낸다. 이후 공개 정책이 바뀌면 가져오기 성공 이력을 유지하고 파생 `processing_status`는 `BLOCKED_POLICY`로 표시한다. [Snapshot Import 계약](https://app.notion.com/p/3f0f86591b5f8170940fe98da87609aa)

## 오케스트레이터 승인 요청

아래 내용은 제안이다. 승인 보류 중인 응답을 호출 규약으로 사용하기 전에 영향을 받는 담당자와 검토한다.

| 결정 | 근거 | 추천 후보 | 영향 | 승인 요청 |
| --- | --- | --- | --- | --- |
| `connection-wire` | API §4 및 OD-011은 연결 역할을 정했고 OAuth App과 GitHub App의 설치·회수 경로 비교가 남았다. | 저장소 단위 최소 읽기 grant를 표현할 수 있는 GitHub App 후보부터 OD-011에서 검증한다. 수집 callback은 서비스 로그인과 분리한다. | WEB 연결 UX, callback, 자격 증명 수명 | OD-011 근거 검토 후 시작·콜백 경로, 응답 형식, 실패 반환 경로를 승인할지 결정 |
| `session-and-scope` | API §2·§11과 Auth 계약에서 backend Bearer와 서버 권한 검사를 기술했다. OD-010은 브라우저 측 전략 검증 중이다. | Next.js 서버 경유를 OD-010 검증 후보로 유지한다. 현재 membership/role을 서버에서 조회하고 권한 밖 ID와 부재 ID는 일관된 concealment를 검토한다. | WEB 세션, Workspace 전달 방식, 401/403 및 대상 조회 거절 | Workspace 선택 값 위치, 경로별 역할, 범위 밖과 부재의 공통 응답 및 OD-010 근거 승인 |
| `source-records` | API §4는 목록과 등록 역할, §8은 공통 목록/opaque cursor 예시를 제공한다. | 필요할 때 Workspace에 묶인 opaque cursor와 공통 `items/page`를 재사용한다. 등록 뒤 제품 저장소는 `source_resource_id`로 참조하고 provider ID는 별도 입력으로 검토한다. | WEB 목록·저장소 선택, 등록 재시도 | 계정·저장소 최소 필드, 등록 입력과 상태, 두 목록의 pagination 적용 여부 승인 |
| `mutation-wire` | API §2·§9와 Snapshot 계약은 안정된 요청 키·같은 논리 작업을 정의한다. retry 응답과 대상별 키 규칙은 비어 있다. OD-004는 12개월/5,000을 조정 가능한 초기값으로 둔다. | 생성·재시도에서 키를 요구하고 Workspace와 operation에 scope를 둔다. 권한 재확인 뒤 동일 요청은 최초 접수로 되돌린다. 재시도는 `202`로 같은 run을 가리키는 방안을 검토한다. | 키 유지·만료, 재시도 버튼, status polling, 기간·한도 UI | 키 필수 여부·동일 요청 판정 및 TTL, retry status/body/job ID, 요청 기간·한도 입력 및 기본값 승인 |
| `conflict-wire` | API §3·§4에 `SYNC_ALREADY_RUNNING`·`IDEMPOTENCY_CONFLICT`와 기존 run ID 반환 의미가 있다. 상태·payload 위치는 비어 있다. | `409`와 공통 error envelope를 검토한다. 권한 있는 활성 중복에만 `error.details.existing_run_id`를 검토하고, 다른 요청으로 같은 키를 쓰는 충돌과 구분한다. | WEB이 기존 run 화면으로 이동하는 방식, 민감한 ID 노출, 새 계획 흐름 | 상태/오류 필드 및 정확한 run ID 위치, 재시도 불가와 성공 후 재시작 거절의 매핑 승인 |
| `status-wire` | API §4의 상태 예시와 §3의 한도 오류 예시는 별개다. | 조회 성공 `200`에서 도메인 결과로 `PARTIAL_WITH_LIMIT` 등을 표시한다. 현재 예시에 근거해 range 표현과 stage별 null/생략 규칙을 명시적으로 정한다. | WEB의 진행률·안내·retry 렌더링, 스키마 호환성 | 조회 HTTP 성공 상태, 상태별 필수/nullable 필드, 범위와 오류 이유 형식 승인 |

결정 근거는 [Open Decisions](https://app.notion.com/p/3e3f86591b5f81fcbc89d50d3f591dc4), [OD-010](https://app.notion.com/p/3e3f86591b5f81d692f8f1cdd5fed345), [OD-011](https://app.notion.com/p/3e3f86591b5f81d89e60c0deeff3838f), [Supabase Auth·Workspace 권한 계약](https://app.notion.com/p/41df86591b5f81948ab8f08667e9d5da)에서 확인했다. Snapshot 문서의 보관 정책 callout에는 OD-003이 미확정으로 남아 있으나 Open Decisions와 API Specification은 이를 확정으로 기록한다. 삭제 계약은 후속 라운드 범위다.

## 로컬 실행

워크트리 루트에서 Python 3.13으로 실행한다. 임시 가상환경에는 공개 Python 패키지만 설치한다.

```sh
import_contract_venv="$(mktemp -d)"
python3.13 -m venv "$import_contract_venv"
"$import_contract_venv/bin/python" -m pip install -r spikes/import-contract/requirements.txt
"$import_contract_venv/bin/python" -B -m unittest discover -s spikes/import-contract -v
```

검증은 `spikes/import-contract/test_contract.py`에서 실행한다. OpenAPI 구조, 내부 참조, inline 예시와 JSON Schema를 검사한 뒤 인프로세스 FastAPI TestClient로 생성·조회·재시도를 실행한다. 연결·계정 목록·저장소 등록/목록은 후보의 경로와 역할만 검사한다.

Workspace·허용 저장소와 관찰 결과는 테스트가 직접 주입한다. 주체와 권한은 앱 팩터리 인자로만 전달한다. 메모리 저장소의 lock은 테스트 프로세스 안에서 활성 중복과 전이만 재현한다. `fixture_outcome`, `fixture_existing_run_id`, 테스트의 재시도 성공/거절 응답, 키 scope와 명시적 `mode` 입력은 합성 선택이다. HTTP 응답의 `X-Request-ID` echo와 보기용 Job URL 반환은 검증 범위 밖이다. 테스트는 Job ID와 run ID로 접수·조회 경계를 확인하며 provider와 Worker 관찰 결과는 스냅샷으로 주입한다.

## 승인 후 전달

- WEB에는 상태 예시와 [필드 근거 및 승인 대기 표](#명세에서-확인한-범위)를 함께 전달한다.
- Backend에는 [OpenAPI 후보](openapi.yaml)와 합성 테스트를 함께 전달한다. 승인된 scope·오류·retry 결정을 후보에 반영한 뒤 실제 데이터 무결성, PostgreSQL fault 검증, 호환성/CI를 후속 라운드에서 확인한다. [T-106](https://app.notion.com/p/3e3f86591b5f8148908dd84514f1fbee)
