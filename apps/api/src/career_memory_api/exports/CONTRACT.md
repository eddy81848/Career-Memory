# M4 Wave 3A Plan C Zero-Quote Exports Contract (`T-407`, `EXPORT-02`, `REF-07b/d/e/f/g`)

Authoritative backend interface specification for Markdown & JSON Claim exports, live `EXPORT_DOWNLOAD` Plan C refetch gates, and the `EXPORT-02` build-time invalidation race fence.

---

## 1. Strict Eligibility & Export Policy Rules

- **Default Policy (`ExportPolicy.SOURCE_VERIFIED_ONLY`)**: Permits ONLY `claims` with `status == 'APPROVED'` AND `computed_verification == 'SOURCE_VERIFIED'`.
- **Opt-In Policy (`ExportPolicy.ALLOW_USER_CONFIRMED`)**: Permits `claims` with `status == 'APPROVED'` AND `computed_verification` in `('SOURCE_VERIFIED', 'USER_CONFIRMED', 'MIXED')`, embedding explicit verification limitation notices per non-`SOURCE_VERIFIED` claim and per `USER_CONFIRMED` atom.
- **Unconditional Rejections (`0` leakage)**:
  - Any unapproved claim (`DRAFT`, `DRAFT_INVALID`, `SUPPORT_VALIDATED`, `REJECTED`, `STALE`, `SUPERSEDED`) -> `409 EXPORT_CLAIM_NOT_APPROVED` (`REF-07f`).
  - Version mismatch (`expected_claim_versions[claim_id] != claim.version`) -> `409 EXPORT_CLAIM_VERSION_MISMATCH` (`REF-07g`).
  - `computed_verification == 'UNVERIFIED'` or any `UNSUPPORTED`/`STALE` atom -> `409 SUPPORT_INCOMPLETE`.
  - `USER_CONFIRMED` / `MIXED` under `SOURCE_VERIFIED_ONLY` -> `409 EXPORT_POLICY_DISALLOWS_VERIFICATION_LEVEL`.
  - Active `deletion_jobs` fence -> `409 DELETION_PENDING`; policy-blocked source -> `403 SOURCE_POLICY_BLOCKED`.

---

## 2. Same-Operation Plan C Refetch & `EXPORT-02` Race Fence (`REF-07b/d/e`)

- Every `POST /v1/exports` AND every `GET /v1/exports/{export_id}` (for `READY` exports) executes live `verify_required_supports_for_gate(..., operation_kind=GateOperationKind.EXPORT_DOWNLOAD)` across all source-backed `ContentUnit`s. Historical `last_verified_at` alone is rejected (`409 REFETCH_REQUIRED_IN_CURRENT_OPERATION`, `REF-07d`).
- **3-Phase Publish Fence (`EXPORT-02` / `REF-07e`)**:
  1. Phase 1 inserts `public.exports` (`status = 'PENDING'`, `artifact_payload = NULL`) + `public.export_claims` (`support_manifest_snapshot` carrying `support_graph_epoch`).
  2. Phase 2 runs live `EXPORT_DOWNLOAD` refetch, renders the zero-quote artifact in memory, and invokes optional `before_publish_hook`.
  3. Phase 3 locks `exports`, `claims`, `claim_atoms`, `claim_atom_support`, `facts`, and `fact_support` `FOR UPDATE`, re-verifying `exports.status == 'PENDING'`, `claim.status == 'APPROVED'`, exact `claim_version`, matching `support_graph_epoch`, live policy fences, and zero deletion fences.
  4. Any concurrent invalidation during build transitions `exports` to `status = 'INVALIDATED'`, `invalidation_reason = 'SUPPORT_INVALIDATED_DURING_BUILD'`, `artifact_payload = NULL`, and fails closed with `409 EXPORT_INVALIDATED`.

---

## 3. Plan C Zero-Quote Citation Rendering (`renderer.py`)

- `manifest` records `export_id`, `format`, `export_policy`, `policy_version`, `generated_at`, `verification_calculated_at`, `claim_versions`, `source_revision_ids`, and `excludes_raw_source_quotes: true`.
- Per-atom citations contain **ONLY**: `canonical_url`, `source_object_revision_id`, `revision_content_hash`, `content_unit_id`, `unit_type`, `cm-locator-v1` (`char_start`, `char_end`, `line_start`, `line_end`, `span_sha256`, etc.), `observed_at`, and `checked_at` — **ZERO raw source quotes, excerpts, or snippets**.
- Idempotent by `(workspace_id, idempotency_key)` and deterministic `input_hash` over `(sorted claim_id:version pairs, format, export_policy, policy_version)`.

---

## 4. HTTP Wire Contract (`/v1/exports*`)

All responses enforce `Cache-Control: private, no-store`, `X-Request-ID`, `404 RESOURCE_NOT_FOUND` concealment, `422 PROHIBITED_PROJECT_ID_SCOPE`, `422 PROHIBITED_RAW_TEXT_PERSISTENCE`, and `422 PROHIBITED_READONLY_VERIFICATION_WRITE`.

| Method & Path | `operationId` | Idempotency-Key | Notes |
|---|---|---|---|
| `GET /v1/exports` | `listWorkspaceExports` | No | Filters: `source_resource_id`, `include_deleted=false`. |
| `POST /v1/exports` | `createWorkspaceExport` | Required (UUID) | Returns `201 ExportDetailResponse` after live refetch & `FOR UPDATE` publish fence. |
| `GET /v1/exports/{export_id}` | `getWorkspaceExport` | No | Re-verifies claims + live `EXPORT_DOWNLOAD` refetch; transitions to `INVALIDATED` (`409`/`403`/`503`) on failure. |
| `DELETE /v1/exports/{export_id}` | `deleteWorkspaceExport` | Required (UUID) | Marks `DELETED`, clears `artifact_payload = NULL`, returns `{export_id, status: "DELETED", deleted_at}`. |
