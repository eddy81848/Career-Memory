/**
 * Server-only configuration and wire constants for the OD-010 / OD-013 Next.js BFF.
 *
 * Header/cache constants mirror `apps/api/src/career_memory_api/edge_guard/models.py`
 * so the Next.js BFF -> FastAPI origin hop matches the backend Edge Guard contract
 * byte-for-byte.
 */

export const ORIGIN_AUTH_HEADER = "X-CM-Origin-Auth";
export const PRIVATE_NO_STORE_CACHE_CONTROL = "private, no-store";
export const PUBLIC_HEALTH_CACHE_CONTROL = "no-store";
export const CDN_NO_STORE_HEADER_VALUE = "no-store";
export const AUTHENTICATED_API_VARY =
  "Authorization, Cookie, X-Workspace-ID, Origin";

export const SECURITY_RESPONSE_HEADERS: ReadonlyArray<readonly [string, string]> = [
  ["X-Content-Type-Options", "nosniff"],
  ["X-Frame-Options", "DENY"],
  ["Referrer-Policy", "no-referrer"],
];

export const STATE_CHANGING_HTTP_METHODS = new Set<string>([
  "POST",
  "PATCH",
  "PUT",
  "DELETE",
]);

export const MIN_COOKIE_SECRET_LENGTH = 32;
export const DEFAULT_REFRESH_SKEW_SECONDS = 30;
export const DEFAULT_PKCE_TTL_SECONDS = 300;
export const DEFAULT_SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;

const SAFE_ORIGIN_RE = /^https?:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?$/;

export class BffConfigurationError extends Error {
  public readonly code = "BFF_NOT_CONFIGURED";

  constructor(message: string) {
    super(message);
    this.name = "BffConfigurationError";
  }
}

export interface BffConfig {
  readonly supabaseUrl: string;
  readonly supabaseAnonKey: string;
  readonly supabaseAuthProvider: string;
  readonly apiOrigin: string;
  readonly publicWebOrigin: string;
  readonly cookieSecret: string;
  readonly cookieSecure: boolean;
  readonly sessionCookieName: string;
  readonly pkceCookieName: string;
  readonly originHmacSecret: string | null;
  readonly refreshSkewSeconds: number;
  readonly pkceTtlSeconds: number;
  readonly sessionCookieMaxAgeSeconds: number;
}

export function normalizeOrigin(raw: string, envName: string): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (!trimmed || !SAFE_ORIGIN_RE.test(trimmed)) {
    throw new BffConfigurationError(
      `${envName} must be a valid origin (e.g. https://app.example.com) without path or wildcard.`
    );
  }
  const parsed = new URL(trimmed);
  return `${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}`;
}

function isLoopbackHttpOrigin(origin: string): boolean {
  try {
    const parsed = new URL(origin);
    return (
      parsed.protocol === "http:" &&
      (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost")
    );
  } catch {
    return false;
  }
}

function parsePositiveInt(
  raw: string | undefined,
  fallback: number,
  envName: string
): number {
  if (!raw || !raw.trim()) return fallback;
  const parsed = Number(raw.trim());
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new BffConfigurationError(
      `${envName} must be a positive integer.`
    );
  }
  return parsed;
}

export function loadBffConfig(
  env: Record<string, string | undefined> = process.env
): BffConfig {
  const rawSupabaseUrl = (env.SUPABASE_URL || "").trim().replace(/\/+$/, "");
  if (!rawSupabaseUrl || !SAFE_ORIGIN_RE.test(rawSupabaseUrl)) {
    throw new BffConfigurationError(
      "SUPABASE_URL is required and must be a valid http(s) origin."
    );
  }

  const supabaseAnonKey = (env.SUPABASE_ANON_KEY || "").trim();
  if (!supabaseAnonKey) {
    throw new BffConfigurationError("SUPABASE_ANON_KEY is required.");
  }

  const supabaseAuthProvider = (
    env.SUPABASE_AUTH_PROVIDER || "github"
  ).trim();
  if (!supabaseAuthProvider || !/^[a-z0-9_-]+$/i.test(supabaseAuthProvider)) {
    throw new BffConfigurationError("SUPABASE_AUTH_PROVIDER is invalid.");
  }

  const apiOrigin = normalizeOrigin(
    env.CAREER_MEMORY_API_ORIGIN || "",
    "CAREER_MEMORY_API_ORIGIN"
  );
  const publicWebOrigin = normalizeOrigin(
    env.CAREER_MEMORY_PUBLIC_WEB_ORIGIN || "",
    "CAREER_MEMORY_PUBLIC_WEB_ORIGIN"
  );

  const cookieSecret = (env.CAREER_MEMORY_BFF_COOKIE_SECRET || "").trim();
  if (cookieSecret.length < MIN_COOKIE_SECRET_LENGTH) {
    throw new BffConfigurationError(
      `CAREER_MEMORY_BFF_COOKIE_SECRET must be at least ${MIN_COOKIE_SECRET_LENGTH} characters.`
    );
  }

  const rawCookieSecure = (env.CAREER_MEMORY_COOKIE_SECURE || "")
    .trim()
    .toLowerCase();
  let cookieSecure: boolean;
  if (rawCookieSecure === "true") {
    cookieSecure = true;
  } else if (rawCookieSecure === "false") {
    if (!isLoopbackHttpOrigin(publicWebOrigin)) {
      throw new BffConfigurationError(
        "CAREER_MEMORY_COOKIE_SECURE=false is only allowed on loopback http://127.0.0.1 or http://localhost origins."
      );
    }
    cookieSecure = false;
  } else if (rawCookieSecure === "") {
    cookieSecure = publicWebOrigin.startsWith("https://");
  } else {
    throw new BffConfigurationError(
      "CAREER_MEMORY_COOKIE_SECURE must be 'true' or 'false' when set."
    );
  }

  const rawOriginSecret = (env.CAREER_MEMORY_ORIGIN_HMAC_SECRET || "").trim();
  const originHmacSecret = rawOriginSecret.length > 0 ? rawOriginSecret : null;

  return {
    supabaseUrl: rawSupabaseUrl,
    supabaseAnonKey,
    supabaseAuthProvider,
    apiOrigin,
    publicWebOrigin,
    cookieSecret,
    cookieSecure,
    sessionCookieName: cookieSecure ? "__Host-cm_session" : "cm_session",
    pkceCookieName: cookieSecure ? "__Host-cm_pkce" : "cm_pkce",
    originHmacSecret,
    refreshSkewSeconds: parsePositiveInt(
      env.CAREER_MEMORY_REFRESH_SKEW_SECONDS,
      DEFAULT_REFRESH_SKEW_SECONDS,
      "CAREER_MEMORY_REFRESH_SKEW_SECONDS"
    ),
    pkceTtlSeconds: parsePositiveInt(
      env.CAREER_MEMORY_PKCE_TTL_SECONDS,
      DEFAULT_PKCE_TTL_SECONDS,
      "CAREER_MEMORY_PKCE_TTL_SECONDS"
    ),
    sessionCookieMaxAgeSeconds: parsePositiveInt(
      env.CAREER_MEMORY_SESSION_COOKIE_MAX_AGE_SECONDS,
      DEFAULT_SESSION_COOKIE_MAX_AGE_SECONDS,
      "CAREER_MEMORY_SESSION_COOKIE_MAX_AGE_SECONDS"
    ),
  };
}
