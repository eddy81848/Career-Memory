"""Ephemeral Local Supabase CLI Integration Tests (Real PostgREST OD006-5 & Real GoTrue OD010-5).

PRECONDITIONS:
- Requires a running local Docker Engine (`docker info` exit code 0) and `supabase` CLI.
- Uses ONLY the isolated `project_id = "t007d-authdb-ephemeral"` on ports 54921-54926.
- Tears down ONLY this project via `supabase stop --no-backup` in `finally` (never stops
  Docker Desktop or touches other projects/containers/volumes).
- NEVER counts a stopped/unreachable Docker or Supabase service as a permission-denial pass:
  * If Docker Engine is unavailable, tests explicitly SKIP with a BLOCKED reason.
  * Before asserting PostgREST denials, a permitted request (`public.t007d_postgrest_healthcheck`)
    MUST succeed with HTTP 200 for BOTH `anon` and the signed-in `authenticated` user on the
    exact same PostgREST instance.
- NEVER logs or prints raw access tokens, refresh tokens, API keys, or cookies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import jwt

from src.db import make_single_connection_pool, scoped_api_workspace_tx
from tests.conftest import snapshot_domain_tables as _snapshot_domain_tables

WS_A = UUID("11111111-1111-4111-8111-111111111111")
USER_ALICE_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROJECT_A1 = UUID("a1000000-0000-4000-8000-000000000001")
ARTIFACT_A1 = UUID("a1000000-0000-4000-8000-000000000101")


def test_local_supabase_od006_5_postgrest_blocks_anon_and_authenticated_domain_access(
    local_supabase_stack: dict,
) -> None:
    """Prove PostgREST works for a permitted canary first, then blocks anon & authenticated domain access with 0 DB changes."""
    api_url = local_supabase_stack["API_URL"]
    anon_key = local_supabase_stack["_resolved_anon_key"]
    db_url = local_supabase_stack["DB_URL"]

    before_snapshot = _snapshot_domain_tables(db_url)

    with httpx.Client(base_url=api_url, timeout=10.0) as client:
        # 1. Sign up a synthetic user in local GoTrue
        signup = client.post(
            "/auth/v1/signup",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": "t007d.dataapi.user@synthetic.local", "password": "SyntheticLocalPassword123!"},
        )
        assert signup.status_code in (200, 201)
        user_jwt = signup.json()["access_token"]

        # 2. POSITIVE CONTROL: Permitted PostgREST query MUST succeed with HTTP 200 for BOTH anon and logged-in user
        canary_statuses: dict[str, int] = {}
        for label, auth_header in [("anon", f"Bearer {anon_key}"), ("authenticated", f"Bearer {user_jwt}")]:
            canary = client.get(
                "/rest/v1/t007d_postgrest_healthcheck?select=id,marker",
                headers={"apikey": anon_key, "Authorization": auth_header},
            )
            canary_statuses[label] = canary.status_code
            assert canary.status_code == 200, (
                f"PostgREST positive-control canary must succeed for {label}; got {canary.status_code}"
            )
            assert canary.json() == [{"id": 1, "marker": "postgrest-alive"}]

        # 3. NEGATIVE CHECKS: Direct domain table read/write/update/delete MUST be blocked on the SAME PostgREST service
        blocked_statuses: dict[str, set[int]] = {}
        for label, auth_header in [("anon", f"Bearer {anon_key}"), ("authenticated", f"Bearer {user_jwt}")]:
            for profile_name, profile_headers in (
                ("app_t007d", {"Accept-Profile": "app_t007d", "Content-Profile": "app_t007d"}),
                ("public", {}),  # default public profile
            ):
                base_headers = {
                    "apikey": anon_key,
                    "Authorization": auth_header,
                    **profile_headers,
                }
                bucket = blocked_statuses.setdefault(f"{label}:{profile_name}", set())
                for table in ("projects", "artifacts"):
                    r_get = client.get(f"/rest/v1/{table}", headers=base_headers)
                    bucket.add(r_get.status_code)
                    assert r_get.status_code in (401, 403, 404, 406), (
                        f"PostgREST GET {table} as {label} must return 4xx denial, got {r_get.status_code}"
                    )

                    r_post = client.post(
                        f"/rest/v1/{table}",
                        headers={**base_headers, "Content-Type": "application/json"},
                        json={"id": str(uuid4()), "workspace_id": str(WS_A), "name": f"bypass-{label}"},
                    )
                    bucket.add(r_post.status_code)
                    assert r_post.status_code in (401, 403, 404, 406), (
                        f"PostgREST POST {table} as {label} must return 4xx denial, got {r_post.status_code}"
                    )

                    target_id = PROJECT_A1 if table == "projects" else ARTIFACT_A1
                    patch_body = {"name": f"mutated-{label}"} if table == "projects" else {"title": f"mutated-{label}"}
                    r_patch = client.patch(
                        f"/rest/v1/{table}?id=eq.{target_id}",
                        headers={**base_headers, "Content-Type": "application/json"},
                        json=patch_body,
                    )
                    bucket.add(r_patch.status_code)
                    assert r_patch.status_code in (401, 403, 404, 406), (
                        f"PostgREST PATCH {table} as {label} must return 4xx denial, got {r_patch.status_code}"
                    )

                    r_delete = client.delete(
                        f"/rest/v1/{table}?id=eq.{target_id}",
                        headers=base_headers,
                    )
                    bucket.add(r_delete.status_code)
                    assert r_delete.status_code in (401, 403, 404, 406), (
                        f"PostgREST DELETE {table} as {label} must return 4xx denial, got {r_delete.status_code}"
                    )

    # 4. Verify BOTH row counts AND exact column values are 100% unchanged after all blocked Data API calls
    after_blocked_snapshot = _snapshot_domain_tables(db_url)
    assert after_blocked_snapshot == before_snapshot, (
        "Blocked PostgREST attempts must not change row counts or any column values"
    )

    # 5. Confirm normal server-mediated DB path (`t007d_api_app` via scoped transaction) succeeds on the SAME Supabase DB
    parsed = urlparse(db_url)
    api_dsn = (
        f"host={parsed.hostname} port={parsed.port} dbname={parsed.path.lstrip('/')} "
        f"user=t007d_api_app password=local_t007d_api_only"
    )
    api_pool = make_single_connection_pool(api_dsn)
    try:
        new_proj_id = uuid4()
        with scoped_api_workspace_tx(api_pool, subject_id=USER_ALICE_A, requested_workspace_id=WS_A) as (
            conn,
            _m,
            _pid,
        ):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_t007d.projects (id, workspace_id, name, created_by)
                    VALUES (%s, %s, 'Server-Mediated Supabase DB Write', %s)
                    RETURNING id
                    """,
                    (str(new_proj_id), str(WS_A), str(USER_ALICE_A)),
                )
                assert cur.fetchone()["id"] == new_proj_id
    finally:
        api_pool.close()

    after_server_write = _snapshot_domain_tables(db_url)
    assert len(after_server_write["projects"]) == len(before_snapshot["projects"]) + 1
    formatted_blocked = {k: sorted(v) for k, v in blocked_statuses.items()}
    print(
        f"[OD006-5 PostgREST observation] "
        f"canary={canary_statuses}, "
        f"blocked_statuses={formatted_blocked}, "
        f"db_counts_and_values_unchanged=True, "
        f"server_mediated_write_projects={len(before_snapshot['projects'])}->{len(after_server_write['projects'])}"
    )


def test_local_supabase_od010_5_gotrue_local_scope_logout_behavior(
    local_supabase_stack: dict,
) -> None:
    """Explicitly test GoTrue current-session-only logout (`scope=local`) and record each stage separately.

    Verifies without relaxing pass criteria:
    1. Pre-logout `/auth/v1/user` succeeds (200) for Session 1 and Session 2.
    2. `POST /auth/v1/logout?scope=local` logs out ONLY Session 1 (200/204).
    3. Session 1 refresh re-issuance (`POST /auth/v1/token?grant_type=refresh_token`) is rejected (4xx).
    4. Session 1 access JWT is STILL unexpired (`exp > now`) and passes stateless signature verification.
    5. Live GoTrue `/auth/v1/user` with Session 1's unexpired access JWT MUST reject (401/403).
    6. Session 2 (another session of the same user) STILL succeeds (200) on `/auth/v1/user`,
       confirming logout scope was strictly `local` (current session only).
    """
    api_url = local_supabase_stack["API_URL"]
    anon_key = local_supabase_stack["_resolved_anon_key"]
    jwt_secret = local_supabase_stack.get("JWT_SECRET")
    email = "t007d.local.logout@synthetic.local"
    password = "SyntheticLocalPassword123!"

    with httpx.Client(base_url=api_url, timeout=10.0) as client:
        # Session 1 via signup
        s1_signup = client.post(
            "/auth/v1/signup",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        assert s1_signup.status_code in (200, 201)
        s1 = s1_signup.json()
        s1_access = s1["access_token"]
        s1_refresh = s1["refresh_token"]

        # Session 2 via independent password login (to verify scope=local affects ONLY Session 1)
        s2_login = client.post(
            "/auth/v1/token?grant_type=password",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        assert s2_login.status_code == 200
        s2_access = s2_login.json()["access_token"]

        # 1. Pre-logout `/auth/v1/user` succeeds for both Session 1 and Session 2
        s1_pre_user = client.get(
            "/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {s1_access}"},
        )
        s2_pre_user = client.get(
            "/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {s2_access}"},
        )
        assert s1_pre_user.status_code == 200
        assert s2_pre_user.status_code == 200

        # 2. Explicitly log out ONLY the current session (`scope=local`)
        s1_logout = client.post(
            "/auth/v1/logout?scope=local",
            headers={"apikey": anon_key, "Authorization": f"Bearer {s1_access}"},
        )
        assert s1_logout.status_code in (200, 204), (
            f"Expected clean scope=local logout (200/204), got {s1_logout.status_code}"
        )

        # 3. Session 1 refresh token re-issuance MUST be rejected
        s1_refresh_resp = client.post(
            "/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"refresh_token": s1_refresh},
        )
        assert s1_refresh_resp.status_code in (400, 401, 403), (
            f"Expected refresh denial after logout, got {s1_refresh_resp.status_code}"
        )

        # 4. Verify Session 1 access JWT is STILL unexpired (`exp > now`) and cryptographically valid
        unverified_header = jwt.get_unverified_header(s1_access)
        alg = unverified_header.get("alg")
        if alg == "HS256" and jwt_secret:
            verified_claims = jwt.decode(
                s1_access,
                jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_signature": True, "verify_exp": True, "verify_aud": True},
            )
        else:
            jwks_client = jwt.PyJWKClient(f"{api_url}/auth/v1/.well-known/jwks.json")
            signing_key = jwks_client.get_signing_key_from_jwt(s1_access)
            verified_claims = jwt.decode(
                s1_access,
                signing_key.key,
                algorithms=[alg],
                audience="authenticated",
                options={"verify_signature": True, "verify_exp": True, "verify_aud": True},
            )
        now_ts = int(datetime.now(timezone.utc).timestamp())
        assert int(verified_claims["exp"]) > now_ts, "Session 1 access JWT must still be unexpired"

        # 5. Live GoTrue `/auth/v1/user` with Session 1's unexpired access JWT MUST reject (do NOT relax!)
        s1_post_user = client.get(
            "/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {s1_access}"},
        )
        # Emit sanitized diagnostic metadata only (HTTP status + error_code; never raw tokens)
        s1_post_err_code = None
        try:
            body = s1_post_user.json()
            s1_post_err_code = body.get("error_code") or body.get("code") or body.get("msg")
        except Exception:
            pass
        print(
            f"[OD010-5 GoTrue scope=local observation] "
            f"pre_user={s1_pre_user.status_code}, "
            f"logout={s1_logout.status_code}, "
            f"refresh_after_logout={s1_refresh_resp.status_code}, "
            f"jwt_alg={alg}, jwt_unexpired_sig_valid=True, "
            f"post_user_with_unexpired_jwt={s1_post_user.status_code} (code={s1_post_err_code})"
        )
        assert s1_post_user.status_code in (401, 403), (
            f"GoTrue /auth/v1/user accepted unexpired JWT after scope=local logout "
            f"(status={s1_post_user.status_code}, code={s1_post_err_code})"
        )

        # 6. Session 2 MUST still be active on `/auth/v1/user` (proving scope=local terminated ONLY Session 1)
        s2_post_user = client.get(
            "/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {s2_access}"},
        )
        print(f"[OD010-5 GoTrue scope=local isolation] session2_post_user={s2_post_user.status_code}")
        assert s2_post_user.status_code == 200, (
            f"Expected Session 2 to remain active after Session 1 scope=local logout, got {s2_post_user.status_code}"
        )
