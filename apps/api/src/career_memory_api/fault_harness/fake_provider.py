"""T-005A Deterministic offline GitHub provider & credential provider for B-004 fault harness.

Operates strictly in memory underneath the real production:
- `SafeGitHubHttpTransport(wire_transport=InMemoryWireTransport(...))`
- `HttpGitHubImportClient`
- `GitHubResolverContentFetcher`
- `PostgresGitHubConnectService` (`GitHubCredentialProviderPort`)

Zero external network I/O. Every scenario plants unique synthetic `CANARY_B004_*` markers into
wire payloads and fake OAuth tokens so T-005B can audit 0-leakage across PostgreSQL, logs,
API responses, and generated reports.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from career_memory_api.fault_harness.scenario_schema import CanarySetSpec
from career_memory_api.github_connect.models import (
    GitHubCredentialMode,
    GitHubExchangedCredential,
    GitHubInspectionOutcome,
    GitHubRepositoryInspection,
    GitHubRepositoryMetadata,
    GitHubRevokeResult,
)
from career_memory_api.github_provider.transport import WireHttpRequest, WireHttpResponse


def _hex40(prefix_hex: str, index: int) -> str:
    """Generate a deterministic 40-char lowercase hex commit SHA."""
    tail = f"{index:036x}"
    combined = (prefix_hex.lower() + tail)[:40]
    return combined.ljust(40, "0")


@dataclass
class DeterministicFakeGitHubProvider:
    """Deterministic in-memory GitHub REST v3 provider with full fault & drift controls."""

    scenario_id: str
    canaries: CanarySetSpec
    repo_id: str = "900400001"
    owner: str = "acme"
    repo: str = "career-memory-b004"
    default_branch: str = "trunk"
    visibility: str = "public"
    private: bool = False
    head_sha: str = "1" * 40
    auth_revoked: bool = False
    policy_check_http_status_override: int | None = None

    # Paginated REST resources
    pr_pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    pr_reviews: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    pr_review_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    pr_issue_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    commit_pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    commit_pr_associations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)

    # Fault controls (keyed by "<path>?page=<n>" or "<path>")
    status_fault_queues: dict[str, list[int]] = field(default_factory=dict)
    timeout_fault_counts: dict[str, int] = field(default_factory=dict)
    retry_after_by_key: dict[str, str] = field(default_factory=dict)
    truncated_content_length_keys: set[str] = field(default_factory=set)
    malformed_pr_page_numbers: set[int] = field(default_factory=set)
    cursor_cycle_pr_page_map: dict[int, int] = field(default_factory=dict)
    commit_assoc_status_override: dict[str, int] = field(default_factory=dict)
    pr_body_override_by_number: dict[int, str] = field(default_factory=dict)

    # Deterministic telemetry
    request_log: list[str] = field(default_factory=list)
    request_counts: Counter[str] = field(default_factory=Counter)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def _etag_for(self, route_key: str) -> str:
        safe = route_key.replace("/", "_").replace("?", "_").replace("=", "_")
        return f'"etag-b004-repo{self.repo_id}-{safe}-v1"'

    def _json_response(
        self,
        payload: Any,
        *,
        route_key: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        corrupt_content_length: bool = False,
    ) -> WireHttpResponse:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        declared_len = len(raw) + 512 if corrupt_content_length else len(raw)
        merged_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(declared_len),
            "ETag": self._etag_for(route_key),
        }
        if headers:
            merged_headers.update(headers)
        return WireHttpResponse(
            status_code=status_code,
            headers=merged_headers,
            body=raw,
            content_length_mismatch=corrupt_content_length,
        )

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
            "updated_at": "2026-10-20T12:00:00Z",
            "archived": False,
            "fork": False,
        }

    def _find_pr(self, pr_num: int) -> dict[str, Any] | None:
        for entries in self.pr_pages.values():
            for entry in entries:
                if entry.get("number") == pr_num:
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
        target = sha.lower()
        for entries in self.commit_pages.values():
            for entry in entries:
                if str(entry.get("sha", "")).lower() == target:
                    copied = dict(entry)
                    copied["repository"] = self._repo_payload()
                    return copied
        return None

    def handle_wire_request(self, request: WireHttpRequest) -> WireHttpResponse:
        """Deterministic InMemoryWireTransport handler for all M2 GitHub REST endpoints."""
        parts = urlsplit(request.url)
        path = parts.path
        query = parse_qs(parts.query)
        page = int(query.get("page", ["1"])[0])
        req_key = f"{path}?page={page}" if "page" in query else path

        self.request_log.append(req_key)
        self.request_counts[req_key] += 1

        if self.auth_revoked:
            return self._json_response(
                {"message": "Bad credentials"},
                route_key=req_key,
                status_code=401,
            )

        # Injected timeouts (decrement count)
        remaining_timeouts = self.timeout_fault_counts.get(req_key, 0)
        if remaining_timeouts > 0:
            self.timeout_fault_counts[req_key] = remaining_timeouts - 1
            raise TimeoutError(f"Injected deterministic timeout for {req_key}")

        # Injected HTTP status faults (pop from FIFO queue)
        queue = self.status_fault_queues.get(req_key)
        if queue:
            status = queue.pop(0)
            hdrs: dict[str, str] = {}
            if status == 429:
                hdrs["Retry-After"] = self.retry_after_by_key.get(req_key, "30")
                hdrs["X-RateLimit-Remaining"] = "0"
                hdrs["X-RateLimit-Reset"] = "1795780830"
            return self._json_response(
                {"message": f"Injected HTTP {status}"},
                route_key=req_key,
                status_code=status,
                headers=hdrs,
            )

        # Repository metadata
        if path in (f"/repositories/{self.repo_id}", f"/repos/{self.owner}/{self.repo}"):
            if self.policy_check_http_status_override is not None:
                return self._json_response(
                    {"message": "Injected policy API failure"},
                    route_key=req_key,
                    status_code=self.policy_check_http_status_override,
                )
            return self._json_response(self._repo_payload(), route_key=req_key)

        # Default branch HEAD
        if path == f"/repositories/{self.repo_id}/branches/{self.default_branch}":
            return self._json_response(
                {"name": self.default_branch, "commit": {"sha": self.head_sha}},
                route_key=req_key,
            )

        # Merged PRs paginated list
        if path == f"/repositories/{self.repo_id}/pulls":
            if page in self.malformed_pr_page_numbers:
                # HTTP 200 with missing required fields (number/head.sha corrupted)
                malformed_entries = [
                    {
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-09-10T10:00:00Z",
                        "updated_at": "2026-09-10T10:00:00Z",
                        "body": "corrupted entry without number or head.sha",
                    }
                ]
                return self._json_response(malformed_entries, route_key=req_key)

            entries = self.pr_pages.get(page, [])
            headers: dict[str, str] = {}
            if page in self.cursor_cycle_pr_page_map:
                target_page = self.cursor_cycle_pr_page_map[page]
                loop_url = request.url.replace(f"&page={page}", f"&page={target_page}")
                headers["Link"] = f'<{loop_url}>; rel="next"'
            elif (page + 1) in self.pr_pages:
                next_url = request.url.replace(f"&page={page}", f"&page={page + 1}")
                headers["Link"] = f'<{next_url}>; rel="next"'
            corrupt_cl = req_key in self.truncated_content_length_keys
            return self._json_response(
                entries,
                route_key=req_key,
                headers=headers,
                corrupt_content_length=corrupt_cl,
            )

        # Single PR detail (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/pulls/") and path.count("/") == 4:
            pr_num = int(path.rsplit("/", 1)[1])
            found = self._find_pr(pr_num)
            if found is None:
                return self._json_response(
                    {"message": "Not Found"}, route_key=req_key, status_code=404
                )
            return self._json_response(found, route_key=req_key)

        # PR reviews list or single PR review
        if path.startswith(f"/repositories/{self.repo_id}/pulls/") and "/reviews" in path:
            segs = path.strip("/").split("/")
            pr_num = int(segs[3])
            if len(segs) == 5:
                return self._json_response(self.pr_reviews.get(pr_num, []), route_key=req_key)
            if len(segs) == 6:
                rev_id = int(segs[5])
                for item in self.pr_reviews.get(pr_num, []):
                    if item["id"] == rev_id:
                        copied = dict(item)
                        copied["repository"] = self._repo_payload()
                        return self._json_response(copied, route_key=req_key)
                return self._json_response(
                    {"message": "Not Found"}, route_key=req_key, status_code=404
                )

        # Single inline review comment (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/pulls/comments/"):
            comment_id = int(path.rsplit("/", 1)[1])
            for items in self.pr_review_comments.values():
                for item in items:
                    if item["id"] == comment_id:
                        copied = dict(item)
                        copied["repository"] = self._repo_payload()
                        return self._json_response(copied, route_key=req_key)
            return self._json_response(
                {"message": "Not Found"}, route_key=req_key, status_code=404
            )

        # PR review comments list
        if path.startswith(f"/repositories/{self.repo_id}/pulls/") and path.endswith("/comments"):
            pr_num = int(path.strip("/").split("/")[3])
            return self._json_response(self.pr_review_comments.get(pr_num, []), route_key=req_key)

        # Single issue comment (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/issues/comments/"):
            comment_id = int(path.rsplit("/", 1)[1])
            for items in self.pr_issue_comments.values():
                for item in items:
                    if item["id"] == comment_id:
                        copied = dict(item)
                        copied["repository"] = self._repo_payload()
                        return self._json_response(copied, route_key=req_key)
            return self._json_response(
                {"message": "Not Found"}, route_key=req_key, status_code=404
            )

        # PR issue comments list
        if path.startswith(f"/repositories/{self.repo_id}/issues/") and path.endswith("/comments"):
            pr_num = int(path.strip("/").split("/")[3])
            return self._json_response(self.pr_issue_comments.get(pr_num, []), route_key=req_key)

        # Single referenced issue
        if path.startswith(f"/repositories/{self.repo_id}/issues/") and path.count("/") == 4:
            issue_num = int(path.rsplit("/", 1)[1])
            issue = self.issues.get(issue_num)
            if issue is None:
                return self._json_response(
                    {"message": "Not Found"}, route_key=req_key, status_code=404
                )
            copied = dict(issue)
            copied["repository"] = self._repo_payload()
            return self._json_response(copied, route_key=req_key)

        # Default branch commits list
        if path == f"/repositories/{self.repo_id}/commits":
            entries = self.commit_pages.get(page, [])
            headers = {}
            if (page + 1) in self.commit_pages:
                next_url = request.url.replace(f"&page={page}", f"&page={page + 1}")
                headers["Link"] = f'<{next_url}>; rel="next"'
            corrupt_cl = req_key in self.truncated_content_length_keys
            return self._json_response(
                entries,
                route_key=req_key,
                headers=headers,
                corrupt_content_length=corrupt_cl,
            )

        # Commit -> PR associations
        if path.startswith(f"/repositories/{self.repo_id}/commits/") and path.endswith("/pulls"):
            sha = path.strip("/").split("/")[3].lower()
            override = self.commit_assoc_status_override.get(sha)
            if override is not None:
                return self._json_response(
                    {"message": f"Injected commit assoc HTTP {override}"},
                    route_key=req_key,
                    status_code=override,
                )
            return self._json_response(self.commit_pr_associations.get(sha, []), route_key=req_key)

        # Single commit detail (used by resolver refetch)
        if path.startswith(f"/repositories/{self.repo_id}/commits/") and path.count("/") == 4:
            sha = path.rsplit("/", 1)[1].lower()
            found_commit = self._find_commit(sha)
            if found_commit is None:
                return self._json_response(
                    {"message": "Not Found"}, route_key=req_key, status_code=404
                )
            return self._json_response(found_commit, route_key=req_key)

        return self._json_response(
            {"message": f"Unhandled route {path}"}, route_key=req_key, status_code=404
        )


class DeterministicFakeCredentialProvider:
    """Offline `GitHubCredentialProviderPort` backed by `DeterministicFakeGitHubProvider`."""

    def __init__(self, provider: DeterministicFakeGitHubProvider) -> None:
        self._provider = provider

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
            "https://github.com/login/oauth/authorize?client_id=b004-offline-client"
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
            external_account_id=f"gh-b004-{self._provider.scenario_id.lower()}",
            login="b004-tester",
            access_token=self._provider.canaries.oauth_token,
            scopes=("read:user",),
        )

    def inspect_repository(
        self,
        *,
        access_token: str,
        owner: str,
        repo: str,
    ) -> GitHubRepositoryInspection:
        if self._provider.policy_check_http_status_override == 503:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.PROVIDER_UNAVAILABLE,
                http_status=503,
                reason="Injected policy API 503 outage.",
            )
        if self._provider.policy_check_http_status_override == 429:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.RATE_LIMITED,
                http_status=429,
                retry_after_seconds=30,
                reason="Injected policy API 429 rate limit.",
            )
        if self._provider.auth_revoked:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.TOKEN_REVOKED,
                http_status=401,
                reason="Bad credentials",
            )
        meta = GitHubRepositoryMetadata(
            provider_resource_id=self._provider.repo_id,
            full_name=f"{owner}/{repo}",
            canonical_url=f"https://github.com/{owner}/{repo}",
            private=self._provider.private,
            visibility=self._provider.visibility,
            default_branch=self._provider.default_branch,
        )
        if not meta.is_strictly_public:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
                http_status=200,
                observed_visibility=self._provider.visibility.upper(),
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


def _make_commit_payload(
    *,
    owner: str,
    repo: str,
    sha: str,
    message: str,
    date_iso: str,
    parent_sha: str,
) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {"date": date_iso},
            "committer": {"date": date_iso},
        },
        "author": {"id": 1001, "login": "alice"},
        "committer": {"id": 1001, "login": "alice"},
        "parents": [{"sha": parent_sha}],
        "html_url": f"https://github.com/{owner}/{repo}/commit/{sha}",
    }


def build_10page_canonical_provider(
    *,
    scenario_id: str,
    canary_id: str | None = None,
    default_branch: str = "trunk",
    repo_id: str = "900400010",
    repo_name: str = "career-memory-10p",
) -> DeterministicFakeGitHubProvider:
    """Build the canonical 10-page PR + 3-page commit dataset shared by S01, S02, S03, S04.

    Pass ``canary_id="S01"`` (defaulting to ``scenario_id``) when testing strict byte-for-byte
    snapshot equivalence across S01..S04; each scenario still plants its own unique OAuth token
    and deterministic body canaries when desired. To keep S01..S04 content hashes 100% identical,
    S01..S04 share the canonical ``CANARY_B004_RAW_*_S01_...`` body canaries while still scanning
    both the canonical body canaries AND the scenario's own OAuth token canary!
    """
    owner = "acme"
    body_canaries = CanarySetSpec.for_scenario(canary_id or scenario_id)
    oauth_canaries = CanarySetSpec.for_scenario(scenario_id)
    canaries = CanarySetSpec(
        raw_pr_body=body_canaries.raw_pr_body,
        raw_commit_msg=body_canaries.raw_commit_msg,
        raw_review_body=body_canaries.raw_review_body,
        oauth_token=oauth_canaries.oauth_token,
    )

    pr_pages: dict[int, list[dict[str, Any]]] = {}
    pr_reviews: dict[int, list[dict[str, Any]]] = {}
    pr_review_comments: dict[int, list[dict[str, Any]]] = {}
    pr_issue_comments: dict[int, list[dict[str, Any]]] = {}

    # 10 pages of merged PRs: page 1 -> PR #110, ..., page 10 -> PR #101 (desc order by updated_at)
    pr_merge_shas: dict[int, str] = {}
    pr_head_shas: dict[int, str] = {}
    for idx in range(10):
        page_num = idx + 1
        pr_num = 110 - idx  # 110 down to 101
        day = 20 - idx  # 2026-10-20 down to 2026-10-11
        head_sha = _hex40("a100", pr_num)
        merge_sha = _hex40("b100", pr_num)
        pr_head_shas[pr_num] = head_sha
        pr_merge_shas[pr_num] = merge_sha

        issue_ref = " References #501 and #999." if pr_num == 101 else ""
        canary_line = f"\n{canaries.raw_pr_body}\n" if pr_num == 101 else "\n"
        pr_pages[page_num] = [
            {
                "number": pr_num,
                "state": "closed",
                "merged": True,
                "draft": False,
                "title": f"Implement milestone feature {pr_num}",
                "body": f"## Summary\nDeterministic PR #{pr_num} on {default_branch}.{issue_ref}{canary_line}",
                "base": {"ref": default_branch, "sha": _hex40("0000", pr_num)},
                "head": {"ref": f"feat/pr-{pr_num}", "sha": head_sha},
                "merge_commit_sha": merge_sha,
                "user": {"id": 1001, "login": "alice"},
                "labels": [{"name": "m2"}],
                "created_at": f"2026-10-{day:02d}T09:00:00Z",
                "updated_at": f"2026-10-{day:02d}T12:00:00Z",
                "closed_at": f"2026-10-{day:02d}T12:00:00Z",
                "merged_at": f"2026-10-{day:02d}T12:00:00Z",
                "html_url": f"https://github.com/{owner}/{repo_name}/pull/{pr_num}",
            }
        ]
        if pr_num == 101:
            pr_reviews[pr_num] = [
                {
                    "id": 5101,
                    "state": "APPROVED",
                    "body": f"LGTM for PR #101.\n{canaries.raw_review_body}\n",
                    "commit_id": head_sha,
                    "user": {"id": 1002, "login": "bob"},
                    "submitted_at": "2026-10-11T11:30:00Z",
                    "html_url": f"https://github.com/{owner}/{repo_name}/pull/101#pullrequestreview-5101",
                }
            ]
            pr_review_comments[pr_num] = [
                {
                    "id": 6101,
                    "body": "Keep half-open span bounds exact.\n",
                    "commit_id": head_sha,
                    "user": {"id": 1002, "login": "bob"},
                    "created_at": "2026-10-11T11:00:00Z",
                    "updated_at": "2026-10-11T11:00:00Z",
                    "html_url": f"https://github.com/{owner}/{repo_name}/pull/101#discussion_r6101",
                }
            ]
            pr_issue_comments[pr_num] = [
                {
                    "id": 7101,
                    "body": "Verified deterministic PG18 migration replay.\n",
                    "user": {"id": 1001, "login": "alice"},
                    "created_at": "2026-10-11T11:45:00Z",
                    "updated_at": "2026-10-11T11:45:00Z",
                    "html_url": f"https://github.com/{owner}/{repo_name}/pull/101#issuecomment-7101",
                }
            ]
        else:
            pr_reviews[pr_num] = []
            pr_review_comments[pr_num] = []
            pr_issue_comments[pr_num] = []

    sha_direct = _hex40("e100", 1)
    sha_out_of_window = _hex40("f100", 1)
    sha_squash_pr105 = _hex40("c100", 105)
    head_sha = pr_merge_shas[110]

    # 3 pages of default-branch commits (4 commits per page = 12 commits total)
    all_commits = [
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[110],
            message="Merge pull request #110\n",
            date_iso="2026-10-20T12:00:00Z",
            parent_sha=pr_merge_shas[109],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[109],
            message="Merge pull request #109\n",
            date_iso="2026-10-19T12:00:00Z",
            parent_sha=pr_merge_shas[108],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[108],
            message="Merge pull request #108\n",
            date_iso="2026-10-18T12:00:00Z",
            parent_sha=pr_merge_shas[107],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[107],
            message="Merge pull request #107\n",
            date_iso="2026-10-17T12:00:00Z",
            parent_sha=pr_merge_shas[106],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[106],
            message="Merge pull request #106\n",
            date_iso="2026-10-16T12:00:00Z",
            parent_sha=sha_squash_pr105,
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=sha_squash_pr105,
            message="feat: squash commit from PR #105\n",
            date_iso="2026-10-15T12:00:00Z",
            parent_sha=pr_merge_shas[104],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[104],
            message="Merge pull request #104\n",
            date_iso="2026-10-14T12:00:00Z",
            parent_sha=pr_merge_shas[103],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[103],
            message="Merge pull request #103\n",
            date_iso="2026-10-13T12:00:00Z",
            parent_sha=sha_direct,
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=sha_direct,
            message=f"chore: verified direct commit on {default_branch}\n\n{canaries.raw_commit_msg}\n",
            date_iso="2026-10-12T15:00:00Z",
            parent_sha=pr_merge_shas[102],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[102],
            message="Merge pull request #102\n",
            date_iso="2026-10-12T12:00:00Z",
            parent_sha=pr_merge_shas[101],
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=pr_merge_shas[101],
            message="Merge pull request #101\n",
            date_iso="2026-10-11T12:00:00Z",
            parent_sha=sha_out_of_window,
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=sha_out_of_window,
            message="feat: commit from out-of-window PR #4\n",
            date_iso="2026-07-05T09:00:00Z",
            parent_sha="0" * 40,
        ),
    ]

    return DeterministicFakeGitHubProvider(
        scenario_id=scenario_id,
        canaries=canaries,
        repo_id=repo_id,
        owner=owner,
        repo=repo_name,
        default_branch=default_branch,
        head_sha=head_sha,
        pr_pages=pr_pages,
        pr_reviews=pr_reviews,
        pr_review_comments=pr_review_comments,
        pr_issue_comments=pr_issue_comments,
        commit_pages={
            1: all_commits[0:4],
            2: all_commits[4:8],
            3: all_commits[8:12],
        },
        commit_pr_associations={
            sha_direct: [],
            sha_out_of_window: [{"number": 4}],
            sha_squash_pr105: [{"number": 105}],
        },
        issues={
            501: {
                "number": 501,
                "state": "closed",
                "body": "## Context\nTracking issue #501 for B-004 10-page import.\n",
                "user": {"id": 1001, "login": "alice"},
                "labels": [{"name": "tracking"}],
                "comments": 1,
                "created_at": "2026-10-01T08:00:00Z",
                "updated_at": "2026-10-11T12:05:00Z",
                "closed_at": "2026-10-11T12:05:00Z",
                "html_url": f"https://github.com/{owner}/{repo_name}/issues/501",
            }
            # Issue #999 intentionally absent (404) -> OPTIONAL_CONTEXT_UNAVAILABLE
        },
    )


def build_compact_multipage_provider(
    *,
    scenario_id: str,
    default_branch: str = "main",
    repo_id: str = "900400020",
    repo_name: str = "career-memory-compact",
    include_direct_commit: bool = True,
    include_out_of_window_commit: bool = True,
    include_squash_commit: bool = True,
) -> DeterministicFakeGitHubProvider:
    """Build a compact 3-page PR + 2-page commit dataset for S05..S16."""
    owner = "acme"
    canaries = CanarySetSpec.for_scenario(scenario_id)

    sha_pr203_head = _hex40("a200", 203)
    sha_pr203_merge = _hex40("b200", 203)
    sha_pr202_head = _hex40("a200", 202)
    sha_pr202_merge = _hex40("b200", 202)
    sha_pr201_head = _hex40("a200", 201)
    sha_pr201_merge = _hex40("b200", 201)
    sha_direct = _hex40("e200", 1)
    sha_out_of_window = _hex40("f200", 1)
    sha_squash_pr202 = _hex40("c200", 202)

    def _pr(num: int, day: int, head_sha: str, merge_sha: str, body: str) -> dict[str, Any]:
        return {
            "number": num,
            "state": "closed",
            "merged": True,
            "draft": False,
            "title": f"Feature PR #{num}",
            "body": body,
            "base": {"ref": default_branch, "sha": _hex40("0000", num)},
            "head": {"ref": f"feat/pr-{num}", "sha": head_sha},
            "merge_commit_sha": merge_sha,
            "user": {"id": 1001, "login": "alice"},
            "labels": [{"name": "core"}],
            "created_at": f"2026-09-{day:02d}T09:00:00Z",
            "updated_at": f"2026-09-{day:02d}T12:00:00Z",
            "closed_at": f"2026-09-{day:02d}T12:00:00Z",
            "merged_at": f"2026-09-{day:02d}T12:00:00Z",
            "html_url": f"https://github.com/{owner}/{repo_name}/pull/{num}",
        }

    pr203 = _pr(
        203, 18, sha_pr203_head, sha_pr203_merge,
        f"## Summary\nPR #203 on {default_branch}.\n",
    )
    pr202 = _pr(
        202, 14, sha_pr202_head, sha_pr202_merge,
        f"## Summary\nPR #202 on {default_branch}.\n",
    )
    pr201 = _pr(
        201, 10, sha_pr201_head, sha_pr201_merge,
        f"## Summary\nPR #201 references #601.\n{canaries.raw_pr_body}\n",
    )

    commits_p1 = [
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=sha_pr203_merge,
            message="Merge pull request #203\n",
            date_iso="2026-09-18T12:00:00Z",
            parent_sha=sha_pr202_merge,
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=sha_pr202_merge,
            message="Merge pull request #202\n",
            date_iso="2026-09-14T12:00:00Z",
            parent_sha=sha_pr201_merge,
        ),
    ]
    commits_p2 = [
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=sha_pr201_merge,
            message="Merge pull request #201\n",
            date_iso="2026-09-10T12:00:00Z",
            parent_sha=sha_pr201_head,
        ),
        _make_commit_payload(
            owner=owner,
            repo=repo_name,
            sha=sha_pr201_head,
            message="feat: PR #201 head commit\n",
            date_iso="2026-09-10T10:00:00Z",
            parent_sha="0" * 40,
        ),
    ]
    assoc: dict[str, list[dict[str, Any]]] = {}

    if include_squash_commit:
        commits_p1.append(
            _make_commit_payload(
                owner=owner,
                repo=repo_name,
                sha=sha_squash_pr202,
                message="feat: squash commit from PR #202\n",
                date_iso="2026-09-14T11:30:00Z",
                parent_sha=sha_pr201_merge,
            )
        )
        assoc[sha_squash_pr202] = [{"number": 202}]

    if include_direct_commit:
        commits_p2.append(
            _make_commit_payload(
                owner=owner,
                repo=repo_name,
                sha=sha_direct,
                message=f"chore: direct hotfix on {default_branch}\n\n{canaries.raw_commit_msg}\n",
                date_iso="2026-09-12T09:00:00Z",
                parent_sha=sha_pr201_merge,
            )
        )
        assoc[sha_direct] = []

    if include_out_of_window_commit:
        commits_p2.append(
            _make_commit_payload(
                owner=owner,
                repo=repo_name,
                sha=sha_out_of_window,
                message="feat: out-of-window PR #7 commit\n",
                date_iso="2026-06-01T09:00:00Z",
                parent_sha="0" * 40,
            )
        )
        assoc[sha_out_of_window] = [{"number": 7}]

    return DeterministicFakeGitHubProvider(
        scenario_id=scenario_id,
        canaries=canaries,
        repo_id=repo_id,
        owner=owner,
        repo=repo_name,
        default_branch=default_branch,
        head_sha=sha_pr203_merge,
        pr_pages={1: [pr203], 2: [pr202], 3: [pr201]},
        pr_reviews={
            201: [
                {
                    "id": 5201,
                    "state": "APPROVED",
                    "body": f"Approved PR #201.\n{canaries.raw_review_body}\n",
                    "commit_id": sha_pr201_head,
                    "user": {"id": 1002, "login": "bob"},
                    "submitted_at": "2026-09-10T11:00:00Z",
                    "html_url": f"https://github.com/{owner}/{repo_name}/pull/201#pullrequestreview-5201",
                }
            ],
            202: [],
            203: [],
        },
        pr_review_comments={201: [], 202: [], 203: []},
        pr_issue_comments={201: [], 202: [], 203: []},
        commit_pages={1: commits_p1, 2: commits_p2},
        commit_pr_associations=assoc,
        issues={
            601: {
                "number": 601,
                "state": "closed",
                "body": "## Context\nTracking issue #601.\n",
                "user": {"id": 1001, "login": "alice"},
                "labels": [{"name": "epic"}],
                "comments": 0,
                "created_at": "2026-09-01T09:00:00Z",
                "updated_at": "2026-09-10T12:05:00Z",
                "closed_at": "2026-09-10T12:05:00Z",
                "html_url": f"https://github.com/{owner}/{repo_name}/issues/601",
            }
        },
    )


__all__ = [
    "DeterministicFakeCredentialProvider",
    "DeterministicFakeGitHubProvider",
    "build_10page_canonical_provider",
    "build_compact_multipage_provider",
]
