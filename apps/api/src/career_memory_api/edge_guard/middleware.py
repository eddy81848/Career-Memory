"""Pure ASGI edge origin auth, cache isolation, CORS/CSRF, caps, and pause middleware (T-507 / INF-07)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from career_memory_api.auth.redaction import redact_text

from .models import (
    AUTHENTICATED_API_VARY,
    CDN_NO_STORE_HEADER_VALUE,
    DEFAULT_EDGE_FREE_ALTERNATIVES,
    FORBIDDEN_WORKER_INGRESS_HEADERS,
    PRIVATE_NO_STORE_CACHE_CONTROL,
    PUBLIC_HEALTH_CACHE_CONTROL,
    SECURITY_RESPONSE_HEADERS,
    STATE_CHANGING_HTTP_METHODS,
    EdgeDenialCode,
    EdgeGuardConfig,
    ManualEdgePauseController,
    OriginAuthConfig,
    RouteClassification,
    RouteTrustClass,
    WorkerAuthConfig,
    WorkerIdentity,
    classify_route,
    utc_now,
)

_STRIPPED_RESPONSE_HEADERS: frozenset[bytes] = frozenset(
    {
        b"cache-control",
        b"cdn-cache-control",
        b"cloudflare-cdn-cache-control",
        b"surrogate-control",
        b"expires",
        b"pragma",
        b"vary",
        b"x-content-type-options",
        b"x-frame-options",
        b"referrer-policy",
        b"access-control-allow-origin",
        b"access-control-allow-credentials",
        b"access-control-allow-methods",
        b"access-control-allow-headers",
        b"access-control-max-age",
    }
)

_SIGNED_ORIGIN_HEADER_RE = re.compile(r"^t=(\d+),sig=([0-9a-fA-F]{64})$")
_XFCC_HASH_RE = re.compile(r'(?i)\bHash="?([0-9a-f]{64})"?')
_XFCC_SUBJECT_RE = re.compile(r'(?i)\bSubject="([^"]+)"')


class _RequestBodyTooLargeError(Exception):
    """Internal sentinel raised when streamed request bytes exceed max_request_body_bytes."""


class SlidingWindowRateLimiter:
    """Thread-safe deterministic sliding-window limiter with injectable clock."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1.")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0.")
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}

    def check_and_consume(self, key: str) -> tuple[bool, int]:
        """Consume 1 token for ``key`` or return ``(False, retry_after_seconds)``."""
        now = float(self._clock())
        cutoff = now - self._window_seconds
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                oldest = bucket[0]
                retry_after = max(1, math.ceil((oldest + self._window_seconds) - now))
                return False, retry_after
            bucket.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class ConcurrencyCapGuard:
    """Thread-safe non-blocking bounded in-flight request limiter (no unbounded queueing)."""

    def __init__(self, max_concurrent_requests: int) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be >= 1.")
        self._max = max_concurrent_requests
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def try_acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self._max:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1


class HmacWorkerTokenVerifier:
    """Deterministic HMAC-SHA256 internal worker OIDC-like token mint/verifier for local & test runs."""

    def __init__(
        self,
        *,
        secret: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secret.strip():
            raise ValueError("Worker token secret must be non-empty.")
        self._secret = secret.encode("utf-8")
        self._clock = clock

    def mint_token(
        self,
        *,
        subject: str,
        issuer: str,
        audience: str,
        expires_at: float,
    ) -> str:
        payload = json.dumps(
            {
                "sub": subject,
                "iss": issuer,
                "aud": audience,
                "exp": float(expires_at),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        sig = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"cmw1.{encoded}.{sig}"

    def __call__(self, token: str) -> WorkerIdentity | None:
        parts = token.strip().split(".")
        if len(parts) != 3 or parts[0] != "cmw1":
            return None
        _, encoded, provided_sig = parts
        expected_sig = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(provided_sig.lower(), expected_sig.lower()):
            return None
        padding = "=" * (-len(encoded) % 4)
        try:
            raw = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
            claims = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(claims, dict):
            return None
        try:
            sub = str(claims["sub"]).strip()
            iss = str(claims["iss"]).strip()
            aud = str(claims["aud"]).strip()
            exp = float(claims["exp"])
        except (KeyError, TypeError, ValueError):
            return None
        if not sub or not iss or not aud or exp <= float(self._clock()):
            return None
        return WorkerIdentity(subject=sub, issuer=iss, audience=aud, expires_at=exp)


def build_edge_error_payload(
    *,
    code: str,
    message: str,
    retryable: bool,
    request_id: str = "",
    include_free_alternatives: bool = False,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    """Build a redacted JSON error envelope consistent with Career Memory auth/http conventions."""
    error_body: dict[str, Any] = {
        "code": code,
        "message": redact_text(message),
        "retryable": retryable,
        "request_id": redact_text(request_id.strip()),
    }
    if retry_after_seconds is not None:
        error_body["retry_after_seconds"] = int(retry_after_seconds)
    if include_free_alternatives:
        error_body["paid_fallback_allowed"] = False
        error_body["free_alternatives"] = [alt.value for alt in DEFAULT_EDGE_FREE_ALTERNATIVES]
    return {"error": error_body}


def _extract_headers(scope: Scope) -> dict[str, str]:
    """Extract lowercase-keyed HTTP request headers (combining duplicates safely)."""
    headers: dict[str, str] = {}
    for raw_k, raw_v in scope.get("headers", ()):
        key = raw_k.decode("latin-1").strip().lower()
        val = raw_v.decode("latin-1").strip()
        if key in headers:
            # Join repeated headers; Cookie uses '; ', others use ', '.
            sep = "; " if key == "cookie" else ", "
            headers[key] = f"{headers[key]}{sep}{val}"
        else:
            headers[key] = val
    return headers


def _extract_client_host(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0]).strip()
    return ""


def _is_run_app_host(host_header: str) -> bool:
    host = host_header.strip().lower()
    if not host:
        return False
    if ":" in host and not host.startswith("["):
        host = host.split(":", 1)[0]
    host = host.rstrip(".")
    return host == "run.app" or host.endswith(".run.app")


def _verify_origin_hmac_or_secret(
    header_value: str,
    *,
    method: str,
    path: str,
    cfg: OriginAuthConfig,
    wall_clock: Callable[[], float],
) -> bool:
    if not header_value or not cfg.origin_hmac_secrets:
        return False

    signed_match = _SIGNED_ORIGIN_HEADER_RE.match(header_value.strip())
    if signed_match:
        ts = int(signed_match.group(1))
        provided_sig = signed_match.group(2).lower()
        now = float(wall_clock())
        if abs(now - ts) > cfg.origin_signature_max_skew_seconds:
            return False
        canonical = f"{ts}:{method.strip().upper()}:{path}".encode("utf-8")
        for secret in cfg.origin_hmac_secrets:
            expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
            if hmac.compare_digest(provided_sig, expected):
                return True
        return False

    # Constant-time secret rotation ring match (e.g. Cloudflare Transform Rule secret header).
    candidate = header_value.strip()
    for secret in cfg.origin_hmac_secrets:
        if hmac.compare_digest(candidate, secret):
            return True
    return False


def _verify_mtls_client_cert(
    headers: dict[str, str],
    *,
    peer_ip: str,
    cfg: OriginAuthConfig,
) -> bool:
    if not peer_ip or peer_ip not in cfg.trusted_mtls_proxy_ips:
        return False
    if not cfg.allowed_mtls_fingerprints and not cfg.allowed_mtls_subjects:
        return False
    xfcc = headers.get(cfg.mtls_cert_header.lower(), "")
    if not xfcc:
        return False

    fp_match = _XFCC_HASH_RE.search(xfcc)
    if fp_match and fp_match.group(1).lower() in cfg.allowed_mtls_fingerprints:
        return True

    subj_match = _XFCC_SUBJECT_RE.search(xfcc)
    if subj_match and subj_match.group(1).strip() in cfg.allowed_mtls_subjects:
        return True

    return False


def _verify_origin_auth(
    headers: dict[str, str],
    *,
    method: str,
    path: str,
    peer_ip: str,
    cfg: OriginAuthConfig,
    wall_clock: Callable[[], float],
) -> bool:
    if not cfg.require_origin_auth:
        return True

    host_header = headers.get("host", "")
    is_run_app = _is_run_app_host(host_header)
    if is_run_app and cfg.block_direct_run_app_hosts and not cfg.allow_run_app_host_with_origin_proof:
        return False

    origin_header_val = headers.get(cfg.origin_auth_header.lower(), "")
    if _verify_origin_hmac_or_secret(
        origin_header_val,
        method=method,
        path=path,
        cfg=cfg,
        wall_clock=wall_clock,
    ):
        return True

    if _verify_mtls_client_cert(headers, peer_ip=peer_ip, cfg=cfg):
        return True

    return False


def _verify_internal_worker_auth(
    headers: dict[str, str],
    *,
    cfg: WorkerAuthConfig,
) -> tuple[bool, str]:
    # 1. Immediately deny any edge/browser/session request regardless of token.
    for forbidden in FORBIDDEN_WORKER_INGRESS_HEADERS:
        if forbidden in headers:
            return False, "Edge, browser, or session headers are forbidden on internal worker routes."

    if cfg.token_verifier is None:
        return False, "Internal worker OIDC/IAM verifier is not configured."

    raw_token = headers.get(cfg.worker_auth_header.lower(), "")
    if not raw_token:
        authz = headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            raw_token = authz[7:].strip()

    if not raw_token:
        return False, "Missing internal worker IAM/OIDC audience token."

    identity = cfg.token_verifier(raw_token)
    if identity is None:
        return False, "Invalid or expired internal worker IAM/OIDC token."

    if identity.audience not in cfg.allowed_worker_audiences:
        return False, "Internal worker token audience is not allowed."
    if identity.issuer not in cfg.allowed_worker_issuers:
        return False, "Internal worker token issuer is not allowed."
    if (
        cfg.allowed_worker_service_accounts
        and identity.subject.strip().lower() not in cfg.allowed_worker_service_accounts
    ):
        return False, "Internal worker caller service account is not allowlisted."

    return True, ""


def _build_rate_limit_key(headers: dict[str, str], *, peer_ip: str, origin_verified: bool) -> str:
    """Derive a redacted actor/workspace/IP rate-limit bucket key (never stores raw tokens)."""
    workspace_id = headers.get("x-workspace-id", "").strip()
    authz = headers.get("authorization", "").strip()
    cookie = headers.get("cookie", "").strip()
    if authz:
        digest = hashlib.sha256(authz.encode("utf-8")).hexdigest()[:24]
        return f"auth:{workspace_id or 'default'}:{digest}"
    if cookie:
        digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:24]
        return f"cookie:{workspace_id or 'default'}:{digest}"
    if origin_verified and headers.get("cf-connecting-ip"):
        return f"ip:{headers['cf-connecting-ip'].strip()}"
    return f"ip:{peer_ip or 'unknown'}"


def _build_security_headers(
    *,
    trust_class: RouteTrustClass,
    allowed_origin: str | None = None,
    allow_credentials: bool = True,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[tuple[bytes, bytes]]:
    """Construct canonical non-cacheable security headers for the route trust class."""
    pairs: list[tuple[str, str]] = []
    if trust_class is RouteTrustClass.PUBLIC_HEALTH:
        pairs.append(("Cache-Control", PUBLIC_HEALTH_CACHE_CONTROL))
        pairs.append(("CDN-Cache-Control", CDN_NO_STORE_HEADER_VALUE))
        pairs.extend(SECURITY_RESPONSE_HEADERS)
    else:
        pairs.append(("Cache-Control", PRIVATE_NO_STORE_CACHE_CONTROL))
        pairs.append(("CDN-Cache-Control", CDN_NO_STORE_HEADER_VALUE))
        pairs.append(("Pragma", "no-cache"))
        pairs.append(("Vary", AUTHENTICATED_API_VARY))
        pairs.extend(SECURITY_RESPONSE_HEADERS)
        if allowed_origin is not None:
            pairs.append(("Access-Control-Allow-Origin", allowed_origin))
            if allow_credentials:
                pairs.append(("Access-Control-Allow-Credentials", "true"))

    if extra_headers:
        pairs.extend(extra_headers)

    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in pairs]


async def _send_json_response(
    send: Send,
    *,
    status_code: int,
    payload: dict[str, Any],
    trust_class: RouteTrustClass,
    allowed_origin: str | None = None,
    allow_credentials: bool = True,
    extra_headers: list[tuple[str, str]] | None = None,
) -> None:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    headers.extend(
        _build_security_headers(
            trust_class=trust_class,
            allowed_origin=allowed_origin,
            allow_credentials=allow_credentials,
            extra_headers=extra_headers,
        )
    )
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )


async def _send_empty_response(
    send: Send,
    *,
    status_code: int,
    trust_class: RouteTrustClass,
    allowed_origin: str | None = None,
    allow_credentials: bool = True,
    extra_headers: list[tuple[str, str]] | None = None,
) -> None:
    headers = _build_security_headers(
        trust_class=trust_class,
        allowed_origin=allowed_origin,
        allow_credentials=allow_credentials,
        extra_headers=extra_headers,
    )
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        }
    )


class EdgeOriginAndCacheMiddleware:
    """Pure ASGI middleware enforcing T-507 / INF-07 / SEC-01 / ADR-020 edge invariants."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: EdgeGuardConfig | None = None,
        controller: ManualEdgePauseController | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        concurrency_guard: ConcurrencyCapGuard | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.config = config or EdgeGuardConfig()
        self.controller = controller or ManualEdgePauseController()
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            limit=self.config.caps.rate_limit_per_window,
            window_seconds=self.config.caps.rate_window_seconds,
            clock=monotonic_clock,
        )
        self.concurrency_guard = concurrency_guard or ConcurrencyCapGuard(
            self.config.caps.max_concurrent_requests
        )
        self._wall_clock = wall_clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        raw_path = scope.get("raw_path")
        raw_path_bytes = raw_path if isinstance(raw_path, bytes) else None
        headers = _extract_headers(scope)
        peer_ip = _extract_client_host(scope)
        request_id = headers.get("x-request-id", "")

        classification = classify_route(
            method, path, self.config, raw_path=raw_path_bytes
        )

        # 1. Fail closed on undeclared / prohibited routes.
        if classification.trust_class is RouteTrustClass.DENIED_UNDECLARED:
            await _send_json_response(
                send,
                status_code=self.config.undeclared_route_status_code,
                payload=build_edge_error_payload(
                    code=EdgeDenialCode.ROUTE_NOT_DECLARED.value,
                    message="The requested route is not declared on this public surface.",
                    retryable=False,
                    request_id=request_id,
                ),
                trust_class=RouteTrustClass.DENIED_UNDECLARED,
                extra_headers=[("X-Request-ID", request_id)] if request_id else None,
            )
            return

        # 2. PUBLIC_HEALTH: GET /healthz (always allowed, Cache-Control: no-store).
        if classification.trust_class is RouteTrustClass.PUBLIC_HEALTH:
            await self._invoke_downstream(
                scope,
                receive,
                send,
                classification=classification,
                allowed_origin=None,
                timeout_seconds=self.config.caps.api_request_slice_timeout_seconds,
                request_id=request_id,
                enforce_body_cap=False,
            )
            return

        # 3. INTERNAL_WORKER_ONLY: NEVER reachable through edge/browser/session ingress.
        if classification.trust_class is RouteTrustClass.INTERNAL_WORKER_ONLY:
            ok, reason = _verify_internal_worker_auth(
                headers, cfg=self.config.worker_auth
            )
            if not ok:
                await _send_json_response(
                    send,
                    status_code=403,
                    payload=build_edge_error_payload(
                        code=EdgeDenialCode.PUBLIC_WORKER_ROUTE_FORBIDDEN.value,
                        message=reason,
                        retryable=False,
                        request_id=request_id,
                    ),
                    trust_class=RouteTrustClass.INTERNAL_WORKER_ONLY,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
                return

            if not await self._enforce_pause_and_body_header(
                send,
                classification=classification,
                headers=headers,
                allowed_origin=None,
                request_id=request_id,
            ):
                return

            await self._run_with_concurrency_and_timeout(
                scope,
                receive,
                send,
                classification=classification,
                allowed_origin=None,
                timeout_seconds=self.config.caps.worker_request_slice_timeout_seconds,
                request_id=request_id,
            )
            return

        # 4. AUTHENTICATED_API (/v1/**)
        # 4a. Origin authentication & direct *.run.app bypass prevention (INF-07).
        origin_verified = _verify_origin_auth(
            headers,
            method=method,
            path=path,
            peer_ip=peer_ip,
            cfg=self.config.origin_auth,
            wall_clock=self._wall_clock,
        )
        if not origin_verified:
            await _send_json_response(
                send,
                status_code=403,
                payload=build_edge_error_payload(
                    code=EdgeDenialCode.ORIGIN_AUTH_REQUIRED.value,
                    message="Direct origin access is forbidden; valid edge origin authentication is required.",
                    retryable=False,
                    request_id=request_id,
                ),
                trust_class=RouteTrustClass.AUTHENTICATED_API,
                extra_headers=[("X-Request-ID", request_id)] if request_id else None,
            )
            return

        # 4b. Strict CORS preflight & CSRF Origin / Sec-Fetch guard (INF-07 / SEC-01).
        cors_cfg = self.config.cors_csrf
        raw_origin = headers.get("origin", "").strip()
        normalized_origin = raw_origin.lower() if raw_origin else ""
        origin_allowed = (
            bool(normalized_origin)
            and normalized_origin in cors_cfg.allowed_web_origins
        )
        effective_cors_origin = raw_origin if origin_allowed else None

        # CORS preflight OPTIONS
        if method == "OPTIONS" and raw_origin and headers.get("access-control-request-method"):
            req_method = headers["access-control-request-method"].strip().upper()
            if not origin_allowed or req_method not in cors_cfg.allowed_methods:
                await _send_json_response(
                    send,
                    status_code=403,
                    payload=build_edge_error_payload(
                        code=EdgeDenialCode.CORS_ORIGIN_DENIED.value,
                        message="Cross-origin preflight request is not permitted.",
                        retryable=False,
                        request_id=request_id,
                    ),
                    trust_class=RouteTrustClass.AUTHENTICATED_API,
                    allowed_origin=None,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
                return

            preflight_headers: list[tuple[str, str]] = [
                ("Access-Control-Allow-Methods", ", ".join(cors_cfg.allowed_methods)),
                ("Access-Control-Allow-Headers", ", ".join(cors_cfg.allowed_headers)),
                ("Access-Control-Max-Age", "600"),
            ]
            if request_id:
                preflight_headers.append(("X-Request-ID", request_id))
            await _send_empty_response(
                send,
                status_code=204,
                trust_class=RouteTrustClass.AUTHENTICATED_API,
                allowed_origin=effective_cors_origin,
                allow_credentials=cors_cfg.allow_credentials,
                extra_headers=preflight_headers,
            )
            return

        # CSRF check on state-changing methods (and cross-site cookie reads).
        sec_fetch_site = headers.get("sec-fetch-site", "").strip().lower()
        has_cookie = bool(headers.get("cookie", "").strip())

        if cors_cfg.enforce_csrf_on_state_changing and method in STATE_CHANGING_HTTP_METHODS:
            csrf_violation = False
            if sec_fetch_site and sec_fetch_site not in ("same-origin", "same-site", "none"):
                csrf_violation = True
            elif raw_origin and not origin_allowed:
                csrf_violation = True
            elif has_cookie and not raw_origin and sec_fetch_site != "same-origin":
                csrf_violation = True

            if csrf_violation:
                await _send_json_response(
                    send,
                    status_code=403,
                    payload=build_edge_error_payload(
                        code=EdgeDenialCode.CSRF_ORIGIN_DENIED.value,
                        message="Cross-site or untrusted origin state-changing request was rejected.",
                        retryable=False,
                        request_id=request_id,
                    ),
                    trust_class=RouteTrustClass.AUTHENTICATED_API,
                    allowed_origin=None,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
                return
        elif has_cookie and sec_fetch_site == "cross-site":
            await _send_json_response(
                send,
                status_code=403,
                payload=build_edge_error_payload(
                    code=EdgeDenialCode.CSRF_ORIGIN_DENIED.value,
                    message="Cross-site cookie-authenticated request was rejected.",
                    retryable=False,
                    request_id=request_id,
                ),
                trust_class=RouteTrustClass.AUTHENTICATED_API,
                allowed_origin=None,
                extra_headers=[("X-Request-ID", request_id)] if request_id else None,
            )
            return

        # 4c. Manual pause / spend cutoff switch + declared Content-Length body cap.
        if not await self._enforce_pause_and_body_header(
            send,
            classification=classification,
            headers=headers,
            allowed_origin=effective_cors_origin,
            request_id=request_id,
        ):
            return

        # 4d. Rate limit (keyed by authenticated actor/workspace hash or client IP).
        rate_key = _build_rate_limit_key(
            headers, peer_ip=peer_ip, origin_verified=origin_verified
        )
        rate_ok, retry_after = self.rate_limiter.check_and_consume(rate_key)
        if not rate_ok:
            extra = [("Retry-After", str(retry_after))]
            if request_id:
                extra.append(("X-Request-ID", request_id))
            await _send_json_response(
                send,
                status_code=429,
                payload=build_edge_error_payload(
                    code=EdgeDenialCode.RATE_LIMIT_EXCEEDED.value,
                    message="Request rate limit exceeded; retry after the indicated window.",
                    retryable=True,
                    request_id=request_id,
                    include_free_alternatives=True,
                    retry_after_seconds=retry_after,
                ),
                trust_class=RouteTrustClass.AUTHENTICATED_API,
                allowed_origin=effective_cors_origin,
                allow_credentials=cors_cfg.allow_credentials,
                extra_headers=extra,
            )
            return

        # 4e. Bounded concurrency + streamed body cap + slice timeout + response header isolation.
        await self._run_with_concurrency_and_timeout(
            scope,
            receive,
            send,
            classification=classification,
            allowed_origin=effective_cors_origin,
            timeout_seconds=self.config.caps.api_request_slice_timeout_seconds,
            request_id=request_id,
        )

    async def _enforce_pause_and_body_header(
        self,
        send: Send,
        *,
        classification: RouteClassification,
        headers: dict[str, str],
        allowed_origin: str | None,
        request_id: str,
    ) -> bool:
        pause_ok, denial_code = self.controller.evaluate_route(classification)
        if not pause_ok and denial_code is not None:
            retry_after = self.config.caps.retry_after_paused_seconds
            extra = [("Retry-After", str(retry_after))]
            if request_id:
                extra.append(("X-Request-ID", request_id))
            await _send_json_response(
                send,
                status_code=503,
                payload=build_edge_error_payload(
                    code=denial_code.value,
                    message=(
                        "High-cost runtime operations are manually paused by the cost guard. "
                        "Account disconnect and deletion routes remain available."
                    ),
                    retryable=True,
                    request_id=request_id,
                    include_free_alternatives=True,
                    retry_after_seconds=retry_after,
                ),
                trust_class=classification.trust_class,
                allowed_origin=allowed_origin,
                allow_credentials=self.config.cors_csrf.allow_credentials,
                extra_headers=extra,
            )
            return False

        # Reject oversized Content-Length BEFORE invoking downstream handler.
        raw_cl = headers.get("content-length")
        if raw_cl is not None and raw_cl != "":
            try:
                content_length = int(raw_cl)
            except ValueError:
                await _send_json_response(
                    send,
                    status_code=400,
                    payload=build_edge_error_payload(
                        code="INVALID_CONTENT_LENGTH",
                        message="Malformed Content-Length header.",
                        retryable=False,
                        request_id=request_id,
                    ),
                    trust_class=classification.trust_class,
                    allowed_origin=allowed_origin,
                    allow_credentials=self.config.cors_csrf.allow_credentials,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
                return False
            if content_length < 0:
                await _send_json_response(
                    send,
                    status_code=400,
                    payload=build_edge_error_payload(
                        code="INVALID_CONTENT_LENGTH",
                        message="Negative Content-Length header is not allowed.",
                        retryable=False,
                        request_id=request_id,
                    ),
                    trust_class=classification.trust_class,
                    allowed_origin=allowed_origin,
                    allow_credentials=self.config.cors_csrf.allow_credentials,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
                return False
            if content_length > self.config.caps.max_request_body_bytes:
                await _send_json_response(
                    send,
                    status_code=413,
                    payload=build_edge_error_payload(
                        code=EdgeDenialCode.REQUEST_BODY_TOO_LARGE.value,
                        message=(
                            f"Request body exceeds maximum allowed size of "
                            f"{self.config.caps.max_request_body_bytes} bytes."
                        ),
                        retryable=False,
                        request_id=request_id,
                    ),
                    trust_class=classification.trust_class,
                    allowed_origin=allowed_origin,
                    allow_credentials=self.config.cors_csrf.allow_credentials,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
                return False

        return True

    async def _run_with_concurrency_and_timeout(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        classification: RouteClassification,
        allowed_origin: str | None,
        timeout_seconds: float,
        request_id: str,
    ) -> None:
        if not self.concurrency_guard.try_acquire():
            retry_after = self.config.caps.retry_after_concurrency_seconds
            extra = [("Retry-After", str(retry_after))]
            if request_id:
                extra.append(("X-Request-ID", request_id))
            await _send_json_response(
                send,
                status_code=503,
                payload=build_edge_error_payload(
                    code=EdgeDenialCode.CONCURRENCY_CAP_REACHED.value,
                    message="Bounded in-process concurrency cap reached; retry shortly.",
                    retryable=True,
                    request_id=request_id,
                    include_free_alternatives=True,
                    retry_after_seconds=retry_after,
                ),
                trust_class=classification.trust_class,
                allowed_origin=allowed_origin,
                allow_credentials=self.config.cors_csrf.allow_credentials,
                extra_headers=extra,
            )
            return

        try:
            await self._invoke_downstream(
                scope,
                receive,
                send,
                classification=classification,
                allowed_origin=allowed_origin,
                timeout_seconds=timeout_seconds,
                request_id=request_id,
                enforce_body_cap=True,
            )
        finally:
            self.concurrency_guard.release()

    async def _invoke_downstream(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        classification: RouteClassification,
        allowed_origin: str | None,
        timeout_seconds: float,
        request_id: str,
        enforce_body_cap: bool,
    ) -> None:
        max_bytes = self.config.caps.max_request_body_bytes
        bytes_seen = 0
        response_started = False

        async def guarded_receive() -> Message:
            nonlocal bytes_seen
            message = await receive()
            if enforce_body_cap and message.get("type") == "http.request":
                chunk = message.get("body", b"")
                if chunk:
                    bytes_seen += len(chunk)
                    if bytes_seen > max_bytes:
                        raise _RequestBodyTooLargeError()
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                raw_headers = list(message.get("headers", ()))
                filtered = [
                    (k, v)
                    for k, v in raw_headers
                    if k.lower() not in _STRIPPED_RESPONSE_HEADERS
                ]
                filtered.extend(
                    _build_security_headers(
                        trust_class=classification.trust_class,
                        allowed_origin=allowed_origin,
                        allow_credentials=self.config.cors_csrf.allow_credentials,
                    )
                )
                message = {**message, "headers": filtered}
            await send(message)

        try:
            await asyncio.wait_for(
                self.app(scope, guarded_receive, guarded_send),
                timeout=timeout_seconds,
            )
        except _RequestBodyTooLargeError:
            if not response_started:
                await _send_json_response(
                    send,
                    status_code=413,
                    payload=build_edge_error_payload(
                        code=EdgeDenialCode.REQUEST_BODY_TOO_LARGE.value,
                        message=(
                            f"Streamed request body exceeds maximum allowed size of "
                            f"{max_bytes} bytes."
                        ),
                        retryable=False,
                        request_id=request_id,
                    ),
                    trust_class=classification.trust_class,
                    allowed_origin=allowed_origin,
                    allow_credentials=self.config.cors_csrf.allow_credentials,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
        except TimeoutError:
            if not response_started:
                await _send_json_response(
                    send,
                    status_code=504,
                    payload=build_edge_error_payload(
                        code=EdgeDenialCode.REQUEST_SLICE_TIMEOUT.value,
                        message="Request exceeded the bounded runtime execution slice timeout.",
                        retryable=True,
                        request_id=request_id,
                        include_free_alternatives=True,
                    ),
                    trust_class=classification.trust_class,
                    allowed_origin=allowed_origin,
                    allow_credentials=self.config.cors_csrf.allow_credentials,
                    extra_headers=[("X-Request-ID", request_id)] if request_id else None,
                )
        except Exception:  # noqa: BLE001
            if not self.config.catch_unhandled_exceptions or response_started:
                raise
            await _send_json_response(
                send,
                status_code=500,
                payload=build_edge_error_payload(
                    code="INTERNAL_SERVER_ERROR",
                    message="An unexpected internal error occurred.",
                    retryable=False,
                    request_id=request_id,
                ),
                trust_class=classification.trust_class,
                allowed_origin=allowed_origin,
                allow_credentials=self.config.cors_csrf.allow_credentials,
                extra_headers=[("X-Request-ID", request_id)] if request_id else None,
            )


@dataclass(frozen=True, slots=True)
class EdgeGuardRuntime:
    """Handle returned by install_edge_guard() for operator inspection and testing."""

    config: EdgeGuardConfig
    controller: ManualEdgePauseController
    rate_limiter: SlidingWindowRateLimiter
    concurrency_guard: ConcurrencyCapGuard


def install_edge_guard(
    app: FastAPI,
    config: EdgeGuardConfig,
    controller: ManualEdgePauseController | None = None,
    *,
    monotonic_clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    utc_clock: Callable[[], datetime] = utc_now,
) -> EdgeGuardRuntime:
    """Mount EdgeOriginAndCacheMiddleware onto a FastAPI app in a single composable call."""
    resolved_controller = (
        controller if controller is not None else ManualEdgePauseController(clock=utc_clock)
    )
    rate_limiter = SlidingWindowRateLimiter(
        limit=config.caps.rate_limit_per_window,
        window_seconds=config.caps.rate_window_seconds,
        clock=monotonic_clock,
    )
    concurrency_guard = ConcurrencyCapGuard(config.caps.max_concurrent_requests)

    app.add_middleware(
        EdgeOriginAndCacheMiddleware,
        config=config,
        controller=resolved_controller,
        rate_limiter=rate_limiter,
        concurrency_guard=concurrency_guard,
        monotonic_clock=monotonic_clock,
        wall_clock=wall_clock,
    )

    runtime = EdgeGuardRuntime(
        config=config,
        controller=resolved_controller,
        rate_limiter=rate_limiter,
        concurrency_guard=concurrency_guard,
    )
    app.state.edge_guard_config = config
    app.state.edge_pause_controller = resolved_controller
    app.state.edge_guard_runtime = runtime
    return runtime
