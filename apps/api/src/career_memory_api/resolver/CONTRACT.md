# Option C Raw-Non-Persistent Refetch Resolver Contract (`M1-T103B`)

## 1. Execution Order (`Option C §4`)
`RefetchResolverService.resolve_unit(session, request)` executes in strict order:
1. **Workspace & Provenance Ownership + Policy Fence Pre-Check**:
   - Looks up `content_unit -> source_object_revision -> source_object -> source_resource -> source_account` strictly within `access.workspace_id` (PostgreSQL FORCE RLS via `workspace_scoped_transaction`).
   - Nonexistent or cross-workspace `content_unit_id` raises identical `WorkspaceResourceNotFoundError` (`404 RESOURCE_NOT_FOUND`).
   - Before any remote fetch (`fetch_attempted = False`), rejects `DELETED`/`DELETION_PENDING`/deletion-fenced resources, `unit_policy_state != 'ACTIVE'`, `raw_storage_mode != 'NONE'`, `source_account.status == 'REVOKED'`, `POLICY_REVIEW_REQUIRED`, `visibility != 'PUBLIC'`, or tombstoned revisions with `ResolutionStatus.POLICY_BLOCKED`.
2. **Server-Owned Endpoint Construction & SSRF Guard (`REF-05`)**:
   - No caller/LLM URL input is accepted. Endpoint URLs are built exclusively from validated provider IDs (`build_provider_endpoint_url`).
   - `EndpointSecurityPolicy.validate_endpoint_url` enforces HTTPS-only, allowlisted hostnames, zero URL credentials (`user:pass@`), port 443 only, IP-literal rejection, private/loopback/link-local/CGNAT/metadata IP rejection (including IPv4-mapped IPv6 and injectable DNS rebinding checks), and hop-by-hop 30x redirect validation.
3. **Operation Dedupe & Budget Limits (`REF-08`)**:
   - Reuses an in-memory `EphemeralCanonicalDocument` from `OperationRefetchSession` when `(source_object_revision_id, expected_revision_hash)` was already verified in the same `operation_id` (`deduped = True`, zero additional remote requests).
   - Enforces `max_requests`, `max_total_bytes`, `max_unit_bytes`, `max_tokens`, `max_retries`, `max_concurrency`, and rejects HTTP `206` / truncated responses with `ResolutionStatus.PARTIAL_WITH_LIMIT` (zero silent truncation).
4. **Canonicalization & Revision Hash Verification (`REF-01`, `REF-02`)**:
   - Runs `career_memory_api.canonicalization.canonicalize_source_bytes` (`cm-canon-nfc-lf-v1`).
   - Any hash drift returns `ResolutionStatus.REVISION_MISMATCH` without ever mutating historical `source_object_revisions` (`DATA-01`).
5. **Exact Unit & Span Verification (`REF-01`, `REF-02`)**:
   - Reconstructs and verifies `SpanLocator` (`cm-locator-v1`) via `extract_verified_span` and compares `span_sha256` against `expected_unit_hash`.
   - Any locator corruption, offset mismatch, outdated/ineligible anchor, or sub-span violation returns `ResolutionStatus.INVALID_LOCATOR` without silent anchor repair.
6. **Transient / 404 Classification (`REF-03`)**:
   - `429`, `5xx`, timeouts, and connection errors retry up to `max_retries` and otherwise return `ResolutionStatus.UNAVAILABLE` (`retryable=True`).
   - Provider `404`/`410` returns `ResolutionStatus.UNAVAILABLE` (`retryable=False`, `reason_code="PROVIDER_OBJECT_NOT_FOUND"`), never misclassified as user deletion (`OD-003`) or falsity.

## 2. Internal `ResolutionStatus` ↔ HTTP / Gate Error Mapping (`Option C §7`)
| `ResolutionStatus` | HTTP Status | Error `code` | `retryable` | Notes |
|---|---:|---|---|---|
| `MATCHED` | `200` | — | `false` | Ephemeral text returned in memory / HTTP `200` with `Cache-Control: private, no-store` |
| `REVISION_MISMATCH` | `409` | `SOURCE_REVISION_MISMATCH` | `false` | Remote revision hash drifted; historical revision untouched |
| `INVALID_LOCATOR` | `409` | `SOURCE_LOCATOR_INVALID` | `false` | Same hash or version/anchor failure; no silent anchor fix |
| `POLICY_BLOCKED` | `403` | `SOURCE_POLICY_BLOCKED` | `false` | Visibility/consent/revocation/deletion fence/SSRF/redirect block |
| `UNAVAILABLE` | `503` | `SOURCE_UNAVAILABLE` | `metadata.retryable` | `true` for 429/5xx/timeout; `false` for provider 404/410 |
| `PARTIAL_WITH_LIMIT` | `422` | `SOURCE_FETCH_LIMIT` | `false` | HTTP 206, truncated stream, byte/token/request/concurrency cap |

Auth & concealment boundaries on the same endpoint (always `Cache-Control: private, no-store`):
- `400 INVALID_REQUEST` (forbidden user URL query param)
- `401 UNAUTHENTICATED` / `401 SESSION_TERMINATED`
- `403 WORKSPACE_SCOPE_VIOLATION`
- `404 RESOURCE_NOT_FOUND` (identical for nonexistent and cross-workspace `content_unit_id`)
- `503 AUTH_VERIFICATION_UNAVAILABLE`

## 3. HTTP Endpoint Schema (`GET /v1/content-units/{content_unit_id}/content`)
- **Headers**: `Authorization: Bearer <token>`, optional `X-Workspace-ID`, optional `X-Request-ID`
- **Query**: `workspace_id` (target workspace to verify via `WorkspaceAccessGuard`), `include_context` (`bool`, default `true`)
- **Response Header (ALL 2xx/4xx/5xx)**: `Cache-Control: private, no-store`
- **200 Body**:
  ```json
  {
    "content_unit_id": "uuid",
    "unit_type": "PR_BODY_SECTION",
    "content": "<ephemeral text>",
    "context": {
      "section": "string | null",
      "file_path": "string | null",
      "previous_file_path": "string | null",
      "hunk_header": "string | null",
      "preceding_text": "string | null",
      "following_text": "string | null"
    },
    "source_resolution": {
      "status": "MATCHED",
      "checked_at": "2026-11-27T14:00:00Z",
      "expected_revision_hash": "sha256:...",
      "observed_revision_hash": "sha256:...",
      "retryable": false,
      "reason_code": null
    }
  }
  ```
- **Error Body (403/409/422/503)**:
  ```json
  {
    "error": {
      "code": "SOURCE_REVISION_MISMATCH",
      "message": "...",
      "retryable": false,
      "request_id": "..."
    },
    "source_resolution": {
      "status": "REVISION_MISMATCH",
      "checked_at": "2026-11-27T14:00:00Z",
      "expected_revision_hash": "sha256:...",
      "observed_revision_hash": "sha256:...",
      "retryable": false,
      "reason_code": "REVISION_HASH_MISMATCH"
    }
  }
  ```

## 4. Downstream Port Signatures (`M2 T-202`, `M4 T-403/405/406/407`)
- **`ProviderContentFetcherPort`** (implemented by M2 `T-202` GitHub connector):
  - `fetch(request: ProviderFetchRequest) -> ProviderFetchResponse`
- **`ProvenanceMetadataStorePort`** (PostgreSQL implementation: `PostgresProvenanceMetadataStore`):
  - `get_unit_resolution_target(access: WorkspaceAccessContext, content_unit_id: str) -> PersistedUnitResolutionTarget | None`
  - `list_sibling_unit_anchors(access: WorkspaceAccessContext, source_object_revision_id: str) -> tuple[PersistedUnitResolutionTarget, ...]`
  - `check_policy_fence_current(access: WorkspaceAccessContext, content_unit_id: str) -> PersistedUnitResolutionTarget | None`
  - `record_resolution_metadata(access: WorkspaceAccessContext, metadata: ResolutionMetadata) -> None`
- **`RefetchResolverPort` / Gate Helpers** (called by M4 `T-403/405/406/407`):
  - `open_operation_session(access, *, operation_id, budget=None) -> OperationRefetchSession`
  - `resolve_unit(session, request: RefetchRequestInput) -> OperationMemoryResolution`
  - `verify_required_supports_for_gate(session, required_units, *, operation_kind, resolver=None, check_before_commit_guard=None, export_precondition=None) -> GateVerificationResult`
  - `project_derived_unit_state_after_import_ready(resolution: ResolutionMetadata, *, import_run_status="SUCCESS") -> DerivedUnitProjection`
  - `JobRefetchManifest` / `JobRefetchManifestItem` (non-raw job boundary handoff)
