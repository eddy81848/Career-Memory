import crypto from "node:crypto";
import {
  AUTHENTICATED_API_VARY,
  BffConfigurationError,
  CDN_NO_STORE_HEADER_VALUE,
  EPHEMERAL_GITHUB_PUBLIC_TOKEN_HEADER,
  loadBffConfig,
  ORIGIN_AUTH_HEADER,
  PRIVATE_NO_STORE_CACHE_CONTROL,
  SECURITY_RESPONSE_HEADERS,
  STATE_CHANGING_HTTP_METHODS,
  type BffConfig,
} from "@/server/bff/config";
import {
  decodeJwtPayloadUnverified,
  decryptPkceCookie,
  decryptSessionCookie,
  deriveSessionCsrfToken,
  encryptPkceCookie,
  encryptSessionCookie,
  parseCookieHeader,
  serializeClearCookie,
  serializeCookie,
  signOriginAuthHeader,
  timingSafeEqualStrings,
  verifySessionCsrfToken,
  type PkceEnvelope,
  type SessionEnvelope,
} from "@/server/bff/crypto";
import {
  buildSupabaseAuthorizeUrl,
  computeCodeChallengeS256,
  ConsumedPkceStateStore,
  generateCodeVerifier,
  generateOAuthState,
  validateReturnTo,
} from "@/server/bff/pkce";
import { SingleFlightRefreshCoordinator } from "@/server/bff/refresh-coordinator";

/**
 * Framework-agnostic server-side Request/Response handlers for the OD-010 / OD-013
 * Next.js BFF (`/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/session`,
 * and `/bff/v1/[...path]`).
 */

export interface BffDependencies {
  config?: BffConfig;
  env?: Record<string, string | undefined>;
  fetchImpl?: typeof fetch;
  nowSeconds?: () => number;
  refreshCoordinator?: SingleFlightRefreshCoordinator;
  consumedPkceStates?: ConsumedPkceStateStore;
}

const SAFE_FORWARDED_REQUEST_HEADERS = [
  "x-workspace-id",
  "x-request-id",
  "idempotency-key",
  "if-match",
  "content-type",
  "accept",
] as const;

const SAFE_FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "etag",
  "x-request-id",
  "retry-after",
] as const;

const UNSAFE_PROXY_SEGMENT_RE = /(%2e|%2f|%5c|\\|[\u0000-\u001f\u007f])/i;

interface GlobalBffSingletons {
  __cmRefreshCoordinator?: SingleFlightRefreshCoordinator;
  __cmConsumedPkceStates?: ConsumedPkceStateStore;
}

function getDefaultSingletons(): {
  refreshCoordinator: SingleFlightRefreshCoordinator;
  consumedPkceStates: ConsumedPkceStateStore;
} {
  const g = globalThis as unknown as GlobalBffSingletons;
  if (!g.__cmRefreshCoordinator) {
    g.__cmRefreshCoordinator = new SingleFlightRefreshCoordinator();
  }
  if (!g.__cmConsumedPkceStates) {
    g.__cmConsumedPkceStates = new ConsumedPkceStateStore();
  }
  return {
    refreshCoordinator: g.__cmRefreshCoordinator,
    consumedPkceStates: g.__cmConsumedPkceStates,
  };
}

function resolveDeps(deps?: BffDependencies): {
  config: BffConfig;
  fetchImpl: typeof fetch;
  nowSeconds: () => number;
  refreshCoordinator: SingleFlightRefreshCoordinator;
  consumedPkceStates: ConsumedPkceStateStore;
} {
  const defaults = getDefaultSingletons();
  const config = deps?.config ?? loadBffConfig(deps?.env ?? process.env);
  return {
    config,
    fetchImpl: deps?.fetchImpl ?? globalThis.fetch.bind(globalThis),
    nowSeconds: deps?.nowSeconds ?? (() => Math.floor(Date.now() / 1000)),
    refreshCoordinator: deps?.refreshCoordinator ?? defaults.refreshCoordinator,
    consumedPkceStates: deps?.consumedPkceStates ?? defaults.consumedPkceStates,
  };
}

function buildBasePrivateHeaders(extra?: Record<string, string>): Headers {
  const headers = new Headers();
  headers.set("Cache-Control", PRIVATE_NO_STORE_CACHE_CONTROL);
  headers.set("CDN-Cache-Control", CDN_NO_STORE_HEADER_VALUE);
  headers.set("Vary", AUTHENTICATED_API_VARY);
  for (const [k, v] of SECURITY_RESPONSE_HEADERS) {
    headers.set(k, v);
  }
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      headers.set(k, v);
    }
  }
  return headers;
}

function jsonPrivateResponse(
  payload: unknown,
  status: number,
  setCookies: string[] = [],
  extraHeaders?: Record<string, string>
): Response {
  const headers = buildBasePrivateHeaders({
    "Content-Type": "application/json; charset=utf-8",
    ...(extraHeaders ?? {}),
  });
  for (const cookie of setCookies) {
    headers.append("Set-Cookie", cookie);
  }
  return new Response(JSON.stringify(payload), { status, headers });
}

function errorResponse(
  status: number,
  code: string,
  message: string,
  retryable: boolean,
  setCookies: string[] = [],
  requestId?: string
): Response {
  const reqId = requestId || crypto.randomUUID();
  return jsonPrivateResponse(
    {
      error: {
        code,
        message,
        retryable,
        request_id: reqId,
      },
    },
    status,
    setCookies,
    { "X-Request-ID": reqId }
  );
}

function handleConfigError(err: unknown): Response {
  if (err instanceof BffConfigurationError) {
    return errorResponse(
      503,
      "BFF_NOT_CONFIGURED",
      "Hosted BFF authentication is not configured.",
      false
    );
  }
  return errorResponse(
    500,
    "BFF_INTERNAL_ERROR",
    "Unexpected BFF error.",
    false
  );
}

function normalizeRequestOrigin(rawOrigin: string | null): string | null {
  if (!rawOrigin) return null;
  const trimmed = rawOrigin.trim();
  if (!trimmed || trimmed === "null") return null;
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return null;
    }
    return `${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}`;
  } catch {
    return null;
  }
}

function verifyOriginAndFetchSite(
  request: Request,
  config: BffConfig
): boolean {
  const normalizedOrigin = normalizeRequestOrigin(request.headers.get("origin"));
  if (!normalizedOrigin || normalizedOrigin !== config.publicWebOrigin) {
    return false;
  }

  const secFetchSite = request.headers.get("sec-fetch-site");
  if (secFetchSite !== null && secFetchSite.trim() !== "") {
    const lower = secFetchSite.trim().toLowerCase();
    if (lower !== "same-origin" && lower !== "none") {
      return false;
    }
  }

  return true;
}

function readSessionFromRequest(
  request: Request,
  config: BffConfig
): { session: SessionEnvelope | null; hadCookie: boolean } {
  const cookies = parseCookieHeader(request.headers.get("cookie"));
  const raw = cookies.get(config.sessionCookieName);
  if (raw === undefined) {
    return { session: null, hadCookie: false };
  }
  const session = decryptSessionCookie(raw, config.cookieSecret);
  return { session, hadCookie: true };
}

function buildSetSessionCookieHeader(
  session: SessionEnvelope,
  config: BffConfig
): string {
  const encrypted = encryptSessionCookie(session, config.cookieSecret);
  return serializeCookie({
    name: config.sessionCookieName,
    value: encrypted,
    secure: config.cookieSecure,
    maxAgeSeconds: config.sessionCookieMaxAgeSeconds,
    sameSite: "Lax",
  });
}

function buildClearSessionCookieHeader(config: BffConfig): string {
  return serializeClearCookie({
    name: config.sessionCookieName,
    secure: config.cookieSecure,
  });
}

function buildClearPkceCookieHeader(config: BffConfig): string {
  return serializeClearCookie({
    name: config.pkceCookieName,
    secure: config.cookieSecure,
  });
}

function parseOptionalJsonObject(text: string): Record<string, unknown> | null {
  if (!text || !text.trim()) return null;
  try {
    const parsed = JSON.parse(text) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function extractErrorCode(parsedBody: Record<string, unknown> | null): string | null {
  if (!parsedBody) return null;
  const err = parsedBody.error;
  if (err && typeof err === "object" && !Array.isArray(err)) {
    const code = (err as Record<string, unknown>).code;
    return typeof code === "string" ? code : null;
  }
  return null;
}

function sanitizeBackendSessionPayload(
  raw: Record<string, unknown>
): Record<string, unknown> {
  const copy: Record<string, unknown> = { ...raw };
  delete copy.access_token;
  delete copy.refresh_token;
  delete copy.provider_token;
  delete copy.provider_refresh_token;
  return copy;
}

function validateProxyPathSegments(
  segments: string[] | undefined
): string | null {
  if (!Array.isArray(segments) || segments.length === 0) return null;
  for (const seg of segments) {
    if (
      typeof seg !== "string" ||
      seg.length === 0 ||
      seg === "." ||
      seg === ".." ||
      seg.includes("/") ||
      UNSAFE_PROXY_SEGMENT_RE.test(seg)
    ) {
      return null;
    }
    try {
      const decoded = decodeURIComponent(seg);
      if (
        decoded === "." ||
        decoded === ".." ||
        decoded.includes("/") ||
        decoded.includes("\\") ||
        decoded.includes("..")
      ) {
        return null;
      }
    } catch {
      return null;
    }
  }
  return `/v1/${segments.join("/")}`;
}

/**
 * `GET /auth/login` — Initiates RFC 7636 PKCE login via Supabase Auth (OD010-1).
 */
export async function handleAuthLogin(
  request: Request,
  deps?: BffDependencies
): Promise<Response> {
  let resolved: ReturnType<typeof resolveDeps>;
  try {
    resolved = resolveDeps(deps);
  } catch (err) {
    return handleConfigError(err);
  }
  const { config, nowSeconds } = resolved;

  const url = new URL(request.url);
  const nextCheck = validateReturnTo(url.searchParams.get("next"));
  if (!nextCheck.ok) {
    return errorResponse(
      400,
      "INVALID_RETURN_PATH",
      "The requested return path is not an allowed internal route.",
      false
    );
  }

  const codeVerifier = generateCodeVerifier();
  const codeChallenge = computeCodeChallengeS256(codeVerifier);
  const state = generateOAuthState();

  const pkceEnvelope: PkceEnvelope = {
    state,
    code_verifier: codeVerifier,
    return_to: nextCheck.path,
    created_at: nowSeconds(),
  };

  const encryptedPkce = encryptPkceCookie(pkceEnvelope, config.cookieSecret);
  const pkceCookie = serializeCookie({
    name: config.pkceCookieName,
    value: encryptedPkce,
    secure: config.cookieSecure,
    maxAgeSeconds: config.pkceTtlSeconds,
    sameSite: "Lax",
  });

  const callbackUrl = `${config.publicWebOrigin}/auth/callback`;
  const authorizeUrl = buildSupabaseAuthorizeUrl({
    supabaseUrl: config.supabaseUrl,
    provider: config.supabaseAuthProvider,
    callbackUrl,
    codeChallenge,
    state,
  });

  const headers = buildBasePrivateHeaders({
    Location: authorizeUrl,
  });
  headers.append("Set-Cookie", pkceCookie);
  return new Response(null, { status: 302, headers });
}

/**
 * `GET /auth/callback` — Completes PKCE exchange server-to-server and mints
 * encrypted `HttpOnly` session cookie (OD010-1 / OD010-2 / OD010-3).
 */
export async function handleAuthCallback(
  request: Request,
  deps?: BffDependencies
): Promise<Response> {
  let resolved: ReturnType<typeof resolveDeps>;
  try {
    resolved = resolveDeps(deps);
  } catch (err) {
    return handleConfigError(err);
  }
  const { config, fetchImpl, nowSeconds, consumedPkceStates } = resolved;
  const clearPkceCookie = buildClearPkceCookieHeader(config);

  const url = new URL(request.url);
  const code = (url.searchParams.get("code") || "").trim();
  const state = (url.searchParams.get("state") || "").trim();

  const cookies = parseCookieHeader(request.headers.get("cookie"));
  const rawPkce = cookies.get(config.pkceCookieName);
  const pkce = decryptPkceCookie(rawPkce, config.cookieSecret);
  const now = nowSeconds();

  const returnCheck = pkce ? validateReturnTo(pkce.return_to) : null;

  if (
    !code ||
    !state ||
    !pkce ||
    !returnCheck?.ok ||
    now - pkce.created_at > config.pkceTtlSeconds ||
    pkce.created_at > now + 60 ||
    !timingSafeEqualStrings(pkce.state, state) ||
    !consumedPkceStates.consumeOnce(pkce.state, now, config.pkceTtlSeconds)
  ) {
    return errorResponse(
      400,
      "PKCE_STATE_MISMATCH",
      "Invalid, expired, or already-consumed PKCE authorization state.",
      false,
      [clearPkceCookie]
    );
  }

  let tokenResp: Response;
  try {
    tokenResp = await fetchImpl(
      `${config.supabaseUrl}/auth/v1/token?grant_type=pkce`,
      {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          apikey: config.supabaseAnonKey,
        },
        body: JSON.stringify({
          auth_code: code,
          code_verifier: pkce.code_verifier,
        }),
      }
    );
  } catch {
    return errorResponse(
      503,
      "AUTH_VERIFICATION_UNAVAILABLE",
      "Authentication provider is temporarily unavailable.",
      true,
      [clearPkceCookie]
    );
  }

  if (tokenResp.status >= 500) {
    return errorResponse(
      503,
      "AUTH_VERIFICATION_UNAVAILABLE",
      "Authentication provider is temporarily unavailable.",
      true,
      [clearPkceCookie]
    );
  }

  const rawBody = await tokenResp.text();
  const payload = parseOptionalJsonObject(rawBody);

  if (!tokenResp.ok || !payload) {
    return errorResponse(
      401,
      "PKCE_EXCHANGE_FAILED",
      "PKCE authorization code exchange failed.",
      false,
      [clearPkceCookie]
    );
  }

  const accessToken =
    typeof payload.access_token === "string" ? payload.access_token.trim() : "";
  const refreshToken =
    typeof payload.refresh_token === "string"
      ? payload.refresh_token.trim()
      : "";

  if (!accessToken || !refreshToken) {
    return errorResponse(
      401,
      "PKCE_EXCHANGE_FAILED",
      "Authentication provider did not return valid session tokens.",
      false,
      [clearPkceCookie]
    );
  }

  const jwtClaims = decodeJwtPayloadUnverified(accessToken);
  const expiresAt =
    typeof payload.expires_at === "number" && Number.isFinite(payload.expires_at)
      ? Math.trunc(payload.expires_at)
      : typeof jwtClaims?.exp === "number" && Number.isFinite(jwtClaims.exp)
        ? Math.trunc(jwtClaims.exp)
        : now +
          (typeof payload.expires_in === "number" &&
          Number.isFinite(payload.expires_in)
            ? Math.trunc(payload.expires_in)
            : 3600);

  const userObj =
    payload.user &&
    typeof payload.user === "object" &&
    !Array.isArray(payload.user)
      ? (payload.user as Record<string, unknown>)
      : null;

  const userSub =
    (typeof userObj?.id === "string" && userObj.id.trim()) ||
    (typeof jwtClaims?.sub === "string" && jwtClaims.sub.trim()) ||
    "unknown-sub";

  const sessionId =
    (typeof jwtClaims?.session_id === "string" && jwtClaims.session_id.trim()) ||
    crypto.randomUUID();
  const providerToken =
    typeof payload.provider_token === "string" &&
    payload.provider_token.trim().length > 0 &&
    !/\s/.test(payload.provider_token.trim())
      ? payload.provider_token.trim()
      : undefined;

  const sessionEnvelope: SessionEnvelope = {
    access_token: accessToken,
    refresh_token: refreshToken,
    expires_at: expiresAt,
    user_sub: userSub,
    session_id: sessionId,
    issued_at: now,
    ...(providerToken ? { provider_token: providerToken } : {}),
  };

  const sessionCookie = buildSetSessionCookieHeader(sessionEnvelope, config);
  const headers = buildBasePrivateHeaders({
    Location: returnCheck.path,
  });
  headers.append("Set-Cookie", clearPkceCookie);
  headers.append("Set-Cookie", sessionCookie);

  return new Response(null, { status: 302, headers });
}

/**
 * `POST /auth/logout` — Enforces Origin + CSRF, revokes upstream GoTrue session,
 * and clears BFF session cookie (OD010-3 / OD010-5).
 */
export async function handleAuthLogout(
  request: Request,
  deps?: BffDependencies
): Promise<Response> {
  let resolved: ReturnType<typeof resolveDeps>;
  try {
    resolved = resolveDeps(deps);
  } catch (err) {
    return handleConfigError(err);
  }
  const { config, fetchImpl, refreshCoordinator } = resolved;

  if (!verifyOriginAndFetchSite(request, config)) {
    return errorResponse(
      403,
      "CSRF_VALIDATION_FAILED",
      "Cross-origin or missing Origin header rejected.",
      false
    );
  }

  const { session } = readSessionFromRequest(request, config);
  const providedCsrf = request.headers.get("x-csrf-token");

  if (!session) {
    if (!providedCsrf || !providedCsrf.trim()) {
      return errorResponse(
        403,
        "CSRF_VALIDATION_FAILED",
        "Missing CSRF token.",
        false
      );
    }
    return jsonPrivateResponse({ authenticated: false }, 200, [
      buildClearSessionCookieHeader(config),
    ]);
  }

  if (!verifySessionCsrfToken(config.cookieSecret, session, providedCsrf)) {
    return errorResponse(
      403,
      "CSRF_VALIDATION_FAILED",
      "Invalid or missing CSRF token.",
      false
    );
  }

  let scope: "local" | "global" = "local";
  try {
    const bodyText = await request.text();
    const parsed = parseOptionalJsonObject(bodyText);
    if (parsed?.scope === "global") {
      scope = "global";
    }
  } catch {
    // Keep default scope="local"
  }

  try {
    await fetchImpl(
      `${config.supabaseUrl}/auth/v1/logout?scope=${encodeURIComponent(scope)}`,
      {
        method: "POST",
        cache: "no-store",
        headers: {
          Authorization: `Bearer ${session.access_token}`,
          apikey: config.supabaseAnonKey,
          Accept: "application/json",
        },
      }
    );
  } catch {
    // Always clear local BFF cookie even if upstream logout network call fails
  }

  refreshCoordinator.clearSession(session.session_id);
  return jsonPrivateResponse({ authenticated: false }, 200, [
    buildClearSessionCookieHeader(config),
  ]);
}

/**
 * `GET /auth/session` — Returns non-secret session metadata + session-bound CSRF token
 * by calling Backend `GET /v1/session` server-to-server (OD010-2 / OD010-4 / OD010-5).
 */
export async function handleAuthSession(
  request: Request,
  deps?: BffDependencies
): Promise<Response> {
  let resolved: ReturnType<typeof resolveDeps>;
  try {
    resolved = resolveDeps(deps);
  } catch (err) {
    return handleConfigError(err);
  }
  const { config, fetchImpl, nowSeconds, refreshCoordinator } = resolved;

  const { session, hadCookie } = readSessionFromRequest(request, config);
  if (!session) {
    const cookiesToSet = hadCookie
      ? [buildClearSessionCookieHeader(config)]
      : [];
    return jsonPrivateResponse(
      {
        authenticated: false,
        csrf_token: null,
        session: null,
      },
      200,
      cookiesToSet
    );
  }

  const now = nowSeconds();
  const refreshOutcome = await refreshCoordinator.ensureFreshSession({
    session,
    config,
    fetchImpl,
    nowSeconds: now,
  });

  if (refreshOutcome.status === "expired") {
    refreshCoordinator.clearSession(session.session_id);
    return jsonPrivateResponse(
      {
        authenticated: false,
        reason: "SESSION_EXPIRED",
        csrf_token: null,
        session: null,
      },
      200,
      [buildClearSessionCookieHeader(config)]
    );
  }

  if (refreshOutcome.status === "unavailable") {
    // OD010-5: Preserve web session cookie on transient auth outage
    return errorResponse(
      503,
      "AUTH_VERIFICATION_UNAVAILABLE",
      "Authentication refresh service is temporarily unavailable.",
      true
    );
  }

  const activeSession = refreshOutcome.session;
  const setCookies = refreshOutcome.rotated
    ? [buildSetSessionCookieHeader(activeSession, config)]
    : [];

  const requestId =
    request.headers.get("x-request-id")?.trim() || crypto.randomUUID();
  const upstreamHeaders: Record<string, string> = {
    Authorization: `Bearer ${activeSession.access_token}`,
    Accept: "application/json",
    "X-Request-ID": requestId,
  };
  if (config.originHmacSecret) {
    upstreamHeaders[ORIGIN_AUTH_HEADER] = signOriginAuthHeader({
      secret: config.originHmacSecret,
      method: "GET",
      pathname: "/v1/session",
      timestampSeconds: now,
    });
  }

  let backendResp: Response;
  try {
    backendResp = await fetchImpl(`${config.apiOrigin}/v1/session`, {
      method: "GET",
      cache: "no-store",
      headers: upstreamHeaders,
    });
  } catch {
    // OD010-5: Preserve session cookie on backend connectivity outage
    return errorResponse(
      503,
      "AUTH_VERIFICATION_UNAVAILABLE",
      "Session verification backend is temporarily unavailable.",
      true,
      setCookies,
      requestId
    );
  }

  const rawBody = await backendResp.text();
  const parsedBody = parseOptionalJsonObject(rawBody);
  const backendErrorCode = extractErrorCode(parsedBody);

  if (backendResp.status === 503) {
    // OD010-5: NEVER clear web session cookie on 503 AUTH_VERIFICATION_UNAVAILABLE
    return errorResponse(
      503,
      backendErrorCode || "AUTH_VERIFICATION_UNAVAILABLE",
      "Authentication verification is temporarily unavailable.",
      true,
      setCookies,
      requestId
    );
  }

  if (backendResp.status === 401) {
    if (backendErrorCode === "SESSION_TERMINATED") {
      refreshCoordinator.clearSession(activeSession.session_id);
      return errorResponse(
        401,
        "SESSION_TERMINATED",
        "Your session has been terminated. Please sign in again.",
        false,
        [buildClearSessionCookieHeader(config)],
        requestId
      );
    }
    return errorResponse(
      401,
      backendErrorCode || "UNAUTHORIZED",
      "Authentication failed.",
      false,
      setCookies,
      requestId
    );
  }

  if (!backendResp.ok || !parsedBody) {
    return errorResponse(
      backendResp.status >= 500 ? 503 : backendResp.status,
      backendErrorCode || "SESSION_LOOKUP_FAILED",
      "Failed to load authenticated session metadata.",
      backendResp.status >= 500,
      setCookies,
      requestId
    );
  }

  const csrfToken = deriveSessionCsrfToken(config.cookieSecret, activeSession);
  return jsonPrivateResponse(
    {
      authenticated: true,
      csrf_token: csrfToken,
      session: sanitizeBackendSessionPayload(parsedBody),
    },
    200,
    setCookies,
    { "X-Request-ID": requestId }
  );
}

/**
 * `GET|POST|PATCH|PUT|DELETE /bff/v1/[...path]` — Same-Origin BFF proxy to Backend API
 * with Origin+CSRF validation, single-flight refresh, server-only Bearer injection,
 * `X-CM-Origin-Auth` HMAC signing, and OD010-5 401/503 cookie discipline.
 */
export async function handleBffProxy(
  request: Request,
  pathSegments: string[],
  deps?: BffDependencies
): Promise<Response> {
  let resolved: ReturnType<typeof resolveDeps>;
  try {
    resolved = resolveDeps(deps);
  } catch (err) {
    return handleConfigError(err);
  }
  const { config, fetchImpl, nowSeconds, refreshCoordinator } = resolved;

  const apiPathname = validateProxyPathSegments(pathSegments);
  if (!apiPathname) {
    return errorResponse(
      400,
      "INVALID_PROXY_PATH",
      "Proxy path contains unsafe or disallowed segments.",
      false
    );
  }

  const method = request.method.trim().toUpperCase();
  const isStateChanging = STATE_CHANGING_HTTP_METHODS.has(method);

  // 1. Cross-origin / missing Origin / Sec-Fetch-Site rejected fail-closed BEFORE backend
  if (isStateChanging && !verifyOriginAndFetchSite(request, config)) {
    return errorResponse(
      403,
      "CSRF_VALIDATION_FAILED",
      "Cross-origin or invalid Origin header rejected.",
      false
    );
  }

  // 2. Decrypt HttpOnly session cookie
  const { session, hadCookie } = readSessionFromRequest(request, config);
  if (!session) {
    return errorResponse(
      401,
      "SESSION_TERMINATED",
      "Authentication session is missing or invalid.",
      false,
      hadCookie ? [buildClearSessionCookieHeader(config)] : []
    );
  }

  // 3. Constant-time session-bound CSRF verification on mutations BEFORE backend
  if (isStateChanging) {
    const providedCsrf = request.headers.get("x-csrf-token");
    if (!verifySessionCsrfToken(config.cookieSecret, session, providedCsrf)) {
      return errorResponse(
        403,
        "CSRF_VALIDATION_FAILED",
        "Missing or invalid X-CSRF-Token header.",
        false
      );
    }
  }

  // 4. Single-flight access token refresh (OD010-4)
  const now = nowSeconds();
  const refreshOutcome = await refreshCoordinator.ensureFreshSession({
    session,
    config,
    fetchImpl,
    nowSeconds: now,
  });

  if (refreshOutcome.status === "expired") {
    refreshCoordinator.clearSession(session.session_id);
    return errorResponse(
      401,
      "SESSION_TERMINATED",
      "Session expired. Please sign in again.",
      false,
      [buildClearSessionCookieHeader(config)]
    );
  }

  if (refreshOutcome.status === "unavailable") {
    // OD010-5: Preserve cookie and NEVER auto-retry POST
    return errorResponse(
      503,
      "AUTH_VERIFICATION_UNAVAILABLE",
      "Authentication verification is temporarily unavailable.",
      true
    );
  }

  const activeSession = refreshOutcome.session;
  const setCookies = refreshOutcome.rotated
    ? [buildSetSessionCookieHeader(activeSession, config)]
    : [];

  // 5. Build server-to-API request (stripping Cookie, browser Authorization, and edge headers)
  const incomingUrl = new URL(request.url);
  const upstreamUrl = `${config.apiOrigin}${apiPathname}${incomingUrl.search}`;

  const upstreamHeaders = new Headers();
  for (const name of SAFE_FORWARDED_REQUEST_HEADERS) {
    const val = request.headers.get(name);
    if (val !== null && val.trim() !== "") {
      upstreamHeaders.set(name, val);
    }
  }
  upstreamHeaders.set("Authorization", `Bearer ${activeSession.access_token}`);
  if (
    activeSession.provider_token &&
    activeSession.provider_token.trim().length > 0 &&
    !/\s/.test(activeSession.provider_token.trim())
  ) {
    upstreamHeaders.set(
      EPHEMERAL_GITHUB_PUBLIC_TOKEN_HEADER,
      activeSession.provider_token.trim()
    );
  }
  if (config.originHmacSecret) {
    upstreamHeaders.set(
      ORIGIN_AUTH_HEADER,
      signOriginAuthHeader({
        secret: config.originHmacSecret,
        method,
        pathname: apiPathname, // STRICTLY without query string (OD-013 / T-507)
        timestampSeconds: now,
      })
    );
  }

  let bodyBytes: ArrayBuffer | undefined;
  if (isStateChanging) {
    const buf = await request.arrayBuffer();
    if (buf.byteLength > 0) {
      bodyBytes = buf;
    }
  }

  // 6. Single upstream call — NEVER auto-retry POST/mutations on 503 or network error
  let upstreamResp: Response;
  try {
    upstreamResp = await fetchImpl(upstreamUrl, {
      method,
      cache: "no-store",
      headers: upstreamHeaders,
      body: bodyBytes,
    });
  } catch {
    return errorResponse(
      503,
      "AUTH_VERIFICATION_UNAVAILABLE",
      "Backend API is temporarily unreachable.",
      true,
      setCookies
    );
  }

  const respBytes = await upstreamResp.arrayBuffer();
  const respHeaders = buildBasePrivateHeaders();
  for (const name of SAFE_FORWARDED_RESPONSE_HEADERS) {
    const val = upstreamResp.headers.get(name);
    if (val !== null) {
      respHeaders.set(name, val);
    }
  }

  // 7. OD010-5 response cookie discipline
  if (upstreamResp.status === 401) {
    const text = new TextDecoder().decode(respBytes);
    const parsed = parseOptionalJsonObject(text);
    if (extractErrorCode(parsed) === "SESSION_TERMINATED") {
      refreshCoordinator.clearSession(activeSession.session_id);
      respHeaders.append("Set-Cookie", buildClearSessionCookieHeader(config));
    } else {
      for (const cookie of setCookies) {
        respHeaders.append("Set-Cookie", cookie);
      }
    }
  } else {
    // Includes 503 AUTH_VERIFICATION_UNAVAILABLE: session cookie is preserved (never cleared!)
    for (const cookie of setCookies) {
      respHeaders.append("Set-Cookie", cookie);
    }
  }

  return new Response(respBytes, {
    status: upstreamResp.status,
    headers: respHeaders,
  });
}
