"""T-007B initial-import page+checkpoint atomicity, fencing, safe resume, and IMPORT_READY tests.

Scope: single repository initial import against REAL local PostgreSQL using fixed
in-memory synthetic pages (NO GitHub / LLM / hosted network calls).
"""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from uuid import UUID

import pytest

from t007a.db import connect
from t007a.faults import FaultPlan, InjectedFault
from t007a.import_run import (
    FenceRejectedError,
    ResumeBlockedError,
    SyntheticPage,
    commit_page_and_checkpoint,
    create_import_run,
    default_repo_plan,
    import_snapshot,
    mark_interrupted_partial,
    prepare_safe_resume,
    read_stream_state,
    run_stream_to_completion,
    try_finalize_import_run,
)
from t007a.worker import Worker

SPIKE_DIR = Path(__file__).resolve().parents[1]


def _force_expire_lease(dsn: str, job_id: UUID) -> None:
    """Deterministic lease expiry via DB clock offset (no sleep-timing race)."""
    with connect(dsn, "t007a-observer") as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '5 seconds' WHERE id = %s",
            (job_id,),
        )


# =======================================================================================
# 1. Atomic page persistence + checkpoint advancement
# =======================================================================================
@pytest.mark.parametrize(
    "fault_point",
    ["import.between_items_and_checkpoint", "import.before_page_commit"],
)
def test_page_and_checkpoint_rollback_together_on_precommit_fault(db, evidence, fault_point):
    """In-code fault injection before commit rolls back BOTH page items and checkpoint."""
    plan = default_repo_plan()
    run_id, job_id = create_import_run(db, plan, idempotency_key=f"t007b-rb-{fault_point}")
    worker = Worker(db, "w-rb")
    claim = worker.claim(job_id)
    assert claim is not None

    page1 = plan.fetch_page("COMMITS", None)
    with pytest.raises(InjectedFault):
        commit_page_and_checkpoint(
            db,
            run_id=run_id,
            claim=claim,
            worker_id="w-rb",
            stream_type="COMMITS",
            expected_version=0,
            page=page1,
            faults=FaultPlan({fault_point: 1}),
        )

    after_fault = evidence("after_precommit_fault", import_snapshot(db, run_id))
    cp = read_stream_state(db, run_id, "COMMITS")
    assert after_fault["item_count"] == 0
    assert cp.checkpoint_version == 0 and cp.cursor is None and cp.pages_committed == 0

    # Subsequent clean execution commits page 1 cleanly once.
    committed = commit_page_and_checkpoint(
        db,
        run_id=run_id,
        claim=claim,
        worker_id="w-rb",
        stream_type="COMMITS",
        expected_version=0,
        page=page1,
    )
    after_retry = evidence("after_clean_retry", import_snapshot(db, run_id))
    assert committed.checkpoint_version == 1
    assert committed.cursor == "cur-commits-p2"
    assert after_retry["item_count"] == 2


def test_post_commit_ack_loss_and_page_replay_neither_duplicates_nor_double_advances(
    db, evidence
):
    """Fault after TX commit loses caller's ack: re-running from DB checkpoint advances to
    page 2, while a stale replay of page 1 (expected_version=0) is rejected without
    duplicating items or advancing checkpoint_version twice."""
    plan = default_repo_plan()
    run_id, job_id = create_import_run(db, plan, idempotency_key="t007b-postcommit-01")
    worker = Worker(db, "w-ack")
    claim = worker.claim(job_id)
    assert claim is not None

    page1 = plan.fetch_page("COMMITS", None)
    with pytest.raises(InjectedFault):
        commit_page_and_checkpoint(
            db,
            run_id=run_id,
            claim=claim,
            worker_id="w-ack",
            stream_type="COMMITS",
            expected_version=0,
            page=page1,
            faults=FaultPlan({"import.after_page_commit": 1}),
        )

    snap_after_ack_loss = evidence("after_postcommit_ack_loss", import_snapshot(db, run_id))
    cp1 = read_stream_state(db, run_id, "COMMITS")
    assert snap_after_ack_loss["item_count"] == 2
    assert cp1.checkpoint_version == 1 and cp1.cursor == "cur-commits-p2"

    # Case A: Blind in-memory replay of page 1 with expected_version=0 -> rejected wholesale.
    with pytest.raises(FenceRejectedError) as exc_info:
        commit_page_and_checkpoint(
            db,
            run_id=run_id,
            claim=claim,
            worker_id="w-ack",
            stream_type="COMMITS",
            expected_version=0,
            page=page1,
        )
    assert exc_info.value.reason == "STALE_CHECKPOINT_VERSION"
    snap_after_stale_replay = evidence("after_stale_page1_replay", import_snapshot(db, run_id))
    assert snap_after_stale_replay["item_count"] == 2
    assert read_stream_state(db, run_id, "COMMITS").checkpoint_version == 1

    # Case B: Even if an upstream bug returned overlapping item c-102 on page 2,
    # PK(run_id, stream_type, external_id) prevents item duplication and advances once.
    overlapping_page2 = SyntheticPage(
        page_cursor="cur-commits-p2",
        items=(
            {"external_id": "c-102", "sha": "aaa102", "title": "duplicate from provider"},
            {"external_id": "c-103", "sha": "aaa103", "title": "fenced checkpoints"},
        ),
        next_cursor="cur-commits-end",
        has_more=False,
    )
    cp2 = commit_page_and_checkpoint(
        db,
        run_id=run_id,
        claim=claim,
        worker_id="w-ack",
        stream_type="COMMITS",
        expected_version=cp1.checkpoint_version,
        page=overlapping_page2,
    )
    snap_final = evidence("final", import_snapshot(db, run_id))
    assert cp2.checkpoint_version == 2
    assert cp2.items_committed == 3  # 2 from page1 + 1 newly inserted from page2
    assert snap_final["item_count"] == 3


# =======================================================================================
# 2. Deterministic control of stalled Worker A vs takeover Worker B (fencing matrix)
# =======================================================================================
def test_late_worker_write_rejected_both_before_and_after_takeover_deterministic(db, evidence):
    """Deterministically controls Worker A and Worker B with threading.Event barriers
    (no sleep timing):
    Phase 1: A claims attempt 1, reads page 1 outside TX, and pauses at barrier.
    Phase 2: A's lease expires, NOT YET taken over -> A's write is rejected with
             LEASE_EXPIRED_UNCLAIMED (0 items, checkpoint_version stays 0).
    Phase 3: Worker B claims attempt 2, commits page 1 (version 1) and page 2 (version 2).
    Phase 4: Worker A wakes up and attempts to commit its stale page 1 (or any page):
             - With lease fence enabled -> rejected with LEASE_TAKEN_OVER.
             - With lease check bypassed to isolate checkpoint CAS -> rejected with
               STALE_CHECKPOINT_VERSION, including any rogue extra items on A's page.
    """
    plan = default_repo_plan()
    run_id, job_id = create_import_run(db, plan, idempotency_key="t007b-fence-order-01")

    a_read_page = threading.Event()
    allow_a_unclaimed_attempt = threading.Event()
    a_unclaimed_done = threading.Event()
    allow_a_post_takeover_attempt = threading.Event()
    outcomes: dict[str, str] = {}

    worker_a = Worker(db, "worker-A")
    worker_b = Worker(db, "worker-B")

    def run_worker_a():
        claim_a = worker_a.claim(job_id)
        assert claim_a is not None and claim_a.attempt_no == 1
        # A reads synthetic page 1 with a rogue extra item so any partial leak is visible.
        stale_page_from_a = SyntheticPage(
            page_cursor="START",
            items=(
                *plan.fetch_page("COMMITS", None).items,
                {"external_id": "c-rogue-from-A", "sha": "bad999", "title": "must never commit"},
            ),
            next_cursor="cur-from-stale-A",
            has_more=True,
        )
        a_read_page.set()

        # Step 2a: Lease expired, B has NOT claimed yet.
        assert allow_a_unclaimed_attempt.wait(5)
        try:
            commit_page_and_checkpoint(
                db,
                run_id=run_id,
                claim=claim_a,
                worker_id="worker-A",
                stream_type="COMMITS",
                expected_version=0,
                page=stale_page_from_a,
            )
        except FenceRejectedError as exc:
            outcomes["expired_unclaimed"] = exc.reason
        a_unclaimed_done.set()

        # Step 2b: B has now taken over and committed pages 1..2.
        assert allow_a_post_takeover_attempt.wait(5)
        try:
            commit_page_and_checkpoint(
                db,
                run_id=run_id,
                claim=claim_a,
                worker_id="worker-A",
                stream_type="COMMITS",
                expected_version=0,
                page=stale_page_from_a,
            )
        except FenceRejectedError as exc:
            outcomes["after_takeover_lease_fence"] = exc.reason

        # Step 2c: Isolate checkpoint_version CAS alone (even if lease check were skipped).
        try:
            commit_page_and_checkpoint(
                db,
                run_id=run_id,
                claim=claim_a,
                worker_id="worker-A",
                stream_type="COMMITS",
                expected_version=0,
                page=stale_page_from_a,
                enforce_lease_fence=False,
            )
        except FenceRejectedError as exc:
            outcomes["after_takeover_version_fence"] = exc.reason

    thread_a = threading.Thread(target=run_worker_a)
    thread_a.start()
    assert a_read_page.wait(5)

    # Expire A's lease deterministically while B has NOT taken over yet.
    _force_expire_lease(db, job_id)
    allow_a_unclaimed_attempt.set()
    assert a_unclaimed_done.wait(5)

    snap_unclaimed = evidence("after_expired_unclaimed_write_attempt", import_snapshot(db, run_id))
    assert outcomes["expired_unclaimed"] == "LEASE_EXPIRED_UNCLAIMED"
    assert snap_unclaimed["item_count"] == 0
    assert read_stream_state(db, run_id, "COMMITS").checkpoint_version == 0

    # Worker B now takes over (attempt 2) and commits COMMITS to completion.
    claim_b = worker_b.claim(job_id)
    assert claim_b is not None and claim_b.attempt_no == 2
    state_b = run_stream_to_completion(
        db,
        run_id=run_id,
        claim=claim_b,
        worker_id="worker-B",
        plan=plan,
        stream_type="COMMITS",
    )
    assert state_b.status == "COMPLETED" and state_b.checkpoint_version == 2
    snap_after_b = evidence("after_worker_b_commits", import_snapshot(db, run_id))

    # Release Worker A to attempt its late writes after B's takeover.
    allow_a_post_takeover_attempt.set()
    thread_a.join(5)

    evidence("rejection_reasons", outcomes)
    snap_final = evidence("after_worker_a_late_attempts", import_snapshot(db, run_id))

    assert outcomes == {
        "expired_unclaimed": "LEASE_EXPIRED_UNCLAIMED",
        "after_takeover_lease_fence": "LEASE_TAKEN_OVER",
        "after_takeover_version_fence": "STALE_CHECKPOINT_VERSION",
    }
    # Wholesale rejection: rogue item c-rogue-from-A was rolled back with the TX,
    # cursor remained cur-commits-end, and every stored item belongs to worker-B attempt 2.
    assert snap_final["logical"] == snap_after_b["logical"]
    assert snap_final["item_count"] == 3
    assert all(w["worker_id"] == "worker-B" and w["attempt_no"] == 2 for w in snap_final["item_writers"])


def test_real_subprocess_sigkill_mid_import_then_safe_takeover_converges(db, evidence):
    """REAL OS process kill (SIGKILL, distinct from in-code fault injection):
    Child worker process commits page 1 of COMMITS, signals readiness, and is killed
    with SIGKILL before page 2. Parent expires lease, Worker B takes over from the
    committed checkpoint, finishes both streams, and emits exactly 1 IMPORT_READY."""
    plan = default_repo_plan()
    run_id, job_id = create_import_run(db, plan, idempotency_key="t007b-sigkill-01")

    script = (
        "import os, sys, time\n"
        "from uuid import UUID\n"
        "from t007a.import_run import default_repo_plan, run_stream_to_completion\n"
        "from t007a.worker import Worker\n"
        "dsn, run_id, job_id = sys.argv[1], UUID(sys.argv[2]), UUID(sys.argv[3])\n"
        "w = Worker(dsn, 'proc-worker-A', lease_s=30)\n"
        "claim = w.claim(job_id)\n"
        "run_stream_to_completion(dsn, run_id=run_id, claim=claim, worker_id='proc-worker-A',\n"
        "                         plan=default_repo_plan(), stream_type='COMMITS', max_pages=1)\n"
        "print('PAGE1_COMMITTED', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, db, str(run_id), str(job_id)],
        cwd=SPIKE_DIR,
        env={**os.environ, "PYTHONPATH": str(SPIKE_DIR)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line == "PAGE1_COMMITTED"

    # Real abrupt process termination via SIGKILL (exit -9).
    os.kill(proc.pid, signal.SIGKILL)
    rc = proc.wait(5)
    evidence("killed_subprocess_returncode", rc)
    assert rc == -signal.SIGKILL

    mid_snap = evidence("after_sigkill_before_takeover", import_snapshot(db, run_id))
    assert mid_snap["item_count"] == 2
    assert len(mid_snap["import_ready"]) == 0

    _force_expire_lease(db, job_id)
    worker_b = Worker(db, "proc-takeover-B")
    claim_b = worker_b.claim(job_id)
    assert claim_b is not None and claim_b.attempt_no == 2

    for stream in plan.required_streams:
        run_stream_to_completion(
            db,
            run_id=run_id,
            claim=claim_b,
            worker_id="proc-takeover-B",
            plan=plan,
            stream_type=stream,
        )
    fin = try_finalize_import_run(
        db, run_id=run_id, claim=claim_b, worker_id="proc-takeover-B", attempt_no=claim_b.attempt_no
    )
    final_snap = evidence("after_takeover_complete", import_snapshot(db, run_id))

    assert fin["finalized"] is True and fin["emitted_import_ready"] is True
    assert final_snap["item_count"] == 6
    assert len(final_snap["import_ready"]) == 1
    assert final_snap["run"]["status"] == "SUCCEEDED"


# =======================================================================================
# 3. Safe resume equivalence, blocked unsafe/drifted resume, and IMPORT_READY race
# =======================================================================================
def test_normal_run_and_interrupted_resumed_run_produce_identical_logical_state(db, evidence):
    """Fixed plan executed uninterrupted vs interrupted mid-stream + safely resumed
    must yield the exact same logical DB state (streams, cursors, items, IMPORT_READY=1)."""
    plan = default_repo_plan()

    # Run 1: Normal uninterrupted execution.
    normal_run, normal_job = create_import_run(db, plan, idempotency_key="t007b-eq-normal")
    w_norm = Worker(db, "w-normal")
    c_norm = w_norm.claim(normal_job)
    assert c_norm is not None
    for stream in plan.required_streams:
        run_stream_to_completion(
            db, run_id=normal_run, claim=c_norm, worker_id="w-normal", plan=plan, stream_type=stream
        )
    try_finalize_import_run(
        db, run_id=normal_run, claim=c_norm, worker_id="w-normal", attempt_no=c_norm.attempt_no
    )
    normal_snap = evidence("normal_run", import_snapshot(db, normal_run))

    # Run 2: Interrupted after 1 page of COMMITS and 0 pages of PULL_REQUESTS, then resumed.
    resumed_run, resumed_job = create_import_run(db, plan, idempotency_key="t007b-eq-resumed")
    w1 = Worker(db, "w-part-1")
    c1 = w1.claim(resumed_job)
    assert c1 is not None
    run_stream_to_completion(
        db,
        run_id=resumed_run,
        claim=c1,
        worker_id="w-part-1",
        plan=plan,
        stream_type="COMMITS",
        max_pages=1,
    )
    # Before all required streams complete, IMPORT_READY must remain 0 even if checked.
    premature = try_finalize_import_run(
        db, run_id=resumed_run, claim=c1, worker_id="w-part-1", attempt_no=c1.attempt_no
    )
    assert premature == {
        "finalized": False,
        "emitted_import_ready": False,
        "incomplete": ["COMMITS", "PULL_REQUESTS"],
    }
    mark_interrupted_partial(
        db, run_id=resumed_run, claim=c1, failure_reason="TRANSIENT_PROVIDER_STOP"
    )
    mid_snap = evidence("interrupted_mid_run", import_snapshot(db, resumed_run))
    assert len(mid_snap["import_ready"]) == 0
    assert mid_snap["run"]["status"] == "PARTIAL"

    # Safe resume on the SAME fixed plan.
    resumed_streams = prepare_safe_resume(db, run_id=resumed_run, plan=plan)
    assert {s.stream_type: s.cursor for s in resumed_streams} == {
        "COMMITS": "cur-commits-p2",
        "PULL_REQUESTS": None,
    }
    w2 = Worker(db, "w-part-2")
    c2 = w2.claim(resumed_job)
    assert c2 is not None and c2.attempt_no == 2
    for stream in plan.required_streams:
        run_stream_to_completion(
            db, run_id=resumed_run, claim=c2, worker_id="w-part-2", plan=plan, stream_type=stream
        )
    try_finalize_import_run(
        db, run_id=resumed_run, claim=c2, worker_id="w-part-2", attempt_no=c2.attempt_no
    )
    resumed_snap = evidence("resumed_run", import_snapshot(db, resumed_run))

    assert normal_snap["logical"] == resumed_snap["logical"]
    assert resumed_snap["logical"]["import_ready_count"] == 1
    assert resumed_snap["logical"]["import_ready_total_items"] == [6]


def test_unsafe_checkpoint_and_plan_drift_block_automatic_resume(db, evidence):
    """Automatic resume of the same run is refused if safe_checkpoint=false OR if the
    plan changed (plan_fingerprint mismatch -> source_drift=true, PLAN_DRIFT)."""
    plan = default_repo_plan()

    # Case 1: Unsafe checkpoint blocks resume.
    unsafe_run, unsafe_job = create_import_run(db, plan, idempotency_key="t007b-unsafe-01")
    w1 = Worker(db, "w-unsafe")
    c1 = w1.claim(unsafe_job)
    assert c1 is not None
    unsafe_page = SyntheticPage(
        page_cursor="START",
        items=plan.fetch_page("COMMITS", None).items,
        next_cursor="cur-unverified",
        has_more=True,
        safe_checkpoint=False,
    )
    commit_page_and_checkpoint(
        db,
        run_id=unsafe_run,
        claim=c1,
        worker_id="w-unsafe",
        stream_type="COMMITS",
        expected_version=0,
        page=unsafe_page,
    )
    mark_interrupted_partial(db, run_id=unsafe_run, claim=c1, failure_reason="UNVERIFIED_CURSOR")

    with pytest.raises(ResumeBlockedError) as unsafe_exc:
        prepare_safe_resume(db, run_id=unsafe_run, plan=plan)
    assert unsafe_exc.value.reason == "UNSAFE_CHECKPOINT"
    unsafe_snap = evidence("unsafe_checkpoint_blocked", import_snapshot(db, unsafe_run))
    assert unsafe_snap["run"]["status"] == "PARTIAL"
    assert unsafe_snap["run"]["failure_reason"] == "UNSAFE_CHECKPOINT"
    assert len(unsafe_snap["import_ready"]) == 0

    # Case 2: Plan drift blocks resume and sets source_drift=true.
    drift_run, drift_job = create_import_run(db, plan, idempotency_key="t007b-drift-01")
    w2 = Worker(db, "w-drift")
    c2 = w2.claim(drift_job)
    assert c2 is not None
    run_stream_to_completion(
        db,
        run_id=drift_run,
        claim=c2,
        worker_id="w-drift",
        plan=plan,
        stream_type="COMMITS",
        max_pages=1,
    )
    mark_interrupted_partial(db, run_id=drift_run, claim=c2, failure_reason="TRANSIENT_STOP")

    drifted_plan = default_repo_plan(plan_id="plan-repo-alpha-v2-force-pushed")
    with pytest.raises(ResumeBlockedError) as drift_exc:
        prepare_safe_resume(db, run_id=drift_run, plan=drifted_plan)
    assert drift_exc.value.reason == "PLAN_DRIFT"
    drift_snap = evidence("plan_drift_blocked", import_snapshot(db, drift_run))
    assert drift_snap["run"]["status"] == "PARTIAL"
    assert drift_snap["run"]["source_drift"] is True
    assert drift_snap["run"]["failure_reason"] == "PLAN_DRIFT"
    assert len(drift_snap["import_ready"]) == 0


def test_import_ready_is_zero_before_all_streams_done_and_one_after_completion_race(
    db, evidence
):
    """IMPORT_READY is 0 when only COMMITS is COMPLETED, and under an 8-thread completion
    race right as PULL_REQUESTS finishes, exactly 1 caller emits IMPORT_READY and
    import_ready_outbox contains 1 row."""
    plan = default_repo_plan()
    run_id, job_id = create_import_run(db, plan, idempotency_key="t007b-ready-race-01")
    worker = Worker(db, "w-race")
    claim = worker.claim(job_id)
    assert claim is not None

    # Complete COMMITS only; PULL_REQUESTS is still PENDING/partial.
    run_stream_to_completion(
        db, run_id=run_id, claim=claim, worker_id="w-race", plan=plan, stream_type="COMMITS"
    )
    run_stream_to_completion(
        db,
        run_id=run_id,
        claim=claim,
        worker_id="w-race",
        plan=plan,
        stream_type="PULL_REQUESTS",
        max_pages=1,
    )
    before_all_done = try_finalize_import_run(
        db, run_id=run_id, claim=claim, worker_id="w-race", attempt_no=claim.attempt_no
    )
    snap_before = evidence("before_all_streams_completed", import_snapshot(db, run_id))
    assert before_all_done == {
        "finalized": False,
        "emitted_import_ready": False,
        "incomplete": ["PULL_REQUESTS"],
    }
    assert len(snap_before["import_ready"]) == 0

    # Finish the remaining page of PULL_REQUESTS.
    run_stream_to_completion(
        db, run_id=run_id, claim=claim, worker_id="w-race", plan=plan, stream_type="PULL_REQUESTS"
    )

    # 8 concurrent finalizers race to emit IMPORT_READY on the completed run.
    barrier = threading.Barrier(8)
    results: list[dict] = []
    lock = threading.Lock()

    def race_finalize(idx: int):
        barrier.wait()
        out = try_finalize_import_run(
            db,
            run_id=run_id,
            claim=None,
            worker_id=f"w-finalizer-{idx}",
            attempt_no=claim.attempt_no,
            enforce_lease_fence=False,
        )
        with lock:
            results.append(out)

    threads = [threading.Thread(target=race_finalize, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    emitted_flags = [r["emitted_import_ready"] for r in results]
    evidence("race_emitted_counts", {"true": sum(emitted_flags), "false": emitted_flags.count(False)})
    snap_after = evidence("after_completion_race", import_snapshot(db, run_id))

    assert sum(emitted_flags) == 1
    assert emitted_flags.count(False) == 7
    assert len(snap_after["import_ready"]) == 1
    assert snap_after["import_ready"][0]["intent_type"] == "IMPORT_READY"
    assert snap_after["import_ready"][0]["total_items"] == 6
    assert snap_after["run"]["status"] == "SUCCEEDED"
