# M5 `T-504` / `T-504A` Cross-Subsystem Fault Injection & Race Matrix Report

- **Tasks / Issue**: `T-504`, `T-504A` / Issue `#33`
- **Package**: `apps/api/src/career_memory_api/fault_matrix/`
- **CLI Runner**: `scripts/run_t504a_fault_matrix.py` (`--verify` zero-drift mode)
- **Pytest Suite**: `apps/api/tests/fault_matrix/test_t504a_cross_subsystem_matrix_pg18.py`
- **Redacted Evidence Contract**: `contracts/fault-matrix/t504a_evidence_bundle.v1.json`
- **Database Engine**: Real ephemeral PostgreSQL 18 (`0010..0080`, 46 `public.*` tables, `FORCE RLS` + `NOBYPASSRLS` runtime roles)
- **Synchronization Method**: Deterministic `threading.Barrier` + named service hooks (`0` `time.sleep`-based race waits)
- **External Network Calls / Spend**: `0` calls / `₩0`
- **Immutable M0 OpenAPI Pin**: `contracts/import-v1/openapi.yaml` SHA `07594d75425744d6149250a9f7ff95ac24659f80`

---

## 1. Cross-Subsystem Scenario Matrix (`XFLT-01` .. `XFLT-12`)

| Scenario | Requirement IDs | Subsystems Covered | Barrier Sync | Assertions | Plan C (46 Tables) | Outcome |
|---|---|---|---|---|---|---|
| `XFLT-01` | `REL-02` | `jobs`, `outbox`, `sync_runs` | Yes (`Barrier(2)`) | 21/21 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-02` | `CLAIM-09`, `ID-05`, `SEARCH-01` | `claims`, `identities`, `resolver`, `search` | Yes (`Barrier(2)` x2) | 9/9 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-03` | `EXPORT-02`, `ER-012`, `REF-03`, `REF-07` | `exports`, `facts`, `resolver` | Yes (`Barrier(2)`) | 7/7 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-04` | `DELETE-07`, `EXPORT-02` | `initial_import`, `claims`, `search`, `exports`, `deletion` | Yes (`Barrier(5)`) | 7/7 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-05` | `DELETE-05`, `DELETE-06`, `OD-003` | `deletion`, `search`, `resolver`, `exports` | No (fault injector) | 25/25 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-06` | `SEARCH-01`, `SEARCH-03`, `SEARCH-04`, `SEARCH-05`, `OD-008` | `search`, `work_events`, `facts`, `claims` | No (lag toggle) | 11/11 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-07` | `SEC-01`, `SEC-02`, `REF-04`, `REF-05` | `canonicalization`, `claims`, `exports`, `resolver`, `search`, `edge_guard` | No | 14/14 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-08` | `INF-03`, `INF-06`, `PERF-01`, `OD-003` | `budgets`, `circuit_breaker`, `github_connect`, `deletion` | No | 25/25 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-09` | `INF-07`, `SEC-01`, `OD-003` | `edge_guard`, `budgets`, `deletion`, `search` | No | 18/18 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-10` | `REF-01..08` | `resolver`, `claims`, `exports` | No | 13/13 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-11` | `INF-10`, `REL-01`, `DELETE-07` | `backup_restore`, `deletion`, `search`, `jobs` | No | 9/9 | `PASS` (0 leaks) | **PASSED** |
| `XFLT-12` | `REL-01`, `REL-02`, `SEARCH-01`, `EXPORT-01` | Full pipeline (`jobs` -> `exports`) | No | 8/8 | `PASS` (0 leaks) | **PASSED** |

**Totals**: `12/12` scenarios passed, `167/167` assertions passed, `4` barrier-synchronized concurrency suites, `0` Plan C / secret / canary / XSS / log / export leaks across all 46 PostgreSQL 18 tables.

---

## 2. Production Bugs Discovered & Fixed During Wave 3 Integration

Running the integrated FastAPI stack with real default `WorkspaceAccessGuard` access resolvers (without test-only custom overrides) uncovered a genuine production bug across three routers:

- **Bug**: `apps/api/src/career_memory_api/deletion/router.py`, `apps/api/src/career_memory_api/budgets/router.py`, and `apps/api/src/career_memory_api/contracts_api/router.py` called `get_auth_credentials(authorization=..., x_session_id=...)` inside `_default_access_resolver`, whereas `dependencies.get_auth_credentials` accepts strictly `authorization` (raising `TypeError` at runtime when mounted with the default access resolver).
- **Fix**: Removed the invalid `x_session_id` keyword argument in all three routers so the default `WorkspaceAccessGuard` authentication boundary executes cleanly across `/v1/search`, `/v1/source-resources/{id}/deletion-preview`, `/v1/source-resources/{id}/deletion-confirm`, `/v1/deletion-jobs/{id}`, and `/v1/quota-status`.
- **Verification**: Exercised live in `XFLT-05`, `XFLT-07`, and `XFLT-09` as well as the full backend pytest suite.

---

## 3. Live FE↔BE PostgreSQL 18 M5 Integration Gate (`spikes/web-import`)

- **Backend Harness**: `spikes/web-import/tests/contract-http/run_live_memory_ops_backend.py`
  - Boots an ephemeral local PG18 cluster (`0010..0080`, `FORCE RLS` + `NOBYPASSRLS`) and real `uvicorn` FastAPI server mounting `PostgresSearchService`, `PostgresDeletionService`, `PostgresQuotaBudgetService`, `PostgresClaimService`, `PostgresExportService`, and `EdgeOriginAndCacheMiddleware`.
- **Frontend Integration Test**: `spikes/web-import/tests/contract-http/live-backend-memory-ops.test.tsx`
  - Verifies `EdgeOriginAndCacheMiddleware` rejects direct calls missing `X-CM-Origin-Auth` with `403 ORIGIN_AUTH_REQUIRED` and `Cache-Control: private, no-store`.
  - Verifies `MemoryOpsApp` + `FetchMemoryOpsHttpTransport` over real HTTP across:
    1. Live `/v1/quota-status` transitions (`ALLOWED` -> `WARNING_80_PERCENT` -> `CUTOFF_95_PERCENT`) disabling high-cost actions while preserving `OD-003` deletion.
    2. Live PG18 FTS `/v1/search` (`WORK_EVENT`, `FACT`, `CLAIM`), opaque cursor pagination (`limit=3` -> page 2), `OD-008` repository scoping, and zero canary leakage.
    3. Live `OD-003` `deletion-preview` (real non-zero DB impact counts) -> `deletion-confirm` (`202`, immediate zero-window search block) -> `GET /v1/deletion-jobs/{id}` (`PARTIAL`/`DELETION_PENDING` -> `SUCCEEDED`/`DELETED`), preserving the independent sibling repository (`acme/portfolio-telemetry`).
