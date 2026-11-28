import type {
  ConfirmWorkEventInput,
  DissolveWorkEventInput,
  EditWorkEventInput,
  EventReviewAdapter,
  EventReviewHarnessControls,
  EventReviewResult,
  ListWorkEventsQuery,
  MergeWorkEventsInput,
  RejectWorkEventInput,
  SplitWorkEventInput,
} from "@/adapters/event-review-adapter";
import type {
  AllowedEventAction,
  CompositeMemberSnapshot,
  DissolveWorkEventResponse,
  EventInboxAvailabilityState,
  EventPolicyState,
  EventReviewScenarioId,
  MergeWorkEventsResponse,
  SplitWorkEventResponse,
  WorkEventDetailResponse,
  WorkEventListResponse,
  WorkEventSummaryItem,
} from "@/domain/event-review-models";
import {
  computeActiveTopLevelCount,
  isEventMutationBlockedByPolicyOrDeletion,
  validateMergeSelection,
  validateSplitPartitions,
} from "@/domain/event-review-presenter";

/**
 * Local prototype fixture adapter for M3 Event Review & Curation Inbox.
 *
 * Implements the exact same `EventReviewAdapter` contract as
 * `FetchEventReviewHttpTransport`, enabling zero-backend interactive UI
 * exploration, deterministic unit testing, and Playwright E2E testing.
 */

export interface MockEventReviewAdapterOptions {
  latencyMs?: number;
  initialScenario?: EventReviewScenarioId;
  sourceResourceId?: string;
}

const WORKSPACE_ID = "ws_demo_seoyeon_01";
const DEFAULT_REPO_ID = "repo_demo_ledger_01";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function toSummary(detail: WorkEventDetailResponse): WorkEventSummaryItem {
  return {
    event_id: detail.event_id,
    workspace_id: detail.workspace_id,
    source_resource_id: detail.source_resource_id,
    anchor_mode: detail.anchor_mode,
    status: detail.status,
    version: detail.version,
    etag: detail.etag,
    is_active_top_level: detail.is_active_top_level,
    title: detail.title,
    problem: detail.problem,
    action: detail.action,
    outcome: detail.outcome,
    created_by: detail.created_by,
    occurred_start_at: detail.occurred_start_at,
    occurred_end_at: detail.occurred_end_at,
    reviewed_at: detail.reviewed_at,
    updated_at: detail.updated_at,
    review_flags: [...detail.review_flags],
    allowed_actions: [...detail.allowed_actions],
    evidence_count: detail.evidence_memberships.length,
    context_count: detail.context_relations.length,
    direct_commit_badge: detail.direct_commit_badge
      ? clone(detail.direct_commit_badge)
      : null,
    composite_summary: detail.composite_summary
      ? clone(detail.composite_summary)
      : null,
    policy_state: clone(detail.policy_state),
  };
}

function buildDefaultEvents(
  sourceResourceId: string,
  policyState: EventPolicyState,
  regenerationStatus: "COMPLETE" | "FAILED" = "COMPLETE"
): WorkEventDetailResponse[] {
  const blocked =
    policyState.status === "POLICY_BLOCKED" ||
    policyState.status === "DELETION_PENDING";

  const candidateActions: AllowedEventAction[] = blocked
    ? []
    : ["CONFIRM", "EDIT", "REJECT", "SPLIT", "MERGE"];
  const directCommitActions: AllowedEventAction[] = blocked
    ? []
    : ["CONFIRM", "EDIT", "REJECT", "MERGE"];
  const compositeActions: AllowedEventAction[] = blocked
    ? []
    : ["CONFIRM", "EDIT", "DISSOLVE"];

  const pr104: WorkEventDetailResponse = {
    event_id: "evt_pr_104",
    workspace_id: WORKSPACE_ID,
    source_resource_id: sourceResourceId,
    anchor_mode: "PR",
    status: "CANDIDATE",
    version: 1,
    etag: '"v1"',
    is_active_top_level: true,
    title: "PR #104: 정산 원장 멱등성 키 검증 강화 및 배치 타임아웃 리팩터링",
    problem:
      "중복 결제 웹훅 재전송 시 원장 레코드가 이중 적재되고, 대용량 정산 배치에서 커넥션 타임아웃이 간헐적으로 발생함",
    action:
      "ledger/idempotency.ts에 결정론적 멱등성 해시 락을 추가하고, worker/batch-runner.ts의 청크 단위 커밋 파이프라인을 분리 구현함",
    outcome:
      "중복 원장 삽입 0건 달성 및 정산 배치 99p 실행 시간 42% 단축 (PR 리뷰 2건 통과 후 main 병합)",
    created_by: "RULE",
    occurred_start_at: "2026-09-10T02:15:00Z",
    occurred_end_at: "2026-09-12T08:40:00Z",
    reviewed_at: null,
    updated_at: "2026-09-12T08:45:00Z",
    review_flags: ["MIXED_PURPOSE_SIGNALS", "MULTIPLE_LINKED_ISSUES"],
    allowed_actions: candidateActions,
    evidence_count: 4,
    context_count: 2,
    direct_commit_badge: null,
    composite_summary: null,
    policy_state: clone(policyState),
    primary_anchor_source_object_id: "so_pr_104",
    anchor_scope: {
      kind: "PR",
      file_paths: [
        "src/ledger/idempotency.ts",
        "src/ledger/idempotency.test.ts",
        "src/worker/batch-runner.ts",
        "src/worker/connection-pool.ts",
      ],
    },
    evidence_memberships: [
      {
        membership_id: "mem_104_anchor",
        source_object_id: "so_pr_104",
        object_type: "PULL_REQUEST",
        provider_object_id: "PR#104",
        relation_class: "MUST_LINK",
        relation_type: "PRIMARY_ANCHOR",
        origin: "SOURCE",
        title: "Pull Request #104 (merged -> main)",
        subtitle: "base: main · 2 commits · 2 reviews",
        file_paths: ["src/ledger/idempotency.ts", "src/worker/batch-runner.ts"],
        occurred_at: "2026-09-12T08:40:00Z",
      },
      {
        membership_id: "mem_104_c1",
        source_object_id: "so_commit_104_a",
        object_type: "COMMIT",
        provider_object_id: "7c91e4a",
        relation_class: "MUST_LINK",
        relation_type: "PR_COMMIT",
        origin: "SOURCE",
        title: "feat(ledger): enforce deterministic idempotency hash lock",
        subtitle: "src/ledger/idempotency.ts, src/ledger/idempotency.test.ts",
        file_paths: ["src/ledger/idempotency.ts", "src/ledger/idempotency.test.ts"],
        hunk_range: "L14-L128",
        content_unit_id: "cu_104_ledger",
        occurred_at: "2026-09-10T05:10:00Z",
      },
      {
        membership_id: "mem_104_c2",
        source_object_id: "so_commit_104_b",
        object_type: "COMMIT",
        provider_object_id: "b32d81f",
        relation_class: "MUST_LINK",
        relation_type: "PR_COMMIT",
        origin: "SOURCE",
        title: "refactor(worker): split chunked settlement batch runner",
        subtitle: "src/worker/batch-runner.ts, src/worker/connection-pool.ts",
        file_paths: ["src/worker/batch-runner.ts", "src/worker/connection-pool.ts"],
        hunk_range: "L40-L210",
        content_unit_id: "cu_104_worker",
        occurred_at: "2026-09-11T11:20:00Z",
      },
      {
        membership_id: "mem_104_rev",
        source_object_id: "so_review_104_1",
        object_type: "REVIEW",
        provider_object_id: "PRR#104-1",
        relation_class: "MUST_LINK",
        relation_type: "PR_REVIEW",
        origin: "SOURCE",
        title: "Review 승인 및 락 격리 수준 코멘트 반영",
        subtitle: "APPROVED · inline review discussion",
        occurred_at: "2026-09-12T07:55:00Z",
      },
    ],
    context_relations: [
      {
        membership_id: "ctx_104_issue_41",
        source_object_id: "so_issue_41",
        object_type: "ISSUE",
        provider_object_id: "ISSUE#41",
        relation_class: "CONTEXT_ONLY",
        relation_type: "ISSUE_REFERENCE",
        origin: "SOURCE",
        title: "Issue #41: 중복 웹훅 정산 이중 기입 방지",
        rationale: "PR 본문에서 참조된 Issue (참고 문맥 전용 · 소속 근거 아님)",
      },
      {
        membership_id: "ctx_104_issue_48",
        source_object_id: "so_issue_48",
        object_type: "ISSUE",
        provider_object_id: "ISSUE#48",
        relation_class: "CONTEXT_ONLY",
        relation_type: "ISSUE_REFERENCE",
        origin: "RULE",
        title: "Issue #48: 야간 정산 배치 커넥션 풀 타임아웃 조사",
        rationale: "공유 티켓 참조 단서 (자동 병합 금지 · CONTEXT_ONLY)",
      },
    ],
    cannot_link_constraints: {
      source_object_ids: [],
      peer_event_ids: [],
      constraints: [],
    },
    composite_details: null,
    downstream_impact: {
      fact_count: 3,
      claim_count: 2,
      export_count: 1,
      stale_fact_count: 0,
      stale_claim_count: 0,
      processing_status: blocked ? "BLOCKED_POLICY" : "COMPLETE",
    },
    version_history: [
      {
        version: 1,
        status: "CANDIDATE",
        action: "CREATE_CANDIDATE",
        actor: "RULE",
        reason: "Merged PR anchored on default branch main",
        created_at: "2026-09-12T08:45:00Z",
      },
    ],
    review_actions: [],
  };

  const dc209: WorkEventDetailResponse = {
    event_id: "evt_dc_209",
    workspace_id: WORKSPACE_ID,
    source_resource_id: sourceResourceId,
    anchor_mode: "DIRECT_COMMIT",
    status: "CANDIDATE",
    version: 1,
    etag: '"v1"',
    is_active_top_level: true,
    title: "hotfix(ledger): 환율 반올림 경계값 검증 테스트 보강",
    problem:
      "정산 배포 직후 특정 통화 소수점 반올림 회귀를 막기 위한 긴급 테스트 보강이 필요함",
    action:
      "main 기본 브랜치에 직접 커밋(a8f3c1e)으로 경계값 단위 테스트와 스냅샷 검증을 반영함",
    outcome:
      "CI 테스트 스위트 통과 및 반올림 경계 오차 재발 방지",
    created_by: "RULE",
    occurred_start_at: "2026-09-13T04:10:00Z",
    occurred_end_at: "2026-09-13T04:10:00Z",
    reviewed_at: null,
    updated_at: "2026-09-13T04:15:00Z",
    review_flags: ["DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED"],
    allowed_actions: directCommitActions,
    evidence_count: 1,
    context_count: 1,
    direct_commit_badge: {
      badge_code: "DIRECT_DEFAULT_BRANCH_NO_PR",
      label: "PR 없음 · default branch 직접 반영",
      default_branch: "main",
      abbreviated_sha: "a8f3c1e",
      committed_at: "2026-09-13T04:10:00Z",
      pr_association_state: "NO_MATCH_OBSERVED",
      no_match_observed_means_definitely_no_pr: false,
      requires_user_confirmation: true,
    },
    composite_summary: null,
    policy_state: clone(policyState),
    primary_anchor_source_object_id: "so_commit_dc209",
    anchor_scope: {
      kind: "DIRECT_COMMIT",
      file_paths: ["src/ledger/rounding.test.ts"],
    },
    evidence_memberships: [
      {
        membership_id: "mem_dc209_anchor",
        source_object_id: "so_commit_dc209",
        object_type: "COMMIT",
        provider_object_id: "a8f3c1e",
        relation_class: "MUST_LINK",
        relation_type: "PRIMARY_ANCHOR",
        origin: "SOURCE",
        title: "Direct commit a8f3c1e on main",
        subtitle: "default_branch=main · PR 연결 미관측(NO_MATCH_OBSERVED)",
        file_paths: ["src/ledger/rounding.test.ts"],
        occurred_at: "2026-09-13T04:10:00Z",
      },
    ],
    context_relations: [
      {
        membership_id: "ctx_dc209_issue_41",
        source_object_id: "so_issue_41",
        object_type: "ISSUE",
        provider_object_id: "ISSUE#41",
        relation_class: "CONTEXT_ONLY",
        relation_type: "ISSUE_REFERENCE",
        origin: "RULE",
        title: "Issue #41: 중복 웹훅 정산 이중 기입 방지",
        rationale: "커밋 메시지 참조 단서 (참고 문맥 전용 · 자동 병합 안 함)",
      },
    ],
    cannot_link_constraints: {
      source_object_ids: [],
      peer_event_ids: [],
      constraints: [],
    },
    composite_details: null,
    downstream_impact: {
      fact_count: 1,
      claim_count: 1,
      export_count: 0,
      stale_fact_count: 0,
      stale_claim_count: 0,
      processing_status: blocked ? "BLOCKED_POLICY" : "COMPLETE",
    },
    version_history: [
      {
        version: 1,
        status: "CANDIDATE",
        action: "CREATE_CANDIDATE",
        actor: "RULE",
        reason: "Direct default-branch commit (NO_MATCH_OBSERVED)",
        created_at: "2026-09-13T04:15:00Z",
      },
    ],
    review_actions: [],
  };

  const dc215: WorkEventDetailResponse = {
    event_id: "evt_dc_215",
    workspace_id: WORKSPACE_ID,
    source_resource_id: sourceResourceId,
    anchor_mode: "DIRECT_COMMIT",
    status: "CANDIDATE",
    version: 1,
    etag: '"v1"',
    is_active_top_level: true,
    title: "fix misc",
    problem: null,
    action: "trunk 기본 브랜치 직접 커밋(9c4e2b0) 반영",
    outcome: null,
    created_by: "RULE",
    occurred_start_at: "2026-09-15T10:00:00Z",
    occurred_end_at: "2026-09-15T10:00:00Z",
    reviewed_at: null,
    updated_at: "2026-09-15T10:05:00Z",
    review_flags: [
      "AMBIGUOUS_AUTHOR_IDENTITY",
      "DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED",
      "GENERIC_TITLE_OR_SUMMARY",
      "PARTIAL_SOURCE_SKIPPED",
      "REVIEW_REQUIRED_MULTI_PARENT",
    ],
    allowed_actions: directCommitActions,
    evidence_count: 1,
    context_count: 0,
    direct_commit_badge: {
      badge_code: "DIRECT_DEFAULT_BRANCH_NO_PR",
      label: "PR 없음 · default branch 직접 반영",
      default_branch: "trunk",
      abbreviated_sha: "9c4e2b0",
      committed_at: "2026-09-15T10:00:00Z",
      pr_association_state: "NO_MATCH_OBSERVED",
      no_match_observed_means_definitely_no_pr: false,
      requires_user_confirmation: true,
    },
    composite_summary: null,
    policy_state: clone(policyState),
    primary_anchor_source_object_id: "so_commit_dc215",
    anchor_scope: {
      kind: "DIRECT_COMMIT",
      file_paths: ["scripts/release-notes.sh"],
    },
    evidence_memberships: [
      {
        membership_id: "mem_dc215_anchor",
        source_object_id: "so_commit_dc215",
        object_type: "COMMIT",
        provider_object_id: "9c4e2b0",
        relation_class: "MUST_LINK",
        relation_type: "PRIMARY_ANCHOR",
        origin: "SOURCE",
        title: "Direct commit 9c4e2b0 on trunk (multi-parent)",
        subtitle: "default_branch=trunk · PR 연결 미관측(NO_MATCH_OBSERVED)",
        file_paths: ["scripts/release-notes.sh"],
        occurred_at: "2026-09-15T10:00:00Z",
      },
    ],
    context_relations: [],
    cannot_link_constraints: {
      source_object_ids: ["so_pr_112"],
      peer_event_ids: ["evt_pr_112"],
      constraints: [
        {
          constraint_id: "cl_215_112",
          target_type: "PEER_EVENT",
          target_id: "evt_pr_112",
          target_label: "PR #112: 감사 로그 마스킹 정책 적용",
          reason: "사용자가 이전 검토에서 서로 무관한 작업으로 분리 제약(CANNOT_LINK) 설정",
          created_at: "2026-09-16T01:00:00Z",
        },
      ],
    },
    composite_details: null,
    downstream_impact: {
      fact_count: 0,
      claim_count: 0,
      export_count: 0,
      stale_fact_count: 0,
      stale_claim_count: 0,
      processing_status: blocked ? "BLOCKED_POLICY" : "NOT_STARTED",
    },
    version_history: [
      {
        version: 1,
        status: "CANDIDATE",
        action: "CREATE_CANDIDATE",
        actor: "RULE",
        reason: "Direct commit on provider default branch trunk",
        created_at: "2026-09-15T10:05:00Z",
      },
    ],
    review_actions: [],
  };

  const pr112: WorkEventDetailResponse = {
    event_id: "evt_pr_112",
    workspace_id: WORKSPACE_ID,
    source_resource_id: sourceResourceId,
    anchor_mode: "PR",
    status: "CANDIDATE",
    version: 1,
    etag: '"v1"',
    is_active_top_level: true,
    title: "PR #112: 감사 로그 마스킹 정책 적용",
    problem: "외부 전달 로그에 민감 헤더가 노출될 위험이 있었음",
    action: "audit/redactor.ts에 allowlist 기반 마스킹 필터를 구현함",
    outcome: "감사 로그 보안 점검 통과",
    created_by: "RULE",
    occurred_start_at: "2026-09-15T14:00:00Z",
    occurred_end_at: "2026-09-16T03:30:00Z",
    reviewed_at: null,
    updated_at: "2026-09-16T03:35:00Z",
    review_flags: [],
    allowed_actions: candidateActions,
    evidence_count: 2,
    context_count: 0,
    direct_commit_badge: null,
    composite_summary: null,
    policy_state: clone(policyState),
    primary_anchor_source_object_id: "so_pr_112",
    anchor_scope: {
      kind: "PR",
      file_paths: ["src/audit/redactor.ts", "src/audit/redactor.test.ts"],
    },
    evidence_memberships: [
      {
        membership_id: "mem_112_anchor",
        source_object_id: "so_pr_112",
        object_type: "PULL_REQUEST",
        provider_object_id: "PR#112",
        relation_class: "MUST_LINK",
        relation_type: "PRIMARY_ANCHOR",
        origin: "SOURCE",
        title: "Pull Request #112 (merged -> main)",
        file_paths: ["src/audit/redactor.ts"],
        occurred_at: "2026-09-16T03:30:00Z",
      },
      {
        membership_id: "mem_112_c1",
        source_object_id: "so_commit_112_a",
        object_type: "COMMIT",
        provider_object_id: "f410d2a",
        relation_class: "MUST_LINK",
        relation_type: "PR_COMMIT",
        origin: "SOURCE",
        title: "feat(audit): apply strict allowlist header redactor",
        file_paths: ["src/audit/redactor.ts", "src/audit/redactor.test.ts"],
        occurred_at: "2026-09-16T02:10:00Z",
      },
    ],
    context_relations: [],
    cannot_link_constraints: {
      source_object_ids: ["so_commit_dc215"],
      peer_event_ids: ["evt_dc_215"],
      constraints: [
        {
          constraint_id: "cl_112_215",
          target_type: "PEER_EVENT",
          target_id: "evt_dc_215",
          target_label: "fix misc (9c4e2b0)",
          reason: "사용자가 이전 검토에서 서로 무관한 작업으로 분리 제약(CANNOT_LINK) 설정",
          created_at: "2026-09-16T01:00:00Z",
        },
      ],
    },
    composite_details: null,
    downstream_impact: {
      fact_count: 2,
      claim_count: 1,
      export_count: 0,
      stale_fact_count: 0,
      stale_claim_count: 0,
      processing_status: blocked ? "BLOCKED_POLICY" : "COMPLETE",
    },
    version_history: [
      {
        version: 1,
        status: "CANDIDATE",
        action: "CREATE_CANDIDATE",
        actor: "RULE",
        reason: "Merged PR anchored on main",
        created_at: "2026-09-16T03:35:00Z",
      },
    ],
    review_actions: [],
  };

  // Pre-existing USER_COMPOSITE with 2 SUPERSEDED members (collapsed from default list!)
  const memberPr088: WorkEventDetailResponse = {
    event_id: "evt_pr_088",
    workspace_id: WORKSPACE_ID,
    source_resource_id: sourceResourceId,
    anchor_mode: "PR",
    status: "SUPERSEDED",
    version: 2,
    etag: '"v2"',
    is_active_top_level: false,
    title: "PR #88: 결제 Webhook 서명 검증 스키마 추가",
    problem: "Webhook 서명 검증 규칙이 분산되어 있었음",
    action: "HMAC 서명 검증 미들웨어를 통합 구현함",
    outcome: "Webhook 위변조 차단",
    created_by: "RULE",
    occurred_start_at: "2026-08-20T01:00:00Z",
    occurred_end_at: "2026-08-21T06:00:00Z",
    reviewed_at: "2026-08-22T09:00:00Z",
    updated_at: "2026-08-22T09:00:00Z",
    review_flags: [],
    allowed_actions: [],
    evidence_count: 1,
    context_count: 0,
    direct_commit_badge: null,
    composite_summary: null,
    policy_state: clone(policyState),
    primary_anchor_source_object_id: "so_pr_088",
    anchor_scope: { kind: "PR", file_paths: ["src/webhook/signature.ts"] },
    evidence_memberships: [
      {
        membership_id: "mem_088_anchor",
        source_object_id: "so_pr_088",
        object_type: "PULL_REQUEST",
        provider_object_id: "PR#88",
        relation_class: "MUST_LINK",
        relation_type: "PRIMARY_ANCHOR",
        origin: "SOURCE",
        title: "Pull Request #88 (merged -> main)",
        file_paths: ["src/webhook/signature.ts"],
        occurred_at: "2026-08-21T06:00:00Z",
      },
    ],
    context_relations: [],
    cannot_link_constraints: {
      source_object_ids: [],
      peer_event_ids: [],
      constraints: [],
    },
    composite_details: {
      member_count: 0,
      merge_reason: "결제 Webhook 서명 및 타임스탬프 방어 작업 통합",
      regeneration_status: regenerationStatus,
      superseded_by_composite_id: "evt_comp_301",
      members: [],
    },
    downstream_impact: {
      fact_count: 0,
      claim_count: 0,
      export_count: 0,
      stale_fact_count: 1,
      stale_claim_count: 1,
      processing_status: "COMPLETE",
    },
    version_history: [
      {
        version: 1,
        status: "CANDIDATE",
        action: "CREATE_CANDIDATE",
        actor: "RULE",
        reason: "Merged PR #88",
        created_at: "2026-08-21T06:05:00Z",
      },
      {
        version: 2,
        status: "SUPERSEDED",
        action: "MERGE",
        actor: "USER",
        reason: "Superseded by evt_comp_301",
        created_at: "2026-08-22T09:00:00Z",
      },
    ],
    review_actions: [],
  };

  const memberDc091: WorkEventDetailResponse = {
    event_id: "evt_dc_091",
    workspace_id: WORKSPACE_ID,
    source_resource_id: sourceResourceId,
    anchor_mode: "DIRECT_COMMIT",
    status: "SUPERSEDED",
    version: 2,
    etag: '"v2"',
    is_active_top_level: false,
    title: "chore(webhook): 재생 공격 방지 타임스탬프 허용 오차 5분 설정",
    problem: "서명 검증 직후 리플레이 방어 허용 창 설정 필요",
    action: "main에 직접 커밋(d501ab2)으로 replay window 설정 추가",
    outcome: "리플레이 테스트 통과",
    created_by: "RULE",
    occurred_start_at: "2026-08-21T08:00:00Z",
    occurred_end_at: "2026-08-21T08:00:00Z",
    reviewed_at: "2026-08-22T09:00:00Z",
    updated_at: "2026-08-22T09:00:00Z",
    review_flags: ["DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED"],
    allowed_actions: [],
    evidence_count: 1,
    context_count: 0,
    direct_commit_badge: {
      badge_code: "DIRECT_DEFAULT_BRANCH_NO_PR",
      label: "PR 없음 · default branch 직접 반영",
      default_branch: "main",
      abbreviated_sha: "d501ab2",
      committed_at: "2026-08-21T08:00:00Z",
      pr_association_state: "NO_MATCH_OBSERVED",
      no_match_observed_means_definitely_no_pr: false,
      requires_user_confirmation: true,
    },
    composite_summary: null,
    policy_state: clone(policyState),
    primary_anchor_source_object_id: "so_commit_dc091",
    anchor_scope: { kind: "DIRECT_COMMIT", file_paths: ["src/webhook/replay-window.ts"] },
    evidence_memberships: [
      {
        membership_id: "mem_091_anchor",
        source_object_id: "so_commit_dc091",
        object_type: "COMMIT",
        provider_object_id: "d501ab2",
        relation_class: "MUST_LINK",
        relation_type: "PRIMARY_ANCHOR",
        origin: "SOURCE",
        title: "Direct commit d501ab2 on main",
        file_paths: ["src/webhook/replay-window.ts"],
        occurred_at: "2026-08-21T08:00:00Z",
      },
    ],
    context_relations: [],
    cannot_link_constraints: {
      source_object_ids: [],
      peer_event_ids: [],
      constraints: [],
    },
    composite_details: {
      member_count: 0,
      merge_reason: "결제 Webhook 서명 및 타임스탬프 방어 작업 통합",
      regeneration_status: regenerationStatus,
      superseded_by_composite_id: "evt_comp_301",
      members: [],
    },
    downstream_impact: {
      fact_count: 0,
      claim_count: 0,
      export_count: 0,
      stale_fact_count: 1,
      stale_claim_count: 0,
      processing_status: "COMPLETE",
    },
    version_history: [
      {
        version: 1,
        status: "CANDIDATE",
        action: "CREATE_CANDIDATE",
        actor: "RULE",
        reason: "Direct commit d501ab2",
        created_at: "2026-08-21T08:05:00Z",
      },
      {
        version: 2,
        status: "SUPERSEDED",
        action: "MERGE",
        actor: "USER",
        reason: "Superseded by evt_comp_301",
        created_at: "2026-08-22T09:00:00Z",
      },
    ],
    review_actions: [],
  };

  const composite301: WorkEventDetailResponse = {
    event_id: "evt_comp_301",
    workspace_id: WORKSPACE_ID,
    source_resource_id: sourceResourceId,
    anchor_mode: "USER_COMPOSITE",
    status: "CONFIRMED",
    version: 2,
    etag: '"v2"',
    is_active_top_level: true,
    title: "결제 Webhook 서명 검증 및 리플레이 방어 체계 구축",
    problem: "결제 Webhook의 서명 검증과 리플레이 방지 정책이 분리되어 보안 경계가 불완전함",
    action: "PR #88 서명 검증 미들웨어와 후속 직접 커밋(d501ab2) 리플레이 허용 오차 구성을 하나의 논리 작업으로 통합함",
    outcome: "결제 Webhook 위변조·재전송 공격 방어 체계 확립",
    created_by: "USER",
    occurred_start_at: "2026-08-20T01:00:00Z",
    occurred_end_at: "2026-08-21T08:00:00Z",
    reviewed_at: "2026-08-22T09:05:00Z",
    updated_at: "2026-08-22T09:05:00Z",
    review_flags: [],
    allowed_actions: compositeActions,
    evidence_count: 2,
    context_count: 0,
    direct_commit_badge: null,
    composite_summary: {
      member_count: 2,
      badge_label: "2개 Event를 사용자가 병합",
      merge_reason: "결제 Webhook 서명 및 타임스탬프 방어 작업 통합",
      regeneration_status: regenerationStatus,
    },
    policy_state: clone(policyState),
    primary_anchor_source_object_id: null,
    anchor_scope: {
      kind: "USER_COMPOSITE",
      file_paths: ["src/webhook/signature.ts", "src/webhook/replay-window.ts"],
    },
    evidence_memberships: [
      ...memberPr088.evidence_memberships,
      ...memberDc091.evidence_memberships,
    ],
    context_relations: [],
    cannot_link_constraints: {
      source_object_ids: [],
      peer_event_ids: [],
      constraints: [],
    },
    composite_details: {
      member_count: 2,
      merge_reason: "결제 Webhook 서명 및 타임스탬프 방어 작업 통합",
      regeneration_status: regenerationStatus,
      superseded_by_composite_id: null,
      members: [
        {
          member_event_id: "evt_pr_088",
          member_version: 1,
          anchor_mode: "PR",
          title: memberPr088.title,
          status_at_merge: "CANDIDATE",
          current_status: "SUPERSEDED",
          evidence_count: 1,
          occurred_start_at: memberPr088.occurred_start_at,
          occurred_end_at: memberPr088.occurred_end_at,
        },
        {
          member_event_id: "evt_dc_091",
          member_version: 1,
          anchor_mode: "DIRECT_COMMIT",
          title: memberDc091.title,
          status_at_merge: "CANDIDATE",
          current_status: "SUPERSEDED",
          evidence_count: 1,
          occurred_start_at: memberDc091.occurred_start_at,
          occurred_end_at: memberDc091.occurred_end_at,
          direct_commit_badge: clone(memberDc091.direct_commit_badge),
        },
      ],
    },
    downstream_impact: {
      fact_count: 2,
      claim_count: 1,
      export_count: 1,
      stale_fact_count: 0,
      stale_claim_count: 0,
      processing_status: blocked
        ? "BLOCKED_POLICY"
        : regenerationStatus === "FAILED"
          ? "FAILED"
          : "COMPLETE",
      processing_message:
        regenerationStatus === "FAILED"
          ? "USER_COMPOSITE 병합 자체는 성공적으로 커밋되었으나, 후속 파생 Fact/Claim 재생성 작업이 일시 실패했습니다. 기존 병합 결과는 유지되며 파생 재생성만 별도로 재시도할 수 있습니다."
          : null,
    },
    version_history: [
      {
        version: 1,
        status: "CANDIDATE",
        action: "MERGE",
        actor: "USER",
        reason: "결제 Webhook 서명 및 타임스탬프 방어 작업 통합",
        created_at: "2026-08-22T09:00:00Z",
      },
      {
        version: 2,
        status: "CONFIRMED",
        action: "CONFIRM",
        actor: "USER",
        reason: "병합 범위 확인 후 확정",
        created_at: "2026-08-22T09:05:00Z",
      },
    ],
    review_actions: [
      {
        action_id: "ra_comp_301_1",
        action: "MERGE",
        actor: "USER",
        from_version: null,
        to_version: 1,
        reason: "결제 Webhook 서명 및 타임스탬프 방어 작업 통합",
        created_at: "2026-08-22T09:00:00Z",
      },
      {
        action_id: "ra_comp_301_2",
        action: "CONFIRM",
        actor: "USER",
        from_version: 1,
        to_version: 2,
        reason: "병합 범위 확인 후 확정",
        created_at: "2026-08-22T09:05:00Z",
      },
    ],
  };

  return [pr104, dc209, dc215, pr112, composite301, memberPr088, memberDc091];
}

export class MockEventReviewAdapter
  implements EventReviewAdapter, EventReviewHarnessControls
{
  private readonly latencyMs: number;
  private readonly sourceResourceId: string;
  private scenarioId: EventReviewScenarioId;
  private events: Map<string, WorkEventDetailResponse> = new Map();
  private availabilityState: EventInboxAvailabilityState = "READY";
  private availabilityNotice: string | null = null;
  private forceConflictOnNextMutation = false;
  private sequenceCounter = 400;

  public confirmCallCount = 0;
  public editCallCount = 0;
  public rejectCallCount = 0;
  public splitCallCount = 0;
  public mergeCallCount = 0;
  public dissolveCallCount = 0;
  public lastRequestedPath: string | null = null;
  public lastMutationPath: string | null = null;
  public requestedPaths: string[] = [];

  constructor(options?: MockEventReviewAdapterOptions) {
    this.latencyMs = options?.latencyMs ?? 120;
    this.sourceResourceId = options?.sourceResourceId ?? DEFAULT_REPO_ID;
    this.scenarioId = options?.initialScenario ?? "default_inbox";
    this.seedScenario(this.scenarioId);
  }

  private async delay(): Promise<void> {
    if (this.latencyMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, this.latencyMs));
    }
  }

  private seedScenario(scenarioId: EventReviewScenarioId): void {
    this.events.clear();
    this.forceConflictOnNextMutation = false;
    this.availabilityState = "READY";
    this.availabilityNotice = null;

    if (scenarioId === "empty_no_eligible_events") {
      this.availabilityState = "EMPTY_NO_ELIGIBLE_EVENTS";
      this.availabilityNotice =
        "완료된 가져오기(SUCCESS)에서 적격 병합 PR과 적격 기본 브랜치 직접 커밋이 모두 0건이었습니다. 오류가 아닌 정상 빈 상태입니다.";
      return;
    }

    if (scenarioId === "association_unknown_incomplete") {
      this.availabilityState = "ASSOCIATION_UNKNOWN_INCOMPLETE";
      this.availabilityNotice =
        "일부 커밋의 PR 연결 확인이 미완료(UNKNOWN) 상태로 남아 있어 Event 후보 승격이 보류되었습니다. 적격 Event 0건인 정상 빈 상태와 다릅니다.";
      return;
    }

    let policyState: EventPolicyState = {
      status: "ACTIVE",
      reason_code: null,
      message: null,
      raw_view_blocked: false,
      external_ai_blocked: false,
      export_blocked: false,
    };

    if (scenarioId === "policy_blocked") {
      this.availabilityState = "POLICY_BLOCKED";
      this.availabilityNotice =
        "저장소가 공개→비공개로 전환되었거나 공개 정책 확인에 실패했습니다(POLICY_BLOCKED). 원본 삭제가 아니며 정책 재확인 전까지 편집·확정·분할·병합·해제가 차단됩니다.";
      policyState = {
        status: "POLICY_BLOCKED",
        reason_code: "POLICY_REVIEW_REQUIRED",
        message: this.availabilityNotice,
        raw_view_blocked: true,
        external_ai_blocked: true,
        export_blocked: true,
      };
    } else if (scenarioId === "deletion_pending") {
      this.availabilityState = "DELETION_PENDING";
      this.availabilityNotice =
        "삭제 진행 중 펜스(DELETION_PENDING · OD-003)가 활성화되어 모든 Event 편집·확정·병합·해제 명령이 차단되었습니다.";
      policyState = {
        status: "DELETION_PENDING",
        reason_code: "DELETION_FENCE_ACTIVE",
        message: this.availabilityNotice,
        raw_view_blocked: true,
        external_ai_blocked: true,
        export_blocked: true,
      };
    }

    const regenerationStatus =
      scenarioId === "regeneration_partial_failure" ? "FAILED" : "COMPLETE";
    const seeded = buildDefaultEvents(
      this.sourceResourceId,
      policyState,
      regenerationStatus
    );
    for (const item of seeded) {
      this.events.set(item.event_id, item);
    }

    if (scenarioId === "conflict_on_next_mutation") {
      this.forceConflictOnNextMutation = true;
    }
  }

  public setScenario(scenarioId: EventReviewScenarioId): void {
    this.scenarioId = scenarioId;
    this.seedScenario(scenarioId);
  }

  public getScenario(): EventReviewScenarioId {
    return this.scenarioId;
  }

  public simulateConcurrentUpdate(eventId: string): void {
    const existing = this.events.get(eventId);
    if (!existing) {
      return;
    }
    const nextVersion = existing.version + 1;
    const nowIso = new Date().toISOString();
    existing.version = nextVersion;
    existing.etag = `"v${nextVersion}"`;
    existing.status = existing.status === "CANDIDATE" ? "CONFIRMED" : existing.status;
    existing.updated_at = nowIso;
    existing.reviewed_at = nowIso;
    existing.version_history.push({
      version: nextVersion,
      status: existing.status,
      action: "CONCURRENT_EXTERNAL_UPDATE",
      actor: "USER",
      reason: "다른 탭에서 먼저 수정됨",
      created_at: nowIso,
    });
  }

  public reset(): void {
    this.seedScenario(this.scenarioId);
  }

  private checkPolicyOrDeletion(
    event: WorkEventDetailResponse
  ): EventReviewResult<never> | null {
    if (event.policy_state.status === "POLICY_BLOCKED") {
      return {
        ok: false,
        error: {
          kind: "policy_blocked",
          httpStatus: 403,
          code: "POLICY_BLOCKED",
          message:
            event.policy_state.message ??
            "공개 정책 차단(POLICY_BLOCKED)으로 변경할 수 없습니다. 원본 삭제가 아닌 정책 차단 상태입니다.",
        },
      };
    }
    if (event.policy_state.status === "DELETION_PENDING") {
      return {
        ok: false,
        error: {
          kind: "deletion_pending",
          httpStatus: 409,
          code: "DELETION_PENDING",
          message:
            event.policy_state.message ??
            "삭제 펜스(DELETION_PENDING)가 활성화되어 변경할 수 없습니다.",
        },
      };
    }
    return null;
  }

  private checkVersionConflict(
    event: WorkEventDetailResponse,
    expectedVersion: number
  ): EventReviewResult<never> | null {
    if (this.forceConflictOnNextMutation) {
      this.forceConflictOnNextMutation = false;
      this.simulateConcurrentUpdate(event.event_id);
    }
    if (event.version !== expectedVersion) {
      return {
        ok: false,
        error: {
          kind: "version_conflict",
          httpStatus: 409,
          code: "EVENT_VERSION_CONFLICT",
          message: `버전 충돌(409 EVENT_VERSION_CONFLICT): 요청한 v${expectedVersion} 대신 서버 최신 버전은 v${event.version} (${event.status})입니다. 자동 덮어쓰기를 수행하지 않았으니 최신 상태를 다시 불러온 뒤 재적용하세요.`,
          conflictDetails: {
            current_version: event.version,
            current_status: event.status,
            allowed_actions: [...event.allowed_actions],
            event_id: event.event_id,
          },
        },
      };
    }
    return null;
  }

  private recordPath(path: string, isMutation = false): void {
    this.lastRequestedPath = path;
    this.requestedPaths.push(path);
    if (isMutation) {
      this.lastMutationPath = path;
    }
  }

  public async listWorkEvents(
    query?: ListWorkEventsQuery
  ): Promise<EventReviewResult<WorkEventListResponse>> {
    await this.delay();
    this.recordPath("/v1/work-events");

    const allSummaries = Array.from(this.events.values()).map(toSummary);
    const activeTopLevelTotal = computeActiveTopLevelCount(allSummaries);

    const byStatus: Record<string, number> = {
      CANDIDATE: 0,
      CONFIRMED: 0,
      REJECTED: 0,
      STALE: 0,
      SUPERSEDED: 0,
    };
    for (const item of allSummaries) {
      byStatus[item.status] = (byStatus[item.status] ?? 0) + 1;
    }

    let filtered = allSummaries;
    if (query?.source_resource_id) {
      filtered = filtered.filter(
        (item) => item.source_resource_id === query.source_resource_id
      );
    }
    if (query?.status && query.status !== "ALL" && query.status !== "ACTIVE") {
      filtered = filtered.filter((item) => item.status === query.status);
    } else if (!query?.status || query.status === "ACTIVE") {
      filtered = filtered.filter(
        (item) => item.is_active_top_level && item.status !== "SUPERSEDED"
      );
    }

    return {
      ok: true,
      data: {
        items: filtered,
        page: { next_cursor: null, has_more: false },
        counts: {
          active_top_level_total: activeTopLevelTotal,
          by_status: byStatus,
        },
        inbox_state: this.availabilityState,
        inbox_notice: this.availabilityNotice,
        default_branch: "main",
      },
      meta: {
        httpStatus: 200,
        cacheControl: "private, no-store",
      },
    };
  }

  public async getWorkEventDetail(
    eventId: string
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    await this.delay();
    this.recordPath(`/v1/work-events/${eventId}`);
    const event = this.events.get(eventId);
    if (!event) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "EVENT_NOT_FOUND",
          message: `Event(${eventId})를 찾을 수 없습니다.`,
        },
      };
    }
    return {
      ok: true,
      data: clone(event),
      meta: {
        httpStatus: 200,
        etag: event.etag,
        cacheControl: "private, no-store",
      },
    };
  }

  public async confirmWorkEvent(
    input: ConfirmWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    await this.delay();
    this.confirmCallCount += 1;
    this.recordPath(`/v1/work-events/${input.event_id}/actions/confirm`, true);

    const event = this.events.get(input.event_id);
    if (!event) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "EVENT_NOT_FOUND",
          message: `Event(${input.event_id})를 찾을 수 없습니다.`,
        },
      };
    }

    const blocked = this.checkPolicyOrDeletion(event);
    if (blocked) return blocked;
    const conflict = this.checkVersionConflict(event, input.expected_version);
    if (conflict) return conflict;

    const nowIso = new Date().toISOString();
    const prevVersion = event.version;
    event.version += 1;
    event.etag = `"v${event.version}"`;
    event.status = "CONFIRMED";
    event.reviewed_at = nowIso;
    event.updated_at = nowIso;
    event.version_history.push({
      version: event.version,
      status: "CONFIRMED",
      action: "CONFIRM",
      actor: "USER",
      reason: input.reason ?? "User confirmed via Keep (확정)",
      created_at: nowIso,
    });
    event.review_actions.push({
      action_id: `ra_${++this.sequenceCounter}`,
      action: "CONFIRM",
      actor: "USER",
      from_version: prevVersion,
      to_version: event.version,
      reason: input.reason ?? "User confirmed via Keep (확정)",
      created_at: nowIso,
    });

    return {
      ok: true,
      data: clone(event),
      meta: { httpStatus: 200, etag: event.etag, cacheControl: "private, no-store" },
    };
  }

  public async editWorkEvent(
    input: EditWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    await this.delay();
    this.editCallCount += 1;
    this.recordPath(`/v1/work-events/${input.event_id}`, true);

    const event = this.events.get(input.event_id);
    if (!event) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "EVENT_NOT_FOUND",
          message: `Event(${input.event_id})를 찾을 수 없습니다.`,
        },
      };
    }

    const blocked = this.checkPolicyOrDeletion(event);
    if (blocked) return blocked;
    const conflict = this.checkVersionConflict(event, input.expected_version);
    if (conflict) return conflict;

    if (!input.title.trim()) {
      return {
        ok: false,
        error: {
          kind: "validation_error",
          httpStatus: 422,
          code: "INVALID_TITLE",
          message: "제목은 비워둘 수 없습니다.",
        },
      };
    }

    const nowIso = new Date().toISOString();
    const prevVersion = event.version;
    event.version += 1;
    event.etag = `"v${event.version}"`;
    event.status = "CANDIDATE";
    event.title = input.title.trim();
    event.problem = input.problem?.trim() || null;
    event.action = input.action?.trim() || null;
    event.outcome = input.outcome?.trim() || null;
    event.updated_at = nowIso;
    event.review_flags = event.review_flags.filter(
      (f) => f !== "GENERIC_TITLE_OR_SUMMARY"
    );
    event.version_history.push({
      version: event.version,
      status: "CANDIDATE",
      action: "EDIT",
      actor: "USER",
      reason: input.reason ?? "User edited title/summary",
      created_at: nowIso,
    });
    event.review_actions.push({
      action_id: `ra_${++this.sequenceCounter}`,
      action: "EDIT",
      actor: "USER",
      from_version: prevVersion,
      to_version: event.version,
      reason: input.reason ?? "User edited title/summary",
      created_at: nowIso,
    });

    return {
      ok: true,
      data: clone(event),
      meta: { httpStatus: 200, etag: event.etag, cacheControl: "private, no-store" },
    };
  }

  public async rejectWorkEvent(
    input: RejectWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    await this.delay();
    this.rejectCallCount += 1;
    this.recordPath(`/v1/work-events/${input.event_id}/actions/reject`, true);

    const event = this.events.get(input.event_id);
    if (!event) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "EVENT_NOT_FOUND",
          message: `Event(${input.event_id})를 찾을 수 없습니다.`,
        },
      };
    }

    const blocked = this.checkPolicyOrDeletion(event);
    if (blocked) return blocked;
    const conflict = this.checkVersionConflict(event, input.expected_version);
    if (conflict) return conflict;

    const nowIso = new Date().toISOString();
    const prevVersion = event.version;
    event.version += 1;
    event.etag = `"v${event.version}"`;
    event.status = "REJECTED";
    event.reviewed_at = nowIso;
    event.updated_at = nowIso;
    event.allowed_actions = ["REOPEN"];
    event.version_history.push({
      version: event.version,
      status: "REJECTED",
      action: "REJECT",
      actor: "USER",
      reason: input.reason ?? "User excluded candidate event",
      created_at: nowIso,
    });
    event.review_actions.push({
      action_id: `ra_${++this.sequenceCounter}`,
      action: "REJECT",
      actor: "USER",
      from_version: prevVersion,
      to_version: event.version,
      reason: input.reason ?? "User excluded candidate event",
      created_at: nowIso,
    });

    return {
      ok: true,
      data: clone(event),
      meta: { httpStatus: 200, etag: event.etag, cacheControl: "private, no-store" },
    };
  }

  public async splitWorkEvent(
    input: SplitWorkEventInput
  ): Promise<EventReviewResult<SplitWorkEventResponse>> {
    await this.delay();
    this.splitCallCount += 1;
    this.recordPath(`/v1/work-events/${input.event_id}/actions/split`, true);

    const parent = this.events.get(input.event_id);
    if (!parent) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "EVENT_NOT_FOUND",
          message: `Event(${input.event_id})를 찾을 수 없습니다.`,
        },
      };
    }

    const blocked = this.checkPolicyOrDeletion(parent);
    if (blocked) return blocked;
    const conflict = this.checkVersionConflict(parent, input.expected_version);
    if (conflict) return conflict;

    const allowedIds = parent.evidence_memberships.map((m) => m.source_object_id);
    const validation = validateSplitPartitions(input.partitions, allowedIds);
    if (!validation.valid) {
      return {
        ok: false,
        error: {
          kind: "validation_error",
          httpStatus: 422,
          code: "INVALID_SPLIT_PARTITIONS",
          message: validation.reason ?? "유효하지 않은 분할 범위입니다.",
        },
      };
    }

    const nowIso = new Date().toISOString();
    const prevVersion = parent.version;
    parent.version += 1;
    parent.etag = `"v${parent.version}"`;
    parent.status = "SUPERSEDED";
    parent.is_active_top_level = false;
    parent.allowed_actions = [];
    parent.updated_at = nowIso;
    parent.version_history.push({
      version: parent.version,
      status: "SUPERSEDED",
      action: "SPLIT",
      actor: "USER",
      reason: input.reason ?? "User split event into PR segments",
      created_at: nowIso,
    });
    parent.review_actions.push({
      action_id: `ra_${++this.sequenceCounter}`,
      action: "SPLIT",
      actor: "USER",
      from_version: prevVersion,
      to_version: parent.version,
      reason: input.reason ?? "User split event into PR segments",
      created_at: nowIso,
    });

    const createdSegments: WorkEventDetailResponse[] = input.partitions.map(
      (part, index) => {
        const segId = `evt_seg_${++this.sequenceCounter}`;
        const memberships = parent.evidence_memberships.filter((m) =>
          part.source_object_ids.includes(m.source_object_id)
        );
        const filePaths = Array.from(
          new Set(memberships.flatMap((m) => m.file_paths ?? []))
        );
        const seg: WorkEventDetailResponse = {
          event_id: segId,
          workspace_id: parent.workspace_id,
          source_resource_id: parent.source_resource_id,
          anchor_mode: "PR_SEGMENT",
          status: "CANDIDATE",
          version: 1,
          etag: '"v1"',
          is_active_top_level: true,
          title: part.title.trim(),
          problem: parent.problem,
          action: parent.action,
          outcome: parent.outcome,
          created_by: "USER",
          occurred_start_at: parent.occurred_start_at,
          occurred_end_at: parent.occurred_end_at,
          reviewed_at: null,
          updated_at: nowIso,
          review_flags: [],
          allowed_actions: ["CONFIRM", "EDIT", "REJECT", "MERGE"],
          evidence_count: memberships.length,
          context_count: 0,
          direct_commit_badge: null,
          composite_summary: null,
          policy_state: clone(parent.policy_state),
          primary_anchor_source_object_id:
            memberships[0]?.source_object_id ??
            parent.primary_anchor_source_object_id,
          anchor_scope: {
            kind: "PR_SEGMENT",
            file_paths: part.anchor_scope?.file_paths ?? filePaths,
            hunk_ranges: part.anchor_scope?.hunk_ranges,
            content_unit_ids: part.anchor_scope?.content_unit_ids,
          },
          evidence_memberships: clone(memberships),
          context_relations: [],
          cannot_link_constraints: {
            source_object_ids: [],
            peer_event_ids: [],
            constraints: [],
          },
          composite_details: null,
          downstream_impact: {
            fact_count: 0,
            claim_count: 0,
            export_count: 0,
            stale_fact_count: 0,
            stale_claim_count: 0,
            processing_status: "NOT_STARTED",
          },
          version_history: [
            {
              version: 1,
              status: "CANDIDATE",
              action: "SPLIT_CREATE",
              actor: "USER",
              reason: `Split #${index + 1} from ${parent.event_id}`,
              created_at: nowIso,
            },
          ],
          review_actions: [],
        };
        this.events.set(segId, seg);
        return clone(seg);
      }
    );

    return {
      ok: true,
      data: {
        superseded_parent: clone(parent),
        created_segments: createdSegments,
      },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async mergeWorkEvents(
    input: MergeWorkEventsInput
  ): Promise<EventReviewResult<MergeWorkEventsResponse>> {
    await this.delay();
    this.mergeCallCount += 1;
    this.recordPath("/v1/work-events/actions/merge", true);

    const memberEvents: WorkEventDetailResponse[] = [];
    const detailsMap: Record<string, WorkEventDetailResponse> = {};

    for (const ref of input.members) {
      const event = this.events.get(ref.event_id);
      if (!event) {
        return {
          ok: false,
          error: {
            kind: "not_found",
            httpStatus: 404,
            code: "EVENT_NOT_FOUND",
            message: `병합 대상 Event(${ref.event_id})를 찾을 수 없습니다.`,
          },
        };
      }
      const blocked = this.checkPolicyOrDeletion(event);
      if (blocked) return blocked;
      const conflict = this.checkVersionConflict(event, ref.expected_version);
      if (conflict) return conflict;

      memberEvents.push(event);
      detailsMap[event.event_id] = event;
    }

    const validation = validateMergeSelection(
      memberEvents.map(toSummary),
      detailsMap
    );
    if (!validation.valid) {
      return {
        ok: false,
        error: {
          kind:
            validation.code === "CANNOT_LINK_CONFLICT"
              ? "cannot_link_conflict"
              : "validation_error",
          httpStatus: validation.code === "CANNOT_LINK_CONFLICT" ? 409 : 422,
          code: validation.code,
          message: validation.reason ?? "병합 조건을 충족하지 않습니다.",
        },
      };
    }

    const nowIso = new Date().toISOString();
    const compositeId = `evt_comp_${++this.sequenceCounter}`;
    const mergeReason = input.reason?.trim() || "사용자 수동 병합 (USER_COMPOSITE)";

    const snapshots: CompositeMemberSnapshot[] = [];
    const mergedEvidence = [];
    const mergedContext = [];
    let totalFacts = 0;
    let totalClaims = 0;
    let totalExports = 0;

    for (const member of memberEvents) {
      totalFacts += member.downstream_impact.fact_count;
      totalClaims += member.downstream_impact.claim_count;
      totalExports += member.downstream_impact.export_count;

      snapshots.push({
        member_event_id: member.event_id,
        member_version: member.version,
        anchor_mode: member.anchor_mode as "PR" | "PR_SEGMENT" | "DIRECT_COMMIT",
        title: member.title,
        status_at_merge: member.status,
        current_status: "SUPERSEDED",
        evidence_count: member.evidence_memberships.length,
        occurred_start_at: member.occurred_start_at,
        occurred_end_at: member.occurred_end_at,
        direct_commit_badge: member.direct_commit_badge
          ? clone(member.direct_commit_badge)
          : null,
      });

      mergedEvidence.push(...clone(member.evidence_memberships));
      mergedContext.push(...clone(member.context_relations));

      const prevVersion = member.version;
      member.version += 1;
      member.etag = `"v${member.version}"`;
      member.status = "SUPERSEDED";
      member.is_active_top_level = false;
      member.allowed_actions = [];
      member.updated_at = nowIso;
      member.composite_details = {
        member_count: 0,
        merge_reason: mergeReason,
        regeneration_status: "COMPLETE",
        superseded_by_composite_id: compositeId,
        members: [],
      };
      member.version_history.push({
        version: member.version,
        status: "SUPERSEDED",
        action: "MERGE",
        actor: "USER",
        reason: `Superseded by ${compositeId}`,
        created_at: nowIso,
      });
      member.review_actions.push({
        action_id: `ra_${++this.sequenceCounter}`,
        action: "MERGE",
        actor: "USER",
        from_version: prevVersion,
        to_version: member.version,
        reason: mergeReason,
        created_at: nowIso,
      });
    }

    const regenerationFailed =
      this.scenarioId === "regeneration_partial_failure";
    const sortedStarts = memberEvents
      .map((m) => m.occurred_start_at)
      .sort();
    const sortedEnds = memberEvents
      .map((m) => m.occurred_end_at)
      .sort();

    const compositeEvent: WorkEventDetailResponse = {
      event_id: compositeId,
      workspace_id: memberEvents[0].workspace_id,
      source_resource_id: memberEvents[0].source_resource_id,
      anchor_mode: "USER_COMPOSITE",
      status: "CANDIDATE",
      version: 1,
      etag: '"v1"',
      is_active_top_level: true,
      title: input.title.trim() || `${memberEvents[0].title} 외 ${memberEvents.length - 1}건 병합`,
      problem: memberEvents.map((m) => m.problem).filter(Boolean).join(" / ") || null,
      action: memberEvents.map((m) => m.action).filter(Boolean).join(" / ") || null,
      outcome: memberEvents.map((m) => m.outcome).filter(Boolean).join(" / ") || null,
      created_by: "USER",
      occurred_start_at: sortedStarts[0] ?? nowIso,
      occurred_end_at: sortedEnds[sortedEnds.length - 1] ?? nowIso,
      reviewed_at: null,
      updated_at: nowIso,
      review_flags: [],
      allowed_actions: isEventMutationBlockedByPolicyOrDeletion(memberEvents[0])
        ? []
        : ["CONFIRM", "EDIT", "DISSOLVE"],
      evidence_count: mergedEvidence.length,
      context_count: mergedContext.length,
      direct_commit_badge: null,
      composite_summary: {
        member_count: memberEvents.length,
        badge_label: `${memberEvents.length}개 Event를 사용자가 병합`,
        merge_reason: mergeReason,
        regeneration_status: regenerationFailed ? "FAILED" : "COMPLETE",
      },
      policy_state: clone(memberEvents[0].policy_state),
      primary_anchor_source_object_id: null,
      anchor_scope: {
        kind: "USER_COMPOSITE",
        file_paths: Array.from(
          new Set(mergedEvidence.flatMap((m) => m.file_paths ?? []))
        ),
      },
      evidence_memberships: mergedEvidence,
      context_relations: mergedContext,
      cannot_link_constraints: {
        source_object_ids: [],
        peer_event_ids: [],
        constraints: [],
      },
      composite_details: {
        member_count: memberEvents.length,
        merge_reason: mergeReason,
        regeneration_status: regenerationFailed ? "FAILED" : "COMPLETE",
        superseded_by_composite_id: null,
        members: snapshots,
      },
      downstream_impact: {
        fact_count: 0,
        claim_count: 0,
        export_count: 0,
        stale_fact_count: totalFacts,
        stale_claim_count: totalClaims,
        processing_status: regenerationFailed ? "FAILED" : "RUNNING",
        processing_message: regenerationFailed
          ? "USER_COMPOSITE 병합 커밋은 성공했으나, 후속 Fact/Claim 재생성이 실패했습니다. 병합 자체는 보존됩니다."
          : "기존 멤버의 Fact/Claim/Export는 자동 승계되지 않으며, 새 Composite 기준으로 재생성·재검증됩니다.",
      },
      version_history: [
        {
          version: 1,
          status: "CANDIDATE",
          action: "MERGE",
          actor: "USER",
          reason: mergeReason,
          created_at: nowIso,
        },
      ],
      review_actions: [
        {
          action_id: `ra_${++this.sequenceCounter}`,
          action: "MERGE",
          actor: "USER",
          from_version: null,
          to_version: 1,
          reason: mergeReason,
          created_at: nowIso,
        },
      ],
    };

    this.events.set(compositeId, compositeEvent);

    return {
      ok: true,
      data: {
        composite_event: clone(compositeEvent),
        superseded_member_ids: memberEvents.map((m) => m.event_id),
        affected_downstream_impact: {
          fact_count: totalFacts,
          claim_count: totalClaims,
          export_count: totalExports,
        },
      },
      meta: {
        httpStatus: 200,
        etag: compositeEvent.etag,
        cacheControl: "private, no-store",
      },
    };
  }

  public async dissolveWorkEvent(
    input: DissolveWorkEventInput
  ): Promise<EventReviewResult<DissolveWorkEventResponse>> {
    await this.delay();
    this.dissolveCallCount += 1;
    this.recordPath(`/v1/work-events/${input.event_id}/actions/dissolve`, true);

    const composite = this.events.get(input.event_id);
    if (!composite) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "EVENT_NOT_FOUND",
          message: `Event(${input.event_id})를 찾을 수 없습니다.`,
        },
      };
    }

    if (composite.anchor_mode !== "USER_COMPOSITE") {
      return {
        ok: false,
        error: {
          kind: "validation_error",
          httpStatus: 422,
          code: "NOT_A_COMPOSITE_EVENT",
          message: "USER_COMPOSITE Event만 병합 해제(Dissolve)할 수 있습니다.",
        },
      };
    }

    const blocked = this.checkPolicyOrDeletion(composite);
    if (blocked) return blocked;
    const conflict = this.checkVersionConflict(composite, input.expected_version);
    if (conflict) return conflict;

    const nowIso = new Date().toISOString();
    const dissolveReason = input.reason?.trim() || "사용자 병합 해제 (Dissolve)";
    const prevVersion = composite.version;

    const staleFacts = composite.downstream_impact.fact_count;
    const staleClaims = composite.downstream_impact.claim_count;
    const staleExports = composite.downstream_impact.export_count;

    composite.version += 1;
    composite.etag = `"v${composite.version}"`;
    composite.status = "SUPERSEDED";
    composite.is_active_top_level = false;
    composite.allowed_actions = [];
    composite.updated_at = nowIso;
    composite.downstream_impact = {
      ...composite.downstream_impact,
      stale_fact_count: composite.downstream_impact.stale_fact_count + staleFacts,
      stale_claim_count: composite.downstream_impact.stale_claim_count + staleClaims,
      processing_status: "COMPLETE",
      processing_message:
        "병합 해제로 인해 Composite의 기존 Fact/Claim/Export가 STALE 처리되었습니다.",
    };
    composite.version_history.push({
      version: composite.version,
      status: "SUPERSEDED",
      action: "DISSOLVE",
      actor: "USER",
      reason: dissolveReason,
      created_at: nowIso,
    });
    composite.review_actions.push({
      action_id: `ra_${++this.sequenceCounter}`,
      action: "DISSOLVE",
      actor: "USER",
      from_version: prevVersion,
      to_version: composite.version,
      reason: dissolveReason,
      created_at: nowIso,
    });

    const restoredMembers: WorkEventDetailResponse[] = [];
    const snapshots = composite.composite_details?.members ?? [];

    for (const snap of snapshots) {
      const member = this.events.get(snap.member_event_id);
      if (!member) continue;
      const prevMemberVersion = member.version;
      member.version += 1;
      member.etag = `"v${member.version}"`;
      member.status =
        snap.status_at_merge === "CONFIRMED" ? "CONFIRMED" : "CANDIDATE";
      member.is_active_top_level = true;
      member.allowed_actions =
        member.anchor_mode === "DIRECT_COMMIT"
          ? ["CONFIRM", "EDIT", "REJECT", "MERGE"]
          : ["CONFIRM", "EDIT", "REJECT", "SPLIT", "MERGE"];
      member.updated_at = nowIso;
      member.composite_details = null;
      member.version_history.push({
        version: member.version,
        status: member.status,
        action: "DISSOLVE_RESTORE",
        actor: "USER",
        reason: `Restored from dissolved composite ${composite.event_id}`,
        created_at: nowIso,
      });
      member.review_actions.push({
        action_id: `ra_${++this.sequenceCounter}`,
        action: "DISSOLVE",
        actor: "USER",
        from_version: prevMemberVersion,
        to_version: member.version,
        reason: dissolveReason,
        created_at: nowIso,
      });
      restoredMembers.push(clone(member));
    }

    return {
      ok: true,
      data: {
        dissolved_composite: clone(composite),
        restored_members: restoredMembers,
        stale_downstream_impact: {
          stale_fact_count: staleFacts,
          stale_claim_count: staleClaims,
          stale_export_count: staleExports,
        },
      },
      meta: {
        httpStatus: 200,
        etag: composite.etag,
        cacheControl: "private, no-store",
      },
    };
  }
}
