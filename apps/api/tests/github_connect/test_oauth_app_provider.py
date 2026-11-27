"""Unit tests for GitHubOAuthAppCredentialProvider, scope policy, and offline transport."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from career_memory_api.github_connect.models import (
    ALLOWED_GITHUB_OAUTH_SCOPES,
    GitHubCredentialMode,
    GitHubInspectionOutcome,
    GitHubScopeValidationResult,
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


class _StubTransport:
    """Deterministic offline HTTP transport for GitHub OAuth App tests."""

    def __init__(self, responses: list[GitHubHttpResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[GitHubHttpRequest] = []

    def send(self, request: GitHubHttpRequest) -> GitHubHttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"Unexpected transport call: {request.method} {request.url}")
        return self._responses.pop(0)


def _config() -> GitHubOAuthAppConfig:
    return GitHubOAuthAppConfig(
        client_id="Iv1.career_memory_local_client",
        client_secret="ghs_super_secret_oauth_client_secret_987654321",
        allowed_redirect_uris=("https://app.career-memory.local/oauth/github/callback",),
    )


def test_authorization_url_requests_only_read_user_scope_and_never_leaks_secret() -> None:
    transport = _StubTransport([])
    provider = GitHubOAuthAppCredentialProvider(config=_config(), transport=transport)

    assert provider.credential_mode is GitHubCredentialMode.OAUTH_APP

    url = provider.build_authorization_url(
        state="raw-state-token-xyz",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
        code_challenge="challenge-s256-abc",
    )
    parts = urlsplit(url)
    params = parse_qs(parts.query)

    assert parts.scheme == "https"
    assert parts.netloc == "github.com"
    assert parts.path == "/login/oauth/authorize"
    assert params["client_id"] == ["Iv1.career_memory_local_client"]
    assert params["scope"] == ["read:user"]
    assert params["state"] == ["raw-state-token-xyz"]
    assert params["code_challenge"] == ["challenge-s256-abc"]
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in url
    assert "ghs_super_secret_oauth_client_secret_987654321" not in url
    assert "repo" not in params["scope"][0]


def test_scope_validator_allows_only_read_user_and_rejects_repo_and_write_scopes() -> None:
    assert ALLOWED_GITHUB_OAUTH_SCOPES == frozenset({"read:user"})
    assert parse_oauth_scopes("read:user") == ("read:user",)
    assert parse_oauth_scopes(" read:user , public_repo ") == ("public_repo", "read:user")

    ok = validate_minimum_github_scopes(("read:user",))
    assert ok == GitHubScopeValidationResult(
        is_valid=True,
        granted_scopes=("read:user",),
        disallowed_scopes=(),
        missing_required_scopes=(),
    )

    for forbidden in (
        ("read:user", "repo"),
        ("read:user", "public_repo"),
        ("read:user", "write:org"),
        ("read:user", "admin:repo_hook"),
        ("read:user", "delete_repo"),
        ("read:user", "workflow"),
        ("read:user", "gist"),
        ("user",),
    ):
        res = validate_minimum_github_scopes(forbidden)
        assert not res.is_valid
        assert len(res.disallowed_scopes) >= 1

    empty = validate_minimum_github_scopes(())
    assert not empty.is_valid
    assert empty.missing_required_scopes == ("read:user",)


def test_exchange_and_user_fetch_uses_offline_transport_and_merges_header_scopes() -> None:
    transport = _StubTransport(
        [
            GitHubHttpResponse(
                status_code=200,
                json_body={
                    "access_token": "gho_valid_read_user_token_123456",
                    "token_type": "bearer",
                    "scope": "read:user",
                },
            ),
            GitHubHttpResponse(
                status_code=200,
                headers={"X-OAuth-Scopes": "read:user"},
                json_body={"id": 424242, "login": "octo-dev"},
            ),
        ]
    )
    provider = GitHubOAuthAppCredentialProvider(config=_config(), transport=transport)

    cred = provider.exchange_authorization_code(
        code="auth-code-001",
        pkce_code_verifier="verifier-001",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    assert cred.external_account_id == "424242"
    assert cred.login == "octo-dev"
    assert cred.scopes == ("read:user",)
    assert cred.access_token == "gho_valid_read_user_token_123456"
    # Secret MUST NOT appear in repr/str
    assert "gho_valid_read_user_token_123456" not in repr(cred)
    assert "gho_valid_read_user_token_123456" not in str(cred)
    assert "ghs_super_secret" not in repr(_config())
    assert len(transport.requests) == 2


def test_inspect_repository_classifies_public_private_revoked_ambiguous_and_rate_limit() -> None:
    responses = [
        # 1. Public confirmed
        GitHubHttpResponse(
            status_code=200,
            headers={"X-OAuth-Scopes": "read:user"},
            json_body={
                "id": 9001,
                "full_name": "acme/public-lib",
                "html_url": "https://github.com/acme/public-lib",
                "private": False,
                "visibility": "public",
                "default_branch": "main",
            },
        ),
        # 2. Private confirmed (Public -> Private)
        GitHubHttpResponse(
            status_code=200,
            headers={"X-OAuth-Scopes": "read:user"},
            json_body={
                "id": 9001,
                "full_name": "acme/public-lib",
                "html_url": "https://github.com/acme/public-lib",
                "private": True,
                "visibility": "private",
                "default_branch": "main",
            },
        ),
        # 3. 401 Token revoked
        GitHubHttpResponse(
            status_code=401,
            json_body={"message": "Bad credentials"},
        ),
        # 4. 404 Ambiguous (private transition or access loss or deleted)
        GitHubHttpResponse(
            status_code=404,
            json_body={"message": "Not Found"},
        ),
        # 5. 403 Non-rate-limit Ambiguous
        GitHubHttpResponse(
            status_code=403,
            headers={"X-RateLimit-Remaining": "4999"},
            json_body={"message": "Resource protected by organization SAML enforcement"},
        ),
        # 6. 403 Rate limit
        GitHubHttpResponse(
            status_code=403,
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "120"},
            json_body={"message": "API rate limit exceeded"},
        ),
        # 7. 200 with missing read:user scope in header -> SCOPE_LOST
        GitHubHttpResponse(
            status_code=200,
            headers={"X-OAuth-Scopes": ""},
            json_body={
                "id": 9001,
                "full_name": "acme/public-lib",
                "html_url": "https://github.com/acme/public-lib",
                "private": False,
                "visibility": "public",
            },
        ),
    ]
    transport = _StubTransport(responses)
    provider = GitHubOAuthAppCredentialProvider(config=_config(), transport=transport)

    r1 = provider.inspect_repository(access_token="gho_tok_1", owner="acme", repo="public-lib")
    assert r1.outcome is GitHubInspectionOutcome.PUBLIC_CONFIRMED
    assert r1.repository is not None
    assert r1.repository.provider_resource_id == "9001"
    assert r1.repository.canonical_url == "https://github.com/acme/public-lib"

    r2 = provider.inspect_repository(access_token="gho_tok_1", owner="acme", repo="public-lib")
    assert r2.outcome is GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED
    assert r2.observed_visibility == "PRIVATE"

    r3 = provider.inspect_repository(access_token="gho_tok_1", owner="acme", repo="public-lib")
    assert r3.outcome is GitHubInspectionOutcome.TOKEN_REVOKED

    r4 = provider.inspect_repository(access_token="gho_tok_1", owner="acme", repo="public-lib")
    assert r4.outcome is GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY

    r5 = provider.inspect_repository(access_token="gho_tok_1", owner="acme", repo="public-lib")
    assert r5.outcome is GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY

    r6 = provider.inspect_repository(access_token="gho_tok_1", owner="acme", repo="public-lib")
    assert r6.outcome is GitHubInspectionOutcome.RATE_LIMITED
    assert r6.retry_after_seconds == 120

    r7 = provider.inspect_repository(access_token="gho_tok_1", owner="acme", repo="public-lib")
    assert r7.outcome is GitHubInspectionOutcome.SCOPE_LOST


def test_revoke_grant_calls_github_application_grant_delete_with_basic_auth() -> None:
    transport = _StubTransport(
        [
            GitHubHttpResponse(status_code=204),
            GitHubHttpResponse(status_code=404),
        ]
    )
    provider = GitHubOAuthAppCredentialProvider(config=_config(), transport=transport)

    res1 = provider.revoke_grant(access_token="gho_revoke_me_123456")
    assert res1.revoked is True
    assert res1.already_revoked is False
    assert transport.requests[0].method == "DELETE"
    assert (
        transport.requests[0].url
        == "https://api.github.com/applications/Iv1.career_memory_local_client/grant"
    )
    assert transport.requests[0].headers["Authorization"].startswith("Basic ")
    assert transport.requests[0].json_body == {"access_token": "gho_revoke_me_123456"}

    res2 = provider.revoke_grant(access_token="gho_revoke_me_123456")
    assert res2.revoked is True
    assert res2.already_revoked is True
