import type {
  SyntheticHttpRequest,
  SyntheticHttpResponse,
  SyntheticHttpTransport,
} from "@/adapters/synthetic-http-transport";

/**
 * Test-only real loopback HTTP transport for WEB-05.
 *
 * Connects ONLY the 3 OpenAPI 0.2.0-draft sync-run paths to a local 127.0.0.1
 * FastAPI stub launched from CONTRACT's `create_app` + `FixtureActor`:
 *   - POST /v1/sync-runs
 *   - GET  /v1/sync-runs/{id}
 *   - POST /v1/sync-runs/{id}/retry
 *
 * Strictly test-only: NEVER used by production UI code and NEVER injects
 * synthetic user or workspace identity headers.
 */

const ALLOWED_CREATE_PATH = /^\/v1\/sync-runs$/;
const ALLOWED_STATUS_PATH = /^\/v1\/sync-runs\/[^/?#]+$/;
const ALLOWED_RETRY_PATH = /^\/v1\/sync-runs\/[^/?#]+\/retry$/;

function assertAllowedSyncRunRequest(request: SyntheticHttpRequest): void {
  if (request.method === "POST" && ALLOWED_CREATE_PATH.test(request.path)) {
    return;
  }
  if (request.method === "GET" && ALLOWED_STATUS_PATH.test(request.path)) {
    return;
  }
  if (request.method === "POST" && ALLOWED_RETRY_PATH.test(request.path)) {
    return;
  }
  throw new Error(
    `LoopbackContractHttpTransport blocked non-sync-run route: ${request.method} ${request.path}`
  );
}

export interface LoopbackResponseHookContext {
  request: SyntheticHttpRequest;
  response: SyntheticHttpResponse;
}

export type LoopbackResponseHook = (
  ctx: LoopbackResponseHookContext
) => Promise<SyntheticHttpResponse> | SyntheticHttpResponse;

export class LoopbackContractHttpTransport implements SyntheticHttpTransport {
  private readonly baseUrl: string;
  private readonly afterResponseHooks: LoopbackResponseHook[] = [];
  public readonly sentRequests: SyntheticHttpRequest[] = [];

  constructor(baseUrl: string) {
    if (!baseUrl.startsWith("http://127.0.0.1:")) {
      throw new Error(
        `LoopbackContractHttpTransport only permits http://127.0.0.1:<port>, got ${baseUrl}`
      );
    }
    this.baseUrl = baseUrl;
  }

  /**
   * Queue a one-shot test hook that runs AFTER the real FastAPI stub has
   * processed the HTTP request and returned a response (e.g., to simulate
   * response loss or delayed delivery after stub acceptance).
   */
  public queueAfterResponseHook(hook: LoopbackResponseHook): void {
    this.afterResponseHooks.push(hook);
  }

  public async send(
    request: SyntheticHttpRequest
  ): Promise<SyntheticHttpResponse> {
    assertAllowedSyncRunRequest(request);
    this.sentRequests.push({
      ...request,
      headers: { ...request.headers },
    });

    const res = await fetch(`${this.baseUrl}${request.path}`, {
      method: request.method,
      headers: request.headers,
      // Explicitly pass undefined body for bodyless GET and retry POST
      body: request.body,
    });

    const rawBody = await res.text();
    const headers: Record<string, string> = {};
    res.headers.forEach((value, key) => {
      headers[key] = value;
    });

    const httpResponse: SyntheticHttpResponse = {
      status: res.status,
      headers,
      rawBody,
    };

    const hook = this.afterResponseHooks.shift();
    if (hook) {
      return hook({ request, response: httpResponse });
    }

    return httpResponse;
  }
}
