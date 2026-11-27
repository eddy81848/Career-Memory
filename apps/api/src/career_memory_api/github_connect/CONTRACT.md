# M2-T201 (`T-201` + `T-201A`) GitHub OAuth Connect & Policy Boundary Contract

## 1. Security & Domain Invariants (`OD-011`, `AUTH-02`, `POLICY-01`)

1. **Strict Separation of Service Login and Source Ingestion**:
   - Service login (Supabase Auth JWT / `WorkspaceAccessContext`) is never reused as a GitHub ingestion credential.
   - GitHub credentials are stored exclusively behind `SecretStorePort` (`secret://github-oauth-token/...`) and referenced in `public.source_accounts.token_secret_ref`.
   - `token_secret_ref`, raw `access_token`, `code`, `state`, `code_verifier`, and `client_secret` are never persisted in plaintext in PostgreSQL, HTTP responses, logs, or `audit_events.redacted_metadata`.
2. **True Minimum Scope (`read:user` Only)**:
   - `POST /v1/sources/github/oauth/start` always requests `scope=read:user` exclusively (zero `repo`, `public_repo`, `write:*`, `admin:*`, `delete_repo`, `gist`, `workflow` scopes).
   - `POST /v1/sources/github/oauth/callback` validates both the token exchange `scope` payload and `X-OAuth-Scopes` header via `validate_minimum_github_scopes(...)`. Any scope outside `{"read:user"}` immediately calls `GitHubCredentialProviderPort.revoke_grant(...)`, writes zero `source_accounts`/`source_resources` rows, records a redacted audit event, and rejects with `403 EXCESSIVE_OAUTH_SCOPE`.
3. **Explicit Public Repository Allowlist (`PUBLIC_CLOUD`, Plan C `raw_storage_mode='NONE'`)**:
   - `POST /v1/source-resources` accepts a single explicit `owner/repo` identifier (rejecting URLs, wildcards, or path traversal).
   - Only repositories verified with `private == false` and `visibility == "public"` are registered as `visibility='PUBLIC'`, `processing_mode='PUBLIC_CLOUD'`, `raw_storage_mode='NONE'`, `raw_retention_seconds=NULL`, `status='ACTIVE'`.
4. **Atomic Persistence & Zero Orphan Secrets (`AUTH-02`)**:
   - `oauth_connect_states` is consumed atomically once (`UPDATE ... WHERE consumed_at IS NULL AND expires_at > now`) before GitHub code exchange.
   - `source_accounts` upsert + optional `source_resources` upsert + `audit_events` insert execute in a single RLS-scoped PostgreSQL transaction with live active membership verification.
   - Any DB commit failure or mid-callback membership revocation rolls back the transaction, revokes the exchanged grant via `GitHubCredentialProviderPort.revoke_grant(...)`, deletes the secret from `SecretStorePort`, and records `public.orphan_secret_cleanups`.
5. **Visibility / Permission Transitions vs External Deletion (`T-201A`, `POLICY-01`)**:
   - Permission loss, token revocation, `Public -> Private` transitions, or ambiguous `404`/`403` provider responses **never** create tombstones (`source_object_revisions.is_tombstone = true`) and **never** mark resources or provenance objects `DELETED`.

## 2. HTTP Endpoints (`Cache-Control: private, no-store` on all responses)

| Method & Path | Status | Allowed Roles | Description |
| --- | --- | --- | --- |
| `POST /v1/sources/github/oauth/start` | `201` | `OWNER`, `MEMBER` | Issues one-time hashed `state` + PKCE S256 challenge and returns `read:user`-only GitHub authorize URL. |
| `POST /v1/sources/github/oauth/callback` | `200` | `OWNER`, `MEMBER` | Requires canonical UUID `Idempotency-Key` header + one-time `state`. Exchanges code, enforces `read:user` scope, persists `source_accounts` (+ optional single public `source_resources`) + `audit_events`. |
| `POST /v1/source-resources` | `201` | `OWNER`, `MEMBER` | Verifies repository is strictly public via GitHub metadata and registers it on the workspace allowlist (`PUBLIC_CLOUD`, `raw_storage_mode='NONE'`). |
| `GET /v1/source-accounts` | `200` | `OWNER`, `MEMBER`, `VIEWER` | Lists workspace-scoped connected accounts without exposing `token_secret_ref` or raw tokens. |
| `GET /v1/source-resources` | `200` | `OWNER`, `MEMBER`, `VIEWER` | Lists workspace-scoped non-deleted resources with computed `guidance_code`. |
| `POST /v1/source-resources/{id}/verify-policy` | `200` | `OWNER`, `MEMBER` | Re-verifies visibility & token health and applies `POLICY-01` transitions atomically. |
| `POST /v1/source-accounts/{id}/disconnect` | `200` | `OWNER`, `MEMBER` | Idempotently revokes GitHub grant, deletes secret, marks account `REVOKED`, locks resources in `POLICY_REVIEW_REQUIRED`, and cancels active jobs/runs. |

## 3. State Transition Matrix (`REAUTH_REQUIRED` vs `POLICY_REVIEW_REQUIRED` vs `VISIBILITY_OR_ACCESS_AMBIGUOUS`)

| Trigger / Provider Signal | `source_accounts` | `source_resources` | Active `jobs` & `sync_runs` | `source_objects` / `content_units` | Tombstones | Client `guidance_code` |
| --- | --- | --- | --- | --- | --- | --- |
| Confirmed `Public -> Private` / `Internal` (`200` with `private=true` or `visibility!=public`) | Unchanged (`ACTIVE`) | `visibility='PRIVATE'` (or `'INTERNAL'`), `status='POLICY_REVIEW_REQUIRED'` | Active jobs `CANCELED` (`POLICY_BLOCKED`); active runs `CANCELED`; `SUCCESS` runs `BLOCKED_POLICY` | `source_objects -> POLICY_REVIEW_REQUIRED`; `content_units -> BLOCKED` | **0** | `POLICY_REVIEW_REQUIRED` |
| `401 Bad credentials` or missing `read:user` scope or excessive scope drift | `status='REVOKED'`, `revoked_at=now`, secret deleted | All account resources `status='POLICY_REVIEW_REQUIRED'` | Active jobs `CANCELED` (`REAUTH_REQUIRED`); active runs `CANCELED`; `SUCCESS` runs `BLOCKED_SOURCE` | Preserved (not deleted) | **0** | `REAUTH_REQUIRED` |
| Ambiguous `404` or non-rate-limit `403` | Unchanged (`ACTIVE`) | `status='POLICY_REVIEW_REQUIRED'` (not `DELETED`) | Active jobs `CANCELED` (`VISIBILITY_OR_ACCESS_AMBIGUOUS`); active runs `CANCELED`; `SUCCESS` runs `BLOCKED_POLICY` | `source_objects -> POLICY_REVIEW_REQUIRED`; `content_units -> BLOCKED` | **0** | `VISIBILITY_OR_ACCESS_AMBIGUOUS` |
| `429` / `403` Rate Limit or `5xx` Provider Error | Unchanged | Unchanged | Unchanged | Unchanged | **0** | HTTP `429`/`503` (`retryable=true`) |
| `POST /v1/source-accounts/{id}/disconnect` | `status='REVOKED'`, `revoked_at=now`, GitHub grant revoked, secret deleted | Linked resources `status='POLICY_REVIEW_REQUIRED'` | Active jobs `CANCELED` (`ACCOUNT_DISCONNECTED`); active runs `CANCELED`; `SUCCESS` runs `BLOCKED_SOURCE` | Preserved (not deleted) | **0** | `REAUTH_REQUIRED` |

## 4. `GitHubCredentialProviderPort` Signature (`OD-011`)

```python
class GitHubCredentialProviderPort(Protocol):
    @property
    def credential_mode(self) -> GitHubCredentialMode: ...
    def build_authorization_url(
        self, *, state: str, redirect_uri: str, code_challenge: str, code_challenge_method: str = "S256"
    ) -> str: ...
    def exchange_authorization_code(
        self, *, code: str, pkce_code_verifier: str, redirect_uri: str
    ) -> GitHubExchangedCredential: ...
    def inspect_repository(
        self, *, access_token: str, owner: str, repo: str
    ) -> GitHubRepositoryInspection: ...
    def revoke_grant(
        self, *, access_token: str
    ) -> GitHubRevokeResult: ...
```

## 5. M2 Round 2 Orchestrator Integration Surface

- Router mount: `create_github_connect_router(service=..., resolve_access=...)` + `install_github_connect_error_handlers(app)`
- Policy enforcement before ingestion / refetch / AI / export:
  `service.assert_resource_processing_allowed(access, source_resource_id=..., operation=PolicyGuardedOperation.INGESTION)`
- Policy-cleared credential handoff to `github_provider` / `backfill_engine`:
  `service.resolve_active_credential_for_resource(access, source_resource_id=..., operation=PolicyGuardedOperation.INGESTION)`
