import { NextResponse, type NextRequest } from "next/server";
import {
  EDGE_AUTH_HEADER,
  verifyEdgeAuthHeader,
} from "@/server/bff/edge-auth";

function denyDirectCloudRunBypass(code: string, message: string, status = 403): NextResponse {
  return new NextResponse(
    JSON.stringify({
      error: {
        code,
        message,
        retryable: false,
      },
    }),
    {
      status,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "private, no-store",
        "CDN-Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
      },
    }
  );
}

export async function middleware(request: NextRequest): Promise<NextResponse> {
  const requireEdgeAuth =
    (process.env.CAREER_MEMORY_REQUIRE_EDGE_AUTH || "").trim().toLowerCase() ===
    "true";

  if (!requireEdgeAuth) {
    return NextResponse.next();
  }

  const masterSecret = (process.env.CAREER_MEMORY_ORIGIN_HMAC_SECRET || "").trim();
  if (!masterSecret) {
    return denyDirectCloudRunBypass(
      "EDGE_AUTH_MISCONFIGURED",
      "Edge origin authentication is required but not configured.",
      503
    );
  }

  const verified = await verifyEdgeAuthHeader({
    headerValue: request.headers.get(EDGE_AUTH_HEADER),
    masterOriginHmacSecret: masterSecret,
    method: request.method,
    pathname: request.nextUrl.pathname,
  });

  if (!verified) {
    return denyDirectCloudRunBypass(
      "EDGE_ORIGIN_AUTH_REQUIRED",
      "Direct Cloud Run access is forbidden; use the official Cloudflare domain.",
      403
    );
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/:path*"],
};
