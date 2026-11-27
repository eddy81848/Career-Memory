"""Pytest fixtures for spinning up an isolated ephemeral local PostgreSQL 18 cluster.

No hosted Supabase project, no Docker daemon, and no shared prototype database is touched.
All PostgreSQL data directories live under pytest's temporary directory and are stopped cleanly
at session teardown.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

SPIKE_ROOT = Path(__file__).resolve().parents[1]
if str(SPIKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SPIKE_ROOT))

from src.api_app import create_api_app
from src.auth_tokens import (
    StrictJwtVerifier,
    SyntheticJwtAuthority,
    SyntheticRevocationStore,
    SyntheticSessionLifecycleStore,
)
from src.bff_app import create_bff_app
from src.db import DbRolesConfig, make_single_connection_pool

SQL_DIR = SPIKE_ROOT / "sql"

# Synthetic fixed UUIDs matching sql/003_seed_synthetic.sql
WS_A = UUID("11111111-1111-4111-8111-111111111111")
WS_B = UUID("22222222-2222-4222-8222-222222222222")

USER_ALICE_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_BOB_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
USER_CHARLIE_REVOKED = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
USER_DANA_DUAL = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

PROJECT_A1 = UUID("a1000000-0000-4000-8000-000000000001")
PROJECT_B1 = UUID("b2000000-0000-4000-8000-000000000001")

ARTIFACT_A1 = UUID("a1000000-0000-4000-8000-000000000101")
ARTIFACT_B1 = UUID("b2000000-0000-4000-8000-000000000201")


def _find_pg_bin_dir() -> Path:
    candidates = [
        os.environ.get("PG_BIN_DIR"),
        "/opt/homebrew/opt/postgresql@18/bin",
        "/opt/homebrew/opt/postgresql/bin",
        "/usr/local/opt/postgresql@18/bin",
        "/usr/local/opt/postgresql/bin",
    ]
    for candidate in candidates:
        if candidate:
            p = Path(candidate)
            if (p / "initdb").exists() and (p / "pg_ctl").exists() and (p / "psql").exists():
                return p
    initdb = shutil.which("initdb")
    if initdb:
        return Path(initdb).parent
    raise RuntimeError(
        "Local PostgreSQL binaries (initdb/pg_ctl/psql) not found. "
        "Set PG_BIN_DIR or install PostgreSQL locally."
    )


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def pg_cluster(tmp_path_factory: pytest.TempPathFactory) -> DbRolesConfig:
    pg_bin = _find_pg_bin_dir()
    pg_root = tmp_path_factory.mktemp("t007d_pg18")
    data_dir = pg_root / "data"
    sock_dir = pg_root / "sock"
    sock_dir.mkdir(mode=0o700)
    log_file = pg_root / "postgres.log"
    port = _pick_free_port()
    superuser = getpass.getuser()
    dbname = "t007d_authdb"

    subprocess.run(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(data_dir),
            "-U",
            superuser,
            "--auth=trust",
            "--encoding=UTF8",
            "--no-locale",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # Require password authentication (scram-sha-256) on TCP 127.0.0.1 for app/worker/migrator roles,
    # while allowing local Unix socket trust for the ephemeral superuser bootstrap.
    pg_hba = data_dir / "pg_hba.conf"
    pg_hba.write_text(
        "\n".join(
            [
                f"local   all             {superuser}                             trust",
                "host    all             all             127.0.0.1/32            scram-sha-256",
                "host    all             all             ::1/128                 scram-sha-256",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            str(pg_bin / "pg_ctl"),
            "-D",
            str(data_dir),
            "-l",
            str(log_file),
            "-o",
            f"-p {port} -h 127.0.0.1 -k {sock_dir}",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        subprocess.run(
            [str(pg_bin / "createdb"), "-h", str(sock_dir), "-p", str(port), "-U", superuser, dbname],
            check=True,
            capture_output=True,
            text=True,
        )

        for sql_name in ("001_roles.sql", "002_schema_and_rls.sql", "003_seed_synthetic.sql"):
            subprocess.run(
                [
                    str(pg_bin / "psql"),
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    str(sock_dir),
                    "-p",
                    str(port),
                    "-U",
                    superuser,
                    "-d",
                    dbname,
                    "-f",
                    str(SQL_DIR / sql_name),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        yield DbRolesConfig(
            host="127.0.0.1",
            port=port,
            dbname=dbname,
            superuser=f"host={sock_dir} port={port} dbname={dbname} user={superuser}",
            migrator_dsn=(
                f"host=127.0.0.1 port={port} dbname={dbname} "
                f"user=t007d_migrator password=local_t007d_migrator_only"
            ),
            api_dsn=(
                f"host=127.0.0.1 port={port} dbname={dbname} "
                f"user=t007d_api_app password=local_t007d_api_only"
            ),
            worker_dsn=(
                f"host=127.0.0.1 port={port} dbname={dbname} "
                f"user=t007d_worker_app password=local_t007d_worker_only"
            ),
            data_api_anon_dsn=(
                f"host=127.0.0.1 port={port} dbname={dbname} "
                f"user=t007d_data_api_anon password=local_t007d_anon_only"
            ),
            data_api_authenticated_dsn=(
                f"host=127.0.0.1 port={port} dbname={dbname} "
                f"user=t007d_data_api_authenticated password=local_t007d_authenticated_only"
            ),
        )
    finally:
        subprocess.run(
            [str(pg_bin / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
            text=True,
        )


@pytest.fixture(autouse=True)
def reset_synthetic_data(pg_cluster: DbRolesConfig) -> None:
    """Restore deterministic synthetic seed data before each test."""
    seed_sql = (SQL_DIR / "003_seed_synthetic.sql").read_text(encoding="utf-8")
    with psycopg.connect(pg_cluster.superuser, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE
                    app_t007d.artifacts,
                    app_t007d.artifacts_single_fk_baseline,
                    app_t007d.artifacts_rls_exists_candidate,
                    app_t007d.artifacts_trigger_candidate,
                    app_t007d.projects,
                    app_t007d.workspace_memberships,
                    app_t007d.users,
                    app_t007d.workspaces
                CASCADE
                """
            )
            cur.execute(seed_sql)


@pytest.fixture
def api_pool(pg_cluster: DbRolesConfig):
    pool = make_single_connection_pool(pg_cluster.api_dsn)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def worker_pool(pg_cluster: DbRolesConfig):
    pool = make_single_connection_pool(pg_cluster.worker_dsn)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def superuser_conn(pg_cluster: DbRolesConfig):
    with psycopg.connect(pg_cluster.superuser, row_factory=dict_row, autocommit=False) as conn:
        yield conn


@pytest.fixture
def jwt_authority() -> SyntheticJwtAuthority:
    return SyntheticJwtAuthority()


@pytest.fixture
def revocation_store() -> SyntheticRevocationStore:
    return SyntheticRevocationStore()


@pytest.fixture
def session_lifecycle() -> SyntheticSessionLifecycleStore:
    return SyntheticSessionLifecycleStore()


@pytest.fixture
def api_app(api_pool, jwt_authority, revocation_store):
    verifier = StrictJwtVerifier(
        public_pem=jwt_authority.public_pem,
        revocation_store=revocation_store,
    )
    return create_api_app(api_pool=api_pool, verifier=verifier)


@pytest.fixture
def api_client(api_app):
    with TestClient(api_app, base_url="http://api.local") as client:
        yield client


@pytest.fixture
def bff_app(api_app, jwt_authority, revocation_store):
    def make_backend_client() -> TestClient:
        return TestClient(api_app, base_url="http://api.local")

    return create_bff_app(
        backend_client_factory=make_backend_client,
        jwt_authority=jwt_authority,
        revocation_store=revocation_store,
        allowed_origin="http://localhost:3000",
        cookie_secure=False,
    )


@pytest.fixture
def lifecycle_verifier(jwt_authority) -> StrictJwtVerifier:
    """Stateless JWT verifier (no revocation denylist) paired with live session checks on sensitive paths."""
    return StrictJwtVerifier(
        public_pem=jwt_authority.public_pem,
        revocation_store=None,
    )


@pytest.fixture
def lifecycle_api_app(api_pool, lifecycle_verifier, session_lifecycle):
    return create_api_app(
        api_pool=api_pool,
        verifier=lifecycle_verifier,
        session_lifecycle=session_lifecycle,
    )


@pytest.fixture
def lifecycle_api_client(lifecycle_api_app):
    with TestClient(lifecycle_api_app, base_url="http://api.local") as client:
        yield client


@pytest.fixture
def lifecycle_bff_app(lifecycle_api_app, jwt_authority, session_lifecycle):
    def make_backend_client() -> TestClient:
        return TestClient(lifecycle_api_app, base_url="http://api.local")

    return create_bff_app(
        backend_client_factory=make_backend_client,
        jwt_authority=jwt_authority,
        revocation_store=None,
        session_lifecycle=session_lifecycle,
        allowed_origin="http://localhost:3000",
        cookie_secure=False,
    )


def _docker_engine_ready() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker CLI not found"
    proc = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        reason = (proc.stderr or proc.stdout or "unknown docker error").strip().splitlines()[-1]
        return False, reason
    return True, proc.stdout.strip()


def snapshot_domain_tables(db_url: str) -> dict[str, list[tuple]]:
    """Capture both row counts and full column values for projects and artifacts."""
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, workspace_id, name, created_by FROM app_t007d.projects ORDER BY id"
            )
            projects = [
                (r["id"], r["workspace_id"], r["name"], r["created_by"])
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT id, workspace_id, project_id, title, payload_note "
                "FROM app_t007d.artifacts ORDER BY id"
            )
            artifacts = [
                (r["id"], r["workspace_id"], r["project_id"], r["title"], r["payload_note"])
                for r in cur.fetchall()
            ]
    return {"projects": projects, "artifacts": artifacts}


def _wait_for_postgrest_canary(api_url: str, anon_key: str, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    with httpx.Client(base_url=api_url, timeout=5.0) as client:
        while time.monotonic() < deadline:
            resp = client.get(
                "/rest/v1/t007d_postgrest_healthcheck?select=id,marker",
                headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
            )
            last_status = resp.status_code
            if resp.status_code == 200 and resp.json() == [{"id": 1, "marker": "postgrest-alive"}]:
                return
            time.sleep(0.5)
    raise AssertionError(
        f"PostgREST positive-control canary did not return HTTP 200 within {timeout_seconds}s "
        f"(last_status={last_status})"
    )


@pytest.fixture(scope="session")
def local_supabase_stack():
    """Start isolated ephemeral local Supabase instance once per session and clean up ONLY this project."""
    ready, detail = _docker_engine_ready()
    if not ready:
        pytest.skip(f"BLOCKED (local Docker Engine unavailable): {detail}")
    if not shutil.which("supabase"):
        pytest.skip("BLOCKED: supabase CLI not installed")

    start_cmd = [
        "supabase",
        "start",
        "--workdir",
        str(SPIKE_ROOT),
        "--exclude",
        "studio,storage-api,imgproxy,edge-runtime,logflare,vector,supavisor,realtime,postgres-meta,mailpit",
    ]
    try:
        subprocess.run(start_cmd, check=True, capture_output=True, text=True, timeout=300)
        status_proc = subprocess.run(
            ["supabase", "status", "--workdir", str(SPIKE_ROOT), "-o", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        env_info = json.loads(status_proc.stdout)

        db_url = env_info["DB_URL"]
        api_url = env_info["API_URL"]
        anon_key = env_info.get("ANON_KEY") or env_info["PUBLISHABLE_KEY"]

        # Apply synthetic T-007D roles/schema/seed + PostgREST positive-control canary to this ephemeral DB only
        with psycopg.connect(db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                # Supabase local Postgres uses a non-superuser `postgres` role with CREATEROLE.
                # Under PG16+, creating schemas with AUTHORIZATION <role> or running SET ROLE <role>
                # requires explicit membership in that role for the bootstrap admin (`current_user`).
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't007d_migrator') THEN
                            CREATE ROLE t007d_migrator
                                LOGIN
                                PASSWORD 'local_t007d_migrator_only'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
                        END IF;
                        EXECUTE format('GRANT t007d_migrator TO %I', current_user);
                    END
                    $$;
                    """
                )
                for sql_name in ("001_roles.sql", "002_schema_and_rls.sql", "003_seed_synthetic.sql"):
                    cur.execute((SQL_DIR / sql_name).read_text(encoding="utf-8"))

                # Ensure Supabase Data API roles cannot access domain schema/tables directly
                cur.execute("REVOKE ALL ON SCHEMA app_t007d FROM anon, authenticated")
                cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app_t007d FROM anon, authenticated")
                cur.execute("REVOKE ALL ON SCHEMA auth_ctx FROM anon, authenticated")

                # Harmless public canary so we PROVE PostgREST is alive & validates both anon & user JWTs
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.t007d_postgrest_healthcheck (
                        id int PRIMARY KEY,
                        marker text NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO public.t007d_postgrest_healthcheck (id, marker)
                    VALUES (1, 'postgrest-alive')
                    ON CONFLICT (id) DO UPDATE SET marker = EXCLUDED.marker
                    """
                )
                cur.execute("GRANT USAGE ON SCHEMA public TO anon, authenticated")
                cur.execute("GRANT SELECT ON public.t007d_postgrest_healthcheck TO anon, authenticated")
                cur.execute("NOTIFY pgrst, 'reload schema'")

        # Verify BOTH GoTrue health AND PostgREST positive-control canary before running tests
        with httpx.Client(timeout=10.0) as client:
            auth_health = client.get(f"{api_url}/auth/v1/health", headers={"apikey": anon_key})
            assert auth_health.status_code == 200, "GoTrue health check must pass before running tests"

        _wait_for_postgrest_canary(api_url, anon_key)
        env_info["_resolved_anon_key"] = anon_key

        class _RedactedSupabaseInfo(dict):
            def __repr__(self) -> str:
                safe = {
                    k: (v if k in ("API_URL", "DB_URL", "GRAPHQL_URL") else "<redacted>")
                    for k, v in self.items()
                }
                return repr(safe)

        yield _RedactedSupabaseInfo(env_info)
    finally:
        # Clean up ONLY this ephemeral project (`t007d-authdb-ephemeral`); never touch Docker Desktop or other projects.
        subprocess.run(
            ["supabase", "stop", "--workdir", str(SPIKE_ROOT), "--no-backup"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        for runtime_dir in (SPIKE_ROOT / "supabase" / ".branches", SPIKE_ROOT / "supabase" / ".temp"):
            if runtime_dir.exists():
                shutil.rmtree(runtime_dir, ignore_errors=True)
