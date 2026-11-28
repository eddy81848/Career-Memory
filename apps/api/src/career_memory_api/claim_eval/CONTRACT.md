# M4 Wave 4 Frozen 0.4.0-Synthetic-Golden Claim & Refetch Evaluation Harness (`T-407A`)

Authoritative contract for `career_memory_api.claim_eval`, `scripts/run_t407a_claim_eval.py`, and `contracts/claim-eval/t407a_eval_bundle.v1.json`.

---

## 1. Governance & Non-Negotiable Invariants (`ADR-018`, `ADR-013`, `ADR-020`, `OD-002`)

1. **Single Frozen Entrypoint (`T-004B -> T-407A`)**:
   - Reads **ONLY** `evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json` (`0.4.0-synthetic-golden`: 16 scenarios `SCN-01..16`, 29 claims, 50 atoms, 34 fact candidates, 38 validations, 31 canonical facts, 35 fact supports, 27 refetch cases `refcase-01a..08d`, 17 families).
   - Rejects loading archived `0.3.0-synthetic-golden` (`APPROVAL_MANIFEST.json`) or unapproved `CANDIDATE_MANIFEST.json` directly (`GoldenDatasetMisloadError`).
   - Runs both `validate_dataset.py` and `validate_golden_approval.py` read-only (`python -B`) and verifies all 10 on-disk SHA-256 digests + `approved_candidate_bundle_sha256` (`sha256:27b6d1eeace3e4ac5771f1abb1555a2af3c6cd9446a9f7f10005404572d7b059`).
   - Verifies strict `SYNTHETIC_REGRESSION_DEV` vs `SYNTHETIC_REGRESSION_TEST` partition disjointness across families, source revisions, scenarios, claims, and refetch cases.
2. **100% Frozen Fixture Immutability**:
   - **0 bytes** modified under `evaluation/claim-draft/**`, `evaluation/event-draft/**`, or `contracts/import-v1/openapi.yaml` (`07594d75425744d6149250a9f7ff95ac24659f80`).
   - Zero oracle overwrite flags exist in `scripts/run_t407a_claim_eval.py`.
3. **Zero Live External LLM Calls (`OD-002` / `OD-014` OFF)**:
   - Live subscription LLM execution remains hard-OFF (`external_live_llm_calls == 0`, verified via `LlmPolicyGate(HostedLlmApprovalConfig())` -> `MANUAL_ONLY_BLOCKED`).
   - Strictly separates `SYNTHETIC_CONTRACT_AND_SAFETY_GATE = PASSED` from `LIVE_LLM_SEMANTIC_EVAL = NOT_RUN_BLOCKED_BY_OD002_OD014` (`live_pilot_gate_eligible = false`, zero population accuracy claims).
4. **Multi-Surface Plan C Zero Raw Persistence (`ADR-013` / `REF-04`)**:
   - Audits all PostgreSQL 18 tables & text/jsonb columns (`0010..0070`), `content_units.text_content IS NULL`, `content_units.content_ref IS NULL`, `fact_support.extracted_span IS NULL`, Python logs, resolver caches, HTTP metadata responses, rendered Markdown/JSON exports, and the redacted bundle itself (`0` canary hits, `0` raw source leaks, `0` forbidden raw keys), plus verifies the negative leak detector catches `negative_leaky_payload_fixture`.

---

## 2. Production Stack Exercised (Real PostgreSQL 18 + FastAPI)

- Ephemeral local PostgreSQL 18 cluster with migrations `0010..0070`, `NOBYPASSRLS` runtime roles (`cm_api_app`, `cm_worker_app`), and `ENABLE + FORCE ROW LEVEL SECURITY`.
- Real FastAPI application mounting `identities_router`, `work_events_router`, `facts_router`, `claims_router`, `exports_router`, `resolver_router`, and `contracts_router`.
- Real M4 production services:
  - `PostgresIdentityService` (`ID-04` attribution gate)
  - `PostgresFactService` (Stage 2A `FactCandidateExtractor`, Stage 2B `FactSupportValidator`, `CanonicalFactNormalizer`, `record_contribution`)
  - `PostgresClaimService` (Stage 3A `ClaimContextBuilder` + `ClaimGenerator`, Stage 3B `ClaimAtomizer`, Stage 3C `ClaimSupportValidator`, read-only `VerificationCalculator`, `ER-012` edit, `revalidate`, `CLAIM-09` race-fenced approval)
  - `PostgresExportService` (Plan C zero-quote `MARKDOWN` + `JSON` export creation/fetch/delete, `EXPORT-02` TOCTOU fence)
  - `RefetchResolverService` (`REF-01..08` exact span reproduction, hash/locator mismatch, 404/206/429/503/timeout, canary audit, 403/SSRF pre-fetch block, crash/replay, gate checks, dedupe/manifest/limits)
  - `execute_source_deletion_m4_cascade` (`OD-003` / `SCN-13` transactional cleanup)

---

## 3. CLI & Programmatic Entrypoints

```bash
# Write redacted zero-drift evidence bundle
cd apps/api && uv run python ../../scripts/run_t407a_claim_eval.py --write-bundle

# Verify committed bundle matches live PG18 run with 0 drift
cd apps/api && uv run python ../../scripts/run_t407a_claim_eval.py --verify-bundle
```
