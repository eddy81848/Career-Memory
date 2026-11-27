"""Unit & contract tests for JWT/JWKS verification, live sessions, return_path, and redaction."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from career_memory_api.auth.http import (
    AUTH_NO_STORE_CACHE_CONTROL,
    build_auth_error_response,
    install_auth_error_handlers,
)
from career_memory_api.auth.jwt_verifier import (
    JwkKey,
    JwksSnapshot,
    JwksUnavailableError,
    JwtAccessTokenVerifier,
    JwtVerificationConfig,
    LiveSessionStatus,
    VerifiedJwtClaims,
)
from career_memory_api.auth.redaction import (
    RedactingLogFilter,
    redact_headers,
    redact_mapping,
    redact_text,
    redact_url_query,
)
from career_memory_api.auth.return_path import (
    InvalidReturnPathError,
    validate_return_path,
)
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthenticatedUser,
    AuthSubject,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceAccessContext,
    WorkspaceMembership,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.dependencies import get_request_id, require_workspace_roles
from career_memory_api.workspace_resources import (
    WorkspaceResource,
    WorkspaceResourceNotFoundError,
    ensure_workspace_resource_ownership,
)
from tests.auth.crypto_helpers import (
    Es256TestKeyPair,
    Rs256TestKeyPair,
    b64url_encode,
    sign_hs256_jwt,
    sign_unsigned_none_jwt,
)

_ISSUER = "https://auth.career-memory.local/auth/v1"
_AUDIENCE = "authenticated"


class _StubJwksProvider:
    def __init__(
        self,
        snapshots: list[JwksSnapshot | Exception],
    ) -> None:
        self._snapshots = list(snapshots)
        self.calls: list[bool] = []

    def get_jwks(self, *, force_refresh: bool = False) -> JwksSnapshot:
        self.calls.append(force_refresh)
        if not self._snapshots:
            raise JwksUnavailableError("no snapshot configured")
        item = self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]
        if isinstance(item, Exception):
            raise item
        return item


def _now() -> datetime:
    return datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)


def _base_claims(now: datetime | None = None) -> dict[str, Any]:
    ts = int((now or _now()).timestamp())
    return {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": "sub-user-001",
        "exp": ts + 300,
        "iat": ts - 10,
        "nbf": ts - 5,
        "session_id": "sess-001",
        "email": "alice@example.com",
    }


def test_jwt_config_rejects_none_and_symmetric_algorithms() -> None:
    for forbidden_alg in ("none", "NONE", "HS256", "HS384", "HS512"):
        with pytest.raises(ValueError):
            JwtVerificationConfig(
                expected_issuer=_ISSUER,
                expected_audience=_AUDIENCE,
                allowed_algorithms=frozenset({forbidden_alg}),
            )


def test_es256_and_rs256_valid_tokens_verify_cleanly() -> None:
    now = _now()
    es_key = Es256TestKeyPair.generate("kid-es-1", seed=42)
    rs_key = Rs256TestKeyPair.generate("kid-rs-1")

    snapshot = JwksSnapshot(
        keys=(
            JwkKey.from_dict(es_key.to_jwk()),
            JwkKey.from_dict(rs_key.to_jwk()),
        ),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    provider = _StubJwksProvider([snapshot])
    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=provider,
        clock=lambda: now,
    )

    es_token = es_key.sign_jwt(_base_claims(now))
    es_claims = verifier.verify(es_token)
    assert es_claims.auth_subject.issuer == _ISSUER
    assert es_claims.auth_subject.subject == "sub-user-001"
    assert es_claims.session_id == "sess-001"

    rs_payload = _base_claims(now)
    rs_payload["aud"] = ["other-aud", _AUDIENCE]
    rs_token = rs_key.sign_jwt(rs_payload)
    rs_claims = verifier.verify(rs_token)
    assert rs_claims.auth_subject.issuer == _ISSUER
    assert rs_claims.auth_subject.subject == "sub-user-001"
    assert _AUDIENCE in rs_claims.audience


def test_algorithm_confusion_and_header_injection_are_rejected() -> None:
    now = _now()
    es_key = Es256TestKeyPair.generate("kid-es-1", seed=99)
    snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(es_key.to_jwk()),),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    provider = _StubJwksProvider([snapshot])
    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=provider,
        clock=lambda: now,
    )

    claims = _base_claims(now)
    attack_tokens = [
        sign_unsigned_none_jwt(claims, kid="kid-es-1"),
        sign_hs256_jwt(claims, secret=b"super-secret-hmac", kid="kid-es-1"),
        es_key.sign_jwt(
            claims,
            header_overrides={"jku": "https://evil.example/jwks.json"},
        ),
        es_key.sign_jwt(
            claims,
            header_overrides={"x5u": "https://evil.example/cert.pem"},
        ),
        es_key.sign_jwt(
            claims,
            header_overrides={"jwk": es_key.to_jwk()},
        ),
    ]

    for bad_token in attack_tokens:
        with pytest.raises(UnauthenticatedError) as exc_info:
            verifier.verify(bad_token)
        assert bad_token not in str(exc_info.value)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda c, now: {**c, "iss": "https://evil-issuer.example/auth/v1"},
        lambda c, now: {**c, "aud": "wrong-audience"},
        lambda c, now: {**c, "exp": int((now - timedelta(seconds=120)).timestamp())},
        lambda c, now: {k: v for k, v in c.items() if k != "exp"},
        lambda c, now: {**c, "nbf": int((now + timedelta(seconds=120)).timestamp())},
        lambda c, now: {**c, "sub": ""},
        lambda c, now: {k: v for k, v in c.items() if k != "sub"},
    ],
)
def test_invalid_jwt_claims_are_rejected_without_leaking_token(mutator: Any) -> None:
    now = _now()
    es_key = Es256TestKeyPair.generate("kid-es-1", seed=123)
    snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(es_key.to_jwk()),),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=_StubJwksProvider([snapshot]),
        clock=lambda: now,
    )

    bad_payload = mutator(_base_claims(now), now)
    token = es_key.sign_jwt(bad_payload)
    with pytest.raises(UnauthenticatedError) as exc_info:
        verifier.verify(token)
    assert token not in str(exc_info.value)


def test_tampered_payload_or_signature_is_rejected() -> None:
    now = _now()
    es_key = Es256TestKeyPair.generate("kid-es-1", seed=77)
    snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(es_key.to_jwk()),),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=_StubJwksProvider([snapshot]),
        clock=lambda: now,
    )
    valid_token = es_key.sign_jwt(_base_claims(now))
    h, p, s = valid_token.split(".")

    # Tamper payload while keeping signature
    tampered_payload = b64url_encode(b'{"iss":"https://auth.career-memory.local/auth/v1","aud":"authenticated","sub":"admin","exp":9999999999}')
    with pytest.raises(UnauthenticatedError):
        verifier.verify(f"{h}.{tampered_payload}.{s}")

    # Tamper signature bit
    flipped_sig = ("A" if s[0] != "A" else "B") + s[1:]
    with pytest.raises(UnauthenticatedError):
        verifier.verify(f"{h}.{p}.{flipped_sig}")


def test_unknown_kid_triggers_bounded_single_refresh_then_fails_closed() -> None:
    now = _now()
    old_key = Es256TestKeyPair.generate("kid-old", seed=10)
    new_key = Es256TestKeyPair.generate("kid-new", seed=20)
    unknown_key = Es256TestKeyPair.generate("kid-unknown", seed=30)

    old_snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(old_key.to_jwk()),),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    rotated_snapshot = JwksSnapshot(
        keys=(
            JwkKey.from_dict(old_key.to_jwk()),
            JwkKey.from_dict(new_key.to_jwk()),
        ),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    # Case 1: Rotated key appears after bounded single refresh
    provider_rotate = _StubJwksProvider([old_snapshot, rotated_snapshot])
    verifier_rotate = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=provider_rotate,
        clock=lambda: now,
    )
    claims = verifier_rotate.verify(new_key.sign_jwt(_base_claims(now)))
    assert claims.subject == "sub-user-001"
    assert provider_rotate.calls == [False, True]

    # Case 2: Unknown kid stays missing after 1 refresh -> UnauthenticatedError,
    # and repeated unknown-kid probes within cooldown do not spam JWKS refreshes.
    provider_missing = _StubJwksProvider([old_snapshot, old_snapshot])
    verifier_missing = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
            unknown_kid_refresh_cooldown_seconds=30.0,
        ),
        jwks_provider=provider_missing,
        clock=lambda: now,
    )
    with pytest.raises(UnauthenticatedError):
        verifier_missing.verify(unknown_key.sign_jwt(_base_claims(now)))
    assert provider_missing.calls == [False, True]

    another_unknown_key = Es256TestKeyPair.generate("kid-unknown-2", seed=31)
    with pytest.raises(UnauthenticatedError):
        verifier_missing.verify(another_unknown_key.sign_jwt(_base_claims(now)))
    # Still only 1 forced refresh despite repeated unknown kids
    assert provider_missing.calls == [False, True]


def test_jwks_outage_does_not_accept_with_expired_cache() -> None:
    t0 = _now()
    current_time = [t0]
    es_key = Es256TestKeyPair.generate("kid-es-1", seed=55)

    initial_snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(es_key.to_jwk()),),
        fetched_at=t0,
        expires_at=t0 + timedelta(seconds=60),
    )
    provider = _StubJwksProvider(
        [initial_snapshot, JwksUnavailableError("jwks endpoint 503")]
    )
    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=provider,
        clock=lambda: current_time[0],
    )

    token = es_key.sign_jwt(_base_claims(t0))
    # Valid while cache is fresh
    assert verifier.verify(token).subject == "sub-user-001"

    # Advance past cache expiration -> must refresh and fail-closed with 503-class error
    current_time[0] = t0 + timedelta(seconds=61)
    with pytest.raises(AccessVerificationUnavailableError):
        verifier.verify(token)


def test_live_session_verifier_distinguishes_terminated_401_from_unavailable_503() -> None:
    now = _now()
    es_key = Es256TestKeyPair.generate("kid-es-1", seed=66)
    snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(es_key.to_jwk()),),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=_StubJwksProvider([snapshot]),
        clock=lambda: now,
    )
    token = es_key.sign_jwt(_base_claims(now))

    class _TerminatedSessionVerifier:
        def verify_live_session(self, claims: VerifiedJwtClaims) -> LiveSessionStatus:
            return LiveSessionStatus.SESSION_TERMINATED

    class _OutageSessionVerifier:
        def verify_live_session(self, claims: VerifiedJwtClaims) -> LiveSessionStatus:
            raise TimeoutError("supabase auth session lookup timed out")

    with pytest.raises(SessionTerminatedError):
        verifier.verify(token, live_session_verifier=_TerminatedSessionVerifier())

    with pytest.raises(AccessVerificationUnavailableError):
        verifier.verify(token, live_session_verifier=_OutageSessionVerifier())


@pytest.mark.parametrize(
    "valid_path",
    [
        "/",
        "/settings/sources",
        "/workspaces/ws-123/imports?tab=github",
        "/onboarding/connect",
    ],
)
def test_return_path_accepts_safe_relative_paths(valid_path: str) -> None:
    assert validate_return_path(valid_path) == valid_path


@pytest.mark.parametrize(
    "evil_path",
    [
        "",
        "   ",
        "https://evil.example/phish",
        "http://evil.example",
        "//evil.example/steal",
        "///evil.example",
        "/\\evil.example",
        "/%2f/evil.example",
        "/%5cevil.example",
        "javascript:alert(1)",
        "settings/sources",
        "/ok\r\nSet-Cookie: session=evil",
        "/ok?next=https://evil.example",
        "/user@evil.example",
    ],
)
def test_return_path_rejects_open_redirects_and_injections(evil_path: str) -> None:
    with pytest.raises(InvalidReturnPathError):
        validate_return_path(evil_path)


def test_redaction_strips_tokens_cookies_oauth_params_and_log_records() -> None:
    raw_jwt = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.c2lnbmF0dXJlMTIzNDU2"
    raw_gh = "gho_1234567890abcdefSECRETTOKEN"
    raw_code = "gh_code_secret_98765"
    raw_state = "oauth_state_secret_54321"
    raw_cookie = "__Host-session=topsecretcookievalue"

    text = (
        f"Authorization: Bearer {raw_jwt}; Cookie: {raw_cookie}; "
        f"callback?code={raw_code}&state={raw_state}; token={raw_gh}"
    )
    redacted = redact_text(text)
    for secret in (raw_jwt, raw_gh, raw_code, raw_state, "topsecretcookievalue"):
        assert secret not in redacted

    headers = redact_headers(
        {
            "Authorization": f"Bearer {raw_jwt}",
            "Cookie": raw_cookie,
            "Set-Cookie": "__Host-session=xyz; HttpOnly",
            "X-Request-ID": "req-123",
        }
    )
    assert headers["Authorization"] == "[REDACTED]"
    assert headers["Cookie"] == "[REDACTED]"
    assert headers["Set-Cookie"] == "[REDACTED]"
    assert headers["X-Request-ID"] == "req-123"

    url = redact_url_query(
        f"/oauth/callback?code={raw_code}&state={raw_state}&provider=github"
    )
    assert raw_code not in url
    assert raw_state not in url
    assert "provider=github" in url

    mapping = redact_mapping(
        {
            "access_token": raw_gh,
            "refresh_token": "rt_secret",
            "code_verifier": "pkce_secret",
            "nested": {"state": raw_state, "ok": "visible"},
        }
    )
    assert mapping["access_token"] == "[REDACTED]"
    assert mapping["refresh_token"] == "[REDACTED]"
    assert mapping["code_verifier"] == "[REDACTED]"
    assert mapping["nested"]["state"] == "[REDACTED]"
    assert mapping["nested"]["ok"] == "visible"

    # Log filter redacts formatted messages and args
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Failed auth with Bearer %s and code=%s",
        args=(raw_jwt, raw_code),
        exc_info=None,
    )
    log_filter = RedactingLogFilter()
    assert log_filter.filter(record) is True
    rendered = record.getMessage()
    assert raw_jwt not in rendered
    assert raw_code not in rendered


def test_http_auth_error_responses_enforce_private_no_store_and_redacted_envelopes() -> None:
    app = FastAPI()
    install_auth_error_handlers(app)

    @app.get("/_test/session-ended")
    async def session_ended() -> None:
        raise SessionTerminatedError("raw-secret-should-not-echo")

    @app.get("/_test/ws-denied")
    async def ws_denied() -> None:
        raise WorkspaceMembershipDeniedError("denied")

    @app.get("/_test/concealed")
    async def concealed() -> None:
        raise WorkspaceResourceNotFoundError()

    @app.get("/_test/auth-down")
    async def auth_down() -> None:
        raise AccessVerificationUnavailableError("jwks timeout")

    with TestClient(app) as client:
        r401 = client.get("/_test/session-ended", headers={"X-Request-ID": "req-1"})
        r403 = client.get("/_test/ws-denied", headers={"X-Request-ID": "req-2"})
        r404 = client.get("/_test/concealed", headers={"X-Request-ID": "req-3"})
        r503 = client.get("/_test/auth-down", headers={"X-Request-ID": "req-4"})

    for resp in (r401, r403, r404, r503):
        assert resp.headers["cache-control"] == AUTH_NO_STORE_CACHE_CONTROL

    assert r401.status_code == 401
    assert r401.json()["error"]["code"] == "SESSION_TERMINATED"
    assert r401.json()["error"]["retryable"] is False
    assert "raw-secret-should-not-echo" not in r401.text

    assert r403.status_code == 403
    assert r403.json()["error"]["code"] == "WORKSPACE_SCOPE_VIOLATION"

    assert r404.status_code == 404
    assert r404.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    assert r503.status_code == 503
    assert r503.json()["error"]["code"] == "AUTH_VERIFICATION_UNAVAILABLE"
    assert r503.json()["error"]["retryable"] is True

    direct = build_auth_error_response(
        status_code=401,
        code="UNAUTHENTICATED",
        message="Authentication required.",
        retryable=False,
        request_id="req-99",
    )
    assert direct.headers["cache-control"] == AUTH_NO_STORE_CACHE_CONTROL


def test_openssl_signed_es256_and_rs256_interoperability(tmp_path: Any) -> None:
    """Cross-verify our pure-Python ES256 and RS256 verifiers against OpenSSL CLI."""
    import json as _json
    import subprocess as _subprocess

    now = _now()
    payload = _base_claims(now)
    payload_b64 = b64url_encode(
        _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )

    # --- OpenSSL ECDSA P-256 (ES256) ---
    ec_priv = tmp_path / "ec_priv.pem"
    ec_pub = tmp_path / "ec_pub.pem"
    _subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(ec_priv)],
        check=True,
        capture_output=True,
    )
    _subprocess.run(
        ["openssl", "ec", "-in", str(ec_priv), "-pubout", "-out", str(ec_pub)],
        check=True,
        capture_output=True,
    )
    ec_text = _subprocess.run(
        ["openssl", "ec", "-pubin", "-in", str(ec_pub), "-text", "-noout"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pub_hex = "".join(
        line.strip().replace(":", "")
        for line in ec_text.splitlines()
        if line.startswith("    ") and ":" in line
    )
    pub_bytes = bytes.fromhex(pub_hex)
    assert len(pub_bytes) == 65 and pub_bytes[0] == 0x04
    ec_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "use": "sig",
        "kid": "openssl-es256",
        "x": b64url_encode(pub_bytes[1:33]),
        "y": b64url_encode(pub_bytes[33:65]),
    }

    ec_hdr_b64 = b64url_encode(
        _json.dumps({"alg": "ES256", "typ": "JWT", "kid": "openssl-es256"}, separators=(",", ":")).encode("utf-8")
    )
    ec_signing_input = f"{ec_hdr_b64}.{payload_b64}".encode("ascii")
    ec_der_sig = _subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(ec_priv)],
        input=ec_signing_input,
        check=True,
        capture_output=True,
    ).stdout
    # Parse ASN.1 DER SEQUENCE { INTEGER r, INTEGER s } -> raw 64-byte JWS r||s
    assert ec_der_sig[0] == 0x30
    pos = 2 if ec_der_sig[1] < 0x80 else 2 + (ec_der_sig[1] & 0x7F)
    assert ec_der_sig[pos] == 0x02
    r_len = ec_der_sig[pos + 1]
    r_int = int.from_bytes(ec_der_sig[pos + 2 : pos + 2 + r_len], "big")
    pos = pos + 2 + r_len
    assert ec_der_sig[pos] == 0x02
    s_len = ec_der_sig[pos + 1]
    s_int = int.from_bytes(ec_der_sig[pos + 2 : pos + 2 + s_len], "big")
    ec_raw_sig = r_int.to_bytes(32, "big") + s_int.to_bytes(32, "big")
    ec_jwt = f"{ec_hdr_b64}.{payload_b64}.{b64url_encode(ec_raw_sig)}"

    # --- OpenSSL RSA-2048 (RS256) ---
    rsa_priv = tmp_path / "rsa_priv.pem"
    _subprocess.run(
        ["openssl", "genrsa", "-out", str(rsa_priv), "2048"],
        check=True,
        capture_output=True,
    )
    mod_hex = _subprocess.run(
        ["openssl", "rsa", "-in", str(rsa_priv), "-modulus", "-noout"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().split("=", 1)[1]
    n_bytes = bytes.fromhex(mod_hex)
    rsa_jwk = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": "openssl-rs256",
        "n": b64url_encode(n_bytes),
        "e": b64url_encode((65537).to_bytes(3, "big")),
    }
    rsa_hdr_b64 = b64url_encode(
        _json.dumps({"alg": "RS256", "typ": "JWT", "kid": "openssl-rs256"}, separators=(",", ":")).encode("utf-8")
    )
    rsa_signing_input = f"{rsa_hdr_b64}.{payload_b64}".encode("ascii")
    rsa_sig = _subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(rsa_priv)],
        input=rsa_signing_input,
        check=True,
        capture_output=True,
    ).stdout
    rsa_jwt = f"{rsa_hdr_b64}.{payload_b64}.{b64url_encode(rsa_sig)}"

    snapshot = JwksSnapshot(
        keys=(JwkKey.from_dict(ec_jwk), JwkKey.from_dict(rsa_jwk)),
        fetched_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(expected_issuer=_ISSUER, expected_audience=_AUDIENCE),
        jwks_provider=_StubJwksProvider([snapshot]),
        clock=lambda: now,
    )
    assert verifier.verify(ec_jwt).subject == "sub-user-001"
    assert verifier.verify(rsa_jwt).subject == "sub-user-001"


def test_require_workspace_roles_dependency_and_resource_ownership_helper() -> None:
    from career_memory_api.app import create_app
    from tests.synthetic_access import SyntheticWorkspaceAccessProvider

    alice = AuthenticatedUser(
        user_id="usr-alice",
        auth_subject=AuthSubject(issuer=_ISSUER, subject="sub-alice"),
    )
    provider = SyntheticWorkspaceAccessProvider(
        users_by_token={"tok-alice": alice},
        memberships_by_pair={
            ("usr-alice", "ws-owner"): WorkspaceMembership("ws-owner", "usr-alice", "OWNER"),
            ("usr-alice", "ws-viewer"): WorkspaceMembership("ws-viewer", "usr-alice", "VIEWER"),
        },
    )
    app = create_app(access_provider=provider)
    install_auth_error_handlers(app)

    @app.post("/_test/workspaces/{workspace_id}/connect")
    async def connect_probe(
        access: Annotated[
            WorkspaceAccessContext, Depends(require_workspace_roles("OWNER", "MEMBER"))
        ],
        request_id: Annotated[str | None, Depends(get_request_id)],
    ) -> dict[str, str | None]:
        return {"workspace_id": access.workspace_id, "role": access.role, "request_id": request_id}

    with TestClient(app) as client:
        ok = client.post(
            "/_test/workspaces/ws-owner/connect",
            headers={"Authorization": "Bearer tok-alice", "X-Request-ID": "req-ok"},
        )
        denied = client.post(
            "/_test/workspaces/ws-viewer/connect",
            headers={"Authorization": "Bearer tok-alice", "X-Request-ID": "req-no"},
        )

    assert ok.status_code == 200
    assert ok.json() == {"workspace_id": "ws-owner", "role": "OWNER", "request_id": "req-ok"}
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == AUTH_NO_STORE_CACHE_CONTROL

    ctx = WorkspaceAccessContext(
        user=alice,
        membership=WorkspaceMembership("ws-owner", "usr-alice", "OWNER"),
    )
    own_res = WorkspaceResource(resource_id="res-1", workspace_id="ws-owner", title="Own")
    other_res = WorkspaceResource(resource_id="res-1", workspace_id="ws-other", title="Other")
    assert ensure_workspace_resource_ownership(ctx, own_res, "res-1") == own_res
    with pytest.raises(WorkspaceResourceNotFoundError) as e_other:
        ensure_workspace_resource_ownership(ctx, other_res, "res-1")
    with pytest.raises(WorkspaceResourceNotFoundError) as e_none:
        ensure_workspace_resource_ownership(ctx, None, "res-1")
    assert str(e_other.value) == str(e_none.value)
