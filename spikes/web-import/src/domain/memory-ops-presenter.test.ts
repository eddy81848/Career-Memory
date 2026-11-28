import { describe, expect, it } from "vitest";
import type {
  DeletionImpactCounts,
  DeletionJobStatusResponse,
  QuotaStatusResponse,
  WorkspaceSourceResourceSummary,
} from "./memory-ops-models";
import {
  assertNoProjectIdInRecord,
  buildDeletionImpactRows,
  buildSafeSearchQueryParams,
  buildSearchHitDrilldownHref,
  evaluateOperationPermissions,
  formatDeletionComponentStatusBadge,
  formatDeletionUserVisibleStateBadge,
  formatQuotaAdmissionBadge,
  formatSearchEntityBadge,
  formatUtilizationPercent,
  resolveDefaultSearchScope,
  sumDeletionImpactCounts,
  summarizeDeletionReceipt,
} from "./memory-ops-presenter";

describe("memory-ops-presenter (M5 OD-008, OD-003, T-503 / INF-06)", () => {
  it("builds OD-008 search query params with repeated entity_types and strictly blocks project_id", () => {
    const params = buildSafeSearchQueryParams({
      q: "  webhook HMAC  ",
      source_resource_id: "src_repo_ledger_01",
      entity_types: ["WORK_EVENT", "CLAIM", "WORK_EVENT"],
      limit: 20,
      cursor: "cur_v1_abc",
    });

    expect(params.get("q")).toBe("webhook HMAC");
    expect(params.get("source_resource_id")).toBe("src_repo_ledger_01");
    expect(params.getAll("entity_types")).toEqual(["WORK_EVENT", "CLAIM"]);
    expect(params.get("limit")).toBe("20");
    expect(params.get("cursor")).toBe("cur_v1_abc");
    expect(params.toString()).not.toContain("project_id");

    // OD-008 fail-closed guard against any project_id variant
    expect(() =>
      buildSafeSearchQueryParams({
        q: "webhook",
        project_id: "proj_forbidden_01",
      } as unknown as Parameters<typeof buildSafeSearchQueryParams>[0])
    ).toThrow(/OD-008/);

    expect(() =>
      assertNoProjectIdInRecord({ nested: { ProjectId: "proj_2" } })
    ).toThrow(/OD-008/);
  });

  it("auto-selects source_resource_id when exactly 1 active repo exists and stays explicit for multiple repos", () => {
    const single: WorkspaceSourceResourceSummary[] = [
      {
        source_resource_id: "src_1",
        repository_full_name: "org/one",
        provider: "GITHUB",
        visibility: "PUBLIC",
        fence_active: false,
        deleted: false,
        active_deletion_job_id: null,
      },
    ];
    expect(resolveDefaultSearchScope(single)).toMatchObject({
      autoSelectedSingleRepo: true,
      defaultSourceResourceId: "src_1",
    });

    const multi: WorkspaceSourceResourceSummary[] = [
      ...single,
      {
        source_resource_id: "src_2",
        repository_full_name: "org/two",
        provider: "GITHUB",
        visibility: "PUBLIC",
        fence_active: false,
        deleted: false,
        active_deletion_job_id: null,
      },
    ];
    expect(resolveDefaultSearchScope(multi)).toMatchObject({
      autoSelectedSingleRepo: false,
      defaultSourceResourceId: null,
    });
  });

  it("formats entity badges and direct drill-down links for WORK_EVENT, FACT, and CLAIM", () => {
    expect(formatSearchEntityBadge("WORK_EVENT").label).toContain("WORK_EVENT");
    expect(formatSearchEntityBadge("FACT").label).toContain("FACT");
    expect(formatSearchEntityBadge("CLAIM").label).toContain("CLAIM");

    expect(
      buildSearchHitDrilldownHref({
        entity_type: "WORK_EVENT",
        entity_id: "evt_1",
        source_resource_id: "src_1",
        title_or_summary: "Event",
        score: 0.95,
      }).href
    ).toBe("/event-review?event_id=evt_1");

    expect(
      buildSearchHitDrilldownHref({
        entity_type: "FACT",
        entity_id: "fact_1",
        source_resource_id: "src_1",
        title_or_summary: "Fact",
        score: 0.9,
      }).href
    ).toBe("/claim-review?fact_id=fact_1");

    expect(
      buildSearchHitDrilldownHref({
        entity_type: "CLAIM",
        entity_id: "clm_1",
        source_resource_id: "src_1",
        title_or_summary: "Claim",
        score: 0.92,
      }).href
    ).toBe("/claim-review?claim_id=clm_1");
  });

  it("presents all 10 OD-003 deletion impact counts and highlights user-edited/approved Claims & server Exports", () => {
    const counts: DeletionImpactCounts = {
      work_events: 6,
      facts: 18,
      contributions: 7,
      claims_draft: 2,
      claims_edited: 3,
      claims_approved: 4,
      claims_historical_versions: 5,
      search_index_entries: 28,
      ephemeral_cache_entries: 4,
      server_exports: 3,
    };

    const rows = buildDeletionImpactRows(counts);
    expect(rows).toHaveLength(10);
    expect(sumDeletionImpactCounts(counts)).toBe(80);

    const highlightedKeys = rows.filter((r) => r.highRisk).map((r) => r.key);
    expect(highlightedKeys).toContain("claims_approved");
    expect(highlightedKeys).toContain("claims_edited");
    expect(highlightedKeys).toContain("claims_historical_versions");
    expect(highlightedKeys).toContain("server_exports");
  });

  it("summarizes PARTIAL / DELETION_PENDING vs SUCCEEDED / DELETED deletion job receipts accurately", () => {
    const partialJob: DeletionJobStatusResponse = {
      deletion_job_id: "del_1",
      workspace_id: "ws_1",
      source_resource_id: "src_1",
      status: "PARTIAL",
      user_visible_state: "DELETION_PENDING",
      fence_active: true,
      checkpoint: {},
      failure_summary: {},
      components: [
        {
          component: "JOBS_AND_OUTBOX",
          status: "SUCCEEDED",
          attempt: 1,
          purged_count: 2,
          last_error_code: null,
          receipt_metadata: {},
          updated_at: "2026-11-28T05:10:00Z",
          completed_at: "2026-11-28T05:10:00Z",
        },
        {
          component: "EXPORTS",
          status: "SUCCEEDED",
          attempt: 1,
          purged_count: 3,
          last_error_code: null,
          receipt_metadata: {},
          updated_at: "2026-11-28T05:10:01Z",
          completed_at: "2026-11-28T05:10:01Z",
        },
        {
          component: "FTS_INDEX",
          status: "FAILED",
          attempt: 2,
          purged_count: 10,
          last_error_code: "FTS_TIMEOUT",
          receipt_metadata: {},
          updated_at: "2026-11-28T05:10:02Z",
          completed_at: null,
        },
        {
          component: "EPHEMERAL_CACHE",
          status: "SUCCEEDED",
          attempt: 1,
          purged_count: 1,
          last_error_code: null,
          receipt_metadata: {},
          updated_at: "2026-11-28T05:10:00Z",
          completed_at: "2026-11-28T05:10:00Z",
        },
        {
          component: "AUTHORITATIVE_DOMAIN",
          status: "PENDING",
          attempt: 0,
          purged_count: 0,
          last_error_code: null,
          receipt_metadata: {},
          updated_at: "2026-11-28T05:10:02Z",
          completed_at: null,
        },
      ],
      requested_by: "usr_1",
      requested_at: "2026-11-28T05:09:59Z",
      completed_at: null,
    };

    const summary = summarizeDeletionReceipt(partialJob);
    expect(summary.totalComponents).toBe(5);
    expect(summary.succeededCount).toBe(3);
    expect(summary.failedCount).toBe(1);
    expect(summary.pendingOrRunningCount).toBe(1);
    expect(summary.totalPurgedCount).toBe(16);
    expect(summary.incompleteComponents).toEqual([
      "FTS_INDEX",
      "AUTHORITATIVE_DOMAIN",
    ]);
    expect(summary.isPartialPending).toBe(true);
    expect(summary.isFullyDeleted).toBe(false);

    expect(
      formatDeletionUserVisibleStateBadge("DELETION_PENDING", "PARTIAL").label
    ).toContain("PARTIAL");
    expect(formatDeletionComponentStatusBadge("FAILED").badgeClass).toBe(
      "badge--danger"
    );
  });

  it("enforces T-503 / INF-06 quota permissions: 95% cutoff blocks high-cost ops while keeping OD-003 deletion and disconnect enabled", () => {
    const cutoffQuota: QuotaStatusResponse = {
      workspace_id: "ws_1",
      period_key: "2026-11",
      admission_state: "CUTOFF_95_PERCENT",
      pause_state: "PAUSED_QUOTA",
      pause_reason_code: "BUDGET_95_PERCENT_CUTOFF",
      circuit_breaker_state: "OPEN",
      high_cost_operations_allowed: false,
      safety_operations_allowed: true,
      paid_fallback_allowed: false,
      retry_after_seconds: 900,
      free_alternatives: ["Reduce scope"],
      meters: [],
      checked_at: "2026-11-28T05:15:00Z",
    };

    expect(formatQuotaAdmissionBadge("CUTOFF_95_PERCENT").badgeClass).toBe(
      "badge--danger"
    );
    expect(formatUtilizationPercent(96.25)).toBe("96.3%");

    const permsActiveRepo = evaluateOperationPermissions({
      quotaStatus: cutoffQuota,
      selectedRepoFenced: false,
      selectedRepoDeleted: false,
    });
    expect(permsActiveRepo.canRunHighCostOnSelectedRepo).toBe(false);
    expect(permsActiveRepo.canDeleteSelectedRepo).toBe(true);
    expect(permsActiveRepo.canDisconnectAccount).toBe(true);
    expect(permsActiveRepo.paidFallbackAllowed).toBe(false);

    const permsFencedRepo = evaluateOperationPermissions({
      quotaStatus: cutoffQuota,
      selectedRepoFenced: true,
      selectedRepoDeleted: false,
    });
    expect(permsFencedRepo.canSearchSelectedRepo).toBe(false);
    expect(permsFencedRepo.canRunHighCostOnSelectedRepo).toBe(false);
    expect(permsFencedRepo.canDeleteSelectedRepo).toBe(false);
  });
});
