/**
 * Cloudflare Edge Reverse-Proxy & Origin-Lock Guard (`https://career-memory.site`).
 *
 * Enforces OD-013 / T-507 / INF-07 invariants at ₹0 Free-Tier cost:
 * 1. Canonicalizes HTTP -> HTTPS and `www.career-memory.site` -> `https://career-memory.site` (308).
 * 2. Hard-blocks `/internal/*` routes at the Cloudflare edge (403).
 * 3. Reads `UPSTREAM_WEB_ORIGIN` and `CF_EDGE_AUTH_SECRET` strictly from Cloudflare Encrypted
 *    Secrets (zero Cloud Run `.run.app` URLs or secrets in Git), strips any spoofed incoming
 *    `X-CM-Edge-Auth` header, and attaches a timestamped HMAC-SHA256 `X-CM-Edge-Auth` signature
 *    so direct internet requests to `cm-web` (`*.run.app`) fail closed with `403`.
 * 4. Preserves multi-value `Set-Cookie` headers and enforces `Cache-Control: private, no-store`
 *    (`cf-cache-status: DYNAMIC`) on all auth/BFF routes.
 */

const BLOCKED_EDGE_PREFIX = "/internal/";
const EDGE_AUTH_HEADER = "X-CM-Edge-Auth";

const FORWARDED_HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "x-cm-edge-auth",
]);

const encoder = new TextEncoder();
let cachedSecretStr = null;
let cachedCryptoKeyPromise = null;

function getHmacKey(secret) {
  if (secret !== cachedSecretStr || !cachedCryptoKeyPromise) {
    cachedSecretStr = secret;
    cachedCryptoKeyPromise = crypto.subtle.importKey(
      "raw",
      encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
  }
  return cachedCryptoKeyPromise;
}

function bytesToHex(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

async function signEdgeAuthHeader(secret, method, pathname, nowSeconds) {
  const key = await getHmacKey(secret);
  const message = `${nowSeconds}.${method.toUpperCase()}.${pathname}`;
  const sigBuf = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return `t=${nowSeconds},sig=${bytesToHex(sigBuf)}`;
}

export default {
  async fetch(request, env) {
    const incomingUrl = new URL(request.url);
    const canonicalHost = (env.CANONICAL_HOST || "career-memory.site").toLowerCase();

    // 1. Canonicalize HTTP -> HTTPS and www -> apex
    if (
      incomingUrl.protocol !== "https:" ||
      incomingUrl.hostname.toLowerCase() === `www.${canonicalHost}`
    ) {
      const target = new URL(incomingUrl.toString());
      target.protocol = "https:";
      target.hostname = canonicalHost;
      return Response.redirect(target.toString(), 308);
    }

    // 2. Hard-block internal worker paths at the edge
    if (
      incomingUrl.pathname === "/internal" ||
      incomingUrl.pathname.startsWith(BLOCKED_EDGE_PREFIX)
    ) {
      return new Response(
        JSON.stringify({
          error: {
            code: "PUBLIC_WORKER_ROUTE_FORBIDDEN",
            message: "Internal worker routes are forbidden on public edge ingress.",
            retryable: false,
          },
        }),
        {
          status: 403,
          headers: {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "private, no-store",
            "CDN-Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
          },
        }
      );
    }

    const rawUpstreamOrigin = (env.CF_UPSTREAM_WEB_ORIGIN || "").trim().replace(/\/+$/, "");
    const edgeAuthSecret = (env.CF_EDGE_AUTH_SECRET || "").trim();
    if (!rawUpstreamOrigin || !edgeAuthSecret) {
      return new Response(
        JSON.stringify({
          error: {
            code: "EDGE_NOT_CONFIGURED",
            message: "Edge origin bindings are not configured.",
            retryable: false,
          },
        }),
        {
          status: 503,
          headers: {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "private, no-store",
          },
        }
      );
    }

    // 3. Build upstream request with stripped spoof headers + fresh X-CM-Edge-Auth signature
    const upstreamUrl = new URL(
      incomingUrl.pathname + incomingUrl.search,
      rawUpstreamOrigin
    );

    const method = request.method.toUpperCase();
    const upstreamHeaders = new Headers();
    for (const [key, value] of request.headers.entries()) {
      if (!FORWARDED_HOP_BY_HOP.has(key.toLowerCase())) {
        upstreamHeaders.append(key, value);
      }
    }
    upstreamHeaders.set("X-Forwarded-Host", canonicalHost);
    upstreamHeaders.set("X-Forwarded-Proto", "https");
    const clientIp = request.headers.get("CF-Connecting-IP");
    if (clientIp) {
      upstreamHeaders.set("X-Forwarded-For", clientIp);
    }
    const nowSeconds = Math.floor(Date.now() / 1000);
    upstreamHeaders.set(
      EDGE_AUTH_HEADER,
      await signEdgeAuthHeader(edgeAuthSecret, method, incomingUrl.pathname, nowSeconds)
    );

    const hasBody = method !== "GET" && method !== "HEAD";
    const upstreamResponse = await fetch(upstreamUrl.toString(), {
      method,
      headers: upstreamHeaders,
      body: hasBody ? request.body : undefined,
      redirect: "manual",
    });

    // 4. Preserve multi-value Set-Cookie & strictly enforce no-store Cache-Control on auth/BFF routes
    const responseHeaders = new Headers(upstreamResponse.headers);
    const path = incomingUrl.pathname;
    if (path.startsWith("/auth/") || path.startsWith("/bff/")) {
      responseHeaders.set("Cache-Control", "private, no-store");
      responseHeaders.set("CDN-Cache-Control", "no-store");
    } else if (path === "/api/health" || path === "/healthz") {
      responseHeaders.set("Cache-Control", "no-store");
      responseHeaders.set("CDN-Cache-Control", "no-store");
    } else if (!path.startsWith("/_next/static/")) {
      if (!responseHeaders.has("CDN-Cache-Control")) {
        responseHeaders.set("CDN-Cache-Control", "no-store");
      }
    }

    return new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      statusText: upstreamResponse.statusText,
      headers: responseHeaders,
    });
  },
};
