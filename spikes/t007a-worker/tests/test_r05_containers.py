"""RUNTIME-05 local container execution test using the spike Dockerfile.

Verifies ONLY local container packaging & inter-container HTTP/DB wiring:
- Builds ``t007a-r05-<run_id>:local`` from ``spikes/t007a-worker/Dockerfile`` (never pushed).
- Runs an isolated throwaway PostgreSQL container (tmpfs PGDATA, no named volumes, no host port)
  plus TWO containers from the SAME built image: one with ``T007A_ROLE=worker`` and one with
  ``T007A_ROLE=api``, all attached to a private bridge network ``t007a-r05-net-<run_id>``.
- Confirms:
  1. Happy path: submit -> DB commit -> container HTTP wake -> 1 result stored.
  2. Worker outage & recovery: stop worker container -> submit still returns 202 and persists
     job+outbox (`wake=failed`) -> start worker container -> bounded `/internal/t007a/recover`
     completes the same `job_id` with 1 result.
- Cleans up ONLY the containers, bridge network, and image tag created in this run (verified
  zero residue; existing unrelated Docker resources like ``qdrant_data`` are untouched).

NOTE: Local Docker results do NOT substitute for Cloud Run IAM, cold start, CPU throttling,
or hosted Supabase PostgreSQL / Supavisor verification.
"""

import json
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

SPIKE_DIR = Path(__file__).resolve().parents[1]


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _free_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(cmd: list[str], *, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=SPIKE_DIR, check=check, capture_output=True, text=True, timeout=timeout)


def _wait_http_ok(url: str, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last_err = ""
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_err = repr(exc)
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}: {last_err}")


def _pg_snapshot_via_exec(pg_container: str) -> dict:
    sql = (
        "SELECT json_build_object("
        "'jobs', (SELECT coalesce(json_agg(json_build_object('id', id, 'status', status, 'attempts', attempt_count)), '[]'::json) FROM t007a_spike.jobs),"
        "'outbox', (SELECT coalesce(json_agg(json_build_object('job_id', job_id, 'status', status, 'dispatch_attempts', dispatch_attempts, 'recovery_attempts', recovery_attempts, 'last_error', last_error)), '[]'::json) FROM t007a_spike.outbox),"
        "'results_count', (SELECT count(*) FROM t007a_spike.results),"
        "'attempts_count', (SELECT count(*) FROM t007a_spike.job_attempts)"
        ")::text;"
    )
    out = _run(
        ["docker", "exec", pg_container, "psql", "-U", "t007a", "-d", "t007a_spike_db", "-tAc", sql]
    ).stdout.strip()
    return json.loads(out)


def test_dockerfile_single_image_api_and_worker_happy_and_stop_recover(evidence):
    if not _docker_ready():
        pytest.skip("LOCAL DOCKER ENGINE NOT AVAILABLE: container check left unexecuted")

    tag_id = secrets.token_hex(4)
    image_tag = f"t007a-r05-{tag_id}:local"
    net_name = f"t007a-r05-net-{tag_id}"
    pg_name = f"t007a-r05-pg-{tag_id}"
    worker_name = f"t007a-r05-worker-{tag_id}"
    api_name = f"t007a-r05-api-{tag_id}"
    api_host_port = _free_local_port()
    worker_host_port = _free_local_port()

    try:
        # 1. Build single prototype image from existing Dockerfile.
        _run(["docker", "build", "-q", "-t", image_tag, "."], timeout=300)
        _run(["docker", "network", "create", net_name])

        # 2. Start isolated ephemeral PostgreSQL (explicit PGDATA on tmpfs, no named volume, no host port).
        _run(
            [
                "docker", "run", "-d", "--name", pg_name,
                "--network", net_name,
                "--tmpfs", "/tmp/t007a-pgdata:rw,uid=70,gid=70",
                "-e", "PGDATA=/tmp/t007a-pgdata/data",
                "-e", "POSTGRES_USER=t007a",
                "-e", "POSTGRES_PASSWORD=t007a-local-throwaway",
                "-e", "POSTGRES_DB=t007a_spike_db",
                "postgres:18-alpine",
            ],
            timeout=180,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            ready = _run(
                ["docker", "exec", pg_name, "pg_isready", "-U", "t007a", "-d", "t007a_spike_db"],
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.3)
        else:
            logs = _run(["docker", "logs", pg_name], check=False).stdout[-500:]
            raise RuntimeError(f"isolated postgres container did not become ready: {logs}")

        dsn = f"postgresql://t007a:t007a-local-throwaway@{pg_name}:5432/t007a_spike_db"

        # Apply spike schema using the built image itself (one-off --rm container).
        _run(
            [
                "docker", "run", "--rm", "--network", net_name,
                "-e", f"T007A_DATABASE_URL={dsn}",
                image_tag,
                "python", "-c",
                "import os; from t007a.db import apply_schema; apply_schema(os.environ['T007A_DATABASE_URL'])",
            ]
        )

        # 3. Start Worker role and API role from the SAME image_tag.
        _run(
            [
                "docker", "run", "-d", "--name", worker_name,
                "--network", net_name,
                "-p", f"127.0.0.1:{worker_host_port}:8080",
                "-e", "T007A_ROLE=worker",
                "-e", "T007A_WORKER_ID=container-worker-1",
                "-e", f"T007A_DATABASE_URL={dsn}",
                image_tag,
            ]
        )
        _run(
            [
                "docker", "run", "-d", "--name", api_name,
                "--network", net_name,
                "-p", f"127.0.0.1:{api_host_port}:8080",
                "-e", "T007A_ROLE=api",
                "-e", f"T007A_DATABASE_URL={dsn}",
                "-e", f"T007A_WORKER_WAKE_URL=http://{worker_name}:8080/internal/t007a/wake",
                "-e", "T007A_WAKE_TIMEOUT_S=2",
                image_tag,
            ]
        )

        api_base = f"http://127.0.0.1:{api_host_port}"
        worker_base = f"http://127.0.0.1:{worker_host_port}"
        _wait_http_ok(f"{worker_base}/healthz")
        _wait_http_ok(f"{api_base}/healthz")

        # 4a. Happy path: submit -> DB commit -> container wake -> 1 result stored.
        r_happy = httpx.post(
            f"{api_base}/_prototype/t007a/jobs",
            json={"text": "container vertical happy path"},
            headers={"Idempotency-Key": "r05-container-happy-01"},
            timeout=5.0,
        )
        assert r_happy.status_code == 202
        happy_body = r_happy.json()
        assert happy_body["wake"] == "acknowledged"
        happy_job_id = happy_body["job_id"]

        st_happy = httpx.get(f"{api_base}{happy_body['status_url']}", timeout=5.0).json()
        assert st_happy["job"]["status"] == "SUCCEEDED"
        assert st_happy["result"]["worker_id"] == "container-worker-1"
        snap_happy = evidence("container_happy_path", _pg_snapshot_via_exec(pg_name))
        assert snap_happy["results_count"] == 1

        # 4b. Stop Worker container -> submit still commits job+outbox -> restart Worker -> recover.
        _run(["docker", "stop", "-t", "2", worker_name])
        r_outage = httpx.post(
            f"{api_base}/_prototype/t007a/jobs",
            json={"text": "submitted while worker container stopped"},
            headers={"Idempotency-Key": "r05-container-outage-01"},
            timeout=5.0,
        )
        assert r_outage.status_code == 202
        outage_body = r_outage.json()
        assert outage_body["wake"] == "failed"
        outage_job_id = outage_body["job_id"]

        st_during_outage = httpx.get(f"{api_base}{outage_body['status_url']}", timeout=5.0).json()
        assert st_during_outage["job"]["status"] == "PENDING"
        assert st_during_outage["outbox"]["status"] == "PENDING"
        assert st_during_outage["result"] is None
        snap_outage = evidence("during_worker_container_stopped", _pg_snapshot_via_exec(pg_name))
        assert snap_outage["results_count"] == 1  # only the earlier happy job has a result

        # Restart the SAME stopped worker container and trigger bounded recovery.
        _run(["docker", "start", worker_name])
        _wait_http_ok(f"{worker_base}/healthz")
        rec = httpx.post(
            f"{worker_base}/internal/t007a/recover",
            params={"limit": 5, "grace_s": 0},
            timeout=5.0,
        ).json()
        evidence("container_recover_response", rec)
        assert rec["selected"] == 1 and rec["acknowledged"] == 1
        assert list(rec["jobs"].keys()) == [outage_job_id]

        # Resubmit same Idempotency-Key + poll status: converges to same job_id & 1 result.
        r_replay = httpx.post(
            f"{api_base}/_prototype/t007a/jobs",
            json={"text": "submitted while worker container stopped"},
            headers={"Idempotency-Key": "r05-container-outage-01"},
            timeout=5.0,
        ).json()
        assert r_replay["job_id"] == outage_job_id and r_replay["replayed"] is True

        st_recovered = httpx.get(f"{api_base}{outage_body['status_url']}", timeout=5.0).json()
        assert st_recovered["job"]["status"] == "SUCCEEDED"
        assert st_recovered["outbox"]["status"] == "DONE"
        assert st_recovered["result"]["worker_id"] == "container-worker-1"

        snap_final = evidence("after_worker_container_restarted_and_recovered", _pg_snapshot_via_exec(pg_name))
        assert snap_final["results_count"] == 2  # 1 for happy_job_id + 1 for outage_job_id
        assert {j["id"]: j["status"] for j in snap_final["jobs"]} == {
            happy_job_id: "SUCCEEDED",
            outage_job_id: "SUCCEEDED",
        }
    finally:
        # -v removes any anonymous volume attached to pg_name ONLY; never touches named/other volumes.
        for cname in (api_name, worker_name, pg_name):
            _run(["docker", "rm", "-fv", cname], check=False)
        _run(["docker", "network", "rm", net_name], check=False)
        _run(["docker", "image", "rm", "-f", image_tag], check=False)

    # Verify zero residue for our t007a-r05-* prefix (safe even when sibling agents use Docker).
    rem_containers = _run(
        ["docker", "ps", "-a", "--filter", f"name=t007a-r05-{tag_id}", "--format", "{{.Names}}"]
    ).stdout.split()
    rem_nets = _run(
        ["docker", "network", "ls", "--filter", f"name=t007a-r05-net-{tag_id}", "--format", "{{.Name}}"]
    ).stdout.split()
    rem_imgs = _run(["docker", "images", "-q", image_tag]).stdout.split()
    rem_vols = _run(
        ["docker", "volume", "ls", "--filter", f"name=t007a-r05-", "--format", "{{.Name}}"]
    ).stdout.split()

    cleanup = {
        "remaining_run_containers": len(rem_containers),
        "remaining_run_networks": len(rem_nets),
        "remaining_run_images": len(rem_imgs),
        "remaining_r05_volumes": len(rem_vols),
    }
    evidence("docker_cleanup_verified", cleanup)
    assert cleanup == {
        "remaining_run_containers": 0,
        "remaining_run_networks": 0,
        "remaining_run_images": 0,
        "remaining_r05_volumes": 0,
    }
