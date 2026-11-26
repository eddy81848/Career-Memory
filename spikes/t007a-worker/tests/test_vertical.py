"""T-007A vertical tests against a REAL PostgreSQL (see conftest.py).

Each case records observable job/outbox/result/attempt row counts and states via
``evidence(...)``; the same numbers are asserted, so the JSON cannot drift from
what was actually checked.
"""

import threading
import time
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from t007a.api import PREFIX, create_api
from t007a.db import connect, open_app_transactions, snapshot
from t007a.faults import FaultPlan, InjectedFault
from t007a.relay import recover
from t007a.settings import Settings
from t007a.worker import Worker, create_worker_app

BODY = {"text": "synthetic career note for t007a"}


class ObservingWake:
    """In-process stand-in for the HTTP wake: records what the DB looked like at
    wake time (proves wake happens after commit, with no app transaction open),
    then lets a worker drain."""

    def __init__(self, dsn: str, worker: Worker | None, max_jobs: int = 5):
        self.dsn, self.worker, self.max_jobs = dsn, worker, max_jobs
        self.observations: list[dict] = []

    def __call__(self, job_id: UUID) -> None:
        with connect(self.dsn, "t007a-observer") as c:
            row = c.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        self.observations.append(
            {
                "job_visible_from_other_session": row is not None,
                "open_app_transactions": len(open_app_transactions(self.dsn)),
            }
        )
        if self.worker:
            self.worker.drain(self.max_jobs, job_id)


def make_client(dsn, wake=None, faults=None) -> TestClient:
    kwargs = {"wake": wake}
    if faults is not None:
        kwargs["faults"] = faults
    return TestClient(create_api(Settings(database_url=dsn), **kwargs))


def submit(client, key, body=BODY):
    return client.post(f"{PREFIX}/jobs", json=body, headers={"Idempotency-Key": key})


def only_job_id(dsn) -> UUID:
    with connect(dsn, "t007a-observer") as c:
        rows = c.execute("SELECT id FROM jobs").fetchall()
    assert len(rows) == 1
    return rows[0]["id"]


def expect(snap, jobs, outbox, results, attempts):
    assert snap["jobs"]["count"] == sum(jobs.values()) and snap["jobs"]["by_status"] == jobs
    assert snap["outbox"]["count"] == sum(outbox.values())
    assert snap["outbox"]["by_status"] == outbox
    assert snap["results"]["count"] == results
    assert snap["job_attempts"]["by_outcome"] == attempts


# ---------------------------------------------------------------------------------------
def test_happy_path(db, evidence):
    wake = ObservingWake(db, Worker(db, "w1"))
    client = make_client(db, wake)
    r = submit(client, "key-happy-0001")
    assert r.status_code == 202
    assert r.headers["X-T007A-Prototype"] == "not-product-api"
    body = r.json()
    assert body["replayed"] is False and body["wake"] == "acknowledged"
    assert body["status_url"] == f"{PREFIX}/jobs/{body['job_id']}"

    status = client.get(body["status_url"]).json()
    assert status["job"]["status"] == "SUCCEEDED"
    assert status["result"]["output"]["processor"] == "t007a-fixed-v1"
    assert [a["outcome"] for a in status["attempts"]] == ["SUCCEEDED"]

    assert wake.observations == [
        {"job_visible_from_other_session": True, "open_app_transactions": 0}
    ]
    snap = evidence("final", snapshot(db))
    evidence("wake_observation", wake.observations)
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})
    assert snap["outbox"]["dispatch_attempts"] == 1


@pytest.mark.parametrize("point", ["api.between_job_and_outbox", "api.before_commit"])
def test_failure_before_commit_rolls_back_job_and_outbox(db, evidence, point):
    wake = ObservingWake(db, Worker(db, "w1"))
    client = make_client(db, wake, FaultPlan({point: 1}))
    with pytest.raises(InjectedFault):
        submit(client, "key-rollback-01")
    snap = evidence("after_fault", snapshot(db))
    expect(snap, {}, {}, 0, {})
    assert wake.observations == []  # no wake for an uncommitted job

    r = submit(client, "key-rollback-01")  # client retries the same request
    assert r.status_code == 202 and r.json()["replayed"] is False
    snap = evidence("after_retry", snapshot(db))
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})


def test_wake_failure_then_bounded_recovery(db, evidence):
    worker = Worker(db, "w1")
    client = make_client(db, ObservingWake(db, worker), FaultPlan({"api.wake": 1}))
    r = submit(client, "key-wakefail-1")
    assert r.status_code == 202 and r.json()["wake"] == "failed"
    job_id = r.json()["job_id"]
    snap = evidence("after_wake_failure", snapshot(db))
    expect(snap, {"PENDING": 1}, {"PENDING": 1}, 0, {})
    assert snap["outbox"]["last_errors"] == ["wake_failed:injected"]

    out = evidence("recover_1", recover(db, ObservingWake(db, worker), limit=1, grace_s=0))
    assert out["selected"] == 1 and out["acknowledged"] == 1 and list(out["jobs"]) == [job_id]
    snap = evidence("after_recovery", snapshot(db))
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})
    assert snap["outbox"]["recovery_attempts"] == 1

    out = evidence("recover_2", recover(db, ObservingWake(db, worker), limit=10, grace_s=0))
    assert out["selected"] == 0
    assert client.get(f"{PREFIX}/jobs/{job_id}").json()["job"]["status"] == "SUCCEEDED"


def test_recovery_is_bounded_and_respects_grace(db, evidence):
    client = make_client(db, None, FaultPlan({"api.wake": 5}))
    for i in range(5):
        assert submit(client, f"key-bounded-{i:03d}", {"text": f"n{i}"}).json()["wake"] == "failed"
    one_per_wake = ObservingWake(db, Worker(db, "w1"), max_jobs=1)
    assert recover(db, one_per_wake, limit=10, grace_s=3600)["selected"] == 0  # too fresh
    first = recover(db, one_per_wake, limit=2, grace_s=0)
    assert first["selected"] == 2 and first["acknowledged"] == 2
    snap = evidence("after_recover_limit_2", snapshot(db))
    expect(snap, {"SUCCEEDED": 2, "PENDING": 3}, {"DONE": 2, "PENDING": 3}, 2,
           {"SUCCEEDED": 2})
    rounds = 1
    while recover(db, one_per_wake, limit=2, grace_s=0)["selected"]:
        rounds += 1
    evidence("recover_rounds_until_empty", rounds)
    assert rounds == 3  # non-empty rounds: 2 + 2 + 1
    snap = evidence("after_drained", snapshot(db))
    expect(snap, {"SUCCEEDED": 5}, {"DONE": 5}, 5, {"SUCCEEDED": 5})


def test_response_lost_then_same_input_redelivered(db, evidence):
    wake = ObservingWake(db, Worker(db, "w1"))
    client = make_client(db, wake, FaultPlan({"api.response_lost": 1}))
    with pytest.raises(InjectedFault):  # client never sees the job_id
        submit(client, "key-lostresp-1")
    hidden_job_id = only_job_id(db)
    snap = evidence("after_lost_response", snapshot(db))
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})

    r = submit(client, "key-lostresp-1")
    assert r.status_code == 202
    assert r.json() | {"note": None} == {
        "job_id": str(hidden_job_id), "status_url": f"{PREFIX}/jobs/{hidden_job_id}",
        "replayed": True, "wake": "skipped", "note": None,
    }
    snap = evidence("after_redelivery", snapshot(db))
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})
    assert len(wake.observations) == 1


def test_response_lost_and_wake_failed_converge_by_retry_or_recovery(db, evidence):
    worker = Worker(db, "w1")
    client = make_client(
        db, ObservingWake(db, worker), FaultPlan({"api.wake": 1, "api.response_lost": 1})
    )
    with pytest.raises(InjectedFault):
        submit(client, "key-lostboth-1")
    hidden_job_id = only_job_id(db)
    snap = evidence("after_lost_response_and_wake", snapshot(db))
    expect(snap, {"PENDING": 1}, {"PENDING": 1}, 0, {})

    r = submit(client, "key-lostboth-1")  # same input: replay re-sends the hint once
    assert r.json()["job_id"] == str(hidden_job_id)
    assert r.json()["replayed"] is True and r.json()["wake"] == "acknowledged"
    snap = evidence("after_redelivery", snapshot(db))
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})
    assert snap["outbox"]["dispatch_attempts"] == 2

    assert recover(db, ObservingWake(db, worker), limit=10, grace_s=0)["selected"] == 0
    r = submit(client, "key-lostboth-1")
    assert r.json()["job_id"] == str(hidden_job_id) and r.json()["wake"] == "skipped"
    expect(evidence("final", snapshot(db)), {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})


def test_same_key_different_body_is_rejected(db, evidence):
    client = make_client(db, None)
    assert submit(client, "key-conflict-1").status_code == 202
    r = submit(client, "key-conflict-1", {"text": "different"})
    assert r.status_code == 409
    expect(evidence("final", snapshot(db)), {"PENDING": 1}, {"PENDING": 1}, 0, {})


def test_concurrent_duplicate_submissions_create_one_job(db, evidence):
    app = create_api(Settings(database_url=db), wake=ObservingWake(db, Worker(db, "w1")))
    barrier, ids, errors = threading.Barrier(8), [], []

    def go():
        try:
            barrier.wait()
            r = submit(TestClient(app), "key-concurrent-1")
            ids.append((r.status_code, r.json()["job_id"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert {c for c, _ in ids} == {202} and len({j for _, j in ids}) == 1
    snap = evidence("final", snapshot(db))
    evidence("distinct_job_ids_returned", len({j for _, j in ids}))
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})
    assert snap["outbox"]["dispatch_attempts"] == 1  # replays do not amplify wakes


def test_concurrent_workers_claim_once(db, evidence):
    client = make_client(db, None, FaultPlan({"api.wake": 1}))
    submit(client, "key-workers-01")
    barrier, done = threading.Barrier(8), []

    def go(i):
        barrier.wait()
        done.extend(Worker(db, f"w{i}").drain(5))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(done) == 1
    expect(evidence("final", snapshot(db)), {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})


def test_lease_expiry_duplicate_processing_stores_one_result(db, evidence):
    client = make_client(db, None, FaultPlan({"api.wake": 1}))
    job_id = submit(client, "key-lease-0001").json()["job_id"]

    def slow(payload):
        time.sleep(2.0)
        return {"processor": "slow"}

    slow_worker = Worker(db, "w-slow", lease_s=1, process=slow)
    out_a: list = []
    ta = threading.Thread(target=lambda: out_a.append(slow_worker.run_one()))
    ta.start()
    time.sleep(1.4)  # lease of w-slow has expired, it is still "processing"
    out_b = Worker(db, "w-fast").run_one()
    ta.join()
    assert out_b["outcome"] == "SUCCEEDED" and out_b["attempt_no"] == 2
    assert out_a[0]["outcome"] == "LEASE_LOST" and out_a[0]["attempt_no"] == 1
    snap = evidence("final", snapshot(db))
    expect(snap, {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"LEASE_LOST": 1, "SUCCEEDED": 1})
    result = client.get(f"{PREFIX}/jobs/{job_id}").json()["result"]
    assert result["attempt_no"] == 2 and result["worker_id"] == "w-fast"
    evidence("stored_result_attempt", {"attempt_no": 2, "worker_id": "w-fast"})


def test_result_primary_key_is_backstop(db, evidence):
    make_client(db, ObservingWake(db, Worker(db, "w1"))).post(
        f"{PREFIX}/jobs", json=BODY, headers={"Idempotency-Key": "key-backstop-1"}
    )
    job_id = only_job_id(db)
    with connect(db, "t007a-observer") as c, pytest.raises(psycopg.errors.UniqueViolation):
        c.execute(
            "INSERT INTO results (job_id, attempt_no, worker_id, output) VALUES (%s,9,'x','{}')",
            (job_id,),
        )
    evidence("second_insert", "UniqueViolation")
    expect(evidence("final", snapshot(db)), {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})


def test_no_db_transaction_open_during_processing(db, evidence):
    # Negative control: the observer really detects an open app transaction.
    with connect(db, "t007a-worker") as c, c.transaction():
        c.execute("SELECT 1")
        assert len(open_app_transactions(db)) == 1

    client = make_client(db, None, FaultPlan({"api.wake": 1}))
    submit(client, "key-notx-00001")
    started = threading.Event()

    def slow(payload):
        started.set()
        time.sleep(1.0)
        return {"processor": "slow"}

    t = threading.Thread(target=lambda: Worker(db, "w1", process=slow).run_one())
    t.start()
    assert started.wait(5)
    time.sleep(0.3)
    during = {"open_app_transactions": len(open_app_transactions(db)),
              "jobs": snapshot(db)["jobs"]}
    evidence("during_processing", during)
    t.join()
    assert during == {"open_app_transactions": 0,
                      "jobs": {"count": 1, "by_status": {"RUNNING": 1}}}
    expect(evidence("final", snapshot(db)), {"SUCCEEDED": 1}, {"DONE": 1}, 1, {"SUCCEEDED": 1})


def test_fault_injection_is_not_reachable_over_http(db, evidence):
    api = create_api(Settings(database_url=db))
    worker = create_worker_app(Settings(database_url=db))
    api_paths = sorted({r.path for r in api.routes if not r.path.startswith(("/docs", "/redoc",
                                                                             "/openapi"))})
    worker_paths = sorted({r.path for r in worker.routes if r.path.startswith(("/internal",
                                                                               "/healthz"))})
    evidence("routes", {"api": api_paths, "worker": worker_paths})
    assert api_paths == ["/_prototype/t007a/jobs", "/_prototype/t007a/jobs/{job_id}", "/healthz"]
    assert worker_paths == ["/healthz", "/internal/t007a/recover", "/internal/t007a/wake"]
    assert not any("fault" in f for f in Settings.__dataclass_fields__)
    assert TestClient(api).post("/_prototype/t007a/faults").status_code == 404
