## Summary

- Describe what changed and why.

## Mandatory Traceability Links (T-106A / ADR-020)

> Required whenever this PR changes OpenAPI specs, AI JSON Schemas, domain enums, state transitions, or SQL migrations (`db/migrations/*`, `contracts/*`, `apps/api/src/career_memory_api/*`).

- **Requirement**: <!-- Notion Requirement ID / URL -->
- **ADR**: <!-- ADR ID / URL (e.g., ADR-020, ADR-018) -->
- **UML**: <!-- Sequence / State / Domain UML diagram link -->
- **Work Item**: <!-- Notion Work Item ID / URL (e.g., T-106, T-106A) -->
- **Test Case**: <!-- Test case IDs & local pytest / conformance CI commands run -->

## Contract, Schema & Plan C Conformance Checklist

- [ ] `contracts/import-v1/openapi.yaml` (M0 approved 4-route snapshot) was **NOT** mutated (`git rev-parse HEAD:contracts/import-v1/openapi.yaml == 07594d75425744d6149250a9f7ff95ac24659f80`).
- [ ] Generated `contracts/openapi/career-memory-v1.openapi.json` and `contracts/ai-schemas/v1/*.schema.json` match code (`cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres`).
- [ ] **Plan C (방안 C / REF-04)**: Zero raw source text, quote, diff, or prompt body persisted in DB columns, JSONB metadata, AI schemas, or logs.
- [ ] **Read-Only Verification**: No writable `verification` or direct `SOURCE_VERIFIED` field exists on Claim write APIs, DB `claims` table, or Stage 2A/2B/3A/3B AI schemas.
- [ ] **OD-008 Search Scope**: Zero `project_id` parameters or schema fields exist; search is scoped strictly to authenticated `workspace_id` + optional `source_resource_id`.
- [ ] **OD-003 Source Deletion**: Deletion preview returns affected counts, non-recoverable scopes, and version-bound `preview_token`; confirm requires `Idempotency-Key` and matching `preview_token`.
- [ ] **Multi-Tenant Isolation**: Every tenant table enforces `ENABLE + FORCE ROW LEVEL SECURITY`, fail-closed `auth_ctx.current_workspace_id()`, and Composite FKs `(workspace_id, <fk>)`.
- [ ] **Scope Boundary**: No BYOK routes, no external LLM subscription OAuth callbacks (deferred until `T-408`), and no Post-MVP tables (`embeddings`, etc.) introduced.
