import { describe, expect, it } from "vitest";
import { MockEventReviewAdapter } from "@/adapters/mock-event-review-adapter";
import type {
  ReviewFlagCode,
  WorkEventDetailResponse,
  WorkEventSummaryItem,
} from "@/domain/event-review-models";
import {
  computeActiveTopLevelCount,
  formatAnchorModeBadge,
  REVIEW_FLAG_CATALOG,
  validateMergeSelection,
  validateSplitPartitions,
} from "@/domain/event-review-presenter";
import {
  canEnterEventInbox,
  createInitialImportJourneyState,
} from "@/domain/state-machine";
import type { ImportJourneyState, ImportRunViewModel } from "@/domain/view-models";

function makeBaseReadyState(run: ImportRunViewModel | null): ImportJourneyState {
  const base = createInitialImportJourneyState();
  return {
    ...base,
    phase: "ready",
    serviceAuth: {
      status: "authenticated",
      account: {
        syntheticUserId: "u1",
        displayName: "Tester",
        email: "t@example.invalid",
      },
      sessionNote: "ok",
      lastCheckedAt: "2026-11-28T00:00:00Z",
    },
    githubConnection: {
      status: "connected",
      accountHandle: "@tester",
      connectionRef: "ref1",
      scopeSummary: "read:user",
      revocationReason: null,
      connectedAt: "2026-11-28T00:00:00Z",
    },
    activeRun: run,
  };
}

function makeRun(overrides: Partial<ImportRunViewModel>): ImportRunViewModel {
  return {
    runId: "run_1",
    repositoryId: "repo_1",
    repositoryFullName: "owner/repo",
    contractStatus: "SUCCESS",
    status: "succeeded",
    attemptNumber: 1,
    startedAt: "2026-11-28T00:00:00Z",
    updatedAt: "2026-11-28T00:01:00Z",
    progressMessage: "done",
    readiness: {
      importReady: true,
      importReadyState: "ready",
      importReadySummary: "ready",
      processingStatus: "COMPLETE",
      derivedAllReady: true,
      derivedStages: [],
    },
    retryPolicy: {
      allowed: false,
      reason: "done",
      nextAllowedAt: null,
    },
    summary: null,
    partialDetail: null,
    limitDetail: null,
    error: null,
    ...overrides,
  };
}

describe("Event Inbox entry gate (Product Spec §5)", () => {
  it("enables Event Inbox strictly when status=SUCCESS, import_ready=true, and processing_status!=BLOCKED_POLICY", () => {
    expect(canEnterEventInbox(makeBaseReadyState(makeRun({})))).toBe(true);

    // Derived processing FAILED while import_ready=true & not BLOCKED_POLICY is still eligible
    expect(
      canEnterEventInbox(
        makeBaseReadyState(
          makeRun({
            readiness: {
              importReady: true,
              importReadyState: "ready",
              importReadySummary: "ready",
              processingStatus: "FAILED",
              derivedAllReady: false,
              derivedStages: [],
            },
          })
        )
      )
    ).toBe(true);
  });

  it("strictly blocks Event Inbox for PENDING, RUNNING, PARTIAL, PARTIAL_WITH_LIMIT, FAILED, CANCELED, import_ready=false, and BLOCKED_POLICY", () => {
    const blockedCases: Array<Partial<ImportRunViewModel>> = [
      { contractStatus: "PENDING", status: "running", readiness: { importReady: false, importReadyState: "pending", importReadySummary: "", processingStatus: "NOT_STARTED", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "RUNNING", status: "running", readiness: { importReady: false, importReadyState: "in_progress", importReadySummary: "", processingStatus: "RUNNING", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "PARTIAL", status: "partial", readiness: { importReady: false, importReadyState: "partial", importReadySummary: "", processingStatus: "NOT_STARTED", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "PARTIAL_WITH_LIMIT", status: "limit_reached", readiness: { importReady: false, importReadyState: "partial", importReadySummary: "", processingStatus: "NOT_STARTED", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "FAILED", status: "failed", readiness: { importReady: false, importReadyState: "failed", importReadySummary: "", processingStatus: "FAILED", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "CANCELED", status: "canceled", readiness: { importReady: false, importReadyState: "failed", importReadySummary: "", processingStatus: "NOT_STARTED", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "SUCCESS", status: "succeeded", readiness: { importReady: false, importReadyState: "in_progress", importReadySummary: "", processingStatus: "COMPLETE", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "SUCCESS", status: "succeeded", readiness: { importReady: true, importReadyState: "ready", importReadySummary: "", processingStatus: "BLOCKED_POLICY", derivedAllReady: false, derivedStages: [] } },
      { contractStatus: "SUCCESS", status: "succeeded", readiness: { importReady: true, importReadyState: "ready", importReadySummary: "", processingStatus: "BLOCKED_SOURCE", derivedAllReady: false, derivedStages: [] } },
    ];

    for (const runOverride of blockedCases) {
      expect(canEnterEventInbox(makeBaseReadyState(makeRun(runOverride)))).toBe(
        false
      );
    }
    expect(canEnterEventInbox(makeBaseReadyState(null))).toBe(false);
  });
});

describe("Event Review domain presenters & validators", () => {
  it("defines all 7 deterministic review_flags with Korean labels and descriptions", () => {
    const all7: ReviewFlagCode[] = [
      "AMBIGUOUS_AUTHOR_IDENTITY",
      "DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED",
      "GENERIC_TITLE_OR_SUMMARY",
      "MIXED_PURPOSE_SIGNALS",
      "MULTIPLE_LINKED_ISSUES",
      "PARTIAL_SOURCE_SKIPPED",
      "REVIEW_REQUIRED_MULTI_PARENT",
    ];
    expect(Object.keys(REVIEW_FLAG_CATALOG)).toHaveLength(7);
    for (const code of all7) {
      expect(REVIEW_FLAG_CATALOG[code].code).toBe(code);
      expect(REVIEW_FLAG_CATALOG[code].label.length).toBeGreaterThan(3);
      expect(REVIEW_FLAG_CATALOG[code].description.length).toBeGreaterThan(10);
    }
  });

  it("counts USER_COMPOSITE as exactly 1 logical Event and excludes SUPERSEDED members (EV-010)", async () => {
    const adapter = new MockEventReviewAdapter({ latencyMs: 0 });
    const allList = await adapter.listWorkEvents({ status: "ALL" });
    expect(allList.ok).toBe(true);
    if (!allList.ok) return;

    // Default fixture has 7 rows total: 4 active PR/DIRECT_COMMIT + 1 active USER_COMPOSITE + 2 SUPERSEDED members
    expect(allList.data.items).toHaveLength(7);
    expect(computeActiveTopLevelCount(allList.data.items)).toBe(5);
    expect(allList.data.counts.active_top_level_total).toBe(5);

    const composite = allList.data.items.find(
      (i) => i.anchor_mode === "USER_COMPOSITE"
    )!;
    expect(formatAnchorModeBadge(composite).label).toBe("2개 Event를 사용자가 병합");

    const dc = allList.data.items.find((i) => i.event_id === "evt_dc_215")!;
    expect(formatAnchorModeBadge(dc).label).toBe(
      "PR 없음 · default branch 직접 반영"
    );
    expect(dc.direct_commit_badge?.default_branch).toBe("trunk");
  });

  it("blocks nested USER_COMPOSITE merge and CANNOT_LINK peer merge, and computes downstream impact", async () => {
    const adapter = new MockEventReviewAdapter({ latencyMs: 0 });
    const listRes = await adapter.listWorkEvents({ status: "ACTIVE" });
    expect(listRes.ok).toBe(true);
    if (!listRes.ok) return;

    const details: Record<string, WorkEventDetailResponse> = {};
    for (const item of listRes.data.items) {
      const d = await adapter.getWorkEventDetail(item.event_id);
      if (d.ok) details[item.event_id] = d.data;
    }

    const byId = (id: string): WorkEventSummaryItem =>
      listRes.data.items.find((i) => i.event_id === id)!;

    // Valid merge: PR #104 + DIRECT_COMMIT #209
    const valid = validateMergeSelection(
      [byId("evt_pr_104"), byId("evt_dc_209")],
      details
    );
    expect(valid.valid).toBe(true);
    expect(valid.affectedImpact).toEqual({
      fact_count: 4,
      claim_count: 3,
      export_count: 1,
    });

    // Nested USER_COMPOSITE must be blocked
    const nested = validateMergeSelection(
      [byId("evt_pr_104"), byId("evt_comp_301")],
      details
    );
    expect(nested.valid).toBe(false);
    expect(nested.code).toBe("NESTED_COMPOSITE_FORBIDDEN");

    // CANNOT_LINK peer pair (evt_pr_112 & evt_dc_215) must be blocked
    const cannotLink = validateMergeSelection(
      [byId("evt_pr_112"), byId("evt_dc_215")],
      details
    );
    expect(cannotLink.valid).toBe(false);
    expect(cannotLink.code).toBe("CANNOT_LINK_CONFLICT");
    expect(cannotLink.reason).toContain("CANNOT_LINK");
  });

  it("validates disjoint non-empty split partitions strictly against MUST_LINK evidence IDs", () => {
    const allowed = ["so_1", "so_2", "so_3"];

    expect(
      validateSplitPartitions(
        [
          { title: "Part A", source_object_ids: ["so_1"] },
          { title: "Part B", source_object_ids: ["so_2", "so_3"] },
        ],
        allowed
      ).valid
    ).toBe(true);

    // Overlapping partition rejected
    const overlapping = validateSplitPartitions(
      [
        { title: "Part A", source_object_ids: ["so_1", "so_2"] },
        { title: "Part B", source_object_ids: ["so_2", "so_3"] },
      ],
      allowed
    );
    expect(overlapping.valid).toBe(false);
    expect(overlapping.reason).toContain("겹칩니다");

    // Empty partition rejected
    const emptyPart = validateSplitPartitions(
      [
        { title: "Part A", source_object_ids: ["so_1"] },
        { title: "Part B", source_object_ids: [] },
      ],
      allowed
    );
    expect(emptyPart.valid).toBe(false);

    // CONTEXT_ONLY object outside MUST_LINK rejected
    const contextObj = validateSplitPartitions(
      [
        { title: "Part A", source_object_ids: ["so_1"] },
        { title: "Part B", source_object_ids: ["so_issue_99"] },
      ],
      allowed
    );
    expect(contextObj.valid).toBe(false);
  });
});
