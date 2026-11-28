import React from "react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { FetchEventReviewHttpTransport } from "@/adapters/event-review-http-transport";
import { UUID_V4_REGEX } from "@/adapters/synthetic-http-transport";
import { EventInboxApp } from "@/components/event-review/EventInboxApp";

/**
 * Isolated 127.0.0.1 ephemeral-port HTTP contract integration tests for
 * `FetchEventReviewHttpTransport` & `EventInboxApp` (T-303 / T-304 / ER-004).
 */

interface RecordedStubRequest {
  method: string;
  path: string;
  query: string;
  if_match: string | null;
  idempotency_key: string | null;
  request_id: string | null;
  status: number;
}

let stubProc: ChildProcessWithoutNullStreams | null = null;
let baseUrl = "";

async function startEventReviewStub(): Promise<{
  proc: ChildProcessWithoutNullStreams;
  baseUrl: string;
}> {
  const scriptPath = path.resolve(
    process.cwd(),
    "tests/contract-http/run_event_review_stub.py"
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
          `Timed out waiting for event review stub startup. stderr:\n${stderrChunks.join("")}`
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
        // ignore non-JSON
      }
    });

    child.once("exit", (code) => {
      clearTimeout(timeout);
      rl.close();
      reject(
        new Error(
          `Event review stub exited prematurely (${code}). stderr:\n${stderrChunks.join("")}`
        )
      );
    });
  });

  return { proc: child, baseUrl: readyUrl };
}

async function stopEventReviewStub(): Promise<void> {
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

async function bumpServerVersion(
  eventId: string,
  status = "CONFIRMED"
): Promise<void> {
  const res = await fetch(`${baseUrl}/__control/bump-version`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_id: eventId, status }),
  });
  if (!res.ok) throw new Error(`bump-version failed: ${res.status}`);
}

async function getRecordedRequests(): Promise<RecordedStubRequest[]> {
  const res = await fetch(`${baseUrl}/__control/requests`);
  if (!res.ok) throw new Error(`requests fetch failed: ${res.status}`);
  const data = (await res.json()) as { requests: RecordedStubRequest[] };
  return data.requests;
}

beforeAll(async () => {
  const started = await startEventReviewStub();
  stubProc = started.proc;
  baseUrl = started.baseUrl;
});

afterEach(() => {
  cleanup();
});

afterAll(async () => {
  await stopEventReviewStub();
});

describe("M3 Event Review real 127.0.0.1 FastAPI HTTP wire contract", () => {
  it("exchanges GET list/detail (ETag, Cache-Control: private, no-store), POST /actions/confirm, PATCH, POST /actions/merge, and POST /actions/dissolve", async () => {
    await resetStub();
    const transport = new FetchEventReviewHttpTransport({ baseUrl });

    // 1. GET /v1/work-events
    const listRes = await transport.listWorkEvents({
      limit: 50,
      source_resource_id: "repo_demo_ledger_01",
    });
    expect(listRes.ok).toBe(true);
    expect(listRes.meta?.cacheControl).toBe("private, no-store");
    if (!listRes.ok) return;
    expect(listRes.data.items).toHaveLength(2);
    expect(listRes.data.counts.active_top_level_total).toBe(2);

    // 2. GET /v1/work-events/{id} -> ETag: "v1", Cache-Control: private, no-store
    const detailRes = await transport.getWorkEventDetail("evt_pr_104");
    expect(detailRes.ok).toBe(true);
    expect(detailRes.meta?.etag).toBe('"v1"');
    expect(detailRes.meta?.cacheControl).toBe("private, no-store");

    // 3. Keep -> POST /v1/work-events/{id}/actions/confirm with If-Match + Idempotency-Key
    const confirmRes = await transport.confirmWorkEvent({
      event_id: "evt_pr_104",
      expected_version: 1,
      reason: "Confirmed in contract test",
    });
    expect(confirmRes.ok).toBe(true);
    expect(confirmRes.meta?.etag).toBe('"v2"');
    if (confirmRes.ok) {
      expect(confirmRes.data.status).toBe("CONFIRMED");
      expect(confirmRes.data.version).toBe(2);
    }

    // 4. PATCH /v1/work-events/{id} -> v3 CANDIDATE
    const editRes = await transport.editWorkEvent({
      event_id: "evt_pr_104",
      expected_version: 2,
      title: "Updated PR #104 Title",
      problem: "Updated problem",
      action: "Updated action",
      outcome: "Updated outcome",
    });
    expect(editRes.ok).toBe(true);
    expect(editRes.meta?.etag).toBe('"v3"');
    if (editRes.ok) {
      expect(editRes.data.version).toBe(3);
      expect(editRes.data.status).toBe("CANDIDATE");
    }

    // 5. Merge -> POST /v1/work-events/actions/merge
    const mergeRes = await transport.mergeWorkEvents({
      members: [
        { event_id: "evt_pr_104", expected_version: 3 },
        { event_id: "evt_dc_209", expected_version: 1 },
      ],
      title: "HTTP Merged Composite",
      reason: "Contract HTTP merge test",
    });
    expect(mergeRes.ok).toBe(true);
    if (!mergeRes.ok) return;
    const compId = mergeRes.data.composite_event.event_id;
    expect(mergeRes.data.composite_event.anchor_mode).toBe("USER_COMPOSITE");
    expect(mergeRes.data.superseded_member_ids).toEqual([
      "evt_pr_104",
      "evt_dc_209",
    ]);

    // Verify active logical count collapsed from 2 -> 1
    const afterMergeList = await transport.listWorkEvents({ limit: 50 });
    expect(afterMergeList.ok).toBe(true);
    if (afterMergeList.ok) {
      expect(afterMergeList.data.counts.active_top_level_total).toBe(1);
    }

    // 6. Dissolve -> POST /v1/work-events/{id}/actions/dissolve
    const dissolveRes = await transport.dissolveWorkEvent({
      event_id: compId,
      expected_version: 1,
      reason: "Contract HTTP dissolve test",
    });
    expect(dissolveRes.ok).toBe(true);
    if (dissolveRes.ok) {
      expect(dissolveRes.data.dissolved_composite.status).toBe("SUPERSEDED");
      expect(dissolveRes.data.restored_members).toHaveLength(2);
    }

    // Verify exact HTTP wire headers recorded by real FastAPI server
    const logs = await getRecordedRequests();
    const confirmLog = logs.find(
      (r) =>
        r.method === "POST" && r.path === "/v1/work-events/evt_pr_104/actions/confirm"
    );
    expect(confirmLog).toBeDefined();
    expect(confirmLog?.if_match).toBe('"v1"');
    expect(confirmLog?.idempotency_key).toMatch(UUID_V4_REGEX);

    const patchLog = logs.find(
      (r) => r.method === "PATCH" && r.path === "/v1/work-events/evt_pr_104"
    );
    expect(patchLog?.if_match).toBe('"v2"');
    expect(patchLog?.idempotency_key).toMatch(UUID_V4_REGEX);

    const dissolveLog = logs.find(
      (r) =>
        r.method === "POST" && r.path === `/v1/work-events/${compId}/actions/dissolve`
    );
    expect(dissolveLog?.if_match).toBe('"v1"');
    expect(dissolveLog?.idempotency_key).toMatch(UUID_V4_REGEX);
  });

  it("handles real HTTP 409 EVENT_VERSION_CONFLICT in EventInboxApp without auto-POST and recovers safely", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new FetchEventReviewHttpTransport({ baseUrl });

    render(<EventInboxApp adapter={transport} />);

    // Initial GET loads evt_pr_104 at v1
    await waitFor(() => {
      expect(screen.getByTestId("detail-version-badge")).toHaveTextContent("v1");
    });

    // Simulate concurrent tab updating server to v2 CONFIRMED
    await bumpServerVersion("evt_pr_104", "CONFIRMED");

    // User clicks Keep (확정) with stale v1 -> real HTTP 409 EVENT_VERSION_CONFLICT
    await user.click(screen.getByTestId("action-keep-button"));
    const banner = await screen.findByTestId("version-conflict-banner");
    expect(banner).toHaveTextContent("409 EVENT_VERSION_CONFLICT");
    expect(screen.getByTestId("conflict-latest-meta")).toHaveTextContent(
      "최신 버전: v2"
    );

    const logsBeforeReload = await getRecordedRequests();
    const confirmPosts = logsBeforeReload.filter(
      (r) =>
        r.method === "POST" && r.path === "/v1/work-events/evt_pr_104/actions/confirm"
    );
    // Strict ER-004 invariant: exactly 1 POST (no silent auto-POST!)
    expect(confirmPosts).toHaveLength(1);
    expect(confirmPosts[0].status).toBe(409);

    // Click "최신 상태 다시 불러오기" -> recovers v2 via GET
    await user.click(screen.getByTestId("reload-latest-after-conflict-button"));
    await waitFor(() => {
      expect(screen.getByTestId("detail-version-badge")).toHaveTextContent("v2");
    });
    expect(screen.getByTestId("detail-status-badge")).toHaveTextContent(
      "확정됨 (CONFIRMED)"
    );
  });
});
