# M4 Wave 1A Schema & GitHub Identity Contract (`0070` · `T-401` · `T-401A` · `ID-01..05`)

Authoritative backend interface specification for **M4 Wave 2** (`T-402` Fact & Contribution extraction, `T-403` Claim draft & support validation) and **M4 Wave 3** (`T-405` Claim verification/approval & `T-407` Exports).

---

## 1. Schema Foundation (`db/migrations/0070_m4_fact_versions_and_exports.sql`)

### 1.1 `public.facts` Versioning (`MVP API §6` / `ER-012`)
- Columns added:
  - `version integer NOT NULL DEFAULT 1` (`ck_facts_version_positive CHECK (version >= 1)`)
  - `supersedes_fact_id uuid NULL` (`ck_facts_no_self_supersede CHECK (supersedes_fact_id IS NULL OR supersedes_fact_id <> id)`)
- Composite FK: `fk_facts_same_workspace_supersedes FOREIGN KEY (workspace_id, supersedes_fact_id) REFERENCES public.facts (workspace_id, id) ON DELETE SET NULL`.
- Partial Index: `idx_facts_workspace_supersedes ON public.facts (workspace_id, supersedes_fact_id) WHERE supersedes_fact_id IS NOT NULL`.
- Status CHECK constraint `ck_facts_status` updated idempotently to:
  `('CANDIDATE', 'SUPPORTED', 'USER_CONFIRMED', 'CONFLICTED', 'STALE', 'REJECTED', 'SUPERSEDED')`.

### 1.2 `public.exports` & `public.export_claims` (`MVP API §7`, `T-407`, `EXPORT-02`, `OD-003`)
- `public.exports`:
  - `format`: `MARKDOWN | JSON` (`ExportFormat`)
  - `export_policy`: `SOURCE_VERIFIED_ONLY | ALLOW_USER_CONFIRMED` (`ExportPolicy`)
  - `status`: `PENDING | READY | INVALIDATED | FAILED | DELETED` (`ExportStatus`)
  - Plan C & secret guards on `manifest` and `artifact_payload` (`ck_exports_manifest_no_raw`, `ck_exports_manifest_no_secret`, `ck_exports_artifact_payload_no_raw`, `ck_exports_artifact_payload_no_secret`).
  - Composite FK `fk_exports_same_workspace_resource (workspace_id, source_resource_id) -> public.source_resources (workspace_id, id) ON DELETE CASCADE`.
  - Unique idempotency constraint `uq_exports_workspace_idempotency (workspace_id, idempotency_key)`.
- `public.export_claims`:
  - `computed_verification_at_export`: `SOURCE_VERIFIED | USER_CONFIRMED | MIXED` (`ExportVerificationClass`)
  - Plan C guard on `support_manifest_snapshot` (`ck_export_claims_snapshot_no_raw`).
  - Composite FKs to `public.exports (workspace_id, id)` and `public.claims (workspace_id, id)` (`ON DELETE CASCADE`).
- Security: `ENABLE + FORCE ROW LEVEL SECURITY` on both tables; `cm_api_app` policy enforces `workspace_id = auth_ctx.current_workspace_id()` **and** active `workspace_memberships` (`revoked_at IS NULL`); `cm_worker_app` has `SELECT, INSERT, UPDATE` (zero `DELETE`).
- Zero LLM key/token persistence tables (`ADR-020` / `OD-014` live LLM OFF).

---

## 2. Pure Classification & Plan C Email Helpers (`career_memory_api.identities.models`)

| Function | Signature / Invariant | Consumer |
|---|---|---|
| `hash_email` | `(email: str) -> str` — trims + lowercases and returns `sha256:<64-hex>`. Raw email is NEVER persisted or returned. | `T-401`, `T-402` |
| `parse_github_noreply_email` | `(email: str \| None) -> GitHubNoreplyInfo \| None` — separates `<numeric_id>+<login>@users.noreply.github.com` (`has_numeric_id=True`), `<login>@users.noreply.github.com` (`has_numeric_id=False`), and `noreply@github.com` (`is_web_flow=True`). | `T-401`, `T-402` |
| `is_bot_actor` | `(*, username=None, display_name=None, provider_actor_type=None, email=None, actor_role=None) -> bool` — identifies `[bot]`, `provider_actor_type='Bot'`, GitHub Actions, Dependabot, Renovate, etc. | `T-401`, `T-402` |
| `parse_co_author_trailers` | `(commit_message: str) -> list[CoAuthorTrailer]` — parses `Co-authored-by:` trailers and returns `email_hash` + noreply hints without retaining raw email. | `T-402` |
| `classify_actor_contribution_eligibility` | `(*, actor_role: GitHubActorRole, identity_type: IdentityType) -> ActorContributionEligibility` — `PR_AUTHOR`/`COMMIT_AUTHOR` -> `IMPLEMENTED`; `REVIEWER` -> `REVIEWED`; `COMMITTER` (e.g. `web-flow`), `CO_AUTHOR`, and `BOT` are ineligible for automatic `VERIFIED` personal contribution attribution. | `T-402` |

---

## 3. Downstream Attribution Eligibility Helper (`ID-04`)

```python
evaluate_identity_attribution(
    conn_or_cur: psycopg.Connection | psycopg.Cursor,
    workspace_id: str,
    identity_id: str,
) -> IdentityAttributionEligibility
```

- `can_support_source_verified` is `True` **ONLY** when:
  1. `identities.status == 'ACTIVE'` and `identities.identity_type == 'PERSON'`,
  2. At least one `external_identity_mappings` row has `status == 'VERIFIED'`, AND
  3. Zero `external_identity_mappings` rows have `status == 'CONFLICTED'`.
- `can_support_user_confirmed` is `True` when `ACTIVE` `PERSON` has zero `CONFLICTED` mappings and at least one `VERIFIED` or `USER_CONFIRMED` mapping.
- `block_reason_code` (`IdentityAttributionBlockReason | None`):
  - `IDENTITY_IS_BOT`
  - `IDENTITY_REVOKED`
  - `IDENTITY_CONFLICTED`
  - `IDENTITY_AMBIGUOUS`
  - `IDENTITY_HEURISTIC_ONLY`
  - `IDENTITY_NOT_FOUND`

---

## 4. Transactional Service Contract (`career_memory_api.identities.PostgresIdentityService`)

```python
PostgresIdentityService(
    *,
    api_connection_factory: ConnectionFactory,
    before_commit_hook: BeforeCommitHook | None = None,
    now_fn: Callable[[], datetime] | None = None,
)
```

| Method | Signature | Transaction & Side-Effect Guarantees |
|---|---|---|
| `list_identities` | `(access) -> IdentityListResponse` | Runs under `cm_api_app` `FORCE RLS`; verifies live membership; returns redacted `email_hash` only + computed `attribution_eligibility`. |
| `resolve_or_upsert_github_identity` | `(access, *, observation: GitHubActorObservation) -> ResolveGitHubIdentityResult` | Stable `external_user_id` precedence over `username`/`email_hash`; updates username in-place on rename (`ID-02`) without creating duplicate `Identity` + writes `IDENTITY_USERNAME_RENAMED` audit event; marks collisions `CONFLICTED` + `AMBIGUOUS`. |
| `resolve_github_actor_in_tx` | `(cur, *, workspace_id, actor_user_id, observation, now=None) -> ResolveGitHubIdentityResult` | In-transaction variant for Wave 2 (`T-402`) ingestion pipelines. |
| `confirm_identity_mapping` | `(access, *, mapping_id, idempotency_key, request: ConfirmIdentityMappingRequest) -> ConfirmIdentityMappingResponse` | Locks mapping + parent identity + sibling mappings (`FOR UPDATE`); rejects `BOT` (`422 BOT_IDENTITY_CONFIRMATION_FORBIDDEN`), `CONFLICTED`/`AMBIGUOUS` (`409 MAPPING_CONFLICTED`), `REVOKED` (`409 MAPPING_REVOKED`), `VERIFIED` (`409 MAPPING_ALREADY_VERIFIED`); promotes `HEURISTIC` `PERSON` to `USER_CONFIRMED` (`verification_method='USER_CONFIRMATION'`, NEVER `VERIFIED`). |
| `revoke_identity_mapping` | `(access, *, mapping_id, idempotency_key, request: RevokeIdentityMappingRequest) -> RevokeIdentityMappingResponse` | Single `workspace_scoped_transaction`: locks rows `FOR UPDATE`, transitions mapping to `REVOKED`, cascades `STALE` to dependent `contributions` + `facts` + `claim_atoms` + `claims` (`APPROVED`/`SUPPORT_VALIDATED` -> `STALE`) and `INVALIDATED` (`invalidation_reason='IDENTITY_MAPPING_INVALIDATED'`) to `PENDING`/`READY` `exports`, writes `review_actions` (`REJECT`), `audit_events`, and `outbox_events` (`IDENTITY_ATTRIBUTION_INVALIDATED`), runs `before_commit_hook`, and re-verifies live membership + `deletion_jobs` fence before commit. |
| `mark_identity_mapping_conflicted` | `(access, *, mapping_id, idempotency_key, reason, confidence_reason='PROVIDER_IDENTITY_CONFLICT') -> RevokeIdentityMappingResponse` | Same atomic `FOR UPDATE` + cascade + audit/outbox + pre-commit fence contract; transitions mapping to `CONFLICTED` and parent identity to `AMBIGUOUS`. |

---

## 5. HTTP Wire Contract (`/v1/identities`)

- All endpoints return `Cache-Control: private, no-store` and `X-Request-ID`.
- Missing or cross-workspace `mapping_id` is strictly concealed as `404 RESOURCE_NOT_FOUND` (zero `details`).
- `project_id` / `project_ids` query or body keys are rejected with `422 PROHIBITED_PROJECT_ID_SCOPE` (`OD-008`).
- Plan C forbidden raw-text keys in request bodies are rejected with `422 PROHIBITED_RAW_TEXT_PERSISTENCE`.

| Method & Path | `operationId` | Idempotency-Key | Notes |
|---|---|---|---|
| `GET /v1/identities` | `listWorkspaceIdentities` | No | Lists identities, redacted mappings (`email_hash`), and `attribution_eligibility`. |
| `POST /v1/identities/mappings/{mapping_id}/actions/confirm` | `confirmIdentityMapping` | Required (UUID) | Promotes `HEURISTIC` `PERSON` mapping -> `USER_CONFIRMED` (`USER_CONFIRMATION`). |
| `POST /v1/identities/mappings/{mapping_id}/actions/revoke` | `revokeIdentityMapping` | Required (UUID) | Requires non-empty `reason`; returns updated mapping + `invalidated_counts`. |

### Stable Error Codes
- `401 UNAUTHENTICATED` / `SESSION_TERMINATED`
- `403 WORKSPACE_SCOPE_VIOLATION` (including mid-flight membership revocation before commit)
- `404 RESOURCE_NOT_FOUND`
- `409 MAPPING_ALREADY_REVOKED`, `MAPPING_ALREADY_CONFLICTED`, `MAPPING_ALREADY_VERIFIED`, `MAPPING_ALREADY_CONFIRMED`, `MAPPING_CONFLICTED`, `MAPPING_REVOKED`, `DELETION_PENDING`, `IDEMPOTENCY_CONFLICT`
- `422 INVALID_IDEMPOTENCY_KEY`, `INVALID_JSON_BODY`, `INVALID_CONFIRM_IDENTITY_REQUEST`, `INVALID_REVOKE_IDENTITY_REQUEST`, `BOT_IDENTITY_CONFIRMATION_FORBIDDEN`, `PROHIBITED_RAW_TEXT_PERSISTENCE`, `PROHIBITED_PROJECT_ID_SCOPE`
