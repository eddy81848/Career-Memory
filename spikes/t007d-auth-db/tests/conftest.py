"""Pytest fixtures for spinning up an isolated ephemeral local PostgreSQL 18 cluster.

No hosted Supabase project, no Docker daemon, and no shared prototype database is touched.
All PostgreSQL data directories live under pytest's temporary directory and are stopped cleanly
at session teardown.
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import psycopg
import pytest
from psycopg.rows import dict_row

SPIKE_ROOT = Path(__file__).resolve().parents[1]
if str(SPIKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SPIKE_ROOT))

from src.api_app import create_api_app
from src.auth_tokens import StrictJwtVerifier, SyntheticJwtAuthority, SyntheticRevocationStore
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
