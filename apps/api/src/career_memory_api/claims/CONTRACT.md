# M4 Wave 3A Grounded Claim Pipeline & `/v1/claims*` API Contract (`T-404`, `T-405`, `T-405A`, `CLAIM-04..09`, `ER-012`)

Authoritative backend interface specification for the Stage 3A/3B/3C Grounded Claim Pipeline, read-only verification computation, `CLAIM-09` approval race fence, and `/v1/claims*` HTTP wire contract.

---

## 1. Strict Bounded Stage Separation (`Grounded Claim Pipeline §11`)

| Module | Pipeline Role | `generation_runs.run_type` | AI JSON Schema (`v1`) | Algorithm / Version |
|---|---|---|---|---|
| `career_memory_api.claims.context_builder` | Stage 3A `ClaimContextBuilder` | — (deterministic selection & budget fence) | — | deterministic `(fact_type, fact_id)` |
| `career_memory_api.claims.generator` | Stage 3A `ClaimGenerator` | `CLAIM_DRAFT_GENERATION` | `claim_draft.v1.schema.json` | `claim-generator-v1` |
| `career_memory_api.claims.atomizer` | Stage 3B `ClaimAtomizer` | `CLAIM_ATOMIZATION` | `claim_atomization.v1.schema.json` | `claim-atomizer-v1` |
| `career_memory_api.claims.validator` | Stage 3C `ClaimSupportValidator` | `CLAIM_SUPPORT_VALIDATION` | `claim_support_validation.v1.schema.json` | `claim-validator-v1` |
| `career_memory_api.claims.verification` | Read-only `VerificationCalculator` | — (read-only view & epoch computation) | — | `v_claim_verification_summary` + `support_graph_epoch` |

### Core Separation & Plan C Invariants
- Pairwise distinct execution run IDs are strictly enforced: `generator_run_id`, `atomizer_run_id`, and `validation_run_id` MUST be distinct (`422 STAGE_RUN_ID_COLLISION`).
- Stage 3A/3B/3C execute strictly through `career_memory_api.llm_gate.BoundedStageRunner`, preserving the default hard-OFF live LLM gate (`MANUAL_ONLY_BLOCKED`, `0` external calls, actionable `free_alternatives`) while supporting `SYNTHETIC_FIXTURE_OFFLINE` and approved subscription execution.
- Zero raw source text, quotes, excerpts, or operational secrets are persisted in `claims`, `claim_atoms`, `claim_atom_support`, `generation_runs`, `review_actions`, `audit_events`, `outbox_events`, or HTTP responses.

---

## 2. Stage 3A — `ClaimContextBuilder` & `ClaimGenerator` (`T-404`)

- Target `WorkEvent` MUST be `CONFIRMED` and NOT an active member of a `USER_COMPOSITE` event (`409 EVENT_NOT_CONFIRMED` / `409 EVENT_IS_COMPOSITE_MEMBER`).
- Includes ONLY active, non-superseded same-event `facts` with `status == 'SUPPORTED'`, `>= 1` `ACTIVE` `DIRECT` `fact_support`, `0` `ACTIVE` `CONTRADICTS`, `ACTIVE` `ContentUnit`, non-tombstoned `SourceObjectRevision`, and zero deletion/policy fence.
- Deterministic sort by `(fact_type, fact_id)`, deduplicated by `(fact_id, version, canonical_input_hash)`, and explicit byte/token budget fence (`PARTIAL_WITH_LIMIT` / `MANUAL_ONLY` — never silently dropping required facts).
- `0` eligible facts (`SCN-01`) yields `ContextBuildStatus.EMPTY` (`DRAFT_INVALID` if generation attempted; cannot invent facts).
- `ClaimGenerator` validates `claim_draft.v1.schema.json`, non-empty subset `input_fact_ids`, hallucination guards (no invented metrics/environments/roles outside manifest), and persists `public.claims` with `status = 'DRAFT'`, `version = 1` (never setting `verification` or `SOURCE_VERIFIED`).

---

## 3. Stage 3B `ClaimAtomizer` & Stage 3C `ClaimSupportValidator` (`T-405`, `T-405A`)

- `ClaimAtomizer` decomposes a Claim version into ordered (`ordinal = 0..K-1`) typed `claim_atoms` starting strictly in `status = 'UNSUPPORTED'` without ever creating `claim_atom_support`.
- `ClaimSupportValidator` validates every atom deterministically against same-event Canonical Facts, active provenance, current-operation Plan C `RefetchResolverService` matches, and adversarial guards:
  - `CONTEXT`-only bindings (`SCN-04`), contradicted facts (`SCN-05`), `PLANNED`/`NEGATED`/`TARGET_UNMET`/`TEAM`/`STAGING_TEST`/`OBSERVED_CORRELATION` mismatches (`SCN-06..10`), and numeric/scope distortions (`SCN-14`, `SCN-16`) resolve to `UNSUPPORTED`.
  - **`CLAIM-05` Role / Contribution Gate (`SCN-15`)**: `ROLE` or personal attribution atoms require an `ACTIVE` `VERIFIED` `PERSON` identity (`evaluate_identity_attribution(...).can_support_source_verified == True`), a matching `SOURCE_SUPPORTED` row in `public.contributions`, AND `>= 1` `ACTIVE` `public.contribution_support` pointing to a concrete `SUPPORTED` Fact.
  - **`CLAIM-06` 6-Element Metric Gate (`SCN-16`)**: `METRIC` atoms require exact 6-element grounding (`value`, `unit`, `period`, `source_basis`, `environment_scope`, `measurement_scope`) matching the supporting `METRIC` Fact with no scope over-generalization or numeric distortion.
  - **`USER_CONFIRMED` Atom Rule (`SCN-12` `claim-12-4`)**: Atoms backed only by `USER_CONFIRMED` facts receive `USER_CONFIRMED` status and NEVER promote `computed_verification` to `SOURCE_VERIFIED`.
- `VerificationCalculator` computes read-only `computed_verification` (`SOURCE_VERIFIED | USER_CONFIRMED | MIXED | UNVERIFIED`), `approvable`, `approval_block_reasons`, and deterministic `support_graph_epoch` (`sha256:<64-hex>`).

---

## 4. `ER-012` Non-Overwrite Edit (`CLAIM-08`) & `CLAIM-09` Approval Race Fence

- **`PATCH /v1/claims/{claim_id}`**: Never overwrites in place. Marks editable non-approved rows `SUPERSEDED`; preserves prior `APPROVED` rows as history; inserts a new `DRAFT` row (`version = expected_version + 1`, `supersedes_claim_id = old_id`, `approved_at = NULL`, `last_verified_at = NULL`, `last_verification_run_id = NULL`, zero copied atoms/supports).
- **`POST /v1/claims/{claim_id}/actions/approve`**:
  1. Rejects any writable `verification` or `SOURCE_VERIFIED` field (`422 PROHIBITED_READONLY_VERIFICATION_WRITE`).
  2. Executes live current-operation Plan C refetch (`verify_required_supports_for_gate(..., operation_kind=GateOperationKind.APPROVAL)`, `REF-07a`/`REF-07d`).
  3. Acquires `FOR UPDATE` locks across `claims`, `claim_atoms`, `claim_atom_support`, linked `facts`/`fact_support`, and `contributions` inside a single `workspace_scoped_transaction`.
  4. Re-computes `support_graph_epoch`; any drift against pre-check or `expected_support_graph_epoch` fails closed with `409 SUPPORT_INCOMPLETE` (`details.reason_code = "CLAIM_NOT_APPROVABLE_EPOCH_DRIFT"`).
  5. Requires `acknowledge_user_confirmed_atoms = True` when `computed_verification` is `USER_CONFIRMED` or `MIXED`.

---

## 5. HTTP Wire Contract (`/v1/claims*`)

All responses enforce `Cache-Control: private, no-store`, `X-Request-ID`, `404 RESOURCE_NOT_FOUND` concealment, `422 PROHIBITED_PROJECT_ID_SCOPE`, `422 PROHIBITED_RAW_TEXT_PERSISTENCE`, and `422 PROHIBITED_READONLY_VERIFICATION_WRITE`.

| Method & Path | `operationId` | Idempotency-Key | Notes |
|---|---|---|---|
| `GET /v1/claims` | `listWorkspaceClaims` | No | Filters: `event_id`, `status`, `include_superseded=false`, `limit (1..100)`, HMAC cursor. |
| `POST /v1/claims/generations` | `generateEventClaims` | Required (UUID) | Runs Stage 3A -> 3B -> 3C; returns `201` on `SUCCEEDED`, `200` on `MANUAL_ONLY_BLOCKED`/`DRAFT_INVALID`. |
| `GET /v1/claims/{claim_id}` | `getClaimDetail` | No | Full `ClaimDetailResponse` with atoms, `computed_verification`, and `support_graph_epoch`. |
| `GET /v1/claims/{claim_id}/supports` | `getClaimSupports` | No | Full non-raw `Claim -> Atom -> Fact -> FactSupport -> ContentUnit -> Revision` provenance chain. |
| `POST /v1/claims/{claim_id}/actions/revalidate` | `revalidateClaimVersion` | Required (UUID) | Fresh Stage 3B + Stage 3C validation on edited/stale claim versions. |
| `POST /v1/claims/{claim_id}/actions/approve` | `approveClaimAction` | Required (UUID) | Live `APPROVAL` refetch + `FOR UPDATE` epoch race fence (`CLAIM-09`). |
| `POST /v1/claims/{claim_id}/actions/reject` | `rejectClaimAction` | Required (UUID) | Marks `REJECTED` + cascades `INVALIDATED` to dependent `exports`. |
| `POST /v1/claims/drafts` | `createClaimDraft` | No | Preserved M1 `contracts_router` route (delegates to `PostgresClaimService` when configured). |
| `PATCH /v1/claims/{claim_id}` | `editClaimVersion` | No | Preserved M1 `contracts_router` route (delegates `ER-012` versioned edit to `PostgresClaimService`). |
| `POST /v1/claims/{claim_id}/approve` | `approveClaimVersion` | No | Preserved M1 `contracts_router` route (delegates race-fenced approval to `PostgresClaimService`). |
