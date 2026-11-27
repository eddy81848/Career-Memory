"""Asymmetric JWT/JWKS verifier (ES256 & RS256) and live session verification port.

Security invariants:
- Strictly allows asymmetric algorithms (ES256, RS256); rejects ``none`` and
  symmetric ``HS*`` algorithm confusion at both configuration and token-parse time.
- Never derives JWKS endpoints from caller-controlled token headers/claims
  (rejects ``jku``, ``x5u``, ``jwk``, ``x5c``).
- Enforces fixed expected ``iss``, ``aud``, required ``sub``, required ``exp``,
  and ``nbf`` (when present) with bounded clock skew.
- Unknown ``kid`` triggers at most one bounded JWKS refresh; JWKS fetch outage
  fails closed with ``AccessVerificationUnavailableError`` (never falls back to
  an expired JWKS cache).
- Never leaks raw JWT bytes in error messages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from career_memory_api.auth.redaction import redact_mapping
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthSubject,
    SessionTerminatedError,
    UnauthenticatedError,
)

SUPPORTED_ASYMMETRIC_ALGORITHMS: frozenset[str] = frozenset({"ES256", "RS256"})
_FORBIDDEN_HEADER_PARAMS: frozenset[str] = frozenset({"jku", "x5u", "jwk", "x5c"})

# NIST P-256 (secp256r1) curve constants
_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = _P256_P - 3
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

# RFC 8017 SHA-256 DigestInfo DER prefix
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64url_decode(segment: str) -> bytes:
    if not isinstance(segment, str) or not segment:
        raise ValueError("Empty base64url segment.")
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode((segment + padding).encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid base64url encoding.") from exc


def _p256_is_on_curve(x: int, y: int) -> bool:
    if not (0 <= x < _P256_P and 0 <= y < _P256_P):
        return False
    return (y * y - (x * x * x + _P256_A * x + _P256_B)) % _P256_P == 0


def _p256_jacobian_double(
    p: tuple[int, int, int],
) -> tuple[int, int, int]:
    x1, y1, z1 = p
    if z1 == 0 or y1 == 0:
        return (0, 1, 0)
    y1_sq = (y1 * y1) % _P256_P
    s = (4 * x1 * y1_sq) % _P256_P
    # a = -3 mod p => 3*x1^2 + a*z1^4 = 3*(x1 - z1^2)*(x1 + z1^2)
    z1_sq = (z1 * z1) % _P256_P
    m = (3 * (x1 - z1_sq) * (x1 + z1_sq)) % _P256_P
    x3 = (m * m - 2 * s) % _P256_P
    y3 = (m * (s - x3) - 8 * y1_sq * y1_sq) % _P256_P
    z3 = (2 * y1 * z1) % _P256_P
    return (x3, y3, z3)


def _p256_jacobian_add(
    p1: tuple[int, int, int], p2: tuple[int, int, int]
) -> tuple[int, int, int]:
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    if z1 == 0:
        return p2
    if z2 == 0:
        return p1
    z1_sq = (z1 * z1) % _P256_P
    z2_sq = (z2 * z2) % _P256_P
    u1 = (x1 * z2_sq) % _P256_P
    u2 = (x2 * z1_sq) % _P256_P
    s1 = (y1 * z2 * z2_sq) % _P256_P
    s2 = (y2 * z1 * z1_sq) % _P256_P
    if u1 == u2:
        if s1 != s2:
            return (0, 1, 0)
        return _p256_jacobian_double(p1)
    h = (u2 - u1) % _P256_P
    r = (s2 - s1) % _P256_P
    h_sq = (h * h) % _P256_P
    h_cu = (h * h_sq) % _P256_P
    u1_h_sq = (u1 * h_sq) % _P256_P
    x3 = (r * r - h_cu - 2 * u1_h_sq) % _P256_P
    y3 = (r * (u1_h_sq - x3) - s1 * h_cu) % _P256_P
    z3 = (h * z1 * z2) % _P256_P
    return (x3, y3, z3)


def _p256_scalar_mult(k: int, affine: tuple[int, int]) -> tuple[int, int, int]:
    result = (0, 1, 0)
    addend = (affine[0], affine[1], 1)
    scalar = k % _P256_N
    while scalar > 0:
        if scalar & 1:
            result = _p256_jacobian_add(result, addend)
        addend = _p256_jacobian_double(addend)
        scalar >>= 1
    return result


def _p256_to_affine(p: tuple[int, int, int]) -> tuple[int, int] | None:
    x, y, z = p
    if z == 0:
        return None
    z_inv = pow(z, -1, _P256_P)
    z_inv_sq = (z_inv * z_inv) % _P256_P
    return ((x * z_inv_sq) % _P256_P, (y * z_inv_sq * z_inv) % _P256_P)


class JwksUnavailableError(Exception):
    """Raised by a JWKS provider when signing keys cannot be retrieved."""


class LiveSessionStatus(StrEnum):
    """Outcome of a live session check for sensitive operations."""

    ACTIVE = "ACTIVE"
    SESSION_TERMINATED = "SESSION_TERMINATED"


@dataclass(frozen=True, slots=True)
class JwtVerificationConfig:
    """Environment-pinned JWT verification parameters."""

    expected_issuer: str
    expected_audience: str
    allowed_algorithms: frozenset[str] = field(
        default_factory=lambda: SUPPORTED_ASYMMETRIC_ALGORITHMS
    )
    clock_skew_seconds: int = 30
    require_exp: bool = True
    require_kid: bool = True
    unknown_kid_refresh_cooldown_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.expected_issuer, str) or not self.expected_issuer.strip():
            raise ValueError("expected_issuer must be a non-empty string.")
        if (
            not isinstance(self.expected_audience, str)
            or not self.expected_audience.strip()
        ):
            raise ValueError("expected_audience must be a non-empty string.")
        if not self.allowed_algorithms:
            raise ValueError("allowed_algorithms must not be empty.")
        unsupported = set(self.allowed_algorithms) - SUPPORTED_ASYMMETRIC_ALGORITHMS
        if unsupported:
            raise ValueError(
                f"Unsupported or disallowed JWT algorithms: {sorted(unsupported)}. "
                f"Only {sorted(SUPPORTED_ASYMMETRIC_ALGORITHMS)} are permitted."
            )
        if self.clock_skew_seconds < 0:
            raise ValueError("clock_skew_seconds must be non-negative.")
        if self.unknown_kid_refresh_cooldown_seconds < 0:
            raise ValueError("unknown_kid_refresh_cooldown_seconds must be non-negative.")


@dataclass(frozen=True, slots=True)
class JwkKey:
    """Validated public JWK entry (EC P-256 or RSA >= 2048-bit)."""

    kid: str
    kty: str
    alg: str | None = None
    use: str | None = None
    crv: str | None = None
    x: int | None = None
    y: int | None = None
    n: int | None = None
    e: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JwkKey:
        kid = str(data.get("kid", "")).strip()
        kty = str(data.get("kty", "")).strip().upper()
        alg = data.get("alg")
        use = data.get("use")
        if not kid:
            raise ValueError("JWK must include a non-empty kid.")
        if use is not None and use != "sig":
            raise ValueError("Only signature JWKs (use='sig') are accepted.")

        if kty == "EC":
            crv = str(data.get("crv", "")).strip()
            if crv != "P-256":
                raise ValueError(f"Unsupported EC curve: {crv!r}.")
            x_bytes = _b64url_decode(str(data["x"]))
            y_bytes = _b64url_decode(str(data["y"]))
            if len(x_bytes) != 32 or len(y_bytes) != 32:
                raise ValueError("P-256 coordinates must be 32 bytes each.")
            x_val = int.from_bytes(x_bytes, "big")
            y_val = int.from_bytes(y_bytes, "big")
            if not _p256_is_on_curve(x_val, y_val):
                raise ValueError("EC public key point is not on NIST P-256.")
            return cls(
                kid=kid,
                kty="EC",
                alg=str(alg) if alg is not None else None,
                use=str(use) if use is not None else None,
                crv="P-256",
                x=x_val,
                y=y_val,
            )

        if kty == "RSA":
            n_bytes = _b64url_decode(str(data["n"]))
            e_bytes = _b64url_decode(str(data["e"]))
            n_val = int.from_bytes(n_bytes, "big")
            e_val = int.from_bytes(e_bytes, "big")
            if n_val.bit_length() < 2048:
                raise ValueError("RSA key modulus must be at least 2048 bits.")
            if e_val < 3 or (e_val % 2) == 0:
                raise ValueError("RSA public exponent must be an odd integer >= 3.")
            return cls(
                kid=kid,
                kty="RSA",
                alg=str(alg) if alg is not None else None,
                use=str(use) if use is not None else None,
                n=n_val,
                e=e_val,
            )

        raise ValueError(f"Unsupported JWK key type: {kty!r}.")


@dataclass(frozen=True, slots=True)
class JwksSnapshot:
    """Time-bounded snapshot of public signing keys."""

    keys: tuple[JwkKey, ...]
    fetched_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def find_by_kid(self, kid: str) -> JwkKey | None:
        for key in self.keys:
            if key.kid == kid:
                return key
        return None


class JwksProviderPort(Protocol):
    """Port for retrieving pinned-endpoint JWKS snapshots (no caller-controlled URLs)."""

    def get_jwks(self, *, force_refresh: bool = False) -> JwksSnapshot:
        """Return a JWKS snapshot or raise ``JwksUnavailableError``."""
        ...


@dataclass(frozen=True, slots=True)
class VerifiedJwtClaims:
    """Verified JWT identity claims after cryptographic and semantic validation."""

    auth_subject: AuthSubject
    issuer: str
    subject: str
    audience: tuple[str, ...]
    expires_at: datetime
    issued_at: datetime | None = None
    not_before: datetime | None = None
    jwt_id: str | None = None
    session_id: str | None = None
    email: str | None = None
    name: str | None = None
    raw_claims_redacted: Mapping[str, Any] = field(default_factory=dict)


class LiveSessionVerifierPort(Protocol):
    """Optional live-session checker for sensitive operations (e.g. OAuth connect/revoke)."""

    def verify_live_session(self, claims: VerifiedJwtClaims) -> LiveSessionStatus:
        """Confirm session is still active, return SESSION_TERMINATED, or raise on outage."""
        ...


def _verify_es256(key: JwkKey, signing_input: bytes, signature: bytes) -> bool:
    if key.kty != "EC" or key.crv != "P-256" or key.x is None or key.y is None:
        return False
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < _P256_N and 1 <= s < _P256_N):
        return False

    z = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
    w = pow(s, -1, _P256_N)
    u1 = (z * w) % _P256_N
    u2 = (r * w) % _P256_N
    p1 = _p256_scalar_mult(u1, (_P256_GX, _P256_GY))
    p2 = _p256_scalar_mult(u2, (key.x, key.y))
    affine = _p256_to_affine(_p256_jacobian_add(p1, p2))
    if affine is None:
        return False
    expected_r = (affine[0] % _P256_N).to_bytes(32, "big")
    return hmac.compare_digest(expected_r, r.to_bytes(32, "big"))


def _verify_rs256(key: JwkKey, signing_input: bytes, signature: bytes) -> bool:
    if key.kty != "RSA" or key.n is None or key.e is None:
        return False
    k = (key.n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if not (0 <= sig_int < key.n):
        return False
    m = pow(sig_int, key.e, key.n)
    em = m.to_bytes(k, "big")
    digest = hashlib.sha256(signing_input).digest()
    t = _SHA256_DIGEST_INFO_PREFIX + digest
    ps_len = k - len(t) - 3
    if ps_len < 8:
        return False
    expected_em = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t
    return hmac.compare_digest(em, expected_em)


def _parse_numeric_timestamp(value: Any, claim_name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnauthenticatedError(f"JWT claim '{claim_name}' must be numeric.")
    return datetime.fromtimestamp(float(value), tz=UTC)


class JwtAccessTokenVerifier:
    """Fail-closed asymmetric JWT verifier using pinned config and JWKS port."""

    def __init__(
        self,
        *,
        config: JwtVerificationConfig,
        jwks_provider: JwksProviderPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._jwks_provider = jwks_provider
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._cached_snapshot: JwksSnapshot | None = None
        self._last_forced_refresh_at: datetime | None = None

    @property
    def config(self) -> JwtVerificationConfig:
        return self._config

    def _load_fresh_snapshot(self, *, force_refresh: bool) -> JwksSnapshot:
        now = self._clock()
        if (
            not force_refresh
            and self._cached_snapshot is not None
            and not self._cached_snapshot.is_expired(now)
        ):
            return self._cached_snapshot

        try:
            snapshot = self._jwks_provider.get_jwks(force_refresh=force_refresh)
        except AccessVerificationUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AccessVerificationUnavailableError(
                "Signing keyset verification service is unavailable."
            ) from exc

        if snapshot.is_expired(now):
            raise AccessVerificationUnavailableError(
                "Signing keyset snapshot is expired and cannot be trusted."
            )
        self._cached_snapshot = snapshot
        if force_refresh:
            self._last_forced_refresh_at = now
        return snapshot

    def _resolve_signing_key(self, kid: str) -> JwkKey:
        snapshot = self._load_fresh_snapshot(force_refresh=False)
        key = snapshot.find_by_kid(kid)
        if key is not None:
            return key

        # Bounded single refresh on unknown kid (rate-limited by cooldown)
        now = self._clock()
        cooldown = self._config.unknown_kid_refresh_cooldown_seconds
        if (
            self._last_forced_refresh_at is not None
            and (now - self._last_forced_refresh_at).total_seconds() < cooldown
        ):
            raise UnauthenticatedError("Token signing key identifier is unknown.")

        refreshed = self._load_fresh_snapshot(force_refresh=True)
        key = refreshed.find_by_kid(kid)
        if key is None:
            raise UnauthenticatedError("Token signing key identifier is unknown.")
        return key

    def verify(
        self,
        raw_token: str | None,
        *,
        live_session_verifier: LiveSessionVerifierPort | None = None,
    ) -> VerifiedJwtClaims:
        """Verify JWS signature, fixed claims, and optional live session."""
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise UnauthenticatedError("Authentication token is required.")

        parts = raw_token.strip().split(".")
        if len(parts) != 3 or not all(parts):
            raise UnauthenticatedError("Malformed authentication token.")

        header_b64, payload_b64, signature_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
            payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
            signature = _b64url_decode(signature_b64)
        except Exception as exc:  # noqa: BLE001
            raise UnauthenticatedError("Malformed authentication token encoding.") from exc

        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise UnauthenticatedError("Authentication token segments must be JSON objects.")

        # Block header-directed key injection (jku, x5u, jwk, x5c)
        if any(param in header for param in _FORBIDDEN_HEADER_PARAMS):
            raise UnauthenticatedError("Authentication token header contains forbidden key parameters.")

        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._config.allowed_algorithms:
            raise UnauthenticatedError("Authentication token algorithm is not allowed.")

        kid = header.get("kid")
        if self._config.require_kid and (not isinstance(kid, str) or not kid.strip()):
            raise UnauthenticatedError("Authentication token must include a key identifier.")
        assert isinstance(kid, str)

        key = self._resolve_signing_key(kid.strip())
        if key.alg is not None and key.alg != alg:
            raise UnauthenticatedError("Token algorithm does not match signing key algorithm.")

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        if alg == "ES256":
            valid_sig = _verify_es256(key, signing_input, signature)
        elif alg == "RS256":
            valid_sig = _verify_rs256(key, signing_input, signature)
        else:  # pragma: no cover - guarded by allowed_algorithms
            valid_sig = False

        if not valid_sig:
            raise UnauthenticatedError("Authentication token signature is invalid.")

        # Validate fixed claims
        iss = payload.get("iss")
        if not isinstance(iss, str) or iss != self._config.expected_issuer:
            raise UnauthenticatedError("Authentication token issuer is invalid.")

        raw_aud = payload.get("aud")
        if isinstance(raw_aud, str):
            audiences = (raw_aud,)
        elif isinstance(raw_aud, list) and all(isinstance(a, str) for a in raw_aud):
            audiences = tuple(raw_aud)
        else:
            raise UnauthenticatedError("Authentication token audience is invalid.")

        if self._config.expected_audience not in audiences:
            raise UnauthenticatedError("Authentication token audience is not accepted.")

        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise UnauthenticatedError("Authentication token subject is missing.")

        now = self._clock()
        skew = float(self._config.clock_skew_seconds)

        if "exp" not in payload:
            if self._config.require_exp:
                raise UnauthenticatedError("Authentication token expiration is missing.")
            exp_dt = now
        else:
            exp_dt = _parse_numeric_timestamp(payload["exp"], "exp")
            if now.timestamp() - skew >= exp_dt.timestamp():
                raise UnauthenticatedError("Authentication token has expired.")

        nbf_dt: datetime | None = None
        if "nbf" in payload and payload["nbf"] is not None:
            nbf_dt = _parse_numeric_timestamp(payload["nbf"], "nbf")
            if now.timestamp() + skew < nbf_dt.timestamp():
                raise UnauthenticatedError("Authentication token is not yet valid.")

        iat_dt: datetime | None = None
        if "iat" in payload and payload["iat"] is not None:
            iat_dt = _parse_numeric_timestamp(payload["iat"], "iat")

        claims = VerifiedJwtClaims(
            auth_subject=AuthSubject(issuer=iss, subject=sub.strip()),
            issuer=iss,
            subject=sub.strip(),
            audience=audiences,
            expires_at=exp_dt,
            issued_at=iat_dt,
            not_before=nbf_dt,
            jwt_id=str(payload["jti"]) if isinstance(payload.get("jti"), str) else None,
            session_id=(
                str(payload["session_id"])
                if isinstance(payload.get("session_id"), str)
                else None
            ),
            email=str(payload["email"]) if isinstance(payload.get("email"), str) else None,
            name=str(payload["name"]) if isinstance(payload.get("name"), str) else None,
            raw_claims_redacted=redact_mapping(payload),
        )

        if live_session_verifier is not None:
            try:
                status = live_session_verifier.verify_live_session(claims)
            except (SessionTerminatedError, AccessVerificationUnavailableError):
                raise
            except Exception as exc:  # noqa: BLE001
                raise AccessVerificationUnavailableError(
                    "Live session verification service is unavailable."
                ) from exc

            if status is LiveSessionStatus.SESSION_TERMINATED:
                raise SessionTerminatedError("The session has ended.")
            if status is not LiveSessionStatus.ACTIVE:
                raise AccessVerificationUnavailableError(
                    "Live session verification returned an unknown state."
                )

        return claims
