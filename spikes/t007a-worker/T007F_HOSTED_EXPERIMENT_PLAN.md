# T-007F 최소 호스팅 실험 설계 및 무료 한도(Zero-Cost) 안전 게이트

> [!CAUTION]
> **현재 실행 판정: `NO-GO` (클라우드 연결·이미지 업로드·리소스 생성·배포 미실행, 호스팅 승인 전 RUNTIME 대기)**
> - 본 문서는 `T-007F` 관리형 환경 검증을 위한 **정정된 사전 실험 설계 및 비용 안전 게이트(`RUNTIME-06` 반영)**입니다.
> - **계정별 당월 잔여 한도**, **과금·사용량 집계 지연(Metering Lag)**을 감안한 안전 여유, **정리 여유(Teardown Buffer)**, 그리고 공용 프로젝트의 유료 보안 스캔 활성화 여부를 확인하지 않았으므로 **현재 판정은 `NO-GO`를 유지**합니다.
> - **GCP 예산 알림(Budget Alert)은 지연된 사후 통지일 뿐 자동 차단(Auto-Shutoff) 수단이 아닙니다.** 또한 비용 회피를 이유로 **공용 프로젝트·조직의 보안 설정(예: Container Scanning API, 조직 정책, 공용 IAM)을 임의로 변경·비활성화하지 않습니다.**
> - 로컬 Docker 컨테이너 실행(`tests/test_r05_containers.py` 및 `linux/amd64` 타깃 검증 `tests/test_r06_amd64_container.py`)은 로컬 패키징·역할 분리 증거일 뿐이며, **Cloud Run IAM, 네트워크 인그레스 경로, cold start, request-based CPU 회수, 호스팅 Supabase Supavisor 동작을 대체하지 않습니다.**

---

## 1. `T-007F` 최소 호스팅 검증 설계 (정정된 순서 · 측정 방법 · 통과 기준)

### 핵심 설계 원칙 (`RUNTIME-06` 정정 사항)
1. **서비스 준비 및 호출 권한/네트워크 검증 선행**: Cloud Run을 사용하는 Supabase Pooler 실험은 **서비스 준비 → IAM 호출 권한 검증 → 네트워크 접근 경로 검증**이 모두 통과한 뒤에 배치합니다.
2. **IAM 권한과 네트워크 접근 경로 분리 검증**: 호출 주체 신원을 검사하는 **Cloud Run IAM (`roles/run.invoker` + OIDC)**과 패킷 유입 경로를 제한하는 **`--ingress` 네트워크 정책**은 서로 다른 계층이므로 별개 단계로 분리 검증합니다.
3. **30분 이내 탐색적 Cold Start 측정 (`p95` 해석 금지)**: 전체 실험 시간 상한(`<= 30분`) 안에서 실행 가능하도록 첫 호출 지연은 **`n = 2~3`회 소표본 탐색 측정**으로 제한하고, 원시 관측값(`raw samples`, `min/max`)만 기록합니다. **표본이 적으므로 `p50`/`p95` 등 분위수(percentile)로 해석하거나 외삽하지 않습니다.**
4. **합산 요청 상한과 중단 기준 단일화 (`<= 100`건)**: 외부 클라이언트 호출 + API→Worker wake 호출 + 자동/수동 재시도 + `/internal/t007a/recover` 복구 호출 + 권한/경로 프로브를 **모두 합산한 총 요청 상한(Hard Cap)과 즉시 중단(Kill-Switch) 기준을 동일하게 `100`건**으로 고정합니다(계획 사용량 약 `50~65`건).

### 단계별 검증 매트릭스 (총 실험 시간 `<= 30분`, 합산 총 요청 `<= 100`건)

| 순서 | 검증 항목 | 실험 구성 및 측정 방법 (예상 합산 요청 수) | 통과 기준 (Pass Criteria) | 실패 시 조치 / 분기 |
|---:|---|---|---|---|
| **Step 1** | **서비스 준비 및 비공개 Worker IAM 호출 권한** *(DB 비의존)* | - 단일 `linux/amd64` 이미지로 API·Worker를 `min-instances=0`, `max-instances=1`로 준비하고 DB 연결 없이 경량 상태 엔드포인트 기준으로 **호출 주체(Identity) 인가**만 먼저 검증 *(약 `9`건)*<br>- ① 무인증 요청 3회, ② `roles/run.invoker`가 없는 비인가 SA의 OIDC 토큰 요청 3회, ③ `roles/run.invoker`가 부여된 API 전용 SA(및 복구 전용 SA)의 `audience=<worker-url>` OIDC ID 토큰 요청 3회 전송 | - ① 무인증 → `401/403` (`100%` 차단, 앱 컨테이너 미진입)<br>- ② 비인가 SA → `403` (`100%` 차단)<br>- ③ 인가된 API/복구 SA OIDC 토큰 → `200` 응답 | `401/403` 미차단 시 이후 단계 진입 금지, 즉시 서비스 삭제(Teardown) 및 IAM 바인딩 점검 |
| **Step 2** | **비공개 Worker 네트워크 접근 경로 (`--ingress`)** *(IAM과 분리 검증)* | - IAM 신원 검증과 별개로 **네트워크 유입 경로 제어(`--ingress`)** 동작을 확인 *(약 `6`건)*<br>- 유료 상시 리소스(Serverless VPC Connector 등)를 만들지 않는 조건에서, ① Worker `--ingress` 설정값(`internal` vs `internal-and-cloud-load-balancing` vs `all`)에 따라 공용망 직접 호출과 기본 서버리스 API→Worker 호출이 각각 네트워크 계층에서 어떻게 허용/차단되는지 HTTP 응답 및 Cloud Run 시스템 로그로 분리 기록 | - IAM 차단(`401/403` 인증·권한 실패)과 네트워크 인그레스 차단(`403/404` 경로 거부)을 혼동 없이 구분해 문서화<br>- 유료 VPC Connector 없이 API→Worker 호출이 성립하는 최소 노출 구성(예: VPC 미구성 시 `--ingress=all` + 강제 IAM OIDC의 경로 노출 한계 명시, 또는 무료 범위 확인된 Direct VPC Egress 여부)을 확정 | API→Worker 기본 이그레스가 `--ingress=internal`에 막힐 경우 유료 VPC Connector를 임의 생성하지 않고, 비용 `\$0` 범위 내 허용 구성과 보안 한계를 기록 |
| **Step 3** | **탐색적 첫 호출 지연 (Scale-to-Zero Cold Start)** *(`<= 30분` 내 소표본)* | - 전체 `30분` 창을 넘기지 않도록 **최대 `n = 2~3`회 탐색 측정**만 수행 *(약 `6~9`건, 대기 상한 `<= 15분`)*:<br>  - `Sample 1`: 초기 배포(또는 무트래픽 신규 리비전) 직후 `instance_count == 0` 상태의 첫 합성 작업 호출<br>  - `Sample 2~(3)`: 유휴 대기 후 `instance_count == 0` 축소가 `15분` 내에 확인될 때 1회(여유 시 최대 2회) 추가 호출<br>- API wake RTT(`T007A_WAKE_TIMEOUT_S` 성공/timeout 여부)와 Cloud Run 첫 응답 지연의 **개별 원시값(`t_1, t_2, ...` 및 `min/max`)만 기록** (**소표본이므로 `p95` 등 분위수 산출·해석 금지**) | - Cold start 중 wake가 timeout 나더라도 API는 `202`(`wake="failed"`, `outbox="PENDING"`)를 반환하고, 인플라이트 처리 완료 또는 제한된 `recover` 1회 호출로 `SUCCEEDED` + `results=1` 수렴<br>- 원시 관측치 범위(`min~max`)를 기록하되 통계적 `p95` 보장치로 해석하지 않고 후속 큐/타임아웃 의사결정(OD-012)의 탐색 참고치로만 분류 | `15분` 내 유휴 축소가 관찰되지 않으면 추가 대기를 중단해 `30분` 상한을 지키고 부분 표본(`n=1~2`) 한계를 명시 |
| **Step 4** | **Cloud Run → Supabase Pooler 연결 수 및 권한 경계** *(`T-007D` 결합, 서비스·권한 확인 후 실행)* | - Step 1~2 통과 후 Cloud Run(`max-instances=2`, `concurrency=4`, 인스턴스당 DB 연결 상한 `1~2`)에서 호스팅 Supabase 테스트 프로젝트의 **Supavisor Transaction Mode (`:6543`)**에 `t007d_api_app` / `t007d_worker_app`(`BYPASSRLS` 없음)으로 연결 *(약 `20~24`건)*<br>- `prepare_threshold=None` 및 트랜잭션 내부 `SET LOCAL` 구성으로 동시 요청 `8`건 + 권한 경계 음수 테스트 `4`건 실행 후 `pg_stat_activity`·Supavisor 슬롯 측정 | - `(EMAXCONN)` / `(EMAXCONNSESSION)` / `DuplicatePreparedStatement` 오류 `0`건<br>- 동시 연결 피크 `<= 20` (Nano/Free Supavisor `200`의 `10%`, 역할 `pool_size=15` 이내)<br>- 런타임 역할에서 `SET ROLE` 상승·타 Workspace 조회·DDL 시도 시 `InsufficientPrivilege` 또는 `0 rows` (fail-closed) | startup `options`가 `:6543`에서 거부되면 연결 문자열 옵션 대신 트랜잭션 시작 시 `SET LOCAL search_path / statement_timeout / lock_timeout`으로 고정 |
| **Step 5** | **호출자 Timeout 및 Worker 종료(`SIGTERM`) 후 재개** | - **5a (호출자 disconnect)**: 합성 지연 `4s`, API `T007A_WAKE_TIMEOUT_S=1s`로 API 소켓 조기 종료 시 request-based Cloud Run의 Worker CPU 유지/스로틀 여부와 DB 수렴 상태 측정 *(약 `6`건)*<br>- **5b (`SIGTERM` / lease 만료 재개)**: `T-007B` 1페이지 커밋 직후 리비전 교체/종료로 `SIGTERM` 발생 후 lease 만료 뒤 `recover`가 남은 스트림만 이어받아 완료하는지 측정 *(약 `10~14`건)* | - **5a**: `results <= 1` 유지, 중도 절단 시 `recover` 1회로 `SUCCEEDED` 수렴(장기 열린 트랜잭션 `0`개)<br>- **5b**: 1페이지 항목 중복 `0`건, 이전 인스턴스 늦은 쓰기 `LEASE_TAKEN_OVER` / `STALE_CHECKPOINT_VERSION` 차단, 최종 `IMPORT_READY=1` | 5a에서 호출자 disconnect 직후 CPU 회수로 절단이 빈번하면, 직접 wake는 초단기 트리거로만 제한하거나 비동기 디스패처 후보(OD-012) 비교로 전환 |

---

## 2. 인바운드 경계 비교: Cloudflare Proxy vs Cloudflare Tunnel (배포 미수행 비교)

Cloud Run 공개 엔드포인트(향후 BFF/API 전면 배치 후보) 앞에 둘 수 있는 두 방식을 **상시 연결 프로세스 필요 여부**와 **원본 서버(Origin) 보호 수준** 기준으로 비교합니다.

| 비교 축 | Cloudflare DNS Reverse Proxy ("Orange Cloud") | Cloudflare Tunnel (`cloudflared`) | `T-007F` / MVP 아키텍처 시사점 |
|---|---|---|---|
| **상시 연결 프로세스(Daemon) 필요 여부** | **불필요.** DNS/HTTPS 리버스 프록시이므로 상시 실행할 커넥터 컨테이너나 VM이 없음. **Cloud Run `min-instances=0`(scale-to-zero)과 충돌하지 않음.** | **필요.** `cloudflared` 데몬이 Cloudflare 에지로 아웃바운드 터널을 **상시 유지**해야 함. Cloud Run에서 터널을 유지하려면 `min-instances >= 1` + instance-based CPU 상시 과금이 필요해 무료/유휴 `\$0` 전제와 충돌함. | Scale-to-zero 비용 목표에서는 **상시 커넥터가 필요 없는 DNS Proxy(또는 Cloud Run 자체 도메인 매핑)**가 유리함. |
| **원본 서버(Origin) 노출 및 보호** | 원본이 공개 HTTPS 엔드포인트(`*.run.app` 등)를 가져야 함. 기본 설정만으로는 원본 URL 직접 우회 호출 가능성이 남으며, 완벽히 막으려면 Authenticated Origin Pulls(mTLS) 또는 앱 레벨 검증 헤더/토큰이 추가로 필요함. | 인바운드 방화벽 포트를 전혀 열지 않아도 됨(사설망/로컬 호스트 보호에 강점). 다만 **Worker는 어차피 외부 인바운드가 전혀 필요 없는 내부 전용 서비스**이므로 Cloud Run `--no-allow-unauthenticated` + IAM OIDC만으로 신원 보호가 가능함. | - **Worker**: Cloudflare 불필요 (`--no-allow-unauthenticated` + IAM `run.invoker` 및 Step 2 인그레스 검증 대상)<br>- **API/Web 전면**: 공개 도메인이 필요할 때만 Cloudflare DNS Proxy + WAF/Rate Limit 검토 |

---

## 3. 무료 범위(Free Tier) 내 실행 조건 및 `GO / NO-GO` 안전 게이트

**공식 무료 제공량(Official Tier Quota)**과 **계정별 당월 잔여 한도(Account Remaining Quota)**는 다르며, 여기에 **집계 지연(Metering Lag)**과 **정리 여유(Teardown Buffer)**까지 감안해야 실제 `\$0` 실행이 보장됩니다.

> [!WARNING]
> - **예산 알림(Budget Alert)은 자동 차단이 아님**: GCP Billing 예산 알림은 수 시간 이상 지연될 수 있는 사후 통지 메커니즘이며, 한도 도달 시 Cloud Run이나 Artifact Registry를 자동으로 멈추지 않습니다.
> - **공용 보안 설정 변경 금지**: 비용을 피하기 위해 공용 프로젝트나 조직에 설정된 보안 기능(예: `containerscanning.googleapis.com` 취약점 자동 스캔, 조직 IAM/보안 정책)을 끄거나 약화시키지 않습니다. 공용 프로젝트에 이미지 푸시 즉시 과금되는 보안 스캔이 켜져 있다면 설정을 변경하는 대신 **`NO-GO`를 유지**(또는 별도 격리 샌드박스 프로젝트 승인 요청)합니다.

### 3.1 항목별 공식 무료 범위 vs 계정별 잔여 한도 · 집계 지연 · 정리 여유 확인 기준

| 리소스 항목 | 공식 무료 범위 (Official Free Tier 기준) | 사전 확인 필수 항목: 잔여 한도 · 집계 지연 · 정리 여유 (하나라도 미충족 시 `NO-GO`) | `T-007F` 실험 상한 (Hard Cap) |
|---|---|---|---|
| **Cloud Run** | Request-based(Tier 1 리전 기준, Billing Account 전체 공유): 월 **180,000 vCPU-sec**, **360,000 GiB-sec**, **200만 요청**, 북미 인터넷 egress **1 GB** *(Tier 2 리전 예: `asia-northeast3` 서울은 무료 티어 조건/단가 상이 가능)* | - 배포 리전의 Tier 1 Free Tier 적격 여부<br>- Billing Account 내 타 프로젝트 합산 당월 사용량이 무료 한도의 **`50%` 미만**일 것(최소 24시간 **집계 지연** 감안 안전 여유)<br>- **예산 알림은 참고용 통지**로만 두고 클라이언트 요청 하드 스톱으로 통제 | API·Worker 2개 서비스, `linux/amd64`, 각 `cpu=1`, `memory=256Mi~512Mi`, `min-instances=0`, `max-instances=2`, **호출·재시도·복구 합산 총 요청 `<= 100`건**, 총 실험 시간 **`<= 30분`** |
| **Artifact Registry** | Billing Account 합산 월 **0.5 GiB (500 MB)** 스토리지 무료. **Container Scanning(취약점 자동 스캔) 및 타 리전/인터넷 egress는 유료** | - Billing Account 전체 저장소 현재 사용량이 **`<= 250 MB`**일 것(`linux/amd64` 단일 이미지 + 레이어/매니페스트 **정리 여유 `>= 250 MB`** 확보)<br>- 대상 프로젝트에 유료 Container Scanning이 활성화되어 있지 않음을 **조회로만 확인** (**활성화되어 있으면 공용 설정을 끄지 않고 `NO-GO` 판정**) | 단일 `python:3.12-slim` (`linux/amd64`) 이미지 1개만 업로드, Cloud Run과 **동일 리전** 배치, 실험 종료 즉시 태그 및 untagged digest 완전 삭제 |
| **Secret Manager** | Billing Account 합산 활성 시크릿 버전 **6개**, 접근 연산 월 **10,000회** 무료 | - Billing Account 전체 활성(Enabled) secret version 총개수 **`<= 4`개**(신규 1개 + 교체/정리 여유 1개 확보) 및 당월 접근 수 `< 5,000`회 | `T007A_DATABASE_URL` 활성 버전 최대 **1개**만 사용, 실험 종료 즉시 버전 destroy 및 시크릿 삭제 |
| **Cloud Logging** | 프로젝트당 월 **50 GiB** 수집 무료, 기본 **30일** 보관 무료 | - 당월 수집량 `< 35 GiB`(집계 지연 및 정리 전 로그 유입 여유 `>= 15 GiB`) | `INFO`/`WARNING` 수준만 기록(원문 비영속 준수), 실험 수집량 `< 5 MB` |
| **Cloud Scheduler / VPC** | Scheduler 월 **3개 잡** 무료; Serverless VPC Connector는 인스턴스 상시 과금(유료) | - 기존 Scheduler 잡 수 확인; VPC Connector 생성 금지 | **Scheduler 잡 `0`개, 유료 VPC Connector `0`개** (`recover`는 테스트 러너에서 OIDC 수동 호출) |
| **Supabase (DB · Pooler · 전송량)** | Free Plan(Organization 공유): 활성 프로젝트 **2개**, DB **500 MB**, **Uncached Egress 월 5 GB** + Cached 5 GB, Supavisor `:6543` 클라이언트 `200` (`pool_size` 기본 `15`) | - 비운영 **전용 테스트 프로젝트** 여부<br>- Org 당월 Uncached Egress 사용량 **`<= 3.5 GB`**(최소 수 시간 **집계 지연** 및 정리 여유 `>= 1.5 GB`), DB 용량 **`<= 300 MB`** | 격리 스키마 `t007a_spike`만 사용, 합성 payload `< 1 KB/건`, 실험 전송량 `< 5 MB`, 종료 즉시 `DROP SCHEMA t007a_spike CASCADE` |
| **Cloudflare / 도메인** | Cloudflare DNS Proxy 및 Zero Trust Tunnel(50석 이하) **\$0/월**, `trycloudflare.com` 무료. **신규 커스텀 도메인 등록은 유료(연 \$10~)** | - 신규 도메인 구매 불필요 확인 | **`T-007F`에서 커스텀 도메인 구매·연결 금지** (`*.run.app` 기본 URL + IAM OIDC만 사용, 비용 `\$0`) |

### 3.2 현재 판정: `NO-GO` 유지 사유
1. **계정별 잔여 한도 및 집계 지연 여유 미확인**: GCP Billing Account(타 프로젝트 합산 Cloud Run / Artifact Registry `0.5 GiB` / Secret Manager `6 versions`) 및 Supabase Organization(`5 GB` Uncached Egress)의 당월 실사용량과 집계 지연 안전 여유가 확인되지 않음.
2. **공용 프로젝트 보안 설정 과금 여부 미확인**: 대상 GCP 프로젝트에 유료 Container Scanning 등이 켜져 있는지 미확인 상태이며, 공용 보안 설정은 변경하지 않으므로 확인 전까지 과금 가능성을 배제할 수 없음.
3. **정리 여유(Teardown Buffer) 및 명시적 호스팅 승인 부재**: 테스트 전용 프로젝트 지정, 정리 실패 대비 여유 한도, 리소스 생성/삭제 실행 승인이 아직 없음.

---

## 4. 중단(Kill-Switch) · 안전한 정리(Teardown) · 수동 재개 기준

### 4.1 신규 작업 즉시 중단(Stop / Kill-Switch) 기준 *(실험 상한과 일치)*
예산 알림 수신을 기다리지 않고(예산 알림은 자동 차단이 아님), 테스트 러너·운영자가 다음 중 하나라도 감지하면 **즉시 신규 호출·재시도·복구 전송을 멈추고 4.2 정리 절차**로 진입합니다.
- **합산 요청 수 상한 도달**: 외부 호출 + 내부 wake + 재시도 + `recover` + 프로브를 합친 **총 요청 수가 상한인 `100`건**에 도달하거나, 실험 경과 시간이 **`30분`**에 도달했을 때
- **비인가 노출 감지**: 비공개 Worker가 무인증 또는 비인가 SA 요청에 `401/403` 이외의 응답(`2xx`/`5xx`)을 반환할 때
- **스토리지·전송량 안전선 침범**: Artifact Registry 총용량이 **`400 MB`**를 넘거나, Supabase DB 용량이 **`400 MB`**를 넘거나, 실험 중 비정상 재시도 루프/대량 로그가 감지될 때
- **과금 징후 감지**: 유료 API(예: Container Scanning, 유료 리전 SKU, VPC Connector) 호출/활성화가 감지되거나 예상 청구액이 **`\$0.01` 이상**으로 표시될 때 *(단, 공용 보안 API는 임의로 끄지 않고 즉시 이미지·서비스만 정리 후 중단)*

### 4.2 안전한 정리(Safe Teardown) 순서 및 정리 확인
1. **호출 즉시 정지**: 로컬 실험 실행기 종료(추가 요청·재시도·복구 호출 `0`건 확정).
2. **Cloud Run 서비스 삭제**: Worker → API 순서로 서비스 삭제 후 활성 서비스·인스턴스 `0`개 확인.
3. **Artifact Registry 이미지 완전 삭제**: 실험 이미지 태그 및 모든 untagged manifest digest를 삭제하여 실험 추가 용량 `0 B` 복원 확인.
4. **시크릿·전용 SA 정리**: 실험용 Secret version destroy/시크릿 삭제 및 실험 전용 서비스 계정/`run.invoker` 바인딩 제거.
5. **테스트 DB 스키마 정리**: 테스트 전용 Supabase DB에서 `DROP SCHEMA IF EXISTS t007a_spike CASCADE;` 실행 및 잔여 런타임 세션 `0`개 확인.

### 4.3 수동 재개(Manual Resume) 기준
자동 재배포·자동 재시도로 재개하지 않으며, 아래 조건이 모두 충족될 때만 수동 재개합니다.
1. 중단 원인이 로컬 테스트(`pytest` 50건 전체)에서 먼저 수정·검증됨.
2. 4.2 정리 완료 및 **집계 지연 시간 경과 후**에도 GCP Billing / Artifact Registry / Secret Manager / Supabase Usage 잔여 한도와 정리 여유(3.1 기준)가 안전 범위 내임이 확인됨.
3. 공용 보안 설정 변경 없이 과금 위험이 해소되었고, 프로젝트 소유자의 명시적 호스팅 실행 승인을 받음.

### 4.4 호스팅 승인 전 비용 `\$0` 로컬 검증 대안 (현재 완료 및 대기 기준)
호스팅 승인이 내려오기 전까지는 클라우드 연결 없이 아래 로컬 검증 결과만 유지하고 추가 RUNTIME 호스팅 작업은 대기합니다.
1. **일회성 로컬 PostgreSQL 18 회귀 (`48`건)** + **로컬 Docker 단일 이미지 API/Worker 중단·복구 (`1`건, `test_r05_containers.py`)**
2. **Cloud Run 타깃 플랫폼(`linux/amd64`) 로컬 전용 이미지 빌드·플랫폼 검증·단일 작업 실행 (`1`건, `test_r06_amd64_container.py`)**
   *(로컬 `linux/amd64` 검증 역시 관리형 Cloud Run IAM/인그레스/cold start/Supabase Pooler 증거를 대체하지 않습니다.)*
