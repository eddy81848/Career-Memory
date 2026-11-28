import crypto from "node:crypto";
import { sha256Hex } from "@/server/bff/crypto";

/**
 * RFC 7636 PKCE generation, strict internal return-path validation (open-redirect
 * defense), Supabase authorize URL construction, and server-side at-most-once PKCE
 * state consumption tracking (OD010-1).
 */

const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;
const SCHEME_PREFIX_RE = /^\/[a-zA-Z][a-zA-Z0-9+.-]*:/;

export type ReturnPathValidationResult =
  | { readonly ok: true; readonly path: string }
  | { readonly ok: false; readonly errorCode: "INVALID_RETURN_PATH" };

export function generateCodeVerifier(): string {
  return crypto.randomBytes(32).toString("base64url");
}

export function computeCodeChallengeS256(codeVerifier: string): string {
  return crypto
    .createHash("sha256")
    .update(codeVerifier, "ascii")
    .digest("base64url");
}

export function generateOAuthState(): string {
  return crypto.randomBytes(24).toString("base64url");
}

/**
 * Validate `?next=` against a strict internal relative-path allowlist (OD010-1).
 *
 * Accepts only single-leading-slash relative paths (e.g. "/", "/event-review",
 * "/claim-review?event_id=123"). Rejects protocol-relative ("//evil.example"),
 * absolute URLs ("https://evil.example"), backslashes ("/\evil"), control chars,
 * path traversal (".."), and internal auth/BFF loop paths ("/auth/*", "/bff/*").
 */
export function validateReturnTo(
  rawNext: string | null | undefined
): ReturnPathValidationResult {
  if (rawNext === null || rawNext === undefined || rawNext === "") {
    return { ok: true, path: "/" };
  }
  if (typeof rawNext !== "string") {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }
  if (rawNext.length > 512) {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }
  if (
    !rawNext.startsWith("/") ||
    rawNext.startsWith("//") ||
    rawNext.includes("\\") ||
    rawNext.includes("://") ||
    rawNext.includes("@") ||
    CONTROL_CHAR_RE.test(rawNext) ||
    /\s/.test(rawNext) ||
    SCHEME_PREFIX_RE.test(rawNext)
  ) {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }

  let decoded = rawNext;
  try {
    decoded = decodeURIComponent(rawNext);
  } catch {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }
  if (
    decoded.includes("\\") ||
    decoded.includes("..") ||
    decoded.startsWith("//") ||
    CONTROL_CHAR_RE.test(decoded)
  ) {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }

  let parsed: URL;
  try {
    parsed = new URL(rawNext, "https://internal.invalid");
  } catch {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }
  if (
    parsed.origin !== "https://internal.invalid" ||
    !parsed.pathname.startsWith("/") ||
    parsed.pathname.startsWith("//")
  ) {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }

  const lowerPath = parsed.pathname.toLowerCase();
  if (
    lowerPath === "/auth" ||
    lowerPath.startsWith("/auth/") ||
    lowerPath === "/bff" ||
    lowerPath.startsWith("/bff/") ||
    lowerPath === "/local-dev" ||
    lowerPath.startsWith("/local-dev/")
  ) {
    return { ok: false, errorCode: "INVALID_RETURN_PATH" };
  }

  return {
    ok: true,
    path: `${parsed.pathname}${parsed.search}`,
  };
}

export function buildSupabaseAuthorizeUrl(params: {
  supabaseUrl: string;
  provider: string;
  callbackUrl: string;
  codeChallenge: string;
  state: string;
}): string {
  const url = new URL(`${params.supabaseUrl.replace(/\/+$/, "")}/auth/v1/authorize`);
  url.searchParams.set("provider", params.provider);
  url.searchParams.set("redirect_to", params.callbackUrl);
  url.searchParams.set("code_challenge", params.codeChallenge);
  url.searchParams.set("code_challenge_method", "s256");
  url.searchParams.set("state", params.state);
  return url.toString();
}

/**
 * Server-side at-most-once PKCE state consumption guard.
 *
 * Even if an attacker replays a captured `__Host-cm_pkce` cookie header alongside
 * the callback query string, the second callback attempt fails immediately with
 * `400 PKCE_STATE_MISMATCH`.
 */
export class ConsumedPkceStateStore {
  private readonly consumed = new Map<string, number>();

  public consumeOnce(
    state: string,
    nowSeconds: number,
    ttlSeconds: number
  ): boolean {
    this.prune(nowSeconds);
    const key = sha256Hex(state);
    if (this.consumed.has(key)) {
      return false;
    }
    this.consumed.set(key, nowSeconds + Math.max(ttlSeconds, 60));
    return true;
  }

  public prune(nowSeconds: number): void {
    for (const [key, expiresAt] of this.consumed.entries()) {
      if (expiresAt <= nowSeconds) {
        this.consumed.delete(key);
      }
    }
  }

  public clear(): void {
    this.consumed.clear();
  }
}
