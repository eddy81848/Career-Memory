import React from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type {
  DissolveWorkEventInput,
  EventReviewResult,
  MergeWorkEventsInput,
} from "@/adapters/event-review-adapter";
import { MockEventReviewAdapter } from "@/adapters/mock-event-review-adapter";
import { MockFixtureImportAdapter } from "@/adapters/mock-fixture-adapter";
import { ImportJourneyApp } from "@/components/ImportJourneyApp";
import type {
  DissolveWorkEventResponse,
  MergeWorkEventsResponse,
} from "@/domain/event-review-models";
import { EventInboxApp } from "./EventInboxApp";

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

afterEach(() => {
  cleanup();
});

describe("ImportJourneyApp -> Event Inbox entry gate (Product Spec §5)", () => {
  it("does NOT expose Event Inbox CTA for PARTIAL / FAILED / BLOCKED_POLICY, and enables it strictly after SUCCESS + import_ready=true", async () => {
    const user = userEvent.setup();
    const importAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "partial_non_retryable",
    });
    const eventAdapter = new MockEventReviewAdapter({ latencyMs: 0 });

    render(
      <ImportJourneyApp
        adapter={importAdapter}
        harness={importAdapter}
        eventReviewAdapter={eventAdapter}
        eventReviewHarness={eventAdapter}
        pollIntervalMs={20}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });
    expect(screen.queryByTestId("enter-event-inbox-cta")).not.toBeInTheDocument();
    expect(screen.queryByTestId("event-inbox-root")).not.toBeInTheDocument();

    // BLOCKED_POLICY must also keep Event Inbox blocked
    await user.click(screen.getByRole("button", { name: "진행 결과 초기화" }));
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "success_blocked_policy"
    );
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("processing-blocked-notice")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("enter-event-inbox-cta")).not.toBeInTheDocument();

    // SUCCESS + import_ready=true + COMPLETE enables Event Inbox CTA & tab switch
    await user.click(screen.getByRole("button", { name: "진행 결과 초기화" }));
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "success_full"
    );
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("enter-event-inbox-cta")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("enter-event-inbox-cta"));
    expect(await screen.findByTestId("event-inbox-root")).toBeInTheDocument();
    expect(await screen.findByTestId("active-event-count")).toHaveTextContent(
      "5건"
    );
  });
});

describe("EventInboxApp — M3 Curation UX & Invariants", () => {
  it("renders DIRECT_COMMIT badges with dynamic default_branch (main & trunk), abbreviated_sha, and NO_MATCH_OBSERVED notice", async () => {
    const user = userEvent.setup();
    const adapter = new MockEventReviewAdapter({ latencyMs: 0 });

    render(<EventInboxApp adapter={adapter} harness={adapter} />);

    // Select main DIRECT_COMMIT
    await user.click(await screen.findByTestId("select-event-evt_dc_209"));
    const dcBox = await screen.findByTestId("direct-commit-detail-box");
    expect(dcBox).toHaveTextContent("PR 없음 · default branch 직접 반영");
    expect(dcBox).toHaveTextContent("default_branch: main");
    expect(dcBox).toHaveTextContent("a8f3c1e");
    expect(dcBox).toHaveTextContent("PR 연결 미관측(NO_MATCH_OBSERVED) — 사용자 확인 필요");

    // Select trunk DIRECT_COMMIT (verifies non-hardcoded provider-reported default_branch)
    await user.click(screen.getByTestId("select-event-evt_dc_215"));
    await waitFor(() => {
      expect(screen.getByTestId("direct-commit-meta-chip")).toHaveTextContent(
        "default_branch: trunk"
      );
    });
    expect(screen.getByTestId("direct-commit-meta-chip")).toHaveTextContent(
      "9c4e2b0"
    );
    expect(
      screen.getByTestId("review-flag-REVIEW_REQUIRED_MULTI_PARENT")
    ).toBeInTheDocument();
  });

  it("strictly separates MUST_LINK evidence from CONTEXT_ONLY issues and renders Keep -> /actions/confirm, Edit, and Exclude non-deletion notice", async () => {
    const user = userEvent.setup();
    const adapter = new MockEventReviewAdapter({ latencyMs: 0 });

    render(<EventInboxApp adapter={adapter} harness={adapter} />);

    await screen.findByTestId("event-detail-panel");

    // Strict separation between MUST_LINK and CONTEXT_ONLY
    const mustLinkSection = screen.getByTestId("must-link-section");
    const contextOnlySection = screen.getByTestId("context-only-section");
    expect(mustLinkSection).toHaveTextContent("MUST_LINK");
    expect(mustLinkSection).toHaveTextContent("PR#104");
    expect(mustLinkSection).not.toHaveTextContent("ISSUE#41");
    expect(contextOnlySection).toHaveTextContent("참고 문맥 (Context Only — 소속 근거 아님");
    expect(contextOnlySection).toHaveTextContent("ISSUE#41");
    expect(contextOnlySection).toHaveTextContent("ISSUE#48");

    // Keep (확정) MUST call /actions/confirm (NEVER /keep)
    await user.click(screen.getByTestId("action-keep-button"));
    await waitFor(() => {
      expect(screen.getByTestId("detail-status-badge")).toHaveTextContent(
        "확정됨 (CONFIRMED)"
      );
    });
    expect(adapter.confirmCallCount).toBe(1);
    expect(adapter.lastMutationPath).toBe("/v1/work-events/evt_pr_104/actions/confirm");
    expect(adapter.requestedPaths.some((p) => p.includes("/keep"))).toBe(false);

    // Edit creates new CANDIDATE version
    await user.click(screen.getByTestId("action-edit-button"));
    const titleInput = screen.getByTestId("edit-title-input");
    await user.clear(titleInput);
    await user.type(titleInput, "PR #104 수정된 제목");
    await user.click(screen.getByTestId("submit-edit-button"));
    await waitFor(() => {
      expect(screen.getByTestId("detail-event-title")).toHaveTextContent(
        "PR #104 수정된 제목"
      );
    });
    expect(screen.getByTestId("detail-version-badge")).toHaveTextContent("v3");
    expect(screen.getByTestId("detail-status-badge")).toHaveTextContent(
      "검토 대기 (CANDIDATE)"
    );

    // Exclude shows explicit non-deletion notice and transitions to REJECTED
    await user.click(screen.getByTestId("action-exclude-button"));
    expect(screen.getByTestId("exclude-non-deletion-notice")).toHaveTextContent(
      "원본 GitHub 기록 및 출처 메타데이터는 삭제되지 않습니다 (Source 삭제와 별도)"
    );
    await user.click(screen.getByTestId("confirm-exclude-submit-button"));
    await waitFor(() => {
      expect(screen.getByTestId("detail-status-badge")).toHaveTextContent(
        "제외됨 (REJECTED)"
      );
    });
    expect(adapter.rejectCallCount).toBe(1);
  });

  it("enforces non-optimistic Merge & Dissolve (waits for server 200), EV-010 single count, member snapshots drilldown, and blocks nested USER_COMPOSITE & CANNOT_LINK", async () => {
    const user = userEvent.setup();
    const adapter = new MockEventReviewAdapter({ latencyMs: 0 });

    const mergeHold = createDeferred<EventReviewResult<MergeWorkEventsResponse>>();
    const originalMerge = adapter.mergeWorkEvents.bind(adapter);
    adapter.mergeWorkEvents = async (input: MergeWorkEventsInput) => {
      const result = await originalMerge(input);
      // Wait until test explicitly releases server 200
      await mergeHold.promise;
      return result;
    };

    const dissolveHold =
      createDeferred<EventReviewResult<DissolveWorkEventResponse>>();
    const originalDissolve = adapter.dissolveWorkEvent.bind(adapter);
    adapter.dissolveWorkEvent = async (input: DissolveWorkEventInput) => {
      const result = await originalDissolve(input);
      await dissolveHold.promise;
      return result;
    };

    render(<EventInboxApp adapter={adapter} harness={adapter} />);

    // Initially 5 active logical events (4 PR/DC + 1 USER_COMPOSITE; 2 SUPERSEDED members hidden)
    expect(await screen.findByTestId("active-event-count")).toHaveTextContent(
      "5건"
    );
    expect(
      screen.queryByTestId("event-list-item-evt_pr_088")
    ).not.toBeInTheDocument();

    // Verify nested USER_COMPOSITE merge is blocked
    await user.click(screen.getByTestId("merge-checkbox-evt_pr_104"));
    await user.click(screen.getByTestId("merge-checkbox-evt_comp_301"));
    expect(screen.getByTestId("merge-validation-error")).toHaveAttribute(
      "data-validation-code",
      "NESTED_COMPOSITE_FORBIDDEN"
    );
    expect(screen.getByTestId("open-merge-modal-button")).toBeDisabled();

    // Uncheck composite and verify CANNOT_LINK block (evt_pr_112 + evt_dc_215)
    await user.click(screen.getByTestId("merge-checkbox-evt_comp_301"));
    await user.click(screen.getByTestId("merge-checkbox-evt_pr_104"));
    await user.click(screen.getByTestId("merge-checkbox-evt_pr_112"));
    await user.click(screen.getByTestId("merge-checkbox-evt_dc_215"));
    await waitFor(() => {
      expect(screen.getByTestId("merge-validation-error")).toHaveAttribute(
        "data-validation-code",
        "CANNOT_LINK_CONFLICT"
      );
    });
    expect(screen.getByTestId("open-merge-modal-button")).toBeDisabled();

    // Clear and select valid pair: evt_pr_104 + evt_dc_209
    await user.click(screen.getByRole("button", { name: "선택 해제" }));
    await user.click(screen.getByTestId("merge-checkbox-evt_pr_104"));
    await user.click(screen.getByTestId("merge-checkbox-evt_dc_209"));

    await waitFor(() => {
      expect(screen.getByTestId("open-merge-modal-button")).toBeEnabled();
    });
    await user.click(screen.getByTestId("open-merge-modal-button"));

    // Pre-submit confirmation modal shows member versions and Fact/Claim/Export impact
    const mergeModal = screen.getByTestId("merge-confirm-modal");
    expect(
      within(mergeModal).getByTestId("merge-modal-member-versions")
    ).toHaveTextContent("evt_pr_104 · v1");
    expect(
      within(mergeModal).getByTestId("merge-modal-member-versions")
    ).toHaveTextContent("evt_dc_209 · v1");
    expect(
      within(mergeModal).getByTestId("merge-modal-impact-warning")
    ).toHaveTextContent("Fact 4건 · Claim 3건 · Export 1건");

    // Submit merge -> MUST stay in non-optimistic processing state until server 200 resolves!
    await user.click(screen.getByTestId("confirm-merge-submit-button"));
    expect(
      screen.getByTestId("merge-modal-processing-status")
    ).toHaveTextContent("병합 처리 중...");
    // Count MUST STILL be 5 (NOT optimistically decremented to 4!)
    expect(screen.getByTestId("active-event-count")).toHaveTextContent("5건");

    // Now resolve server 200 response
    mergeHold.resolve({ ok: true, data: {} as MergeWorkEventsResponse });
    await waitFor(() => {
      expect(screen.getByTestId("active-event-count")).toHaveTextContent("4건");
    });

    // Superseded members collapsed from default list, visible inside Member Snapshots drilldown
    expect(
      screen.queryByTestId("event-list-item-evt_pr_104")
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("event-list-item-evt_dc_209")
    ).not.toBeInTheDocument();
    const snapshots = screen.getByTestId("composite-member-snapshots");
    expect(snapshots).toHaveTextContent("evt_pr_104");
    expect(snapshots).toHaveTextContent("evt_dc_209");

    // Now test non-optimistic Dissolve on the newly created composite
    await user.click(screen.getByTestId("action-dissolve-button"));
    expect(screen.getByTestId("dissolve-confirm-modal")).toBeInTheDocument();
    await user.click(screen.getByTestId("confirm-dissolve-submit-button"));

    expect(
      screen.getByTestId("dissolve-modal-processing-status")
    ).toHaveTextContent("병합 해제 처리 중...");
    // Count MUST STILL be 4 before server 200 resolves!
    expect(screen.getByTestId("active-event-count")).toHaveTextContent("4건");

    dissolveHold.resolve({ ok: true, data: {} as DissolveWorkEventResponse });
    await waitFor(() => {
      expect(screen.getByTestId("active-event-count")).toHaveTextContent("5건");
    });
    expect(screen.getByTestId("event-list-item-evt_pr_104")).toBeInTheDocument();
    expect(screen.getByTestId("event-list-item-evt_dc_209")).toBeInTheDocument();
  });

  it("handles 409 EVENT_VERSION_CONFLICT without auto-POST and recovers via explicit reload latest button (ER-004)", async () => {
    const user = userEvent.setup();
    const adapter = new MockEventReviewAdapter({
      latencyMs: 0,
      initialScenario: "conflict_on_next_mutation",
    });

    render(<EventInboxApp adapter={adapter} harness={adapter} />);

    await screen.findByTestId("event-detail-panel");
    expect(screen.getByTestId("detail-version-badge")).toHaveTextContent("v1");

    // Attempt Keep -> receives 409 EVENT_VERSION_CONFLICT (server bumped to v2 CONFIRMED)
    await user.click(screen.getByTestId("action-keep-button"));
    const conflictBanner = await screen.findByTestId("version-conflict-banner");
    expect(conflictBanner).toHaveTextContent("409 EVENT_VERSION_CONFLICT");
    expect(screen.getByTestId("conflict-latest-meta")).toHaveTextContent(
      "최신 버전: v2"
    );
    // MUST NOT auto-POST a second confirm request!
    expect(adapter.confirmCallCount).toBe(1);

    // User clicks "최신 상태 다시 불러오기" -> read-only reload to v2
    await user.click(screen.getByTestId("reload-latest-after-conflict-button"));
    await waitFor(() => {
      expect(screen.queryByTestId("version-conflict-banner")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("detail-version-badge")).toHaveTextContent("v2");
    expect(adapter.confirmCallCount).toBe(1);
  });

  it("disables all mutating actions on POLICY_BLOCKED and DELETION_PENDING and distinguishes empty vs UNKNOWN states and composite regeneration failure", async () => {
    const user = userEvent.setup();
    const adapter = new MockEventReviewAdapter({
      latencyMs: 0,
      initialScenario: "policy_blocked",
    });

    render(<EventInboxApp adapter={adapter} harness={adapter} />);

    expect(
      await screen.findByTestId("inbox-policy-blocked-banner")
    ).toHaveTextContent("정책 차단 (POLICY_BLOCKED · 삭제 아님)");
    expect(screen.getByTestId("action-keep-button")).toBeDisabled();
    expect(screen.getByTestId("action-edit-button")).toBeDisabled();
    expect(screen.getByTestId("action-exclude-button")).toBeDisabled();
    expect(screen.getByTestId("action-split-button")).toBeDisabled();

    // Switch to DELETION_PENDING
    await user.selectOptions(
      screen.getByTestId("event-review-scenario-select"),
      "deletion_pending"
    );
    expect(
      await screen.findByTestId("inbox-deletion-pending-banner")
    ).toHaveTextContent("DELETION_PENDING");
    expect(screen.getByTestId("action-keep-button")).toBeDisabled();

    // Switch to regeneration_partial_failure -> check separate banner on USER_COMPOSITE
    await user.selectOptions(
      screen.getByTestId("event-review-scenario-select"),
      "regeneration_partial_failure"
    );
    await user.click(await screen.findByTestId("select-event-evt_comp_301"));
    const regenBanner = await screen.findByTestId(
      "composite-regeneration-status-banner"
    );
    expect(regenBanner).toHaveTextContent("USER_COMPOSITE 병합 커밋 성공");
    expect(regenBanner).toHaveTextContent("파생 재생성 상태: FAILED");

    // Switch to normal 0-event empty state
    await user.selectOptions(
      screen.getByTestId("event-review-scenario-select"),
      "empty_no_eligible_events"
    );
    expect(
      await screen.findByTestId("inbox-empty-eligible-state")
    ).toHaveTextContent("정상 빈 상태");

    // Switch to UNKNOWN incomplete association state
    await user.selectOptions(
      screen.getByTestId("event-review-scenario-select"),
      "association_unknown_incomplete"
    );
    expect(
      await screen.findByTestId("inbox-association-unknown-state")
    ).toHaveTextContent("PR_ASSOCIATION_UNKNOWN");
  });

  it("supports Split modal with disjoint validation and creates PR_SEGMENT candidates", async () => {
    const user = userEvent.setup();
    const adapter = new MockEventReviewAdapter({ latencyMs: 0 });

    render(<EventInboxApp adapter={adapter} harness={adapter} />);

    await screen.findByTestId("event-detail-panel");
    await user.click(screen.getByTestId("action-split-button"));
    expect(screen.getByTestId("split-event-modal")).toBeInTheDocument();

    // Default split is disjoint (2 + 2 evidence items) -> submit succeeds
    await user.click(screen.getByTestId("confirm-split-submit-button"));
    await waitFor(() => {
      expect(screen.queryByTestId("split-event-modal")).not.toBeInTheDocument();
    });
    expect(adapter.splitCallCount).toBe(1);
    // Original 5 active -> parent SUPERSEDED (-1) + 2 new PR_SEGMENTs (+2) = 6 active
    expect(screen.getByTestId("active-event-count")).toHaveTextContent("6건");
    expect(screen.getByTestId("detail-anchor-badge")).toHaveTextContent(
      "PR 범위 분할 (PR_SEGMENT)"
    );
  });
});
