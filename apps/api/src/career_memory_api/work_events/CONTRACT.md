# M3 Work Events Backend Contract (`T-302` · `T-305` · `T-303`)

Handoff interface specification for **Wave 3** (`T-304` Event Review Mutations: `CONFIRM`, `EDIT`, `SPLIT`, `MERGE`, `DISSOLVE`, `REJECT` and `T-306A` M3 UI/Backend E2E integration).

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

| Function | Signature / Behavior | Wave 3 Consumer |
|---|---|---|
| `canonicalize_event_pair` | `(event_a, event_b) -> (left_id, right_id)` (`left < right`; raises `ValueError` on equal/invalid UUID) | `T-304` `SPLIT` / `MERGE` |
| `list_active_evidence_memberships` | `(conn, *, workspace_id, event_id)` -> strictly `MUST_LINK` + `ACTIVE` only (`ER-002`) | `T-303`, Fact/Claim pipeline |
| `list_active_context_relations` | `(conn, *, workspace_id, event_id)` -> strictly `CONTEXT_ONLY` + `ACTIVE` only | `T-303`, UI context drawer |
| `list_cannot_link_summary` | `(conn, *, workspace_id, event_id)` -> `CannotLinkSummary(source_object_ids, peer_event_ids, constraints)` | `T-303`, `T-304` |
| `record_source_relation` | Rejects `USER_COMPOSITE` + `MUST_LINK`, rejects `ISSUE`/heuristic `MUST_LINK` (`EVENT-04`), blocks auto `SOURCE`/`RULE` `MUST_LINK` when active `CANNOT_LINK` exists (`EVENT-05`) | `T-304` `EDIT` / `SPLIT` |
| `record_pairwise_cannot_link` | Idempotent canonical insert into `public.event_constraints` (`origin='SPLIT'|'USER'|'POLICY'`) | `T-304A` `SPLIT` sibling guard, `T-304B` `DISSOLVE` / user exclusion |
| `check_merge_cannot_link_conflicts` | `(conn, *, workspace_id, member_event_ids)` -> detects BOTH pairwise `event_constraints` (`CAND-EV-021`, `SPLIT` siblings) AND cross-member `MUST_LINK` vs `CANNOT_LINK` source-object conflicts | `T-304B` `MERGE` pre-check (return `409 CANNOT_LINK_CONFLICT`) |

---

## 3. Immutable History API (`career_memory_api.work_events.history`, `T-305`)

Every Wave 3 mutation (`CONFIRM`, `EDIT`, `SPLIT`, `MERGE`, `DISSOLVE`, `REJECT`) MUST follow this exact transactional order inside `workspace_scoped_transaction`:
1. Lock target `public.work_events` row(s) with `SELECT ... FOR UPDATE` and verify `If-Match` / `expected_version` against `format_work_event_etag(row.version)`.
2. Record the append-only audit entry via `record_work_event_review_action(...)` (idempotent on `(workspace_id, action, 'WORK_EVENT', target_id, idempotency_key)`).
3. Update the live projection row(s) in `public.work_events` (incrementing `version = version + 1`) and update `event_memberships` / `event_composite_memberships` / `event_constraints` as required.
4. Append the new immutable version snapshot via `append_event_version_snapshot(..., created_by_review_action_id=review_action.review_action_id)`.
   - For `USER_COMPOSITE` `MERGE`: record `event_composite_memberships` with each member's pre-supersede `member_event_version` (ensuring a snapshot for that `member_event_version` exists in `work_event_versions`), transition members to `SUPERSEDED` (and append their `SUPERSEDED` version snapshot), then append the composite's `v1` snapshot.
   - For source invalidation (`STALE`): call `mark_work_event_stale_from_source(...)` which updates `work_events`, appends a `work_event_versions` snapshot (`created_by_review_action_id = NULL`), and writes `audit_events` (`WORK_EVENT_MARKED_STALE`) — **never** creating a user `REJECT` row in `review_actions`.

---

## 4. Read API Wire Contract (`T-303`, OpenAPI Integrated)

- **Common Guarantees**:
  - Authenticated via `WorkspaceAccessGuard` (`401`/`403`/`503`), executed under `cm_api_app` `FORCE RLS`.
  - Response headers: `Cache-Control: private, no-store` and `X-Request-ID` on all responses; `ETag: "v{version}"` on `GET /v1/work-events/{id}`.
  - `project_id` / `project_ids` query params rejected with `422 PROHIBITED_PROJECT_ID_SCOPE` (`OD-008`).
  - Missing or cross-workspace `event_id` / `source_resource_id` concealed as `404 RESOURCE_NOT_FOUND` (no `details`).
  - Zero external GitHub API calls during metadata reads (`Plan C`).
  - Real-time `policy_state` (`ALLOWED | POLICY_BLOCKED | DELETION_PENDING`) evaluated against `source_resources`, `source_accounts`, and active `deletion_jobs` fences; when non-`ALLOWED`, `allowed_actions` is `[]` and `downstream_impact.processing_status` reflects `POLICY_BLOCKED` or `DELETION_PENDING`.
- **`GET /v1/work-events`** (`operationId: listWorkEvents`):
  - Query params: `limit` (`1..100`, default `50`), `cursor` (HMAC-bound opaque token), `status`, `source_resource_id`, `from` (`YYYY-MM-DD`), `to` (`YYYY-MM-DD`), `role` (`ContributionRole`).
  - Default (`status` omitted): returns active top-level events only (`status IN ('CANDIDATE','CONFIRMED','STALE')` and NOT an active `event_composite_memberships` member), guaranteeing `0` double-counting (`EV-010`).
  - Body: `{ items: WorkEventSummaryItem[], page: { next_cursor, has_more }, counts: { active_top_level_total, by_status: { CANDIDATE, CONFIRMED, STALE } } }`.
- **`GET /v1/work-events/{id}`** (`operationId: getWorkEventDetail`):
  - Serves active, `SUPERSEDED`, and `REJECTED` events for full audit traceability (`EV-010`, `EV-011`, `EV-012`).
  - Header: `ETag: "v{version}"` (`ER-004`).
  - Body: `WorkEventSummaryItem` fields + `primary_anchor_source_object_id`, `anchor_scope`, `evidence_memberships` (`MUST_LINK`+`ACTIVE` only), `context_relations` (`CONTEXT_ONLY`+`ACTIVE` only), `cannot_link_constraints`, `composite_details` (with immutable `work_event_versions` member snapshots), `downstream_impact`, `version_history`, `review_actions`.
