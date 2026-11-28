# M4 Wave 2 Facts Pipeline & `/v1/facts*` API Contract (`T-402`, `T-403`, `T-403A`, `ER-012`, `MVP API §6`)

Authoritative backend interface specification for **M4 Wave 3** (`T-404` ClaimGenerator, `T-405` ClaimAtomizer & ClaimSupportValidator, `T-406` Claims API) and **M4 Wave 4** (`T-407` Exports & `T-407A` Golden Evaluation).

---

## 1. Strict Bounded Stage Separation (`Grounded Claim Pipeline §11`)

| Module | Pipeline Role | `generation_runs.run_type` | AI JSON Schema (`v1`) | Algorithm / Version |
|---|---|---|---|---|
| `career_memory_api.facts.extractor` | Stage 2A `FactCandidateExtractor` | `FACT_CANDIDATE_EXTRACTION` | `fact_candidate.v1.schema.json` | `fact-extractor-v1` |
| `career_memory_api.facts.validator` | Stage 2B `FactSupportValidator` | `FACT_SUPPORT_VALIDATION` | `fact_support_validation.v1.schema.json` | `fact-validator-v1` |
| `career_memory_api.facts.normalizer` | `CanonicalFactNormalizer` & `Contributions` | — (pure deterministic rules) | — | deterministic structured key |

### Core Plan C (`ADR-013` / `REF-04`) & Security (`SEC-02`) Invariants
- Ephemeral source text is obtained **exclusively** in-memory from a verified `OperationMemoryResolution` (`status == ResolutionStatus.MATCHED`) via `RefetchResolverService` — never read from `content_units.text_content` (which is `NULL`).
- `fact_support.extracted_span` is **ALWAYS `NULL`** in PostgreSQL and strictly omitted from HTTP API schemas.
- Zero raw source text, quotes, diffs, prompts, or operational secrets are persisted in `facts`, `fact_support`, `contributions`, `generation_runs`, `jobs`, `outbox_events`, `audit_events`, `review_actions`, logs, or HTTP responses.
- Both Stage 2A and Stage 2B execute strictly through `career_memory_api.llm_gate.BoundedStageRunner`, preserving the default hard-OFF live LLM gate (`MANUAL_ONLY_BLOCKED`, `0` external calls) while supporting `SYNTHETIC_FIXTURE_OFFLINE` and approved subscription runs.

---

## 2. Stage 2A — `FactCandidateExtractor` (`extractor.py`)

- Supports `0..N` candidates per `ContentUnit`:
  - `0` candidates (`[]`) is a valid `SUCCEEDED` extraction (`SCN-01`).
  - Any schema, Plan C, locator, or hostile-text violation fails the entire batch closed (`status = 'FAILED'`, `error_code = 'AI_SCHEMA_INVALID'`, **zero partial candidates persisted**).
- Every persisted candidate row in `public.facts` starts in `status = 'CANDIDATE'` and `version = 1`.
- Idempotent by `(workspace_id, event_id, content_unit_id, extractor_version, input_hash)`.
- Semantic attributes persisted in `normalized_value` & `fact_metadata`:
  - `completion_state`: `COMPLETED | PLANNED | NEGATED | TARGET_UNMET`
  - `scope`: `INDIVIDUAL | TEAM | UNSPECIFIED`
  - `environment_scope`: `PRODUCTION | STAGING_TEST | UNSPECIFIED`
  - `causal_basis`: `DIRECT_CAUSATION | OBSERVED_CORRELATION | UNSPECIFIED`
  - `uncertainty_notes`: `str | None`

---

## 3. Stage 2B — `FactSupportValidator` (`validator.py`)

- **Hard Separation Guard**: Strictly enforces `validator_run_id != extractor_run_id` (`422 STAGE_RUN_ID_COLLISION`).
- **Deterministic Prechecks (executed BEFORE semantic validation)**:
  1. Re-verified `OperationMemoryResolution` (`MATCHED`, exact revision & unit hashes, active policy/no tombstone).
  2. Exact `cm-locator-v1` bounds & `span_sha256` verification inside the `ContentUnit`'s canonical text.
  3. **Identity Gate (`ID-04`, `SCN-11`)**: `subject_identity_id`, `ROLE` facts, and `INDIVIDUAL` scope require `evaluate_identity_attribution(...).can_support_source_verified == True` (blocks `HEURISTIC`, `CONFLICTED`, `AMBIGUOUS`, `REVOKED`, `BOT`).
  4. **6-Element Metric Gate (`CLAIM-06`, `SCN-09`, `SCN-16`)**: `METRIC` facts require non-empty `value`, `unit`, `period`, `source_basis`, `environment_scope`, `measurement_scope`.
- **Adversarial Post-Validator Guards (`FACT-04..08`, `T-403A`)**:
  - `CONTEXT`-only / `ISSUE_SECTION` background NEVER promotes to `SUPPORTED` (`SCN-04`).
  - Future plan/roadmap/TODO/target text CANNOT promote `COMPLETED` (`SCN-06`); truthful `PLANNED` facts are preserved.
  - Negation/rollback/unmet-target text CANNOT promote positive achievements (`SCN-07`); truthful `NEGATED`/`TARGET_UNMET` facts are preserved.
  - Team-wide or passive-anonymous text CANNOT promote `INDIVIDUAL` personal attribution (`SCN-08`, `SCN-05`).
  - `STAGING_TEST` evidence CANNOT promote `PRODUCTION` metrics/results (`SCN-09`).
  - `CONTRADICTS` support transitions the Fact to `CONFLICTED` (never `SUPPORTED`, `SCN-05`).

---

## 4. `CanonicalFactNormalizer` & `Contributions` (`normalizer.py`)

- Promotes a `Fact` to `SUPPORTED` **ONLY IF**: `>= 1` `ACTIVE` `DIRECT` `fact_support` + `0` `ACTIVE` `CONTRADICTS` + identity gate passes.
- Deduplicates within the same `WorkEvent` strictly via deterministic SHA-256 over `(workspace_id, event_id, fact_type, subject_identity_id, normalized_predicate, normalized_object_key, completion_state, scope, environment_scope)` — never by fuzzy/embedding/LLM similarity.
- Automatic `RULE` contributions (`origin = 'RULE'`) are strictly limited to `IMPLEMENTED` and `REVIEWED` (`PROHIBITED_MVP_RULE_CONTRIBUTION_ROLE` otherwise) and reach `SOURCE_SUPPORTED` **ONLY** with an `ACTIVE` `VERIFIED` `PERSON` identity AND `>= 1` `ACTIVE` `contribution_support` pointing to a `SUPPORTED` Fact with `DIRECT` `fact_support` (`CLAIM-05`, `SCN-15`).

---

## 5. Programmatic Service Entrypoints (`PostgresFactService`) for Wave 3 & Wave 4

```python
from career_memory_api.facts import PostgresFactService
```

| Method | Purpose | Consumer |
|---|---|---|
| `extract_candidates_for_unit(access, *, event_id, content_unit_id, budget, adapter, provider_mode, ...)` | Stage 2A idempotent extraction for one ContentUnit; records `FACT_CANDIDATE_EXTRACTION` run + `0..N` `CANDIDATE` facts | Wave 3 / Wave 4 |
| `validate_and_normalize_candidates(access, *, event_id, fact_ids, content_unit_id, budget, adapter, provider_mode, ...)` | Stage 2B independent validation (`validator_run_id != extractor_run_id`) + `fact_support` (`extracted_span=NULL`) + status recomputation | Wave 3 / Wave 4 |
| `extract_and_validate_event_facts(access, *, event_id, content_unit_ids, budget, adapter, provider_mode, ...)` | End-to-end Stage 2A -> Stage 2B -> Normalizer within one `OperationRefetchSession` | Wave 3 / Wave 4 |
| `record_contribution(access, *, event_id, identity_id, role, origin=RecordOrigin.RULE, supporting_fact_ids=())` | Persists MVP `contributions` + `contribution_support` with `CLAIM-05` gating | Wave 3 / Wave 4 |
| `recalculate_facts_after_support_or_validator_change(access, *, content_unit_ids=None, source_object_revision_ids=None, invalidated_support_ids=None, superseded_validator_versions=None, reason=...)` | Invalidates affected `fact_support`, transitions `SUPPORTED` -> `STALE`/`CONFLICTED`, cascades `STALE` to `contributions`/`claim_atoms`/`claims` and `INVALIDATED` to `exports` | Wave 3 / M5 |

---

## 6. HTTP Wire Contract (`/v1/facts*`, `MVP API §6`, `ER-012`)

- All responses return `Cache-Control: private, no-store` and `X-Request-ID`.
- Missing or cross-workspace `fact_id` is strictly concealed as `404 RESOURCE_NOT_FOUND` (zero `details`).
- `project_id` / `project_ids` rejected with `422 PROHIBITED_PROJECT_ID_SCOPE` (`OD-008`).
- Plan C raw-text keys rejected with `422 PROHIBITED_RAW_TEXT_PERSISTENCE`; writable status/verification keys rejected with `422 PROHIBITED_FACT_STATUS_WRITE`.

| Method & Path | `operationId` | Idempotency-Key | Notes |
|---|---|---|---|
| `GET /v1/facts` | `listWorkspaceFacts` | No | Filters: `event_id`, `status`, `fact_type`, `include_superseded=false`, `limit (1..100)`, HMAC-signed `cursor`. |
| `GET /v1/facts/{fact_id}/supports` | `getFactSupports` | No | Returns `FactSummaryItem` + non-raw `FactSupportItem[]` (zero `extracted_span`). |
| `POST /v1/facts/{fact_id}/actions/approve` | `approveFact` | Required (UUID) | Requires `If-Match` or `expected_version`. Transitions eligible `CANDIDATE` -> `USER_CONFIRMED` (never `SUPPORTED`). |
| `POST /v1/facts/{fact_id}/actions/reject` | `rejectFact` | Required (UUID) | Requires `If-Match` or `expected_version` + `reason`. Marks `REJECTED` + atomic downstream `STALE`/`INVALIDATED` cascade. |
| `PATCH /v1/facts/{fact_id}` | `editFactVersion` | Required (UUID) | **ER-012 Non-Overwrite Edit**: marks prior row `SUPERSEDED`, inserts NEW `CANDIDATE` row (`version + 1`, `supersedes_fact_id = old_id`, zero copied supports), and atomically invalidates downstream dependents. |
