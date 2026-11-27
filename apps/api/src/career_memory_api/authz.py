"""Minimal server-verified user identity and Workspace membership boundary.

Design boundaries for FOUNDATION-01:
- Domain code receives only a server-verified ``WorkspaceAccessContext``.
- A caller-supplied ``workspace_id`` is strictly the target workspace to check,
  never proof of authorization.
- In T-101, real Supabase JWT verification will resolve ``AuthSubject(issuer, sub)``
  to an app user and check current server-side Workspace membership, role, and
  resource permissions. Service login credentials remain separate from GitHub
  ingestion permissions.
- Final role taxonomy and product HTTP status codes are intentionally deferred
  to T-101.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


@dataclass(frozen=True, slots=True)
class AuthSubject:
    """Verified identity subject pair, matching future JWT ``(issuer, sub)``."""

    issuer: str
    subject: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _require_non_empty(self.issuer, "issuer"))
        object.__setattr__(self, "subject", _require_non_empty(self.subject, "subject"))


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Server-verified application user identity (distinct from GitHub tokens)."""

    user_id: str
    auth_subject: AuthSubject

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _require_non_empty(self.user_id, "user_id"))


@dataclass(frozen=True, slots=True)
class WorkspaceMembership:
    """Server-verified Workspace membership for an authenticated user.

    ``role`` is an opaque optional string; the final role list is deferred to T-101.
    """

    workspace_id: str
    user_id: str
    role: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workspace_id", _require_non_empty(self.workspace_id, "workspace_id")
        )
        object.__setattr__(self, "user_id", _require_non_empty(self.user_id, "user_id"))
        if self.role is not None:
            object.__setattr__(self, "role", _require_non_empty(self.role, "role"))


@dataclass(frozen=True, slots=True)
class WorkspaceAccessContext:
    """Trusted server-verified context handed to domain code."""

    user: AuthenticatedUser
    membership: WorkspaceMembership

    def __post_init__(self) -> None:
        if self.membership.user_id != self.user.user_id:
            raise ValueError(
                "WorkspaceMembership.user_id must match AuthenticatedUser.user_id."
            )

    @property
    def user_id(self) -> str:
        return self.user.user_id

    @property
    def workspace_id(self) -> str:
        return self.membership.workspace_id

    @property
    def role(self) -> str | None:
        return self.membership.role

    def has_role(self, *allowed_roles: str) -> bool:
        """Return True if the verified membership role is in ``allowed_roles``."""
        return self.membership.role is not None and self.membership.role in allowed_roles

    def require_role(self, *allowed_roles: str) -> WorkspaceAccessContext:
        """Enforce that the verified membership holds one of ``allowed_roles``."""
        if not self.has_role(*allowed_roles):
            raise WorkspaceMembershipDeniedError(
                "Workspace membership role does not permit this operation."
            )
        return self


@dataclass(frozen=True, slots=True)
class AuthCredentials:
    """Raw bearer credential extracted from the request.

    Intentionally excludes untrusted identity/workspace fields from headers or body,
    and redacts ``bearer_token`` from ``repr()`` / ``str()`` to avoid accidental leaks.
    """

    bearer_token: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        token_state = "<redacted>" if self.bearer_token is not None else "None"
        return f"AuthCredentials(bearer_token={token_state})"

    def __str__(self) -> str:
        return repr(self)


class AccessDenialReason(StrEnum):
    """Distinct boundary denial causes; all deny protected operations."""

    UNAUTHENTICATED = "unauthenticated"
    MEMBERSHIP_NOT_FOUND = "membership_not_found"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"


class AuthorizationBoundaryError(Exception):
    """Base error raised when server-side access verification denies an operation."""

    def __init__(self, reason: AccessDenialReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class UnauthenticatedError(AuthorizationBoundaryError):
    """Raised when user identity is missing or cannot be authenticated."""

    def __init__(self, message: str = "Authentication is required.") -> None:
        super().__init__(AccessDenialReason.UNAUTHENTICATED, message)


class SessionTerminatedError(UnauthenticatedError):
    """Raised when a live session check confirms the user session has ended."""

    def __init__(self, message: str = "The session has ended.") -> None:
        super().__init__(message)


class WorkspaceMembershipDeniedError(AuthorizationBoundaryError):
    """Raised when the authenticated user lacks membership in the target Workspace."""

    def __init__(
        self, message: str = "Workspace membership could not be verified."
    ) -> None:
        super().__init__(AccessDenialReason.MEMBERSHIP_NOT_FOUND, message)


class AccessVerificationUnavailableError(AuthorizationBoundaryError):
    """Raised when identity or membership verification fails or is unavailable."""

    def __init__(
        self, message: str = "Access verification service is unavailable."
    ) -> None:
        super().__init__(AccessDenialReason.VERIFICATION_UNAVAILABLE, message)


class WorkspaceAccessProvider(Protocol):
    """Provider contract for server-side identity and Workspace membership checks."""

    def verify_identity(
        self, credentials: AuthCredentials
    ) -> AuthenticatedUser | None:
        """Verify raw credentials and resolve the app user, or return None."""
        ...

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        """Verify that ``user`` holds membership in ``target_workspace_id``."""
        ...


class UnconfiguredWorkspaceAccessProvider:
    """Fail-closed default provider used in normal runtime before T-101."""

    def verify_identity(
        self, credentials: AuthCredentials
    ) -> AuthenticatedUser | None:
        if credentials.bearer_token is None or not credentials.bearer_token.strip():
            raise UnauthenticatedError("Authentication credentials are required.")
        raise AccessVerificationUnavailableError(
            "Authentication verification provider is not configured."
        )

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        raise AccessVerificationUnavailableError(
            "Workspace membership verification provider is not configured."
        )


def extract_bearer_credentials(authorization_header: str | None) -> AuthCredentials:
    """Parse strictly an ``Authorization: Bearer <token>`` header."""
    if not authorization_header:
        return AuthCredentials(bearer_token=None)

    parts = authorization_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return AuthCredentials(bearer_token=None)

    return AuthCredentials(bearer_token=parts[1])


class WorkspaceAccessGuard:
    """Enforces default-deny server verification before invoking domain code."""

    def __init__(self, provider: WorkspaceAccessProvider) -> None:
        self._provider = provider

    @property
    def provider(self) -> WorkspaceAccessProvider:
        return self._provider

    def require_workspace_access(
        self, credentials: AuthCredentials | None, target_workspace_id: str
    ) -> WorkspaceAccessContext:
        """Verify identity and target Workspace membership, failing closed on any error."""
        if (
            credentials is None
            or credentials.bearer_token is None
            or not credentials.bearer_token.strip()
        ):
            raise UnauthenticatedError("Authentication credentials are required.")

        try:
            user = self._provider.verify_identity(credentials)
        except (UnauthenticatedError, AccessVerificationUnavailableError):
            raise
        except Exception as exc:
            raise AccessVerificationUnavailableError(
                "Identity verification failed unexpectedly."
            ) from exc

        if user is None:
            raise UnauthenticatedError("Credentials could not be authenticated.")

        if not isinstance(target_workspace_id, str) or not target_workspace_id.strip():
            raise WorkspaceMembershipDeniedError("A target workspace_id is required.")
        normalized_workspace_id = target_workspace_id.strip()

        try:
            membership = self._provider.verify_workspace_membership(
                user, normalized_workspace_id
            )
        except (WorkspaceMembershipDeniedError, AccessVerificationUnavailableError):
            raise
        except Exception as exc:
            raise AccessVerificationUnavailableError(
                "Workspace membership verification failed unexpectedly."
            ) from exc

        if membership is None:
            raise WorkspaceMembershipDeniedError(
                "User does not have membership in the target Workspace."
            )

        if (
            membership.user_id != user.user_id
            or membership.workspace_id != normalized_workspace_id
        ):
            raise WorkspaceMembershipDeniedError(
                "Verified membership does not match the authenticated user or target Workspace."
            )

        return WorkspaceAccessContext(user=user, membership=membership)
