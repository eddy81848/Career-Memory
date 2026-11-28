"""Typed route trust matrix, runtime caps, and manual cost-guard pause controller (T-507 / INF-07)."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import unquote, urlsplit

from career_memory_api.auth.redaction import redact_text

EDGE_GUARD_POLICY_VERSION = "edge-guard-policy-v1"
HOSTED_PUBLIC_PILOT_GATE_DEFAULT = "HELD_NO_GO_PENDING_OD013_OD015"

PUBLIC_HEALTH_CACHE_CONTROL = "no-store"
PRIVATE_NO_STORE_CACHE_CONTROL = "private, no-store"
CDN_NO_STORE_HEADER_VALUE = "no-store"
AUTHENTICATED_API_VARY = "Authorization, Cookie, X-Workspace-ID, Origin"

DEFAULT_ORIGIN_AUTH_HEADER = "X-CM-Origin-Auth"
DEFAULT_WORKER_AUTH_HEADER = "X-CM-Worker-Auth"
DEFAULT_MTLS_CERT_HEADER = "X-Forwarded-Client-Cert"

SECURITY_RESPONSE_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
)

# Edge / browser / session headers that are NEVER allowed on internal worker routes.
FORBIDDEN_WORKER_INGRESS_HEADERS: frozenset[str] = frozenset(
    {
        "cf-connecting-ip",
        "cf-ray",
        "cf-visitor",
        "cf-ipcountry",
        "x-cm-origin-auth",
        "cookie",
        "origin",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "referer",
    }
)

STATE_CHANGING_HTTP_METHODS: frozenset[str] = frozenset(
    {"POST", "PATCH", "PUT", "DELETE"}
)

DEFAULT_ALLOWED_API_PREFIXES: frozenset[str] = frozenset(
    {
        "/v1/sources/github",
        "/v1/source-accounts",
        "/v1/source-resources",
        "/v1/deletion-jobs",
        "/v1/sync-runs",
        "/v1/content-units",
        "/v1/work-events",
        "/v1/identities",
        "/v1/facts",
        "/v1/claims",
        "/v1/exports",
        "/v1/workspaces",
        "/v1/me",
        "/v1/session",
    }
)

# Explicitly denied undeclared / prohibited prefixes (ADR-020 / OD-014 / INF-07).
PROHIBITED_ROUTE_PREFIXES: frozenset[str] = frozenset(
    {
        "/debug",
        "/admin",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/v1/byok",
        "/v1/llm",
        "/v1/admin",
        "/v1/debug",
    }
)

# Essential safety & user-rights route patterns that MUST stay allowed during PAUSED_COST_GUARD.
_ESSENTIAL_USER_RIGHTS_PATTERNS: tuple[tuple[frozenset[str], re.Pattern[str], str], ...] = (
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/source-accounts/[^/]+/disconnect$"),
        "POST /v1/source-accounts/{id}/disconnect",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/source-resources/[^/]+/deletion-preview$"),
        "POST /v1/source-resources/{id}/deletion-preview",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/source-resources/[^/]+/deletion-confirm$"),
        "POST /v1/source-resources/{id}/deletion-confirm",
    ),
    (
        frozenset({"GET", "HEAD"}),
        re.compile(r"^/v1/deletion-jobs/[^/]+$"),
        "GET /v1/deletion-jobs/{id}",
    ),
)

# Named high-cost / mutable growth routes explicitly blocked during PAUSED_COST_GUARD.
_HIGH_COST_GROWTH_PATTERNS: tuple[tuple[frozenset[str], re.Pattern[str], str], ...] = (
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/sync-runs$"),
        "POST /v1/sync-runs",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/sync-runs/[^/]+/retry$"),
        "POST /v1/sync-runs/{id}/retry",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/work-events/[^/]+/claims:generate$"),
        "POST /v1/work-events/{id}/claims:generate",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/work-events/[^/]+/facts:extract$"),
        "POST /v1/work-events/{id}/facts:extract",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/claims/drafts$"),
        "POST /v1/claims/drafts",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"^/v1/exports$"),
        "POST /v1/exports",
    ),
)

_SAFE_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?$")
_SUSPICIOUS_RAW_PATH_RE = re.compile(r"(?i)(%2e|%2f|%5c|\\|//|\.\.)")


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


def format_utc_timestamp(ts: datetime) -> str:
    """Format a UTC datetime in canonical ISO-8601 Z format."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


class RouteTrustClass(StrEnum):
    """Explicit ingress trust classification for every HTTP route (INF-07 / T-507)."""

    PUBLIC_HEALTH = "PUBLIC_HEALTH"
    AUTHENTICATED_API = "AUTHENTICATED_API"
    INTERNAL_WORKER_ONLY = "INTERNAL_WORKER_ONLY"
    DENIED_UNDECLARED = "DENIED_UNDECLARED"


class EdgePausePolicy(StrEnum):
    """Behavior of a classified route when the manual cost-guard pause switch is engaged."""

    ESSENTIAL_SAFETY_ALWAYS_ALLOWED = "ESSENTIAL_SAFETY_ALWAYS_ALLOWED"
    ALLOWED_IN_COST_GUARD = "ALLOWED_IN_COST_GUARD"
    BLOCKED_IN_COST_GUARD = "BLOCKED_IN_COST_GUARD"


class EdgeRuntimeMode(StrEnum):
    """Operator-controlled runtime state for manual emergency spend/incident cutoff."""

    ACTIVE = "ACTIVE"
    PAUSED_COST_GUARD = "PAUSED_COST_GUARD"
    PAUSED_INCIDENT = "PAUSED_INCIDENT"


class EdgeDenialCode(StrEnum):
    """Stable machine-readable error codes emitted by the edge/origin/cost guard."""

    ORIGIN_AUTH_REQUIRED = "ORIGIN_AUTH_REQUIRED"
    PUBLIC_WORKER_ROUTE_FORBIDDEN = "PUBLIC_WORKER_ROUTE_FORBIDDEN"
    ROUTE_NOT_DECLARED = "ROUTE_NOT_DECLARED"
    CSRF_ORIGIN_DENIED = "CSRF_ORIGIN_DENIED"
    CORS_ORIGIN_DENIED = "CORS_ORIGIN_DENIED"
    REQUEST_BODY_TOO_LARGE = "REQUEST_BODY_TOO_LARGE"
    CONCURRENCY_CAP_REACHED = "CONCURRENCY_CAP_REACHED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    REQUEST_SLICE_TIMEOUT = "REQUEST_SLICE_TIMEOUT"
    RUNTIME_PAUSED_COST_GUARD = "RUNTIME_PAUSED_COST_GUARD"
    RUNTIME_PAUSED_INCIDENT = "RUNTIME_PAUSED_INCIDENT"


class EdgeFreeAlternative(StrEnum):
    """Zero-cost fallback guidance returned on cap/pause responses (ADR-020 / OD-015)."""

    REDUCE_WORK_SCOPE = "REDUCE_WORK_SCOPE"
    WAIT_FOR_WINDOW_OR_OPERATOR_RESUME = "WAIT_FOR_WINDOW_OR_OPERATOR_RESUME"
    LOCAL_DEMO_AND_EVALUATION_MODE = "LOCAL_DEMO_AND_EVALUATION_MODE"
    ACCOUNT_DISCONNECT_AND_DELETION_STILL_AVAILABLE = (
        "ACCOUNT_DISCONNECT_AND_DELETION_STILL_AVAILABLE"
    )


DEFAULT_EDGE_FREE_ALTERNATIVES: tuple[EdgeFreeAlternative, ...] = (
    EdgeFreeAlternative.REDUCE_WORK_SCOPE,
    EdgeFreeAlternative.WAIT_FOR_WINDOW_OR_OPERATOR_RESUME,
    EdgeFreeAlternative.LOCAL_DEMO_AND_EVALUATION_MODE,
    EdgeFreeAlternative.ACCOUNT_DISCONNECT_AND_DELETION_STILL_AVAILABLE,
)


class ManualResumeRejectedError(ValueError):
    """Raised when resume() is attempted without explicit budget_verified=True."""


@dataclass(frozen=True, slots=True)
class EdgeRuntimeCaps:
    """Explicit compute, payload, rate, concurrency, and timeout bounds (T-507 / OD-015)."""

    max_request_body_bytes: int = 262_144  # 256 KiB
    max_concurrent_requests: int = 16
    rate_limit_per_window: int = 60
    rate_window_seconds: float = 60.0
    api_request_slice_timeout_seconds: float = 15.0
    worker_request_slice_timeout_seconds: float = 60.0
    retry_after_concurrency_seconds: int = 5
    retry_after_paused_seconds: int = 300

    def __post_init__(self) -> None:
        if self.max_request_body_bytes <= 0:
            raise ValueError("max_request_body_bytes must be > 0.")
        if self.max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be >= 1.")
        if self.rate_limit_per_window < 1:
            raise ValueError("rate_limit_per_window must be >= 1.")
        if self.rate_window_seconds <= 0:
            raise ValueError("rate_window_seconds must be > 0.")
        if self.api_request_slice_timeout_seconds <= 0:
            raise ValueError("api_request_slice_timeout_seconds must be > 0.")
        if self.worker_request_slice_timeout_seconds <= 0:
            raise ValueError("worker_request_slice_timeout_seconds must be > 0.")
        if self.retry_after_concurrency_seconds < 1:
            raise ValueError("retry_after_concurrency_seconds must be >= 1.")
        if self.retry_after_paused_seconds < 1:
            raise ValueError("retry_after_paused_seconds must be >= 1.")


@dataclass(frozen=True, slots=True)
class OriginAuthConfig:
    """Cloudflare -> Cloud Run origin verification & direct *.run.app bypass guard (INF-07)."""

    require_origin_auth: bool = True
    origin_auth_header: str = DEFAULT_ORIGIN_AUTH_HEADER
    origin_hmac_secrets: tuple[str, ...] = ()
    origin_signature_max_skew_seconds: int = 300
    mtls_cert_header: str = DEFAULT_MTLS_CERT_HEADER
    trusted_mtls_proxy_ips: frozenset[str] = frozenset()
    allowed_mtls_fingerprints: frozenset[str] = frozenset()
    allowed_mtls_subjects: frozenset[str] = frozenset()
    block_direct_run_app_hosts: bool = True
    allow_run_app_host_with_origin_proof: bool = False

    def __post_init__(self) -> None:
        if self.origin_signature_max_skew_seconds < 1:
            raise ValueError("origin_signature_max_skew_seconds must be >= 1.")
        cleaned_secrets = tuple(s for s in self.origin_hmac_secrets if s.strip())
        object.__setattr__(self, "origin_hmac_secrets", cleaned_secrets)
        object.__setattr__(
            self,
            "allowed_mtls_fingerprints",
            frozenset(fp.strip().lower() for fp in self.allowed_mtls_fingerprints if fp.strip()),
        )
        object.__setattr__(
            self,
            "allowed_mtls_subjects",
            frozenset(s.strip() for s in self.allowed_mtls_subjects if s.strip()),
        )
        object.__setattr__(
            self,
            "trusted_mtls_proxy_ips",
            frozenset(ip.strip() for ip in self.trusted_mtls_proxy_ips if ip.strip()),
        )


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """Verified internal Cloud Run worker IAM/OIDC caller identity."""

    subject: str
    issuer: str
    audience: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class WorkerAuthConfig:
    """Private internal worker OIDC/IAM audience verification configuration (INF-07)."""

    worker_auth_header: str = DEFAULT_WORKER_AUTH_HEADER
    allowed_worker_audiences: frozenset[str] = frozenset(
        {"https://worker.internal.career-memory.local"}
    )
    allowed_worker_issuers: frozenset[str] = frozenset({"https://accounts.google.com"})
    allowed_worker_service_accounts: frozenset[str] = frozenset()
    token_verifier: Callable[[str], WorkerIdentity | None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_worker_audiences",
            frozenset(a.strip() for a in self.allowed_worker_audiences if a.strip()),
        )
        object.__setattr__(
            self,
            "allowed_worker_issuers",
            frozenset(i.strip() for i in self.allowed_worker_issuers if i.strip()),
        )
        object.__setattr__(
            self,
            "allowed_worker_service_accounts",
            frozenset(
                sa.strip().lower() for sa in self.allowed_worker_service_accounts if sa.strip()
            ),
        )


@dataclass(frozen=True, slots=True)
class CorsCsrfConfig:
    """Strict single-origin CORS & CSRF Origin/Sec-Fetch policy (INF-07 / SEC-01)."""

    allowed_web_origins: frozenset[str] = frozenset(
        {"https://app.career-memory.example"}
    )
    allowed_methods: tuple[str, ...] = ("GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS")
    allowed_headers: tuple[str, ...] = (
        "Authorization",
        "Content-Type",
        "X-Workspace-ID",
        "X-Request-ID",
        "Idempotency-Key",
    )
    allow_credentials: bool = True
    enforce_csrf_on_state_changing: bool = True

    def __post_init__(self) -> None:
        normalized: set[str] = set()
        for raw in self.allowed_web_origins:
            origin = raw.strip()
            if not origin or origin == "*" or origin.endswith("/"):
                raise ValueError(
                    f"Invalid allowed_web_origin {raw!r}: wildcards and trailing slashes are forbidden."
                )
            if not _SAFE_ORIGIN_RE.match(origin):
                raise ValueError(f"Invalid allowed_web_origin syntax: {raw!r}.")
            parts = urlsplit(origin)
            normalized.add(f"{parts.scheme.lower()}://{parts.netloc.lower()}")
        object.__setattr__(self, "allowed_web_origins", frozenset(normalized))


@dataclass(frozen=True, slots=True)
class EdgeGuardConfig:
    """Top-level configuration for Career Memory edge/origin/cache/cost protection."""

    origin_auth: OriginAuthConfig = field(default_factory=OriginAuthConfig)
    worker_auth: WorkerAuthConfig = field(default_factory=WorkerAuthConfig)
    cors_csrf: CorsCsrfConfig = field(default_factory=CorsCsrfConfig)
    caps: EdgeRuntimeCaps = field(default_factory=EdgeRuntimeCaps)
    allowed_api_prefixes: frozenset[str] = DEFAULT_ALLOWED_API_PREFIXES
    prohibited_route_prefixes: frozenset[str] = PROHIBITED_ROUTE_PREFIXES
    undeclared_route_status_code: int = 404
    catch_unhandled_exceptions: bool = True
    policy_version: str = EDGE_GUARD_POLICY_VERSION
    hosted_public_pilot_gate: str = HOSTED_PUBLIC_PILOT_GATE_DEFAULT

    def __post_init__(self) -> None:
        if self.undeclared_route_status_code not in (403, 404):
            raise ValueError("undeclared_route_status_code must be 403 or 404.")


@dataclass(frozen=True, slots=True)
class RouteClassification:
    """Result of classifying an HTTP method + path against the T-507 Route Trust Matrix."""

    trust_class: RouteTrustClass
    pause_policy: EdgePausePolicy
    matched_rule: str
    normalized_path: str
    method: str


def _matches_prefix(path: str, prefix: str) -> bool:
    """Match a route prefix strictly on a path segment boundary."""
    norm_prefix = prefix.rstrip("/")
    if not norm_prefix:
        return path == "/"
    return path == norm_prefix or path.startswith(norm_prefix + "/")


def _has_unsafe_path_syntax(path: str, raw_path: bytes | None = None) -> bool:
    if not path or not path.startswith("/"):
        return True
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return True
    if ".." in path or "//" in path or "\\" in path:
        return True
    decoded = unquote(path)
    if ".." in decoded or "//" in decoded or "\\" in decoded:
        return True
    if raw_path is not None:
        raw_text = raw_path.decode("latin-1", errors="replace")
        if _SUSPICIOUS_RAW_PATH_RE.search(raw_text):
            return True
    return False


def classify_route(
    method: str,
    path: str,
    config: EdgeGuardConfig | None = None,
    *,
    raw_path: bytes | None = None,
) -> RouteClassification:
    """Classify an incoming HTTP method and path fail-closed per T-507 / INF-07."""
    cfg = config or EdgeGuardConfig()
    norm_method = method.strip().upper()

    if _has_unsafe_path_syntax(path, raw_path=raw_path):
        return RouteClassification(
            trust_class=RouteTrustClass.DENIED_UNDECLARED,
            pause_policy=EdgePausePolicy.BLOCKED_IN_COST_GUARD,
            matched_rule="UNSAFE_PATH_SYNTAX",
            normalized_path=path,
            method=norm_method,
        )

    normalized_path = path.rstrip("/") if path != "/" else "/"

    # 1. PUBLIC_HEALTH: GET/HEAD /healthz
    if normalized_path == "/healthz":
        if norm_method in ("GET", "HEAD"):
            return RouteClassification(
                trust_class=RouteTrustClass.PUBLIC_HEALTH,
                pause_policy=EdgePausePolicy.ESSENTIAL_SAFETY_ALWAYS_ALLOWED,
                matched_rule="GET /healthz",
                normalized_path=normalized_path,
                method=norm_method,
            )
        return RouteClassification(
            trust_class=RouteTrustClass.DENIED_UNDECLARED,
            pause_policy=EdgePausePolicy.BLOCKED_IN_COST_GUARD,
            matched_rule="HEALTHZ_METHOD_NOT_ALLOWED",
            normalized_path=normalized_path,
            method=norm_method,
        )

    # 2. INTERNAL_WORKER_ONLY: /internal/worker/**
    if _matches_prefix(normalized_path, "/internal/worker"):
        return RouteClassification(
            trust_class=RouteTrustClass.INTERNAL_WORKER_ONLY,
            pause_policy=EdgePausePolicy.BLOCKED_IN_COST_GUARD,
            matched_rule="/internal/worker/**",
            normalized_path=normalized_path,
            method=norm_method,
        )

    # 3. Explicitly prohibited / undeclared routes fail closed even under /v1/**
    for prohibited in sorted(cfg.prohibited_route_prefixes):
        if _matches_prefix(normalized_path, prohibited):
            return RouteClassification(
                trust_class=RouteTrustClass.DENIED_UNDECLARED,
                pause_policy=EdgePausePolicy.BLOCKED_IN_COST_GUARD,
                matched_rule=f"PROHIBITED:{prohibited}",
                normalized_path=normalized_path,
                method=norm_method,
            )

    # 4. AUTHENTICATED_API: declared /v1/** allowlist only
    matched_prefix: str | None = None
    for prefix in sorted(cfg.allowed_api_prefixes, key=len, reverse=True):
        if _matches_prefix(normalized_path, prefix):
            matched_prefix = prefix
            break

    if matched_prefix is not None:
        # 4a. Essential safety & user-rights routes stay allowed under PAUSED_COST_GUARD.
        for methods, pattern, rule_name in _ESSENTIAL_USER_RIGHTS_PATTERNS:
            if norm_method in methods and pattern.match(normalized_path):
                return RouteClassification(
                    trust_class=RouteTrustClass.AUTHENTICATED_API,
                    pause_policy=EdgePausePolicy.ESSENTIAL_SAFETY_ALWAYS_ALLOWED,
                    matched_rule=rule_name,
                    normalized_path=normalized_path,
                    method=norm_method,
                )

        # 4b. Explicitly named high-cost / mutable growth routes are blocked under PAUSED_COST_GUARD.
        for methods, pattern, rule_name in _HIGH_COST_GROWTH_PATTERNS:
            if norm_method in methods and pattern.match(normalized_path):
                return RouteClassification(
                    trust_class=RouteTrustClass.AUTHENTICATED_API,
                    pause_policy=EdgePausePolicy.BLOCKED_IN_COST_GUARD,
                    matched_rule=rule_name,
                    normalized_path=normalized_path,
                    method=norm_method,
                )

        # 4c. Any other state-changing mutation defaults fail-closed during PAUSED_COST_GUARD;
        # read-only GET/HEAD/OPTIONS remain allowed in PAUSED_COST_GUARD (blocked in PAUSED_INCIDENT).
        if norm_method in STATE_CHANGING_HTTP_METHODS:
            pause_policy = EdgePausePolicy.BLOCKED_IN_COST_GUARD
        else:
            pause_policy = EdgePausePolicy.ALLOWED_IN_COST_GUARD

        return RouteClassification(
            trust_class=RouteTrustClass.AUTHENTICATED_API,
            pause_policy=pause_policy,
            matched_rule=f"API:{matched_prefix}",
            normalized_path=normalized_path,
            method=norm_method,
        )

    # 5. Everything else fails closed as DENIED_UNDECLARED.
    return RouteClassification(
        trust_class=RouteTrustClass.DENIED_UNDECLARED,
        pause_policy=EdgePausePolicy.BLOCKED_IN_COST_GUARD,
        matched_rule="UNDECLARED_PATH",
        normalized_path=normalized_path,
        method=norm_method,
    )


def sign_origin_auth_header(
    secret: str,
    *,
    method: str,
    path: str,
    timestamp: int,
) -> str:
    """Create a cryptographic HMAC-SHA256 X-CM-Origin-Auth header value (`t=<ts>,sig=<hex>`)."""
    if not secret:
        raise ValueError("Origin auth secret must be non-empty.")
    canonical = f"{int(timestamp)}:{method.strip().upper()}:{path}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},sig={digest}"


@dataclass(frozen=True, slots=True)
class PauseAuditEvent:
    """Redacted immutable audit entry for manual pause/resume operator transitions."""

    action: str
    from_mode: EdgeRuntimeMode
    to_mode: EdgeRuntimeMode
    reason_code: str
    actor_id: str
    budget_verified: bool
    occurred_at: datetime

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "reason_code": self.reason_code,
            "actor_id": self.actor_id,
            "budget_verified": self.budget_verified,
            "occurred_at": format_utc_timestamp(self.occurred_at),
        }


@dataclass(frozen=True, slots=True)
class EdgePauseState:
    """Current snapshot of the manual emergency pause / spend cutoff switch."""

    mode: EdgeRuntimeMode
    reason_code: str | None
    actor_id: str | None
    updated_at: datetime
    budget_verified: bool
    transition_count: int


class ManualEdgePauseController:
    """Thread-safe manual emergency pause / spend cutoff controller (ADR-020 / OD-015 / T-507).

    In ``PAUSED_COST_GUARD`` mode, high-cost / mutable growth routes are blocked with
    ``503 RUNTIME_PAUSED_COST_GUARD`` and zero paid fallback, while essential safety
    and user-rights routes (healthz, account disconnect, deletion preview/confirm/status)
    remain allowed. ``resume()`` MUST reject ``budget_verified=False``.
    """

    def __init__(
        self,
        *,
        initial_mode: EdgeRuntimeMode = EdgeRuntimeMode.ACTIVE,
        initial_reason_code: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._mode = initial_mode
        self._reason_code = redact_text(initial_reason_code) if initial_reason_code else None
        self._actor_id: str | None = None
        self._updated_at = clock()
        self._budget_verified = initial_mode is EdgeRuntimeMode.ACTIVE
        self._audit_log: list[PauseAuditEvent] = []

    @property
    def state(self) -> EdgePauseState:
        with self._lock:
            return EdgePauseState(
                mode=self._mode,
                reason_code=self._reason_code,
                actor_id=self._actor_id,
                updated_at=self._updated_at,
                budget_verified=self._budget_verified,
                transition_count=len(self._audit_log),
            )

    @property
    def audit_log(self) -> tuple[PauseAuditEvent, ...]:
        with self._lock:
            return tuple(self._audit_log)

    def pause(
        self,
        reason_code: str,
        actor_id: str,
        *,
        mode: EdgeRuntimeMode = EdgeRuntimeMode.PAUSED_COST_GUARD,
    ) -> EdgePauseState:
        """Manually pause high-cost or all non-essential runtime traffic."""
        cleaned_reason = redact_text(reason_code.strip())
        cleaned_actor = redact_text(actor_id.strip())
        if not cleaned_reason:
            raise ValueError("reason_code must be non-empty.")
        if not cleaned_actor:
            raise ValueError("actor_id must be non-empty.")
        if mode is EdgeRuntimeMode.ACTIVE:
            raise ValueError("pause() requires PAUSED_COST_GUARD or PAUSED_INCIDENT mode.")

        with self._lock:
            now = self._clock()
            previous = self._mode
            self._mode = mode
            self._reason_code = cleaned_reason
            self._actor_id = cleaned_actor
            self._updated_at = now
            self._budget_verified = False
            self._audit_log.append(
                PauseAuditEvent(
                    action="PAUSE",
                    from_mode=previous,
                    to_mode=mode,
                    reason_code=cleaned_reason,
                    actor_id=cleaned_actor,
                    budget_verified=False,
                    occurred_at=now,
                )
            )
            return EdgePauseState(
                mode=self._mode,
                reason_code=self._reason_code,
                actor_id=self._actor_id,
                updated_at=self._updated_at,
                budget_verified=self._budget_verified,
                transition_count=len(self._audit_log),
            )

    def resume(
        self,
        actor_id: str,
        *,
        budget_verified: bool = False,
        reason_code: str = "OPERATOR_BUDGET_VERIFIED_RESUME",
    ) -> EdgePauseState:
        """Resume normal operation ONLY when operator explicitly asserts budget_verified=True."""
        cleaned_actor = redact_text(actor_id.strip())
        if not cleaned_actor:
            raise ValueError("actor_id must be non-empty.")
        if budget_verified is not True:
            raise ManualResumeRejectedError(
                "Cannot resume edge runtime while budget_verified is False (ADR-020 / OD-015)."
            )
        cleaned_reason = redact_text(reason_code.strip()) or "OPERATOR_BUDGET_VERIFIED_RESUME"

        with self._lock:
            now = self._clock()
            previous = self._mode
            self._mode = EdgeRuntimeMode.ACTIVE
            self._reason_code = cleaned_reason
            self._actor_id = cleaned_actor
            self._updated_at = now
            self._budget_verified = True
            self._audit_log.append(
                PauseAuditEvent(
                    action="RESUME",
                    from_mode=previous,
                    to_mode=EdgeRuntimeMode.ACTIVE,
                    reason_code=cleaned_reason,
                    actor_id=cleaned_actor,
                    budget_verified=True,
                    occurred_at=now,
                )
            )
            return EdgePauseState(
                mode=self._mode,
                reason_code=self._reason_code,
                actor_id=self._actor_id,
                updated_at=self._updated_at,
                budget_verified=self._budget_verified,
                transition_count=len(self._audit_log),
            )

    def evaluate_route(
        self, classification: RouteClassification
    ) -> tuple[bool, EdgeDenialCode | None]:
        """Return (allowed, denial_code) for a classified route under the current pause state."""
        with self._lock:
            current_mode = self._mode

        if current_mode is EdgeRuntimeMode.ACTIVE:
            return True, None

        if classification.pause_policy is EdgePausePolicy.ESSENTIAL_SAFETY_ALWAYS_ALLOWED:
            return True, None

        if current_mode is EdgeRuntimeMode.PAUSED_COST_GUARD:
            if classification.pause_policy is EdgePausePolicy.ALLOWED_IN_COST_GUARD:
                return True, None
            return False, EdgeDenialCode.RUNTIME_PAUSED_COST_GUARD

        # PAUSED_INCIDENT blocks everything except essential safety & user-rights routes.
        return False, EdgeDenialCode.RUNTIME_PAUSED_INCIDENT
