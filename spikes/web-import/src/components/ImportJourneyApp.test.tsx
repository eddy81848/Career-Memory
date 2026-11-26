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
      "MVP 최초 스냅샷 수집 상한 초과"
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
});
