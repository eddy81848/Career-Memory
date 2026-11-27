import { MockFixtureImportAdapter } from "./mock-fixture-adapter";
import { describe, expect, it } from "vitest";
import { ContractSyncRunClient } from "./contract-sync-run-client";
import { SYNTHETIC_PUBLIC_REPOSITORIES } from "./fixtures";
import {
  UUID_V4_REGEX,
  type SyntheticHttpRequest,
  type SyntheticHttpResponse,
  type SyntheticHttpTransport,
} from "./synthetic-http-transport";
import {
  SyntheticOpenApiTransport,
  type SyntheticOpenApiState,
} from "./synthetic-openapi-transport";

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

    // Retry lost response -> explicit resend reuses EXACT SAME retry UUID and JSON expected_run_version body
    const lostRetry = await client.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_A,
      expectedRunVersion: 3,
    });
    expect(lostRetry.ok).toBe(false);

    const resentRetry = await client.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_A,
      mode: "resend_pending",
    });
    expect(resentRetry.ok).toBe(true);
    expect(requests[4].path).toBe("/v1/sync-runs/synthetic-run-1/retry");
    expect(requests[4].headers["Content-Type"]).toBe("application/json");
    expect(requests[4].body).toBe(JSON.stringify({ expected_run_version: 3 }));
    expect(requests[5].body).toBe(JSON.stringify({ expected_run_version: 3 }));
    expect(requests[4].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
    expect(requests[5].headers["Idempotency-Key"]).toBe(
      requests[4].headers["Idempotency-Key"]
    );

    // Subsequent distinct new retry gets a fresh UUID and latest expected_run_version
    await client.retryInitialImport({
      runId: "synthetic-run-1",
      repo: REPO_A,
      mode: "new_retry",
      expectedRunVersion: 5,
    });
    expect(requests[6].body).toBe(JSON.stringify({ expected_run_version: 5 }));
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
      expectedRunVersion: 2,
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

  describe("WEB-04 uncertain outcome Idempotency-Key preservation", () => {
    it("preserves Idempotency-Key on explicit create resend after 502 when server already accepted the first request", async () => {
      let acceptedKey: string | null = null;
      const acceptedRunId = "synthetic-run-committed-before-502";
      const { transport, requests } = createScriptedTransport([
        // 1st POST: server commits the run under this key, but gateway returns 502
        (req) => {
          acceptedKey = req.headers["Idempotency-Key"];
          return {
            status: 502,
            rawBody: "Bad Gateway",
          };
        },
        // 2nd explicit POST: server replays 202 ONLY if the exact same key is resent
        (req) => {
          if (req.headers["Idempotency-Key"] === acceptedKey) {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_type: "SYNC_RUN",
                target_id: acceptedRunId,
                status_url: `/v1/sync-runs/${acceptedRunId}`,
              }),
            };
          }
          return {
            status: 409,
            rawBody: JSON.stringify({
              error: { code: "SYNC_ALREADY_RUNNING" },
            }),
          };
        },
      ]);

      const client = new ContractSyncRunClient({ transport });

      const first = await client.createInitialImport({ repo: REPO_A });
      expect(first.ok).toBe(false);
      if (!first.ok) {
        expect(first.kind).toBe("uncertain_outcome");
        expect(first.fault.httpStatus).toBe(502);
        expect(first.fault.serverConfirmed).toBe(false);
      }
      // No automatic second POST
      expect(requests).toHaveLength(1);

      const second = await client.createInitialImport({ repo: REPO_A });
      expect(requests).toHaveLength(2);
      expect(requests[0].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
      expect(requests[1].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(second.ok).toBe(true);
      if (second.ok) {
        expect(second.data.run.runId).toBe(acceptedRunId);
        expect(second.data.run.status).toBe("running");
      }
    });

    it("preserves Idempotency-Key on explicit create resend after 202 missing required fields (server already accepted)", async () => {
      let acceptedKey: string | null = null;
      const acceptedRunId = "synthetic-run-damaged-202";
      const { transport, requests } = createScriptedTransport([
        // 1st POST: server committed run, but 202 response body is missing status_url
        (req) => {
          acceptedKey = req.headers["Idempotency-Key"];
          return {
            status: 202,
            rawBody: JSON.stringify({
              status: "PENDING",
              target_id: acceptedRunId,
              // status_url intentionally missing
            }),
          };
        },
        // 2nd POST: still missing target_id -> still unverifiable, must keep key
        (req) => {
          expect(req.headers["Idempotency-Key"]).toBe(acceptedKey);
          return {
            status: 202,
            rawBody: JSON.stringify({
              status: "PENDING",
              status_url: `/v1/sync-runs/${acceptedRunId}`,
              // target_id intentionally missing
            }),
          };
        },
        // 3rd POST: server replays complete 202 for the same key
        (req) => {
          if (req.headers["Idempotency-Key"] === acceptedKey) {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: acceptedRunId,
                status_url: `/v1/sync-runs/${acceptedRunId}`,
              }),
            };
          }
          return { status: 409, rawBody: "" };
        },
      ]);

      const client = new ContractSyncRunClient({ transport });

      const first = await client.createInitialImport({ repo: REPO_A });
      expect(first.ok).toBe(false);
      if (!first.ok) {
        expect(first.kind).toBe("uncertain_outcome");
        expect(first.fault.httpStatus).toBe(202);
        expect(first.fault.serverConfirmed).toBe(false);
      }
      expect(requests).toHaveLength(1);

      const second = await client.createInitialImport({ repo: REPO_A });
      expect(second.ok).toBe(false);
      if (!second.ok) {
        expect(second.kind).toBe("uncertain_outcome");
        expect(second.fault.httpStatus).toBe(202);
        expect(second.fault.serverConfirmed).toBe(false);
      }
      expect(requests).toHaveLength(2);
      expect(requests[1].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );

      const third = await client.createInitialImport({ repo: REPO_A });
      expect(requests).toHaveLength(3);
      expect(requests[2].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(third.ok).toBe(true);
      if (third.ok) {
        expect(third.data.run.runId).toBe(acceptedRunId);
      }
    });

    it("preserves Idempotency-Key and expected_run_version body on explicit retry resend after 502 when server already accepted the retry", async () => {
      const originalRunId = "synthetic-run-retry-502";
      let acceptedRetryKey: string | null = null;
      let acceptedRetryBody: string | undefined;
      const { transport, requests } = createScriptedTransport([
        // 1st versioned retry: server accepted retry, gateway returned 502
        (req) => {
          acceptedRetryKey = req.headers["Idempotency-Key"];
          acceptedRetryBody = req.body;
          return {
            status: 502,
            rawBody: "",
          };
        },
        // 2nd explicit resend: replays 202 ONLY for the same Idempotency-Key and original body
        (req) => {
          if (
            req.headers["Idempotency-Key"] === acceptedRetryKey &&
            req.body === acceptedRetryBody
          ) {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_type: "SYNC_RUN",
                target_id: originalRunId,
                status_url: `/v1/sync-runs/${originalRunId}`,
              }),
            };
          }
          return {
            status: 409,
            rawBody: JSON.stringify({
              error: { code: "RESOURCE_VERSION_CONFLICT" },
            }),
          };
        },
      ]);

      const client = new ContractSyncRunClient({ transport });

      const first = await client.retryInitialImport({
        runId: originalRunId,
        repo: REPO_A,
        expectedRunVersion: 2,
      });
      expect(first.ok).toBe(false);
      if (!first.ok) {
        expect(first.kind).toBe("uncertain_outcome");
        expect(first.fault.httpStatus).toBe(502);
        expect(first.fault.serverConfirmed).toBe(false);
        expect(first.pendingRetry).toEqual({
          runId: originalRunId,
          expectedRunVersion: 2,
        });
      }
      expect(requests).toHaveLength(1);
      expect(requests[0].body).toBe(JSON.stringify({ expected_run_version: 2 }));

      const second = await client.retryInitialImport({
        runId: originalRunId,
        repo: REPO_A,
        mode: "resend_pending",
      });
      expect(requests).toHaveLength(2);
      expect(requests[1].body).toBe(JSON.stringify({ expected_run_version: 2 }));
      expect(requests[0].headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
      expect(requests[1].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(second.ok).toBe(true);
      if (second.ok) {
        expect(second.data.run.runId).toBe(originalRunId);
        expect(second.data.pendingRetry).toBeNull();
      }
    });

    it("preserves Idempotency-Key and expected_run_version body on explicit retry resend after 202 missing required fields", async () => {
      const originalRunId = "synthetic-run-retry-bad-202";
      let acceptedRetryKey: string | null = null;
      const { transport, requests } = createScriptedTransport([
        // 1st versioned retry: server committed retry, 202 body missing status_url
        (req) => {
          acceptedRetryKey = req.headers["Idempotency-Key"];
          return {
            status: 202,
            rawBody: JSON.stringify({
              status: "PENDING",
              target_id: originalRunId,
            }),
          };
        },
        // 2nd explicit resend: server replays full 202 on matching key + body
        (req) => {
          if (req.headers["Idempotency-Key"] === acceptedRetryKey) {
            return {
              status: 202,
              rawBody: JSON.stringify({
                status: "PENDING",
                target_id: originalRunId,
                status_url: `/v1/sync-runs/${originalRunId}`,
              }),
            };
          }
          return { status: 409, rawBody: "" };
        },
      ]);

      const client = new ContractSyncRunClient({ transport });

      const first = await client.retryInitialImport({
        runId: originalRunId,
        repo: REPO_A,
        expectedRunVersion: 4,
      });
      expect(first.ok).toBe(false);
      if (!first.ok) {
        expect(first.kind).toBe("uncertain_outcome");
        expect(first.fault.httpStatus).toBe(202);
        expect(first.fault.serverConfirmed).toBe(false);
      }
      expect(requests).toHaveLength(1);
      expect(requests[0].body).toBe(JSON.stringify({ expected_run_version: 4 }));

      const second = await client.retryInitialImport({
        runId: originalRunId,
        repo: REPO_A,
        mode: "resend_pending",
      });
      expect(requests).toHaveLength(2);
      expect(requests[1].body).toBe(JSON.stringify({ expected_run_version: 4 }));
      expect(requests[1].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(second.ok).toBe(true);
      if (second.ok) {
        expect(second.data.run.runId).toBe(originalRunId);
      }
    });

    it("never reuses Idempotency-Key across different auth scopes, different targets, or different operations", async () => {
      const { transport, requests } = createScriptedTransport([
        // 0: create REPO_A in scope-1 -> 502
        () => ({ status: 502, rawBody: "" }),
        // 1: create REPO_A in scope-2 (different auth scope) -> 502
        () => ({ status: 502, rawBody: "" }),
        // 2: create REPO_B in scope-2 (different target) -> 502
        () => ({ status: 502, rawBody: "" }),
        // 3: retry run-1 in scope-2 (different operation) -> 502
        () => ({ status: 502, rawBody: "" }),
        // 4: retry run-2 in scope-2 (different retry target) -> 502
        () => ({ status: 502, rawBody: "" }),
        // 5: retry run-2 in scope-3 (different auth scope) -> 401 SESSION_TERMINATED (clears keys)
        () => ({
          status: 401,
          rawBody: JSON.stringify({
            error: {
              code: "SESSION_TERMINATED",
              message: "Session terminated.",
              retryable: false,
              request_id: "req-401",
            },
          }),
        }),
        // 6: retry run-2 in scope-3 again after 401 -> 202
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            target_id: "run-2",
            status_url: "/v1/sync-runs/run-2",
          }),
        }),
      ]);

      const client = new ContractSyncRunClient({
        transport,
        initialAuthScope: "scope-1",
      });

      await client.createInitialImport({ repo: REPO_A });
      client.setAuthScope("scope-2");
      await client.createInitialImport({ repo: REPO_A });
      await client.createInitialImport({ repo: REPO_B });
      await client.retryInitialImport({
        runId: "run-1",
        repo: REPO_A,
        expectedRunVersion: 2,
      });
      await client.retryInitialImport({
        runId: "run-2",
        repo: REPO_A,
        expectedRunVersion: 2,
      });
      await client.retryInitialImport({
        runId: "run-2",
        repo: REPO_A,
        expectedRunVersion: 2,
        authScope: "scope-3",
      });
      await client.retryInitialImport({
        runId: "run-2",
        repo: REPO_A,
        expectedRunVersion: 2,
        authScope: "scope-3",
      });

      expect(requests).toHaveLength(7);
      const uniqueKeys = new Set(
        requests.map((r) => r.headers["Idempotency-Key"])
      );
      expect(uniqueKeys.size).toBe(7);
      for (const req of requests) {
        expect(req.headers["Idempotency-Key"]).toMatch(UUID_V4_REGEX);
      }
    });

    it("rejects create 202 when status_url does not point to target_id, never polls mismatched path, and keeps Idempotency-Key", async () => {
      const { transport, requests } = createScriptedTransport([
        // 1st POST: status_url points to a different run than target_id
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: "synthetic-run-A",
            status_url: "/v1/sync-runs/synthetic-run-B",
          }),
        }),
        // 2nd POST: non-relative/query status_url -> still rejected
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: "synthetic-run-A",
            status_url: "/v1/sync-runs/synthetic-run-A?evil=1",
          }),
        }),
        // 3rd POST: valid matching 202
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: "synthetic-run-A",
            status_url: "/v1/sync-runs/synthetic-run-A",
          }),
        }),
      ]);

      const client = new ContractSyncRunClient({ transport });

      const first = await client.createInitialImport({ repo: REPO_A });
      expect(first.ok).toBe(false);
      if (!first.ok) {
        expect(first.kind).toBe("uncertain_outcome");
        expect(first.fault.httpStatus).toBe(202);
        expect(first.fault.serverConfirmed).toBe(false);
      }
      // Must NOT have polled /v1/sync-runs/synthetic-run-B or stored it
      expect(requests).toHaveLength(1);
      expect(requests.some((r) => r.method === "GET")).toBe(false);
      expect(client.getStatusUrl("synthetic-run-A")).toBe(
        "/v1/sync-runs/synthetic-run-A"
      );

      const second = await client.createInitialImport({ repo: REPO_A });
      expect(second.ok).toBe(false);
      expect(requests[1].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );

      const third = await client.createInitialImport({ repo: REPO_A });
      expect(third.ok).toBe(true);
      expect(requests[2].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      if (third.ok) {
        expect(third.data.run.runId).toBe("synthetic-run-A");
        expect(third.data.run.statusUrl).toBe("/v1/sync-runs/synthetic-run-A");
      }
    });

    it("rejects retry 202 when target_id differs from original run or status_url mismatches, never navigates or polls wrong run, and keeps Idempotency-Key", async () => {
      const originalRunId = "synthetic-run-orig";
      const { transport, requests } = createScriptedTransport([
        // 1st retry: target_id points to a DIFFERENT run
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: "synthetic-run-other",
            status_url: "/v1/sync-runs/synthetic-run-other",
          }),
        }),
        // 2nd retry: target_id matches original, but status_url points to different run
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: originalRunId,
            status_url: "/v1/sync-runs/synthetic-run-other",
          }),
        }),
        // 3rd retry: valid 202 for original run
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: originalRunId,
            status_url: `/v1/sync-runs/${originalRunId}`,
          }),
        }),
      ]);

      const client = new ContractSyncRunClient({ transport });

      const first = await client.retryInitialImport({
        runId: originalRunId,
        repo: REPO_A,
        expectedRunVersion: 2,
      });
      expect(first.ok).toBe(false);
      if (!first.ok) {
        expect(first.kind).toBe("uncertain_outcome");
        expect(first.fault.httpStatus).toBe(202);
        expect(first.fault.serverConfirmed).toBe(false);
        expect(first.fault.message).toContain("다른 실행으로 이동하지 않았으며");
      }
      expect(requests).toHaveLength(1);
      expect(requests.some((r) => r.method === "GET")).toBe(false);

      const second = await client.retryInitialImport({
        runId: originalRunId,
        repo: REPO_A,
        mode: "resend_pending",
      });
      expect(second.ok).toBe(false);
      expect(requests).toHaveLength(2);
      expect(requests[1].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(requests[1].body).toBe(JSON.stringify({ expected_run_version: 2 }));
      expect(client.getStatusUrl(originalRunId)).toBe(
        `/v1/sync-runs/${originalRunId}`
      );

      const third = await client.retryInitialImport({
        runId: originalRunId,
        repo: REPO_A,
        mode: "resend_pending",
      });
      expect(third.ok).toBe(true);
      expect(requests[2].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(requests[2].body).toBe(JSON.stringify({ expected_run_version: 2 }));
      if (third.ok) {
        expect(third.data.run.runId).toBe(originalRunId);
      }
    });

    it("never converts GET 5xx poll failure into a new POST /v1/sync-runs request", async () => {
      const { transport, requests } = createScriptedTransport([
        () => ({ status: 502, rawBody: "Bad Gateway" }),
      ]);

      const client = new ContractSyncRunClient({ transport });
      const res = await client.getImportStatus({
        runId: "synthetic-run-poll-502",
        repo: REPO_A,
      });

      expect(res.ok).toBe(false);
      if (!res.ok) {
        expect(res.kind).toBe("uncertain_outcome");
        expect(res.fault.httpStatus).toBe(502);
        expect(res.fault.serverConfirmed).toBe(false);
      }
      expect(requests).toHaveLength(1);
      expect(requests[0].method).toBe("GET");
      expect(requests.some((r) => r.method === "POST")).toBe(false);
    });

    it("does NOT persist pending Idempotency-Key across reload (fresh adapter instance over shared sessionStorage)", async () => {
      window.sessionStorage.clear();
      const observedKeys: string[] = [];

      const transport: SyntheticHttpTransport = {
        async send(req) {
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            observedKeys.push(req.headers["Idempotency-Key"]);
          }
          return { status: 502, rawBody: "" };
        },
      };

      const beforeReload = new MockFixtureImportAdapter({
        latencyMs: 0,
        disableBrowserStorage: false,
        syncRunTransport: transport,
      });

      const res1 = await beforeReload.startInitialImport({ repoId: REPO_A.repoId });
      expect(res1.ok).toBe(false);
      expect(observedKeys).toHaveLength(1);

      // Verify persisted prototype snapshot does NOT contain the pending key
      const persistedRaw = window.sessionStorage.getItem(
        "career_memory_spike_web_import_v1"
      );
      expect(persistedRaw ?? "").not.toContain(observedKeys[0]);

      // Simulate browser reload with a brand new adapter instance reading the same sessionStorage
      const afterReload = new MockFixtureImportAdapter({
        latencyMs: 0,
        disableBrowserStorage: false,
        syncRunTransport: transport,
      });

      const res2 = await afterReload.startInitialImport({ repoId: REPO_A.repoId });
      expect(res2.ok).toBe(false);
      expect(observedKeys).toHaveLength(2);
      expect(observedKeys[1]).not.toBe(observedKeys[0]);
      window.sessionStorage.clear();
    });
  });

  describe("WEB-05 late 202/401 isolation across logout and session changes", () => {
    it("ignores late 202 and late 401 from an old session so they never overwrite the new session's pending key, stored run, or auth state", async () => {
      window.sessionStorage.clear();

      let resolveSession1Create!: (res: SyntheticHttpResponse) => void;
      let resolveSession1Retry401!: (res: SyntheticHttpResponse) => void;
      const recordedCreateKeys: string[] = [];
      let callCount = 0;

      const transport: SyntheticHttpTransport = {
        send(req) {
          if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
            return Promise.resolve({
              status: 200,
              rawBody: JSON.stringify({ items: [] }),
            });
          }
          if (req.method === "POST" && req.path === "/v1/sync-runs") {
            callCount += 1;
            recordedCreateKeys.push(req.headers["Idempotency-Key"]);
            if (callCount === 1) {
              // Session 1 in-flight create -> will resolve LATE with 202
              return new Promise<SyntheticHttpResponse>((resolve) => {
                resolveSession1Create = resolve;
              });
            }
            if (callCount === 2) {
              // Session 2 create -> returns 502 so Session 2 holds a pending Idempotency-Key
              return Promise.resolve({ status: 502, rawBody: "" });
            }
            if (callCount === 3) {
              // Session 2 explicit resend -> must STILL carry Session 2's pending key!
              return Promise.resolve({
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_id: "synthetic-run-session-2",
                  status_url: "/v1/sync-runs/synthetic-run-session-2",
                }),
              });
            }
          }
          if (req.method === "POST" && req.path.endsWith("/retry")) {
            return new Promise<SyntheticHttpResponse>((resolve) => {
              resolveSession1Retry401 = resolve;
            });
          }
          return Promise.resolve({ status: 404, rawBody: "" });
        },
      };

      const adapter = new MockFixtureImportAdapter({
        latencyMs: 0,
        disableBrowserStorage: false,
        syncRunTransport: transport,
      });

      // Session 1 starts create (in-flight)
      const session1CreatePromise = adapter.startInitialImport({
        repoId: REPO_A.repoId,
      });
      await Promise.resolve();

      // Session 1 signs out and Session 2 signs in while Session 1 create is still in flight
      await adapter.signOutService();
      await adapter.signInService();

      // Session 2 starts create and receives 502 -> Session 2 now holds a pending key
      const session2First = await adapter.startInitialImport({
        repoId: REPO_A.repoId,
      });
      expect(session2First.ok).toBe(false);
      expect(recordedCreateKeys).toHaveLength(2);
      const session2PendingKey = recordedCreateKeys[1];
      expect(session2PendingKey).not.toBe(recordedCreateKeys[0]);

      // Now Session 1's LATE 202 arrives!
      resolveSession1Create({
        status: 202,
        rawBody: JSON.stringify({
          status: "PENDING",
          target_id: "synthetic-run-session-1-late",
          status_url: "/v1/sync-runs/synthetic-run-session-1-late",
        }),
      });
      await session1CreatePromise;

      // Session 2's stored snapshot MUST NOT contain Session 1's late run!
      const bootAfterLate202 = await adapter.bootstrapSession();
      expect(bootAfterLate202.ok).toBe(true);
      if (bootAfterLate202.ok) {
        expect(bootAfterLate202.data.latestRun).toBeNull();
        expect(bootAfterLate202.data.serviceAuth.status).toBe("authenticated");
      }

      // Session 2 explicitly resends -> MUST STILL reuse Session 2's pending key!
      const session2Resend = await adapter.startInitialImport({
        repoId: REPO_A.repoId,
      });
      expect(session2Resend.ok).toBe(true);
      expect(recordedCreateKeys).toHaveLength(3);
      expect(recordedCreateKeys[2]).toBe(session2PendingKey);

      // Now test late 401 SESSION_TERMINATED from an old session on retry after logout + re-login
      const oldRetryPromise = adapter.retryImportRun("synthetic-run-session-2", {
        mode: "new_retry",
        expectedRunVersion: 2,
      });
      await Promise.resolve();
      await adapter.signOutService();
      await adapter.signInService();

      resolveSession1Retry401({
        status: 401,
        rawBody: JSON.stringify({
          error: {
            code: "SESSION_TERMINATED",
            message: "Old session terminated.",
            retryable: false,
            request_id: "req-old-401",
          },
        }),
      });
      await oldRetryPromise;

      // New session MUST still be authenticated (not expired by old session's late 401)!
      const bootAfterLate401 = await adapter.bootstrapSession();
      expect(bootAfterLate401.ok).toBe(true);
      if (bootAfterLate401.ok) {
        expect(bootAfterLate401.data.serviceAuth.status).toBe("authenticated");
      }

      window.sessionStorage.clear();
    });
  });

  describe("WEB-06 recovery lookup, IDEMPOTENCY_KEY_EXPIRED, and repo/session stale guards", () => {
    it("recovers active and terminal runs via GET /v1/sync-runs?source_resource_id=...&limit=1 and handles empty items without auto-POST", async () => {
      const requests: SyntheticHttpRequest[] = [];
      let call = 0;
      const client = new ContractSyncRunClient({
        transport: {
          async send(req) {
            requests.push(req);
            call += 1;
            if (call === 1) {
              return { status: 200, rawBody: JSON.stringify({ items: [] }) };
            }
            if (call === 2) {
              return {
                status: 200,
                rawBody: JSON.stringify({
                  items: [
                    {
                      target_id: "synthetic-run-active-06",
                      status_url: "/v1/sync-runs/synthetic-run-active-06",
                      import_status: {
                        status: "RUNNING",
                        plan_id: "synthetic-plan-06",
                        import_ready: false,
                        processing_status: "NOT_STARTED",
                        source_drift: false,
                        retry: { allowed: false },
                      },
                    },
                  ],
                }),
              };
            }
            return {
              status: 200,
              rawBody: JSON.stringify({
                status: "SUCCESS",
                plan_id: "synthetic-plan-06",
                import_ready: true,
                processing_status: "BLOCKED_POLICY",
                source_drift: false,
                retry: { allowed: false },
              }),
            };
          },
        },
      });

      // 1. Empty items -> clean null, zero POST
      const emptyRes = await client.recoverLatestRunForRepository({ repo: REPO_A });
      expect(emptyRes.ok).toBe(true);
      if (emptyRes.ok) {
        expect(emptyRes.data.run).toBeNull();
      }
      expect(requests[0].method).toBe("GET");
      expect(requests[0].path).toBe(
        `/v1/sync-runs?source_resource_id=${encodeURIComponent(REPO_A.repoId)}&limit=1`
      );
      expect(requests[0].body).toBeUndefined();

      // 2. Active run recovered -> target_id/status_url validated & registered
      const activeRes = await client.recoverLatestRunForRepository({ repo: REPO_A });
      expect(activeRes.ok).toBe(true);
      if (activeRes.ok) {
        expect(activeRes.data.run?.runId).toBe("synthetic-run-active-06");
        expect(activeRes.data.run?.status).toBe("running");
      }

      // 3. Subsequent detail GET uses the validated status_url
      const detailRes = await client.getImportStatus({
        runId: "synthetic-run-active-06",
        repo: REPO_A,
      });
      expect(detailRes.ok).toBe(true);
      if (detailRes.ok) {
        expect(detailRes.data.run.status).toBe("succeeded");
        expect(detailRes.data.run.readiness.processingStatus).toBe("BLOCKED_POLICY");
      }
      expect(requests[2].path).toBe("/v1/sync-runs/synthetic-run-active-06");
      expect(requests.filter((r) => r.method === "POST")).toHaveLength(0);
    });

    it("rejects mismatched target_id/status_url, empty 403, and 5xx on recovery GET without auto-POST", async () => {
      const requests: SyntheticHttpRequest[] = [];
      const responses: SyntheticHttpResponse[] = [
        {
          status: 200,
          rawBody: JSON.stringify({
            items: [
              {
                target_id: "synthetic-run-a",
                status_url: "/v1/sync-runs/synthetic-run-different",
                import_status: {
                  status: "RUNNING",
                  plan_id: "synthetic-plan-a",
                  import_ready: false,
                  processing_status: "NOT_STARTED",
                  source_drift: false,
                  retry: { allowed: false },
                },
              },
            ],
          }),
        },
        { status: 403, rawBody: "" },
        { status: 502, rawBody: "" },
      ];

      const client = new ContractSyncRunClient({
        transport: {
          async send(req) {
            requests.push(req);
            return responses.shift() ?? { status: 500, rawBody: "" };
          },
        },
      });

      const mismatch = await client.recoverLatestRunForRepository({ repo: REPO_A });
      expect(mismatch.ok).toBe(false);
      if (!mismatch.ok) {
        expect(mismatch.kind).toBe("uncertain_outcome");
        expect(mismatch.fault.title).toContain("불일치");
      }

      const empty403 = await client.recoverLatestRunForRepository({ repo: REPO_A });
      expect(empty403.ok).toBe(false);
      if (!empty403.ok) {
        expect(empty403.kind).toBe("request_rejected");
        expect(empty403.fault.httpStatus).toBe(403);
      }

      const err502 = await client.recoverLatestRunForRepository({ repo: REPO_A });
      expect(err502.ok).toBe(false);
      if (!err502.ok) {
        expect(err502.kind).toBe("uncertain_outcome");
        expect(err502.fault.httpStatus).toBe(502);
      }

      expect(requests.filter((r) => r.method === "POST")).toHaveLength(0);
    });

    it("handles 409 IDEMPOTENCY_KEY_EXPIRED on create and bodyless retry without auto-POST or silently issuing a new key", async () => {
      const createKeys: string[] = [];
      const retryKeys: string[] = [];

      const client = new ContractSyncRunClient({
        transport: {
          async send(req) {
            if (req.method === "POST" && req.path === "/v1/sync-runs") {
              createKeys.push(req.headers["Idempotency-Key"]);
              if (createKeys.length === 1) {
                return { status: 502, rawBody: "" };
              }
              if (createKeys.length === 2) {
                return {
                  status: 409,
                  rawBody: JSON.stringify({
                    error: { code: "IDEMPOTENCY_KEY_EXPIRED" },
                  }),
                };
              }
              return {
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_id: "synthetic-run-fresh",
                  status_url: "/v1/sync-runs/synthetic-run-fresh",
                }),
              };
            }
            if (req.method === "POST" && req.path === "/v1/sync-runs/run-exp-1/retry") {
              retryKeys.push(req.headers["Idempotency-Key"]);
              if (retryKeys.length === 1) {
                return { status: 502, rawBody: "" };
              }
              if (retryKeys.length === 2) {
                return {
                  status: 409,
                  rawBody: JSON.stringify({
                    error: { code: "IDEMPOTENCY_KEY_EXPIRED" },
                  }),
                };
              }
              return {
                status: 202,
                rawBody: JSON.stringify({
                  status: "PENDING",
                  target_id: "run-exp-1",
                  status_url: "/v1/sync-runs/run-exp-1",
                }),
              };
            }
            return { status: 404, rawBody: "" };
          },
        },
      });

      // 1. Create gets 502 -> retains pending key
      const firstCreate = await client.createInitialImport({
        repo: REPO_A,
        authScope: "scope-exp",
      });
      expect(firstCreate.ok).toBe(false);

      // 2. Explicit resend gets 409 IDEMPOTENCY_KEY_EXPIRED -> clears expired key, NO auto-POST
      const expiredCreate = await client.createInitialImport({
        repo: REPO_A,
        authScope: "scope-exp",
      });
      expect(expiredCreate.ok).toBe(false);
      if (!expiredCreate.ok) {
        expect(expiredCreate.kind).toBe("uncertain_outcome");
        expect(expiredCreate.fault.httpStatus).toBe(409);
        expect(expiredCreate.fault.title).toContain("만료");
      }
      expect(createKeys).toHaveLength(2);
      expect(createKeys[1]).toBe(createKeys[0]);

      // 3. Only if user explicitly requests again later does it issue a fresh UUID key (never auto)
      const explicitFreshCreate = await client.createInitialImport({
        repo: REPO_A,
        authScope: "scope-exp",
      });
      expect(explicitFreshCreate.ok).toBe(true);
      expect(createKeys).toHaveLength(3);
      expect(createKeys[2]).not.toBe(createKeys[0]);

      // 4. Versioned retry gets 502 then 409 IDEMPOTENCY_KEY_EXPIRED -> clears key, NO auto-POST
      const firstRetry = await client.retryInitialImport({
        runId: "run-exp-1",
        repo: REPO_A,
        expectedRunVersion: 2,
        previousRun: null,
        authScope: "scope-exp",
      });
      expect(firstRetry.ok).toBe(false);

      const expiredRetry = await client.retryInitialImport({
        runId: "run-exp-1",
        repo: REPO_A,
        mode: "resend_pending",
        previousRun: null,
        authScope: "scope-exp",
      });
      expect(expiredRetry.ok).toBe(false);
      if (!expiredRetry.ok) {
        expect(expiredRetry.kind).toBe("uncertain_outcome");
        expect(expiredRetry.fault.httpStatus).toBe(409);
        expect(expiredRetry.fault.errorCode).toBe("IDEMPOTENCY_KEY_EXPIRED");
      }
      expect(retryKeys).toHaveLength(2);
      expect(retryKeys[1]).toBe(retryKeys[0]);

      // Explicit later new retry issues fresh key (expired key was cleared)
      const explicitFreshRetry = await client.retryInitialImport({
        runId: "run-exp-1",
        repo: REPO_A,
        mode: "new_retry",
        expectedRunVersion: 2,
        previousRun: null,
        authScope: "scope-exp",
      });
      expect(explicitFreshRetry.ok).toBe(true);
      expect(retryKeys).toHaveLength(3);
      expect(retryKeys[2]).not.toBe(retryKeys[0]);
    });

    it("discards late recovery GET responses after repository switch or session change", async () => {
      let resolveSlowGet!: (res: SyntheticHttpResponse) => void;
      const adapter = new MockFixtureImportAdapter({
        latencyMs: 0,
        disableBrowserStorage: true,
        syncRunTransport: {
          send(req) {
            if (req.method === "GET" && req.path.startsWith("/v1/sync-runs?")) {
              return new Promise<SyntheticHttpResponse>((resolve) => {
                resolveSlowGet = resolve;
              });
            }
            return Promise.resolve({ status: 404, rawBody: "" });
          },
        },
      });

      const lookupPromise = adapter.recoverRepositoryRun({
        repoId: REPO_A.repoId,
      });
      await Promise.resolve();

      // Switch repository while REPO_A recovery GET is still in flight
      adapter.selectRepository(REPO_B.repoId);

      resolveSlowGet({
        status: 200,
        rawBody: JSON.stringify({
          items: [
            {
              target_id: "synthetic-run-stale-repo-a",
              status_url: "/v1/sync-runs/synthetic-run-stale-repo-a",
              import_status: {
                status: "RUNNING",
                run_version: 2,
                plan_id: "synthetic-plan-stale",
                import_ready: false,
                processing_status: "NOT_STARTED",
                source_drift: false,
                retry: { allowed: false },
              },
            },
          ],
        }),
      });

      const result = await lookupPromise;
      expect(result.ok).toBe(false);
      if (!result.ok) {
        expect(result.kind).toBe("uncertain_outcome");
      }
    });
  });

  describe("OpenAPI 0.5.0-draft versioned retry, resend separation, and approved error envelopes", () => {
    it("reads run_version from detail GET and recovery GET attributed per target_id and forwards expected_run_version JSON", async () => {
      const { transport, requests } = createScriptedTransport([
        // 0: detail GET for run-detail-1 -> run_version: 3
        () => ({
          status: 200,
          rawBody: JSON.stringify({
            status: "PARTIAL",
            run_version: 3,
            plan_id: "plan-detail-1",
            failure_reason: "TRANSIENT_PROVIDER_ERROR",
            import_ready: false,
            processing_status: "NOT_STARTED",
            source_drift: false,
            retry: { allowed: true, from_safe_checkpoint: true },
          }),
        }),
        // 1: retry POST for run-detail-1 -> expects {"expected_run_version":3}
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_type: "SYNC_RUN",
            target_id: "run-detail-1",
            status_url: "/v1/sync-runs/run-detail-1",
          }),
        }),
        // 2: recovery GET for REPO_B -> run-recovered-2 with run_version: 7
        () => ({
          status: 200,
          rawBody: JSON.stringify({
            items: [
              {
                target_id: "run-recovered-2",
                status_url: "/v1/sync-runs/run-recovered-2",
                import_status: {
                  status: "PARTIAL",
                  run_version: 7,
                  plan_id: "plan-rec-2",
                  failure_reason: "TRANSIENT_PROVIDER_ERROR",
                  import_ready: false,
                  processing_status: "NOT_STARTED",
                  source_drift: false,
                  retry: { allowed: true, from_safe_checkpoint: true },
                },
              },
            ],
          }),
        }),
        // 3: retry POST for run-recovered-2 -> expects {"expected_run_version":7}
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_type: "SYNC_RUN",
            target_id: "run-recovered-2",
            status_url: "/v1/sync-runs/run-recovered-2",
          }),
        }),
      ]);

      const client = new ContractSyncRunClient({ transport });

      const detail = await client.getImportStatus({
        runId: "run-detail-1",
        repo: REPO_A,
      });
      expect(detail.ok).toBe(true);
      if (detail.ok) {
        expect(detail.data.run.runVersion).toBe(3);
      }

      const detailRetry = await client.retryInitialImport({
        runId: "run-detail-1",
        repo: REPO_A,
      });
      expect(detailRetry.ok).toBe(true);
      expect(requests[1].method).toBe("POST");
      expect(requests[1].headers["Content-Type"]).toBe("application/json");
      expect(JSON.parse(requests[1].body ?? "{}")).toEqual({
        expected_run_version: 3,
      });

      const recovered = await client.recoverLatestRunForRepository({
        repo: REPO_B,
      });
      expect(recovered.ok).toBe(true);
      if (recovered.ok) {
        expect(recovered.data.run?.runId).toBe("run-recovered-2");
        expect(recovered.data.run?.runVersion).toBe(7);
      }

      const recoveredRetry = await client.retryInitialImport({
        runId: "run-recovered-2",
        repo: REPO_B,
      });
      expect(recoveredRetry.ok).toBe(true);
      expect(requests[3].method).toBe("POST");
      expect(JSON.parse(requests[3].body ?? "{}")).toEqual({
        expected_run_version: 7,
      });
      expect(requests[3].headers["Idempotency-Key"]).not.toBe(
        requests[1].headers["Idempotency-Key"]
      );
    });

    it("sends 0 POST requests when run_version is missing/invalid or belongs to a different run", async () => {
      const { transport, requests } = createScriptedTransport([
        // Detail GET returns run_version for run-A ONLY
        () => ({
          status: 200,
          rawBody: JSON.stringify({
            status: "PARTIAL",
            run_version: 4,
            plan_id: "plan-a",
            failure_reason: "TRANSIENT_PROVIDER_ERROR",
            import_ready: false,
            processing_status: "NOT_STARTED",
            source_drift: false,
            retry: { allowed: true, from_safe_checkpoint: true },
          }),
        }),
        // Detail GET for run-no-ver omits run_version
        () => ({
          status: 200,
          rawBody: JSON.stringify({
            status: "PARTIAL",
            plan_id: "plan-no-ver",
            failure_reason: "TRANSIENT_PROVIDER_ERROR",
            import_ready: false,
            processing_status: "NOT_STARTED",
            source_drift: false,
            retry: { allowed: true, from_safe_checkpoint: true },
          }),
        }),
      ]);

      const client = new ContractSyncRunClient({ transport });
      const runARes = await client.getImportStatus({
        runId: "run-A",
        repo: REPO_A,
      });
      expect(runARes.ok).toBe(true);

      const noVerRes = await client.getImportStatus({
        runId: "run-no-ver",
        repo: REPO_A,
      });
      expect(noVerRes.ok).toBe(true);
      if (noVerRes.ok) {
        expect(noVerRes.data.run.runVersion).toBeUndefined();
      }

      // 1. Missing version on run-no-ver -> 0 POST
      const blockedMissing = await client.retryInitialImport({
        runId: "run-no-ver",
        repo: REPO_A,
      });
      expect(blockedMissing.ok).toBe(false);
      if (!blockedMissing.ok) {
        expect(blockedMissing.fault.title).toContain("run_version");
      }

      // 2. Invalid version (0 / negative / non-integer) -> 0 POST
      for (const badVersion of [0, -2, 1.5, Number.NaN]) {
        const blockedInvalid = await client.retryInitialImport({
          runId: "run-A",
          repo: REPO_A,
          expectedRunVersion: badVersion,
        });
        expect(blockedInvalid.ok).toBe(false);
      }

      // 3. Target mismatch (previousRun is run-A, retrying run-B) -> 0 POST (never borrows run-A's version)
      const blockedMismatch = await client.retryInitialImport({
        runId: "run-B",
        repo: REPO_A,
        previousRun: runARes.ok ? runARes.data.run : null,
      });
      expect(blockedMismatch.ok).toBe(false);
      if (!blockedMismatch.ok) {
        expect(blockedMismatch.fault.title).toContain("불일치");
      }

      expect(requests.filter((r) => r.method === "POST")).toHaveLength(0);
    });

    it("rejects bodyless or invalid retry requests with 422 in SyntheticOpenApiTransport", async () => {
      let openApiState: SyntheticOpenApiState = {
        scenarioId: "partial_retryable",
        pollStep: 1,
        runSequence: 1,
        runVersion: 3,
        isRetryRecoveryFlow: false,
        activeRunId: "run_demo_1",
        activePlanId: "plan-1",
        failNextPollWithNetworkError: false,
      };
      const transport = new SyntheticOpenApiTransport(() => ({
        isAuthenticated: true,
        state: openApiState,
        onStateChange: (next) => {
          openApiState = next;
        },
      }));

      const validKey = "11111111-1111-4111-8111-111111111111";

      // Intentional bodyless retry -> 422
      const bodyless = await transport.send({
        method: "POST",
        path: "/v1/sync-runs/run_demo_1/retry",
        headers: { "Idempotency-Key": validKey },
      });
      expect(bodyless.status).toBe(422);

      // Extra unexpected property -> 422 (additionalProperties: false)
      const extraField = await transport.send({
        method: "POST",
        path: "/v1/sync-runs/run_demo_1/retry",
        headers: {
          "Idempotency-Key": validKey,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ expected_run_version: 3, extra: true }),
      });
      expect(extraField.status).toBe(422);

      // Stale version with new key -> 409 RESOURCE_VERSION_CONFLICT
      const stale = await transport.send({
        method: "POST",
        path: "/v1/sync-runs/run_demo_1/retry",
        headers: {
          "Idempotency-Key": validKey,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ expected_run_version: 2 }),
      });
      expect(stale.status).toBe(409);
      expect(JSON.parse(stale.rawBody ?? "{}").error.code).toBe(
        "RESOURCE_VERSION_CONFLICT"
      );
    });

    it("preserves Failure A pending key and original expected_run_version body across intermediate GET for Failure B, while new_retry uses fresh key and Failure B version", async () => {
      const { transport, requests } = createScriptedTransport([
        // 0: Failure A retry (v3) -> response lost (network error)
        () => {
          throw new Error("LOST_RETRY_A_202");
        },
        // 1: Intermediate detail GET observes Failure B (v5)
        () => ({
          status: 200,
          rawBody: JSON.stringify({
            status: "PARTIAL",
            run_version: 5,
            plan_id: "plan-ab",
            failure_reason: "TRANSIENT_PROVIDER_ERROR",
            import_ready: false,
            processing_status: "NOT_STARTED",
            source_drift: false,
            retry: { allowed: true, from_safe_checkpoint: true },
          }),
        }),
        // 2: Explicit resend of Failure A -> replays original 202
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: "run-ab",
            status_url: "/v1/sync-runs/run-ab",
          }),
        }),
        // 3: Explicit new_retry for Failure B (v5) -> accepted with new key
        () => ({
          status: 202,
          rawBody: JSON.stringify({
            status: "PENDING",
            target_id: "run-ab",
            status_url: "/v1/sync-runs/run-ab",
          }),
        }),
      ]);

      const client = new ContractSyncRunClient({ transport });

      // 1. Failure A retry loses response -> pendingRetry stores (key A, v3)
      const lostA = await client.retryInitialImport({
        runId: "run-ab",
        repo: REPO_A,
        expectedRunVersion: 3,
      });
      expect(lostA.ok).toBe(false);
      expect(client.getPendingRetrySummary()).toEqual({
        runId: "run-ab",
        expectedRunVersion: 3,
      });

      // 2. Intermediate GET updates known run_version to 5, MUST NOT overwrite pendingRetry (v3)
      const getB = await client.getImportStatus({
        runId: "run-ab",
        repo: REPO_A,
      });
      expect(getB.ok).toBe(true);
      if (getB.ok) {
        expect(getB.data.run.runVersion).toBe(5);
        expect(getB.data.pendingRetry).toEqual({
          runId: "run-ab",
          expectedRunVersion: 3,
        });
      }
      // 0 auto-POST during GET
      expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);

      // 3. Explicit resend sends EXACT original key A + {"expected_run_version":3}
      const resendA = await client.retryInitialImport({
        runId: "run-ab",
        repo: REPO_A,
        mode: "resend_pending",
      });
      expect(resendA.ok).toBe(true);
      expect(requests[2].headers["Idempotency-Key"]).toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(requests[2].body).toBe(JSON.stringify({ expected_run_version: 3 }));
      expect(client.getPendingRetrySummary()).toBeNull();

      // 4. Explicit new_retry on Failure B sends FRESH key B + {"expected_run_version":5}
      const newRetryB = await client.retryInitialImport({
        runId: "run-ab",
        repo: REPO_A,
        mode: "new_retry",
        expectedRunVersion: 5,
      });
      expect(newRetryB.ok).toBe(true);
      expect(requests[3].headers["Idempotency-Key"]).not.toBe(
        requests[0].headers["Idempotency-Key"]
      );
      expect(requests[3].body).toBe(JSON.stringify({ expected_run_version: 5 }));
    });

    it("handles approved 0.5.0-draft error envelopes (409 RESOURCE_VERSION_CONFLICT, 401 SESSION_TERMINATED vs empty 401, 403 WORKSPACE_SCOPE_VIOLATION, 404 RESOURCE_NOT_FOUND, 503 AUTH_VERIFICATION_UNAVAILABLE) with 0 auto-POST", async () => {
      const { transport, requests } = createScriptedTransport([
        // 0: 409 RESOURCE_VERSION_CONFLICT on retry
        () => ({
          status: 409,
          rawBody: JSON.stringify({
            error: {
              code: "RESOURCE_VERSION_CONFLICT",
              message: "Run version changed.",
              retryable: false,
              request_id: "req-ver-conflict",
            },
          }),
        }),
        // 1: generic bodyless 401 on GET -> NOT confirmed SESSION_TERMINATED!
        () => ({ status: 401, rawBody: "" }),
        // 2: 503 AUTH_VERIFICATION_UNAVAILABLE (retryable: true) on retry -> 0 auto-POST, keeps pendingRetry
        () => ({
          status: 503,
          rawBody: JSON.stringify({
            error: {
              code: "AUTH_VERIFICATION_UNAVAILABLE",
              message: "Auth verifier unavailable.",
              retryable: true,
              request_id: "req-503-auth",
            },
          }),
        }),
        // 3: 404 RESOURCE_NOT_FOUND on GET -> neutral, clearActiveRun: true
        () => ({
          status: 404,
          rawBody: JSON.stringify({
            error: {
              code: "RESOURCE_NOT_FOUND",
              message: "Not found.",
              retryable: false,
              request_id: "req-404",
            },
          }),
        }),
        // 4: 403 WORKSPACE_SCOPE_VIOLATION on GET -> workspaceAccessRevoked: true, clears pending keys
        () => ({
          status: 403,
          rawBody: JSON.stringify({
            error: {
              code: "WORKSPACE_SCOPE_VIOLATION",
              message: "Workspace access revoked.",
              retryable: false,
              request_id: "req-403-ws",
            },
          }),
        }),
        // 5: 401 SESSION_TERMINATED on GET -> confirmed session_expired
        () => ({
          status: 401,
          rawBody: JSON.stringify({
            error: {
              code: "SESSION_TERMINATED",
              message: "Session terminated.",
              retryable: false,
              request_id: "req-401-term",
            },
          }),
        }),
      ]);

      const client = new ContractSyncRunClient({ transport });

      // 409 RESOURCE_VERSION_CONFLICT
      const verConflict = await client.retryInitialImport({
        runId: "run-err-1",
        repo: REPO_A,
        expectedRunVersion: 2,
      });
      expect(verConflict.ok).toBe(false);
      if (!verConflict.ok) {
        expect(verConflict.kind).toBe("version_conflict");
        expect(verConflict.fault.httpStatus).toBe(409);
        expect(verConflict.fault.errorCode).toBe("RESOURCE_VERSION_CONFLICT");
      }

      // Generic bodyless 401 -> request_rejected (NOT session_expired!)
      const empty401 = await client.getImportStatus({
        runId: "run-err-1",
        repo: REPO_A,
      });
      expect(empty401.ok).toBe(false);
      if (!empty401.ok) {
        expect(empty401.kind).toBe("request_rejected");
        expect(empty401.fault.httpStatus).toBe(401);
        expect(empty401.fault.message).not.toContain("세션이 종료되었습니다");
      }

      // 503 AUTH_VERIFICATION_UNAVAILABLE -> 0 auto-POST despite retryable: true
      const auth503 = await client.retryInitialImport({
        runId: "run-err-1",
        repo: REPO_A,
        expectedRunVersion: 3,
      });
      expect(auth503.ok).toBe(false);
      if (!auth503.ok) {
        expect(auth503.kind).toBe("auth_verification_unavailable");
        expect(auth503.fault.httpStatus).toBe(503);
        expect(auth503.fault.errorCode).toBe("AUTH_VERIFICATION_UNAVAILABLE");
        expect(auth503.pendingRetry).toEqual({
          runId: "run-err-1",
          expectedRunVersion: 3,
        });
      }

      // 404 RESOURCE_NOT_FOUND -> clearActiveRun, neutral message
      const notFound = await client.getImportStatus({
        runId: "run-err-1",
        repo: REPO_A,
      });
      expect(notFound.ok).toBe(false);
      if (!notFound.ok) {
        expect(notFound.kind).toBe("resource_not_found");
        expect(notFound.clearActiveRun).toBe(true);
        expect(notFound.fault.httpStatus).toBe(404);
        expect(notFound.fault.errorCode).toBe("RESOURCE_NOT_FOUND");
        expect(notFound.fault.message).toContain("구별하지 않으며");
      }

      // 403 WORKSPACE_SCOPE_VIOLATION -> workspaceAccessRevoked, clears pending keys
      const ws403 = await client.getImportStatus({
        runId: "run-err-2",
        repo: REPO_A,
      });
      expect(ws403.ok).toBe(false);
      if (!ws403.ok) {
        expect(ws403.kind).toBe("workspace_scope_violation");
        expect(ws403.workspaceAccessRevoked).toBe(true);
        expect(ws403.pendingRetry).toBeNull();
      }

      // 401 SESSION_TERMINATED -> confirmed session_expired
      const term401 = await client.getImportStatus({
        runId: "run-err-2",
        repo: REPO_A,
      });
      expect(term401.ok).toBe(false);
      if (!term401.ok) {
        expect(term401.kind).toBe("session_expired");
        expect(term401.fault.errorCode).toBe("SESSION_TERMINATED");
        expect(term401.pendingRetry).toBeNull();
      }

      // Exactly 6 explicit calls (2 explicit POSTs + 4 explicit GETs), 0 auto-POSTs
      expect(requests).toHaveLength(6);
      expect(requests.filter((r) => r.method === "POST")).toHaveLength(2);
    });

    it("clears pending retry key/body on target switch and never persists it across reload", async () => {
      window.sessionStorage.clear();
      const transport: SyntheticHttpTransport = {
        async send(req) {
          if (req.method === "POST" && req.path.endsWith("/retry")) {
            return { status: 502, rawBody: "" };
          }
          return { status: 200, rawBody: JSON.stringify({ items: [] }) };
        },
      };

      const adapter = new MockFixtureImportAdapter({
        latencyMs: 0,
        disableBrowserStorage: false,
        syncRunTransport: transport,
      });

      const failedRetry = await adapter.retryImportRun("run-persist-check", {
        mode: "new_retry",
        expectedRunVersion: 3,
      });
      expect(failedRetry.ok).toBe(false);
      expect(adapter.syncRunClient.getPendingRetrySummary()).toEqual({
        runId: "run-persist-check",
        expectedRunVersion: 3,
      });

      // Browser storage MUST NOT contain pending retry key or expected_run_version
      const storedRaw =
        window.sessionStorage.getItem("career_memory_spike_web_import_v1") ?? "";
      expect(storedRaw).not.toContain("expected_run_version");
      expect(storedRaw).not.toContain("pendingRetry");

      // Reloaded adapter starts with null pendingRetry
      const reloaded = new MockFixtureImportAdapter({
        latencyMs: 0,
        disableBrowserStorage: false,
        syncRunTransport: transport,
      });
      expect(reloaded.syncRunClient.getPendingRetrySummary()).toBeNull();

      // Switching target repository clears pendingRetry in memory
      adapter.selectRepository(REPO_B.repoId);
      expect(adapter.syncRunClient.getPendingRetrySummary()).toBeNull();

      window.sessionStorage.clear();
    });
  });
});
