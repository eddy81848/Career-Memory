"""End-to-end T-103B RefetchResolverService + GitHubResolverContentFetcher + local PG18 tests."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx2
import psycopg
import pytest

from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    SourceObjectKind,
    canonicalize_source_bytes,
    parse_content_units,
)
from career_memory_api.github_provider import (
    GitHubResolverContentFetcher,
    HttpGitHubImportClient,
    Httpx2GitHubWireTransport,
    SafeGitHubHttpTransport,
    build_frozen_import_plan,
)
from career_memory_api.resolver import (
    OperationBudgetConfig,
    PostgresProvenanceMetadataStore,
    ProviderFetchOutcomeKind,
    ProviderFetchRequest,
    RefetchRequestInput,
    RefetchResolverService,
    ResolutionStatus,
)
from tests.resolver.pg_fixtures import EphemeralResolverPg18Cluster, run_ephemeral_pg18_cluster

_ISSUER = "https://auth.career-memory.local/auth/v1"
_FIXED_NOW = datetime(2026, 11, 27, 15, 30, 0, tzinfo=UTC)
_CANARY_E2E = "CANARY_T202_PG18_RESOLVER_ZERO_PERSISTENCE_8A4F1C"
_SECRET_TOKEN = "gho_T202_PG18_RESOLVER_BEARER_SECRET_11223344"
_REPO_ID = "777888999"
_PR_NUMBER = "42"
_HEAD_SHA = "a" * 40


def test_resolver_fetcher_supports_commit_comment_review_and_diff_objects() -> None:
    store = InMemorySecretStore()
    token_ref = store.store_secret("github-oauth-token", _SECRET_TOKEN)
    diff_text = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"

    def _mock(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("accept") == "application/vnd.github.v3.diff":
            return httpx2.Response(
                200,
                content=diff_text.encode("utf-8"),
                headers={"Content-Type": "application/vnd.github.v3.diff"},
            )
        if request.url.path.endswith(f"/commits/{_HEAD_SHA}"):
            return httpx2.Response(
                200,
                json={"sha": _HEAD_SHA, "commit": {"message": "feat: add parser\n"}},
            )
        if request.url.path.endswith("/issues/comments/500"):
            return httpx2.Response(200, json={"id": 500, "body": "Ship it!\n"})
        if request.url.path.endswith("/pulls/comments/600"):
            return httpx2.Response(200, json={"id": 600, "body": "Nit: rename var\n"})
        return httpx2.Response(404)

    http_client = httpx2.Client(transport=httpx2.MockTransport(_mock), follow_redirects=False)
    try:
        transport = SafeGitHubHttpTransport(
            wire_transport=Httpx2GitHubWireTransport(http_client),
            secret_store=store,
            clock=lambda: _FIXED_NOW,
        )
        fetcher = GitHubResolverContentFetcher(transport=transport)

        # Commit message
        res_commit = fetcher.fetch(
            ProviderFetchRequest(
                provider="GITHUB",
                endpoint_url=f"https://api.github.com/repositories/{_REPO_ID}/commits/{_HEAD_SHA}",
                provider_resource_id=_REPO_ID,
                resource_name="acme/core",
                object_type="COMMIT",
                provider_object_id=_HEAD_SHA,
                provider_revision_id=_HEAD_SHA,
                token_secret_ref=token_ref,
                operation_id="op-commit",
                max_bytes=1024 * 1024,
                timeout_seconds=5.0,
                source_object_revision_id="rev-1",
                expected_revision_hash="sha256:" + "0" * 64,
            )
        )
        assert res_commit.kind is ProviderFetchOutcomeKind.OK
        assert res_commit.raw_bytes == b"feat: add parser\n"

        # Issue/PR comment
        res_comment = fetcher.fetch(
            ProviderFetchRequest(
                provider="GITHUB",
                endpoint_url=f"https://api.github.com/repositories/{_REPO_ID}/issues/comments/500",
                provider_resource_id=_REPO_ID,
                resource_name="acme/core",
                object_type="COMMENT",
                provider_object_id="500",
                provider_revision_id=None,
                token_secret_ref=token_ref,
                operation_id="op-comment",
                max_bytes=1024 * 1024,
                timeout_seconds=5.0,
                source_object_revision_id="rev-2",
                expected_revision_hash="sha256:" + "0" * 64,
            )
        )
        assert res_comment.kind is ProviderFetchOutcomeKind.OK
        assert res_comment.raw_bytes == b"Ship it!\n"

        # Review comment
        res_review = fetcher.fetch(
            ProviderFetchRequest(
                provider="GITHUB",
                endpoint_url=f"https://api.github.com/repositories/{_REPO_ID}/pulls/comments/600",
                provider_resource_id=_REPO_ID,
                resource_name="acme/core",
                object_type="REVIEW",
                provider_object_id="600",
                provider_revision_id=None,
                token_secret_ref=token_ref,
                operation_id="op-review",
                max_bytes=1024 * 1024,
                timeout_seconds=5.0,
                source_object_revision_id="rev-3",
                expected_revision_hash="sha256:" + "0" * 64,
            )
        )
        assert res_review.kind is ProviderFetchOutcomeKind.OK
        assert res_review.raw_bytes == b"Nit: rename var\n"

        # Unified diff via Accept: application/vnd.github.v3.diff
        res_diff = fetcher.fetch(
            ProviderFetchRequest(
                provider="GITHUB",
                endpoint_url=f"https://api.github.com/repositories/{_REPO_ID}/pulls/{_PR_NUMBER}",
                provider_resource_id=_REPO_ID,
                resource_name="acme/core",
                object_type="PR",
                provider_object_id=_PR_NUMBER,
                provider_revision_id=None,
                token_secret_ref=token_ref,
                operation_id="op-diff",
                max_bytes=1024 * 1024,
                timeout_seconds=5.0,
                source_object_revision_id="rev-4",
                expected_revision_hash="sha256:" + "0" * 64,
                is_diff=True,
            )
        )
        assert res_diff.kind is ProviderFetchOutcomeKind.OK
        assert res_diff.raw_bytes == diff_text.encode("utf-8")
        assert res_diff.mime_type == "text/x-diff"
    finally:
        http_client.close()


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralResolverPg18Cluster]:
    yield from run_ephemeral_pg18_cluster()


def _seed_pg18_from_observed_revision(
    admin_dsn: str,
    *,
    user_id: str,
    auth_subject: str,
    workspace_id: str,
    token_secret_ref: str,
    provider_resource_id: str,
    provider_object_id: str,
    revision_content_hash: str,
    normalized_metadata: dict[str, Any],
    raw_body_for_unit_anchors_only: bytes,
) -> dict[str, str]:
    canon = canonicalize_source_bytes(raw_body_for_unit_anchors_only)
    assert canon.status == "CANONICALIZED" and canon.document is not None
    assert canon.document.metadata.content_sha256 == revision_content_hash
    parsed = parse_content_units(canon.document, SourceObjectKind.PR_BODY)
    assert len(parsed.persisted_metadata) >= 2

    account_id = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())
    object_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    unit_ids: list[str] = []

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                VALUES (%s::uuid, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, _ISSUER, auth_subject, f"{auth_subject}@local.test", auth_subject),
            )
            cur.execute(
                """
                INSERT INTO public.workspaces (id, owner_user_id, name)
                VALUES (%s::uuid, %s::uuid, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (workspace_id, user_id, f"ws-{workspace_id[:8]}"),
            )
            cur.execute(
                """
                INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                VALUES (%s::uuid, %s::uuid, 'OWNER')
                ON CONFLICT (workspace_id, user_id) DO NOTHING
                """,
                (workspace_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO public.source_accounts
                    (id, workspace_id, provider, external_account_id, token_secret_ref, scopes, status)
                VALUES (%s::uuid, %s::uuid, 'GITHUB', %s, %s, ARRAY['public_repo'], 'ACTIVE')
                """,
                (account_id, workspace_id, f"gh-{workspace_id[:8]}", token_secret_ref),
            )
            cur.execute(
                """
                INSERT INTO public.source_resources
                    (id, workspace_id, source_account_id, provider_resource_id, resource_type,
                     name, canonical_url, visibility, processing_mode, raw_storage_mode,
                     raw_retention_seconds, status, policy_version)
                VALUES
                    (%s::uuid, %s::uuid, %s::uuid, %s, 'GITHUB_REPOSITORY',
                     'acme/career-core', 'https://github.com/acme/career-core', 'PUBLIC', 'PUBLIC_CLOUD',
                     'NONE', NULL, 'ACTIVE', 'policy-v1')
                """,
                (resource_id, workspace_id, account_id, provider_resource_id),
            )
            cur.execute(
                """
                INSERT INTO public.source_objects
                    (id, workspace_id, source_resource_id, object_type, provider_object_id, lifecycle_state)
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s, 'ACTIVE')
                """,
                (object_id, workspace_id, resource_id, provider_object_id),
            )
            cur.execute(
                """
                INSERT INTO public.source_object_revisions
                    (id, workspace_id, source_object_id, provider_revision_id, content_hash,
                     hash_policy_version, normalized_metadata, connector_version, schema_version)
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'etag-pr-42', %s, %s, %s::jsonb, 'cm-github-rest-v1', 'cm-github-meta-v1')
                """,
                (
                    revision_id,
                    workspace_id,
                    object_id,
                    revision_content_hash,
                    CANONICALIZATION_VERSION,
                    json.dumps(normalized_metadata),
                ),
            )
            for idx, meta in enumerate(parsed.persisted_metadata):
                cu_id = str(uuid.uuid4())
                unit_ids.append(cu_id)
                anchor = meta.to_persisted_anchor_dict()
                anchor.pop("text_content", None)
                anchor.pop("content_ref", None)
                anchor.pop("retention_class", None)
                cur.execute(
                    """
                    INSERT INTO public.content_units
                        (id, workspace_id, source_object_revision_id, unit_type, anchor,
                         text_content, content_ref, content_hash, canonicalization_version,
                         retention_class, expires_at, ordinal, policy_state)
                    VALUES
                        (%s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb,
                         NULL, NULL, %s, %s, 'NONE', NULL, %s, 'ACTIVE')
                    """,
                    (
                        cu_id,
                        workspace_id,
                        revision_id,
                        meta.unit_type.value,
                        json.dumps(anchor),
                        meta.locator.span_sha256,
                        CANONICALIZATION_VERSION,
                        idx,
                    ),
                )

    return {
        "resource_id": resource_id,
        "revision_id": revision_id,
        "unit_0_id": unit_ids[0],
        "unit_1_id": unit_ids[1],
    }


def _dump_entire_public_schema(admin_dsn: str) -> str:
    chunks: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            for (table,) in cur.fetchall():
                cur.execute(f"SELECT row_to_json(t)::text FROM public.{table} AS t")
                for (row_json,) in cur.fetchall():
                    chunks.append(f"{table}:{row_json}")
    return "\n".join(chunks)


@pytest.mark.postgres
def test_pg18_end_to_end_github_import_to_refetch_resolver_and_error_modes(
    pg18_cluster: EphemeralResolverPg18Cluster,
    caplog: Any,
) -> None:
    store = InMemorySecretStore()
    token_ref = store.store_secret("github-oauth-token", _SECRET_TOKEN)
    pr_body = (
        f"# Summary\r\nIntro section.\r\n\r\n"
        f"## Implementation\r\nVerified {_CANARY_E2E} implementation evidence.\r\n\r\n"
        f"## Verification\r\nAll checks passed.\r\n"
    )

    mode_box = ["OK"]
    rate_limit_attempts = [0]

    def _mock_github(request: httpx2.Request) -> httpx2.Response:
        assert request.headers.get("authorization") == f"Bearer {_SECRET_TOKEN}"
        path = request.url.path
        if path == f"/repositories/{_REPO_ID}" or path == "/repos/acme/career-core":
            return httpx2.Response(
                200,
                json={
                    "id": int(_REPO_ID),
                    "name": "career-core",
                    "full_name": "acme/career-core",
                    "owner": {"login": "acme"},
                    "html_url": "https://github.com/acme/career-core",
                    "private": False,
                    "visibility": "public",
                    "default_branch": "main",
                },
            )
        if path == f"/repositories/{_REPO_ID}/branches/main":
            return httpx2.Response(200, json={"name": "main", "commit": {"sha": _HEAD_SHA}})
        if path == f"/repositories/{_REPO_ID}/pulls/{_PR_NUMBER}":
            mode = mode_box[0]
            if mode == "OK":
                return httpx2.Response(
                    200,
                    json={
                        "number": int(_PR_NUMBER),
                        "state": "closed",
                        "body": pr_body,
                        "merged": True,
                        "merged_at": "2026-10-15T10:00:00Z",
                        "updated_at": "2026-10-15T10:00:00Z",
                        "base": {
                            "ref": "main",
                            "sha": "e" * 40,
                            "repo": {"private": False, "visibility": "public"},
                        },
                        "head": {"ref": "feat/t202", "sha": "f" * 40},
                        "user": {"id": 1001, "login": "alice"},
                        "html_url": "https://github.com/acme/career-core/pull/42",
                    },
                    headers={"ETag": '"etag-pr-42"'},
                )
            if mode == "MISMATCH":
                return httpx2.Response(
                    200,
                    json={
                        "number": int(_PR_NUMBER),
                        "state": "closed",
                        "body": "# Summary\nEdited remote body.\n",
                        "base": {"repo": {"private": False, "visibility": "public"}},
                    },
                )
            if mode == "PRIVATE":
                return httpx2.Response(
                    200,
                    json={
                        "number": int(_PR_NUMBER),
                        "state": "closed",
                        "body": pr_body,
                        "base": {"repo": {"private": True, "visibility": "private"}},
                    },
                )
            if mode == "NOT_FOUND":
                return httpx2.Response(404, json={"message": f"Not Found {_CANARY_E2E}"})
            if mode == "RATE_LIMIT_THEN_OK":
                rate_limit_attempts[0] += 1
                if rate_limit_attempts[0] == 1:
                    return httpx2.Response(
                        429,
                        headers={"Retry-After": "1"},
                        json={"message": f"Slow down {_CANARY_E2E}"},
                    )
                return httpx2.Response(
                    200,
                    json={
                        "number": int(_PR_NUMBER),
                        "state": "closed",
                        "body": pr_body,
                        "base": {"repo": {"private": False, "visibility": "public"}},
                    },
                )
            if mode == "RATE_LIMIT_ALWAYS":
                return httpx2.Response(429, headers={"Retry-After": "60"}, json={"message": "limit"})
            if mode == "PARTIAL_206":
                return httpx2.Response(206, content=b'{"body": "truncated')
            if mode == "SSRF_REDIRECT":
                return httpx2.Response(
                    302,
                    headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                )
            if mode == "UNAUTHORIZED_401":
                return httpx2.Response(401, json={"message": "Bad credentials"})
        return httpx2.Response(404)

    http_client = httpx2.Client(transport=httpx2.MockTransport(_mock_github), follow_redirects=False)
    try:
        transport = SafeGitHubHttpTransport(
            wire_transport=Httpx2GitHubWireTransport(http_client),
            secret_store=store,
            clock=lambda: _FIXED_NOW,
        )
        import_client = HttpGitHubImportClient(transport=transport, clock=lambda: _FIXED_NOW)
        fetcher = GitHubResolverContentFetcher(transport=transport)

        # 1) Initial import produces strictly non-raw ObservedGitHubRevision
        snap = import_client.fetch_repository_snapshot(
            owner="acme",
            repo="career-core",
            provider_resource_id=_REPO_ID,
            token_secret_ref=token_ref,
        )
        plan = build_frozen_import_plan(
            source_resource_id=str(uuid.uuid4()),
            repo_snapshot=snap,
            cutoff_at=_FIXED_NOW,
        )
        observed = import_client.fetch_pull_request_detail(
            plan, pr_number=int(_PR_NUMBER), token_secret_ref=token_ref
        )
        assert _CANARY_E2E not in json.dumps(observed.to_metadata_record_dict())

        # 2) Seed PostgreSQL 18 with ONLY non-raw metadata & anchors
        user_id = str(uuid.uuid4())
        ws_id = str(uuid.uuid4())
        seeded = _seed_pg18_from_observed_revision(
            pg18_cluster.admin_dsn,
            user_id=user_id,
            auth_subject="sub-t202-e2e",
            workspace_id=ws_id,
            token_secret_ref=token_ref,
            provider_resource_id=_REPO_ID,
            provider_object_id=_PR_NUMBER,
            revision_content_hash=observed.content_hash,
            normalized_metadata=observed.normalized_metadata,
            raw_body_for_unit_anchors_only=pr_body.encode("utf-8"),
        )

        access = WorkspaceAccessContext(
            user=AuthenticatedUser(
                user_id=user_id,
                auth_subject=AuthSubject(issuer=_ISSUER, subject="sub-t202-e2e"),
            ),
            membership=WorkspaceMembership(workspace_id=ws_id, user_id=user_id, role="OWNER"),
        )

        with caplog.at_level(logging.DEBUG):
            with psycopg.connect(pg18_cluster.api_dsn) as api_conn:
                pg_store = PostgresProvenanceMetadataStore(api_conn)
                resolver = RefetchResolverService(
                    metadata_store=pg_store,
                    fetcher=fetcher,
                    default_budget=OperationBudgetConfig(max_retries=1),
                    clock=lambda: _FIXED_NOW,
                )

                # Case A: MATCHED (same Revision + Span hash verified live via GitHubResolverContentFetcher)
                mode_box[0] = "OK"
                with resolver.open_operation_session(access, operation_id="op-matched") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-matched",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.MATCHED
                        assert res.metadata.observed_revision_hash == observed.content_hash
                        assert res.ephemeral_unit is not None
                        assert _CANARY_E2E in res.ephemeral_unit.text
                    # After context exit, ephemeral text is cleared
                    assert res.closed is True
                    assert res.ephemeral_unit is None

                # Case B: REVISION_MISMATCH (remote PR edited)
                mode_box[0] = "MISMATCH"
                with resolver.open_operation_session(access, operation_id="op-mismatch") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-mismatch",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.REVISION_MISMATCH
                        assert res.metadata.retryable is False

                # Case C: Visibility flipped to PRIVATE in GitHub payload -> POLICY_BLOCKED
                mode_box[0] = "PRIVATE"
                with resolver.open_operation_session(access, operation_id="op-private") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-private",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.POLICY_BLOCKED
                        assert res.metadata.reason_code == "RESOURCE_VISIBILITY_CHANGED"

                # Case D: Provider 404 -> UNAVAILABLE (retryable=False, ambiguous, not deletion)
                mode_box[0] = "NOT_FOUND"
                with resolver.open_operation_session(access, operation_id="op-404") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-404",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.UNAVAILABLE
                        assert res.metadata.retryable is False
                        assert res.metadata.reason_code == "PROVIDER_OBJECT_NOT_FOUND"

                # Case E: Provider 429 transient retry -> recovers to MATCHED
                mode_box[0] = "RATE_LIMIT_THEN_OK"
                with resolver.open_operation_session(access, operation_id="op-429-ok") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-429-ok",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.MATCHED
                        assert res.metadata.requests_used == 2

                # Case F: Provider 429 exhausted -> UNAVAILABLE (retryable=True)
                mode_box[0] = "RATE_LIMIT_ALWAYS"
                with resolver.open_operation_session(access, operation_id="op-429-fail") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-429-fail",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.UNAVAILABLE
                        assert res.metadata.retryable is True

                # Case G: Provider 206 Partial Content -> PARTIAL_WITH_LIMIT
                mode_box[0] = "PARTIAL_206"
                with resolver.open_operation_session(access, operation_id="op-206") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-206",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.PARTIAL_WITH_LIMIT

                # Case H: Provider 302 SSRF redirect -> POLICY_BLOCKED (REDIRECT_BLOCKED)
                mode_box[0] = "SSRF_REDIRECT"
                with resolver.open_operation_session(access, operation_id="op-ssrf") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-ssrf",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.POLICY_BLOCKED
                        assert res.metadata.reason_code == "REDIRECT_BLOCKED"

                # Case I: Provider 401 -> POLICY_BLOCKED (PROVIDER_AUTH_REVOKED)
                mode_box[0] = "UNAUTHORIZED_401"
                with resolver.open_operation_session(access, operation_id="op-401") as sess:
                    with resolver.resolve_unit(
                        sess,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=seeded["unit_1_id"],
                            operation_id="op-401",
                        ),
                    ) as res:
                        assert res.metadata.status is ResolutionStatus.POLICY_BLOCKED
                        assert res.metadata.reason_code == "PROVIDER_AUTH_REVOKED"

        # 3) Zero-leak audit across entire PostgreSQL 18 schema, logs, and adapter state!
        db_dump = _dump_entire_public_schema(pg18_cluster.admin_dsn)
        logs = "\n".join(rec.getMessage() for rec in caplog.records)
        adapter_repr = f"{fetcher!r} {transport!r} {import_client!r}"

        assert _CANARY_E2E not in db_dump
        assert _CANARY_E2E not in logs
        assert _CANARY_E2E not in adapter_repr
        assert _SECRET_TOKEN not in db_dump
        assert _SECRET_TOKEN not in logs
        assert _SECRET_TOKEN not in adapter_repr
    finally:
        http_client.close()
