"""Ephemeral local PostgreSQL 18 integration & concurrency tests for M1-T101."""

from __future__ import annotations

import inspect
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from career_memory_api.auth.jwt_verifier import (
    JwkKey,
    JwksSnapshot,
    JwtAccessTokenVerifier,
    JwtVerificationConfig,
)
from career_memory_api.auth.oauth import (
    ExchangedOAuthToken,
    InMemorySecretStore,
    OAuthConnectionCoordinator,
    OAuthPersistenceFailedError,
    OAuthProvider,
    OAuthStateInvalidOrReplayedError,
    OrphanSecretCleaner,
    OrphanSecretCleanupStatus,
    PostgresOAuthBoundaryStore,
)
from career_memory_api.auth.postgres_guard import (
    PostgresSourceAccountRepository,
    PostgresWorkspaceAccessProvider,
    workspace_scoped_transaction,
)
from career_memory_api.authz import (
    AuthCredentials,
    UnauthenticatedError,
    WorkspaceAccessGuard,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError
from tests.auth.crypto_helpers import Es256TestKeyPair

pytestmark = pytest.mark.postgres

_PG18_BIN_DIR = Path("/opt/homebrew/opt/postgresql@18/bin")
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MIGRATION_0010 = (
    _REPO_ROOT / "db" / "migrations" / "0010_identity_workspace_source_auth.sql"
)
_ISSUER = "https://auth.career-memory.local/auth/v1"
_AUDIENCE = "authenticated"


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pg_bin(name: str) -> str:
    candidate = _PG18_BIN_DIR / name
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        pytest.fail(
            f"PostgreSQL 18 binary {name!r} not found in {_PG18_BIN_DIR} or PATH."
        )
    return resolved


class _EphemeralPg18Cluster:
    """Disposable local PostgreSQL 18 cluster bound strictly to 127.0.0.1."""

    def __init__(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="cm-m1-t101-pg18-"))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_pg_admin"
        self.dbname = "career_memory_test"
        self.api_login = "cm_api_login_test"
        self.api_password = "local_ephemeral_api_pw"
        self.worker_login = "cm_worker_login_test"
        self.worker_password = "local_ephemeral_worker_pw"

    @property
    def admin_dsn(self) -> str:
        return f"postgresql://{self.superuser}@127.0.0.1:{self.port}/{self.dbname}"

    @property
    def api_dsn(self) -> str:
        return (
            f"postgresql://{self.api_login}:{self.api_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    @property
    def worker_dsn(self) -> str:
        return (
            f"postgresql://{self.worker_login}:{self.worker_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    def start(self) -> None:
        initdb = _pg_bin("initdb")
        pg_ctl = _pg_bin("pg_ctl")
        createdb = _pg_bin("createdb")

        env = os.environ.copy()
        env["LC_ALL"] = "C"

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

        # Configure ephemeral port and strictly loopback listen address
        with (self._pgdata / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write(
                f"\nlisten_addresses = '127.0.0.1'\n"
                f"port = {self.port}\n"
                f"unix_socket_directories = '{self._tmpdir}'\n"
                f"fsync = off\n"
                f"synchronous_commit = off\n"
            )

        subprocess.run(
            [
                pg_ctl,
                "-D",
                str(self._pgdata),
                "-l",
                str(self._logfile),
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

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

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_dsn, connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SHOW server_version_num")
                        row = cur.fetchone()
                        assert row is not None and int(row[0]) >= 180000
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("Ephemeral PG18 cluster did not become ready.")

    def apply_migration_0010(self) -> None:
        sql = _MIGRATION_0010.read_text(encoding="utf-8")
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def provision_runtime_logins(self) -> None:
        """Create non-superuser NOBYPASSRLS login users bound to cm_api_app / cm_worker_app."""
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
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
        pg_ctl = _pg_bin("pg_ctl")
        if self._pgdata.exists():
            subprocess.run(
                [pg_ctl, "-D", str(self._pgdata), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(self._tmpdir, ignore_errors=True)


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[_EphemeralPg18Cluster]:
    cluster = _EphemeralPg18Cluster()
    try:
        cluster.start()
        cluster.apply_migration_0010()
        # Re-apply migration immediately to prove idempotency
        cluster.apply_migration_0010()
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture()
def clean_db(pg18_cluster: _EphemeralPg18Cluster) -> _EphemeralPg18Cluster:
    with psycopg.connect(pg18_cluster.admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE
                    public.orphan_secret_cleanups,
                    public.source_accounts,
                    public.oauth_connect_states,
                    public.workspace_memberships,
                    public.workspaces,
                    public.users
                CASCADE
                """
            )
    return pg18_cluster


class _StaticJwksProvider:
    def __init__(self, snapshot: JwksSnapshot) -> None:
        self._snapshot = snapshot

    def get_jwks(self, *, force_refresh: bool = False) -> JwksSnapshot:
        return self._snapshot


def _make_verifier(key: Es256TestKeyPair, now: datetime) -> JwtAccessTokenVerifier:
    snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(key.to_jwk()),),
        fetched_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    return JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=_StaticJwksProvider(snapshot),
        clock=lambda: now,
    )


def _sign_for_subject(
    key: Es256TestKeyPair,
    *,
    subject: str,
    now: datetime,
    issuer: str = _ISSUER,
    email: str | None = None,
) -> str:
    ts = int(now.timestamp())
    payload = {
        "iss": issuer,
        "aud": _AUDIENCE,
        "sub": subject,
        "exp": ts + 600,
        "iat": ts - 5,
    }
    if email is not None:
        payload["email"] = email
    return key.sign_jwt(payload)


def _seed_identity_and_workspaces(cluster: _EphemeralPg18Cluster) -> dict[str, str]:
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    ws_alpha = str(uuid.uuid4())
    ws_beta = str(uuid.uuid4())
    ws_gamma = str(uuid.uuid4())
    sa_alpha = str(uuid.uuid4())
    sa_beta = str(uuid.uuid4())

    with psycopg.connect(cluster.admin_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                VALUES
                    (%s, %s, 'sub-alice', 'alice@example.com', 'Alice'),
                    (%s, %s, 'sub-bob', 'bob@example.com', 'Bob')
                """,
                (alice_id, _ISSUER, bob_id, _ISSUER),
            )
            cur.execute(
                """
                INSERT INTO public.workspaces (id, owner_user_id, name)
                VALUES
                    (%s, %s, 'Alpha Workspace'),
                    (%s, %s, 'Beta Workspace'),
                    (%s, %s, 'Gamma Workspace')
                """,
                (ws_alpha, alice_id, ws_beta, bob_id, ws_gamma, alice_id),
            )
            cur.execute(
                """
                INSERT INTO public.workspace_memberships (workspace_id, user_id, role, revoked_at)
                VALUES
                    (%s, %s, 'OWNER', NULL),
                    (%s, %s, 'OWNER', NULL),
                    (%s, %s, 'MEMBER', NULL)
                """,
                (ws_alpha, alice_id, ws_beta, bob_id, ws_gamma, alice_id),
            )
            cur.execute(
                """
                INSERT INTO public.source_accounts
                    (id, workspace_id, provider, external_account_id, token_secret_ref, scopes, status, revoked_at)
                VALUES
                    (%s, %s, 'GITHUB', 'gh-alice-1', 'secret://github-oauth-token/alpha-ref', ARRAY['read:user'], 'ACTIVE', NULL),
                    (%s, %s, 'GITHUB', 'gh-bob-1', 'secret://github-oauth-token/beta-ref', ARRAY['read:user'], 'ACTIVE', NULL)
                """,
                (sa_alpha, ws_alpha, sa_beta, ws_beta),
            )
        conn.commit()

    return {
        "alice_id": alice_id,
        "bob_id": bob_id,
        "ws_alpha": ws_alpha,
        "ws_beta": ws_beta,
        "ws_gamma": ws_gamma,
        "sa_alpha": sa_alpha,
        "sa_beta": sa_beta,
    }


def test_migration_0010_fresh_and_idempotent_reapply_and_roles(
    clean_db: _EphemeralPg18Cluster,
) -> None:
    # Apply one more time during test body to be doubly sure re-apply on populated schema works
    clean_db.apply_migration_0010()

    with psycopg.connect(clean_db.admin_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rolname, rolsuper, rolbypassrls
                FROM pg_roles
                WHERE rolname IN ('cm_migrator', 'cm_api_app', 'cm_worker_app')
                ORDER BY rolname
                """
            )
            roles = cur.fetchall()
            assert roles == [
                ("cm_api_app", False, False),
                ("cm_migrator", False, False),
                ("cm_worker_app", False, False),
            ]

            cur.execute(
                """
                SELECT c.relname, pg_get_userbyid(c.relowner), c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN (
                      'users',
                      'workspaces',
                      'workspace_memberships',
                      'oauth_connect_states',
                      'source_accounts',
                      'orphan_secret_cleanups'
                  )
                ORDER BY c.relname
                """
            )
            tables = cur.fetchall()
            assert len(tables) == 6
            for table_name, owner, rls_enabled, rls_forced in tables:
                assert owner == "cm_migrator", f"{table_name} owned by {owner}"
                assert rls_enabled is True, f"{table_name} RLS not enabled"
                assert rls_forced is True, f"{table_name} RLS not forced"

    # Worker role MUST NOT be able to query users or workspace_memberships
    with psycopg.connect(clean_db.worker_dsn) as worker_conn:
        with worker_conn.cursor() as cur:
            for forbidden_table in (
                "public.users",
                "public.workspaces",
                "public.workspace_memberships",
                "public.oauth_connect_states",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"SELECT 1 FROM {forbidden_table}")
                worker_conn.rollback()


def test_identity_mapping_unique_issuer_subject_and_no_email_auto_merge(
    clean_db: _EphemeralPg18Cluster,
) -> None:
    ids = _seed_identity_and_workspaces(clean_db)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    es_key = Es256TestKeyPair.generate("kid-pg-1", seed=101)
    verifier = _make_verifier(es_key, now)

    with psycopg.connect(clean_db.api_dsn, autocommit=False) as api_conn:
        # Unscoped SELECT on users MUST return 0 rows under FORCE RLS
        with api_conn.cursor() as cur:
            cur.execute("SELECT id FROM public.users")
            assert cur.fetchall() == []
        api_conn.rollback()

        provider = PostgresWorkspaceAccessProvider(api_conn, verifier)
        guard = WorkspaceAccessGuard(provider)

        alice_token = _sign_for_subject(
            es_key, subject="sub-alice", now=now, email="alice@example.com"
        )
        ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=alice_token),
            target_workspace_id=ids["ws_alpha"],
        )
        assert ctx.user_id == ids["alice_id"]
        assert ctx.workspace_id == ids["ws_alpha"]
        assert ctx.role == "OWNER"

        # Same email, different sub -> MUST NOT auto-merge or auto-create
        impostor_token = _sign_for_subject(
            es_key, subject="sub-impostor", now=now, email="alice@example.com"
        )
        with pytest.raises(UnauthenticatedError):
            guard.require_workspace_access(
                AuthCredentials(bearer_token=impostor_token),
                target_workspace_id=ids["ws_alpha"],
            )

    # Distinct (auth_issuer, auth_subject) sharing the same email remain separate users
    second_alice_id = str(uuid.uuid4())
    with psycopg.connect(clean_db.admin_dsn, autocommit=True) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                VALUES (%s, %s, 'sub-alice-secondary', 'alice@example.com', 'Alice Secondary')
                """,
                (second_alice_id, _ISSUER),
            )

    with psycopg.connect(clean_db.api_dsn, autocommit=False) as api_conn:
        provider = PostgresWorkspaceAccessProvider(api_conn, verifier)
        secondary_token = _sign_for_subject(
            es_key, subject="sub-alice-secondary", now=now, email="alice@example.com"
        )
        resolved = provider.verify_identity(AuthCredentials(bearer_token=secondary_token))
        assert resolved is not None
        assert resolved.user_id == second_alice_id
        assert resolved.user_id != ids["alice_id"]
        # Secondary user has NO membership in ws_alpha despite sharing Alice's email
        assert provider.verify_workspace_membership(resolved, ids["ws_alpha"]) is None

    # Confirm users table count is strictly 3 (never auto-merged)
    with psycopg.connect(clean_db.admin_dsn) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.users")
            row = cur.fetchone()
            assert row == (3,)


def test_cross_workspace_blocked_revoked_immediate_other_workspace_preserved_and_concealment(
    clean_db: _EphemeralPg18Cluster,
) -> None:
    # Verify repository exposes NO bare get_by_id method
    repo_methods = [
        name
        for name, value in inspect.getmembers(
            PostgresSourceAccountRepository, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    ]
    assert "get_by_id" not in repo_methods
    for method_name in repo_methods:
        sig = inspect.signature(getattr(PostgresSourceAccountRepository, method_name))
        params = list(sig.parameters.keys())
        assert params[:2] == ["self", "access"], f"{method_name} missing access first arg"

    ids = _seed_identity_and_workspaces(clean_db)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    es_key = Es256TestKeyPair.generate("kid-pg-2", seed=202)
    verifier = _make_verifier(es_key, now)
    alice_token = _sign_for_subject(es_key, subject="sub-alice", now=now)

    with psycopg.connect(clean_db.api_dsn, autocommit=False) as api_conn:
        provider = PostgresWorkspaceAccessProvider(api_conn, verifier)
        guard = WorkspaceAccessGuard(provider)
        repo = PostgresSourceAccountRepository(api_conn)

        alpha_ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=alice_token),
            target_workspace_id=ids["ws_alpha"],
        )
        own = repo.read_or_conceal(alpha_ctx, ids["sa_alpha"])
        assert own.id == ids["sa_alpha"]
        assert own.workspace_id == ids["ws_alpha"]

        # Cross-workspace target blocked at guard
        with pytest.raises(WorkspaceMembershipDeniedError):
            guard.require_workspace_access(
                AuthCredentials(bearer_token=alice_token),
                target_workspace_id=ids["ws_beta"],
            )

        # Within ws_alpha, Bob's sa_beta ID vs nonexistent UUID yield IDENTICAL concealed error
        with pytest.raises(WorkspaceResourceNotFoundError) as other_ws_exc:
            repo.read_or_conceal(alpha_ctx, ids["sa_beta"])
        with pytest.raises(WorkspaceResourceNotFoundError) as missing_exc:
            repo.read_or_conceal(alpha_ctx, str(uuid.uuid4()))

        assert str(other_ws_exc.value) == str(missing_exc.value)
        assert other_ws_exc.value.args == missing_exc.value.args
        assert ids["sa_beta"] not in str(other_ws_exc.value)

        # Revoke Alice's membership in ws_alpha only (ws_gamma remains active)
        with psycopg.connect(clean_db.admin_dsn, autocommit=True) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.workspace_memberships
                    SET revoked_at = now()
                    WHERE workspace_id = %s AND user_id = %s
                    """,
                    (ids["ws_alpha"], ids["alice_id"]),
                )

        # Immediate block on ws_alpha
        with pytest.raises(WorkspaceMembershipDeniedError):
            guard.require_workspace_access(
                AuthCredentials(bearer_token=alice_token),
                target_workspace_id=ids["ws_alpha"],
            )
        # Even using stale alpha_ctx directly against repo, RLS returns 0 rows -> concealed!
        with pytest.raises(WorkspaceResourceNotFoundError):
            repo.read_or_conceal(alpha_ctx, ids["sa_alpha"])

        # ws_gamma membership remains intact and usable
        gamma_ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=alice_token),
            target_workspace_id=ids["ws_gamma"],
        )
        assert gamma_ctx.workspace_id == ids["ws_gamma"]
        assert gamma_ctx.role == "MEMBER"


def test_set_local_zero_leakage_across_reused_connection_transactions(
    clean_db: _EphemeralPg18Cluster,
) -> None:
    ids = _seed_identity_and_workspaces(clean_db)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    es_key = Es256TestKeyPair.generate("kid-pg-3", seed=303)
    verifier = _make_verifier(es_key, now)

    alice_token = _sign_for_subject(es_key, subject="sub-alice", now=now)
    bob_token = _sign_for_subject(es_key, subject="sub-bob", now=now)

    # Single physical connection reused across multiple tenant transactions
    with psycopg.connect(clean_db.api_dsn, autocommit=False) as pooled_conn:
        provider = PostgresWorkspaceAccessProvider(pooled_conn, verifier)
        guard = WorkspaceAccessGuard(provider)

        alice_ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=alice_token),
            target_workspace_id=ids["ws_alpha"],
        )
        bob_ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=bob_token),
            target_workspace_id=ids["ws_beta"],
        )

        # Tx 1: Scoped to Alice / ws_alpha
        with workspace_scoped_transaction(pooled_conn, alice_ctx) as cur:
            cur.execute("SELECT id::text FROM public.source_accounts")
            assert cur.fetchall() == [(ids["sa_alpha"],)]

        # Tx 2: Reused connection WITHOUT setting GUCs -> MUST be NULL and 0 rows
        with pooled_conn.transaction():
            with pooled_conn.cursor() as cur:
                cur.execute(
                    "SELECT auth_ctx.current_workspace_id(), auth_ctx.current_user_id()"
                )
                assert cur.fetchone() == (None, None)
                cur.execute("SELECT id::text FROM public.source_accounts")
                assert cur.fetchall() == []

        # Tx 3: Scoped to Bob / ws_beta -> sees only Bob's row
        with workspace_scoped_transaction(pooled_conn, bob_ctx) as cur:
            cur.execute("SELECT id::text FROM public.source_accounts")
            assert cur.fetchall() == [(ids["sa_beta"],)]

        # Tx 4: Rollback mid-transaction -> next Tx still sees NULL & 0 rows
        with pytest.raises(RuntimeError):
            with workspace_scoped_transaction(pooled_conn, alice_ctx) as cur:
                cur.execute("SELECT id::text FROM public.source_accounts")
                assert cur.fetchall() == [(ids["sa_alpha"],)]
                raise RuntimeError("simulated rollback")

        with pooled_conn.transaction():
            with pooled_conn.cursor() as cur:
                cur.execute("SELECT auth_ctx.current_workspace_id()")
                assert cur.fetchone() == (None,)
                cur.execute("SELECT id::text FROM public.source_accounts")
                assert cur.fetchall() == []


def test_postgres_concurrent_oauth_callback_replay_zero_duplicates(
    clean_db: _EphemeralPg18Cluster,
) -> None:
    ids = _seed_identity_and_workspaces(clean_db)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    es_key = Es256TestKeyPair.generate("kid-pg-4", seed=404)
    verifier = _make_verifier(es_key, now)
    alice_token = _sign_for_subject(es_key, subject="sub-alice", now=now)

    shared_secrets = InMemorySecretStore()
    raw_token = "gho_concurrent_secret_token_777"
    raw_code = "gh_concurrent_code_777"

    exchange_count_lock = threading.Lock()
    exchange_calls: list[str] = []

    class _CountingExchanger:
        def exchange_authorization_code(
            self,
            *,
            provider: OAuthProvider,
            code: str,
            pkce_code_verifier: str,
            redirect_uri: str,
        ) -> ExchangedOAuthToken:
            with exchange_count_lock:
                exchange_calls.append(code)
            return ExchangedOAuthToken(
                provider=provider,
                external_account_id="gh-new-concurrent-acct",
                access_token=raw_token,
                scopes=("read:user", "public_repo"),
            )

    exchanger = _CountingExchanger()

    with psycopg.connect(clean_db.api_dsn, autocommit=False) as start_conn:
        guard = WorkspaceAccessGuard(
            PostgresWorkspaceAccessProvider(start_conn, verifier)
        )
        alice_ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=alice_token),
            target_workspace_id=ids["ws_alpha"],
        )
        start_coord = OAuthConnectionCoordinator(
            boundary_store=PostgresOAuthBoundaryStore(start_conn),
            secret_store=shared_secrets,
            token_exchanger=exchanger,
            clock=lambda: now,
        )
        started = start_coord.start_connect(
            alice_ctx,
            provider=OAuthProvider.GITHUB,
            return_path="/settings/sources",
            redirect_uri="https://app.career-memory.local/oauth/callback",
        )

    barrier = threading.Barrier(2)
    results: list[object] = [None, None]

    def worker(slot: int) -> None:
        with psycopg.connect(clean_db.api_dsn, autocommit=False) as worker_conn:
            coord = OAuthConnectionCoordinator(
                boundary_store=PostgresOAuthBoundaryStore(worker_conn),
                secret_store=shared_secrets,
                token_exchanger=exchanger,
                clock=lambda: now,
            )
            barrier.wait(timeout=5.0)
            try:
                results[slot] = coord.handle_callback(
                    alice_ctx,
                    provider=OAuthProvider.GITHUB,
                    state=started.state,
                    code=raw_code,
                    redirect_uri="https://app.career-memory.local/oauth/callback",
                    request_id=f"req-race-{slot}",
                )
            except Exception as exc:  # noqa: BLE001
                results[slot] = exc

    t0 = threading.Thread(target=worker, args=(0,))
    t1 = threading.Thread(target=worker, args=(1,))
    t0.start()
    t1.start()
    t0.join(timeout=10.0)
    t1.join(timeout=10.0)

    successes = [r for r in results if not isinstance(r, Exception)]
    replays = [r for r in results if isinstance(r, OAuthStateInvalidOrReplayedError)]
    assert len(successes) == 1
    assert len(replays) == 1
    assert len(exchange_calls) == 1

    # Verify DB state: exactly 1 newly connected source account, 0 raw secrets in DB
    with psycopg.connect(clean_db.admin_dsn) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_account_id, token_secret_ref
                FROM public.source_accounts
                WHERE workspace_id = %s AND external_account_id = 'gh-new-concurrent-acct'
                """,
                (ids["ws_alpha"],),
            )
            rows = cur.fetchall()
            assert len(rows) == 1
            assert raw_token not in rows[0][1]

            cur.execute(
                "SELECT state_hash, pkce_code_verifier_ref, consumed_at FROM public.oauth_connect_states"
            )
            state_rows = cur.fetchall()
            assert len(state_rows) == 1
            assert state_rows[0][0] != started.state
            assert state_rows[0][2] is not None
            assert raw_code not in str(state_rows)
            assert started.state not in str(state_rows)


def test_postgres_oauth_db_failure_after_token_exchange_triggers_idempotent_orphan_cleanup(
    clean_db: _EphemeralPg18Cluster,
) -> None:
    ids = _seed_identity_and_workspaces(clean_db)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    es_key = Es256TestKeyPair.generate("kid-pg-5", seed=505)
    verifier = _make_verifier(es_key, now)
    alice_token = _sign_for_subject(es_key, subject="sub-alice", now=now)

    secrets_store = InMemorySecretStore()
    raw_token = "gho_pg_orphan_secret_token_999"
    raw_code = "gh_pg_orphan_code_999"

    class _RevokingDuringExchangeExchanger:
        """Simulates membership revocation committed right as token exchange returns."""

        def exchange_authorization_code(
            self,
            *,
            provider: OAuthProvider,
            code: str,
            pkce_code_verifier: str,
            redirect_uri: str,
        ) -> ExchangedOAuthToken:
            # Revoke Alice's membership in ws_alpha before source_accounts INSERT runs!
            with psycopg.connect(clean_db.admin_dsn, autocommit=True) as admin_conn:
                with admin_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.workspace_memberships
                        SET revoked_at = now()
                        WHERE workspace_id = %s AND user_id = %s
                        """,
                        (ids["ws_alpha"], ids["alice_id"]),
                    )
            return ExchangedOAuthToken(
                provider=provider,
                external_account_id="gh-revoked-mid-callback",
                access_token=raw_token,
                scopes=("read:user",),
            )

    with psycopg.connect(clean_db.api_dsn, autocommit=False) as api_conn:
        guard = WorkspaceAccessGuard(
            PostgresWorkspaceAccessProvider(api_conn, verifier)
        )
        alice_ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=alice_token),
            target_workspace_id=ids["ws_alpha"],
        )
        pg_store = PostgresOAuthBoundaryStore(api_conn)
        coord = OAuthConnectionCoordinator(
            boundary_store=pg_store,
            secret_store=secrets_store,
            token_exchanger=_RevokingDuringExchangeExchanger(),
            clock=lambda: now,
        )

        started = coord.start_connect(
            alice_ctx,
            provider=OAuthProvider.GITHUB,
            return_path="/settings/sources",
            redirect_uri="https://app.career-memory.local/oauth/callback",
        )

        with pytest.raises(OAuthPersistenceFailedError) as exc_info:
            coord.handle_callback(
                alice_ctx,
                provider=OAuthProvider.GITHUB,
                state=started.state,
                code=raw_code,
                redirect_uri="https://app.career-memory.local/oauth/callback",
                request_id="req-pg-orphan-1",
            )

        # 0 orphan secrets left in SecretStore!
        assert secrets_store.all_refs() == frozenset()
        cleanup = exc_info.value.cleanup_record
        assert cleanup is not None
        assert cleanup.status is OrphanSecretCleanupStatus.DELETED

        # Re-run cleanup idempotently against PostgreSQL
        cleaner = OrphanSecretCleaner(
            secret_store=secrets_store,
            cleanup_store=pg_store,
            clock=lambda: now + timedelta(seconds=5),
        )
        idempotent_again = cleaner.cleanup_orphan_secret(
            workspace_id=alice_ctx.workspace_id,
            actor_user_id=alice_ctx.user_id,
            provider=OAuthProvider.GITHUB,
            token_secret_ref=cleanup.token_secret_ref,
            failure_reason="idempotent_recheck",
            metadata={"request_id": "req-pg-orphan-1", "token": raw_token},
        )
        assert idempotent_again.status is OrphanSecretCleanupStatus.DELETED

    # Verify DB state: 0 source_accounts for gh-revoked-mid-callback, 1 redacted cleanup row
    with psycopg.connect(clean_db.admin_dsn) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM public.source_accounts
                WHERE external_account_id = 'gh-revoked-mid-callback'
                """
            )
            assert cur.fetchone() == (0,)

            cur.execute(
                """
                SELECT status, attempt_count, failure_reason, redacted_metadata::text
                FROM public.orphan_secret_cleanups
                WHERE workspace_id = %s
                """,
                (ids["ws_alpha"],),
            )
            cleanup_rows = cur.fetchall()
            assert len(cleanup_rows) == 1
            status, attempt_count, failure_reason, metadata_text = cleanup_rows[0]
            assert status == "DELETED"
            assert attempt_count == 2
            for secret in (raw_token, raw_code, started.state):
                assert secret not in failure_reason
                assert secret not in metadata_text


def test_postgres_oauth_constraint_failure_after_token_exchange_cleans_orphan_secret(
    clean_db: _EphemeralPg18Cluster,
) -> None:
    """Verify CHECK constraint failure during source_accounts write cleans orphan secret."""
    ids = _seed_identity_and_workspaces(clean_db)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    es_key = Es256TestKeyPair.generate("kid-pg-6", seed=606)
    verifier = _make_verifier(es_key, now)
    alice_token = _sign_for_subject(es_key, subject="sub-alice", now=now)

    secrets_store = InMemorySecretStore()
    raw_token = "gho_pg_constraint_orphan_token_888"
    raw_code = "gh_pg_constraint_code_888"

    class _BlankExternalIdExchanger:
        def exchange_authorization_code(
            self,
            *,
            provider: OAuthProvider,
            code: str,
            pkce_code_verifier: str,
            redirect_uri: str,
        ) -> ExchangedOAuthToken:
            # Blank external_account_id violates public.source_accounts CHECK constraint
            return ExchangedOAuthToken(
                provider=provider,
                external_account_id="   ",
                access_token=raw_token,
                scopes=("read:user",),
            )

    with psycopg.connect(clean_db.api_dsn, autocommit=False) as api_conn:
        guard = WorkspaceAccessGuard(
            PostgresWorkspaceAccessProvider(api_conn, verifier)
        )
        alice_ctx = guard.require_workspace_access(
            AuthCredentials(bearer_token=alice_token),
            target_workspace_id=ids["ws_alpha"],
        )
        coord = OAuthConnectionCoordinator(
            boundary_store=PostgresOAuthBoundaryStore(api_conn),
            secret_store=secrets_store,
            token_exchanger=_BlankExternalIdExchanger(),
            clock=lambda: now,
        )
        started = coord.start_connect(
            alice_ctx,
            provider=OAuthProvider.GITHUB,
            return_path="/settings/sources",
            redirect_uri="https://app.career-memory.local/oauth/callback",
        )

        with pytest.raises(OAuthPersistenceFailedError) as exc_info:
            coord.handle_callback(
                alice_ctx,
                provider=OAuthProvider.GITHUB,
                state=started.state,
                code=raw_code,
                redirect_uri="https://app.career-memory.local/oauth/callback",
                request_id="req-pg-constraint-1",
            )

    assert secrets_store.all_refs() == frozenset()
    assert exc_info.value.cleanup_record is not None
    assert exc_info.value.cleanup_record.status is OrphanSecretCleanupStatus.DELETED
    assert raw_token not in str(exc_info.value)
    assert raw_code not in str(exc_info.value)
    assert started.state not in str(exc_info.value)
