# M3 Work Events Backend Contract (`T-302` · `T-305` · `T-303` · `T-304` · `T-304A` · `T-304B`)

Authoritative interface specification for **Wave 3** (`T-304` Atomic Event Review & Curation Mutations) and **Wave 4** (`T-306A` 21-case synthetic regression harness & M3 E2E integration).

---

## 1. Schema Foundation (`db/migrations/0060_m3_event_versions_and_constraints.sql`)

### 1.1 `public.event_constraints` (`OD-007` Option B)
- Stores **pairwise `WorkEvent ↔ WorkEvent`** constraints separately from `public.event_memberships`.
- Enforces canonical pair ordering via `CHECK (left_event_id < right_event_id)` (no self-reference, no reversed duplicates).
- Enforces single active constraint per pair via partial unique index `uq_event_constraints_active_pair` (`WHERE status = 'ACTIVE'`).
- Columns: `constraint_type ('CANNOT_LINK')`, `origin ('USER' | 'SPLIT' | 'POLICY')`, `status ('ACTIVE' | 'REVOKED')`, `rationale_code`, `constraint_metadata` (Plan C JSONB guard), `created_by_review_action_id` (Composite FK to `review_actions`), `revoked_at`.
- RLS: `ENABLE + FORCE RLS` scoped by `workspace_id = auth_ctx.current_workspace_id()`. Grants: `SELECT, INSERT, UPDATE` to `cm_api_app` and `cm_worker_app`.

### 1.2 `public.work_event_versions` (`T-305` Append-Only Immutable Snapshots)
- Stores immutable version snapshots `v1..vN` for every `work_events` row alongside Plan C non-raw `membership_snapshot`.
- Unique per `(workspace_id, event_id, version)` (`uq_work_event_versions_event_version`).
- Enforces immutability via `BEFORE UPDATE OR DELETE` trigger `trg_work_event_versions_immutable` (`public.cm_guard_work_event_versions_immutable()`):
  - `UPDATE` is always rejected (`ERRCODE = '23514'`).
  - `DELETE` is rejected unless `auth_ctx.allow_revision_purge()` is `true` (`SET LOCAL app.allow_revision_purge = 'on'` inside an approved `OD-003` deletion purge).
- Grants: strictly append-only (`SELECT, INSERT` only to `cm_api_app` and `cm_worker_app`).
- `PostgresEventCandidateBuilder` automatically writes `v1` snapshots inside the same transaction whenever `to_regclass('public.work_event_versions') IS NOT NULL`.

---

## 2. Relation Semantics API (`career_memory_api.work_events.relations`, `T-302`)

| Function | Signature / Behavior | Consumer |
|---|---|---|
| `canonicalize_event_pair` | `(event_a, event_b) -> (left_id, right_id)` (`left < right`; raises `ValueError` on equal/invalid UUID) | `T-304` `SPLIT` / `MERGE` |
| `list_active_evidence_memberships` | `(conn, *, workspace_id, event_id)` -> strictly `MUST_LINK` + `ACTIVE` only (`ER-002`) | `T-303`, Fact/Claim pipeline |
| `list_active_context_relations` | `(conn, *, workspace_id, event_id)` -> strictly `CONTEXT_ONLY` + `ACTIVE` only | `T-303`, UI context drawer |
| `list_cannot_link_summary` | `(conn, *, workspace_id, event_id)` -> `CannotLinkSummary(source_object_ids, peer_event_ids, constraints)` | `T-303`, `T-304` |
| `record_source_relation` | Rejects `USER_COMPOSITE` + `MUST_LINK`, rejects `ISSUE`/heuristic `MUST_LINK` (`EVENT-04`), blocks auto `SOURCE`/`RULE` `MUST_LINK` when active `CANNOT_LINK` exists (`EVENT-05`) | `T-304` `EDIT` / `SPLIT` |
| `record_pairwise_cannot_link` | Idempotent canonical insert into `public.event_constraints` (`origin='SPLIT'\|'USER'\|'POLICY'`) | `T-304A` `SPLIT` sibling guard, `T-304B` `DISSOLVE` / user exclusion |
| `check_merge_cannot_link_conflicts` | `(conn, *, workspace_id, member_event_ids)` -> detects BOTH pairwise `event_constraints` (`CAND-EV-021`, `SPLIT` siblings) AND cross-member `MUST_LINK` vs `CANNOT_LINK` source-object conflicts | `T-304B` `MERGE` pre-check (`409 CANNOT_LINK_CONFLICT`) |

---

## 3. Immutable History API (`career_memory_api.work_events.history`, `T-305`)

Every mutation (`CONFIRM`, `EDIT`, `SPLIT`, `MERGE`, `DISSOLVE`, `REJECT`) executes inside `workspace_scoped_transaction`:
1. Lock target `public.work_events` row(s) in deterministic UUID ascending order (`ORDER BY id ASC FOR UPDATE`).
2. Re-verify live `source_resources` visibility/status (`403 SOURCE_POLICY_BLOCKED`) and `deletion_jobs` fence (`409 DELETION_PENDING`).
3. Check request-fingerprint idempotency against `public.review_actions` (exact replay returns stored result; conflicting key reuse raises `409 IDEMPOTENCY_CONFLICT`).
4. Verify `expected_version` (and optional `If-Match`) against locked `work_events.version` (`409 EVENT_VERSION_CONFLICT` with `{event_id, expected_version, current_version, current_status, etag, allowed_actions}`).
5. Ensure pre-transition `work_event_versions` snapshot exists, write `review_actions`, update live `work_events` (`version + 1`), append post-transition `work_event_versions` snapshot, emit `outbox_events` & `audit_events`, and invoke optional `before_commit_hook`.

---

## 4. Read API Wire Contract (`T-303`, OpenAPI Integrated)

- **Common Guarantees**:
  - Authenticated via `WorkspaceAccessGuard` (`401`/`403`/`503`), executed under `cm_api_app` `FORCE RLS`.
  - Response headers: `Cache-Control: private, no-store` and `X-Request-ID` on all responses; `ETag: "v{version}"` on detail & mutation responses.
  - `project_id` / `project_ids` query/body params rejected with `422 PROHIBITED_PROJECT_ID_SCOPE` (`OD-008`).
  - Plan C forbidden raw-text keys in any request body rejected with `422 PROHIBITED_RAW_TEXT_PERSISTENCE`.
  - Missing or cross-workspace IDs concealed as `404 RESOURCE_NOT_FOUND` (no `details`).
  - `compute_allowed_actions(..., anchor_scope=...)`: allows `SPLIT` on active `PR` and unsplit active `DIRECT_COMMIT` (`anchor_scope is None` or empty via `has_non_empty_anchor_scope`); already split `PR_SEGMENT` and scoped `DIRECT_COMMIT` allow `MERGE` without re-split.
- **`GET /v1/work-events`** (`operationId: listWorkEvents`)
- **`GET /v1/work-events/{id}`** (`operationId: getWorkEventDetail`)

---

## 5. Atomic Mutation Domain Service & Wire Routes (`T-304` · `T-304A` · `T-304B`)

### 5.1 Service Class (`career_memory_api.work_events.PostgresWorkEventMutationService`)
```python
PostgresWorkEventMutationService(
    *,
    api_connection_factory: ConnectionFactory,
    before_commit_hook: BeforeCommitHook | None = None,  # called before commit for barriers / rollback tests
    now_fn: Callable[[], datetime] | None = None,
)
```

| Method | Signature | HTTP Route & `operationId` |
|---|---|---|
| `confirm_work_event` | `(access, *, event_id, request: ConfirmWorkEventRequest, idempotency_key_header=None, if_match_header=None) -> WorkEventDetailResponse` | `POST /v1/work-events/{id}/actions/confirm` (`confirmWorkEvent`, UI Keep) |
| `edit_work_event` | `(access, *, event_id, request: EditWorkEventRequest, idempotency_key_header=None, if_match_header=None) -> WorkEventDetailResponse` | `PATCH /v1/work-events/{id}` (`editWorkEvent`) |
| `reject_work_event` | `(access, *, event_id, request: RejectWorkEventRequest, idempotency_key_header=None, if_match_header=None) -> WorkEventDetailResponse` | `POST /v1/work-events/{id}/actions/reject` (`rejectWorkEvent`, UI Exclude) |
| `split_work_event` | `(access, *, event_id, request: SplitWorkEventRequest, idempotency_key_header=None, if_match_header=None) -> SplitWorkEventResponse` | `POST /v1/work-events/{id}/actions/split` (`splitWorkEvent`) |
| `merge_work_events` | `(access, *, request: MergeWorkEventsRequest, idempotency_key_header=None, conceal_missing_as_not_found=True) -> MergeWorkEventsResponse` | `POST /v1/work-events/actions/merge` (`mergeWorkEvents`) |
| `dissolve_work_event` | `(access, *, event_id, request: DissolveWorkEventRequest, idempotency_key_header=None, if_match_header=None) -> DissolveWorkEventResponse` | `POST /v1/work-events/{id}/actions/dissolve` (`dissolveWorkEvent`) |

### 5.2 Stable Error Codes & Domain Semantics (Wave 4 `T-306A` Reference)
- **Headers & Validation (`422`)**:
  - `MISSING_IDEMPOTENCY_KEY`, `IDEMPOTENCY_KEY_MISMATCH`, `INVALID_IDEMPOTENCY_KEY`
  - `INVALID_IF_MATCH_HEADER`, `VERSION_EXPECTATION_MISMATCH`
  - `INVALID_EDIT_REQUEST`, `INVALID_MERGE_REQUEST`
  - `INVALID_SPLIT_SCOPE` (empty `anchor_scope`, blank title, foreign `source_object_id` or `content_unit_id`)
  - `SPLIT_SCOPE_OVERLAP` (intersecting `file_paths`, `diff_hunks`/`hunk_ranges`, `review_thread_ids`, or `content_unit_ids`)
  - `NON_EVENT_MEMBER_NOT_ALLOWED` (`EV-013`: `ISSUE` or non-Event `source_objects` passed to `merge`)
  - `PROHIBITED_RAW_TEXT_PERSISTENCE` (Plan C), `PROHIBITED_PROJECT_ID_SCOPE` (`OD-008`)
- **Policy & Deletion Fences**:
  - `403 SOURCE_POLICY_BLOCKED` (repository non-`PUBLIC` / paused / `POLICY_REVIEW_REQUIRED` / account revoked)
  - `409 DELETION_PENDING` (active `deletion_jobs` fence in `PENDING`/`RUNNING`/`PARTIAL`/`SUCCEEDED` or resource `DELETED`)
- **Concurrency & Idempotency (`409`)**:
  - `EVENT_VERSION_CONFLICT` (`details`: `event_id`, `expected_version`, `current_version`, `current_status`, `etag`, `allowed_actions`)
  - `IDEMPOTENCY_CONFLICT` (same `idempotency_key` reused with different action/target/version/params)
- **Lifecycle & Curation Guards (`409`)**:
  - `EVENT_ALREADY_CONFIRMED`, `EVENT_NOT_ACTIVE`, `SPLIT_NOT_ALLOWED`
  - `USER_COMPOSITE_REJECT_REQUIRES_DISSOLVE` (direct `REJECT` on `USER_COMPOSITE`)
  - `NOT_ACTIVE_USER_COMPOSITE` (`DISSOLVE` on non-composite or already dissolved composite)
  - `DUPLICATE_COMPOSITE_MEMBER`, `PR_ASSOCIATION_INCOMPLETE` (`EV-013` unpromoted `UNKNOWN` commit)
  - `CROSS_REPOSITORY_MEMBER` (`EV-016`), `CROSS_WORKSPACE_MEMBER` (domain call with `conceal_missing_as_not_found=False`; HTTP returns `404 RESOURCE_NOT_FOUND` with no `details`)
  - `NESTED_COMPOSITE_NOT_ALLOWED` (`EV-014`), `MEMBER_ALREADY_COMPOSED` (`EV-014`), `MEMBER_NOT_ACTIVE` (`EV-014`)
  - `CANNOT_LINK_CONFLICT` (`CAND-EV-021`, `SPLIT` siblings, cross-member `MUST_LINK` vs `CANNOT_LINK`)
- **Downstream & Provenance Invariants**:
  - `REJECT` (`EV-011`) NEVER deletes or modifies `source_objects`, `source_object_revisions`, or `content_units`.
  - `MERGE` (`T-304B`) NEVER inserts `MUST_LINK` rows on `USER_COMPOSITE` and NEVER copies/inherits member `facts`/`claims`; member downstream rows become `STALE` and `outbox_events` (`WORK_EVENT_COMPOSITE_MERGED`) is written atomically.
  - `DISSOLVE` (`EV-012`, `EVENT-12`) marks composite `facts`/`claims` `STALE`, writes `outbox_events` (`WORK_EVENT_COMPOSITE_DISSOLVED`), and re-evaluates each member as `STALE` (invalid/orphaned/tombstoned/policy-blocked source), `CONFIRMED` (valid source + pre-merge snapshot `CONFIRMED`), or `CANDIDATE` (valid source otherwise).

---

## 6. Search & Export Single Logical Event Collapse (`career_memory_api.work_events.search_collapse`)

| Function | Behavior |
|---|---|
| `collapse_event_ids_for_search_and_export(conn, *, workspace_id, candidate_event_ids) -> list[str]` | Maps active `USER_COMPOSITE` members strictly to their active composite ID (0 duplicates, `EV-009`/`EV-010`), excludes `REJECTED` (`EV-011`) and dissolved `SUPERSEDED` composites (`EV-012`), preserves ranking order. |
| `query_searchable_top_level_work_events(conn, *, workspace_id, query_text, source_resource_id=None, limit=50) -> list[str]` | Matches non-raw WorkEvent text and linked `source_objects.provider_object_id` across active top-level events and composite members, then collapses via `collapse_event_ids_for_search_and_export`. |

---

## 7. Router Assembly & Frontend Wire Interoperability

- `create_work_events_router(*, service: WorkEventsReadPort | None = None, mutation_service: WorkEventsMutationPort | None = None, resolve_access: AccessResolver | None = None) -> APIRouter`
- `spikes/web-import/src/adapters/event-review-http-transport.ts` normalizes both synthetic stubs and live `career_memory_api` JSON payloads losslessly (`normalizeWorkEventListResponse`, `normalizeWorkEventDetail`, `normalizeSplitWorkEventResponse`, `normalizeMergeWorkEventsResponse`, `normalizeDissolveWorkEventResponse`), verified via live PG18 FastAPI loopback in `spikes/web-import/tests/contract-http/live-backend-event-review.test.tsx`.
