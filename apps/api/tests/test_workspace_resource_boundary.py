"""Tests for the Workspace-scoped storage resource lookup boundary (FOUNDATION-03).

All tests use synthetic data and test-only providers/stores; no real DB or OAuth
is connected.
"""

from __future__ import annotations

import inspect

import pytest

from career_memory_api.app import create_app
from career_memory_api.authz import (
    AccessDenialReason,
    AccessVerificationUnavailableError,
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    UnauthenticatedError,
    WorkspaceAccessContext,
    WorkspaceAccessGuard,
    WorkspaceMembership,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.workspace_resources import (
    WorkspaceResource,
    WorkspaceResourceNotFoundError,
    WorkspaceResourceReader,
    WorkspaceResourceStore,
)
from tests.synthetic_access import (
    SyntheticTokenClaims,
    SyntheticWorkspaceAccessProvider,
)


class _SyntheticWorkspaceResourceStore:
    """Test-only in-memory store recording every lookup call."""

    def __init__(self, resources: list[WorkspaceResource]) -> None:
        self._by_scoped_key: dict[tuple[str, str], WorkspaceResource] = {
            (resource.workspace_id, resource.resource_id): resource
            for resource in resources
        }
        self.calls: list[tuple[WorkspaceAccessContext, str]] = []

    def get_by_workspace_and_id(
        self,
        access: WorkspaceAccessContext,
        resource_id: str,
    ) -> WorkspaceResource | None:
        self.calls.append((access, resource_id))
        return self._by_scoped_key.get((access.workspace_id, resource_id))


def _build_fixture_provider_and_store(
    *,
    identity_error: Exception | None = None,
    membership_error: Exception | None = None,
) -> tuple[SyntheticWorkspaceAccessProvider, _SyntheticWorkspaceResourceStore]:
    alice_subject = AuthSubject(
        issuer="https://synthetic-supabase.local/auth/v1",
        subject="sub-alice-001",
    )
    alice = AuthenticatedUser(user_id="usr-alice", auth_subject=alice_subject)

    impostor_same_email_subject = AuthSubject(
        issuer="https://synthetic-supabase.local/auth/v1",
        subject="sub-impostor-999",
    )
    other_issuer_same_sub = AuthSubject(
        issuer="https://untrusted-issuer.local/auth/v1",
        subject="sub-alice-001",
    )

    provider = SyntheticWorkspaceAccessProvider(
        claims_by_token={
            "token-alice": SyntheticTokenClaims(
                auth_subject=alice_subject,
                email="alice@example.com",
                display_name="Alice Kim",
            ),
            "token-impostor-same-email": SyntheticTokenClaims(
                auth_subject=impostor_same_email_subject,
                email="alice@example.com",
                display_name="Alice Kim",
            ),
            "token-other-issuer-same-sub": SyntheticTokenClaims(
                auth_subject=other_issuer_same_sub,
                email="alice@example.com",
                display_name="Alice Kim",
            ),
        },
        users_by_subject={alice_subject: alice},
        memberships_by_pair={
            ("usr-alice", "ws-alpha"): WorkspaceMembership(
                workspace_id="ws-alpha",
                user_id="usr-alice",
            )
        },
        identity_error=identity_error,
        membership_error=membership_error,
    )
    store = _SyntheticWorkspaceResourceStore(
        [
            WorkspaceResource(
                resource_id="res-alpha-1",
                workspace_id="ws-alpha",
                title="Alpha Career Note",
            ),
            WorkspaceResource(
                resource_id="res-beta-1",
                workspace_id="ws-beta",
                title="Beta Secret Note",
            ),
        ]
    )
    return provider, store


def test_storage_lookup_interface_has_single_workspace_scoped_method_only() -> None:
    protocol_methods = [
        name
        for name, value in WorkspaceResourceStore.__dict__.items()
        if not name.startswith("_") and callable(value)
    ]
    assert protocol_methods == ["get_by_workspace_and_id"]

    signature = inspect.signature(WorkspaceResourceStore.get_by_workspace_and_id)
    assert list(signature.parameters.keys()) == ["self", "access", "resource_id"]

    reader_methods = [
        name
        for name, value in WorkspaceResourceReader.__dict__.items()
        if not name.startswith("_") and callable(value)
    ]
    assert reader_methods == ["read_resource"]


def test_valid_membership_reads_only_own_workspace_resource() -> None:
    provider, store = _build_fixture_provider_and_store()
    reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

    resource = reader.read_resource(
        credentials=AuthCredentials(bearer_token="token-alice"),
        target_workspace_id="ws-alpha",
        resource_id="res-alpha-1",
    )

    assert resource == WorkspaceResource(
        resource_id="res-alpha-1",
        workspace_id="ws-alpha",
        title="Alpha Career Note",
    )
    assert len(store.calls) == 1
    passed_access, passed_resource_id = store.calls[0]
    assert isinstance(passed_access, WorkspaceAccessContext)
    assert passed_access.user_id == "usr-alice"
    assert passed_access.workspace_id == "ws-alpha"
    assert passed_access.user.auth_subject == AuthSubject(
        issuer="https://synthetic-supabase.local/auth/v1",
        subject="sub-alice-001",
    )
    assert passed_resource_id == "res-alpha-1"


def test_missing_membership_blocks_before_storage_lookup_is_called() -> None:
    provider, store = _build_fixture_provider_and_store()
    reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

    with pytest.raises(WorkspaceMembershipDeniedError) as exc_info:
        reader.read_resource(
            credentials=AuthCredentials(bearer_token="token-alice"),
            target_workspace_id="ws-beta",
            resource_id="res-beta-1",
        )

    assert exc_info.value.reason is AccessDenialReason.MEMBERSHIP_NOT_FOUND
    assert store.calls == []


def test_revoked_membership_blocks_before_storage_lookup_is_called() -> None:
    provider, store = _build_fixture_provider_and_store()
    reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

    first = reader.read_resource(
        credentials=AuthCredentials(bearer_token="token-alice"),
        target_workspace_id="ws-alpha",
        resource_id="res-alpha-1",
    )
    assert first.resource_id == "res-alpha-1"
    assert len(store.calls) == 1

    provider.revoke_membership("usr-alice", "ws-alpha")

    with pytest.raises(WorkspaceMembershipDeniedError) as exc_info:
        reader.read_resource(
            credentials=AuthCredentials(bearer_token="token-alice"),
            target_workspace_id="ws-alpha",
            resource_id="res-alpha-1",
        )

    assert exc_info.value.reason is AccessDenialReason.MEMBERSHIP_NOT_FOUND
    assert len(store.calls) == 1


@pytest.mark.parametrize(
    ("credentials", "identity_error", "membership_error", "expected_error", "expected_reason"),
    [
        (
            None,
            None,
            None,
            UnauthenticatedError,
            AccessDenialReason.UNAUTHENTICATED,
        ),
        (
            AuthCredentials(bearer_token="unknown-token"),
            None,
            None,
            UnauthenticatedError,
            AccessDenialReason.UNAUTHENTICATED,
        ),
        (
            AuthCredentials(bearer_token="token-alice"),
            AccessVerificationUnavailableError("identity verifier down"),
            None,
            AccessVerificationUnavailableError,
            AccessDenialReason.VERIFICATION_UNAVAILABLE,
        ),
        (
            AuthCredentials(bearer_token="token-alice"),
            RuntimeError("unexpected auth failure"),
            None,
            AccessVerificationUnavailableError,
            AccessDenialReason.VERIFICATION_UNAVAILABLE,
        ),
        (
            AuthCredentials(bearer_token="token-alice"),
            None,
            AccessVerificationUnavailableError("membership store down"),
            AccessVerificationUnavailableError,
            AccessDenialReason.VERIFICATION_UNAVAILABLE,
        ),
        (
            AuthCredentials(bearer_token="token-alice"),
            None,
            TimeoutError("membership lookup timed out"),
            AccessVerificationUnavailableError,
            AccessDenialReason.VERIFICATION_UNAVAILABLE,
        ),
    ],
)
def test_unauthenticated_or_unavailable_verification_never_calls_storage_lookup(
    credentials: AuthCredentials | None,
    identity_error: Exception | None,
    membership_error: Exception | None,
    expected_error: type[Exception],
    expected_reason: AccessDenialReason,
) -> None:
    provider, store = _build_fixture_provider_and_store(
        identity_error=identity_error,
        membership_error=membership_error,
    )
    reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

    with pytest.raises(expected_error) as exc_info:
        reader.read_resource(
            credentials=credentials,
            target_workspace_id="ws-alpha",
            resource_id="res-alpha-1",
        )

    assert exc_info.value.reason is expected_reason
    assert store.calls == []


def test_other_workspace_resource_id_and_nonexistent_id_expose_identical_error() -> None:
    provider, store = _build_fixture_provider_and_store()
    reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

    with pytest.raises(WorkspaceResourceNotFoundError) as cross_workspace_exc:
        reader.read_resource(
            credentials=AuthCredentials(bearer_token="token-alice"),
            target_workspace_id="ws-alpha",
            resource_id="res-beta-1",
        )

    with pytest.raises(WorkspaceResourceNotFoundError) as nonexistent_exc:
        reader.read_resource(
            credentials=AuthCredentials(bearer_token="token-alice"),
            target_workspace_id="ws-alpha",
            resource_id="res-does-not-exist",
        )

    assert type(cross_workspace_exc.value) is type(nonexistent_exc.value)
    assert str(cross_workspace_exc.value) == str(nonexistent_exc.value)
    assert repr(cross_workspace_exc.value) == repr(nonexistent_exc.value)
    assert cross_workspace_exc.value.args == nonexistent_exc.value.args
    assert cross_workspace_exc.value.__dict__ == nonexistent_exc.value.__dict__
    assert "res-beta-1" not in str(cross_workspace_exc.value)
    assert "ws-beta" not in str(cross_workspace_exc.value)


def test_reader_collapses_buggy_cross_workspace_store_return_into_same_not_found_error() -> None:
    provider, _ = _build_fixture_provider_and_store()

    class _BuggyCrossWorkspaceStore:
        def get_by_workspace_and_id(
            self,
            access: WorkspaceAccessContext,
            resource_id: str,
        ) -> WorkspaceResource | None:
            if resource_id == "res-beta-1":
                return WorkspaceResource(
                    resource_id="res-beta-1",
                    workspace_id="ws-beta",
                    title="Leaked From Beta",
                )
            return None

    reader = WorkspaceResourceReader(
        WorkspaceAccessGuard(provider),
        _BuggyCrossWorkspaceStore(),
    )

    with pytest.raises(WorkspaceResourceNotFoundError) as leaked_exc:
        reader.read_resource(
            credentials=AuthCredentials(bearer_token="token-alice"),
            target_workspace_id="ws-alpha",
            resource_id="res-beta-1",
        )

    with pytest.raises(WorkspaceResourceNotFoundError) as missing_exc:
        reader.read_resource(
            credentials=AuthCredentials(bearer_token="token-alice"),
            target_workspace_id="ws-alpha",
            resource_id="res-missing",
        )

    assert str(leaked_exc.value) == str(missing_exc.value)
    assert leaked_exc.value.args == missing_exc.value.args


@pytest.mark.parametrize(
    "unregistered_token",
    [
        "token-impostor-same-email",
        "token-other-issuer-same-sub",
    ],
)
def test_unregistered_subject_with_matching_email_or_name_is_not_auto_linked_or_created(
    unregistered_token: str,
) -> None:
    provider, store = _build_fixture_provider_and_store()
    before_subjects = provider.registered_subjects()
    reader = WorkspaceResourceReader(WorkspaceAccessGuard(provider), store)

    with pytest.raises(UnauthenticatedError) as exc_info:
        reader.read_resource(
            credentials=AuthCredentials(bearer_token=unregistered_token),
            target_workspace_id="ws-alpha",
            resource_id="res-alpha-1",
        )

    assert exc_info.value.reason is AccessDenialReason.UNAUTHENTICATED
    assert provider.registered_subjects() == before_subjects
    assert store.calls == []


def test_no_new_public_api_routes_added_to_app() -> None:
    app = create_app()
    public_paths = {
        route.path
        for route in app.routes
        if getattr(route, "include_in_schema", True)
    }
    assert public_paths == {"/healthz"}
