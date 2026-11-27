"""RUNTIME-06 local linux/amd64 container build & single-job execution test.

Verifies Cloud Run target architecture (`linux/amd64`) packaging locally ONLY:
- Builds `t007a-r06-amd64-<run_id>:local` with `docker build --platform linux/amd64 --load`
  from `spikes/t007a-worker/Dockerfile` (no registry push, no Cloud Build, no tool install).
- Separately records image build/platform inspection (`linux/amd64`) and runtime execution.
- Starts both `T007A_ROLE=worker` and `T007A_ROLE=api` with `--platform linux/amd64` from the
  SAME `linux/amd64` image against an isolated tmpfs PostgreSQL container on a private bridge
  network, confirms in-container `x86_64` runtime architecture, and verifies 1 synthetic job
  converges (`202` -> wake `acknowledged` -> `SUCCEEDED` -> `results_count == 1`).
- Cleans up ONLY the `t007a-r06-*` containers, bridge network, and image tag created in this run.
"""

import json
import platform
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


def _run(cmd: list[str], *, check: bool = True, timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=SPIKE_DIR, check=check, capture_output=True, text=True, timeout=timeout)


def _wait_http_ok(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    last_err = ""
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.5)
            if r.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_err = repr(exc)
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {url}: {last_err}")


def _pg_snapshot_via_exec(pg_container: str) -> dict:
    sql = (
        "SELECT json_build_object("
        "'jobs', (SELECT coalesce(json_agg(json_build_object('id', id, 'status', status, 'attempts', attempt_count)), '[]'::json) FROM t007a_spike.jobs),"
        "'outbox', (SELECT coalesce(json_agg(json_build_object('job_id', job_id, 'status', status, 'dispatch_attempts', dispatch_attempts)), '[]'::json) FROM t007a_spike.outbox),"
        "'results_count', (SELECT count(*) FROM t007a_spike.results),"
        "'attempts_count', (SELECT count(*) FROM t007a_spike.job_attempts)"
        ")::text;"
    )
    out = _run(
        ["docker", "exec", pg_container, "psql", "-U", "t007a", "-d", "t007a_spike_db", "-tAc", sql]
    ).stdout.strip()
    return json.loads(out)


def test_dockerfile_linux_amd64_local_build_inspect_and_single_job(evidence):
    if not _docker_ready():
        pytest.skip("LOCAL DOCKER ENGINE NOT AVAILABLE: linux/amd64 check left unexecuted")

    tag_id = secrets.token_hex(4)
    image_tag = f"t007a-r06-amd64-{tag_id}:local"
    net_name = f"t007a-r06-net-{tag_id}"
    pg_name = f"t007a-r06-pg-{tag_id}"
    worker_name = f"t007a-r06-worker-{tag_id}"
    api_name = f"t007a-r06-api-{tag_id}"
    api_host_port = _free_local_port()
    worker_host_port = _free_local_port()

    try:
        # 1. Local-only build targeting Cloud Run's linux/amd64 platform (--load keeps it local).
        _run(
            ["docker", "build", "--platform", "linux/amd64", "--load", "-q", "-t", image_tag, "."],
            timeout=360,
        )
        inspected = _run(
            ["docker", "image", "inspect", image_tag, "--format", "{{.Os}}/{{.Architecture}}"]
        ).stdout.strip()
        build_info = evidence(
            "amd64_local_build",
            {
                "built": True,
                "host_machine": platform.machine(),
                "image_platform": inspected,
            },
        )
        assert build_info["image_platform"] == "linux/amd64"

        # 2. Separate check for whether local engine can execute linux/amd64 binaries.
        probe = _run(
            [
                "docker", "run", "--rm", "--platform", "linux/amd64", image_tag,
                "python", "-c", "import platform; print(platform.machine())",
            ],
            check=False,
            timeout=60,
        )
        probe_arch = probe.stdout.strip()
        if probe.returncode != 0:
            evidence(
                "amd64_local_runtime_probe",
                {
                    "runnable": False,
                    "returncode": probe.returncode,
                    "stderr": probe.stderr.strip()[:300],
                },
            )
            pytest.skip(
                f"linux/amd64 image built locally ({inspected}), but local execution unavailable: {probe.stderr.strip()[:160]}"
            )

        evidence("amd64_local_runtime_probe", {"runnable": True, "container_machine": probe_arch})
        assert probe_arch == "x86_64"

        # 3. Start isolated private bridge network + throwaway tmpfs PostgreSQL.
        _run(["docker", "network", "create", net_name])
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

        # Apply schema via the linux/amd64 image itself.
        _run(
            [
                "docker", "run", "--rm", "--platform", "linux/amd64", "--network", net_name,
                "-e", f"T007A_DATABASE_URL={dsn}",
                image_tag,
                "python", "-c",
                "import os; from t007a.db import apply_schema; apply_schema(os.environ['T007A_DATABASE_URL'])",
            ]
        )

        # 4. Run BOTH Worker and API roles as linux/amd64 from the SAME image.
        _run(
            [
                "docker", "run", "-d", "--platform", "linux/amd64", "--name", worker_name,
                "--network", net_name,
                "-p", f"127.0.0.1:{worker_host_port}:8080",
                "-e", "T007A_ROLE=worker",
                "-e", "T007A_WORKER_ID=amd64-worker-1",
                "-e", f"T007A_DATABASE_URL={dsn}",
                image_tag,
            ]
        )
        _run(
            [
                "docker", "run", "-d", "--platform", "linux/amd64", "--name", api_name,
                "--network", net_name,
                "-p", f"127.0.0.1:{api_host_port}:8080",
                "-e", "T007A_ROLE=api",
                "-e", f"T007A_DATABASE_URL={dsn}",
                "-e", f"T007A_WORKER_WAKE_URL=http://{worker_name}:8080/internal/t007a/wake",
                "-e", "T007A_WAKE_TIMEOUT_S=5",
                image_tag,
            ]
        )

        api_base = f"http://127.0.0.1:{api_host_port}"
        worker_base = f"http://127.0.0.1:{worker_host_port}"
        _wait_http_ok(f"{worker_base}/healthz")
        _wait_http_ok(f"{api_base}/healthz")

        worker_arch = _run(
            ["docker", "exec", worker_name, "python", "-c", "import platform; print(platform.machine())"]
        ).stdout.strip()
        api_arch = _run(
            ["docker", "exec", api_name, "python", "-c", "import platform; print(platform.machine())"]
        ).stdout.strip()
        assert worker_arch == "x86_64" and api_arch == "x86_64"

        # 5. Submit 1 synthetic job and verify full vertical convergence.
        r = httpx.post(
            f"{api_base}/_prototype/t007a/jobs",
            json={"text": "linux/amd64 single image synthetic job"},
            headers={"Idempotency-Key": "r06-amd64-single-job-01"},
            timeout=10.0,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["wake"] == "acknowledged"

        st = httpx.get(f"{api_base}{body['status_url']}", timeout=5.0).json()
        assert st["job"]["status"] == "SUCCEEDED"
        assert st["outbox"]["status"] == "DONE"
        assert st["result"]["worker_id"] == "amd64-worker-1"

        snap = evidence(
            "amd64_single_job_execution",
            {
                "worker_arch": worker_arch,
                "api_arch": api_arch,
                "job_id": body["job_id"],
                "wake": body["wake"],
                "db": _pg_snapshot_via_exec(pg_name),
            },
        )
        assert snap["db"]["results_count"] == 1
        assert snap["db"]["jobs"] == [{"id": body["job_id"], "status": "SUCCEEDED", "attempts": 1}]
    finally:
        for cname in (api_name, worker_name, pg_name):
            _run(["docker", "rm", "-fv", cname], check=False)
        _run(["docker", "network", "rm", net_name], check=False)
        _run(["docker", "image", "rm", "-f", image_tag], check=False)

    rem_containers = _run(
        ["docker", "ps", "-a", "--filter", f"name=t007a-r06-", "--format", "{{.Names}}"]
    ).stdout.split()
    rem_nets = _run(
        ["docker", "network", "ls", "--filter", f"name=t007a-r06-net-", "--format", "{{.Name}}"]
    ).stdout.split()
    rem_imgs = _run(["docker", "images", "-q", image_tag]).stdout.split()
    rem_vols = _run(
        ["docker", "volume", "ls", "--filter", "name=t007a-r06-", "--format", "{{.Name}}"]
    ).stdout.split()

    cleanup = evidence(
        "amd64_docker_cleanup_verified",
        {
            "remaining_r06_containers": len(rem_containers),
            "remaining_r06_networks": len(rem_nets),
            "remaining_r06_images": len(rem_imgs),
            "remaining_r06_volumes": len(rem_vols),
        },
    )
    assert cleanup == {
        "remaining_r06_containers": 0,
        "remaining_r06_networks": 0,
        "remaining_r06_images": 0,
        "remaining_r06_volumes": 0,
    }
