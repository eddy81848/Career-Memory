"""Ephemeral local PostgreSQL tests for membership-revocation TOCTOU race (FOUNDATION-04).

Uses a disposable local Docker PostgreSQL container and a test-only schema.
No production migrations, production RLS, real OAuth, or public API routes are added.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import urlparse

import psycopg
import pytest

from career_memory_api.authz import (
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceAccessGuard,
    WorkspaceMembership,
)
from career_memory_api.workspace_resources import (
    WorkspaceResource,
    WorkspaceResourceNotFoundError,
    WorkspaceResourceReader,
)

pytestmark = pytest.mark.postgres

_POSTGRES_IMAGE = "postgres:18-alpine"
_TEST_SCHEMA = "test_foundation04"
_LOCAL_TCP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALICE_TOKEN = "synthetic-alice-token"
_ALICE_USER = AuthenticatedUser(
    user_id="usr-alice",
    auth_subject=AuthSubject(
        issuer="https://synthetic-supabase.local/auth/v1",
        subject="sub-alice-001",
    ),
)


def _is_local_docker_endpoint(endpoint: str) -> bool:
    parsed = urlparse(endpoint.strip())
    if parsed.scheme in {"unix", "npipe"}:
        return bool(parsed.path)
    if parsed.scheme in {"tcp", "http", "https"}:
        return (parsed.hostname or "").lower() in _LOCAL_TCP_HOSTS
    return False


def _ensure_local_docker_engine() -> str:
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        pytest.fail(
            "PostgreSQL opt-in tests require a local Docker engine, "
            "but the 'docker' CLI was not found on PATH."
        )

    env_host = os.environ.get("DOCKER_HOST", "").strip()
    if env_host:
        endpoint = env_host
    else:
        ctx_proc = subprocess.run(
            [
                docker_bin,
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ctx_proc.returncode != 0 or not ctx_proc.stdout.strip():
            detail = (ctx_proc.stderr or ctx_proc.stdout).strip() or "empty context endpoint"
            pytest.fail(
                "PostgreSQL opt-in tests require a verified local Docker engine, "
                f"but the active Docker context could not be inspected: {detail}"
            )
        endpoint = ctx_proc.stdout.strip().splitlines()[0].strip()

    if not _is_local_docker_endpoint(endpoint):
        pytest.fail(
            "PostgreSQL opt-in tests refuse to run against a non-local or "
            f"unverified Docker endpoint: {endpoint!r}"
        )

    try:
        info_proc = subprocess.run(
            [docker_bin, "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"Local Docker engine ({endpoint}) timed out during 'docker info'."
        )

    if info_proc.returncode != 0 or not info_proc.stdout.strip():
        detail = (info_proc.stderr or info_proc.stdout).strip() or "no server version returned"
        pytest.fail(
            f"Local Docker engine ({endpoint}) is unavailable: {detail}"
        )

    return docker_bin


def _start_ephemeral_postgres() -> tuple[str, str]:
    docker_bin = _ensure_local_docker_engine()

    container_name = f"cm-foundation04-pg-{uuid.uuid4().hex[:12]}"
    user = "cm_test"
    password = "cm_test_password"
    dbname = "cm_foundation04"

    run_proc = subprocess.run(
        [
            docker_bin,
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-p",
            "127.0.0.1::5432",
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={dbname}",
            _POSTGRES_IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container_id = run_proc.stdout.strip()

    try:
        port_proc = subprocess.run(
            [docker_bin, "port", container_id, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
        )
        # Example output: "127.0.0.1:54321"
        host_port = port_proc.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        dsn = f"postgresql://{user}:{password}@127.0.0.1:{host_port}/{dbname}"

        deadline = time.monotonic() + 25.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                return container_id, dsn
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.2)

        raise RuntimeError(
            f"Ephemeral PostgreSQL container {container_id} did not become ready: {last_error}"
        )
    except Exception:
        subprocess.run(
            [docker_bin, "rm", "-f", container_id],
            check=False,
            capture_output=True,
            text=True,
        )
        raise


def _stop_ephemeral_postgres(container_id: str) -> None:
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        return
    subprocess.run(
        [docker_bin, "rm", "-f", container_id],
        check=False,
        capture_output=True,
        text=True,
    )


def _create_test_schema(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_TEST_SCHEMA}")
            cur.execute(
                f"""
                CREATE TABLE {_TEST_SCHEMA}.workspace_memberships (
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    revoked_at TIMESTAMPTZ NULL,
                    PRIMARY KEY (workspace_id, user_id)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE {_TEST_SCHEMA}.workspace_resources (
                    workspace_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, resource_id)
                )
                """
            )
        conn.commit()


@pytest.fixture(scope="module")
def ephemeral_postgres_dsn() -> Iterator[str]:
    container_id, dsn = _start_ephemeral_postgres()
    try:
        _create_test_schema(dsn)
        yield dsn
    finally:
        _stop_ephemeral_postgres(container_id)


@pytest.fixture()
def pg_dsn(ephemeral_postgres_dsn: str) -> str:
    with psycopg.connect(ephemeral_postgres_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"TRUNCATE TABLE {_TEST_SCHEMA}.workspace_resources, "
                f"{_TEST_SCHEMA}.workspace_memberships"
            )
        conn.commit()
    return ephemeral_postgres_dsn


class _PostgresMembershipAccessProvider:
    """Test-only provider verifying current active membership against PostgreSQL."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def verify_identity(
        self, credentials: AuthCredentials
    ) -> AuthenticatedUser | None:
        if credentials.bearer_token == _ALICE_TOKEN:
            return _ALICE_USER
        return None

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT workspace_id, user_id
                FROM {_TEST_SCHEMA}.workspace_memberships
                WHERE workspace_id = %s
                  AND user_id = %s
                  AND revoked_at IS NULL
                """,
                (target_workspace_id, user.user_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return WorkspaceMembership(workspace_id=row[0], user_id=row[1])


@dataclass
class _QueryOutcome:
    row_count: int | None = None
    resource: WorkspaceResource | None = None
    error: Exception | None = None


class _PostgresScopedResourceStore:
    """Test-only store enforcing both Workspace scope and active membership in SQL."""

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        before_select_hook: Callable[[WorkspaceAccessContext, str], None] | None = None,
        after_select_hook: Callable[[int], None] | None = None,
    ) -> None:
        self._conn = conn
        self._before_select_hook = before_select_hook
        self._after_select_hook = after_select_hook
        self.executed_queries = 0
        self.last_row_count: int | None = None

    def get_by_workspace_and_id(
        self,
        access: WorkspaceAccessContext,
        resource_id: str,
    ) -> WorkspaceResource | None:
        if self._before_select_hook is not None:
            self._before_select_hook(access, resource_id)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT r.resource_id, r.workspace_id, r.title
                FROM {_TEST_SCHEMA}.workspace_resources AS r
                INNER JOIN {_TEST_SCHEMA}.workspace_memberships AS m
                    ON m.workspace_id = r.workspace_id
                   AND m.user_id = %s
                   AND m.revoked_at IS NULL
                WHERE r.workspace_id = %s
                  AND r.resource_id = %s
                """,
                (access.user_id, access.workspace_id, resource_id),
            )
            rows = cur.fetchall()

        self.executed_queries += 1
        self.last_row_count = len(rows)
        if self._after_select_hook is not None:
            self._after_select_hook(len(rows))

        if not rows:
            return None
        assert len(rows) == 1
        row = rows[0]
        return WorkspaceResource(
            resource_id=row[0],
            workspace_id=row[1],
            title=row[2],
        )


def _seed_rows(
    dsn: str,
    *,
    memberships: list[tuple[str, str]],
    resources: list[tuple[str, str, str]],
) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for workspace_id, user_id in memberships:
                cur.execute(
                    f"""
                    INSERT INTO {_TEST_SCHEMA}.workspace_memberships (workspace_id, user_id, revoked_at)
                    VALUES (%s, %s, NULL)
                    """,
                    (workspace_id, user_id),
                )
            for workspace_id, resource_id, title in resources:
                cur.execute(
                    f"""
                    INSERT INTO {_TEST_SCHEMA}.workspace_resources (workspace_id, resource_id, title)
                    VALUES (%s, %s, %s)
                    """,
                    (workspace_id, resource_id, title),
                )
        conn.commit()


def test_revoked_membership_committed_between_guard_and_select_returns_zero_rows(
    pg_dsn: str,
) -> None:
    _seed_rows(
        pg_dsn,
        memberships=[("ws-alpha", "usr-alice")],
        resources=[
            ("ws-alpha", "res-shared-001", "Alpha Protected Resource"),
            ("ws-beta", "res-shared-001", "Beta Protected Resource"),
        ],
    )

    timeline: list[str] = []
    timeline_lock = threading.Lock()
    guard_passed = threading.Event()
    revocation_committed = threading.Event()
    outcome = _QueryOutcome()

    def record(step: str) -> None:
        with timeline_lock:
            timeline.append(step)

    def pause_after_guard(access: WorkspaceAccessContext, resource_id: str) -> None:
        assert access.user_id == "usr-alice"
        assert access.workspace_id == "ws-alpha"
        assert resource_id == "res-shared-001"
        record("1_guard_passed_and_paused")
        guard_passed.set()
        assert revocation_committed.wait(timeout=5.0), "Timed out waiting for revocation commit"
        record("3_resource_select_started")

    def after_select(row_count: int) -> None:
        outcome.row_count = row_count
        record(f"4_resource_select_finished_rows_{row_count}")

    def run_request() -> None:
        with psycopg.connect(pg_dsn, autocommit=False) as conn_request:
            with conn_request.cursor() as cur:
                cur.execute("SHOW transaction_isolation")
                isolation_row = cur.fetchone()
                assert isolation_row is not None
                assert isolation_row[0] == "read committed"

            provider = _PostgresMembershipAccessProvider(conn_request)
            store = _PostgresScopedResourceStore(
                conn_request,
                before_select_hook=pause_after_guard,
                after_select_hook=after_select,
            )
            reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)
            try:
                outcome.resource = reader.read_resource(
                    credentials=AuthCredentials(bearer_token=_ALICE_TOKEN),
                    target_workspace_id="ws-alpha",
                    resource_id="res-shared-001",
                )
            except Exception as exc:  # noqa: BLE001
                outcome.error = exc
            finally:
                conn_request.rollback()

    request_thread = threading.Thread(target=run_request, daemon=True)
    request_thread.start()

    try:
        assert guard_passed.wait(timeout=5.0), "Timed out waiting for guard to pass"

        with psycopg.connect(pg_dsn, autocommit=False) as conn_revoker:
            with conn_revoker.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {_TEST_SCHEMA}.workspace_memberships
                    SET revoked_at = clock_timestamp()
                    WHERE workspace_id = %s
                      AND user_id = %s
                      AND revoked_at IS NULL
                    """,
                    ("ws-alpha", "usr-alice"),
                )
                assert cur.rowcount == 1
            conn_revoker.commit()

        record("2_revocation_committed")
        revocation_committed.set()
    finally:
        revocation_committed.set()
        request_thread.join(timeout=5.0)
        assert not request_thread.is_alive()

    assert timeline == [
        "1_guard_passed_and_paused",
        "2_revocation_committed",
        "3_resource_select_started",
        "4_resource_select_finished_rows_0",
    ]
    assert outcome.resource is None
    assert outcome.row_count == 0
    assert isinstance(outcome.error, WorkspaceResourceNotFoundError)


def test_active_membership_reads_own_workspace_row_and_excludes_same_id_in_other_workspace(
    pg_dsn: str,
) -> None:
    _seed_rows(
        pg_dsn,
        memberships=[("ws-alpha", "usr-alice")],
        resources=[
            ("ws-alpha", "res-shared-001", "Alpha Protected Resource"),
            ("ws-beta", "res-shared-001", "Beta Protected Resource"),
        ],
    )

    with psycopg.connect(pg_dsn, autocommit=False) as conn_request:
        provider = _PostgresMembershipAccessProvider(conn_request)
        store = _PostgresScopedResourceStore(conn_request)
        reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

        resource = reader.read_resource(
            credentials=AuthCredentials(bearer_token=_ALICE_TOKEN),
            target_workspace_id="ws-alpha",
            resource_id="res-shared-001",
        )
        conn_request.rollback()

    assert store.executed_queries == 1
    assert store.last_row_count == 1
    assert resource == WorkspaceResource(
        resource_id="res-shared-001",
        workspace_id="ws-alpha",
        title="Alpha Protected Resource",
    )


def test_same_resource_id_in_other_workspace_only_returns_zero_rows(pg_dsn: str) -> None:
    _seed_rows(
        pg_dsn,
        memberships=[("ws-alpha", "usr-alice")],
        resources=[
            ("ws-beta", "res-shared-001", "Beta Protected Resource"),
        ],
    )

    with psycopg.connect(pg_dsn, autocommit=False) as conn_request:
        provider = _PostgresMembershipAccessProvider(conn_request)
        store = _PostgresScopedResourceStore(conn_request)
        reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

        with pytest.raises(WorkspaceResourceNotFoundError) as cross_ws_exc:
            reader.read_resource(
                credentials=AuthCredentials(bearer_token=_ALICE_TOKEN),
                target_workspace_id="ws-alpha",
                resource_id="res-shared-001",
            )
        cross_ws_rows = store.last_row_count

        with pytest.raises(WorkspaceResourceNotFoundError) as missing_exc:
            reader.read_resource(
                credentials=AuthCredentials(bearer_token=_ALICE_TOKEN),
                target_workspace_id="ws-alpha",
                resource_id="res-does-not-exist",
            )
        missing_rows = store.last_row_count
        conn_request.rollback()

    assert cross_ws_rows == 0
    assert missing_rows == 0
    assert str(cross_ws_exc.value) == str(missing_exc.value)
    assert cross_ws_exc.value.args == missing_exc.value.args
