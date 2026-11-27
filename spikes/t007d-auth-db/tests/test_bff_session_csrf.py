"""Browser -> Web Server (BFF) -> Backend API boundary, HttpOnly cookie, CSRF, and logout caveat tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.bff_app import SESSION_COOKIE_NAME
from tests.conftest import (
    PROJECT_A1,
    PROJECT_B1,
    USER_ALICE_A,
    USER_BOB_B,
    WS_A,
    WS_B,
)


def _make_browser_client(bff_app) -> TestClient:
    return TestClient(bff_app, base_url="http://localhost:3000")


def test_bff_cookie_session_hides_bearer_token_and_enforces_workspace_boundary(bff_app) -> None:
    """Browser receives HttpOnly cookie only (never Bearer JWT) and accesses workspaces via BFF."""
    with _make_browser_client(bff_app) as alice_browser, _make_browser_client(bff_app) as bob_browser:
        # 1. Alice synthetic login -> HttpOnly SameSite=Lax cookie set, no JWT in response body
        login_a = alice_browser.post(
            "/bff/auth/synthetic-login",
            json={"subject_id": str(USER_ALICE_A)},
        )
        assert login_a.status_code == 200
        set_cookie_header = login_a.headers.get("set-cookie", "")
        assert f"{SESSION_COOKIE_NAME}=" in set_cookie_header
        assert "httponly" in set_cookie_header.lower()
        assert "samesite=lax" in set_cookie_header.lower()

        login_a_body = login_a.json()
        assert "access_token" not in login_a_body
        assert "refresh_token" not in login_a_body
        assert "eyJ" not in login_a.text
        csrf_a = login_a_body["csrf_token"]

        # 2. Bob synthetic login
        login_b = bob_browser.post(
            "/bff/auth/synthetic-login",
            json={"subject_id": str(USER_BOB_B)},
        )
        assert login_b.status_code == 200

        # 3. Alice reads WS_A via BFF -> succeeds; reads WS_B -> 403
        r_a_ok = alice_browser.get(f"/bff/workspaces/{WS_A}/projects")
        assert r_a_ok.status_code == 200
        assert [i["id"] for i in r_a_ok.json()["items"]] == [str(PROJECT_A1)]

        r_a_denied = alice_browser.get(f"/bff/workspaces/{WS_B}/projects")
        assert r_a_denied.status_code == 403

        # 4. Bob reads WS_B via BFF on the SAME physical DB connection -> succeeds; reads WS_A -> 403
        r_b_ok = bob_browser.get(f"/bff/workspaces/{WS_B}/projects")
        assert r_b_ok.status_code == 200
        assert [i["id"] for i in r_b_ok.json()["items"]] == [str(PROJECT_B1)]
        assert r_a_ok.headers["X-T007D-Backend-PID"] == r_b_ok.headers["X-T007D-Backend-PID"]

        r_b_denied = bob_browser.get(f"/bff/workspaces/{WS_A}/projects")
        assert r_b_denied.status_code == 403

        # 5. State-changing POST via BFF with valid Origin + CSRF token succeeds
        r_create = alice_browser.post(
            f"/bff/workspaces/{WS_A}/projects",
            headers={
                "Origin": "http://localhost:3000",
                "X-CSRF-Token": csrf_a,
            },
            json={"name": "BFF Created Repo"},
        )
        assert r_create.status_code == 201
        assert r_create.json()["workspace_id"] == str(WS_A)


def test_bff_csrf_protection_blocks_missing_or_invalid_origin_and_token(bff_app) -> None:
    """Verify local Origin allowlist and synchronizer X-CSRF-Token enforcement on POST."""
    with _make_browser_client(bff_app) as browser:
        login = browser.post(
            "/bff/auth/synthetic-login",
            json={"subject_id": str(USER_ALICE_A)},
        )
        csrf_token = login.json()["csrf_token"]

        # 1. Missing Origin -> 403
        r_no_origin = browser.post(
            f"/bff/workspaces/{WS_A}/projects",
            headers={"X-CSRF-Token": csrf_token},
            json={"name": "no-origin"},
        )
        assert r_no_origin.status_code == 403
        assert r_no_origin.json()["detail"]["code"] == "CSRF_ORIGIN_MISMATCH"

        # 2. Cross-site Origin -> 403
        r_evil_origin = browser.post(
            f"/bff/workspaces/{WS_A}/projects",
            headers={
                "Origin": "https://attacker.example",
                "X-CSRF-Token": csrf_token,
            },
            json={"name": "evil-origin"},
        )
        assert r_evil_origin.status_code == 403
        assert r_evil_origin.json()["detail"]["code"] == "CSRF_ORIGIN_MISMATCH"

        # 3. Valid Origin but missing X-CSRF-Token -> 403
        r_no_csrf = browser.post(
            f"/bff/workspaces/{WS_A}/projects",
            headers={"Origin": "http://localhost:3000"},
            json={"name": "no-csrf"},
        )
        assert r_no_csrf.status_code == 403
        assert r_no_csrf.json()["detail"]["code"] == "CSRF_TOKEN_INVALID"

        # 4. Valid Origin but wrong X-CSRF-Token -> 403
        r_bad_csrf = browser.post(
            f"/bff/workspaces/{WS_A}/projects",
            headers={
                "Origin": "http://localhost:3000",
                "X-CSRF-Token": "wrong-csrf-token",
            },
            json={"name": "bad-csrf"},
        )
        assert r_bad_csrf.status_code == 403
        assert r_bad_csrf.json()["detail"]["code"] == "CSRF_TOKEN_INVALID"


def test_synthetic_logout_with_and_without_server_revocation_model(bff_app, api_client) -> None:
    """Distinguish BFF session deletion + synthetic revocation model from stateless JWT lifetime.

    Proves explicitly:
    - Case A (propagate_revocation=True): BFF cookie AND direct JWT both rejected via synthetic denylist.
    - Case B (propagate_revocation=False): BFF cookie rejected immediately, BUT an already-issued
      unexpired JWT sent directly to a stateless API remains accepted until `exp`.
      Therefore, local synthetic logout does NOT prove immediate Supabase JWT invalidation.
    """
    # Case A: BFF logout + synthetic revocation store
    with _make_browser_client(bff_app) as browser_a:
        login_a = browser_a.post(
            "/bff/auth/synthetic-login",
            json={"subject_id": str(USER_ALICE_A)},
        )
        csrf_a = login_a.json()["csrf_token"]
        cookie_id_a = browser_a.cookies.get(SESSION_COOKIE_NAME)
        assert cookie_id_a is not None
        leaked_jwt_a = bff_app.state._debug_sessions[cookie_id_a].backend_jwt

        logout_a = browser_a.post(
            "/bff/auth/logout?propagate_revocation=true",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_a},
        )
        assert logout_a.status_code == 200
        assert logout_a.json()["synthetic_revocation_propagated"] is True

        # BFF rejects subsequent cookie usage
        assert browser_a.get(f"/bff/workspaces/{WS_A}/projects").status_code == 401
        # Direct API rejects captured JWT because synthetic revocation store recorded session_id
        r_direct_a = api_client.get(
            f"/api/v1/workspaces/{WS_A}/projects",
            headers={"Authorization": f"Bearer {leaked_jwt_a}"},
        )
        assert r_direct_a.status_code == 401
        assert r_direct_a.json()["detail"]["code"] == "SESSION_REVOKED"

    # Case B: BFF logout WITHOUT backend denylist (illustrating stateless JWT caveat)
    with _make_browser_client(bff_app) as browser_b:
        login_b = browser_b.post(
            "/bff/auth/synthetic-login",
            json={"subject_id": str(USER_ALICE_A)},
        )
        csrf_b = login_b.json()["csrf_token"]
        cookie_id_b = browser_b.cookies.get(SESSION_COOKIE_NAME)
        assert cookie_id_b is not None
        leaked_jwt_b = bff_app.state._debug_sessions[cookie_id_b].backend_jwt

        logout_b = browser_b.post(
            "/bff/auth/logout?propagate_revocation=false",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_b},
        )
        assert logout_b.status_code == 200
        assert logout_b.json()["synthetic_revocation_propagated"] is False

        # BFF session cookie is dead immediately
        assert browser_b.get(f"/bff/workspaces/{WS_A}/projects").status_code == 401
        # Direct API with purely stateless verification STILL accepts unexpired JWT until exp!
        r_direct_b = api_client.get(
            f"/api/v1/workspaces/{WS_A}/projects",
            headers={"Authorization": f"Bearer {leaked_jwt_b}"},
        )
        assert r_direct_b.status_code == 200
