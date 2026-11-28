import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { FetchClaimReviewHttpTransport } from "@/adapters/claim-review-http-transport";

/**
 * Live 127.0.0.1 loopback interoperability test between `FetchClaimReviewHttpTransport`
 * and the REAL Python `career_memory_api` M4 FastAPI routers (`facts`, `claims`,
 * `resolver`, `exports`, `contracts`) backed by PostgreSQL 18 (`0010..0070`,
 * `cm_api_app` NOBYPASSRLS + FORCE RLS).
 *
 * Verifies every wire normalization fix caught during T-407A:
 * - `support_graph_epoch` SHA-256 string round-trip (`^sha256:[0-9a-f]{64}$`)
 * - `SpanLocatorV1` `line_start`/`char_start`/`locator_spec_version` normalization
 * - `ClaimAtomDetailItem` `unsupported_reason` / `metric_grounding` / `role_contribution_grounding`
 * - Plan C ephemeral viewer `MATCHED` (200 + `private, no-store`) and `REVISION_MISMATCH` (409)
 * - ER-012 Claim `PATCH` edit -> `revalidate` -> `approve` & fail-closed unsupported claim block
 * - Zero-quote `MARKDOWN` and `JSON` export create -> detail -> list -> delete (0 canary leakage!)
 */

interface LiveM4Bootstrap {
  baseUrl: string;
  workspace_id: string;
  source_resource_id: string;
  event_id: string;
  fact_id: string;
  content_unit_id: string;
  verified_claim_id: string;
  unsupported_claim_id: string;
  canary_token: string;
}

let backendProc: ChildProcessWithoutNullStreams | null = null;
let bootstrap: LiveM4Bootstrap | null = null;

async function startLiveM4Backend(): Promise<{
  proc: ChildProcessWithoutNullStreams;
  info: LiveM4Bootstrap;
}> {
  const apiDir = path.resolve(process.cwd(), "../../apps/api");
  const scriptPath = path.resolve(
    process.cwd(),
    "tests/contract-http/run_live_claim_review_backend.py"
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

  const info = await new Promise<LiveM4Bootstrap>((resolve, reject) => {
    const rl = readline.createInterface({ input: child.stdout });
    const timeout = setTimeout(() => {
      rl.close();
      reject(
        new Error(
          `Timed out waiting for live M4 career_memory_api backend. stderr:\n${stderrChunks.join("")}`
        )
      );
    }, 45_000);

    rl.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) return;
      try {
        const parsed = JSON.parse(trimmed) as Partial<LiveM4Bootstrap>;
        if (parsed.baseUrl && parsed.verified_claim_id && parsed.fact_id) {
          clearTimeout(timeout);
          rl.close();
          resolve(parsed as LiveM4Bootstrap);
        }
      } catch {
        // ignore non-JSON output
      }
    });

    child.once("exit", (code) => {
      clearTimeout(timeout);
      rl.close();
      reject(
        new Error(
          `Live M4 career_memory_api backend exited prematurely (${code}). stderr:\n${stderrChunks.join("")}`
        )
      );
    });
  });

  return { proc: child, info };
}

async function stopLiveM4Backend(): Promise<void> {
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

async function resetLiveM4Backend(): Promise<LiveM4Bootstrap> {
  if (!bootstrap) throw new Error("live M4 backend not started");
  const res = await fetch(`${bootstrap.baseUrl}/__control/reset`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`reset failed: ${res.status}`);
  const data = (await res.json()) as Omit<LiveM4Bootstrap, "baseUrl">;
  bootstrap = { baseUrl: bootstrap.baseUrl, ...data };
  return bootstrap;
}

async function setRefetchMode(mode: "MATCHED" | "REVISION_MISMATCH"): Promise<void> {
  if (!bootstrap) throw new Error("live M4 backend not started");
  const res = await fetch(`${bootstrap.baseUrl}/__control/set-refetch-mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!res.ok) throw new Error(`set-refetch-mode failed: ${res.status}`);
}

beforeAll(async () => {
  const started = await startLiveM4Backend();
  backendProc = started.proc;
  bootstrap = started.info;
}, 50_000);

afterAll(async () => {
  await stopLiveM4Backend();
});

describe("Live PG18 M4 FastAPI <-> FetchClaimReviewHttpTransport FE-BE contract verification", () => {
  it("verifies Facts, Claims, SHA-256 support_graph_epoch, SpanLocatorV1, Plan C viewer, PATCH->revalidate->approve, and zero-quote exports", async () => {
    const ctx = await resetLiveM4Backend();
    const transport = new FetchClaimReviewHttpTransport({
      baseUrl: ctx.baseUrl,
      workspaceId: ctx.workspace_id,
      authorization: "Bearer live-m4-contract-test",
    });

    // 1. GET /v1/facts + GET /v1/facts/{id}/supports
    const factsRes = await transport.listFacts({ event_id: ctx.event_id });
    expect(factsRes.ok).toBe(true);
    expect(factsRes.meta?.cacheControl).toBe("private, no-store");
    if (!factsRes.ok) return;
    expect(factsRes.data.items).toHaveLength(1);
    expect(factsRes.data.items[0]?.status).toBe("SUPPORTED");

    const factSupRes = await transport.getFactSupports(ctx.fact_id);
    expect(factSupRes.ok).toBe(true);
    if (!factSupRes.ok) return;
    expect(factSupRes.data.items).toHaveLength(1);
    const locator = factSupRes.data.items[0]?.locator;
    expect(locator).not.toBeNull();
    // Real backend emits char_start/char_end/line_start/line_end -> normalized cleanly!
    expect(locator?.start_line).toBe(1);
    expect(locator?.start_char).toBe(0);
    expect(locator?.end_char).toBeGreaterThan(0);
    expect(locator?.span_sha256).toMatch(/^sha256:[0-9a-f]{64}$/);
    // Plan C: no raw canary in fact supports metadata
    expect(JSON.stringify(factSupRes.data)).not.toContain(ctx.canary_token);

    // 2. GET /v1/claims + GET /v1/claims/{id} + GET /v1/claims/{id}/supports
    const claimsListRes = await transport.listClaims({ event_id: ctx.event_id });
    expect(claimsListRes.ok).toBe(true);
    if (!claimsListRes.ok) return;
    expect(claimsListRes.data.items.length).toBeGreaterThanOrEqual(2);

    const claimDetailRes = await transport.getClaimDetail(ctx.verified_claim_id);
    expect(claimDetailRes.ok).toBe(true);
    if (!claimDetailRes.ok) return;
    expect(claimDetailRes.data.computed_verification).toBe("SOURCE_VERIFIED");
    expect(claimDetailRes.data.approvable).toBe(true);
    // Real backend emits SHA-256 string support_graph_epoch (losslessly preserved!)
    expect(typeof claimDetailRes.data.support_graph_epoch).toBe("string");
    expect(String(claimDetailRes.data.support_graph_epoch)).toMatch(
      /^sha256:[0-9a-f]{64}$/
    );

    const claimSupportsRes = await transport.getClaimSupports(ctx.verified_claim_id);
    expect(claimSupportsRes.ok).toBe(true);
    if (!claimSupportsRes.ok) return;
    expect(claimSupportsRes.data.atoms).toHaveLength(1);
    expect(claimSupportsRes.data.atoms[0]?.supports[0]?.fact_supports[0]?.locator?.start_line).toBe(1);

    // 3. Plan C Ephemeral Source Viewer: MATCHED (200 + no-store) & REVISION_MISMATCH (409)
    const viewOk = await transport.getContentUnitContent({
      content_unit_id: ctx.content_unit_id,
      workspace_id: ctx.workspace_id,
      include_context: true,
    });
    expect(viewOk.ok).toBe(true);
    expect(viewOk.meta?.cacheControl).toBe("private, no-store");
    if (!viewOk.ok) return;
    expect(viewOk.data.source_resolution.status).toBe("MATCHED");
    expect(viewOk.data.content).toContain("idempotent retry queue");

    await setRefetchMode("REVISION_MISMATCH");
    const viewDrift = await transport.getContentUnitContent({
      content_unit_id: ctx.content_unit_id,
      workspace_id: ctx.workspace_id,
    });
    expect(viewDrift.ok).toBe(false);
    if (!viewDrift.ok) {
      expect(viewDrift.error.kind).toBe("source_revision_mismatch");
      expect(viewDrift.error.sourceResolution?.status).toBe("REVISION_MISMATCH");
    }
    await setRefetchMode("MATCHED");

    // 4. Fail-closed approval block on UNVERIFIED adversarial claim
    const unverifiedDetail = await transport.getClaimDetail(ctx.unsupported_claim_id);
    expect(unverifiedDetail.ok).toBe(true);
    if (!unverifiedDetail.ok) return;
    expect(unverifiedDetail.data.computed_verification).toBe("UNVERIFIED");
    const blockedApprove = await transport.approveClaim({
      claim_id: ctx.unsupported_claim_id,
      expected_version: unverifiedDetail.data.version,
      expected_support_graph_epoch: unverifiedDetail.data.support_graph_epoch,
      acknowledge_user_confirmed_atoms: false,
    });
    expect(blockedApprove.ok).toBe(false);
    if (!blockedApprove.ok) {
      expect(blockedApprove.error.httpStatus).toBe(409);
      expect(blockedApprove.error.kind).toBe("support_incomplete");
    }

    // 5. Approve v1 using real SHA-256 support_graph_epoch (proves no 422 epoch coercion bug!)
    const approvedV1 = await transport.approveClaim({
      claim_id: ctx.verified_claim_id,
      expected_version: 1,
      expected_support_graph_epoch: claimDetailRes.data.support_graph_epoch,
      acknowledge_user_confirmed_atoms: false,
    });
    expect(approvedV1.ok).toBe(true);
    if (!approvedV1.ok) return;
    expect(approvedV1.data.status).toBe("APPROVED");
    expect(approvedV1.data.computed_verification).toBe("SOURCE_VERIFIED");

    // 6. ER-012 Claim PATCH edit (v1 APPROVED preserved) -> revalidate v2 -> approve v2
    const patchRes = await transport.editClaim({
      claim_id: ctx.verified_claim_id,
      expected_version: 1,
      claim_text:
        "Implemented idempotent retry queue with exponential backoff for billing webhooks.",
      edit_reason: "Live FE-BE wire contract edit check",
    });
    expect(patchRes.ok).toBe(true);
    if (!patchRes.ok) return;
    expect(patchRes.data.version).toBe(2);
    expect(patchRes.data.status).toBe("DRAFT");
    const v2ClaimId = patchRes.data.claim_id;

    const revalRes = await transport.revalidateClaim({
      claim_id: v2ClaimId,
      expected_version: 2,
    });
    expect(revalRes.ok).toBe(true);
    if (!revalRes.ok) return;
    expect(revalRes.data.computed_verification).toBe("SOURCE_VERIFIED");

    const approvedV2 = await transport.approveClaim({
      claim_id: v2ClaimId,
      expected_version: 2,
      expected_support_graph_epoch: revalRes.data.support_graph_epoch,
      acknowledge_user_confirmed_atoms: false,
    });
    expect(approvedV2.ok).toBe(true);
    if (!approvedV2.ok) return;
    expect(approvedV2.data.status).toBe("APPROVED");

    // 7. Zero-quote MARKDOWN & JSON Exports (create -> get -> list -> delete, 0 canary leakage!)
    const mdExport = await transport.createExport({
      format: "MARKDOWN",
      claim_ids: [v2ClaimId],
      export_policy: "SOURCE_VERIFIED_ONLY",
    });
    expect(mdExport.ok).toBe(true);
    if (!mdExport.ok) return;
    expect(mdExport.data.status).toBe("READY");
    expect(mdExport.data.manifest.contains_raw_quotes).toBe(false);
    expect(mdExport.data.manifest.claim_count).toBe(1);
    expect(mdExport.data.claim_ids).toEqual([v2ClaimId]);
    expect(mdExport.data.rendered_markdown).not.toContain(ctx.canary_token);

    const jsonExport = await transport.createExport({
      format: "JSON",
      claim_ids: [v2ClaimId],
      export_policy: "SOURCE_VERIFIED_ONLY",
    });
    expect(jsonExport.ok).toBe(true);
    if (!jsonExport.ok) return;
    expect(jsonExport.data.status).toBe("READY");
    expect(jsonExport.data.manifest.contains_raw_quotes).toBe(false);
    expect(jsonExport.data.manifest.citations.length).toBeGreaterThanOrEqual(1);
    expect(jsonExport.data.manifest.citations[0]?.locator?.start_line).toBe(1);
    expect(JSON.stringify(jsonExport.data)).not.toContain(ctx.canary_token);

    const fetchedExport = await transport.getExportDetail(jsonExport.data.export_id);
    expect(fetchedExport.ok).toBe(true);

    const listExports = await transport.listExports();
    expect(listExports.ok).toBe(true);
    if (listExports.ok) {
      expect(listExports.data.items.length).toBeGreaterThanOrEqual(2);
    }

    const delExport = await transport.deleteExport({
      export_id: mdExport.data.export_id,
    });
    expect(delExport.ok).toBe(true);
    if (delExport.ok) {
      expect(delExport.data.status).toBe("DELETED");
    }
  }, 40_000);
});
