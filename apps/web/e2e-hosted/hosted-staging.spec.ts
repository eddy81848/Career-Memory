import { expect, test } from "@playwright/test";

/**
 * LIVE DEPLOYED SERVER HOSTED BROWSER & BFF SECURITY E2E SUITE.
 *
 * Runs real headless Chromium + live HTTP probes against deployed Cloud Run `cm-web`
 * (`HOSTED_WEB_URL`) and live Supabase Auth (`PKCE S256` authorize hop).
 * Executed both during CD (pre-promotion candidate & post-promotion public URL,
 * triggering automatic rollback on any failure) and every night in Nightly CI.
 */
test.describe("Hosted Cloud Run cm-web Live Browser & BFF Security E2E", () => {
  for (const routePath of ["/", "/event-review", "/claim-review", "/search"]) {
    test(`renders ${routePath} in hosted mode with Next.js hydration, PKCE gate, zero runtime/console errors, and zero Web Storage tokens`, async ({
      page,
    }) => {
      const pageErrors: string[] = [];
      const appConsoleErrors: string[] = [];
      const brokenAppAssetUrls: string[] = [];
      page.on("pageerror", (err) => pageErrors.push(err.message));
      page.on("response", (res) => {
        const url = res.url();
        if (res.status() >= 500) {
          brokenAppAssetUrls.push(`${res.status()} ${url}`);
        } else if (
          res.status() >= 400 &&
          !url.endsWith("/favicon.ico") &&
          url.includes("/_next/")
        ) {
          brokenAppAssetUrls.push(`${res.status()} ${url}`);
        }
      });
      page.on("console", (msg) => {
        if (msg.type() !== "error") return;
        const locUrl = msg.location()?.url ?? "";
        const text = msg.text();
        if (
          locUrl.endsWith("/favicon.ico") ||
          (text.includes("status of 404") && !locUrl.includes("/_next/"))
        ) {
          return;
        }
        appConsoleErrors.push(text);
      });

      const response = await page.goto(routePath, { waitUntil: "networkidle" });
      expect(response).not.toBeNull();
      expect(response!.status()).toBe(200);

      await expect(page.getByTestId("hosted-auth-gate")).toBeVisible();
      const loginLink = page.getByTestId("hosted-login-link");
      await expect(loginLink).toBeVisible();
      await expect(loginLink).toHaveAttribute(
        "href",
        `/auth/login?next=${encodeURIComponent(routePath)}`
      );

      const hydratedAndZeroStorage = await page.evaluate(() => ({
        nextHydrated:
          document.querySelectorAll("script[src*='/_next/static/']").length > 0,
        storageKeys: window.localStorage.length + window.sessionStorage.length,
      }));
      expect(hydratedAndZeroStorage.nextHydrated).toBe(true);
      expect(hydratedAndZeroStorage.storageKeys).toBe(0);
      expect(brokenAppAssetUrls).toEqual([]);
      expect(pageErrors).toEqual([]);
      expect(appConsoleErrors).toEqual([]);
    });
  }

  test("returns clean 404 on unknown route /non-existent-route-check without 500 crash", async ({
    page,
  }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(err.message));

    const response = await page.goto("/non-existent-route-check", {
      waitUntil: "domcontentloaded",
    });
    expect(response).not.toBeNull();
    expect(response!.status()).toBe(404);
    expect(pageErrors).toEqual([]);
  });

  test("verifies live PKCE S256 OAuth redirect chain through live Supabase Auth toward GitHub OAuth", async ({
    request,
  }) => {
    const healthRes = await request.get("/api/health");
    expect(healthRes.status()).toBe(200);
    expect(healthRes.headers()["cache-control"] ?? "").toContain("no-store");

    const sessionRes = await request.get("/auth/session");
    expect(sessionRes.status()).toBe(200);
    expect(sessionRes.headers()["cache-control"] ?? "").toContain("private");
    expect(sessionRes.headers()["cache-control"] ?? "").toContain("no-store");
    const sessionBody = await sessionRes.json();
    expect(sessionBody.authenticated).toBe(false);

    const pkceRes = await request.get("/auth/login?next=/claim-review", {
      maxRedirects: 0,
    });
    expect([302, 303, 307]).toContain(pkceRes.status());
    const authorizeLocation = pkceRes.headers()["location"] ?? "";
    expect(authorizeLocation).toContain("/auth/v1/authorize");

    const parsedAuthUrl = new URL(authorizeLocation);
    expect(parsedAuthUrl.searchParams.get("provider")).toBe("github");
    expect(parsedAuthUrl.searchParams.get("code_challenge")).toBeTruthy();
    expect(
      (parsedAuthUrl.searchParams.get("code_challenge_method") ?? "").toUpperCase()
    ).toBe("S256");
    expect(parsedAuthUrl.searchParams.get("state")).toBeTruthy();
    expect(parsedAuthUrl.searchParams.get("redirect_to") ?? "").toMatch(
      /\/auth\/callback$/
    );

    const setCookieLower = (pkceRes.headers()["set-cookie"] ?? "").toLowerCase();
    expect(setCookieLower).toContain("__host-cm_pkce=");
    expect(setCookieLower).toContain("httponly");
    expect(setCookieLower).toContain("secure");

    // Follow Location to live Supabase Auth:
    // - 302/303 -> GitHub OAuth authorize when GitHub provider is enabled in Supabase
    // - 400 validation_failed ("provider is not enabled") -> live GoTrue reached cleanly before operator OAuth toggle
    const supaRes = await request.get(authorizeLocation, { maxRedirects: 0 });
    expect(supaRes.status()).toBeLessThan(500);
    if ([302, 303].includes(supaRes.status())) {
      const githubLocation = supaRes.headers()["location"] ?? "";
      expect(githubLocation).toContain("github.com/login/oauth/authorize");
    } else {
      expect(supaRes.status()).toBe(400);
      const supaBody = await supaRes.json();
      expect(String(supaBody?.msg ?? "")).toContain("provider is not enabled");
    }

    for (const badNext of [
      "//evil.example",
      "https://evil.example/phish",
      "/auth/login",
    ]) {
      const openRedirRes = await request.get(
        `/auth/login?next=${encodeURIComponent(badNext)}`,
        { maxRedirects: 0 }
      );
      expect(openRedirRes.status()).toBe(400);
    }
  });

  test("enforces fail-closed /auth/callback PKCE state guards and /auth/logout CSRF & cookie clearing", async ({
    request,
  }) => {
    // 1. /auth/callback with missing state cookie -> 400 PKCE_STATE_MISMATCH, 0 session cookie minted
    const missingCookieCb = await request.get(
      "/auth/callback?code=synthetic_code&state=missing_cookie_state",
      { maxRedirects: 0 }
    );
    expect(missingCookieCb.status()).toBe(400);
    expect(missingCookieCb.headers()["cache-control"] ?? "").toContain(
      "no-store"
    );
    const missingCbBody = await missingCookieCb.json();
    expect(missingCbBody?.error?.code).toBe("PKCE_STATE_MISMATCH");
    const missingCbSetCookie = missingCookieCb.headers()["set-cookie"] ?? "";
    expect(missingCbSetCookie).not.toMatch(/__Host-cm_session=[^;]+/);

    // 2. Obtain a real encrypted __Host-cm_pkce cookie, then send mismatched state -> 400, 0 session cookie
    const loginRes = await request.get("/auth/login?next=/event-review", {
      maxRedirects: 0,
    });
    const loginSetCookie = loginRes.headers()["set-cookie"] ?? "";
    const pkceCookiePair = loginSetCookie.split(";")[0]?.trim() ?? "";
    expect(pkceCookiePair.startsWith("__Host-cm_pkce=")).toBe(true);

    const redirectToParam =
      new URL(loginRes.headers()["location"] ?? "").searchParams.get(
        "redirect_to"
      ) ?? "";
    const configuredPublicOrigin = new URL(redirectToParam).origin;

    const mismatchCb = await request.get(
      "/auth/callback?code=synthetic_code&state=forged_mismatched_state",
      {
        maxRedirects: 0,
        headers: {
          Cookie: pkceCookiePair,
        },
      }
    );
    expect(mismatchCb.status()).toBe(400);
    const mismatchBody = await mismatchCb.json();
    expect(mismatchBody?.error?.code).toBe("PKCE_STATE_MISMATCH");
    const mismatchSetCookie = mismatchCb.headers()["set-cookie"] ?? "";
    expect(mismatchSetCookie.toLowerCase()).toContain("max-age=0");
    expect(mismatchSetCookie).not.toMatch(/__Host-cm_session=[^;]+/);

    // 3. /auth/logout cross-origin -> 403, missing CSRF -> 403, valid same-origin + CSRF probe -> 200 clear cookie
    const evilLogout = await request.post("/auth/logout", {
      headers: {
        Origin: "https://evil.example",
        "X-CSRF-Token": "probe-csrf",
      },
    });
    expect(evilLogout.status()).toBe(403);

    const noCsrfLogout = await request.post("/auth/logout", {
      headers: {
        Origin: configuredPublicOrigin,
      },
    });
    expect(noCsrfLogout.status()).toBe(403);

    const cleanLogout = await request.post("/auth/logout", {
      headers: {
        Origin: configuredPublicOrigin,
        "X-CSRF-Token": "probe-csrf-token",
      },
    });
    expect(cleanLogout.status()).toBe(200);
    const cleanLogoutBody = await cleanLogout.json();
    expect(cleanLogoutBody.authenticated).toBe(false);
    const logoutSetCookie = (
      cleanLogout.headers()["set-cookie"] ?? ""
    ).toLowerCase();
    expect(logoutSetCookie).toContain("__host-cm_session=");
    expect(logoutSetCookie).toContain("max-age=0");
  });

  test("enforces BFF proxy 401 no-store without cookie, 401 clean rejection on forged AES-256-GCM cookie, and 403 cross-origin CSRF block", async ({
    request,
  }) => {
    const bffReadRoutes = [
      "/bff/v1/session",
      "/bff/v1/work-events",
      "/bff/v1/claims",
      "/bff/v1/search?q=architecture",
    ];

    const forgedAesGcmCookie =
      "__Host-cm_session=v1.AAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBAA.CCCCCCCCCCCCCCCC";

    for (const bffRoute of bffReadRoutes) {
      // (a) No cookie -> 401 + Cache-Control: private, no-store
      const unauthRes = await request.get(bffRoute);
      expect(unauthRes.status()).toBe(401);
      const cc = unauthRes.headers()["cache-control"] ?? "";
      expect(cc).toContain("private");
      expect(cc).toContain("no-store");

      // (b) Forged/tampered __Host-cm_session AES-256-GCM cookie -> 401 (zero 500!) + clear cookie
      const forgedRes = await request.get(bffRoute, {
        headers: {
          Cookie: forgedAesGcmCookie,
        },
      });
      expect(forgedRes.status()).toBe(401);
      const forgedSetCookie = (
        forgedRes.headers()["set-cookie"] ?? ""
      ).toLowerCase();
      expect(forgedSetCookie).toContain("__host-cm_session=");
      expect(forgedSetCookie).toContain("max-age=0");
    }

    // Forged cookie on /auth/session also degrades cleanly to authenticated=false (200, zero 500) + clears cookie
    const forgedAuthSession = await request.get("/auth/session", {
      headers: {
        Cookie: forgedAesGcmCookie,
      },
    });
    expect(forgedAuthSession.status()).toBe(200);
    const forgedSessionBody = await forgedAuthSession.json();
    expect(forgedSessionBody.authenticated).toBe(false);
    expect(
      (forgedAuthSession.headers()["set-cookie"] ?? "").toLowerCase()
    ).toContain("max-age=0");

    // (c) Cross-origin mutating POST /bff/v1/sync-runs -> 403
    const csrfRes = await request.post("/bff/v1/sync-runs", {
      headers: {
        Origin: "https://evil.example",
        "Content-Type": "application/json",
      },
      data: {},
    });
    expect(csrfRes.status()).toBe(403);
  });
});
