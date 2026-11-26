"""Test-only synthetic access provider.

Lives strictly under ``tests/`` and must only be injected explicitly by tests.
Normal application code never imports or activates this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from career_memory_api.authz import (
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    WorkspaceMembership,
)


@dataclass(frozen=True, slots=True)
class SyntheticTokenClaims:
    """Synthetic verified token claims separating (issuer, sub) from profile hints."""

    auth_subject: AuthSubject
    email: str | None = None
    display_name: str | None = None


class SyntheticWorkspaceAccessProvider:
    """In-memory synthetic provider for verifying the Workspace access boundary."""

    def __init__(
        self,
        *,
        users_by_token: Mapping[str, AuthenticatedUser] | None = None,
        claims_by_token: Mapping[str, SyntheticTokenClaims] | None = None,
        users_by_subject: Mapping[AuthSubject, AuthenticatedUser] | None = None,
        memberships_by_pair: Mapping[tuple[str, str], WorkspaceMembership]
        | None = None,
        identity_error: Exception | None = None,
        membership_error: Exception | None = None,
    ) -> None:
        self._users_by_token = dict(users_by_token or {})
        self._claims_by_token = dict(claims_by_token or {})
        self._users_by_subject = dict(users_by_subject or {})
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
        if credentials.bearer_token in self._users_by_token:
            return self._users_by_token[credentials.bearer_token]
        claims = self._claims_by_token.get(credentials.bearer_token)
        if claims is None:
            return None
        # Resolve strictly by verified (issuer, subject); never auto-link by
        # email/display_name and never auto-provision unregistered users.
        return self._users_by_subject.get(claims.auth_subject)

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        if self._membership_error is not None:
            raise self._membership_error
        return self._memberships_by_pair.get((user.user_id, target_workspace_id))

    def revoke_membership(self, user_id: str, workspace_id: str) -> None:
        """Simulate membership revocation for boundary tests."""
        self._memberships_by_pair.pop((user_id, workspace_id), None)

    def registered_subjects(self) -> frozenset[AuthSubject]:
        """Return currently registered (issuer, subject) keys for assertion."""
        return frozenset(self._users_by_subject.keys())
