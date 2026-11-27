"""AUTHDB-04: Local Supabase Real JWT + GoTrue Live Session + FastAPI Delete E2E Tests.

Verifies against the isolated ephemeral local Supabase stack (`t007d-authdb-ephemeral`):
1. Reproduces the gap between the existing `RS256`-only `StrictJwtVerifier` (and mock-only
   assumptions such as `jti`) and a real local Supabase GoTrue user JWT (`ES256`, no `jti`).
2. Validates the real user JWT inside FastAPI using fixed test configuration
   (`jwks_url`, `expected_issuer`, `expected_audience`, `allowed_algorithms=("ES256",)`)
   WITHOUT deriving allowed algorithms or JWKS endpoints from untrusted JWT headers.
   (Local spike verification harness only; does NOT finalize product JWT policy.)
3. Validates current session liveness via GoTrue `GET /auth/v1/user` strictly BEFORE
   opening any PostgreSQL transaction (`pg_stat_activity` + pool stats verified during the
   in-flight HTTP check), then exercises the existing FastAPI synthetic delete path
   (`DELETE /api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}`) against the SAME
   local Supabase DB (`t007d_api_app` role).
4. Confirms ONLY active session + active workspace membership deletes succeed, while
   logged-out unexpired JWT, session check timeout/outage, and revoked membership are all
   blocked with 0 DB row/value mutations, and separately verifies a second active session
   of the same user.

SECURITY NOTE: Never logs or prints raw access tokens, refresh tokens, API keys, or cookies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import jwt
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from src.api_app import create_api_app
from src.auth_tokens import (
    FixedConfigJwksJwtVerifier,
    GoTrueLiveSessionVerifier,
    StrictJwtVerifier,
    SyntheticJwtAuthority,
)
from src.db import make_single_connection_pool
from tests.conftest import (
    PROJECT_A1,
    PROJECT_B1,
    WS_A,
    WS_B,
    snapshot_domain_tables,
)


def _supabase_api_dsn(db_url: str) -> str:
    parsed = urlparse(db_url)
    return (
        f"host={parsed.hostname} port={parsed.port} dbname={parsed.path.lstrip('/')} "
        f"user=t007d_api_app password=local_t007d_api_only"
    )


def _count_open_api_transactions(db_url: str) -> int:
    """Count PostgreSQL transactions currently open under the restricted `t007d_api_app` role."""
    with psycopg.connect(db_url, row_factory=dict_row, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS open_tx
                  FROM pg_stat_activity
                 WHERE usename = 't007d_api_app'
                   AND xact_start IS NOT NULL
                """
            )
            row = cur.fetchone()
            assert row is not None
            return int(row["open_tx"])


def _seed_real_user_memberships(
    db_url: str,
    *,
    subject_id: UUID,
    email: str,
    workspaces: tuple[UUID, ...],
) -> None:
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_t007d.users (subject_id, email)
                VALUES (%s, %s)
                ON CONFLICT (subject_id) DO NOTHING
                """,
                (str(subject_id), email),
            )
            for ws_id in workspaces:
                cur.execute(
                    """
                    INSERT INTO app_t007d.workspace_memberships (workspace_id, subject_id, member_role, revoked_at)
                    VALUES (%s, %s, 'member', NULL)
                    ON CONFLICT (workspace_id, subject_id)
                    DO UPDATE SET revoked_at = NULL
                    """,
                    (str(ws_id), str(subject_id)),
                )


def _seed_artifact(
    db_url: str,
    *,
    artifact_id: UUID,
    workspace_id: UUID,
    project_id: UUID,
    title: str,
    payload_note: str,
) -> None:
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title, payload_note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (str(artifact_id), str(workspace_id), str(project_id), title, payload_note),
            )


def _tamper_jwt_signature(token: str) -> str:
    import base64

    header_b64, payload_b64, sig_b64 = token.split(".")
    padded = sig_b64 + "=" * (-len(sig_b64) % 4)
    sig_bytes = bytearray(base64.urlsafe_b64decode(padded))
    sig_bytes[0] ^= 0x01
    tampered_sig_b64 = base64.urlsafe_b64encode(bytes(sig_bytes)).rstrip(b"=").decode("ascii")
    return f"{header_b64}.{payload_b64}.{tampered_sig_b64}"


class _ErrorTransport(httpx.BaseTransport):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self._exc


class _FixedStatusTransport(httpx.BaseTransport):
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self._status_code = status_code
        self._body = body or {"error": "upstream_unavailable"}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self._status_code, json=self._body, request=request)


def test_authdb04_real_supabase_jwt_rs256_gap_and_fixed_jwks_verification(
    local_supabase_stack: dict,
) -> None:
    """Reproduce RS256-only/jti mock gap on a real local Supabase JWT, then verify via fixed JWKS config."""
    api_url = local_supabase_stack["API_URL"]
    anon_key = local_supabase_stack["_resolved_anon_key"]
    db_url = local_supabase_stack["DB_URL"]
    email = "t007d.authdb04.jwt@synthetic.local"
    password = "SyntheticLocalPassword123!"

    with httpx.Client(base_url=api_url, timeout=10.0) as client:
        signup = client.post(
            "/auth/v1/signup",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        assert signup.status_code in (200, 201)
        real_jwt = signup.json()["access_token"]

    # Inspect metadata/claim keys ONLY (never print raw token)
    unverified_header = jwt.get_unverified_header(real_jwt)
    unverified_claims = jwt.decode(
        real_jwt,
        options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
    )
    real_alg = unverified_header.get("alg")
    has_kid = bool(unverified_header.get("kid"))
    has_jti = "jti" in unverified_claims
    has_session_id = bool(unverified_claims.get("session_id"))
    real_sub = UUID(str(unverified_claims["sub"]))

    # Compare mock assumptions vs real local Supabase GoTrue JWT
    assert real_alg == "ES256", f"Expected local GoTrue JWT alg=ES256, observed {real_alg}"
    assert has_kid, "Expected local GoTrue JWT header to carry kid for JWKS lookup"
    assert has_session_id, "Expected local GoTrue JWT payload to carry session_id"
    assert not has_jti, "Expected real local GoTrue JWT to omit mock-only jti claim"
    assert unverified_claims.get("iss") == f"{api_url}/auth/v1"
    assert unverified_claims.get("aud") == "authenticated"
    assert unverified_claims.get("role") == "authenticated"

    _seed_real_user_memberships(db_url, subject_id=real_sub, email=email, workspaces=(WS_A,))

    api_pool = make_single_connection_pool(_supabase_api_dsn(db_url))
    try:
        # 1. Reproduce gap with existing RS256-only StrictJwtVerifier inside FastAPI
        legacy_rs256_verifier = StrictJwtVerifier(public_pem=SyntheticJwtAuthority().public_pem)
        legacy_app = create_api_app(api_pool=api_pool, verifier=legacy_rs256_verifier)
        with TestClient(legacy_app, base_url="http://api.local") as legacy_client:
            legacy_resp = legacy_client.get(
                f"/api/v1/workspaces/{WS_A}/projects",
                headers={"Authorization": f"Bearer {real_jwt}"},
            )
            assert legacy_resp.status_code == 401
            assert legacy_resp.json()["detail"]["code"] == "INVALID_ALGORITHM"

        # 2. Verify via fixed test config (fixed JWKS URL, fixed issuer, fixed audience, fixed allowed_algorithms)
        fixed_jwks_url = f"{api_url}/auth/v1/.well-known/jwks.json"
        fixed_issuer = f"{api_url}/auth/v1"
        fixed_audience = "authenticated"
        fixed_allowed_algs = ("ES256",)

        fixed_verifier = FixedConfigJwksJwtVerifier(
            jwks_url=fixed_jwks_url,
            expected_issuer=fixed_issuer,
            expected_audience=fixed_audience,
            allowed_algorithms=fixed_allowed_algs,
        )
        principal = fixed_verifier.verify_stateless(real_jwt)
        assert principal.subject_id == real_sub
        assert principal.jti is None  # real token omits mock-only jti
        assert principal.session_id == str(unverified_claims["session_id"])

        fixed_app = create_api_app(api_pool=api_pool, verifier=fixed_verifier)
        with TestClient(fixed_app, base_url="http://api.local") as fixed_client:
            ok_resp = fixed_client.get(
                f"/api/v1/workspaces/{WS_A}/projects",
                headers={"Authorization": f"Bearer {real_jwt}"},
            )
            assert ok_resp.status_code == 200
            body = ok_resp.json()
            assert body["workspace_id"] == str(WS_A)
            assert any(item["id"] == str(PROJECT_A1) for item in body["items"])

            # 3a. Tampered signature on real JWT MUST be rejected
            tampered_resp = fixed_client.get(
                f"/api/v1/workspaces/{WS_A}/projects",
                headers={"Authorization": f"Bearer {_tamper_jwt_signature(real_jwt)}"},
            )
            assert tampered_resp.status_code == 401
            assert tampered_resp.json()["detail"]["code"] == "INVALID_SIGNATURE"

            # 3b. Anon key MUST be rejected (cannot pass as authenticated user JWT)
            anon_resp = fixed_client.get(
                f"/api/v1/workspaces/{WS_A}/projects",
                headers={"Authorization": f"Bearer {anon_key}"},
            )
            assert anon_resp.status_code == 401

        # 3c. Fixed allowed_algorithms=('RS256',) MUST reject real ES256 token (never trusts header alg)
        rs256_only_jwks = FixedConfigJwksJwtVerifier(
            jwks_url=fixed_jwks_url,
            expected_issuer=fixed_issuer,
            expected_audience=fixed_audience,
            allowed_algorithms=("RS256",),
        )
        with TestClient(
            create_api_app(api_pool=api_pool, verifier=rs256_only_jwks),
            base_url="http://api.local",
        ) as wrong_alg_client:
            wrong_alg_resp = wrong_alg_client.get(
                f"/api/v1/workspaces/{WS_A}/projects",
                headers={"Authorization": f"Bearer {real_jwt}"},
            )
            assert wrong_alg_resp.status_code == 401
            assert wrong_alg_resp.json()["detail"]["code"] == "INVALID_ALGORITHM"

        # 3d. Wrong fixed issuer MUST reject
        wrong_iss_jwks = FixedConfigJwksJwtVerifier(
            jwks_url=fixed_jwks_url,
            expected_issuer="https://wrong-issuer.local/auth/v1",
            expected_audience=fixed_audience,
            allowed_algorithms=fixed_allowed_algs,
        )
        with TestClient(
            create_api_app(api_pool=api_pool, verifier=wrong_iss_jwks),
            base_url="http://api.local",
        ) as wrong_iss_client:
            wrong_iss_resp = wrong_iss_client.get(
                f"/api/v1/workspaces/{WS_A}/projects",
                headers={"Authorization": f"Bearer {real_jwt}"},
            )
            assert wrong_iss_resp.status_code == 401
            assert wrong_iss_resp.json()["detail"]["code"] == "INVALID_ISSUER"

        # 3e. Wrong fixed audience MUST reject
        wrong_aud_jwks = FixedConfigJwksJwtVerifier(
            jwks_url=fixed_jwks_url,
            expected_issuer=fixed_issuer,
            expected_audience="service_role",
            allowed_algorithms=fixed_allowed_algs,
        )
        with TestClient(
            create_api_app(api_pool=api_pool, verifier=wrong_aud_jwks),
            base_url="http://api.local",
        ) as wrong_aud_client:
            wrong_aud_resp = wrong_aud_client.get(
                f"/api/v1/workspaces/{WS_A}/projects",
                headers={"Authorization": f"Bearer {real_jwt}"},
            )
            assert wrong_aud_resp.status_code == 401
            assert wrong_aud_resp.json()["detail"]["code"] == "INVALID_AUDIENCE"

        print(
            "[AUTHDB-04 JWT observation] "
            f"real_alg={real_alg}, has_kid={has_kid}, has_jti={has_jti}, has_session_id={has_session_id}, "
            f"legacy_rs256_status={legacy_resp.status_code} ({legacy_resp.json()['detail']['code']}), "
            f"fixed_jwks_status={ok_resp.status_code}, "
            f"tampered={tampered_resp.status_code}, anon={anon_resp.status_code}, "
            f"wrong_alg={wrong_alg_resp.status_code}, wrong_iss={wrong_iss_resp.status_code}, "
            f"wrong_aud={wrong_aud_resp.status_code}"
        )
    finally:
        api_pool.close()


def test_authdb04_gotrue_live_session_and_fastapi_delete_e2e(
    local_supabase_stack: dict,
) -> None:
    """Verify GoTrue /auth/v1/user + FastAPI delete path against the same local Supabase DB without holding DB tx."""
    api_url = local_supabase_stack["API_URL"]
    anon_key = local_supabase_stack["_resolved_anon_key"]
    db_url = local_supabase_stack["DB_URL"]
    email = "t007d.authdb04.e2e@synthetic.local"
    password = "SyntheticLocalPassword123!"

    # 1. Obtain TWO independent real sessions for the same synthetic user in local GoTrue
    with httpx.Client(base_url=api_url, timeout=10.0) as client:
        s1_signup = client.post(
            "/auth/v1/signup",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        assert s1_signup.status_code in (200, 201)
        s1_access = s1_signup.json()["access_token"]

        s2_login = client.post(
            "/auth/v1/token?grant_type=password",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        assert s2_login.status_code == 200
        s2_access = s2_login.json()["access_token"]

    fixed_verifier = FixedConfigJwksJwtVerifier(
        jwks_url=f"{api_url}/auth/v1/.well-known/jwks.json",
        expected_issuer=f"{api_url}/auth/v1",
        expected_audience="authenticated",
        allowed_algorithms=("ES256",),
    )
    p1 = fixed_verifier.verify_stateless(s1_access)
    p2 = fixed_verifier.verify_stateless(s2_access)
    assert p1.subject_id == p2.subject_id
    assert p1.session_id != p2.session_id, "Session 1 and Session 2 must be distinct GoTrue sessions"
    real_sub = p1.subject_id

    # Seed real user into the SAME local Supabase DB (active memberships in WS_A and WS_B)
    _seed_real_user_memberships(db_url, subject_id=real_sub, email=email, workspaces=(WS_A, WS_B))

    art_s1_delete = uuid4()
    art_s2_delete = uuid4()
    art_blocked_guard = uuid4()
    art_wsb_active = uuid4()

    _seed_artifact(
        db_url,
        artifact_id=art_s1_delete,
        workspace_id=WS_A,
        project_id=PROJECT_A1,
        title="AUTHDB04 S1 Delete Target",
        payload_note="s1-delete-note",
    )
    _seed_artifact(
        db_url,
        artifact_id=art_s2_delete,
        workspace_id=WS_A,
        project_id=PROJECT_A1,
        title="AUTHDB04 S2 Delete Target",
        payload_note="s2-delete-note",
    )
    _seed_artifact(
        db_url,
        artifact_id=art_blocked_guard,
        workspace_id=WS_A,
        project_id=PROJECT_A1,
        title="AUTHDB04 Guard Artifact",
        payload_note="must-remain-unchanged",
    )
    _seed_artifact(
        db_url,
        artifact_id=art_wsb_active,
        workspace_id=WS_B,
        project_id=PROJECT_B1,
        title="AUTHDB04 WSB Active Target",
        payload_note="wsb-active-note",
    )

    api_pool: ConnectionPool = make_single_connection_pool(_supabase_api_dsn(db_url))
    tx_checks_during_gotrue: list[tuple[int, int]] = []

    def _assert_no_db_tx_during_session_check() -> None:
        stats = api_pool.get_stats()
        checked_out = stats["pool_size"] - stats["pool_available"]
        open_tx = _count_open_api_transactions(db_url)
        tx_checks_during_gotrue.append((checked_out, open_tx))
        assert checked_out == 0, f"DB pool connection must not be checked out during GoTrue check (got {checked_out})"
        assert open_tx == 0, f"No DB transaction may be open during GoTrue check (got {open_tx})"

    try:
        live_checker = GoTrueLiveSessionVerifier(
            api_url=api_url,
            anon_key=anon_key,
            timeout_seconds=3.0,
            on_before_request=_assert_no_db_tx_during_session_check,
        )
        live_app = create_api_app(
            api_pool=api_pool,
            verifier=fixed_verifier,
            live_session_verifier=live_checker,
        )

        with TestClient(live_app, base_url="http://api.local") as client:
            # Case 1: Active Session 1 + Active WS_A membership -> DELETE succeeds
            snap_before_s1 = snapshot_domain_tables(db_url)
            r_s1_ok = client.delete(
                f"/api/v1/workspaces/{WS_A}/artifacts/{art_s1_delete}",
                headers={"Authorization": f"Bearer {s1_access}"},
            )
            assert r_s1_ok.status_code == 200
            assert r_s1_ok.json()["deleted_id"] == str(art_s1_delete)
            snap_after_s1 = snapshot_domain_tables(db_url)
            assert len(snap_after_s1["artifacts"]) == len(snap_before_s1["artifacts"]) - 1
            assert all(row[0] != art_s1_delete for row in snap_after_s1["artifacts"])

            # Case 2: Log out ONLY Session 1 (`scope=local`) -> unexpired JWT is STILL cryptographically
            # valid, but FastAPI delete path MUST block it via GoTrue /auth/v1/user with 0 DB changes
            with httpx.Client(base_url=api_url, timeout=10.0) as auth_client:
                logout_resp = auth_client.post(
                    "/auth/v1/logout?scope=local",
                    headers={"apikey": anon_key, "Authorization": f"Bearer {s1_access}"},
                )
                assert logout_resp.status_code in (200, 204)

            # Confirm logged-out access JWT is still unexpired and passes stateless JWKS check
            s1_claims_after = jwt.decode(
                s1_access,
                options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
            )
            assert int(s1_claims_after["exp"]) > int(datetime.now(timezone.utc).timestamp())
            assert fixed_verifier.verify_stateless(s1_access).subject_id == real_sub

            snap_before_s1_blocked = snapshot_domain_tables(db_url)
            r_s1_blocked = client.delete(
                f"/api/v1/workspaces/{WS_A}/artifacts/{art_s2_delete}",
                headers={"Authorization": f"Bearer {s1_access}"},
            )
            assert r_s1_blocked.status_code == 401
            assert r_s1_blocked.json()["detail"]["code"] == "SESSION_TERMINATED"
            assert snapshot_domain_tables(db_url) == snap_before_s1_blocked

            # Case 3: Same user's OTHER active session (Session 2) -> DELETE succeeds independently
            r_s2_ok = client.delete(
                f"/api/v1/workspaces/{WS_A}/artifacts/{art_s2_delete}",
                headers={"Authorization": f"Bearer {s2_access}"},
            )
            assert r_s2_ok.status_code == 200
            assert r_s2_ok.json()["deleted_id"] == str(art_s2_delete)
            snap_after_s2 = snapshot_domain_tables(db_url)
            assert len(snap_after_s2["artifacts"]) == len(snap_before_s1_blocked["artifacts"]) - 1
            assert all(row[0] != art_s2_delete for row in snap_after_s2["artifacts"])

        # Case 4: Session check timeout -> fail-closed block & DB values 100% unchanged
        timeout_checker = GoTrueLiveSessionVerifier(
            api_url=api_url,
            anon_key=anon_key,
            timeout_seconds=0.5,
            transport=_ErrorTransport(httpx.ReadTimeout("simulated GoTrue /auth/v1/user timeout")),
            on_before_request=_assert_no_db_tx_during_session_check,
        )
        timeout_app = create_api_app(
            api_pool=api_pool,
            verifier=fixed_verifier,
            live_session_verifier=timeout_checker,
        )
        snap_before_timeout = snapshot_domain_tables(db_url)
        with TestClient(timeout_app, base_url="http://api.local") as timeout_client:
            r_timeout = timeout_client.delete(
                f"/api/v1/workspaces/{WS_A}/artifacts/{art_blocked_guard}",
                headers={"Authorization": f"Bearer {s2_access}"},
            )
            assert r_timeout.status_code == 503
            assert r_timeout.json()["detail"]["code"] == "SESSION_STATUS_UNAVAILABLE"
        assert snapshot_domain_tables(db_url) == snap_before_timeout

        # Case 5a: Session check upstream 503 outage -> fail-closed block & DB values 100% unchanged
        outage_503_checker = GoTrueLiveSessionVerifier(
            api_url=api_url,
            anon_key=anon_key,
            transport=_FixedStatusTransport(503),
            on_before_request=_assert_no_db_tx_during_session_check,
        )
        with TestClient(
            create_api_app(
                api_pool=api_pool,
                verifier=fixed_verifier,
                live_session_verifier=outage_503_checker,
            ),
            base_url="http://api.local",
        ) as outage_503_client:
            r_outage_503 = outage_503_client.delete(
                f"/api/v1/workspaces/{WS_A}/artifacts/{art_blocked_guard}",
                headers={"Authorization": f"Bearer {s2_access}"},
            )
            assert r_outage_503.status_code == 503
            assert r_outage_503.json()["detail"]["code"] == "SESSION_STATUS_UNAVAILABLE"
        assert snapshot_domain_tables(db_url) == snap_before_timeout

        # Case 5b: Session check network ConnectError -> fail-closed block & DB values 100% unchanged
        outage_conn_checker = GoTrueLiveSessionVerifier(
            api_url=api_url,
            anon_key=anon_key,
            transport=_ErrorTransport(httpx.ConnectError("simulated GoTrue unreachable")),
            on_before_request=_assert_no_db_tx_during_session_check,
        )
        with TestClient(
            create_api_app(
                api_pool=api_pool,
                verifier=fixed_verifier,
                live_session_verifier=outage_conn_checker,
            ),
            base_url="http://api.local",
        ) as outage_conn_client:
            r_outage_conn = outage_conn_client.delete(
                f"/api/v1/workspaces/{WS_A}/artifacts/{art_blocked_guard}",
                headers={"Authorization": f"Bearer {s2_access}"},
            )
            assert r_outage_conn.status_code == 503
            assert r_outage_conn.json()["detail"]["code"] == "SESSION_STATUS_UNAVAILABLE"
        assert snapshot_domain_tables(db_url) == snap_before_timeout

        # Case 6: Revoke WS_A membership while Session 2 is STILL active in GoTrue
        with psycopg.connect(db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app_t007d.workspace_memberships
                       SET revoked_at = now()
                     WHERE workspace_id = %s
                       AND subject_id = %s
                    """,
                    (str(WS_A), str(real_sub)),
                )

        # Confirm Session 2 is STILL active at GoTrue `/auth/v1/user`
        with httpx.Client(base_url=api_url, timeout=10.0) as auth_client:
            s2_user_check = auth_client.get(
                "/auth/v1/user",
                headers={"apikey": anon_key, "Authorization": f"Bearer {s2_access}"},
            )
            assert s2_user_check.status_code == 200

        snap_before_revoked = snapshot_domain_tables(db_url)
        with TestClient(live_app, base_url="http://api.local") as client:
            # WS_A delete MUST be blocked by DB active membership check with 0 DB changes
            r_revoked_wsa = client.delete(
                f"/api/v1/workspaces/{WS_A}/artifacts/{art_blocked_guard}",
                headers={"Authorization": f"Bearer {s2_access}"},
            )
            assert r_revoked_wsa.status_code == 403
            assert r_revoked_wsa.json()["detail"]["code"] == "WORKSPACE_FORBIDDEN"
            assert snapshot_domain_tables(db_url) == snap_before_revoked

            # Same Session 2 in active WS_B MUST still succeed, while WS_A guard artifact stays untouched
            r_active_wsb = client.delete(
                f"/api/v1/workspaces/{WS_B}/artifacts/{art_wsb_active}",
                headers={"Authorization": f"Bearer {s2_access}"},
            )
            assert r_active_wsb.status_code == 200
            assert r_active_wsb.json()["deleted_id"] == str(art_wsb_active)

        final_snap = snapshot_domain_tables(db_url)
        assert any(
            row[0] == art_blocked_guard and row[3] == "AUTHDB04 Guard Artifact" and row[4] == "must-remain-unchanged"
            for row in final_snap["artifacts"]
        ), "Guard artifact in WS_A must remain completely unchanged"
        assert all(row[0] != art_wsb_active for row in final_snap["artifacts"])

        # Confirm our no-open-DB-tx invariant ran on every single live/simulated session check
        assert len(tx_checks_during_gotrue) == 8
        assert all(pair == (0, 0) for pair in tx_checks_during_gotrue)

        print(
            "[AUTHDB-04 E2E delete observation] "
            f"s1_active_delete={r_s1_ok.status_code}, "
            f"s1_logout={logout_resp.status_code}, "
            f"s1_logged_out_unexpired_delete={r_s1_blocked.status_code}, "
            f"s2_other_active_session_delete={r_s2_ok.status_code}, "
            f"timeout_delete={r_timeout.status_code}, "
            f"outage_503_delete={r_outage_503.status_code}, "
            f"outage_conn_delete={r_outage_conn.status_code}, "
            f"revoked_wsa_delete={r_revoked_wsa.status_code}, "
            f"active_wsb_delete={r_active_wsb.status_code}, "
            f"db_tx_open_checks={len(tx_checks_during_gotrue)} (all 0 open tx / 0 checked out)"
        )
    finally:
        api_pool.close()
