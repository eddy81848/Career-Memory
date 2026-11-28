"""Deterministic offline ``local_sample`` GitHub catalog & live public-read adapters.

Supports two strictly read-only modes:
1. ``local_sample`` (default, 0 external network calls, ₩0):
   - ``SamplePublicGitHubCatalog`` holds two realistic public repositories:
     * ``seoyeon-dev/ledger-engine`` (3 merged PRs + reviews + comments + commits + diffs
       with concrete metrics such as webhook replay latency p99 ``180ms -> 42ms`` and
       idempotency ledger dedupe ``100%``, plus 1 direct default-branch hotfix commit)
     * ``seoyeon-dev/search-gateway`` (2 merged PRs + reviews + comments + commits + diffs
       covering PostgreSQL FTS ranking & cursor pagination)
   - ``SampleGitHubCredentialProvider`` implements ``GitHubCredentialProviderPort``
     (``read:user`` scope only).
   - ``SampleGitHubWireTransport`` implements ``GitHubWireTransportPort`` so BOTH
     ``HttpGitHubImportClient`` AND ``GitHubResolverContentFetcher`` read the exact same
     canonical JSON/diff payloads, ensuring 100% live SHA-256 hash verification under Plan C.
2. ``live_public_github`` (opt-in via ``CAREER_MEMORY_GITHUB_PROVIDER_MODE=live``):
   - Uses real ``UrllibGitHubWireTransport`` + ``SafeGitHubHttpTransport`` with
     ``EndpointSecurityPolicy`` SSRF/allowlist enforcement.
   - Wires ``GitHubOAuthAppCredentialProvider`` when ``GITHUB_CLIENT_ID`` &
     ``GITHUB_CLIENT_SECRET`` are configured, or ``LivePublicReadGitHubCredentialProvider``
     for read-only public repository registration using optional ``GITHUB_PUBLIC_READ_TOKEN``.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from career_memory_api.fullstack_runtime.models import SampleRepositoryDescriptor
from career_memory_api.github_connect.models import (
    REQUIRED_GITHUB_OAUTH_SCOPE,
    GitHubCredentialMode,
    GitHubExchangedCredential,
    GitHubInspectionOutcome,
    GitHubRepositoryInspection,
    GitHubRepositoryMetadata,
    GitHubRevokeResult,
    parse_oauth_scopes,
    validate_minimum_github_scopes,
)
from career_memory_api.github_connect.oauth_app_provider import (
    GitHubOAuthAppConfig,
    GitHubOAuthAppCredentialProvider,
)
from career_memory_api.github_connect.ports import (
    GitHubHttpRequest,
    GitHubHttpResponse,
)
from career_memory_api.github_provider.models import (
    GitHubErrorCode,
    GitHubProviderError,
    format_utc_iso,
)
from career_memory_api.github_provider.transport import (
    SafeGitHubHttpTransport,
    WireHttpRequest,
    WireHttpResponse,
)


def _det_sha(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _json_response(
    payload: Any,
    *,
    status_code: int = 200,
    etag: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> WireHttpResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "X-OAuth-Scopes": REQUIRED_GITHUB_OAUTH_SCOPE,
    }
    if etag:
        headers["ETag"] = etag
    if extra_headers:
        headers.update(extra_headers)
    return WireHttpResponse(status_code=status_code, headers=headers, body=body)


def _diff_response(diff_text: str, *, etag: str | None = None) -> WireHttpResponse:
    body = diff_text.encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/vnd.github.v3.diff; charset=utf-8",
        "Content-Length": str(len(body)),
    }
    if etag:
        headers["ETag"] = etag
    return WireHttpResponse(status_code=200, headers=headers, body=body)


@dataclass(frozen=True)
class SampleRepositoryBundle:
    """In-memory GitHub REST v3 representation of a single public repository."""

    descriptor: SampleRepositoryDescriptor
    repo_payload: dict[str, Any]
    head_sha: str
    pulls_by_number: dict[int, dict[str, Any]]
    diffs_by_pr_number: dict[int, str]
    reviews_by_pr_number: dict[int, list[dict[str, Any]]]
    review_comments_by_pr_number: dict[int, list[dict[str, Any]]]
    issue_comments_by_pr_number: dict[int, list[dict[str, Any]]]
    commits_ordered: list[dict[str, Any]]
    commits_by_sha: dict[str, dict[str, Any]]
    commit_pr_numbers: dict[str, list[int]]
    issues_by_number: dict[int, dict[str, Any]]
    reviews_by_id: dict[str, dict[str, Any]]
    review_comments_by_id: dict[str, dict[str, Any]]
    issue_comments_by_id: dict[str, dict[str, Any]]


def _build_actor(uid: int, login: str) -> dict[str, Any]:
    return {"id": uid, "login": login, "type": "User"}


def _build_ledger_engine_bundle(base_time: datetime) -> SampleRepositoryBundle:
    repo_id = 810001
    owner = "seoyeon-dev"
    name = "ledger-engine"
    full_name = f"{owner}/{name}"
    html_base = f"https://github.com/{full_name}"
    seoyeon = _build_actor(904210, "seoyeon-dev")
    reviewer = _build_actor(904299, "minho-reviewer")

    repo_payload: dict[str, Any] = {
        "id": repo_id,
        "name": name,
        "full_name": full_name,
        "owner": seoyeon,
        "private": False,
        "visibility": "public",
        "html_url": html_base,
        "description": "Idempotent settlement ledger & webhook replay defense engine",
        "default_branch": "main",
        "language": "Python",
        "archived": False,
        "fork": False,
        "updated_at": format_utc_iso(base_time - timedelta(days=1)),
    }

    # Commits (4 default-branch commits: 3 tied to merged PRs + 1 direct hotfix commit)
    sha_pr101 = _det_sha("ledger-engine:pr101:head")
    sha_pr102 = _det_sha("ledger-engine:pr102:head")
    sha_pr103 = _det_sha("ledger-engine:pr103:head")
    sha_hotfix = _det_sha("ledger-engine:hotfix:direct")
    sha_root = _det_sha("ledger-engine:root")

    t_pr101 = base_time - timedelta(days=28)
    t_pr102 = base_time - timedelta(days=18)
    t_pr103 = base_time - timedelta(days=9)
    t_hotfix = base_time - timedelta(days=3)

    def _repo_ref() -> dict[str, Any]:
        return {"id": repo_id, "full_name": full_name, "private": False, "visibility": "public"}

    pr101_body = (
        "## Summary\n\n"
        "Implemented deterministic HMAC-SHA256 webhook signature verification and replay-window "
        "deduplication in the settlement ingress pipeline.\n\n"
        "## Measured Impact\n\n"
        "- Reduced webhook replay verification p99 latency from 180ms to 42ms in production traffic.\n"
        "- Achieved 100% idempotency ledger deduplication across 12,400 replayed webhook deliveries.\n"
        "- Added fail-closed clock-skew rejection outside +/- 300s window. Refs #11.\n"
    )
    pr102_body = (
        "## Summary\n\n"
        "Built double-entry PostgreSQL advisory-locked settlement ledger postings with "
        "deterministic idempotency keys and zero-balance drift assertions.\n\n"
        "## Verification\n\n"
        "- Eliminated concurrent double-posting race conditions under 32-worker stress load.\n"
        "- Verified 100% idempotency ledger dedupe across retry storms without manual reconciliation.\n"
    )
    pr103_body = (
        "## Summary\n\n"
        "Introduced dead-letter quarantine queue and exponential backoff worker leases for "
        "downstream payment provider timeouts.\n\n"
        "## Impact\n\n"
        "- Bounded poison-message isolation reduced worker queue saturation by 74% during provider outages.\n"
        "- Preserved exact checkpoint recovery on worker restart without duplicate charges.\n"
    )

    pulls: list[dict[str, Any]] = [
        {
            "id": 810103,
            "number": 103,
            "state": "closed",
            "title": "Isolate poison webhook jobs with bounded backoff and lease fencing",
            "body": pr103_body,
            "user": seoyeon,
            "draft": False,
            "merged": True,
            "merged_at": format_utc_iso(t_pr103),
            "created_at": format_utc_iso(t_pr103 - timedelta(days=2)),
            "updated_at": format_utc_iso(t_pr103 + timedelta(minutes=10)),
            "closed_at": format_utc_iso(t_pr103),
            "html_url": f"{html_base}/pull/103",
            "base": {"ref": "main", "sha": sha_pr102, "repo": _repo_ref()},
            "head": {"ref": "feat/dlq-lease-fencing", "sha": sha_pr103, "repo": _repo_ref()},
            "merge_commit_sha": sha_pr103,
            "labels": [{"name": "reliability"}, {"name": "ledger"}],
            "changed_files": 7,
            "additions": 312,
            "deletions": 48,
            "commits": 3,
            "comments": 1,
            "review_comments": 1,
        },
        {
            "id": 810102,
            "number": 102,
            "state": "closed",
            "title": "Enforce atomic double-entry idempotency ledger postings",
            "body": pr102_body,
            "user": seoyeon,
            "draft": False,
            "merged": True,
            "merged_at": format_utc_iso(t_pr102),
            "created_at": format_utc_iso(t_pr102 - timedelta(days=3)),
            "updated_at": format_utc_iso(t_pr102 + timedelta(minutes=15)),
            "closed_at": format_utc_iso(t_pr102),
            "html_url": f"{html_base}/pull/102",
            "base": {"ref": "main", "sha": sha_pr101, "repo": _repo_ref()},
            "head": {"ref": "feat/idempotency-ledger", "sha": sha_pr102, "repo": _repo_ref()},
            "merge_commit_sha": sha_pr102,
            "labels": [{"name": "ledger"}, {"name": "postgres"}],
            "changed_files": 9,
            "additions": 428,
            "deletions": 64,
            "commits": 4,
            "comments": 1,
            "review_comments": 1,
        },
        {
            "id": 810101,
            "number": 101,
            "state": "closed",
            "title": "Harden webhook HMAC verification and replay latency cache",
            "body": pr101_body,
            "user": seoyeon,
            "draft": False,
            "merged": True,
            "merged_at": format_utc_iso(t_pr101),
            "created_at": format_utc_iso(t_pr101 - timedelta(days=4)),
            "updated_at": format_utc_iso(t_pr101 + timedelta(minutes=20)),
            "closed_at": format_utc_iso(t_pr101),
            "html_url": f"{html_base}/pull/101",
            "base": {"ref": "main", "sha": sha_root, "repo": _repo_ref()},
            "head": {"ref": "feat/webhook-replay-guard", "sha": sha_pr101, "repo": _repo_ref()},
            "merge_commit_sha": sha_pr101,
            "labels": [{"name": "security"}, {"name": "performance"}],
            "changed_files": 6,
            "additions": 265,
            "deletions": 39,
            "commits": 2,
            "comments": 1,
            "review_comments": 1,
        },
    ]

    diffs_by_pr = {
        101: (
            "diff --git a/ledger/webhook_guard.py b/ledger/webhook_guard.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/ledger/webhook_guard.py\n"
            "+++ b/ledger/webhook_guard.py\n"
            "@@ -12,6 +12,18 @@ def verify_webhook_signature(secret: bytes, payload: bytes, sig: str) -> bool:\n"
            "+    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()\n"
            "+    return hmac.compare_digest(expected, sig)\n"
        ),
        102: (
            "diff --git a/ledger/posting.py b/ledger/posting.py\n"
            "index 3333333..4444444 100644\n"
            "--- a/ledger/posting.py\n"
            "+++ b/ledger/posting.py\n"
            "@@ -40,4 +40,14 @@ def record_double_entry(cur, idempotency_key: str, entries: list) -> None:\n"
            "+    cur.execute('INSERT INTO idempotency_ledger (key) VALUES (%s) ON CONFLICT DO NOTHING', (idempotency_key,))\n"
        ),
        103: (
            "diff --git a/ledger/worker.py b/ledger/worker.py\n"
            "index 5555555..6666666 100644\n"
            "--- a/ledger/worker.py\n"
            "+++ b/ledger/worker.py\n"
            "@@ -22,3 +22,12 @@ def quarantine_poison_job(cur, job_id: str) -> None:\n"
            "+    cur.execute(\"UPDATE jobs SET status = 'DEAD_LETTER' WHERE id = %s\", (job_id,))\n"
        ),
    }

    reviews_by_pr: dict[int, list[dict[str, Any]]] = {
        101: [
            {
                "id": 910101,
                "user": reviewer,
                "state": "APPROVED",
                "body": "Confirmed constant-time HMAC check and benchmarked p99 drop from 180ms to 42ms.",
                "commit_id": sha_pr101,
                "submitted_at": format_utc_iso(t_pr101 - timedelta(hours=2)),
                "html_url": f"{html_base}/pull/101#pullrequestreview-910101",
            }
        ],
        102: [
            {
                "id": 910102,
                "user": reviewer,
                "state": "APPROVED",
                "body": "Double-entry invariant and 100% idempotency dedupe tests look solid.",
                "commit_id": sha_pr102,
                "submitted_at": format_utc_iso(t_pr102 - timedelta(hours=3)),
                "html_url": f"{html_base}/pull/102#pullrequestreview-910102",
            }
        ],
        103: [
            {
                "id": 910103,
                "user": reviewer,
                "state": "APPROVED",
                "body": "Lease fencing cleanly stops stale workers from committing after timeout.",
                "commit_id": sha_pr103,
                "submitted_at": format_utc_iso(t_pr103 - timedelta(hours=1)),
                "html_url": f"{html_base}/pull/103#pullrequestreview-910103",
            }
        ],
    }

    review_comments_by_pr: dict[int, list[dict[str, Any]]] = {
        101: [
            {
                "id": 920101,
                "user": reviewer,
                "body": "Nice move moving replay-nonce lookup onto the covering B-tree index.",
                "commit_id": sha_pr101,
                "created_at": format_utc_iso(t_pr101 - timedelta(hours=3)),
                "updated_at": format_utc_iso(t_pr101 - timedelta(hours=3)),
                "html_url": f"{html_base}/pull/101#discussion_r920101",
            }
        ],
        102: [],
        103: [],
    }

    issue_comments_by_pr: dict[int, list[dict[str, Any]]] = {
        101: [
            {
                "id": 930101,
                "user": seoyeon,
                "body": "Verified p99 webhook replay latency stays at 42ms after 24h canary soak.",
                "created_at": format_utc_iso(t_pr101 - timedelta(hours=1)),
                "updated_at": format_utc_iso(t_pr101 - timedelta(hours=1)),
                "html_url": f"{html_base}/pull/101#issuecomment-930101",
            }
        ],
        102: [],
        103: [],
    }

    issues_by_number: dict[int, dict[str, Any]] = {
        11: {
            "id": 940011,
            "number": 11,
            "state": "closed",
            "title": "Webhook replay verification p99 spike above 150ms",
            "body": "Track p99 latency regression during billing webhook replay bursts.",
            "user": reviewer,
            "labels": [{"name": "performance"}],
            "comments": 2,
            "created_at": format_utc_iso(t_pr101 - timedelta(days=7)),
            "updated_at": format_utc_iso(t_pr101),
            "closed_at": format_utc_iso(t_pr101),
            "html_url": f"{html_base}/issues/11",
        }
    }

    commits_ordered: list[dict[str, Any]] = [
        {
            "sha": sha_hotfix,
            "author": seoyeon,
            "committer": seoyeon,
            "parents": [{"sha": sha_pr103}],
            "html_url": f"{html_base}/commit/{sha_hotfix}",
            "commit": {
                "message": (
                    "hotfix: clamp webhook replay clock skew tolerance to 300s\n\n"
                    "Direct default-branch operational fix after NTP drift alert."
                ),
                "author": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_hotfix),
                },
                "committer": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_hotfix),
                },
            },
        },
        {
            "sha": sha_pr103,
            "author": seoyeon,
            "committer": seoyeon,
            "parents": [{"sha": sha_pr102}],
            "html_url": f"{html_base}/commit/{sha_pr103}",
            "commit": {
                "message": "Isolate poison webhook jobs with bounded backoff and lease fencing (#103)",
                "author": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr103),
                },
                "committer": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr103),
                },
            },
        },
        {
            "sha": sha_pr102,
            "author": seoyeon,
            "committer": seoyeon,
            "parents": [{"sha": sha_pr101}],
            "html_url": f"{html_base}/commit/{sha_pr102}",
            "commit": {
                "message": "Enforce atomic double-entry idempotency ledger postings (#102)",
                "author": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr102),
                },
                "committer": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr102),
                },
            },
        },
        {
            "sha": sha_pr101,
            "author": seoyeon,
            "committer": seoyeon,
            "parents": [{"sha": sha_root}],
            "html_url": f"{html_base}/commit/{sha_pr101}",
            "commit": {
                "message": "Harden webhook HMAC verification and replay latency cache (#101)",
                "author": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr101),
                },
                "committer": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr101),
                },
            },
        },
    ]

    commit_pr_numbers = {
        sha_hotfix: [],
        sha_pr103: [103],
        sha_pr102: [102],
        sha_pr101: [101],
    }

    return SampleRepositoryBundle(
        descriptor=SampleRepositoryDescriptor(
            provider_resource_id=str(repo_id),
            owner=owner,
            name=name,
            full_name=full_name,
            canonical_url=html_base,
            default_branch="main",
            primary_language="Python",
            description="Idempotent settlement ledger & webhook replay defense engine",
            estimated_commits=len(commits_ordered),
            estimated_pull_requests=len(pulls),
        ),
        repo_payload=repo_payload,
        head_sha=sha_hotfix,
        pulls_by_number={int(p["number"]): p for p in pulls},
        diffs_by_pr_number=diffs_by_pr,
        reviews_by_pr_number=reviews_by_pr,
        review_comments_by_pr_number=review_comments_by_pr,
        issue_comments_by_pr_number=issue_comments_by_pr,
        commits_ordered=commits_ordered,
        commits_by_sha={str(c["sha"]): c for c in commits_ordered},
        commit_pr_numbers=commit_pr_numbers,
        issues_by_number=issues_by_number,
        reviews_by_id={
            str(r["id"]): r
            for items in reviews_by_pr.values()
            for r in items
        },
        review_comments_by_id={
            str(c["id"]): c
            for items in review_comments_by_pr.values()
            for c in items
        },
        issue_comments_by_id={
            str(c["id"]): c
            for items in issue_comments_by_pr.values()
            for c in items
        },
    )


def _build_search_gateway_bundle(base_time: datetime) -> SampleRepositoryBundle:
    repo_id = 810002
    owner = "seoyeon-dev"
    name = "search-gateway"
    full_name = f"{owner}/{name}"
    html_base = f"https://github.com/{full_name}"
    seoyeon = _build_actor(904210, "seoyeon-dev")
    reviewer = _build_actor(904299, "minho-reviewer")

    repo_payload: dict[str, Any] = {
        "id": repo_id,
        "name": name,
        "full_name": full_name,
        "owner": seoyeon,
        "private": False,
        "visibility": "public",
        "html_url": html_base,
        "description": "PostgreSQL FTS ranking & versioned cursor pagination gateway",
        "default_branch": "main",
        "language": "TypeScript",
        "archived": False,
        "fork": False,
        "updated_at": format_utc_iso(base_time - timedelta(days=2)),
    }

    sha_pr201 = _det_sha("search-gateway:pr201:head")
    sha_pr202 = _det_sha("search-gateway:pr202:head")
    sha_root = _det_sha("search-gateway:root")
    t_pr201 = base_time - timedelta(days=14)
    t_pr202 = base_time - timedelta(days=5)

    def _repo_ref() -> dict[str, Any]:
        return {"id": repo_id, "full_name": full_name, "private": False, "visibility": "public"}

    pr201_body = (
        "## Summary\n\n"
        "Implemented PostgreSQL GIN-backed FTS ranking (`ts_rank_cd`) with authoritative "
        "pre-response permission filtering across WorkEvents, Facts, and Claims.\n\n"
        "## Impact\n\n"
        "- Kept search p95 query latency under 28ms across 50,000 indexed workspace entries.\n"
        "- Eliminated index-lag leakage by re-checking deletion fences inside the read transaction.\n"
    )
    pr202_body = (
        "## Summary\n\n"
        "Added HMAC-signed opaque cursor pagination bound to `(workspace_id, source_resource_id, "
        "query_hash, index_version, ranking_version)`.\n\n"
        "## Verification\n\n"
        "- Rejects tampered or stale index-version cursors fail-closed with 400 INVALID_SEARCH_CURSOR.\n"
        "- Preserves deterministic tie-breaking across concurrent index rebuilds.\n"
    )

    pulls: list[dict[str, Any]] = [
        {
            "id": 820202,
            "number": 202,
            "state": "closed",
            "title": "Bind opaque search cursors to index and ranking versions",
            "body": pr202_body,
            "user": seoyeon,
            "draft": False,
            "merged": True,
            "merged_at": format_utc_iso(t_pr202),
            "created_at": format_utc_iso(t_pr202 - timedelta(days=2)),
            "updated_at": format_utc_iso(t_pr202 + timedelta(minutes=8)),
            "closed_at": format_utc_iso(t_pr202),
            "html_url": f"{html_base}/pull/202",
            "base": {"ref": "main", "sha": sha_pr201, "repo": _repo_ref()},
            "head": {"ref": "feat/versioned-search-cursor", "sha": sha_pr202, "repo": _repo_ref()},
            "merge_commit_sha": sha_pr202,
            "labels": [{"name": "search"}, {"name": "pagination"}],
            "changed_files": 5,
            "additions": 194,
            "deletions": 21,
            "commits": 2,
            "comments": 1,
            "review_comments": 0,
        },
        {
            "id": 820201,
            "number": 201,
            "state": "closed",
            "title": "Add PostgreSQL FTS ranking with authoritative pre-response filter",
            "body": pr201_body,
            "user": seoyeon,
            "draft": False,
            "merged": True,
            "merged_at": format_utc_iso(t_pr201),
            "created_at": format_utc_iso(t_pr201 - timedelta(days=3)),
            "updated_at": format_utc_iso(t_pr201 + timedelta(minutes=12)),
            "closed_at": format_utc_iso(t_pr201),
            "html_url": f"{html_base}/pull/201",
            "base": {"ref": "main", "sha": sha_root, "repo": _repo_ref()},
            "head": {"ref": "feat/pg-fts-ranking", "sha": sha_pr201, "repo": _repo_ref()},
            "merge_commit_sha": sha_pr201,
            "labels": [{"name": "search"}, {"name": "postgres"}],
            "changed_files": 8,
            "additions": 356,
            "deletions": 40,
            "commits": 3,
            "comments": 1,
            "review_comments": 0,
        },
    ]

    diffs_by_pr = {
        201: (
            "diff --git a/src/fts_rank.sql b/src/fts_rank.sql\n"
            "index aaaaaaa..bbbbbbb 100644\n"
            "--- a/src/fts_rank.sql\n"
            "+++ b/src/fts_rank.sql\n"
            "@@ -1,3 +1,8 @@\n"
            "+SELECT entity_id, ts_rank_cd(search_tsv, websearch_to_tsquery('simple', $1)) AS score\n"
            "+FROM search_documents WHERE workspace_id = $2;\n"
        ),
        202: (
            "diff --git a/src/cursor.ts b/src/cursor.ts\n"
            "index ccccccc..ddddddd 100644\n"
            "--- a/src/cursor.ts\n"
            "+++ b/src/cursor.ts\n"
            "@@ -10,4 +10,9 @@ export function verifyCursor(token: string, version: string) {\n"
            "+  if (parsed.index_version !== version) throw new InvalidCursorError();\n"
        ),
    }

    reviews_by_pr: dict[int, list[dict[str, Any]]] = {
        201: [
            {
                "id": 910201,
                "user": reviewer,
                "state": "APPROVED",
                "body": "FTS GIN plan hits index scan cleanly and respects workspace isolation.",
                "commit_id": sha_pr201,
                "submitted_at": format_utc_iso(t_pr201 - timedelta(hours=2)),
                "html_url": f"{html_base}/pull/201#pullrequestreview-910201",
            }
        ],
        202: [
            {
                "id": 910202,
                "user": reviewer,
                "state": "APPROVED",
                "body": "Cursor HMAC verification prevents cross-scope replay cleanly.",
                "commit_id": sha_pr202,
                "submitted_at": format_utc_iso(t_pr202 - timedelta(hours=1)),
                "html_url": f"{html_base}/pull/202#pullrequestreview-910202",
            }
        ],
    }

    issue_comments_by_pr: dict[int, list[dict[str, Any]]] = {
        201: [
            {
                "id": 930201,
                "user": seoyeon,
                "body": "EXPLAIN ANALYZE verified p95 < 28ms on 50k search_documents rows.",
                "created_at": format_utc_iso(t_pr201 - timedelta(hours=1)),
                "updated_at": format_utc_iso(t_pr201 - timedelta(hours=1)),
                "html_url": f"{html_base}/pull/201#issuecomment-930201",
            }
        ],
        202: [
            {
                "id": 930202,
                "user": seoyeon,
                "body": "Added drift regression tests covering index_version bumps.",
                "created_at": format_utc_iso(t_pr202 - timedelta(minutes=30)),
                "updated_at": format_utc_iso(t_pr202 - timedelta(minutes=30)),
                "html_url": f"{html_base}/pull/202#issuecomment-930202",
            }
        ],
    }

    commits_ordered: list[dict[str, Any]] = [
        {
            "sha": sha_pr202,
            "author": seoyeon,
            "committer": seoyeon,
            "parents": [{"sha": sha_pr201}],
            "html_url": f"{html_base}/commit/{sha_pr202}",
            "commit": {
                "message": "Bind opaque search cursors to index and ranking versions (#202)",
                "author": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr202),
                },
                "committer": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr202),
                },
            },
        },
        {
            "sha": sha_pr201,
            "author": seoyeon,
            "committer": seoyeon,
            "parents": [{"sha": sha_root}],
            "html_url": f"{html_base}/commit/{sha_pr201}",
            "commit": {
                "message": "Add PostgreSQL FTS ranking with authoritative pre-response filter (#201)",
                "author": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr201),
                },
                "committer": {
                    "name": "Seoyeon Park",
                    "email": "904210+seoyeon-dev@users.noreply.github.com",
                    "date": format_utc_iso(t_pr201),
                },
            },
        },
    ]

    commit_pr_numbers = {
        sha_pr202: [202],
        sha_pr201: [201],
    }

    return SampleRepositoryBundle(
        descriptor=SampleRepositoryDescriptor(
            provider_resource_id=str(repo_id),
            owner=owner,
            name=name,
            full_name=full_name,
            canonical_url=html_base,
            default_branch="main",
            primary_language="TypeScript",
            description="PostgreSQL FTS ranking & versioned cursor pagination gateway",
            estimated_commits=len(commits_ordered),
            estimated_pull_requests=len(pulls),
        ),
        repo_payload=repo_payload,
        head_sha=sha_pr202,
        pulls_by_number={int(p["number"]): p for p in pulls},
        diffs_by_pr_number=diffs_by_pr,
        reviews_by_pr_number=reviews_by_pr,
        review_comments_by_pr_number={201: [], 202: []},
        issue_comments_by_pr_number=issue_comments_by_pr,
        commits_ordered=commits_ordered,
        commits_by_sha={str(c["sha"]): c for c in commits_ordered},
        commit_pr_numbers=commit_pr_numbers,
        issues_by_number={},
        reviews_by_id={
            str(r["id"]): r
            for items in reviews_by_pr.values()
            for r in items
        },
        review_comments_by_id={},
        issue_comments_by_id={
            str(c["id"]): c
            for items in issue_comments_by_pr.values()
            for c in items
        },
    )


class SamplePublicGitHubCatalog:
    """Deterministic in-memory catalog of public GitHub repositories for ``local_sample`` mode."""

    def __init__(self, *, base_time: datetime | None = None) -> None:
        anchor_time = (base_time or datetime.now(UTC)).replace(microsecond=0)
        bundles = (
            _build_ledger_engine_bundle(anchor_time),
            _build_search_gateway_bundle(anchor_time),
        )
        self._by_id: dict[str, SampleRepositoryBundle] = {
            b.descriptor.provider_resource_id: b for b in bundles
        }
        self._by_full_name: dict[str, SampleRepositoryBundle] = {
            b.descriptor.full_name.lower(): b for b in bundles
        }

    def list_descriptors(self) -> tuple[SampleRepositoryDescriptor, ...]:
        return tuple(b.descriptor for b in self._by_id.values())

    def get_by_id(self, provider_resource_id: str) -> SampleRepositoryBundle | None:
        return self._by_id.get(str(provider_resource_id).strip())

    def get_by_full_name(self, owner: str, repo: str) -> SampleRepositoryBundle | None:
        return self._by_full_name.get(f"{owner.strip()}/{repo.strip()}".lower())


class SampleGitHubCredentialProvider:
    """Offline ``GitHubCredentialProviderPort`` backed by ``SamplePublicGitHubCatalog``."""

    def __init__(
        self,
        catalog: SamplePublicGitHubCatalog,
        *,
        external_account_id: str = "904210",
        login: str = "seoyeon-dev",
    ) -> None:
        self._catalog = catalog
        self._external_account_id = external_account_id
        self._login = login

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
        query = urlencode(
            {
                "client_id": "local-sample-github-oauth-app",
                "redirect_uri": redirect_uri,
                "scope": REQUIRED_GITHUB_OAUTH_SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            }
        )
        return f"https://github.com/login/oauth/authorize?{query}"

    def exchange_authorization_code(
        self,
        *,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> GitHubExchangedCredential:
        del code, pkce_code_verifier, redirect_uri
        return GitHubExchangedCredential(
            external_account_id=self._external_account_id,
            login=self._login,
            access_token="gho_local_sample_read_user_token_0001",
            scopes=(REQUIRED_GITHUB_OAUTH_SCOPE,),
            token_type="bearer",
        )

    def inspect_repository(
        self,
        *,
        access_token: str,
        owner: str,
        repo: str,
    ) -> GitHubRepositoryInspection:
        del access_token
        bundle = self._catalog.get_by_full_name(owner, repo)
        if bundle is None:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY,
                http_status=404,
                observed_scopes=(REQUIRED_GITHUB_OAUTH_SCOPE,),
                reason=f"Sample repository {owner}/{repo} not found in local_sample catalog.",
            )
        desc = bundle.descriptor
        return GitHubRepositoryInspection(
            outcome=GitHubInspectionOutcome.PUBLIC_CONFIRMED,
            http_status=200,
            observed_visibility="PUBLIC",
            repository=GitHubRepositoryMetadata(
                provider_resource_id=desc.provider_resource_id,
                full_name=desc.full_name,
                canonical_url=desc.canonical_url,
                private=False,
                visibility="public",
                default_branch=desc.default_branch,
            ),
            observed_scopes=(REQUIRED_GITHUB_OAUTH_SCOPE,),
            reason="Sample public repository confirmed strictly PUBLIC.",
        )

    def revoke_grant(self, *, access_token: str) -> GitHubRevokeResult:
        del access_token
        return GitHubRevokeResult(revoked=True, already_revoked=False, http_status=204)


_RE_REPO_BY_NAME = re.compile(r"^/repos/([^/]+)/([^/]+)$")
_RE_REPO_BY_ID = re.compile(r"^/repositories/([^/]+)$")
_RE_BRANCH = re.compile(r"^/repositories/([^/]+)/branches/([^/]+)$")
_RE_PULLS = re.compile(r"^/repositories/([^/]+)/pulls$")
_RE_PULL_DETAIL = re.compile(r"^/repositories/([^/]+)/pulls/(\d+)$")
_RE_PULL_REVIEWS = re.compile(r"^/repositories/([^/]+)/pulls/(\d+)/reviews$")
_RE_PULL_REVIEW_DETAIL = re.compile(r"^/repositories/([^/]+)/pulls/(\d+)/reviews/(\d+)$")
_RE_PULL_COMMENTS = re.compile(r"^/repositories/([^/]+)/pulls/(\d+)/comments$")
_RE_PULL_COMMENT_DETAIL = re.compile(r"^/repositories/([^/]+)/pulls/comments/(\d+)$")
_RE_ISSUE_COMMENTS = re.compile(r"^/repositories/([^/]+)/issues/(\d+)/comments$")
_RE_ISSUE_COMMENT_DETAIL = re.compile(r"^/repositories/([^/]+)/issues/comments/(\d+)$")
_RE_ISSUE_DETAIL = re.compile(r"^/repositories/([^/]+)/issues/(\d+)$")
_RE_COMMITS = re.compile(r"^/repositories/([^/]+)/commits$")
_RE_COMMIT_DETAIL = re.compile(r"^/repositories/([^/]+)/commits/([0-9a-fA-F]{40})$")
_RE_COMMIT_PULLS = re.compile(r"^/repositories/([^/]+)/commits/([0-9a-fA-F]{40})/pulls$")


class SampleGitHubWireTransport:
    """Zero-network ``GitHubWireTransportPort`` serving ``SamplePublicGitHubCatalog``."""

    def __init__(self, catalog: SamplePublicGitHubCatalog) -> None:
        self._catalog = catalog

    def send(self, request: WireHttpRequest) -> WireHttpResponse:
        parts = urlsplit(request.url)
        path = parts.path
        qs = parse_qs(parts.query)
        page = int(qs.get("page", ["1"])[0])
        accept = next(
            (v for k, v in request.headers.items() if k.lower() == "accept"),
            "application/vnd.github+json",
        )

        # GET /repos/{owner}/{repo}
        m = _RE_REPO_BY_NAME.match(path)
        if m:
            bundle = self._catalog.get_by_full_name(m.group(1), m.group(2))
            if bundle is None:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(bundle.repo_payload, etag=f'W/"repo-{bundle.descriptor.provider_resource_id}"')

        # GET /repositories/{id}
        m = _RE_REPO_BY_ID.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            if bundle is None:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(bundle.repo_payload, etag=f'W/"repo-{bundle.descriptor.provider_resource_id}"')

        # GET /repositories/{id}/branches/{branch}
        m = _RE_BRANCH.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            if bundle is None or m.group(2) != bundle.descriptor.default_branch:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(
                {"name": bundle.descriptor.default_branch, "commit": {"sha": bundle.head_sha}},
                etag=f'W/"branch-{bundle.head_sha[:12]}"',
            )

        # GET /repositories/{id}/pulls
        m = _RE_PULLS.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            if bundle is None:
                return _json_response({"message": "Not Found"}, status_code=404)
            items = (
                sorted(
                    bundle.pulls_by_number.values(),
                    key=lambda p: str(p.get("updated_at") or ""),
                    reverse=True,
                )
                if page == 1
                else []
            )
            return _json_response(items, etag=f'W/"pulls-{bundle.descriptor.provider_resource_id}-p{page}"')

        # GET /repositories/{id}/pulls/{number}
        m = _RE_PULL_DETAIL.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            pr_num = int(m.group(2))
            if bundle is None or pr_num not in bundle.pulls_by_number:
                return _json_response({"message": "Not Found"}, status_code=404)
            if "diff" in accept.lower():
                return _diff_response(
                    bundle.diffs_by_pr_number.get(pr_num, ""),
                    etag=f'W/"pr-diff-{bundle.descriptor.provider_resource_id}-{pr_num}"',
                )
            return _json_response(
                bundle.pulls_by_number[pr_num],
                etag=f'W/"pr-{bundle.descriptor.provider_resource_id}-{pr_num}"',
            )

        # GET /repositories/{id}/pulls/{number}/reviews
        m = _RE_PULL_REVIEWS.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            pr_num = int(m.group(2))
            if bundle is None:
                return _json_response({"message": "Not Found"}, status_code=404)
            items = bundle.reviews_by_pr_number.get(pr_num, []) if page == 1 else []
            return _json_response(items, etag=f'W/"reviews-{bundle.descriptor.provider_resource_id}-{pr_num}"')

        # GET /repositories/{id}/pulls/{number}/reviews/{review_id}
        m = _RE_PULL_REVIEW_DETAIL.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            rev_id = m.group(3)
            if bundle is None or rev_id not in bundle.reviews_by_id:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(
                bundle.reviews_by_id[rev_id],
                etag=f'W/"review-{rev_id}"',
            )

        # GET /repositories/{id}/pulls/{number}/comments
        m = _RE_PULL_COMMENTS.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            pr_num = int(m.group(2))
            if bundle is None:
                return _json_response({"message": "Not Found"}, status_code=404)
            items = bundle.review_comments_by_pr_number.get(pr_num, []) if page == 1 else []
            return _json_response(items, etag=f'W/"rc-{bundle.descriptor.provider_resource_id}-{pr_num}"')

        # GET /repositories/{id}/pulls/comments/{comment_id}
        m = _RE_PULL_COMMENT_DETAIL.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            cid = m.group(2)
            if bundle is None or cid not in bundle.review_comments_by_id:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(bundle.review_comments_by_id[cid], etag=f'W/"rc-{cid}"')

        # GET /repositories/{id}/issues/{number}/comments
        m = _RE_ISSUE_COMMENTS.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            pr_num = int(m.group(2))
            if bundle is None:
                return _json_response({"message": "Not Found"}, status_code=404)
            items = bundle.issue_comments_by_pr_number.get(pr_num, []) if page == 1 else []
            return _json_response(items, etag=f'W/"ic-{bundle.descriptor.provider_resource_id}-{pr_num}"')

        # GET /repositories/{id}/issues/comments/{comment_id}
        m = _RE_ISSUE_COMMENT_DETAIL.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            cid = m.group(2)
            if bundle is None or cid not in bundle.issue_comments_by_id:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(bundle.issue_comments_by_id[cid], etag=f'W/"ic-{cid}"')

        # GET /repositories/{id}/issues/{number}
        m = _RE_ISSUE_DETAIL.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            issue_num = int(m.group(2))
            if bundle is None or issue_num not in bundle.issues_by_number:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(
                bundle.issues_by_number[issue_num],
                etag=f'W/"issue-{bundle.descriptor.provider_resource_id}-{issue_num}"',
            )

        # GET /repositories/{id}/commits
        m = _RE_COMMITS.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            if bundle is None:
                return _json_response({"message": "Not Found"}, status_code=404)
            items = bundle.commits_ordered if page == 1 else []
            return _json_response(items, etag=f'W/"commits-{bundle.descriptor.provider_resource_id}-p{page}"')

        # GET /repositories/{id}/commits/{sha}
        m = _RE_COMMIT_DETAIL.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            sha = m.group(2).lower()
            if bundle is None or sha not in bundle.commits_by_sha:
                return _json_response({"message": "Not Found"}, status_code=404)
            return _json_response(bundle.commits_by_sha[sha], etag=f'W/"commit-{sha[:12]}"')

        # GET /repositories/{id}/commits/{sha}/pulls
        m = _RE_COMMIT_PULLS.match(path)
        if m:
            bundle = self._catalog.get_by_id(m.group(1))
            sha = m.group(2).lower()
            if bundle is None or sha not in bundle.commit_pr_numbers:
                return _json_response({"message": "Not Found"}, status_code=404)
            nums = bundle.commit_pr_numbers[sha]
            items = [bundle.pulls_by_number[n] for n in nums if n in bundle.pulls_by_number]
            return _json_response(items, etag=f'W/"commit-pulls-{sha[:12]}"')

        return _json_response({"message": "Not Found"}, status_code=404)


class UrllibGitHubOAuthTransport:
    """Real stdlib ``urllib.request`` transport implementing ``GitHubOAuthTransportPort``."""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout_seconds = timeout_seconds

    def send(self, request: GitHubHttpRequest) -> GitHubHttpResponse:
        headers = dict(request.headers)
        data: bytes | None = None
        if request.json_body is not None:
            data = json.dumps(request.json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif request.form_body is not None:
            data = urllib.parse.urlencode(request.form_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        req = urllib.request.Request(
            url=request.url,
            data=data,
            headers=headers,
            method=request.method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                status = int(getattr(resp, "status", resp.getcode()))
                raw_text = resp.read().decode("utf-8", errors="replace")
                resp_headers = {str(k): str(v) for k, v in resp.headers.items()}
        except urllib.error.HTTPError as err:
            status = int(err.code)
            raw_text = err.read().decode("utf-8", errors="replace") if err.fp else ""
            resp_headers = {str(k): str(v) for k, v in err.headers.items()} if err.headers else {}

        parsed: dict[str, Any] | None = None
        try:
            obj = json.loads(raw_text)
            if isinstance(obj, dict):
                parsed = obj
        except ValueError:
            parsed = None

        return GitHubHttpResponse(
            status_code=status,
            headers=resp_headers,
            json_body=parsed,
            text_body=raw_text,
        )


class LivePublicReadGitHubCredentialProvider:
    """Opt-in live public-read GitHub provider when OAuth App client credentials are omitted.

    Enforces strictly read-only public repository inspection via ``SafeGitHubHttpTransport``
    (SSRF/allowlist policy) and rejects tokens advertising write/private scopes.
    """

    def __init__(
        self,
        *,
        transport: SafeGitHubHttpTransport,
        public_read_token_ref: str | None = None,
        external_account_id: str = "904210",
        login: str = "seoyeon-dev",
    ) -> None:
        self._transport = transport
        self._public_read_token_ref = public_read_token_ref
        self._external_account_id = external_account_id
        self._login = login

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
        query = urlencode(
            {
                "client_id": "local-public-read-mode",
                "redirect_uri": redirect_uri,
                "scope": REQUIRED_GITHUB_OAUTH_SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            }
        )
        return f"https://github.com/login/oauth/authorize?{query}"

    def exchange_authorization_code(
        self,
        *,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> GitHubExchangedCredential:
        del code, pkce_code_verifier, redirect_uri
        return GitHubExchangedCredential(
            external_account_id=self._external_account_id,
            login=self._login,
            access_token="gho_local_public_read_placeholder",
            scopes=(REQUIRED_GITHUB_OAUTH_SCOPE,),
            token_type="bearer",
        )

    def inspect_repository(
        self,
        *,
        access_token: str,
        owner: str,
        repo: str,
    ) -> GitHubRepositoryInspection:
        del access_token
        safe_owner = urllib.parse.quote(owner.strip(), safe="")
        safe_repo = urllib.parse.quote(repo.strip(), safe="")
        url = f"https://api.github.com/repos/{safe_owner}/{safe_repo}"
        try:
            payload, wire_resp = self._transport.get_json(
                url,
                token_secret_ref=self._public_read_token_ref,
                require_credentials=False,
            )
        except GitHubProviderError as exc:
            if exc.code is GitHubErrorCode.RATE_LIMITED:
                return GitHubRepositoryInspection(
                    outcome=GitHubInspectionOutcome.RATE_LIMITED,
                    http_status=exc.http_status or 429,
                    retry_after_seconds=exc.retry_after_seconds,
                    reason="GitHub API rate limit exceeded.",
                )
            if exc.code is GitHubErrorCode.AUTH_REVOKED:
                return GitHubRepositoryInspection(
                    outcome=GitHubInspectionOutcome.TOKEN_REVOKED,
                    http_status=401,
                    reason="GitHub public read token rejected with 401.",
                )
            if exc.code in (GitHubErrorCode.NOT_FOUND_OR_INACCESSIBLE, GitHubErrorCode.FORBIDDEN_POLICY):
                return GitHubRepositoryInspection(
                    outcome=GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY,
                    http_status=exc.http_status or 404,
                    reason="Repository not found or not publicly accessible.",
                )
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.PROVIDER_UNAVAILABLE,
                http_status=exc.http_status or 503,
                reason=str(exc),
            )

        raw_scopes = wire_resp.get_header("X-OAuth-Scopes")
        observed_scopes: tuple[str, ...] | None = None
        if raw_scopes is not None and raw_scopes.strip():
            observed_scopes = parse_oauth_scopes(raw_scopes)
            scope_check = validate_minimum_github_scopes(observed_scopes)
            if scope_check.disallowed_scopes:
                return GitHubRepositoryInspection(
                    outcome=GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED,
                    http_status=200,
                    observed_scopes=observed_scopes,
                    reason="GITHUB_PUBLIC_READ_TOKEN includes disallowed write/private scopes.",
                )

        if not isinstance(payload, dict):
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.PROVIDER_UNAVAILABLE,
                http_status=200,
                reason="Unexpected GitHub repository response payload.",
            )

        raw_id = payload.get("id")
        full_name = str(payload.get("full_name") or f"{owner}/{repo}").strip()
        canonical_url = str(payload.get("html_url") or f"https://github.com/{full_name}").strip()
        is_private = True if payload.get("private") is None else bool(payload.get("private"))
        raw_vis = str(payload.get("visibility") or ("private" if is_private else "public")).lower()
        default_branch = str(payload.get("default_branch") or "main").strip()

        metadata = GitHubRepositoryMetadata(
            provider_resource_id=str(raw_id).strip() if raw_id is not None else "",
            full_name=full_name,
            canonical_url=canonical_url,
            private=is_private,
            visibility=raw_vis,
            default_branch=default_branch,
        )
        if not metadata.is_strictly_public:
            vis = "INTERNAL" if raw_vis == "internal" else "PRIVATE"
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
                http_status=200,
                observed_visibility=vis,
                repository=metadata,
                observed_scopes=observed_scopes or (REQUIRED_GITHUB_OAUTH_SCOPE,),
                reason=f"Repository visibility is {vis} (not PUBLIC).",
            )
        return GitHubRepositoryInspection(
            outcome=GitHubInspectionOutcome.PUBLIC_CONFIRMED,
            http_status=200,
            observed_visibility="PUBLIC",
            repository=metadata,
            observed_scopes=observed_scopes or (REQUIRED_GITHUB_OAUTH_SCOPE,),
            reason="Live GitHub repository confirmed strictly PUBLIC.",
        )

    def revoke_grant(self, *, access_token: str) -> GitHubRevokeResult:
        del access_token
        return GitHubRevokeResult(revoked=True, already_revoked=True, http_status=204)


def build_live_oauth_credential_provider(
    *,
    client_id: str,
    client_secret: str,
    allowed_redirect_uris: tuple[str, ...],
) -> GitHubOAuthAppCredentialProvider:
    """Create a real ``GitHubOAuthAppCredentialProvider`` backed by stdlib urllib."""
    return GitHubOAuthAppCredentialProvider(
        config=GitHubOAuthAppConfig(
            client_id=client_id,
            client_secret=client_secret,
            allowed_redirect_uris=allowed_redirect_uris,
        ),
        transport=UrllibGitHubOAuthTransport(),
    )


__all__ = [
    "LivePublicReadGitHubCredentialProvider",
    "SampleGitHubCredentialProvider",
    "SampleGitHubWireTransport",
    "SamplePublicGitHubCatalog",
    "SampleRepositoryBundle",
    "UrllibGitHubOAuthTransport",
    "build_live_oauth_credential_provider",
]
