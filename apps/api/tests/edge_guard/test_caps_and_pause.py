"""Tests for runtime caps (body size, concurrency, rate limit, timeout) and manual pause/resume switch (T-507 / ADR-020 / OD-015)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from career_memory_api.edge_guard import (
    CorsCsrfConfig,
    EdgeDenialCode,
    EdgeGuardConfig,
    EdgeRuntimeCaps,
    EdgeRuntimeMode,
    ManualEdgePauseController,
    ManualResumeRejectedError,
    OriginAuthConfig,
    PRIVATE_NO_STORE_CACHE_CONTROL,
    install_edge_guard,
)

ORIGIN_SECRET = "cm-cap-test-origin-secret"
ALLOWED_ORIGIN = "https://app.career-memory.example"


class FakeMonotonicClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeUtcClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _raw_asgi_request(
    app: FastAPI,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body_chunks: list[bytes] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("203.0.113.5", 44321),
        "server": ("api.career-memory.example", 443),
    }
    chunks = list(body_chunks) if body_chunks is not None else [b""]
    idx = 0

    async def receive() -> dict[str, Any]:
        nonlocal idx
        if idx < len(chunks):
            data = chunks[idx]
            idx += 1
            return {
                "type": "http.request",
                "body": data,
                "more_body": idx < len(chunks),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    status_code = 0
    resp_headers: dict[str, str] = {}
    resp_body = bytearray()

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            for k, v in message.get("headers", ()):
                resp_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            resp_body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return status_code, resp_headers, bytes(resp_body)


def test_request_body_size_cap_rejects_content_length_and_streamed_chunks_before_handler() -> None:
    handler_calls = 0
    app = FastAPI()

    @app.post("/v1/exports")
    async def create_export(request: Request) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        data = await request.body()
        return {"bytes": len(data)}

    cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(ORIGIN_SECRET,),
        ),
        cors_csrf=CorsCsrfConfig(allowed_web_origins=frozenset({ALLOWED_ORIGIN})),
        caps=EdgeRuntimeCaps(max_request_body_bytes=64),
    )
    install_edge_guard(app, cfg)

    headers = {"X-CM-Origin-Auth": ORIGIN_SECRET, "Origin": ALLOWED_ORIGIN}

    # 1. Exact boundary (64 bytes) succeeds
    status_ok, _, body_ok = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/v1/exports",
            headers={**headers, "Content-Length": "64"},
            body_chunks=[b"x" * 64],
        )
    )
    assert status_ok == 200
    assert json.loads(body_ok) == {"bytes": 64}
    assert handler_calls == 1

    # 2. Declared Content-Length > 64 rejected BEFORE handler invocation
    status_cl, resp_headers_cl, body_cl = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/v1/exports",
            headers={**headers, "Content-Length": "65"},
            body_chunks=[b"x" * 65],
        )
    )
    assert status_cl == 413
    assert json.loads(body_cl)["error"]["code"] == EdgeDenialCode.REQUEST_BODY_TOO_LARGE.value
    assert resp_headers_cl["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL
    assert handler_calls == 1  # handler was NOT entered!

    # 3. Streamed/chunked body without Content-Length exceeding 64 bytes rejected mid-stream
    status_stream, resp_headers_stream, body_stream = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/v1/exports",
            headers=headers,
            body_chunks=[b"a" * 30, b"b" * 30, b"c" * 10],
        )
    )
    assert status_stream == 413
    assert (
        json.loads(body_stream)["error"]["code"]
        == EdgeDenialCode.REQUEST_BODY_TOO_LARGE.value
    )
    assert resp_headers_stream["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL


def test_concurrency_cap_returns_503_with_retry_after_without_queueing() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    app = FastAPI()

    @app.get("/v1/work-events")
    async def slow_list() -> dict[str, str]:
        entered.set()
        await release.wait()
        return {"items": "ok"}

    cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(ORIGIN_SECRET,),
        ),
        caps=EdgeRuntimeCaps(
            max_concurrent_requests=1,
            retry_after_concurrency_seconds=7,
        ),
    )
    runtime = install_edge_guard(app, cfg)

    async def scenario() -> None:
        hdrs = {"X-CM-Origin-Auth": ORIGIN_SECRET}
        first_task = asyncio.create_task(
            _raw_asgi_request(app, method="GET", path="/v1/work-events", headers=hdrs)
        )
        await entered.wait()

        # Second concurrent request immediately fails with 503 CONCURRENCY_CAP_REACHED
        status_2, headers_2, body_2 = await _raw_asgi_request(
            app, method="GET", path="/v1/work-events", headers=hdrs
        )
        assert status_2 == 503
        parsed_2 = json.loads(body_2)
        assert parsed_2["error"]["code"] == EdgeDenialCode.CONCURRENCY_CAP_REACHED.value
        assert parsed_2["error"]["paid_fallback_allowed"] is False
        assert len(parsed_2["error"]["free_alternatives"]) >= 3
        assert headers_2["retry-after"] == "7"
        assert headers_2["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL

        # Release first request and verify slot cleanup
        release.set()
        status_1, _, _ = await first_task
        assert status_1 == 200
        assert runtime.concurrency_guard.in_flight == 0

    asyncio.run(scenario())


def test_sliding_window_rate_limit_with_injectable_clock_and_actor_isolation() -> None:
    clock = FakeMonotonicClock()
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/work-events")
    async def list_events() -> dict[str, str]:
        return {"ok": "true"}

    cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(ORIGIN_SECRET,),
        ),
        caps=EdgeRuntimeCaps(rate_limit_per_window=2, rate_window_seconds=30.0),
    )
    install_edge_guard(app, cfg, monotonic_clock=clock)

    actor_a = {
        "X-CM-Origin-Auth": ORIGIN_SECRET,
        "X-Workspace-ID": "ws-a",
        "Authorization": "Bearer actor-a-token",
    }
    actor_b = {
        "X-CM-Origin-Auth": ORIGIN_SECRET,
        "X-Workspace-ID": "ws-b",
        "Authorization": "Bearer actor-b-token",
    }

    with TestClient(app) as client:
        assert client.get("/v1/work-events", headers=actor_a).status_code == 200
        clock.advance(5.0)
        assert client.get("/v1/work-events", headers=actor_a).status_code == 200

        # 3rd request within 30s window -> 429 RATE_LIMIT_EXCEEDED
        r_limited = client.get("/v1/work-events", headers=actor_a)
        assert r_limited.status_code == 429
        assert r_limited.json()["error"]["code"] == EdgeDenialCode.RATE_LIMIT_EXCEEDED.value
        assert int(r_limited.headers["retry-after"]) == 25
        assert r_limited.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL
        assert "actor-a-token" not in r_limited.text

        # Actor B has an isolated bucket and still succeeds
        assert client.get("/v1/work-events", headers=actor_b).status_code == 200

        # /healthz is never blocked by user rate limits
        for _ in range(5):
            assert client.get("/healthz").status_code == 200

        # Advance clock past the 30s window -> Actor A allowed again
        clock.advance(26.0)
        assert client.get("/v1/work-events", headers=actor_a).status_code == 200


def test_request_slice_timeout_returns_504_and_releases_concurrency_slot() -> None:
    app = FastAPI()

    @app.get("/v1/work-events")
    async def hang_route() -> dict[str, str]:
        await asyncio.sleep(2.0)
        return {"never": "reached"}

    cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(ORIGIN_SECRET,),
        ),
        caps=EdgeRuntimeCaps(
            api_request_slice_timeout_seconds=0.05,
            max_concurrent_requests=1,
        ),
    )
    runtime = install_edge_guard(app, cfg)

    status, headers, body = asyncio.run(
        _raw_asgi_request(
            app,
            method="GET",
            path="/v1/work-events",
            headers={"X-CM-Origin-Auth": ORIGIN_SECRET},
        )
    )
    assert status == 504
    parsed = json.loads(body)
    assert parsed["error"]["code"] == EdgeDenialCode.REQUEST_SLICE_TIMEOUT.value
    assert parsed["error"]["paid_fallback_allowed"] is False
    assert headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL
    assert runtime.concurrency_guard.in_flight == 0


def test_manual_pause_cost_guard_blocks_growth_routes_preserves_safety_and_requires_budget_verified_resume() -> None:
    utc_clock = FakeUtcClock()
    controller = ManualEdgePauseController(clock=utc_clock)
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sync-runs")
    async def start_sync() -> dict[str, str]:
        return {"route": "sync-runs"}

    @app.post("/v1/sync-runs/{run_id}/retry")
    async def retry_sync(run_id: str) -> dict[str, str]:
        return {"route": "retry", "id": run_id}

    @app.post("/v1/work-events/{event_id}/claims:generate")
    async def generate_claims(event_id: str) -> dict[str, str]:
        return {"route": "claims:generate", "id": event_id}

    @app.post("/v1/exports")
    async def create_export() -> dict[str, str]:
        return {"route": "exports"}

    @app.post("/v1/source-accounts/{account_id}/disconnect")
    async def disconnect_account(account_id: str) -> dict[str, str]:
        return {"route": "disconnect", "id": account_id}

    @app.post("/v1/source-resources/{resource_id}/deletion-preview")
    async def deletion_preview(resource_id: str) -> dict[str, str]:
        return {"route": "deletion-preview", "id": resource_id}

    @app.post("/v1/source-resources/{resource_id}/deletion-confirm")
    async def deletion_confirm(resource_id: str) -> dict[str, str]:
        return {"route": "deletion-confirm", "id": resource_id}

    @app.get("/v1/deletion-jobs/{job_id}")
    async def deletion_job(job_id: str) -> dict[str, str]:
        return {"route": "deletion-job", "id": job_id}

    cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(ORIGIN_SECRET,),
        ),
        cors_csrf=CorsCsrfConfig(allowed_web_origins=frozenset({ALLOWED_ORIGIN})),
        caps=EdgeRuntimeCaps(retry_after_paused_seconds=600),
    )
    install_edge_guard(app, cfg, controller=controller)

    # Engage manual cost-guard pause (also test secret redaction in reason_code).
    paused_state = controller.pause(
        reason_code="OD015_SPEND_SPIKE Authorization: Bearer secret_leak_123",
        actor_id="operator-1",
        mode=EdgeRuntimeMode.PAUSED_COST_GUARD,
    )
    assert paused_state.mode is EdgeRuntimeMode.PAUSED_COST_GUARD
    assert "secret_leak_123" not in (paused_state.reason_code or "")

    # resume(budget_verified=False) MUST fail closed and leave pause active!
    with pytest.raises(ManualResumeRejectedError):
        controller.resume(actor_id="operator-1", budget_verified=False)
    assert controller.state.mode is EdgeRuntimeMode.PAUSED_COST_GUARD

    api_headers = {
        "X-CM-Origin-Auth": ORIGIN_SECRET,
        "Origin": ALLOWED_ORIGIN,
        "Sec-Fetch-Site": "same-origin",
    }

    with TestClient(app) as client:
        # High-cost / mutable growth routes MUST be blocked with 503 + Retry-After + free alternatives
        for path in (
            "/v1/sync-runs",
            "/v1/sync-runs/run-123/retry",
            "/v1/work-events/ev-123/claims:generate",
            "/v1/exports",
        ):
            r = client.post(path, headers=api_headers)
            assert r.status_code == 503, f"Expected 503 paused for POST {path}"
            body = r.json()
            assert body["error"]["code"] == EdgeDenialCode.RUNTIME_PAUSED_COST_GUARD.value
            assert body["error"]["paid_fallback_allowed"] is False
            assert len(body["error"]["free_alternatives"]) == 4
            assert r.headers["retry-after"] == "600"
            assert r.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL

        # Essential safety & user-rights routes MUST REMAIN ALLOWED!
        assert client.get("/healthz").status_code == 200
        assert (
            client.post("/v1/source-accounts/acc-1/disconnect", headers=api_headers).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/source-resources/res-1/deletion-preview", headers=api_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/source-resources/res-1/deletion-confirm", headers=api_headers
            ).status_code
            == 200
        )
        assert client.get("/v1/deletion-jobs/job-1", headers=api_headers).status_code == 200

        # Resume with budget_verified=True restores high-cost routes cleanly
        utc_clock.advance(120)
        resumed = controller.resume(actor_id="operator-1", budget_verified=True)
        assert resumed.mode is EdgeRuntimeMode.ACTIVE
        assert resumed.budget_verified is True
        assert len(controller.audit_log) == 2

        for path in (
            "/v1/sync-runs",
            "/v1/sync-runs/run-123/retry",
            "/v1/work-events/ev-123/claims:generate",
            "/v1/exports",
        ):
            assert client.post(path, headers=api_headers).status_code == 200


def test_paused_incident_and_worker_slice_timeout_and_config_validation() -> None:
    from career_memory_api.edge_guard import HmacWorkerTokenVerifier, WorkerAuthConfig

    worker_secret = "worker-timeout-secret"
    worker_verifier = HmacWorkerTokenVerifier(secret=worker_secret, clock=lambda: 1000.0)
    worker_token = worker_verifier.mint_token(
        subject="cm-worker@proj.iam.gserviceaccount.com",
        issuer="https://accounts.google.com",
        audience="https://worker.internal.career-memory.local",
        expires_at=2000.0,
    )

    # Config fail-closed validations
    with pytest.raises(ValueError, match="wildcards"):
        CorsCsrfConfig(allowed_web_origins=frozenset({"*"}))
    with pytest.raises(ValueError, match="max_request_body_bytes"):
        EdgeRuntimeCaps(max_request_body_bytes=0)
    with pytest.raises(ValueError, match="403 or 404"):
        EdgeGuardConfig(undeclared_route_status_code=200)

    controller = ManualEdgePauseController()
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/work-events")
    async def list_events() -> dict[str, str]:
        return {"ok": "true"}

    @app.post("/v1/source-accounts/{account_id}/disconnect")
    async def disconnect(account_id: str) -> dict[str, str]:
        return {"disconnected": account_id}

    @app.post("/internal/worker/slow-slice")
    async def slow_worker_slice() -> dict[str, str]:
        await asyncio.sleep(0.2)
        return {"done": "ok"}

    cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(ORIGIN_SECRET,),
        ),
        worker_auth=WorkerAuthConfig(
            allowed_worker_audiences=frozenset({"https://worker.internal.career-memory.local"}),
            allowed_worker_issuers=frozenset({"https://accounts.google.com"}),
            token_verifier=worker_verifier,
        ),
        cors_csrf=CorsCsrfConfig(allowed_web_origins=frozenset({ALLOWED_ORIGIN})),
        caps=EdgeRuntimeCaps(
            api_request_slice_timeout_seconds=15.0,
            worker_request_slice_timeout_seconds=0.05,
        ),
    )
    install_edge_guard(app, cfg, controller=controller)

    # 1. Worker slice uses worker_request_slice_timeout_seconds (0.05s) and returns 504
    status_w_to, _, body_w_to = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/internal/worker/slow-slice",
            headers={"X-CM-Worker-Auth": worker_token},
        )
    )
    assert status_w_to == 504
    assert json.loads(body_w_to)["error"]["code"] == EdgeDenialCode.REQUEST_SLICE_TIMEOUT.value

    # 2. PAUSED_INCIDENT blocks normal read-only GETs and internal worker routes,
    #    while preserving healthz and disconnect user-rights routes.
    controller.pause(
        reason_code="INCIDENT_MITIGATION",
        actor_id="operator-2",
        mode=EdgeRuntimeMode.PAUSED_INCIDENT,
    )
    api_headers = {
        "X-CM-Origin-Auth": ORIGIN_SECRET,
        "Origin": ALLOWED_ORIGIN,
        "Sec-Fetch-Site": "same-origin",
    }
    with TestClient(app) as client:
        r_read = client.get("/v1/work-events", headers=api_headers)
        assert r_read.status_code == 503
        assert r_read.json()["error"]["code"] == EdgeDenialCode.RUNTIME_PAUSED_INCIDENT.value

        r_worker = client.post(
            "/internal/worker/slow-slice",
            headers={"X-CM-Worker-Auth": worker_token},
        )
        assert r_worker.status_code == 503
        assert r_worker.json()["error"]["code"] == EdgeDenialCode.RUNTIME_PAUSED_INCIDENT.value

        assert client.get("/healthz").status_code == 200
        assert (
            client.post("/v1/source-accounts/acc-9/disconnect", headers=api_headers).status_code
            == 200
        )
