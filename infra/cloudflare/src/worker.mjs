/**
 * Cloudflare Edge Reverse-Proxy & Security Guard for Career Memory (`https://career-memory.site`).
 *
 * Enforces OD-013 / T-507 / INF-07 edge invariants at ₹0 Free-Tier cost:
 * 1. Canonicalizes `www.career-memory.site` -> `https://career-memory.site` (308) so `__Host-`
 *    cookies and `Origin` checks never split across hostnames.
 * 2. Blocks any external `/internal/*` path at the Cloudflare edge (403).
 * 3. Proxies requests to Cloud Run `cm-web` (`UPSTREAM_WEB_ORIGIN`) without following redirects
 *    (`redirect: "manual"`), preserving PKCE/Session `__Host-*` `Set-Cookie` headers and `Origin`.
 * 4. Forces `cacheEverything: false` and `CDN-Cache-Control: no-store` on dynamic/auth routes so
 *    authenticated or session responses are NEVER cached at Cloudflare POPs.
 */

const BLOCKED_EDGE_PREFIX = "/internal/";

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
]);

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

    // 3. Build upstream Cloud Run `cm-web` URL & sanitized headers
    const upstreamOrigin = (
      env.UPSTREAM_WEB_ORIGIN || "https://cm-web-oaxjehpcqq-uc.a.run.app"
    ).replace(/\/+$/, "");
    const upstreamUrl = new URL(
      incomingUrl.pathname + incomingUrl.search,
      upstreamOrigin
    );

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

    const method = request.method.toUpperCase();
    const hasBody = method !== "GET" && method !== "HEAD";

    const upstreamResponse = await fetch(upstreamUrl.toString(), {
      method,
      headers: upstreamHeaders,
      body: hasBody ? request.body : undefined,
      redirect: "manual",
    });

    // 4. Preserve multi-value Set-Cookie & strictly enforce no-store Cache-Control on auth/API routes
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
