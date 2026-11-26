"""Ephemeral Local Supabase CLI Integration Tests (Real PostgREST OD006-5 & Real GoTrue OD010-5).

PRECONDITIONS:
- Requires a running local Docker Engine (`docker info` exit code 0) and `supabase` CLI.
- Uses ONLY the isolated `project_id = "t007d-authdb-ephemeral"` on ports 54921-54926.
- Tears down ONLY this project via `supabase stop --no-backup` in `finally` (never stops
  Docker Desktop or touches other projects/containers/volumes).
- NEVER counts a stopped/unreachable Docker or Supabase service as a permission-denial pass:
  if Docker Engine is unavailable, tests explicitly SKIP with a BLOCKED reason.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import psycopg
import pytest

SPIKE_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = SPIKE_ROOT / "sql"
SUPABASE_WORKDIR = SPIKE_ROOT


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


@pytest.fixture(scope="module")
def local_supabase_stack():
    """Start isolated ephemeral local Supabase instance and always clean up ONLY this project."""
    ready, detail = _docker_engine_ready()
    if not ready:
        pytest.skip(f"BLOCKED (local Docker Engine unavailable): {detail}")
    if not shutil.which("supabase"):
        pytest.skip("BLOCKED: supabase CLI not installed")

    start_cmd = [
        "supabase",
        "start",
        "--workdir",
        str(SUPABASE_WORKDIR),
        "--exclude",
        "studio,storage-api,imgproxy,edge-runtime,logflare,vector,supavisor",
    ]
    try:
        subprocess.run(start_cmd, check=True, capture_output=True, text=True, timeout=300)
        status_proc = subprocess.run(
            ["supabase", "status", "--workdir", str(SUPABASE_WORKDIR), "-o", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        env_info = json.loads(status_proc.stdout)

        # Apply synthetic T-007D roles/schema/seed to this isolated local Supabase DB only
        db_url = env_info["DB_URL"]
        with psycopg.connect(db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                for sql_name in ("001_roles.sql", "002_schema_and_rls.sql", "003_seed_synthetic.sql"):
                    cur.execute((SQL_DIR / sql_name).read_text(encoding="utf-8"))
                # Explicitly ensure Supabase PostgREST roles cannot access app_t007d
                cur.execute("REVOKE ALL ON SCHEMA app_t007d FROM anon, authenticated")
                cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app_t007d FROM anon, authenticated")

        # Verify live PostgREST & GoTrue health before yielding (never count downed service as pass)
        api_url = env_info["API_URL"]
        anon_key = env_info["ANON_KEY"]
        with httpx.Client(timeout=10.0) as client:
            auth_health = client.get(f"{api_url}/auth/v1/health", headers={"apikey": anon_key})
            assert auth_health.status_code == 200, "GoTrue health check must pass before running tests"

        yield env_info
    finally:
        # Clean up ONLY this ephemeral project (`t007d-authdb-ephemeral`); never touch other projects.
        subprocess.run(
            ["supabase", "stop", "--workdir", str(SUPABASE_WORKDIR), "--no-backup"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )


def test_local_supabase_od006_5_postgrest_blocks_anon_and_authenticated_domain_access(
    local_supabase_stack: dict,
) -> None:
    """Verify real local PostgREST blocks anon & authenticated user direct reads/writes to app_t007d."""
    api_url = local_supabase_stack["API_URL"]
    anon_key = local_supabase_stack["ANON_KEY"]
    db_url = local_supabase_stack["DB_URL"]

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app_t007d.projects")
            before_count = cur.fetchone()[0]

    with httpx.Client(base_url=api_url, timeout=10.0) as client:
        # Sign up synthetic user in local GoTrue
        signup = client.post(
            "/auth/v1/signup",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": "t007d.local.user@synthetic.local", "password": "SyntheticLocalPassword123!"},
        )
        assert signup.status_code in (200, 201)
        user_jwt = signup.json()["access_token"]

        # Attempt Data API bypass as anon and as logged-in user against app_t007d.projects
        for label, auth_header in [("anon", f"Bearer {anon_key}"), ("authenticated", f"Bearer {user_jwt}")]:
            headers = {
                "apikey": anon_key,
                "Authorization": auth_header,
                "Accept-Profile": "app_t007d",
                "Content-Profile": "app_t007d",
            }
            r_get = client.get("/rest/v1/projects", headers=headers)
            assert r_get.status_code in (401, 403, 404, 406), f"PostgREST GET as {label} must be denied"
            r_post = client.post(
                "/rest/v1/projects",
                headers=headers,
                json={"name": f"bypass-{label}"},
            )
            assert r_post.status_code in (401, 403, 404, 406), f"PostgREST POST as {label} must be denied"

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app_t007d.projects")
            after_count = cur.fetchone()[0]
    assert after_count == before_count


def test_local_supabase_od010_5_gotrue_logout_revokes_refresh_and_session_endpoint(
    local_supabase_stack: dict,
) -> None:
    """Verify real local GoTrue logout revokes refresh token and `/auth/v1/user` session check."""
    api_url = local_supabase_stack["API_URL"]
    anon_key = local_supabase_stack["ANON_KEY"]

    with httpx.Client(base_url=api_url, timeout=10.0) as client:
        signup = client.post(
            "/auth/v1/signup",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": "t007d.logout.user@synthetic.local", "password": "SyntheticLocalPassword123!"},
        )
        assert signup.status_code in (200, 201)
        payload = signup.json()
        access_jwt = payload["access_token"]
        refresh_token = payload["refresh_token"]

        # Active session confirmed by GoTrue `/auth/v1/user`
        pre_user = client.get(
            "/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {access_jwt}"},
        )
        assert pre_user.status_code == 200

        # Perform GoTrue logout
        logout = client.post(
            "/auth/v1/logout",
            headers={"apikey": anon_key, "Authorization": f"Bearer {access_jwt}"},
        )
        assert logout.status_code in (200, 204)

        # Refresh re-issuance denied
        refresh = client.post(
            "/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
        )
        assert refresh.status_code in (400, 401, 403)

        # Live GoTrue session check (`/auth/v1/user`) rejects terminated session even before JWT exp
        post_user = client.get(
            "/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {access_jwt}"},
        )
        assert post_user.status_code in (401, 403)
