"""T-007C lease/exhaustion, retry classification, ambiguity quarantine, and concurrency tests.

Covers on real PostgreSQL (no network / GitHub / LLM calls; canned fixtures only):
1. Final attempt claimed -> worker dies -> lease expires -> recovery atomically reaps
   job + attempt + outbox into FAILED(MAX_ATTEMPTS_LEASE_EXPIRED) without waking a worker,
   and repeated recovery never wakes it again.
2. Late write from the zombie/stalled worker after final-attempt reap is rejected by lease
   fencing; no result is stored and reaped FAILED state is preserved.
3. Concurrent recovery workers (8 threads) against mixed exhausted + pending backlog:
   SKIP LOCKED prevents double-reap and double-wake.
4. Duplicate wake deliveries (concurrent + post-completion) execute processing once and
   store at most one result.
5. Worker HTTP response lost immediately after result save + commit: outbox is already DONE,
   subsequent recovery / redelivery does not re-execute or duplicate results.
6. Transient fixture errors with remaining attempt budget retry deterministically via
   bounded recovery and converge to SUCCEEDED (or FAILED(MAX_ATTEMPTS_EXHAUSTED:...) when
   budget runs out); permanent fixture errors fail immediately on attempt 1.
7. Ambiguous upstream timeout (unknown side-effect completion) transitions to
   REVIEW_REQUIRED, closes outbox DONE, and NEVER auto-re-invokes the external fixture.
"""

import threading
import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from t007a.api import PREFIX, create_api
from t007a.db import connect, snapshot
from t007a.faults import FaultPlan, InjectedFault
from t007a.fixtures import (
    AmbiguousExternalTimeoutError,
    FixtureExternalProcessor,
    PermanentExternalError,
    TransientExternalError,
)
from t007a.relay import EXHAUSTED_REASON, recover
from t007a.settings import Settings
from t007a.wake import WakeError
from t007a.worker import Worker, create_worker_app

BODY = {"text": "synthetic career note for t007c"}


def make_client(dsn, wake=None, faults=None) -> TestClient:
    kwargs = {"wake": wake}
    if faults is not None:
        kwargs["faults"] = faults
    return TestClient(create_api(Settings(database_url=dsn), **kwargs))


def submit(client, key, body=BODY):
    return client.post(f"{PREFIX}/jobs", json=body, headers={"Idempotency-Key": key})


# --- 1. Final attempt worker death + lease expiry + recovery --------------------------
def test_last_attempt_worker_death_lease_expiry_converges_to_failed(db, evidence):
    """Claim final attempt -> worker dies -> lease expires -> recover reaps to FAILED."""
    client = make_client(db, None, FaultPlan({"api.wake": 1}))
    job_id = UUID(submit(client, "t007c-last-die-01").json()["job_id"])

    with connect(db, "t007a-observer") as conn:
        conn.execute("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (job_id,))

    dying_worker = Worker(db, "w-dying", lease_s=0.3)
    claim = dying_worker.claim(job_id)
    assert claim is not None and claim.attempt_no == 1
    time.sleep(0.5)  # lease expired while RUNNING at 1/1

    fresh_worker = Worker(db, "w-fresh")
    wakes_received: list[UUID] = []

    def wake_cb(jid: UUID) -> None:
        wakes_received.append(jid)
        fresh_worker.drain(5, jid)

    rec1 = evidence("recover_1", recover(db, wake_cb, limit=10, grace_s=0))
    snap1 = evidence("after_recover_1", snapshot(db))
    status = client.get(f"{PREFIX}/jobs/{job_id}").json()

    assert rec1 == {
        "selected": 0,
        "reaped": 1,
        "acknowledged": 0,
        "failed": 0,
        "jobs": {str(job_id): "reaped_failed"},
    }
    assert status["job"]["status"] == "FAILED"
    assert status["job"]["terminal_reason"] == EXHAUSTED_REASON
    assert status["job"]["attempt_count"] == 1
    assert status["outbox"]["status"] == "DONE"
    assert status["result"] is None
    assert status["attempts"][0]["outcome"] == "LEASE_LOST"
    assert status["attempts"][0]["error_class"] == "LeaseExpiredAtMaxAttempts"
    assert snap1["jobs"]["by_status"] == {"FAILED": 1}
    assert snap1["outbox"]["by_status"] == {"DONE": 1}
    assert snap1["results"]["count"] == 0
    assert snap1["job_attempts"]["by_outcome"] == {"LEASE_LOST": 1}
    assert wakes_received == []

    # Repeated recovery must neither select, reap, nor wake this job again.
    rec2 = evidence("recover_2", recover(db, wake_cb, limit=10, grace_s=0))
    assert rec2["selected"] == 0 and rec2["reaped"] == 0 and wakes_received == []


# --- 2. Zombie worker late write after final-attempt reap -----------------------------
def test_zombie_worker_late_write_after_final_attempt_reap_is_rejected(db, evidence):
    """Previous worker wakes up after recovery already reaped the exhausted job:
    its late result save (and late failure report) must be fenced out."""
    client = make_client(db, None, FaultPlan({"api.wake": 1}))
    job_id = UUID(submit(client, "t007c-zombie-late-01").json()["job_id"])
    with connect(db, "t007a-observer") as conn:
        conn.execute("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (job_id,))

    zombie = Worker(db, "w-zombie", lease_s=0.3)
    claim = zombie.claim(job_id)
    assert claim is not None and claim.attempt_no == 1
    time.sleep(0.5)

    rec = recover(db, None, limit=10, grace_s=0)
    assert rec["reaped"] == 1

    # Now the stalled zombie finishes processing and tries to save a late result.
    late_save_outcome = zombie._save(claim, {"processor": "zombie-late", "ok": True})
    evidence("late_save_outcome", late_save_outcome)
    assert late_save_outcome == "LEASE_LOST"

    status = client.get(f"{PREFIX}/jobs/{job_id}").json()
    snap = evidence("final", snapshot(db))
    assert status["job"]["status"] == "FAILED"
    assert status["job"]["terminal_reason"] == EXHAUSTED_REASON
    assert status["result"] is None
    assert status["attempts"][0]["error_class"] == "LeaseExpiredAtMaxAttempts"
    assert snap["jobs"]["by_status"] == {"FAILED": 1}
    assert snap["outbox"]["by_status"] == {"DONE": 1}
    assert snap["results"]["count"] == 0
    assert snap["job_attempts"]["by_outcome"] == {"LEASE_LOST": 1}


# --- 3. Concurrent recovery execution -------------------------------------------------
def test_concurrent_recover_workers_do_not_double_reap_or_double_wake(db, evidence):
    """8 concurrent relays against 2 exhausted expired jobs + 2 pending jobs:
    each exhausted job is reaped once and each pending job is woken once."""
    client = make_client(db, None, FaultPlan({"api.wake": 4}))
    exhausted_ids = [UUID(submit(client, f"t007c-crec-ex-{i}").json()["job_id"]) for i in range(2)]
    pending_ids = [UUID(submit(client, f"t007c-crec-ok-{i}").json()["job_id"]) for i in range(2)]

    with connect(db, "t007a-observer") as conn:
        for jid in exhausted_ids:
            conn.execute("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (jid,))

    for i, jid in enumerate(exhausted_ids):
        assert Worker(db, f"w-die-{i}", lease_s=0.25).claim(jid) is not None
    time.sleep(0.45)

    # Age initial submission timestamps past the 5s recovery grace window so
    # concurrent relays share a real cooldown window via last_recovery_at.
    with connect(db, "t007a-observer") as conn:
        conn.execute("UPDATE outbox SET created_at = now() - interval '60 seconds'")

    woke_lock = threading.Lock()
    woke: list[UUID] = []

    def wake_cb(jid: UUID) -> None:
        with woke_lock:
            woke.append(jid)
        Worker(db, "w-live").drain(1, jid)

    barrier = threading.Barrier(8)
    summaries: list[dict] = []

    def run_relay():
        barrier.wait()
        out = recover(db, wake_cb, limit=10, grace_s=5.0)
        with woke_lock:
            summaries.append(out)

    threads = [threading.Thread(target=run_relay) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    total_reaped = sum(s["reaped"] for s in summaries)
    total_selected = sum(s["selected"] for s in summaries)
    evidence("relay_totals", {"reaped": total_reaped, "selected": total_selected, "woke": len(woke)})
    snap = evidence("final", snapshot(db))

    assert total_reaped == 2
    assert total_selected == 2
    assert sorted(woke) == sorted(pending_ids)
    assert snap["jobs"]["by_status"] == {"FAILED": 2, "SUCCEEDED": 2}
    assert snap["outbox"]["by_status"] == {"DONE": 4}
    assert snap["outbox"]["recovery_attempts"] == 4
    assert snap["results"]["count"] == 2
    assert snap["job_attempts"]["by_outcome"] == {"LEASE_LOST": 2, "SUCCEEDED": 2}


# --- 4. Duplicate wake delivery -------------------------------------------------------
def test_duplicate_wake_deliveries_process_once(db, evidence):
    """Concurrent + repeated /internal/t007a/wake calls with the same job_id hint
    invoke the processor once and store exactly one result."""
    fixture = FixtureExternalProcessor([{"summary": "canonical"}])
    client = make_client(db, None, FaultPlan({"api.wake": 1}))
    job_id = submit(client, "t007c-dupwake-01").json()["job_id"]

    worker_app = create_worker_app(Settings(database_url=db, worker_id="w-dup"), process=fixture)
    barrier = threading.Barrier(6)
    processed_counts: list[int] = []

    def send_wake():
        barrier.wait()
        r = TestClient(worker_app).post("/internal/t007a/wake", json={"job_id": job_id})
        processed_counts.append(len(r.json()["processed"]))

    threads = [threading.Thread(target=send_wake) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    # Late duplicate wake after completion:
    late = TestClient(worker_app).post("/internal/t007a/wake", json={"job_id": job_id}).json()
    assert late == {"processed": []}

    evidence("wake_processed_counts", sorted(processed_counts))
    evidence("fixture_calls", fixture.call_count)
    snap = evidence("final", snapshot(db))

    assert sorted(processed_counts) == [0, 0, 0, 0, 0, 1]
    assert fixture.call_count == 1
    assert snap["jobs"]["by_status"] == {"SUCCEEDED": 1}
    assert snap["outbox"]["by_status"] == {"DONE": 1}
    assert snap["results"]["count"] == 1
    assert snap["job_attempts"]["by_outcome"] == {"SUCCEEDED": 1}


# --- 5. Worker response lost right after result save ----------------------------------
def test_worker_response_lost_after_result_saved_converges_without_reexecution(db, evidence):
    """Worker saves result + marks outbox DONE, then crashes/drops HTTP response before
    API receives wake ack. API records wake=failed, but outbox stays DONE, fixture ran
    once, and neither recovery nor client redelivery re-executes the job."""
    fixture = FixtureExternalProcessor([{"summary": "saved-before-drop"}])
    worker_app = create_worker_app(
        Settings(database_url=db, worker_id="w-drop"),
        process=fixture,
        faults=FaultPlan({"worker.response_lost_after_save": 1}),
    )
    wclient = TestClient(worker_app, raise_server_exceptions=False)

    def http_like_wake(jid: UUID) -> None:
        try:
            r = wclient.post("/internal/t007a/wake", json={"job_id": str(jid)})
        except InjectedFault as exc:
            raise WakeError("RemoteProtocolError") from exc
        if r.status_code >= 300:
            raise WakeError(f"http_{r.status_code}")

    api_client = make_client(db, http_like_wake)
    r1 = submit(api_client, "t007c-saveresp-01")
    assert r1.status_code == 202
    assert r1.json()["wake"] == "failed"  # caller saw wake failure
    job_id = r1.json()["job_id"]

    snap_after_drop = evidence("after_worker_response_lost", snapshot(db))
    assert snap_after_drop["jobs"]["by_status"] == {"SUCCEEDED": 1}
    assert snap_after_drop["outbox"]["by_status"] == {"DONE": 1}
    assert snap_after_drop["results"]["count"] == 1
    assert fixture.call_count == 1

    # Recovery sees outbox DONE -> nothing selected or reaped.
    rec = evidence("recover_after_drop", recover(db, http_like_wake, limit=10, grace_s=0))
    assert rec["selected"] == 0 and rec["reaped"] == 0

    # Client redelivery returns same job_id with wake=skipped and does not re-run fixture.
    r2 = submit(api_client, "t007c-saveresp-01")
    assert r2.json()["job_id"] == job_id
    assert r2.json()["replayed"] is True and r2.json()["wake"] == "skipped"
    assert fixture.call_count == 1
    snap_final = evidence("final", snapshot(db))
    assert snap_final["results"]["count"] == 1
    assert snap_final["job_attempts"]["by_outcome"] == {"SUCCEEDED": 1}


# --- 6. Transient retries vs budget exhaustion vs permanent error ---------------------
def test_transient_fixture_error_retries_within_budget_and_succeeds(db, evidence):
    """2 transient errors followed by fixture success with max_attempts=3 -> SUCCEEDED on #3."""
    fixture = FixtureExternalProcessor(
        [
            TransientExternalError("upstream_503_1"),
            TransientExternalError("upstream_503_2"),
            {"summary": "ok_on_third"},
        ]
    )
    worker = Worker(db, "w-retry", process=fixture)
    client = make_client(db, lambda jid: worker.drain(1, jid))

    r = submit(client, "t007c-transient-ok")
    job_id = r.json()["job_id"]
    snap_1 = evidence("after_attempt_1", snapshot(db))
    assert snap_1["jobs"]["by_status"] == {"PENDING": 1}
    assert snap_1["outbox"]["by_status"] == {"PENDING": 1}

    recover(db, lambda jid: worker.drain(1, jid), limit=5, grace_s=0)
    snap_2 = evidence("after_attempt_2", snapshot(db))
    assert snap_2["jobs"]["by_status"] == {"PENDING": 1}

    recover(db, lambda jid: worker.drain(1, jid), limit=5, grace_s=0)
    snap_3 = evidence("final", snapshot(db))
    status = client.get(f"{PREFIX}/jobs/{job_id}").json()

    assert fixture.call_count == 3
    assert status["job"]["status"] == "SUCCEEDED"
    assert status["job"]["attempt_count"] == 3
    assert status["result"]["attempt_no"] == 3
    assert snap_3["jobs"]["by_status"] == {"SUCCEEDED": 1}
    assert snap_3["outbox"]["by_status"] == {"DONE": 1}
    assert snap_3["results"]["count"] == 1
    assert snap_3["job_attempts"]["by_outcome"] == {"FAILED": 2, "SUCCEEDED": 1}
    assert recover(db, lambda jid: worker.drain(1, jid), limit=5, grace_s=0)["selected"] == 0


def test_transient_errors_exhausting_budget_converge_to_failed(db, evidence):
    """3 transient errors with max_attempts=3 -> FAILED(MAX_ATTEMPTS_EXHAUSTED:...) and DONE."""
    fixture = FixtureExternalProcessor(
        [TransientExternalError(f"503_{i}") for i in range(4)]
    )
    worker = Worker(db, "w-exhaust", process=fixture)
    client = make_client(db, lambda jid: worker.drain(1, jid))
    job_id = submit(client, "t007c-transient-exhaust").json()["job_id"]

    recover(db, lambda jid: worker.drain(1, jid), limit=5, grace_s=0)
    recover(db, lambda jid: worker.drain(1, jid), limit=5, grace_s=0)
    snap = evidence("final", snapshot(db))
    status = client.get(f"{PREFIX}/jobs/{job_id}").json()

    assert fixture.call_count == 3
    assert status["job"]["status"] == "FAILED"
    assert status["job"]["terminal_reason"] == "MAX_ATTEMPTS_EXHAUSTED:TransientExternalError"
    assert snap["jobs"]["by_status"] == {"FAILED": 1}
    assert snap["outbox"]["by_status"] == {"DONE": 1}
    assert snap["results"]["count"] == 0
    assert snap["job_attempts"]["by_outcome"] == {"FAILED": 3}
    # Further recovery must not wake it again.
    after = recover(db, lambda jid: worker.drain(1, jid), limit=5, grace_s=0)
    assert after["selected"] == 0 and after["reaped"] == 0 and fixture.call_count == 3


def test_permanent_fixture_error_fails_immediately_without_retry(db, evidence):
    """Permanent upstream rejection fails on attempt 1 even when max_attempts=3."""
    fixture = FixtureExternalProcessor([PermanentExternalError("invalid_schema"), {"ok": True}])
    worker = Worker(db, "w-perm", process=fixture)
    client = make_client(db, lambda jid: worker.drain(1, jid))
    job_id = submit(client, "t007c-perm-01").json()["job_id"]

    snap = evidence("final", snapshot(db))
    status = client.get(f"{PREFIX}/jobs/{job_id}").json()
    assert fixture.call_count == 1
    assert status["job"]["status"] == "FAILED"
    assert status["job"]["attempt_count"] == 1
    assert status["job"]["terminal_reason"] == "PERMANENT_FAILURE:PermanentExternalError"
    assert snap["outbox"]["by_status"] == {"DONE": 1}
    assert recover(db, lambda jid: worker.drain(1, jid), limit=5, grace_s=0)["selected"] == 0


# --- 7. Ambiguous external timeout quarantined for manual review ----------------------
@pytest.mark.parametrize("initial_attempt", [1])
def test_ambiguous_external_timeout_quarantines_without_auto_retry(
    db, evidence, initial_attempt
):
    """Timeout after sending an external request whose completion is unknown MUST NOT
    auto-retry even when attempt_count (1) < max_attempts (3). Transitions to
    REVIEW_REQUIRED, closes outbox DONE, and ignores subsequent wakes/recovery."""
    fixture = FixtureExternalProcessor(
        [
            AmbiguousExternalTimeoutError("read_timeout_after_request_sent"),
            {"summary": "MUST_NEVER_BE_CALLED"},
        ]
    )
    worker = Worker(db, "w-ambig", process=fixture)
    client = make_client(db, lambda jid: worker.drain(1, jid))
    r = submit(client, "t007c-ambig-01")
    job_id = UUID(r.json()["job_id"])

    status = client.get(f"{PREFIX}/jobs/{job_id}").json()
    snap = evidence("after_timeout", snapshot(db))

    assert fixture.call_count == initial_attempt == 1
    assert status["job"]["status"] == "REVIEW_REQUIRED"
    assert status["job"]["attempt_count"] == 1
    assert status["job"]["max_attempts"] == 3  # 2 attempts left, intentionally NOT used
    assert status["job"]["terminal_reason"] == (
        "AMBIGUOUS_EXTERNAL_TIMEOUT:AmbiguousExternalTimeoutError"
    )
    assert status["outbox"]["status"] == "DONE"
    assert status["result"] is None
    assert [a["outcome"] for a in status["attempts"]] == ["AMBIGUOUS_TIMEOUT"]
    assert snap["jobs"]["by_status"] == {"REVIEW_REQUIRED": 1}
    assert snap["outbox"]["by_status"] == {"DONE": 1}

    # Neither direct worker drain, bounded recovery, nor client replay may re-invoke fixture.
    assert worker.drain(5, job_id) == []
    rec = evidence("recover_after_quarantine", recover(db, lambda j: worker.drain(1, j), grace_s=0))
    assert rec["selected"] == 0 and rec["reaped"] == 0
    replay = submit(client, "t007c-ambig-01")
    assert replay.json()["job_id"] == str(job_id) and replay.json()["wake"] == "skipped"
    assert fixture.call_count == 1
