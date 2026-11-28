# `career_memory_api.budgets` Contract (`T-503` / `PERF-01`, `INF-06`, `INF-03`, `ADR-019`, `ADR-020`)

## Purpose
Implements workspace-scoped quota ledgers, 80% warning / 95% hard-cutoff admission control, single-operation resource bounds, provider circuit breaker + ambiguous delivery no-auto-retry protection, and manual operator pause/resume on top of Migration `0080` (`public.workspace_quota_ledgers` and `public.runtime_pause_controls`, both `FORCE RLS` + `cm_migrator` owned).

## Wire Contract (`GET /v1/quota-status`, 1:1 with Wave 2B Frontend)
- **Route**: `GET /v1/quota-status` (`operation_id="getWorkspaceQuotaStatus"`, `tags=["quotas"]`, `Cache-Control: private, no-store`)
- **Response**: `QuotaStatusResponse`:
  - `workspace_id`, `period_key` (`YYYY-MM`)
  - `admission_state`: `ALLOWED | WARNING_80_PERCENT | CUTOFF_95_PERCENT | PAUSED`
  - `pause_state`: `ACTIVE | PAUSED_QUOTA | PAUSED_CIRCUIT_BREAKER | PAUSED_OPERATOR`
  - `pause_reason_code`: `string | null`
  - `circuit_breaker_state`: `CLOSED | OPEN | HALF_OPEN`
  - `high_cost_operations_allowed`: `boolean`
  - `safety_operations_allowed`: `true` (literal)
  - `paid_fallback_allowed`: `false` (literal)
  - `retry_after_seconds`: `integer | null`
  - `free_alternatives`: `string[]`
  - `meters`: `QuotaMeterItem[]` (`metric_kind`, `used_value`, `soft_warning_limit`, `hard_cutoff_limit`, `utilization_percent`, `status`)
  - `checked_at`: UTC `date-time`

## Key Invariants
1. **80% Soft Warning & 95% Hard Cutoff (`PERF-01`, `INF-06`, `OD-015`)**:
   - Fresh workspaces start at `used_value = 0` in `ALLOWED` state (`ESTIMATED_COST_KRW` defaults to `soft=16000`, `hard=19000` under the `<20,000 KRW` ceiling; `LLM_TOKENS` defaults to `0/0` while live LLM is `OFF`).
   - Reaching `soft_warning_limit` transitions `admission_state` to `WARNING_80_PERCENT` while still allowing bounded work.
   - Reaching or exceeding `hard_cutoff_limit` transitions `admission_state` to `CUTOFF_95_PERCENT`, blocks high-cost operations BEFORE execution (`429 BUDGET_CUTOFF_REACHED`, `Retry-After`, `paid_fallback_allowed: false`, `free_alternatives`), and transitions `runtime_pause_controls` to `PAUSED_QUOTA` when `auto_pause_on_cutoff=True`.
   - Unverifiable budget state fails closed (`503 BUDGET_VERIFICATION_UNAVAILABLE`).
2. **Safety Reservation Rule (`ADR-020`, `OD-015`, `INF-06`)**:
   - `SOURCE_DISCONNECT`, `DELETION_PREVIEW`, `DELETION_CONFIRM`, `DELETION_EXECUTE`, `AUTH_REVOKE`, `HEALTH_CHECK`, and `QUOTA_READ` are **ALWAYS ALLOWED** even under `CUTOFF_95_PERCENT`, `PAUSED_QUOTA`, `PAUSED_OPERATOR`, or `OPEN` circuit breaker.
3. **Circuit Breaker & Ambiguous Delivery Guard (`INF-03`)**:
   - `ProviderCircuitBreaker` transitions deterministically `CLOSED -> OPEN -> HALF_OPEN -> CLOSED` with an injectable monotonic clock.
   - `classify_external_call_outcome()` classifies ambiguous timeouts / unknown delivery outcomes on potentially billable or side-effecting calls as `AMBIGUOUS_DELIVERY_NO_AUTO_RETRY` (`auto_retry_allowed = False`) so uncertain calls are never auto-repeated.
4. **Verified Manual Resume (`ADR-020`, `OD-015`)**:
   - `resume_runtime(..., budget_verified=False)` ALWAYS fails with `409 MANUAL_RESUME_VERIFICATION_REQUIRED`.
   - `resume_runtime(..., budget_verified=True)` restores `ACTIVE` only when no meter remains at/above hard cutoff, and records an immutable `audit_events` entry.
