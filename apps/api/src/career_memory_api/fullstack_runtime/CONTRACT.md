# `career_memory_api.fullstack_runtime` Contract

## Purpose
Assembles the complete M1–M5 Career Memory FastAPI + PostgreSQL 18 (`0010..0080`) backend runtime and background pipeline worker for local development, end-to-end smoke checks, and `apps/web` live mode (`NEXT_PUBLIC_API_MODE=live`).

## Security & Fail-Closed Invariants
1. **`create_app()` & `main.py` untouched**: Default `create_app()` remains strictly fail-closed (`UnconfiguredWorkspaceAccessProvider`). No environment variable or request header enables synthetic or local-dev auth on `create_app()`.
2. **OpenAPI contract purity**: `GET /local-dev/bootstrap` is mounted ONLY inside `create_local_dev_fullstack_app()` (`include_in_schema=False`) and NEVER on `create_m1_contract_app()` or `contracts/openapi/career-memory-v1.openapi.json`.
3. **Real ES256 JWT + PG18 FORCE RLS**: Every `/v1/*` call validates a real P-256 (`ES256`) signed JWT through `JwtAccessTokenVerifier` and resolves workspace membership in PostgreSQL 18 under `cm_api_app` (`NOBYPASSRLS` + `FORCE RLS`).
4. **Plan C Non-Persistence**: Zero raw source text (`text_content`, `content_ref`, `extracted_span`, forbidden JSONB keys) is ever persisted to PostgreSQL 18. Ephemeral raw content viewing (`GET /v1/content-units/{id}/content`), `APPROVAL`, and `EXPORT_DOWNLOAD` gates live-refetch and SHA-256 verify on demand.

## Modes
- `local_sample` (default): Zero external network calls (`₩0`), deterministic `SamplePublicGitHubCatalog` (`seoyeon-dev/ledger-engine` + `seoyeon-dev/search-gateway`), pre-seeded workspace with live `WorkEvents`, `Facts`, `Claims`, and FTS search index while leaving `seoyeon-dev/search-gateway` ready for live `POST /v1/sync-runs`.
- `live_public_github` (opt-in via `CAREER_MEMORY_GITHUB_PROVIDER_MODE=live` or `--github-mode live`): Uses real `SafeGitHubHttpTransport` (`UrllibGitHubWireTransport`) + `EndpointSecurityPolicy` SSRF/allowlist against public GitHub repositories (`read:user` / read-only scope enforced).
