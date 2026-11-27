# T-007F Step 1용 DB 비의존 로컬 IAM 프로브 후보 (`RUNTIME-IAM-PROBE-CHECKPOINT`)

> [!CAUTION]
> **현재 게이트 판정: `오너 결정에 따른 호스팅 검증 유예 (Deferred / Waived for current gate, 클라우드 실환경 미실행)` · 로컬 재빌드 기준점 전용 (`NO-GO` 유지)**
> - 프로젝트 오너 결정에 따라 M0→M1 로컬 구현 전환 차단은 해제하되, **실제 Cloud Run 배포·IAM(`roles/run.invoker`/OIDC)·네트워크 `--ingress`·Artifact Registry 청구량·호스팅 Supabase `:6543` 동작은 모두 미실행/미검증** 상태를 유지하며 향후 호스팅 진입 전 본 프로브 및 상위 런북(`../T007F_HOSTED_EXPERIMENT_PLAN.md`) 기준으로 별도 실행해야 합니다.
> - 본 디렉터리는 향후 `T-007F` Step 1(비공개 Cloud Run IAM 호출 권한 검증)에 사용할 **DB 의존성 없는 경량 프로브 소스와 digest 고정 기반 이미지의 로컬 재현 기준점**입니다.
> - 기존 Worker(`t007a.worker:create_worker_app`)는 초기화 시 `T007A_DATABASE_URL`을 필수로 요구하므로, 가짜 DSN을 주입해 Worker 이미지를 그대로 띄우지 않고 별도 격리 디렉터리(`spikes/t007a-worker/hosted-iam-probe/`)로 분리했습니다.
> - **`READY` 상태는 로컬 재빌드 준비 완료만을 뜻하며, Cloud Run 배포 승인·서비스 신원(Service Account)·Cloud Run 앞단 IAM(`--no-allow-unauthenticated` / `roles/run.invoker` / OIDC)·네트워크 인그레스(`--ingress`)·Artifact Registry 실제 전송/저장 청구량·보안 스캔 비용은 모두 미검증(`NO-GO` 유지)입니다.**

---

## 1. 구성 파일 및 경계

| 상대 경로 (워크트리 루트 기준) | 역할 |
|---|---|
| `spikes/t007a-worker/hosted-iam-probe/probe.py` | Python 표준 라이브러리(`http.server.ThreadingHTTPServer`)만 사용하는 DB 비의존 HTTP 프로브. `0.0.0.0`과 주입된 `PORT`(기본 `8080`, `1..65535` 검증)로 기동하며 `GET /healthz`만 고정 응답(`{"ok":true,"probe":"t007f-iam-probe"}\n`, 38 bytes) 반환 |
| `spikes/t007a-worker/hosted-iam-probe/Dockerfile` | 검증된 공식 OCI 이미지 인덱스 digest(`python:3.12-slim@sha256:191083...`) 고정, 외부 패키지 설치 없음, `USER nobody`, `PORT=8080`, `CMD ["python", "-B", "/app/probe.py"]` |
| `spikes/t007a-worker/hosted-iam-probe/.dockerignore` | 테스트·문서·바이트코드 파일이 Docker 빌드 컨텍스트에 포함되지 않도록 제외 |
| `spikes/t007a-worker/hosted-iam-probe/test_probe.py` | 표준 라이브러리 `unittest` 기반 로컬 검증 스위트 (`6`개 테스트, 고정 digest 정적 계약 포함) |
| `spikes/t007a-worker/hosted-iam-probe/README.md` | 기반 이미지 digest·플랫폼 근거, 재현·해시 대조 명령, 로컬 검증 및 정리 결과 |

### 의도적 제외 범위
- Worker 라우트(`/internal/t007a/wake`, `/internal/t007a/recover`), 제품/프로토타입 API(`/_prototype/t007a/*`), DB 연결, 외부 API 호출, 토큰 파싱·검증 로직을 포함하지 않습니다.
- 앱 코드 안에서 IAM을 흉내 내거나 인증되지 않은 호출을 승인·거부하는 인증 로직을 두지 않습니다. 실제 호출 주체 인증·인가는 향후 호스팅 시 Cloud Run 앞단(`--no-allow-unauthenticated` + `roles/run.invoker`)에서 검증합니다.
- 응답 헤더·바디와 `stdout`/`stderr` 로그에는 자격 정보, 환경 변수 값(`PORT`, `T007A_DATABASE_URL` 등 포함), 내부 설정, 요청 헤더(`Authorization`, `Cookie`), 쿼리 문자열, 미검증 경로, 요청 본문을 출력하지 않습니다.

---

## 2. 기반 이미지 Digest 및 `linux/amd64` 플랫폼 근거

로컬 Docker 캐시(`desktop-linux`, Docker Engine `29.1.2 aarch64`, `io.containerd.snapshotter.v1`)에서 추가 pull 없이 `docker image inspect` 및 `docker image ls --tree`로 확인한 매핑입니다.

| 구분 | Digest / 식별자 | Media Type / 플랫폼 | 로컬 확인 근거 |
|---|---|---|---|
| **멀티아키텍처 OCI 이미지 인덱스 (`Dockerfile` 고정 대상)** | `python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b` | `application/vnd.oci.image.index.v1+json` (size `10,373` B) | 로컬 `RepoDigests` 키와 일치(`docker image inspect python@sha256:191083... --platform linux/amd64` 성공). 인덱스 SHA-256이 하위 플랫폼 manifest digest 전체를 암호학적으로 고정하므로 `--platform linux/amd64 --pull=false` 재빌드 시 추가 pull 없이 동일 `linux/amd64` 레이어를 결정적으로 선택 |
| **선택된 `linux/amd64` 하위 OCI 이미지 Manifest** | `sha256:4a68d3169325c47e1bafbb86425220f03fbdff6f079a419e8d39407183288217` | `application/vnd.oci.image.manifest.v1+json` (`linux/amd64`, `3.12.16-slim-trixie`, base `debian:trixie-slim@sha256:ab3b3c8c5159eebfca4376430132d53c4d425f869c765ecbe47177a030da575d`) | `docker image inspect --platform linux/amd64 python:3.12-slim`으로 확인. 로컬 콘텐츠 크기 `43,272,757` bytes (`41.27 MiB`), `docker image ls --tree` 디스크 표시 `177MB`. 최종 빌드 이미지의 `RootFS.Layers` 상위 4개 레이어가 이 manifest의 4개 `linux/amd64` 레이어(`54ae9782...`, `dc8af65e...`, `e1beed03...`, `7283aa26...`)와 100% 일치 |
| *(참고)* `docker images` 표시 `382MB` | `python:3.12-slim` (`191083cdf6c7`) | 멀티아키텍처 로컬 캐시 합계 | `docker image ls --tree` 기준 로컬에 캐시된 `linux/amd64`(`177MB`) + `linux/arm64/v8`(`203MB`) 합계이며, 단일 `linux/amd64` 프로브 크기(`43,266,903` bytes)나 Registry 청구 지표와 다른 수치 |

---

## 3. 빌드 전 파일 무결성 대조 및 로컬 재현 명령

### 3.1 빌드 전 4개 실행·빌드 파일 SHA-256 대조 명령

향후 실행 에이전트는 빌드 전에 워크트리 루트에서 아래 명령으로 `probe.py`, `test_probe.py`, `Dockerfile`, `.dockerignore` 4개 실행·빌드 파일의 SHA-256이 고정 기준값과 일치하는지 대조합니다(`README.md`는 문서 갱신 대상이므로 실행 파일 해시 고정 목록에서 분리합니다).

```bash
cd "/Users/eddy81848/Projects/Career Memory/.worktrees/t007a-worker"
shasum -a 256 \
  spikes/t007a-worker/hosted-iam-probe/.dockerignore \
  spikes/t007a-worker/hosted-iam-probe/Dockerfile \
  spikes/t007a-worker/hosted-iam-probe/probe.py \
  spikes/t007a-worker/hosted-iam-probe/test_probe.py
```

고정 기준 SHA-256:
- `.dockerignore`: `8fbd1830fcc9ae28a48b6f1301dd3df8e9eb2a14b1a4724eae3901cf6b89a16e`
- `Dockerfile`: `c70ebb4aef18d782f7aede2a5371c968a448b5773aa920baee32383d3fdb79a1`
- `probe.py`: `8927017b95115f57a483e29f578015ca8fdb38397e69e7ed8c83c633147522b0`
- `test_probe.py`: `9b8696b5d6f867964c1834858ad938a15c716a652506648b7120cd79c6b2ed6c`

### 3.2 표준 라이브러리 로컬 검증 스위트 (DB · Docker · 외부 네트워크 불필요)

```bash
cd "/Users/eddy81848/Projects/Career Memory/.worktrees/t007a-worker/spikes/t007a-worker"
unset T007A_DATABASE_URL DATABASE_URL
python3 -B -m unittest discover -s hosted-iam-probe -p 'test_*.py' -v
```

실행 결과: **`Ran 6 tests in 1.523s` — `OK` (6 통과 / 0 실패 / 0 건너뜀)**

### 3.3 로컬 캐시 기반 `linux/amd64` 재빌드 · 기동 · 정리 명령

```bash
cd "/Users/eddy81848/Projects/Career Memory/.worktrees/t007a-worker/spikes/t007a-worker/hosted-iam-probe"

# 1) 로컬 캐시 기반 linux/amd64 재빌드 (추가 pull 없음, 레지스트리 업로드 금지)
docker build --platform linux/amd64 --pull=false --load -t t007f-iam-probe-ckpt:local .
docker image inspect t007f-iam-probe-ckpt:local \
  --format 'ID={{.Id}} OS={{.Os}}/{{.Architecture}} SIZE={{.Size}} USER={{.Config.User}}'

# 2) 127.0.0.1 한정 + 비기본 PORT(9092) 기동 (DB 주소·키·호스트 볼륨 미주입)
docker run -d --platform linux/amd64 --name t007f-iam-probe-ckpt-run \
  -p 127.0.0.1:19092:9092 -e PORT=9092 t007f-iam-probe-ckpt:local

# 3) HTTP · 비루트 · 런타임 아키텍처 확인
curl -i http://127.0.0.1:19092/healthz               # 200 OK + 고정 JSON
curl -i http://127.0.0.1:19092/internal/t007a/wake   # 404 Not Found
curl -i http://127.0.0.1:19092/other                 # 404 Not Found
curl -i -X POST http://127.0.0.1:19092/healthz       # 405 Method Not Allowed + Allow: GET
docker exec t007f-iam-probe-ckpt-run id
docker exec t007f-iam-probe-ckpt-run python -c 'import os, platform; print(os.getuid(), os.geteuid(), platform.machine())'

# 4) 정상 종료(SIGTERM -> exit 0) 및 임시 산출물만 정리 (기반 이미지 캐시 보존)
docker stop -t 5 t007f-iam-probe-ckpt-run
docker inspect t007f-iam-probe-ckpt-run --format 'STATUS={{.State.Status}} EXIT_CODE={{.State.ExitCode}}'
docker rm -fv t007f-iam-probe-ckpt-run
docker image rm -f t007f-iam-probe-ckpt:local
```

#### 실제 관측 수치 (`RUNTIME-IAM-PROBE-CHECKPOINT`)
- **최종 재빌드 이미지**:
  - Image ID (manifest list): `sha256:2b06628d1044a3b076e019d6fdf205b20560d607ee17d8ef49f6c527734a114a` (`2b06628d1044`)
  - Image Manifest / Config: `sha256:6106f7d19a0195de20f2895d3312979e5c405b14...` / `sha256:3154972e0c1557034f975bbf16dfd62a63f9b9f46e...` (`LOCAL-02`와 동일)
  - OS / 아키텍처: `linux/amd64` (컨테이너 런타임 `platform.machine()` = `x86_64`)
  - 로컬 이미지 크기: **`43,266,903` bytes** (`41.26 MiB` / `43.3 MB`, `docker image inspect --format '{{.Size}}'`)
  - *주의: 로컬 이미지 크기는 로컬 Docker 기준 측정값이며, Artifact Registry 전송량이나 저장·청구량 실측이 아닙니다.*
- **컨테이너 기동 및 정상 종료 확인**:
  - 바인딩 / 볼륨: `PORTS={"9092/tcp":[{"HostIp":"127.0.0.1","HostPort":"19092"}]}`, `MOUNTS=[]`
  - 비루트 실행: `uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)` (`uid=65534`, `euid=65534`, `Config.User=nobody`)
  - 비기본 `PORT=9092` HTTP 응답: `GET /healthz` `200`, `GET /internal/t007a/wake`·`/internal/t007a/recover`·`/other` `404`, `POST`/`PUT`/`DELETE /healthz` `405` + `Allow: GET`
  - `docker stop -t 5`: `STATUS=exited`, `EXIT_CODE=0`, `OOM=false`; 로그에 `probe_started` / `request` / `probe_stopped`만 출력(`9092`·타 경로 미노출)

---

## 4. 생성한 로컬 리소스 정리 결과

| 리소스 종류 | 이번 실행 작업 | 종료 후 상태 | 확인 방법 |
|---|---|---|---|
| 임시 프로브 컨테이너 (`t007f-iam-probe-ckpt-run`) | 생성·검증 후 삭제 | 잔여 `0`건 | `docker ps -a` → `0`건 |
| 임시 빌드 이미지 (`t007f-iam-probe-ckpt:local`) | 재빌드·검증 후 삭제 | 잔여 `0`건 | `docker images -q t007f-iam-probe-ckpt:local` → `0`건 |
| 기반 이미지 로컬 캐시 (`python:3.12-slim`) | 추가 pull 없이 기존 캐시 재사용 | **보존** (`sha256:191083cdf6c7...`, `191083cdf6c7`) | `docker images python:3.12-slim --digests` (기존 타 리소스 미변경, `docker system prune` 미실행) |
| 로컬 프로브 프로세스 / `__pycache__` | `unittest` 6건 실행 (`-B`) | 각 `0`건 | `pgrep -fl 'hosted-iam-probe/probe.py'` / `find ...` → 각 `0`건 |

---

## 5. 미검증 범위 (`NO-GO` · 현 게이트 유예 유지)
- **새 이미지 pull, 패키지 다운로드, 원격 Docker, `docker push`, `git push`/`merge`, GCP·Supabase 호출, Cloud Build, Cloud Run 배포, API/IAM 변경 모두 미실행** (`M0-CLOSE`에서는 `spike/t-007a-worker` 로컬 체크포인트 커밋과 문서상 유예 판정 기록만 수행).
- **Cloud Run 서비스 신원(Service Account), 앞단 IAM(`--no-allow-unauthenticated`, `roles/run.invoker`, OIDC), 네트워크 인그레스(`--ingress`), Artifact Registry 실제 전송량·저장 청구량·컨테이너 스캔 과금 여부, 당월 무료 잔여 한도, 호스팅 Supabase `:6543` 동작은 모두 미검증이며 호스팅은 계속 `NO-GO`(현 게이트 유예)입니다.**
