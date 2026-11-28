"""Typed configuration, security errors, and wire models for the hosted Cloud Run runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlsplit

from career_memory_api.auth.jwt_verifier import SUPPORTED_ASYMMETRIC_ALGORITHMS
from career_memory_api.edge_guard.models import EdgeRuntimeCaps
from career_memory_api.fullstack_runtime.models import BootstrapResourceSummary

FORBIDDEN_RUNTIME_DB_ROLES: frozenset[str] = frozenset(
    {
        "postgres",
        "supabase_admin",
        "supabase_auth_admin",
        "supabase_storage_admin",
        "authenticator",
        "anon",
        "authenticated",
        "service_role",
        "cm_migrator",
    }
)

FORBIDDEN_MIGRATION_DB_ROLES: frozenset[str] = frozenset(
    {
        "cm_api_app",
        "cm_worker_app",
        "anon",
        "authenticated",
        "authenticator",
    }
)

ALLOWED_TLS_SSLMODES: frozenset[str] = frozenset({"require", "verify-ca", "verify-full"})


class HostedRoleSecurityError(RuntimeError):
    """Raised when a hosted runtime attempts to connect with a privileged or disallowed DB role."""


class HostedConfigurationError(ValueError):
    """Raised when hosted runtime configuration violates security, TLS, or pool caps."""


class HostedServiceRole(StrEnum):
    """Supported Cloud Run service roles built from the single multi-role API/Worker image."""

    API = "api"
    WORKER = "worker"

    @classmethod
    def from_env(cls, raw: str | None) -> HostedServiceRole:
        cleaned = (raw or "api").strip().lower()
        if cleaned == "api":
            return cls.API
        if cleaned == "worker":
            return cls.WORKER
        raise HostedConfigurationError(
            f"Unsupported CAREER_MEMORY_SERVICE_ROLE={raw!r}. Expected 'api' or 'worker'."
        )


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    cleaned = raw.strip().lower()
    if cleaned in ("1", "true", "yes", "on"):
        return True
    if cleaned in ("0", "false", "no", "off"):
        return False
    raise HostedConfigurationError(f"Invalid boolean env var {name}={raw!r}.")


def _parse_csv_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def extract_dsn_username(database_url: str) -> str:
    """Extract the PostgreSQL username from a connection URI without leaking password."""
    parsed = urlsplit(database_url.strip())
    if parsed.scheme.lower() not in ("postgres", "postgresql"):
        raise HostedConfigurationError(
            "database_url must use the 'postgresql://' or 'postgres://' scheme."
        )
    if not parsed.hostname:
        raise HostedConfigurationError("database_url must include a non-empty hostname.")
    if not parsed.username:
        raise HostedConfigurationError("database_url must specify an explicit DB username.")
    return parsed.username.strip()


def extract_base_role_name(username: str) -> str:
    """Return the base role name (handling Supavisor '<role>.<project_ref>' usernames)."""
    cleaned = username.strip().lower()
    if "." in cleaned:
        return cleaned.split(".", 1)[0]
    return cleaned


@dataclass(frozen=True, slots=True)
class HostedDbPoolConfig:
    """Bounded Supabase Supavisor Transaction Pooler (:6543) pool configuration (OD-006)."""

    database_url: str = field(repr=False)
    min_size: int = 0
    max_size: int = 2
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 10_000
    lock_timeout_ms: int = 3_000
    require_tls: bool = True
    allow_insecure_local_test: bool = False
    expected_role_group: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise HostedConfigurationError("database_url must be a non-empty string.")
        if self.min_size < 0:
            raise HostedConfigurationError("min_size must be >= 0.")
        if not (1 <= self.max_size <= 4):
            raise HostedConfigurationError(
                f"max_size must be between 1 and 4 for Supabase Free Supavisor safety (got {self.max_size})."
            )
        if self.min_size > self.max_size:
            raise HostedConfigurationError("min_size must be <= max_size.")
        if self.connect_timeout_seconds < 1:
            raise HostedConfigurationError("connect_timeout_seconds must be >= 1.")
        if self.statement_timeout_ms < 100:
            raise HostedConfigurationError("statement_timeout_ms must be >= 100.")
        if self.lock_timeout_ms < 100:
            raise HostedConfigurationError("lock_timeout_ms must be >= 100.")

        username = extract_dsn_username(self.database_url)
        base_role = extract_base_role_name(username)
        if (
            username.lower() in FORBIDDEN_RUNTIME_DB_ROLES
            or base_role in FORBIDDEN_RUNTIME_DB_ROLES
        ):
            raise HostedRoleSecurityError(
                f"Runtime pool refuses privileged or disallowed database role {username!r} (OD-006)."
            )

        parsed = urlsplit(self.database_url.strip())
        query_params = {k.lower(): v for k, v in parse_qs(parsed.query).items()}

        if parsed.port == 6543 and "options" in query_params:
            raise HostedConfigurationError(
                "Startup 'options' parameter is forbidden on Supavisor Transaction Pooler (:6543); "
                "use transaction-local SET LOCAL inside transactions instead."
            )

        if self.require_tls and not self.allow_insecure_local_test:
            sslmodes = [m.strip().lower() for m in query_params.get("sslmode", []) if m.strip()]
            if not sslmodes or sslmodes[-1] not in ALLOWED_TLS_SSLMODES:
                raise HostedConfigurationError(
                    "Hosted database_url requires TLS sslmode in {'require', 'verify-ca', 'verify-full'}."
                )


@dataclass(frozen=True, slots=True)
class HostedSessionPayload:
    """Read-only wire payload for ``GET /v1/session`` shared with ``apps/web`` BFF.

    Security invariant (OD-010): NEVER includes ``access_token`` or ``refresh_token``.
    """

    workspace_id: str
    workspace_name: str
    user_id: str
    user_display_name: str
    user_email: str
    github_login: str
    connected_account_id: str | None
    resources: tuple[BootstrapResourceSummary, ...]
    default_source_resource_id: str | None
    default_confirmed_event_id: str | None
    mode: str = "hosted_supabase"
    sample_repositories: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "user_id": self.user_id,
            "user_display_name": self.user_display_name,
            "user_email": self.user_email,
            "github_login": self.github_login,
            "connected_account_id": self.connected_account_id,
            "resources": [r.to_dict() for r in self.resources],
            "default_source_resource_id": self.default_source_resource_id,
            "default_confirmed_event_id": self.default_confirmed_event_id,
            "sample_repositories": list(self.sample_repositories),
        }


@dataclass(frozen=True, slots=True)
class HostedApiServiceConfig:
    """Configuration for the hosted Cloud Run API service (``cm-api``)."""

    db_pool: HostedDbPoolConfig
    jwt_issuer: str
    jwks_url: str
    jwt_audience: str = "authenticated"
    allowed_jwt_algorithms: frozenset[str] = field(
        default_factory=lambda: SUPPORTED_ASYMMETRIC_ALGORITHMS
    )
    supabase_auth_url: str | None = None
    supabase_anon_key: str | None = field(default=None, repr=False)
    jwks_cache_ttl_seconds: float = 300.0
    auth_http_timeout_seconds: float = 3.0
    require_origin_auth: bool = True
    origin_hmac_secrets: tuple[str, ...] = field(default=(), repr=False)
    allow_run_app_host_with_origin_proof: bool = True
    allowed_web_origins: frozenset[str] = field(
        default_factory=lambda: frozenset({"https://app.career-memory.example"})
    )
    allowed_redirect_uris: tuple[str, ...] = (
        "https://app.career-memory.example/api/auth/github/callback",
    )
    worker_wake_url: str | None = None
    worker_wake_audience: str | None = None
    worker_wake_timeout_seconds: float = 2.0
    worker_hmac_secret: str | None = field(default=None, repr=False)
    github_client_id: str | None = None
    github_client_secret: str | None = field(default=None, repr=False)
    github_public_read_token: str | None = field(default=None, repr=False)
    allow_http_loopback_for_tests: bool = False
    edge_caps: EdgeRuntimeCaps = field(default_factory=EdgeRuntimeCaps)

    def __post_init__(self) -> None:
        if not self.jwt_issuer.strip():
            raise HostedConfigurationError("jwt_issuer must be non-empty.")
        if not self.jwks_url.strip():
            raise HostedConfigurationError("jwks_url must be non-empty.")
        if not self.jwt_audience.strip():
            raise HostedConfigurationError("jwt_audience must be non-empty.")
        if self.jwks_cache_ttl_seconds <= 0:
            raise HostedConfigurationError("jwks_cache_ttl_seconds must be > 0.")
        if self.auth_http_timeout_seconds <= 0:
            raise HostedConfigurationError("auth_http_timeout_seconds must be > 0.")
        if self.worker_wake_timeout_seconds <= 0:
            raise HostedConfigurationError("worker_wake_timeout_seconds must be > 0.")

    @classmethod
    def from_env(cls) -> HostedApiServiceConfig:
        db_url = (os.environ.get("API_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
        if not db_url:
            raise HostedConfigurationError(
                "API_DATABASE_URL or DATABASE_URL must be set for hosted API service."
            )
        allow_insecure = _parse_bool_env("CAREER_MEMORY_ALLOW_INSECURE_LOCAL_TEST", False)
        max_pool = int(os.environ.get("CAREER_MEMORY_DB_MAX_POOL_SIZE", "2"))
        min_pool = int(os.environ.get("CAREER_MEMORY_DB_MIN_POOL_SIZE", "0"))
        stmt_timeout_ms = int(os.environ.get("CAREER_MEMORY_DB_STATEMENT_TIMEOUT_MS", "10000"))
        lock_timeout_ms = int(os.environ.get("CAREER_MEMORY_DB_LOCK_TIMEOUT_MS", "3000"))

        db_pool = HostedDbPoolConfig(
            database_url=db_url,
            min_size=min_pool,
            max_size=max_pool,
            statement_timeout_ms=stmt_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            require_tls=not allow_insecure,
            allow_insecure_local_test=allow_insecure,
            expected_role_group="cm_api_app",
        )

        jwt_issuer = (os.environ.get("SUPABASE_JWT_ISSUER") or "").strip()
        jwks_url = (os.environ.get("SUPABASE_JWKS_URL") or "").strip()
        if not jwks_url and jwt_issuer:
            jwks_url = f"{jwt_issuer.rstrip('/')}/.well-known/jwks.json"
        if not jwt_issuer or not jwks_url:
            raise HostedConfigurationError(
                "SUPABASE_JWT_ISSUER and SUPABASE_JWKS_URL must be set for hosted API service."
            )

        web_origins = _parse_csv_env("CAREER_MEMORY_ALLOWED_WEB_ORIGINS")
        redirect_uris = _parse_csv_env("CAREER_MEMORY_GITHUB_REDIRECT_URIS")
        origin_secrets = _parse_csv_env("CAREER_MEMORY_ORIGIN_HMAC_SECRETS")
        algs_raw = _parse_csv_env("SUPABASE_JWT_ALLOWED_ALGORITHMS")
        allowed_algs = frozenset(algs_raw) if algs_raw else SUPPORTED_ASYMMETRIC_ALGORITHMS

        return cls(
            db_pool=db_pool,
            jwt_issuer=jwt_issuer,
            jwks_url=jwks_url,
            jwt_audience=(os.environ.get("SUPABASE_JWT_AUDIENCE") or "authenticated").strip(),
            allowed_jwt_algorithms=allowed_algs,
            supabase_auth_url=(os.environ.get("SUPABASE_AUTH_URL") or "").strip() or None,
            supabase_anon_key=(os.environ.get("SUPABASE_ANON_KEY") or "").strip() or None,
            require_origin_auth=_parse_bool_env("CAREER_MEMORY_REQUIRE_ORIGIN_AUTH", True),
            origin_hmac_secrets=origin_secrets,
            allow_run_app_host_with_origin_proof=_parse_bool_env(
                "CAREER_MEMORY_ALLOW_RUN_APP_ORIGIN_PROOF", True
            ),
            allowed_web_origins=(
                frozenset(web_origins)
                if web_origins
                else frozenset({"https://app.career-memory.example"})
            ),
            allowed_redirect_uris=(
                redirect_uris
                if redirect_uris
                else ("https://app.career-memory.example/api/auth/github/callback",)
            ),
            worker_wake_url=(os.environ.get("CAREER_MEMORY_WORKER_WAKE_URL") or "").strip() or None,
            worker_wake_audience=(
                (os.environ.get("CAREER_MEMORY_WORKER_WAKE_AUDIENCE") or "").strip() or None
            ),
            worker_wake_timeout_seconds=float(
                os.environ.get("CAREER_MEMORY_WORKER_WAKE_TIMEOUT_SECONDS", "2.0")
            ),
            worker_hmac_secret=(os.environ.get("CAREER_MEMORY_WORKER_HMAC_SECRET") or "").strip()
            or None,
            github_client_id=(os.environ.get("GITHUB_CLIENT_ID") or "").strip() or None,
            github_client_secret=(os.environ.get("GITHUB_CLIENT_SECRET") or "").strip() or None,
            github_public_read_token=(os.environ.get("GITHUB_PUBLIC_READ_TOKEN") or "").strip()
            or None,
            allow_http_loopback_for_tests=allow_insecure,
        )


@dataclass(frozen=True, slots=True)
class HostedWorkerServiceConfig:
    """Configuration for the private IAM Cloud Run Worker service (``cm-worker``)."""

    db_pool: HostedDbPoolConfig
    allowed_worker_audiences: frozenset[str] = field(
        default_factory=lambda: frozenset({"https://worker.internal.career-memory.local"})
    )
    allowed_worker_issuers: frozenset[str] = field(
        default_factory=lambda: frozenset({"https://accounts.google.com", "accounts.google.com"})
    )
    allowed_worker_service_accounts: frozenset[str] = field(default_factory=frozenset)
    google_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    worker_hmac_secret: str | None = field(default=None, repr=False)
    worker_id: str = "cm-hosted-worker"
    default_workspace_ids: tuple[str, ...] = ()
    max_slices_per_wake: int = 5
    worker_request_slice_timeout_seconds: float = 180.0
    lease_duration_seconds: int = 210
    github_public_read_token: str | None = field(default=None, repr=False)
    allow_http_loopback_for_tests: bool = False
    edge_caps: EdgeRuntimeCaps = field(default_factory=EdgeRuntimeCaps)

    def __post_init__(self) -> None:
        if self.db_pool.max_size > 2:
            raise HostedConfigurationError(
                f"Worker DB pool max_size must be <= 2 (got {self.db_pool.max_size})."
            )
        if not self.allowed_worker_audiences:
            raise HostedConfigurationError("allowed_worker_audiences must not be empty.")
        if self.max_slices_per_wake < 1:
            raise HostedConfigurationError("max_slices_per_wake must be >= 1.")
        if self.worker_request_slice_timeout_seconds <= 0:
            raise HostedConfigurationError("worker_request_slice_timeout_seconds must be > 0.")
        if self.lease_duration_seconds < 10:
            raise HostedConfigurationError("lease_duration_seconds must be >= 10.")

    @classmethod
    def from_env(cls) -> HostedWorkerServiceConfig:
        db_url = (
            os.environ.get("WORKER_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
        ).strip()
        if not db_url:
            raise HostedConfigurationError(
                "WORKER_DATABASE_URL or DATABASE_URL must be set for hosted worker service."
            )
        allow_insecure = _parse_bool_env("CAREER_MEMORY_ALLOW_INSECURE_LOCAL_TEST", False)
        max_pool = int(os.environ.get("CAREER_MEMORY_WORKER_DB_MAX_POOL_SIZE", "2"))
        min_pool = int(os.environ.get("CAREER_MEMORY_WORKER_DB_MIN_POOL_SIZE", "0"))
        stmt_timeout_ms = int(os.environ.get("CAREER_MEMORY_DB_STATEMENT_TIMEOUT_MS", "15000"))
        lock_timeout_ms = int(os.environ.get("CAREER_MEMORY_DB_LOCK_TIMEOUT_MS", "3000"))

        db_pool = HostedDbPoolConfig(
            database_url=db_url,
            min_size=min_pool,
            max_size=max_pool,
            statement_timeout_ms=stmt_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            require_tls=not allow_insecure,
            allow_insecure_local_test=allow_insecure,
            expected_role_group="cm_worker_app",
        )

        audiences = _parse_csv_env("CAREER_MEMORY_WORKER_AUDIENCES")
        issuers = _parse_csv_env("CAREER_MEMORY_WORKER_ISSUERS")
        service_accounts = _parse_csv_env("CAREER_MEMORY_WORKER_SERVICE_ACCOUNTS")
        workspace_ids = _parse_csv_env("CAREER_MEMORY_WORKER_WORKSPACE_IDS")
        slice_timeout = float(os.environ.get("CAREER_MEMORY_WORKER_SLICE_TIMEOUT_SECONDS", "180.0"))

        return cls(
            db_pool=db_pool,
            allowed_worker_audiences=(
                frozenset(audiences)
                if audiences
                else frozenset({"https://worker.internal.career-memory.local"})
            ),
            allowed_worker_issuers=(
                frozenset(issuers)
                if issuers
                else frozenset({"https://accounts.google.com", "accounts.google.com"})
            ),
            allowed_worker_service_accounts=frozenset(sa.lower() for sa in service_accounts),
            google_jwks_url=(
                os.environ.get("CAREER_MEMORY_GOOGLE_JWKS_URL")
                or "https://www.googleapis.com/oauth2/v3/certs"
            ).strip(),
            worker_hmac_secret=(os.environ.get("CAREER_MEMORY_WORKER_HMAC_SECRET") or "").strip()
            or None,
            worker_id=(os.environ.get("CAREER_MEMORY_WORKER_ID") or "cm-hosted-worker").strip(),
            default_workspace_ids=workspace_ids,
            max_slices_per_wake=int(os.environ.get("CAREER_MEMORY_WORKER_MAX_SLICES", "5")),
            worker_request_slice_timeout_seconds=slice_timeout,
            lease_duration_seconds=int(os.environ.get("CAREER_MEMORY_WORKER_LEASE_SECONDS", "210")),
            github_public_read_token=(os.environ.get("GITHUB_PUBLIC_READ_TOKEN") or "").strip()
            or None,
            allow_http_loopback_for_tests=allow_insecure,
            edge_caps=EdgeRuntimeCaps(worker_request_slice_timeout_seconds=slice_timeout),
        )


__all__ = [
    "ALLOWED_TLS_SSLMODES",
    "FORBIDDEN_MIGRATION_DB_ROLES",
    "FORBIDDEN_RUNTIME_DB_ROLES",
    "HostedApiServiceConfig",
    "HostedConfigurationError",
    "HostedDbPoolConfig",
    "HostedRoleSecurityError",
    "HostedServiceRole",
    "HostedSessionPayload",
    "HostedWorkerServiceConfig",
    "extract_base_role_name",
    "extract_dsn_username",
]
