import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
});
