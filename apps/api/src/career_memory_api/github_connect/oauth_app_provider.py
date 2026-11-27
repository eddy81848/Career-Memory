"""GitHub OAuth App (``read:user``-only) implementation of ``GitHubCredentialProviderPort``."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from career_memory_api.auth.oauth import OAuthTokenExchangeError
from career_memory_api.auth.redaction import redact_text
from career_memory_api.github_connect.models import (
    REQUIRED_GITHUB_OAUTH_SCOPE,
    GitHubAccountIdentity,
    GitHubCredentialMode,
    GitHubExchangedCredential,
    GitHubInspectionOutcome,
    GitHubRepositoryInspection,
    GitHubRepositoryMetadata,
    GitHubRevokeResult,
    parse_oauth_scopes,
    validate_minimum_github_scopes,
)
from career_memory_api.github_connect.ports import (
    GitHubHttpRequest,
    GitHubHttpResponse,
    GitHubOAuthTransportPort,
)

_GITHUB_API_VERSION = "2022-11-28"
_GITHUB_JSON_ACCEPT = "application/vnd.github+json"


@dataclass(frozen=True, slots=True)
class GitHubOAuthAppConfig:
    """Configuration for GitHub OAuth App credential adapter (``client_secret`` redacted)."""

    client_id: str
    client_secret: str = field(repr=False)
    allowed_redirect_uris: tuple[str, ...] = ()
    authorize_url: str = "https://github.com/login/oauth/authorize"
    token_url: str = "https://github.com/login/oauth/access_token"
    api_base_url: str = "https://api.github.com"

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_id.strip():
            raise ValueError("GitHubOAuthAppConfig.client_id must be non-empty.")
        if not self.client_secret or not self.client_secret.strip():
            raise ValueError("GitHubOAuthAppConfig.client_secret must be non-empty.")

    def __repr__(self) -> str:
        return (
            f"GitHubOAuthAppConfig(client_id={self.client_id!r}, "
            f"client_secret=<redacted>, authorize_url={self.authorize_url!r}, "
            f"api_base_url={self.api_base_url!r})"
        )

    def __str__(self) -> str:
        return repr(self)


def _is_rate_limit_response(response: GitHubHttpResponse) -> tuple[bool, int | None]:
    retry_after_raw = response.header_ci("Retry-After")
    retry_after: int | None = None
    if retry_after_raw is not None:
        try:
            retry_after = max(0, int(retry_after_raw.strip()))
        except ValueError:
            retry_after = None

    if response.status_code == 429:
        return True, retry_after

    remaining = response.header_ci("X-RateLimit-Remaining")
    if remaining is not None and remaining.strip() == "0":
        return True, retry_after

    if retry_after is not None and response.status_code == 403:
        return True, retry_after

    if response.status_code == 403 and isinstance(response.json_body, dict):
        msg = str(response.json_body.get("message", "")).lower()
        if "rate limit" in msg or "secondary rate limit" in msg:
            return True, retry_after

    return False, None


class GitHubOAuthAppCredentialProvider:
    """MVP ``GitHubCredentialProviderPort`` backed by a GitHub OAuth App (`read:user` only).

    All HTTP operations flow through the injected ``GitHubOAuthTransportPort`` so no
    real network call is ever performed during unit or integration tests.
    """

    def __init__(
        self,
        *,
        config: GitHubOAuthAppConfig,
        transport: GitHubOAuthTransportPort,
    ) -> None:
        self._config = config
        self._transport = transport

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
        """Build the authorize URL strictly requesting ``scope=read:user`` only."""
        query = urlencode(
            {
                "client_id": self._config.client_id,
                "redirect_uri": redirect_uri,
                "scope": REQUIRED_GITHUB_OAUTH_SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            }
        )
        return f"{self._config.authorize_url}?{query}"

    def fetch_authenticated_account(
        self,
        *,
        access_token: str,
    ) -> GitHubAccountIdentity:
        """Resolve stable numeric GitHub user ID and inspect ``X-OAuth-Scopes``."""
        response = self._transport.send(
            GitHubHttpRequest(
                method="GET",
                url=f"{self._config.api_base_url.rstrip('/')}/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": _GITHUB_JSON_ACCEPT,
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                },
            )
        )
        if response.status_code != 200 or not isinstance(response.json_body, dict):
            raise OAuthTokenExchangeError(
                redact_text(
                    f"GitHub authenticated user lookup failed with status {response.status_code}."
                )
            )
        raw_id = response.json_body.get("id")
        raw_login = response.json_body.get("login")
        if raw_id is None or not str(raw_id).strip():
            raise OAuthTokenExchangeError("GitHub user response missing stable account id.")

        raw_header_scopes = response.header_ci("X-OAuth-Scopes")
        header_scopes = (
            parse_oauth_scopes(raw_header_scopes)
            if raw_header_scopes is not None
            else None
        )
        return GitHubAccountIdentity(
            external_account_id=str(raw_id).strip(),
            login=str(raw_login or "").strip() or f"github-user-{raw_id}",
            header_scopes=header_scopes,
        )

    def exchange_authorization_code(
        self,
        *,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> GitHubExchangedCredential:
        """Exchange authorization code + PKCE verifier via offline-injectable transport."""
        response = self._transport.send(
            GitHubHttpRequest(
                method="POST",
                url=self._config.token_url,
                headers={"Accept": "application/json"},
                form_body={
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": pkce_code_verifier,
                },
            )
        )
        if response.status_code != 200 or not isinstance(response.json_body, dict):
            raise OAuthTokenExchangeError(
                redact_text(
                    f"GitHub token exchange failed with HTTP status {response.status_code}."
                )
            )

        payload = response.json_body
        if payload.get("error"):
            err_code = redact_text(str(payload.get("error")))
            raise OAuthTokenExchangeError(
                f"GitHub OAuth token exchange rejected by provider ({err_code})."
            )

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise OAuthTokenExchangeError("GitHub token exchange returned an empty access token.")

        token_type = str(payload.get("token_type") or "bearer").strip().lower()
        body_scopes = parse_oauth_scopes(payload.get("scope"))
        body_validation = validate_minimum_github_scopes(body_scopes)

        # If the token exchange response already violates scope policy, skip /user
        # and return immediately so the service revokes the grant with zero extra API calls.
        if not body_validation.is_valid:
            return GitHubExchangedCredential(
                external_account_id="unverified-scope-rejected",
                login="unverified",
                access_token=access_token,
                scopes=body_scopes,
                token_type=token_type,
            )

        identity = self.fetch_authenticated_account(access_token=access_token)
        merged_scopes = body_scopes
        if identity.header_scopes is not None:
            merged_scopes = parse_oauth_scopes(list(body_scopes) + list(identity.header_scopes))

        return GitHubExchangedCredential(
            external_account_id=identity.external_account_id,
            login=identity.login,
            access_token=access_token,
            scopes=merged_scopes,
            token_type=token_type,
        )

    def inspect_repository(
        self,
        *,
        access_token: str,
        owner: str,
        repo: str,
    ) -> GitHubRepositoryInspection:
        """Inspect repository visibility and credential health without fetching raw content."""
        safe_owner = quote(owner, safe="")
        safe_repo = quote(repo, safe="")
        response = self._transport.send(
            GitHubHttpRequest(
                method="GET",
                url=f"{self._config.api_base_url.rstrip('/')}/repos/{safe_owner}/{safe_repo}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": _GITHUB_JSON_ACCEPT,
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                },
            )
        )

        raw_header_scopes = response.header_ci("X-OAuth-Scopes")
        observed_scopes = (
            parse_oauth_scopes(raw_header_scopes)
            if raw_header_scopes is not None
            else None
        )

        if response.status_code == 401:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.TOKEN_REVOKED,
                http_status=401,
                observed_scopes=observed_scopes,
                reason="Provider rejected credential with 401 Unauthorized.",
            )

        is_rate_limited, retry_after = _is_rate_limit_response(response)
        if is_rate_limited:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.RATE_LIMITED,
                http_status=response.status_code,
                observed_scopes=observed_scopes,
                retry_after_seconds=retry_after,
                reason="GitHub API rate limit exceeded.",
            )

        if response.status_code >= 500:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.PROVIDER_UNAVAILABLE,
                http_status=response.status_code,
                observed_scopes=observed_scopes,
                reason=f"GitHub API returned transient server error ({response.status_code}).",
            )

        if observed_scopes is not None:
            scope_check = validate_minimum_github_scopes(observed_scopes)
            if scope_check.disallowed_scopes:
                return GitHubRepositoryInspection(
                    outcome=GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED,
                    http_status=response.status_code,
                    observed_scopes=observed_scopes,
                    reason="Observed GitHub token scopes exceed OD-011 read:user allowlist.",
                )
            if scope_check.missing_required_scopes:
                return GitHubRepositoryInspection(
                    outcome=GitHubInspectionOutcome.SCOPE_LOST,
                    http_status=response.status_code,
                    observed_scopes=observed_scopes,
                    reason="GitHub token lost required read:user scope.",
                )

        if response.status_code in (403, 404):
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY,
                http_status=response.status_code,
                observed_scopes=observed_scopes,
                reason=(
                    "Provider returned ambiguous 404/403 response; repository may have become "
                    "private, lost access, or moved (never assumed deleted)."
                ),
            )

        if response.status_code != 200 or not isinstance(response.json_body, dict):
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.PROVIDER_UNAVAILABLE,
                http_status=response.status_code,
                observed_scopes=observed_scopes,
                reason=f"Unexpected GitHub repository response status ({response.status_code}).",
            )

        payload = response.json_body
        raw_id = payload.get("id")
        full_name = str(payload.get("full_name") or f"{owner}/{repo}").strip()
        canonical_url = str(
            payload.get("html_url") or f"https://github.com/{full_name}"
        ).strip()
        raw_private = payload.get("private")
        is_private = True if raw_private is None else bool(raw_private)
        raw_visibility = str(
            payload.get("visibility") or ("private" if is_private else "public")
        ).strip().lower()
        default_branch_raw = payload.get("default_branch")
        default_branch = str(default_branch_raw).strip() if default_branch_raw else None

        metadata = GitHubRepositoryMetadata(
            provider_resource_id=str(raw_id).strip() if raw_id is not None else "",
            full_name=full_name,
            canonical_url=canonical_url,
            private=is_private,
            visibility=raw_visibility,
            default_branch=default_branch,
        )

        if not metadata.is_strictly_public:
            normalized_vis = "INTERNAL" if raw_visibility == "internal" else "PRIVATE"
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
                http_status=200,
                observed_visibility=normalized_vis,
                repository=metadata,
                observed_scopes=observed_scopes,
                reason=f"Repository visibility is {normalized_vis} (not PUBLIC).",
            )

        if not metadata.provider_resource_id:
            return GitHubRepositoryInspection(
                outcome=GitHubInspectionOutcome.PROVIDER_UNAVAILABLE,
                http_status=200,
                observed_scopes=observed_scopes,
                reason="GitHub repository metadata missing stable numeric id.",
            )

        return GitHubRepositoryInspection(
            outcome=GitHubInspectionOutcome.PUBLIC_CONFIRMED,
            http_status=200,
            observed_visibility="PUBLIC",
            repository=metadata,
            observed_scopes=observed_scopes,
            reason="Repository confirmed strictly PUBLIC.",
        )

    def revoke_grant(
        self,
        *,
        access_token: str,
    ) -> GitHubRevokeResult:
        """Revoke the OAuth App grant at GitHub via DELETE /applications/{client_id}/grant."""
        basic_raw = f"{self._config.client_id}:{self._config.client_secret}".encode("utf-8")
        basic_b64 = base64.b64encode(basic_raw).decode("ascii")
        safe_client_id = quote(self._config.client_id, safe="")

        response = self._transport.send(
            GitHubHttpRequest(
                method="DELETE",
                url=f"{self._config.api_base_url.rstrip('/')}/applications/{safe_client_id}/grant",
                headers={
                    "Authorization": f"Basic {basic_b64}",
                    "Accept": _GITHUB_JSON_ACCEPT,
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                },
                json_body={"access_token": access_token},
            )
        )
        if response.status_code == 204:
            return GitHubRevokeResult(
                revoked=True,
                already_revoked=False,
                http_status=204,
            )
        if response.status_code == 404:
            return GitHubRevokeResult(
                revoked=True,
                already_revoked=True,
                http_status=404,
            )
        return GitHubRevokeResult(
            revoked=False,
            already_revoked=False,
            http_status=response.status_code,
            error_code=f"GITHUB_REVOKE_HTTP_{response.status_code}",
        )
