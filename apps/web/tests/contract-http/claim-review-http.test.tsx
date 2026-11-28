import React from "react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { FetchClaimReviewHttpTransport } from "@/adapters/claim-review-http-transport";
import { UUID_V4_REGEX } from "@/adapters/synthetic-http-transport";
import { ClaimReviewApp } from "@/components/claim-review/ClaimReviewApp";

/**
 * Isolated 127.0.0.1 ephemeral-port HTTP contract integration tests for
 * `FetchClaimReviewHttpTransport` & `ClaimReviewApp` (M4 T-406).
 */

interface RecordedRequest {
  method: string;
  path: string;
  query: string;
  authorization: string | null;
  workspace_id: string | null;
  if_match: string | null;
  idempotency_key: string | null;
  request_id: string | null;
  status: number;
  body: Record<string, unknown> | null;
}

let stubProc: ChildProcessWithoutNullStreams | null = null;
let baseUrl = "";

async function startClaimReviewStub(): Promise<{
  proc: ChildProcessWithoutNullStreams;
  baseUrl: string;
}> {
  const scriptPath = path.resolve(
    process.cwd(),
    "tests/contract-http/run_claim_review_stub.py"
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
          `Timed out waiting for claim review stub startup. stderr:\n${stderrChunks.join("")}`
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
          `Claim review stub exited prematurely (${code}). stderr:\n${stderrChunks.join("")}`
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

async function bumpClaimVersion(claimId: string): Promise<void> {
  const res = await fetch(`${baseUrl}/__control/bump-claim-version`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ claim_id: claimId }),
  });
  if (!res.ok) throw new Error(`bump failed: ${res.status}`);
}

async function getRecordedRequests(): Promise<RecordedRequest[]> {
  const res = await fetch(`${baseUrl}/__control/requests`);
  const data = (await res.json()) as { requests: RecordedRequest[] };
  return data.requests;
}

beforeAll(async () => {
  const started = await startClaimReviewStub();
  stubProc = started.proc;
  baseUrl = started.baseUrl;
});

afterEach(() => {
  cleanup();
});

afterAll(async () => {
  await stopStub();
});

describe("M4 Claim Review real 127.0.0.1 FastAPI HTTP wire contract", () => {
  it("exchanges /v1/facts*, /v1/claims*, /v1/content-units/{id}/content (all Plan C states), and /v1/exports* with exact headers and zero forbidden write keys", async () => {
    await resetStub();
    const transport = new FetchClaimReviewHttpTransport({
      baseUrl,
      workspaceId: "ws_http_m4",
      authorization: "Bearer test-m4-token",
    });

    // 1. Facts list, supports, approve, edit, reject
    const factsRes = await transport.listFacts({
      event_id: "evt_comp_301",
      include_superseded: false,
    });
    expect(factsRes.ok).toBe(true);
    expect(factsRes.meta?.cacheControl).toBe("private, no-store");
    if (!factsRes.ok) return;
    expect(factsRes.data.items).toHaveLength(2);

    const factSupRes = await transport.getFactSupports("fact_hmac_01");
    expect(factSupRes.ok).toBe(true);
    if (factSupRes.ok) {
      expect(factSupRes.data.items).toHaveLength(1);
      expect(
        "extracted_span" in (factSupRes.data.items[0] as unknown as Record<string, unknown>)
      ).toBe(false);
    }

    const approveFactRes = await transport.approveFact({
      fact_id: "fact_cand_02",
      expected_version: 1,
    });
    expect(approveFactRes.ok).toBe(true);
    if (approveFactRes.ok) {
      expect(approveFactRes.data.fact.status).toBe("USER_CONFIRMED");
    }

    const editFactRes = await transport.editFact({
      fact_id: "fact_cand_02",
      expected_version: 1,
      predicate: "configured_replay_window",
      object_text: "Updated 5m replay window",
    });
    expect(editFactRes.ok).toBe(true);
    if (editFactRes.ok) {
      expect(editFactRes.data.fact.version).toBe(2);
      expect(editFactRes.data.fact.status).toBe("CANDIDATE");
      expect(editFactRes.data.fact.supersedes_fact_id).toBe("fact_cand_02");
    }

    // 2. Claims list, generate (MANUAL_ONLY_BLOCKED), detail, supports, edit -> revalidate -> approve
    const genRes = await transport.generateClaims({
      confirmed_event_id: "evt_comp_301",
    });
    expect(genRes.ok).toBe(true);
    if (genRes.ok) {
      expect(genRes.data.generation_status).toBe("MANUAL_ONLY_BLOCKED");
      expect(genRes.data.free_alternatives).toHaveLength(4);
    }

    const editClaimRes = await transport.editClaim({
      claim_id: "clm_http_01",
      expected_version: 1,
      claim_text: "Updated grounded HMAC claim text",
      edit_reason: "Contract test edit",
    });
    expect(editClaimRes.ok).toBe(true);
    if (!editClaimRes.ok) return;
    expect(editClaimRes.data.status).toBe("DRAFT");
    expect(editClaimRes.data.version).toBe(2);
    const editedClaimId = editClaimRes.data.claim_id;

    const revalRes = await transport.revalidateClaim({
      claim_id: editedClaimId,
      expected_version: 2,
    });
    expect(revalRes.ok).toBe(true);
    if (revalRes.ok) {
      expect(revalRes.data.status).toBe("SUPPORT_VALIDATED");
      expect(revalRes.data.approvable).toBe(true);
    }

    const approveClaimRes = await transport.approveClaim({
      claim_id: editedClaimId,
      expected_version: 2,
      acknowledge_user_confirmed_atoms: false,
    });
    expect(approveClaimRes.ok).toBe(true);
    if (approveClaimRes.ok) {
      expect(approveClaimRes.data.status).toBe("APPROVED");
    }

    // 3. Plan C Ephemeral Content Unit Viewer (200 MATCHED, 409 REVISION_MISMATCH, 403 POLICY_BLOCKED, 422 LIMIT, 503 UNAVAILABLE)
    const matchedRes = await transport.getContentUnitContent({
      content_unit_id: "cu_matched_01",
      workspace_id: "ws_http_m4",
      include_context: true,
    });
    expect(matchedRes.ok).toBe(true);
    if (matchedRes.ok) {
      expect(matchedRes.data.source_resolution.status).toBe("MATCHED");
      expect(matchedRes.data.content).toContain("timingSafeEqual");
    }

    const driftRes = await transport.getContentUnitContent({
      content_unit_id: "cu_drift_409",
      workspace_id: "ws_http_m4",
    });
    expect(driftRes.ok).toBe(false);
    if (!driftRes.ok) {
      expect(driftRes.error.sourceResolution?.status).toBe("REVISION_MISMATCH");
    }

    const policyRes = await transport.getContentUnitContent({
      content_unit_id: "cu_policy_403",
      workspace_id: "ws_http_m4",
    });
    expect(policyRes.ok).toBe(false);
    if (!policyRes.ok) {
      expect(policyRes.error.sourceResolution?.status).toBe("POLICY_BLOCKED");
    }

    const limitRes = await transport.getContentUnitContent({
      content_unit_id: "cu_limit_422",
      workspace_id: "ws_http_m4",
    });
    expect(limitRes.ok).toBe(false);
    if (!limitRes.ok) {
      expect(limitRes.error.sourceResolution?.status).toBe("PARTIAL_WITH_LIMIT");
    }

    const unavail503Res = await transport.getContentUnitContent({
      content_unit_id: "cu_unavail_503",
      workspace_id: "ws_http_m4",
    });
    expect(unavail503Res.ok).toBe(false);
    if (!unavail503Res.ok) {
      expect(unavail503Res.error.sourceResolution?.status).toBe("UNAVAILABLE");
      expect(unavail503Res.error.sourceResolution?.retryable).toBe(true);
    }

    // 4. Exports create -> list -> detail -> delete
    const createExpRes = await transport.createExport({
      format: "MARKDOWN",
      claim_ids: [editedClaimId],
      expected_claim_versions: { [editedClaimId]: 2 },
      export_policy: "SOURCE_VERIFIED_ONLY",
    });
    expect(createExpRes.ok).toBe(true);
    if (!createExpRes.ok) return;
    const expId = createExpRes.data.export_id;
    expect(createExpRes.data.manifest.contains_raw_quotes).toBe(false);

    const listExpRes = await transport.listExports();
    expect(listExpRes.ok).toBe(true);
    if (listExpRes.ok) {
      expect(listExpRes.data.items.some((e) => e.export_id === expId)).toBe(true);
    }

    const delExpRes = await transport.deleteExport({ export_id: expId });
    expect(delExpRes.ok).toBe(true);
    if (delExpRes.ok) {
      expect(delExpRes.data.status).toBe("DELETED");
    }

    // Verify wire headers recorded by FastAPI server
    const logs = await getRecordedRequests();
    const approveClaimLog = logs.find(
      (r) =>
        r.method === "POST" && r.path === `/v1/claims/${editedClaimId}/actions/approve`
    );
    expect(approveClaimLog?.authorization).toBe("Bearer test-m4-token");
    expect(approveClaimLog?.workspace_id).toBe("ws_http_m4");
    expect(approveClaimLog?.idempotency_key).toMatch(UUID_V4_REGEX);
    expect(approveClaimLog?.request_id).toMatch(UUID_V4_REGEX);
    expect(approveClaimLog?.body).not.toHaveProperty("verification");
  });

  it("handles real HTTP 409 CLAIM_VERSION_CONFLICT in ClaimReviewApp with zero auto-POST and recovers via Reload Latest Claim", async () => {
    await resetStub();
    const user = userEvent.setup();
    const transport = new FetchClaimReviewHttpTransport({
      baseUrl,
      workspaceId: "ws_http_m4",
      authorization: "Bearer test-m4-token",
    });

    render(<ClaimReviewApp adapter={transport} eventId="evt_comp_301" />);

    await waitFor(() => {
      expect(screen.getByTestId("claim-version-badge")).toHaveTextContent("v1");
    });

    // Simulate concurrent tab bumping clm_http_01 to v2 on server
    await bumpClaimVersion("clm_http_01");

    await user.click(screen.getByTestId("claim-approve-button"));
    const banner = await screen.findByTestId("claim-conflict-recovery-banner");
    expect(banner).toHaveTextContent("409 CLAIM_VERSION_CONFLICT");

    const logsBeforeReload = await getRecordedRequests();
    const approvePosts = logsBeforeReload.filter(
      (r) => r.method === "POST" && r.path === "/v1/claims/clm_http_01/actions/approve"
    );
    // Strict non-retry invariant: exactly 1 POST!
    expect(approvePosts).toHaveLength(1);
    expect(approvePosts[0].status).toBe(409);

    // Reload Latest Claim via GET
    await user.click(screen.getByTestId("reload-latest-claim-button"));
    await waitFor(() => {
      expect(screen.getByTestId("claim-version-badge")).toHaveTextContent("v2");
    });
    expect(
      screen.queryByTestId("claim-conflict-recovery-banner")
    ).not.toBeInTheDocument();
  });
});
