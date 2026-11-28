"""Encrypted AES-256-GCM Backup Envelope & Real PG18 Isolated Restore/Fence Drill Tests (T-505 / INF-10 / REL-01)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_memory_api.auth.redaction import SENSITIVE_KEY_NAMES
from career_memory_api.backup_restore import (
    BACKUP_ENVELOPE_FORMAT_VERSION,
    HOSTED_PUBLIC_PILOT_GATE,
    BackupTamperDetectedError,
    EncryptedBackupEnvelope,
    RpoModel,
    decrypt_backup_bytes,
    encrypt_backup_bytes,
    normalize_evidence_for_drift_check,
    run_t505_backup_restore_drill,
)
from career_memory_api.conformance.ai_schemas import render_canonical_json
from career_memory_api.conformance.checks import M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1
from career_memory_api.conformance.db_catalog import FULL_M1_MIGRATIONS
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    assert_no_forbidden_secret_keys,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
EVIDENCE_PATH = REPO_ROOT / "contracts" / "backup-restore" / "t505_restore_evidence.v1.json"
CLI_SCRIPT = REPO_ROOT / "scripts" / "run_t505_backup_restore_drill.py"


def test_aes256_gcm_envelope_roundtrip_and_fail_closed_tamper_rejection() -> None:
    plaintext = b"PGDMP-synthetic-backup-payload-0123456789" * 64
    passphrase = "test-strong-backup-passphrase-2026!"

    envelope = encrypt_backup_bytes(
        plaintext,
        passphrase=passphrase,
        pg_version="18.3",
        schema_migrations=FULL_M1_MIGRATIONS,
        created_at=datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC),
    )
    assert envelope.header.format_version == BACKUP_ENVELOPE_FORMAT_VERSION
    assert envelope.header.cipher == "AES-256-GCM"
    assert envelope.ciphertext != plaintext
    assert passphrase.encode("utf-8") not in envelope.serialize()

    header_dict = envelope.header.model_dump(mode="json")
    assert_no_forbidden_raw_keys(header_dict, field_name="backup_header")
    assert_no_forbidden_secret_keys(header_dict, field_name="backup_header")

    raw_bytes = envelope.serialize()
    recovered, verified_header = decrypt_backup_bytes(raw_bytes, passphrase=passphrase)
    assert recovered == plaintext
    assert verified_header.plaintext_sha256 == envelope.header.plaintext_sha256

    # 1. Flip 1 bit in ciphertext -> MUST fail closed
    tampered_ct = bytearray(raw_bytes)
    tampered_ct[-1] ^= 0x80
    with pytest.raises(BackupTamperDetectedError):
        decrypt_backup_bytes(bytes(tampered_ct), passphrase=passphrase)

    # 2. Mutate AAD-authenticated header metadata -> MUST fail closed
    for field_name, bad_val in (
        ("pg_version", "17.9-tampered"),
        ("created_at", "2025-01-01T00:00:00Z"),
        ("schema_migrations", ["0010_identity_workspace_source_auth.sql"]),
    ):
        bad_env = EncryptedBackupEnvelope(
            header=envelope.header.model_copy(update={field_name: bad_val}),
            ciphertext=envelope.ciphertext,
        )
        with pytest.raises(BackupTamperDetectedError):
            decrypt_backup_bytes(bad_env, passphrase=passphrase)

    # 3. Wrong passphrase -> MUST fail closed
    with pytest.raises(BackupTamperDetectedError):
        decrypt_backup_bytes(raw_bytes, passphrase="wrong-backup-passphrase!")

    # 4. Corrupted framing magic -> MUST fail closed
    with pytest.raises(BackupTamperDetectedError):
        decrypt_backup_bytes(b"NOT_CMBK" + raw_bytes[8:], passphrase=passphrase)


@pytest.mark.postgres
def test_pg18_full_encrypted_backup_isolated_restore_fence_survival_and_zero_drift() -> None:
    report = run_t505_backup_restore_drill(repo_root=REPO_ROOT)

    assert report.pg_major_version >= 18
    assert report.all_invariants_passed is True
    assert report.openapi_import_v1_blob_sha == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1
    assert report.rpo_model is RpoModel.LOGICAL_SNAPSHOT_ON_DEMAND_OR_SCHEDULED

    # Crypto checks
    assert report.crypto_checks["tamper_ciphertext_rejected"] is True
    assert report.crypto_checks["tamper_aad_header_rejected"] is True
    assert report.crypto_checks["wrong_passphrase_rejected"] is True
    assert report.crypto_checks["sha256_integrity_verified"] is True

    # Repo A exact parity (approval history, auth mapping, facts, claims, exports, FTS, quotas)
    assert report.repo_a_parity["exact_parity"] is True
    counts = report.repo_a_parity["table_row_counts"]
    assert counts["workspace_memberships"] == 1
    assert counts["identities"] == 1
    assert counts["external_identity_mappings"] == 1
    assert counts["work_events"] == 1
    assert counts["work_event_versions"] == 2
    assert counts["review_actions"] == 1
    assert counts["facts"] == 1
    assert counts["claims"] == 1
    assert counts["claim_atoms"] == 1
    assert counts["exports"] == 1
    assert counts["search_entries"] >= 3
    assert counts["workspace_quota_ledgers"] == 7

    # FORCE RLS & NOBYPASSRLS across all 46 tables + cross-workspace isolation
    assert report.rls_isolation["restored_table_count"] == 46
    assert report.rls_isolation["all_tables_force_rls"] is True
    assert report.rls_isolation["all_runtime_roles_nobypassrls"] is True
    assert report.rls_isolation["cross_workspace_reads_blocked"] is True
    assert report.rls_isolation["catalog_violations"] == 0

    # Repo B deletion fence survival + 0 resurrection + clean post-restore completion
    fence = report.repo_b_deletion_fence
    assert fence["pre_restore_status"] == "PARTIAL"
    assert fence["post_restore_status_before_resume"] == "PARTIAL"
    assert fence["fence_survived_restore"] is True
    assert fence["refetch_blocked"] is True
    assert fence["search_blocked"] is True
    assert fence["export_blocked"] is True
    assert fence["replay_commit_blocked"] is True
    assert fence["resurrected_rows"] == 0
    assert fence["post_restore_completion_status"] == "SUCCEEDED"
    assert fence["post_restore_completion_succeeded"] is True
    assert fence["completed_components_count"] == 5
    assert fence["repo_a_intact_after_repo_b_purge"] is True

    # Plan C audit across all 46 restored tables + backup metadata
    plan_c = report.plan_c_audit
    assert plan_c["audited_table_count"] == 46
    assert plan_c["raw_text_violations"] == 0
    assert plan_c["secret_key_violations"] == 0
    assert plan_c["canary_string_hits"] == 0
    assert plan_c["backup_metadata_clean"] is True

    # Evidence contract redaction & zero drift against committed t505_restore_evidence.v1.json
    assert EVIDENCE_PATH.is_file(), f"Missing committed T-505 evidence file: {EVIDENCE_PATH}"
    committed = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    assert_no_forbidden_raw_keys(committed, field_name="t505_restore_evidence.v1.json")
    stack: list[object] = [committed]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                assert str(k).strip().lower() not in SENSITIVE_KEY_NAMES
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)

    assert committed["external_cloud_calls"] == 0
    assert committed["supabase_free_hosting_constraints"]["paid_pitr_assumed"] is False
    assert (
        committed["supabase_free_hosting_constraints"]["hosted_public_pilot_gate"]
        == HOSTED_PUBLIC_PILOT_GATE
    )

    live_evidence = report.to_redacted_evidence()
    assert render_canonical_json(normalize_evidence_for_drift_check(committed)) == (
        render_canonical_json(normalize_evidence_for_drift_check(live_evidence))
    )

    # Verify CLI --verify exits 0 with zero drift
    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--verify"],
        cwd=REPO_ROOT / "apps" / "api",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"CLI --verify failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
