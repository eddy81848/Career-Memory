"""Tests for SafeGitHubHttpTransport, HTTP error/SSRF classification, and HttpGitHubImportClient."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest

from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.canonicalization import CANONICALIZATION_VERSION, canonicalize_source_bytes
from career_memory_api.github_provider import (
    CompletionGateEvaluator,
    GitHubErrorCode,
    GitHubProviderError,
    GitHubStreamKind,
    HttpGitHubImportClient,
    Httpx2GitHubWireTransport,
    InMemoryWireTransport,
    SafeGitHubHttpTransport,
    WireHttpRequest,
    WireHttpResponse,
    build_frozen_import_plan,
)
from career_memory_api.schema_contracts.enums import (
    PrAssociationState,
    ResourceVisibility,
    SourceObjectType,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    validate_normalized_metadata,
)

_NOW = datetime(2026, 11, 27, 14, 0, 0, tzinfo=UTC)
_CANARY_BODY = "CANARY_GITHUB_IMPORT_RAW_BODY_NEVER_PERSIST_9F3D2A"
_CANARY_TITLE = "CANARY_GITHUB_TITLE_NEVER_LEAK_4A8C1B"
_CANARY_COMMIT_MSG = "CANARY_COMMIT_MESSAGE_NEVER_PERSIST_6E1B0C"
_CANARY_REVIEW_BODY = "CANARY_REVIEW_BODY_NEVER_PERSIST_2D7F8E"
_CANARY_COMMENT_BODY = "CANARY_COMMENT_BODY_NEVER_PERSIST_5C4A9D"
_CANARY_ISSUE_BODY = "CANARY_ISSUE_BODY_NEVER_PERSIST_8B2E3F"
_SECRET_TOKEN = "gho_SUPER_SECRET_TOKEN_CANARY_9876543210"
_HEAD_SHA = "c" * 40


def _json_response(
    payload: Any,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> WireHttpResponse:
    raw = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
    if headers:
        merged.update(headers)
    return WireHttpResponse(status_code=status_code, headers=merged, body=raw)


def _store_with_token() -> tuple[InMemorySecretStore, str]:
    store = InMemorySecretStore()
    ref = store.store_secret("github-oauth-token", _SECRET_TOKEN)
    return store, ref


def test_wire_request_is_strictly_read_only_and_redacts_secrets_and_bodies() -> None:
    with pytest.raises(ValueError, match="GET"):
        WireHttpRequest(
            method="POST",
            url="https://api.github.com/repos/acme/app",
            headers={},
            timeout_seconds=5.0,
            max_bytes=1024,
        )

    req = WireHttpRequest(
        method="GET",
        url="https://api.github.com/repos/acme/app",
        headers={"Authorization": f"Bearer {_SECRET_TOKEN}", "Accept": "application/vnd.github+json"},
        timeout_seconds=5.0,
        max_bytes=1024,
    )
    assert _SECRET_TOKEN not in repr(req)
    assert _SECRET_TOKEN not in str(req)

    resp = WireHttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=_CANARY_BODY.encode("utf-8"),
    )
    assert _CANARY_BODY not in repr(resp)
    assert _CANARY_BODY not in str(resp)


def test_transport_classifies_http_errors_redirects_partial_responses_and_leaks_zero_raw_text(
    caplog: Any,
) -> None:
    store, token_ref = _store_with_token()

    def _run_with_response(
        response: WireHttpResponse | Exception,
        *,
        url: str = "https://api.github.com/repositories/123/pulls/1",
        max_bytes: int = 65536,
    ) -> GitHubProviderError:
        def _handler(_req: WireHttpRequest) -> WireHttpResponse:
            if isinstance(response, Exception):
                raise response
            return response

        transport = SafeGitHubHttpTransport(
            wire_transport=InMemoryWireTransport(_handler),
            secret_store=store,
            clock=lambda: _NOW,
        )
        with pytest.raises(GitHubProviderError) as exc_info:
            transport.get(
                url,
                token_secret_ref=token_ref,
                max_bytes=max_bytes,
                follow_safe_redirects=True,
            )
        return exc_info.value

    with caplog.at_level(logging.DEBUG):
        # 1) 429 with Retry-After + X-RateLimit-Reset
        reset_epoch = int((_NOW + timedelta(seconds=120)).timestamp())
        err_429 = _run_with_response(
            WireHttpResponse(
                status_code=429,
                headers={
                    "Retry-After": "45",
                    "X-RateLimit-Reset": str(reset_epoch),
                },
                body=f'{{"message":"{_CANARY_BODY}"}}'.encode("utf-8"),
            )
        )
        assert err_429.code is GitHubErrorCode.RATE_LIMITED
        assert err_429.http_status == 429
        assert err_429.retryable is True
        assert err_429.retry_after_seconds == 45
        assert err_429.rate_limit_reset_at == datetime.fromtimestamp(reset_epoch, tz=UTC)

        # 2) 403 rate limit exhausted -> RATE_LIMITED
        err_403_rl = _run_with_response(
            WireHttpResponse(
                status_code=403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_epoch)},
                body=f'{{"message":"rate limit {_CANARY_BODY}"}}'.encode("utf-8"),
            )
        )
        assert err_403_rl.code is GitHubErrorCode.RATE_LIMITED
        assert err_403_rl.retryable is True

        # 3) 403 non-rate-limit -> FORBIDDEN_POLICY
        err_403 = _run_with_response(
            WireHttpResponse(
                status_code=403,
                headers={"X-RateLimit-Remaining": "4999"},
                body=f'{{"message":"forbidden {_CANARY_BODY}"}}'.encode("utf-8"),
            )
        )
        assert err_403.code is GitHubErrorCode.FORBIDDEN_POLICY
        assert err_403.retryable is False

        # 4) 401 -> AUTH_REVOKED
        err_401 = _run_with_response(
            WireHttpResponse(
                status_code=401,
                headers={},
                body=f'{{"message":"bad creds {_CANARY_BODY}"}}'.encode("utf-8"),
            )
        )
        assert err_401.code is GitHubErrorCode.AUTH_REVOKED
        assert err_401.retryable is False

        # 5) 404 -> NOT_FOUND_OR_INACCESSIBLE (ambiguous, not deletion)
        err_404 = _run_with_response(
            WireHttpResponse(
                status_code=404,
                headers={},
                body=f'{{"message":"not found {_CANARY_BODY}"}}'.encode("utf-8"),
            )
        )
        assert err_404.code is GitHubErrorCode.NOT_FOUND_OR_INACCESSIBLE
        assert err_404.retryable is False

        # 6) 206 Partial Content -> PARTIAL_RESPONSE
        err_206 = _run_with_response(
            WireHttpResponse(
                status_code=206,
                headers={"Content-Range": "bytes 0-10/100"},
                body=_CANARY_BODY.encode("utf-8"),
            )
        )
        assert err_206.code is GitHubErrorCode.PARTIAL_RESPONSE
        assert err_206.retryable is False

        # 7) Content-Length mismatch -> PARTIAL_RESPONSE
        err_cl = _run_with_response(
            WireHttpResponse(
                status_code=200,
                headers={"Content-Length": "9999"},
                body=_CANARY_BODY.encode("utf-8"),
            )
        )
        assert err_cl.code is GitHubErrorCode.PARTIAL_RESPONSE
        assert err_cl.retryable is False

        # 8) Byte limit exceeded -> BYTE_LIMIT_EXCEEDED
        err_bytes = _run_with_response(
            WireHttpResponse(
                status_code=200,
                headers={},
                body=_CANARY_BODY.encode("utf-8"),
                truncated=True,
            ),
            max_bytes=8,
        )
        assert err_bytes.code is GitHubErrorCode.BYTE_LIMIT_EXCEEDED
        assert err_bytes.retryable is False

        # 9) 302 redirect to metadata IP / external host -> REDIRECT_BLOCKED
        for bad_loc in (
            "http://169.254.169.254/latest/meta-data/",
            "https://evil.example.com/steal",
            "http://api.github.com/downgrade",
            "https://user:pass@api.github.com/creds",
        ):
            err_redir = _run_with_response(
                WireHttpResponse(
                    status_code=302,
                    headers={"Location": bad_loc},
                    body=_CANARY_BODY.encode("utf-8"),
                )
            )
            assert err_redir.code is GitHubErrorCode.REDIRECT_BLOCKED
            assert err_redir.retryable is False

        # 10) Timeout & connection errors
        err_timeout = _run_with_response(TimeoutError(f"timed out {_CANARY_BODY}"))
        assert err_timeout.code is GitHubErrorCode.TIMEOUT
        assert err_timeout.retryable is True

        err_conn = _run_with_response(ConnectionError(f"reset {_SECRET_TOKEN}"))
        assert err_conn.code is GitHubErrorCode.CONNECTION_ERROR
        assert err_conn.retryable is True

        # 11) Disallowed initial URL -> ENDPOINT_BLOCKED (zero wire call!)
        err_url = _run_with_response(
            WireHttpResponse(status_code=200, headers={}, body=b"{}"),
            url="http://127.0.0.1:8080/internal",
        )
        assert err_url.code is GitHubErrorCode.ENDPOINT_BLOCKED

        all_errors = (
            err_429,
            err_403_rl,
            err_403,
            err_401,
            err_404,
            err_206,
            err_cl,
            err_bytes,
            err_timeout,
            err_conn,
            err_url,
        )
        for exc in all_errors:
            assert _CANARY_BODY not in str(exc)
            assert _CANARY_BODY not in repr(exc)
            assert _SECRET_TOKEN not in str(exc)
            assert _SECRET_TOKEN not in repr(exc)

    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert _CANARY_BODY not in logs
    assert _SECRET_TOKEN not in logs


def test_httpx2_wire_transport_handles_safe_redirect_and_chunk_limit() -> None:
    store, token_ref = _store_with_token()
    calls: list[str] = []

    def _mock_handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        if request.url.path == "/repositories/123/old":
            return httpx2.Response(
                301,
                headers={"Location": "https://api.github.com/repositories/123/new"},
            )
        if request.url.path == "/repositories/123/new":
            payload = json.dumps({"ok": True}).encode("utf-8")
            return httpx2.Response(
                200,
                headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
                content=payload,
            )
        return httpx2.Response(404)

    client = httpx2.Client(transport=httpx2.MockTransport(_mock_handler), follow_redirects=False)
    try:
        wire = Httpx2GitHubWireTransport(client)
        transport = SafeGitHubHttpTransport(wire_transport=wire, secret_store=store)
        resp = transport.get(
            "https://api.github.com/repositories/123/old",
            token_secret_ref=token_ref,
            follow_safe_redirects=True,
        )
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"ok": True}
        assert calls == [
            "https://api.github.com/repositories/123/old",
            "https://api.github.com/repositories/123/new",
        ]
    finally:
        client.close()


def test_import_client_extracts_strict_non_raw_revisions_across_all_streams_and_gates_drift(
    caplog: Any,
) -> None:
    store, token_ref = _store_with_token()
    repo_id = "424242"
    pr_body_raw = f"# Summary\r\nFixes #77 and relates to #88.\r\n\r\n## Details\r\n{_CANARY_BODY}\r\n"
    expected_pr_canon = canonicalize_source_bytes(pr_body_raw.encode("utf-8"))
    assert expected_pr_canon.status == "CANONICALIZED" and expected_pr_canon.document is not None

    commit_msg_raw = f"feat: deterministic adapter\n\n{_CANARY_COMMIT_MSG}\n"
    expected_commit_canon = canonicalize_source_bytes(commit_msg_raw.encode("utf-8"))
    assert expected_commit_canon.document is not None

    review_body_raw = f"LGTM after check: {_CANARY_REVIEW_BODY}"
    comment_body_raw = f"Follow-up note: {_CANARY_COMMENT_BODY}"
    issue_body_raw = f"Issue spec: {_CANARY_ISSUE_BODY}"

    merged_at_iso = "2026-10-15T09:30:00Z"
    updated_at_iso = "2026-10-15T10:00:00Z"
    out_of_window_merged_iso = "2024-01-01T00:00:00Z"

    head_sha_box = [_HEAD_SHA]
    pr_updated_box = [updated_at_iso]

    def _mock_github(request: httpx2.Request) -> httpx2.Response:
        assert request.headers.get("authorization") == f"Bearer {_SECRET_TOKEN}"
        path = request.url.path
        if path == "/repos/acme/career-core" or path == f"/repositories/{repo_id}":
            return httpx2.Response(
                200,
                json={
                    "id": int(repo_id),
                    "name": "career-core",
                    "full_name": "acme/career-core",
                    "owner": {"login": "acme"},
                    "html_url": "https://github.com/acme/career-core",
                    "private": False,
                    "visibility": "public",
                    "default_branch": "trunk",
                    "updated_at": "2026-11-27T13:55:00Z",
                    "archived": False,
                    "fork": False,
                },
            )
        if path == f"/repositories/{repo_id}/branches/trunk":
            return httpx2.Response(
                200,
                json={"name": "trunk", "commit": {"sha": head_sha_box[0]}},
            )
        if path == f"/repositories/{repo_id}/pulls":
            return httpx2.Response(
                200,
                json=[
                    {
                        "number": 42,
                        "state": "closed",
                        "title": _CANARY_TITLE,
                        "body": pr_body_raw,
                        "draft": False,
                        "merged_at": merged_at_iso,
                        "closed_at": merged_at_iso,
                        "created_at": "2026-10-10T08:00:00Z",
                        "updated_at": pr_updated_box[0],
                        "merge_commit_sha": "d" * 40,
                        "base": {"ref": "trunk", "sha": "e" * 40},
                        "head": {"ref": "feat/import", "sha": "f" * 40},
                        "user": {"id": 1001, "login": "alice"},
                        "labels": [{"name": "backend"}, {"name": "m2"}],
                        "changed_files": 4,
                        "additions": 120,
                        "deletions": 15,
                        "commits": 2,
                        "comments": 1,
                        "review_comments": 1,
                        "html_url": "https://github.com/acme/career-core/pull/42",
                    },
                    {
                        # Unmerged closed PR -> must be skipped
                        "number": 41,
                        "state": "closed",
                        "title": "abandoned",
                        "body": _CANARY_BODY,
                        "merged_at": None,
                        "updated_at": updated_at_iso,
                        "head": {"ref": "old", "sha": "1" * 40},
                        "base": {"ref": "trunk", "sha": "e" * 40},
                    },
                    {
                        # Out-of-window merged PR -> must be excluded
                        "number": 5,
                        "state": "closed",
                        "title": "ancient",
                        "body": _CANARY_BODY,
                        "merged_at": out_of_window_merged_iso,
                        "updated_at": out_of_window_merged_iso,
                        "head": {"ref": "ancient", "sha": "2" * 40},
                        "base": {"ref": "trunk", "sha": "e" * 40},
                    },
                ],
                headers={"ETag": '"etag-pr-page-1"'},
            )
        if path == f"/repositories/{repo_id}/pulls/42":
            return httpx2.Response(
                200,
                json={
                    "number": 42,
                    "state": "closed",
                    "title": _CANARY_TITLE,
                    "body": pr_body_raw,
                    "merged": True,
                    "draft": False,
                    "merged_at": merged_at_iso,
                    "closed_at": merged_at_iso,
                    "created_at": "2026-10-10T08:00:00Z",
                    "updated_at": pr_updated_box[0],
                    "merge_commit_sha": "d" * 40,
                    "base": {"ref": "trunk", "sha": "e" * 40},
                    "head": {"ref": "feat/import", "sha": "f" * 40},
                    "user": {"id": 1001, "login": "alice"},
                    "html_url": "https://github.com/acme/career-core/pull/42",
                },
                headers={"ETag": '"etag-pr-42"'},
            )
        if path == f"/repositories/{repo_id}/pulls/42/reviews":
            return httpx2.Response(
                200,
                json=[
                    {
                        "id": 9001,
                        "state": "APPROVED",
                        "body": review_body_raw,
                        "commit_id": "f" * 40,
                        "submitted_at": "2026-10-15T09:00:00Z",
                        "user": {"id": 2002, "login": "bob"},
                        "html_url": "https://github.com/acme/career-core/pull/42#pullrequestreview-9001",
                    }
                ],
            )
        if path == f"/repositories/{repo_id}/pulls/42/comments":
            return httpx2.Response(
                200,
                json=[
                    {
                        "id": 9101,
                        "body": review_body_raw,
                        "commit_id": "f" * 40,
                        "created_at": "2026-10-15T08:45:00Z",
                        "updated_at": "2026-10-15T08:50:00Z",
                        "user": {"id": 2002, "login": "bob"},
                        "html_url": "https://github.com/acme/career-core/pull/42#discussion_r9101",
                    }
                ],
            )
        if path == f"/repositories/{repo_id}/issues/42/comments":
            return httpx2.Response(
                200,
                json=[
                    {
                        "id": 9201,
                        "body": comment_body_raw,
                        "created_at": "2026-10-15T09:10:00Z",
                        "updated_at": "2026-10-15T09:12:00Z",
                        "user": {"id": 1001, "login": "alice"},
                        "html_url": "https://github.com/acme/career-core/pull/42#issuecomment-9201",
                    }
                ],
            )
        if path == f"/repositories/{repo_id}/commits":
            assert request.url.params.get("sha") == _HEAD_SHA
            return httpx2.Response(
                200,
                json=[
                    {
                        "sha": _HEAD_SHA,
                        "commit": {
                            "message": commit_msg_raw,
                            "author": {"date": "2026-10-15T09:30:00Z"},
                            "committer": {"date": "2026-10-15T09:30:00Z"},
                        },
                        "author": {"id": 1001, "login": "alice"},
                        "committer": {"id": 1001, "login": "alice"},
                        "parents": [{"sha": "e" * 40}, {"sha": "f" * 40}],
                        "html_url": f"https://github.com/acme/career-core/commit/{_HEAD_SHA}",
                    }
                ],
            )
        if path == f"/repositories/{repo_id}/commits/{_HEAD_SHA}/pulls":
            return httpx2.Response(200, json=[{"number": 42}])
        if path == f"/repositories/{repo_id}/commits/{'e' * 40}/pulls":
            return httpx2.Response(200, json=[])
        if path == f"/repositories/{repo_id}/issues/77":
            return httpx2.Response(
                200,
                json={
                    "number": 77,
                    "state": "closed",
                    "title": _CANARY_TITLE,
                    "body": issue_body_raw,
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-15T09:30:00Z",
                    "closed_at": "2026-10-15T09:30:00Z",
                    "user": {"id": 1001, "login": "alice"},
                    "labels": [{"name": "spec"}],
                    "comments": 3,
                    "html_url": "https://github.com/acme/career-core/issues/77",
                },
            )
        return httpx2.Response(404, json={"message": "Not Found"})

    http_client = httpx2.Client(
        transport=httpx2.MockTransport(_mock_github),
        follow_redirects=False,
    )
    try:
        transport = SafeGitHubHttpTransport(
            wire_transport=Httpx2GitHubWireTransport(http_client),
            secret_store=store,
            clock=lambda: _NOW,
        )
        client = HttpGitHubImportClient(transport=transport, clock=lambda: _NOW)

        with caplog.at_level(logging.DEBUG):
            start_snap = client.fetch_repository_snapshot(
                owner="acme",
                repo="career-core",
                token_secret_ref=token_ref,
            )
            assert start_snap.default_branch == "trunk"
            assert start_snap.default_branch_head_sha == _HEAD_SHA
            assert start_snap.visibility is ResourceVisibility.PUBLIC

            plan = build_frozen_import_plan(
                source_resource_id="11111111-1111-4111-8111-111111111111",
                repo_snapshot=start_snap,
                cutoff_at=_NOW,
            )

            pr_page = client.fetch_merged_pull_requests_page(plan, page=1, token_secret_ref=token_ref)
            assert len(pr_page.items) == 1
            assert len(pr_page.cues) == 1
            pr_rev = pr_page.items[0]
            assert pr_rev.source_object_type is SourceObjectType.PR
            assert pr_rev.provider_object_id == "42"
            assert pr_rev.content_hash == expected_pr_canon.document.metadata.content_sha256
            assert pr_rev.hash_policy_version == CANONICALIZATION_VERSION
            assert pr_rev.referenced_issue_numbers == (77, 88)

            pr_detail = client.fetch_pull_request_detail(plan, pr_number=42, token_secret_ref=token_ref)
            assert pr_detail.content_hash == pr_rev.content_hash

            reviews_page = client.fetch_pull_request_reviews_page(plan, pr_number=42, token_secret_ref=token_ref)
            assert len(reviews_page.items) == 1
            assert reviews_page.items[0].source_object_type is SourceObjectType.REVIEW

            review_comments_page = client.fetch_pull_request_review_comments_page(
                plan, pr_number=42, token_secret_ref=token_ref
            )
            assert len(review_comments_page.items) == 1
            assert review_comments_page.items[0].provider_object_id == "9101"

            issue_comments_page = client.fetch_pull_request_issue_comments_page(
                plan, pr_number=42, token_secret_ref=token_ref
            )
            assert len(issue_comments_page.items) == 1
            assert issue_comments_page.items[0].source_object_type is SourceObjectType.COMMENT
            assert issue_comments_page.items[0].provider_object_id == "9201"

            commits_page = client.fetch_default_branch_commits_page(plan, page=1, token_secret_ref=token_ref)
            assert len(commits_page.items) == 1
            commit_rev = commits_page.items[0]
            assert commit_rev.source_object_type is SourceObjectType.COMMIT
            assert commit_rev.provider_object_id == _HEAD_SHA
            assert commit_rev.content_hash == expected_commit_canon.document.metadata.content_sha256
            assert commit_rev.parent_shas == ("e" * 40, "f" * 40)

            assoc_matched = client.fetch_commit_pull_request_associations(
                plan, commit_sha=_HEAD_SHA, token_secret_ref=token_ref
            )
            assert assoc_matched.pr_association_state is PrAssociationState.MATCHED_PR
            assert assoc_matched.associated_pr_numbers == (42,)

            assoc_none = client.fetch_commit_pull_request_associations(
                plan, commit_sha="e" * 40, token_secret_ref=token_ref
            )
            assert assoc_none.pr_association_state is PrAssociationState.NO_MATCH_OBSERVED
            assert assoc_none.associated_pr_numbers == ()

            issue_rev = client.fetch_referenced_issue(plan, issue_number=77, token_secret_ref=token_ref)
            assert issue_rev.source_object_type is SourceObjectType.ISSUE
            assert issue_rev.stream_kind is GitHubStreamKind.REFERENCED_ISSUES
            assert issue_rev.provider_object_id == "77"

            # Verify EVERY single ObservedGitHubRevision passes strict whitelist & has 0 canary bytes
            all_revisions = (
                pr_rev,
                pr_detail,
                reviews_page.items[0],
                review_comments_page.items[0],
                issue_comments_page.items[0],
                commit_rev,
                issue_rev,
            )
            canaries = (
                _CANARY_BODY,
                _CANARY_TITLE,
                _CANARY_COMMIT_MSG,
                _CANARY_REVIEW_BODY,
                _CANARY_COMMENT_BODY,
                _CANARY_ISSUE_BODY,
                _SECRET_TOKEN,
            )
            for rev in all_revisions:
                validate_normalized_metadata(rev.normalized_metadata, strict_whitelist=True)
                assert_no_forbidden_raw_keys(rev.normalized_metadata, field_name="normalized_metadata")
                serialized = json.dumps(rev.to_metadata_record_dict(), sort_keys=True)
                for canary in canaries:
                    assert canary not in serialized
                    assert canary not in repr(rev)

            # 2nd-pass completion gate check: clean match -> SUCCESS
            end_snap = client.fetch_repository_snapshot(
                owner="acme",
                repo="career-core",
                provider_resource_id=repo_id,
                token_secret_ref=token_ref,
            )
            second_cues = client.enumerate_merged_pr_cues(plan, token_secret_ref=token_ref)
            gate = CompletionGateEvaluator().verify_completion_snapshot(
                plan=plan,
                end_repo_snapshot=end_snap,
                first_pass_merged_prs=pr_page.cues,
                second_pass_merged_prs=second_cues,
            )
            assert gate.passed is True
            assert gate.import_ready is True

            # Mutate PR updated_at & HEAD SHA -> 2nd-pass detects PLAN_DRIFT
            pr_updated_box[0] = "2026-10-15T10:05:00Z"
            head_sha_box[0] = "9" * 40
            drifted_snap = client.fetch_repository_snapshot(
                owner="acme",
                repo="career-core",
                provider_resource_id=repo_id,
                token_secret_ref=token_ref,
            )
            drifted_cues = client.enumerate_merged_pr_cues(plan, token_secret_ref=token_ref)
            drifted_gate = CompletionGateEvaluator().verify_completion_snapshot(
                plan=plan,
                end_repo_snapshot=drifted_snap,
                first_pass_merged_prs=pr_page.cues,
                second_pass_merged_prs=drifted_cues,
            )
            assert drifted_gate.passed is False
            assert drifted_gate.failure_reason_code == "PLAN_DRIFT"
            assert drifted_gate.retry_allowed is False
            assert "DEFAULT_BRANCH_HEAD_CHANGED" in drifted_gate.drift_reasons
            assert "MERGED_PR_ENUMERATION_DRIFT" in drifted_gate.drift_reasons

        logs = "\n".join(rec.getMessage() for rec in caplog.records)
        for canary in (
            _CANARY_BODY,
            _CANARY_TITLE,
            _CANARY_COMMIT_MSG,
            _CANARY_REVIEW_BODY,
            _CANARY_COMMENT_BODY,
            _CANARY_ISSUE_BODY,
            _SECRET_TOKEN,
        ):
            assert canary not in logs
    finally:
        http_client.close()


def test_import_client_rejects_malicious_pagination_link_header() -> None:
    store, token_ref = _store_with_token()
    repo_id = "424242"

    def _handler(request: WireHttpRequest) -> WireHttpResponse:
        if request.url.endswith(f"/repositories/{repo_id}"):
            return _json_response(
                {
                    "id": int(repo_id),
                    "name": "career-core",
                    "full_name": "acme/career-core",
                    "owner": {"login": "acme"},
                    "html_url": "https://github.com/acme/career-core",
                    "private": False,
                    "visibility": "public",
                    "default_branch": "main",
                }
            )
        if request.url.endswith("/branches/main"):
            return _json_response({"name": "main", "commit": {"sha": _HEAD_SHA}})
        return _json_response(
            [],
            headers={"Link": '<http://127.0.0.1:9000/internal?page=2>; rel="next"'},
        )

    transport = SafeGitHubHttpTransport(
        wire_transport=InMemoryWireTransport(_handler),
        secret_store=store,
        clock=lambda: _NOW,
    )
    client = HttpGitHubImportClient(transport=transport, clock=lambda: _NOW)
    snap = client.fetch_repository_snapshot(
        owner="acme",
        repo="career-core",
        provider_resource_id=repo_id,
        token_secret_ref=token_ref,
    )
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=snap,
        cutoff_at=_NOW,
    )
    with pytest.raises(GitHubProviderError) as exc_info:
        client.fetch_merged_pull_requests_page(plan, page=1, token_secret_ref=token_ref)
    assert exc_info.value.code is GitHubErrorCode.ENDPOINT_BLOCKED


def test_retry_after_http_date_redirect_limit_and_hostile_nul_body_skip() -> None:
    from career_memory_api.github_provider.transport import (
        _read_bounded_stream,
        parse_retry_after_seconds,
    )
    import io

    # 1) RFC 7231 HTTP-date Retry-After parsing
    http_date = "Fri, 27 Nov 2026 14:02:00 GMT"
    assert parse_retry_after_seconds(http_date, now=_NOW) == 120

    # 2) Bounded stream reader enforces max_bytes without buffering past cap
    data, truncated = _read_bounded_stream(io.BytesIO(b"abcdefghij"), max_bytes=5)
    assert truncated is True
    assert data == b""

    # 3) Redirect hop limit exceeded -> REDIRECT_BLOCKED
    store, token_ref = _store_with_token()

    def _loop_handler(req: WireHttpRequest) -> WireHttpResponse:
        return WireHttpResponse(
            status_code=302,
            headers={"Location": "https://api.github.com/repositories/123/loop"},
            body=b"",
        )

    transport = SafeGitHubHttpTransport(
        wire_transport=InMemoryWireTransport(_loop_handler),
        secret_store=store,
        clock=lambda: _NOW,
    )
    with pytest.raises(GitHubProviderError) as exc_info:
        transport.get(
            "https://api.github.com/repositories/123/loop",
            token_secret_ref=token_ref,
            follow_safe_redirects=True,
        )
    assert exc_info.value.code is GitHubErrorCode.REDIRECT_BLOCKED
    assert exc_info.value.reason_code == "REDIRECT_LIMIT_EXCEEDED"

    # 4) Hostile NUL-byte PR body does not crash import; records deterministic SKIPPED
    repo_id = "555555"

    def _nul_handler(req: WireHttpRequest) -> WireHttpResponse:
        if req.url.endswith(f"/repositories/{repo_id}"):
            return _json_response(
                {
                    "id": int(repo_id),
                    "name": "app",
                    "full_name": "acme/app",
                    "owner": {"login": "acme"},
                    "html_url": "https://github.com/acme/app",
                    "private": False,
                    "visibility": "public",
                    "default_branch": "release/v2",
                }
            )
        if req.url.endswith("/branches/release%2Fv2"):
            return _json_response({"name": "release/v2", "commit": {"sha": _HEAD_SHA}})
        if req.url.endswith("/pulls/9"):
            return _json_response(
                {
                    "number": 9,
                    "state": "closed",
                    "body": f"bad\x00{_CANARY_BODY}",
                    "merged": True,
                    "merged_at": "2026-10-15T09:00:00Z",
                    "updated_at": "2026-10-15T09:00:00Z",
                    "head": {"ref": "feat", "sha": _HEAD_SHA},
                    "base": {"ref": "release/v2", "sha": "e" * 40},
                }
            )
        return WireHttpResponse(status_code=404, headers={}, body=b"{}")

    client = HttpGitHubImportClient(
        transport=SafeGitHubHttpTransport(
            wire_transport=InMemoryWireTransport(_nul_handler),
            secret_store=store,
            clock=lambda: _NOW,
        ),
        clock=lambda: _NOW,
    )
    snap = client.fetch_repository_snapshot(
        owner="acme", repo="app", provider_resource_id=repo_id, token_secret_ref=token_ref
    )
    assert snap.default_branch == "release/v2"
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=snap,
        cutoff_at=_NOW,
    )
    rev = client.fetch_pull_request_detail(plan, pr_number=9, token_secret_ref=token_ref)
    assert rev.canonicalization_status == "SKIPPED"
    assert rev.skip_reason_code == "BINARY_CONTENT"
    validate_normalized_metadata(rev.normalized_metadata, strict_whitelist=True)
    assert _CANARY_BODY not in json.dumps(rev.to_metadata_record_dict())

