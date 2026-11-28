import React from "react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { FetchMemoryOpsHttpTransport } from "@/adapters/memory-ops-http-transport";
import {
  generateUuidV4,
  UUID_V4_REGEX,
} from "@/adapters/synthetic-http-transport";
import { MemoryOpsApp } from "@/components/memory-ops/MemoryOpsApp";

/**
 * Isolated 127.0.0.1 ephemeral-port HTTP contract integration tests for
 * `FetchMemoryOpsHttpTransport` & `MemoryOpsApp` (M5 Wave 2B).
 */

interface RecordedRequest {
  method: string;
  path: string;
  query: string;
  authorization: string | null;
  workspace_id: string | null;
  idempotency_key: string | null;
  request_id: string | null;
  status: number;
  body: Record<string, unknown> | null;
}

let stubProc: ChildProcessWithoutNullStreams | null = null;
let baseUrl = "";

async function startMemoryOpsStub(): Promise<{
  proc: ChildProcessWithoutNullStreams;
  baseUrl: string;
}> {
  const scriptPath = path.resolve(
    process.cwd(),
    "tests/contract-http/run_memory_ops_stub.py"
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
  child.stderr.on("data", (chunk) => stderrChunks.push(chunk.toString("utf8")));

  const readyUrl = await new Promise<string>((resolve, reject) => {
    const rl = readline.createInterface({ input: child.stdout });
    const timeout = setTimeout(() => {
      rl.close();
      reject(
        new Error(
          `Timed out waiting for memory ops stub startup. stderr:\n${stderrChunks.join("")}`
        )
      );
    }, 20_000);

    rl.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) return;
      try {
        const parsed = JSON.parse(trimmed) as { baseUrl?: string };
        if (parsed.baseUrl) {
          clearTimeout(timeout);
          rl.close();
          resolve(parsed.baseUrl);
        }
      } catch {
        // ignore
      }
    });

    child.once("exit", (code) => {
      clearTimeout(timeout);
      rl.close();
      reject(
        new Error(
          `Memory ops stub exited prematurely (${code}). stderr:\n${stderrChunks.join("")}`
        )
      );
    });
  });

  return { proc: child, baseUrl: readyUrl };
}

async function stopStub(): Promise<void> {
  const child = stubProc;
  stubProc = null;
  if (!child || child.exitCode !== null) return;
  await new Promise<void>((resolve) => {
    const force = setTimeout(() => {
      if (child.exitCode === null) child.kill("SIGKILL");
      resolve();
    }, 3_000);
    child.once("exit", () => {
      clearTimeout(force);
      resolve();
    });
    child.kill("SIGTERM");
  });
}

async function resetStub(): Promise<void> {
  const res = await fetch(`${baseUrl}/__control/reset`, { method: "POST" });
  if (!res.ok) throw new Error(`reset failed: ${res.status}`);
}

async function bumpIndexVersion(): Promise<void> {
  const res = await fetch(`${baseUrl}/__control/bump-index-version`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`bump-index-version failed: ${res.status}`);
}

async function armScopeDrift(): Promise<void> {
  const res = await fetch(`${baseUrl}/__control/arm-scope-drift`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`arm-scope-drift failed: ${res.status}`);
}

async function completeDeletionJob(deletionJobId: string): Promise<void> {
  const res = await fetch(`${baseUrl}/__control/complete-deletion-job`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ deletion_job_id: deletionJobId }),
  });
  if (!res.ok) throw new Error(`complete-deletion-job failed: ${res.status}`);
}

async function setQuotaMode(
  mode: "ALLOWED" | "WARNING_80_PERCENT" | "CUTOFF_95_PERCENT"
): Promise<void> {
  const res = await fetch(`${baseUrl}/__control/set-quota-mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!res.ok) throw new Error(`set-quota-mode failed: ${res.status}`);
}

async function getRecordedRequests(): Promise<RecordedRequest[]> {
  const res = await fetch(`${baseUrl}/__control/requests`);
  const data = (await res.json()) as { requests: RecordedRequest[] };
  return data.requests;
}

beforeAll(async () => {
  const started = await startMemoryOpsStub();
  stubProc = started.proc;
  baseUrl = started.baseUrl;
});

afterEach(() => {
  cleanup();
});

afterAll(async () => {
  await stopStub();
});

describe("M5 Memory Ops real 127.0.0.1 FastAPI HTTP wire contract", () => {
  it("verifies /v1/search pagination, 400 INVALID_SEARCH_CURSOR, 422 PROHIBITED_PROJECT_ID_SCOPE, OD-003 preview->409->202->PARTIAL->SUCCEEDED, and /v1/quota-status 80%/95%", async () => {
    await resetStub();
    const transport = new FetchMemoryOpsHttpTransport({
      baseUrl,
      workspaceId: "ws_http_m5",
      authorization: "Bearer test-m5-token",
    });

    // 1. /v1/search page 1 -> page 2 -> cursor drift (400 INVALID_SEARCH_CURSOR)
    const page1 = await transport.searchMemory({
      q: "webhook",
      entity_types: ["WORK_EVENT", "FACT", "CLAIM"],
      limit: 2,
    });
    expect(page1.ok).toBe(true);
    expect(page1.meta?.cacheControl).toBe("private, no-store");
    if (!page1.ok) return;
    expect(page1.data.items).toHaveLength(2);
    expect(page1.data.next_cursor).toBeTruthy();

    const page2 = await transport.searchMemory({
      q: "webhook",
      entity_types: ["WORK_EVENT", "FACT", "CLAIM"],
      limit: 2,
      cursor: page1.data.next_cursor,
    });
    expect(page2.ok).toBe(true);
    if (page2.ok) {
      expect(page2.data.items).toHaveLength(2);
      expect(page2.data.next_cursor).toBeNull();
    }

    // Bump index version -> stale cursor returns 400 INVALID_SEARCH_CURSOR
    await bumpIndexVersion();
    const drifted = await transport.searchMemory({
      q: "webhook",
      entity_types: ["WORK_EVENT", "FACT", "CLAIM"],
      limit: 2,
      cursor: page1.data.next_cursor,
    });
    expect(drifted.ok).toBe(false);
    if (!drifted.ok) {
      expect(drifted.error.httpStatus).toBe(400);
      expect(drifted.error.kind).toBe("invalid_search_cursor");
      expect(drifted.error.code).toBe("INVALID_SEARCH_CURSOR");
    }

    // Direct 422 PROHIBITED_PROJECT_ID_SCOPE check against server + client guard check
    await expect(
      transport.searchMemory({
        q: "webhook",
        project_id: "proj_forbidden",
      } as unknown as Parameters<typeof transport.searchMemory>[0])
    ).rejects.toThrow(/OD-008/);

    const rawProhibited = await fetch(
      `${baseUrl}/v1/search?q=webhook&project_id=proj_forbidden`,
      {
        headers: {
          Authorization: "Bearer test-m5-token",
          "X-Workspace-ID": "ws_http_m5",
        },
      }
    );
    expect(rawProhibited.status).toBe(422);
    const rawProhibitedJson = (await rawProhibited.json()) as {
      error: { code: string };
    };
    expect(rawProhibitedJson.error.code).toBe("PROHIBITED_PROJECT_ID_SCOPE");

    // 2. OD-003 deletion-preview -> 409 DELETION_PREVIEW_SCOPE_CHANGED -> refresh -> 202 confirm -> idempotent replay
    await armScopeDrift();
    const preview1 = await transport.previewSourceDeletion("src_repo_ledger_01");
    expect(preview1.ok).toBe(true);
    if (!preview1.ok) return;
    expect(preview1.data.impact_counts.claims_approved).toBe(3);
    expect(preview1.data.non_recoverable_scopes.already_downloaded_exports).toBe(
      true
    );

    const confirm409 = await transport.confirmSourceDeletion({
      source_resource_id: "src_repo_ledger_01",
      preview_token: preview1.data.preview_token,
      expected_resource_version: preview1.data.resource_version,
      expected_impact_scope_version: preview1.data.impact_scope_version,
      acknowledge_irreversible_purge: true,
      idempotency_key: generateUuidV4(),
    });
    expect(confirm409.ok).toBe(false);
    if (!confirm409.ok) {
      expect(confirm409.error.httpStatus).toBe(409);
      expect(confirm409.error.kind).toBe("deletion_preview_scope_changed");
      expect(confirm409.error.code).toBe("DELETION_PREVIEW_SCOPE_CHANGED");
    }

    const preview2 = await transport.previewSourceDeletion("src_repo_ledger_01");
    expect(preview2.ok).toBe(true);
    if (!preview2.ok) return;
    expect(preview2.data.resource_version).toBe("rv_http_02");

    const preservedKey = generateUuidV4();
    const confirm202 = await transport.confirmSourceDeletion({
      source_resource_id: "src_repo_ledger_01",
      preview_token: preview2.data.preview_token,
      expected_resource_version: preview2.data.resource_version,
      expected_impact_scope_version: preview2.data.impact_scope_version,
      acknowledge_irreversible_purge: true,
      idempotency_key: preservedKey,
    });
    expect(confirm202.ok).toBe(true);
    expect(confirm202.meta?.httpStatus).toBe(202);
    if (!confirm202.ok) return;
    expect(confirm202.data.status).toBe("PENDING");
    expect(confirm202.data.fence_active).toBe(true);
    const jobId = confirm202.data.deletion_job_id;

    // Retry with same preserved Idempotency-Key returns same deletion_job_id
    const confirmReplay = await transport.confirmSourceDeletion({
      source_resource_id: "src_repo_ledger_01",
      preview_token: preview2.data.preview_token,
      expected_resource_version: preview2.data.resource_version,
      expected_impact_scope_version: preview2.data.impact_scope_version,
      acknowledge_irreversible_purge: true,
      idempotency_key: preservedKey,
    });
    expect(confirmReplay.ok).toBe(true);
    if (confirmReplay.ok) {
      expect(confirmReplay.data.deletion_job_id).toBe(jobId);
    }

    // 3. GET /v1/deletion-jobs/{id}: PARTIAL -> SUCCEEDED
    const jobPartial = await transport.getDeletionJobStatus(jobId);
    expect(jobPartial.ok).toBe(true);
    if (!jobPartial.ok) return;
    expect(jobPartial.data.status).toBe("PARTIAL");
    expect(jobPartial.data.user_visible_state).toBe("DELETION_PENDING");
    expect(jobPartial.data.components).toHaveLength(5);

    await completeDeletionJob(jobId);
    const jobDone = await transport.getDeletionJobStatus(jobId);
    expect(jobDone.ok).toBe(true);
    if (jobDone.ok) {
      expect(jobDone.data.status).toBe("SUCCEEDED");
      expect(jobDone.data.user_visible_state).toBe("DELETED");
      expect(
        jobDone.data.components.every((c) => c.status === "SUCCEEDED")
      ).toBe(true);
    }

    // 4. GET /v1/quota-status: WARNING_80_PERCENT & CUTOFF_95_PERCENT
    await setQuotaMode("WARNING_80_PERCENT");
    const quota80 = await transport.getQuotaStatus({
      source_resource_id: "src_repo_portfolio_02",
    });
    expect(quota80.ok).toBe(true);
    if (quota80.ok) {
      expect(quota80.data.admission_state).toBe("WARNING_80_PERCENT");
      expect(quota80.data.high_cost_operations_allowed).toBe(true);
      expect(quota80.data.safety_operations_allowed).toBe(true);
      expect(quota80.data.paid_fallback_allowed).toBe(false);
    }

    await setQuotaMode("CUTOFF_95_PERCENT");
    const quota95 = await transport.getQuotaStatus();
    expect(quota95.ok).toBe(true);
    if (quota95.ok) {
      expect(quota95.data.admission_state).toBe("CUTOFF_95_PERCENT");
      expect(quota95.data.pause_state).toBe("PAUSED_QUOTA");
      expect(quota95.data.high_cost_operations_allowed).toBe(false);
      expect(quota95.data.safety_operations_allowed).toBe(true);
      expect(quota95.data.paid_fallback_allowed).toBe(false);
      expect(quota95.data.retry_after_seconds).toBe(900);
    }

    // Verify exact wire headers recorded by FastAPI
    const logs = await getRecordedRequests();
    const confirmLog = logs.find(
      (r) =>
        r.method === "POST" &&
        r.path === "/v1/source-resources/src_repo_ledger_01/deletion-confirm" &&
        r.status === 202
    );
    expect(confirmLog?.authorization).toBe("Bearer test-m5-token");
    expect(confirmLog?.workspace_id).toBe("ws_http_m5");
    expect(confirmLog?.idempotency_key).toBe(preservedKey);
    expect(confirmLog?.request_id).toMatch(UUID_V4_REGEX);
  });

  it("renders MemoryOpsApp wired to live loopback FetchMemoryOpsHttpTransport for 95% cutoff + OD-003 deletion-confirm", async () => {
    await resetStub();
    await setQuotaMode("CUTOFF_95_PERCENT");
    const user = userEvent.setup();
    const transport = new FetchMemoryOpsHttpTransport({
      baseUrl,
      workspaceId: "ws_http_m5",
      authorization: "Bearer test-m5-token",
    });

    render(
      <MemoryOpsApp
        adapter={transport}
        initialRepositories={[
          {
            source_resource_id: "src_repo_ledger_01",
            repository_full_name: "seoyeon-lab-demo/ledger-engine",
            provider: "GITHUB",
            visibility: "PUBLIC",
            fence_active: false,
            deleted: false,
            active_deletion_job_id: null,
          },
          {
            source_resource_id: "src_repo_portfolio_02",
            repository_full_name: "seoyeon-lab-demo/portfolio-telemetry",
            provider: "GITHUB",
            visibility: "PUBLIC",
            fence_active: false,
            deleted: false,
            active_deletion_job_id: null,
          },
        ]}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("quota-admission-badge")).toHaveTextContent(
        "CUTOFF_95_PERCENT"
      );
    });
    expect(
      screen.getByTestId("repo-high-cost-action-src_repo_ledger_01")
    ).toBeDisabled();
    expect(
      screen.getByTestId("open-source-deletion-src_repo_ledger_01")
    ).toBeEnabled();

    await user.click(
      screen.getByTestId("open-source-deletion-src_repo_ledger_01")
    );
    await screen.findByTestId("deletion-impact-counts-section");
    await user.click(screen.getByTestId("deletion-ack-checkbox"));
    await user.click(screen.getByTestId("confirm-source-deletion-button"));

    const receipt = await screen.findByTestId("deletion-job-receipt-panel");
    expect(receipt).toHaveAttribute(
      "data-user-visible-state",
      "DELETION_PENDING"
    );
  });
});
