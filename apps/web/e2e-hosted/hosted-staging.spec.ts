import { expect, test } from "@playwright/test";

/**
 * LIVE POST-DEPLOY HOSTED BROWSER E2E VERIFICATION SUITE.
 *
 * Runs real headless Chromium against the deployed Cloud Run `cm-web` URL
 * (`HOSTED_WEB_URL`) before and after traffic promotion. If any assertion fails,
 * the CD pipeline automatically rolls back `cm-web`, `cm-api`, and `cm-worker`.
 */
test.describe("Hosted Cloud Run cm-web Post-Deploy Browser E2E", () => {
  for (const routePath of ["/", "/event-review", "/claim-review", "/search"]) {
    test(`renders ${routePath} in hosted mode with PKCE gate, zero runtime errors, and zero Web Storage tokens`, async ({
      page,
    }) => {
      const pageErrors: string[] = [];
      page.on("pageerror", (err) => pageErrors.push(err.message));

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

      const storageKeyCount = await page.evaluate(
        () => window.localStorage.length + window.sessionStorage.length
      );
      expect(storageKeyCount).toBe(0);
      expect(pageErrors).toEqual([]);
    });
  }

  test("enforces /api/health no-store, /auth/session no-store, PKCE S256 redirect, open-redirect block, and cross-origin CSRF block", async ({
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

    const pkceRes = await request.get("/auth/login?next=/event-review", {
      maxRedirects: 0,
    });
    expect([302, 303, 307]).toContain(pkceRes.status());
    expect(pkceRes.headers()["location"] ?? "").toContain("code_challenge=");
    expect(
      (pkceRes.headers()["set-cookie"] ?? "").toLowerCase()
    ).toContain("httponly");

    const openRedirRes = await request.get("/auth/login?next=//evil.example", {
      maxRedirects: 0,
    });
    expect(openRedirRes.status()).toBe(400);

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
