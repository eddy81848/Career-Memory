"""Synthetic RS256 JWT issuer and strict verifier for T-007D local verification.

IMPORTANT BOUNDARY:
- This module is a local synthetic test harness modeling Supabase-shaped JWT claims
  (`sub`, `iss`, `aud`, `exp`, `iat`, `jti`, `session_id`, `role`).
- It does NOT replace Supabase Auth or test hosted JWKS rotation.
- Optional synthetic revocation (`revoked_sessions` / `revoked_jtis`) models an
  explicit server-side revocation check ONLY; it does NOT claim that Supabase Auth
  immediately invalidates already-issued stateless JWTs upon logout.
- Login auth JWTs MUST NOT carry or authorize GitHub ingestion tokens or LLM plan
  entitlements (strict separation of concerns).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import secrets
from typing import Literal, Protocol
from uuid import UUID, uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


EXPECTED_ISSUER = "https://synthetic-supabase.local/auth/v1"
EXPECTED_AUDIENCE = "authenticated"
DISALLOWED_MIXED_CLAIMS = frozenset({"github_token", "github_installation_id", "llm_plan", "llm_quota"})


class TokenVerificationError(Exception):
    """Raised when JWT validation fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VerifiedPrincipal:
    subject_id: UUID
    session_id: str
    jti: str | None
    issuer: str
    audience: str


class JwtVerifierProtocol(Protocol):
    def verify_stateless(self, token: str) -> VerifiedPrincipal: ...

    def verify(self, token: str) -> VerifiedPrincipal: ...


class SessionCheckUnavailableError(Exception):
    """Raised when live auth session status cannot be confirmed (must fail closed)."""


class SessionTerminateError(Exception):
    """Raised when upstream session termination fails (distinct from clean logout)."""


@dataclass
class SyntheticSessionLifecycleStore:
    """In-memory synthetic auth session & refresh-token store for OD010-5 local verification.

    Models:
    - Active sessions (`session_id -> subject_id`)
    - Refresh token issuance & rotation (`refresh_token -> session_id`)
    - Session termination (`terminate_session`) which revokes refresh tokens
    - Simulated upstream logout failure (`simulate_terminate_failure`)
    - Simulated session-introspection outage (`simulate_status_unavailable`)
    """

    active_sessions: dict[str, UUID] = field(default_factory=dict)
    terminated_sessions: set[str] = field(default_factory=set)
    refresh_tokens: dict[str, str] = field(default_factory=dict)
    simulate_terminate_failure: bool = False
    simulate_status_unavailable: bool = False

    def register_session(self, *, session_id: str, subject_id: UUID) -> str:
        refresh_token = f"rt_synth_{secrets.token_urlsafe(24)}"
        self.active_sessions[session_id] = subject_id
        self.refresh_tokens[refresh_token] = session_id
        return refresh_token

    def terminate_session(self, session_id: str) -> None:
        if self.simulate_terminate_failure:
            raise SessionTerminateError("Synthetic auth session termination endpoint unavailable")
        self.active_sessions.pop(session_id, None)
        self.terminated_sessions.add(session_id)
        stale = [rt for rt, sid in self.refresh_tokens.items() if sid == session_id]
        for rt in stale:
            self.refresh_tokens.pop(rt, None)

    def refresh_access_token(
        self,
        *,
        refresh_token: str,
        authority: SyntheticJwtAuthority,
        expires_in_seconds: int = 300,
    ) -> tuple[str, str, str, str]:
        session_id = self.refresh_tokens.get(refresh_token)
        if not session_id or session_id not in self.active_sessions:
            raise TokenVerificationError("REFRESH_REVOKED", "Refresh token is invalid or session terminated")
        subject_id = self.active_sessions[session_id]
        # Rotate refresh token
        self.refresh_tokens.pop(refresh_token, None)
        new_refresh = f"rt_synth_{secrets.token_urlsafe(24)}"
        self.refresh_tokens[new_refresh] = session_id
        new_jwt, sid, jti = authority.issue_token(
            subject_id=subject_id,
            session_id=session_id,
            expires_in_seconds=expires_in_seconds,
        )
        return new_jwt, new_refresh, sid, jti

    def check_session_status(self, session_id: str) -> Literal["active", "terminated", "unknown"]:
        if self.simulate_status_unavailable:
            raise SessionCheckUnavailableError("Synthetic auth session check service unavailable")
        if session_id in self.terminated_sessions:
            return "terminated"
        if session_id in self.active_sessions:
            return "active"
        return "unknown"


@dataclass
class SyntheticRevocationStore:
    """In-memory synthetic revocation store (models server-side revocation only)."""

    revoked_sessions: set[str] = field(default_factory=set)
    revoked_jtis: set[str] = field(default_factory=set)

    def revoke_session(self, session_id: str) -> None:
        self.revoked_sessions.add(session_id)

    def revoke_jti(self, jti: str) -> None:
        self.revoked_jtis.add(jti)

    def is_revoked(self, *, session_id: str, jti: str) -> bool:
        return session_id in self.revoked_sessions or jti in self.revoked_jtis


class SyntheticJwtAuthority:
    """Ephemeral RS256 keypair used only inside local T-007D tests."""

    def __init__(
        self,
        *,
        issuer: str = EXPECTED_ISSUER,
        audience: str = EXPECTED_AUDIENCE,
        kid: str = "synthetic-t007d-rs256-k1",
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.kid = kid
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self.public_pem = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def issue_token(
        self,
        *,
        subject_id: UUID | str,
        session_id: str | None = None,
        jti: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        expires_in_seconds: int = 300,
        extra_claims: dict | None = None,
    ) -> tuple[str, str, str]:
        now = datetime.now(timezone.utc)
        sid = session_id or f"sess-{uuid4()}"
        token_jti = jti or str(uuid4())
        payload = {
            "sub": str(subject_id),
            "iss": issuer if issuer is not None else self.issuer,
            "aud": audience if audience is not None else self.audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
            "jti": token_jti,
            "session_id": sid,
            "role": "authenticated",
        }
        if extra_claims:
            payload.update(extra_claims)
        token = jwt.encode(
            payload,
            self.private_pem,
            algorithm="RS256",
            headers={"kid": self.kid},
        )
        return token, sid, token_jti


class StrictJwtVerifier:
    """Verifies RS256 signature, issuer, audience, expiry, UUID subject, and optional revocation."""

    def __init__(
        self,
        *,
        public_pem: bytes,
        expected_issuer: str = EXPECTED_ISSUER,
        expected_audience: str = EXPECTED_AUDIENCE,
        revocation_store: SyntheticRevocationStore | None = None,
        leeway_seconds: int = 0,
    ) -> None:
        self._public_pem = public_pem
        self._expected_issuer = expected_issuer
        self._expected_audience = expected_audience
        self._revocation_store = revocation_store
        self._leeway_seconds = leeway_seconds

    def verify_stateless(self, token: str) -> VerifiedPrincipal:
        """Verify cryptographic signature and standard claims WITHOUT checking session liveness."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenVerificationError("INVALID_TOKEN_HEADER", "Malformed JWT header") from exc

        if header.get("alg") != "RS256":
            raise TokenVerificationError("INVALID_ALGORITHM", "Only RS256 is permitted")

        try:
            claims = jwt.decode(
                token,
                self._public_pem,
                algorithms=["RS256"],
                audience=self._expected_audience,
                issuer=self._expected_issuer,
                leeway=self._leeway_seconds,
                options={
                    "require": ["sub", "iss", "aud", "exp", "iat", "jti", "session_id"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenVerificationError("TOKEN_EXPIRED", "JWT has expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenVerificationError("INVALID_ISSUER", "JWT issuer mismatch") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenVerificationError("INVALID_AUDIENCE", "JWT audience mismatch") from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenVerificationError("INVALID_SIGNATURE", "JWT signature verification failed") from exc
        except jwt.PyJWTError as exc:
            raise TokenVerificationError("INVALID_TOKEN", "JWT verification failed") from exc

        # Enforce separation of concerns: reject auth JWTs attempting to smuggle
        # GitHub ingestion tokens or LLM plan entitlements as login claims.
        mixed = DISALLOWED_MIXED_CLAIMS.intersection(claims.keys())
        if mixed:
            raise TokenVerificationError(
                "MIXED_CONCERN_CLAIM",
                f"Auth JWT must not carry non-auth claims: {sorted(mixed)}",
            )

        try:
            subject_uuid = UUID(str(claims["sub"]))
        except ValueError as exc:
            raise TokenVerificationError("INVALID_SUBJECT", "JWT sub must be a valid UUID") from exc

        return VerifiedPrincipal(
            subject_id=subject_uuid,
            session_id=str(claims["session_id"]),
            jti=str(claims["jti"]),
            issuer=str(claims["iss"]),
            audience=str(claims["aud"]),
        )

    def verify(self, token: str) -> VerifiedPrincipal:
        principal = self.verify_stateless(token)
        if self._revocation_store is not None and self._revocation_store.is_revoked(
            session_id=principal.session_id, jti=principal.jti or ""
        ):
            raise TokenVerificationError("SESSION_REVOKED", "Synthetic session/token has been revoked")
        return principal


class FixedConfigJwksJwtVerifier:
    """JWKS-backed JWT verifier using fixed configuration (local test harness candidate).

    Security invariants:
    - Allowed algorithms come strictly from fixed configuration (`allowed_algorithms`);
      they are NEVER derived from the untrusted JWT header `alg`.
    - JWKS endpoint comes strictly from fixed configuration (`jwks_url`); it is NEVER
      derived from untrusted JWT claims/headers (`iss`, `jku`, `x5u`).
    - Does NOT assume mock-only claims such as `jti` (real Supabase GoTrue user JWTs
      omit `jti` while supplying `sub`, `iss`, `aud`, `exp`, `iat`, `session_id`, `role`).
    - Local spike verification harness only; does NOT finalize the product's token policy.
    """

    def __init__(
        self,
        *,
        jwks_url: str,
        expected_issuer: str,
        expected_audience: str,
        allowed_algorithms: tuple[str, ...] = ("ES256",),
        required_claims: tuple[str, ...] = ("sub", "iss", "aud", "exp", "iat", "session_id", "role"),
        expected_role: str | None = "authenticated",
        leeway_seconds: int = 0,
        jwks_timeout_seconds: float = 5.0,
    ) -> None:
        normalized_algs = tuple(a.strip() for a in allowed_algorithms if a and a.strip())
        if not normalized_algs or any(a.lower() == "none" for a in normalized_algs):
            raise ValueError("allowed_algorithms must contain at least one explicit non-none algorithm")
        self._jwks_url = jwks_url
        self._expected_issuer = expected_issuer
        self._expected_audience = expected_audience
        self._allowed_algorithms = normalized_algs
        self._required_claims = tuple(required_claims)
        self._expected_role = expected_role
        self._leeway_seconds = leeway_seconds
        self._jwks_client = jwt.PyJWKClient(self._jwks_url, timeout=jwks_timeout_seconds)

    def verify_stateless(self, token: str) -> VerifiedPrincipal:
        """Verify JWT using fixed JWKS URL, fixed allowed algorithms, and fixed iss/aud."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenVerificationError("INVALID_TOKEN_HEADER", "Malformed JWT header") from exc

        token_alg = header.get("alg")
        if token_alg not in self._allowed_algorithms:
            raise TokenVerificationError(
                "INVALID_ALGORITHM",
                f"Token algorithm is not in fixed allowed_algorithms ({', '.join(self._allowed_algorithms)})",
            )

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        except (jwt.PyJWKClientError, jwt.PyJWTError) as exc:
            raise TokenVerificationError("INVALID_SIGNATURE", "Unable to resolve signing key from fixed JWKS") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._allowed_algorithms),
                audience=self._expected_audience,
                issuer=self._expected_issuer,
                leeway=self._leeway_seconds,
                options={
                    "require": list(self._required_claims),
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenVerificationError("TOKEN_EXPIRED", "JWT has expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenVerificationError("INVALID_ISSUER", "JWT issuer mismatch") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenVerificationError("INVALID_AUDIENCE", "JWT audience mismatch") from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenVerificationError("INVALID_SIGNATURE", "JWT signature verification failed") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise TokenVerificationError("MISSING_REQUIRED_CLAIM", "JWT missing required claim") from exc
        except jwt.PyJWTError as exc:
            raise TokenVerificationError("INVALID_TOKEN", "JWT verification failed") from exc

        mixed = DISALLOWED_MIXED_CLAIMS.intersection(claims.keys())
        if mixed:
            raise TokenVerificationError(
                "MIXED_CONCERN_CLAIM",
                f"Auth JWT must not carry non-auth claims: {sorted(mixed)}",
            )

        if self._expected_role is not None and claims.get("role") != self._expected_role:
            raise TokenVerificationError("INVALID_ROLE", "JWT role mismatch")

        try:
            subject_uuid = UUID(str(claims["sub"]))
        except ValueError as exc:
            raise TokenVerificationError("INVALID_SUBJECT", "JWT sub must be a valid UUID") from exc

        session_id = str(claims.get("session_id") or "").strip()
        if not session_id:
            raise TokenVerificationError("INVALID_SESSION_ID", "JWT session_id must be non-empty")

        raw_jti = claims.get("jti")
        aud_val = claims["aud"]
        aud_str = aud_val if isinstance(aud_val, str) else ",".join(str(x) for x in aud_val)

        return VerifiedPrincipal(
            subject_id=subject_uuid,
            session_id=session_id,
            jti=str(raw_jti) if raw_jti is not None else None,
            issuer=str(claims["iss"]),
            audience=aud_str,
        )

    def verify(self, token: str) -> VerifiedPrincipal:
        return self.verify_stateless(token)


class GoTrueLiveSessionVerifier:
    """Fail-closed live session liveness checker backed by GoTrue `GET /auth/v1/user`.

    MUST execute strictly BEFORE opening any PostgreSQL transaction so upstream HTTP
    latency never holds a DB transaction or pool connection open.
    Never logs or includes raw access tokens or API keys in error messages.
    """

    def __init__(
        self,
        *,
        api_url: str,
        anon_key: str,
        timeout_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        on_before_request: Callable[[], None] | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._anon_key = anon_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._on_before_request = on_before_request

    def check_live_session(
        self,
        *,
        access_token: str,
        principal: VerifiedPrincipal,
    ) -> Literal["active", "terminated", "unknown"]:
        if self._on_before_request is not None:
            self._on_before_request()

        try:
            with httpx.Client(
                base_url=self._api_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                resp = client.get(
                    "/auth/v1/user",
                    headers={
                        "apikey": self._anon_key,
                        "Authorization": f"Bearer {access_token}",
                    },
                )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise SessionCheckUnavailableError(
                f"GoTrue /auth/v1/user session check unavailable ({type(exc).__name__})"
            ) from exc

        if resp.status_code == 200:
            try:
                body = resp.json()
                live_subject = UUID(str(body.get("id")))
            except Exception as exc:
                raise SessionCheckUnavailableError("Malformed GoTrue /auth/v1/user response") from exc
            if live_subject != principal.subject_id:
                return "unknown"
            return "active"

        if resp.status_code in (401, 403):
            return "terminated"

        raise SessionCheckUnavailableError(
            f"GoTrue /auth/v1/user returned unexpected status {resp.status_code}"
        )
