import React from "react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { ContractSyncRunClient } from "@/adapters/contract-sync-run-client";
import { SYNTHETIC_PUBLIC_REPOSITORIES } from "@/adapters/fixtures";
import { MockFixtureImportAdapter } from "@/adapters/mock-fixture-adapter";
import { UUID_V4_REGEX } from "@/adapters/synthetic-http-transport";
import { ImportJourneyApp } from "@/components/ImportJourneyApp";
import { LoopbackContractHttpTransport } from "./loopback-http-transport";

/**
 * WEB-05 / WEB-06 REAL LOCAL LOOPBACK HTTP INTEGRATION TESTS.
 *
 * Launches CONTRACT's actual FastAPI stub (`create_app` + `FixtureActor` from
 * commit 1d754a5bc33cf166d92b1c1ce8568031859ff0a4, OpenAPI 0.3.0-draft, read-only)
 * on 127.0.0.1 and connects ONLY the 4 OpenAPI sync-run endpoints through WEB's
 * existing HTTP adapter boundary (`SyntheticHttpTransport` -> `ContractSyncRunClient`
 * -> `MockFixtureImportAdapter` -> `ImportJourneyApp`).
 *
 * Strictly test-only. Never reads synthetic identity from browser headers.
 * Always terminates the stub server in afterAll.
 */

const WORKSPACE_ID = "workspace-synthetic-web05";
const REPO_1 = SYNTHETIC_PUBLIC_REPOSITORIES[0]; // repo_demo_ledger_01
const REPO_2 = SYNTHETIC_PUBLIC_REPOSITORIES[1]; // repo_demo_tokens_02

interface StubActorSpec {
  workspace_id: string;
  allowed_resources: string[];
  unreadable_runs?: string[];
}

interface StubRequestLogEntry {
  method: string;
  path: string;
  query_string?: string;
  full_path?: string;
  idempotency_key: string | null;
  request_id: string | null;
  body_length: number;
  status: number | null;
  injected?: boolean;
}

interface StubStateSnapshot {
  run_count: number;
  run_ids: string[];
  create_replay_count: number;
  retry_replay_count: number;
  runs: Record<string, Record<string, unknown>>;
  request_log: StubRequestLogEntry[];
  clock_offset_seconds?: number;
}

let stubProcess: ChildProcessWithoutNullStreams | null = null;
let contractUrl = "";
let controlUrl = "";

async function startContractStubServer(): Promise<{
  process: ChildProcessWithoutNullStreams;
  contractUrl: string;
  controlUrl: string;
}> {
  const scriptPath = path.resolve(
    process.cwd(),
    "tests/contract-http/run_contract_stub.py"
  );
  const child = spawn(
    "uv",
    [
      "run",
      "--python",
      "3.13",
      "--with",
      "fastapi==0.115.12",
      "--with",
      "uvicorn==0.34.0",
      "--with",
      "jsonschema==4.26.0",
      "--with",
      "PyYAML==6.0.3",
      "--with",
      "rfc3339-validator==0.1.4",
      "python",
      "-B",
      scriptPath,
    ],
    {
      cwd: process.cwd(),
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    }
  );

  const stderrChunks: string[] = [];
  child.stderr.on("data", (chunk) => {
    stderrChunks.push(chunk.toString("utf8"));
  });

  const readyInfo = await new Promise<{ contractUrl: string; controlUrl: string }>(
    (resolve, reject) => {
      const rl = readline.createInterface({ input: child.stdout });
      const timeout = setTimeout(() => {
        rl.close();
        reject(
          new Error(
            `Timed out waiting for FastAPI contract stub startup. stderr:\n${stderrChunks.join("")}`
          )
        );
      }, 20_000);

      rl.on("line", (line) => {
        const trimmed = line.trim();
        if (!trimmed.startsWith("{")) {
          return;
        }
        try {
          const parsed = JSON.parse(trimmed) as {
            contractUrl?: string;
            controlUrl?: string;
          };
          if (parsed.contractUrl && parsed.controlUrl) {
            clearTimeout(timeout);
            rl.close();
            resolve({
              contractUrl: parsed.contractUrl,
              controlUrl: parsed.controlUrl,
            });
          }
        } catch {
          // Ignore non-JSON log lines
        }
      });

      child.once("exit", (code) => {
        clearTimeout(timeout);
        rl.close();
        reject(
          new Error(
            `FastAPI contract stub exited prematurely (code=${code}). stderr:\n${stderrChunks.join("")}`
          )
        );
      });
    }
  );

  return { process: child, ...readyInfo };
}

async function stopContractStubServer(): Promise<void> {
  const child = stubProcess;
  stubProcess = null;
  if (!child || child.exitCode !== null) {
    return;
  }

  await new Promise<void>((resolve) => {
    const forceKill = setTimeout(() => {
      if (child.exitCode === null) {
        child.kill("SIGKILL");
      }
      resolve();
    }, 3_000);

    child.once("exit", () => {
      clearTimeout(forceKill);
      resolve();
    });

    child.kill("SIGTERM");
  });
}

async function resetStub(options?: {
  resources?: Array<[string, string]>;
  actor?: StubActorSpec | null;
}): Promise<void> {
  const res = await fetch(`${controlUrl}/__control/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      resources: options?.resources ?? [
        [WORKSPACE_ID, REPO_1.repoId],
        [WORKSPACE_ID, REPO_2.repoId],
      ],
      actor:
        options?.actor === undefined
          ? {
              workspace_id: WORKSPACE_ID,
              allowed_resources: [REPO_1.repoId, REPO_2.repoId],
            }
          : options.actor,
    }),
  });
  if (!res.ok) {
    throw new Error(`control reset failed: ${res.status} ${await res.text()}`);
  }
}

async function setStubActor(actor: StubActorSpec | null): Promise<void> {
  const res = await fetch(`${controlUrl}/__control/set-actor`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actor }),
  });
  if (!res.ok) {
    throw new Error(`control set-actor failed: ${res.status}`);
  }
}

async function advanceStubClock(params: {
  hours?: number;
  seconds?: number;
}): Promise<void> {
  const res = await fetch(`${controlUrl}/__control/advance-clock`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      hours: params.hours ?? 0,
      seconds: params.seconds ?? 0,
    }),
  });
  if (!res.ok) {
    throw new Error(`control advance-clock failed: ${res.status}`);
  }
}

async function queueStubHttpResponse(params: {
  status: number;
  body?: string;
  contentType?: string;
  method?: string;
  pathPrefix?: string;
}): Promise<void> {
  const res = await fetch(`${controlUrl}/__control/queue-http-response`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status: params.status,
      body: params.body ?? "",
      content_type: params.contentType ?? null,
      method: params.method ?? null,
      path_prefix: params.pathPrefix ?? null,
    }),
  });
  if (!res.ok) {
    throw new Error(`control queue-http-response failed: ${res.status}`);
  }
}

async function observeRun(params: {
  runId: string;
  example?: string;
  observation?: Record<string, unknown>;
  workspaceId?: string;
}): Promise<void> {
  const res = await fetch(`${controlUrl}/__control/observe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: params.workspaceId ?? WORKSPACE_ID,
      run_id: params.runId,
      example: params.example ?? null,
      observation: params.observation ?? null,
    }),
  });
  if (!res.ok) {
    throw new Error(`control observe failed: ${res.status} ${await res.text()}`);
  }
}

async function seedExistingRun(params: {
  repoId: string;
  key: string;
  actor?: StubActorSpec;
}): Promise<{ target_id: string; status_url: string }> {
  const res = await fetch(`${controlUrl}/__control/seed-create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      actor: params.actor ?? {
        workspace_id: WORKSPACE_ID,
        allowed_resources: [REPO_1.repoId, REPO_2.repoId],
      },
      key: params.key,
      payload: {
        source_resource_id: params.repoId,
        mode: "INITIAL_BACKFILL",
      },
    }),
  });
  if (!res.ok) {
    throw new Error(`control seed-create failed: ${res.status} ${await res.text()}`);
  }
  const data = (await res.json()) as {
    receipt: { target_id: string; status_url: string };
  };
  return data.receipt;
}

async function getStubState(): Promise<StubStateSnapshot> {
  const res = await fetch(`${controlUrl}/__control/state`);
  if (!res.ok) {
    throw new Error(`control state failed: ${res.status}`);
  }
  return (await res.json()) as StubStateSnapshot;
}

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeAll(async () => {
  const started = await startContractStubServer();
  stubProcess = started.process;
  contractUrl = started.contractUrl;
  controlUrl = started.controlUrl;
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

afterAll(async () => {
  await stopContractStubServer();
});

describe("WEB-05 real 127.0.0.1 FastAPI contract stub integration", () => {
  it("completes normal 202 -> 200 journey over real 127.0.0.1 HTTP without treating 202 as completed", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new LoopbackContractHttpTransport(contractUrl);
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport,
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    // 202 Accepted MUST render as running (NOT completed) with import_ready=false
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "synthetic-run-1"
    );
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: false"
    );

    // Observe SUCCESS + COMPLETE in CONTRACT FixtureStore -> next real GET 200 completes UI
    await observeRun({
      runId: "synthetic-run-1",
      observation: {
        status: "SUCCESS",
        import_ready: true,
        processing_status: "COMPLETE",
        observed_at: "2026-10-05T09:00:00Z",
        source_drift: false,
        retry: { allowed: false },
      },
    });

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

    const stubState = await getStubState();
    expect(stubState.run_count).toBe(1);
    expect(stubState.create_replay_count).toBe(1);
    const postCreate = stubState.request_log.find(
      (entry) => entry.method === "POST" && entry.path === "/v1/sync-runs"
    );
    expect(postCreate).toBeDefined();
    expect(postCreate?.status).toBe(202);
    expect(postCreate?.idempotency_key).toMatch(UUID_V4_REGEX);
    expect(
      stubState.request_log.some(
        (entry) =>
          entry.method === "GET" &&
          entry.path === "/v1/sync-runs/synthetic-run-1" &&
          entry.status === 200
      )
    ).toBe(true);
  });

  it("executes bodyless retry over real 127.0.0.1 HTTP for retryable transientPartial and recovers to SUCCESS", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new LoopbackContractHttpTransport(contractUrl);
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport,
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    // Apply OpenAPI transientPartial example -> retry.allowed=true, from_safe_checkpoint=true
    await observeRun({ runId: "synthetic-run-1", example: "transientPartial" });

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: false"
    );

    const retryBtn = screen.getByTestId("retry-import-button");
    expect(retryBtn).toBeEnabled();
    await user.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });

    // Verify stub received a strictly bodyless POST /v1/sync-runs/synthetic-run-1/retry -> 202
    const afterRetryState = await getStubState();
    const retryLog = afterRetryState.request_log.find(
      (entry) =>
        entry.method === "POST" &&
        entry.path === "/v1/sync-runs/synthetic-run-1/retry"
    );
    expect(retryLog).toBeDefined();
    expect(retryLog?.body_length).toBe(0);
    expect(retryLog?.status).toBe(202);
    expect(retryLog?.idempotency_key).toMatch(UUID_V4_REGEX);
    expect(afterRetryState.retry_replay_count).toBe(1);

    await observeRun({
      runId: "synthetic-run-1",
      observation: {
        status: "SUCCESS",
        import_ready: true,
        processing_status: "COMPLETE",
        source_drift: false,
        retry: { allowed: false },
      },
    });

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
  });

  it("handles PARTIAL_WITH_LIMIT as non-success with retry disabled over real 127.0.0.1 HTTP", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new LoopbackContractHttpTransport(contractUrl);
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport,
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    await observeRun({ runId: "synthetic-run-1", example: "itemLimit" });

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

    // Also confirm stub rejects any direct retry on PARTIAL_WITH_LIMIT with empty 409
    const directClient = new ContractSyncRunClient({ transport });
    const rejectedRetry = await directClient.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_1,
    });
    expect(rejectedRetry.ok).toBe(false);
    if (!rejectedRetry.ok) {
      expect(rejectedRetry.kind).toBe("request_rejected");
      expect(rejectedRetry.fault.httpStatus).toBe(409);
    }
  });

  it("separates historical SUCCESS (import_ready=true) from BLOCKED_POLICY and BLOCKED_SOURCE over real 127.0.0.1 HTTP", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new LoopbackContractHttpTransport(contractUrl);
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport,
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    // 1. OpenAPI policyChangedAfterSuccess example -> SUCCESS + BLOCKED_POLICY
    await observeRun({
      runId: "synthetic-run-1",
      example: "policyChangedAfterSuccess",
    });

    await waitFor(() => {
      expect(screen.getByTestId("processing-blocked-notice")).toBeInTheDocument();
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );
    expect(screen.getByTestId("processing-status-badge")).toHaveTextContent(
      "BLOCKED_POLICY"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();

    // 2. Start a second run on REPO_2 and observe SUCCESS + BLOCKED_SOURCE
    await user.click(screen.getByRole("radio", { name: /design-tokens-kit/i }));
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-2"
      );
    });

    await observeRun({
      runId: "synthetic-run-2",
      observation: {
        status: "SUCCESS",
        import_ready: true,
        processing_status: "BLOCKED_SOURCE",
        source_drift: false,
        retry: { allowed: false },
      },
    });

    await waitFor(() => {
      expect(screen.getByTestId("processing-status-badge")).toHaveTextContent(
        "BLOCKED_SOURCE"
      );
    });
    expect(screen.getByTestId("processing-blocked-notice")).toBeInTheDocument();
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );
    expect(screen.queryByTestId("retry-import-button")).not.toBeInTheDocument();
  });

  it("navigates on 409 SYNC_ALREADY_RUNNING only when stub provides existing_run_id, and stays put when unreadable", async () => {
    await resetStub();

    const user = userEvent.setup();
    const transport1 = new LoopbackContractHttpTransport(contractUrl);
    const adapter1 = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport1,
    });

    const firstRender = render(
      <ImportJourneyApp adapter={adapter1} harness={adapter1} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });

    // Seed concurrent active run AFTER initial mount -> submit hits 409 SYNC_ALREADY_RUNNING with existing_run_id
    const seeded = await seedExistingRun({
      repoId: REPO_1.repoId,
      key: "00000000-0000-4000-8000-000000000101",
    });
    expect(seeded.target_id).toBe("synthetic-run-1");

    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
        "synthetic-run-1"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "synthetic-run-1"
    );
    firstRender.unmount();

    // Now switch stub actor so existing run is unreadable -> recovery GET returns empty [],
    // and submit returns 409 SYNC_ALREADY_RUNNING WITHOUT existing_run_id
    await setStubActor({
      workspace_id: WORKSPACE_ID,
      allowed_resources: [REPO_1.repoId, REPO_2.repoId],
      unreadable_runs: ["synthetic-run-1"],
    });

    const transport2 = new LoopbackContractHttpTransport(contractUrl);
    const adapter2 = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport2,
    });

    render(
      <ImportJourneyApp adapter={adapter2} harness={adapter2} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "SYNC_ALREADY_RUNNING"
      );
    });
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    // Mount did recovery GET (/v1/sync-runs?...) which returned empty [];
    // MUST NEVER attempt detail GET /v1/sync-runs/synthetic-run-1 after 409 without existing_run_id!
    expect(
      transport2.sentRequests.some(
        (req) => req.method === "GET" && req.path.startsWith("/v1/sync-runs/")
      )
    ).toBe(false);
  });

  it("handles real empty 401/403/409/422 error bodies without JSON errors and ignores spoofed identity headers", async () => {
    // 1. actor=None -> stub ignores browser Authorization / X-Workspace-ID headers and returns empty 401
    await resetStub({ actor: null });
    const rawSpoof = await fetch(`${contractUrl}/v1/sync-runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "00000000-0000-4000-8000-000000000201",
        Authorization: "Bearer synthetic-spoof",
        "X-Workspace-ID": WORKSPACE_ID,
      },
      body: JSON.stringify({
        source_resource_id: REPO_1.repoId,
        mode: "INITIAL_BACKFILL",
      }),
    });
    expect(rawSpoof.status).toBe(401);
    expect(await rawSpoof.text()).toBe("");

    const transport401 = new LoopbackContractHttpTransport(contractUrl);
    const client401 = new ContractSyncRunClient({ transport: transport401 });
    const res401 = await client401.createInitialImport({ repo: REPO_1 });
    expect(res401.ok).toBe(false);
    if (!res401.ok) {
      expect(res401.kind).toBe("session_expired");
      expect(res401.fault.httpStatus).toBe(401);
    }

    // 2. Empty 403 on unauthorized repo
    await resetStub({
      resources: [
        [WORKSPACE_ID, REPO_1.repoId],
        [WORKSPACE_ID, REPO_2.repoId],
      ],
      actor: {
        workspace_id: WORKSPACE_ID,
        allowed_resources: [REPO_1.repoId], // REPO_2 denied
      },
    });
    const transport = new LoopbackContractHttpTransport(contractUrl);
    const client = new ContractSyncRunClient({ transport });
    const res403 = await client.createInitialImport({ repo: REPO_2 });
    expect(res403.ok).toBe(false);
    if (!res403.ok) {
      expect(res403.kind).toBe("request_rejected");
      expect(res403.fault.httpStatus).toBe(403);
      expect(res403.fault.message).toContain("세부 사유를 특정할 수 없습니다");
    }

    // 3. Empty 409 when creating again after terminal SUCCESS
    const created = await client.createInitialImport({ repo: REPO_1 });
    expect(created.ok).toBe(true);
    await observeRun({
      runId: "synthetic-run-1",
      observation: {
        status: "SUCCESS",
        import_ready: true,
        processing_status: "COMPLETE",
        source_drift: false,
        retry: { allowed: false },
      },
    });
    const res409 = await client.createInitialImport({ repo: REPO_1 });
    expect(res409.ok).toBe(false);
    if (!res409.ok) {
      expect(res409.kind).toBe("request_rejected");
      expect(res409.fault.httpStatus).toBe(409);
      expect(res409.fault.message).toContain("이유를 단정할 수 없습니다");
    }

    // 4. Empty 422 on invalid non-UUID Idempotency-Key
    const invalidKeyClient = new ContractSyncRunClient({
      transport,
      keyGenerator: () => "not-a-uuid",
    });
    const res422 = await invalidKeyClient.createInitialImport({ repo: REPO_1 });
    expect(res422.ok).toBe(false);
    if (!res422.ok) {
      expect(res422.kind).toBe("request_rejected");
      expect(res422.fault.httpStatus).toBe(422);
      expect(res422.fault.message).toContain("세부 원인은 응답에 포함되지 않았습니다");
    }
  });

  it("drops create 202 response AFTER stub accepts: no auto-POST, explicit resend with same key replays same run without creating a second run", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new LoopbackContractHttpTransport(contractUrl);
    let droppedStubStatus: number | null = null;

    // Stub ACTUALLY receives POST /v1/sync-runs and returns 202, then test drops the response!
    transport.queueAfterResponseHook(
      ({ response }) => {
        droppedStubStatus = response.status;
        throw new Error("TEST_DROPPED_CREATE_RESPONSE_AFTER_STUB_ACCEPTED");
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport,
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });
    expect(droppedStubStatus).toBe(202);
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();

    // Wait briefly and verify ZERO automatic POST was issued!
    await new Promise((resolve) => setTimeout(resolve, 100));
    const afterDropState = await getStubState();
    const postCreatesBeforeResend = afterDropState.request_log.filter(
      (r) => r.method === "POST" && r.path === "/v1/sync-runs"
    );
    expect(postCreatesBeforeResend).toHaveLength(1);
    expect(afterDropState.run_count).toBe(1);
    expect(afterDropState.create_replay_count).toBe(1);

    // Explicit user resend -> reuses EXACT SAME Idempotency-Key -> stub replays synthetic-run-1
    await user.click(screen.getByTestId("start-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    const afterResendState = await getStubState();
    const postCreatesAfterResend = afterResendState.request_log.filter(
      (r) => r.method === "POST" && r.path === "/v1/sync-runs"
    );
    expect(postCreatesAfterResend).toHaveLength(2);
    expect(postCreatesAfterResend[1].idempotency_key).toBe(
      postCreatesAfterResend[0].idempotency_key
    );
    expect(postCreatesAfterResend[1].status).toBe(202);
    // Stub MUST NOT have added a second run or second create replay!
    expect(afterResendState.run_count).toBe(1);
    expect(afterResendState.create_replay_count).toBe(1);
  });

  it("drops bodyless retry 202 response AFTER stub accepts: no auto-POST, explicit resend with same key replays same run without adding a new retry", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new LoopbackContractHttpTransport(contractUrl);
    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport,
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    await observeRun({ runId: "synthetic-run-1", example: "transientPartial" });
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "partial"
      );
    });

    // Drop the real 202 response AFTER the stub accepted the bodyless retry!
    let droppedRetryStatus: number | null = null;
    transport.queueAfterResponseHook(({ response }) => {
      droppedRetryStatus = response.status;
      throw new Error("TEST_DROPPED_RETRY_RESPONSE_AFTER_STUB_ACCEPTED");
    });

    await user.click(screen.getByTestId("retry-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });
    expect(droppedRetryStatus).toBe(202);

    // Wait briefly and verify ZERO automatic retry POST occurred!
    await new Promise((resolve) => setTimeout(resolve, 100));
    const afterDropState = await getStubState();
    const postRetriesBeforeResend = afterDropState.request_log.filter(
      (r) => r.method === "POST" && r.path === "/v1/sync-runs/synthetic-run-1/retry"
    );
    expect(postRetriesBeforeResend).toHaveLength(1);
    expect(postRetriesBeforeResend[0].body_length).toBe(0);
    expect(afterDropState.retry_replay_count).toBe(1);
    expect(
      afterDropState.runs[`${WORKSPACE_ID}:synthetic-run-1`]?.status
    ).toBe("PENDING");

    // Explicit user resend -> reuses EXACT SAME retry Idempotency-Key -> stub replays synthetic-run-1
    await user.click(screen.getByTestId("retry-import-button"));

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });

    const afterResendState = await getStubState();
    const postRetriesAfterResend = afterResendState.request_log.filter(
      (r) => r.method === "POST" && r.path === "/v1/sync-runs/synthetic-run-1/retry"
    );
    expect(postRetriesAfterResend).toHaveLength(2);
    expect(postRetriesAfterResend[1].body_length).toBe(0);
    expect(postRetriesAfterResend[1].idempotency_key).toBe(
      postRetriesAfterResend[0].idempotency_key
    );
    expect(postRetriesAfterResend[1].status).toBe(202);
    // Stub MUST NOT have created a second run or second retry record!
    expect(afterResendState.run_count).toBe(1);
    expect(afterResendState.retry_replay_count).toBe(1);
  });

  it("isolates late real 202 and late real 401 from an old session after logout/session change so new session UI, stored run, and pending key stay untouched", async () => {
    // --- Part A: Late real 202 from Session 1 ---
    await resetStub();
    const user = userEvent.setup();
    const transport = new LoopbackContractHttpTransport(contractUrl);

    const holdSession1Late202 = createDeferred<void>();
    let capturedLate202Status: number | null = null;

    // Delay Session 1's real 202 response from the FastAPI stub in-flight
    transport.queueAfterResponseHook(
      async ({ response }) => {
        capturedLate202Status = response.status;
        await holdSession1Late202.promise;
        return response;
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transport,
    });

    const firstMount = render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    // Session 1 clicks submit -> real POST hits stub (creates synthetic-run-1), response held in flight
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(capturedLate202Status).toBe(202);
    });

    // While Session 1's 202 is in flight, logout and sign in as Session 2!
    await user.click(screen.getByRole("button", { name: "모의 로그아웃" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그아웃"
    );
    await user.click(screen.getByRole("button", { name: "모의 서비스 로그인" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그인 활성"
    );

    // Session 2 selects REPO_2 and submits; test drops Session 2's response after stub accepts
    // so Session 2 holds its own pending Idempotency-Key in memory
    await user.click(
      screen.getByRole("radio", { name: /seoyeon-lab-demo\/design-tokens-kit/i })
    );
    transport.queueAfterResponseHook(
      ({ response }) => {
        expect(response.status).toBe(202);
        throw new Error("SESSION_2_DROPPED_RESPONSE");
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );

    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });

    // NOW release Session 1's LATE 202!
    holdSession1Late202.resolve();
    await new Promise((resolve) => setTimeout(resolve, 60));

    // Session 2's UI and sessionStorage MUST NOT contain Session 1's late synthetic-run-1!
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("로그인 활성");
    const persistedAfterLate202 = window.sessionStorage.getItem(
      "career_memory_spike_web_import_v1"
    );
    expect(persistedAfterLate202 ?? "").not.toContain("synthetic-run-1");

    // Session 2 explicitly resends -> MUST STILL reuse Session 2's exact pending key and replay synthetic-run-2!
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-2"
      );
    });

    const stateAfterPartA = await getStubState();
    const repo2Creates = stateAfterPartA.request_log.filter(
      (entry) => entry.method === "POST" && entry.path === "/v1/sync-runs"
    );
    // Request 0: Session 1 (REPO_1), Request 1 & 2: Session 2 (REPO_2, same key!)
    expect(repo2Creates).toHaveLength(3);
    expect(repo2Creates[2].idempotency_key).toBe(repo2Creates[1].idempotency_key);
    expect(repo2Creates[1].idempotency_key).not.toBe(repo2Creates[0].idempotency_key);
    expect(stateAfterPartA.run_count).toBe(2);
    expect(stateAfterPartA.create_replay_count).toBe(2);

    firstMount.unmount();
    window.sessionStorage.clear();

    // --- Part B: Late real 401 from Session 1 ---
    await resetStub();
    const transportB = new LoopbackContractHttpTransport(contractUrl);
    const holdSession1Late401 = createDeferred<void>();
    let capturedLate401Status: number | null = null;

    const adapterB = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportB,
    });

    render(
      <ImportJourneyApp adapter={adapterB} harness={adapterB} pollIntervalMs={30} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });

    // Expire stub session right before Session 1 submits (after initial mount finishes)
    await setStubActor(null);
    transportB.queueAfterResponseHook(
      async ({ response }) => {
        capturedLate401Status = response.status;
        await holdSession1Late401.promise;
        return response;
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );

    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(capturedLate401Status).toBe(401);
    });

    // While Session 1's 401 is in flight, logout, configure authenticated stub actor for Session 2, and re-login
    await user.click(screen.getByRole("button", { name: "모의 로그아웃" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그아웃"
    );
    await setStubActor({
      workspace_id: WORKSPACE_ID,
      allowed_resources: [REPO_1.repoId, REPO_2.repoId],
    });
    await user.click(screen.getByRole("button", { name: "모의 서비스 로그인" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그인 활성"
    );

    // Session 2 submits and drops response after stub accepts -> holds Session 2's pending key
    transportB.queueAfterResponseHook(
      ({ response }) => {
        expect(response.status).toBe(202);
        throw new Error("SESSION_2_DROPPED_FOR_401_TEST");
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });

    // NOW release Session 1's LATE 401!
    holdSession1Late401.resolve();
    await new Promise((resolve) => setTimeout(resolve, 60));

    // Session 2 MUST stay authenticated in UI and sessionStorage, and keep its pending key!
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("로그인 활성");
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
      "data-fault-kind",
      "network_error"
    );
    const persistedAfterLate401 = JSON.parse(
      window.sessionStorage.getItem("career_memory_spike_web_import_v1") ?? "{}"
    ) as { serviceAuth?: { status?: string } };
    expect(persistedAfterLate401.serviceAuth?.status).toBe("authenticated");

    // Session 2 explicitly resends -> reuses Session 2's exact pending key and replays synthetic-run-1
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    const stateAfterPartB = await getStubState();
    const createsB = stateAfterPartB.request_log.filter(
      (entry) => entry.method === "POST" && entry.path === "/v1/sync-runs"
    );
    expect(createsB).toHaveLength(3);
    expect(createsB[0].status).toBe(401);
    expect(createsB[1].status).toBe(202);
    expect(createsB[2].status).toBe(202);
    expect(createsB[2].idempotency_key).toBe(createsB[1].idempotency_key);
    expect(stateAfterPartB.run_count).toBe(1);
    expect(stateAfterPartB.create_replay_count).toBe(1);
  });
});

describe("WEB-06 real 127.0.0.1 FastAPI contract stub recovery & edge cases (OpenAPI 0.3.0-draft)", () => {
  it("recovers an ACTIVE run after reload when create was accepted on server but client reloaded before receiving 202, continuing detail GET polling without auto-POST", async () => {
    await resetStub();
    window.sessionStorage.clear();
    const user = userEvent.setup();

    // Tab 1: submit POST /v1/sync-runs -> stub accepts (202, synthetic-run-1), response is lost before WEB receives it
    const transportBeforeReload = new LoopbackContractHttpTransport(contractUrl);
    let droppedStatus: number | null = null;
    transportBeforeReload.queueAfterResponseHook(
      ({ response }) => {
        droppedStatus = response.status;
        throw new Error("CLIENT_RELOADED_BEFORE_CREATE_RESPONSE");
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );

    const adapterBeforeReload = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportBeforeReload,
    });

    const tab1 = render(
      <ImportJourneyApp
        adapter={adapterBeforeReload}
        harness={adapterBeforeReload}
        pollIntervalMs={25}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(droppedStatus).toBe(202);
      expect(screen.getByTestId("unconfirmed-fault-banner")).toBeInTheDocument();
    });

    // Confirm neither runId nor any Idempotency-Key was persisted across reload
    const persistedBeforeReload = window.sessionStorage.getItem(
      "career_memory_spike_web_import_v1"
    );
    expect(persistedBeforeReload ?? "").not.toContain("synthetic-run-1");
    const firstPostKey = transportBeforeReload.sentRequests.find(
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    )?.headers["Idempotency-Key"];
    expect(firstPostKey).toMatch(UUID_V4_REGEX);
    expect(persistedBeforeReload ?? "").not.toContain(firstPostKey!);

    // Simulate full browser reload: unmount Tab 1 and create brand-new transport + adapter
    tab1.unmount();

    const transportAfterReload = new LoopbackContractHttpTransport(contractUrl);
    const adapterAfterReload = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportAfterReload,
    });

    render(
      <ImportJourneyApp
        adapter={adapterAfterReload}
        harness={adapterAfterReload}
        pollIntervalMs={25}
      />
    );

    // Reload has no runId -> MUST recover via GET /v1/sync-runs?source_resource_id=...&limit=1
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "running"
      );
    });
    expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
      "synthetic-run-1"
    );
    // Must NOT claim the lost request succeeded — explicitly notes run may be historical
    expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
      "이전 이력일 수 있으므로"
    );

    expect(transportAfterReload.sentRequests[0].method).toBe("GET");
    expect(transportAfterReload.sentRequests[0].path).toBe(
      `/v1/sync-runs?source_resource_id=${encodeURIComponent(REPO_1.repoId)}&limit=1`
    );

    // Active run continues polling via detail GET -> transitions to SUCCESS when stub observes it
    await observeRun({
      runId: "synthetic-run-1",
      observation: {
        status: "SUCCESS",
        import_ready: true,
        processing_status: "COMPLETE",
        source_drift: false,
        retry: { allowed: false },
      },
    });

    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });

    const stubState = await getStubState();
    const allCreatePosts = stubState.request_log.filter(
      (entry) => entry.method === "POST" && entry.path === "/v1/sync-runs"
    );
    // Strictly ZERO auto-POST on reload
    expect(allCreatePosts).toHaveLength(1);
    expect(stubState.run_count).toBe(1);
    expect(
      stubState.request_log.some(
        (entry) =>
          entry.method === "GET" &&
          entry.path === "/v1/sync-runs" &&
          entry.query_string === `source_resource_id=${REPO_1.repoId}&limit=1` &&
          entry.status === 200
      )
    ).toBe(true);
    expect(
      stubState.request_log.some(
        (entry) =>
          entry.method === "GET" &&
          entry.path === "/v1/sync-runs/synthetic-run-1" &&
          entry.status === 200
      )
    ).toBe(true);
  });

  it("recovers already TERMINAL runs (SUCCESS and PARTIAL_WITH_LIMIT) after lost-response reload, shows historical caveat, and stops automatic polling", async () => {
    // --- Sub-case A: SUCCESS completed before reload ---
    await resetStub();
    window.sessionStorage.clear();
    const user = userEvent.setup();

    const transport1 = new LoopbackContractHttpTransport(contractUrl);
    transport1.queueAfterResponseHook(
      () => {
        throw new Error("LOST_CREATE_RESPONSE_BEFORE_TERMINAL_RELOAD");
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );

    const adapter1 = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transport1,
    });
    const tab1 = render(
      <ImportJourneyApp adapter={adapter1} harness={adapter1} pollIntervalMs={25} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toBeInTheDocument();
    });
    tab1.unmount();

    // Server finishes run as SUCCESS BEFORE reload
    await observeRun({
      runId: "synthetic-run-1",
      observation: {
        status: "SUCCESS",
        import_ready: true,
        processing_status: "COMPLETE",
        source_drift: false,
        retry: { allowed: false },
      },
    });

    const transportReloadSuccess = new LoopbackContractHttpTransport(contractUrl);
    const adapterReloadSuccess = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportReloadSuccess,
    });
    const tab2 = render(
      <ImportJourneyApp
        adapter={adapterReloadSuccess}
        harness={adapterReloadSuccess}
        pollIntervalMs={25}
      />
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
    expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
      "자동 폴링은 종료했습니다"
    );

    // Wait > 4 poll intervals -> ZERO automatic detail GETs and ZERO auto-POSTs!
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(
      transportReloadSuccess.sentRequests.filter(
        (req) => req.method === "GET" && req.path.startsWith("/v1/sync-runs/")
      )
    ).toHaveLength(0);
    expect(
      transportReloadSuccess.sentRequests.filter((req) => req.method === "POST")
    ).toHaveLength(0);
    tab2.unmount();

    // --- Sub-case B: PARTIAL_WITH_LIMIT completed before reload on REPO_2 ---
    await resetStub();
    window.sessionStorage.clear();

    const transportLimit1 = new LoopbackContractHttpTransport(contractUrl);
    transportLimit1.queueAfterResponseHook(
      () => {
        throw new Error("LOST_CREATE_RESPONSE_LIMIT_RELOAD");
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );
    const adapterLimit1 = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportLimit1,
    });
    const tabLimit1 = render(
      <ImportJourneyApp
        adapter={adapterLimit1}
        harness={adapterLimit1}
        pollIntervalMs={25}
      />
    );

    const repo2Radio = await screen.findByRole("radio", {
      name: /design-tokens-kit/i,
    });
    await user.click(repo2Radio);
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toBeInTheDocument();
    });
    tabLimit1.unmount();

    await observeRun({ runId: "synthetic-run-1", example: "itemLimit" });

    const transportLimitReload = new LoopbackContractHttpTransport(contractUrl);
    const adapterLimitReload = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportLimitReload,
    });
    render(
      <ImportJourneyApp
        adapter={adapterLimitReload}
        harness={adapterLimitReload}
        pollIntervalMs={25}
      />
    );

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
    expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
      "이전 이력일 수 있으므로"
    );

    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(transportLimitReload.sentRequests).toHaveLength(1);
    expect(transportLimitReload.sentRequests[0].path).toBe(
      `/v1/sync-runs?source_resource_id=${encodeURIComponent(REPO_2.repoId)}&limit=1`
    );
  });

  it("uses detail GET (not list GET) when run ID is known for current session+repo and reflects post-SUCCESS BLOCKED_POLICY without restarting auto-polling", async () => {
    await resetStub();
    window.sessionStorage.clear();
    const user = userEvent.setup();

    const transport1 = new LoopbackContractHttpTransport(contractUrl);
    const adapter1 = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transport1,
    });

    const tab1 = render(
      <ImportJourneyApp adapter={adapter1} harness={adapter1} pollIntervalMs={25} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    // Complete as SUCCESS + COMPLETE
    await observeRun({
      runId: "synthetic-run-1",
      observation: {
        status: "SUCCESS",
        import_ready: true,
        processing_status: "COMPLETE",
        source_drift: false,
        retry: { allowed: false },
      },
    });
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveAttribute(
        "data-run-status",
        "succeeded"
      );
    });
    expect(
      screen.queryByTestId("processing-blocked-notice")
    ).not.toBeInTheDocument();

    // Policy changes AFTER SUCCESS -> BLOCKED_POLICY
    await observeRun({
      runId: "synthetic-run-1",
      example: "policyChangedAfterSuccess",
    });

    // 1. Manual detail re-lookup on same tab reflects BLOCKED_POLICY via detail GET
    const requestsBeforeManual = transport1.sentRequests.length;
    await user.click(screen.getByTestId("manual-refresh-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("processing-blocked-notice")).toHaveTextContent(
        "BLOCKED_POLICY"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );
    const manualRequests = transport1.sentRequests.slice(requestsBeforeManual);
    expect(manualRequests).toHaveLength(1);
    expect(manualRequests[0].method).toBe("GET");
    expect(manualRequests[0].path).toBe("/v1/sync-runs/synthetic-run-1");

    // No auto-polling restarts after terminal detail GET
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(transport1.sentRequests.length).toBe(requestsBeforeManual + 1);

    // 2. Reload with known (session, repo) runId -> uses DETAIL GET (NOT list GET)
    tab1.unmount();

    const transportReload = new LoopbackContractHttpTransport(contractUrl);
    const adapterReload = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportReload,
    });

    render(
      <ImportJourneyApp
        adapter={adapterReload}
        harness={adapterReload}
        pollIntervalMs={25}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("processing-blocked-notice")).toHaveTextContent(
        "BLOCKED_POLICY"
      );
    });
    expect(screen.getByTestId("import-ready-badge")).toHaveTextContent(
      "import_ready: true (준비 완료)"
    );

    // Must have called detail GET /v1/sync-runs/synthetic-run-1 and NOT list GET /v1/sync-runs?...
    expect(transportReload.sentRequests).toHaveLength(1);
    expect(transportReload.sentRequests[0].method).toBe("GET");
    expect(transportReload.sentRequests[0].path).toBe(
      "/v1/sync-runs/synthetic-run-1"
    );

    // Still terminal -> zero automatic polling after reload detail GET
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(transportReload.sentRequests).toHaveLength(1);
  });

  it("never auto-POSTs or silently issues a new key on empty list, empty 403, 5xx, or 409 IDEMPOTENCY_KEY_EXPIRED (create & retry) over real 127.0.0.1 HTTP", async () => {
    const user = userEvent.setup();

    // --- 1. Empty list, empty 403 (unauthorized repo), and real 502 on recovery GET ---
    await resetStub({
      resources: [
        [WORKSPACE_ID, REPO_1.repoId],
        [WORKSPACE_ID, REPO_2.repoId],
      ],
      actor: {
        workspace_id: WORKSPACE_ID,
        allowed_resources: [REPO_1.repoId], // REPO_2 -> real empty 403
      },
    });
    window.sessionStorage.clear();

    const transport1 = new LoopbackContractHttpTransport(contractUrl);
    const adapter1 = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport1,
    });
    const mount1 = render(
      <ImportJourneyApp adapter={adapter1} harness={adapter1} pollIntervalMs={25} />
    );

    // Empty list on mount + explicit check -> idle state, zero POST
    await screen.findByTestId("check-repository-status-button");
    await user.click(screen.getByTestId("check-repository-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("reload-recovery-notice")).toHaveTextContent(
        "빈 조회만으로 실패를 확정하지 않으며"
      );
    });

    // Empty 403 on unauthorized REPO_2 recovery GET -> neutral banner, zero POST
    await user.click(screen.getByRole("radio", { name: /design-tokens-kit/i }));
    await user.click(screen.getByTestId("check-repository-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "request_rejected"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "HTTP 403"
    );

    // Real 502 over 127.0.0.1 on REPO_1 recovery GET -> uncertain_outcome, zero POST
    await user.click(screen.getByRole("radio", { name: /ledger-engine/i }));
    await queueStubHttpResponse({
      status: 502,
      body: "",
      method: "GET",
      pathPrefix: "/v1/sync-runs?",
    });
    await user.click(screen.getByTestId("check-repository-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "uncertain_outcome"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
      "HTTP 502"
    );

    await new Promise((resolve) => setTimeout(resolve, 100));
    const stateAfterReadErrors = await getStubState();
    expect(
      stateAfterReadErrors.request_log.filter((entry) => entry.method === "POST")
    ).toHaveLength(0);
    mount1.unmount();

    // --- 2. Real 409 IDEMPOTENCY_KEY_EXPIRED on create (after 25h clock advance) & bodyless retry ---
    await resetStub();
    window.sessionStorage.clear();

    const transport2 = new LoopbackContractHttpTransport(contractUrl);
    const adapter2 = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport2,
    });
    render(
      <ImportJourneyApp adapter={adapter2} harness={adapter2} pollIntervalMs={25} />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });

    // Create accepted by stub, response dropped -> WEB retains pending Idempotency-Key
    transport2.queueAfterResponseHook(
      ({ response }) => {
        expect(response.status).toBe(202);
        throw new Error("DROP_CREATE_BEFORE_EXPIRY");
      },
      (req) => req.method === "POST" && req.path === "/v1/sync-runs"
    );
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });

    // Advance CONTRACT FixtureStore clock by 25 hours (> 24h RECEIPT_RETENTION)
    await advanceStubClock({ hours: 25 });

    // Explicit resend sends the SAME key -> stub replies real 409 IDEMPOTENCY_KEY_EXPIRED
    await user.click(screen.getByTestId("start-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "IDEMPOTENCY_KEY_EXPIRED"
      );
    });
    expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
      "data-fault-kind",
      "uncertain_outcome"
    );

    // Wait and confirm ZERO automatic POST and ZERO silent replacement key!
    await new Promise((resolve) => setTimeout(resolve, 100));
    const stateAfterCreateExpiry = await getStubState();
    const createPosts = stateAfterCreateExpiry.request_log.filter(
      (entry) => entry.method === "POST" && entry.path === "/v1/sync-runs"
    );
    expect(createPosts).toHaveLength(2);
    expect(createPosts[0].status).toBe(202);
    expect(createPosts[1].status).toBe(409);
    expect(createPosts[1].idempotency_key).toBe(createPosts[0].idempotency_key);
    expect(stateAfterCreateExpiry.run_count).toBe(1);

    // User clicks "서버 상태 다시 확인" -> recovers synthetic-run-1 via read-only GET!
    await user.click(
      screen.getByRole("button", { name: "서버 상태 다시 확인" })
    );
    await waitFor(() => {
      expect(screen.getByTestId("run-status-banner")).toHaveTextContent(
        "synthetic-run-1"
      );
    });

    // Transition to retryable transientPartial -> test real 409 IDEMPOTENCY_KEY_EXPIRED on bodyless retry!
    await observeRun({ runId: "synthetic-run-1", example: "transientPartial" });
    await waitFor(() => {
      expect(screen.getByTestId("retry-import-button")).toBeEnabled();
    });

    transport2.queueAfterResponseHook(
      ({ response }) => {
        expect(response.status).toBe(202);
        throw new Error("DROP_RETRY_BEFORE_EXPIRY");
      },
      (req) =>
        req.method === "POST" &&
        req.path === "/v1/sync-runs/synthetic-run-1/retry"
    );
    await user.click(screen.getByTestId("retry-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveAttribute(
        "data-fault-kind",
        "network_error"
      );
    });

    await advanceStubClock({ hours: 25 });
    await user.click(screen.getByTestId("retry-import-button"));
    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "IDEMPOTENCY_KEY_EXPIRED"
      );
    });

    await new Promise((resolve) => setTimeout(resolve, 100));
    const stateAfterRetryExpiry = await getStubState();
    const retryPosts = stateAfterRetryExpiry.request_log.filter(
      (entry) =>
        entry.method === "POST" &&
        entry.path === "/v1/sync-runs/synthetic-run-1/retry"
    );
    expect(retryPosts).toHaveLength(2);
    expect(retryPosts[0].status).toBe(202);
    expect(retryPosts[1].status).toBe(409);
    expect(retryPosts[1].body_length).toBe(0);
    expect(retryPosts[1].idempotency_key).toBe(retryPosts[0].idempotency_key);
    expect(stateAfterRetryExpiry.retry_replay_count).toBe(1);
  });

  it("rejects mismatched target_id/status_url in recovery GET response without polling mismatched path or auto-POSTing", async () => {
    await resetStub();
    await seedExistingRun({
      repoId: REPO_1.repoId,
      key: "00000000-0000-4000-8000-000000000601",
    });

    const transport = new LoopbackContractHttpTransport(contractUrl);
    transport.queueAfterResponseHook(
      ({ response }) => {
        expect(response.status).toBe(200);
        const parsed = JSON.parse(response.rawBody ?? "") as {
          items: Array<{
            target_id: string;
            status_url: string;
            import_status: Record<string, unknown>;
          }>;
        };
        parsed.items[0].status_url = "/v1/sync-runs/synthetic-run-mismatch";
        return {
          ...response,
          rawBody: JSON.stringify(parsed),
        };
      },
      (req) => req.method === "GET" && req.path.startsWith("/v1/sync-runs?")
    );

    const adapter = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transport,
    });

    render(
      <ImportJourneyApp adapter={adapter} harness={adapter} pollIntervalMs={25} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("unconfirmed-fault-banner")).toHaveTextContent(
        "불일치"
      );
    });
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();

    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(
      transport.sentRequests.some((req) =>
        req.path.includes("synthetic-run-mismatch")
      )
    ).toBe(false);
    expect(
      transport.sentRequests.filter((req) => req.method === "POST")
    ).toHaveLength(0);
  });

  it("discards late real 127.0.0.1 recovery GET responses arriving after repository switch or session change", async () => {
    const user = userEvent.setup();

    // --- Part A: Repository switch (REPO_1 -> REPO_2) while REPO_1 recovery GET is in flight ---
    await resetStub();
    window.sessionStorage.clear();

    const transportRepo = new LoopbackContractHttpTransport(contractUrl);
    const adapterRepo = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: true,
      syncRunTransport: transportRepo,
    });

    const repoMount = render(
      <ImportJourneyApp
        adapter={adapterRepo}
        harness={adapterRepo}
        pollIntervalMs={25}
      />
    );

    const repo2Radio = await screen.findByRole("radio", {
      name: /design-tokens-kit/i,
    });

    // Seed an active run on REPO_1 after initial mount, then trigger a slow recovery GET for REPO_1
    await seedExistingRun({
      repoId: REPO_1.repoId,
      key: "00000000-0000-4000-8000-000000000602",
    });

    const holdRepo1Lookup = createDeferred<void>();
    let repo1LookupReachedStub = false;
    transportRepo.queueAfterResponseHook(
      async ({ response }) => {
        expect(response.status).toBe(200);
        repo1LookupReachedStub = true;
        await holdRepo1Lookup.promise;
        return response;
      },
      (req) =>
        req.method === "GET" &&
        req.path ===
          `/v1/sync-runs?source_resource_id=${encodeURIComponent(REPO_1.repoId)}&limit=1`
    );

    await user.click(screen.getByTestId("check-repository-status-button"));
    await waitFor(() => {
      expect(repo1LookupReachedStub).toBe(true);
    });

    // Switch to REPO_2 while REPO_1 lookup is still in flight
    await user.click(repo2Radio);
    expect(repo2Radio).toBeChecked();

    // Release REPO_1's late real 200 recovery response -> MUST be discarded!
    holdRepo1Lookup.resolve();
    await new Promise((resolve) => setTimeout(resolve, 100));

    expect(repo2Radio).toBeChecked();
    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    expect(
      transportRepo.sentRequests.some(
        (req) =>
          req.method === "GET" && req.path === "/v1/sync-runs/synthetic-run-1"
      )
    ).toBe(false);
    expect(
      transportRepo.sentRequests.filter((req) => req.method === "POST")
    ).toHaveLength(0);
    repoMount.unmount();

    // --- Part B: Session change (logout -> re-login) while Session 1 recovery GET is in flight ---
    await resetStub();
    window.sessionStorage.clear();

    const transportSession = new LoopbackContractHttpTransport(contractUrl);
    const adapterSession = new MockFixtureImportAdapter({
      latencyMs: 0,
      disableBrowserStorage: false,
      syncRunTransport: transportSession,
    });

    render(
      <ImportJourneyApp
        adapter={adapterSession}
        harness={adapterSession}
        pollIntervalMs={25}
      />
    );

    await screen.findByRole("radio", { name: /ledger-engine/i });
    await seedExistingRun({
      repoId: REPO_1.repoId,
      key: "00000000-0000-4000-8000-000000000603",
    });

    const holdSession1Lookup = createDeferred<void>();
    let session1LookupReachedStub = false;
    transportSession.queueAfterResponseHook(
      async ({ response }) => {
        expect(response.status).toBe(200);
        session1LookupReachedStub = true;
        await holdSession1Lookup.promise;
        return response;
      },
      (req) => req.method === "GET" && req.path.startsWith("/v1/sync-runs?")
    );

    await user.click(screen.getByTestId("check-repository-status-button"));
    await waitFor(() => {
      expect(session1LookupReachedStub).toBe(true);
    });

    // Logout and sign in as Session 2 while Session 1's lookup is in flight
    await user.click(screen.getByRole("button", { name: "모의 로그아웃" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그아웃"
    );
    await user.click(screen.getByRole("button", { name: "모의 서비스 로그인" }));
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent(
      "로그인 활성"
    );

    // Release Session 1's late real 200 recovery response -> MUST be discarded!
    holdSession1Lookup.resolve();
    await new Promise((resolve) => setTimeout(resolve, 100));

    expect(screen.queryByTestId("import-run-details")).not.toBeInTheDocument();
    const persisted = window.sessionStorage.getItem(
      "career_memory_spike_web_import_v1"
    );
    expect(persisted ?? "").not.toContain("synthetic-run-1");
    expect(
      transportSession.sentRequests.some(
        (req) =>
          req.method === "GET" && req.path === "/v1/sync-runs/synthetic-run-1"
      )
    ).toBe(false);
    expect(
      transportSession.sentRequests.filter((req) => req.method === "POST")
    ).toHaveLength(0);
  });
});
