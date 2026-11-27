import React from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type {
  AdapterResult,
  RunSyncPayload,
  StartInitialImportInput,
} from "@/adapters/import-service-adapter";
import { MockFixtureImportAdapter } from "@/adapters/mock-fixture-adapter";
import { ImportJourneyApp } from "./ImportJourneyApp";

/**
 * UI INTEGRATION TESTS (LOCAL UI FIXTURES ONLY).
 *
 * These tests validate UI view-model transitions, rendering guards,
 * keyboard accessibility, duplicate click suppression, server-gated retry
 * exposure, and reload recovery against the local mock fixture adapter.
 * They do NOT test real HTTP contracts, real OAuth/GitHub APIs, or Workers.
 */

afterEach(() => {
  cleanup();
});

function createControlledDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("ImportJourneyApp — UI fixture journey verification", () => {
  it("shows separated service login vs GitHub collection status and completes normal flow with import_ready and derived stages", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_full",
      initialGitHubStatus: "disconnected",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    // Service auth is active, while GitHub collection is separately disconnected
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그인 활성"
    );
    expect(screen.getByTestId("github-connection-badge")).toHaveTextContent(
      "미연결"
    );
    expect(screen.getByTestId("repo-selector-blocked")).toHaveTextContent(
      "GitHub 수집 연결이 필요합니다"
    );

    // Connect GitHub collection separately
    await user.click(screen.getByRole("button", { name: "GitHub 수집 연결" }));
    expect(await screen.findByTestId("github-connection-badge")).toHaveTextContent(
      "수집 연결됨"
    );

    // Select a different public repository via radio input and submit
    const secondRepoRadio = screen.getByRole("radio", {
      name: /seoyeon-lab-demo\/design-tokens-kit/i,
    });
    await user.click(secondRepoRadio);
    expect(secondRepoRadio).toBeChecked();

    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });

    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );
    expect(screen.getByTestId("derived-ready-badge")).toHaveTextContent(
      "파생 단계 전체 준비 완료"
    );
    expect(adapter.startImportCallCount).toBe(1);
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
  });

  it("clearly distinguishes import_ready=true from incomplete/failed derived stages", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_import_ready_derived_pending",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("derived-lag-notice")).toBeInTheDocument();
    });

    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );
    expect(screen.getByTestId("derived-ready-badge")).toHaveTextContent(
      "파생 단계 미완료"
    );
    expect(screen.getByTestId("derived-stage-timeline_indexing")).toHaveTextContent(
      "실패 (미준비)"
    );
  });

  it("blocks duplicate rapid clicks before server confirmation and while running, never displaying optimistic confirmation", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });

    const deferred = createControlledDeferred<AdapterResult<RunSyncPayload>>();
    const originalStart = adapter.startInitialImport.bind(adapter);
    let interceptedCalls = 0;

    adapter.startInitialImport = async (input: StartInitialImportInput) => {
      interceptedCalls += 1;
      const realResult = await originalStart(input);
      deferred.resolve(realResult);
      return deferred.promise;
    };

    // Wait manually using a second promise so we can inspect the in-flight unconfirmed state
    const hold = createControlledDeferred<void>();
    adapter.startInitialImport = async (input: StartInitialImportInput) => {
      interceptedCalls += 1;
      await hold.promise;
      return originalStart(input);
    };

    render(
      <ImportJourneyApp
        adapter={adapter}
        harness={adapter}
        pollIntervalMs={5_000}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    const startButton = screen.getByTestId("start-import-button");

    // Click twice rapidly while request is still awaiting server response
    await user.click(startButton);
    await user.click(startButton);

    // Must show unconfirmed pending banner and duplicate guard notice; NO active run confirmed yet!
    expect(screen.getByTestId("pending-status-banner")).toHaveTextContent(
      "요청 전송 중 — 서버 미확정"
    );
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(screen.getByTestId("duplicate-guard-alert")).toHaveTextContent(
      "중복 요청을 방지했습니다"
    );
    expect(interceptedCalls).toBe(1);

    // Resolve server acknowledgement -> transitions to running
    hold.resolve();
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });

    // Clicking again while running must also be suppressed without another adapter call
    await user.click(startButton);
    expect(screen.getByTestId("duplicate-guard-alert")).toHaveTextContent(
      "중복 실행을 방지했으며"
    );
    expect(interceptedCalls).toBe(1);
    expect(adapter.startImportCallCount).toBe(1);
  });

  it("displays partial results as warning (NOT full success) and gates retry strictly by server retryPolicy.allowed", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "partial_non_retryable",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });

    expect(screen.getByTestId("run-status-badge")).toHaveTextContent(
      "부분 결과 — 전체 성공 아님"
    );
    expect(screen.getByTestId("partial-detail-box")).toBeInTheDocument();
    // Server denied retry -> retry button MUST NOT be exposed
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("retry-policy-reason")).toHaveTextContent(
      "재시도 불가"
    );

    // Now reset and test partial_retryable -> retry button MUST appear and recover
    await user.click(screen.getByRole("button", { name: "진행 결과 초기화" }));
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "partial_retryable"
    );
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });

    const retryButton = screen.getByTestId("retry-import-button");
    expect(retryButton).toBeEnabled();
    await user.click(retryButton);

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(adapter.retryImportCallCount).toBe(1);
  });

  it("displays limit_reached (cap exceeded) distinctly from success and hides retry when server disallows", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "limit_cap_blocked",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /telemetry-cli/i });
    await user.click(screen.getByRole("radio", { name: /telemetry-cli/i }));
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "limit_reached"
      );
    });

    expect(screen.getByTestId("run-status-badge")).toHaveTextContent(
      "한도 도달 — 제한됨"
    );
    expect(screen.getByTestId("limit-detail-box")).toHaveTextContent(
      "SYNC_PARTIAL_WITH_LIMIT"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
  });

  it("exposes retry on retryable server error and hides retry on non-retryable server error", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "error_non_retryable",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "failed"
      );
    });
    expect(screen.getByTestId("error-detail-box")).toHaveTextContent(
      "MOCK_REPO_UNSUPPORTED_STRUCTURE"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();

    // Switch to retryable error
    await user.click(screen.getByRole("button", { name: "진행 결과 초기화" }));
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "error_retryable"
    );
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("retry-import-button")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("retry-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
  });

  it("handles GitHub permission revocation without exposing retry and restores after reconnect", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "permission_revoked",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "permission_revoked"
      );
    });

    // Service auth stays active while GitHub connection becomes revoked
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent(
      "로그인 활성"
    );
    expect(screen.getByTestId("github-connection-badge")).toHaveTextContent(
      "권한 상실"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();

    // Reconnect GitHub via dedicated CTA
    await user.click(screen.getByTestId("reconnect-github-from-run-button"));
    await waitFor(() => {
      expect(screen.getByTestId("github-connection-badge")).toHaveTextContent(
        "수집 연결됨"
      );
    });
  });

  it("shows explicit unconfirmed guidance on session expiration and network failure", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "session_expired_on_submit",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "세션 만료 — 요청 미확정"
      );
    });
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent(
      "세션 만료"
    );
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();

    // Re-authenticate service and verify network_error_on_submit
    await user.click(
      screen.getAllByRole("button", { name: "모의 서비스 다시 로그인" })[0]
    );
    await waitFor(() => {
      expect(screen.getByTestId("service-auth-badge")).toHaveTextContent(
        "로그인 활성"
      );
    });

    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "network_error_on_submit"
    );
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "네트워크 실패 — 서버 미확정"
      );
    });
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
  });

  it("recovers an in-progress run after reload simulation without issuing a duplicate import request", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });

    render(
      <ImportJourneyApp
        adapter={adapter}
        harness={adapter}
        pollIntervalMs={5_000}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });
    expect(adapter.startImportCallCount).toBe(1);

    // Trigger reload recovery
    await user.click(screen.getByRole("button", { name: "새로고침 복구 확인" }));

    await waitFor(() => {
      expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
        "중복 요청을 다시 보내지 않고"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "running"
    );
    expect(adapter.startImportCallCount).toBe(1);
  });

  it("supports keyboard-only selection and import trigger without a mouse", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_full",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    const firstRadio = await screen.findByRole("radio", {
      name: /ledger-engine/i,
    });
    firstRadio.focus();
    expect(firstRadio).toHaveFocus();

    // Move to second repository using ArrowDown key only
    await user.keyboard("{ArrowDown}");
    const secondRadio = screen.getByRole("radio", {
      name: /design-tokens-kit/i,
    });
    expect(secondRadio).toBeChecked();

    // Move focus to submit button and activate with Enter key
    const submitBtn = screen.getByTestId("start-import-button");
    submitBtn.focus();
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "seoyeon-lab-demo/design-tokens-kit"
    );
  });

  it("keeps polling and eventually succeeds when server returns identical runId, status, and updatedAt multiple times", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_full",
    });

    const FIXED_TIMESTAMP = "2026-11-26T12:00:00.000Z";
    let identicalPollCalls = 0;

    const originalStart = adapter.startInitialImport.bind(adapter);
    let frozenRunningRun: RunSyncPayload["run"] | null = null;

    adapter.startInitialImport = async (input: StartInitialImportInput) => {
      const res = await originalStart(input);
      if (res.ok) {
        frozenRunningRun = {
          ...res.data.run,
          startedAt: FIXED_TIMESTAMP,
          updatedAt: FIXED_TIMESTAMP,
        };
        return {
          ok: true,
          data: {
            ...res.data,
            run: structuredClone(frozenRunningRun),
          },
        };
      }
      return res;
    };

    adapter.pollImportRun = async () => {
      identicalPollCalls += 1;
      // Return identical runId, status="running", and updatedAt twice in a row
      if (identicalPollCalls <= 2 && frozenRunningRun) {
        return {
          ok: true,
          data: {
            run: structuredClone(frozenRunningRun),
          },
        };
      }

      return {
        ok: true,
        data: {
          run: {
            ...frozenRunningRun!,
            status: "succeeded",
            updatedAt: "2026-11-26T12:00:05.000Z",
            progressMessage: "동일 응답 반복 후 최종 완료",
            readiness: {
              importReady: true,
              importReadyState: "ready",
              importReadySummary: "원본 스냅샷 완료",
              derivedAllReady: true,
              derivedStages: [
                {
                  key: "event_normalization",
                  label: "작업 이벤트 정규화",
                  status: "ready",
                  detail: "완료",
                },
                {
                  key: "timeline_indexing",
                  label: "타임라인 준비",
                  status: "ready",
                  detail: "완료",
                },
              ],
            },
          },
        },
      };
    };

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(
      () => {
        expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
          "data-run-status",
          "succeeded"
        );
      },
      { timeout: 600 }
    );

    expect(identicalPollCalls).toBe(3);
  });

  it("enforces strictly single-flight polling even when timer elapses and manual refresh is clicked", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });

    let activePolls = 0;
    let maxConcurrentPolls = 0;
    let totalPolls = 0;
    const firstPollGate = createControlledDeferred<void>();
    const originalPoll = adapter.pollImportRun.bind(adapter);

    adapter.pollImportRun = async (runId: string) => {
      totalPolls += 1;
      activePolls += 1;
      maxConcurrentPolls = Math.max(maxConcurrentPolls, activePolls);
      try {
        if (totalPolls === 1) {
          await firstPollGate.promise;
        }
        return await originalPoll(runId);
      } finally {
        activePolls -= 1;
      }
    };

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(totalPolls).toBe(1);
    });

    // While poll #1 is still in flight, click manual refresh and wait past pollIntervalMs
    await user.click(screen.getByRole("button", { name: "상태 지금 다시 확인" }));
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(totalPolls).toBe(1);
    expect(maxConcurrentPolls).toBe(1);

    // Release poll #1 and allow run to complete cleanly
    firstPollGate.resolve();
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(maxConcurrentPolls).toBe(1);

    // Verify polling stops completely after terminal state
    const callsAtTerminal = totalPolls;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(totalPolls).toBe(callsAtTerminal);
  });

  it("stops polling immediately on session expiration, permission revocation, and screen unmount", async () => {
    const user = userEvent.setup();

    // Case A: Session expiration stops polling
    const adapterSession = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });
    const sessionRender = render(
      <ImportJourneyApp
        adapter={adapterSession}
        harness={adapterSession}
        pollIntervalMs={20}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });
    await user.click(screen.getByRole("button", { name: "세션 만료 시뮬레이션" }));
    await waitFor(() => {
      expect(screen.getByTestId("service-auth-badge")).toHaveTextContent(
        "세션 만료"
      );
    });
    const pollsAfterSessionExpired = adapterSession.pollCallCount;
    await new Promise((resolve) => setTimeout(resolve, 70));
    expect(adapterSession.pollCallCount).toBe(pollsAfterSessionExpired);
    sessionRender.unmount();

    // Case B: Permission revocation stops polling
    const adapterRevoked = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });
    const revokedRender = render(
      <ImportJourneyApp
        adapter={adapterRevoked}
        harness={adapterRevoked}
        pollIntervalMs={20}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });
    await user.click(screen.getByRole("button", { name: "권한 상실 시뮬레이션" }));
    await waitFor(() => {
      expect(screen.getByTestId("github-connection-badge")).toHaveTextContent(
        "권한 상실"
      );
    });
    const pollsAfterRevoked = adapterRevoked.pollCallCount;
    await new Promise((resolve) => setTimeout(resolve, 70));
    expect(adapterRevoked.pollCallCount).toBe(pollsAfterRevoked);
    revokedRender.unmount();

    // Case C: Screen unmount cancels pending polling timer
    const adapterUnmount = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });
    const unmountRender = render(
      <ImportJourneyApp
        adapter={adapterUnmount}
        harness={adapterUnmount}
        pollIntervalMs={25}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });
    const pollsBeforeUnmount = adapterUnmount.pollCallCount;
    unmountRender.unmount();
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(adapterUnmount.pollCallCount).toBe(pollsBeforeUnmount);
  });

  it("ignores a late poll response from a previous run so it never overwrites the current run", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });

    const stalePollGate = createControlledDeferred<void>();
    let staleRunIdCaptured: string | null = null;
    const originalBootstrap = adapter.bootstrapSession.bind(adapter);

    adapter.pollImportRun = async (runId: string) => {
      staleRunIdCaptured = runId;
      await stalePollGate.promise;
      // Late response from the old run arrives with failed status
      return {
        ok: true,
        data: {
          run: {
            runId,
            repositoryId: "repo_demo_ledger_01",
            repositoryFullName: "seoyeon-lab-demo/ledger-engine",
            status: "failed",
            attemptNumber: 1,
            startedAt: "2026-11-26T12:00:00.000Z",
            updatedAt: "2026-11-26T12:00:02.000Z",
            progressMessage: "이전 작업의 늦은 실패 응답",
            readiness: {
              importReady: false,
              importReadyState: "failed",
              importReadySummary: "실패",
              derivedAllReady: false,
              derivedStages: [],
            },
            retryPolicy: {
              allowed: false,
              reason: "실패",
              nextAllowedAt: null,
            },
            summary: null,
            partialDetail: null,
            limitDetail: null,
            error: {
              code: "MOCK_STALE_LATE_FAILURE",
              title: "늦게 도착한 이전 작업 오류",
              message: "현재 화면을 덮어쓰면 안 됩니다.",
              actionGuidance: "무시되어야 함",
            },
          },
        },
      };
    };

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(staleRunIdCaptured).not.toBeNull();
    });

    // While run_demo_101 poll is still pending, reload recovery returns a newer succeeded run
    adapter.bootstrapSession = async (options) => {
      const base = await originalBootstrap(options);
      if (!base.ok) return base;
      return {
        ok: true,
        data: {
          ...base.data,
          latestRun: {
            runId: "run_demo_202",
            repositoryId: "repo_demo_ledger_01",
            repositoryFullName: "seoyeon-lab-demo/ledger-engine",
            status: "succeeded",
            attemptNumber: 2,
            startedAt: "2026-11-26T12:01:00.000Z",
            updatedAt: "2026-11-26T12:01:10.000Z",
            progressMessage: "최신 작업 완료",
            readiness: {
              importReady: true,
              importReadyState: "ready",
              importReadySummary: "원본 스냅샷 완료",
              derivedAllReady: true,
              derivedStages: [
                {
                  key: "event_normalization",
                  label: "작업 이벤트 정규화",
                  status: "ready",
                  detail: "완료",
                },
                {
                  key: "timeline_indexing",
                  label: "타임라인 준비",
                  status: "ready",
                  detail: "완료",
                },
              ],
            },
            retryPolicy: {
              allowed: false,
              reason: "이미 완료",
              nextAllowedAt: null,
            },
            summary: {
              commitsCaptured: 142,
              pullRequestsCaptured: 28,
              capturedAt: "2026-11-26T12:01:10.000Z",
            },
            partialDetail: null,
            limitDetail: null,
            error: null,
          },
        },
      };
    };

    await user.click(screen.getByRole("button", { name: "새로고침 복구 확인" }));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "run_demo_202"
      );
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });

    // Now release the late poll response from run_demo_101
    stalePollGate.resolve();
    await new Promise((resolve) => setTimeout(resolve, 40));

    // Current run_demo_202 succeeded screen must remain intact!
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "run_demo_202"
    );
    expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "succeeded"
    );
    expect(screen.queryByTestId("error-detail-box")).not.toBeInTheDocument();
  });

  it("does not automatically start a new import when status polling fails with a network error", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "running_slow_for_reload",
    });

    render(
      <ImportJourneyApp
        adapter={adapter}
        harness={adapter}
        pollIntervalMs={5_000}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });
    expect(adapter.startImportCallCount).toBe(1);

    // Inject network error on status poll
    await user.click(
      screen.getByRole("button", { name: "다음 조회 네트워크 오류 주입" })
    );

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "상태 조회 중 네트워크 실패"
      );
    });

    // Wait briefly and assert NO automatic new import request was created
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(adapter.startImportCallCount).toBe(1);
  });

  it("marks import_ready=false for both PARTIAL and PARTIAL_WITH_LIMIT outcomes", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "partial_non_retryable",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: false (부분 수집 — 미준비)"
    );

    await user.click(screen.getByRole("button", { name: "진행 결과 초기화" }));
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "limit_cap_blocked"
    );
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "limit_reached"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: false (부분 수집 — 미준비)"
    );
  });

  it("separates historical SUCCESS (import_ready=true) from current BLOCKED_POLICY and BLOCKED_SOURCE in UI", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_blocked_policy",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("processing-blocked-notice")).toBeInTheDocument();
    });

    // Historical import history is preserved as SUCCESS / import_ready=true
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );
    // Current processing blocked state is shown separately
    expect(screen.getByTestId("processing-status-badge")).toHaveTextContent(
      "BLOCKED_POLICY"
    );
    expect(screen.getByTestId("derived-ready-badge")).toHaveTextContent(
      "현재 처리 차단 (BLOCKED_POLICY)"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();

    // Also verify BLOCKED_SOURCE
    await user.click(screen.getByRole("button", { name: "진행 결과 초기화" }));
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "success_blocked_source"
    );
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("processing-status-badge")).toHaveTextContent(
        "BLOCKED_SOURCE"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );
    expect(screen.getByTestId("derived-ready-badge")).toHaveTextContent(
      "현재 처리 차단 (BLOCKED_SOURCE)"
    );
  });

  it("moves UI to existing run on 409 SYNC_ALREADY_RUNNING only when existing_run_id is provided", async () => {
    const user = userEvent.setup();

    // Case 1: 409 with existing_run_id -> navigates to synthetic-run-existing-1 and completes
    const adapterWithId = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "conflict_existing_run",
    });

    const first = render(
      <ImportJourneyApp
        adapter={adapterWithId}
        harness={adapterWithId}
        pollIntervalMs={15}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
        "synthetic-run-existing-1"
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "synthetic-run-existing-1"
    );
    first.unmount();

    // Case 2: 409 SYNC_ALREADY_RUNNING without existing_run_id -> stays without moving
    const adapterWithoutId = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send() {
          return {
            status: 409,
            rawBody: JSON.stringify({
              error: { code: "SYNC_ALREADY_RUNNING" },
            }),
          };
        },
      },
    });

    render(
      <ImportJourneyApp
        adapter={adapterWithoutId}
        harness={adapterWithoutId}
        pollIntervalMs={15}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "SYNC_ALREADY_RUNNING"
      );
    });
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
  });

  it("renders neutral generic guidance for bodyless 403, 409, and 422 without JSON parse errors", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "empty_403",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "HTTP 403"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "세부 사유를 특정할 수 없습니다"
    );
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();

    // Empty 409
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "empty_409"
    );
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "HTTP 409"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "이유를 단정할 수 없습니다"
    );

    // Empty 422
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "empty_422"
    );
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "HTTP 422"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "세부 원인은 응답에 포함되지 않았습니다"
    );
  });

  it("reuses the exact same UUID Idempotency-Key when user resends after a lost submit response", async () => {
    const user = userEvent.setup();
    const capturedKeys: string[] = [];
    let attempt = 0;

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            capturedKeys.push(req.headers["Idempotency-Key"]);
            attempt += 1;
            if (attempt === 1) {
              throw new Error("LOST_SUBMIT_RESPONSE");
            }
            return {
              status: 202,
              rawBody: JSON.stringify({
                target_id: "synthetic-run-resend-1",
                status_url: "/v1/sync-runs/synthetic-run-resend-1",
              }),
            };
          }
          return {
            status: 200,
            rawBody: JSON.stringify({
              status: "SUCCESS",
              plan_id: "synthetic-plan-resend-1",
              import_ready: true,
              processing_status: "COMPLETE",
              source_drift: false,
              retry: { allowed: false },
            }),
          };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "네트워크 실패 — 서버 미확정"
      );
    });

    // Resend exact same submit -> must reuse the same UUID key and succeed
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });

    expect(capturedKeys).toHaveLength(2);
    expect(capturedKeys[0]).toBe(capturedKeys[1]);
  });

  it("shows 502 on create as uncertain_outcome without auto-POST and reuses Idempotency-Key on explicit resend (server already accepted)", async () => {
    const user = userEvent.setup();
    const capturedKeys: string[] = [];
    let acceptedKey: string | null = null;
    const committedRunId = "synthetic-run-ui-502-replay";

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            const key = req.headers["Idempotency-Key"];
            capturedKeys.push(key);
            if (!acceptedKey) {
              acceptedKey = key;
              return { status: 502, rawBody: "Bad Gateway" };
            }
            if (key === acceptedKey) {
              return {
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_id: committedRunId,
                  status_url: `/v1/sync-runs/${committedRunId}`,
                }),
              };
            }
            return {
              status: 409,
              rawBody: JSON.stringify({
                error: { code: "SYNC_ALREADY_RUNNING" },
              }),
            };
          }
          if (req.method === "GET" && req.path === `/v1/sync-runs/${committedRunId}`) {
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "SUCCESS",
                plan_id: "synthetic-plan-ui-502",
                import_ready: true,
                processing_status: "COMPLETE",
                source_drift: false,
                retry: { allowed: false },
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "uncertain_outcome"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "처리 여부 미확정 (HTTP 502)"
    );
    // Must NOT display as success or confirmed failure
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();

    // Wait briefly and verify zero automatic POST occurred
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(capturedKeys).toHaveLength(1);

    // Explicit user resend reuses exact same Idempotency-Key and replays 202 -> succeeds
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(capturedKeys).toHaveLength(2);
    expect(capturedKeys[1]).toBe(capturedKeys[0]);
  });

  it("handles versioned retry 502 and mismatched retry 202 without moving runs or auto-POSTing, then succeeds on explicit resend with same key and original body", async () => {
    const user = userEvent.setup();
    const originalRunId = "synthetic-run-ui-retry-guard";
    const retryKeys: string[] = [];
    const retryBodies: Array<string | undefined> = [];
    const getPaths: string[] = [];
    let retryAttempt = 0;

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: originalRunId,
                status_url: `/v1/sync-runs/${originalRunId}`,
              }),
            };
          }
          if (req.method === "POST" && req.path === `/v1/sync-runs/${originalRunId}/retry`) {
            retryKeys.push(req.headers["Idempotency-Key"]);
            retryBodies.push(req.body);
            retryAttempt += 1;
            if (retryAttempt === 1) {
              return { status: 502, rawBody: "" };
            }
            if (retryAttempt === 2) {
              // Mismatched target_id pointing to a different run!
              return {
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_id: "synthetic-run-hijack-999",
                  status_url: "/v1/sync-runs/synthetic-run-hijack-999",
                }),
              };
            }
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: originalRunId,
                status_url: `/v1/sync-runs/${originalRunId}`,
              }),
            };
          }
          if (req.method === "GET") {
            getPaths.push(req.path);
            if (retryAttempt < 3) {
              return {
                status: 200,
                rawBody: JSON.stringify({
                  status: "PARTIAL",
                  run_version: 3,
                  plan_id: "synthetic-plan-retry-guard",
                  failure_reason: "TRANSIENT_PROVIDER_ERROR",
                  import_ready: false,
                  processing_status: "NOT_STARTED",
                  source_drift: false,
                  retry: { allowed: true, from_safe_checkpoint: true },
                }),
              };
            }
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "SUCCESS",
                run_version: 5,
                plan_id: "synthetic-plan-retry-guard",
                import_ready: true,
                processing_status: "COMPLETE",
                source_drift: false,
                retry: { allowed: false },
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });

    // 1st explicit versioned retry -> 502 uncertain_outcome, stays on originalRunId, exposes separate resend button
    await user.click(screen.getByTestId("retry-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "처리 여부 미확정 (HTTP 502)"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(originalRunId);
    expect(screen.getByTestId("resend-pending-retry-button")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(retryKeys).toHaveLength(1);

    // 2nd explicit resend -> mismatched 202 (other run), must NOT navigate or poll other run
    await user.click(screen.getByTestId("resend-pending-retry-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "처리 여부 미확정 (HTTP 202)"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(originalRunId);
    expect(screen.getByTestId("run-status-banner")).not.toHaveTextContent(
      "synthetic-run-hijack-999"
    );
    expect(getPaths).not.toContain("/v1/sync-runs/synthetic-run-hijack-999");

    // 3rd explicit resend -> valid 202 for originalRunId -> succeeds
    await user.click(screen.getByTestId("resend-pending-retry-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });

    expect(retryKeys).toHaveLength(3);
    expect(retryKeys[1]).toBe(retryKeys[0]);
    expect(retryKeys[2]).toBe(retryKeys[0]);
    expect(retryBodies).toEqual([
      JSON.stringify({ expected_run_version: 3 }),
      JSON.stringify({ expected_run_version: 3 }),
      JSON.stringify({ expected_run_version: 3 }),
    ]);
    expect(getPaths).not.toContain("/v1/sync-runs/synthetic-run-hijack-999");
  });

  it("rotates Idempotency-Key after auth scope change (sign out and re-login) following a 502", async () => {
    const user = userEvent.setup();
    const capturedKeys: string[] = [];

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            capturedKeys.push(req.headers["Idempotency-Key"]);
            if (capturedKeys.length === 1) {
              return { status: 502, rawBody: "" };
            }
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: "synthetic-run-new-session",
                status_url: "/v1/sync-runs/synthetic-run-new-session",
              }),
            };
          }
          return {
            status: 200,
            rawBody: JSON.stringify({
              status: "SUCCESS",
              plan_id: "synthetic-plan-new-session",
              import_ready: true,
              processing_status: "COMPLETE",
              source_drift: false,
              retry: { allowed: false },
            }),
          };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "uncertain_outcome"
      );
    });

    // Change auth scope: sign out then sign back in
    await user.click(screen.getByRole("button", { name: "모의 로그아웃" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그아웃"
    );
    await user.click(screen.getByRole("button", { name: "모의 서비스 로그인" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그인 활성"
    );

    // Resend in the NEW auth scope -> MUST NOT reuse the old session's key
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });

    expect(capturedKeys).toHaveLength(2);
    expect(capturedKeys[1]).not.toBe(capturedKeys[0]);
  });

  it("blocks polling mismatched status_url on create 202 in UI and recovers on explicit resend with same key", async () => {
    const user = userEvent.setup();
    const capturedKeys: string[] = [];
    const getPaths: string[] = [];

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return {
              status: 200,
              rawBody: JSON.stringify({ items: [] }),
            };
          }
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            capturedKeys.push(req.headers["Idempotency-Key"]);
            if (capturedKeys.length === 1) {
              return {
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_id: "synthetic-run-real",
                  status_url: "/v1/sync-runs/synthetic-run-wrong",
                }),
              };
            }
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: "synthetic-run-real",
                status_url: "/v1/sync-runs/synthetic-run-real",
              }),
            };
          }
          if (req.method === "GET") {
            getPaths.push(req.path);
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "SUCCESS",
                plan_id: "synthetic-plan-real",
                import_ready: true,
                processing_status: "COMPLETE",
                source_drift: false,
                retry: { allowed: false },
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "접수 응답 경로 불일치"
      );
    });
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(getPaths).toHaveLength(0);

    // Explicit resend reuses same key and polls ONLY the matching status_url
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });

    expect(capturedKeys).toHaveLength(2);
    expect(capturedKeys[1]).toBe(capturedKeys[0]);
    expect(getPaths).toEqual(["/v1/sync-runs/synthetic-run-real"]);
  });

  it("ignores late 202 and late 401 from an old session after logout/session change without altering new session UI, stored run, or pending key", async () => {
    const user = userEvent.setup();
    window.sessionStorage.clear();

    const lateGates: Array<() => void> = [];
    const postKeys: Array<string | undefined> = [];
    let callIndex = 0;

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            callIndex += 1;
            const currentCall = callIndex;
            postKeys.push(req.headers["Idempotency-Key"]);

            if (currentCall === 1) {
              // Session 1 create -> delayed 202
              await new Promise<void>((resolve) => {
                lateGates.push(resolve);
              });
              return {
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_type: "SYNC_RUN",
                  target_id: "synthetic-run-old-202",
                  status_url: "/v1/sync-runs/synthetic-run-old-202",
                }),
              };
            }

            if (currentCall === 2) {
              // Session 2 create -> immediate 502 so Session 2 holds a pending create key
              return { status: 502, rawBody: "" };
            }

            if (currentCall === 3) {
              // Session 2 resend -> delayed 401
              await new Promise<void>((resolve) => {
                lateGates.push(resolve);
              });
              return { status: 401, rawBody: "" };
            }

            // Session 3 create -> immediate 202
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_type: "SYNC_RUN",
                target_id: "synthetic-run-session-3",
                status_url: "/v1/sync-runs/synthetic-run-session-3",
              }),
            };
          }

          if (req.method === "GET" && req.path === "/v1/sync-runs/synthetic-run-session-3") {
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "SUCCESS",
                plan_id: "synthetic-plan-s3",
                import_ready: true,
                processing_status: "COMPLETE",
                source_drift: false,
                retry: { allowed: false },
              }),
            };
          }

          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });

    // Session 1 starts import (held in-flight)
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(lateGates).toHaveLength(1);
    });

    // Logout & re-login -> Session 2
    await user.click(screen.getByRole("button", { name: "모의 로그아웃" }));
    await user.click(screen.getByRole("button", { name: "모의 서비스 로그인" }));

    // Session 2 submits -> gets 502 and keeps a pending create key
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "HTTP 502"
      );
    });

    // Release Session 1's late 202
    lateGates[0]();
    await new Promise((resolve) => setTimeout(resolve, 40));

    // Late 202 must NOT show synthetic-run-old-202, store it, or wipe Session 2's pending key
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    const afterLate202 = await adapter.bootstrapSession();
    expect(afterLate202.ok && afterLate202.data.latestRun).toBeNull();
    expect(
      window.sessionStorage.getItem("career_memory_spike_web_import_v1") ?? ""
    ).not.toContain("synthetic-run-old-202");

    // Session 2 explicitly resends (must reuse Session 2's key, NOT wiped by late 202)
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(lateGates).toHaveLength(2);
    });
    expect(postKeys[2]).toBe(postKeys[1]);
    expect(postKeys[1]).not.toBe(postKeys[0]);

    // Logout & re-login -> Session 3 while Session 2's 401 is in flight
    await user.click(screen.getByRole("button", { name: "모의 로그아웃" }));
    await user.click(screen.getByRole("button", { name: "모의 서비스 로그인" }));

    // Session 3 starts import and succeeds
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "synthetic-run-session-3"
    );
    expect(postKeys[3]).not.toBe(postKeys[1]);

    // Release Session 2's late 401
    lateGates[1]();
    await new Promise((resolve) => setTimeout(resolve, 40));

    // Session 3 must remain authenticated with synthetic-run-session-3 intact
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("로그인 활성");
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "synthetic-run-session-3"
    );
    const afterLate401 = await adapter.bootstrapSession();
    expect(afterLate401.ok ? afterLate401.data.latestRun?.runId : null).toBe(
      "synthetic-run-session-3"
    );
    window.sessionStorage.clear();
  });

  it("discards a late recovery GET response when the user switches repository while lookup is in flight", async () => {
    const user = userEvent.setup();
    let releaseRepoALookup: (() => void) | null = null;
    let recoveryCall = 0;
    const postCalls: string[] = [];

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST") {
            postCalls.push(req.path);
          }
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            recoveryCall += 1;
            if (recoveryCall === 1) {
              // Initial mount empty
              return { status: 200, rawBody: JSON.stringify({ items: [] }) };
            }
            // Explicit check on Repo A -> slow response
            await new Promise<void>((resolve) => {
              releaseRepoALookup = resolve;
            });
            return {
              status: 200,
              rawBody: JSON.stringify({
                items: [
                  {
                    target_id: "synthetic-run-repo-a-stale",
                    status_url: "/v1/sync-runs/synthetic-run-repo-a-stale",
                    import_status: {
                      status: "RUNNING",
                      plan_id: "synthetic-plan-stale",
                      import_ready: false,
                      processing_status: "NOT_STARTED",
                      source_drift: false,
                      retry: { allowed: false },
                    },
                  },
                ],
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    const repoBRadio = await screen.findByRole("radio", {
      name: /design-tokens-kit/i,
    });
    await user.click(screen.getByTestId("check-repository-status-button"));

    await waitFor(() => {
      expect(releaseRepoALookup).not.toBeNull();
    });

    // Switch to Repo B while Repo A's recovery GET is still in flight
    await user.click(repoBRadio);
    expect(repoBRadio).toBeChecked();

    // Release Repo A's late response -> MUST be discarded!
    releaseRepoALookup!();
    await new Promise((resolve) => setTimeout(resolve, 40));

    expect(repoBRadio).toBeChecked();
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(postCalls).toHaveLength(0);
  });

  it("refreshes a terminal SUCCESS run via detail GET to reflect post-SUCCESS BLOCKED_POLICY without restarting auto-polling", async () => {
    const user = userEvent.setup();
    const detailGetPaths: string[] = [];
    let detailCalls = 0;

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return {
              status: 200,
              rawBody: JSON.stringify({
                items: [
                  {
                    target_id: "synthetic-run-term-06",
                    status_url: "/v1/sync-runs/synthetic-run-term-06",
                    import_status: {
                      status: "SUCCESS",
                      plan_id: "synthetic-plan-term-06",
                      import_ready: true,
                      processing_status: "COMPLETE",
                      source_drift: false,
                      retry: { allowed: false },
                    },
                  },
                ],
              }),
            };
          }
          if (req.method === "GET" && req.path === "/v1/sync-runs/synthetic-run-term-06") {
            detailCalls += 1;
            detailGetPaths.push(req.path);
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "SUCCESS",
                plan_id: "synthetic-plan-term-06",
                import_ready: true,
                processing_status: "BLOCKED_POLICY",
                source_drift: false,
                retry: { allowed: false },
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
      "이전 이력일 수 있으므로"
    );

    // Terminal run recovered -> NO automatic detail polling should happen
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(detailCalls).toBe(0);

    // Manual detail re-lookup reflects post-SUCCESS BLOCKED_POLICY
    await user.click(screen.getByTestId("manual-refresh-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("processing-blocked-notice")).toHaveTextContent(
        "BLOCKED_POLICY"
      );
    });
    expect(detailGetPaths).toEqual(["/v1/sync-runs/synthetic-run-term-06"]);

    // Still terminal -> NO automatic polling restarts after manual refresh
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(detailCalls).toBe(1);
  });

  it("blocks additional startInitialImport calls on the same succeeded repository (CTA + handler) while keeping manual status GET and other repositories unblocked", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_full",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    const startButton = screen.getByTestId("start-import-button");
    await user.click(startButton);

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(adapter.startImportCallCount).toBe(1);

    // Button remains visible (not hidden), disabled, and aria-disabled for the same succeeded repo
    expect(startButton).toBeVisible();
    expect(startButton).toBeDisabled();
    expect(startButton).toHaveAttribute("aria-disabled", "true");

    // Clicking disabled CTA emits 0 additional startInitialImport calls
    await user.click(startButton);
    expect(adapter.startImportCallCount).toBe(1);

    // Invoke React onClick handler directly to prove handleSubmitImport also guards
    // even if button disabled state were bypassed -> 0 additional calls!
    const reactPropsKey = Object.keys(startButton).find((key) =>
      key.startsWith("__reactProps$")
    );
    expect(reactPropsKey).toBeDefined();
    const onClick = (
      startButton as unknown as Record<string, { onClick?: () => void }>
    )[reactPropsKey!].onClick;
    expect(typeof onClick).toBe("function");
    await act(async () => {
      onClick!();
    });
    expect(screen.getByTestId("duplicate-guard-alert")).toHaveTextContent(
      "이미 성공"
    );
    expect(adapter.startImportCallCount).toBe(1);

    // Manual status GET remains unblocked on the succeeded repository
    const pollsBeforeManual = adapter.pollCallCount;
    await user.click(screen.getByTestId("manual-refresh-status-button"));
    await waitFor(() => {
      expect(adapter.pollCallCount).toBeGreaterThan(pollsBeforeManual);
    });
    expect(adapter.startImportCallCount).toBe(1);

    // Switching to a different repository re-enables submitting a new initial import
    await user.click(
      screen.getByRole("radio", { name: /design-tokens-kit/i })
    );
    expect(startButton).toBeEnabled();
    expect(startButton).toHaveAttribute("aria-disabled", "false");
    await user.click(startButton);

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "design-tokens-kit"
      );
    });
    expect(adapter.startImportCallCount).toBe(2);
  });

  it("does not treat PARTIAL, PARTIAL_WITH_LIMIT, or unconfirmed responses as succeeded on the same repository", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "partial_non_retryable",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    const startButton = screen.getByTestId("start-import-button");

    // 1. PARTIAL (import_ready=false, retry disabled) is NOT treated as succeeded
    await user.click(startButton);
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: false"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
    expect(startButton).toBeEnabled();

    // 2. PARTIAL_WITH_LIMIT (import_ready=false, retry disabled) is NOT treated as succeeded
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "limit_cap_blocked"
    );
    await user.click(startButton);
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "limit_reached"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: false"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
    expect(startButton).toBeEnabled();

    // 3. Unconfirmed network failure is NOT treated as succeeded
    await user.selectOptions(
      screen.getByLabelText(/응답 시나리오/i),
      "network_error_on_submit"
    );
    await user.click(startButton);
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });
    expect(startButton).toBeEnabled();
  });

  it("keeps A blocked after A succeeds -> select B -> reselect A (with activeRun=null), without throwing in handler, showing B's run on A, or auto-POSTing", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_full",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    const repoARadio = await screen.findByRole("radio", {
      name: /ledger-engine/i,
    });
    const repoBRadio = screen.getByRole("radio", {
      name: /design-tokens-kit/i,
    });
    const startButton = screen.getByTestId("start-import-button");

    // 1. A succeeds
    await user.click(startButton);
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(adapter.startImportCallCount).toBe(1);
    expect(startButton).toBeDisabled();

    // 2. Switch to B -> B is allowed and can submit its own import
    await user.click(repoBRadio);
    expect(repoBRadio).toBeChecked();
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(startButton).toBeEnabled();
    expect(adapter.startImportCallCount).toBe(1);

    // Run B to completion so we also verify B's details NEVER appear on A
    await user.click(startButton);
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "design-tokens-kit"
      );
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(adapter.startImportCallCount).toBe(2);

    // 3. Reselect A -> activeRun is null (B's run cleared), NO auto-POST, A's CTA stays disabled
    await user.click(repoARadio);
    expect(repoARadio).toBeChecked();
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(screen.queryByText(/design-tokens-kit.*완료/)).not.toBeInTheDocument();
    expect(startButton).toBeVisible();
    expect(startButton).toBeDisabled();
    expect(startButton).toHaveAttribute("aria-disabled", "true");
    expect(adapter.startImportCallCount).toBe(2);

    // Clicking disabled CTA -> 0 additional startInitialImport calls
    await user.click(startButton);
    expect(adapter.startImportCallCount).toBe(2);

    // Directly invoke handleSubmitImport while activeRun is null to prove it
    // NEVER throws (no activeRun! dereference) and emits 0 additional calls!
    const reactPropsKey = Object.keys(startButton).find((key) =>
      key.startsWith("__reactProps$")
    );
    expect(reactPropsKey).toBeDefined();
    const onClick = (
      startButton as unknown as Record<string, { onClick?: () => void }>
    )[reactPropsKey!].onClick;
    expect(typeof onClick).toBe("function");
    await act(async () => {
      onClick!();
    });
    expect(screen.getByTestId("duplicate-guard-alert")).toHaveTextContent(
      "ledger-engine"
    );
    expect(adapter.startImportCallCount).toBe(2);

    // Manual read-only status check on A remains available and does NOT issue POST
    const checkStatusBtn = screen.getByTestId("check-repository-status-button");
    expect(checkStatusBtn).toBeEnabled();
    await user.click(checkStatusBtn);
    await waitFor(() => {
      expect(screen.getByTestId("reload-recovery-notice")).toBeInTheDocument();
    });
    expect(adapter.startImportCallCount).toBe(2);
  });

  it("hides prior run details on session expiry, never leaks prior run/success when new scope omits latestRun, and makes 0 lookups/submits with prior runId", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_full",
    });

    const polledRunIds: string[] = [];
    const recoveredKnownRunIds: Array<string | null | undefined> = [];
    const submittedRepoIds: string[] = [];

    // Wrap adapter to simulate an auth/session response that omits `latestRun`
    // (the exact regression scenario) and track every runId passed to lookup/poll/submit.
    const originalSimulateExpired = adapter.simulateSessionExpired.bind(adapter);
    const originalSignIn = adapter.signInService.bind(adapter);
    const originalPoll = adapter.pollImportRun.bind(adapter);
    const originalRecover = adapter.recoverRepositoryRun.bind(adapter);
    const originalStart = adapter.startInitialImport.bind(adapter);

    let latePollGate: ReturnType<typeof createControlledDeferred<void>> | null =
      null;

    adapter.simulateSessionExpired = async () => {
      const res = await originalSimulateExpired();
      if (!res.ok) return res;
      // Intentionally omit `latestRun` to test reducer + ref cleanup
      return {
        ok: true,
        data: {
          serviceAuth: res.data.serviceAuth,
          githubConnection: res.data.githubConnection,
          repositories: res.data.repositories,
        },
      };
    };

    adapter.signInService = async () => {
      const res = await originalSignIn();
      if (!res.ok) return res;
      // Intentionally omit `latestRun` and switch synthetic account identity
      return {
        ok: true,
        data: {
          serviceAuth: {
            ...res.data.serviceAuth,
            account: {
              syntheticUserId: "usr_demo_account_2",
              displayName: "민수 (새 계정 모의)",
              email: "minsu.demo@example.invalid",
            },
          },
          githubConnection: res.data.githubConnection,
          repositories: res.data.repositories,
        },
      };
    };

    adapter.pollImportRun = async (runId: string) => {
      polledRunIds.push(runId);
      const result = await originalPoll(runId);
      if (latePollGate) {
        await latePollGate.promise;
      }
      return result;
    };

    adapter.recoverRepositoryRun = async (input) => {
      recoveredKnownRunIds.push(input.knownRunId);
      return originalRecover(input);
    };

    adapter.startInitialImport = async (input) => {
      submittedRepoIds.push(input.repoId);
      return originalStart(input);
    };

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    const startButton = screen.getByTestId("start-import-button");

    // 1. Account 1 succeeds on repo A
    await user.click(startButton);
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    const acct1RunText = screen.getByTestId("run-status-banner").textContent ?? "";
    const acct1RunIdMatch = acct1RunText.match(/run_demo_\d+/);
    expect(acct1RunIdMatch).not.toBeNull();
    const acct1RunId = acct1RunIdMatch![0];
    expect(startButton).toBeDisabled();

    // Start a slow manual detail poll for Account 1's run, then expire session mid-flight
    latePollGate = createControlledDeferred<void>();
    await user.click(screen.getByTestId("manual-refresh-status-button"));
    await waitFor(() => {
      expect(polledRunIds[polledRunIds.length - 1]).toBe(acct1RunId);
    });
    const pollsBeforeExpiry = polledRunIds.length;

    // 2. Confirmed session expiration (with latestRun omitted)
    await user.click(screen.getByRole("button", { name: "세션 만료 시뮬레이션" }));
    await waitFor(() => {
      expect(screen.getByTestId("service-auth-badge")).toHaveTextContent(
        "세션 만료"
      );
    });

    // Previous run details and recovery notice MUST NOT be rendered while expired
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(screen.queryByTestId("reload-recovery-notice")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("manual-refresh-status-button")
    ).not.toBeInTheDocument();

    // 3. New Account 2 signs in (with latestRun omitted)
    await user.click(
      screen.getByRole("button", { name: "모의 서비스 다시 로그인" })
    );
    await waitFor(() => {
      expect(screen.getByTestId("service-auth-badge")).toHaveTextContent(
        "로그인 활성"
      );
    });

    // Release Account 1's late poll response -> MUST NOT overwrite Account 2's screen!
    latePollGate.resolve();
    latePollGate = null;
    await new Promise((resolve) => setTimeout(resolve, 40));

    // Also verify a stray pollImportRun with Account 1's runId is rejected by adapter in Account 2's session
    const strayPoll = await originalPoll(acct1RunId);
    expect(strayPoll.ok).toBe(false);

    // Account 2 MUST NOT see Account 1's run details or inherit Account 1's repo A block
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(screen.queryByTestId("reload-recovery-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("start-import-button")).toBeEnabled();
    expect(screen.getByTestId("start-import-button")).toHaveAttribute(
      "aria-disabled",
      "false"
    );

    // Manual status check for Account 2 MUST pass knownRunId: null (0 uses of Account 1's runId!)
    await user.click(screen.getByTestId("check-repository-status-button"));
    await waitFor(() => {
      expect(recoveredKnownRunIds).toHaveLength(1);
    });
    expect(recoveredKnownRunIds).toEqual([null]);
    expect(polledRunIds.length).toBe(pollsBeforeExpiry);

    // Account 2 can submit repo A cleanly without inheriting Account 1's block
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(submittedRepoIds).toHaveLength(2);
    expect(screen.getByTestId("run-status-banner")).not.toHaveTextContent(
      acct1RunId
    );
  });

  it("keeps the user signed in on temporary auth service outage (503) while surfacing unconfirmed guidance and issuing 0 auto-POSTs", async () => {
    const user = userEvent.setup();
    let postCalls = 0;

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST") {
            postCalls += 1;
          }
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return { status: 200, rawBody: JSON.stringify({ items: [] }) };
          }
          return { status: 503, rawBody: "" };
        },
      },
    });

    // Inject temporary 503 on manual repository status lookup
    const originalRecover = adapter.recoverRepositoryRun.bind(adapter);
    adapter.recoverRepositoryRun = async (input) => {
      await originalRecover(input);
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: {
          kind: "uncertain_outcome",
          httpStatus: 503,
          title: "인증 서비스 일시 장애 (HTTP 503)",
          message:
            "일시적인 인증 확인 지연으로 요청을 중단했습니다. 로그아웃하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance: "잠시 후 서버 상태를 다시 확인하세요.",
        },
      };
    };

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("check-repository-status-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "uncertain_outcome"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "HTTP 503"
    );
    // MUST stay authenticated (NOT logged out by a temporary outage!)
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent(
      "로그인 활성"
    );

    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(postCalls).toBe(0);
  });

  it("separates explicit resend of unconfirmed Failure A retry (same key + v3) from new retry on Failure B (fresh key + v5) after intermediate GET", async () => {
    const user = userEvent.setup();
    const runId = "synthetic-run-ab-ui";
    const retryLog: Array<{ key: string; body: string | undefined }> = [];
    let getCount = 0;
    let serverPhase: "failure_a" | "failure_b" | "succeeded" = "failure_a";

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return { status: 200, rawBody: JSON.stringify({ items: [] }) };
          }
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: runId,
                status_url: `/v1/sync-runs/${runId}`,
              }),
            };
          }
          if (req.method === "GET" && req.path === `/v1/sync-runs/${runId}`) {
            getCount += 1;
            if (serverPhase === "failure_a") {
              return {
                status: 200,
                rawBody: JSON.stringify({
                  status: "PARTIAL",
                  run_version: 3,
                  plan_id: "plan-ab",
                  failure_reason: "TRANSIENT_PROVIDER_ERROR",
                  import_ready: false,
                  processing_status: "NOT_STARTED",
                  source_drift: false,
                  retry: { allowed: true, from_safe_checkpoint: true },
                }),
              };
            }
            if (serverPhase === "failure_b") {
              return {
                status: 200,
                rawBody: JSON.stringify({
                  status: "PARTIAL",
                  run_version: 5,
                  plan_id: "plan-ab",
                  failure_reason: "TRANSIENT_PROVIDER_ERROR",
                  import_ready: false,
                  processing_status: "NOT_STARTED",
                  source_drift: false,
                  retry: { allowed: true, from_safe_checkpoint: true },
                }),
              };
            }
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "SUCCESS",
                run_version: 7,
                plan_id: "plan-ab",
                import_ready: true,
                processing_status: "COMPLETE",
                source_drift: false,
                retry: { allowed: false },
              }),
            };
          }
          if (req.method === "POST" && req.path === `/v1/sync-runs/${runId}/retry`) {
            retryLog.push({
              key: req.headers["Idempotency-Key"],
              body: req.body,
            });
            if (retryLog.length === 1) {
              // Server accepted Failure A retry & advanced to Failure B, but response was dropped
              serverPhase = "failure_b";
              throw new Error("DROPPED_RETRY_A_RESPONSE");
            }
            if (retryLog.length === 2) {
              // Replay of Failure A returns original 202 without advancing past Failure B
              return {
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_id: runId,
                  status_url: `/v1/sync-runs/${runId}`,
                }),
              };
            }
            // New retry on Failure B (v5) succeeds
            serverPhase = "succeeded";
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: runId,
                status_url: `/v1/sync-runs/${runId}`,
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent("v3");
    });

    // 1. Click retry on Failure A (v3) -> response dropped -> 0 auto-POST
    await user.click(screen.getByTestId("retry-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });
    expect(screen.getByTestId("pending-retry-resend-panel")).toHaveTextContent(
      "새 작업의 최초 접수가 될 수 있습니다"
    );
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(retryLog).toHaveLength(1);

    // 2. Manual GET refresh observes Failure B (v5) without auto-POSTing or overwriting pending A (v3)
    const getsBeforeRefresh = getCount;
    await user.click(screen.getByRole("button", { name: "서버 상태 다시 확인" }));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent("v5");
    });
    expect(getCount).toBeGreaterThan(getsBeforeRefresh);
    expect(retryLog).toHaveLength(1);

    // Both separated buttons are visible with distinct versions
    expect(screen.getByTestId("resend-pending-retry-button")).toHaveTextContent(
      "v3"
    );
    expect(screen.getByTestId("retry-import-button")).toHaveTextContent("v5");

    // 3. Explicit resend of Failure A sends EXACT original key A + {"expected_run_version":3}
    await user.click(screen.getByTestId("resend-pending-retry-button"));
    await waitFor(() => {
      expect(retryLog).toHaveLength(2);
    });
    expect(retryLog[1].key).toBe(retryLog[0].key);
    expect(retryLog[1].body).toBe(JSON.stringify({ expected_run_version: 3 }));

    // Next GET settles on Failure B (v5) and pending-retry panel is cleared
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
      expect(
        screen.queryByTestId("pending-retry-resend-panel")
      ).not.toBeInTheDocument();
    });

    // 4. Explicit new retry on Failure B sends a FRESH key B (!= key A) + {"expected_run_version":5}
    await user.click(screen.getByTestId("retry-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(retryLog).toHaveLength(3);
    expect(retryLog[2].key).not.toBe(retryLog[0].key);
    expect(retryLog[2].body).toBe(JSON.stringify({ expected_run_version: 5 }));
  });

  it("blocks retry POST (0 POSTs) and guides GET status refresh when run_version is absent", async () => {
    const user = userEvent.setup();
    const runId = "synthetic-run-no-ver";
    let retryPostCalls = 0;

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return { status: 200, rawBody: JSON.stringify({ items: [] }) };
          }
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: runId,
                status_url: `/v1/sync-runs/${runId}`,
              }),
            };
          }
          if (req.method === "GET" && req.path === `/v1/sync-runs/${runId}`) {
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "PARTIAL",
                // run_version intentionally omitted
                plan_id: "plan-no-ver",
                failure_reason: "TRANSIENT_PROVIDER_ERROR",
                import_ready: false,
                processing_status: "NOT_STARTED",
                source_drift: false,
                retry: { allowed: true, from_safe_checkpoint: true },
              }),
            };
          }
          if (req.method === "POST" && req.path.endsWith("/retry")) {
            retryPostCalls += 1;
            return { status: 422, rawBody: "" };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={15} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("retry-import-button")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("retry-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "run_version"
      );
    });
    expect(retryPostCalls).toBe(0);
  });

  it("handles 0.5.0-draft 409 RESOURCE_VERSION_CONFLICT, 403 WORKSPACE_SCOPE_VIOLATION, 404 RESOURCE_NOT_FOUND, 503 AUTH_VERIFICATION_UNAVAILABLE, and empty 401 safely in UI with 0 auto-POST", async () => {
    const user = userEvent.setup();

    // Case 1: 409 RESOURCE_VERSION_CONFLICT on retry -> guides GET, 0 auto-POST
    let conflictRetryPosts = 0;
    const conflictAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return { status: 200, rawBody: JSON.stringify({ items: [] }) };
          }
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: "run-ver-conflict",
                status_url: "/v1/sync-runs/run-ver-conflict",
              }),
            };
          }
          if (req.method === "GET" && req.path === "/v1/sync-runs/run-ver-conflict") {
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "PARTIAL",
                run_version: 2,
                plan_id: "plan-vc",
                failure_reason: "TRANSIENT_PROVIDER_ERROR",
                import_ready: false,
                processing_status: "NOT_STARTED",
                source_drift: false,
                retry: { allowed: true, from_safe_checkpoint: true },
              }),
            };
          }
          if (req.method === "POST" && req.path === "/v1/sync-runs/run-ver-conflict/retry") {
            conflictRetryPosts += 1;
            return {
              status: 409,
              rawBody: JSON.stringify({
                error: {
                  code: "RESOURCE_VERSION_CONFLICT",
                  message: "Run version changed.",
                  retryable: false,
                  request_id: "req-vc",
                },
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });
    const r1 = render(
      <ImportJourneyApp
        adapter={conflictAdapter}
        harness={conflictAdapter}
        pollIntervalMs={15}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("retry-import-button")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("retry-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "version_conflict"
      );
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(conflictRetryPosts).toBe(1);
    r1.unmount();

    // Case 2: 403 WORKSPACE_SCOPE_VIOLATION closes Workspace repos/run/polling while keeping login
    let wsPostCount = 0;
    let wsPollCount = 0;
    const wsAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST") {
            wsPostCount += 1;
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: "run-ws-revoked",
                status_url: "/v1/sync-runs/run-ws-revoked",
              }),
            };
          }
          if (req.method === "GET" && req.path === "/v1/sync-runs/run-ws-revoked") {
            wsPollCount += 1;
            return {
              status: 403,
              rawBody: JSON.stringify({
                error: {
                  code: "WORKSPACE_SCOPE_VIOLATION",
                  message: "No workspace access.",
                  retryable: false,
                  request_id: "req-ws",
                },
              }),
            };
          }
          return { status: 200, rawBody: JSON.stringify({ items: [] }) };
        },
      },
    });
    const r2 = render(
      <ImportJourneyApp adapter={wsAdapter} harness={wsAdapter} pollIntervalMs={15} />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "workspace_scope_violation"
      );
    });
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("로그인 활성");
    expect(screen.getByTestId("repo-selector-blocked")).toHaveTextContent(
      "Workspace"
    );
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    const pollsAfterWsRevoked = wsPollCount;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(wsPollCount).toBe(pollsAfterWsRevoked);
    expect(wsPostCount).toBe(1);
    r2.unmount();

    // Case 3: 404 RESOURCE_NOT_FOUND hides previous run details with neutral message + 503 AUTH_VERIFICATION_UNAVAILABLE + empty 401 keep login
    let postCount3 = 0;
    let detailCall3 = 0;
    const errAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "POST") {
            postCount3 += 1;
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: "run-err-cases",
                status_url: "/v1/sync-runs/run-err-cases",
              }),
            };
          }
          if (req.method === "GET" && req.path === "/v1/sync-runs/run-err-cases") {
            detailCall3 += 1;
            if (detailCall3 === 1) {
              return {
                status: 404,
                rawBody: JSON.stringify({
                  error: {
                    code: "RESOURCE_NOT_FOUND",
                    message: "Resource not found.",
                    retryable: false,
                    request_id: "req-404-ui",
                  },
                }),
              };
            }
          }
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            if (detailCall3 === 0) {
              return { status: 200, rawBody: JSON.stringify({ items: [] }) };
            }
            if (detailCall3 === 1) {
              detailCall3 += 1;
              return {
                status: 503,
                rawBody: JSON.stringify({
                  error: {
                    code: "AUTH_VERIFICATION_UNAVAILABLE",
                    message: "Auth check unavailable.",
                    retryable: true,
                    request_id: "req-503-ui",
                  },
                }),
              };
            }
            return { status: 401, rawBody: "" };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });
    const r3 = render(
      <ImportJourneyApp adapter={errAdapter} harness={errAdapter} pollIntervalMs={15} />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    // 404 RESOURCE_NOT_FOUND
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "resource_not_found"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "구별하지 않으며"
    );
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("로그인 활성");

    // 503 AUTH_VERIFICATION_UNAVAILABLE (retryable: true) -> login stays active, 0 auto-POST
    await user.click(screen.getByRole("button", { name: "서버 상태 다시 확인" }));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "auth_verification_unavailable"
      );
    });
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("로그인 활성");

    // Generic empty 401 -> request_rejected (NOT session_expired), login stays active
    await user.click(screen.getByRole("button", { name: "서버 상태 다시 확인" }));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "request_rejected"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "HTTP 401"
    );
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("로그인 활성");
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(postCount3).toBe(1);
    r3.unmount();
  });

  it("renders T-205 plan/visibility/limit/observation metadata, post-SUCCESS non-auto-sync notice, and OD-011 read:user scope alignment", async () => {
    const user = userEvent.setup();
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_full",
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={20} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });

    // OD-011: GitHub OAuth App read:user minimal connection (0 repo/write scopes, single public repo allowlist)
    expect(screen.getByTestId("github-oauth-scope")).toHaveTextContent(
      'scope="read:user"'
    );
    expect(screen.getByTestId("github-oauth-scope")).toHaveTextContent(
      "저장소/쓰기 스코프 0개"
    );
    expect(screen.getByTestId("github-od011-policy")).toHaveTextContent(
      "Allowlist"
    );
    expect(screen.getByTestId("repo-plan-policy-summary")).toHaveTextContent(
      "raw_storage_mode: NONE"
    );
    expect(screen.getByTestId("repo-plan-policy-summary")).toHaveTextContent(
      "5,000"
    );

    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-contract-status",
        "SUCCESS"
      );
    });

    expect(screen.getByTestId("contract-status-badge")).toHaveTextContent(
      "SUCCESS"
    );
    expect(screen.getByTestId("repo-visibility-policy")).toHaveTextContent(
      "PUBLIC 확인"
    );
    expect(screen.getByTestId("repo-visibility-policy")).toHaveTextContent(
      "PUBLIC_CLOUD"
    );
    expect(screen.getByTestId("repo-visibility-policy")).toHaveTextContent(
      "raw_storage_mode: NONE"
    );
    expect(screen.getByTestId("plan-window-and-limit")).toHaveTextContent(
      "max_objects: 5,000건"
    );
    expect(screen.getByTestId("observation-timestamps")).toHaveTextContent(
      "시작:"
    );
    expect(screen.getByTestId("observation-timestamps")).toHaveTextContent(
      "종료 관찰:"
    );

    // T-205 #4: Post-SUCCESS non-auto-sync snapshot notice
    expect(screen.getByTestId("snapshot-no-auto-sync-notice")).toHaveTextContent(
      "일회성 관찰 스냅샷이며, 이후 GitHub에서 발생한 새 커밋·PR·수정·삭제와 자동으로 동기화되지 않습니다"
    );
    expect(screen.getByTestId("snapshot-no-auto-sync-notice")).toHaveTextContent(
      "원문은 저장하지 않으며 근거 검증 시 재조회함"
    );
  });

  it("renders T-205 PENDING/RUNNING/CANCELED distinct states, PLAN_DRIFT & structured failure reasons, stream last_committed_page, and resume eligibility", async () => {
    const user = userEvent.setup();

    // 1. CANCELED neutral banner + retry disabled (distinct from FAILED)
    const canceledAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "canceled_run",
    });
    const r1 = render(
      <ImportJourneyApp
        adapter={canceledAdapter}
        harness={canceledAdapter}
        pollIntervalMs={20}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "canceled"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
      "data-contract-status",
      "CANCELED"
    );
    expect(screen.getByTestId("contract-status-badge")).toHaveTextContent(
      "CANCELED"
    );
    expect(screen.getByTestId("canceled-detail-box")).toBeInTheDocument();
    expect(screen.queryByTestId("error-detail-box")).not.toBeInTheDocument();
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("non-resumable-plan-notice")).toHaveTextContent(
      "같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간 미제공), 부분 수집 결과는 Event/Claim으로 승격되지 않습니다"
    );
    r1.unmount();

    // 2. PLAN_DRIFT (source_drift=true), streams last_committed_page, processed/remaining ranges, non-resumable
    const driftAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "partial_plan_drift",
    });
    const r2 = render(
      <ImportJourneyApp
        adapter={driftAdapter}
        harness={driftAdapter}
        pollIntervalMs={20}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("failure-reason-summary")).toHaveAttribute(
        "data-failure-code",
        "PLAN_DRIFT"
      );
    });
    expect(screen.getByTestId("failure-reason-summary")).toHaveTextContent(
      "source_drift: true"
    );
    expect(screen.getByTestId("stream-progress-list")).toHaveTextContent(
      "last_committed_page=5"
    );
    expect(screen.getByTestId("processed-range")).toHaveTextContent(
      "2025-09-23 through 2026-07-31"
    );
    expect(screen.getByTestId("remaining-range")).toHaveTextContent(
      "2026-08-01 through 2026-10-05"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("non-resumable-plan-notice")).toBeInTheDocument();
    r2.unmount();

    // 3. Custom transport verifying PENDING -> INCOMPLETE_PR_ASSOCIATION -> retryable TRANSIENT_PROVIDER_ERROR CTA wording
    let pollStep = 0;
    const customAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: {
        async send(req) {
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return { status: 200, rawBody: JSON.stringify({ items: [] }) };
          }
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_type: "SYNC_RUN",
                target_id: "run-t205-structured",
                status_url: "/v1/sync-runs/run-t205-structured",
              }),
            };
          }
          if (req.method === "GET" && req.path === "/v1/sync-runs/run-t205-structured") {
            pollStep += 1;
            if (pollStep === 1) {
              return {
                status: 200,
                rawBody: JSON.stringify({
                  status: "PARTIAL",
                  run_version: 2,
                  plan_id: "plan-t205",
                  failure_reason: "INCOMPLETE_PR_ASSOCIATION",
                  streams: [
                    {
                      type: "PR",
                      status: "PARTIAL",
                      last_committed_page: 4,
                      unresolved_failures: 2,
                    },
                  ],
                  import_ready: false,
                  processing_status: "NOT_STARTED",
                  observed_at: "2026-10-05T09:10:00Z",
                  source_drift: false,
                  processed_range: "2025-09-23 through 2026-07-01",
                  remaining_range: "2026-07-02 through 2026-10-05",
                  retry: { allowed: false },
                }),
              };
            }
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "PARTIAL",
                run_version: 3,
                plan_id: "plan-t205",
                failure_reason: "TRANSIENT_PROVIDER_ERROR",
                streams: [
                  {
                    type: "PR",
                    status: "PARTIAL",
                    last_committed_page: 7,
                    unresolved_failures: 1,
                  },
                ],
                import_ready: false,
                processing_status: "NOT_STARTED",
                observed_at: "2026-10-05T09:12:00Z",
                source_drift: false,
                processed_range: "2025-09-23 through 2026-08-31",
                remaining_range: "2026-09-01 through 2026-10-05",
                retry: { allowed: true, from_safe_checkpoint: true },
              }),
            };
          }
          return { status: 404, rawBody: "" };
        },
      },
    });

    const r3 = render(
      <ImportJourneyApp
        adapter={customAdapter}
        harness={customAdapter}
        pollIntervalMs={20}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("failure-reason-summary")).toHaveAttribute(
        "data-failure-code",
        "INCOMPLETE_PR_ASSOCIATION"
      );
    });
    expect(screen.getByTestId("failure-reason-summary")).toHaveTextContent(
      "UNKNOWN PR"
    );
    expect(screen.getByTestId("stream-progress-list")).toHaveTextContent(
      "last_committed_page=4"
    );
    expect(screen.getByTestId("stream-progress-list")).toHaveTextContent(
      "unresolved_failures=2"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();

    // Manual refresh moves to retryable TRANSIENT_PROVIDER_ERROR -> exposes safe checkpoint resume button
    await user.click(screen.getByTestId("manual-refresh-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("retry-import-button")).toHaveTextContent(
        "같은 계획·마지막 안전 체크포인트에서 재개"
      );
    });
    r3.unmount();
  });

  it("explicitly blocks raw view, external AI, and Export on BLOCKED_POLICY, BLOCKED_SOURCE, and SOURCE_POLICY_BLOCKED without GitHub deletion misclassification", async () => {
    const user = userEvent.setup();

    // 1. SUCCESS + BLOCKED_POLICY
    const blockedPolicyAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "success_blocked_policy",
    });
    const r1 = render(
      <ImportJourneyApp
        adapter={blockedPolicyAdapter}
        harness={blockedPolicyAdapter}
        pollIntervalMs={20}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("processing-blocked-notice")).toBeInTheDocument();
    });
    expect(screen.getByTestId("processing-blocked-notice")).toHaveTextContent(
      "원문 열람 · 외부 AI 전송 · Export가 즉시 차단되었습니다"
    );
    expect(screen.getByTestId("processing-blocked-notice")).toHaveTextContent(
      "GitHub 원본이 삭제된 것으로 처리하지 않으며"
    );
    expect(screen.getByTestId("blocked-surfaces-badges")).toHaveTextContent(
      "POLICY_REVIEW_REQUIRED"
    );
    r1.unmount();

    // 2. PARTIAL + SOURCE_POLICY_BLOCKED -> dedicated policy-export-block-notice
    const sourceBlockedAdapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      initialScenario: "partial_non_retryable",
    });
    const r2 = render(
      <ImportJourneyApp
        adapter={sourceBlockedAdapter}
        harness={sourceBlockedAdapter}
        pollIntervalMs={20}
      />
    );
    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("policy-export-block-notice")).toBeInTheDocument();
    });
    expect(screen.getByTestId("policy-export-block-notice")).toHaveTextContent(
      "원문 열람 · 외부 AI 전송 · Export가 즉시 차단되었습니다"
    );
    expect(screen.getByTestId("policy-export-block-notice")).toHaveTextContent(
      "Public → Private"
    );
    expect(screen.getByTestId("policy-export-block-notice")).toHaveTextContent(
      "GitHub 원본이 삭제된 것으로 처리하지 않으며"
    );
    r2.unmount();
  });
});
