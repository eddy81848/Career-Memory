"""Ports for GitHub credential provisioning (OD-011) and injectable offline HTTP transport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from career_memory_api.github_connect.models import (
    GitHubCredentialMode,
    GitHubExchangedCredential,
    GitHubRepositoryInspection,
    GitHubRevokeResult,
)


@dataclass(frozen=True, slots=True)
class GitHubHttpRequest:
    """Transport-agnostic HTTP request sent to GitHub OAuth / REST endpoints."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, Any] | None = None
    form_body: Mapping[str, str] | None = None

    def __repr__(self) -> str:
        return f"GitHubHttpRequest(method={self.method!r}, url={self.url!r}, body=<redacted>)"


@dataclass(frozen=True, slots=True)
class GitHubHttpResponse:
    """Transport-agnostic HTTP response returned by ``GitHubOAuthTransportPort``."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, Any] | None = None
    text_body: str = ""

    def header_ci(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


class GitHubOAuthTransportPort(Protocol):
    """Injectable HTTP transport port so tests run 100% offline and deterministically."""

    def send(self, request: GitHubHttpRequest) -> GitHubHttpResponse:
        """Execute a single HTTP request and return a normalized response."""
        ...


class GitHubCredentialProviderPort(Protocol):
    """OD-011 abstraction boundary isolating OAuth App (MVP) from GitHub App (Post-MVP).

    Domain and service layers depend strictly on this port so migrating from a
    minimum-scope GitHub OAuth App (``read:user``) to a GitHub App installation
    credential model requires swapping only the adapter implementation.
    """

    @property
    def credential_mode(self) -> GitHubCredentialMode:
        """Identify the underlying GitHub credential model (``OAUTH_APP`` in MVP)."""
        ...

    def build_authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        """Build the GitHub authorization redirect URL with minimum required scopes only."""
        ...

    def exchange_authorization_code(
        self,
        *,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> GitHubExchangedCredential:
        """Exchange a one-time OAuth authorization code + PKCE verifier for a credential."""
        ...

    def inspect_repository(
        self,
        *,
        access_token: str,
        owner: str,
        repo: str,
    ) -> GitHubRepositoryInspection:
        """Inspect repository visibility and token/scope validity without writing raw content."""
        ...

    def revoke_grant(
        self,
        *,
        access_token: str,
    ) -> GitHubRevokeResult:
        """Revoke the OAuth grant/token at GitHub on disconnect, deletion, or policy rejection."""
        ...
