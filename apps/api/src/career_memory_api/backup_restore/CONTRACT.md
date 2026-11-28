# `career_memory_api.backup_restore` Contract (`T-505` / `INF-10`, `REL-01`)

## Purpose
Implements authenticated AES-256-GCM logical backup encryption (`crypto_envelope.py`) and an automated, self-cleaning PostgreSQL 18 backup, isolated restore, RLS, Plan C, and active deletion-fence survival drill (`drill_runner.py`).

## Security & Durability Invariants
1. **Authenticated Encryption Envelope (`AES-256-GCM`)**:
   - Derives a 256-bit key via `scrypt` (`N=16384, r=8, p=1`) with per-backup 16-byte random salt and 12-byte GCM nonce.
   - Authenticates all envelope metadata (`format_version`, `cipher`, `kdf`, `created_at`, `pg_version`, `plaintext_sha256`, `plaintext_size_bytes`, `schema_migrations`, `salt_b64`, `nonce_b64`) as AES-256-GCM AAD.
   - Any flipped ciphertext byte, modified AAD header metadata, wrong passphrase, or SHA-256 mismatch fails closed with `BackupTamperDetectedError`.
   - Zero `.dump`, `.enc`, or passphrase material is ever committed to git or left on disk (`pg_dump -Fc` and `pg_restore` stream strictly in memory).
2. **Real PostgreSQL 18 Isolated Restore Parity (`INF-10`, `REL-01`)**:
   - Restores into a brand-new isolated PG18 database (`cm_t505_restore_target_db`) and proves exact parity of Repo A approval history (`work_event_versions`, `review_actions`, `facts`, `claims`, `claim_atoms`, `exports`), auth/workspace memberships, `external_identity_mappings`, `search_entries`, and `workspace_quota_ledgers`.
   - Proves `FORCE RLS` across all 46 restored tables, `NOBYPASSRLS` on all runtime roles, and 0 leaked rows on cross-workspace reads.
3. **Post-Restore Deletion Fence Survival & Zero Resurrection (`DELETE-07`, `INF-10`)**:
   - Seeds an in-flight `PARTIAL` deletion job for Repo B prior to `pg_dump`.
   - Verifies post-restore that the fence survives (`PARTIAL` / `fence_active=True`), `GET /v1/content-units/{id}/content`, `/v1/search`, `/v1/exports`, and worker replay/commit paths are 100% blocked (`0` resurrected rows), and running `execute_deletion_job` on the restored DB cleanly completes all 5 components to `SUCCEEDED` while keeping Repo A intact.
4. **Plan C Audit & Supabase Free Constraints**:
   - Audits all 46 restored tables and encrypted envelope metadata for zero raw source text, zero raw canaries, and zero secret keys.
   - Documents `RpoModel.LOGICAL_SNAPSHOT_ON_DEMAND_OR_SCHEDULED` without assuming paid Supabase PITR at `₩0`.
