# M5 `T-505` Encrypted PostgreSQL 18 Backup, Isolated Restore & Post-Restore Fence Drill Report (`INF-10`, `REL-01`)

- **Task / Issue**: `T-505` / Issue `#29`
- **Package**: `apps/api/src/career_memory_api/backup_restore/`
- **CLI Runner**: `scripts/run_t505_backup_restore_drill.py`
- **Redacted Evidence Contract**: `contracts/backup-restore/t505_restore_evidence.v1.json`
- **External Cloud Calls Executed**: `0` (`₩0` spend)
- **Hosted Public Pilot Gate Status**: **`HELD (NO-GO)` — `HELD_NO_GO_PENDING_OD013_OD015`**

---

## 1. Encryption & Zero-Plaintext Artifact Discipline (`INF-10`)

- **Cipher**: Authenticated **AES-256-GCM** (`format_version = cm-pg18-aes256gcm-v1`) with `scrypt` (`N=16384, r=8, p=1`) key derivation, random 16-byte salt, 12-byte nonce, and 16-byte GCM authentication tag.
- **AAD Binding**: Every envelope metadata field (`format_version`, `cipher`, `kdf`, `created_at`, `pg_version`, `plaintext_sha256`, `plaintext_size_bytes`, `schema_migrations`, `salt_b64`, `nonce_b64`) is bound as AES-256-GCM AAD.
- **Fail-Closed Tamper Tests Verified**:
  1. Single-bit ciphertext flip -> rejected with `BackupTamperDetectedError`.
  2. Header AAD metadata mutation (`pg_version`) -> rejected with `BackupTamperDetectedError`.
  3. Wrong passphrase -> rejected with `BackupTamperDetectedError`.
  4. Post-decrypt SHA-256 and byte-length digest check -> verified exact match.
- **Zero Persisted Dump/Secret Files**: `pg_dump -Fc` output and `pg_restore` input stream strictly in memory; zero `.dump`, `.enc`, or passphrase files are written to the repository.

---

## 2. Seeded Multi-Repository Pre-Backup Topology

Executed against an ephemeral local **PostgreSQL 18** cluster with migrations `0010..0080`, `cm_migrator` table ownership, and `NOBYPASSRLS` runtime roles (`cm_api_app`, `cm_worker_app`):

1. **Workspace A — Repo A (`acme/repo-alpha-active`, Active)**:
   - Verified `Identities` + `external_identity_mappings` (`GITHUB`, `OAUTH_ACCOUNT`, `VERIFIED`).
   - `CONFIRMED` `WorkEvent` with multi-version history (`work_event_versions` v1 `CANDIDATE` -> v2 `CONFIRMED`) and `review_actions` (`CONFIRM`).
   - `SUPPORTED` `Fact` (+ `fact_support`), `SOURCE_SUPPORTED` `Contribution`, `APPROVED` `Claim` (`SOURCE_VERIFIED` via `claim_atoms` + `claim_atom_support`), `READY` zero-quote `Export` (+ `export_claims`).
   - Populated FTS `search_entries` and `workspace_quota_ledgers` (`IMPORT_OBJECTS`, `PROVIDER_REQUESTS`).
2. **Workspace A — Repo B (`acme/repo-beta-fenced`, In-Flight `OD-003` Deletion)**:
   - Confirmed `OD-003` deletion (`preview_source_deletion` -> `confirm_source_deletion`) canceling queued `sync_runs`/`jobs` and marking affected `exports` as `DELETED` (`artifact_payload = NULL`).
   - Simulated crash during `FTS_INDEX` cleanup so `deletion_jobs.status = 'PARTIAL'` (`JOBS_AND_OUTBOX` and `EXPORTS` `SUCCEEDED`; `FTS_INDEX` `FAILED`; `EPHEMERAL_CACHE` and `AUTHORITATIVE_DOMAIN` `PENDING`) right before `pg_dump`.
3. **Workspace B (Cross-Tenant Control)**:
   - Separate tenant user/workspace proving `FORCE RLS` cross-workspace isolation survives restore.

---

## 3. Verified Post-Restore Invariants (`INF-10`, `REL-01`, `DELETE-07`, `REF-04`)

| Invariant | Verification Method on Restored DB (`cm_t505_restore_target_db`) | Result |
|---|---|---|
| **Repo A Exact Parity** | Row-level SHA-256 digest & count equality across `workspace_memberships`, `identities`, `external_identity_mappings`, `work_events`, `work_event_versions`, `review_actions`, `facts`, `claims`, `claim_atoms`, `exports`, `search_entries`, and `workspace_quota_ledgers` | **PASS** |
| **FORCE RLS & NOBYPASSRLS** | Catalog introspection across all 46 restored `public.*` tables (`rowsecurity=true`, `forcerowsecurity=true`), 0 catalog invariant violations, all 5 roles `NOBYPASSRLS`, and cross-workspace reads from Workspace B returning `0` rows / `404` | **PASS** |
| **Repo B Deletion Fence Survival** | `deletion_jobs.status = 'PARTIAL'` and `is_deletion_fence_active = true` persisted intact across `pg_dump`/`pg_restore` | **PASS** |
| **Zero-Window Post-Restore Blocking** | `GET /v1/content-units/{repo_b_unit_id}/content` -> `403 SOURCE_POLICY_BLOCKED` (`0` provider fetches); `GET /v1/search` -> `0` Repo B hits; Repo B export stays `DELETED` with `artifact_payload = NULL`; fenced worker replay & search re-index raise `DeletionFenceActiveError` | **PASS** |
| **Post-Restore Deletion Completion** | Re-running `execute_deletion_job` on restored DB converges all 5 components to `SUCCEEDED` with `0` resurrected Repo B rows while Repo A remains 100% intact | **PASS** |
| **Plan C Audit (46 Tables + Header)** | `0` non-null `text_content`/`content_ref`/`extracted_span`, `0` forbidden raw/secret keys across all JSONB columns and backup header, `0` canary or passphrase hits | **PASS** |

---

## 4. RTO / RPO Model & Honest Supabase Free (`₩0`) Constraints

- **RPO Model**: `LOGICAL_SNAPSHOT_ON_DEMAND_OR_SCHEDULED`
- **RTO Budget**: Verified well under `30,000 ms` on local PostgreSQL 18 (exact measured `backup_duration_ms`, `restore_duration_ms`, and `total_rto_ms` are recorded in `contracts/backup-restore/t505_restore_evidence.v1.json`).
- **Supabase Free (`₩0`) Hosting Constraint (`ADR-020`, `OD-013`, `OD-015`)**:
  - Paid Point-In-Time Recovery (PITR) is **NOT** assumed on Supabase Free (`paid_pitr_assumed: false`).
  - Recovery under the `₩0` baseline relies on scheduled or operator-triggered encrypted logical `pg_dump` snapshots verified against isolated PostgreSQL 18 restore targets via `scripts/run_t505_backup_restore_drill.py --verify`.
  - Live public internet pilot exposure remains **`HELD_NO_GO_PENDING_OD013_OD015`** until external hosting/billing/backup storage controls are verified by the operator.
