"""Typed configuration and bootstrap models for the local PG18 Fullstack Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class GitHubProviderMode(StrEnum):
    """Runtime GitHub provider mode for the local fullstack server."""

    LOCAL_SAMPLE = "local_sample"
    LIVE_PUBLIC_GITHUB = "live_public_github"

    @classmethod
    def from_cli_or_env(cls, raw: str | None) -> GitHubProviderMode:
        cleaned = (raw or "sample").strip().lower()
        if cleaned in ("sample", "local_sample", "local"):
            return cls.LOCAL_SAMPLE
        if cleaned in ("live", "live_public_github", "public"):
            return cls.LIVE_PUBLIC_GITHUB
        raise ValueError(
            f"Unsupported GitHub provider mode: {raw!r}. Expected 'sample' or 'live'."
        )


@dataclass(frozen=True, slots=True)
class FullstackRuntimeConfig:
    """Configuration for ``FullstackPostgresRuntime``."""

    github_mode: GitHubProviderMode = GitHubProviderMode.LOCAL_SAMPLE
    database_url: str | None = None
    pgdata_dir: Path | None = None
    repo_root: Path | None = None
    seed_initial_workspace: bool = True
    empty_workspace: bool = False
    worker_poll_interval_seconds: float = 0.25
    start_background_worker: bool = True
    allowed_redirect_uris: tuple[str, ...] = (
        "http://127.0.0.1:3100/api/auth/github/callback",
        "http://localhost:3100/api/auth/github/callback",
        "http://127.0.0.1:3100/",
        "http://localhost:3100/",
    )
    github_client_id: str | None = None
    github_client_secret: str | None = field(default=None, repr=False)
    github_public_read_token: str | None = field(default=None, repr=False)
    workspace_name: str = "Career Memory Local PG18 Workspace"
    user_email: str = "seoyeon@career-memory.local"
    user_display_name: str = "Seoyeon Park"
    github_actor_login: str = "seoyeon-dev"
    github_actor_external_id: str = "904210"


@dataclass(frozen=True, slots=True)
class BootstrapResourceSummary:
    """Non-secret allowlisted repository summary returned by ``GET /local-dev/bootstrap``."""

    source_resource_id: str
    source_account_id: str
    provider_resource_id: str
    repository_full_name: str
    canonical_url: str
    visibility: str
    status: str
    default_branch: str
    description: str
    primary_language: str
    estimated_commits: int
    estimated_pull_requests: int
    latest_sync_run_id: str | None = None
    latest_sync_run_status: str | None = None
    latest_processing_status: str | None = None
    latest_import_ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_resource_id": self.source_resource_id,
            "source_account_id": self.source_account_id,
            "provider_resource_id": self.provider_resource_id,
            "repository_full_name": self.repository_full_name,
            "canonical_url": self.canonical_url,
            "visibility": self.visibility,
            "status": self.status,
            "default_branch": self.default_branch,
            "description": self.description,
            "primary_language": self.primary_language,
            "estimated_commits": self.estimated_commits,
            "estimated_pull_requests": self.estimated_pull_requests,
            "latest_sync_run_id": self.latest_sync_run_id,
            "latest_sync_run_status": self.latest_sync_run_status,
            "latest_processing_status": self.latest_processing_status,
            "latest_import_ready": self.latest_import_ready,
        }


@dataclass(frozen=True, slots=True)
class SampleRepositoryDescriptor:
    """Catalog metadata describing an available sample public repository."""

    provider_resource_id: str
    owner: str
    name: str
    full_name: str
    canonical_url: str
    default_branch: str
    primary_language: str
    description: str
    estimated_commits: int
    estimated_pull_requests: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_resource_id": self.provider_resource_id,
            "owner": self.owner,
            "name": self.name,
            "full_name": self.full_name,
            "canonical_url": self.canonical_url,
            "default_branch": self.default_branch,
            "primary_language": self.primary_language,
            "description": self.description,
            "estimated_commits": self.estimated_commits,
            "estimated_pull_requests": self.estimated_pull_requests,
        }


@dataclass(frozen=True, slots=True)
class LocalDevBootstrapPayload:
    """Response payload for local-dev-only ``GET /local-dev/bootstrap``."""

    mode: str
    workspace_id: str
    workspace_name: str
    user_id: str
    user_display_name: str
    user_email: str
    github_login: str
    access_token: str = field(repr=False)
    connected_account_id: str | None
    resources: tuple[BootstrapResourceSummary, ...]
    default_source_resource_id: str | None
    default_confirmed_event_id: str | None
    sample_repositories: tuple[SampleRepositoryDescriptor, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "user_id": self.user_id,
            "user_display_name": self.user_display_name,
            "user_email": self.user_email,
            "github_login": self.github_login,
            "access_token": self.access_token,
            "connected_account_id": self.connected_account_id,
            "resources": [r.to_dict() for r in self.resources],
            "default_source_resource_id": self.default_source_resource_id,
            "default_confirmed_event_id": self.default_confirmed_event_id,
            "sample_repositories": [s.to_dict() for s in self.sample_repositories],
        }


__all__ = [
    "BootstrapResourceSummary",
    "FullstackRuntimeConfig",
    "GitHubProviderMode",
    "LocalDevBootstrapPayload",
    "SampleRepositoryDescriptor",
]
