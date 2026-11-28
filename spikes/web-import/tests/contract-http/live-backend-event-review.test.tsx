import React from "react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { FetchEventReviewHttpTransport } from "@/adapters/event-review-http-transport";
import { EventInboxApp } from "@/components/event-review/EventInboxApp";

/**
 * Live 127.0.0.1 loopback interoperability test between `FetchEventReviewHttpTransport` /
 * `EventInboxApp` and the ACTUAL Python `career_memory_api.work_events` FastAPI router
 * backed by PostgreSQL 18 (`cm_api_app` FORCE RLS).
 */

interface BackendBootstrap {
  baseUrl: string;
  workspace_id: string;
  source_resource_id: string;
  pr_event_id: string;
  dc_event_id: string;
}

let backendProc: ChildProcessWithoutNullStreams | null = null;
let bootstrap: BackendBootstrap | null = null;

async function startLiveBackend(): Promise<{
  proc: ChildProcessWithoutNullStreams;
  info: BackendBootstrap;
}> {
  const apiDir = path.resolve(process.cwd(), "../../apps/api");
  const scriptPath = path.resolve(
    process.cwd(),
    "tests/contract-http/run_live_work_events_backend.py"
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

  const info = await new Promise<BackendBootstrap>((resolve, reject) => {
    const rl = readline.createInterface({ input: child.stdout });
    const timeout = setTimeout(() => {
      rl.close();
      reject(
        new Error(
          `Timed out waiting for live career_memory_api backend. stderr:\n${stderrChunks.join("")}`
        )
      );
    }, 40_000);

    rl.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) return;
      try {
        const parsed = JSON.parse(trimmed) as Partial<BackendBootstrap>;
        if (parsed.baseUrl && parsed.pr_event_id && parsed.dc_event_id) {
          clearTimeout(timeout);
          rl.close();
          resolve(parsed as BackendBootstrap);
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
          `Live career_memory_api backend exited prematurely (${code}). stderr:\n${stderrChunks.join("")}`
        )
      );
    });
  });

  return { proc: child, info };
}

async function stopLiveBackend(): Promise<void> {
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

async function resetLiveBackend(): Promise<BackendBootstrap> {
  if (!bootstrap) throw new Error("backend not started");
  const res = await fetch(`${bootstrap.baseUrl}/__control/reset`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`reset failed: ${res.status}`);
  const data = (await res.json()) as Omit<BackendBootstrap, "baseUrl">;
  bootstrap = { baseUrl: bootstrap.baseUrl, ...data };
  return bootstrap;
}

async function bumpServerVersion(
  eventId: string,
  status = "CONFIRMED"
): Promise<void> {
  if (!bootstrap) throw new Error("backend not started");
  const res = await fetch(`${bootstrap.baseUrl}/__control/bump-version`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_id: eventId, status }),
  });
  if (!res.ok) throw new Error(`bump-version failed: ${res.status}`);
}

beforeAll(async () => {
  const started = await startLiveBackend();
  backendProc = started.proc;
  bootstrap = started.info;
}, 45_000);

afterEach(() => {
  cleanup();
});

afterAll(async () => {
  await stopLiveBackend();
});

describe("Live career_memory_api FastAPI + PG18 <-> FetchEventReviewHttpTransport wire interoperability", () => {
  it("completes GET -> confirm -> edit -> merge -> 409 EVENT_VERSION_CONFLICT -> dissolve losslessly", async () => {
    const ctx = await resetLiveBackend();
    const transport = new FetchEventReviewHttpTransport({
      baseUrl: ctx.baseUrl,
    });

    // 1. GET /v1/work-events normalized from real Python backend
    const listRes = await transport.listWorkEvents({
      limit: 50,
      source_resource_id: ctx.source_resource_id,
    });
    expect(listRes.ok).toBe(true);
    expect(listRes.meta?.cacheControl).toBe("private, no-store");
    if (!listRes.ok) return;
    expect(listRes.data.items).toHaveLength(2);
    expect(listRes.data.counts.active_top_level_total).toBe(2);
    expect(listRes.data.items[0].policy_state.status).toBe("ACTIVE");

    // 2. GET /v1/work-events/{id} -> ETag "v1", evidence & context normalized
    const detailRes = await transport.getWorkEventDetail(ctx.pr_event_id);
    expect(detailRes.ok).toBe(true);
    expect(detailRes.meta?.etag).toBe('"v1"');
    if (!detailRes.ok) return;
    expect(detailRes.data.evidence_memberships).toHaveLength(1);
    expect(detailRes.data.evidence_memberships[0].object_type).toBe(
      "PULL_REQUEST"
    );
    expect(detailRes.data.context_relations).toHaveLength(1);
    expect(detailRes.data.context_relations[0].relation_class).toBe(
      "CONTEXT_ONLY"
    );

    // 3. confirm -> v2 CONFIRMED
    const confirmRes = await transport.confirmWorkEvent({
      event_id: ctx.pr_event_id,
      expected_version: 1,
      reason: "Live confirm",
    });
    expect(confirmRes.ok).toBe(true);
    expect(confirmRes.meta?.etag).toBe('"v2"');
    if (!confirmRes.ok) return;
    expect(confirmRes.data.status).toBe("CONFIRMED");
    expect(confirmRes.data.version).toBe(2);
    expect(confirmRes.data.review_actions[0].action).toBe("CONFIRM");

    // 4. edit -> v3 CANDIDATE
    const editRes = await transport.editWorkEvent({
      event_id: ctx.pr_event_id,
      expected_version: 2,
      title: "PR #104 Edited Over Live Wire",
      problem: "Updated problem",
      action: "Updated action",
      outcome: "Updated outcome",
    });
    expect(editRes.ok).toBe(true);
    expect(editRes.meta?.etag).toBe('"v3"');
    if (!editRes.ok) return;
    expect(editRes.data.status).toBe("CANDIDATE");
    expect(editRes.data.version).toBe(3);

    // 5. Stale version -> real HTTP 409 EVENT_VERSION_CONFLICT
    const conflictRes = await transport.confirmWorkEvent({
      event_id: ctx.pr_event_id,
      expected_version: 2,
      reason: "Stale attempt",
    });
    expect(conflictRes.ok).toBe(false);
    if (!conflictRes.ok) {
      expect(conflictRes.error.kind).toBe("version_conflict");
      expect(conflictRes.error.code).toBe("EVENT_VERSION_CONFLICT");
      expect(conflictRes.error.conflictDetails?.current_version).toBe(3);
      expect(conflictRes.error.conflictDetails?.current_status).toBe(
        "CANDIDATE"
      );
    }

    // 6. merge -> USER_COMPOSITE v1
    const mergeRes = await transport.mergeWorkEvents({
      members: [
        { event_id: ctx.pr_event_id, expected_version: 3 },
        { event_id: ctx.dc_event_id, expected_version: 1 },
      ],
      title: "Live Wire Composite",
      reason: "Combined PR and hotfix",
    });
    expect(mergeRes.ok).toBe(true);
    if (!mergeRes.ok) return;
    const compId = mergeRes.data.composite_event.event_id;
    expect(mergeRes.data.composite_event.anchor_mode).toBe("USER_COMPOSITE");
    expect(mergeRes.data.composite_event.composite_details?.members).toHaveLength(
      2
    );
    expect(mergeRes.data.superseded_member_ids).toEqual([
      ctx.pr_event_id,
      ctx.dc_event_id,
    ]);

    // 7. dissolve -> restores both members and marks composite SUPERSEDED
    const dissolveRes = await transport.dissolveWorkEvent({
      event_id: compId,
      expected_version: 1,
      reason: "Undo live wire merge",
    });
    expect(dissolveRes.ok).toBe(true);
    if (!dissolveRes.ok) return;
    expect(dissolveRes.data.dissolved_composite.status).toBe("SUPERSEDED");
    expect(dissolveRes.data.restored_members).toHaveLength(2);
  }, 30_000);

  it("renders EventInboxApp directly against live career_memory_api PG18 backend and recovers from real 409 conflict", async () => {
    const ctx = await resetLiveBackend();
    const user = userEvent.setup();
    const transport = new FetchEventReviewHttpTransport({
      baseUrl: ctx.baseUrl,
    });

    render(<EventInboxApp adapter={transport} />);

    await waitFor(() => {
      expect(screen.getByTestId("detail-version-badge")).toHaveTextContent("v1");
    });

    // Bump both seeded events on server to trigger a real 409 on whichever item is selected first
    await bumpServerVersion(ctx.pr_event_id, "CONFIRMED");
    await bumpServerVersion(ctx.dc_event_id, "CONFIRMED");

    await user.click(screen.getByTestId("action-keep-button"));
    const banner = await screen.findByTestId("version-conflict-banner");
    expect(banner).toHaveTextContent("409 EVENT_VERSION_CONFLICT");
    expect(screen.getByTestId("conflict-latest-meta")).toHaveTextContent(
      "최신 버전: v2"
    );

    await user.click(screen.getByTestId("reload-latest-after-conflict-button"));
    await waitFor(() => {
      expect(screen.getByTestId("detail-version-badge")).toHaveTextContent("v2");
    });
  }, 30_000);
});
