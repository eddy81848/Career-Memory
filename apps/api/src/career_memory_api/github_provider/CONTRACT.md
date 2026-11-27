# GitHub Read-Only Provider Adapter, Frozen Import Plan & Resolver Bridge Contract (`M2-T202`)

## 1. Frozen Initial Import Plan (`FrozenImportPlan` & `CompletionGateEvaluator`)
- **Window & Limits**:
  - `mode = SyncRunMode.INITIAL_BACKFILL`
  - Default window: `[cutoff_at - 365 days, cutoff_at]` (or explicit ISO `YYYY-MM-DD` / ISO-8601 `backfill_from <= cutoff_at`).
  - Hard caps: `limits.max_objects` defaults to `5000` and is strictly bounded to `1..5000`; `max_requests`, `max_total_bytes`, `max_item_bytes`, and `per_page` (`1..100`) are frozen into `plan_spec`.
- **Dynamic Default Branch & Snapshot Anchoring**:
  - `default_branch` is read directly from GitHub repository metadata (never hardcoded to `main`/`master`; supports `main`, `master`, `trunk`, `develop`, `release/*`, etc.).
  - `visibility` must be `PUBLIC` (`private == False`); `PRIVATE` and `INTERNAL` are rejected fail-closed.
  - `start_head_sha` and `started_observed_at` are frozen at plan creation.
- **Deterministic Completion Gate (`CompletionGateEvaluator.verify_completion_snapshot`)**:
  1. Repository ID mismatch or non-`PUBLIC` visibility flip -> `FAILED` (`REPOSITORY_IDENTITY_MISMATCH` / `REPOSITORY_VISIBILITY_CHANGED`), `import_ready=False`, `retry_allowed=False`.
  2. Limit exceeded -> `PARTIAL_WITH_LIMIT` (`IMPORT_LIMIT_EXCEEDED`), non-empty `remaining_range`, `import_ready=False`, `retry_allowed=False`.
  3. Default branch renamed, default branch HEAD SHA moved (`!= start_head_sha`), or 2nd-pass merged PR cue set `(pr_number, head_sha, merged_at, updated_at)` differs from 1st-pass -> `PARTIAL` + `PLAN_DRIFT`, `source_drift=True`, `retry_allowed=False`, `import_ready=False`.
  4. Missing required streams or unresolved transient errors -> `PARTIAL` + `TRANSIENT_PROVIDER_ERROR`, `retry_allowed=has_safe_checkpoint`, `source_drift=False`.
  5. Otherwise -> `SUCCESS`, `import_ready=True`, `source_drift=False`, `retry_allowed=False`.

## 2. Plan C Non-Raw Ingestion (`GitHubImportClientPort` & `HttpGitHubImportClient`)
- **Required Streams**:
  - `REPOSITORY_METADATA` (`fetch_repository_snapshot`)
  - `MERGED_PULL_REQUESTS` (`fetch_merged_pull_requests_page`, `enumerate_merged_pr_cues`, `fetch_pull_request_detail`)
  - `PULL_REQUEST_REVIEWS` (`fetch_pull_request_reviews_page`)
  - `PULL_REQUEST_COMMENTS` (`fetch_pull_request_review_comments_page`, `fetch_pull_request_issue_comments_page`)
  - `DEFAULT_BRANCH_COMMITS` (`fetch_default_branch_commits_page`, pinned to `sha=plan.start_head_sha`)
  - `COMMIT_PR_ASSOCIATIONS` (`fetch_commit_pull_request_associations`, `/commits/{sha}/pulls`)
- **Optional Context Stream**:
  - `REFERENCED_ISSUES` (`fetch_referenced_issue`)
- **Zero Raw Persistence Invariant**:
  - Raw source text bytes are extracted via `extract_canonical_source_bytes`, hashed in memory via `canonicalize_source_bytes` (`cm-canon-nfc-lf-v1`), and immediately discarded.
  - Every `ObservedGitHubRevision.normalized_metadata` passes `validate_normalized_metadata(..., strict_whitelist=True)` (`ALLOWED_NORMALIZED_METADATA_KEYS`).

## 3. Transport Safety & Error Classification (`SafeGitHubHttpTransport`)
| Condition | `GitHubErrorCode` | `ProviderFetchOutcomeKind` | `retryable` | Notes |
|---|---|---|---|---|
| Initial URL violates `EndpointSecurityPolicy` | `ENDPOINT_BLOCKED` | `POLICY_DENIED` | `False` | HTTPS + `api.github.com` only, no creds/IPs |
| 30x redirect violates policy or hop limit | `REDIRECT_BLOCKED` | `REDIRECT` (resolver validates) / `POLICY_DENIED` | `False` | Wire transport never auto-follows redirects |
| HTTP `401` | `AUTH_REVOKED` | `POLICY_DENIED` | `False` | Credential revoked/invalid |
| HTTP `403` (non-rate-limit) | `FORBIDDEN_POLICY` | `POLICY_DENIED` | `False` | Scope or repository access policy denial |
| HTTP `429` or `403` (`X-RateLimit-Remaining: 0` / `Retry-After`) | `RATE_LIMITED` | `RATE_LIMITED` | `True` | Parses `Retry-After` and `X-RateLimit-Reset` |
| HTTP `404` / `410` | `NOT_FOUND_OR_INACCESSIBLE` | `NOT_FOUND` | `False` | Ambiguous (missing vs private); never concluded as deletion |
| HTTP `206` or `Content-Length` mismatch | `PARTIAL_RESPONSE` | `PARTIAL_CONTENT` | `False` | Truncated payload rejected immediately |
| Body exceeds `max_bytes` | `BYTE_LIMIT_EXCEEDED` | `LIMIT_EXCEEDED` | `False` | Bounded chunk reader stops without buffering |
| HTTP `5xx` | `SERVER_ERROR` | `SERVER_ERROR` | `True` | Transient upstream error |
| Timeout / Network failure | `TIMEOUT` / `CONNECTION_ERROR` | `TIMEOUT` / `CONNECTION_ERROR` | `True` | Transient transport failure |

- Raw response bodies and `Authorization` headers are strictly redacted from `WireHttpRequest`, `WireHttpResponse`, `GitHubProviderError`, and `RedactingLogFilter`-guarded logs.

## 4. T-103B Resolver Bridge (`GitHubResolverContentFetcher`)
- Implements `career_memory_api.resolver.ports.ProviderContentFetcherPort`.
- Shares `extract_canonical_source_bytes` 1:1 with `HttpGitHubImportClient` so persisted revision hashes and live refetched hashes match deterministically across `PR`, `ISSUE`, `COMMIT`, `COMMENT`, `REVIEW`, and `is_diff=True` (`Accept: application/vnd.github.v3.diff`).
- Inspects `base.repo` / `repository` visibility metadata on returned JSON payloads to surface `provider_visibility` to `RefetchResolverService`.
