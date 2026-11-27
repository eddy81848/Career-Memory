"""Local verification tests for the DB-free Cloud Run IAM probe candidate.

Uses Python standard library only (no external packages, no DB, no network calls
outside loopback, no GCP calls).
"""

from __future__ import annotations

import ast
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
PROBE_SCRIPT = PROBE_DIR / "probe.py"
DOCKERFILE = PROBE_DIR / "Dockerfile"

# Ensure local module import works when run from any working directory.
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import probe  # noqa: E402


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw
    finally:
        conn.close()


def _wait_ready(port: int, proc: subprocess.Popen[str], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2.0)
            raise AssertionError(
                f"probe exited early with code {proc.returncode}: stdout={out!r} stderr={err!r}"
            )
        try:
            status, _, _ = _request(port, "GET", "/healthz")
            if status == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("timed out waiting for local probe /healthz")


class HostedIamProbeLocalTest(unittest.TestCase):
    def _spawn_probe(self, env: dict[str, str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-B", str(PROBE_SCRIPT)],
            cwd=str(PROBE_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _stop_probe(self, proc: subprocess.Popen[str]) -> tuple[int, str, str]:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=2.0)
            self.fail("probe did not exit cleanly on SIGTERM")
        return proc.returncode, stdout, stderr

    def test_healthz_succeeds_without_db_env_vars(self) -> None:
        port = _free_local_port()
        minimal_env = {
            "PATH": os.environ.get("PATH", ""),
            "PORT": str(port),
        }
        # Explicitly confirm no DB variables are present in the probe env.
        for forbidden in ("T007A_DATABASE_URL", "DATABASE_URL", "PGHOST", "SUPABASE_URL"):
            self.assertNotIn(forbidden, minimal_env)

        proc = self._spawn_probe(minimal_env)
        try:
            _wait_ready(port, proc)
            status, headers, body = _request(port, "GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("content-type"), "application/json")
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(headers.get("x-t007f-probe"), "db-free-iam-probe")
            self.assertNotIn("python", headers.get("server", "").lower())
            self.assertEqual(body, probe.HEALTHZ_BODY)
            self.assertEqual(json.loads(body.decode("utf-8")), {"ok": True, "probe": "t007f-iam-probe"})
            self.assertLess(len(body), 64)
        finally:
            code, stdout, stderr = self._stop_probe(proc)

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn('"event":"probe_started"', stderr)
        self.assertIn('"event":"probe_stopped"', stderr)

    def test_other_paths_and_mutating_requests_rejected(self) -> None:
        port = _free_local_port()
        proc = self._spawn_probe({"PATH": os.environ.get("PATH", ""), "PORT": str(port)})
        try:
            _wait_ready(port, proc)

            # Non-/healthz GET paths must fail closed with 404.
            disallowed_get_paths = (
                "/",
                "/health",
                "/healthz/",
                "/healthz/extra",
                "/healthz?probe=1",
                "//healthz",
                "/internal/t007a/wake",
                "/internal/t007a/recover",
                "/_prototype/t007a/jobs",
                "/metrics",
                "/token",
            )
            for path in disallowed_get_paths:
                status, _, body = _request(port, "GET", path)
                self.assertEqual(status, 404, f"expected 404 for GET {path}")
                self.assertEqual(body, probe.NOT_FOUND_BODY)

            # Mutating / non-GET methods on /healthz must return 405 + Allow: GET.
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
                status, headers, body = _request(
                    port,
                    method,
                    "/healthz",
                    headers={"Content-Type": "application/json"},
                    body=b'{"mutate":true}',
                )
                self.assertEqual(status, 405, f"expected 405 for {method} /healthz")
                self.assertEqual(headers.get("allow"), "GET")
                self.assertEqual(body, probe.METHOD_NOT_ALLOWED_BODY)

            # HEAD /healthz must also return 405 with empty body.
            status, headers, body = _request(port, "HEAD", "/healthz")
            self.assertEqual(status, 405)
            self.assertEqual(headers.get("allow"), "GET")
            self.assertEqual(body, b"")

            # Mutating methods on Worker/API/other paths must fail closed with 404.
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                for path in (
                    "/internal/t007a/wake",
                    "/internal/t007a/recover",
                    "/_prototype/t007a/jobs",
                    "/",
                ):
                    status, _, body = _request(
                        port,
                        method,
                        path,
                        headers={"Content-Type": "application/json"},
                        body=b'{"job_id":"00000000-0000-0000-0000-000000000000"}',
                    )
                    self.assertEqual(status, 404, f"expected 404 for {method} {path}")
                    self.assertEqual(body, probe.NOT_FOUND_BODY)

            # Unknown HTTP method must fail closed without echoing method name.
            status, _, body = _request(port, "PURGE", "/healthz")
            self.assertEqual(status, 501)
            self.assertEqual(body, probe.ERROR_BODY)
            self.assertNotIn(b"PURGE", body)
        finally:
            code, _, _ = self._stop_probe(proc)
        self.assertEqual(code, 0)

    def test_no_credentials_env_vars_or_internal_config_leaked(self) -> None:
        port = _free_local_port()
        canaries = [
            "canary-db-password-991",
            "postgresql://canary_user:canary-db-password-991@db.invalid:5432/canary_db",
            "/tmp/canary-gcp-sa-key.json",
            "canary-api-secret-773",
            "canary-bearer-jwt-884",
            "canary-cookie-552",
            "canary-query-token-663",
            "canary-path-secret-441",
            "canary-body-secret-330",
            str(port),
            "T007A_DATABASE_URL",
        ]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PORT": str(port),
            "T007A_DATABASE_URL": "postgresql://canary_user:canary-db-password-991@db.invalid:5432/canary_db",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/canary-gcp-sa-key.json",
            "INTERNAL_SECRET_TOKEN": "canary-api-secret-773",
        }

        proc = self._spawn_probe(env)
        collected_responses: list[str] = []
        try:
            _wait_ready(port, proc)
            requests_to_send = [
                (
                    "GET",
                    "/healthz",
                    {
                        "Authorization": "Bearer canary-bearer-jwt-884",
                        "Cookie": "sid=canary-cookie-552",
                    },
                    None,
                ),
                (
                    "GET",
                    "/healthz?token=canary-query-token-663",
                    {"Authorization": "Bearer canary-bearer-jwt-884"},
                    None,
                ),
                (
                    "GET",
                    "/canary-path-secret-441",
                    {},
                    None,
                ),
                (
                    "POST",
                    "/healthz",
                    {"Authorization": "Bearer canary-bearer-jwt-884"},
                    b'{"secret":"canary-body-secret-330"}',
                ),
                (
                    "POST",
                    "/internal/t007a/wake",
                    {"Authorization": "Bearer canary-bearer-jwt-884"},
                    b'{"secret":"canary-body-secret-330"}',
                ),
            ]
            for method, path, headers, body in requests_to_send:
                _, resp_headers, resp_body = _request(port, method, path, headers=headers, body=body)
                collected_responses.append(json.dumps(resp_headers) + resp_body.decode("utf-8"))
        finally:
            code, stdout, stderr = self._stop_probe(proc)

        self.assertEqual(code, 0)
        all_output = "\n".join(collected_responses) + "\n" + stdout + "\n" + stderr
        for canary in canaries:
            self.assertNotIn(canary, all_output, f"unexpected leak of {canary!r}")

    def test_no_iam_simulation_and_stdlib_only_imports(self) -> None:
        source = PROBE_SCRIPT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        allowed_stdlib = set(sys.stdlib_module_names)
        self.assertTrue(
            imported_roots.issubset(allowed_stdlib),
            f"non-stdlib imports found: {sorted(imported_roots - allowed_stdlib)}",
        )
        for forbidden_mod in ("psycopg", "fastapi", "uvicorn", "httpx", "requests", "google", "jwt", "t007a"):
            self.assertNotIn(forbidden_mod, imported_roots)

        # Ensure executable code (excluding docstrings) does not inspect
        # Authorization headers, read DB env vars, or simulate IAM.
        docstring_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                ):
                    docstring_ids.add(id(node.body[0].value))

        code_strings = [
            node.value.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ]
        all_code_strings = "\n".join(code_strings)
        self.assertNotIn("authorization", all_code_strings)
        self.assertNotIn("t007a_database_url", all_code_strings)
        self.assertNotIn("bearer", all_code_strings)

        # Runtime check: GET /healthz returns the same fixed response with or
        # without an Authorization header (no in-app IAM simulation).
        port = _free_local_port()
        proc = self._spawn_probe({"PATH": os.environ.get("PATH", ""), "PORT": str(port)})
        try:
            _wait_ready(port, proc)
            st1, _, b1 = _request(port, "GET", "/healthz")
            st2, _, b2 = _request(
                port,
                "GET",
                "/healthz",
                headers={"Authorization": "Bearer not-verified-in-app"},
            )
            self.assertEqual((st1, b1), (200, probe.HEALTHZ_BODY))
            self.assertEqual((st2, b2), (200, probe.HEALTHZ_BODY))
        finally:
            code, _, _ = self._stop_probe(proc)
        self.assertEqual(code, 0)

    def test_invalid_port_env_fails_closed_without_echoing_value(self) -> None:
        for bad_port in ("not-a-port-canary-123", "0", "65536", "-1", "80_80", "+8080", " 8080 "):
            proc = self._spawn_probe({"PATH": os.environ.get("PATH", ""), "PORT": bad_port})
            stdout, stderr = proc.communicate(timeout=5.0)
            self.assertNotEqual(proc.returncode, 0, f"expected failure for PORT={bad_port!r}")
            self.assertNotIn(bad_port, stdout + stderr)
            self.assertIn('"reason":"invalid_port"', stderr)

    def test_dockerfile_candidate_contract(self) -> None:
        raw_lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
        instructions = "\n".join(
            line.strip() for line in raw_lines if line.strip() and not line.strip().startswith("#")
        )
        self.assertIn(
            "FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b",
            instructions,
        )
        self.assertIn("ENV PORT=8080", instructions)
        self.assertIn("USER nobody", instructions)
        self.assertIn('CMD ["python", "-B", "/app/probe.py"]', instructions)
        for forbidden in (
            "pip install",
            "apt-get",
            "requirements.txt",
            "T007A_DATABASE_URL",
            "t007a",
            "wake",
            "recover",
        ):
            self.assertNotIn(forbidden, instructions)


if __name__ == "__main__":
    unittest.main()
