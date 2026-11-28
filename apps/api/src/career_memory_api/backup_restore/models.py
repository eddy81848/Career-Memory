"""Typed models, crypto envelope schema, and T-505 restore evidence contracts (INF-10 / REL-01)."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BACKUP_ENVELOPE_FORMAT_VERSION = "cm-pg18-aes256gcm-v1"
T505_EVIDENCE_SCHEMA_VERSION = "t505-restore-evidence-v1"
HOSTED_PUBLIC_PILOT_GATE = "HELD_NO_GO_PENDING_OD013_OD015"
DEFAULT_RTO_BUDGET_MS = 30_000


class RpoModel(StrEnum):
    """Recovery Point Objective model under ₩0 / Supabase Free constraints."""

    LOGICAL_SNAPSHOT_ON_DEMAND_OR_SCHEDULED = "LOGICAL_SNAPSHOT_ON_DEMAND_OR_SCHEDULED"


class BackupCryptoError(RuntimeError):
    """Base error raised on backup encryption/decryption or envelope format violations."""


class BackupTamperDetectedError(BackupCryptoError):
    """Raised fail-closed when AES-256-GCM tag, AAD header, or plaintext SHA-256 fails verification."""


class BackupEnvelopeHeader(BaseModel):
    """Authenticated metadata header stored alongside AES-256-GCM backup ciphertext.

    Every metadata field (except ``tag_b64``) is bound as AES-256-GCM AAD so any tampering
    with ``pg_version``, ``plaintext_sha256``, ``schema_migrations``, or ``created_at`` fails
    decryption immediately. Strictly contains zero raw source text and zero secrets.
    """

    model_config = ConfigDict(extra="forbid")

    format_version: str = Field(default=BACKUP_ENVELOPE_FORMAT_VERSION, min_length=1)
    cipher: Literal["AES-256-GCM"] = "AES-256-GCM"
    kdf: Literal["SCRYPT-N16384-R8-P1", "PBKDF2-HMAC-SHA256-600000"] = "SCRYPT-N16384-R8-P1"
    created_at: str = Field(..., min_length=1)
    pg_version: str = Field(..., min_length=1)
    plaintext_sha256: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    plaintext_size_bytes: int = Field(..., ge=1)
    schema_migrations: list[str] = Field(..., min_length=1)
    salt_b64: str = Field(..., min_length=1)
    nonce_b64: str = Field(..., min_length=1)
    tag_b64: str = Field(..., min_length=1)

    def aad_payload(self) -> dict[str, Any]:
        """Return canonical metadata fields authenticated as AES-256-GCM AAD."""
        return {
            "cipher": self.cipher,
            "created_at": self.created_at,
            "format_version": self.format_version,
            "kdf": self.kdf,
            "nonce_b64": self.nonce_b64,
            "pg_version": self.pg_version,
            "plaintext_sha256": self.plaintext_sha256,
            "plaintext_size_bytes": self.plaintext_size_bytes,
            "salt_b64": self.salt_b64,
            "schema_migrations": list(self.schema_migrations),
        }


@dataclass(frozen=True, slots=True)
class EncryptedBackupEnvelope:
    """In-memory authenticated encrypted PostgreSQL 18 logical backup envelope."""

    header: BackupEnvelopeHeader
    ciphertext: bytes

    def serialize(self) -> bytes:
        """Serialize as ``CMBK1\\n<4-byte big-endian header len><header JSON><ciphertext>``."""
        import json

        header_bytes = json.dumps(
            self.header.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return b"CMBK1\n" + len(header_bytes).to_bytes(4, "big") + header_bytes + self.ciphertext

    @classmethod
    def deserialize(cls, raw: bytes) -> EncryptedBackupEnvelope:
        import json

        if not raw.startswith(b"CMBK1\n") or len(raw) < 10:
            raise BackupTamperDetectedError("Invalid encrypted backup envelope magic or framing.")
        header_len = int.from_bytes(raw[6:10], "big")
        if header_len <= 0 or 10 + header_len >= len(raw):
            raise BackupTamperDetectedError("Corrupted encrypted backup header length.")
        header_raw = raw[10 : 10 + header_len]
        ciphertext = raw[10 + header_len :]
        try:
            parsed = json.loads(header_raw.decode("utf-8"))
            header = BackupEnvelopeHeader.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001
            raise BackupTamperDetectedError(
                f"Malformed encrypted backup header: {type(exc).__name__}"
            ) from exc
        return cls(header=header, ciphertext=ciphertext)


@dataclass(frozen=True, slots=True)
class T505DrillReport:
    """Complete result of a real PostgreSQL 18 encrypted backup, isolated restore & fence drill."""

    pg_major_version: int
    pg_version_label: str
    schema_migrations: tuple[str, ...]
    backup_envelope_header: dict[str, Any]
    crypto_checks: dict[str, bool]
    repo_a_parity: dict[str, Any]
    rls_isolation: dict[str, Any]
    repo_b_deletion_fence: dict[str, Any]
    plan_c_audit: dict[str, Any]
    backup_duration_ms: int
    restore_duration_ms: int
    total_rto_ms: int
    rto_budget_ms: int
    rpo_model: RpoModel
    openapi_import_v1_blob_sha: str
    drill_timestamp_utc: str

    @property
    def all_invariants_passed(self) -> bool:
        return (
            all(self.crypto_checks.values())
            and bool(self.repo_a_parity.get("exact_parity"))
            and bool(self.rls_isolation.get("all_tables_force_rls"))
            and bool(self.rls_isolation.get("all_runtime_roles_nobypassrls"))
            and bool(self.rls_isolation.get("cross_workspace_reads_blocked"))
            and bool(self.rls_isolation.get("catalog_violations") == 0)
            and bool(self.repo_b_deletion_fence.get("fence_survived_restore"))
            and bool(self.repo_b_deletion_fence.get("refetch_blocked"))
            and bool(self.repo_b_deletion_fence.get("search_blocked"))
            and bool(self.repo_b_deletion_fence.get("export_blocked"))
            and bool(self.repo_b_deletion_fence.get("replay_commit_blocked"))
            and bool(self.repo_b_deletion_fence.get("resurrected_rows") == 0)
            and bool(self.repo_b_deletion_fence.get("post_restore_completion_succeeded"))
            and bool(self.repo_b_deletion_fence.get("repo_a_intact_after_repo_b_purge"))
            and bool(self.plan_c_audit.get("raw_text_violations") == 0)
            and bool(self.plan_c_audit.get("secret_key_violations") == 0)
            and bool(self.plan_c_audit.get("canary_string_hits") == 0)
            and bool(self.plan_c_audit.get("backup_metadata_clean"))
            and self.total_rto_ms <= self.rto_budget_ms
        )

    def to_redacted_evidence(self) -> dict[str, Any]:
        """Render redacted, schema-stable evidence bundle for ``contracts/backup-restore/t505_restore_evidence.v1.json``."""
        return {
            "schema_version": T505_EVIDENCE_SCHEMA_VERSION,
            "task_id": "T-505",
            "issue_number": 29,
            "gate_ids": ["INF-10", "REL-01", "DELETE-07", "REF-04"],
            "drill_timestamp_utc": self.drill_timestamp_utc,
            "external_cloud_calls": 0,
            "openapi_import_v1_blob_sha": self.openapi_import_v1_blob_sha,
            "postgres_major_version": self.pg_major_version,
            "schema_migrations": list(self.schema_migrations),
            "rpo_model": self.rpo_model.value,
            "supabase_free_hosting_constraints": {
                "paid_pitr_assumed": False,
                "zero_spend_backup_mechanism": "LOGICAL_PG_DUMP_AES256_GCM_ENCRYPTED_ENVELOPE",
                "isolated_restore_drill_verified": True,
                "hosted_public_pilot_gate": HOSTED_PUBLIC_PILOT_GATE,
                "notes": (
                    "Supabase Free does not include paid Point-In-Time Recovery (PITR) at ₩0; "
                    "recovery relies on on-demand/scheduled encrypted logical pg_dump snapshots "
                    "verified via isolated PostgreSQL 18 restore drills."
                ),
            },
            "encryption_envelope": {
                "format_version": BACKUP_ENVELOPE_FORMAT_VERSION,
                "cipher": self.backup_envelope_header["cipher"],
                "kdf": self.backup_envelope_header["kdf"],
                "aad_bound_fields": sorted(
                    k
                    for k in self.backup_envelope_header.keys()
                    if k != "tag_b64"
                ),
                "tamper_checks": dict(self.crypto_checks),
                "plaintext_files_committed": 0,
            },
            "invariants": {
                "all_passed": self.all_invariants_passed,
                "repo_a_parity": dict(self.repo_a_parity),
                "rls_isolation": dict(self.rls_isolation),
                "repo_b_deletion_fence": dict(self.repo_b_deletion_fence),
                "plan_c_audit": dict(self.plan_c_audit),
            },
            "measured_rto_metrics": {
                "backup_duration_ms": int(self.backup_duration_ms),
                "restore_duration_ms": int(self.restore_duration_ms),
                "total_rto_ms": int(self.total_rto_ms),
                "rto_budget_ms": int(self.rto_budget_ms),
                "within_rto_budget": self.total_rto_ms <= self.rto_budget_ms,
            },
        }


def normalize_evidence_for_drift_check(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of T-505 evidence with only volatile wall-clock durations normalized for `--verify`."""
    normalized = copy.deepcopy(evidence)
    metrics = normalized.get("measured_rto_metrics")
    if isinstance(metrics, dict):
        for field_name in ("backup_duration_ms", "restore_duration_ms", "total_rto_ms"):
            if field_name in metrics:
                metrics[field_name] = "<measured_ms>"
    return normalized


__all__ = [
    "BACKUP_ENVELOPE_FORMAT_VERSION",
    "BackupCryptoError",
    "BackupEnvelopeHeader",
    "BackupTamperDetectedError",
    "DEFAULT_RTO_BUDGET_MS",
    "EncryptedBackupEnvelope",
    "HOSTED_PUBLIC_PILOT_GATE",
    "RpoModel",
    "T505DrillReport",
    "T505_EVIDENCE_SCHEMA_VERSION",
    "normalize_evidence_for_drift_check",
]
