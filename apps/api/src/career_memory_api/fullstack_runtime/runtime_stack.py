"""Dual-mode PostgreSQL 18 Fullstack Runtime & Local Dev FastAPI App Factory.

IMPORTANT SECURITY BOUNDARY:
- ``career_memory_api.app:create_app()`` and ``career_memory_api.main:app`` remain
  100% fail-closed (``UnconfiguredWorkspaceAccessProvider``).
- ``GET /local-dev/bootstrap`` is mounted EXCLUSIVELY by
  ``create_local_dev_fullstack_app()`` and NEVER on ``create_app()`` or
  ``create_m1_contract_app()`` (so ``contracts/openapi/career-memory-v1.openapi.json``
  never gains local-dev routes).
- Every ``/v1/*`` request executes real ES256 cryptographic JWT verification
  (``JwtAccessTokenVerifier``) + real PostgreSQL 18 ``FORCE RLS`` membership checks
  (``PostgresWorkspaceAccessProvider`` under ``cm_api_app`` ``NOBYPASSRLS``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Request, Response

from career_memory_api.app import create_app
from career_memory_api.auth.jwt_verifier import (
    JwkKey,
    JwksSnapshot,
    JwtAccessTokenVerifier,
    JwtVerificationConfig,
)
from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.auth.postgres_guard import (
    PostgresWorkspaceAccessProvider,
    workspace_scoped_transaction,
)
from career_memory_api.authz import (
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.backfill_engine.postgres_page_committer import (
    PostgresBackfillPageCommitter,
)
from career_memory_api.budgets import (
    DefaultQuotaPolicyConfig,
    PostgresQuotaBudgetService,
    QuotaMetricLimit,
    create_budgets_router,
)
from career_memory_api.canonicalization import build_span_locator
from career_memory_api.claims import PostgresClaimService, create_claims_router
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_PACKAGE_VERSION,
    FactPromotionDecision,
)
from career_memory_api.conformance.checks import default_repo_root
from career_memory_api.conformance.db_catalog import FULL_M1_MIGRATIONS, resolve_pg18_bin
from career_memory_api.contracts_api.models import PRIVATE_NO_STORE_HEADER
from career_memory_api.contracts_api.router import (
    PostgresContractBoundaryService,
    create_contracts_router,
)
from career_memory_api.deletion import (
    EphemeralCacheRegistry,
    PostgresDeletionService,
    create_deletion_router,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_workspace_access_guard,
)
from career_memory_api.event_candidates.postgres_builder import (
    PostgresEventCandidateBuilder,
)
from career_memory_api.exports import PostgresExportService, create_exports_router
from career_memory_api.facts import PostgresFactService, create_facts_router
from career_memory_api.fullstack_runtime.models import (
    BootstrapResourceSummary,
    FullstackRuntimeConfig,
    GitHubProviderMode,
    LocalDevBootstrapPayload,
    SampleRepositoryDescriptor,
)
from career_memory_api.fullstack_runtime.sample_github_provider import (
    LivePublicReadGitHubCredentialProvider,
    SampleGitHubCredentialProvider,
    SampleGitHubWireTransport,
    SamplePublicGitHubCatalog,
    build_live_oauth_credential_provider,
)
from career_memory_api.github_connect import (
    PostgresGitHubConnectService,
    create_github_connect_router,
)
from career_memory_api.github_connect.ports import GitHubCredentialProviderPort
from career_memory_api.github_provider.import_client import HttpGitHubImportClient
from career_memory_api.github_provider.resolver_adapter import (
    GitHubResolverContentFetcher,
)
from career_memory_api.github_provider.transport import (
    GITHUB_API_ALLOWED_HOSTS,
    SafeGitHubHttpTransport,
    UrllibGitHubWireTransport,
)
from career_memory_api.identities import PostgresIdentityService, create_identities_router
from career_memory_api.initial_import.derived_units import DerivedUnitIngestionProcessor
from career_memory_api.initial_import.orchestrator import InitialBackfillOrchestrator
from career_memory_api.initial_import.worker_runner import InitialBackfillJobWorker
from career_memory_api.jobs import PostgresJobRuntime
from career_memory_api.jobs.models import WakeSignal
from career_memory_api.llm_gate import (
    ExecutionOrigin,
    LlmProviderMode,
    RawProviderStageResponse,
    StageAdapterInput,
    StageBudgetPolicy,
)
from career_memory_api.resolver import (
    EndpointSecurityPolicy,
    OperationBudgetConfig,
    PersistedUnitResolutionTarget,
    RefetchResolverService,
    ResolutionMetadata,
    create_resolver_router,
)
from career_memory_api.resolver.postgres_store import PostgresProvenanceMetadataStore
from career_memory_api.schema_contracts.enums import (
    FactStatus,
    FactType,
    QuotaMetricKind,
    SupportType,
    SupportValidator,
)
from career_memory_api.search import PostgresSearchIndexer, PostgresSearchService
from career_memory_api.sync_runs import (
    PostgresSyncRunService,
    create_sync_runs_router,
    install_sync_runs_error_handlers,
)
from career_memory_api.work_events import (
    ConfirmWorkEventRequest,
    EditWorkEventRequest,
    PostgresWorkEventMutationService,
    PostgresWorkEventsReadService,
    create_work_events_router,
)

logger = logging.getLogger(__name__)

# NIST P-256 (secp256r1) curve constants for pure-Python local ES256 JWT issuance
_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = _P256_P - 3
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

LOCAL_JWT_ISSUER = "https://local-dev.career-memory.internal/auth/v1"
LOCAL_JWT_AUDIENCE = "career-memory-api"

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _p256_add(
    p1: tuple[int, int] | None, p2: tuple[int, int] | None
) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P256_P == 0:
            return None
        lam = ((3 * x1 * x1 + _P256_A) * pow(2 * y1, -1, _P256_P)) % _P256_P
    else:
        lam = ((y2 - y1) * pow(x2 - x1, -1, _P256_P)) % _P256_P
    x3 = (lam * lam - x1 - x2) % _P256_P
    y3 = (lam * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _p256_mul(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    res: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    scalar = k % _P256_N
    while scalar > 0:
        if scalar & 1:
            res = _p256_add(res, addend)
        addend = _p256_add(addend, addend)
        scalar >>= 1
    return res


@dataclass(frozen=True)
class LocalEs256KeyPair:
    """Ephemeral P-256 signing keypair used to issue real ES256 JWTs for local dev."""

    kid: str
    d: int
    x: int
    y: int

    @classmethod
    def generate(cls, kid: str = "cm-local-es256-v1") -> LocalEs256KeyPair:
        d = secrets.randbelow(_P256_N - 1) + 1
        pub = _p256_mul(d, (_P256_GX, _P256_GY))
        assert pub is not None
        return cls(kid=kid, d=d, x=pub[0], y=pub[1])

    def to_jwk(self) -> JwkKey:
        return JwkKey.from_dict(
            {
                "kty": "EC",
                "crv": "P-256",
                "alg": "ES256",
                "use": "sig",
                "kid": self.kid,
                "x": _b64url_encode(self.x.to_bytes(32, "big")),
                "y": _b64url_encode(self.y.to_bytes(32, "big")),
            }
        )

    def sign_jwt(self, payload: Mapping[str, Any]) -> str:
        header = {"alg": "ES256", "typ": "JWT", "kid": self.kid}
        h_b64 = _b64url_encode(
            json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        p_b64 = _b64url_encode(
            json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        z = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
        while True:
            k = secrets.randbelow(_P256_N - 1) + 1
            pt = _p256_mul(k, (_P256_GX, _P256_GY))
            if pt is None:
                continue
            r = pt[0] % _P256_N
            if r == 0:
                continue
            s = (pow(k, -1, _P256_N) * (z + r * self.d)) % _P256_N
            if s != 0:
                break
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"


class _StaticJwksProvider:
    def __init__(self, key: JwkKey) -> None:
        self._key = key

    def get_jwks(self, *, force_refresh: bool = False) -> JwksSnapshot:
        del force_refresh
        now = datetime.now(UTC)
        return JwksSnapshot(
            keys=(self._key,),
            fetched_at=now,
            expires_at=now + timedelta(hours=24),
        )


class PooledPostgresWorkspaceAccessProvider:
    """Thread-safe ``WorkspaceAccessProvider`` delegating to ``PostgresWorkspaceAccessProvider``."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        token_verifier: JwtAccessTokenVerifier,
    ) -> None:
        self._connection_factory = connection_factory
        self._token_verifier = token_verifier

    def verify_identity(self, credentials: AuthCredentials) -> AuthenticatedUser | None:
        with self._connection_factory() as conn:
            return PostgresWorkspaceAccessProvider(
                conn, self._token_verifier
            ).verify_identity(credentials)

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        with self._connection_factory() as conn:
            return PostgresWorkspaceAccessProvider(
                conn, self._token_verifier
            ).verify_workspace_membership(user, target_workspace_id)


class PooledPostgresProvenanceMetadataStore:
    """Thread-safe Plan C provenance store delegating to ``PostgresProvenanceMetadataStore``."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def get_unit_resolution_target(
        self, access: WorkspaceAccessContext, content_unit_id: str
    ) -> PersistedUnitResolutionTarget | None:
        with self._connection_factory() as conn:
            return PostgresProvenanceMetadataStore(conn).get_unit_resolution_target(
                access, content_unit_id
            )

    def list_sibling_unit_anchors(
        self, access: WorkspaceAccessContext, source_object_revision_id: str
    ) -> tuple[PersistedUnitResolutionTarget, ...]:
        with self._connection_factory() as conn:
            return PostgresProvenanceMetadataStore(conn).list_sibling_unit_anchors(
                access, source_object_revision_id
            )

    def check_policy_fence_current(
        self, access: WorkspaceAccessContext, content_unit_id: str
    ) -> PersistedUnitResolutionTarget | None:
        with self._connection_factory() as conn:
            return PostgresProvenanceMetadataStore(conn).check_policy_fence_current(
                access, content_unit_id
            )

    def record_resolution_metadata(
        self, access: WorkspaceAccessContext, metadata: ResolutionMetadata
    ) -> None:
        with self._connection_factory() as conn:
            PostgresProvenanceMetadataStore(conn).record_resolution_metadata(
                access, metadata
            )


class _EventWakeNotifier:
    """Post-commit wake notifier that signals the local background worker thread."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def notify_job_available(self, signal: WakeSignal) -> None:
        del signal
        self._event.set()


class _DeterministicOfflineStageAdapter:
    """Zero-network Stage 2A/2B/3A offline adapter for local fullstack runs (₩0 spend)."""

    def __init__(self, handler: Callable[[StageAdapterInput], Any]) -> None:
        self._handler = handler

    @property
    def execution_origin(self) -> ExecutionOrigin:
        return "SYNTHETIC_FIXTURE"

    def execute_stage(self, adapter_input: StageAdapterInput) -> RawProviderStageResponse:
        return RawProviderStageResponse(
            payload=self._handler(adapter_input),
            stream_completed=True,
        )


def _build_local_claim_generator_adapter() -> _DeterministicOfflineStageAdapter:
    """Build a schema-valid Stage 3A draft directly from eligible SUPPORTED facts."""

    def _generate(inp: StageAdapterInput) -> dict[str, Any]:
        meta = inp.context_metadata
        eligible_ids = list(meta.get("eligible_fact_ids", []))
        fact_texts: list[str] = []
        try:
            manifest = json.loads(inp.ephemeral_source_text)
            if isinstance(manifest, dict) and isinstance(manifest.get("facts"), list):
                for item in manifest["facts"]:
                    if isinstance(item, dict) and isinstance(item.get("object_text"), str):
                        text = item["object_text"].strip()
                        if text:
                            fact_texts.append(text)
        except Exception:  # noqa: BLE001
            fact_texts = []

        if fact_texts:
            # Keep the primary fact statement verbatim so deterministic atomization +
            # lexical support validation bind cleanly with zero hallucinated metrics/roles.
            claim_text = fact_texts[0]
            if not claim_text.endswith("."):
                claim_text = f"{claim_text}."
        else:
            claim_text = "Completed verified engineering change backed by imported GitHub evidence."

        return {
            "schema_version": SCHEMA_PACKAGE_VERSION,
            "generator_run_id": str(meta["generator_run_id"]),
            "confirmed_event_id": str(meta["confirmed_event_id"]),
            "input_fact_ids": eligible_ids[:1] if eligible_ids else [],
            "claim_version": 1,
            "claim_text": claim_text,
            "status": "DRAFT",
        }

    return _DeterministicOfflineStageAdapter(_generate)


def _offline_stage_budget() -> StageBudgetPolicy:
    return StageBudgetPolicy(
        max_input_bytes=256 * 1024,
        max_calls=50,
        calls_used=0,
        max_repair_attempts=0,
        max_pre_spend_retries=0,
        overage_credit_disabled=True,
        quota_remaining_known_and_sufficient=True,
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ManagedPostgres18Database:
    """Manages either an existing PG18 ``DATABASE_URL`` or a local PG18 cluster."""

    def __init__(
        self,
        *,
        repo_root: Path,
        database_url: str | None = None,
        pgdata_dir: Path | None = None,
        dbname: str = "career_memory_local",
    ) -> None:
        self._repo_root = repo_root
        self._migrations_dir = repo_root / "db" / "migrations"
        self._external_url = database_url.strip() if database_url else None
        self._persistent_pgdata = pgdata_dir.resolve() if pgdata_dir is not None else None
        self._tmpdir: Path | None = None
        self._pgdata: Path | None = None
        self._socket_dir: Path | None = None
        self._logfile: Path | None = None
        self._started_local = False

        self.dbname = dbname
        self.port: int = 0
        self.superuser = "postgres"
        self.api_login = "cm_fullstack_api_login"
        self.api_password = "cm_fullstack_api_pw"
        self.worker_login = "cm_fullstack_worker_login"
        self.worker_password = "cm_fullstack_worker_pw"
        self.pg_version_label = ""

    def admin_dsn(self, dbname: str | None = None) -> str:
        if self._external_url is not None:
            return self._external_url
        target = dbname or self.dbname
        return f"postgresql://{self.superuser}@127.0.0.1:{self.port}/{target}"

    def api_dsn(self) -> str:
        if self._external_url is not None:
            return self._replace_dsn_credentials(
                self._external_url, self.api_login, self.api_password
            )
        return (
            f"postgresql://{self.api_login}:{self.api_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    def worker_dsn(self) -> str:
        if self._external_url is not None:
            return self._replace_dsn_credentials(
                self._external_url, self.worker_login, self.worker_password
            )
        return (
            f"postgresql://{self.worker_login}:{self.worker_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    @staticmethod
    def _replace_dsn_credentials(dsn: str, user: str, password: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(dsn)
        host = parts.hostname or "127.0.0.1"
        port_part = f":{parts.port}" if parts.port else ""
        netloc = f"{user}:{password}@{host}{port_part}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    @contextmanager
    def admin_connection(
        self, dbname: str | None = None, *, autocommit: bool = False
    ) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.admin_dsn(dbname), autocommit=autocommit) as conn:
            yield conn

    def api_connection_factory(self) -> ConnectionFactory:
        dsn = self.api_dsn()

        @contextmanager
        def _connect() -> Iterator[psycopg.Connection]:
            with psycopg.connect(dsn) as conn:
                yield conn

        return _connect

    def worker_connection_factory(self) -> ConnectionFactory:
        dsn = self.worker_dsn()

        @contextmanager
        def _connect() -> Iterator[psycopg.Connection]:
            with psycopg.connect(dsn) as conn:
                yield conn

        return _connect

    def start(self) -> str:
        if self._external_url is not None:
            self._verify_pg18_version()
            self._ensure_schema_and_roles()
            return self.pg_version_label

        initdb = resolve_pg18_bin("initdb")
        pg_ctl = resolve_pg18_bin("pg_ctl")
        createdb = resolve_pg18_bin("createdb")
        env = os.environ.copy()
        env["LC_ALL"] = "C"

        self.port = _find_free_port()
        if self._persistent_pgdata is not None:
            self._pgdata = self._persistent_pgdata
            self._pgdata.parent.mkdir(parents=True, exist_ok=True)
            self._socket_dir = Path(tempfile.mkdtemp(prefix="cm-pgsock-"))
            self._logfile = self._pgdata.parent / "postgres-local.log"
        else:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="cm-fullstack-pg18-"))
            self._pgdata = self._tmpdir / "pgdata"
            self._socket_dir = self._tmpdir
            self._logfile = self._tmpdir / "postgres.log"

        needs_init = not (self._pgdata / "PG_VERSION").is_file()
        if needs_init:
            subprocess.run(
                [
                    initdb,
                    "-D",
                    str(self._pgdata),
                    "-U",
                    self.superuser,
                    "--auth=trust",
                    "--encoding=UTF8",
                    "--no-locale",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

        # Write local runtime override config cleanly via postgresql.auto.conf
        auto_conf = self._pgdata / "postgresql.auto.conf"
        auto_conf.write_text(
            f"listen_addresses = '127.0.0.1'\n"
            f"port = {self.port}\n"
            f"unix_socket_directories = '{self._socket_dir}'\n"
            f"fsync = off\n"
            f"synchronous_commit = off\n"
            f"max_connections = 80\n",
            encoding="utf-8",
        )

        subprocess.run(
            [pg_ctl, "-D", str(self._pgdata), "-l", str(self._logfile), "-w", "start"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        self._started_local = True

        self._wait_for_ready()
        self._verify_pg18_version(dbname="postgres")

        # Create database if it does not yet exist
        with self.admin_connection("postgres", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (self.dbname,)
                )
                exists = cur.fetchone() is not None
        if not exists:
            subprocess.run(
                [
                    createdb,
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(self.port),
                    "-U",
                    self.superuser,
                    self.dbname,
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

        self._ensure_schema_and_roles()
        return self.pg_version_label

    def _wait_for_ready(self) -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_dsn("postgres"), connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        raise RuntimeError("Local PostgreSQL 18 cluster did not become ready in time.")

    def _verify_pg18_version(self, dbname: str | None = None) -> None:
        with self.admin_connection(dbname, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version_num")
                num_row = cur.fetchone()
                cur.execute("SHOW server_version")
                ver_row = cur.fetchone()
                if num_row is None or int(num_row[0]) < 180000:
                    raise RuntimeError(f"Expected PostgreSQL 18+, got {num_row!r}")
                self.pg_version_label = str(ver_row[0]) if ver_row else "18"

    def _ensure_schema_and_roles(self) -> None:
        with self.admin_connection(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.runtime_pause_controls') IS NOT NULL")
                already_migrated = bool((cur.fetchone() or (False,))[0])

        if not already_migrated:
            for mig_name in FULL_M1_MIGRATIONS:
                sql = (self._migrations_dir / mig_name).read_text(encoding="utf-8")
                with self.admin_connection(autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql)

        with self.admin_connection(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.api_login}') THEN
                            CREATE ROLE {self.api_login}
                                LOGIN PASSWORD '{self.api_password}'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.worker_login}') THEN
                            CREATE ROLE {self.worker_login}
                                LOGIN PASSWORD '{self.worker_password}'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        END IF;
                    END
                    $$;
                    GRANT cm_api_app TO {self.api_login};
                    GRANT cm_worker_app TO {self.worker_login};
                    """
                )

    def stop(self) -> None:
        if self._started_local and self._pgdata is not None and self._pgdata.exists():
            try:
                pg_ctl = resolve_pg18_bin("pg_ctl")
                subprocess.run(
                    [pg_ctl, "-D", str(self._pgdata), "-m", "fast", "stop"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                self._started_local = False

        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        elif self._socket_dir is not None and self._socket_dir.exists():
            shutil.rmtree(self._socket_dir, ignore_errors=True)
            self._socket_dir = None


class BackgroundPipelineWorker:
    """Stoppable daemon loop processing queued ``SYNC_RUN`` and ``deletion_jobs``."""

    def __init__(
        self,
        runtime: FullstackPostgresRuntime,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self._runtime = runtime
        self._poll_interval = max(0.05, float(poll_interval_seconds))
        self._stop_event = threading.Event()
        self._wake_event = runtime.wake_event
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="cm-fullstack-pipeline-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                did_work = self._runtime.run_pending_work_once()
            except Exception:  # noqa: BLE001
                logger.exception("BackgroundPipelineWorker iteration failed")
                did_work = False

            if did_work:
                continue
            self._wake_event.wait(timeout=self._poll_interval)
            self._wake_event.clear()


class FullstackPostgresRuntime:
    """End-to-end PostgreSQL 18 fullstack runtime wiring all M1-M5 domain services."""

    def __init__(self, config: FullstackRuntimeConfig | None = None) -> None:
        self.config = config or FullstackRuntimeConfig()
        self._repo_root = (
            self.config.repo_root.resolve()
            if self.config.repo_root is not None
            else default_repo_root()
        )
        self.db = ManagedPostgres18Database(
            repo_root=self._repo_root,
            database_url=self.config.database_url,
            pgdata_dir=self.config.pgdata_dir,
        )
        self.wake_event = threading.Event()
        self.secret_store = InMemorySecretStore()
        self.cache_registry = EphemeralCacheRegistry()
        self.catalog = SamplePublicGitHubCatalog()
        self.jwt_keypair = LocalEs256KeyPair.generate()
        self.token_verifier = JwtAccessTokenVerifier(
            config=JwtVerificationConfig(
                expected_issuer=LOCAL_JWT_ISSUER,
                expected_audience=LOCAL_JWT_AUDIENCE,
            ),
            jwks_provider=_StaticJwksProvider(self.jwt_keypair.to_jwk()),
        )

        self._work_lock = threading.Lock()
        self._started = False
        self._worker: BackgroundPipelineWorker | None = None

        # Populated during start()
        self.access_provider: PooledPostgresWorkspaceAccessProvider | None = None
        self.workspace_id: str = ""
        self.user_id: str = ""
        self.identity_id: str = ""
        self.access_token: str = ""
        self.access: WorkspaceAccessContext | None = None
        self.connected_account_id: str | None = None
        self.default_source_resource_id: str | None = None
        self.default_confirmed_event_id: str | None = None

        # Domain services populated during start()
        self.credential_provider: GitHubCredentialProviderPort | None = None
        self.github_transport: SafeGitHubHttpTransport | None = None
        self.github_import_client: HttpGitHubImportClient | None = None
        self.content_fetcher: GitHubResolverContentFetcher | None = None
        self.resolver: RefetchResolverService | None = None
        self.github_connect_service: PostgresGitHubConnectService | None = None
        self.sync_run_service: PostgresSyncRunService | None = None
        self.job_runtime: PostgresJobRuntime | None = None
        self.backfill_orchestrator: InitialBackfillOrchestrator | None = None
        self.derived_unit_processor: DerivedUnitIngestionProcessor | None = None
        self.event_candidate_builder: PostgresEventCandidateBuilder | None = None
        self.backfill_worker: InitialBackfillJobWorker | None = None
        self.search_indexer: PostgresSearchIndexer | None = None
        self.search_service: PostgresSearchService | None = None
        self.work_events_read_service: PostgresWorkEventsReadService | None = None
        self.work_events_mutation_service: PostgresWorkEventMutationService | None = None
        self.identity_service: PostgresIdentityService | None = None
        self.fact_service: PostgresFactService | None = None
        self.claim_service: PostgresClaimService | None = None
        self.export_service: PostgresExportService | None = None
        self.deletion_service: PostgresDeletionService | None = None
        self.budget_service: PostgresQuotaBudgetService | None = None
        self.contracts_service: PostgresContractBoundaryService | None = None

    def start(self) -> FullstackPostgresRuntime:
        if self._started:
            return self
        try:
            self.db.start()
            self._wire_services()
            self._seed_workspace_and_identity()
            if (
                self.config.github_mode is GitHubProviderMode.LOCAL_SAMPLE
                and self.config.seed_initial_workspace
                and not self.config.empty_workspace
            ):
                self._seed_local_sample_journey()
            elif (
                self.config.github_mode is GitHubProviderMode.LIVE_PUBLIC_GITHUB
                and not self.config.empty_workspace
            ):
                self._ensure_live_public_account()

            self._started = True
            if self.config.start_background_worker:
                self._worker = BackgroundPipelineWorker(
                    self,
                    poll_interval_seconds=self.config.worker_poll_interval_seconds,
                )
                self._worker.start()
            return self
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        self.db.stop()
        self._started = False

    def __enter__(self) -> FullstackPostgresRuntime:
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.stop()

    def _wire_services(self) -> None:
        api_factory = self.db.api_connection_factory()
        worker_factory = self.db.worker_connection_factory()
        self.access_provider = PooledPostgresWorkspaceAccessProvider(
            api_factory, self.token_verifier
        )

        # Configure GitHub provider mode
        security_policy = EndpointSecurityPolicy(
            allowed_hosts=GITHUB_API_ALLOWED_HOSTS | frozenset({"raw.githubusercontent.com"}),
            allow_redirects=True,
            max_redirects=3,
        )
        if self.config.github_mode is GitHubProviderMode.LOCAL_SAMPLE:
            wire_transport = SampleGitHubWireTransport(self.catalog)
            self.github_transport = SafeGitHubHttpTransport(
                wire_transport=wire_transport,
                secret_store=self.secret_store,
                security_policy=security_policy,
            )
            self.credential_provider = SampleGitHubCredentialProvider(
                self.catalog,
                external_account_id=self.config.github_actor_external_id,
                login=self.config.github_actor_login,
            )
        else:
            public_token_ref: str | None = None
            raw_pub_token = (
                self.config.github_public_read_token
                or os.environ.get("GITHUB_PUBLIC_READ_TOKEN")
                or ""
            ).strip()
            if raw_pub_token:
                public_token_ref = self.secret_store.store_secret(
                    "github-public-read", raw_pub_token
                )
            self.github_transport = SafeGitHubHttpTransport(
                wire_transport=UrllibGitHubWireTransport(),
                secret_store=self.secret_store,
                security_policy=security_policy,
            )
            client_id = (
                self.config.github_client_id or os.environ.get("GITHUB_CLIENT_ID") or ""
            ).strip()
            client_secret = (
                self.config.github_client_secret
                or os.environ.get("GITHUB_CLIENT_SECRET")
                or ""
            ).strip()
            if client_id and client_secret:
                self.credential_provider = build_live_oauth_credential_provider(
                    client_id=client_id,
                    client_secret=client_secret,
                    allowed_redirect_uris=self.config.allowed_redirect_uris,
                )
            else:
                self.credential_provider = LivePublicReadGitHubCredentialProvider(
                    transport=self.github_transport,
                    public_read_token_ref=public_token_ref,
                    external_account_id=self.config.github_actor_external_id,
                    login=self.config.github_actor_login,
                )

        self.github_import_client = HttpGitHubImportClient(
            transport=self.github_transport
        )
        self.content_fetcher = GitHubResolverContentFetcher(
            transport=self.github_transport,
            require_credentials=False,
        )
        provenance_store = PooledPostgresProvenanceMetadataStore(api_factory)
        self.resolver = RefetchResolverService(
            metadata_store=provenance_store,
            fetcher=self.content_fetcher,
            default_budget=OperationBudgetConfig(
                max_requests=200,
                max_total_bytes=64 * 1024 * 1024,
                max_unit_bytes=5 * 1024 * 1024,
            ),
            security_policy=security_policy,
        )

        self.github_connect_service = PostgresGitHubConnectService(
            connection_factory=api_factory,
            credential_provider=self.credential_provider,
            secret_store=self.secret_store,
            allowed_redirect_uris=self.config.allowed_redirect_uris,
        )
        self.sync_run_service = PostgresSyncRunService(
            connection_factory=api_factory,
            worker_connection_factory=worker_factory,
            wake_notifier=_EventWakeNotifier(self.wake_event),
        )
        self.job_runtime = PostgresJobRuntime(connection_factory=worker_factory)
        page_committer = PostgresBackfillPageCommitter(
            worker_connection_factory=worker_factory
        )
        self.backfill_orchestrator = InitialBackfillOrchestrator(
            worker_connection_factory=worker_factory,
            github_client=self.github_import_client,
            page_committer=page_committer,
            github_connect_service=self.github_connect_service,
        )
        self.derived_unit_processor = DerivedUnitIngestionProcessor(
            worker_connection_factory=worker_factory,
            content_fetcher=self.content_fetcher,
            resolver_service=self.resolver,
            security_policy=security_policy,
        )
        self.event_candidate_builder = PostgresEventCandidateBuilder(
            worker_connection_factory=worker_factory
        )
        self.backfill_worker = InitialBackfillJobWorker(
            job_runtime=self.job_runtime,
            orchestrator=self.backfill_orchestrator,
            derived_unit_processor=self.derived_unit_processor,
            event_candidate_builder=self.event_candidate_builder,
        )

        self.search_indexer = PostgresSearchIndexer(connection_factory=api_factory)
        self.search_service = PostgresSearchService(api_connection_factory=api_factory)
        self.work_events_read_service = PostgresWorkEventsReadService(
            api_connection_factory=api_factory
        )
        self.work_events_mutation_service = PostgresWorkEventMutationService(
            api_connection_factory=api_factory,
            search_indexer=self.search_indexer,
        )
        self.identity_service = PostgresIdentityService(
            api_connection_factory=api_factory
        )
        self.fact_service = PostgresFactService(
            api_connection_factory=api_factory,
            resolver=self.resolver,
            search_indexer=self.search_indexer,
        )
        self.claim_service = PostgresClaimService(
            api_connection_factory=api_factory,
            resolver=self.resolver,
            default_adapter=_build_local_claim_generator_adapter(),
            default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            search_indexer=self.search_indexer,
        )
        self.export_service = PostgresExportService(
            api_connection_factory=api_factory,
            resolver=self.resolver,
        )
        self.deletion_service = PostgresDeletionService(
            api_connection_factory=api_factory,
            worker_connection_factory=worker_factory,
            ephemeral_cache_registry=self.cache_registry,
        )
        quota_policy = DefaultQuotaPolicyConfig(
            limits={
                QuotaMetricKind.IMPORT_OBJECTS: QuotaMetricLimit(
                    soft_warning_limit=4000, hard_cutoff_limit=5000
                ),
                QuotaMetricKind.PROVIDER_REQUESTS: QuotaMetricLimit(
                    soft_warning_limit=400, hard_cutoff_limit=500
                ),
                QuotaMetricKind.REFETCH_BYTES: QuotaMetricLimit(
                    soft_warning_limit=50_000_000, hard_cutoff_limit=64_000_000
                ),
                QuotaMetricKind.EXPORT_BUILDS: QuotaMetricLimit(
                    soft_warning_limit=80, hard_cutoff_limit=100
                ),
                QuotaMetricKind.SEARCH_QUERIES: QuotaMetricLimit(
                    soft_warning_limit=800, hard_cutoff_limit=1000
                ),
                QuotaMetricKind.ESTIMATED_COST_KRW: QuotaMetricLimit(
                    soft_warning_limit=0, hard_cutoff_limit=0
                ),
                QuotaMetricKind.LLM_TOKENS: QuotaMetricLimit(
                    soft_warning_limit=0, hard_cutoff_limit=0
                ),
            }
        )
        self.budget_service = PostgresQuotaBudgetService(
            api_connection_factory=api_factory,
            policy_config=quota_policy,
        )
        self.contracts_service = PostgresContractBoundaryService(
            claims_service=self.claim_service,
            search_service=self.search_service,
            deletion_service=self.deletion_service,
        )

    def _seed_workspace_and_identity(self) -> None:
        now = datetime.now(UTC)
        auth_subject_str = f"local-dev-{self.config.github_actor_login}"

        with self.db.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.users
                        WHERE auth_issuer = %s AND auth_subject = %s
                        """,
                        (LOCAL_JWT_ISSUER, auth_subject_str),
                    )
                    user_row = cur.fetchone()
                    if user_row is not None:
                        user_id = str(user_row[0])
                    else:
                        user_id = str(uuid.uuid4())
                        cur.execute(
                            """
                            INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                            VALUES (%s::uuid, %s, %s, %s, %s)
                            """,
                            (
                                user_id,
                                LOCAL_JWT_ISSUER,
                                auth_subject_str,
                                self.config.user_email,
                                self.config.user_display_name,
                            ),
                        )

                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.workspaces
                        WHERE owner_user_id = %s::uuid
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (user_id,),
                    )
                    ws_row = cur.fetchone()
                    if ws_row is not None:
                        ws_id = str(ws_row[0])
                    else:
                        ws_id = str(uuid.uuid4())
                        cur.execute(
                            """
                            INSERT INTO public.workspaces (id, owner_user_id, name)
                            VALUES (%s::uuid, %s::uuid, %s)
                            """,
                            (ws_id, user_id, self.config.workspace_name),
                        )
                        cur.execute(
                            """
                            INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                            VALUES (%s::uuid, %s::uuid, 'OWNER')
                            ON CONFLICT (workspace_id, user_id) DO NOTHING
                            """,
                            (ws_id, user_id),
                        )

                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.identities
                        WHERE workspace_id = %s::uuid
                          AND canonical_user_id = %s::uuid
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (ws_id, user_id),
                    )
                    ident_row = cur.fetchone()
                    if ident_row is not None:
                        ident_id = str(ident_row[0])
                    else:
                        ident_id = str(uuid.uuid4())
                        cur.execute(
                            """
                            INSERT INTO public.identities (
                                id, workspace_id, display_name, identity_type,
                                canonical_user_id, status
                            ) VALUES (%s::uuid, %s::uuid, %s, 'PERSON', %s::uuid, 'ACTIVE')
                            """,
                            (ident_id, ws_id, self.config.user_display_name, user_id),
                        )
                        cur.execute(
                            """
                            INSERT INTO public.external_identity_mappings (
                                id, workspace_id, identity_id, provider, external_user_id,
                                username, verification_method, status, verified_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, 'GITHUB', %s,
                                %s, 'OAUTH_ACCOUNT', 'VERIFIED', %s
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_id,
                                ident_id,
                                self.config.github_actor_external_id,
                                self.config.github_actor_login,
                                now,
                            ),
                        )

        self.user_id = user_id
        self.workspace_id = ws_id
        self.identity_id = ident_id
        self.access = WorkspaceAccessContext(
            user=AuthenticatedUser(
                user_id=user_id,
                auth_subject=AuthSubject(issuer=LOCAL_JWT_ISSUER, subject=auth_subject_str),
            ),
            membership=WorkspaceMembership(
                workspace_id=ws_id, user_id=user_id, role="OWNER"
            ),
        )
        self.access_token = self.issue_access_token()

        # Initialize workspace quota ledger rows immediately
        assert self.budget_service is not None
        self.budget_service.get_quota_status(self.access)

    def issue_access_token(self, *, ttl_seconds: int = 86_400) -> str:
        """Issue a fresh cryptographically signed ES256 JWT for the seeded local user."""
        assert self.access is not None
        now_ts = int(datetime.now(UTC).timestamp())
        return self.jwt_keypair.sign_jwt(
            {
                "iss": LOCAL_JWT_ISSUER,
                "aud": LOCAL_JWT_AUDIENCE,
                "sub": self.access.user.auth_subject.subject,
                "iat": now_ts,
                "nbf": now_ts - 5,
                "exp": now_ts + ttl_seconds,
                "jti": str(uuid.uuid4()),
                "email": self.config.user_email,
                "name": self.config.user_display_name,
            }
        )

    def _ensure_connected_account(self) -> str:
        assert self.github_connect_service is not None and self.access is not None
        accounts = self.github_connect_service.list_source_accounts(self.access)
        for acct in accounts:
            if acct.status == "ACTIVE":
                self.connected_account_id = acct.id
                return acct.id

        redirect_uri = self.config.allowed_redirect_uris[0]
        start_res = self.github_connect_service.start_oauth(
            self.access,
            return_path="/",
            redirect_uri=redirect_uri,
        )
        cb_res = self.github_connect_service.complete_oauth_callback(
            self.access,
            idempotency_key=str(uuid.uuid4()),
            state=start_res.state,
            code="local-sample-oauth-code",
            redirect_uri=redirect_uri,
        )
        self.connected_account_id = cb_res.source_account.id
        return cb_res.source_account.id

    def _ensure_live_public_account(self) -> None:
        self._ensure_connected_account()

    def _seed_local_sample_journey(self) -> None:
        """Pre-connect sample repos and run real PG18 import + event/fact/claim seed on ledger-engine."""
        assert self.github_connect_service is not None
        assert self.sync_run_service is not None
        assert self.work_events_read_service is not None
        assert self.work_events_mutation_service is not None
        assert self.claim_service is not None
        assert self.search_indexer is not None
        assert self.access is not None

        account_id = self._ensure_connected_account()
        existing_resources = {
            r.name.lower(): r
            for r in self.github_connect_service.list_source_resources(self.access)
        }

        ledger_res = existing_resources.get("seoyeon-dev/ledger-engine")
        if ledger_res is None:
            ledger_res = self.github_connect_service.register_source_resource(
                self.access,
                source_account_id=account_id,
                repository_full_name="seoyeon-dev/ledger-engine",
            )

        search_res = existing_resources.get("seoyeon-dev/search-gateway")
        if search_res is None:
            self.github_connect_service.register_source_resource(
                self.access,
                source_account_id=account_id,
                repository_full_name="seoyeon-dev/search-gateway",
            )

        self.default_source_resource_id = ledger_res.id

        # Check if ledger-engine already has a completed SyncRun (e.g. persistent pgdata_dir)
        recovered = self.sync_run_service.recover_latest(
            self.access, source_resource_id=ledger_res.id
        )
        ledger_sync_run_id: str | None = None
        if not recovered.get("items"):
            receipt = self.sync_run_service.create_initial_import(
                self.access,
                idempotency_key=str(uuid.uuid4()),
                request_payload={
                    "source_resource_id": ledger_res.id,
                    "mode": "INITIAL_BACKFILL",
                },
            )
            ledger_sync_run_id = receipt.target_id
            # Run backfill + derived units + candidate builder first so we can curate/confirm PR #101
            # BEFORE extracting Facts (avoiding STALE invalidation from edit_work_event).
            assert self.backfill_worker is not None
            self.backfill_worker.claim_and_execute_next(
                self.workspace_id,
                worker_id="cm-local-fullstack-seed",
                access=self.access,
                run_derived_units=True,
                run_event_candidate_builder=True,
            )
        else:
            ledger_sync_run_id = str(recovered["items"][0]["target_id"])

        # Curate & confirm the first PR event on ledger-engine so /claim-review & /search
        # have rich live PG18 data immediately while leaving remaining events as CANDIDATE for /event-review!
        events_resp = self.work_events_read_service.list_work_events(
            self.access, source_resource_id=ledger_res.id, limit=50
        )
        confirmed_id: str | None = None
        for ev in events_resp.items:
            if ev.status.value == "CONFIRMED":
                confirmed_id = ev.event_id
                break

        if confirmed_id is None and events_resp.items:
            # Prefer PR #101 (webhook replay latency p99 180ms -> 42ms)
            target_ev = next(
                (e for e in events_resp.items if "101" in e.title),
                events_resp.items[0],
            )
            edited = self.work_events_mutation_service.edit_work_event(
                self.access,
                event_id=target_ev.event_id,
                request=EditWorkEventRequest(
                    expected_version=target_ev.version,
                    title="PR #101: Webhook HMAC Verification & Replay Latency Guard",
                    problem="Billing webhook replay verification spiked p99 latency to 180ms under retry bursts.",
                    action="Implemented constant-time HMAC-SHA256 verification and covering-index replay dedupe.",
                    outcome="Reduced webhook replay p99 latency from 180ms to 42ms with 100% idempotency ledger dedupe.",
                ),
                idempotency_key_header=str(uuid.uuid4()),
            )
            confirmed = self.work_events_mutation_service.confirm_work_event(
                self.access,
                event_id=edited.event_id,
                request=ConfirmWorkEventRequest(
                    expected_version=edited.version,
                    reason="Verified imported PR #101 evidence for local fullstack seed.",
                ),
                idempotency_key_header=str(uuid.uuid4()),
            )
            confirmed_id = confirmed.event_id

        # Extract deterministic SUPPORTED Facts across ledger-engine events (after PR #101 edit/confirm)
        if ledger_sync_run_id is not None:
            self._extract_deterministic_facts_for_sync_run(ledger_sync_run_id)

        # Generate a live SOURCE_VERIFIED claim for the confirmed event via Stage 3A/3B/3C
        if confirmed_id is not None:
            existing_claims = self.claim_service.list_claims(
                self.access, event_id=confirmed_id
            )
            if not existing_claims.items:
                from career_memory_api.claims.models import (
                    ClaimGenerationStatus,
                    GenerateEventClaimsRequest,
                )

                gen_res = self.claim_service.generate_event_claims(
                    self.access,
                    idempotency_key=str(uuid.uuid4()),
                    request=GenerateEventClaimsRequest(confirmed_event_id=confirmed_id),
                )
                if gen_res.generation_status is not ClaimGenerationStatus.SUCCEEDED:
                    raise RuntimeError(
                        f"Seed claim generation failed: {gen_res.generation_status} ({gen_res.reason_code})"
                    )

        self.default_confirmed_event_id = confirmed_id
        self.search_indexer.rebuild_workspace(self.workspace_id, access=self.access)

    def _extract_deterministic_facts_for_sync_run(self, sync_run_id: str) -> int:
        """Extract & validate Plan C grounded Facts for events produced by a completed SyncRun."""
        assert self.access is not None
        assert self.fact_service is not None
        assert self.resolver is not None
        api_factory = self.db.api_connection_factory()

        targets: list[tuple[str, str, str]] = []
        with api_factory() as conn:
            with workspace_scoped_transaction(conn, self.access) as cur:
                cur.execute(
                    """
                    SELECT sr.source_resource_id::text
                    FROM public.sync_runs sr
                    WHERE sr.workspace_id = %s::uuid
                      AND sr.id = %s::uuid
                    """,
                    (self.workspace_id, sync_run_id),
                )
                row = cur.fetchone()
                if row is None:
                    return 0
                source_resource_id = str(row[0])

                # Find primary-anchor content units for work_events belonging to this resource
                # that do not yet have any facts extracted.
                cur.execute(
                    """
                    SELECT DISTINCT ON (we.id)
                        we.id::text,
                        cu.id::text,
                        rev.id::text
                    FROM public.work_events we
                    JOIN public.source_objects so
                      ON so.workspace_id = we.workspace_id
                     AND so.id = we.primary_anchor_source_object_id
                    JOIN public.source_object_revisions rev
                      ON rev.workspace_id = so.workspace_id
                     AND rev.source_object_id = so.id
                     AND rev.is_tombstone = false
                    JOIN public.content_units cu
                      ON cu.workspace_id = rev.workspace_id
                     AND cu.source_object_revision_id = rev.id
                     AND cu.policy_state = 'ACTIVE'
                    WHERE we.workspace_id = %s::uuid
                      AND so.source_resource_id = %s::uuid
                      AND we.status IN ('CANDIDATE', 'CONFIRMED')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.facts f
                          WHERE f.workspace_id = we.workspace_id
                            AND f.event_id = we.id
                      )
                    ORDER BY we.id, cu.ordinal ASC
                    """,
                    (self.workspace_id, source_resource_id),
                )
                targets = [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]

        facts_created = 0
        for event_id, content_unit_id, revision_id in targets:
            try:
                fact_count = self._extract_and_validate_single_unit_fact(
                    event_id=event_id,
                    content_unit_id=content_unit_id,
                    revision_id=revision_id,
                )
                facts_created += fact_count
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Deterministic fact extraction skipped for event=%s unit=%s",
                    event_id,
                    content_unit_id,
                )
        return facts_created

    def _extract_and_validate_single_unit_fact(
        self,
        *,
        event_id: str,
        content_unit_id: str,
        revision_id: str,
    ) -> int:
        assert self.access is not None
        assert self.fact_service is not None
        assert self.resolver is not None

        # Derive a clean grounded summary sentence ephemerally from the resolved content unit
        with self.resolver.open_operation_session(
            self.access, operation_id=f"local-fact-extract-{content_unit_id}"
        ) as session:
            from career_memory_api.resolver.models import (
                RefetchRequestInput,
                ResolutionStatus,
            )

            with self.resolver.resolve_unit(
                session,
                RefetchRequestInput(
                    access=self.access,
                    content_unit_id=content_unit_id,
                    operation_id=f"local-fact-extract-{content_unit_id}",
                    include_context=False,
                ),
            ) as resolution:
                if (
                    resolution.metadata.status is not ResolutionStatus.MATCHED
                    or resolution.ephemeral_unit is None
                ):
                    return 0
                unit_text = resolution.ephemeral_unit.text
                unit_loc = build_span_locator(unit_text, 0, len(unit_text)).to_dict()
                # Pick first meaningful non-heading sentence/bullet from the canonical unit text
                candidate_lines = [
                    re.sub(r"^[-*#>\s]+", "", ln).strip()
                    for ln in unit_text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                summary_line = next(
                    (ln for ln in candidate_lines if len(ln) >= 16),
                    candidate_lines[0] if candidate_lines else unit_text.strip(),
                )
                summary_line = summary_line[:240].strip()
                if not summary_line:
                    return 0
                del unit_text

        s2a = _DeterministicOfflineStageAdapter(
            lambda inp: [
                {
                    "schema_version": SCHEMA_PACKAGE_VERSION,
                    "extractor_run_id": inp.context_metadata["extractor_run_id"],
                    "extractor_version": inp.context_metadata["extractor_version"],
                    "source_object_revision_id": revision_id,
                    "content_unit_id": content_unit_id,
                    "locator": unit_loc,
                    "fact_type": FactType.ACTION.value,
                    "predicate": "delivered_engineering_change",
                    "object_text": summary_line,
                    "normalized_value": {
                        "completion_state": "COMPLETED",
                        "scope": "INDIVIDUAL",
                        "environment_scope": "PRODUCTION",
                        "causal_basis": "UNSPECIFIED",
                    },
                    "status": FactStatus.CANDIDATE.value,
                    "subject_identity_id": self.identity_id,
                    "uncertainty_notes": None,
                }
            ]
        )
        ext = self.fact_service.extract_candidates_for_unit(
            self.access,
            event_id=event_id,
            content_unit_id=content_unit_id,
            budget=_offline_stage_budget(),
            adapter=s2a,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            operation_id=f"local-s2a-{content_unit_id}",
        )
        if not ext.persisted_fact_ids:
            return 0
        fact_id = ext.persisted_fact_ids[0]
        s2b = _DeterministicOfflineStageAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "validator_run_id": inp.context_metadata["validator_run_id"],
                "extractor_run_id": ext.extractor_run_id,
                "fact_candidate_id": fact_id,
                "source_object_revision_id": revision_id,
                "content_unit_id": content_unit_id,
                "verified_locator": unit_loc,
                "support_type": SupportType.DIRECT.value,
                "validator": SupportValidator.MODEL.value,
                "validator_version": inp.context_metadata["validator_version"],
                "promotion_decision": FactPromotionDecision.PROMOTE_SUPPORTED.value,
                "revision_and_locator_verified": True,
                "promoted_fact_id": fact_id,
                "rejection_reason": None,
            }
        )
        self.fact_service.validate_and_normalize_candidates(
            self.access,
            event_id=event_id,
            fact_ids=[fact_id],
            content_unit_id=content_unit_id,
            budget=_offline_stage_budget(),
            adapter=s2b,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            candidate_locator_by_fact_id={fact_id: unit_loc},
            operation_id=f"local-s2b-{content_unit_id}",
        )
        return 1

    def run_pending_work_once(self) -> bool:
        """Process one batch of pending ``SYNC_RUN`` jobs and ``deletion_jobs``."""
        assert self.backfill_worker is not None
        assert self.deletion_service is not None
        assert self.search_indexer is not None
        assert self.access is not None

        did_work = False
        with self._work_lock:
            # 1. Process all ready INITIAL_BACKFILL_SYNC jobs
            while True:
                step = self.backfill_worker.claim_and_execute_next(
                    self.workspace_id,
                    worker_id="cm-local-fullstack-worker",
                    access=self.access,
                    run_derived_units=True,
                    run_event_candidate_builder=True,
                )
                if step is None:
                    break
                did_work = True
                if step.execution.import_ready:
                    self._extract_deterministic_facts_for_sync_run(
                        step.execution.sync_run_id
                    )
                    self.search_indexer.rebuild_workspace(
                        self.workspace_id, access=self.access
                    )

            # 2. Process any pending / partial OD-003 deletion_jobs
            pending_deletion_ids: list[str] = []
            with self.db.worker_connection_factory()() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        from career_memory_api.jobs.postgres_runtime import (
                            bind_worker_workspace_context,
                        )

                        bind_worker_workspace_context(cur, self.workspace_id)
                        cur.execute(
                            """
                            SELECT id::text
                            FROM public.deletion_jobs
                            WHERE workspace_id = %s::uuid
                              AND status IN ('PENDING', 'PARTIAL')
                            ORDER BY requested_at ASC
                            """,
                            (self.workspace_id,),
                        )
                        pending_deletion_ids = [str(r[0]) for r in cur.fetchall()]

            for del_job_id in pending_deletion_ids:
                self.deletion_service.execute_deletion_job(
                    self.workspace_id,
                    del_job_id,
                    access=self.access,
                )
                did_work = True

        return did_work

    def build_bootstrap_payload(self) -> LocalDevBootstrapPayload:
        """Build the current local-dev bootstrap snapshot for ``GET /local-dev/bootstrap``."""
        assert self.github_connect_service is not None
        assert self.sync_run_service is not None
        assert self.work_events_read_service is not None
        assert self.access is not None

        accounts = self.github_connect_service.list_source_accounts(self.access)
        active_acct = next((a for a in accounts if a.status == "ACTIVE"), None)
        resources_raw = self.github_connect_service.list_source_resources(self.access)
        sample_by_name = {
            d.full_name.lower(): d for d in self.catalog.list_descriptors()
        }

        resource_summaries: list[BootstrapResourceSummary] = []
        for res in resources_raw:
            sample_desc = sample_by_name.get(res.name.lower())
            recovered = self.sync_run_service.recover_latest(
                self.access, source_resource_id=res.id
            )
            items = recovered.get("items") or []
            latest_id: str | None = None
            latest_status: str | None = None
            latest_proc: str | None = None
            latest_ready = False
            if items:
                first = items[0]
                latest_id = str(first.get("target_id") or "") or None
                imp = first.get("import_status") or {}
                latest_status = str(imp.get("status") or "") or None
                latest_proc = str(imp.get("processing_status") or "") or None
                latest_ready = bool(imp.get("import_ready"))

            resource_summaries.append(
                BootstrapResourceSummary(
                    source_resource_id=res.id,
                    source_account_id=res.source_account_id,
                    provider_resource_id=res.provider_resource_id,
                    repository_full_name=res.name,
                    canonical_url=res.canonical_url,
                    visibility=res.visibility,
                    status=res.status,
                    default_branch=sample_desc.default_branch if sample_desc else "main",
                    description=(
                        sample_desc.description
                        if sample_desc
                        else f"Allowlisted public GitHub repository ({res.name})"
                    ),
                    primary_language=sample_desc.primary_language if sample_desc else "Code",
                    estimated_commits=sample_desc.estimated_commits if sample_desc else 0,
                    estimated_pull_requests=sample_desc.estimated_pull_requests if sample_desc else 0,
                    latest_sync_run_id=latest_id,
                    latest_sync_run_status=latest_status,
                    latest_processing_status=latest_proc,
                    latest_import_ready=latest_ready,
                )
            )

        # Pick live default confirmed event ID (if deleted/changed, look up fresh)
        events_resp = self.work_events_read_service.list_work_events(
            self.access, limit=50
        )
        confirmed_id = next(
            (e.event_id for e in events_resp.items if e.status.value == "CONFIRMED"),
            None,
        )
        if confirmed_id is None and events_resp.items:
            confirmed_id = events_resp.items[0].event_id

        default_res_id = (
            resource_summaries[0].source_resource_id if resource_summaries else None
        )
        sample_descs: tuple[SampleRepositoryDescriptor, ...] = (
            self.catalog.list_descriptors()
            if self.config.github_mode is GitHubProviderMode.LOCAL_SAMPLE
            else ()
        )

        return LocalDevBootstrapPayload(
            mode=self.config.github_mode.value,
            workspace_id=self.workspace_id,
            workspace_name=self.config.workspace_name,
            user_id=self.user_id,
            user_display_name=self.config.user_display_name,
            user_email=self.config.user_email,
            github_login=self.config.github_actor_login,
            access_token=self.access_token,
            connected_account_id=active_acct.id if active_acct else self.connected_account_id,
            resources=tuple(resource_summaries),
            default_source_resource_id=default_res_id,
            default_confirmed_event_id=confirmed_id,
            sample_repositories=sample_descs,
        )


def _guard_access_resolver(request: Request) -> WorkspaceAccessContext:
    guard = get_workspace_access_guard(request)
    credentials = get_auth_credentials(authorization=request.headers.get("Authorization"))
    target_ws = (request.headers.get("X-Workspace-ID") or "").strip()
    return guard.require_workspace_access(
        credentials=credentials,
        target_workspace_id=target_ws,
    )


def create_local_dev_fullstack_app(runtime: FullstackPostgresRuntime) -> FastAPI:
    """Assemble the complete local PG18 Fullstack FastAPI app with real JWT + FORCE RLS.

    Mounts ``GET /local-dev/bootstrap`` ONLY on this explicit local dev app — NEVER on
    ``create_app()`` or ``create_m1_contract_app()``.
    """
    if not runtime._started:
        runtime.start()
    assert runtime.access_provider is not None
    assert runtime.github_connect_service is not None
    assert runtime.sync_run_service is not None
    assert runtime.work_events_read_service is not None
    assert runtime.work_events_mutation_service is not None
    assert runtime.identity_service is not None
    assert runtime.fact_service is not None
    assert runtime.claim_service is not None
    assert runtime.export_service is not None
    assert runtime.resolver is not None
    assert runtime.deletion_service is not None
    assert runtime.budget_service is not None
    assert runtime.contracts_service is not None

    app = create_app(
        access_provider=runtime.access_provider,
        sync_runs_router=create_sync_runs_router(
            service=runtime.sync_run_service,
            resolve_access=_guard_access_resolver,
        ),
        resolver_router=create_resolver_router(runtime.resolver),
        contracts_router=create_contracts_router(
            service=runtime.contracts_service,
            resolve_access=_guard_access_resolver,
        ),
        github_connect_router=create_github_connect_router(
            service=runtime.github_connect_service,
            resolve_access=_guard_access_resolver,
        ),
        work_events_router=create_work_events_router(
            service=runtime.work_events_read_service,
            mutation_service=runtime.work_events_mutation_service,
            resolve_access=_guard_access_resolver,
        ),
        identities_router=create_identities_router(
            service=runtime.identity_service,
            resolve_access=_guard_access_resolver,
        ),
        facts_router=create_facts_router(
            service=runtime.fact_service,
            resolve_access=_guard_access_resolver,
        ),
        claims_router=create_claims_router(
            service=runtime.claim_service,
            resolve_access=_guard_access_resolver,
        ),
        exports_router=create_exports_router(
            service=runtime.export_service,
            resolve_access=_guard_access_resolver,
        ),
        deletion_router=create_deletion_router(
            service=runtime.deletion_service,
            resolve_access=_guard_access_resolver,
        ),
        budgets_router=create_budgets_router(
            service=runtime.budget_service,
            resolve_access=_guard_access_resolver,
        ),
        install_contract_handlers=True,
        install_github_connect_handlers=True,
        install_work_events_handlers=True,
        install_identities_handlers=True,
        install_facts_handlers=True,
        install_claims_handlers=True,
        install_exports_handlers=True,
        install_deletion_handlers=True,
        install_budgets_handlers=True,
    )
    install_sync_runs_error_handlers(app)
    app.state.fullstack_runtime = runtime

    # Wake background deletion worker immediately after POST /deletion-confirm commits
    @app.middleware("http")
    async def wake_worker_on_deletion_confirm(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.method == "POST" and request.url.path.endswith("/deletion-confirm"):
            if response.status_code == 202:
                runtime.wake_event.set()
        return response

    @app.get("/local-dev/bootstrap", include_in_schema=False)
    def local_dev_bootstrap(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = PRIVATE_NO_STORE_HEADER
        return runtime.build_bootstrap_payload().to_dict()

    return app


__all__ = [
    "BackgroundPipelineWorker",
    "FullstackPostgresRuntime",
    "LOCAL_JWT_AUDIENCE",
    "LOCAL_JWT_ISSUER",
    "LocalEs256KeyPair",
    "ManagedPostgres18Database",
    "PooledPostgresProvenanceMetadataStore",
    "PooledPostgresWorkspaceAccessProvider",
    "create_local_dev_fullstack_app",
]
