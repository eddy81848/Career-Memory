# T-007F 최소 호스팅 실험 설계 및 무료 한도(Zero-Cost) 안전 게이트

> [!CAUTION]
> **현재 실행 판정: `NO-GO` (배포·리소스 생성·연결 미실행)**
> - 본 문서는 `T-007F` 관리형 환경 검증을 위한 **사전 실험 설계 및 비용 안전 게이트**입니다.
> - GCP Billing Account의 당월 누적 사용량·예산 알림 설정 여부, Supabase Organization의 당월 잔여 Uncached Egress·활성 프로젝트 슬롯, 도메인 보유 여부를 이번 작업에서 조회하지 않았으므로(클라우드 미접속 원칙) **무료 한도 안전 게이트 기준에 따라 현재 상태는 `NO-GO`로 표시**합니다.
> - 로컬 Docker 컨테이너 실행 성공(`tests/test_r05_containers.py`)은 패키징·프로세스 분리 확인일 뿐이며, **Cloud Run IAM, cold start, request-based CPU 회수, Supabase Supavisor pooler 동작의 증거로 대체하지 않습니다.**

---

## 1. `T-007F` 최소 호스팅 검증 설계 (검증 순서 · 측정 방법 · 통과 기준)

비용과 원인 분리를 위해 **DB 연결·권한 경계 → 비공개 Worker IAM → Cold Start 지연 → 호출자 Timeout / Worker 종료 후 재개** 순서로 단건·소량(총 요청 수 `< 100`건) 실험만 수행합니다.

### 단계별 검증 매트릭스

| 순서 | 검증 항목 | 실험 구성 및 측정 방법 | 통과 기준 (Pass Criteria) | 실패 시 조치 / 분기 |
|---:|---|---|---|---|
| **Step 1** | **Supabase Pooler 연결 수 및 권한 경계** (`T-007D` 결합) | - 호스팅 Supabase 테스트 프로젝트의 **Supavisor Transaction Mode (`:6543`)**에 `t007d_api_app` / `t007d_worker_app` 최소 권한 역할(`BYPASSRLS` 없음)로 연결<br>- `psycopg` 연결 시 startup `options` 미지원 여부와 prepared statement 충돌 여부를 확인(`prepare_threshold=None` 및 트랜잭션 내부 `SET LOCAL` 전환 측정)<br>- Cloud Run `max-instances=2`, `concurrency=4`, 인스턴스당 연결 상한 `1~2`로 제한한 상태에서 동시 요청 8건 실행 후 `pg_stat_activity` 및 Supavisor 클라이언트 슬롯 수 측정 | - `(EMAXCONN)` / `(EMAXCONNSESSION)` / `DuplicatePreparedStatement` 오류 `0`건<br>- 동시 연결 피크가 Nano/Free Supavisor 한도(`200`)의 `10%`(`<= 20`) 및 역할 `pool_size`(`15`) 이내<br>- 런타임 역할에서 `SET ROLE` 상승·타 Workspace 행 조회·DDL 시도 시 `InsufficientPrivilege` 또는 `0 rows` (fail-closed) | startup `options`가 `:6543`에서 거부되면 연결 문자열 옵션 대신 트랜잭션 시작 시 `SET LOCAL search_path / statement_timeout / lock_timeout`으로 전환 |
| **Step 2** | **비공개 Cloud Run Worker 호출 권한 (IAM / OIDC)** | - Worker를 `--no-allow-unauthenticated`, `--ingress=internal-and-cloud-load-balancing`(또는 기본 IAM 강제), `min-instances=0`, `max-instances=1`로 배포<br>- ① 무인증 요청, ② 권한 없는 SA의 OIDC 토큰 요청, ③ `roles/run.invoker`가 부여된 API 전용 SA(및 복구 전용 SA)의 `audience=<worker-url>` OIDC ID 토큰 요청을 각각 3회씩 전송해 HTTP 상태 코드와 Cloud Run 요청 로그 기록 | - ① 무인증 → `401/403` (`100%` 차단, 앱 컨테이너 미진입)<br>- ② 비인가 SA → `403` (`100%` 차단)<br>- ③ 인가된 API/복구 SA OIDC 토큰 → `200` 응답 및 단일 job 처리 완료 | `401/403` 미차단 시 즉시 서비스 삭제 및 IAM 바인딩 재점검 |
| **Step 3** | **첫 호출 지연 (Scale-to-Zero Cold Start)** | - `min-instances=0`에서 15분 이상 유휴 대기하여 활성 인스턴스 `0` 확인(`container/instance_count == 0`)<br>- API에 합성 작업 1건을 접수하고 ① API 내부 wake RTT(`T007A_WAKE_TIMEOUT_S=2s` 기준 성공/timeout 여부), ② Cloud Run 지표 `billable_instance_time` 및 첫 바이트 응답 시간(`p50/max`, 5회 반복, 각 회차 scale-to-zero 대기) 측정 | - Cold start 중 API wake가 timeout(`2s`) 나더라도 API 응답은 `202`(`wake="failed"`, `outbox="PENDING"`)로 즉시 반환되고, ① 이미 전달된 Worker 인플라이트 요청이 완료하거나 ② 다음 제한된 `recover` 1회 호출 내에 `SUCCEEDED` + `results=1`로 수렴<br>- Cold start 실측치(`T_cold`)를 기록해 기본 wake timeout 조정값(`min(5s, p95)` 등) 또는 큐 도입(OD-012) 판단 근거 확보 | Cold start가 일관되게 길어 wake 실패율이 높더라도 `outbox` 복구 SLA 이내면 직접 wake 유지 가능; SLA 초과 시 Cloud Tasks 등 디스패처 후보 비교 |
| **Step 4** | **호출자 Timeout 및 Worker 종료(`SIGTERM`) 후 재개** | - **4a (호출자 disconnect)**: 고정 합성 지연을 `4s`로 두고 API `T007A_WAKE_TIMEOUT_S=1s`로 호출해 API가 먼저 소켓을 끊었을 때, request-based 과금 Cloud Run이 Worker 요청의 CPU를 계속 유지해 `results=1`을 저장하는지 vs 요청 취소/스로틀로 중단되는지 Cloud Run 로그·DB 상태로 측정<br>- **4b (`SIGTERM` / lease 만료 재개)**: 다단계 초기 가져오기(`T-007B`) 1페이지 커밋 직후 새 리비전 배포 또는 강제 종료로 `SIGTERM`(10s 유예)을 발생시키고, 미완료 상태에서 lease 만료 후 `recover`가 남은 스트림만 이어받아 `IMPORT_READY=1`로 완료하는지 측정 | - **4a**: 완료되든 중단되든 `results <= 1`이고, 중단된 경우 `recover` 1회로 `SUCCEEDED` 수렴(열린 장기 트랜잭션 `0`개)<br>- **4b**: 1페이지 항목 중복 `0`건, 이전 인스턴스의 늦은 쓰기 `LEASE_TAKEN_OVER` / `STALE_CHECKPOINT_VERSION` 차단, 최종 `IMPORT_READY=1` (`MAX_ATTEMPTS_LEASE_EXPIRED` 소진 시 `FAILED` 수렴) | 4a에서 호출자 timeout 직후 Worker CPU가 즉시 회수되어 중도 절단이 잦다면, API wake는 짧은 "claim 트리거"가 아니라 비동기 디스패치(또는 Worker 응답 전 짧은 배치 제한)로 고정 |

---

## 2. 인바운드 경계 비교: Cloudflare Proxy vs Cloudflare Tunnel (배포 미수행 비교)

Cloud Run 공개 엔드포인트(향후 BFF/API 전면 배치 후보) 앞에 둘 수 있는 두 방식을 **상시 연결 프로세스 필요 여부**와 **원본 서버(Origin) 보호 수준** 기준으로 비교합니다.

| 비교 축 | Cloudflare DNS Reverse Proxy ("Orange Cloud") | Cloudflare Tunnel (`cloudflared`) | `T-007F` / MVP 아키텍처 시사점 |
|---|---|---|---|
| **상시 연결 프로세스(Daemon) 필요 여부** | **불필요.** DNS/HTTPS 리버스 프록시이므로 상시 실행할 커넥터 컨테이너나 VM이 없음. **Cloud Run `min-instances=0`(scale-to-zero)과 충돌하지 않음.** | **필요.** `cloudflared` 데몬이 Cloudflare 에지로 아웃바운드 터널을 **상시 유지**해야 함. Cloud Run에서 터널을 유지하려면 `min-instances >= 1` + instance-based CPU 상시 과금이 필요해 무료/유휴 `0`원 전제와 충돌함. | Scale-to-zero 비용 목표에서는 **상시 커넥터가 필요 없는 DNS Proxy(또는 Cloud Run 자체 도메인 매핑)**가 유리함. |
| **원본 서버(Origin) 노출 및 보호** | 원본이 공개 HTTPS 엔드포인트(`*.run.app` 등)를 가져야 함. 기본 설정만으로는 원본 URL 직접 우회 호출 가능성이 남으며, 완벽히 막으려면 Authenticated Origin Pulls(mTLS) 또는 앱 레벨 검증 헤더/토큰이 추가로 필요함. | 인바운드 방화벽 포트를 전혀 열지 않아도 됨(사설망/로컬 호스트 보호에 강점). 다만 **Worker는 어차피 외부 인바운드가 전혀 필요 없는 내부 전용 서비스**이므로 Cloud Run `--no-allow-unauthenticated` + IAM OIDC만으로 원본 보호가 완결됨. | - **Worker**: Cloudflare 불필요 (`--no-allow-unauthenticated` + IAM `run.invoker`로 직접 보호)<br>- **API/Web 전면**: 공개 도메인이 필요할 때만 Cloudflare DNS Proxy + WAF/Rate Limit 적용 검토 |

---

## 3. 무료 범위(Free Tier) 내 실행 조건 및 `GO / NO-GO` 게이트

**공식 무료 제공량(Official Tier Quota)**과 **실제 계정의 당월 잔여 한도(Account Remaining Quota)**는 다릅니다. 공식 무료 한도는 Billing Account 또는 Organization 전체 프로젝트가 공유하므로, 타 프로젝트 사용량이 있으면 실험 즉시 과금될 수 있습니다.

### 3.1 항목별 공식 요금 무료 범위 vs 계정별 사전 확인 필수 항목

| 리소스 항목 | 공식 무료 범위 (Official Free Tier 기준) | 계정별 잔여 한도 확인 자료 (미확인 시 `NO-GO`) | `T-007F` 실험 상한 (Hard Cap) |
|---|---|---|---|
| **Cloud Run** | Request-based(Tier 1 리전 기준, Billing Account 공유): 월 **180,000 vCPU-sec**, **360,000 GiB-sec**, **200만 요청**, 북미 인터넷 egress **1 GB** *(주의: Tier 2 리전 예: `asia-northeast3` 서울은 무료 티어 적용 방식/단가가 다를 수 있음)* | GCP Billing → 당월 Cloud Run SKU 누적 사용량 및 배포 대상 리전의 Free Tier 적격 여부 확인 캡처 | 2개 서비스(API, Worker), 각 `cpu=1`, `memory=256Mi~512Mi`, `min-instances=0`, `max-instances=2`, 총 실험 요청 `< 100`건, 실험 시간 `< 30분` |
| **Artifact Registry** | Billing Account 합산 월 **0.5 GiB (500 MB)** 스토리지 무료. 취약점 자동 스캔(Container Scanning API) 및 타 리전/인터넷 egress는 **유료** | Artifact Registry 전체 저장소 합산 용량(`< 200 MB` 여유 필요) 및 `containerscanning.googleapis.com` **비활성화** 확인 | 단일 `python:3.12-slim` 기반 이미지 1개(`~60–90 MB` 압축), 동일 리전 배포, 실험 직후 이미지 태그·다이제스트 즉시 삭제 |
| **Secret Manager** | Billing Account 합산 활성 시크릿 버전 **6개**, 접근 연산 월 **10,000회** 무료 | 현재 Billing Account 내 활성(Enabled) secret version 총개수 확인 (`<= 4`개일 때만 신규 1~2개 허용) | `T007A_DATABASE_URL` 1개 버전만 생성(또는 1회성 실험 시 비영속 런타임 주입 후 즉시 폐기), 실험 종료 시 secret 삭제 |
| **Cloud Logging** | 프로젝트당 월 **50 GiB** 수집 무료, 기본 **30일** 보관 무료 | GCP Monitoring/Logging 당월 수집량 확인 (`< 40 GiB`) | DEBUG 대량 로그 비활성(`INFO`/`WARNING`), 단건 실험으로 `< 5 MB` 유지 |
| **Cloud Scheduler** *(선택)* | Billing Account당 월 **3개 잡** 무료 | 기존 Scheduler 잡 개수 확인 (`< 3`개) | **생성하지 않음 권장** (`T-007F`에서는 수동 OIDC 호출로 `/internal/t007a/recover` 트리거하여 잡 `0`개 유지) |
| **Supabase (DB · Pooler · 전송량)** | Free Plan(Organization 기준): 활성 프로젝트 최대 **2개**, DB 디스크 **500 MB**(초과 시 read-only 전환), **Uncached Egress 월 5 GB** + Cached 5 GB, Nano 인스턴스 Direct 연결 `60` / Supavisor `:6543` 클라이언트 `200` (`pool_size` 기본 `15`) | Supabase Org Usage 화면에서 ① 당월 **Uncached Egress 잔여량**(`>= 1 GB` 여유), ② DB 크기(`< 300 MB`), ③ 비운영(전용 테스트) 프로젝트 여부 확인 | 격리 스키마 `t007a_spike`만 사용, 합성 텍스트 `< 1 KB/건`, 총 DB 전송량 `< 5 MB`, 실험 종료 시 `DROP SCHEMA t007a_spike CASCADE` |
| **Cloudflare / 도메인** | Cloudflare DNS Proxy 및 Zero Trust Tunnel(50석 이하) 자체는 **\$0/월**, `trycloudflare.com` 임시 터널 무료. 단, **신규 커스텀 도메인 등록은 유료(연 \$10~)** | 기존 보유 도메인 유무 확인; 신규 도메인 구매가 필요하면 비용 발생 | **`T-007F`에서는 커스텀 도메인 구매·연결 금지** (`*.run.app` 기본 URL + IAM OIDC만으로 검증하여 도메인 비용 `\$0` 고정) |

### 3.2 현재 판정: `NO-GO` 사유
1. GCP Billing Account의 당월 누적 사용량(Cloud Run / Artifact Registry `0.5 GiB` / Secret Manager `6 versions`), 예산 알림 설정, Free Tier 적격 리전 선택이 아직 확인·제출되지 않음.
2. Supabase Organization의 당월 잔여 Uncached Egress(`5 GB` 한도) 및 전용 비운영 테스트 프로젝트 지정이 확인되지 않음.
3. 사용자 배포 승인(IAM 서비스 계정·Cloud Run 리소스 생성 승인)이 아직 없음.

---

## 4. 중단(Kill-Switch) · 안전한 정리(Teardown) · 수동 재개 기준 및 무료 로컬 대안

### 4.1 신규 작업 즉시 중단(Stop / Kill-Switch) 기준
다음 중 하나라도 발생하면 즉시 신규 요청 주입을 멈추고 **4.2 정리 절차**를 실행합니다.
- GCP Billing 예산 알림 또는 당월 예상 청구액이 **`$0.01` 이상** 발생하거나 Free Tier 적격 리전이 아닌 것으로 확인될 때
- Artifact Registry 저장 용량이 **`400 MB` (`0.5 GiB`의 80%)**를 넘거나 Container Scanning API가 켜진 것이 감지될 때
- Cloud Run 인스턴스가 루프/재시도 오류로 의도치 않게 연속 실행되어 요청 수가 **`200`건**을 넘을 때
- Supabase Uncached Egress가 일일 **`500 MB`** 이상 증가하거나 DB 용량이 **`400 MB`**를 넘을 때
- 비공개 Worker 엔드포인트가 무인증 요청에 `401/403` 이외의 응답(`2xx`/`5xx`)을 반환할 때

### 4.2 안전한 정리(Safe Teardown) 순서
1. **트래픽·스케줄 차단**: 외부 테스트 스크립트 종료(Scheduler를 만들었다면 먼저 pause/delete).
2. **Cloud Run 서비스 삭제**: Worker → API 순서로 서비스 삭제(인플라이트 요청 종료 후 인스턴스 `0` 확정).
3. **컨테이너 이미지 삭제**: Artifact Registry의 실험 이미지 태그 및 untagged manifest digest 완전 삭제(`0 B` 복원).
4. **시크릿·전용 SA 정리**: 실험용 Secret version destroy/delete 및 임시 서비스 계정/`run.invoker` 바인딩 제거.
5. **테스트 DB 스키마 정리**: 테스트 전용 Supabase DB에서 `DROP SCHEMA IF EXISTS t007a_spike CASCADE;` 실행 및 활성 세션 `0`개 확인.

### 4.3 수동 재개(Manual Resume) 기준
자동 재배포·자동 재시도 스크립트로 재개하지 않으며, 아래 3가지를 사람이 직접 확인한 뒤에만 수동 재개합니다.
1. 위 4.1 중단 원인의 근본 수정(예: 리전 변경, 타임아웃/루프 수정, 스캔 API 비활성화)이 로컬 테스트에서 먼저 통과함.
2. 4.2 정리 완료 후 GCP Billing / Artifact Registry / Secret Manager / Supabase Usage 대시보드에서 잔여 한도가 안전 범위 내임을 재확인함.
3. 프로젝트 소유자의 명시적 재실행 승인을 받음.

### 4.4 비용 `$0` 무료 대안 (로컬 검증 유지)
호스팅 승인과 잔여 한도 확인 전까지는 클라우드 리소스를 전혀 쓰지 않는 아래 2단계 로컬 검증을 무료 대안으로 유지합니다.
1. **일회성 로컬 PostgreSQL 18 수직·복구·초기가져오기·방안 C 회귀 (`48`건)**: `.venv/bin/python -m pytest -v --ignore=tests/test_r05_containers.py`
2. **동일 `Dockerfile` 기반 API/Worker 역할 분리 및 Worker 중단·복구 컨테이너 검증 (`1`건)**: `.venv/bin/python -m pytest -v tests/test_r05_containers.py`
   *(단, 위 로컬 검증은 Cloud Run IAM/cold start/CPU 회수 및 호스팅 Supabase Pooler 동작을 대체하지 못하므로 해당 항목은 호스팅 실험 전까지 `미검증`으로 유지합니다.)*
