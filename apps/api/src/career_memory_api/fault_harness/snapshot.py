"""T-005C Logical PostgreSQL snapshot extractor and 6 global B-004 invariant verifiers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

from career_memory_api.fault_harness.fault_hooks import CanaryAuditResult


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    """Single B-004 global invariant violation."""

    invariant_id: str
    phase: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "invariant_id": self.invariant_id,
            "phase": self.phase,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class GateCheckObservations:
    """Observed post-run gate behaviors (Refetch, AI, Export, Claim Approval) and extra metrics."""

    refetch_content_allowed: bool | None = None
    refetch_resolution_status: str | None = None
    external_ai_gate_allowed: bool | None = None
    export_gate_allowed: bool | None = None
    claim_approval_gate_allowed: bool | None = None
    concurrent_stale_fence_discarded: int = 0
    extra: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "refetch_content_allowed": self.refetch_content_allowed,
            "refetch_resolution_status": self.refetch_resolution_status,
            "external_ai_gate_allowed": self.external_ai_gate_allowed,
            "export_gate_allowed": self.export_gate_allowed,
            "claim_approval_gate_allowed": self.claim_approval_gate_allowed,
            "concurrent_stale_fence_discarded": self.concurrent_stale_fence_discarded,
        }
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


@dataclass(frozen=True, slots=True)
class LogicalDbSnapshot:
    """Normalized, UUID-free, wall-clock-free PostgreSQL 18 logical state for B-004 comparison."""

    data: dict[str, Any]

    @property
    def snapshot_sha256(self) -> str:
        return canonical_json_sha256(self.data)

    @property
    def core_convergence_sha256(self) -> str:
        """Hash over core DB tables (excluding scenario-specific gate telemetry & extra notes)."""
        core = {
            "source_objects": self.data["source_objects"],
            "source_object_revisions": self.data["source_object_revisions"],
            "sync_commit_pr_associations": self.data["sync_commit_pr_associations"],
            "sync_checkpoints": self.data["sync_checkpoints"],
            "sync_run_page_observations": self.data["sync_run_page_observations"],
            "sync_item_outcomes": self.data["sync_item_outcomes"],
            "sync_runs": self.data["sync_runs"],
            "outbox_summary": self.data["outbox_summary"],
            "work_events": self.data["work_events"],
            "event_memberships_count": self.data["event_memberships_count"],
            "content_units": self.data["content_units"],
            "resource_and_account_policy": self.data["resource_and_account_policy"],
        }
        return canonical_json_sha256(core)


def extract_logical_db_snapshot(
    admin_conn: psycopg.Connection,
    *,
    workspace_id: str,
    source_resource_id: str | None = None,
    sync_run_id: str | None = None,
    gate_checks: GateCheckObservations | None = None,
) -> LogicalDbSnapshot:
    """Extract deterministic logical DB state from PostgreSQL 18 ignoring random UUIDs & wall clocks."""
    with admin_conn.cursor() as cur:
        # 1. source_objects
        if source_resource_id is not None:
            cur.execute(
                """
                SELECT object_type, provider_object_id, lifecycle_state, pr_association_state
                FROM public.source_objects
                WHERE workspace_id = %s::uuid
                  AND source_resource_id = %s::uuid
                ORDER BY object_type ASC, provider_object_id ASC
                """,
                (workspace_id, source_resource_id),
            )
        else:
            cur.execute(
                """
                SELECT object_type, provider_object_id, lifecycle_state, pr_association_state
                FROM public.source_objects
                WHERE workspace_id = %s::uuid
                ORDER BY object_type ASC, provider_object_id ASC
                """,
                (workspace_id,),
            )
        source_objects = [
            {
                "object_type": str(r[0]),
                "provider_object_id": str(r[1]),
                "lifecycle_state": str(r[2]),
                "pr_association_state": str(r[3]) if r[3] is not None else None,
            }
            for r in cur.fetchall()
        ]

        # 2. source_object_revisions
        if source_resource_id is not None:
            cur.execute(
                """
                SELECT so.object_type, so.provider_object_id, rev.provider_revision_id,
                       rev.content_hash, rev.is_tombstone
                FROM public.source_object_revisions rev
                JOIN public.source_objects so ON so.id = rev.source_object_id
                WHERE rev.workspace_id = %s::uuid
                  AND so.source_resource_id = %s::uuid
                ORDER BY so.object_type ASC, so.provider_object_id ASC, rev.content_hash ASC
                """,
                (workspace_id, source_resource_id),
            )
        else:
            cur.execute(
                """
                SELECT so.object_type, so.provider_object_id, rev.provider_revision_id,
                       rev.content_hash, rev.is_tombstone
                FROM public.source_object_revisions rev
                JOIN public.source_objects so ON so.id = rev.source_object_id
                WHERE rev.workspace_id = %s::uuid
                ORDER BY so.object_type ASC, so.provider_object_id ASC, rev.content_hash ASC
                """,
                (workspace_id,),
            )
        source_object_revisions = [
            {
                "object_type": str(r[0]),
                "provider_object_id": str(r[1]),
                "provider_revision_id": str(r[2]) if r[2] is not None else None,
                "content_hash": str(r[3]),
                "is_tombstone": bool(r[4]),
            }
            for r in cur.fetchall()
        ]

        # 3. sync_commit_pr_associations
        assoc_where = "WHERE assoc.workspace_id = %s::uuid"
        assoc_params: list[Any] = [workspace_id]
        if sync_run_id is not None:
            assoc_where += " AND assoc.sync_run_id = %s::uuid"
            assoc_params.append(sync_run_id)
        elif source_resource_id is not None:
            assoc_where += " AND assoc.source_resource_id = %s::uuid"
            assoc_params.append(source_resource_id)

        cur.execute(
            f"""
            SELECT assoc.commit_sha, assoc.default_branch_name, assoc.pr_association_state,
                   assoc.matched_pr_provider_id, assoc.matched_pr_in_window,
                   assoc.match_mechanism, assoc.unknown_reason_code,
                   (
                       SELECT sio.reason_code
                       FROM public.sync_item_outcomes sio
                       WHERE sio.workspace_id = assoc.workspace_id
                         AND sio.sync_run_id = assoc.sync_run_id
                         AND sio.object_type = 'COMMIT'
                         AND sio.provider_object_id = assoc.commit_sha
                         AND sio.outcome_kind = 'EXCLUDED'
                       ORDER BY sio.reason_code ASC
                       LIMIT 1
                   ) AS excluded_reason
            FROM public.sync_commit_pr_associations assoc
            {assoc_where}
            ORDER BY assoc.commit_sha ASC
            """,
            tuple(assoc_params),
        )
        sync_commit_pr_associations = []
        for r in cur.fetchall():
            matched_id = str(r[3]) if r[3] is not None else None
            in_window = bool(r[4]) if r[4] is not None else None
            matched_nums = [int(matched_id)] if (matched_id is not None and matched_id.isdigit()) else []
            eligible_nums = matched_nums if in_window else []
            sync_commit_pr_associations.append(
                {
                    "commit_sha": str(r[0]),
                    "default_branch_name": str(r[1]),
                    "association_state": str(r[2]),
                    "matched_pr_numbers": matched_nums,
                    "eligible_in_window_pr_numbers": eligible_nums,
                    "match_mechanism": str(r[5]) if r[5] is not None else None,
                    "excluded_reason": str(r[7]) if r[7] is not None else None,
                    "unknown_reason_code": str(r[6]) if r[6] is not None else None,
                }
            )

        # 4. sync_checkpoints
        cp_where = "WHERE workspace_id = %s::uuid"
        cp_params: list[Any] = [workspace_id]
        if sync_run_id is not None:
            cp_where += " AND sync_run_id = %s::uuid"
            cp_params.append(sync_run_id)
        elif source_resource_id is not None:
            cp_where += (
                " AND sync_run_id IN (SELECT id FROM public.sync_runs "
                "WHERE workspace_id = %s::uuid AND source_resource_id = %s::uuid)"
            )
            cp_params.extend([workspace_id, source_resource_id])

        cur.execute(
            f"""
            SELECT stream_name, stream_status, last_committed_page,
                   cursor_metadata ->> 'current_cursor', unresolved_failures
            FROM public.sync_checkpoints
            {cp_where}
            ORDER BY stream_name ASC, last_committed_page ASC NULLS FIRST
            """,
            tuple(cp_params),
        )
        sync_checkpoints = [
            {
                "stream_name": str(r[0]),
                "stream_status": str(r[1]),
                "last_committed_page": int(r[2]) if r[2] is not None else None,
                "current_cursor": str(r[3]) if r[3] is not None else None,
                "unresolved_failures": int(r[4]) if r[4] is not None else 0,
            }
            for r in cur.fetchall()
        ]

        # 5. sync_run_page_observations
        obs_where = "WHERE workspace_id = %s::uuid"
        obs_params: list[Any] = [workspace_id]
        if sync_run_id is not None:
            obs_where += " AND sync_run_id = %s::uuid"
            obs_params.append(sync_run_id)
        elif source_resource_id is not None:
            obs_where += (
                " AND sync_run_id IN (SELECT id FROM public.sync_runs "
                "WHERE workspace_id = %s::uuid AND source_resource_id = %s::uuid)"
            )
            obs_params.extend([workspace_id, source_resource_id])

        cur.execute(
            f"""
            SELECT stream_name, page_number, item_count, unique_object_count
            FROM public.sync_run_page_observations
            {obs_where}
            ORDER BY stream_name ASC, page_number ASC
            """,
            tuple(obs_params),
        )
        sync_run_page_observations = [
            {
                "stream_name": str(r[0]),
                "page_number": int(r[1]),
                "item_count": int(r[2]),
                "unique_object_count": int(r[3]),
            }
            for r in cur.fetchall()
        ]

        # 6. sync_item_outcomes
        out_where = "WHERE workspace_id = %s::uuid"
        out_params: list[Any] = [workspace_id]
        if sync_run_id is not None:
            out_where += " AND sync_run_id = %s::uuid"
            out_params.append(sync_run_id)
        elif source_resource_id is not None:
            out_where += " AND source_resource_id = %s::uuid"
            out_params.append(source_resource_id)

        cur.execute(
            f"""
            SELECT stream_name, object_type, provider_object_id,
                   outcome_kind, reason_code, retryable
            FROM public.sync_item_outcomes
            {out_where}
            ORDER BY stream_name ASC, object_type ASC, provider_object_id ASC,
                     outcome_kind ASC, reason_code ASC
            """,
            tuple(out_params),
        )
        sync_item_outcomes = [
            {
                "stream_name": str(r[0]),
                "object_type": str(r[1]),
                "provider_object_id": str(r[2]),
                "outcome_kind": str(r[3]),
                "reason_code": str(r[4]),
                "retryable": bool(r[5]),
            }
            for r in cur.fetchall()
        ]

        # 7. sync_runs
        run_where = "WHERE workspace_id = %s::uuid"
        run_params: list[Any] = [workspace_id]
        if sync_run_id is not None:
            run_where += " AND id = %s::uuid"
            run_params.append(sync_run_id)
        elif source_resource_id is not None:
            run_where += " AND source_resource_id = %s::uuid"
            run_params.append(source_resource_id)

        cur.execute(
            f"""
            SELECT status, processing_status, import_ready, retry_allowed,
                   has_safe_checkpoint, source_drift, failure_reason_code,
                   is_abandoned, (remaining_range IS NOT NULL AND length(trim(remaining_range)) > 0)
            FROM public.sync_runs
            {run_where}
            ORDER BY created_at ASC, status ASC, is_abandoned ASC
            """,
            tuple(run_params),
        )
        sync_runs = [
            {
                "status": str(r[0]),
                "processing_status": str(r[1]),
                "import_ready": bool(r[2]),
                "retry_allowed": bool(r[3]),
                "has_safe_checkpoint": bool(r[4]),
                "source_drift": bool(r[5]),
                "failure_reason_code": str(r[6]) if r[6] is not None else None,
                "is_abandoned": bool(r[7]),
                "remaining_range_present": bool(r[8]),
            }
            for r in cur.fetchall()
        ]

        # 8. outbox_events (IMPORT_READY) & outbox_consumed_effects
        if sync_run_id is not None:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.outbox_events
                WHERE workspace_id = %s::uuid
                  AND event_type = 'IMPORT_READY'
                  AND aggregate_id = %s::uuid
                """,
                (workspace_id, sync_run_id),
            )
            import_ready_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.outbox_consumed_effects oce
                JOIN public.outbox_events oe ON oe.id = oce.outbox_event_id
                WHERE oce.workspace_id = %s::uuid
                  AND oe.event_type = 'IMPORT_READY'
                  AND oe.aggregate_id = %s::uuid
                """,
                (workspace_id, sync_run_id),
            )
            consumed_count = int(cur.fetchone()[0])
        else:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.outbox_events
                WHERE workspace_id = %s::uuid
                  AND event_type = 'IMPORT_READY'
                """,
                (workspace_id,),
            )
            import_ready_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.outbox_consumed_effects
                WHERE workspace_id = %s::uuid
                """,
                (workspace_id,),
            )
            consumed_count = int(cur.fetchone()[0])

        # 9. work_events & event_memberships
        if source_resource_id is not None:
            cur.execute(
                """
                SELECT we.anchor_mode, so.provider_object_id, we.status, we.summary_metadata
                FROM public.work_events we
                JOIN public.source_objects so ON so.id = we.primary_anchor_source_object_id
                WHERE we.workspace_id = %s::uuid
                  AND so.source_resource_id = %s::uuid
                ORDER BY we.anchor_mode ASC, so.provider_object_id ASC
                """,
                (workspace_id, source_resource_id),
            )
        else:
            cur.execute(
                """
                SELECT we.anchor_mode, so.provider_object_id, we.status, we.summary_metadata
                FROM public.work_events we
                JOIN public.source_objects so ON so.id = we.primary_anchor_source_object_id
                WHERE we.workspace_id = %s::uuid
                ORDER BY we.anchor_mode ASC, so.provider_object_id ASC
                """,
                (workspace_id,),
            )
        work_events = []
        for r in cur.fetchall():
            meta = dict(r[3] or {})
            req_confirm = bool(
                meta.get("requires_user_confirmation") or meta.get("user_review_required")
            )
            work_events.append(
                {
                    "anchor_mode": str(r[0]),
                    "primary_provider_object_id": str(r[1]),
                    "status": str(r[2]),
                    "requires_user_confirmation": req_confirm,
                }
            )

        if source_resource_id is not None:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.event_memberships em
                JOIN public.work_events we ON we.id = em.event_id
                JOIN public.source_objects so ON so.id = we.primary_anchor_source_object_id
                WHERE em.workspace_id = %s::uuid
                  AND so.source_resource_id = %s::uuid
                """,
                (workspace_id, source_resource_id),
            )
        else:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.event_memberships
                WHERE workspace_id = %s::uuid
                """,
                (workspace_id,),
            )
        event_memberships_count = int(cur.fetchone()[0])

        # 10. content_units metadata (Plan C raw-null check included)
        if source_resource_id is not None:
            cur.execute(
                """
                SELECT so.object_type, so.provider_object_id, cu.unit_type, cu.ordinal,
                       cu.content_hash, cu.policy_state,
                       (cu.text_content IS NULL AND cu.content_ref IS NULL
                        AND cu.retention_class = 'NONE' AND cu.expires_at IS NULL)
                FROM public.content_units cu
                JOIN public.source_object_revisions rev ON rev.id = cu.source_object_revision_id
                JOIN public.source_objects so ON so.id = rev.source_object_id
                WHERE cu.workspace_id = %s::uuid
                  AND so.source_resource_id = %s::uuid
                ORDER BY so.object_type ASC, so.provider_object_id ASC, cu.ordinal ASC
                """,
                (workspace_id, source_resource_id),
            )
        else:
            cur.execute(
                """
                SELECT so.object_type, so.provider_object_id, cu.unit_type, cu.ordinal,
                       cu.content_hash, cu.policy_state,
                       (cu.text_content IS NULL AND cu.content_ref IS NULL
                        AND cu.retention_class = 'NONE' AND cu.expires_at IS NULL)
                FROM public.content_units cu
                JOIN public.source_object_revisions rev ON rev.id = cu.source_object_revision_id
                JOIN public.source_objects so ON so.id = rev.source_object_id
                WHERE cu.workspace_id = %s::uuid
                ORDER BY so.object_type ASC, so.provider_object_id ASC, cu.ordinal ASC
                """,
                (workspace_id,),
            )
        content_units = [
            {
                "object_type": str(r[0]),
                "provider_object_id": str(r[1]),
                "unit_type": str(r[2]),
                "ordinal": int(r[3]),
                "content_hash": str(r[4]),
                "policy_state": str(r[5]),
                "raw_null_compliant": bool(r[6]),
            }
            for r in cur.fetchall()
        ]

        # 11. source_resources & source_accounts policy + deletion fence
        if source_resource_id is not None:
            cur.execute(
                """
                SELECT sa.status, sr.status, sr.visibility,
                       EXISTS (
                           SELECT 1
                           FROM public.deletion_jobs dj
                           WHERE dj.workspace_id = sr.workspace_id
                             AND dj.source_resource_id = sr.id
                             AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
                       )
                FROM public.source_resources sr
                JOIN public.source_accounts sa ON sa.id = sr.source_account_id
                WHERE sr.workspace_id = %s::uuid
                  AND sr.id = %s::uuid
                """,
                (workspace_id, source_resource_id),
            )
        else:
            cur.execute(
                """
                SELECT sa.status, sr.status, sr.visibility,
                       EXISTS (
                           SELECT 1
                           FROM public.deletion_jobs dj
                           WHERE dj.workspace_id = sr.workspace_id
                             AND dj.source_resource_id = sr.id
                             AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
                       )
                FROM public.source_resources sr
                JOIN public.source_accounts sa ON sa.id = sr.source_account_id
                WHERE sr.workspace_id = %s::uuid
                ORDER BY sr.name ASC
                LIMIT 1
                """,
                (workspace_id,),
            )
        pol_row = cur.fetchone()
        if pol_row is not None:
            policy_state = {
                "account_status": str(pol_row[0]),
                "resource_status": str(pol_row[1]),
                "resource_visibility": str(pol_row[2]),
                "deletion_fence_active": bool(pol_row[3]),
            }
        else:
            policy_state = {
                "account_status": None,
                "resource_status": None,
                "resource_visibility": None,
                "deletion_fence_active": False,
            }

    return LogicalDbSnapshot(
        data={
            "source_objects": source_objects,
            "source_object_revisions": source_object_revisions,
            "sync_commit_pr_associations": sync_commit_pr_associations,
            "sync_checkpoints": sync_checkpoints,
            "sync_run_page_observations": sync_run_page_observations,
            "sync_item_outcomes": sync_item_outcomes,
            "sync_runs": sync_runs,
            "outbox_summary": {
                "import_ready_count": import_ready_count,
                "outbox_consumed_effects_count": consumed_count,
            },
            "work_events": work_events,
            "event_memberships_count": event_memberships_count,
            "content_units": content_units,
            "resource_and_account_policy": policy_state,
            "gate_checks": (gate_checks or GateCheckObservations()).to_dict(),
        }
    )


def verify_b004_invariants(
    admin_conn: psycopg.Connection,
    *,
    workspace_id: str,
    phase: str,
    canary_audit: CanaryAuditResult | None = None,
    gate_checks: GateCheckObservations | None = None,
    duplicate_race_writes: int = 0,
    source_resource_id: str | None = None,
) -> tuple[InvariantViolation, ...]:
    """Verify all 6 global B-004 invariants directly against PostgreSQL 18 at any lifecycle point."""
    violations: list[InvariantViolation] = []

    with admin_conn.cursor() as cur:
        # ---------------------------------------------------------------------
        # INV-1: checkpoint <= committed page (across every run & stream)
        # ---------------------------------------------------------------------
        cur.execute(
            """
            SELECT cp.sync_run_id::text, cp.stream_name, cp.last_committed_page,
                   (
                       SELECT MAX(obs.page_number)
                       FROM public.sync_run_page_observations obs
                       WHERE obs.workspace_id = cp.workspace_id
                         AND obs.sync_run_id = cp.sync_run_id
                         AND obs.stream_name = cp.stream_name
                   ) AS max_obs_page
            FROM public.sync_checkpoints cp
            WHERE cp.workspace_id = %s::uuid
            """,
            (workspace_id,),
        )
        for run_id, stream_name, last_cp_page, max_obs_page in cur.fetchall():
            if last_cp_page is not None:
                if max_obs_page is None or int(last_cp_page) > int(max_obs_page):
                    violations.append(
                        InvariantViolation(
                            invariant_id="INV-1",
                            phase=phase,
                            message=(
                                f"Checkpoint advanced ahead of committed page on stream={stream_name} "
                                f"(run={run_id}): last_committed_page={last_cp_page} > max_obs_page={max_obs_page}"
                            ),
                        )
                    )

        # ---------------------------------------------------------------------
        # INV-2: 0 IMPORT_READY outbox before SUCCESS; <= 1 per SUCCESS run
        # ---------------------------------------------------------------------
        cur.execute(
            """
            SELECT oe.aggregate_id::text, sr.status
            FROM public.outbox_events oe
            LEFT JOIN public.sync_runs sr
              ON sr.workspace_id = oe.workspace_id
             AND sr.id = oe.aggregate_id
            WHERE oe.workspace_id = %s::uuid
              AND oe.event_type = 'IMPORT_READY'
            """,
            (workspace_id,),
        )
        import_ready_rows = cur.fetchall()
        counts_per_run: dict[str, int] = {}
        for agg_id, sr_status in import_ready_rows:
            if sr_status != "SUCCESS":
                violations.append(
                    InvariantViolation(
                        invariant_id="INV-2",
                        phase=phase,
                        message=(
                            f"IMPORT_READY outbox emitted for non-SUCCESS SyncRun "
                            f"(run={agg_id}, status={sr_status})."
                        ),
                    )
                )
            counts_per_run[str(agg_id)] = counts_per_run.get(str(agg_id), 0) + 1
        for agg_id, cnt in counts_per_run.items():
            if cnt > 1:
                violations.append(
                    InvariantViolation(
                        invariant_id="INV-2",
                        phase=phase,
                        message=f"Duplicate IMPORT_READY outbox rows ({cnt}) for SyncRun {agg_id}.",
                    )
                )

        # ---------------------------------------------------------------------
        # INV-3: 0 domain promotions (work_events, content_units, facts, claims) before SUCCESS
        # ---------------------------------------------------------------------
        if source_resource_id is not None:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM public.sync_runs
                    WHERE workspace_id = %s::uuid
                      AND source_resource_id = %s::uuid
                      AND status = 'SUCCESS'
                )
                """,
                (workspace_id, source_resource_id),
            )
        else:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM public.sync_runs
                    WHERE workspace_id = %s::uuid
                      AND status = 'SUCCESS'
                )
                """,
                (workspace_id,),
            )
        has_success_run = bool(cur.fetchone()[0])

        if not has_success_run:
            if source_resource_id is not None:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.work_events we
                    JOIN public.source_objects so ON so.id = we.primary_anchor_source_object_id
                    WHERE we.workspace_id = %s::uuid AND so.source_resource_id = %s::uuid
                    """,
                    (workspace_id, source_resource_id),
                )
                we_cnt = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.content_units cu
                    JOIN public.source_object_revisions rev ON rev.id = cu.source_object_revision_id
                    JOIN public.source_objects so ON so.id = rev.source_object_id
                    WHERE cu.workspace_id = %s::uuid AND so.source_resource_id = %s::uuid
                    """,
                    (workspace_id, source_resource_id),
                )
                cu_cnt = int(cur.fetchone()[0])
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM public.work_events WHERE workspace_id = %s::uuid",
                    (workspace_id,),
                )
                we_cnt = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT COUNT(*) FROM public.content_units WHERE workspace_id = %s::uuid",
                    (workspace_id,),
                )
                cu_cnt = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*) FROM public.facts WHERE workspace_id = %s::uuid",
                (workspace_id,),
            )
            facts_cnt = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*) FROM public.claims WHERE workspace_id = %s::uuid",
                (workspace_id,),
            )
            claims_cnt = int(cur.fetchone()[0])
            if we_cnt or cu_cnt or facts_cnt or claims_cnt:
                violations.append(
                    InvariantViolation(
                        invariant_id="INV-3",
                        phase=phase,
                        message=(
                            "Premature domain promotion before SUCCESS: "
                            f"work_events={we_cnt}, content_units={cu_cnt}, "
                            f"facts={facts_cnt}, claims={claims_cnt}."
                        ),
                    )
                )

        # ---------------------------------------------------------------------
        # INV-4: 0 checkpoint_version race duplicate writes
        # ---------------------------------------------------------------------
        if duplicate_race_writes > 0:
            violations.append(
                InvariantViolation(
                    invariant_id="INV-4",
                    phase=phase,
                    message=f"Detected {duplicate_race_writes} stale concurrent checkpoint write(s).",
                )
            )
        cur.execute(
            """
            SELECT sync_run_id::text, stream_name, page_number, COUNT(*)
            FROM public.sync_run_page_observations
            WHERE workspace_id = %s::uuid
            GROUP BY sync_run_id, stream_name, page_number
            HAVING COUNT(*) > 1
            """,
            (workspace_id,),
        )
        for dup_row in cur.fetchall():
            violations.append(
                InvariantViolation(
                    invariant_id="INV-4",
                    phase=phase,
                    message=f"Duplicate page observation rows detected: {dup_row}.",
                )
            )

        # ---------------------------------------------------------------------
        # INV-5: DIRECT_COMMIT integrity & requires_user_confirmation=True
        # ---------------------------------------------------------------------
        cur.execute(
            """
            SELECT we.id::text, so.provider_object_id, we.summary_metadata,
                   assoc.pr_association_state, assoc.matched_pr_in_window, assoc.match_mechanism
            FROM public.work_events we
            JOIN public.source_objects so ON so.id = we.primary_anchor_source_object_id
            LEFT JOIN public.sync_commit_pr_associations assoc
              ON assoc.workspace_id = we.workspace_id
             AND assoc.commit_source_object_id = so.id
            WHERE we.workspace_id = %s::uuid
              AND we.anchor_mode = 'DIRECT_COMMIT'
            """,
            (workspace_id,),
        )
        for we_id, commit_pid, summary_meta_raw, assoc_state, _in_win, mech in cur.fetchall():
            meta = dict(summary_meta_raw or {})
            if assoc_state != "NO_MATCH_OBSERVED":
                violations.append(
                    InvariantViolation(
                        invariant_id="INV-5",
                        phase=phase,
                        message=(
                            f"DIRECT_COMMIT work_event {we_id} (commit={commit_pid}) has illegal "
                            f"pr_association_state={assoc_state!r} (mechanism={mech!r})."
                        ),
                    )
                )
            if meta.get("requires_user_confirmation") is not True:
                violations.append(
                    InvariantViolation(
                        invariant_id="INV-5",
                        phase=phase,
                        message=(
                            f"DIRECT_COMMIT work_event {we_id} (commit={commit_pid}) missing "
                            "requires_user_confirmation=True."
                        ),
                    )
                )

        # ---------------------------------------------------------------------
        # INV-6: 0 unauthorized raw/secret exposures (REF-04/06 & policy gates)
        # ---------------------------------------------------------------------
        if canary_audit is not None and not canary_audit.passed:
            violations.append(
                InvariantViolation(
                    invariant_id="INV-6",
                    phase=phase,
                    message=(
                        f"Canary/Plan-C audit failed: canary_hits={canary_audit.canary_hits}, "
                        f"plan_c_compliant={canary_audit.content_units_plan_c_compliant}, "
                        f"locations={canary_audit.leak_locations}."
                    ),
                )
            )

        if gate_checks is not None:
            # If resource is blocked / private / deleted / revoked / BLOCKED_POLICY / BLOCKED_SOURCE,
            # no raw content, external AI, or Export gate may be allowed!
            if source_resource_id is not None:
                cur.execute(
                    """
                    SELECT sa.status, sr.status, sr.visibility,
                           EXISTS (
                               SELECT 1
                               FROM public.deletion_jobs dj
                               WHERE dj.workspace_id = sr.workspace_id
                                 AND dj.source_resource_id = sr.id
                                 AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
                           ),
                           EXISTS (
                               SELECT 1
                               FROM public.sync_runs run
                               WHERE run.workspace_id = sr.workspace_id
                                 AND run.source_resource_id = sr.id
                                 AND run.processing_status IN ('BLOCKED_POLICY', 'BLOCKED_SOURCE')
                           )
                    FROM public.source_resources sr
                    JOIN public.source_accounts sa ON sa.id = sr.source_account_id
                    WHERE sr.workspace_id = %s::uuid
                      AND sr.id = %s::uuid
                    """,
                    (workspace_id, source_resource_id),
                )
                prow = cur.fetchone()
                if prow is not None:
                    acct_st, res_st, res_vis, del_fence, proc_blocked = prow
                    is_policy_blocked = (
                        str(acct_st) != "ACTIVE"
                        or str(res_st) != "ACTIVE"
                        or str(res_vis) != "PUBLIC"
                        or bool(del_fence)
                    )
                    if is_policy_blocked and (
                        gate_checks.refetch_content_allowed is True
                        or gate_checks.external_ai_gate_allowed is True
                        or gate_checks.export_gate_allowed is True
                    ):
                        violations.append(
                            InvariantViolation(
                                invariant_id="INV-6",
                                phase=phase,
                                message=(
                                    "Gate allowed raw/AI/Export exposure despite blocked resource policy "
                                    f"(acct={acct_st}, res={res_st}, vis={res_vis}, del_fence={del_fence})."
                                ),
                            )
                        )
                    if bool(proc_blocked) and (
                        gate_checks.export_gate_allowed is True
                        or gate_checks.claim_approval_gate_allowed is True
                    ):
                        violations.append(
                            InvariantViolation(
                                invariant_id="INV-6",
                                phase=phase,
                                message="Approval or Export gate allowed despite BLOCKED_POLICY/BLOCKED_SOURCE.",
                            )
                        )

    return tuple(violations)


__all__ = [
    "GateCheckObservations",
    "InvariantViolation",
    "LogicalDbSnapshot",
    "canonical_json_sha256",
    "extract_logical_db_snapshot",
    "verify_b004_invariants",
]
