import { describe, expect, it } from "vitest";
import { NextRequest } from "next/server";
import { middleware } from "@/middleware";
import {
  deriveEdgeAuthSubkey,
  EDGE_AUTH_HEADER,
  signEdgeAuthHeader,
  verifyEdgeAuthHeader,
} from "@/server/bff/edge-auth";

describe("Cloudflare -> cm-web Edge Origin Proof (X-CM-Edge-Auth)", () => {
  const masterSecret = "test-master-origin-hmac-secret-at-least-32-bytes";

  it("derives deterministic domain-separated subkey and verifies valid signatures", async () => {
    const subkey = await deriveEdgeAuthSubkey(masterSecret);
    expect(subkey).toMatch(/^[0-9a-f]{64}$/);
    expect(subkey).not.toBe(masterSecret);

    const now = 1_760_000_000;
    const hdr = await signEdgeAuthHeader(subkey, "GET", "/event-review", now);
    expect(
      await verifyEdgeAuthHeader({
        headerValue: hdr,
        masterOriginHmacSecret: masterSecret,
        method: "GET",
        pathname: "/event-review",
        nowSeconds: now + 5,
      })
    ).toBe(true);

    // Rejects wrong path, wrong method, expired timestamp, or tampered signature
    expect(
      await verifyEdgeAuthHeader({
        headerValue: hdr,
        masterOriginHmacSecret: masterSecret,
        method: "GET",
        pathname: "/claim-review",
        nowSeconds: now,
      })
    ).toBe(false);
    expect(
      await verifyEdgeAuthHeader({
        headerValue: hdr,
        masterOriginHmacSecret: masterSecret,
        method: "POST",
        pathname: "/event-review",
        nowSeconds: now,
      })
    ).toBe(false);
    expect(
      await verifyEdgeAuthHeader({
        headerValue: hdr,
        masterOriginHmacSecret: masterSecret,
        method: "GET",
        pathname: "/event-review",
        nowSeconds: now + 305,
      })
    ).toBe(false);
  });

  it("blocks direct Cloud Run bypass with 403 EDGE_ORIGIN_AUTH_REQUIRED when enabled", async () => {
    const prevRequire = process.env.CAREER_MEMORY_REQUIRE_EDGE_AUTH;
    const prevSecret = process.env.CAREER_MEMORY_ORIGIN_HMAC_SECRET;
    try {
      process.env.CAREER_MEMORY_REQUIRE_EDGE_AUTH = "true";
      process.env.CAREER_MEMORY_ORIGIN_HMAC_SECRET = masterSecret;

      const directReq = new NextRequest("https://cm-web-xyz.a.run.app/event-review");
      const blockedRes = await middleware(directReq);
      expect(blockedRes.status).toBe(403);
      expect(blockedRes.headers.get("cache-control")).toBe("private, no-store");
      const body = await blockedRes.json();
      expect(body.error.code).toBe("EDGE_ORIGIN_AUTH_REQUIRED");

      const subkey = await deriveEdgeAuthSubkey(masterSecret);
      const validHdr = await signEdgeAuthHeader(
        subkey,
        "GET",
        "/event-review",
        Math.floor(Date.now() / 1000)
      );
      const cfReq = new NextRequest("https://cm-web-xyz.a.run.app/event-review", {
        headers: { [EDGE_AUTH_HEADER]: validHdr },
      });
      const allowedRes = await middleware(cfReq);
      expect(allowedRes.status).toBe(200);
    } finally {
      process.env.CAREER_MEMORY_REQUIRE_EDGE_AUTH = prevRequire;
      process.env.CAREER_MEMORY_ORIGIN_HMAC_SECRET = prevSecret;
    }
  });
});
