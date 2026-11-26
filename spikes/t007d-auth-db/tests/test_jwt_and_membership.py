"""JWT verification, server-side membership enforcement, spoofing, concealment, and revocation tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from src.auth_tokens import EXPECTED_AUDIENCE, EXPECTED_ISSUER, SyntheticJwtAuthority
from tests.conftest import (
    ARTIFACT_A1,
    PROJECT_A1,
    PROJECT_B1,
    USER_ALICE_A,
    USER_BOB_B,
    USER_CHARLIE_REVOKED,
    USER_DANA_DUAL,
    WS_A,
    WS_B,
)


def test_normal_workspace_a_access_and_workspace_b_denial(api_client, jwt_authority) -> None:
    """Alice accesses WS_A normally; Bob is denied WS_A even when supplying WS_A in path or body."""
    alice_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_ALICE_A)
    bob_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_BOB_B)

    # Normal A access
    ok_resp = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    assert ok_resp.status_code == 200
    assert [i["id"] for i in ok_resp.json()["items"]] == [str(PROJECT_A1)]

    # Bob tries to access WS_A directly -> 403
    denied_resp = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert denied_resp.status_code == 403
    assert denied_resp.json()["detail"]["code"] == "WORKSPACE_FORBIDDEN"

    # Bob calls WS_B endpoint but tries to spoof workspace_id=WS_A in request body -> 403
    spoof_resp = api_client.post(
        f"/api/v1/workspaces/{WS_B}/projects",
        headers={"Authorization": f"Bearer {bob_jwt}"},
        json={"name": "spoofed-project", "workspace_id": str(WS_A)},
    )
    assert spoof_resp.status_code == 403
    assert spoof_resp.json()["detail"]["code"] == "WORKSPACE_MISMATCH"

    # Bob with a spoofed workspace_id=WS_A claim inside JWT is STILL denied WS_A
    bob_spoof_claim_jwt, _, _ = jwt_authority.issue_token(
        subject_id=USER_BOB_B,
        extra_claims={"workspace_id": str(WS_A)},
    )
    spoof_claim_resp = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {bob_spoof_claim_jwt}"},
    )
    assert spoof_claim_resp.status_code == 403
    assert spoof_claim_resp.json()["detail"]["code"] == "WORKSPACE_FORBIDDEN"


def test_concealment_and_cross_workspace_child_rejection_via_api(api_client, jwt_authority) -> None:
    """Out-of-workspace resource IDs and nonexistent IDs return identical 404 responses."""
    alice_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_ALICE_A)
    bob_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_BOB_B)
    nonexistent_id = uuid4()

    # Parent-mediated child listing: Alice sees ARTIFACT_A1; Bob querying PROJECT_A1 via WS_B sees []
    r_alice_via_parent = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects/{PROJECT_A1}/artifacts",
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    assert r_alice_via_parent.status_code == 200
    assert [i["id"] for i in r_alice_via_parent.json()["items"]] == [str(ARTIFACT_A1)]

    r_bob_via_parent = api_client.get(
        f"/api/v1/workspaces/{WS_B}/projects/{PROJECT_A1}/artifacts",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r_bob_via_parent.status_code == 200
    assert r_bob_via_parent.json()["items"] == []

    # Direct parent lookup: WS_A project ID vs nonexistent UUID -> identical 404
    r_cross_proj = api_client.get(
        f"/api/v1/workspaces/{WS_B}/projects/{PROJECT_A1}",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    r_missing_proj = api_client.get(
        f"/api/v1/workspaces/{WS_B}/projects/{nonexistent_id}",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r_cross_proj.status_code == 404
    assert r_missing_proj.status_code == 404
    assert r_cross_proj.json() == r_missing_proj.json()

    # Direct child lookup: WS_A artifact ID vs nonexistent UUID -> identical 404
    r_cross_art = api_client.get(
        f"/api/v1/workspaces/{WS_B}/artifacts/{ARTIFACT_A1}",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    r_missing_art = api_client.get(
        f"/api/v1/workspaces/{WS_B}/artifacts/{nonexistent_id}",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r_cross_art.status_code == 404
    assert r_missing_art.status_code == 404
    assert r_cross_art.json() == r_missing_art.json()

    # Cross-workspace child INSERT via API: rejected by Composite FK -> identical 404
    r_cross_insert = api_client.post(
        f"/api/v1/workspaces/{WS_B}/artifacts",
        headers={"Authorization": f"Bearer {bob_jwt}"},
        json={"project_id": str(PROJECT_A1), "title": "illegal-cross-ws-child"},
    )
    r_missing_insert = api_client.post(
        f"/api/v1/workspaces/{WS_B}/artifacts",
        headers={"Authorization": f"Bearer {bob_jwt}"},
        json={"project_id": str(nonexistent_id), "title": "missing-parent-child"},
    )
    assert r_cross_insert.status_code == 404
    assert r_missing_insert.status_code == 404
    assert r_cross_insert.json() == r_missing_insert.json()

    # Valid child insert in Bob's own workspace succeeds
    r_valid_insert = api_client.post(
        f"/api/v1/workspaces/{WS_B}/artifacts",
        headers={"Authorization": f"Bearer {bob_jwt}"},
        json={"project_id": str(PROJECT_B1), "title": "valid-b-child"},
    )
    assert r_valid_insert.status_code == 201
    assert r_valid_insert.json()["workspace_id"] == str(WS_B)


def test_jwt_validation_rejects_bad_sig_issuer_audience_expiry_alg_and_mixed_claims(
    api_client,
    jwt_authority,
) -> None:
    """Verify strict JWT signature, issuer, audience, expiry, algorithm, and separation-of-concerns checks."""
    # 1. Missing Bearer token
    r_missing = api_client.get(f"/api/v1/workspaces/{WS_A}/projects")
    assert r_missing.status_code == 401
    assert r_missing.json()["detail"]["code"] == "MISSING_BEARER"

    # 2. Tampered signature
    valid_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_ALICE_A)
    parts = valid_jwt.split(".")
    tampered_sig = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    tampered_jwt = f"{parts[0]}.{parts[1]}.{tampered_sig}"
    r_tampered = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {tampered_jwt}"},
    )
    assert r_tampered.status_code == 401
    assert r_tampered.json()["detail"]["code"] == "INVALID_SIGNATURE"

    # 3. Signed with untrusted RSA keypair
    rogue_authority = SyntheticJwtAuthority()
    wrong_key_jwt, _, _ = rogue_authority.issue_token(subject_id=USER_ALICE_A)
    r_wrong_key = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {wrong_key_jwt}"},
    )
    assert r_wrong_key.status_code == 401
    assert r_wrong_key.json()["detail"]["code"] == "INVALID_SIGNATURE"

    # 4. Algorithm confusion (HS256 instead of RS256)
    now = datetime.now(timezone.utc)
    hs256_jwt = jwt.encode(
        {
            "sub": str(USER_ALICE_A),
            "iss": EXPECTED_ISSUER,
            "aud": EXPECTED_AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": str(uuid4()),
            "session_id": "sess-hs256",
        },
        "synthetic-hmac-secret-not-allowed-32b!",
        algorithm="HS256",
    )
    r_alg = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {hs256_jwt}"},
    )
    assert r_alg.status_code == 401
    assert r_alg.json()["detail"]["code"] == "INVALID_ALGORITHM"

    # 5. Wrong issuer
    bad_iss_jwt, _, _ = jwt_authority.issue_token(
        subject_id=USER_ALICE_A,
        issuer="https://evil-issuer.example/auth/v1",
    )
    r_iss = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {bad_iss_jwt}"},
    )
    assert r_iss.status_code == 401
    assert r_iss.json()["detail"]["code"] == "INVALID_ISSUER"

    # 6. Wrong audience
    bad_aud_jwt, _, _ = jwt_authority.issue_token(
        subject_id=USER_ALICE_A,
        audience="service_role",
    )
    r_aud = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {bad_aud_jwt}"},
    )
    assert r_aud.status_code == 401
    assert r_aud.json()["detail"]["code"] == "INVALID_AUDIENCE"

    # 7. Expired JWT
    expired_jwt, _, _ = jwt_authority.issue_token(
        subject_id=USER_ALICE_A,
        expires_in_seconds=-30,
    )
    r_exp = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {expired_jwt}"},
    )
    assert r_exp.status_code == 401
    assert r_exp.json()["detail"]["code"] == "TOKEN_EXPIRED"

    # 8. Separation of concerns: reject login JWT smuggling GitHub token or LLM plan claim
    mixed_jwt, _, _ = jwt_authority.issue_token(
        subject_id=USER_ALICE_A,
        extra_claims={"github_token": "ghu_synthetic_do_not_mix", "llm_plan": "pro"},
    )
    r_mixed = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {mixed_jwt}"},
    )
    assert r_mixed.status_code == 401
    assert r_mixed.json()["detail"]["code"] == "MIXED_CONCERN_CLAIM"


def test_membership_revocation_denies_previously_valid_jwt_immediately(
    api_client,
    jwt_authority,
    superuser_conn,
) -> None:
    """Verify server checks current DB membership on every request (revoked member rejected immediately)."""
    # 1. Pre-revoked Charlie is denied even with a brand-new valid JWT
    charlie_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_CHARLIE_REVOKED)
    r_charlie = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {charlie_jwt}"},
    )
    assert r_charlie.status_code == 403
    assert r_charlie.json()["detail"]["code"] == "WORKSPACE_FORBIDDEN"

    # 2. Dana has active membership in WS_A and WS_B -> both initially succeed
    dana_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_DANA_DUAL)
    assert (
        api_client.get(
            f"/api/v1/workspaces/{WS_A}/projects",
            headers={"Authorization": f"Bearer {dana_jwt}"},
        ).status_code
        == 200
    )
    assert (
        api_client.get(
            f"/api/v1/workspaces/{WS_B}/projects",
            headers={"Authorization": f"Bearer {dana_jwt}"},
        ).status_code
        == 200
    )

    # 3. Revoke Dana's WS_A membership in DB
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

    # 4. With the EXACT SAME unexpired JWT, WS_A is immediately denied while WS_B remains allowed
    r_dana_a_after = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {dana_jwt}"},
    )
    assert r_dana_a_after.status_code == 403
    assert r_dana_a_after.json()["detail"]["code"] == "WORKSPACE_FORBIDDEN"

    r_dana_b_after = api_client.get(
        f"/api/v1/workspaces/{WS_B}/projects",
        headers={"Authorization": f"Bearer {dana_jwt}"},
    )
    assert r_dana_b_after.status_code == 200
