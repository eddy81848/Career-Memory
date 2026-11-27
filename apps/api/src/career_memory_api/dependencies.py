"""FastAPI dependencies bridging HTTP requests to the Workspace access boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Request

from career_memory_api.authz import (
    AuthCredentials,
    WorkspaceAccessContext,
    WorkspaceAccessGuard,
    extract_bearer_credentials,
)


def get_request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> str | None:
    """Extract optional client X-Request-ID header without trusting it for auth."""
    if x_request_id is None:
        return None
    stripped = x_request_id.strip()
    return stripped or None


def get_workspace_access_guard(request: Request) -> WorkspaceAccessGuard:
    """Return the app's configured WorkspaceAccessGuard."""
    guard = getattr(request.app.state, "workspace_access_guard", None)
    if not isinstance(guard, WorkspaceAccessGuard):
        raise RuntimeError("WorkspaceAccessGuard is not initialized on app.state.")
    return guard


def get_auth_credentials(
    authorization: Annotated[str | None, Header()] = None,
) -> AuthCredentials:
    """Extract raw bearer credentials from Authorization header only.

    Untrusted headers (e.g. X-User-Id, X-Workspace-Id) and body fields are ignored.
    """
    return extract_bearer_credentials(authorization)


def require_workspace_access(
    workspace_id: str,
    guard: Annotated[WorkspaceAccessGuard, Depends(get_workspace_access_guard)],
    credentials: Annotated[AuthCredentials, Depends(get_auth_credentials)],
) -> WorkspaceAccessContext:
    """Verify server-side access for a target ``workspace_id`` before domain logic runs.

    ``workspace_id`` is only the target to verify, never proof of authority.
    """
    return guard.require_workspace_access(
        credentials=credentials,
        target_workspace_id=workspace_id,
    )


def require_workspace_roles(
    *allowed_roles: str,
) -> Callable[[WorkspaceAccessContext], WorkspaceAccessContext]:
    """Return a FastAPI dependency enforcing both Workspace access and role allowlist."""
    normalized_roles = tuple(role.strip() for role in allowed_roles if role.strip())
    if not normalized_roles:
        raise ValueError("At least one allowed Workspace role must be specified.")

    def _enforce(
        access: Annotated[WorkspaceAccessContext, Depends(require_workspace_access)],
    ) -> WorkspaceAccessContext:
        return access.require_role(*normalized_roles)

    return _enforce
