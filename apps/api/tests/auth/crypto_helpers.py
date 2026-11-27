"""Test-only cryptographic key generation and JWS signing helpers.

Uses pure-Python P-256 (ES256) and RSA-2048 (RS256 via openssl keygen + stdlib math)
so tests run completely offline without adding runtime dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import subprocess
from dataclasses import dataclass
from typing import Any

# NIST P-256 (secp256r1) parameters
_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = _P256_P - 3
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _point_add(
    p1: tuple[int, int] | None, p2: tuple[int, int] | None
) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P256_P == 0:
            return None
        lam = ((3 * x1 * x1 + _P256_A) * pow(2 * y1, -1, _P256_P)) % _P256_P
    else:
        lam = ((y2 - y1) * pow(x2 - x1, -1, _P256_P)) % _P256_P
    x3 = (lam * lam - x1 - x2) % _P256_P
    y3 = (lam * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _scalar_mult(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    scalar = k % _P256_N
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


@dataclass(frozen=True)
class Es256TestKeyPair:
    kid: str
    d: int
    x: int
    y: int

    @classmethod
    def generate(cls, kid: str, *, seed: int | None = None) -> Es256TestKeyPair:
        if seed is None:
            d = secrets.randbelow(_P256_N - 1) + 1
        else:
            d = (seed % (_P256_N - 1)) + 1
        pub = _scalar_mult(d, (_P256_GX, _P256_GY))
        assert pub is not None
        return cls(kid=kid, d=d, x=pub[0], y=pub[1])

    def to_jwk(self) -> dict[str, Any]:
        return {
            "kty": "EC",
            "crv": "P-256",
            "alg": "ES256",
            "use": "sig",
            "kid": self.kid,
            "x": b64url_encode(self.x.to_bytes(32, "big")),
            "y": b64url_encode(self.y.to_bytes(32, "big")),
        }

    def sign_jwt(
        self,
        payload: dict[str, Any],
        *,
        header_overrides: dict[str, Any] | None = None,
    ) -> str:
        header: dict[str, Any] = {"alg": "ES256", "typ": "JWT", "kid": self.kid}
        if header_overrides:
            header.update(header_overrides)
        header_b64 = b64url_encode(
            json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        payload_b64 = b64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        z = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
        while True:
            k = secrets.randbelow(_P256_N - 1) + 1
            pt = _scalar_mult(k, (_P256_GX, _P256_GY))
            if pt is None:
                continue
            r = pt[0] % _P256_N
            if r == 0:
                continue
            s = (pow(k, -1, _P256_N) * (z + r * self.d)) % _P256_N
            if s != 0:
                break
        sig_bytes = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{header_b64}.{payload_b64}.{b64url_encode(sig_bytes)}"


def _parse_der_length(data: bytes, offset: int) -> tuple[int, int]:
    first = data[offset]
    if first < 0x80:
        return first, offset + 1
    num_bytes = first & 0x7F
    length = int.from_bytes(data[offset + 1 : offset + 1 + num_bytes], "big")
    return length, offset + 1 + num_bytes


def _parse_pkcs1_rsa_private_key(der: bytes) -> tuple[int, int, int]:
    # RSAPrivateKey ::= SEQUENCE { version, modulus(n), publicExponent(e), privateExponent(d), ... }
    assert der[0] == 0x30
    _, pos = _parse_der_length(der, 1)
    ints: list[int] = []
    for _ in range(4):
        assert der[pos] == 0x02
        length, pos = _parse_der_length(der, pos + 1)
        ints.append(int.from_bytes(der[pos : pos + length], "big"))
        pos += length
    return ints[1], ints[2], ints[3]


@dataclass(frozen=True)
class Rs256TestKeyPair:
    kid: str
    n: int
    e: int
    d: int

    @classmethod
    def generate(cls, kid: str) -> Rs256TestKeyPair:
        proc = subprocess.run(
            ["openssl", "genrsa", "2048"],
            check=True,
            capture_output=True,
            text=True,
        )
        pem = proc.stdout
        der_proc = subprocess.run(
            ["openssl", "rsa", "-traditional", "-outform", "DER"],
            input=pem.encode("ascii"),
            check=True,
            capture_output=True,
        )
        n, e, d = _parse_pkcs1_rsa_private_key(der_proc.stdout)
        return cls(kid=kid, n=n, e=e, d=d)

    def to_jwk(self) -> dict[str, Any]:
        n_bytes = self.n.to_bytes((self.n.bit_length() + 7) // 8, "big")
        e_bytes = self.e.to_bytes((self.e.bit_length() + 7) // 8, "big")
        return {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": self.kid,
            "n": b64url_encode(n_bytes),
            "e": b64url_encode(e_bytes),
        }

    def sign_jwt(
        self,
        payload: dict[str, Any],
        *,
        header_overrides: dict[str, Any] | None = None,
    ) -> str:
        header: dict[str, Any] = {"alg": "RS256", "typ": "JWT", "kid": self.kid}
        if header_overrides:
            header.update(header_overrides)
        header_b64 = b64url_encode(
            json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        payload_b64 = b64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        digest = hashlib.sha256(signing_input).digest()
        t = _SHA256_DIGEST_INFO_PREFIX + digest
        k = (self.n.bit_length() + 7) // 8
        ps_len = k - len(t) - 3
        em = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t
        sig_int = pow(int.from_bytes(em, "big"), self.d, self.n)
        sig_bytes = sig_int.to_bytes(k, "big")
        return f"{header_b64}.{payload_b64}.{b64url_encode(sig_bytes)}"


def sign_unsigned_none_jwt(payload: dict[str, Any], *, kid: str = "k-es256-1") -> str:
    header = {"alg": "none", "typ": "JWT", "kid": kid}
    h = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{h}.{p}."


def sign_hs256_jwt(
    payload: dict[str, Any], *, secret: bytes, kid: str = "k-es256-1"
) -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    h = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{b64url_encode(sig)}"
