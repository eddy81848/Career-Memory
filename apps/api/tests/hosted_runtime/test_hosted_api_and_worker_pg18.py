"""PG18 E2E tests for hosted API (edge_guard + /v1/session), zero-DB-hold auth I/O, and private worker."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from career_memory_api.conformance.checks import default_repo_root
from career_memory_api.conformance.db_catalog import EphemeralConformancePg18Cluster
from career_memory_api.edge_guard.middleware import HmacWorkerTokenVerifier
from career_memory_api.edge_guard.models import sign_origin_auth_header
from career_memory_api.fullstack_runtime.runtime_stack import LocalEs256KeyPair
from career_memory_api.fullstack_runtime.sample_github_provider import (
    SampleGitHubCredentialProvider,
    SampleGitHubWireTransport,
    SamplePublicGitHubCatalog,
)
from career_memory_api.hosted_runtime.api_service import create_hosted_api_app
from career_memory_api.hosted_runtime.jwks_client import (
    HttpsJwksProvider,
    SupabaseGoTrueLiveSessionVerifier,
)
from career_memory_api.hosted_runtime.migration_runner import (
    enroll_pilot_user,
    run_staging_migrations,
)
from career_memory_api.hosted_runtime.models import (
    HostedApiServiceConfig,
    HostedDbPoolConfig,
    HostedWorkerServiceConfig,
)
from career_memory_api.hosted_runtime.worker_service import create_hosted_worker_app
from career_memory_api.hosted_runtime.worker_wake import (
    GoogleOidcWorkerTokenVerifier,
    HttpCloudRunWakeNotifier,
)

REPO_ROOT = default_repo_root()
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


class _LoopbackSupabaseAuthServer:
    """Ephemeral local HTTP server simulating Supabase JWKS and GoTrue GET /auth/v1/user."""

    def __init__(self, keypair: LocalEs256KeyPair) -> None:
        self.keypair = keypair
        self.jwk_dict = {
            "kty": "EC",
            "crv": "P-256",
            "alg": "ES256",
            "use": "sig",
            "kid": keypair.kid,
            "x": keypair.to_jwk().x.to_bytes(32, "big") if keypair.to_jwk().x is not None else b"",
        }
        import base64

        jwk = keypair.to_jwk()
        assert jwk.x is not None and jwk.y is not None
        self.public_jwk_json = {
            "kty": "EC",
            "crv": "P-256",
            "alg": "ES256",
            "use": "sig",
            "kid": jwk.kid,
            "x": base64.urlsafe_b64encode(jwk.x.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
            "y": base64.urlsafe_b64encode(jwk.y.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
        }

        self.jwks_status: int = 200
        self.user_status: int = 200
        self.user_subject_id: str = "sub-enrolled-pilot"
        self.on_auth_io_hook: Any = None
        self.jwks_hits: int = 0
        self.user_hits: int = 0

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if outer.on_auth_io_hook is not None:
                    outer.on_auth_io_hook(self.path)

                if self.path.endswith("/.well-known/jwks.json"):
                    outer.jwks_hits += 1
                    body = json.dumps({"keys": [outer.public_jwk_json]}).encode("utf-8")
                    self.send_response(outer.jwks_status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if outer.jwks_status == 200:
                        self.wfile.write(body)
                    else:
                        self.wfile.write(b'{"error":"jwks_unavailable"}')
                    return

                if self.path.endswith("/auth/v1/user"):
                    outer.user_hits += 1
                    self.send_response(outer.user_status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if outer.user_status == 200:
                        self.wfile.write(
                            json.dumps({"id": outer.user_subject_id}).encode("utf-8")
                        )
                    elif outer.user_status == 403:
                        self.wfile.write(
                            json.dumps({"error_code": "session_not_found"}).encode("utf-8")
                        )
                    else:
                        self.wfile.write(b'{"error":"upstream_unavailable"}')
                    return

                self.send_response(404)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def jwks_url(self) -> str:
        return f"{self.base_url}/auth/v1/.well-known/jwks.json"

    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/auth/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _sign_supabase_jwt(
    keypair: LocalEs256KeyPair,
    *,
    issuer: str,
    subject: str,
    audience: str = "authenticated",
    email: str = "pilot@example.test",
    name: str = "Pilot Owner",
) -> str:
    now_ts = int(datetime.now(UTC).timestamp())
    return keypair.sign_jwt(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "iat": now_ts,
            "nbf": now_ts - 5,
            "exp": now_ts + 3600,
            "email": email,
            "name": name,
            "session_id": str(uuid.uuid4()),
        }
    )


@pytest.mark.postgres
def test_hosted_api_session_zero_db_hold_edge_guard_and_worker_recovery_pg18() -> None:
    keypair = LocalEs256KeyPair.generate(kid="supabase-test-es256-1")
    oidc_keypair = LocalEs256KeyPair.generate(kid="google-oidc-es256-1")
    auth_server = _LoopbackSupabaseAuthServer(keypair)
    oidc_server = _LoopbackSupabaseAuthServer(oidc_keypair)
    origin_secret = "test-origin-hmac-secret-track2-2026"
    worker_hmac_secret = "test-worker-hmac-secret-track2-2026"
    worker_audience = "https://cm-worker-xyz.a.run.app"
    issuer = auth_server.auth_url

    try:
        with EphemeralConformancePg18Cluster(prefix="cm-hosted-e2e-") as cluster:
            dbname = "cm_hosted_e2e"
            cluster.create_database(dbname)
            admin_dsn = cluster.dsn(dbname)

            # 1. Apply migrations via migration_runner
            mig_report = run_staging_migrations(
                admin_dsn,
                migrations_dir=MIGRATIONS_DIR,
                allow_insecure_local_test=True,
            )
            assert mig_report.audit.passed is True

            # 2. Create least-privilege login roles for cm_api_app and cm_worker_app
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE ROLE cm_api_hosted_login LOGIN PASSWORD 'api_pw'
                            NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        GRANT cm_api_app TO cm_api_hosted_login;

                        CREATE ROLE cm_worker_hosted_login LOGIN PASSWORD 'worker_pw'
                            NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        GRANT cm_worker_app TO cm_worker_hosted_login;
                        """
                    )

            # 3. Enroll pilot user via cm_migrator
            enrolled = enroll_pilot_user(
                admin_dsn,
                auth_issuer=issuer,
                auth_subject="sub-enrolled-pilot",
                workspace_name="Closed Pilot Workspace",
                email="pilot@example.test",
                display_name="Pilot Owner",
                github_login="seoyeon-dev",
                github_external_id="904210",
                allow_insecure_local_test=True,
            )

            api_dsn = f"postgresql://cm_api_hosted_login:api_pw@127.0.0.1:{cluster.port}/{dbname}"
            worker_dsn = f"postgresql://cm_worker_hosted_login:worker_pw@127.0.0.1:{cluster.port}/{dbname}"

            # 4. Build failing post-commit wake notifier (simulating Cloud Run wake timeout/error)
            #    so we verify API still returns 202 with durable PENDING job!
            def _failing_wake_post(
                _url: str, _headers: dict[str, str], _body: bytes, _timeout: float
            ) -> tuple[int, bytes]:
                raise TimeoutError("simulated Cloud Run cold-start wake timeout")

            failing_wake = HttpCloudRunWakeNotifier(
                worker_url="http://127.0.0.1:19999",
                audience=worker_audience,
                timeout_seconds=0.5,
                hmac_verifier=HmacWorkerTokenVerifier(secret=worker_hmac_secret),
                allow_http_loopback_for_tests=True,
                http_post=_failing_wake_post,
            )

            catalog = SamplePublicGitHubCatalog()
            sample_transport = SampleGitHubWireTransport(catalog)
            sample_creds = SampleGitHubCredentialProvider(
                catalog, external_account_id="904210", login="seoyeon-dev"
            )

            api_config = HostedApiServiceConfig(
                db_pool=HostedDbPoolConfig(
                    database_url=api_dsn,
                    min_size=0,
                    max_size=2,
                    require_tls=False,
                    allow_insecure_local_test=True,
                    expected_role_group="cm_api_app",
                ),
                jwt_issuer=issuer,
                jwks_url=auth_server.jwks_url,
                jwt_audience="authenticated",
                supabase_auth_url=auth_server.auth_url,
                supabase_anon_key="test-anon-key-public",
                require_origin_auth=True,
                origin_hmac_secrets=(origin_secret,),
                allow_run_app_host_with_origin_proof=True,
                allowed_web_origins=frozenset({"https://app.career-memory.example"}),
                allowed_redirect_uris=("https://app.career-memory.example/api/auth/github/callback",),
                allow_http_loopback_for_tests=True,
            )

            api_app = create_hosted_api_app(
                api_config,
                wake_notifier=failing_wake,
                github_wire_transport=sample_transport,
                github_credential_provider=sample_creds,
            )

            # Hook into loopback auth server to assert connections_in_use == 0 & open_tx == 0
            # during EVERY JWKS and /auth/v1/user HTTP call!
            io_observations: list[tuple[str, int, int]] = []

            def _record_db_state_during_auth_io(req_path: str) -> None:
                stats = api_app.state.hosted_api_runtime.pool.get_stats()
                in_use = stats["connections_in_use"]
                with psycopg.connect(admin_dsn, autocommit=True) as probe_conn:
                    with probe_conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT count(*)
                            FROM pg_stat_activity
                            WHERE usename = 'cm_api_hosted_login'
                              AND xact_start IS NOT NULL
                            """
                        )
                        open_tx = int(cur.fetchone()[0])
                io_observations.append((req_path, in_use, open_tx))

            auth_server.on_auth_io_hook = _record_db_state_during_auth_io

            enrolled_jwt = _sign_supabase_jwt(
                keypair, issuer=issuer, subject="sub-enrolled-pilot"
            )
            unenrolled_jwt = _sign_supabase_jwt(
                keypair, issuer=issuer, subject="sub-unenrolled-stranger"
            )

            def _origin_headers(method: str, path: str, extra: dict[str, str] | None = None) -> dict[str, str]:
                sig = sign_origin_auth_header(
                    origin_secret, method=method, path=path, timestamp=int(time.time())
                )
                hdrs = {
                    "Host": "cm-api-xyz.a.run.app",
                    "X-CM-Origin-Auth": sig,
                }
                if extra:
                    hdrs.update(extra)
                return hdrs

            with TestClient(api_app) as api_client:
                # A. Healthz 200
                r_health = api_client.get("/healthz", headers={"Host": "cm-api-xyz.a.run.app"})
                assert r_health.status_code == 200

                # B. Direct *.run.app without X-CM-Origin-Auth -> 403 ORIGIN_AUTH_REQUIRED
                r_direct = api_client.get(
                    "/v1/session",
                    headers={
                        "Host": "cm-api-xyz.a.run.app",
                        "Authorization": f"Bearer {enrolled_jwt}",
                    },
                )
                assert r_direct.status_code == 403
                assert r_direct.json()["error"]["code"] == "ORIGIN_AUTH_REQUIRED"
                assert "private" in r_direct.headers["cache-control"].lower()
                assert "no-store" in r_direct.headers["cache-control"].lower()

                # C. /internal/worker/wake on cm-api -> 403 PUBLIC_WORKER_ROUTE_FORBIDDEN
                r_worker_on_api = api_client.post(
                    "/internal/worker/wake",
                    headers={"Host": "cm-api-xyz.a.run.app"},
                )
                assert r_worker_on_api.status_code == 403
                assert r_worker_on_api.json()["error"]["code"] == "PUBLIC_WORKER_ROUTE_FORBIDDEN"

                # D. Signed X-CM-Origin-Auth GET /v1/session for enrolled pilot user -> 200, 0 tokens in JSON
                r_session = api_client.get(
                    "/v1/session",
                    headers=_origin_headers(
                        "GET", "/v1/session", {"Authorization": f"Bearer {enrolled_jwt}"}
                    ),
                )
                assert r_session.status_code == 200, r_session.text
                body = r_session.json()
                assert body["mode"] == "hosted_supabase"
                assert body["workspace_id"] == enrolled.workspace_id
                assert body["workspace_name"] == "Closed Pilot Workspace"
                assert body["user_id"] == enrolled.user_id
                assert body["github_login"] == "seoyeon-dev"
                assert body["sample_repositories"] == []
                assert "access_token" not in body
                assert "refresh_token" not in body
                assert r_session.headers["cache-control"] == "private, no-store"
                assert r_session.headers["cdn-cache-control"] == "no-store"

                # Assert ZERO pooled DB connections & ZERO open transactions during JWKS and /auth/v1/user!
                assert len(io_observations) >= 2
                for path_seen, in_use, open_tx in io_observations:
                    assert in_use == 0, f"DB connection held during {path_seen}: in_use={in_use}"
                    assert open_tx == 0, f"Open DB tx during {path_seen}: open_tx={open_tx}"

                # E. Unenrolled user -> 403 WORKSPACE_SCOPE_VIOLATION
                auth_server.user_subject_id = "sub-unenrolled-stranger"
                r_unenrolled = api_client.get(
                    "/v1/session",
                    headers=_origin_headers(
                        "GET", "/v1/session", {"Authorization": f"Bearer {unenrolled_jwt}"}
                    ),
                )
                assert r_unenrolled.status_code == 403
                assert r_unenrolled.json()["error"]["code"] == "WORKSPACE_SCOPE_VIOLATION"
                assert "private, no-store" in r_unenrolled.headers["cache-control"]

                # F. GoTrue 403 session_not_found -> 401 SESSION_TERMINATED
                auth_server.user_subject_id = "sub-enrolled-pilot"
                auth_server.user_status = 403
                r_revoked = api_client.get(
                    "/v1/session",
                    headers=_origin_headers(
                        "GET", "/v1/session", {"Authorization": f"Bearer {enrolled_jwt}"}
                    ),
                )
                assert r_revoked.status_code == 401
                assert r_revoked.json()["error"]["code"] == "SESSION_TERMINATED"

                # G. GoTrue 503 outage -> 503 AUTH_VERIFICATION_UNAVAILABLE (preserving web session)
                auth_server.user_status = 503
                r_outage = api_client.get(
                    "/v1/session",
                    headers=_origin_headers(
                        "GET", "/v1/session", {"Authorization": f"Bearer {enrolled_jwt}"}
                    ),
                )
                assert r_outage.status_code == 503
                assert r_outage.json()["error"]["code"] == "AUTH_VERIFICATION_UNAVAILABLE"
                assert r_outage.json()["error"]["retryable"] is True

                # Restore GoTrue active
                auth_server.user_status = 200

                # H. Connect GitHub account + register 2 repos (ledger-engine for SYNC_RUN, search-gateway for deletion)
                r_oauth_start = api_client.post(
                    "/v1/sources/github/oauth/start",
                    headers=_origin_headers(
                        "POST",
                        "/v1/sources/github/oauth/start",
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                        },
                    ),
                    json={
                        "return_path": "/",
                        "redirect_uri": "https://app.career-memory.example/api/auth/github/callback",
                    },
                )
                assert r_oauth_start.status_code in (200, 201), r_oauth_start.text
                oauth_state = r_oauth_start.json()["state"]

                r_oauth_cb = api_client.post(
                    "/v1/sources/github/oauth/callback",
                    headers=_origin_headers(
                        "POST",
                        "/v1/sources/github/oauth/callback",
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                            "Idempotency-Key": str(uuid.uuid4()),
                        },
                    ),
                    json={
                        "state": oauth_state,
                        "code": "sample-code",
                        "redirect_uri": "https://app.career-memory.example/api/auth/github/callback",
                    },
                )
                assert r_oauth_cb.status_code in (200, 201), r_oauth_cb.text
                account_id = r_oauth_cb.json()["source_account"]["id"]

                r_reg_1 = api_client.post(
                    "/v1/source-resources",
                    headers=_origin_headers(
                        "POST",
                        "/v1/source-resources",
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                        },
                    ),
                    json={
                        "source_account_id": account_id,
                        "repository_full_name": "seoyeon-dev/ledger-engine",
                    },
                )
                assert r_reg_1.status_code == 201, r_reg_1.text
                res1_id = r_reg_1.json()["id"]

                r_reg_2 = api_client.post(
                    "/v1/source-resources",
                    headers=_origin_headers(
                        "POST",
                        "/v1/source-resources",
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                        },
                    ),
                    json={
                        "source_account_id": account_id,
                        "repository_full_name": "seoyeon-dev/search-gateway",
                    },
                )
                assert r_reg_2.status_code == 201, r_reg_2.text
                res2_id = r_reg_2.json()["id"]

                # I. Enqueue SYNC_RUN on res1: failing wake notifier MUST NOT fail API 202!
                r_sync = api_client.post(
                    "/v1/sync-runs",
                    headers=_origin_headers(
                        "POST",
                        "/v1/sync-runs",
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                            "Idempotency-Key": str(uuid.uuid4()),
                        },
                    ),
                    json={"source_resource_id": res1_id, "mode": "INITIAL_BACKFILL"},
                )
                assert r_sync.status_code == 202, r_sync.text
                sync_run_id = r_sync.json()["target_id"]
                assert failing_wake.attempts == 1
                assert failing_wake.failures == 1

                # J. Preview + confirm deletion on res2 -> durable PENDING deletion_jobs (wake also fails non-fatally)
                del_prev_path = f"/v1/source-resources/{res2_id}/deletion-preview"
                r_del_prev = api_client.post(
                    del_prev_path,
                    headers=_origin_headers(
                        "POST",
                        del_prev_path,
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                        },
                    ),
                )
                assert r_del_prev.status_code == 200, r_del_prev.text
                prev_body = r_del_prev.json()

                del_conf_path = f"/v1/source-resources/{res2_id}/deletion-confirm"
                r_del_conf = api_client.post(
                    del_conf_path,
                    headers=_origin_headers(
                        "POST",
                        del_conf_path,
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                            "Idempotency-Key": str(uuid.uuid4()),
                        },
                    ),
                    json={
                        "preview_token": prev_body["preview_token"],
                        "expected_resource_version": prev_body["resource_version"],
                        "expected_impact_scope_version": prev_body["impact_scope_version"],
                        "acknowledge_irreversible_purge": True,
                    },
                )
                assert r_del_conf.status_code == 202, r_del_conf.text
                deletion_job_id = r_del_conf.json()["deletion_job_id"]
                assert failing_wake.attempts == 2
                assert failing_wake.failures == 2

                # 5. Now start Private IAM Worker app (`cm_worker_app` ONLY!) with GoogleOidcWorkerTokenVerifier!
                oidc_verifier = GoogleOidcWorkerTokenVerifier(
                    allowed_worker_audiences=frozenset({worker_audience}),
                    allowed_worker_issuers=frozenset({"https://accounts.google.com"}),
                    allowed_worker_service_accounts=frozenset(
                        {"cm-api-sa@career-memory-staging.iam.gserviceaccount.com"}
                    ),
                    jwks_provider=HttpsJwksProvider(
                        jwks_url=oidc_server.jwks_url,
                        allow_http_loopback_for_tests=True,
                    ),
                )
                worker_config = HostedWorkerServiceConfig(
                    db_pool=HostedDbPoolConfig(
                        database_url=worker_dsn,
                        min_size=0,
                        max_size=2,
                        require_tls=False,
                        allow_insecure_local_test=True,
                        expected_role_group="cm_worker_app",
                    ),
                    allowed_worker_audiences=frozenset({worker_audience}),
                    allowed_worker_issuers=frozenset({"https://accounts.google.com"}),
                    allowed_worker_service_accounts=frozenset(
                        {"cm-api-sa@career-memory-staging.iam.gserviceaccount.com"}
                    ),
                    default_workspace_ids=(enrolled.workspace_id,),
                    allow_http_loopback_for_tests=True,
                )
                worker_app = create_hosted_worker_app(
                    worker_config,
                    worker_token_verifier=oidc_verifier,
                    github_wire_transport=sample_transport,
                )

                now_ts = int(datetime.now(UTC).timestamp())
                valid_oidc_token = oidc_keypair.sign_jwt(
                    {
                        "iss": "https://accounts.google.com",
                        "aud": worker_audience,
                        "sub": "109876543210987654321",
                        "email": "cm-api-sa@career-memory-staging.iam.gserviceaccount.com",
                        "email_verified": True,
                        "iat": now_ts,
                        "exp": now_ts + 600,
                    }
                )

                with TestClient(worker_app) as worker_client:
                    # Worker /healthz -> 200
                    assert worker_client.get("/healthz").status_code == 200
                    # Worker blocks /v1/session fail-closed (404 ROUTE_NOT_DECLARED)
                    assert worker_client.get("/v1/session").status_code == 404
                    # Worker blocks unauthenticated /internal/worker/wake (403)
                    assert worker_client.post("/internal/worker/wake").status_code == 403
                    # Worker blocks edge/browser headers even with valid OIDC token (403)
                    r_cf = worker_client.post(
                        "/internal/worker/wake",
                        headers={
                            "Authorization": f"Bearer {valid_oidc_token}",
                            "cf-ray": "forged-edge-header",
                        },
                    )
                    assert r_cf.status_code == 403

                    # Authenticated /internal/worker/recover converges BOTH SYNC_RUN and deletion_jobs!
                    r_recover = worker_client.post(
                        "/internal/worker/recover",
                        headers={"Authorization": f"Bearer {valid_oidc_token}"},
                        json={"workspace_id": enrolled.workspace_id, "max_slices": 5},
                    )
                    assert r_recover.status_code == 200, r_recover.text
                    rec_body = r_recover.json()
                    assert rec_body["status"] == "ok"
                    assert rec_body["sync_jobs_processed"] == 1
                    assert rec_body["deletion_jobs_processed"] == 1

                    # Authenticated /internal/worker/wake with idle queue returns 200 processed_slices=0
                    r_wake_idle = worker_client.post(
                        "/internal/worker/wake",
                        headers={"Authorization": f"Bearer {valid_oidc_token}"},
                        json={"workspace_id": enrolled.workspace_id, "max_slices": 2},
                    )
                    assert r_wake_idle.status_code == 200, r_wake_idle.text
                    assert r_wake_idle.json()["processed_slices"] == 0

                # Verify JWKS outage fails closed with 503 AUTH_VERIFICATION_UNAVAILABLE
                auth_server.jwks_status = 503
                jwks_outage_provider = HttpsJwksProvider(
                    jwks_url=auth_server.jwks_url,
                    allow_http_loopback_for_tests=True,
                )
                with pytest.raises(Exception):
                    jwks_outage_provider.get_jwks(force_refresh=True)
                auth_server.jwks_status = 200

                # 6. Verify via API that SYNC_RUN converged to SUCCESS (import_ready=True)
                #    and deletion_jobs converged to SUCCEEDED!
                sync_status_path = f"/v1/sync-runs/{sync_run_id}"
                r_sync_done = api_client.get(
                    sync_status_path,
                    headers=_origin_headers(
                        "GET",
                        sync_status_path,
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                        },
                    ),
                )
                assert r_sync_done.status_code == 200, r_sync_done.text
                assert r_sync_done.json()["status"] == "SUCCESS"
                assert r_sync_done.json()["import_ready"] is True

                # Abandoning a SUCCESS run must be rejected with 409
                abandon_succ_path = f"/v1/sync-runs/{sync_run_id}/abandon"
                r_abandon_succ = api_client.post(
                    abandon_succ_path,
                    headers=_origin_headers(
                        "POST",
                        abandon_succ_path,
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                        },
                    ),
                )
                assert r_abandon_succ.status_code == 409

                # 7. Verify PLAN_DRIFT unretryable run blocks duplicate POST /v1/sync-runs (409)
                #    until explicitly abandoned via POST /v1/sync-runs/{id}/abandon -> 200 -> 202!
                enrolled_2 = enroll_pilot_user(
                    admin_dsn,
                    auth_issuer=issuer,
                    auth_subject="sub-enrolled-pilot-2",
                    workspace_name="Second Pilot Workspace",
                    email="pilot2@example.test",
                    display_name="Second Pilot Owner",
                    github_login="seoyeon-dev",
                    github_external_id="904210",
                    public_repositories=[("700101", "seoyeon-dev/ledger-engine")],
                    allow_insecure_local_test=True,
                )
                auth_server.user_subject_id = "sub-enrolled-pilot-2"
                enrolled_2_jwt = _sign_supabase_jwt(
                    keypair, issuer=issuer, subject="sub-enrolled-pilot-2"
                )
                res3_id = enrolled_2.source_resource_ids[0]

                r_sync_drift = api_client.post(
                    "/v1/sync-runs",
                    headers=_origin_headers(
                        "POST",
                        "/v1/sync-runs",
                        {
                            "Authorization": f"Bearer {enrolled_2_jwt}",
                            "X-Workspace-ID": enrolled_2.workspace_id,
                            "Idempotency-Key": str(uuid.uuid4()),
                        },
                    ),
                    json={"source_resource_id": res3_id, "mode": "INITIAL_BACKFILL"},
                )
                assert r_sync_drift.status_code == 202, r_sync_drift.text
                drifted_run_id = r_sync_drift.json()["target_id"]

                from career_memory_api.sync_runs.models import SyncRunObservation, SyncRunStatus
                from career_memory_api.sync_runs.postgres_service import PostgresSyncRunService

                worker_obs_service = PostgresSyncRunService(
                    connection_factory=lambda: psycopg.connect(worker_dsn, autocommit=False),
                    worker_connection_factory=lambda: psycopg.connect(worker_dsn, autocommit=False),
                )
                worker_obs_service.record_worker_observation(
                    enrolled_2.workspace_id,
                    drifted_run_id,
                    SyncRunObservation(
                        status=SyncRunStatus.PARTIAL,
                        failure_reason="PLAN_DRIFT",
                        source_drift=True,
                    ),
                )

                r_dup_blocked = api_client.post(
                    "/v1/sync-runs",
                    headers=_origin_headers(
                        "POST",
                        "/v1/sync-runs",
                        {
                            "Authorization": f"Bearer {enrolled_2_jwt}",
                            "X-Workspace-ID": enrolled_2.workspace_id,
                            "Idempotency-Key": str(uuid.uuid4()),
                        },
                    ),
                    json={"source_resource_id": res3_id, "mode": "INITIAL_BACKFILL"},
                )
                assert r_dup_blocked.status_code == 409

                abandon_drift_path = f"/v1/sync-runs/{drifted_run_id}/abandon"
                r_abandon_drift = api_client.post(
                    abandon_drift_path,
                    headers=_origin_headers(
                        "POST",
                        abandon_drift_path,
                        {
                            "Authorization": f"Bearer {enrolled_2_jwt}",
                            "X-Workspace-ID": enrolled_2.workspace_id,
                        },
                    ),
                )
                assert r_abandon_drift.status_code == 200, r_abandon_drift.text
                assert r_abandon_drift.json()["is_abandoned"] is True

                # Idempotent second call returns 200
                r_abandon_idem = api_client.post(
                    abandon_drift_path,
                    headers=_origin_headers(
                        "POST",
                        abandon_drift_path,
                        {
                            "Authorization": f"Bearer {enrolled_2_jwt}",
                            "X-Workspace-ID": enrolled_2.workspace_id,
                        },
                    ),
                )
                assert r_abandon_idem.status_code == 200
                assert r_abandon_idem.json()["is_abandoned"] is True

                # Fresh initial import on res3 now succeeds with 202 Accepted!
                r_sync_fresh = api_client.post(
                    "/v1/sync-runs",
                    headers=_origin_headers(
                        "POST",
                        "/v1/sync-runs",
                        {
                            "Authorization": f"Bearer {enrolled_2_jwt}",
                            "X-Workspace-ID": enrolled_2.workspace_id,
                            "Idempotency-Key": str(uuid.uuid4()),
                        },
                    ),
                    json={"source_resource_id": res3_id, "mode": "INITIAL_BACKFILL"},
                )
                assert r_sync_fresh.status_code == 202, r_sync_fresh.text
                auth_server.user_subject_id = "sub-enrolled-pilot"

                del_status_path = f"/v1/deletion-jobs/{deletion_job_id}"
                r_del_done = api_client.get(
                    del_status_path,
                    headers=_origin_headers(
                        "GET",
                        del_status_path,
                        {
                            "Authorization": f"Bearer {enrolled_jwt}",
                            "X-Workspace-ID": enrolled.workspace_id,
                        },
                    ),
                )
                assert r_del_done.status_code == 200, r_del_done.text
                assert r_del_done.json()["status"] == "SUCCEEDED"
                assert r_del_done.json()["fence_active"] is True
    finally:
        auth_server.close()
        oidc_server.close()
