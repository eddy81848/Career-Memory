"""Authenticated AES-256-GCM encryption envelope for PostgreSQL 18 logical backups (T-505 / INF-10).

Uses ``cryptography.hazmat.primitives.ciphers.aead.AESGCM`` when installed, and otherwise invokes
OpenSSL 3 ``libcrypto`` (``EVP_aes_256_gcm``) directly via ``ctypes`` with Python stdlib
``hashlib.scrypt`` (or ``PBKDF2-HMAC-SHA256``) key derivation. Every header metadata field
(``format_version``, ``created_at``, ``pg_version``, ``plaintext_sha256``, ``schema_migrations``,
``salt_b64``, ``nonce_b64``) is authenticated as AAD so tampering with ciphertext, tag, or metadata
fails closed with ``BackupTamperDetectedError``.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from career_memory_api.backup_restore.models import (
    BACKUP_ENVELOPE_FORMAT_VERSION,
    BackupCryptoError,
    BackupEnvelopeHeader,
    BackupTamperDetectedError,
    EncryptedBackupEnvelope,
)

_NONCE_LEN = 12
_TAG_LEN = 16
_SALT_LEN = 16
_KEY_LEN = 32

# OpenSSL EVP_CTRL constants for AES-GCM (stable across OpenSSL 1.1.1 & 3.x)
_EVP_CTRL_GCM_SET_IVLEN = 0x9
_EVP_CTRL_GCM_GET_TAG = 0x10
_EVP_CTRL_GCM_SET_TAG = 0x11

_CACHED_LIBCRYPTO: ctypes.CDLL | None = None


def _derive_key(
    passphrase: str,
    salt: bytes,
    *,
    kdf: Literal["SCRYPT-N16384-R8-P1", "PBKDF2-HMAC-SHA256-600000"] | None = None,
) -> tuple[bytes, Literal["SCRYPT-N16384-R8-P1", "PBKDF2-HMAC-SHA256-600000"]]:
    if not passphrase or len(passphrase) < 8:
        raise BackupCryptoError("Backup encryption passphrase must be at least 8 characters.")
    raw_pw = passphrase.encode("utf-8")
    if kdf == "PBKDF2-HMAC-SHA256-600000" or (kdf is None and not hasattr(hashlib, "scrypt")):
        key = hashlib.pbkdf2_hmac("sha256", raw_pw, salt, 600_000, dklen=_KEY_LEN)
        return key, "PBKDF2-HMAC-SHA256-600000"
    key = hashlib.scrypt(raw_pw, salt=salt, n=16384, r=8, p=1, dklen=_KEY_LEN)
    return key, "SCRYPT-N16384-R8-P1"


def _canonical_aad_bytes(header: BackupEnvelopeHeader) -> bytes:
    return json.dumps(
        header.aad_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _load_libcrypto() -> ctypes.CDLL:
    global _CACHED_LIBCRYPTO
    if _CACHED_LIBCRYPTO is not None:
        return _CACHED_LIBCRYPTO

    candidates: list[str] = [
        "/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
        "/usr/local/opt/openssl@3/lib/libcrypto.3.dylib",
        "libcrypto.so.3",
        "libcrypto.so.1.1",
    ]
    found = ctypes.util.find_library("crypto")
    if found:
        candidates.append(found)

    last_err: Exception | None = None
    for cand in candidates:
        if cand.startswith("/") and not Path(cand).is_file():
            continue
        try:
            lib = ctypes.CDLL(cand)
            if not hasattr(lib, "EVP_aes_256_gcm"):
                continue
            lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
            lib.EVP_CIPHER_CTX_new.argtypes = []
            lib.EVP_CIPHER_CTX_free.restype = None
            lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
            lib.EVP_aes_256_gcm.restype = ctypes.c_void_p
            lib.EVP_aes_256_gcm.argtypes = []
            lib.EVP_EncryptInit_ex.restype = ctypes.c_int
            lib.EVP_EncryptInit_ex.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
            ]
            lib.EVP_DecryptInit_ex.restype = ctypes.c_int
            lib.EVP_DecryptInit_ex.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
            ]
            lib.EVP_CIPHER_CTX_ctrl.restype = ctypes.c_int
            lib.EVP_CIPHER_CTX_ctrl.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
            ]
            lib.EVP_EncryptUpdate.restype = ctypes.c_int
            lib.EVP_EncryptUpdate.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_char_p,
                ctypes.c_int,
            ]
            lib.EVP_EncryptFinal_ex.restype = ctypes.c_int
            lib.EVP_EncryptFinal_ex.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            lib.EVP_DecryptUpdate.restype = ctypes.c_int
            lib.EVP_DecryptUpdate.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_char_p,
                ctypes.c_int,
            ]
            lib.EVP_DecryptFinal_ex.restype = ctypes.c_int
            lib.EVP_DecryptFinal_ex.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            _CACHED_LIBCRYPTO = lib
            return lib
        except OSError as exc:
            last_err = exc
            continue

    raise BackupCryptoError(
        f"Unable to locate OpenSSL libcrypto with EVP_aes_256_gcm: {last_err!r}"
    )


def _aes_256_gcm_encrypt(
    key: bytes, nonce: bytes, plaintext: bytes, aad: bytes
) -> tuple[bytes, bytes]:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]

        ct_and_tag = AESGCM(key).encrypt(nonce, plaintext, aad)
        return ct_and_tag[:-_TAG_LEN], ct_and_tag[-_TAG_LEN:]
    except ImportError:
        pass

    lib = _load_libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise BackupCryptoError("Failed to allocate OpenSSL EVP_CIPHER_CTX.")
    try:
        if lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_256_gcm(), None, None, None) != 1:
            raise BackupCryptoError("OpenSSL EVP_EncryptInit_ex cipher init failed.")
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_GCM_SET_IVLEN, len(nonce), None) != 1:
            raise BackupCryptoError("OpenSSL EVP_CTRL_GCM_SET_IVLEN failed.")
        if lib.EVP_EncryptInit_ex(ctx, None, None, key, nonce) != 1:
            raise BackupCryptoError("OpenSSL EVP_EncryptInit_ex key/nonce init failed.")

        out_len = ctypes.c_int(0)
        if aad:
            if lib.EVP_EncryptUpdate(ctx, None, ctypes.byref(out_len), aad, len(aad)) != 1:
                raise BackupCryptoError("OpenSSL EVP_EncryptUpdate AAD failed.")

        out_buf = ctypes.create_string_buffer(len(plaintext) + 32)
        if lib.EVP_EncryptUpdate(ctx, out_buf, ctypes.byref(out_len), plaintext, len(plaintext)) != 1:
            raise BackupCryptoError("OpenSSL EVP_EncryptUpdate plaintext failed.")
        total = out_len.value

        final_buf = ctypes.create_string_buffer(32)
        final_len = ctypes.c_int(0)
        if lib.EVP_EncryptFinal_ex(ctx, final_buf, ctypes.byref(final_len)) != 1:
            raise BackupCryptoError("OpenSSL EVP_EncryptFinal_ex failed.")

        tag_buf = ctypes.create_string_buffer(_TAG_LEN)
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_GCM_GET_TAG, _TAG_LEN, tag_buf) != 1:
            raise BackupCryptoError("OpenSSL EVP_CTRL_GCM_GET_TAG failed.")

        ciphertext = out_buf.raw[:total] + final_buf.raw[: final_len.value]
        return ciphertext, tag_buf.raw[:_TAG_LEN]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _aes_256_gcm_decrypt(
    key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes
) -> bytes:
    if len(nonce) != _NONCE_LEN or len(tag) != _TAG_LEN:
        raise BackupTamperDetectedError("Invalid AES-256-GCM nonce or authentication tag length.")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]

        try:
            return AESGCM(key).decrypt(nonce, ciphertext + tag, aad)
        except Exception as exc:  # noqa: BLE001
            raise BackupTamperDetectedError(
                "AES-256-GCM authentication failed (tampered ciphertext, header AAD, or wrong passphrase)."
            ) from exc
    except ImportError:
        pass

    lib = _load_libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise BackupCryptoError("Failed to allocate OpenSSL EVP_CIPHER_CTX.")
    try:
        if lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_256_gcm(), None, None, None) != 1:
            raise BackupCryptoError("OpenSSL EVP_DecryptInit_ex cipher init failed.")
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_GCM_SET_IVLEN, len(nonce), None) != 1:
            raise BackupCryptoError("OpenSSL EVP_CTRL_GCM_SET_IVLEN failed.")
        if lib.EVP_DecryptInit_ex(ctx, None, None, key, nonce) != 1:
            raise BackupCryptoError("OpenSSL EVP_DecryptInit_ex key/nonce init failed.")

        out_len = ctypes.c_int(0)
        if aad:
            if lib.EVP_DecryptUpdate(ctx, None, ctypes.byref(out_len), aad, len(aad)) != 1:
                raise BackupCryptoError("OpenSSL EVP_DecryptUpdate AAD failed.")

        out_buf = ctypes.create_string_buffer(len(ciphertext) + 32)
        if lib.EVP_DecryptUpdate(
            ctx, out_buf, ctypes.byref(out_len), ciphertext, len(ciphertext)
        ) != 1:
            raise BackupTamperDetectedError("OpenSSL EVP_DecryptUpdate failed.")
        total = out_len.value

        tag_buf = ctypes.create_string_buffer(tag, _TAG_LEN)
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_GCM_SET_TAG, _TAG_LEN, tag_buf) != 1:
            raise BackupTamperDetectedError("OpenSSL EVP_CTRL_GCM_SET_TAG failed.")

        final_buf = ctypes.create_string_buffer(32)
        final_len = ctypes.c_int(0)
        ret = lib.EVP_DecryptFinal_ex(ctx, final_buf, ctypes.byref(final_len))
        if ret <= 0:
            raise BackupTamperDetectedError(
                "AES-256-GCM authentication tag verification failed (tampered ciphertext, AAD, or wrong passphrase)."
            )
        return out_buf.raw[:total] + final_buf.raw[: final_len.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def encrypt_backup_bytes(
    plaintext: bytes,
    *,
    passphrase: str,
    pg_version: str,
    schema_migrations: Sequence[str],
    created_at: datetime | None = None,
) -> EncryptedBackupEnvelope:
    """Encrypt logical PostgreSQL dump bytes with AES-256-GCM and AAD-bound header metadata."""
    if not plaintext:
        raise BackupCryptoError("Cannot encrypt empty backup payload.")
    ts = created_at or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    created_iso = ts.astimezone(UTC).isoformat().replace("+00:00", "Z")

    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key, kdf_name = _derive_key(passphrase, salt)
    plaintext_sha256 = f"sha256:{hashlib.sha256(plaintext).hexdigest()}"

    # Construct header with placeholder tag to derive canonical AAD bytes (which exclude tag_b64)
    draft_header = BackupEnvelopeHeader(
        format_version=BACKUP_ENVELOPE_FORMAT_VERSION,
        cipher="AES-256-GCM",
        kdf=kdf_name,
        created_at=created_iso,
        pg_version=pg_version.strip(),
        plaintext_sha256=plaintext_sha256,
        plaintext_size_bytes=len(plaintext),
        schema_migrations=list(schema_migrations),
        salt_b64=base64.b64encode(salt).decode("ascii"),
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        tag_b64="placeholder",
    )
    aad = _canonical_aad_bytes(draft_header)
    ciphertext, tag = _aes_256_gcm_encrypt(key, nonce, plaintext, aad)
    final_header = draft_header.model_copy(
        update={"tag_b64": base64.b64encode(tag).decode("ascii")}
    )
    return EncryptedBackupEnvelope(header=final_header, ciphertext=ciphertext)


def decrypt_backup_bytes(
    envelope_or_bytes: EncryptedBackupEnvelope | bytes,
    *,
    passphrase: str,
) -> tuple[bytes, BackupEnvelopeHeader]:
    """Verify AES-256-GCM tag, AAD header, and plaintext SHA-256; fail closed on any tamper."""
    envelope = (
        envelope_or_bytes
        if isinstance(envelope_or_bytes, EncryptedBackupEnvelope)
        else EncryptedBackupEnvelope.deserialize(envelope_or_bytes)
    )
    header = envelope.header
    try:
        salt = base64.b64decode(header.salt_b64.encode("ascii"), validate=True)
        nonce = base64.b64decode(header.nonce_b64.encode("ascii"), validate=True)
        tag = base64.b64decode(header.tag_b64.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise BackupTamperDetectedError("Corrupted base64 cryptographic parameters.") from exc

    key, _ = _derive_key(passphrase, salt, kdf=header.kdf)
    aad = _canonical_aad_bytes(header)
    plaintext = _aes_256_gcm_decrypt(key, nonce, envelope.ciphertext, tag, aad)

    observed_sha = f"sha256:{hashlib.sha256(plaintext).hexdigest()}"
    if observed_sha != header.plaintext_sha256 or len(plaintext) != header.plaintext_size_bytes:
        raise BackupTamperDetectedError(
            "Decrypted backup payload digest or byte length failed integrity verification."
        )
    return plaintext, header


__all__ = [
    "decrypt_backup_bytes",
    "encrypt_backup_bytes",
]
