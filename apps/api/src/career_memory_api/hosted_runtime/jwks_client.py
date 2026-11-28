"""Pinned HTTPS JWKS provider, GoTrue live-session verifier, and zero-DB-hold access provider.

Security invariants (OD-006 / OD-010 / T-007D):
- Fixed config-pinned endpoints only; rejects HTTP (except explicit loopback test flag),
  private/link-local/metadata SSRF IPs, and HTTP redirects.
- Never serves stale/expired JWKS snapshots on network/HTTP/JSON outage (fail-closed 503).
- ``HostedPostgresWorkspaceAccessProvider.verify_identity()`` executes JWKS signature validation
  and GoTrue ``GET /auth/v1/user`` live session verification **BEFORE** checking out any pooled
  PostgreSQL connection (``connections_in_use == 0``, ``open_tx == 0`` during remote auth I/O).
"""

from __future__ import annotations

import ipaddress
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from career_memory_api.auth.jwt_verifier import (
    JwkKey,
    JwksSnapshot,
    JwksUnavailableError,
    JwtAccessTokenVerifier,
    LiveSessionStatus,
    LiveSessionVerifierPort,
    VerifiedJwtClaims,
)
from career_memory_api.auth.postgres_guard import (
    _normalize_uuid,
    bind_postgres_identity_lookup_context,
)
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceMembership,
)
from career_memory_api.hosted_runtime.models import HostedConfigurationError
from career_memory_api.hosted_runtime.pooler import HostedTransactionPool

_MAX_AUTH_RESPONSE_BYTES = 256 * 1024  # 256 KiB
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_FORBIDDEN_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
    }
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail-closed redirect handler blocking 3xx redirects on auth/JWKS endpoints."""

    def redirect_request(  # type: ignore[override]
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "HTTP redirects are forbidden for pinned auth endpoints.",
            headers,
            fp,
        )


def validate_pinned_auth_url(
    url: str,
    *,
    allow_http_loopback_for_tests: bool = False,
) -> str:
    """Validate a config-pinned HTTPS endpoint URL and block SSRF/private/metadata targets."""
    cleaned = url.strip()
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").strip().lower()

    if not host:
        raise HostedConfigurationError("Pinned auth URL must include a hostname.")
    if parts.username or parts.password:
        raise HostedConfigurationError("Pinned auth URL must not embed credentials.")
    if host in _FORBIDDEN_METADATA_HOSTS:
        raise HostedConfigurationError(f"Pinned auth URL host {host!r} is forbidden.")

    is_loopback = host in _LOOPBACK_HOSTS
    if scheme == "http":
        if not (allow_http_loopback_for_tests and is_loopback):
            raise HostedConfigurationError(
                f"Pinned auth URL must use https:// (got {scheme!r} for host {host!r})."
            )
    elif scheme != "https":
        raise HostedConfigurationError(f"Unsupported pinned auth URL scheme: {scheme!r}.")

    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if ip_obj.is_loopback:
            if not allow_http_loopback_for_tests:
                raise HostedConfigurationError("Loopback IP is forbidden for hosted auth URLs.")
        elif (
            ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            raise HostedConfigurationError(
                f"Private/link-local/non-public IP {host!r} is forbidden for pinned auth URLs."
            )

    return cleaned


def _default_no_redirect_http_get(
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> tuple[int, bytes]:
    """Perform a bounded GET request with redirects strictly disabled."""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            status = int(resp.status)
            body = resp.read(_MAX_AUTH_RESPONSE_BYTES + 1)
            if len(body) > _MAX_AUTH_RESPONSE_BYTES:
                raise OSError("Pinned auth endpoint response exceeded maximum byte cap.")
            return status, body
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            if exc.fp is not None:
                body = exc.fp.read(8192)
        except Exception:  # noqa: BLE001
            body = b""
        return int(exc.code), body


HttpGetFn = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class HttpsJwksProvider:
    """Config-pinned HTTPS JWKS provider implementing ``JwksProviderPort``."""

    def __init__(
        self,
        *,
        jwks_url: str,
        cache_ttl_seconds: float = 300.0,
        timeout_seconds: float = 3.0,
        allow_http_loopback_for_tests: bool = False,
        http_get: HttpGetFn | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._jwks_url = validate_pinned_auth_url(
            jwks_url,
            allow_http_loopback_for_tests=allow_http_loopback_for_tests,
        )
        if cache_ttl_seconds <= 0:
            raise HostedConfigurationError("cache_ttl_seconds must be > 0.")
        if timeout_seconds <= 0:
            raise HostedConfigurationError("timeout_seconds must be > 0.")
        self._cache_ttl_seconds = float(cache_ttl_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._http_get: HttpGetFn = http_get or _default_no_redirect_http_get
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._cached: JwksSnapshot | None = None

    @property
    def jwks_url(self) -> str:
        return self._jwks_url

    def get_jwks(self, *, force_refresh: bool = False) -> JwksSnapshot:
        now = self._clock()
        with self._lock:
            if (
                not force_refresh
                and self._cached is not None
                and not self._cached.is_expired(now)
            ):
                return self._cached

        try:
            status, body = self._http_get(
                self._jwks_url,
                {"Accept": "application/json"},
                self._timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise JwksUnavailableError("JWKS endpoint request failed.") from exc

        if status != 200:
            raise JwksUnavailableError(f"JWKS endpoint returned HTTP {status}.")

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise JwksUnavailableError("JWKS endpoint returned invalid JSON.") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise JwksUnavailableError("JWKS payload missing 'keys' list.")

        parsed_keys: list[JwkKey] = []
        for entry in payload["keys"]:
            if not isinstance(entry, dict):
                continue
            try:
                parsed_keys.append(JwkKey.from_dict(entry))
            except ValueError:
                continue

        if not parsed_keys:
            raise JwksUnavailableError("JWKS payload contained zero valid signature keys.")

        fetched_at = self._clock()
        snapshot = JwksSnapshot(
            keys=tuple(parsed_keys),
            fetched_at=fetched_at,
            expires_at=fetched_at + timedelta(seconds=self._cache_ttl_seconds),
        )
        with self._lock:
            self._cached = snapshot
        return snapshot


class _BoundLiveSessionVerifier:
    """Per-request stateless wrapper binding a raw bearer token to GoTrue verification."""

    def __init__(
        self,
        verifier: SupabaseGoTrueLiveSessionVerifier,
        raw_token: str,
    ) -> None:
        self._verifier = verifier
        self._raw_token = raw_token

    def verify_live_session(self, claims: VerifiedJwtClaims) -> LiveSessionStatus:
        return self._verifier.verify_raw_token(self._raw_token, claims)


class SupabaseGoTrueLiveSessionVerifier:
    """Calls pinned ``GET ${supabase_auth_url}/user`` to detect revoked/logged-out sessions."""

    def __init__(
        self,
        *,
        supabase_auth_url: str,
        supabase_anon_key: str,
        timeout_seconds: float = 3.0,
        allow_http_loopback_for_tests: bool = False,
        http_get: HttpGetFn | None = None,
    ) -> None:
        base = validate_pinned_auth_url(
            supabase_auth_url.rstrip("/"),
            allow_http_loopback_for_tests=allow_http_loopback_for_tests,
        )
        if not isinstance(supabase_anon_key, str) or not supabase_anon_key.strip():
            raise HostedConfigurationError("supabase_anon_key must be non-empty.")
        if timeout_seconds <= 0:
            raise HostedConfigurationError("timeout_seconds must be > 0.")
        self._user_url = f"{base}/user"
        self._anon_key = supabase_anon_key.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._http_get: HttpGetFn = http_get or _default_no_redirect_http_get

    @property
    def user_url(self) -> str:
        return self._user_url

    def for_token(self, raw_token: str) -> LiveSessionVerifierPort:
        """Bind ``raw_token`` into a thread-safe per-request ``LiveSessionVerifierPort``."""
        return _BoundLiveSessionVerifier(self, raw_token)

    def verify_raw_token(
        self,
        raw_token: str,
        claims: VerifiedJwtClaims,
    ) -> LiveSessionStatus:
        """Verify active session with GoTrue ``GET /user``; fail-closed 503 on outage."""
        if not raw_token or not raw_token.strip():
            return LiveSessionStatus.SESSION_TERMINATED

        headers = {
            "Authorization": f"Bearer {raw_token.strip()}",
            "apikey": self._anon_key,
            "Accept": "application/json",
        }
        try:
            status, body = self._http_get(self._user_url, headers, self._timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            raise AccessVerificationUnavailableError(
                "Live session verification service is unavailable."
            ) from exc

        if status == 200:
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise AccessVerificationUnavailableError(
                    "Live session verification returned malformed payload."
                ) from exc
            if not isinstance(data, dict):
                raise AccessVerificationUnavailableError(
                    "Live session verification returned unexpected payload."
                )
            returned_id = str(data.get("id", "")).strip()
            if returned_id and returned_id == claims.subject:
                return LiveSessionStatus.ACTIVE
            return LiveSessionStatus.SESSION_TERMINATED

        if status in (401, 403):
            return LiveSessionStatus.SESSION_TERMINATED

        # 429, 5xx, 3xx redirect blocked, or any unexpected status -> fail-closed 503
        # preserving the web session per OD-010-5 / CONTRACT@87f1515!
        raise AccessVerificationUnavailableError(
            f"Live session verification service returned unavailable status {status}."
        )


@dataclass(frozen=True, slots=True)
class HostedUserProfile:
    """Enrolled application user record read from ``public.users`` under ``cm_api_app``."""

    user_id: str
    auth_issuer: str
    auth_subject: str
    email: str
    display_name: str


@dataclass(frozen=True, slots=True)
class HostedWorkspaceMembershipInfo:
    """Active workspace membership + workspace metadata read under ``cm_api_app``."""

    workspace_id: str
    workspace_name: str
    user_id: str
    role: str


class HostedPostgresWorkspaceAccessProvider:
    """Pooled PostgreSQL ``WorkspaceAccessProvider`` upholding ``open_tx == 0`` during auth I/O.

    CRITICAL OD-006 / T-007D INVARIANT:
    In ``verify_identity(credentials)``, JWT signature verification (JWKS) and optional GoTrue
    ``/auth/v1/user`` verification ALWAYS complete BEFORE ``self._pool.connection()`` is entered.
    """

    def __init__(
        self,
        pool: HostedTransactionPool,
        token_verifier: JwtAccessTokenVerifier,
        *,
        live_session_verifier: SupabaseGoTrueLiveSessionVerifier | None = None,
    ) -> None:
        self._pool = pool
        self._token_verifier = token_verifier
        self._live_session_verifier = live_session_verifier

    def verify_claims(self, credentials: AuthCredentials) -> VerifiedJwtClaims:
        """Verify JWT signature + optional GoTrue live session with ZERO DB connections held."""
        raw_token = credentials.bearer_token
        if raw_token is None or not raw_token.strip():
            raise UnauthenticatedError("Authentication credentials are required.")

        bound_live: LiveSessionVerifierPort | None = (
            self._live_session_verifier.for_token(raw_token)
            if self._live_session_verifier is not None
            else None
        )
        return self._token_verifier.verify(
            raw_token,
            live_session_verifier=bound_live,
        )

    def lookup_user_profile(self, claims: VerifiedJwtClaims) -> HostedUserProfile | None:
        """SELECT-only enrolled user lookup in ``public.users`` using transaction-local RLS GUC."""
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("SET TRANSACTION READ ONLY")
                        bind_postgres_identity_lookup_context(cur, claims.auth_subject)
                        cur.execute(
                            """
                            SELECT id::text, auth_issuer, auth_subject, email, name
                            FROM public.users
                            WHERE auth_issuer = %s
                              AND auth_subject = %s
                            """,
                            (claims.auth_subject.issuer, claims.auth_subject.subject),
                        )
                        row = cur.fetchone()
        except (
            UnauthenticatedError,
            SessionTerminatedError,
            AccessVerificationUnavailableError,
        ):
            raise
        except Exception as exc:  # noqa: BLE001
            raise AccessVerificationUnavailableError(
                "User identity lookup failed due to database unavailability."
            ) from exc

        if row is None:
            return None

        return HostedUserProfile(
            user_id=str(row[0]),
            auth_issuer=str(row[1]),
            auth_subject=str(row[2]),
            email=str(row[3] or claims.email or ""),
            display_name=str(row[4] or claims.name or ""),
        )

    def verify_identity(
        self, credentials: AuthCredentials
    ) -> AuthenticatedUser | None:
        # Step 1: Remote JWKS / GoTrue verification with 0 pooled DB connections checked out!
        claims = self.verify_claims(credentials)
        # Step 2: Short transaction-local SELECT on public.users
        profile = self.lookup_user_profile(claims)
        if profile is None:
            return None
        return AuthenticatedUser(
            user_id=profile.user_id,
            auth_subject=AuthSubject(issuer=profile.auth_issuer, subject=profile.auth_subject),
        )

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        try:
            workspace_uuid = _normalize_uuid(target_workspace_id, "target_workspace_id")
            user_uuid = _normalize_uuid(user.user_id, "user_id")
        except ValueError:
            return None

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute(
                            "SELECT set_config('app.current_user_id', %s, true)",
                            (user_uuid,),
                        )
                        cur.execute(
                            """
                            SELECT workspace_id::text, user_id::text, role
                            FROM public.workspace_memberships
                            WHERE workspace_id = %s::uuid
                              AND user_id = %s::uuid
                              AND revoked_at IS NULL
                            """,
                            (workspace_uuid, user_uuid),
                        )
                        row = cur.fetchone()
        except AccessVerificationUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AccessVerificationUnavailableError(
                "Workspace membership verification failed due to database unavailability."
            ) from exc

        if row is None:
            return None

        return WorkspaceMembership(
            workspace_id=str(row[0]),
            user_id=str(row[1]),
            role=str(row[2]),
        )

    def resolve_active_workspace_membership(
        self,
        user: AuthenticatedUser,
        *,
        requested_workspace_id: str | None = None,
    ) -> HostedWorkspaceMembershipInfo | None:
        """Resolve requested or primary active workspace membership (SELECT-only under RLS)."""
        try:
            user_uuid = _normalize_uuid(user.user_id, "user_id")
        except ValueError:
            return None

        target_ws_uuid: str | None = None
        if requested_workspace_id and requested_workspace_id.strip():
            try:
                target_ws_uuid = _normalize_uuid(requested_workspace_id, "requested_workspace_id")
            except ValueError:
                return None

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute(
                            "SELECT set_config('app.current_user_id', %s, true)",
                            (user_uuid,),
                        )
                        if target_ws_uuid is not None:
                            cur.execute(
                                """
                                SELECT workspace_id::text, role
                                FROM public.workspace_memberships
                                WHERE workspace_id = %s::uuid
                                  AND user_id = %s::uuid
                                  AND revoked_at IS NULL
                                LIMIT 1
                                """,
                                (target_ws_uuid, user_uuid),
                            )
                        else:
                            cur.execute(
                                """
                                SELECT workspace_id::text, role
                                FROM public.workspace_memberships
                                WHERE user_id = %s::uuid
                                  AND revoked_at IS NULL
                                ORDER BY
                                    CASE WHEN role = 'OWNER' THEN 0 ELSE 1 END ASC,
                                    created_at ASC
                                LIMIT 1
                                """,
                                (user_uuid,),
                            )
                        m_row = cur.fetchone()
                        if m_row is None:
                            return None

                        ws_id = str(m_row[0])
                        role = str(m_row[1])
                        cur.execute(
                            "SELECT set_config('app.current_workspace_id', %s, true)",
                            (ws_id,),
                        )
                        cur.execute(
                            """
                            SELECT name
                            FROM public.workspaces
                            WHERE id = %s::uuid
                            """,
                            (ws_id,),
                        )
                        ws_row = cur.fetchone()
                        ws_name = str(ws_row[0]) if ws_row is not None else "Career Memory Workspace"
        except AccessVerificationUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AccessVerificationUnavailableError(
                "Workspace lookup failed due to database unavailability."
            ) from exc

        return HostedWorkspaceMembershipInfo(
            workspace_id=ws_id,
            workspace_name=ws_name,
            user_id=user_uuid,
            role=role,
        )


__all__ = [
    "HostedPostgresWorkspaceAccessProvider",
    "HostedUserProfile",
    "HostedWorkspaceMembershipInfo",
    "HttpGetFn",
    "HttpsJwksProvider",
    "SupabaseGoTrueLiveSessionVerifier",
    "validate_pinned_auth_url",
]
