#!/usr/bin/env python3
"""One-command local fullstack launcher & smoke-check for Career Memory (PG18 + FastAPI + Next.js).

Usage:
    cd apps/api && uv run python ../../scripts/run_local_fullstack.py
    cd apps/api && uv run python ../../scripts/run_local_fullstack.py --smoke-check
    cd apps/api && uv run python ../../scripts/run_local_fullstack.py --github-mode live
    cd apps/api && uv run python ../../scripts/run_local_fullstack.py --pgdata-dir ~/.career-memory-pg18
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
API_SRC = REPO_ROOT / "apps" / "api" / "src"
WEB_DIR = REPO_ROOT / "apps" / "web"

if str(API_SRC) not in sys.path:
    sys.path.insert(0, str(API_SRC))

from career_memory_api.fullstack_runtime import (  # noqa: E402
    FullstackPostgresRuntime,
    FullstackRuntimeConfig,
    GitHubProviderMode,
    create_local_dev_fullstack_app,
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], str]:
    req_headers = dict(headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers=req_headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read().decode("utf-8", errors="replace")
            return status, resp_headers, body
    except urllib.error.HTTPError as err:
        status = int(err.code)
        resp_headers = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
        body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        return status, resp_headers, body


def _wait_http_ok(
    url: str,
    *,
    timeout_seconds: float = 45.0,
    label: str = "service",
    proc: subprocess.Popen[str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_err: str = ""
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"{label} exited prematurely with code {proc.returncode}."
            )
        try:
            status, _, _ = _http_request(url, timeout=3.0)
            if 200 <= status < 300:
                return
            last_err = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise RuntimeError(
        f"Timed out waiting for {label} at {url} ({last_err})."
    )


def _terminate_process_group(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001
        proc.terminate()

    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            proc.kill()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pass


def _start_web_dev_server(*, web_port: int, api_port: int) -> subprocess.Popen[str]:
    node_modules = WEB_DIR / "node_modules"
    if not node_modules.is_dir():
        raise RuntimeError(
            f"Missing {node_modules}. Run `cd apps/web && npm ci` first."
        )
    next_bin = node_modules / ".bin" / "next"
    if not next_bin.exists():
        raise RuntimeError(
            f"Missing Next.js binary at {next_bin}. Run `cd apps/web && npm ci` first."
        )
    env = os.environ.copy()
    env["NEXT_PUBLIC_API_MODE"] = "live"
    env["CAREER_MEMORY_API_ORIGIN"] = f"http://127.0.0.1:{api_port}"
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    return subprocess.Popen(
        [
            str(next_bin),
            "dev",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(web_port),
        ],
        cwd=str(WEB_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _run_smoke_check(*, web_base: str) -> None:
    print(f"[smoke] Verifying Same-Origin fullstack stack via {web_base} ...", flush=True)

    # 1. /healthz through Next.js rewrite proxy
    status, _, body = _http_request(f"{web_base}/healthz")
    assert status == 200 and json.loads(body).get("status") == "ok", (
        f"Unexpected /healthz response: {status} {body}"
    )
    print("[smoke] 1/10 /healthz OK", flush=True)

    # 2. /local-dev/bootstrap
    status, headers, body = _http_request(f"{web_base}/local-dev/bootstrap")
    assert status == 200, f"/local-dev/bootstrap failed: {status} {body}"
    assert "no-store" in headers.get("cache-control", "").lower(), (
        f"Expected private, no-store on /local-dev/bootstrap, got {headers}"
    )
    boot = json.loads(body)
    token = str(boot["access_token"])
    ws_id = str(boot["workspace_id"])
    default_event_id = str(boot["default_confirmed_event_id"])
    resources = boot["resources"]
    assert len(resources) >= 2, f"Expected >= 2 sample resources, got {len(resources)}"
    auth_headers = {
        "Authorization": f"Bearer {token}",
        "X-Workspace-ID": ws_id,
    }
    print(
        f"[smoke] 2/10 /local-dev/bootstrap OK (mode={boot['mode']}, resources={len(resources)})",
        flush=True,
    )

    # 3. /v1/work-events
    status, _, body = _http_request(
        f"{web_base}/v1/work-events?limit=50", headers=auth_headers
    )
    assert status == 200, f"GET /v1/work-events failed: {status} {body}"
    events_data = json.loads(body)
    assert len(events_data.get("items", [])) >= 3, f"Expected >=3 work events: {events_data}"
    print(
        f"[smoke] 3/10 /v1/work-events OK ({len(events_data['items'])} events)",
        flush=True,
    )

    # 4. /v1/facts & /v1/claims & Plan C /v1/content-units/{id}/content
    status, _, body = _http_request(
        f"{web_base}/v1/facts?event_id={urllib.parse.quote(default_event_id)}",
        headers=auth_headers,
    )
    assert status == 200, f"GET /v1/facts failed: {status} {body}"
    facts_data = json.loads(body)
    assert len(facts_data.get("items", [])) >= 1, f"Expected >=1 fact for event: {facts_data}"
    fact_id = str(facts_data["items"][0]["fact_id"])

    status, _, body = _http_request(
        f"{web_base}/v1/facts/{urllib.parse.quote(fact_id)}/supports",
        headers=auth_headers,
    )
    assert status == 200, f"GET /v1/facts/{{id}}/supports failed: {status} {body}"
    supports_data = json.loads(body)
    content_unit_id = str(supports_data["supports"][0]["content_unit_id"])

    status, cu_headers, body = _http_request(
        f"{web_base}/v1/content-units/{urllib.parse.quote(content_unit_id)}/content",
        headers=auth_headers,
    )
    assert status == 200, f"GET /v1/content-units/{{id}}/content failed: {status} {body}"
    assert "no-store" in cu_headers.get("cache-control", "").lower()
    cu_data = json.loads(body)
    assert cu_data.get("source_resolution", {}).get("status") == "MATCHED"
    assert isinstance(cu_data.get("content"), str) and len(cu_data["content"]) > 10

    status, _, body = _http_request(
        f"{web_base}/v1/claims?event_id={urllib.parse.quote(default_event_id)}",
        headers=auth_headers,
    )
    assert status == 200, f"GET /v1/claims failed: {status} {body}"
    claims_data = json.loads(body)
    assert len(claims_data.get("items", [])) >= 1, f"Expected >=1 claim: {claims_data}"
    claim_item = claims_data["items"][0]
    claim_id = str(claim_item["claim_id"])
    claim_version = int(claim_item["version"])
    print(
        f"[smoke] 4/10 /v1/facts, /v1/claims & Plan C /v1/content-units/{{id}}/content MATCHED OK",
        flush=True,
    )

    # 5. Approve claim (if not yet approved) + Create Zero-Quote Export via /v1/exports
    if claim_item.get("status") != "APPROVED":
        status, _, body = _http_request(
            f"{web_base}/v1/claims/{urllib.parse.quote(claim_id)}/actions/approve",
            method="POST",
            headers={
                **auth_headers,
                "Idempotency-Key": str(uuid.uuid4()),
                "If-Match": f'"v{claim_version}"',
            },
            json_body={"expected_version": claim_version},
        )
        assert status == 200, f"POST /v1/claims/{{id}}/actions/approve failed: {status} {body}"

    status, _, body = _http_request(
        f"{web_base}/v1/exports",
        method="POST",
        headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
        json_body={
            "claim_ids": [claim_id],
            "format": "MARKDOWN",
            "export_policy": "SOURCE_VERIFIED_ONLY",
        },
    )
    assert status == 201, f"POST /v1/exports failed: {status} {body}"
    export_data = json.loads(body)
    assert export_data.get("status") == "READY"
    assert export_data.get("manifest", {}).get("excludes_raw_source_quotes") is True
    print("[smoke] 5/10 /v1/exports zero-quote READY OK", flush=True)

    # 6. /v1/search & /v1/quota-status
    status, _, body = _http_request(
        f"{web_base}/v1/search?q=webhook", headers=auth_headers
    )
    assert status == 200, f"GET /v1/search failed: {status} {body}"
    search_data = json.loads(body)
    assert len(search_data.get("items", [])) >= 1, f"Expected >=1 search hit: {search_data}"

    status, _, body = _http_request(
        f"{web_base}/v1/quota-status", headers=auth_headers
    )
    assert status == 200, f"GET /v1/quota-status failed: {status} {body}"
    quota_data = json.loads(body)
    assert quota_data.get("paid_fallback_allowed") is False
    print(
        f"[smoke] 6/10 /v1/search ({len(search_data['items'])} hits) & /v1/quota-status OK",
        flush=True,
    )

    # 7. Trigger live POST /v1/sync-runs on seoyeon-dev/search-gateway & await background worker SUCCESS
    sg_res = next(
        (r for r in resources if r["repository_full_name"] == "seoyeon-dev/search-gateway"),
        None,
    )
    assert sg_res is not None, "seoyeon-dev/search-gateway not found in bootstrap resources"
    status, _, body = _http_request(
        f"{web_base}/v1/sync-runs",
        method="POST",
        headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
        json_body={
            "source_resource_id": sg_res["source_resource_id"],
            "mode": "INITIAL_BACKFILL",
        },
    )
    assert status == 202, f"POST /v1/sync-runs failed: {status} {body}"
    accepted = json.loads(body)
    status_url = str(accepted["status_url"])

    deadline = time.monotonic() + 30.0
    final_sync: dict[str, Any] = {}
    while time.monotonic() < deadline:
        st, _, st_body = _http_request(f"{web_base}{status_url}", headers=auth_headers)
        assert st == 200, f"GET {status_url} failed: {st} {st_body}"
        final_sync = json.loads(st_body)
        if final_sync.get("status") == "SUCCESS" and final_sync.get("processing_status") == "COMPLETE":
            break
        if final_sync.get("status") in ("FAILED", "CANCELED", "PARTIAL", "PARTIAL_WITH_LIMIT"):
            raise AssertionError(f"Unexpected terminal SyncRun status: {final_sync}")
        time.sleep(0.2)
    else:
        raise AssertionError(f"Timed out waiting for background SyncRun SUCCESS: {final_sync}")
    assert final_sync.get("import_ready") is True
    print(
        "[smoke] 7/10 Live POST /v1/sync-runs (seoyeon-dev/search-gateway) -> worker SUCCESS/COMPLETE OK",
        flush=True,
    )

    # 8. Verify newly imported seoyeon-dev/search-gateway appears in /v1/work-events
    status, _, body = _http_request(
        f"{web_base}/v1/work-events?source_resource_id={urllib.parse.quote(sg_res['source_resource_id'])}",
        headers=auth_headers,
    )
    assert status == 200, f"GET /v1/work-events for search-gateway failed: {status} {body}"
    sg_events = json.loads(body)
    assert len(sg_events.get("items", [])) >= 2, f"Expected >=2 events for search-gateway: {sg_events}"
    print(
        f"[smoke] 8/10 Newly imported search-gateway WorkEvents verified ({len(sg_events['items'])} events)",
        flush=True,
    )

    # 9-10. Verify SSR HTML renders cleanly on all 4 Next.js routes
    for route_path in ("/", "/event-review", "/claim-review", "/search"):
        st, hdrs, html = _http_request(f"{web_base}{route_path}", timeout=45.0)
        assert st == 200, f"GET {route_path} returned HTTP {st}"
        assert "text/html" in hdrs.get("content-type", "").lower(), (
            f"Expected text/html on {route_path}, got {hdrs}"
        )
        assert "<html" in html.lower(), f"Missing <html> tag on {route_path}"
    print(
        "[smoke] 9-10/10 SSR HTML verified across /, /event-review, /claim-review, /search OK",
        flush=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch Career Memory local fullstack runtime (PG18 + FastAPI + Worker + Next.js)."
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=8000,
        help="Localhost port for FastAPI (default: 8000; pass 0 for auto free port).",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=3100,
        help="Localhost port for Next.js (default: 3100; pass 0 for auto free port).",
    )
    parser.add_argument(
        "--github-mode",
        choices=("sample", "live"),
        default=None,
        help="GitHub provider mode: 'sample' (default, 0 external calls) or 'live' (public GitHub API).",
    )
    parser.add_argument(
        "--pgdata-dir",
        type=Path,
        default=None,
        help="Optional persistent local PG18 data directory (omit for clean ephemeral cluster).",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.environ.get("DATABASE_URL"),
        help="Optional existing PostgreSQL 18 DATABASE_URL.",
    )
    parser.add_argument(
        "--empty-workspace",
        action="store_true",
        help="Do not pre-seed sample repositories/imports on startup.",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Start only PG18 + FastAPI + background worker without Next.js.",
    )
    parser.add_argument(
        "--smoke-check",
        action="store_true",
        help="Boot full stack, run Same-Origin E2E HTTP & SSR verification, tear down cleanly, and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = GitHubProviderMode.from_cli_or_env(
        args.github_mode or os.environ.get("CAREER_MEMORY_GITHUB_PROVIDER_MODE")
    )
    api_port = _find_free_port() if args.api_port == 0 else int(args.api_port)
    web_port = _find_free_port() if args.web_port == 0 else int(args.web_port)

    config = FullstackRuntimeConfig(
        github_mode=mode,
        database_url=args.database_url,
        pgdata_dir=args.pgdata_dir,
        repo_root=REPO_ROOT,
        empty_workspace=bool(args.empty_workspace),
    )

    runtime = FullstackPostgresRuntime(config=config)
    web_proc: subprocess.Popen[str] | None = None
    uvicorn_server: uvicorn.Server | None = None
    uvicorn_thread: threading.Thread | None = None
    stop_requested = threading.Event()

    def _on_signal(_signum: int, _frame: Any) -> None:
        stop_requested.set()
        if uvicorn_server is not None:
            uvicorn_server.should_exit = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        print(f"[fullstack] Starting PostgreSQL 18 runtime (mode={mode.value}) ...", flush=True)
        runtime.start()
        app = create_local_dev_fullstack_app(runtime)
        print(
            f"[fullstack] PostgreSQL {runtime.db.pg_version_label} ready "
            f"(workspace_id={runtime.workspace_id})",
            flush=True,
        )

        uv_config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=api_port,
            log_level="warning",
            access_log=False,
        )
        uvicorn_server = uvicorn.Server(uv_config)
        uvicorn_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        uvicorn_thread = threading.Thread(
            target=uvicorn_server.run,
            name="cm-uvicorn-server",
            daemon=True,
        )
        uvicorn_thread.start()

        _wait_http_ok(
            f"http://127.0.0.1:{api_port}/healthz",
            timeout_seconds=20.0,
            label="FastAPI server",
        )
        print(f"[fullstack] FastAPI ready at http://127.0.0.1:{api_port}", flush=True)

        if not args.api_only:
            print(
                f"[fullstack] Starting Next.js web app (NEXT_PUBLIC_API_MODE=live) on http://127.0.0.1:{web_port} ...",
                flush=True,
            )
            web_proc = _start_web_dev_server(web_port=web_port, api_port=api_port)
            _wait_http_ok(
                f"http://127.0.0.1:{web_port}/healthz",
                timeout_seconds=45.0,
                label="Next.js Same-Origin proxy",
                proc=web_proc,
            )
            print(
                f"[fullstack] Career Memory Web ready at http://127.0.0.1:{web_port}",
                flush=True,
            )

        if args.smoke_check:
            if args.api_only:
                raise RuntimeError("--smoke-check requires Next.js (do not combine with --api-only).")
            _run_smoke_check(web_base=f"http://127.0.0.1:{web_port}")
            print("[smoke] ALL FULLSTACK SMOKE CHECKS PASSED.", flush=True)
            return 0

        print(
            "[fullstack] Running. Press Ctrl+C to stop cleanly.\n"
            f"  Web UI : http://127.0.0.1:{web_port} (Import / Event Review / Claim Review / Search)\n"
            f"  API    : http://127.0.0.1:{api_port}/healthz",
            flush=True,
        )
        while not stop_requested.is_set():
            if web_proc is not None and web_proc.poll() is not None:
                raise RuntimeError(f"Next.js dev server exited unexpectedly ({web_proc.returncode}).")
            if uvicorn_thread is not None and not uvicorn_thread.is_alive():
                raise RuntimeError("FastAPI uvicorn server stopped unexpectedly.")
            time.sleep(0.25)
        return 0
    finally:
        print("[fullstack] Shutting down Next.js, FastAPI, Worker, and PG18 ...", flush=True)
        _terminate_process_group(web_proc)
        if uvicorn_server is not None:
            uvicorn_server.should_exit = True
        if uvicorn_thread is not None and uvicorn_thread.is_alive():
            uvicorn_thread.join(timeout=5.0)
        runtime.stop()
        print("[fullstack] Clean shutdown complete.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
