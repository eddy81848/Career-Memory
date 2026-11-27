"""Read-only GitHub REST API v3 import client extracting strictly non-raw Plan C metadata."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    canonicalize_source_bytes,
    sha256_prefixed_bytes,
)
from career_memory_api.github_provider.models import (
    DEFAULT_GITHUB_API_BASE_URL,
    GITHUB_CONNECTOR_VERSION,
    GITHUB_METADATA_SCHEMA_VERSION,
    CommitPullRequestAssociation,
    GitHubErrorCode,
    GitHubProviderError,
    GitHubRepositorySnapshot,
    GitHubStreamKind,
    MergedPullRequestCue,
    MergedPullRequestPage,
    ObservedGitHubRevision,
    ObservedRevisionPage,
    classify_github_visibility,
    extract_canonical_source_bytes,
    format_utc_iso,
    normalize_commit_sha,
    parse_github_iso_timestamp,
    validate_safe_path_segment,
)
from career_memory_api.github_provider.plan import FrozenImportPlan
from career_memory_api.github_provider.transport import (
    SafeGitHubHttpTransport,
    parse_github_next_link,
)
from career_memory_api.schema_contracts.enums import PrAssociationState, SourceObjectType

_ISSUE_REF_RE = re.compile(r"(?<![A-Za-z0-9_/-])#([1-9][0-9]{0,7})\b")


class GitHubImportClientPort(Protocol):
    """Read-only GitHub import connector port used by M2 initial backfill workflows."""

    def fetch_repository_snapshot(
        self,
        *,
        owner: str,
        repo: str,
        token_secret_ref: str | None = None,
        provider_resource_id: str | None = None,
    ) -> GitHubRepositorySnapshot:
        ...

    def fetch_merged_pull_requests_page(
        self,
        plan: FrozenImportPlan,
        *,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> MergedPullRequestPage:
        ...

    def enumerate_merged_pr_cues(
        self,
        plan: FrozenImportPlan,
        *,
        token_secret_ref: str | None = None,
        max_pages: int | None = None,
    ) -> tuple[MergedPullRequestCue, ...]:
        ...

    def fetch_pull_request_detail(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        token_secret_ref: str | None = None,
    ) -> ObservedGitHubRevision:
        ...

    def fetch_pull_request_reviews_page(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        ...

    def fetch_pull_request_review_comments_page(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        ...

    def fetch_pull_request_issue_comments_page(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        ...

    def fetch_default_branch_commits_page(
        self,
        plan: FrozenImportPlan,
        *,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        ...

    def fetch_commit_pull_request_associations(
        self,
        plan: FrozenImportPlan,
        *,
        commit_sha: str,
        token_secret_ref: str | None = None,
    ) -> CommitPullRequestAssociation:
        ...

    def fetch_referenced_issue(
        self,
        plan: FrozenImportPlan,
        *,
        issue_number: int,
        token_secret_ref: str | None = None,
    ) -> ObservedGitHubRevision:
        ...


def _extract_referenced_issue_numbers(text: str, *, exclude_number: int | None = None) -> tuple[int, ...]:
    """Extract strictly integer-only referenced issue numbers in memory before discarding body."""
    if not text:
        return ()
    found: set[int] = set()
    for match in _ISSUE_REF_RE.findall(text):
        num = int(match)
        if exclude_number is not None and num == exclude_number:
            continue
        found.add(num)
    return tuple(sorted(found))


def _hash_and_discard_raw_bytes(
    object_type: SourceObjectType,
    payload: Mapping[str, Any],
) -> tuple[str, int, int | None, str, str | None]:
    """Compute canonical SHA-256 + length stats in memory and immediately discard raw bytes."""
    raw_bytes, mime_type = extract_canonical_source_bytes(object_type, payload, is_diff=False)
    byte_length = len(raw_bytes)
    canon = canonicalize_source_bytes(raw_bytes, mime_type=mime_type, is_diff=False)
    if canon.status == "CANONICALIZED" and canon.document is not None:
        return (
            canon.document.metadata.content_sha256,
            canon.document.metadata.utf8_byte_length,
            canon.document.metadata.char_length,
            "CANONICALIZED",
            None,
        )
    skip_code = canon.skip_record.reason_code.value if canon.skip_record is not None else "SKIPPED"
    return (
        sha256_prefixed_bytes(raw_bytes),
        byte_length,
        None,
        "SKIPPED",
        skip_code,
    )


def _extract_user_fields(user_obj: Any) -> tuple[str | None, str | None]:
    if not isinstance(user_obj, Mapping):
        return None, None
    ext_id = str(user_obj["id"]) if user_obj.get("id") is not None else None
    login = str(user_obj["login"]).strip() if isinstance(user_obj.get("login"), str) else None
    return ext_id, login or None


def _extract_label_names(labels_raw: Any) -> list[str]:
    if not isinstance(labels_raw, list):
        return []
    names: list[str] = []
    for item in labels_raw:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            val = item["name"].strip()
            if val:
                names.append(val)
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _clean_metadata(raw_map: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw_map.items() if v is not None}


def _build_pr_revision(
    *,
    provider_resource_id: str,
    payload: Mapping[str, Any],
    observed_at: datetime,
    etag: str | None = None,
) -> ObservedGitHubRevision:
    pr_number = _optional_int(payload.get("number"))
    if pr_number is None or pr_number < 1:
        raise GitHubProviderError(
            GitHubErrorCode.INVALID_RESPONSE,
            "GitHub PR payload missing valid integer 'number'.",
        )
    content_hash, byte_len, char_len, canon_status, skip_code = _hash_and_discard_raw_bytes(
        SourceObjectType.PR, payload
    )
    raw_body_text = payload.get("body") if isinstance(payload.get("body"), str) else ""
    referenced_issues = _extract_referenced_issue_numbers(raw_body_text, exclude_number=pr_number)
    del raw_body_text

    base_obj = payload.get("base") if isinstance(payload.get("base"), Mapping) else {}
    head_obj = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
    author_id, author_login = _extract_user_fields(payload.get("user"))
    updated_at = parse_github_iso_timestamp(payload.get("updated_at"))
    merged_at = parse_github_iso_timestamp(payload.get("merged_at"))

    head_sha = str(head_obj["sha"]).lower() if isinstance(head_obj.get("sha"), str) else None
    provider_revision_id = head_sha or etag

    meta = _clean_metadata(
        {
            "provider_number": pr_number,
            "state": str(payload.get("state")) if payload.get("state") is not None else None,
            "merged": bool(payload.get("merged")) if "merged" in payload else (merged_at is not None),
            "draft": bool(payload.get("draft")) if "draft" in payload else None,
            "base_ref": str(base_obj["ref"]) if isinstance(base_obj.get("ref"), str) else None,
            "head_ref": str(head_obj["ref"]) if isinstance(head_obj.get("ref"), str) else None,
            "base_sha": str(base_obj["sha"]).lower() if isinstance(base_obj.get("sha"), str) else None,
            "head_sha": head_sha,
            "merge_commit_sha": (
                str(payload["merge_commit_sha"]).lower()
                if isinstance(payload.get("merge_commit_sha"), str)
                else None
            ),
            "author_external_id": author_id,
            "author_login": author_login,
            "labels": _extract_label_names(payload.get("labels")) or None,
            "changed_files_count": _optional_int(payload.get("changed_files")),
            "additions": _optional_int(payload.get("additions")),
            "deletions": _optional_int(payload.get("deletions")),
            "commit_count": _optional_int(payload.get("commits")),
            "comment_count": _optional_int(payload.get("comments")),
            "review_count": _optional_int(payload.get("review_comments")),
            "created_at": payload.get("created_at") if isinstance(payload.get("created_at"), str) else None,
            "updated_at": payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None,
            "closed_at": payload.get("closed_at") if isinstance(payload.get("closed_at"), str) else None,
            "merged_at": payload.get("merged_at") if isinstance(payload.get("merged_at"), str) else None,
            "html_url": payload.get("html_url") if isinstance(payload.get("html_url"), str) else None,
            "api_etag": etag,
            "byte_length": byte_len,
            "char_length": char_len,
            "canonicalization_version": CANONICALIZATION_VERSION,
        }
    )
    return ObservedGitHubRevision(
        source_object_type=SourceObjectType.PR,
        stream_kind=GitHubStreamKind.MERGED_PULL_REQUESTS,
        provider_resource_id=provider_resource_id,
        provider_object_id=str(pr_number),
        provider_revision_id=provider_revision_id,
        content_hash=content_hash,
        hash_policy_version=CANONICALIZATION_VERSION,
        observed_at=observed_at,
        provider_updated_at=updated_at,
        normalized_metadata=meta,
        connector_version=GITHUB_CONNECTOR_VERSION,
        schema_version=GITHUB_METADATA_SCHEMA_VERSION,
        referenced_issue_numbers=referenced_issues,
        parent_pr_number=pr_number,
        canonicalization_status=canon_status,
        skip_reason_code=skip_code,
    )


def _build_pr_cue(payload: Mapping[str, Any]) -> MergedPullRequestCue | None:
    merged_at = parse_github_iso_timestamp(payload.get("merged_at"))
    if merged_at is None:
        return None
    pr_number = _optional_int(payload.get("number"))
    head_obj = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
    head_sha = head_obj.get("sha")
    if pr_number is None or not isinstance(head_sha, str):
        raise GitHubProviderError(
            GitHubErrorCode.INVALID_RESPONSE,
            "GitHub merged PR payload missing valid number or head.sha.",
        )
    updated_at = parse_github_iso_timestamp(payload.get("updated_at")) or merged_at
    merge_sha = (
        payload.get("merge_commit_sha")
        if isinstance(payload.get("merge_commit_sha"), str)
        else None
    )
    return MergedPullRequestCue(
        pr_number=pr_number,
        head_sha=head_sha,
        merged_at=merged_at,
        updated_at=updated_at,
        merge_commit_sha=merge_sha,
        state=str(payload.get("state") or "closed"),
    )


class HttpGitHubImportClient:
    """Read-only GitHub REST API v3 connector adhering strictly to Plan C non-persistence."""

    def __init__(
        self,
        *,
        transport: SafeGitHubHttpTransport | None = None,
        api_base_url: str = DEFAULT_GITHUB_API_BASE_URL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport or SafeGitHubHttpTransport()
        self._api_base_url = api_base_url.rstrip("/")
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return f"HttpGitHubImportClient(api_base_url={self._api_base_url!r})"

    def _expect_dict(self, data: Any, *, context: str) -> Mapping[str, Any]:
        if not isinstance(data, Mapping):
            raise GitHubProviderError(
                GitHubErrorCode.INVALID_RESPONSE,
                f"Expected JSON object for {context}.",
            )
        return data

    def _expect_list(self, data: Any, *, context: str) -> list[Any]:
        if not isinstance(data, list):
            raise GitHubProviderError(
                GitHubErrorCode.INVALID_RESPONSE,
                f"Expected JSON array for {context}.",
            )
        return data

    def fetch_repository_snapshot(
        self,
        *,
        owner: str,
        repo: str,
        token_secret_ref: str | None = None,
        provider_resource_id: str | None = None,
    ) -> GitHubRepositorySnapshot:
        """Fetch live repository visibility, dynamic default branch, and branch HEAD SHA."""
        if provider_resource_id is not None:
            safe_repo_id = validate_safe_path_segment(provider_resource_id, "provider_resource_id")
            repo_url = f"{self._api_base_url}/repositories/{safe_repo_id}"
        else:
            safe_owner = validate_safe_path_segment(owner, "owner")
            safe_repo = validate_safe_path_segment(repo, "repo")
            repo_url = f"{self._api_base_url}/repos/{safe_owner}/{safe_repo}"

        raw_repo, _ = self._transport.get_json(repo_url, token_secret_ref=token_secret_ref)
        repo_data = self._expect_dict(raw_repo, context="repository metadata")

        raw_id = repo_data.get("id")
        if raw_id is None or isinstance(raw_id, bool):
            raise GitHubProviderError(
                GitHubErrorCode.INVALID_RESPONSE,
                "GitHub repository metadata missing numeric id.",
            )
        repo_id_str = validate_safe_path_segment(str(raw_id), "repository.id")

        default_branch = repo_data.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise GitHubProviderError(
                GitHubErrorCode.INVALID_RESPONSE,
                "GitHub repository metadata missing non-empty default_branch.",
            )
        default_branch = default_branch.strip()

        encoded_branch = quote(default_branch, safe="")
        branch_url = f"{self._api_base_url}/repositories/{repo_id_str}/branches/{encoded_branch}"
        raw_branch, _ = self._transport.get_json(branch_url, token_secret_ref=token_secret_ref)
        branch_data = self._expect_dict(raw_branch, context="default branch metadata")
        commit_obj = branch_data.get("commit")
        head_sha_raw = commit_obj.get("sha") if isinstance(commit_obj, Mapping) else None
        if not isinstance(head_sha_raw, str):
            raise GitHubProviderError(
                GitHubErrorCode.INVALID_RESPONSE,
                "GitHub default branch payload missing commit.sha.",
            )
        head_sha = normalize_commit_sha(head_sha_raw, "default_branch_head_sha")

        owner_obj = repo_data.get("owner") if isinstance(repo_data.get("owner"), Mapping) else {}
        resolved_owner = (
            str(owner_obj["login"]).strip()
            if isinstance(owner_obj.get("login"), str) and owner_obj["login"].strip()
            else owner.strip()
        )
        resolved_name = (
            str(repo_data["name"]).strip()
            if isinstance(repo_data.get("name"), str) and repo_data["name"].strip()
            else repo.strip()
        )
        full_name = (
            str(repo_data["full_name"]).strip()
            if isinstance(repo_data.get("full_name"), str) and repo_data["full_name"].strip()
            else f"{resolved_owner}/{resolved_name}"
        )
        canonical_url = (
            str(repo_data["html_url"]).strip()
            if isinstance(repo_data.get("html_url"), str) and repo_data["html_url"].startswith("https://")
            else f"https://github.com/{full_name}"
        )
        visibility = classify_github_visibility(
            private=repo_data.get("private"),
            visibility=repo_data.get("visibility"),
        )
        return GitHubRepositorySnapshot(
            provider_resource_id=repo_id_str,
            owner=resolved_owner,
            name=resolved_name,
            full_name=full_name,
            canonical_url=canonical_url,
            visibility=visibility,
            default_branch=default_branch,
            default_branch_head_sha=head_sha,
            observed_at=self._clock(),
            provider_updated_at=parse_github_iso_timestamp(repo_data.get("updated_at")),
            is_archived=bool(repo_data.get("archived", False)),
            is_fork=bool(repo_data.get("fork", False)),
        )

    def fetch_merged_pull_requests_page(
        self,
        plan: FrozenImportPlan,
        *,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> MergedPullRequestPage:
        """Fetch one page of closed PRs targeting ``plan.default_branch`` and keep in-window merged PRs."""
        if page < 1:
            raise ValueError("page must be >= 1.")
        eff_per_page = per_page if per_page is not None else plan.limits.per_page
        query = urlencode(
            {
                "state": "closed",
                "base": plan.default_branch,
                "sort": "updated",
                "direction": "desc",
                "per_page": eff_per_page,
                "page": page,
            }
        )
        url = f"{self._api_base_url}/repositories/{plan.provider_resource_id}/pulls?{query}"
        raw_list, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        entries = self._expect_list(raw_list, context="merged pull requests page")
        etag = resp.get_header("ETag")
        next_url = parse_github_next_link(
            resp.get_header("Link"), security_policy=self._transport.security_policy
        )
        now = self._clock()

        revisions: list[ObservedGitHubRevision] = []
        cues: list[MergedPullRequestCue] = []
        reached_floor = False

        for entry in entries:
            pr_map = self._expect_dict(entry, context="pull request entry")
            updated_at = parse_github_iso_timestamp(pr_map.get("updated_at"))
            if updated_at is not None and updated_at < plan.backfill_from:
                reached_floor = True
                continue
            cue = _build_pr_cue(pr_map)
            if cue is None:
                continue
            if not plan.is_timestamp_in_window(cue.merged_at):
                continue
            cues.append(cue)
            revisions.append(
                _build_pr_revision(
                    provider_resource_id=plan.provider_resource_id,
                    payload=pr_map,
                    observed_at=now,
                    etag=etag,
                )
            )

        has_next = (next_url is not None) and (not reached_floor)
        return MergedPullRequestPage(
            items=tuple(revisions),
            cues=tuple(cues),
            page=page,
            has_next=has_next,
            next_page=(page + 1) if has_next else None,
            next_url=next_url if has_next else None,
            reached_window_floor=reached_floor,
            requests_used=1,
            bytes_read=len(resp.body),
            etag=etag,
        )

    def enumerate_merged_pr_cues(
        self,
        plan: FrozenImportPlan,
        *,
        token_secret_ref: str | None = None,
        max_pages: int | None = None,
    ) -> tuple[MergedPullRequestCue, ...]:
        """Perform lightweight 2nd-pass re-enumeration of in-window merged PR cues for completion gate."""
        page_limit = max_pages if max_pages is not None else plan.limits.max_requests
        collected: dict[int, MergedPullRequestCue] = {}
        page = 1
        while page <= page_limit:
            result = self.fetch_merged_pull_requests_page(
                plan,
                page=page,
                token_secret_ref=token_secret_ref,
            )
            for cue in result.cues:
                collected[cue.pr_number] = cue
            if not result.has_next:
                break
            page += 1
        return tuple(collected[num] for num in sorted(collected.keys()))

    def fetch_pull_request_detail(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        token_secret_ref: str | None = None,
    ) -> ObservedGitHubRevision:
        """Fetch single PR detail and return strictly non-raw ObservedGitHubRevision."""
        if pr_number < 1:
            raise ValueError("pr_number must be >= 1.")
        url = f"{self._api_base_url}/repositories/{plan.provider_resource_id}/pulls/{pr_number}"
        raw_pr, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        pr_data = self._expect_dict(raw_pr, context="pull request detail")
        return _build_pr_revision(
            provider_resource_id=plan.provider_resource_id,
            payload=pr_data,
            observed_at=self._clock(),
            etag=resp.get_header("ETag"),
        )

    def fetch_pull_request_reviews_page(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        """Fetch one page of PR reviews and convert to non-raw ObservedGitHubRevision records."""
        if pr_number < 1 or page < 1:
            raise ValueError("pr_number and page must be >= 1.")
        eff_per_page = per_page if per_page is not None else plan.limits.per_page
        query = urlencode({"per_page": eff_per_page, "page": page})
        url = (
            f"{self._api_base_url}/repositories/{plan.provider_resource_id}"
            f"/pulls/{pr_number}/reviews?{query}"
        )
        raw_list, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        entries = self._expect_list(raw_list, context="pull request reviews")
        etag = resp.get_header("ETag")
        next_url = parse_github_next_link(
            resp.get_header("Link"), security_policy=self._transport.security_policy
        )
        now = self._clock()
        revisions: list[ObservedGitHubRevision] = []

        for entry in entries:
            item = self._expect_dict(entry, context="pull request review")
            review_id = item.get("id")
            if review_id is None or isinstance(review_id, bool):
                raise GitHubProviderError(
                    GitHubErrorCode.INVALID_RESPONSE,
                    "GitHub PR review payload missing id.",
                )
            content_hash, byte_len, char_len, canon_status, skip_code = _hash_and_discard_raw_bytes(
                SourceObjectType.REVIEW, item
            )
            author_id, author_login = _extract_user_fields(item.get("user"))
            submitted_at = parse_github_iso_timestamp(item.get("submitted_at"))
            commit_id = str(item["commit_id"]).lower() if isinstance(item.get("commit_id"), str) else None
            meta = _clean_metadata(
                {
                    "provider_number": pr_number,
                    "state": str(item.get("state")) if item.get("state") is not None else None,
                    "head_sha": commit_id,
                    "author_external_id": author_id,
                    "author_login": author_login,
                    "created_at": item.get("submitted_at") if isinstance(item.get("submitted_at"), str) else None,
                    "updated_at": item.get("submitted_at") if isinstance(item.get("submitted_at"), str) else None,
                    "html_url": item.get("html_url") if isinstance(item.get("html_url"), str) else None,
                    "api_etag": etag,
                    "byte_length": byte_len,
                    "char_length": char_len,
                    "canonicalization_version": CANONICALIZATION_VERSION,
                }
            )
            revisions.append(
                ObservedGitHubRevision(
                    source_object_type=SourceObjectType.REVIEW,
                    stream_kind=GitHubStreamKind.PULL_REQUEST_REVIEWS,
                    provider_resource_id=plan.provider_resource_id,
                    provider_object_id=str(review_id),
                    provider_revision_id=commit_id or etag,
                    content_hash=content_hash,
                    hash_policy_version=CANONICALIZATION_VERSION,
                    observed_at=now,
                    provider_updated_at=submitted_at,
                    normalized_metadata=meta,
                    parent_pr_number=pr_number,
                    canonicalization_status=canon_status,
                    skip_reason_code=skip_code,
                )
            )

        has_next = next_url is not None
        return ObservedRevisionPage(
            stream_kind=GitHubStreamKind.PULL_REQUEST_REVIEWS,
            items=tuple(revisions),
            page=page,
            has_next=has_next,
            next_page=(page + 1) if has_next else None,
            next_url=next_url,
            requests_used=1,
            bytes_read=len(resp.body),
            etag=etag,
        )

    def fetch_pull_request_review_comments_page(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        """Fetch one page of PR inline review comments (mapped to REVIEW for resolver parity)."""
        if pr_number < 1 or page < 1:
            raise ValueError("pr_number and page must be >= 1.")
        eff_per_page = per_page if per_page is not None else plan.limits.per_page
        query = urlencode({"per_page": eff_per_page, "page": page})
        url = (
            f"{self._api_base_url}/repositories/{plan.provider_resource_id}"
            f"/pulls/{pr_number}/comments?{query}"
        )
        raw_list, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        entries = self._expect_list(raw_list, context="pull request review comments")
        etag = resp.get_header("ETag")
        next_url = parse_github_next_link(
            resp.get_header("Link"), security_policy=self._transport.security_policy
        )
        now = self._clock()
        revisions: list[ObservedGitHubRevision] = []

        for entry in entries:
            item = self._expect_dict(entry, context="pull request review comment")
            comment_id = item.get("id")
            if comment_id is None or isinstance(comment_id, bool):
                raise GitHubProviderError(
                    GitHubErrorCode.INVALID_RESPONSE,
                    "GitHub PR review comment payload missing id.",
                )
            content_hash, byte_len, char_len, canon_status, skip_code = _hash_and_discard_raw_bytes(
                SourceObjectType.REVIEW, item
            )
            author_id, author_login = _extract_user_fields(item.get("user"))
            updated_at = parse_github_iso_timestamp(item.get("updated_at") or item.get("created_at"))
            commit_id = str(item["commit_id"]).lower() if isinstance(item.get("commit_id"), str) else None
            meta = _clean_metadata(
                {
                    "provider_number": pr_number,
                    "head_sha": commit_id,
                    "author_external_id": author_id,
                    "author_login": author_login,
                    "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else None,
                    "updated_at": item.get("updated_at") if isinstance(item.get("updated_at"), str) else None,
                    "html_url": item.get("html_url") if isinstance(item.get("html_url"), str) else None,
                    "api_etag": etag,
                    "byte_length": byte_len,
                    "char_length": char_len,
                    "canonicalization_version": CANONICALIZATION_VERSION,
                }
            )
            revisions.append(
                ObservedGitHubRevision(
                    source_object_type=SourceObjectType.REVIEW,
                    stream_kind=GitHubStreamKind.PULL_REQUEST_COMMENTS,
                    provider_resource_id=plan.provider_resource_id,
                    provider_object_id=str(comment_id),
                    provider_revision_id=commit_id or etag,
                    content_hash=content_hash,
                    hash_policy_version=CANONICALIZATION_VERSION,
                    observed_at=now,
                    provider_updated_at=updated_at,
                    normalized_metadata=meta,
                    parent_pr_number=pr_number,
                    canonicalization_status=canon_status,
                    skip_reason_code=skip_code,
                )
            )

        has_next = next_url is not None
        return ObservedRevisionPage(
            stream_kind=GitHubStreamKind.PULL_REQUEST_COMMENTS,
            items=tuple(revisions),
            page=page,
            has_next=has_next,
            next_page=(page + 1) if has_next else None,
            next_url=next_url,
            requests_used=1,
            bytes_read=len(resp.body),
            etag=etag,
        )

    def fetch_pull_request_issue_comments_page(
        self,
        plan: FrozenImportPlan,
        *,
        pr_number: int,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        """Fetch one page of PR/issue conversation comments (mapped to COMMENT for resolver parity)."""
        if pr_number < 1 or page < 1:
            raise ValueError("pr_number and page must be >= 1.")
        eff_per_page = per_page if per_page is not None else plan.limits.per_page
        query = urlencode({"per_page": eff_per_page, "page": page})
        url = (
            f"{self._api_base_url}/repositories/{plan.provider_resource_id}"
            f"/issues/{pr_number}/comments?{query}"
        )
        raw_list, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        entries = self._expect_list(raw_list, context="issue comments")
        etag = resp.get_header("ETag")
        next_url = parse_github_next_link(
            resp.get_header("Link"), security_policy=self._transport.security_policy
        )
        now = self._clock()
        revisions: list[ObservedGitHubRevision] = []

        for entry in entries:
            item = self._expect_dict(entry, context="issue comment")
            comment_id = item.get("id")
            if comment_id is None or isinstance(comment_id, bool):
                raise GitHubProviderError(
                    GitHubErrorCode.INVALID_RESPONSE,
                    "GitHub issue comment payload missing id.",
                )
            content_hash, byte_len, char_len, canon_status, skip_code = _hash_and_discard_raw_bytes(
                SourceObjectType.COMMENT, item
            )
            author_id, author_login = _extract_user_fields(item.get("user"))
            updated_at = parse_github_iso_timestamp(item.get("updated_at") or item.get("created_at"))
            meta = _clean_metadata(
                {
                    "provider_number": pr_number,
                    "author_external_id": author_id,
                    "author_login": author_login,
                    "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else None,
                    "updated_at": item.get("updated_at") if isinstance(item.get("updated_at"), str) else None,
                    "html_url": item.get("html_url") if isinstance(item.get("html_url"), str) else None,
                    "api_etag": etag,
                    "byte_length": byte_len,
                    "char_length": char_len,
                    "canonicalization_version": CANONICALIZATION_VERSION,
                }
            )
            revisions.append(
                ObservedGitHubRevision(
                    source_object_type=SourceObjectType.COMMENT,
                    stream_kind=GitHubStreamKind.PULL_REQUEST_COMMENTS,
                    provider_resource_id=plan.provider_resource_id,
                    provider_object_id=str(comment_id),
                    provider_revision_id=etag,
                    content_hash=content_hash,
                    hash_policy_version=CANONICALIZATION_VERSION,
                    observed_at=now,
                    provider_updated_at=updated_at,
                    normalized_metadata=meta,
                    parent_pr_number=pr_number,
                    canonicalization_status=canon_status,
                    skip_reason_code=skip_code,
                )
            )

        has_next = next_url is not None
        return ObservedRevisionPage(
            stream_kind=GitHubStreamKind.PULL_REQUEST_COMMENTS,
            items=tuple(revisions),
            page=page,
            has_next=has_next,
            next_page=(page + 1) if has_next else None,
            next_url=next_url,
            requests_used=1,
            bytes_read=len(resp.body),
            etag=etag,
        )

    def fetch_default_branch_commits_page(
        self,
        plan: FrozenImportPlan,
        *,
        page: int = 1,
        per_page: int | None = None,
        token_secret_ref: str | None = None,
    ) -> ObservedRevisionPage:
        """Fetch one page of default-branch commits anchored strictly to ``plan.start_head_sha``."""
        if page < 1:
            raise ValueError("page must be >= 1.")
        eff_per_page = per_page if per_page is not None else plan.limits.per_page
        query = urlencode(
            {
                "sha": plan.start_head_sha,
                "since": format_utc_iso(plan.backfill_from),
                "until": format_utc_iso(plan.cutoff_at),
                "per_page": eff_per_page,
                "page": page,
            }
        )
        url = f"{self._api_base_url}/repositories/{plan.provider_resource_id}/commits?{query}"
        raw_list, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        entries = self._expect_list(raw_list, context="default branch commits")
        etag = resp.get_header("ETag")
        next_url = parse_github_next_link(
            resp.get_header("Link"), security_policy=self._transport.security_policy
        )
        now = self._clock()
        revisions: list[ObservedGitHubRevision] = []

        for entry in entries:
            item = self._expect_dict(entry, context="commit entry")
            raw_sha = item.get("sha")
            if not isinstance(raw_sha, str):
                raise GitHubProviderError(
                    GitHubErrorCode.INVALID_RESPONSE,
                    "GitHub commit payload missing sha.",
                )
            sha = normalize_commit_sha(raw_sha, "commit.sha")
            content_hash, byte_len, char_len, canon_status, skip_code = _hash_and_discard_raw_bytes(
                SourceObjectType.COMMIT, item
            )
            author_id, author_login = _extract_user_fields(item.get("author"))
            committer_id, _ = _extract_user_fields(item.get("committer"))

            commit_obj = item.get("commit") if isinstance(item.get("commit"), Mapping) else {}
            commit_author = (
                commit_obj.get("author") if isinstance(commit_obj.get("author"), Mapping) else {}
            )
            commit_committer = (
                commit_obj.get("committer")
                if isinstance(commit_obj.get("committer"), Mapping)
                else {}
            )
            authored_date = commit_author.get("date") if isinstance(commit_author.get("date"), str) else None
            committed_date = (
                commit_committer.get("date") if isinstance(commit_committer.get("date"), str) else None
            )
            updated_at = parse_github_iso_timestamp(committed_date or authored_date)

            parents_raw = item.get("parents") if isinstance(item.get("parents"), list) else []
            parent_shas = tuple(
                normalize_commit_sha(p["sha"], "parent.sha")
                for p in parents_raw
                if isinstance(p, Mapping) and isinstance(p.get("sha"), str)
            )
            meta = _clean_metadata(
                {
                    "head_sha": sha,
                    "author_external_id": author_id,
                    "author_login": author_login,
                    "committer_external_id": committer_id,
                    "created_at": authored_date,
                    "updated_at": committed_date or authored_date,
                    "html_url": item.get("html_url") if isinstance(item.get("html_url"), str) else None,
                    "api_etag": etag,
                    "byte_length": byte_len,
                    "char_length": char_len,
                    "canonicalization_version": CANONICALIZATION_VERSION,
                }
            )
            revisions.append(
                ObservedGitHubRevision(
                    source_object_type=SourceObjectType.COMMIT,
                    stream_kind=GitHubStreamKind.DEFAULT_BRANCH_COMMITS,
                    provider_resource_id=plan.provider_resource_id,
                    provider_object_id=sha,
                    provider_revision_id=sha,
                    content_hash=content_hash,
                    hash_policy_version=CANONICALIZATION_VERSION,
                    observed_at=now,
                    provider_updated_at=updated_at,
                    normalized_metadata=meta,
                    parent_shas=parent_shas,
                    canonicalization_status=canon_status,
                    skip_reason_code=skip_code,
                )
            )

        has_next = next_url is not None
        return ObservedRevisionPage(
            stream_kind=GitHubStreamKind.DEFAULT_BRANCH_COMMITS,
            items=tuple(revisions),
            page=page,
            has_next=has_next,
            next_page=(page + 1) if has_next else None,
            next_url=next_url,
            requests_used=1,
            bytes_read=len(resp.body),
            etag=etag,
        )

    def fetch_commit_pull_request_associations(
        self,
        plan: FrozenImportPlan,
        *,
        commit_sha: str,
        token_secret_ref: str | None = None,
    ) -> CommitPullRequestAssociation:
        """Query GitHub ``/commits/{sha}/pulls`` for authoritative provider PR associations."""
        sha = normalize_commit_sha(commit_sha, "commit_sha")
        url = f"{self._api_base_url}/repositories/{plan.provider_resource_id}/commits/{sha}/pulls"
        raw_list, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        entries = self._expect_list(raw_list, context="commit pull request associations")
        pr_numbers = sorted(
            {
                num
                for item in entries
                if isinstance(item, Mapping)
                and (num := _optional_int(item.get("number"))) is not None
                and num >= 1
            }
        )
        state = (
            PrAssociationState.MATCHED_PR
            if pr_numbers
            else PrAssociationState.NO_MATCH_OBSERVED
        )
        return CommitPullRequestAssociation(
            commit_sha=sha,
            associated_pr_numbers=tuple(pr_numbers),
            pr_association_state=state,
            observed_at=self._clock(),
            requests_used=1,
            bytes_read=len(resp.body),
        )

    def fetch_referenced_issue(
        self,
        plan: FrozenImportPlan,
        *,
        issue_number: int,
        token_secret_ref: str | None = None,
    ) -> ObservedGitHubRevision:
        """Fetch an optional PR-referenced Issue (context-only stream, strictly non-raw)."""
        if issue_number < 1:
            raise ValueError("issue_number must be >= 1.")
        url = f"{self._api_base_url}/repositories/{plan.provider_resource_id}/issues/{issue_number}"
        raw_issue, resp = self._transport.get_json(
            url,
            token_secret_ref=token_secret_ref,
            max_bytes=plan.limits.max_item_bytes,
        )
        issue_data = self._expect_dict(raw_issue, context="referenced issue")
        etag = resp.get_header("ETag")
        content_hash, byte_len, char_len, canon_status, skip_code = _hash_and_discard_raw_bytes(
            SourceObjectType.ISSUE, issue_data
        )
        author_id, author_login = _extract_user_fields(issue_data.get("user"))
        updated_at = parse_github_iso_timestamp(issue_data.get("updated_at"))
        meta = _clean_metadata(
            {
                "provider_number": issue_number,
                "state": str(issue_data.get("state")) if issue_data.get("state") is not None else None,
                "author_external_id": author_id,
                "author_login": author_login,
                "labels": _extract_label_names(issue_data.get("labels")) or None,
                "comment_count": _optional_int(issue_data.get("comments")),
                "created_at": issue_data.get("created_at") if isinstance(issue_data.get("created_at"), str) else None,
                "updated_at": issue_data.get("updated_at") if isinstance(issue_data.get("updated_at"), str) else None,
                "closed_at": issue_data.get("closed_at") if isinstance(issue_data.get("closed_at"), str) else None,
                "html_url": issue_data.get("html_url") if isinstance(issue_data.get("html_url"), str) else None,
                "api_etag": etag,
                "byte_length": byte_len,
                "char_length": char_len,
                "canonicalization_version": CANONICALIZATION_VERSION,
            }
        )
        return ObservedGitHubRevision(
            source_object_type=SourceObjectType.ISSUE,
            stream_kind=GitHubStreamKind.REFERENCED_ISSUES,
            provider_resource_id=plan.provider_resource_id,
            provider_object_id=str(issue_number),
            provider_revision_id=etag,
            content_hash=content_hash,
            hash_policy_version=CANONICALIZATION_VERSION,
            observed_at=self._clock(),
            provider_updated_at=updated_at,
            normalized_metadata=meta,
            canonicalization_status=canon_status,
            skip_reason_code=skip_code,
        )
