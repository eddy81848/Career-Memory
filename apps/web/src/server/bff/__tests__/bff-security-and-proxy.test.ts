// @vitest-environment node
import crypto from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  BffConfigurationError,
  loadBffConfig,
  ORIGIN_AUTH_HEADER,
  PRIVATE_NO_STORE_CACHE_CONTROL,
  type BffConfig,
} from "@/server/bff/config";
import {
  decryptSessionCookie,
  deriveSessionCsrfToken,
  encryptSessionCookie,
  signOriginAuthHeader,
  type SessionEnvelope,
} from "@/server/bff/crypto";
import {
  handleAuthCallback,
  handleAuthLogin,
  handleAuthLogout,
  handleAuthSession,
  handleBffProxy,
  type BffDependencies,
} from "@/server/bff/handlers";
import { ConsumedPkceStateStore } from "@/server/bff/pkce";
import { SingleFlightRefreshCoordinator } from "@/server/bff/refresh-coordinator";

const TEST_CONFIG: BffConfig = loadBffConfig({
  SUPABASE_URL: "https://supabase.test.example",
  SUPABASE_ANON_KEY: "test-supabase-anon-key-not-secret",
  SUPABASE_AUTH_PROVIDER: "github",
  CAREER_MEMORY_API_ORIGIN: "https://api.internal.example",
  CAREER_MEMORY_PUBLIC_WEB_ORIGIN: "https://app.career-memory.example",
  CAREER_MEMORY_BFF_COOKIE_SECRET:
    "0123456789abcdef0123456789abcdef0123456789abcdef",
  CAREER_MEMORY_COOKIE_SECURE: "true",
  CAREER_MEMORY_ORIGIN_HMAC_SECRET: "origin-hmac-shared-secret-test-value",
});

const SAMPLE_BACKEND_SESSION = {
  mode: "hosted_supabase",
  workspace_id: "11111111-1111-4111-8111-111111111111",
  workspace_name: "Hosted Pilot Workspace",
  user_id: "22222222-2222-4222-8222-222222222222",
  user_display_name: "Seoyeon Park",
  user_email: "seoyeon@career-memory.example",
  github_login: "seoyeon-dev",
  connected_account_id: "33333333-3333-4333-8333-333333333333",
  resources: [],
  default_source_resource_id: null,
  default_confirmed_event_id: null,
  sample_repositories: [],
};

function makeFakeJwt(payload: Record<string, unknown>): string {
  const header = Buffer.from(
    JSON.stringify({ alg: "ES256", typ: "JWT" }),
    "utf8"
  ).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload), "utf8").toString(
    "base64url"
  );
  return `${header}.${body}.synthetic-sig`;
}

function getSetCookies(res: Response): string[] {
  const headers = res.headers as Headers & { getSetCookie?: () => string[] };
  if (typeof headers.getSetCookie === "function") {
    return headers.getSetCookie();
  }
  const single = res.headers.get("set-cookie");
  return single ? [single] : [];
}

function extractCookiePair(setCookie: string): string {
  return setCookie.split(";")[0].trim();
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "private, no-store",
    },
  });
}

function buildDeps(overrides?: Partial<BffDependencies>): BffDependencies {
  return {
    config: TEST_CONFIG,
    nowSeconds: () => 1_750_000_000,
    refreshCoordinator: new SingleFlightRefreshCoordinator(),
    consumedPkceStates: new ConsumedPkceStateStore(),
    fetchImpl: (async () => {
      throw new Error("Unexpected upstream fetch");
    }) as typeof fetch,
    ...overrides,
  };
}

function buildSignedSessionCookie(
  session: SessionEnvelope,
  config: BffConfig = TEST_CONFIG
): { cookieHeader: string; csrfToken: string } {
  const encrypted = encryptSessionCookie(session, config.cookieSecret);
  const csrfToken = deriveSessionCsrfToken(config.cookieSecret, session);
  return {
    cookieHeader: `${config.sessionCookieName}=${encrypted}`,
    csrfToken,
  };
}

describe("OD-010 / OD-013 Next.js BFF Security & Proxy Handlers", () => {
  describe("Configuration fail-closed invariants", () => {
    it("rejects missing or short CAREER_MEMORY_BFF_COOKIE_SECRET and returns 503 BFF_NOT_CONFIGURED", async () => {
      expect(() =>
        loadBffConfig({
          SUPABASE_URL: "https://supabase.test.example",
          SUPABASE_ANON_KEY: "anon",
          CAREER_MEMORY_API_ORIGIN: "https://api.internal.example",
          CAREER_MEMORY_PUBLIC_WEB_ORIGIN: "https://app.career-memory.example",
          CAREER_MEMORY_BFF_COOKIE_SECRET: "too-short",
        })
      ).toThrow(BffConfigurationError);

      const res = await handleAuthSession(
        new Request("https://app.career-memory.example/auth/session"),
        { env: {} }
      );
      expect(res.status).toBe(503);
      expect(res.headers.get("cache-control")).toBe(PRIVATE_NO_STORE_CACHE_CONTROL);
      const body = (await res.json()) as { error: { code: string } };
      expect(body.error.code).toBe("BFF_NOT_CONFIGURED");
    });

    it("uses __Host-cm_session on HTTPS and cm_session only on explicit local HTTP loopback", () => {
      expect(TEST_CONFIG.sessionCookieName).toBe("__Host-cm_session");
      expect(TEST_CONFIG.pkceCookieName).toBe("__Host-cm_pkce");
      expect(TEST_CONFIG.cookieSecure).toBe(true);

      const localCfg = loadBffConfig({
        SUPABASE_URL: "http://127.0.0.1:54321",
        SUPABASE_ANON_KEY: "local-anon",
        CAREER_MEMORY_API_ORIGIN: "http://127.0.0.1:8000",
        CAREER_MEMORY_PUBLIC_WEB_ORIGIN: "http://127.0.0.1:3100",
        CAREER_MEMORY_BFF_COOKIE_SECRET:
          "0123456789abcdef0123456789abcdef0123456789abcdef",
        CAREER_MEMORY_COOKIE_SECURE: "false",
      });
      expect(localCfg.sessionCookieName).toBe("cm_session");
      expect(localCfg.pkceCookieName).toBe("cm_pkce");
      expect(localCfg.cookieSecure).toBe(false);
    });
  });

  describe("OD010-1: PKCE login, callback, one-time consumption, state & open-redirect defense", () => {
    it("completes PKCE /auth/login -> /auth/callback happy path and blocks replay with 400 PKCE_STATE_MISMATCH", async () => {
      const consumedPkceStates = new ConsumedPkceStateStore();
      const rawAccessToken = makeFakeJwt({
        sub: "22222222-2222-4222-8222-222222222222",
        session_id: "sess-pkce-001",
        exp: 1_750_003_600,
      });
      const rawRefreshToken = "sb-refresh-secret-value-001";

      let capturedTokenReq: {
        url: string;
        apikey: string | null;
        body: { auth_code: string; code_verifier: string };
      } | null = null;

      const deps = buildDeps({
        consumedPkceStates,
        fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url === "https://supabase.test.example/auth/v1/token?grant_type=pkce") {
            const headers = new Headers(init?.headers);
            capturedTokenReq = {
              url,
              apikey: headers.get("apikey"),
              body: JSON.parse(String(init?.body)) as {
                auth_code: string;
                code_verifier: string;
              },
            };
            return jsonResponse({
              access_token: rawAccessToken,
              refresh_token: rawRefreshToken,
              expires_at: 1_750_003_600,
              user: { id: "22222222-2222-4222-8222-222222222222" },
            });
          }
          return jsonResponse({ error: "unexpected" }, 500);
        }) as typeof fetch,
      });

      // Step 1: GET /auth/login?next=/event-review
      const loginRes = await handleAuthLogin(
        new Request(
          "https://app.career-memory.example/auth/login?next=%2Fevent-review"
        ),
        deps
      );
      expect(loginRes.status).toBe(302);
      expect(loginRes.headers.get("cache-control")).toBe(
        PRIVATE_NO_STORE_CACHE_CONTROL
      );

      const authorizeLocation = loginRes.headers.get("location") || "";
      const authUrl = new URL(authorizeLocation);
      expect(authUrl.origin).toBe("https://supabase.test.example");
      expect(authUrl.pathname).toBe("/auth/v1/authorize");
      expect(authUrl.searchParams.get("provider")).toBe("github");
      const redirectToRaw = authUrl.searchParams.get("redirect_to") || "";
      const redirectToUrl = new URL(redirectToRaw);
      expect(`${redirectToUrl.origin}${redirectToUrl.pathname}`).toBe(
        "https://app.career-memory.example/auth/callback"
      );
      expect(authUrl.searchParams.get("code_challenge_method")).toBe("s256");
      const state = redirectToUrl.searchParams.get("state") || "";
      const codeChallenge = authUrl.searchParams.get("code_challenge") || "";
      expect(state.length).toBeGreaterThanOrEqual(24);
      expect(codeChallenge.length).toBeGreaterThanOrEqual(32);

      const loginCookies = getSetCookies(loginRes);
      expect(loginCookies).toHaveLength(1);
      expect(loginCookies[0]).toContain("__Host-cm_pkce=");
      expect(loginCookies[0]).toContain("HttpOnly");
      expect(loginCookies[0]).toContain("Secure");
      expect(loginCookies[0]).toContain("SameSite=Lax");
      expect(loginCookies[0]).toContain("Path=/");
      expect(loginCookies[0]).toContain("Max-Age=300");

      const pkceCookiePair = extractCookiePair(loginCookies[0]);

      // Step 2: GET /auth/callback?code=auth-code-123&state=<state>
      const callbackRes = await handleAuthCallback(
        new Request(
          `https://app.career-memory.example/auth/callback?code=auth-code-123&state=${encodeURIComponent(state)}`,
          {
            headers: {
              Cookie: pkceCookiePair,
            },
          }
        ),
        deps
      );
      expect(callbackRes.status).toBe(302);
      expect(callbackRes.headers.get("location")).toBe("/event-review");
      expect(callbackRes.headers.get("cache-control")).toBe(
        PRIVATE_NO_STORE_CACHE_CONTROL
      );

      // Verify PKCE S256 challenge matched the verifier sent to Supabase
      expect(capturedTokenReq).not.toBeNull();
      expect(capturedTokenReq!.apikey).toBe(TEST_CONFIG.supabaseAnonKey);
      expect(capturedTokenReq!.body.auth_code).toBe("auth-code-123");
      const recomputedChallenge = crypto
        .createHash("sha256")
        .update(capturedTokenReq!.body.code_verifier, "ascii")
        .digest("base64url");
      expect(recomputedChallenge).toBe(codeChallenge);

      // Verify Set-Cookie clears PKCE cookie AND sets encrypted __Host-cm_session
      const cbCookies = getSetCookies(callbackRes);
      const clearPkce = cbCookies.find((c) => c.startsWith("__Host-cm_pkce="));
      const setSession = cbCookies.find((c) =>
        c.startsWith("__Host-cm_session=")
      );
      expect(clearPkce).toContain("Max-Age=0");
      expect(setSession).toBeDefined();
      expect(setSession).toContain("HttpOnly");
      expect(setSession).toContain("Secure");
      expect(setSession).toContain("SameSite=Lax");
      expect(setSession).toContain("Path=/");
      expect(setSession).not.toContain("Domain=");
      // Raw tokens MUST NEVER appear in plaintext Set-Cookie
      expect(setSession).not.toContain(rawAccessToken);
      expect(setSession).not.toContain(rawRefreshToken);

      // Decrypt confirms envelope holds rotated credentials server-side
      const encryptedVal = extractCookiePair(setSession!).split("=")[1];
      const decrypted = decryptSessionCookie(
        encryptedVal,
        TEST_CONFIG.cookieSecret
      );
      expect(decrypted?.access_token).toBe(rawAccessToken);
      expect(decrypted?.refresh_token).toBe(rawRefreshToken);
      expect(decrypted?.session_id).toBe("sess-pkce-001");

      // Step 3: Replay exact same callback + captured PKCE cookie -> MUST fail 400!
      const replayRes = await handleAuthCallback(
        new Request(
          `https://app.career-memory.example/auth/callback?code=auth-code-123&state=${encodeURIComponent(state)}`,
          {
            headers: {
              Cookie: pkceCookiePair,
            },
          }
        ),
        deps
      );
      expect(replayRes.status).toBe(400);
      const replayJson = (await replayRes.json()) as { error: { code: string } };
      expect(replayJson.error.code).toBe("PKCE_STATE_MISMATCH");
      const replayCookies = getSetCookies(replayRes);
      expect(
        replayCookies.some((c) => c.startsWith("__Host-cm_session="))
      ).toBe(false);
    });

    it("rejects tampered state on /auth/callback with 400 PKCE_STATE_MISMATCH and clears PKCE cookie", async () => {
      const deps = buildDeps();
      const loginRes = await handleAuthLogin(
        new Request("https://app.career-memory.example/auth/login"),
        deps
      );
      const pkceCookiePair = extractCookiePair(getSetCookies(loginRes)[0]);

      const badRes = await handleAuthCallback(
        new Request(
          "https://app.career-memory.example/auth/callback?code=abc&state=tampered-state",
          {
            headers: { Cookie: pkceCookiePair },
          }
        ),
        deps
      );
      expect(badRes.status).toBe(400);
      const body = (await badRes.json()) as { error: { code: string } };
      expect(body.error.code).toBe("PKCE_STATE_MISMATCH");
      const cookies = getSetCookies(badRes);
      expect(cookies.some((c) => c.includes("__Host-cm_pkce=") && c.includes("Max-Age=0"))).toBe(true);
      expect(cookies.some((c) => c.startsWith("__Host-cm_session="))).toBe(false);
    });

    it("rejects open-redirect payloads on /auth/login (?next=//evil.example, https://evil.example, /\\evil, /auth/callback)", async () => {
      const deps = buildDeps();
      const invalidPaths = [
        "//evil.example",
        "https://evil.example/steal",
        "http://evil.example",
        "/\\evil.example",
        "/%5cevil.example",
        "/auth/callback",
        "/bff/v1/work-events",
        "/../etc/passwd",
        "javascript:alert(1)",
      ];

      for (const badNext of invalidPaths) {
        const res = await handleAuthLogin(
          new Request(
            `https://app.career-memory.example/auth/login?next=${encodeURIComponent(badNext)}`
          ),
          deps
        );
        expect(res.status, `Expected 400 for next=${badNext}`).toBe(400);
        const body = (await res.json()) as { error: { code: string } };
        expect(body.error.code).toBe("INVALID_RETURN_PATH");
        expect(getSetCookies(res)).toHaveLength(0);
      }
    });
  });

  describe("OD010-2 & OD010-3: Cookie encryption, CSRF/Origin enforcement, and X-CM-Origin-Auth HMAC signing", () => {
    it("never exposes raw access_token or refresh_token in /auth/session JSON and signs X-CM-Origin-Auth", async () => {
      const session: SessionEnvelope = {
        access_token: "raw-access-jwt-never-in-browser",
        refresh_token: "raw-refresh-token-never-in-browser",
        expires_at: 1_750_003_600,
        user_sub: SAMPLE_BACKEND_SESSION.user_id,
        session_id: "sess-meta-001",
        issued_at: 1_750_000_000,
      };
      const { cookieHeader, csrfToken } = buildSignedSessionCookie(session);

      let seenOriginAuth: string | null = null;
      let seenAuthorization: string | null = null;

      const deps = buildDeps({
        nowSeconds: () => 1_750_000_010,
        fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          expect(url).toBe("https://api.internal.example/v1/session");
          const headers = new Headers(init?.headers);
          seenOriginAuth = headers.get(ORIGIN_AUTH_HEADER);
          seenAuthorization = headers.get("authorization");
          // Even if backend mistakenly included token keys, BFF strips them
          return jsonResponse({
            ...SAMPLE_BACKEND_SESSION,
            access_token: "must-be-stripped",
            refresh_token: "must-be-stripped",
          });
        }) as typeof fetch,
      });

      const res = await handleAuthSession(
        new Request("https://app.career-memory.example/auth/session", {
          headers: { Cookie: cookieHeader },
        }),
        deps
      );

      expect(res.status).toBe(200);
      expect(res.headers.get("cache-control")).toBe(
        PRIVATE_NO_STORE_CACHE_CONTROL
      );
      expect(seenAuthorization).toBe(`Bearer ${session.access_token}`);
      expect(seenOriginAuth).toBe(
        signOriginAuthHeader({
          secret: TEST_CONFIG.originHmacSecret!,
          method: "GET",
          pathname: "/v1/session",
          timestampSeconds: 1_750_000_010,
        })
      );
      expect(seenOriginAuth).toMatch(/^t=1750000010,sig=[0-9a-f]{64}$/);

      const rawText = await res.text();
      expect(rawText).not.toContain(session.access_token);
      expect(rawText).not.toContain(session.refresh_token);
      expect(rawText).not.toContain("must-be-stripped");

      const body = JSON.parse(rawText) as {
        authenticated: boolean;
        csrf_token: string;
        session: Record<string, unknown>;
      };
      expect(body.authenticated).toBe(true);
      expect(body.csrf_token).toBe(csrfToken);
      expect(body.session.workspace_id).toBe(SAMPLE_BACKEND_SESSION.workspace_id);
      expect(body.session.access_token).toBeUndefined();
      expect(body.session.refresh_token).toBeUndefined();
    });

    it("rejects cross-origin Origin, Sec-Fetch-Site=cross-site, and missing/invalid X-CSRF-Token with 403 before touching backend", async () => {
      const session: SessionEnvelope = {
        access_token: "raw-access-jwt-csrf",
        refresh_token: "raw-refresh-csrf",
        expires_at: 1_750_003_600,
        user_sub: SAMPLE_BACKEND_SESSION.user_id,
        session_id: "sess-csrf-001",
        issued_at: 1_750_000_000,
      };
      const { cookieHeader, csrfToken } = buildSignedSessionCookie(session);

      let backendCalled = 0;
      const deps = buildDeps({
        fetchImpl: (async () => {
          backendCalled += 1;
          return jsonResponse({ ok: true });
        }) as typeof fetch,
      });

      // 1. Cross-origin Origin
      const crossOriginRes = await handleBffProxy(
        new Request("https://app.career-memory.example/bff/v1/sync-runs", {
          method: "POST",
          headers: {
            Origin: "https://evil.attacker.example",
            Cookie: cookieHeader,
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({}),
        }),
        ["sync-runs"],
        deps
      );
      expect(crossOriginRes.status).toBe(403);
      expect(((await crossOriginRes.json()) as { error: { code: string } }).error.code).toBe(
        "CSRF_VALIDATION_FAILED"
      );

      // 2. Missing Origin
      const missingOriginRes = await handleBffProxy(
        new Request("https://app.career-memory.example/bff/v1/sync-runs", {
          method: "POST",
          headers: {
            Cookie: cookieHeader,
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({}),
        }),
        ["sync-runs"],
        deps
      );
      expect(missingOriginRes.status).toBe(403);

      // 3. Sec-Fetch-Site: cross-site
      const crossSiteFetchRes = await handleBffProxy(
        new Request("https://app.career-memory.example/bff/v1/sync-runs", {
          method: "POST",
          headers: {
            Origin: TEST_CONFIG.publicWebOrigin,
            "Sec-Fetch-Site": "cross-site",
            Cookie: cookieHeader,
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({}),
        }),
        ["sync-runs"],
        deps
      );
      expect(crossSiteFetchRes.status).toBe(403);

      // 4. Missing X-CSRF-Token
      const missingCsrfRes = await handleBffProxy(
        new Request("https://app.career-memory.example/bff/v1/sync-runs", {
          method: "POST",
          headers: {
            Origin: TEST_CONFIG.publicWebOrigin,
            Cookie: cookieHeader,
          },
          body: JSON.stringify({}),
        }),
        ["sync-runs"],
        deps
      );
      expect(missingCsrfRes.status).toBe(403);

      // 5. Invalid X-CSRF-Token
      const badCsrfRes = await handleBffProxy(
        new Request("https://app.career-memory.example/bff/v1/sync-runs", {
          method: "POST",
          headers: {
            Origin: TEST_CONFIG.publicWebOrigin,
            Cookie: cookieHeader,
            "X-CSRF-Token": "forged-csrf-token",
          },
          body: JSON.stringify({}),
        }),
        ["sync-runs"],
        deps
      );
      expect(badCsrfRes.status).toBe(403);

      expect(backendCalled).toBe(0);
    });

    it("signs X-CM-Origin-Auth over /v1/path WITHOUT query string and strips Cookie & browser Authorization", async () => {
      const session: SessionEnvelope = {
        access_token: "server-only-jwt-999",
        refresh_token: "server-only-refresh-999",
        expires_at: 1_750_003_600,
        user_sub: SAMPLE_BACKEND_SESSION.user_id,
        session_id: "sess-proxy-001",
        issued_at: 1_750_000_000,
      };
      const { cookieHeader } = buildSignedSessionCookie(session);

      const captured: { url: string; headers: Headers | null } = {
        url: "",
        headers: null,
      };
      const fixedNow = 1_750_000_123;

      const deps = buildDeps({
        nowSeconds: () => fixedNow,
        fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
          captured.url = typeof input === "string" ? input : input.toString();
          captured.headers = new Headers(init?.headers);
          return jsonResponse({ items: [] }, 200);
        }) as typeof fetch,
      });

      const res = await handleBffProxy(
        new Request(
          "https://app.career-memory.example/bff/v1/work-events?limit=50&status=CONFIRMED",
          {
            method: "GET",
            headers: {
              Cookie: cookieHeader,
              Authorization: "Bearer forged-browser-token-must-be-ignored",
              "X-Workspace-ID": SAMPLE_BACKEND_SESSION.workspace_id,
              "X-Request-ID": "req-test-123",
              Accept: "application/json",
            },
          }
        ),
        ["work-events"],
        deps
      );

      expect(res.status).toBe(200);
      expect(res.headers.get("cache-control")).toBe(
        PRIVATE_NO_STORE_CACHE_CONTROL
      );
      expect(res.headers.get("cdn-cache-control")).toBe("no-store");

      expect(captured.url).toBe(
        "https://api.internal.example/v1/work-events?limit=50&status=CONFIRMED"
      );
      expect(captured.headers?.get("authorization")).toBe(
        "Bearer server-only-jwt-999"
      );
      expect(captured.headers?.get("cookie")).toBeNull();
      expect(captured.headers?.get("x-workspace-id")).toBe(
        SAMPLE_BACKEND_SESSION.workspace_id
      );

      // Must sign ONLY `/v1/work-events` (no `?limit=50&status=CONFIRMED`)
      const expectedSig = signOriginAuthHeader({
        secret: TEST_CONFIG.originHmacSecret!,
        method: "GET",
        pathname: "/v1/work-events",
        timestampSeconds: fixedNow,
      });
      const wrongWithQuerySig = signOriginAuthHeader({
        secret: TEST_CONFIG.originHmacSecret!,
        method: "GET",
        pathname: "/v1/work-events?limit=50&status=CONFIRMED",
        timestampSeconds: fixedNow,
      });
      expect(captured.headers?.get(ORIGIN_AUTH_HEADER)).toBe(expectedSig);
      expect(captured.headers?.get(ORIGIN_AUTH_HEADER)).toMatch(
        /^t=1750000123,sig=[0-9a-f]{64}$/
      );
      // Verify canonical message matches Python's `f"{ts}:{method}:{path}"` directly
      const manualHmac = crypto
        .createHmac("sha256", Buffer.from(TEST_CONFIG.originHmacSecret!, "utf8"))
        .update(Buffer.from(`${fixedNow}:GET:/v1/work-events`, "utf8"))
        .digest("hex");
      expect(captured.headers?.get(ORIGIN_AUTH_HEADER)).toBe(
        `t=${fixedNow},sig=${manualHmac}`
      );
      expect(expectedSig).not.toBe(
        `t=${fixedNow},sig=${crypto
          .createHmac("sha256", Buffer.from(TEST_CONFIG.originHmacSecret!, "utf8"))
          .update(
            Buffer.from(
              `${fixedNow}:GET:/v1/work-events?limit=50&status=CONFIRMED`,
              "utf8"
            )
          )
          .digest("hex")}`
      );
      // signOriginAuthHeader defensively strips query strings too
      expect(wrongWithQuerySig).toBe(expectedSig);
    });
  });

  describe("OD010-4: Single-flight concurrent multi-tab token refresh", () => {
    it("coalesces 4 simultaneous expired-token /bff/v1/* requests into EXACTLY 1 upstream refresh call", async () => {
      const expiredSession: SessionEnvelope = {
        access_token: "expired-access-token",
        refresh_token: "initial-refresh-token",
        expires_at: 1_750_000_010, // within 30s skew of now=1_750_000_000
        user_sub: SAMPLE_BACKEND_SESSION.user_id,
        session_id: "sess-concurrent-tabs",
        issued_at: 1_749_996_400,
      };
      const { cookieHeader } = buildSignedSessionCookie(expiredSession);

      let refreshCalls = 0;
      const seenApiBearers: string[] = [];
      const coordinator = new SingleFlightRefreshCoordinator();

      const deps = buildDeps({
        nowSeconds: () => 1_750_000_000,
        refreshCoordinator: coordinator,
        fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (
            url ===
            "https://supabase.test.example/auth/v1/token?grant_type=refresh_token"
          ) {
            refreshCalls += 1;
            // Yield microtasks/timer so all 4 tabs collide while in-flight
            await new Promise((resolve) => setTimeout(resolve, 25));
            return jsonResponse({
              access_token: "rotated-fresh-access-token",
              refresh_token: "rotated-fresh-refresh-token",
              expires_at: 1_750_003_600,
              user: { id: SAMPLE_BACKEND_SESSION.user_id },
            });
          }
          if (url.startsWith("https://api.internal.example/v1/")) {
            const headers = new Headers(init?.headers);
            seenApiBearers.push(headers.get("authorization") || "");
            return jsonResponse({ ok: true, items: [] }, 200);
          }
          return jsonResponse({ error: "unexpected" }, 500);
        }) as typeof fetch,
      });

      const responses = await Promise.all([
        handleBffProxy(
          new Request("https://app.career-memory.example/bff/v1/work-events", {
            headers: { Cookie: cookieHeader },
          }),
          ["work-events"],
          deps
        ),
        handleBffProxy(
          new Request("https://app.career-memory.example/bff/v1/claims", {
            headers: { Cookie: cookieHeader },
          }),
          ["claims"],
          deps
        ),
        handleBffProxy(
          new Request("https://app.career-memory.example/bff/v1/quota-status", {
            headers: { Cookie: cookieHeader },
          }),
          ["quota-status"],
          deps
        ),
        handleBffProxy(
          new Request("https://app.career-memory.example/bff/v1/source-resources", {
            headers: { Cookie: cookieHeader },
          }),
          ["source-resources"],
          deps
        ),
      ]);

      expect(refreshCalls).toBe(1);
      expect(seenApiBearers).toEqual([
        "Bearer rotated-fresh-access-token",
        "Bearer rotated-fresh-access-token",
        "Bearer rotated-fresh-access-token",
        "Bearer rotated-fresh-access-token",
      ]);

      for (const res of responses) {
        expect(res.status).toBe(200);
        const setCookies = getSetCookies(res);
        expect(setCookies.length).toBe(1);
        expect(setCookies[0]).toContain("__Host-cm_session=");
        const val = extractCookiePair(setCookies[0]).split("=")[1];
        const decoded = decryptSessionCookie(val, TEST_CONFIG.cookieSecret);
        expect(decoded?.access_token).toBe("rotated-fresh-access-token");
        expect(decoded?.refresh_token).toBe("rotated-fresh-refresh-token");
        // CSRF binding stays stable across refresh
        expect(decoded?.session_id).toBe(expiredSession.session_id);
        expect(decoded?.issued_at).toBe(expiredSession.issued_at);
      }
    });
  });

  describe("OD010-5: Logout revocation, 401 SESSION_TERMINATED cookie clearing, and 503 session preservation", () => {
    it("revokes upstream GoTrue on POST /auth/logout and clears session cookie", async () => {
      const session: SessionEnvelope = {
        access_token: "logout-access-jwt",
        refresh_token: "logout-refresh-token",
        expires_at: 1_750_003_600,
        user_sub: SAMPLE_BACKEND_SESSION.user_id,
        session_id: "sess-logout-001",
        issued_at: 1_750_000_000,
      };
      const { cookieHeader, csrfToken } = buildSignedSessionCookie(session);

      let logoutUpstreamUrl = "";
      let logoutAuthHeader = "";

      const deps = buildDeps({
        fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
          logoutUpstreamUrl = typeof input === "string" ? input : input.toString();
          const headers = new Headers(init?.headers);
          logoutAuthHeader = headers.get("authorization") || "";
          return new Response(null, { status: 204 });
        }) as typeof fetch,
      });

      const res = await handleAuthLogout(
        new Request("https://app.career-memory.example/auth/logout", {
          method: "POST",
          headers: {
            Origin: TEST_CONFIG.publicWebOrigin,
            Cookie: cookieHeader,
            "X-CSRF-Token": csrfToken,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ scope: "global" }),
        }),
        deps
      );

      expect(res.status).toBe(200);
      expect(res.headers.get("cache-control")).toBe(
        PRIVATE_NO_STORE_CACHE_CONTROL
      );
      expect(logoutUpstreamUrl).toBe(
        "https://supabase.test.example/auth/v1/logout?scope=global"
      );
      expect(logoutAuthHeader).toBe("Bearer logout-access-jwt");

      const body = (await res.json()) as { authenticated: boolean };
      expect(body.authenticated).toBe(false);

      const cookies = getSetCookies(res);
      expect(cookies).toHaveLength(1);
      expect(cookies[0]).toContain("__Host-cm_session=;");
      expect(cookies[0]).toContain("Max-Age=0");
    });

    it("clears BFF session cookie on backend 401 SESSION_TERMINATED", async () => {
      const session: SessionEnvelope = {
        access_token: "revoked-access-jwt",
        refresh_token: "revoked-refresh-token",
        expires_at: 1_750_003_600,
        user_sub: SAMPLE_BACKEND_SESSION.user_id,
        session_id: "sess-terminated-001",
        issued_at: 1_750_000_000,
      };
      const { cookieHeader } = buildSignedSessionCookie(session);

      const deps = buildDeps({
        fetchImpl: (async () =>
          jsonResponse(
            {
              error: {
                code: "SESSION_TERMINATED",
                message: "Session revoked",
                retryable: false,
                request_id: "req-401",
              },
            },
            401
          )) as typeof fetch,
      });

      const sessionRes = await handleAuthSession(
        new Request("https://app.career-memory.example/auth/session", {
          headers: { Cookie: cookieHeader },
        }),
        deps
      );
      expect(sessionRes.status).toBe(401);
      expect(
        getSetCookies(sessionRes).some(
          (c) => c.includes("__Host-cm_session=;") && c.includes("Max-Age=0")
        )
      ).toBe(true);

      const proxyRes = await handleBffProxy(
        new Request("https://app.career-memory.example/bff/v1/sync-runs", {
          method: "GET",
          headers: { Cookie: cookieHeader },
        }),
        ["sync-runs"],
        deps
      );
      expect(proxyRes.status).toBe(401);
      expect(
        getSetCookies(proxyRes).some(
          (c) => c.includes("__Host-cm_session=;") && c.includes("Max-Age=0")
        )
      ).toBe(true);
    });

    it("preserves BFF session cookie on backend 503 AUTH_VERIFICATION_UNAVAILABLE and performs 0 automatic POST retries", async () => {
      const session: SessionEnvelope = {
        access_token: "valid-access-jwt-during-503",
        refresh_token: "valid-refresh-during-503",
        expires_at: 1_750_003_600,
        user_sub: SAMPLE_BACKEND_SESSION.user_id,
        session_id: "sess-503-001",
        issued_at: 1_750_000_000,
      };
      const { cookieHeader, csrfToken } = buildSignedSessionCookie(session);

      let postAttempts = 0;
      const deps = buildDeps({
        fetchImpl: (async (_input: RequestInfo | URL, init?: RequestInit) => {
          if ((init?.method || "GET").toUpperCase() === "POST") {
            postAttempts += 1;
          }
          return jsonResponse(
            {
              error: {
                code: "AUTH_VERIFICATION_UNAVAILABLE",
                message: "JWKS/GoTrue temporarily unreachable",
                retryable: true,
                request_id: "req-503",
              },
            },
            503
          );
        }) as typeof fetch,
      });

      // 1. GET /auth/session -> 503 and ZERO Set-Cookie clearing!
      const sessionRes = await handleAuthSession(
        new Request("https://app.career-memory.example/auth/session", {
          headers: { Cookie: cookieHeader },
        }),
        deps
      );
      expect(sessionRes.status).toBe(503);
      expect(sessionRes.headers.get("cache-control")).toBe(
        PRIVATE_NO_STORE_CACHE_CONTROL
      );
      expect(getSetCookies(sessionRes)).toHaveLength(0);

      // 2. POST /bff/v1/sync-runs -> 503, ZERO cookie clearing, and EXACTLY 1 upstream call (0 retries!)
      const postRes = await handleBffProxy(
        new Request("https://app.career-memory.example/bff/v1/sync-runs", {
          method: "POST",
          headers: {
            Origin: TEST_CONFIG.publicWebOrigin,
            Cookie: cookieHeader,
            "X-CSRF-Token": csrfToken,
            "Content-Type": "application/json",
            "Idempotency-Key": "idem-key-503-test",
          },
          body: JSON.stringify({ target_id: "repo-1" }),
        }),
        ["sync-runs"],
        deps
      );
      expect(postRes.status).toBe(503);
      expect(postRes.headers.get("cache-control")).toBe(
        PRIVATE_NO_STORE_CACHE_CONTROL
      );
      expect(getSetCookies(postRes)).toHaveLength(0);
      expect(postAttempts).toBe(1);
    });
  });
});
