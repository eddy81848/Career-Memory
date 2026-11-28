import crypto from "node:crypto";
import { MIN_COOKIE_SECRET_LENGTH } from "@/server/bff/config";

/**
 * Cryptographic primitives for OD-010 encrypted HttpOnly cookies, session-bound
 * CSRF tokens, and OD-013 / T-507 `X-CM-Origin-Auth` HMAC signing.
 *
 * Uses Node's built-in `node:crypto` exclusively (zero extra runtime dependencies).
 */

const HKDF_SALT = Buffer.from("career-memory-bff-v1", "utf8");
const AES_KEY_INFO = Buffer.from("cookie-aes-256-gcm-key", "utf8");
const CSRF_KEY_INFO = Buffer.from("session-csrf-hmac-key", "utf8");
const ENVELOPE_VERSION = "v1";
const AES_GCM_IV_BYTES = 12;
const AES_GCM_TAG_BYTES = 16;

export const SESSION_COOKIE_AAD = "cm_session_v1";
export const PKCE_COOKIE_AAD = "cm_pkce_v1";

export interface SessionEnvelope {
  access_token: string;
  refresh_token: string;
  expires_at: number;
  user_sub: string;
  session_id: string;
  issued_at: number;
  provider_token?: string;
}

export interface PkceEnvelope {
  state: string;
  code_verifier: string;
  return_to: string;
  created_at: number;
}

function assertValidSecret(secret: string): void {
  if (!secret || secret.length < MIN_COOKIE_SECRET_LENGTH) {
    throw new Error(
      `BFF cookie secret must be at least ${MIN_COOKIE_SECRET_LENGTH} characters.`
    );
  }
}

function deriveKey(secret: string, info: Buffer): Buffer {
  assertValidSecret(secret);
  const derived = crypto.hkdfSync(
    "sha256",
    Buffer.from(secret, "utf8"),
    HKDF_SALT,
    info,
    32
  );
  return Buffer.from(derived);
}

export function encryptEnvelope<T extends object>(
  payload: T,
  secret: string,
  aad: string
): string {
  const key = deriveKey(secret, AES_KEY_INFO);
  const iv = crypto.randomBytes(AES_GCM_IV_BYTES);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(aad, "utf8"));
  const plaintext = Buffer.from(JSON.stringify(payload), "utf8");
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const authTag = cipher.getAuthTag();
  return [
    ENVELOPE_VERSION,
    iv.toString("base64url"),
    authTag.toString("base64url"),
    ciphertext.toString("base64url"),
  ].join(".");
}

export function decryptEnvelope(
  token: string | null | undefined,
  secret: string,
  aad: string
): unknown | null {
  if (!token || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 4 || parts[0] !== ENVELOPE_VERSION) return null;

  try {
    const iv = Buffer.from(parts[1], "base64url");
    const authTag = Buffer.from(parts[2], "base64url");
    const ciphertext = Buffer.from(parts[3], "base64url");
    if (iv.length !== AES_GCM_IV_BYTES || authTag.length !== AES_GCM_TAG_BYTES) {
      return null;
    }
    const key = deriveKey(secret, AES_KEY_INFO);
    const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
    decipher.setAAD(Buffer.from(aad, "utf8"));
    decipher.setAuthTag(authTag);
    const plaintext = Buffer.concat([
      decipher.update(ciphertext),
      decipher.final(),
    ]);
    return JSON.parse(plaintext.toString("utf8")) as unknown;
  } catch {
    return null;
  }
}

export function isValidSessionEnvelope(value: unknown): value is SessionEnvelope {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const obj = value as Record<string, unknown>;
  return (
    typeof obj.access_token === "string" &&
    obj.access_token.length > 0 &&
    typeof obj.refresh_token === "string" &&
    obj.refresh_token.length > 0 &&
    typeof obj.expires_at === "number" &&
    Number.isFinite(obj.expires_at) &&
    typeof obj.user_sub === "string" &&
    obj.user_sub.length > 0 &&
    typeof obj.session_id === "string" &&
    obj.session_id.length > 0 &&
    typeof obj.issued_at === "number" &&
    Number.isFinite(obj.issued_at) &&
    (obj.provider_token === undefined || typeof obj.provider_token === "string")
  );
}

export function isValidPkceEnvelope(value: unknown): value is PkceEnvelope {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const obj = value as Record<string, unknown>;
  return (
    typeof obj.state === "string" &&
    obj.state.length > 0 &&
    typeof obj.code_verifier === "string" &&
    obj.code_verifier.length > 0 &&
    typeof obj.return_to === "string" &&
    obj.return_to.length > 0 &&
    typeof obj.created_at === "number" &&
    Number.isFinite(obj.created_at)
  );
}

export function encryptSessionCookie(
  session: SessionEnvelope,
  secret: string
): string {
  return encryptEnvelope(session, secret, SESSION_COOKIE_AAD);
}

export function decryptSessionCookie(
  rawCookieValue: string | null | undefined,
  secret: string
): SessionEnvelope | null {
  const parsed = decryptEnvelope(rawCookieValue, secret, SESSION_COOKIE_AAD);
  return isValidSessionEnvelope(parsed) ? parsed : null;
}

export function encryptPkceCookie(
  pkce: PkceEnvelope,
  secret: string
): string {
  return encryptEnvelope(pkce, secret, PKCE_COOKIE_AAD);
}

export function decryptPkceCookie(
  rawCookieValue: string | null | undefined,
  secret: string
): PkceEnvelope | null {
  const parsed = decryptEnvelope(rawCookieValue, secret, PKCE_COOKIE_AAD);
  return isValidPkceEnvelope(parsed) ? parsed : null;
}

export function timingSafeEqualStrings(a: string, b: string): boolean {
  const bufA = Buffer.from(a, "utf8");
  const bufB = Buffer.from(b, "utf8");
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

export function deriveSessionCsrfToken(
  cookieSecret: string,
  session: Pick<SessionEnvelope, "session_id" | "issued_at">
): string {
  const csrfKey = deriveKey(cookieSecret, CSRF_KEY_INFO);
  const message = `${session.session_id}:${Math.trunc(session.issued_at)}`;
  return crypto
    .createHmac("sha256", csrfKey)
    .update(Buffer.from(message, "utf8"))
    .digest("base64url");
}

export function verifySessionCsrfToken(
  cookieSecret: string,
  session: Pick<SessionEnvelope, "session_id" | "issued_at">,
  providedToken: string | null | undefined
): boolean {
  if (!providedToken || typeof providedToken !== "string") return false;
  const expected = deriveSessionCsrfToken(cookieSecret, session);
  return timingSafeEqualStrings(expected, providedToken.trim());
}

/**
 * Create a cryptographic HMAC-SHA256 `X-CM-Origin-Auth` header value (`t=<ts>,sig=<64-hex>`).
 *
 * Matches `apps/api/src/career_memory_api/edge_guard/models.py::sign_origin_auth_header`
 * byte-for-byte:
 *   canonical = `${int(timestamp)}:${method.trim().upper()}:${pathname}` (UTF-8, NO query string)
 */
export function signOriginAuthHeader(params: {
  secret: string;
  method: string;
  pathname: string;
  timestampSeconds: number;
}): string {
  if (!params.secret) {
    throw new Error("Origin auth secret must be non-empty.");
  }
  const ts = Math.trunc(params.timestampSeconds);
  const normalizedMethod = params.method.trim().toUpperCase();
  const cleanPathname = params.pathname.split("?")[0] || "/";
  const canonical = `${ts}:${normalizedMethod}:${cleanPathname}`;
  const digest = crypto
    .createHmac("sha256", Buffer.from(params.secret, "utf8"))
    .update(Buffer.from(canonical, "utf8"))
    .digest("hex");
  return `t=${ts},sig=${digest}`;
}

export function serializeCookie(params: {
  name: string;
  value: string;
  secure: boolean;
  maxAgeSeconds?: number;
  sameSite?: "Lax" | "Strict";
}): string {
  const parts = [
    `${params.name}=${params.value}`,
    "Path=/",
    "HttpOnly",
    `SameSite=${params.sameSite ?? "Lax"}`,
  ];
  if (params.secure) {
    parts.push("Secure");
  }
  if (params.maxAgeSeconds !== undefined) {
    parts.push(`Max-Age=${Math.max(0, Math.trunc(params.maxAgeSeconds))}`);
  }
  return parts.join("; ");
}

export function serializeClearCookie(params: {
  name: string;
  secure: boolean;
}): string {
  const parts = [
    `${params.name}=`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
  ];
  if (params.secure) {
    parts.push("Secure");
  }
  parts.push("Max-Age=0");
  parts.push("Expires=Thu, 01 Jan 1970 00:00:00 GMT");
  return parts.join("; ");
}

export function parseCookieHeader(
  cookieHeader: string | null | undefined
): Map<string, string> {
  const map = new Map<string, string>();
  if (!cookieHeader) return map;
  for (const segment of cookieHeader.split(";")) {
    const trimmed = segment.trim();
    if (!trimmed) continue;
    const eqIndex = trimmed.indexOf("=");
    if (eqIndex <= 0) continue;
    const key = trimmed.slice(0, eqIndex).trim();
    const val = trimmed.slice(eqIndex + 1).trim();
    if (key) {
      map.set(key, val);
    }
  }
  return map;
}

export function sha256Hex(value: string): string {
  return crypto
    .createHash("sha256")
    .update(Buffer.from(value, "utf8"))
    .digest("hex");
}

/**
 * Best-effort unverified decode of JWT payload solely for extracting non-authoritative
 * metadata (`sub`, `session_id`, `exp`) inside the BFF envelope. All authorization
 * decisions happen on the Backend API via JWKS + GoTrue verification.
 */
export function decodeJwtPayloadUnverified(
  jwt: string
): Record<string, unknown> | null {
  const parts = jwt.split(".");
  if (parts.length !== 3) return null;
  try {
    const raw = Buffer.from(parts[1], "base64url").toString("utf8");
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}
