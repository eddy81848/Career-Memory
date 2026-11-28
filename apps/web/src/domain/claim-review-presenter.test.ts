import { describe, expect, it } from "vitest";
import type { ClaimDetailResponse, ClaimSummaryItem } from "./claim-review-models";
import {
  buildEphemeralViewerStateFromError,
  buildEphemeralViewerStateFromSuccess,
  evaluateClaimApprovalGate,
  evaluateExportSelection,
  evaluateWorkspaceGuard,
  extractMetricSixElementPills,
  formatClaimStatusBadge,
  formatComputedVerificationBadge,
} from "./claim-review-presenter";

function makeClaim(
  overrides: Partial<ClaimDetailResponse> = {}
): ClaimDetailResponse {
  return {
    claim_id: "clm_test_1",
    workspace_id: "ws_1",
    event_id: "evt_1",
    version: 1,
    supersedes_claim_id: null,
    claim_text: "Implemented HMAC verification middleware.",
    status: "SUPPORT_VALIDATED",
    computed_verification: "SOURCE_VERIFIED",
    atom_count: 1,
    supported_atom_count: 1,
    user_confirmed_atom_count: 0,
    unsupported_atom_count: 0,
    stale_atom_count: 0,
    approvable: true,
    approval_block_reasons: [],
    support_graph_epoch: 1,
    approved_at: null,
    last_verified_at: "2026-11-28T03:00:00Z",
    generator_run_id: "run_1",
    last_verification_run_id: "run_2",
    created_at: "2026-11-28T02:00:00Z",
    updated_at: "2026-11-28T03:00:00Z",
    atoms: [
      {
        atom_id: "a1",
        ordinal: 1,
        atom_type: "ACTION",
        atom_text: "Implemented HMAC verification middleware",
        status: "SUPPORTED",
      },
    ],
    ...overrides,
  };
}

describe("claim-review-presenter", () => {
  it("keeps workflow status distinct from read-only computed_verification badges", () => {
    const statusBadge = formatClaimStatusBadge("APPROVED");
    const verBadge = formatComputedVerificationBadge("USER_CONFIRMED");
    expect(statusBadge.label).toContain("APPROVED");
    expect(verBadge.label).toContain("USER_CONFIRMED");
    expect(verBadge.label).toContain("읽기 전용");
  });

  it("enforces workspace entry guard (SUCCESS + import_ready) and policy/deletion locks", () => {
    expect(
      evaluateWorkspaceGuard({
        import_ready: false,
        import_status: "PARTIAL",
        policy_status: "ACTIVE",
        event_id: "evt_1",
        event_title: "Test",
        event_status: "CONFIRMED",
        repository_label: "org/repo",
      })
    ).toMatchObject({
      canEnter: false,
      mutationsLocked: true,
      lockCode: "IMPORT_NOT_READY",
    });

    expect(
      evaluateWorkspaceGuard({
        import_ready: true,
        import_status: "SUCCESS",
        policy_status: "DELETION_PENDING",
        event_id: "evt_1",
        event_title: "Test",
        event_status: "CONFIRMED",
        repository_label: "org/repo",
      })
    ).toMatchObject({
      canEnter: true,
      mutationsLocked: true,
      lockCode: "DELETION_PENDING",
    });
  });

  it("blocks approval when UNSUPPORTED/STALE atoms exist or claim needs revalidation, and requires ack for MIXED/USER_CONFIRMED", () => {
    const draftClaim = makeClaim({
      status: "DRAFT",
      computed_verification: "UNVERIFIED",
      approvable: false,
      needs_revalidation: true,
    });
    const draftGate = evaluateClaimApprovalGate(draftClaim, {
      acknowledgeUserConfirmed: false,
      workspaceMutationsLocked: false,
    });
    expect(draftGate.canApprove).toBe(false);
    expect(draftGate.needsRevalidation).toBe(true);

    const mixedClaim = makeClaim({
      computed_verification: "MIXED",
      user_confirmed_atom_count: 1,
    });
    expect(
      evaluateClaimApprovalGate(mixedClaim, {
        acknowledgeUserConfirmed: false,
        workspaceMutationsLocked: false,
      }).canApprove
    ).toBe(false);
    expect(
      evaluateClaimApprovalGate(mixedClaim, {
        acknowledgeUserConfirmed: true,
        workspaceMutationsLocked: false,
      }).canApprove
    ).toBe(true);
  });

  it("extracts CLAIM-06 6-element metric pills accurately", () => {
    const complete = extractMetricSixElementPills({
      value: "42",
      unit: "%",
      period: "30 days",
      source_basis: "PR benchmark",
      environment_scope: "PRODUCTION",
      measurement_scope: "gateway",
    });
    expect(complete.completeCount).toBe(6);
    expect(complete.isComplete).toBe(true);

    const incomplete = extractMetricSixElementPills({
      value: "100",
      unit: "%",
      environment_scope: "UNSPECIFIED",
    });
    expect(incomplete.completeCount).toBe(2);
    expect(incomplete.isComplete).toBe(false);
  });

  it("classifies all Plan C ephemeral source viewer states distinctly without claiming deletion/falsity on 404 or drift", () => {
    const matched = buildEphemeralViewerStateFromSuccess({
      content_unit_id: "cu_1",
      unit_type: "DIFF_HUNK",
      content: "ephemeral text",
      context: null,
      source_resolution: {
        status: "MATCHED",
        checked_at: "2026-11-28T04:00:00Z",
        expected_revision_hash: "sha256:aaa",
        observed_revision_hash: "sha256:aaa",
        retryable: false,
        reason_code: null,
      },
    });
    expect(matched.uiState).toBe("MATCHED");

    const drifted = buildEphemeralViewerStateFromError({
      kind: "source_revision_mismatch",
      code: "SOURCE_REVISION_MISMATCH",
      message: "drift",
      sourceResolution: {
        status: "REVISION_MISMATCH",
        checked_at: "2026-11-28T04:00:00Z",
        expected_revision_hash: "sha256:aaa",
        observed_revision_hash: "sha256:bbb",
        retryable: false,
        reason_code: "REVISION_HASH_MISMATCH",
      },
    });
    expect(drifted.uiState).toBe("CHANGED");
    expect(drifted.explanation).toContain("허위");

    const unavail404 = buildEphemeralViewerStateFromError({
      kind: "source_unavailable",
      code: "SOURCE_UNAVAILABLE",
      message: "provider 404",
      retryable: false,
      sourceResolution: {
        status: "UNAVAILABLE",
        checked_at: "2026-11-28T04:00:00Z",
        expected_revision_hash: "sha256:aaa",
        observed_revision_hash: null,
        retryable: false,
        reason_code: "PROVIDER_OBJECT_NOT_FOUND",
      },
    });
    expect(unavail404.uiState).toBe("UNAVAILABLE");
    expect(unavail404.canRetry).toBe(false);
    expect(unavail404.explanation).toContain("사용자 삭제");

    const unavail503 = buildEphemeralViewerStateFromError({
      kind: "source_unavailable",
      code: "SOURCE_UNAVAILABLE",
      message: "503",
      retryable: true,
      sourceResolution: {
        status: "UNAVAILABLE",
        checked_at: "2026-11-28T04:00:00Z",
        expected_revision_hash: "sha256:aaa",
        observed_revision_hash: null,
        retryable: true,
        reason_code: "PROVIDER_RATE_LIMIT_OR_503",
      },
    });
    expect(unavail503.uiState).toBe("UNAVAILABLE");
    expect(unavail503.canRetry).toBe(true);

    const policy = buildEphemeralViewerStateFromError({
      kind: "policy_blocked",
      code: "SOURCE_POLICY_BLOCKED",
      message: "blocked",
    });
    expect(policy.uiState).toBe("POLICY_BLOCKED");

    const limit = buildEphemeralViewerStateFromError({
      kind: "source_limit",
      code: "SOURCE_FETCH_LIMIT",
      message: "limit",
    });
    expect(limit.uiState).toBe("LIMIT");
  });

  it("enforces SOURCE_VERIFIED_ONLY vs ALLOW_USER_CONFIRMED export policy rules", () => {
    const mixedApproved: ClaimSummaryItem = {
      ...makeClaim({
        claim_id: "clm_m",
        status: "APPROVED",
        computed_verification: "MIXED",
      }),
    };
    expect(
      evaluateExportSelection([mixedApproved], "SOURCE_VERIFIED_ONLY").valid
    ).toBe(false);

    const allowed = evaluateExportSelection(
      [mixedApproved],
      "ALLOW_USER_CONFIRMED"
    );
    expect(allowed.valid).toBe(true);
    expect(allowed.requiresLimitationNotice).toBe(true);
  });
});
