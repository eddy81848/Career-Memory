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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


EXPECTED_ISSUER = "https://synthetic-supabase.local/auth/v1"
EXPECTED_AUDIENCE = "authenticated"
DISALLOWED_MIXED_CLAIMS = frozenset({"github_token", "github_installation_id", "llm_plan", "llm_quota"})


class TokenVerificationError(Exception):
    """Raised when synthetic JWT validation fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VerifiedPrincipal:
    subject_id: UUID
    session_id: str
    jti: str
    issuer: str
    audience: str


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

    def verify(self, token: str) -> VerifiedPrincipal:
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

        session_id = str(claims["session_id"])
        jti = str(claims["jti"])
        if self._revocation_store is not None and self._revocation_store.is_revoked(
            session_id=session_id, jti=jti
        ):
            raise TokenVerificationError("SESSION_REVOKED", "Synthetic session/token has been revoked")

        return VerifiedPrincipal(
            subject_id=subject_uuid,
            session_id=session_id,
            jti=jti,
            issuer=str(claims["iss"]),
            audience=str(claims["aud"]),
        )
