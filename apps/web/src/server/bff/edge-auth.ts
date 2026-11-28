/**
 * Web Crypto (`crypto.subtle`) Cloudflare -> Cloud Run `cm-web` Edge Origin Proof (`INF-07`).
 *
 * Uses domain-separated subkey derivation (`HMAC-SHA256(originHmacSecret, "cm-cloudflare-edge-v1")`)
 * so `cm-web` verifies Cloudflare edge authenticity without adding a 5th GCP Secret Manager secret
 * (`OD-015` <= 4 active versions cap) and without exposing the backend `cm-api` HMAC key to the edge.
 */

export const EDGE_AUTH_HEADER = "X-CM-Edge-Auth";
export const EDGE_SUBKEY_CONTEXT = "cm-cloudflare-edge-v1";
export const DEFAULT_EDGE_AUTH_MAX_SKEW_SECONDS = 300;

const EDGE_HEADER_RE = /^t=(\d+),sig=([0-9a-fA-F]{64})$/;
const encoder = new TextEncoder();

let cachedMasterSecret: string | null = null;
let cachedDerivedSubkeyPromise: Promise<string> | null = null;

function bytesToHex(buf: ArrayBuffer): string {
  const arr = new Uint8Array(buf);
  let hex = "";
  for (let i = 0; i < arr.length; i += 1) {
    hex += arr[i]!.toString(16).padStart(2, "0");
  }
  return hex;
}

function constantTimeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

async function hmacSha256Hex(keyText: string, message: string): Promise<string> {
  const cryptoKey = await globalThis.crypto.subtle.importKey(
    "raw",
    encoder.encode(keyText),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await globalThis.crypto.subtle.sign(
    "HMAC",
    cryptoKey,
    encoder.encode(message)
  );
  return bytesToHex(sig);
}

export async function deriveEdgeAuthSubkey(masterOriginHmacSecret: string): Promise<string> {
  const trimmed = masterOriginHmacSecret.trim();
  if (!trimmed) {
    throw new Error("Master origin HMAC secret must be non-empty.");
  }
  if (trimmed === cachedMasterSecret && cachedDerivedSubkeyPromise) {
    return cachedDerivedSubkeyPromise;
  }
  cachedMasterSecret = trimmed;
  cachedDerivedSubkeyPromise = hmacSha256Hex(trimmed, EDGE_SUBKEY_CONTEXT);
  return cachedDerivedSubkeyPromise;
}

export async function signEdgeAuthHeader(
  edgeSubkey: string,
  method: string,
  pathname: string,
  timestampSeconds: number
): Promise<string> {
  const normalizedPath = pathname.startsWith("/") ? pathname : `/${pathname}`;
  const sig = await hmacSha256Hex(
    edgeSubkey.trim(),
    `${timestampSeconds}.${method.toUpperCase()}.${normalizedPath}`
  );
  return `t=${timestampSeconds},sig=${sig}`;
}

export async function verifyEdgeAuthHeader(params: {
  headerValue: string | null;
  masterOriginHmacSecret: string;
  method: string;
  pathname: string;
  nowSeconds?: number;
  maxSkewSeconds?: number;
}): Promise<boolean> {
  const {
    headerValue,
    masterOriginHmacSecret,
    method,
    pathname,
    nowSeconds = Math.floor(Date.now() / 1000),
    maxSkewSeconds = DEFAULT_EDGE_AUTH_MAX_SKEW_SECONDS,
  } = params;

  if (!headerValue || !masterOriginHmacSecret.trim()) {
    return false;
  }
  const match = EDGE_HEADER_RE.exec(headerValue.trim());
  if (!match) {
    return false;
  }
  const ts = Number(match[1]);
  const providedSig = match[2]!.toLowerCase();
  if (!Number.isSafeInteger(ts) || Math.abs(nowSeconds - ts) > maxSkewSeconds) {
    return false;
  }

  const subkey = await deriveEdgeAuthSubkey(masterOriginHmacSecret);
  const normalizedPath = pathname.startsWith("/") ? pathname : `/${pathname}`;
  const expectedSig = await hmacSha256Hex(
    subkey,
    `${ts}.${method.toUpperCase()}.${normalizedPath}`
  );
  return constantTimeEqualHex(providedSig, expectedSig);
}
