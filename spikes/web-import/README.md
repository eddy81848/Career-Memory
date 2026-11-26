# Career Memory — 연결 및 일회성 최초 가져오기 UI 프로토타입 (`spikes/web-import`)

이 폴더는 승인된 API 계약과 백엔드를 기다리지 않고, **Next.js 기반 일회성 최초 가져오기 사용자 여정**을 로컬 UI Fixture로 검증하기 위한 격리된 프론트엔드 프로토타입입니다.

## 범위 및 경계

- **포함**:
  - 모의 서비스 로그인 상태와 별도 GitHub 수집 연결 상태 분리 표시
  - 합성 공개 저장소 1곳 선택 및 일회성 최초 가져오기 요청
  - 요청 중(서버 미확정) · 실행 중 · 성공 · 부분 결과 · 한도 도달 · 오류 · 권한 상실 · 세션 만료 · 네트워크 실패 · 새로고침 복구 경로
  - `import_ready`와 파생 단계 준비 여부 분리 표시
  - 서버 `retryPolicy.allowed === true`일 때만 재시도 액션 노출
  - 중복 클릭 방지 및 키보드 전용 조작·텍스트 기반 상태/오류 전달
- **미포함 (의도적 제외)**:
  - 실제 로그인/OAuth/토큰/외부 GitHub API 호출
  - 임의 HTTP API 라우트(`/api/...`), 공용 스키마, 세션 쿠키, 검색/프로젝트 모델
  - 삭제, Claim 생성, LLM 연동, Event 편집

> **중요**: 모든 데이터와 테스트 fixture는 **HTTP 계약이 아닌 로컬 UI 뷰 모델 fixture**입니다. 이 프로토타입 테스트는 실제 인증·권한·Worker 동작을 검증하지 않습니다.

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

# 단위 및 UI 통합 테스트 (Vitest + Testing Library, 로컬 UI Fixture)
npm test

# 빌드 후 로컬 브라우저 E2E 테스트 (Playwright + 설치된 Chrome)
npm run build
npm run test:e2e
```

## 주요 화면 확인 방법

페이지 상단의 **로컬 UI Fixture 시나리오 제어판**에서 아래 시나리오를 선택하고 "최초 가져오기 요청"을 눌러 모든 경로를 확인할 수 있습니다.

1. `정상 성공 (import_ready + 파생 완료)`: 요청 중(미확정) → 실행 중(`import_ready=true`, 파생 진행 중) → 완료.
2. `스냅샷만 준비 (import_ready=true, 파생 미완료)`: `import_ready=true`와 파생 단계 실패/미완료 구분 배너 확인.
3. `부분 결과 (재시도 허용 / 거부)`: 성공 배지가 아닌 경고 배너, 누락 상세, `retryPolicy.allowed`에 따른 재시도 버튼 노출/숨김 확인.
4. `한도 도달 (Rate Limit / 초기 스냅샷 상한 초과)`: 한도 상세 및 재시도 허용/거부 분기 확인.
5. `오류 (재시도 허용 / 거부)`: 오류 상세 및 서버 정책 기반 재시도 버튼 노출/숨김 확인.
6. `권한 상실 (GitHub 수집 권한 회수)`: 서비스 로그인은 유지되면서 GitHub 연결만 `권한 상실`로 전이, 재시도 대신 재연결 버튼 노출 확인.
7. `세션 만료 / 네트워크 실패 (요청 시점)`: 작업 확정으로 표시하지 않고 미확정 안내 및 복구 버튼 노출 확인.
8. `장기 실행 (새로고침 복구 테스트용)`: 실행 중 버튼 중복 클릭 시 차단 안내 확인 후, 브라우저 실제 새로고침(`Cmd+R` / `F5`) 또는 `새로고침 복구 확인` 버튼으로 중복 요청 없는 상태 복구 확인.
9. **키보드 전용 조작**: `Tab`으로 이동, 저장소 라디오에서 `↑`/`↓`로 선택, `Enter`로 요청 실행 확인.

## 계약 연동 문서

승인된 API 계약 도착 시 필요한 필드·변환 규칙·질문 목록은 [docs/CONTRACT_ADAPTER_SPEC.md](./docs/CONTRACT_ADAPTER_SPEC.md)를 참고하세요.
