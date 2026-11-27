"""Workspace-scoped storage resource lookup boundary.

Design invariants for FOUNDATION-03:
- Storage lookup accepts only a server-verified ``WorkspaceAccessContext`` together
  with a ``resource_id``. No lookup-by-resource-id-only method exists.
- Caller-supplied ``target_workspace_id`` is strictly the membership verification
  target passed to ``WorkspaceAccessGuard``; storage is never called when identity
  or membership verification fails or is unavailable.
- Inside an accessible Workspace, a resource ID owned by another Workspace and a
  nonexistent resource ID both produce the exact same ``WorkspaceResourceNotFoundError``
  so no cross-workspace existence signal is exposed.
- Real DB implementations, final role rules, and product HTTP error contracts are
  intentionally deferred to T-101A / T-101.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from career_memory_api.authz import (
    AuthCredentials,
    WorkspaceAccessContext,
    WorkspaceAccessGuard,
)

_NOT_FOUND_MESSAGE = "Workspace resource was not found."


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


@dataclass(frozen=True, slots=True)
class WorkspaceResource:
    """Minimal domain representation of a resource owned by a Workspace."""

    resource_id: str
    workspace_id: str
    title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resource_id", _require_non_empty(self.resource_id, "resource_id")
        )
        object.__setattr__(
            self, "workspace_id", _require_non_empty(self.workspace_id, "workspace_id")
        )
        if self.title is not None:
            object.__setattr__(self, "title", _require_non_empty(self.title, "title"))


class WorkspaceResourceNotFoundError(Exception):
    """Raised when a resource is unavailable within the verified Workspace scope.

    Always carries a fixed message so cross-workspace resource IDs and
    nonexistent resource IDs remain indistinguishable.
    """

    def __init__(self) -> None:
        super().__init__(_NOT_FOUND_MESSAGE)


class WorkspaceResourceStore(Protocol):
    """Single Workspace-scoped storage lookup interface.

    No real database implementation is wired in FOUNDATION-03.
    """

    def get_by_workspace_and_id(
        self,
        access: WorkspaceAccessContext,
        resource_id: str,
    ) -> WorkspaceResource | None:
        """Return the resource owned by ``access.workspace_id``, or ``None``."""
        ...


class WorkspaceResourceReader:
    """Guards storage reads behind ``WorkspaceAccessGuard`` before invoking storage."""

    def __init__(
        self,
        access_guard: WorkspaceAccessGuard,
        resource_store: WorkspaceResourceStore,
    ) -> None:
        self._access_guard = access_guard
        self._resource_store = resource_store

    def read_resource(
        self,
        *,
        credentials: AuthCredentials | None,
        target_workspace_id: str,
        resource_id: str,
    ) -> WorkspaceResource:
        """Verify Workspace membership first, then query storage within verified scope."""
        access = self._access_guard.require_workspace_access(
            credentials=credentials,
            target_workspace_id=target_workspace_id,
        )

        if not isinstance(resource_id, str) or not resource_id.strip():
            raise WorkspaceResourceNotFoundError()
        normalized_resource_id = resource_id.strip()

        resource = self._resource_store.get_by_workspace_and_id(
            access,
            normalized_resource_id,
        )
        if resource is None:
            raise WorkspaceResourceNotFoundError()

        if (
            resource.workspace_id != access.workspace_id
            or resource.resource_id != normalized_resource_id
        ):
            raise WorkspaceResourceNotFoundError()

        return resource


def ensure_workspace_resource_ownership(
    access: WorkspaceAccessContext,
    resource: WorkspaceResource | None,
    expected_resource_id: str,
) -> WorkspaceResource:
    """Validate ownership within ``access.workspace_id`` or conceal identically."""
    if not isinstance(expected_resource_id, str) or not expected_resource_id.strip():
        raise WorkspaceResourceNotFoundError()
    normalized_id = expected_resource_id.strip()
    if (
        resource is None
        or resource.workspace_id != access.workspace_id
        or resource.resource_id != normalized_id
    ):
        raise WorkspaceResourceNotFoundError()
    return resource
