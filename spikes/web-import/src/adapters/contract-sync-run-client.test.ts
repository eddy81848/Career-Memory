import { describe, expect, it } from "vitest";
import { ContractSyncRunClient } from "./contract-sync-run-client";
import { SYNTHETIC_PUBLIC_REPOSITORIES } from "./fixtures";
import {
  UUID_V4_REGEX,
  type SyntheticHttpRequest,
  type SyntheticHttpResponse,
  type SyntheticHttpTransport,
} from "./synthetic-http-transport";

/**
 * Adapter unit tests against synthetic HTTP responses modeled on CONTRACT
 * commit a0314f2cbcb33f1d34ed486db7b1009dcb4adc59 (OpenAPI 0.2.0-draft)
 * for ONLY the 3 sync-run paths:
 *   - POST /v1/sync-runs
 *   - GET  /v1/sync-runs/{id}
 *   - POST /v1/sync-runs/{id}/retry
 */

const REPO_A = SYNTHETIC_PUBLIC_REPOSITORIES[0];
const REPO_B = SYNTHETIC_PUBLIC_REPOSITORIES[1];

function createScriptedTransport(
  handlers: Array<
    (req: SyntheticHttpRequest) => SyntheticHttpResponse | Promise<SyntheticHttpResponse>
  >
): { transport: SyntheticHttpTransport; requests: SyntheticHttpRequest[] } {
  const requests: SyntheticHttpRequest[] = [];
  let idx = 0;
  return {
    requests,
    transport: {
      async send(req: SyntheticHttpRequest): Promise<SyntheticHttpResponse> {
        requests.push({ ...req, headers: { ...req.headers } });
        const handler = handlers[idx];
        idx += 1;
        if (!handler) {
          throw new Error(`Unexpected synthetic HTTP request #${idx}: ${req.method} ${req.path}`);
        }
        return handler(req);
      },
    },
  };
}

describe("ContractSyncRunClient (OpenAPI 0.2.0-draft synthetic HTTP)", () => {
  it("accepts 202 without job_id as non-completed PENDING and polls exact status_url", async () => {
    const { transport, requests } = createScriptedTransport([
      () => ({
        status: 202,
        headers: { "Content-Type": "application/json" },
        rawBody: JSON.stringify({
          status: "PENDING",
          target_type: "SYNC_RUN",
          target_id: "synthetic-run-42",
          status_url: "/v1/sync-runs/synthetic-run-42",
          // job_id intentionally omitted
        }),
      }),
      () => ({
        status: 200,
        headers: { "Content-Type": "application/json" },
        rawBody: JSON.stringify({
          status: "SUCCESS",
          plan_id: "synthetic-plan-42",
          import_ready: true,
          processing_status: "COMPLETE",
          observed_at: "2026-10-05T09:00:00Z",
          source_drift: false,
          retry: { allowed: false },
        }),
      }),
    ]);

    const client = new ContractSyncRunClient({ transport });
    const created = await client.createInitialImport({ repo: REPO_A });

    expect(created.ok).toBe(true);
    if (!created.ok) return;

    // 202 must NOT be displayed as completed!
    expect(created.data.run.runId).toBe("synthetic-run-42");
    expect(created.data.run.statusUrl).toBe("/v1/sync-runs/synthetic-run-42");
    expect(created.data.run.status).toBe("running");
    expect(created.data.run.readiness.importReady).toBe(false);

    // Check POST /v1/sync-runs request contract
    expect(requests[0].method).toBe("POST");
    expect(requests[0].path).toBe("/v1/sync-runs");
    expect(requests[0].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
    expect(JSON.parse(requests[0].body ?? "{}")).toEqual({
      source_resource_id: REPO_A.repoId,
      mode: "INITIAL_BACKFILL",
    });

    // Poll uses exact status_url from 202
    const polled = await client.getImportStatus({
      runId: created.data.run.runId,
      repo: REPO_A,
    });
    expect(polled.ok).toBe(true);
    expect(requests[1].method).toBe("GET");
    expect(requests[1].path).toBe("/v1/sync-runs/synthetic-run-42");
    if (polled.ok) {
      expect(polled.data.run.status).toBe("succeeded");
      expect(polled.data.run.readiness.importReady).toBe(true);
    }
  });

  it("reuses the exact same UUID Idempotency-Key ONLY when resending the same logical request after a lost response", async () => {
    const { transport, requests } = createScriptedTransport([
      // 1st create attempt: response lost
      () => {
        throw new Error("LOST_RESPONSE");
      },
      // 2nd create attempt (same repo A): must reuse exact same UUID
      () => ({
        status: 202,
        rawBody: JSON.stringify({
          target_id: "synthetic-run-1",
          status_url: "/v1/sync-runs/synthetic-run-1",
        }),
      }),
      // 3rd create attempt (new operation after server responded): must use a NEW UUID
      () => {
        throw new Error("LOST_RESPONSE_2");
      },
      // 4th create attempt (switched to repo B): different logical request -> must use a NEW UUID
      () => ({
        status: 202,
        rawBody: JSON.stringify({
          target_id: "synthetic-run-2",
          status_url: "/v1/sync-runs/synthetic-run-2",
        }),
      }),
      // 5th: retry on synthetic-run-1 -> lost response
      () => {
        throw new Error("LOST_RETRY_RESPONSE");
      },
      // 6th: resend SAME retry on synthetic-run-1 -> must reuse retry UUID, bodyless
      () => ({
        status: 202,
        rawBody: JSON.stringify({
          target_id: "synthetic-run-1",
          status_url: "/v1/sync-runs/synthetic-run-1",
        }),
      }),
      // 7th: subsequent retry after 202 -> must use a NEW UUID
      () => ({
        status: 202,
        rawBody: JSON.stringify({
          target_id: "synthetic-run-1",
          status_url: "/v1/sync-runs/synthetic-run-1",
        }),
      }),
    ]);

    const client = new ContractSyncRunClient({ transport });

    const lostCreate = await client.createInitialImport({ repo: REPO_A });
    expect(lostCreate.ok).toBe(false);
    if (!lostCreate.ok) {
      expect(lostCreate.kind).toBe("network_error");
    }

    const resentCreate = await client.createInitialImport({ repo: REPO_A });
    expect(resentCreate.ok).toBe(true);

    // Resend of same logical create reuses EXACT SAME UUID
    expect(requests[0].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
    expect(requests[1].headers["Idempotency-Key"]).toBe(
      requests[0].headers["Idempotency-Key"]
    );

    // Next create after 202 uses a NEW UUID
    await client.createInitialImport({ repo: REPO_A });
    expect(requests[2].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
    expect(requests[2].headers["Idempotency-Key"]).not.toBe(
      requests[1].headers["Idempotency-Key"]
    );

    // Changing logical request (repo B) generates a NEW UUID even after lost response
    await client.createInitialImport({ repo: REPO_B });
    expect(requests[3].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
    expect(requests[3].headers["Idempotency-Key"]).not.toBe(
      requests[2].headers["Idempotency-Key"]
    );

    // Retry lost response -> resend reuses EXACT SAME retry UUID and omits body
    const lostRetry = await client.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_A,
    });
    expect(lostRetry.ok).toBe(false);

    const resentRetry = await client.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_A,
    });
    expect(resentRetry.ok).toBe(true);
    expect(requests[4].path).toBe("/v1/sync-runs/synthetic-run-1/retry");
    expect(requests[4].body).toBeUndefined();
    expect(requests[5].body).toBeUndefined();
    expect(requests[4].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
    expect(requests[5].headers["Idempotency-Key"]).toBe(
      requests[4].headers["Idempotency-Key"]
    );

    // Subsequent distinct retry gets a fresh UUID
    await client.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_A,
    });
    expect(requests[6].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
    expect(requests[6].headers["Idempotency-Key"]).not.toBe(
      requests[5].headers["Idempotency-Key"]
    );
  });

  it("maps 200 PARTIAL_WITH_LIMIT as non-success with import_ready=false and retry disabled", async () => {
    const { transport } = createScriptedTransport([
      () => ({
        status: 200,
        rawBody: JSON.stringify({
          status: "PARTIAL_WITH_LIMIT",
          plan_id: "synthetic-plan-2",
          failure_reason: "SYNC_PARTIAL_WITH_LIMIT",
          source_drift: false,
          import_ready: false,
          processing_status: "NOT_STARTED",
          processed_range: "2025-09-23 through 2026-08-31",
          remaining_range: "2026-09-01 through 2026-10-05",
          retry: { allowed: false },
        }),
      }),
    ]);

    const client = new ContractSyncRunClient({ transport });
    const result = await client.getImportStatus({
      runId: "synthetic-run-2",
      repo: REPO_A,
    });

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.data.run.status).toBe("limit_reached");
    expect(result.data.run.readiness.importReady).toBe(false);
    expect(result.data.run.retryPolicy.allowed).toBe(false);
    expect(result.data.run.processedRange).toBe("2025-09-23 through 2026-08-31");
    expect(result.data.run.remainingRange).toBe("2026-09-01 through 2026-10-05");
  });

  it("separates historical SUCCESS (import_ready=true) from current BLOCKED_POLICY and BLOCKED_SOURCE processing states", async () => {
    const { transport } = createScriptedTransport([
      () => ({
        status: 200,
        rawBody: JSON.stringify({
          status: "SUCCESS",
          plan_id: "synthetic-plan-4",
          source_drift: false,
          import_ready: true,
          processing_status: "BLOCKED_POLICY",
          retry: { allowed: false },
        }),
      }),
      () => ({
        status: 200,
        rawBody: JSON.stringify({
          status: "SUCCESS",
          plan_id: "synthetic-plan-5",
          source_drift: false,
          import_ready: true,
          processing_status: "BLOCKED_SOURCE",
          retry: { allowed: false },
        }),
      }),
    ]);

    const client = new ContractSyncRunClient({ transport });

    const blockedPolicy = await client.getImportStatus({
      runId: "synthetic-run-4",
      repo: REPO_A,
    });
    expect(blockedPolicy.ok).toBe(true);
    if (blockedPolicy.ok) {
      expect(blockedPolicy.data.run.status).toBe("succeeded");
      expect(blockedPolicy.data.run.readiness.importReady).toBe(true);
      expect(blockedPolicy.data.run.readiness.processingStatus).toBe(
        "BLOCKED_POLICY"
      );
      expect(blockedPolicy.data.run.readiness.derivedAllReady).toBe(false);
      expect(
        blockedPolicy.data.run.readiness.processingBlockedMessage
      ).toContain("BLOCKED_POLICY");
      expect(blockedPolicy.data.run.retryPolicy.allowed).toBe(false);
    }

    const blockedSource = await client.getImportStatus({
      runId: "synthetic-run-5",
      repo: REPO_A,
    });
    expect(blockedSource.ok).toBe(true);
    if (blockedSource.ok) {
      expect(blockedSource.data.run.status).toBe("succeeded");
      expect(blockedSource.data.run.readiness.importReady).toBe(true);
      expect(blockedSource.data.run.readiness.processingStatus).toBe(
        "BLOCKED_SOURCE"
      );
      expect(blockedSource.data.run.readiness.derivedAllReady).toBe(false);
      expect(
        blockedSource.data.run.readiness.processingBlockedMessage
      ).toContain("BLOCKED_SOURCE");
    }
  });

  it("navigates to existing run on 409 SYNC_ALREADY_RUNNING ONLY when existing_run_id is provided", async () => {
    const { transport, requests } = createScriptedTransport([
      // Case 1: 409 WITH existing_run_id -> follows GET /v1/sync-runs/synthetic-run-existing
      () => ({
        status: 409,
        rawBody: JSON.stringify({
          error: {
            code: "SYNC_ALREADY_RUNNING",
            details: { existing_run_id: "synthetic-run-existing" },
          },
        }),
      }),
      () => ({
        status: 200,
        rawBody: JSON.stringify({
          status: "RUNNING",
          plan_id: "synthetic-plan-existing",
          import_ready: false,
          processing_status: "NOT_STARTED",
          source_drift: false,
          retry: { allowed: false },
        }),
      }),
      // Case 2: 409 SYNC_ALREADY_RUNNING WITHOUT details.existing_run_id -> no GET!
      () => ({
        status: 409,
        rawBody: JSON.stringify({
          error: { code: "SYNC_ALREADY_RUNNING" },
        }),
      }),
      // Case 3: 409 IDEMPOTENCY_CONFLICT -> no GET!
      () => ({
        status: 409,
        rawBody: JSON.stringify({
          error: { code: "IDEMPOTENCY_CONFLICT" },
        }),
      }),
    ]);

    const client = new ContractSyncRunClient({ transport });

    const attached = await client.createInitialImport({ repo: REPO_A });
    expect(attached.ok).toBe(true);
    if (attached.ok) {
      expect(attached.data.run.runId).toBe("synthetic-run-existing");
      expect(attached.data.recoveryNotice?.kind).toBe("attached_existing_run");
    }
    expect(requests[1].method).toBe("GET");
    expect(requests[1].path).toBe("/v1/sync-runs/synthetic-run-existing");

    const noId = await client.createInitialImport({ repo: REPO_A });
    expect(noId.ok).toBe(false);
    if (!noId.ok) {
      expect(noId.kind).toBe("duplicate_conflict");
    }
    expect(requests).toHaveLength(3); // Did NOT call GET

    const idempConflict = await client.createInitialImport({ repo: REPO_A });
    expect(idempConflict.ok).toBe(false);
    if (!idempConflict.ok) {
      expect(idempConflict.kind).toBe("duplicate_conflict");
    }
    expect(requests).toHaveLength(4); // Did NOT call GET
  });

  it("handles bodyless 403, 409, and 422 without JSON parse errors or cause speculation", async () => {
    const { transport } = createScriptedTransport([
      () => ({ status: 403, rawBody: "" }),
      () => ({ status: 409, rawBody: "" }),
      () => ({ status: 422, rawBody: "" }),
      () => ({ status: 403 }), // undefined body on GET
      () => ({ status: 409, rawBody: "   " }), // whitespace-only body on retry
    ]);

    const client = new ContractSyncRunClient({ transport });

    const r403 = await client.createInitialImport({ repo: REPO_A });
    expect(r403.ok).toBe(false);
    if (!r403.ok) {
      expect(r403.kind).toBe("request_rejected");
      expect(r403.fault.httpStatus).toBe(403);
      expect(r403.fault.message).toContain("세부 사유를 특정할 수 없습니다");
    }

    const r409 = await client.createInitialImport({ repo: REPO_A });
    expect(r409.ok).toBe(false);
    if (!r409.ok) {
      expect(r409.kind).toBe("request_rejected");
      expect(r409.fault.httpStatus).toBe(409);
      expect(r409.fault.message).toContain("이유를 단정할 수 없습니다");
    }

    const r422 = await client.createInitialImport({ repo: REPO_A });
    expect(r422.ok).toBe(false);
    if (!r422.ok) {
      expect(r422.kind).toBe("request_rejected");
      expect(r422.fault.httpStatus).toBe(422);
      expect(r422.fault.message).toContain("세부 원인은 응답에 포함되지 않았습니다");
    }

    const poll403 = await client.getImportStatus({
      runId: "synthetic-run-1",
      repo: REPO_A,
    });
    expect(poll403.ok).toBe(false);
    if (!poll403.ok) {
      expect(poll403.kind).toBe("request_rejected");
      expect(poll403.fault.httpStatus).toBe(403);
    }

    const retry409 = await client.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_A,
    });
    expect(retry409.ok).toBe(false);
    if (!retry409.ok) {
      expect(retry409.kind).toBe("request_rejected");
      expect(retry409.fault.httpStatus).toBe(409);
    }
  });

  it("never turns a GET status failure into a new POST /v1/sync-runs request", async () => {
    const { transport, requests } = createScriptedTransport([
      () => {
        throw new Error("POLL_NETWORK_FAILURE");
      },
    ]);

    const client = new ContractSyncRunClient({ transport });
    const res = await client.getImportStatus({
      runId: "synthetic-run-9",
      repo: REPO_A,
    });

    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.kind).toBe("network_error");
    }
    expect(requests).toHaveLength(1);
    expect(requests[0].method).toBe("GET");
    expect(requests.some((r) => r.method === "POST")).toBe(false);
  });
});
