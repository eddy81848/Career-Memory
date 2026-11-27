"""Two real processes (API + Worker via uvicorn) on localhost, real HTTP wake, real PG."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from t007a.db import snapshot

SPIKE_DIR = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(factory: str, port: int, env: dict):
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", factory, "--factory", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=SPIKE_DIR, env={**os.environ, **env},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(proc.stdout.read().decode(errors="replace"))
            time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(10)


def poll_status(url: str, want: str, timeout_s: float = 10) -> dict:
    deadline = time.time() + timeout_s
    while True:
        body = httpx.get(url).json()
        if body["job"]["status"] == want or time.time() > deadline:
            return body
        time.sleep(0.1)


def test_processes_happy_path_over_http(db, evidence):
    wport, aport = free_port(), free_port()
    base_env = {"T007A_DATABASE_URL": db}
    with serve("t007a.worker:create_worker_app", wport, base_env | {"T007A_WORKER_ID": "proc-w1"}):
        env = base_env | {"T007A_WORKER_WAKE_URL": f"http://127.0.0.1:{wport}/internal/t007a/wake"}
        with serve("t007a.api:create_api", aport, env) as api:
            r = httpx.post(f"{api}/_prototype/t007a/jobs", json={"text": "proc happy"},
                           headers={"Idempotency-Key": "proc-happy-0001"})
            assert r.status_code == 202 and r.json()["wake"] == "acknowledged"
            body = poll_status(api + r.json()["status_url"], "SUCCEEDED")
    assert body["result"]["worker_id"] == "proc-w1"
    snap = evidence("final", snapshot(db))
    assert snap["jobs"]["by_status"] == {"SUCCEEDED": 1}
    assert snap["outbox"]["by_status"] == {"DONE": 1}
    assert snap["results"]["count"] == 1 and snap["job_attempts"]["count"] == 1


def test_processes_worker_unreachable_then_recover_endpoint(db, evidence):
    wport, aport, dead = free_port(), free_port(), free_port()
    base_env = {"T007A_DATABASE_URL": db}
    # API points at a port nobody listens on: a *real* wake failure, not an injected one.
    env = base_env | {"T007A_WORKER_WAKE_URL": f"http://127.0.0.1:{dead}/internal/t007a/wake",
                      "T007A_WAKE_TIMEOUT_S": "1"}
    with serve("t007a.api:create_api", aport, env) as api:
        r = httpx.post(f"{api}/_prototype/t007a/jobs", json={"text": "proc recover"},
                       headers={"Idempotency-Key": "proc-recover-01"})
        assert r.status_code == 202 and r.json()["wake"] == "failed"
        status_url = api + r.json()["status_url"]
        snap = evidence("after_wake_failure", snapshot(db))
        assert snap["jobs"]["by_status"] == {"PENDING": 1}
        assert snap["outbox"]["by_status"] == {"PENDING": 1}
        assert snap["outbox"]["last_errors"] == ["wake_failed:ConnectError"]
        assert snap["results"]["count"] == 0

        with serve("t007a.worker:create_worker_app", wport,
                   base_env | {"T007A_WORKER_ID": "proc-w2"}) as worker:
            out = httpx.post(f"{worker}/internal/t007a/recover",
                             params={"limit": 5, "grace_s": 0}).json()
            evidence("recover_call", out)
            assert out["selected"] == 1 and out["acknowledged"] == 1
            body = poll_status(status_url, "SUCCEEDED")
            again = httpx.post(f"{worker}/internal/t007a/recover",
                               params={"limit": 5, "grace_s": 0}).json()
            assert again["selected"] == 0
    assert body["result"]["worker_id"] == "proc-w2"
    snap = evidence("after_recovery", snapshot(db))
    assert snap["jobs"]["by_status"] == {"SUCCEEDED": 1}
    assert snap["outbox"]["by_status"] == {"DONE": 1}
    assert snap["results"]["count"] == 1 and snap["job_attempts"]["by_outcome"] == {"SUCCEEDED": 1}
