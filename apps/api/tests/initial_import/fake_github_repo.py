"""Deterministic offline GitHub REST wire fixture for M2-T206 integration tests.

Serves real `HttpGitHubImportClient`, `GitHubResolverContentFetcher`, and
`GitHubOAuthAppCredentialProvider` over `InMemoryWireTransport` with zero network I/O.
Supports injecting transient 503/429 faults, visibility flips, 401 revocations,
PLAN_DRIFT (HEAD move / PR mutation), cursor cycles, UNKNOWN commit-PR lookup failures,
and post-SUCCESS raw body drift.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from career_memory_api.github_connect.models import (
    GitHubCredentialMode,
    GitHubExchangedCredential,
    GitHubInspectionOutcome,
    GitHubRepositoryInspection,
    GitHubRepositoryMetadata,
    GitHubRevokeResult,
)
from career_memory_api.github_provider.transport import WireHttpRequest, WireHttpResponse

CANARY_PR_BODY = "CANARY_M2_T206_ZERO_RAW_PERSISTENCE_PR_BODY_9F42A1"
CANARY_COMMIT_MSG = "CANARY_M2_T206_ZERO_RAW_PERSISTENCE_COMMIT_7B31E8"
CANARY_COMMENT_BODY = "CANARY_M2_T206_ZERO_RAW_PERSISTENCE_COMMENT_5C18D2"


@dataclass
class OfflineGitHubRepoFixture:
    """Mutable offline GitHub API state with request counting and fault injection."""

    repo_id: str = "900100200"
    owner: str = "acme"
    repo: str = "career-memory-core"
    default_branch: str = "trunk"
    visibility: str = "public"
    private: bool = False
    head_sha: str = "1" * 40
    auth_revoked: bool = False

    # Pages of closed PRs targeting default_branch
    pr_pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    pr_reviews: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    pr_review_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    pr_issue_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    commit_pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    commit_pr_associations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)

    # Fault injection controls
    transient_faults: dict[str, list[int]] = field(default_factory=dict)
    commit_assoc_status_override: dict[str, int] = field(default_factory=dict)
    force_cursor_cycle_on_pr_page: int | None = None
    pr_body_override_by_number: dict[int, str] = field(default_factory=dict)

    # Telemetry
    request_log: list[str] = field(default_factory=list)
    request_counts: Counter[str] = field(default_factory=Counter)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @classmethod
    def build_standard_multipage(cls) -> OfflineGitHubRepoFixture:
        """Build a realistic 2-page PR, 2-page commit, reviews, comments, and issue fixture."""
        sha_pr10_head = "a" * 40
        sha_pr10_merge = "b" * 40
        sha_pr11_head = "c" * 40
        sha_pr12_head = "d" * 40
        sha_direct_commit = "e" * 40
        sha_out_of_window_pr_commit = "f" * 40

        pr10 = {
            "number": 10,
            "state": "closed",
            "merged": True,
            "draft": False,
            "title": "Add streaming parser",
            "body": f"## Summary\nImplemented parser for #101 and #999.\n{CANARY_PR_BODY}\n\n## Impact\nCut latency 40%.\n",
            "base": {"ref": "trunk", "sha": "0" * 40},
            "head": {"ref": "feat/parser", "sha": sha_pr10_head},
            "merge_commit_sha": sha_pr10_merge,
            "user": {"id": 1001, "login": "alice"},
            "labels": [{"name": "backend"}],
            "created_at": "2026-08-01T10:00:00Z",
            "updated_at": "2026-08-02T12:00:00Z",
            "closed_at": "2026-08-02T12:00:00Z",
            "merged_at": "2026-08-02T12:00:00Z",
            "html_url": "https://github.com/acme/career-memory-core/pull/10",
        }
        pr11 = {
            "number": 11,
            "state": "closed",
            "merged": True,
            "draft": False,
            "title": "Harden checkpoint fencing",
            "body": "## Overview\nAdded row-level checkpoint fencing.\n",
            "base": {"ref": "trunk", "sha": sha_pr10_merge},
            "head": {"ref": "feat/fence", "sha": sha_pr11_head},
            "merge_commit_sha": sha_pr11_head,
            "user": {"id": 1001, "login": "alice"},
            "labels": [{"name": "reliability"}],
            "created_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-02T12:00:00Z",
            "closed_at": "2026-09-02T12:00:00Z",
            "merged_at": "2026-09-02T12:00:00Z",
            "html_url": "https://github.com/acme/career-memory-core/pull/11",
        }
        pr12 = {
            "number": 12,
            "state": "closed",
            "merged": True,
            "draft": False,
            "title": "Add Plan C resolver bridge",
            "body": "## Design\nEphemeral refetch without raw persistence.\n",
            "base": {"ref": "trunk", "sha": sha_pr11_head},
            "head": {"ref": "feat/resolver", "sha": sha_pr12_head},
            "merge_commit_sha": sha_pr12_head,
            "user": {"id": 1002, "login": "bob"},
            "labels": [{"name": "security"}],
            "created_at": "2026-10-01T10:00:00Z",
            "updated_at": "2026-10-02T12:00:00Z",
            "closed_at": "2026-10-02T12:00:00Z",
            "merged_at": "2026-10-02T12:00:00Z",
            "html_url": "https://github.com/acme/career-memory-core/pull/12",
        }

        def _commit_obj(sha: str, msg: str, dt: str, parent: str) -> dict[str, Any]:
            return {
                "sha": sha,
                "commit": {
                    "message": msg,
                    "author": {"date": dt},
                    "committer": {"date": dt},
                },
                "author": {"id": 1001, "login": "alice"},
                "committer": {"id": 1001, "login": "alice"},
                "parents": [{"sha": parent}],
                "html_url": f"https://github.com/acme/career-memory-core/commit/{sha}",
            }

        return cls(
            head_sha=sha_pr12_head,
            pr_pages={
                1: [pr12, pr11],
                2: [pr10],
            },
            pr_reviews={
                10: [
                    {
                        "id": 5001,
                        "state": "APPROVED",
                        "body": f"LGTM on parser!\n{CANARY_COMMENT_BODY}\n",
                        "commit_id": sha_pr10_head,
                        "user": {"id": 1002, "login": "bob"},
                        "submitted_at": "2026-08-02T11:00:00Z",
                        "html_url": "https://github.com/acme/career-memory-core/pull/10#pullrequestreview-5001",
                    }
                ],
                11: [],
                12: [],
            },
            pr_review_comments={
                10: [
                    {
                        "id": 6001,
                        "body": "Nit: keep locator offsets half-open.\n",
                        "commit_id": sha_pr10_head,
                        "user": {"id": 1002, "login": "bob"},
                        "created_at": "2026-08-02T10:30:00Z",
                        "updated_at": "2026-08-02T10:30:00Z",
                        "html_url": "https://github.com/acme/career-memory-core/pull/10#discussion_r6001",
                    }
                ],
                11: [],
                12: [],
            },
            pr_issue_comments={
                10: [
                    {
                        "id": 7001,
                        "body": "Benchmarks verified on PG18.\n",
                        "user": {"id": 1001, "login": "alice"},
                        "created_at": "2026-08-02T11:30:00Z",
                        "updated_at": "2026-08-02T11:30:00Z",
                        "html_url": "https://github.com/acme/career-memory-core/pull/10#issuecomment-7001",
                    }
                ],
                11: [],
                12: [],
            },
            commit_pages={
                1: [
                    _commit_obj(sha_pr12_head, "feat: add Plan C resolver bridge\n", "2026-10-02T12:00:00Z", sha_pr11_head),
                    _commit_obj(sha_pr11_head, "fix: harden checkpoint fencing\n", "2026-09-02T12:00:00Z", sha_direct_commit),
                ],
                2: [
                    _commit_obj(
                        sha_direct_commit,
                        f"chore: direct hotfix on trunk\n\n{CANARY_COMMIT_MSG}\n",
                        "2026-08-15T09:00:00Z",
                        sha_pr10_merge,
                    ),
                    _commit_obj(sha_pr10_merge, "Merge pull request #10\n", "2026-08-02T12:00:00Z", sha_out_of_window_pr_commit),
                    _commit_obj(sha_out_of_window_pr_commit, "feat: legacy out-of-window PR commit\n", "2026-07-10T09:00:00Z", "0" * 40),
                ],
            },
            commit_pr_associations={
                sha_direct_commit: [],  # Verified NO_MATCH_OBSERVED -> 1:1 DIRECT_COMMIT
                sha_out_of_window_pr_commit: [{"number": 4}],  # Out-of-window PR -> EXCLUDED
            },
            issues={
                101: {
                    "number": 101,
                    "state": "closed",
                    "body": "## Context\nTracking issue for streaming parser.\n",
                    "user": {"id": 1001, "login": "alice"},
                    "labels": [{"name": "epic"}],
                    "comments": 2,
                    "created_at": "2026-07-20T09:00:00Z",
                    "updated_at": "2026-08-02T12:05:00Z",
                    "closed_at": "2026-08-02T12:05:00Z",
                    "html_url": "https://github.com/acme/career-memory-core/issues/101",
                }
                # Note: issue #999 intentionally absent (404) -> OPTIONAL_CONTEXT_UNAVAILABLE
            },
        )

    def _json_response(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> WireHttpResponse:
        raw = json.dumps(payload).encode("utf-8")
        merged_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(raw)),
            "ETag": '"etag-static-v1"',
        }
        if headers:
            merged_headers.update(headers)
        return WireHttpResponse(status_code=status_code, headers=merged_headers, body=raw)

    def _maybe_consume_transient_fault(self, key: str) -> WireHttpResponse | None:
        queue = self.transient_faults.get(key)
        if queue:
            status = queue.pop(0)
            hdrs = {"Retry-After": "1"} if status == 429 else {}
            return self._json_response({"message": f"injected transient {status}"}, status_code=status, headers=hdrs)
        return None

    def _repo_payload(self) -> dict[str, Any]:
        return {
            "id": int(self.repo_id),
            "name": self.repo,
            "full_name": self.full_name,
            "owner": {"login": self.owner},
            "html_url": f"https://github.com/{self.full_name}",
            "private": self.private,
            "visibility": self.visibility,
            "default_branch": self.default_branch,
            "updated_at": "2026-10-02T12:00:00Z",
            "archived": False,
            "fork": False,
        }

    def _find_pr(self, pr_num: int) -> dict[str, Any] | None:
        for entries in self.pr_pages.values():
            for entry in entries:
                if entry["number"] == pr_num:
                    copied = dict(entry)
                    if pr_num in self.pr_body_override_by_number:
                        copied["body"] = self.pr_body_override_by_number[pr_num]
                    copied["base"] = {
                        **dict(copied.get("base", {})),
                        "repo": self._repo_payload(),
                    }
                    return copied
        return None

    def _find_commit(self, sha: str) -> dict[str, Any] | None:
        for entries in self.commit_pages.values():
            for entry in entries:
                if entry["sha"].lower() == sha.lower():
                    copied = dict(entry)
                    copied["repository"] = self._repo_payload()
                    return copied
        return None

    def handle_wire_request(self, request: WireHttpRequest) -> WireHttpResponse:
        """`InMemoryWireTransport` handler implementing all GitHub REST endpoints used in M2."""
        parts = urlsplit(request.url)
        path = parts.path
        query = parse_qs(parts.query)
        page = int(query.get("page", ["1"])[0])
        req_key = f"{path}?page={page}" if "page" in query else path
        self.request_log.append(req_key)
        self.request_counts[req_key] += 1

        if self.auth_revoked:
            return self._json_response({"message": "Bad credentials"}, status_code=401)

        injected = self._maybe_consume_transient_fault(req_key)
        if injected is not None:
            return injected

        # Repository metadata
        if path in (f"/repositories/{self.repo_id}", f"/repos/{self.owner}/{self.repo}"):
            return self._json_response(self._repo_payload())

        # Default branch HEAD
        if path == f"/repositories/{self.repo_id}/branches/{self.default_branch}":
            return self._json_response(
                {
                    "name": self.default_branch,
                    "commit": {"sha": self.head_sha},
                }
            )

        # Merged PRs paginated list
        if path == f"/repositories/{self.repo_id}/pulls":
            entries = self.pr_pages.get(page, [])
            headers: dict[str, str] = {}
            if self.force_cursor_cycle_on_pr_page == page:
                # Self-loop next link pointing to same page!
                headers["Link"] = f'<{request.url}>; rel="next"'
            elif (page + 1) in self.pr_pages:
                next_url = request.url.replace(f"&page={page}", f"&page={page + 1}")
                headers["Link"] = f'<{next_url}>; rel="next"'
            return self._json_response(entries, headers=headers)

        # Single PR detail (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/pulls/") and path.count("/") == 4:
            pr_num = int(path.rsplit("/", 1)[1])
            found = self._find_pr(pr_num)
            if found is None:
                return self._json_response({"message": "Not Found"}, status_code=404)
            return self._json_response(found)

        # PR reviews list or single PR review
        if path.startswith(f"/repositories/{self.repo_id}/pulls/") and "/reviews" in path:
            segs = path.strip("/").split("/")
            # repositories/<id>/pulls/<pr>/reviews or .../reviews/<review_id>
            pr_num = int(segs[3])
            if len(segs) == 5:
                return self._json_response(self.pr_reviews.get(pr_num, []))
            if len(segs) == 6:
                rev_id = int(segs[5])
                for item in self.pr_reviews.get(pr_num, []):
                    if item["id"] == rev_id:
                        copied = dict(item)
                        copied["repository"] = self._repo_payload()
                        return self._json_response(copied)
                return self._json_response({"message": "Not Found"}, status_code=404)

        # Single inline review comment (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/pulls/comments/"):
            comment_id = int(path.rsplit("/", 1)[1])
            for items in self.pr_review_comments.values():
                for item in items:
                    if item["id"] == comment_id:
                        copied = dict(item)
                        copied["repository"] = self._repo_payload()
                        return self._json_response(copied)
            return self._json_response({"message": "Not Found"}, status_code=404)

        # PR review comments list
        if path.startswith(f"/repositories/{self.repo_id}/pulls/") and path.endswith("/comments"):
            pr_num = int(path.strip("/").split("/")[3])
            return self._json_response(self.pr_review_comments.get(pr_num, []))

        # Single issue comment (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/issues/comments/"):
            comment_id = int(path.rsplit("/", 1)[1])
            for items in self.pr_issue_comments.values():
                for item in items:
                    if item["id"] == comment_id:
                        copied = dict(item)
                        copied["repository"] = self._repo_payload()
                        return self._json_response(copied)
            return self._json_response({"message": "Not Found"}, status_code=404)

        # PR issue comments list
        if path.startswith(f"/repositories/{self.repo_id}/issues/") and path.endswith("/comments"):
            pr_num = int(path.strip("/").split("/")[3])
            return self._json_response(self.pr_issue_comments.get(pr_num, []))

        # Single referenced issue
        if path.startswith(f"/repositories/{self.repo_id}/issues/") and path.count("/") == 4:
            issue_num = int(path.rsplit("/", 1)[1])
            issue = self.issues.get(issue_num)
            if issue is None:
                return self._json_response({"message": "Not Found"}, status_code=404)
            copied = dict(issue)
            copied["repository"] = self._repo_payload()
            return self._json_response(copied)

        # Default branch commits list
        if path == f"/repositories/{self.repo_id}/commits":
            entries = self.commit_pages.get(page, [])
            headers = {}
            if (page + 1) in self.commit_pages:
                next_url = request.url.replace(f"&page={page}", f"&page={page + 1}")
                headers["Link"] = f'<{next_url}>; rel="next"'
            return self._json_response(entries, headers=headers)

        # Commit -> PR associations
        if path.startswith(f"/repositories/{self.repo_id}/commits/") and path.endswith("/pulls"):
            sha = path.strip("/").split("/")[3].lower()
            override = self.commit_assoc_status_override.get(sha)
            if override is not None:
                return self._json_response({"message": "override error"}, status_code=override)
            return self._json_response(self.commit_pr_associations.get(sha, []))

        # Single commit detail (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/commits/") and path.count("/") == 4:
            sha = path.rsplit("/", 1)[1].lower()
            found_commit = self._find_commit(sha)
            if found_commit is None:
                return self._json_response({"message": "Not Found"}, status_code=404)
            return self._json_response(found_commit)

        return self._json_response({"message": f"Unhandled route {path}"}, status_code=404)


class FixtureGitHubCredentialProvider:
    """Offline `GitHubCredentialProviderPort` backed by `OfflineGitHubRepoFixture`."""

    def __init__(self, fixture: OfflineGitHubRepoFixture) -> None:
        self._fixture = fixture

    @property
    def credential_mode(self) -> GitHubCredentialMode:
        return GitHubCredentialMode.OAUTH_APP

    def build_authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        return (
            f"https://github.com/login/oauth/authorize?client_id=test-client"
            f"&scope=read%3Auser&state={state}&redirect_uri={redirect_uri}"
            f"&code_challenge={code_challenge}&code_challenge_method={code_challenge_method}"
        )

    def exchange_authorization_code(
        self,
        *,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> GitHubExchangedCredential:
        return GitHubExchangedCredential(
            external_account_id="gh-user-1001",
            login="alice",
            access_token="gho_offline_test_token_m2_t206_valid_secret",
            scopes=("read:user",),
        )

    def inspect_repository(
        self,
        *,
        access_token: str,
        owner: str,
        repo: str,
    ) -> GitHubRepositoryInspection:
        if self._fixture.auth_revoked:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.TOKEN_REVOKED,
                http_status=401,
                reason="Bad credentials",
            )
        meta = GitHubRepositoryMetadata(
            provider_resource_id=self._fixture.repo_id,
            full_name=f"{owner}/{repo}",
            canonical_url=f"https://github.com/{owner}/{repo}",
            private=self._fixture.private,
            visibility=self._fixture.visibility,
            default_branch=self._fixture.default_branch,
        )
        if not meta.is_strictly_public:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
                http_status=200,
                observed_visibility=self._fixture.visibility.upper(),
                repository=meta,
                observed_scopes=("read:user",),
                reason="Repository is non-public.",
            )
        return GitHubRepositoryInspection(
            outcome=GitHubInspectionOutcome.PUBLIC_CONFIRMED,
            http_status=200,
            observed_visibility="PUBLIC",
            repository=meta,
            observed_scopes=("read:user",),
        )

    def revoke_grant(self, *, access_token: str) -> GitHubRevokeResult:
        return GitHubRevokeResult(revoked=True, already_revoked=False, http_status=204)
