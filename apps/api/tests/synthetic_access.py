"""Test-only synthetic access provider.

Lives strictly under ``tests/`` and must only be injected explicitly by tests.
Normal application code never imports or activates this module.
"""

from __future__ import annotations

from collections.abc import Mapping

from career_memory_api.authz import (
    AuthCredentials,
    AuthenticatedUser,
    WorkspaceMembership,
)


class SyntheticWorkspaceAccessProvider:
    """In-memory synthetic provider for verifying the Workspace access boundary."""

    def __init__(
        self,
        *,
        users_by_token: Mapping[str, AuthenticatedUser] | None = None,
        memberships_by_pair: Mapping[tuple[str, str], WorkspaceMembership]
        | None = None,
        identity_error: Exception | None = None,
        membership_error: Exception | None = None,
    ) -> None:
        self._users_by_token = dict(users_by_token or {})
        self._memberships_by_pair = dict(memberships_by_pair or {})
        self._identity_error = identity_error
        self._membership_error = membership_error

    def verify_identity(
        self, credentials: AuthCredentials
    ) -> AuthenticatedUser | None:
        if self._identity_error is not None:
            raise self._identity_error
        if credentials.bearer_token is None:
            return None
        return self._users_by_token.get(credentials.bearer_token)

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        if self._membership_error is not None:
            raise self._membership_error
        return self._memberships_by_pair.get((user.user_id, target_workspace_id))
