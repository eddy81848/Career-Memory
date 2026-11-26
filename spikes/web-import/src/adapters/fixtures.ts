import type {
  GitHubConnectionViewModel,
  PrototypeScenarioMeta,
  PublicRepositoryOption,
  ServiceAuthViewModel,
  SyntheticServiceAccount,
} from "@/domain/view-models";

/**
 * LOCAL UI FIXTURES ONLY — NOT an HTTP API contract or backend schema.
 * All accounts, handles, repository names, and timestamps are synthetic examples.
 */

export const SYNTHETIC_SERVICE_ACCOUNT: SyntheticServiceAccount = {
  syntheticUserId: "usr_demo_seoyeon_01",
  displayName: "김서연 (합성 예시)",
  email: "demo.seoyeon@career-memory.invalid",
};

export const SYNTHETIC_AUTHENTICATED_AUTH: ServiceAuthViewModel = {
  status: "authenticated",
  account: SYNTHETIC_SERVICE_ACCOUNT,
  sessionNote:
    "모의 서비스 로그인 활성 상태 (실제 인증/세션 저장 방식은 아직 미확정)",
  lastCheckedAt: "2026-11-26T12:00:00+09:00",
};

export const SYNTHETIC_SIGNED_OUT_AUTH: ServiceAuthViewModel = {
  status: "signed_out",
  account: null,
  sessionNote: "모의 서비스 로그아웃 상태입니다. 로그인 후 진행할 수 있습니다.",
  lastCheckedAt: "2026-11-26T12:00:00+09:00",
};

export const SYNTHETIC_EXPIRED_AUTH: ServiceAuthViewModel = {
  status: "session_expired",
  account: SYNTHETIC_SERVICE_ACCOUNT,
  sessionNote:
    "모의 서비스 세션이 만료되었습니다. GitHub 수집 연결과 별개로 서비스 재로그인이 필요합니다.",
  lastCheckedAt: "2026-11-26T12:05:00+09:00",
};

export const SYNTHETIC_CONNECTED_GITHUB: GitHubConnectionViewModel = {
  status: "connected",
  accountHandle: "@seoyeon-lab-demo",
  connectionRef: "gh_collect_demo_902",
  scopeSummary: "공개 저장소 읽기 전용 스냅샷 수집 (합성 예시)",
  revocationReason: null,
  connectedAt: "2026-11-26T11:58:00+09:00",
};

export const SYNTHETIC_DISCONNECTED_GITHUB: GitHubConnectionViewModel = {
  status: "disconnected",
  accountHandle: null,
  connectionRef: null,
  scopeSummary: "공개 저장소 읽기 전용 스냅샷 수집 (미연결)",
  revocationReason: null,
  connectedAt: null,
};

export const SYNTHETIC_REVOKED_GITHUB: GitHubConnectionViewModel = {
  status: "permission_revoked",
  accountHandle: "@seoyeon-lab-demo",
  connectionRef: "gh_collect_demo_902",
  scopeSummary: "공개 저장소 읽기 전용 스냅샷 수집 (권한 상실)",
  revocationReason:
    "GitHub 측에서 수집 연결 권한이 회수되었거나 만료되었습니다. 다시 연결해야 가져오기를 진행할 수 있습니다.",
  connectedAt: "2026-11-26T11:58:00+09:00",
};

export const SYNTHETIC_PUBLIC_REPOSITORIES: PublicRepositoryOption[] = [
  {
    repoId: "repo_demo_ledger_01",
    fullName: "seoyeon-lab-demo/ledger-engine",
    visibility: "public",
    defaultBranch: "main",
    primaryLanguage: "TypeScript",
    description:
      "합성 예제 공개 저장소: 정산 원장 엔진 및 이벤트 처리 파이프라인",
    lastPushedAt: "2026-11-18",
    estimatedCommits: 142,
    estimatedPullRequests: 28,
  },
  {
    repoId: "repo_demo_tokens_02",
    fullName: "seoyeon-lab-demo/design-tokens-kit",
    visibility: "public",
    defaultBranch: "main",
    primaryLanguage: "TypeScript",
    description:
      "합성 예제 공개 저장소: 접근성 중심 디자인 토큰 및 컴포넌트 사양",
    lastPushedAt: "2026-11-12",
    estimatedCommits: 64,
    estimatedPullRequests: 11,
  },
  {
    repoId: "repo_demo_telemetry_03",
    fullName: "seoyeon-lab-demo/telemetry-cli",
    visibility: "public",
    defaultBranch: "main",
    primaryLanguage: "Go",
    description:
      "합성 예제 공개 저장소: CLI 계측 수집기 (이력이 커서 한도 도달 시뮬레이션에 적합)",
    lastPushedAt: "2026-10-29",
    estimatedCommits: 490,
    estimatedPullRequests: 76,
    advisoryNote: "초기 스냅샷 한도(MVP 캡) 초과 시나리오 확인용 합성 저장소",
  },
];

export const PROTOTYPE_SCENARIOS: PrototypeScenarioMeta[] = [
  {
    id: "success_full",
    label: "정상 성공 (import_ready + 파생 완료)",
    shortDescription:
      "요청 중 → 실행 중(원본 완료/파생 진행 중) → 전체 준비 완료",
  },
  {
    id: "success_import_ready_derived_pending",
    label: "스냅샷만 준비 (import_ready=true, 파생 미완료)",
    shortDescription:
      "import_ready는 준비되었지만 파생 단계 중 일부가 미완료/실패인 상태 구분",
  },
  {
    id: "partial_retryable",
    label: "부분 결과 (재시도 허용)",
    shortDescription:
      "일부 커밋/PR 누락 — 성공으로 표시하지 않음, 서버 retryPolicy.allowed=true",
  },
  {
    id: "partial_non_retryable",
    label: "부분 결과 (재시도 거부)",
    shortDescription:
      "일부 항목 누락 — 서버가 재시도를 허용하지 않아 재시도 버튼 미노출",
  },
  {
    id: "limit_rate_limited",
    label: "한도 도달 — Rate Limit (재시도 허용)",
    shortDescription:
      "GitHub 수집 한도 도달 — 성공으로 표시하지 않음, 재시도 허용 상태",
  },
  {
    id: "limit_cap_blocked",
    label: "한도 도달 — 초기 스냅샷 상한 초과 (재시도 거부)",
    shortDescription:
      "MVP 일회성 수집 상한 도달 — 서버 정책상 재시도 버튼 비노출",
  },
  {
    id: "error_retryable",
    label: "오류 (재시도 허용)",
    shortDescription: "일시적 워커 오류 — 서버가 재시도 허용, 재시도 시 복구",
  },
  {
    id: "error_non_retryable",
    label: "오류 (재시도 거부)",
    shortDescription:
      "영구적 검증 오류 — retryPolicy.allowed=false로 재시도 버튼 숨김",
  },
  {
    id: "permission_revoked",
    label: "권한 상실 (GitHub 수집 권한 회수)",
    shortDescription:
      "실행 중 GitHub 수집 권한 회수 감지 — 재시도 대신 재연결 안내",
  },
  {
    id: "session_expired_on_submit",
    label: "세션 만료 (요청 시점)",
    shortDescription:
      "서비스 로그인 세션 만료 — 요청 미확정 안내 및 서비스 재로그인 유도",
  },
  {
    id: "network_error_on_submit",
    label: "네트워크 실패 (요청 시점)",
    shortDescription:
      "서버 응답 미수신 — 작업 확정처럼 표시하지 않고 상태 확인 안내",
  },
  {
    id: "running_slow_for_reload",
    label: "장기 실행 (새로고침 복구 테스트용)",
    shortDescription:
      "실행 중 상태가 유지되어 페이지 새로고침 후 중복 요청 없이 복구 확인",
  },
];
