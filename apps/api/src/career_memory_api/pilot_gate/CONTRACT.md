# Closed Pilot & 3-Tier Release Gate Contract (`T-506`)

## Scope
`career_memory_api.pilot_gate` programmatically loads, hashes, and verifies every committed M0–M5 evidence bundle (`B-004`, `T-306A`, `T-407A`, `T-408`/`INF-08`, `T-507`/`INF-07`, `T-505`/`INF-10`, `T-504A`), verifies the immutable M0 OpenAPI blob SHA (`07594d75425744d6149250a9f7ff95ac24659f80`), and emits the deterministic 3-tier release gate evaluation bundle (`contracts/pilot-gate/t506_release_gate.v1.json`).

## Strict 3-Tier Separation (`ADR-018`, `ADR-019`, `ADR-020`)
1. **`LOCAL_DETERMINISTIC_MVP_GATE`**: `GO` (`PASSED`) iff all 7 evidence bundles, all required invariant groups (`SEARCH-01..05`, `DELETE-01..07`, `SEC-01..02`, `REL-01..02`, `EXPORT-01..02`, `PERF-01`, `INF-01..10`, `OD-003`, `OD-008`, `ADR-013` Plan C `0` leaks, `M0-OPENAPI-PIN`), and live FE↔BE PG18 integration suites are verified at `₩0`.
2. **`PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE`**: Strictly **`NO-GO` (`HELD_PENDING_OD013_OD015`)** until external Cloud Run / Cloudflare / Supabase Free billing caps, platform origin mTLS/IAM locks, and backup storage controls are executed and approved by the human operator.
3. **`LIVE_SUBSCRIPTION_AI_PILOT_GATE`**: Strictly **`NO-GO` (`HELD_PENDING_OD002_OD014`)** — synthetic fixture passes (`T-407A`, `T-408`/`INF-08`) NEVER close the live model gate while external subscription LLM transmission remains `OFF`.
