"""Tests for Route Trust Matrix, Origin Auth, Private Worker Isolation, Cache Isolation & CORS/CSRF (INF-07 / SEC-01)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from career_memory_api.edge_guard import (
    AUTHENTICATED_API_VARY,
    CDN_NO_STORE_HEADER_VALUE,
    CorsCsrfConfig,
    EdgeDenialCode,
    EdgeGuardConfig,
    HmacWorkerTokenVerifier,
    OriginAuthConfig,
    PRIVATE_NO_STORE_CACHE_CONTROL,
    PUBLIC_HEALTH_CACHE_CONTROL,
    RouteTrustClass,
    WorkerAuthConfig,
    classify_route,
    install_edge_guard,
    sign_origin_auth_header,
)

ORIGIN_SECRET_CURRENT = "cm-origin-secret-current-v2"
ORIGIN_SECRET_PREVIOUS = "cm-origin-secret-previous-v1"
WORKER_HMAC_SECRET = "cm-internal-worker-hmac-secret"
ALLOWED_WEB_ORIGIN = "https://app.career-memory.example"
TRUSTED_PROXY_IP = "10.240.0.15"
ALLOWED_MTLS_FP = "a" * 64
ALLOWED_MTLS_SUBJECT = "CN=cloudflare-edge-client,O=CareerMemory"
WORKER_AUDIENCE = "https://worker.internal.career-memory.local"
WORKER_ISSUER = "https://accounts.google.com"
WORKER_SA = "cm-worker@career-memory-prod.iam.gserviceaccount.com"


class FakeWallClock:
    def __init__(self, initial: float = 1_760_000_000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _call_asgi(
    app: FastAPI,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body_chunks: list[bytes] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 50000),
    raw_path: bytes | None = None,
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
        "raw_path": raw_path if raw_path is not None else path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "client": client,
        "server": ("api.career-memory.example", 443),
    }
    chunks = list(body_chunks) if body_chunks is not None else [b""]
    index = 0

    async def receive() -> dict[str, Any]:
        nonlocal index
        if index < len(chunks):
            chunk = chunks[index]
            index += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks),
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


def _make_guarded_app(
    *,
    wall_clock: FakeWallClock | None = None,
    origin_auth: OriginAuthConfig | None = None,
    undeclared_status: int = 404,
) -> tuple[FastAPI, HmacWorkerTokenVerifier, FakeWallClock]:
    clock = wall_clock or FakeWallClock()
    worker_verifier = HmacWorkerTokenVerifier(secret=WORKER_HMAC_SECRET, clock=clock)
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> Response:
        # Deliberately attempt public caching to prove middleware overrides it.
        return JSONResponse(
            content={"status": "ok"},
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/v1/content-units/{unit_id}/content")
    async def get_ephemeral_content(unit_id: str, request: Request) -> Response:
        workspace_id = request.headers.get("X-Workspace-ID", "none")
        # Deliberately set public/CDN cache headers to prove middleware strips them.
        return JSONResponse(
            content={"unit_id": unit_id, "workspace_id": workspace_id},
            headers={
                "Cache-Control": "public, max-age=86400",
                "CDN-Cache-Control": "public, max-age=86400",
                "Surrogate-Control": "max-age=86400",
                "Pragma": "public",
                "Vary": "Accept-Encoding",
            },
        )

    @app.get("/v1/work-events/{event_id}")
    async def get_event(event_id: str) -> Response:
        if event_id == "missing":
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "NOT_FOUND"}},
                headers={"Cache-Control": "public, max-age=600"},
            )
        if event_id == "crash":
            raise RuntimeError("boom Authorization: Bearer super_secret_token")
        return JSONResponse(content={"event_id": event_id})

    @app.post("/v1/sync-runs")
    async def create_sync_run() -> dict[str, str]:
        return {"status": "queued"}

    @app.patch("/v1/work-events/{event_id}")
    async def patch_event(event_id: str) -> dict[str, str]:
        return {"event_id": event_id, "updated": "true"}

    @app.delete("/v1/exports/{export_id}")
    async def delete_export(export_id: str) -> dict[str, str]:
        return {"export_id": export_id, "deleted": "true"}

    @app.post("/internal/worker/sync-slice")
    async def internal_worker_slice() -> Response:
        return JSONResponse(
            content={"worker": "ok"},
            headers={"Cache-Control": "public, max-age=60"},
        )

    cfg = EdgeGuardConfig(
        origin_auth=origin_auth
        or OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(ORIGIN_SECRET_CURRENT, ORIGIN_SECRET_PREVIOUS),
            trusted_mtls_proxy_ips=frozenset({TRUSTED_PROXY_IP}),
            allowed_mtls_fingerprints=frozenset({ALLOWED_MTLS_FP}),
            allowed_mtls_subjects=frozenset({ALLOWED_MTLS_SUBJECT}),
            block_direct_run_app_hosts=True,
        ),
        worker_auth=WorkerAuthConfig(
            allowed_worker_audiences=frozenset({WORKER_AUDIENCE}),
            allowed_worker_issuers=frozenset({WORKER_ISSUER}),
            allowed_worker_service_accounts=frozenset({WORKER_SA}),
            token_verifier=worker_verifier,
        ),
        cors_csrf=CorsCsrfConfig(
            allowed_web_origins=frozenset({ALLOWED_WEB_ORIGIN}),
        ),
        undeclared_route_status_code=undeclared_status,
    )
    install_edge_guard(app, cfg, wall_clock=clock)
    return app, worker_verifier, clock


def test_route_trust_matrix_classification() -> None:
    assert classify_route("GET", "/healthz").trust_class is RouteTrustClass.PUBLIC_HEALTH
    assert classify_route("POST", "/healthz").trust_class is RouteTrustClass.DENIED_UNDECLARED
    assert (
        classify_route("GET", "/v1/work-events/ev-1").trust_class
        is RouteTrustClass.AUTHENTICATED_API
    )
    assert (
        classify_route("POST", "/internal/worker/sync-slice").trust_class
        is RouteTrustClass.INTERNAL_WORKER_ONLY
    )
    for undeclared in (
        "/debug",
        "/admin",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/v1/byok",
        "/v1/byok/keys",
        "/v1/llm/callback",
        "/v1/llm/connect",
        "/v1/unknown-surface",
        "/v1/sync-runs/../admin",
        "/v1//sync-runs",
    ):
        assert (
            classify_route("GET", undeclared).trust_class
            is RouteTrustClass.DENIED_UNDECLARED
        ), f"Expected DENIED_UNDECLARED for {undeclared}"


def test_public_healthz_allows_without_origin_auth_and_sets_no_store() -> None:
    app, _, _ = _make_guarded_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers["cache-control"] == PUBLIC_HEALTH_CACHE_CONTROL
    assert resp.headers["cdn-cache-control"] == CDN_NO_STORE_HEADER_VALUE
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_origin_auth_blocks_missing_invalid_and_direct_run_app_bypass() -> None:
    app, _, clock = _make_guarded_app()
    with TestClient(app) as client:
        # 1. Missing X-CM-Origin-Auth -> 403 ORIGIN_AUTH_REQUIRED
        r_missing = client.get("/v1/work-events/ev-1")
        assert r_missing.status_code == 403
        assert r_missing.json()["error"]["code"] == EdgeDenialCode.ORIGIN_AUTH_REQUIRED.value
        assert r_missing.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL
        assert r_missing.headers["cdn-cache-control"] == CDN_NO_STORE_HEADER_VALUE

        # 2. Invalid secret -> 403 ORIGIN_AUTH_REQUIRED
        r_invalid = client.get(
            "/v1/work-events/ev-1",
            headers={"X-CM-Origin-Auth": "wrong-secret"},
        )
        assert r_invalid.status_code == 403
        assert r_invalid.json()["error"]["code"] == EdgeDenialCode.ORIGIN_AUTH_REQUIRED.value

        # 3. Direct *.run.app Host bypass rejected even if header supplied
        r_run_app = client.get(
            "/v1/work-events/ev-1",
            headers={
                "Host": "career-memory-api-12345.a.run.app",
                "X-CM-Origin-Auth": ORIGIN_SECRET_CURRENT,
            },
        )
        assert r_run_app.status_code == 403
        assert r_run_app.json()["error"]["code"] == EdgeDenialCode.ORIGIN_AUTH_REQUIRED.value

        # 4. Constant-time rotation ring accepts BOTH current and previous secret
        r_cur = client.get(
            "/v1/work-events/ev-1",
            headers={"X-CM-Origin-Auth": ORIGIN_SECRET_CURRENT},
        )
        assert r_cur.status_code == 200
        r_prev = client.get(
            "/v1/work-events/ev-1",
            headers={"X-CM-Origin-Auth": ORIGIN_SECRET_PREVIOUS},
        )
        assert r_prev.status_code == 200

        # 5. Cryptographic HMAC-SHA256 timestamped signature works and resists tampering/replay
        signed = sign_origin_auth_header(
            ORIGIN_SECRET_CURRENT,
            method="GET",
            path="/v1/work-events/ev-1",
            timestamp=int(clock.now),
        )
        r_signed = client.get(
            "/v1/work-events/ev-1",
            headers={"X-CM-Origin-Auth": signed},
        )
        assert r_signed.status_code == 200

        # Mutated path fails HMAC verification
        r_tampered_path = client.get(
            "/v1/work-events/ev-2",
            headers={"X-CM-Origin-Auth": signed},
        )
        assert r_tampered_path.status_code == 403
        assert r_tampered_path.json()["error"]["code"] == EdgeDenialCode.ORIGIN_AUTH_REQUIRED.value

        # Expired timestamp fails skew check
        clock.advance(400)
        r_expired = client.get(
            "/v1/work-events/ev-1",
            headers={"X-CM-Origin-Auth": signed},
        )
        assert r_expired.status_code == 403


def test_origin_auth_mtls_requires_trusted_proxy_peer_and_fail_closed_when_unconfigured() -> None:
    app, _, _ = _make_guarded_app()
    xfcc_valid = f'By=spiffe://cluster.local/ns/default/sa/api;Hash={ALLOWED_MTLS_FP};Subject="{ALLOWED_MTLS_SUBJECT}"'

    # Spoofed X-Forwarded-Client-Cert from untrusted peer IP -> REJECTED
    status_spoof, _, body_spoof = asyncio.run(
        _call_asgi(
            app,
            method="GET",
            path="/v1/work-events/ev-1",
            headers={
                "Host": "api.career-memory.example",
                "X-Forwarded-Client-Cert": xfcc_valid,
            },
            client=("198.51.100.77", 443),
        )
    )
    assert status_spoof == 403
    assert json.loads(body_spoof)["error"]["code"] == EdgeDenialCode.ORIGIN_AUTH_REQUIRED.value

    # Verified X-Forwarded-Client-Cert from trusted proxy peer IP -> ALLOWED
    status_ok, headers_ok, _ = asyncio.run(
        _call_asgi(
            app,
            method="GET",
            path="/v1/work-events/ev-1",
            headers={
                "Host": "api.career-memory.example",
                "X-Forwarded-Client-Cert": xfcc_valid,
            },
            client=(TRUSTED_PROXY_IP, 443),
        )
    )
    assert status_ok == 200
    assert headers_ok["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL

    # Fail-closed when require_origin_auth=True with zero secrets and zero mTLS configured
    unconfigured_app, _, _ = _make_guarded_app(
        origin_auth=OriginAuthConfig(require_origin_auth=True)
    )
    with TestClient(unconfigured_app) as client:
        r = client.get("/v1/work-events/ev-1", headers={"X-CM-Origin-Auth": "anything"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == EdgeDenialCode.ORIGIN_AUTH_REQUIRED.value


def test_cache_and_tenant_isolation_enforced_across_2xx_4xx_5xx() -> None:
    app, _, _ = _make_guarded_app()
    base_headers = {"X-CM-Origin-Auth": ORIGIN_SECRET_CURRENT}

    with TestClient(app) as client:
        # 2xx: handler deliberately emits public cache headers -> middleware MUST override
        r_ws_a = client.get(
            "/v1/content-units/cu-1/content",
            headers={**base_headers, "X-Workspace-ID": "ws-a", "Authorization": "Bearer tok-a"},
        )
        r_ws_b = client.get(
            "/v1/content-units/cu-1/content",
            headers={**base_headers, "X-Workspace-ID": "ws-b", "Authorization": "Bearer tok-b"},
        )
        assert r_ws_a.status_code == 200
        assert r_ws_b.status_code == 200
        assert r_ws_a.json()["workspace_id"] == "ws-a"
        assert r_ws_b.json()["workspace_id"] == "ws-b"

        # 4xx response
        r_404 = client.get("/v1/work-events/missing", headers=base_headers)
        assert r_404.status_code == 404

        # 5xx response (must also redact exception & enforce private, no-store)
        r_500 = client.get("/v1/work-events/crash", headers=base_headers)
        assert r_500.status_code == 500
        assert "super_secret_token" not in r_500.text

        for resp in (r_ws_a, r_ws_b, r_404, r_500):
            assert resp.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL
            assert resp.headers["cdn-cache-control"] == CDN_NO_STORE_HEADER_VALUE
            assert "surrogate-control" not in resp.headers
            assert resp.headers["pragma"] == "no-cache"
            assert resp.headers["vary"] == AUTHENTICATED_API_VARY
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["x-frame-options"] == "DENY"
            assert resp.headers["referrer-policy"] == "no-referrer"


def test_internal_worker_route_forbids_edge_browser_session_and_requires_oidc_audience() -> None:
    app, verifier, clock = _make_guarded_app()
    valid_token = verifier.mint_token(
        subject=WORKER_SA,
        issuer=WORKER_ISSUER,
        audience=WORKER_AUDIENCE,
        expires_at=clock.now + 300,
    )
    wrong_aud_token = verifier.mint_token(
        subject=WORKER_SA,
        issuer=WORKER_ISSUER,
        audience="https://wrong-audience.example",
        expires_at=clock.now + 300,
    )
    expired_token = verifier.mint_token(
        subject=WORKER_SA,
        issuer=WORKER_ISSUER,
        audience=WORKER_AUDIENCE,
        expires_at=clock.now - 10,
    )

    with TestClient(app) as client:
        # 1. Valid internal worker call without edge/browser/session headers -> 200
        r_ok = client.post(
            "/internal/worker/sync-slice",
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert r_ok.status_code == 200
        assert r_ok.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL
        assert r_ok.headers["cdn-cache-control"] == CDN_NO_STORE_HEADER_VALUE

        # 2. ANY edge / browser / session header MUST fail closed with 403 PUBLIC_WORKER_ROUTE_FORBIDDEN
        for forbidden_headers in (
            {"CF-Connecting-IP": "203.0.113.10"},
            {"CF-Ray": "8a1b2c3d4e5f-ICN"},
            {"X-CM-Origin-Auth": ORIGIN_SECRET_CURRENT},
            {"Cookie": "session=abc"},
            {"Origin": ALLOWED_WEB_ORIGIN},
            {"Sec-Fetch-Site": "same-origin"},
            {"Referer": f"{ALLOWED_WEB_ORIGIN}/dashboard"},
        ):
            r_forb = client.post(
                "/internal/worker/sync-slice",
                headers={"Authorization": f"Bearer {valid_token}", **forbidden_headers},
            )
            assert r_forb.status_code == 403, f"Expected 403 for {forbidden_headers}"
            assert (
                r_forb.json()["error"]["code"]
                == EdgeDenialCode.PUBLIC_WORKER_ROUTE_FORBIDDEN.value
            )
            assert r_forb.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL

        # 3. Missing / wrong audience / expired worker token -> 403 PUBLIC_WORKER_ROUTE_FORBIDDEN
        for bad_headers in (
            {},
            {"Authorization": f"Bearer {wrong_aud_token}"},
            {"Authorization": f"Bearer {expired_token}"},
            {"Authorization": "Bearer not-a-valid-worker-token"},
        ):
            r_bad = client.post("/internal/worker/sync-slice", headers=bad_headers)
            assert r_bad.status_code == 403
            assert (
                r_bad.json()["error"]["code"]
                == EdgeDenialCode.PUBLIC_WORKER_ROUTE_FORBIDDEN.value
            )


def test_undeclared_and_prohibited_routes_fail_closed_with_private_no_store() -> None:
    app, _, _ = _make_guarded_app(undeclared_status=404)
    with TestClient(app) as client:
        for path in ("/debug", "/admin", "/metrics", "/v1/byok", "/v1/llm/callback"):
            resp = client.get(path, headers={"X-CM-Origin-Auth": ORIGIN_SECRET_CURRENT})
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == EdgeDenialCode.ROUTE_NOT_DECLARED.value
            assert resp.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL
            assert resp.headers["cdn-cache-control"] == CDN_NO_STORE_HEADER_VALUE

    # Encoded path traversal via raw_path fails closed
    status_trav, headers_trav, body_trav = asyncio.run(
        _call_asgi(
            app,
            method="GET",
            path="/v1/work-events/ev-1",
            raw_path=b"/v1/work-events/%2e%2e/admin",
            headers={"X-CM-Origin-Auth": ORIGIN_SECRET_CURRENT},
        )
    )
    assert status_trav == 404
    assert json.loads(body_trav)["error"]["code"] == EdgeDenialCode.ROUTE_NOT_DECLARED.value
    assert headers_trav["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL


def test_strict_single_origin_cors_and_csrf_guard() -> None:
    app, _, _ = _make_guarded_app()
    origin_hdr = {"X-CM-Origin-Auth": ORIGIN_SECRET_CURRENT}

    with TestClient(app) as client:
        # 1. Allowed single-origin preflight -> 204 with exact origin (never '*')
        r_pre_ok = client.options(
            "/v1/sync-runs",
            headers={
                **origin_hdr,
                "Origin": ALLOWED_WEB_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r_pre_ok.status_code == 204
        assert r_pre_ok.headers["access-control-allow-origin"] == ALLOWED_WEB_ORIGIN
        assert r_pre_ok.headers["access-control-allow-credentials"] == "true"
        assert r_pre_ok.headers["cache-control"] == PRIVATE_NO_STORE_CACHE_CONTROL

        # 2. Untrusted origin preflight -> 403 CORS_ORIGIN_DENIED, no ACAO header
        r_pre_bad = client.options(
            "/v1/sync-runs",
            headers={
                **origin_hdr,
                "Origin": "https://evil.attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r_pre_bad.status_code == 403
        assert r_pre_bad.json()["error"]["code"] == EdgeDenialCode.CORS_ORIGIN_DENIED.value
        assert "access-control-allow-origin" not in r_pre_bad.headers

        # 3. Cross-origin POST / PATCH / DELETE rejected with 403 CSRF_ORIGIN_DENIED
        for method, path in (
            ("POST", "/v1/sync-runs"),
            ("PATCH", "/v1/work-events/ev-1"),
            ("DELETE", "/v1/exports/exp-1"),
        ):
            r_csrf_origin = client.request(
                method,
                path,
                headers={**origin_hdr, "Origin": "https://evil.attacker.example"},
            )
            assert r_csrf_origin.status_code == 403
            assert r_csrf_origin.json()["error"]["code"] == EdgeDenialCode.CSRF_ORIGIN_DENIED.value

        # 4. Sec-Fetch-Site: cross-site rejected on state-changing POST
        r_cross_site = client.post(
            "/v1/sync-runs",
            headers={
                **origin_hdr,
                "Origin": ALLOWED_WEB_ORIGIN,
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert r_cross_site.status_code == 403
        assert r_cross_site.json()["error"]["code"] == EdgeDenialCode.CSRF_ORIGIN_DENIED.value

        # 5. Cookie-authenticated state-changing POST with missing Origin -> 403 CSRF_ORIGIN_DENIED
        r_cookie_no_origin = client.post(
            "/v1/sync-runs",
            headers={**origin_hdr, "Cookie": "cm_session=opaque-session"},
        )
        assert r_cookie_no_origin.status_code == 403
        assert (
            r_cookie_no_origin.json()["error"]["code"]
            == EdgeDenialCode.CSRF_ORIGIN_DENIED.value
        )

        # 6. Trusted Origin + same-origin POST with Cookie -> 200 ALLOWED
        r_trusted = client.post(
            "/v1/sync-runs",
            headers={
                **origin_hdr,
                "Origin": ALLOWED_WEB_ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "Cookie": "cm_session=opaque-session",
            },
        )
        assert r_trusted.status_code == 200
        assert r_trusted.headers["access-control-allow-origin"] == ALLOWED_WEB_ORIGIN
