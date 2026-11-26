"""OD010-5 & OD010-6 Local Integration Tests: Logout, Session Liveness Fail-Closed, and Membership Revocation.

Verifies independently on local PostgreSQL 18 + FastAPI BFF/API:
1. Normal logout:
   - Browser session cookie is removed (`Max-Age=0` / absent on subsequent calls)
   - Refresh token re-issuance is denied (`REFRESH_REVOKED`)
   - Existing pre-logout access JWT STILL passes stateless cryptographic signature/claim check before `exp`
   - Sensitive verification paths (Approval, Export, Delete, Source Connect/Revoke) BLOCK that same JWT
     once the session is terminated
2. Unconfirmable session status (fail-closed):
   - Even when the access JWT passes cryptographic signature verification before `exp`,
     if current session status cannot be confirmed (verifier outage or unknown session),
     all 4 sensitive verification paths FAIL CLOSED (blocked, 0 DB deletion)
3. Failed logout request vs Normal logout distinction:
   - When upstream session termination fails, logout explicitly reports failure
     (`logout_completed=False`, `upstream_session_terminated=False`) and is NOT conflated with clean logout
4. OD010-6 Workspace membership revocation:
   - Revoking Dana's WS_A membership immediately blocks WS_A (read + all sensitive routes)
     on the next request using the SAME unexpired active JWT, while Dana's active WS_B succeeds.

NOTE: HTTP status/error codes observed here are spike-only test assertions and do NOT lock product API contracts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth_tokens import TokenVerificationError
from src.bff_app import SESSION_COOKIE_NAME
from tests.conftest import (
    ARTIFACT_A1,
    ARTIFACT_B1,
    PROJECT_A1,
    PROJECT_B1,
    USER_ALICE_A,
    USER_DANA_DUAL,
    WS_A,
    WS_B,
)


def _call_all_sensitive_paths(
    api_client: TestClient,
    *,
    workspace_id,
    artifact_id,
    bearer_jwt: str,
) -> dict[str, tuple[int, dict]]:
    headers = {"Authorization": f"Bearer {bearer_jwt}"}
    results = {}
    r_approve = api_client.post(
        f"/api/v1/workspaces/{workspace_id}/approvals",
        headers=headers,
        json={"target_id": "item-1"},
    )
    results["approval"] = (r_approve.status_code, r_approve.json())

    r_export = api_client.post(
        f"/api/v1/workspaces/{workspace_id}/exports",
        headers=headers,
        json={"target_id": "export-1"},
    )
    results["export"] = (r_export.status_code, r_export.json())

    r_delete = api_client.delete(
        f"/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}",
        headers=headers,
    )
    results["delete"] = (r_delete.status_code, r_delete.json())

    r_src_connect = api_client.post(
        f"/api/v1/workspaces/{workspace_id}/sources/connect",
        headers=headers,
        json={"target_id": "repo-1"},
    )
    results["source_connect"] = (r_src_connect.status_code, r_src_connect.json())

    r_src_revoke = api_client.post(
        f"/api/v1/workspaces/{workspace_id}/sources/revoke",
        headers=headers,
        json={"target_id": "repo-1"},
    )
    results["source_revoke"] = (r_src_revoke.status_code, r_src_revoke.json())

    return results


def test_od010_5_logout_clears_cookie_blocks_refresh_keeps_jwt_sig_valid_but_blocks_sensitive_paths(
    lifecycle_bff_app,
    lifecycle_api_client: TestClient,
    lifecycle_verifier,
    jwt_authority,
    session_lifecycle,
    superuser_conn,
) -> None:
    """Separately verify cookie removal, refresh denial, pre-exp JWT signature validity, and sensitive-path blocking."""
    with TestClient(lifecycle_bff_app, base_url="http://localhost:3000") as browser:
        # 1. Login and confirm active session works before logout
        login = browser.post("/bff/auth/synthetic-login", json={"subject_id": str(USER_ALICE_A)})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        cookie_id = browser.cookies.get(SESSION_COOKIE_NAME)
        assert cookie_id is not None

        session_rec = lifecycle_bff_app.state._debug_sessions[cookie_id]
        pre_logout_jwt = session_rec.backend_jwt
        pre_logout_refresh = session_rec.refresh_token
        assert pre_logout_refresh is not None

        # Pre-logout: approval succeeds with active session
        pre_approve = lifecycle_api_client.post(
            f"/api/v1/workspaces/{WS_A}/approvals",
            headers={"Authorization": f"Bearer {pre_logout_jwt}"},
            json={"target_id": "pre-logout-ok"},
        )
        assert pre_approve.status_code == 200

        # 2. Perform clean logout
        logout = browser.post(
            "/bff/auth/logout",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf},
        )
        assert logout.status_code == 200
        logout_body = logout.json()
        assert logout_body["logged_out"] is True
        assert logout_body["bff_session_deleted"] is True
        assert logout_body["upstream_session_terminated"] is True

        # 2a. Browser cookie removed (Set-Cookie deletion + subsequent BFF request rejected)
        set_cookie_hdr = logout.headers.get("set-cookie", "").lower()
        assert SESSION_COOKIE_NAME in set_cookie_hdr
        assert "max-age=0" in set_cookie_hdr
        assert browser.cookies.get(SESSION_COOKIE_NAME) in (None, "")
        assert browser.get(f"/bff/workspaces/{WS_A}/projects").status_code == 401

        # 2b. Refresh token re-issuance is denied after logout
        with pytest.raises(TokenVerificationError) as refresh_exc:
            session_lifecycle.refresh_access_token(
                refresh_token=pre_logout_refresh,
                authority=jwt_authority,
            )
        assert refresh_exc.value.code == "REFRESH_REVOKED"

        # 2c. Existing pre-logout access JWT STILL passes cryptographic signature/claim check before `exp`
        stateless_principal = lifecycle_verifier.verify_stateless(pre_logout_jwt)
        assert stateless_principal.subject_id == USER_ALICE_A

        # 2d. YET all 4 sensitive verification paths (approval, export, delete, source connect/revoke) BLOCK it!
        sensitive_results = _call_all_sensitive_paths(
            lifecycle_api_client,
            workspace_id=WS_A,
            artifact_id=ARTIFACT_A1,
            bearer_jwt=pre_logout_jwt,
        )
        for action, (status_code, body) in sensitive_results.items():
            assert status_code == 401, f"{action} must be blocked after session termination"
            assert body["detail"]["code"] == "SESSION_TERMINATED"

        # Confirm ARTIFACT_A1 was NOT deleted
        with superuser_conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.artifacts WHERE id = %s", (str(ARTIFACT_A1),))
            assert cur.fetchone() is not None


def test_od010_5_unconfirmable_session_status_fails_closed_on_all_sensitive_paths(
    lifecycle_api_client: TestClient,
    lifecycle_verifier,
    jwt_authority,
    session_lifecycle,
    superuser_conn,
) -> None:
    """When live session status cannot be confirmed (outage or unknown), sensitive paths fail closed."""
    active_jwt, sid, _ = jwt_authority.issue_token(subject_id=USER_ALICE_A)
    session_lifecycle.register_session(session_id=sid, subject_id=USER_ALICE_A)

    # Confirm cryptographic signature/claims pass before `exp`
    assert lifecycle_verifier.verify_stateless(active_jwt).subject_id == USER_ALICE_A

    # Case 1: Session status verifier outage -> fail closed on every sensitive path
    session_lifecycle.simulate_status_unavailable = True
    try:
        outage_results = _call_all_sensitive_paths(
            lifecycle_api_client,
            workspace_id=WS_A,
            artifact_id=ARTIFACT_A1,
            bearer_jwt=active_jwt,
        )
        for action, (status_code, body) in outage_results.items():
            assert status_code == 503, f"{action} must fail closed when session status is unavailable"
            assert body["detail"]["code"] == "SESSION_STATUS_UNAVAILABLE"
    finally:
        session_lifecycle.simulate_status_unavailable = False

    # Case 2: Validly signed JWT referencing an unknown/unregistered session -> fail closed
    unknown_jwt, _unknown_sid, _ = jwt_authority.issue_token(subject_id=USER_ALICE_A)
    assert lifecycle_verifier.verify_stateless(unknown_jwt).subject_id == USER_ALICE_A
    unknown_results = _call_all_sensitive_paths(
        lifecycle_api_client,
        workspace_id=WS_A,
        artifact_id=ARTIFACT_A1,
        bearer_jwt=unknown_jwt,
    )
    for action, (status_code, body) in unknown_results.items():
        assert status_code == 401, f"{action} must fail closed when session cannot be confirmed"
        assert body["detail"]["code"] == "SESSION_STATUS_UNKNOWN"

    # Confirm ARTIFACT_A1 remains intact
    with superuser_conn.cursor() as cur:
        cur.execute("SELECT id FROM app_t007d.artifacts WHERE id = %s", (str(ARTIFACT_A1),))
        assert cur.fetchone() is not None


def test_od010_5_failed_logout_request_is_distinguished_from_normal_logout(
    lifecycle_bff_app,
    session_lifecycle,
) -> None:
    """Upstream session termination failure must NOT be reported as a clean logout."""
    with TestClient(lifecycle_bff_app, base_url="http://localhost:3000") as browser:
        login = browser.post("/bff/auth/synthetic-login", json={"subject_id": str(USER_ALICE_A)})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        cookie_id = browser.cookies.get(SESSION_COOKIE_NAME)
        assert cookie_id is not None
        sid = lifecycle_bff_app.state._debug_sessions[cookie_id].auth_session_id

        # Simulate upstream logout endpoint failure
        session_lifecycle.simulate_terminate_failure = True
        try:
            failed_logout = browser.post(
                "/bff/auth/logout",
                headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf},
            )
            assert failed_logout.status_code == 503
            detail = failed_logout.json()["detail"]
            assert detail["code"] == "LOGOUT_UPSTREAM_FAILED"
            assert detail["logout_completed"] is False
            assert detail["upstream_session_terminated"] is False
            # Upstream session was NOT terminated
            assert session_lifecycle.check_session_status(sid) == "active"
        finally:
            session_lifecycle.simulate_terminate_failure = False

        # Once upstream recovers, clean logout succeeds and reports completion explicitly
        clean_logout = browser.post(
            "/bff/auth/logout",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf},
        )
        assert clean_logout.status_code == 200
        assert clean_logout.json()["logged_out"] is True
        assert clean_logout.json()["upstream_session_terminated"] is True
        assert session_lifecycle.check_session_status(sid) == "terminated"


def test_od010_6_membership_revocation_blocks_only_revoked_workspace_on_same_active_jwt(
    lifecycle_api_client: TestClient,
    jwt_authority,
    session_lifecycle,
    superuser_conn,
) -> None:
    """Revoking Dana's WS_A membership blocks WS_A immediately on next call while WS_B stays active."""
    dana_jwt, sid, _ = jwt_authority.issue_token(subject_id=USER_DANA_DUAL)
    session_lifecycle.register_session(session_id=sid, subject_id=USER_DANA_DUAL)
    headers = {"Authorization": f"Bearer {dana_jwt}"}

    # Before revocation: both WS_A and WS_B succeed on read & sensitive paths
    assert lifecycle_api_client.get(f"/api/v1/workspaces/{WS_A}/projects", headers=headers).status_code == 200
    assert lifecycle_api_client.get(f"/api/v1/workspaces/{WS_B}/projects", headers=headers).status_code == 200
    assert (
        lifecycle_api_client.post(
            f"/api/v1/workspaces/{WS_A}/approvals",
            headers=headers,
            json={"target_id": "before-revoke"},
        ).status_code
        == 200
    )

    # Revoke Dana's WS_A membership in DB (leave session active and WS_B active)
    with superuser_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app_t007d.workspace_memberships
               SET revoked_at = now()
             WHERE workspace_id = %s AND subject_id = %s
            """,
            (str(WS_A), str(USER_DANA_DUAL)),
        )
    superuser_conn.commit()

    # Next request with the EXACT SAME active JWT: WS_A read + all sensitive paths immediately blocked
    r_ws_a_read = lifecycle_api_client.get(f"/api/v1/workspaces/{WS_A}/projects", headers=headers)
    assert r_ws_a_read.status_code == 403
    assert r_ws_a_read.json()["detail"]["code"] == "WORKSPACE_FORBIDDEN"

    ws_a_sensitive = _call_all_sensitive_paths(
        lifecycle_api_client,
        workspace_id=WS_A,
        artifact_id=ARTIFACT_A1,
        bearer_jwt=dana_jwt,
    )
    for action, (status_code, body) in ws_a_sensitive.items():
        assert status_code == 403, f"Revoked WS_A {action} must be denied"
        assert body["detail"]["code"] == "WORKSPACE_FORBIDDEN"

    # Active WS_B remains completely functional on both standard and sensitive paths with the SAME JWT
    r_ws_b_read = lifecycle_api_client.get(f"/api/v1/workspaces/{WS_B}/projects", headers=headers)
    assert r_ws_b_read.status_code == 200
    assert [i["id"] for i in r_ws_b_read.json()["items"]] == [str(PROJECT_B1)]

    ws_b_sensitive = _call_all_sensitive_paths(
        lifecycle_api_client,
        workspace_id=WS_B,
        artifact_id=ARTIFACT_B1,
        bearer_jwt=dana_jwt,
    )
    assert ws_b_sensitive["approval"][0] == 200
    assert ws_b_sensitive["export"][0] == 200
    assert ws_b_sensitive["delete"][0] == 200
    assert ws_b_sensitive["source_connect"][0] == 200
    assert ws_b_sensitive["source_revoke"][0] == 200
