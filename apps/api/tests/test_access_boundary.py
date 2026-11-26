"""Tests for the minimal user identity and Workspace membership boundary."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import career_memory_api
from career_memory_api import app as app_module
from career_memory_api import authz as authz_module
from career_memory_api import config as config_module
from career_memory_api import dependencies as dependencies_module
from career_memory_api.app import create_app
from career_memory_api.authz import (
    AccessDenialReason,
    AccessVerificationUnavailableError,
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    UnauthenticatedError,
    UnconfiguredWorkspaceAccessProvider,
    WorkspaceAccessContext,
    WorkspaceAccessGuard,
    WorkspaceMembership,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.dependencies import (
    get_auth_credentials,
    get_workspace_access_guard,
    require_workspace_access,
)
from career_memory_api.main import app as main_app
from tests.synthetic_access import SyntheticWorkspaceAccessProvider


class _TargetWorkspacePayload(BaseModel):
    workspace_id: str
    user_id: str | None = None


def _build_synthetic_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="usr-alice",
        auth_subject=AuthSubject(
            issuer="https://synthetic-auth.local",
            subject="sub-alice-001",
        ),
    )


def _build_synthetic_provider(
    *,
    identity_error: Exception | None = None,
    membership_error: Exception | None = None,
) -> SyntheticWorkspaceAccessProvider:
    alice = _build_synthetic_user()
    return SyntheticWorkspaceAccessProvider(
        users_by_token={"synthetic-alice-token": alice},
        memberships_by_pair={
            ("usr-alice", "ws-alpha"): WorkspaceMembership(
                workspace_id="ws-alpha",
                user_id="usr-alice",
                role="synthetic-member",
            )
        },
        identity_error=identity_error,
        membership_error=membership_error,
    )


def _mount_test_protected_routes(
    app: FastAPI, recorded_contexts: list[WorkspaceAccessContext]
) -> None:
    """Mount test-only routes to exercise the FastAPI boundary without adding product endpoints."""

    @app.get("/_test/workspaces/{workspace_id}/probe")
    async def protected_path_probe(
        access: Annotated[WorkspaceAccessContext, Depends(require_workspace_access)],
    ) -> dict[str, str | None]:
        recorded_contexts.append(access)
        return {
            "user_id": access.user_id,
            "issuer": access.user.auth_subject.issuer,
            "subject": access.user.auth_subject.subject,
            "workspace_id": access.workspace_id,
            "role": access.role,
        }

    @app.post("/_test/workspaces/body-target-probe")
    async def protected_body_target_probe(
        payload: _TargetWorkspacePayload,
        guard: Annotated[WorkspaceAccessGuard, Depends(get_workspace_access_guard)],
        credentials: Annotated[AuthCredentials, Depends(get_auth_credentials)],
    ) -> dict[str, str]:
        # payload.workspace_id is only the target to verify, never proof of access.
        # payload.user_id is untrusted and completely ignored.
        access = guard.require_workspace_access(
            credentials=credentials,
            target_workspace_id=payload.workspace_id,
        )
        recorded_contexts.append(access)
        return {
            "user_id": access.user_id,
            "workspace_id": access.workspace_id,
        }


def test_allowed_synthetic_membership_passes_verified_context_and_ignores_spoofed_fields() -> None:
    recorded: list[WorkspaceAccessContext] = []
    app = create_app(access_provider=_build_synthetic_provider())
    _mount_test_protected_routes(app, recorded)

    with TestClient(app) as client:
        path_response = client.get(
            "/_test/workspaces/ws-alpha/probe",
            headers={
                "Authorization": "Bearer synthetic-alice-token",
                "X-User-Id": "usr-spoofed",
                "X-Workspace-Id": "ws-spoofed",
            },
        )
        body_response = client.post(
            "/_test/workspaces/body-target-probe",
            headers={
                "Authorization": "Bearer synthetic-alice-token",
                "X-User-Id": "usr-spoofed",
                "X-Workspace-Id": "ws-spoofed",
            },
            json={
                "workspace_id": "ws-alpha",
                "user_id": "usr-spoofed-body",
            },
        )

    assert path_response.status_code == 200
    assert path_response.json() == {
        "user_id": "usr-alice",
        "issuer": "https://synthetic-auth.local",
        "subject": "sub-alice-001",
        "workspace_id": "ws-alpha",
        "role": "synthetic-member",
    }

    assert body_response.status_code == 200
    assert body_response.json() == {
        "user_id": "usr-alice",
        "workspace_id": "ws-alpha",
    }

    assert len(recorded) == 2
    assert all(ctx.user_id == "usr-alice" for ctx in recorded)
    assert all(ctx.workspace_id == "ws-alpha" for ctx in recorded)


def test_other_workspace_is_denied_and_blocks_protected_operation() -> None:
    provider = _build_synthetic_provider()
    guard = WorkspaceAccessGuard(provider)

    with pytest.raises(WorkspaceMembershipDeniedError) as exc_info:
        guard.require_workspace_access(
            AuthCredentials(bearer_token="synthetic-alice-token"),
            target_workspace_id="ws-beta",
        )
    assert exc_info.value.reason is AccessDenialReason.MEMBERSHIP_NOT_FOUND

    recorded: list[WorkspaceAccessContext] = []
    app = create_app(access_provider=provider)
    _mount_test_protected_routes(app, recorded)

    with TestClient(app) as client:
        path_response = client.get(
            "/_test/workspaces/ws-beta/probe",
            headers={
                "Authorization": "Bearer synthetic-alice-token",
                "X-Workspace-Id": "ws-alpha",
            },
        )
        body_response = client.post(
            "/_test/workspaces/body-target-probe",
            headers={"Authorization": "Bearer synthetic-alice-token"},
            json={"workspace_id": "ws-beta", "user_id": "usr-alice"},
        )

    assert path_response.status_code >= 400
    assert path_response.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.MEMBERSHIP_NOT_FOUND.value,
    }
    assert body_response.status_code >= 400
    assert body_response.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.MEMBERSHIP_NOT_FOUND.value,
    }
    assert recorded == []


def test_unauthenticated_requests_are_denied_even_with_spoofed_headers_and_body() -> None:
    provider = _build_synthetic_provider()
    guard = WorkspaceAccessGuard(provider)

    for bad_credentials in (
        None,
        AuthCredentials(bearer_token=None),
        AuthCredentials(bearer_token="   "),
        AuthCredentials(bearer_token="unknown-token"),
    ):
        with pytest.raises(UnauthenticatedError) as exc_info:
            guard.require_workspace_access(bad_credentials, target_workspace_id="ws-alpha")
        assert exc_info.value.reason is AccessDenialReason.UNAUTHENTICATED

    recorded: list[WorkspaceAccessContext] = []
    app = create_app(access_provider=provider)
    _mount_test_protected_routes(app, recorded)

    with TestClient(app) as client:
        no_auth_response = client.get(
            "/_test/workspaces/ws-alpha/probe",
            headers={
                "X-User-Id": "usr-alice",
                "X-Workspace-Id": "ws-alpha",
            },
        )
        bad_scheme_response = client.post(
            "/_test/workspaces/body-target-probe",
            headers={
                "Authorization": "Basic synthetic-alice-token",
                "X-User-Id": "usr-alice",
                "X-Workspace-Id": "ws-alpha",
            },
            json={"workspace_id": "ws-alpha", "user_id": "usr-alice"},
        )

    assert no_auth_response.status_code >= 400
    assert no_auth_response.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.UNAUTHENTICATED.value,
    }
    assert bad_scheme_response.status_code >= 400
    assert bad_scheme_response.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.UNAUTHENTICATED.value,
    }
    assert recorded == []


@pytest.mark.parametrize(
    ("identity_error", "membership_error"),
    [
        (AccessVerificationUnavailableError("auth backend down"), None),
        (RuntimeError("unexpected jwks timeout"), None),
        (None, AccessVerificationUnavailableError("membership store down")),
        (None, TimeoutError("membership query timed out")),
    ],
)
def test_verification_service_outage_fails_closed_and_is_distinguished(
    identity_error: Exception | None,
    membership_error: Exception | None,
) -> None:
    provider = _build_synthetic_provider(
        identity_error=identity_error,
        membership_error=membership_error,
    )
    guard = WorkspaceAccessGuard(provider)

    with pytest.raises(AccessVerificationUnavailableError) as exc_info:
        guard.require_workspace_access(
            AuthCredentials(bearer_token="synthetic-alice-token"),
            target_workspace_id="ws-alpha",
        )
    assert exc_info.value.reason is AccessDenialReason.VERIFICATION_UNAVAILABLE

    recorded: list[WorkspaceAccessContext] = []
    app = create_app(access_provider=provider)
    _mount_test_protected_routes(app, recorded)

    with TestClient(app) as client:
        response = client.get(
            "/_test/workspaces/ws-alpha/probe",
            headers={"Authorization": "Bearer synthetic-alice-token"},
        )

    assert response.status_code >= 400
    assert response.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.VERIFICATION_UNAVAILABLE.value,
    }
    assert recorded == []


def test_guard_rejects_mismatched_membership_returned_by_faulty_provider() -> None:
    alice = _build_synthetic_user()

    class _MismatchedWorkspaceProvider:
        def verify_identity(
            self, credentials: AuthCredentials
        ) -> AuthenticatedUser | None:
            return alice

        def verify_workspace_membership(
            self, user: AuthenticatedUser, target_workspace_id: str
        ) -> WorkspaceMembership | None:
            return WorkspaceMembership(
                workspace_id="ws-other",
                user_id=user.user_id,
            )

    guard = WorkspaceAccessGuard(_MismatchedWorkspaceProvider())
    with pytest.raises(WorkspaceMembershipDeniedError) as exc_info:
        guard.require_workspace_access(
            AuthCredentials(bearer_token="any-token"),
            target_workspace_id="ws-alpha",
        )
    assert exc_info.value.reason is AccessDenialReason.MEMBERSHIP_NOT_FOUND


def test_synthetic_provider_cannot_be_enabled_in_normal_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_key, env_value in {
        "CAREER_MEMORY_USE_SYNTHETIC_AUTH": "true",
        "CAREER_MEMORY_SYNTHETIC_AUTH": "1",
        "USE_SYNTHETIC_AUTH": "true",
        "ENABLE_SYNTHETIC_AUTH": "1",
        "ENABLE_TEST_AUTH": "true",
        "APP_ENV": "test",
        "ENVIRONMENT": "local",
    }.items():
        monkeypatch.setenv(env_key, env_value)

    app = create_app()
    assert isinstance(
        app.state.workspace_access_guard.provider,
        UnconfiguredWorkspaceAccessProvider,
    )
    assert isinstance(
        main_app.state.workspace_access_guard.provider,
        UnconfiguredWorkspaceAccessProvider,
    )

    recorded: list[WorkspaceAccessContext] = []
    _mount_test_protected_routes(app, recorded)

    with TestClient(app) as client:
        missing_token_resp = client.get(
            "/_test/workspaces/ws-alpha/probe",
            headers={
                "X-Synthetic-Auth": "true",
                "X-User-Id": "usr-alice",
                "X-Workspace-Id": "ws-alpha",
            },
        )
        synthetic_token_resp = client.get(
            "/_test/workspaces/ws-alpha/probe",
            headers={
                "Authorization": "Bearer synthetic-alice-token",
                "X-Synthetic-Auth": "true",
                "X-User-Id": "usr-alice",
                "X-Workspace-Id": "ws-alpha",
            },
        )
        body_resp = client.post(
            "/_test/workspaces/body-target-probe",
            headers={
                "Authorization": "Bearer synthetic-alice-token",
                "X-Synthetic-Auth": "true",
            },
            json={"workspace_id": "ws-alpha", "user_id": "usr-alice"},
        )

    assert missing_token_resp.status_code >= 400
    assert missing_token_resp.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.UNAUTHENTICATED.value,
    }
    assert synthetic_token_resp.status_code >= 400
    assert synthetic_token_resp.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.VERIFICATION_UNAVAILABLE.value,
    }
    assert body_resp.status_code >= 400
    assert body_resp.json() == {
        "error": "access_denied",
        "boundary_reason": AccessDenialReason.VERIFICATION_UNAVAILABLE.value,
    }
    assert recorded == []

    # Production package modules must not expose any synthetic provider symbol.
    for module in (
        career_memory_api,
        app_module,
        authz_module,
        config_module,
        dependencies_module,
    ):
        assert not any("synthetic" in name.lower() for name in dir(module))


def test_auth_credentials_redacts_bearer_token_in_repr_and_str_while_provider_receives_raw_token() -> None:
    raw_token = "synthetic-secret-token-9f8e7d"
    credentials = authz_module.extract_bearer_credentials(f"Bearer {raw_token}")

    assert raw_token not in repr(credentials)
    assert raw_token not in str(credentials)
    assert repr(credentials) == "AuthCredentials(bearer_token=<redacted>)"
    assert str(credentials) == "AuthCredentials(bearer_token=<redacted>)"

    empty_credentials = AuthCredentials(bearer_token=None)
    assert repr(empty_credentials) == "AuthCredentials(bearer_token=None)"
    assert str(empty_credentials) == "AuthCredentials(bearer_token=None)"

    observed_tokens: list[str | None] = []
    alice = _build_synthetic_user()

    class _RecordingSyntheticProvider:
        def verify_identity(
            self, creds: AuthCredentials
        ) -> AuthenticatedUser | None:
            observed_tokens.append(creds.bearer_token)
            if creds.bearer_token == raw_token:
                return alice
            return None

        def verify_workspace_membership(
            self, user: AuthenticatedUser, target_workspace_id: str
        ) -> WorkspaceMembership | None:
            if user.user_id == alice.user_id and target_workspace_id == "ws-alpha":
                return WorkspaceMembership(
                    workspace_id="ws-alpha",
                    user_id=user.user_id,
                    role="synthetic-member",
                )
            return None

    guard = WorkspaceAccessGuard(_RecordingSyntheticProvider())
    context = guard.require_workspace_access(credentials, target_workspace_id="ws-alpha")

    assert observed_tokens == [raw_token]
    assert context.user_id == "usr-alice"
    assert context.workspace_id == "ws-alpha"
