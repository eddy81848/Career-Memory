// @vitest-environment node
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  deriveEdgeAuthSubkey,
  EDGE_AUTH_HEADER,
  verifyEdgeAuthHeader,
} from "@/server/bff/edge-auth";

const REPO_ROOT = path.resolve(__dirname, "../../../../../..");
const WRANGLER_TOML_PATH = path.join(REPO_ROOT, "infra/cloudflare/wrangler.toml");
const WORKER_MJS_PATH = path.join(REPO_ROOT, "infra/cloudflare/src/worker.mjs");

interface EdgeWorkerModule {
  fetch: (
    request: Request,
    env: Record<string, string | undefined>
  ) => Promise<Response>;
}

let worker: EdgeWorkerModule;

beforeAll(async () => {
  const mod = (await import(
    /* @vite-ignore */ pathToFileURL(WORKER_MJS_PATH).href
  )) as { default: EdgeWorkerModule };
  worker = mod.default;
});

describe("Cloudflare Edge Worker (infra/cloudflare) unit & security invariants", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("enforces zero plaintext .run.app URLs, workers_dev=false, and canonical routes in wrangler.toml", () => {
    const toml = fs.readFileSync(WRANGLER_TOML_PATH, "utf8");
    expect(toml).toContain('name = "career-memory-edge"');
    expect(toml).toContain("workers_dev = false");
    expect(toml).toContain('{ pattern = "career-memory.site", custom_domain = true }');
    expect(toml).toContain('{ pattern = "www.career-memory.site", custom_domain = true }');
    expect(toml).not.toMatch(/\.run\.app/i);
    expect(toml).not.toMatch(/CF_EDGE_AUTH_SECRET\s*=/);
    expect(toml).not.toMatch(/CF_UPSTREAM_WEB_ORIGIN\s*=/);
  });

  it("canonicalizes HTTP -> HTTPS and www -> apex with 308 while preserving PKCE callback query params", async () => {
    const env = {
      CANONICAL_HOST: "career-memory.site",
      CF_UPSTREAM_WEB_ORIGIN: "https://upstream-web.internal.example",
      CF_EDGE_AUTH_SECRET: "edge-subkey-test",
    };

    const httpRes = await worker.fetch(
      new Request("http://career-memory.site/auth/callback?state=st1&code=c1"),
      env
    );
    expect(httpRes.status).toBe(308);
    expect(httpRes.headers.get("location")).toBe(
      "https://career-memory.site/auth/callback?state=st1&code=c1"
    );

    const wwwRes = await worker.fetch(
      new Request("https://www.career-memory.site/event-review?tab=inbox"),
      env
    );
    expect(wwwRes.status).toBe(308);
    expect(wwwRes.headers.get("location")).toBe(
      "https://career-memory.site/event-review?tab=inbox"
    );
  });

  it("hard-blocks /internal and /internal/* worker routes at the edge with 403 no-store", async () => {
    const env = {
      CANONICAL_HOST: "career-memory.site",
      CF_UPSTREAM_WEB_ORIGIN: "https://upstream-web.internal.example",
      CF_EDGE_AUTH_SECRET: "edge-subkey-test",
    };

    for (const blockedPath of ["/internal", "/internal/worker/wake"]) {
      const res = await worker.fetch(
        new Request(`https://career-memory.site${blockedPath}`, {
          method: "POST",
        }),
        env
      );
      expect(res.status).toBe(403);
      expect(res.headers.get("cache-control")).toBe("private, no-store");
      const body = (await res.json()) as { error: { code: string } };
      expect(body.error.code).toBe("PUBLIC_WORKER_ROUTE_FORBIDDEN");
    }
  });

  it("fails closed with 503 EDGE_NOT_CONFIGURED when encrypted secrets are absent", async () => {
    const res = await worker.fetch(new Request("https://career-memory.site/"), {
      CANONICAL_HOST: "career-memory.site",
    });
    expect(res.status).toBe(503);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("EDGE_NOT_CONFIGURED");
  });

  it("strips spoofed X-CM-Edge-Auth, attaches a valid signature accepted by cm-web, and preserves multi-Set-Cookie", async () => {
    const masterOriginSecret = "test-master-origin-hmac-secret-0123456789";
    const derivedSubkey = await deriveEdgeAuthSubkey(masterOriginSecret);

    let capturedUpstreamUrl = "";
    let capturedUpstreamHeaders: Headers | null = null;

    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      capturedUpstreamUrl = String(input);
      capturedUpstreamHeaders = new Headers(init?.headers);
      const upstreamHeaders = new Headers({
        Location: "/event-review",
      });
      upstreamHeaders.append(
        "Set-Cookie",
        "__Host-cm_pkce=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
      );
      upstreamHeaders.append(
        "Set-Cookie",
        "__Host-cm_session=encrypted-v1; Path=/; Max-Age=604800; HttpOnly; Secure; SameSite=Lax"
      );
      return new Response(null, { status: 302, headers: upstreamHeaders });
    }) as typeof fetch;

    const res = await worker.fetch(
      new Request(
        "https://career-memory.site/auth/callback?state=pkce_state_ok&code=auth_code_ok",
        {
          headers: {
            Cookie: "__Host-cm_pkce=encrypted-pkce",
            "CF-Connecting-IP": "203.0.113.42",
            [EDGE_AUTH_HEADER]: "t=9999999999,sig=" + "f".repeat(64),
          },
        }
      ),
      {
        CANONICAL_HOST: "career-memory.site",
        CF_UPSTREAM_WEB_ORIGIN: "https://upstream-web.internal.example",
        CF_EDGE_AUTH_SECRET: derivedSubkey,
      }
    );

    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe("/event-review");
    expect(res.headers.get("cache-control")).toBe("private, no-store");
    expect(res.headers.get("cdn-cache-control")).toBe("no-store");

    expect(capturedUpstreamUrl).toBe(
      "https://upstream-web.internal.example/auth/callback?state=pkce_state_ok&code=auth_code_ok"
    );
    expect(capturedUpstreamHeaders).not.toBeNull();
    expect(capturedUpstreamHeaders!.get("x-forwarded-host")).toBe(
      "career-memory.site"
    );
    expect(capturedUpstreamHeaders!.get("x-forwarded-proto")).toBe("https");
    expect(capturedUpstreamHeaders!.get("x-forwarded-for")).toBe("203.0.113.42");

    // Verify cm-web middleware accepts the worker's fresh X-CM-Edge-Auth header (and spoofed header was replaced)
    const signedHeader = capturedUpstreamHeaders!.get(EDGE_AUTH_HEADER);
    expect(signedHeader).not.toContain("f".repeat(64));
    const verified = await verifyEdgeAuthHeader({
      headerValue: signedHeader,
      method: "GET",
      pathname: "/auth/callback",
      masterOriginHmacSecret: masterOriginSecret,
    });
    expect(verified).toBe(true);

    // Multi-value Set-Cookie preservation check
    const setCookies = (
      res.headers as Headers & { getSetCookie?: () => string[] }
    ).getSetCookie?.() ?? [res.headers.get("set-cookie") || ""];
    expect(setCookies).toHaveLength(2);
    expect(setCookies[0]).toContain("__Host-cm_pkce=");
    expect(setCookies[1]).toContain("__Host-cm_session=encrypted-v1");
  });
});
