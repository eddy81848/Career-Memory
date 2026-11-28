import type { BffConfig } from "@/server/bff/config";
import {
  decodeJwtPayloadUnverified,
  sha256Hex,
  type SessionEnvelope,
} from "@/server/bff/crypto";

/**
 * Single-flight Supabase refresh-token coordinator keyed by `session_id` (OD010-4).
 *
 * Guarantees that 2+ concurrent browser tabs arriving with an expired or soon-expiring
 * access token trigger AT MOST ONE upstream `POST /auth/v1/token?grant_type=refresh_token`
 * request, preventing refresh-token rotation reuse detection from falsely logging the
 * user out.
 */

export type RefreshResult =
  | {
      readonly status: "ok";
      readonly session: SessionEnvelope;
      readonly rotated: boolean;
    }
  | {
      readonly status: "expired";
      readonly httpStatus: number;
    }
  | {
      readonly status: "unavailable";
      readonly httpStatus: number;
    };

interface RecentRotationEntry {
  readonly sessionId: string;
  readonly session: SessionEnvelope;
  readonly expiresAtMs: number;
}

const RECENT_ROTATION_CACHE_TTL_MS = 10_000;

export function needsTokenRefresh(
  session: Pick<SessionEnvelope, "expires_at">,
  nowSeconds: number,
  refreshSkewSeconds: number
): boolean {
  return session.expires_at <= nowSeconds + refreshSkewSeconds;
}

export class SingleFlightRefreshCoordinator {
  private readonly inFlightBySessionId = new Map<
    string,
    Promise<RefreshResult>
  >();
  private readonly recentRotationsByTokenKey = new Map<
    string,
    RecentRotationEntry
  >();

  public async ensureFreshSession(params: {
    session: SessionEnvelope;
    config: BffConfig;
    fetchImpl: typeof fetch;
    nowSeconds: number;
  }): Promise<RefreshResult> {
    const { session, config, fetchImpl, nowSeconds } = params;

    if (!needsTokenRefresh(session, nowSeconds, config.refreshSkewSeconds)) {
      return { status: "ok", session, rotated: false };
    }

    const nowMs = nowSeconds * 1000;
    this.pruneRecent(nowMs);

    const tokenKey = sha256Hex(`${session.session_id}:${session.refresh_token}`);
    const recent = this.recentRotationsByTokenKey.get(tokenKey);
    if (recent && recent.expiresAtMs > nowMs) {
      return { status: "ok", session: recent.session, rotated: true };
    }

    const existingFlight = this.inFlightBySessionId.get(session.session_id);
    if (existingFlight) {
      return existingFlight;
    }

    const flight = this.executeUpstreamRefresh({
      session,
      config,
      fetchImpl,
      nowSeconds,
      tokenKey,
      nowMs,
    }).finally(() => {
      if (this.inFlightBySessionId.get(session.session_id) === flight) {
        this.inFlightBySessionId.delete(session.session_id);
      }
    });

    this.inFlightBySessionId.set(session.session_id, flight);
    return flight;
  }

  private async executeUpstreamRefresh(params: {
    session: SessionEnvelope;
    config: BffConfig;
    fetchImpl: typeof fetch;
    nowSeconds: number;
    tokenKey: string;
    nowMs: number;
  }): Promise<RefreshResult> {
    const { session, config, fetchImpl, nowSeconds, tokenKey, nowMs } = params;
    const refreshUrl = `${config.supabaseUrl}/auth/v1/token?grant_type=refresh_token`;

    let response: Response;
    try {
      response = await fetchImpl(refreshUrl, {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          apikey: config.supabaseAnonKey,
        },
        body: JSON.stringify({
          refresh_token: session.refresh_token,
        }),
      });
    } catch {
      return { status: "unavailable", httpStatus: 503 };
    }

    if (response.status >= 500) {
      return { status: "unavailable", httpStatus: 503 };
    }

    if (
      response.status === 400 ||
      response.status === 401 ||
      response.status === 403
    ) {
      return { status: "expired", httpStatus: response.status };
    }

    if (!response.ok) {
      return { status: "unavailable", httpStatus: 503 };
    }

    let payload: Record<string, unknown>;
    try {
      const parsed = (await response.json()) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { status: "unavailable", httpStatus: 503 };
      }
      payload = parsed as Record<string, unknown>;
    } catch {
      return { status: "unavailable", httpStatus: 503 };
    }

    const accessToken =
      typeof payload.access_token === "string" ? payload.access_token.trim() : "";
    const refreshToken =
      typeof payload.refresh_token === "string"
        ? payload.refresh_token.trim()
        : "";
    if (!accessToken || !refreshToken) {
      return { status: "expired", httpStatus: 401 };
    }

    const jwtClaims = decodeJwtPayloadUnverified(accessToken);
    const rawExpiresAt =
      typeof payload.expires_at === "number" && Number.isFinite(payload.expires_at)
        ? Math.trunc(payload.expires_at)
        : typeof jwtClaims?.exp === "number" && Number.isFinite(jwtClaims.exp)
          ? Math.trunc(jwtClaims.exp)
          : nowSeconds +
            (typeof payload.expires_in === "number" &&
            Number.isFinite(payload.expires_in)
              ? Math.trunc(payload.expires_in)
              : 3600);

    const userObj =
      payload.user && typeof payload.user === "object" && !Array.isArray(payload.user)
        ? (payload.user as Record<string, unknown>)
        : null;
    const userSub =
      (typeof userObj?.id === "string" && userObj.id.trim()) ||
      (typeof jwtClaims?.sub === "string" && jwtClaims.sub.trim()) ||
      session.user_sub;

    // Preserve session_id and issued_at across silent refresh so session-bound
    // CSRF tokens remain valid across open tabs without rotation-induced 403s.
    const nextSession: SessionEnvelope = {
      access_token: accessToken,
      refresh_token: refreshToken,
      expires_at: rawExpiresAt,
      user_sub: userSub,
      session_id: session.session_id,
      issued_at: session.issued_at,
    };

    this.recentRotationsByTokenKey.set(tokenKey, {
      sessionId: session.session_id,
      session: nextSession,
      expiresAtMs: nowMs + RECENT_ROTATION_CACHE_TTL_MS,
    });

    return {
      status: "ok",
      session: nextSession,
      rotated: true,
    };
  }

  public clearSession(sessionId: string): void {
    this.inFlightBySessionId.delete(sessionId);
    for (const [key, entry] of this.recentRotationsByTokenKey.entries()) {
      if (entry.sessionId === sessionId) {
        this.recentRotationsByTokenKey.delete(key);
      }
    }
  }

  public clearAll(): void {
    this.inFlightBySessionId.clear();
    this.recentRotationsByTokenKey.clear();
  }

  private pruneRecent(nowMs: number): void {
    for (const [key, entry] of this.recentRotationsByTokenKey.entries()) {
      if (entry.expiresAtMs <= nowMs) {
        this.recentRotationsByTokenKey.delete(key);
      }
    }
  }
}
