"""HTTP contract tests for M2-T201 GitHub Connect FastAPI router (AUTH-02 & POLICY-01)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.auth.redaction import RedactingLogFilter
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.github_connect.models import (
    GitHubInspectionOutcome,
    GitHubRepositoryInspection,
    GitHubRepositoryMetadata,
)
from career_memory_api.github_connect.postgres_service import (
    PostgresGitHubConnectService,
)
from career_memory_api.github_connect.router import create_github_connect_app

from .pg_harness import (
    EphemeralPg18Cluster,
    seed_provenance_and_active_work,
    seed_workspace_member,
)
from .test_postgres_github_connect_contract import FakeGitHubCredentialProvider

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralPg18Cluster]:
    cluster = EphemeralPg18Cluster(prefix="cm-m2-t201-http-pg18-")
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def _clean_db(pg18_cluster: EphemeralPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _make_client(
    cluster: EphemeralPg18Cluster,
    *,
    access_by_token: dict[str, WorkspaceAccessContext],
    provider: FakeGitHubCredentialProvider,
    secret_store: InMemorySecretStore,
) -> TestClient:
    service = PostgresGitHubConnectService(
        connection_factory=cluster.api_connection,
        credential_provider=provider,
        secret_store=secret_store,
        allowed_redirect_uris=("https://app.career-memory.local/oauth/github/callback",),
    )

    def _resolve_access(request: Request) -> WorkspaceAccessContext:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            from career_memory_api.authz import UnauthenticatedError

            raise UnauthenticatedError()
        token = auth.removeprefix("Bearer ").strip()
        if token not in access_by_token:
            from career_memory_api.authz import UnauthenticatedError

            raise UnauthenticatedError()
        return access_by_token[token]

    app = create_github_connect_app(service=service, resolve_access=_resolve_access)
    return TestClient(app)


def test_http_full_lifecycle_headers_concealment_and_zero_token_leakage_in_logs(
    pg18_cluster: EphemeralPg18Cluster,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner_a = seed_workspace_member(pg18_cluster, workspace_name="WS A", role="OWNER")
    viewer_a = seed_workspace_member(
        pg18_cluster,
        workspace_id=owner_a.workspace_id,
        role="VIEWER",
    )
    owner_b = seed_workspace_member(pg18_cluster, workspace_name="WS B", role="OWNER")

    provider = FakeGitHubCredentialProvider()
    provider.next_access_token = "gho_super_secret_http_token_11223344"
    provider.set_public_repo("acme/http-public-repo", provider_resource_id="gh-http-1")
    secret_store = InMemorySecretStore()

    client = _make_client(
        pg18_cluster,
        access_by_token={
            "tok-owner-a": owner_a,
            "tok-viewer-a": viewer_a,
            "tok-owner-b": owner_b,
        },
        provider=provider,
        secret_store=secret_store,
    )

    # Attach redacting filter to capture logs cleanly
    gh_logger = logging.getLogger("career_memory_api.github_connect")
    redacting_filter = RedactingLogFilter()
    gh_logger.addFilter(redacting_filter)
    caplog.set_level(logging.DEBUG, logger="career_memory_api.github_connect")

    try:
        # 1. VIEWER cannot initiate OAuth connect
        r_viewer = client.post(
            "/v1/sources/github/oauth/start",
            headers={"Authorization": "Bearer tok-viewer-a", "X-Request-ID": "req-viewer"},
            json={
                "return_path": "/settings/sources",
                "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
            },
        )
        assert r_viewer.status_code == 403
        assert r_viewer.headers["Cache-Control"] == "private, no-store"
        assert r_viewer.json()["error"]["code"] == "WORKSPACE_SCOPE_VIOLATION"

        # 2. Open redirect blocked on start
        r_redir = client.post(
            "/v1/sources/github/oauth/start",
            headers={"Authorization": "Bearer tok-owner-a"},
            json={
                "return_path": "https://evil.example/steal",
                "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
            },
        )
        assert r_redir.status_code == 400
        assert r_redir.headers["Cache-Control"] == "private, no-store"
        assert r_redir.json()["error"]["code"] == "INVALID_RETURN_PATH"

        # 3. Start OAuth (read:user only)
        r_start = client.post(
            "/v1/sources/github/oauth/start",
            headers={"Authorization": "Bearer tok-owner-a", "X-Request-ID": "req-start-1"},
            json={
                "return_path": "/settings/sources",
                "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
            },
        )
        assert r_start.status_code == 201
        assert r_start.headers["Cache-Control"] == "private, no-store"
        assert r_start.headers["X-Request-ID"] == "req-start-1"
        start_body = r_start.json()
        assert start_body["requested_scopes"] == ["read:user"]
        assert "scope=read%3Auser" in start_body["authorization_url"]
        raw_state = start_body["state"]

        # 4. Callback missing Idempotency-Key -> 422
        r_no_idem = client.post(
            "/v1/sources/github/oauth/callback",
            headers={"Authorization": "Bearer tok-owner-a"},
            json={
                "state": raw_state,
                "code": "gh-code-secret-http",
                "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
            },
        )
        assert r_no_idem.status_code == 422
        assert r_no_idem.headers["Cache-Control"] == "private, no-store"
        assert r_no_idem.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"

        # 5. Callback succeeds with canonical UUID Idempotency-Key
        idem_key = str(uuid.uuid4())
        r_cb = client.post(
            "/v1/sources/github/oauth/callback",
            headers={
                "Authorization": "Bearer tok-owner-a",
                "Idempotency-Key": idem_key,
                "X-Request-ID": "req-cb-1",
            },
            json={
                "state": raw_state,
                "code": "gh-code-secret-http",
                "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
            },
        )
        assert r_cb.status_code == 200
        assert r_cb.headers["Cache-Control"] == "private, no-store"
        cb_body = r_cb.json()
        account_id = cb_body["source_account"]["id"]
        assert cb_body["source_account"]["scopes"] == ["read:user"]
        assert "token_secret_ref" not in cb_body["source_account"]
        assert provider.next_access_token not in r_cb.text

        # 6. Register public repo
        r_res = client.post(
            "/v1/source-resources",
            headers={"Authorization": "Bearer tok-owner-a"},
            json={
                "source_account_id": account_id,
                "repository_full_name": "acme/http-public-repo",
            },
        )
        assert r_res.status_code == 201
        assert r_res.headers["Cache-Control"] == "private, no-store"
        resource_id = r_res.json()["id"]
        assert r_res.json()["visibility"] == "PUBLIC"
        assert r_res.json()["processing_mode"] == "PUBLIC_CLOUD"
        assert r_res.json()["raw_storage_mode"] == "NONE"

        # 7. GET list endpoints (OWNER and VIEWER allowed, private no-store, no secrets)
        r_accts = client.get(
            "/v1/source-accounts",
            headers={"Authorization": "Bearer tok-viewer-a"},
        )
        assert r_accts.status_code == 200
        assert r_accts.headers["Cache-Control"] == "private, no-store"
        assert len(r_accts.json()["items"]) == 1
        assert "token_secret_ref" not in r_accts.text

        r_resources = client.get(
            "/v1/source-resources",
            headers={"Authorization": "Bearer tok-viewer-a"},
        )
        assert r_resources.status_code == 200
        assert r_resources.headers["Cache-Control"] == "private, no-store"
        assert len(r_resources.json()["items"]) == 1

        # 8. Cross-workspace concealment returns 404 RESOURCE_NOT_FOUND
        r_cross_verify = client.post(
            f"/v1/source-resources/{resource_id}/verify-policy",
            headers={"Authorization": "Bearer tok-owner-b"},
        )
        assert r_cross_verify.status_code == 404
        assert r_cross_verify.headers["Cache-Control"] == "private, no-store"
        assert r_cross_verify.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        r_cross_disc = client.post(
            f"/v1/source-accounts/{account_id}/disconnect",
            headers={"Authorization": "Bearer tok-owner-b"},
        )
        assert r_cross_disc.status_code == 404
        assert r_cross_disc.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        # 9. Verify policy after Public -> Private transition
        seed_provenance_and_active_work(
            pg18_cluster,
            workspace_id=owner_a.workspace_id,
            source_resource_id=resource_id,
        )
        provider.repo_inspections["acme/http-public-repo"] = GitHubRepositoryInspection(
            outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
            http_status=200,
            observed_visibility="PRIVATE",
            repository=GitHubRepositoryMetadata(
                provider_resource_id="gh-http-1",
                full_name="acme/http-public-repo",
                canonical_url="https://github.com/acme/http-public-repo",
                private=True,
                visibility="private",
            ),
        )
        r_verify = client.post(
            f"/v1/source-resources/{resource_id}/verify-policy",
            headers={"Authorization": "Bearer tok-owner-a"},
        )
        assert r_verify.status_code == 200
        assert r_verify.headers["Cache-Control"] == "private, no-store"
        v_body = r_verify.json()
        assert v_body["policy_decision"] == "POLICY_BLOCKED"
        assert v_body["guidance_code"] == "POLICY_REVIEW_REQUIRED"
        assert v_body["tombstone_created"] is False
        assert v_body["resource"]["visibility"] == "PRIVATE"
        assert v_body["resource"]["status"] == "POLICY_REVIEW_REQUIRED"

        # 10. Disconnect source account
        r_disc = client.post(
            f"/v1/source-accounts/{account_id}/disconnect",
            headers={"Authorization": "Bearer tok-owner-a"},
        )
        assert r_disc.status_code == 200
        assert r_disc.headers["Cache-Control"] == "private, no-store"
        d_body = r_disc.json()
        assert d_body["source_account"]["status"] == "REVOKED"
        assert d_body["provider_grant_revoked"] is True
        assert d_body["secret_deleted"] is True

        # 11. Zero plaintext token / code / state bytes in captured logs
        all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
        assert provider.next_access_token not in all_logs
        assert raw_state not in all_logs
        assert "gh-code-secret-http" not in all_logs
    finally:
        gh_logger.removeFilter(redacting_filter)


def test_http_excessive_scope_returns_403_with_no_store_and_revokes_token(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    owner = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.exchange_scopes = ("read:user", "repo")
    provider.next_access_token = "gho_excessive_scope_token_555555"
    secret_store = InMemorySecretStore()

    client = _make_client(
        pg18_cluster,
        access_by_token={"tok-owner": owner},
        provider=provider,
        secret_store=secret_store,
    )

    r_start = client.post(
        "/v1/sources/github/oauth/start",
        headers={"Authorization": "Bearer tok-owner"},
        json={
            "return_path": "/settings/sources",
            "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
        },
    )
    assert r_start.status_code == 201
    state = r_start.json()["state"]

    r_cb = client.post(
        "/v1/sources/github/oauth/callback",
        headers={
            "Authorization": "Bearer tok-owner",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={
            "state": state,
            "code": "code-bad-scope",
            "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
        },
    )
    assert r_cb.status_code == 403
    assert r_cb.headers["Cache-Control"] == "private, no-store"
    err = r_cb.json()["error"]
    assert err["code"] == "EXCESSIVE_OAUTH_SCOPE"
    assert err["details"]["policy_action"] == "POLICY_REVIEW_REQUIRED"
    assert err["details"]["token_revoked"] is True
    assert provider.next_access_token not in r_cb.text
    assert provider.revoked_tokens == [provider.next_access_token]


def test_http_end_to_end_with_real_oauth_app_provider_and_offline_transport(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    """Wire Router -> PostgresGitHubConnectService -> GitHubOAuthAppCredentialProvider -> PG18."""
    from career_memory_api.github_connect.oauth_app_provider import (
        GitHubOAuthAppConfig,
        GitHubOAuthAppCredentialProvider,
    )
    from career_memory_api.github_connect.ports import (
        GitHubHttpRequest,
        GitHubHttpResponse,
    )

    class _QueueTransport:
        def __init__(self, responses: list[GitHubHttpResponse]) -> None:
            self._responses = list(responses)
            self.requests: list[GitHubHttpRequest] = []

        def send(self, request: GitHubHttpRequest) -> GitHubHttpResponse:
            self.requests.append(request)
            return self._responses.pop(0)

    owner = seed_workspace_member(pg18_cluster, role="OWNER")
    transport = _QueueTransport(
        [
            GitHubHttpResponse(
                status_code=200,
                json_body={
                    "access_token": "gho_real_adapter_token_abcdef012345",
                    "token_type": "bearer",
                    "scope": "read:user",
                },
            ),
            GitHubHttpResponse(
                status_code=200,
                headers={"X-OAuth-Scopes": "read:user"},
                json_body={"id": 777001, "login": "real-adapter-user"},
            ),
            GitHubHttpResponse(
                status_code=200,
                headers={"X-OAuth-Scopes": "read:user"},
                json_body={
                    "id": 888001,
                    "full_name": "acme/e2e-public",
                    "html_url": "https://github.com/acme/e2e-public",
                    "private": False,
                    "visibility": "public",
                    "default_branch": "main",
                },
            ),
            GitHubHttpResponse(status_code=204),
        ]
    )
    real_provider = GitHubOAuthAppCredentialProvider(
        config=GitHubOAuthAppConfig(
            client_id="Iv1.e2e_client",
            client_secret="ghs_e2e_client_secret_never_logged",
            allowed_redirect_uris=("https://app.career-memory.local/oauth/github/callback",),
        ),
        transport=transport,
    )
    service = PostgresGitHubConnectService(
        connection_factory=pg18_cluster.api_connection,
        credential_provider=real_provider,
        secret_store=InMemorySecretStore(),
        allowed_redirect_uris=("https://app.career-memory.local/oauth/github/callback",),
    )
    app = create_github_connect_app(service=service, resolve_access=lambda _req: owner)
    client = TestClient(app)

    # Invalid redirect_uri and invalid repo name rejected with 400
    r_bad_redir = client.post(
        "/v1/sources/github/oauth/start",
        json={"return_path": "/sources", "redirect_uri": "https://evil.example/cb"},
    )
    assert r_bad_redir.status_code == 400
    assert r_bad_redir.json()["error"]["code"] == "INVALID_REDIRECT_URI"

    r_start = client.post(
        "/v1/sources/github/oauth/start",
        json={
            "return_path": "/sources",
            "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
        },
    )
    assert r_start.status_code == 201
    state = r_start.json()["state"]

    r_cb = client.post(
        "/v1/sources/github/oauth/callback",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "state": state,
            "code": "e2e-code",
            "redirect_uri": "https://app.career-memory.local/oauth/github/callback",
            "repository_full_name": "acme/e2e-public",
        },
    )
    assert r_cb.status_code == 200
    body = r_cb.json()
    assert body["source_account"]["external_account_id"] == "777001"
    assert body["source_resource"]["name"] == "acme/e2e-public"
    assert body["source_resource"]["visibility"] == "PUBLIC"
    assert "gho_real_adapter_token" not in r_cb.text
    assert "ghs_e2e_client_secret" not in r_cb.text

    r_bad_repo = client.post(
        "/v1/source-resources",
        json={
            "source_account_id": body["source_account"]["id"],
            "repository_full_name": "acme/*",
        },
    )
    assert r_bad_repo.status_code == 400
    assert r_bad_repo.json()["error"]["code"] == "INVALID_REPOSITORY_NAME"

    r_disc = client.post(f"/v1/source-accounts/{body['source_account']['id']}/disconnect")
    assert r_disc.status_code == 200
    assert r_disc.json()["provider_grant_revoked"] is True
    assert transport.requests[-1].method == "DELETE"
    assert transport.requests[-1].url.endswith("/applications/Iv1.e2e_client/grant")

