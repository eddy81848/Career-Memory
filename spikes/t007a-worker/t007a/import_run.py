"""T-007B initial-import stream/page/checkpoint prototype (single repository, canned fixtures).

Validates on real PostgreSQL (spike-only, NOT product code or migration):
1. Atomic page items + next checkpoint persistence in one short DB transaction.
   Pre-commit failure rolls back both; post-commit response loss + page replay never
   duplicates items or double-advances ``checkpoint_version`` / ``cursor``.
2. Multi-layer fencing against a stalled/zombie worker's late page write:
   - Active lease fence (``jobs.status = 'RUNNING' AND lease_token = %s AND lease_expires_at > now()``):
     distinguishes ``LEASE_EXPIRED_UNCLAIMED`` (expired, not yet taken over) from
     ``LEASE_TAKEN_OVER`` (Worker B already claimed a new lease token).
   - Stream ``checkpoint_version`` compare-and-swap (``checkpoint_version = expected``):
     rejects stale writes with ``STALE_CHECKPOINT_VERSION`` even if a caller presents
     an outdated cursor snapshot.
   Any fence rejection raises inside ``conn.transaction()`` before items are committed,
   so the late write is rejected wholesale (0 items, 0 cursor advance).
3. Safe resume & finalization:
   - Normal run and interrupted + safely-resumed run on the same fixed plan converge to
     identical logical DB state.
   - Unsafe checkpoint (``safe_checkpoint = false``) or plan drift
     (``plan_fingerprint`` mismatch) blocks automatic resume of the same run.
   - ``IMPORT_READY`` is 0 while any required stream is incomplete, and exactly 1 after
     all required streams complete — even under concurrent completion races.
"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from .db import connect
from .faults import NO_FAULTS, FaultPlan
from .worker import Claim

APP_NAME = "t007a-worker"


@dataclass(frozen=True)
class SyntheticPage:
    page_cursor: str  # cursor used to request this page ("START" for first page)
    items: tuple[dict, ...]
    next_cursor: str | None
    has_more: bool
    safe_checkpoint: bool = True


@dataclass(frozen=True)
class SyntheticRepoPlan:
    repo_ref: str
    plan_id: str
    required_streams: tuple[str, ...]
    pages_by_stream: dict[str, tuple[SyntheticPage, ...]]

    @property
    def fingerprint(self) -> str:
        canonical = {
            "repo_ref": self.repo_ref,
            "plan_id": self.plan_id,
            "required_streams": list(self.required_streams),
            "stream_heads": {
                s: [p.page_cursor for p in pages]
                for s, pages in sorted(self.pages_by_stream.items())
            },
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def fetch_page(self, stream_type: str, cursor: str | None) -> SyntheticPage:
        """Deterministic in-memory page lookup. Runs OUTSIDE any DB transaction."""
        want = cursor or "START"
        for page in self.pages_by_stream[stream_type]:
            if page.page_cursor == want:
                return page
        raise KeyError(f"no synthetic page for stream={stream_type} cursor={want}")


def default_repo_plan(
    *,
    plan_id: str = "plan-repo-alpha-v1",
    commits_head: str = "START",
) -> SyntheticRepoPlan:
    """Fixed two-stream ('COMMITS', 'PULL_REQUESTS') synthetic repository fixture."""
    return SyntheticRepoPlan(
        repo_ref="github:synthetic-org/career-repo",
        plan_id=plan_id,
        required_streams=("COMMITS", "PULL_REQUESTS"),
        pages_by_stream={
            "COMMITS": (
                SyntheticPage(
                    page_cursor=commits_head,
                    items=(
                        {"external_id": "c-101", "sha": "aaa101", "title": "init architecture"},
                        {"external_id": "c-102", "sha": "aaa102", "title": "add worker queue"},
                    ),
                    next_cursor="cur-commits-p2",
                    has_more=True,
                ),
                SyntheticPage(
                    page_cursor="cur-commits-p2",
                    items=(
                        {"external_id": "c-103", "sha": "aaa103", "title": "fenced checkpoints"},
                    ),
                    next_cursor="cur-commits-end",
                    has_more=False,
                ),
            ),
            "PULL_REQUESTS": (
                SyntheticPage(
                    page_cursor="START",
                    items=(
                        {"external_id": "pr-1", "number": 1, "title": "Initial import spike"},
                        {"external_id": "pr-2", "number": 2, "title": "Lease recovery"},
                    ),
                    next_cursor="cur-prs-p2",
                    has_more=True,
                ),
                SyntheticPage(
                    page_cursor="cur-prs-p2",
                    items=(
                        {"external_id": "pr-3", "number": 3, "title": "Stream resume"},
                    ),
                    next_cursor="cur-prs-end",
                    has_more=False,
                ),
            ),
        },
    )


class FenceRejectedError(RuntimeError):
    """Raised when a late/stale page or finalization write fails fencing."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ResumeBlockedError(RuntimeError):
    """Raised when automatic resume of an existing run is unsafe or drifted."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class StreamState:
    stream_type: str
    status: str
    cursor: str | None
    safe_checkpoint: bool
    checkpoint_version: int
    pages_committed: int
    items_committed: int


def create_import_run(dsn: str, plan: SyntheticRepoPlan, *, idempotency_key: str) -> tuple[UUID, UUID]:
    """Create linked job + outbox + import_run + initial stream checkpoints in one short TX."""
    job_id = uuid4()
    run_id = uuid4()
    payload = {"kind": "initial_import", "repo_ref": plan.repo_ref, "plan_id": plan.plan_id}
    req_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with connect(dsn, "t007a-api") as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO jobs (id, idempotency_key, request_hash, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (job_id, idempotency_key, req_hash, json.dumps(payload)),
        )
        conn.execute("INSERT INTO outbox (job_id) VALUES (%s)", (job_id,))
        conn.execute(
            """
            INSERT INTO import_runs
                (run_id, job_id, repo_ref, plan_id, plan_fingerprint, required_streams)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                job_id,
                plan.repo_ref,
                plan.plan_id,
                plan.fingerprint,
                list(plan.required_streams),
            ),
        )
        for stream_type in plan.required_streams:
            conn.execute(
                """
                INSERT INTO import_stream_checkpoints (run_id, stream_type)
                VALUES (%s, %s)
                """,
                (run_id, stream_type),
            )
    return run_id, job_id


def read_stream_state(dsn: str, run_id: UUID, stream_type: str) -> StreamState:
    with connect(dsn, APP_NAME) as conn:
        row = conn.execute(
            """
            SELECT stream_type, status, cursor, safe_checkpoint, checkpoint_version,
                   pages_committed, items_committed
              FROM import_stream_checkpoints
             WHERE run_id = %s AND stream_type = %s
            """,
            (run_id, stream_type),
        ).fetchone()
    if row is None:
        raise KeyError((run_id, stream_type))
    return StreamState(**row)


def _verify_active_lease(conn, claim: Claim) -> None:
    """Check job lease inside the write TX and classify the exact rejection reason."""
    job = conn.execute(
        """
        SELECT status, lease_token, (lease_expires_at > now()) AS lease_alive
          FROM jobs
         WHERE id = %s
           FOR UPDATE
        """,
        (claim.job_id,),
    ).fetchone()
    if job is None:
        raise FenceRejectedError("JOB_NOT_FOUND")
    if job["status"] != "RUNNING":
        raise FenceRejectedError(f"JOB_NOT_RUNNING:{job['status']}")
    if job["lease_token"] != claim.lease_token:
        raise FenceRejectedError("LEASE_TAKEN_OVER")
    if not job["lease_alive"]:
        raise FenceRejectedError("LEASE_EXPIRED_UNCLAIMED")


def commit_page_and_checkpoint(
    dsn: str,
    *,
    run_id: UUID,
    claim: Claim,
    worker_id: str,
    stream_type: str,
    expected_version: int,
    page: SyntheticPage,
    enforce_lease_fence: bool = True,
    faults: FaultPlan = NO_FAULTS,
) -> StreamState:
    """Persist page items + advance stream checkpoint in ONE short DB transaction.

    ``enforce_lease_fence=False`` is ONLY for the pre-fix baseline test (or isolated
    checkpoint-version CAS testing when two callers were contrived to bypass lease checks).
    """
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        if enforce_lease_fence:
            _verify_active_lease(conn, claim)

        cp = conn.execute(
            """
            SELECT status, cursor, safe_checkpoint, checkpoint_version,
                   pages_committed, items_committed
              FROM import_stream_checkpoints
             WHERE run_id = %s AND stream_type = %s
               FOR UPDATE
            """,
            (run_id, stream_type),
        ).fetchone()
        if cp is None:
            raise FenceRejectedError("STREAM_NOT_FOUND")

        if cp["checkpoint_version"] != expected_version:
            raise FenceRejectedError("STALE_CHECKPOINT_VERSION")

        next_version = expected_version + 1
        inserted = 0
        for item in page.items:
            row = conn.execute(
                """
                INSERT INTO import_page_items
                    (run_id, stream_type, external_id, page_cursor, checkpoint_version,
                     attempt_no, worker_id, body)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_id, stream_type, external_id) DO NOTHING
                RETURNING external_id
                """,
                (
                    run_id,
                    stream_type,
                    item["external_id"],
                    page.page_cursor,
                    next_version,
                    claim.attempt_no,
                    worker_id,
                    json.dumps(item, sort_keys=True),
                ),
            ).fetchone()
            if row is not None:
                inserted += 1

        faults.maybe_raise("import.between_items_and_checkpoint")

        new_status = "RUNNING" if page.has_more else "COMPLETED"
        updated = conn.execute(
            """
            UPDATE import_stream_checkpoints
               SET status = %s,
                   cursor = %s,
                   safe_checkpoint = %s,
                   checkpoint_version = %s,
                   pages_committed = pages_committed + 1,
                   items_committed = items_committed + %s,
                   updated_by_attempt = %s,
                   updated_by_worker = %s,
                   updated_at = now()
             WHERE run_id = %s
               AND stream_type = %s
               AND checkpoint_version = %s
         RETURNING stream_type, status, cursor, safe_checkpoint, checkpoint_version,
                   pages_committed, items_committed
            """,
            (
                new_status,
                page.next_cursor,
                page.safe_checkpoint,
                next_version,
                inserted,
                claim.attempt_no,
                worker_id,
                run_id,
                stream_type,
                expected_version,
            ),
        ).fetchone()
        assert updated is not None

        faults.maybe_raise("import.before_page_commit")

    faults.maybe_raise("import.after_page_commit")
    return StreamState(**updated)


def mark_interrupted_partial(
    dsn: str,
    *,
    run_id: UUID,
    claim: Claim,
    failure_reason: str,
    safe_checkpoint: bool | None = None,
    stream_type: str | None = None,
) -> None:
    """Record a partial interruption on the run (e.g. transient provider stop mid-run)."""
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        _verify_active_lease(conn, claim)
        if stream_type is not None and safe_checkpoint is not None:
            conn.execute(
                """
                UPDATE import_stream_checkpoints
                   SET safe_checkpoint = %s, updated_at = now()
                 WHERE run_id = %s AND stream_type = %s
                """,
                (safe_checkpoint, run_id, stream_type),
            )
        conn.execute(
            """
            UPDATE import_runs
               SET status = 'PARTIAL', failure_reason = %s, updated_at = now()
             WHERE run_id = %s
            """,
            (failure_reason, run_id),
        )


def prepare_safe_resume(dsn: str, *, run_id: UUID, plan: SyntheticRepoPlan) -> list[StreamState]:
    """Gate automatic resume on plan fingerprint match + safe checkpoints.

    Blocks (does NOT resume the same run) if:
    - ``plan.fingerprint != import_runs.plan_fingerprint`` -> marks ``source_drift = true``,
      ``failure_reason = 'PLAN_DRIFT'``, raises ``ResumeBlockedError('PLAN_DRIFT')``.
    - Any partially progressed stream (``checkpoint_version > 0`` or ``cursor IS NOT NULL``)
      has ``safe_checkpoint = false`` -> raises ``ResumeBlockedError('UNSAFE_CHECKPOINT')``.
    """
    block_reason: str | None = None
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        run = conn.execute(
            """
            SELECT run_id, job_id, plan_fingerprint, status
              FROM import_runs
             WHERE run_id = %s
               FOR UPDATE
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise ResumeBlockedError("RUN_NOT_FOUND")
        if run["status"] == "SUCCEEDED":
            raise ResumeBlockedError("ALREADY_SUCCEEDED")

        streams = conn.execute(
            """
            SELECT stream_type, status, cursor, safe_checkpoint, checkpoint_version,
                   pages_committed, items_committed
              FROM import_stream_checkpoints
             WHERE run_id = %s
             ORDER BY stream_type
               FOR UPDATE
            """,
            (run_id,),
        ).fetchall()

        if run["plan_fingerprint"] != plan.fingerprint:
            conn.execute(
                """
                UPDATE import_runs
                   SET status = 'PARTIAL',
                       source_drift = true,
                       failure_reason = 'PLAN_DRIFT',
                       updated_at = now()
                 WHERE run_id = %s
                """,
                (run_id,),
            )
            block_reason = "PLAN_DRIFT"
        else:
            for s in streams:
                progressed = s["checkpoint_version"] > 0 or s["cursor"] is not None
                if progressed and not s["safe_checkpoint"]:
                    conn.execute(
                        """
                        UPDATE import_runs
                           SET status = 'PARTIAL',
                               failure_reason = 'UNSAFE_CHECKPOINT',
                               updated_at = now()
                         WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                    block_reason = "UNSAFE_CHECKPOINT"
                    break

        if block_reason is None:
            conn.execute(
                """
                UPDATE import_runs
                   SET status = 'RUNNING', failure_reason = NULL, updated_at = now()
                 WHERE run_id = %s
                """,
                (run_id,),
            )
            # Re-open job to PENDING if previous worker failed/released so a new attempt can claim.
            conn.execute(
                """
                UPDATE jobs
                   SET status = 'PENDING',
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       updated_at = now()
                 WHERE id = %s AND status IN ('PENDING', 'RUNNING')
                """,
                (run["job_id"],),
            )
    if block_reason is not None:
        raise ResumeBlockedError(block_reason)
    return [StreamState(**s) for s in streams]


def try_finalize_import_run(
    dsn: str,
    *,
    run_id: UUID,
    claim: Claim | None,
    worker_id: str,
    attempt_no: int,
    enforce_lease_fence: bool = True,
) -> dict:
    """Emit at most one IMPORT_READY row iff ALL required streams are COMPLETED.

    Safe under concurrent completion races: locks ``import_runs`` row, checks every
    ``required_streams`` entry in ``import_stream_checkpoints``, and inserts into
    ``import_ready_outbox`` with ``ON CONFLICT (run_id) DO NOTHING``.
    """
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        if enforce_lease_fence:
            if claim is None:
                raise ValueError("claim is required when enforce_lease_fence=True")
            _verify_active_lease(conn, claim)

        run = conn.execute(
            """
            SELECT run_id, job_id, plan_id, required_streams, status
              FROM import_runs
             WHERE run_id = %s
               FOR UPDATE
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise FenceRejectedError("RUN_NOT_FOUND")

        cps = conn.execute(
            """
            SELECT stream_type, status
              FROM import_stream_checkpoints
             WHERE run_id = %s
            """,
            (run_id,),
        ).fetchall()
        status_by_stream = {r["stream_type"]: r["status"] for r in cps}
        incomplete = [
            s for s in run["required_streams"] if status_by_stream.get(s) != "COMPLETED"
        ]
        if incomplete:
            return {"finalized": False, "emitted_import_ready": False, "incomplete": incomplete}

        total_items = conn.execute(
            "SELECT count(*) AS n FROM import_page_items WHERE run_id = %s",
            (run_id,),
        ).fetchone()["n"]

        emitted = conn.execute(
            """
            INSERT INTO import_ready_outbox
                (run_id, plan_id, attempt_no, worker_id, total_items)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            RETURNING run_id
            """,
            (run_id, run["plan_id"], attempt_no, worker_id, total_items),
        ).fetchone()

        if emitted is not None:
            conn.execute(
                """
                UPDATE import_runs
                   SET status = 'SUCCEEDED',
                       failure_reason = NULL,
                       finished_at = now(),
                       updated_at = now()
                 WHERE run_id = %s
                """,
                (run_id,),
            )
            if claim is not None:
                conn.execute(
                    """
                    UPDATE jobs
                       SET status = 'SUCCEEDED', finished_at = now(), updated_at = now(),
                           lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                     WHERE id = %s AND lease_token = %s
                    """,
                    (claim.job_id, claim.lease_token),
                )
                conn.execute(
                    """
                    INSERT INTO results (job_id, attempt_no, worker_id, output)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (job_id) DO NOTHING
                    """,
                    (
                        claim.job_id,
                        attempt_no,
                        worker_id,
                        json.dumps({"run_id": str(run_id), "total_items": total_items}),
                    ),
                )
                conn.execute(
                    "UPDATE outbox SET status = 'DONE', done_at = now(), last_error = NULL WHERE job_id = %s",
                    (claim.job_id,),
                )
                conn.execute(
                    """
                    UPDATE job_attempts
                       SET outcome = 'SUCCEEDED', finished_at = COALESCE(finished_at, now())
                     WHERE job_id = %s AND attempt_no = %s AND lease_token = %s
                    """,
                    (claim.job_id, claim.attempt_no, claim.lease_token),
                )

        return {
            "finalized": True,
            "emitted_import_ready": emitted is not None,
            "incomplete": [],
            "total_items": total_items,
        }


def run_stream_to_completion(
    dsn: str,
    *,
    run_id: UUID,
    claim: Claim,
    worker_id: str,
    plan: SyntheticRepoPlan,
    stream_type: str,
    max_pages: int | None = None,
    faults: FaultPlan = NO_FAULTS,
) -> StreamState:
    """Fetch pages outside DB TX from current checkpoint and commit each page atomically."""
    state = read_stream_state(dsn, run_id, stream_type)
    pages_done = 0
    while state.status != "COMPLETED":
        if max_pages is not None and pages_done >= max_pages:
            break
        # Read synthetic provider page OUTSIDE any DB transaction.
        page = plan.fetch_page(stream_type, state.cursor)
        state = commit_page_and_checkpoint(
            dsn,
            run_id=run_id,
            claim=claim,
            worker_id=worker_id,
            stream_type=stream_type,
            expected_version=state.checkpoint_version,
            page=page,
            faults=faults,
        )
        pages_done += 1
    return state


def import_snapshot(dsn: str, run_id: UUID) -> dict:
    """Logical + operational DB snapshot for a single import run."""
    with connect(dsn, "t007a-observer") as conn:
        run = conn.execute(
            """
            SELECT status, plan_id, source_drift, failure_reason
              FROM import_runs WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()
        streams = conn.execute(
            """
            SELECT stream_type, status, cursor, safe_checkpoint, checkpoint_version,
                   pages_committed, items_committed, updated_by_attempt, updated_by_worker
              FROM import_stream_checkpoints
             WHERE run_id = %s
             ORDER BY stream_type
            """,
            (run_id,),
        ).fetchall()
        items = conn.execute(
            """
            SELECT stream_type, external_id, page_cursor, checkpoint_version,
                   attempt_no, worker_id, body
              FROM import_page_items
             WHERE run_id = %s
             ORDER BY stream_type, external_id
            """,
            (run_id,),
        ).fetchall()
        ready = conn.execute(
            """
            SELECT intent_type, plan_id, attempt_no, worker_id, total_items
              FROM import_ready_outbox
             WHERE run_id = %s
            """,
            (run_id,),
        ).fetchall()
    logical = {
        "run_status": run["status"],
        "plan_id": run["plan_id"],
        "source_drift": run["source_drift"],
        "streams": [
            {
                "stream_type": s["stream_type"],
                "status": s["status"],
                "cursor": s["cursor"],
                "safe_checkpoint": s["safe_checkpoint"],
                "checkpoint_version": s["checkpoint_version"],
                "pages_committed": s["pages_committed"],
                "items_committed": s["items_committed"],
            }
            for s in streams
        ],
        "items": [
            {
                "stream_type": i["stream_type"],
                "external_id": i["external_id"],
                "page_cursor": i["page_cursor"],
                "body": i["body"],
            }
            for i in items
        ],
        "import_ready_count": len(ready),
        "import_ready_total_items": [r["total_items"] for r in ready],
    }
    return {
        "run": run,
        "streams": streams,
        "item_count": len(items),
        "item_writers": [
            {
                "external_id": i["external_id"],
                "attempt_no": i["attempt_no"],
                "worker_id": i["worker_id"],
                "checkpoint_version": i["checkpoint_version"],
            }
            for i in items
        ],
        "import_ready": ready,
        "logical": logical,
    }
