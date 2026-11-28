import React from "react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { FetchMemoryOpsHttpTransport } from "@/adapters/memory-ops-http-transport";
import { MemoryOpsApp } from "@/components/memory-ops/MemoryOpsApp";

/**
 * Live 127.0.0.1 loopback interoperability test between `MemoryOpsApp` +
 * `FetchMemoryOpsHttpTransport` and the REAL Python `career_memory_api` M5 FastAPI
 * server backed by PostgreSQL 18 (`0010..0080`, `cm_api_app`/`cm_worker_app`
 * `NOBYPASSRLS` + `FORCE RLS`) wrapped with `EdgeOriginAndCacheMiddleware`:
 *
 * 1. Live `/v1/quota-status` (`ALLOWED` -> `WARNING_80_PERCENT` -> `CUTOFF_95_PERCENT`)
 *    rendered in `QuotaBudgetBanner` with `paid_fallback_allowed=false`.
 * 2. Live PG18 FTS `/v1/search` across `WORK_EVENT`, `FACT`, `CLAIM`, cursor pagination,
 *    and `OD-008` repository scoping (`source_resource_id`, zero `project_id`).
 * 3. Live `OD-003` `deletion-preview` (real non-zero DB `impact_counts`) ->
 *    `deletion-confirm` (`202`) -> live `GET /v1/deletion-jobs/{id}`
 *    (`DELETION_PENDING` -> `DELETED`), verifying `/v1/search` immediately returns `0`
 *    hits for the deleted repo while preserving an independent second repository.
 * 4. Verifies `EdgeOriginAndCacheMiddleware` blocks direct calls missing origin auth
 *    (`403 ORIGIN_AUTH_REQUIRED`) and enforces `Cache-Control: private, no-store`.
 */

interface SeededRepoInfo {
  source_resource_id: string;
  repository_full_name: string;
  event_id: string;
  extra_event_id: string | null;
  fact_id: string;
  claim_id: string;
  export_id: string;
  content_unit_id: string;
}

interface LiveM5Bootstrap {
  baseUrl: string;
  workspace_id: string;
  origin_auth_secret: string;
  repo_a: SeededRepoInfo;
  repo_b: SeededRepoInfo;
  canary_a: string;
  canary_b: string;
}

let backendProc: ChildProcessWithoutNullStreams | null = null;
let bootstrap: LiveM5Bootstrap | null = null;

async function startLiveM5Backend(): Promise<{
  proc: ChildProcessWithoutNullStreams;
  info: LiveM5Bootstrap;
}> {
  const apiDir = path.resolve(process.cwd(), "../../apps/api");
  const scriptPath = path.resolve(
    process.cwd(),
    "tests/contract-http/run_live_memory_ops_backend.py"
  );
  const child = spawn(
    "uv",
    ["run", "--with", "uvicorn==0.34.0", "python", "-B", scriptPath],
    {
      cwd: apiDir,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    }
  );

  const stderrChunks: string[] = [];
  child.stderr.on("data", (chunk) => stderrChunks.push(chunk.toString("utf8")));

  const info = await new Promise<LiveM5Bootstrap>((resolve, reject) => {
    const rl = readline.createInterface({ input: child.stdout });
    const timeout = setTimeout(() => {
      rl.close();
      reject(
        new Error(
          `Timed out waiting for live M5 career_memory_api backend. stderr:\n${stderrChunks.join("")}`
        )
      );
    }, 45_000);

    rl.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) return;
      try {
        const parsed = JSON.parse(trimmed) as Partial<LiveM5Bootstrap>;
        if (parsed.baseUrl && parsed.repo_a && parsed.repo_b) {
          clearTimeout(timeout);
          rl.close();
          resolve(parsed as LiveM5Bootstrap);
        }
      } catch {
        // ignore non-JSON lines
      }
    });

    child.once("exit", (code) => {
      clearTimeout(timeout);
      rl.close();
      reject(
        new Error(
          `Live M5 career_memory_api backend exited prematurely (${code}). stderr:\n${stderrChunks.join("")}`
        )
      );
    });
  });

  return { proc: child, info };
}

async function stopLiveM5Backend(): Promise<void> {
  const child = backendProc;
  backendProc = null;
  if (!child || child.exitCode !== null) return;
  await new Promise<void>((resolve) => {
    const force = setTimeout(() => {
      if (child.exitCode === null) child.kill("SIGKILL");
      resolve();
    }, 5_000);
    child.once("exit", () => {
      clearTimeout(force);
      resolve();
    });
    child.kill("SIGTERM");
  });
}

async function resetLiveM5Backend(): Promise<LiveM5Bootstrap> {
  if (!bootstrap) throw new Error("live M5 backend not started");
  const res = await fetch(`${bootstrap.baseUrl}/__control/reset`, {
    method: "POST",
    headers: { "X-CM-Origin-Auth": bootstrap.origin_auth_secret },
  });
  if (!res.ok) throw new Error(`reset failed: ${res.status}`);
  const data = (await res.json()) as Omit<LiveM5Bootstrap, "baseUrl">;
  bootstrap = { baseUrl: bootstrap.baseUrl, ...data };
  return bootstrap;
}

async function setLiveQuotaMode(
  mode: "ALLOWED" | "WARNING_80_PERCENT" | "CUTOFF_95_PERCENT"
): Promise<void> {
  if (!bootstrap) throw new Error("live M5 backend not started");
  const res = await fetch(`${bootstrap.baseUrl}/__control/set-quota-mode`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CM-Origin-Auth": bootstrap.origin_auth_secret,
    },
    body: JSON.stringify({ mode }),
  });
  if (!res.ok) throw new Error(`set-quota-mode failed: ${res.status}`);
}

async function executeLiveDeletionJob(
  deletionJobId: string,
  failComponent?: string
): Promise<Record<string, unknown>> {
  if (!bootstrap) throw new Error("live M5 backend not started");
  const res = await fetch(
    `${bootstrap.baseUrl}/__control/execute-deletion-job`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CM-Origin-Auth": bootstrap.origin_auth_secret,
      },
      body: JSON.stringify({
        deletion_job_id: deletionJobId,
        ...(failComponent ? { fail_component: failComponent } : {}),
      }),
    }
  );
  if (!res.ok) throw new Error(`execute-deletion-job failed: ${res.status}`);
  return (await res.json()) as Record<string, unknown>;
}

beforeAll(async () => {
  const started = await startLiveM5Backend();
  backendProc = started.proc;
  bootstrap = started.info;
}, 50_000);

afterEach(() => {
  cleanup();
});

afterAll(async () => {
  await stopLiveM5Backend();
});

describe("Live PG18 M5 FastAPI + EdgeGuard <-> MemoryOpsApp & FetchMemoryOpsHttpTransport", () => {
  it("verifies EdgeGuard origin auth, live /v1/quota-status transitions, PG18 FTS search pagination & OD-008 scoping, and OD-003 deletion preview->confirm->PARTIAL->DELETED preserving Repo B", async () => {
    const ctx = await resetLiveM5Backend();
    const user = userEvent.setup();

    // 0. EdgeOriginAndCacheMiddleware blocks direct bypass missing X-CM-Origin-Auth (403)
    const bypassRes = await fetch(`${ctx.baseUrl}/v1/quota-status`, {
      headers: {
        Authorization: "Bearer live-m5-token",
        "X-Workspace-ID": ctx.workspace_id,
      },
    });
    expect(bypassRes.status).toBe(403);
    expect(bypassRes.headers.get("cache-control")).toBe("private, no-store");
    const bypassJson = (await bypassRes.json()) as {
      error: { code: string };
    };
    expect(bypassJson.error.code).toBe("ORIGIN_AUTH_REQUIRED");

    const transport = new FetchMemoryOpsHttpTransport({
      baseUrl: ctx.baseUrl,
      workspaceId: ctx.workspace_id,
      authorization: "Bearer live-m5-token",
      defaultHeaders: {
        "X-CM-Origin-Auth": ctx.origin_auth_secret,
      },
    });

    // 1. Render MemoryOpsApp backed by real PG18 server
    render(
      <MemoryOpsApp
        adapter={transport}
        initialQuery="webhook"
        initialRepositories={[
          {
            source_resource_id: ctx.repo_a.source_resource_id,
            repository_full_name: ctx.repo_a.repository_full_name,
            provider: "GITHUB",
            visibility: "PUBLIC",
            fence_active: false,
            deleted: false,
            active_deletion_job_id: null,
          },
          {
            source_resource_id: ctx.repo_b.source_resource_id,
            repository_full_name: ctx.repo_b.repository_full_name,
            provider: "GITHUB",
            visibility: "PUBLIC",
            fence_active: false,
            deleted: false,
            active_deletion_job_id: null,
          },
        ]}
      />
    );

    // 1a. Quota starts ALLOWED
    await waitFor(() => {
      expect(screen.getByTestId("quota-budget-banner")).toHaveAttribute(
        "data-admission-state",
        "ALLOWED"
      );
    });

    // 1b. Transition real PG18 ledger to WARNING_80_PERCENT and refresh banner
    await setLiveQuotaMode("WARNING_80_PERCENT");
    await user.click(screen.getByTestId("refresh-quota-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("quota-budget-banner")).toHaveAttribute(
        "data-admission-state",
        "WARNING_80_PERCENT"
      );
    });

    // 1c. Transition real PG18 ledger to CUTOFF_95_PERCENT -> high-cost disabled, OD-003 deletion stays enabled!
    await setLiveQuotaMode("CUTOFF_95_PERCENT");
    await user.click(screen.getByTestId("refresh-quota-status-button"));
    await waitFor(() => {
      expect(screen.getByTestId("quota-budget-banner")).toHaveAttribute(
        "data-admission-state",
        "CUTOFF_95_PERCENT"
      );
    });
    expect(
      screen.getByTestId(
        `repo-high-cost-action-${ctx.repo_a.source_resource_id}`
      )
    ).toBeDisabled();
    expect(
      screen.getByTestId(
        `open-source-deletion-${ctx.repo_a.source_resource_id}`
      )
    ).toBeEnabled();

    // 2. Live PG18 FTS search + cursor pagination + OD-008 repository scoping
    await waitFor(() => {
      expect(screen.getByTestId("search-results-list").children.length).toBe(3);
    });
    expect(screen.getByTestId("load-next-search-page-button")).toBeEnabled();

    // Load page 2 via real opaque PG18 cursor
    await user.click(screen.getByTestId("load-next-search-page-button"));
    await waitFor(() => {
      expect(
        screen.getByTestId("search-results-list").children.length
      ).toBeGreaterThanOrEqual(6);
    });

    // Verify all 3 entity types (WORK_EVENT, FACT, CLAIM) are returned by live PG18 FTS
    const directSearchAll = await transport.searchMemory({
      q: "webhook",
      entity_types: ["WORK_EVENT", "FACT", "CLAIM"],
      limit: 20,
    });
    expect(directSearchAll.ok).toBe(true);
    expect(directSearchAll.meta?.cacheControl).toBe("private, no-store");
    if (!directSearchAll.ok) return;
    const seenTypes = new Set(directSearchAll.data.items.map((i) => i.entity_type));
    expect(seenTypes.has("WORK_EVENT")).toBe(true);
    expect(seenTypes.has("FACT")).toBe(true);
    expect(seenTypes.has("CLAIM")).toBe(true);
    expect(JSON.stringify(directSearchAll.data)).not.toContain(ctx.canary_a);
    expect(JSON.stringify(directSearchAll.data)).not.toContain(ctx.canary_b);

    // OD-008 scope to Repo B -> only Repo B hits returned
    await user.selectOptions(
      screen.getByTestId("search-source-resource-select"),
      ctx.repo_b.source_resource_id
    );
    await waitFor(() => {
      expect(screen.getByTestId("search-response-meta")).toHaveTextContent(
        `scope.source_resource_id=${ctx.repo_b.source_resource_id}`
      );
    });
    const repoBItems = Array.from(
      screen.getByTestId("search-results-list").children
    );
    expect(repoBItems.length).toBe(3);

    // 3. Live OD-003 deletion-preview -> deletion-confirm (202) -> DELETION_PENDING -> DELETED
    //    (performed while workspace is STILL at CUTOFF_95_PERCENT!)
    await user.click(
      screen.getByTestId(
        `open-source-deletion-${ctx.repo_a.source_resource_id}`
      )
    );
    await screen.findByTestId("deletion-impact-counts-section");
    expect(screen.getByTestId("impact-count-work_events")).toHaveTextContent(
      "2건"
    );
    expect(screen.getByTestId("impact-count-facts")).toHaveTextContent("1건");
    expect(
      screen.getByTestId("impact-count-claims_approved")
    ).toHaveTextContent("1건");
    expect(screen.getByTestId("impact-count-server_exports")).toHaveTextContent(
      "1건"
    );

    await user.click(screen.getByTestId("deletion-ack-checkbox"));
    await user.click(screen.getByTestId("confirm-source-deletion-button"));

    const receipt = await screen.findByTestId("deletion-job-receipt-panel");
    expect(receipt).toHaveAttribute(
      "data-user-visible-state",
      "DELETION_PENDING"
    );
    expect(receipt).toHaveAttribute("data-fence-active", "true");

    // Extract live deletion_job_id from the rendered receipt panel subtitle
    const subtitleText = receipt.textContent ?? "";
    const jobIdMatch = subtitleText.match(/deletion_job_id=([0-9a-f-]{36})/);
    expect(jobIdMatch).not.toBeNull();
    const deletionJobId = jobIdMatch![1]!;

    // Immediately after 202 confirm (before worker finishes), live /v1/search for Repo A returns 0 hits!
    const searchRepoAImmediate = await transport.searchMemory({
      q: "webhook",
      source_resource_id: ctx.repo_a.source_resource_id,
      limit: 20,
    });
    expect(searchRepoAImmediate.ok).toBe(true);
    if (searchRepoAImmediate.ok) {
      expect(searchRepoAImmediate.data.items).toHaveLength(0);
    }

    // Run worker with partial failure at FTS_INDEX -> stays DELETION_PENDING
    await executeLiveDeletionJob(deletionJobId, "FTS_INDEX");
    await user.click(screen.getByTestId("refresh-deletion-receipt-button"));
    await waitFor(() => {
      expect(screen.getByTestId("deletion-job-receipt-panel")).toHaveAttribute(
        "data-internal-status",
        "PARTIAL"
      );
    });
    expect(screen.getByTestId("deletion-job-receipt-panel")).toHaveAttribute(
      "data-user-visible-state",
      "DELETION_PENDING"
    );

    // Complete remaining components on live PG18 -> receipt transitions to DELETED!
    await executeLiveDeletionJob(deletionJobId);
    await user.click(screen.getByTestId("refresh-deletion-receipt-button"));
    await waitFor(() => {
      expect(screen.getByTestId("deletion-job-receipt-panel")).toHaveAttribute(
        "data-user-visible-state",
        "DELETED"
      );
    });
    expect(screen.getByTestId("deletion-job-receipt-panel")).toHaveAttribute(
      "data-internal-status",
      "SUCCEEDED"
    );

    // Final live PG18 /v1/search check: Repo A = 0 hits, Repo B = 3 preserved hits!
    const searchRepoAAfter = await transport.searchMemory({
      q: "webhook",
      source_resource_id: ctx.repo_a.source_resource_id,
      limit: 20,
    });
    expect(searchRepoAAfter.ok).toBe(true);
    if (searchRepoAAfter.ok) {
      expect(searchRepoAAfter.data.items).toHaveLength(0);
    }

    const searchRepoBAfter = await transport.searchMemory({
      q: "webhook",
      source_resource_id: ctx.repo_b.source_resource_id,
      limit: 20,
    });
    expect(searchRepoBAfter.ok).toBe(true);
    if (searchRepoBAfter.ok) {
      expect(searchRepoBAfter.data.items).toHaveLength(3);
      expect(
        searchRepoBAfter.data.items.every(
          (item) => item.source_resource_id === ctx.repo_b.source_resource_id
        )
      ).toBe(true);
    }
  }, 45_000);
});
