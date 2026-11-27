"""Port protocols for M2 provider adapters, PostgreSQL metadata stores, and M4 claim gates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.resolver.models import (
    OperationMemoryResolution,
    PersistedUnitResolutionTarget,
    ProviderFetchRequest,
    ProviderFetchResponse,
    RefetchRequestInput,
    ResolutionMetadata,
)
from career_memory_api.resolver.policy import OperationBudgetConfig

if TYPE_CHECKING:
    from career_memory_api.resolver.service import OperationRefetchSession


class ProviderContentFetcherPort(Protocol):
    """Port implemented by M2 (T-202) GitHub HTTP adapters and offline M1 test adapters.

    Implementations MUST:
    - Fetch only ``request.endpoint_url`` (never follow redirects automatically; return
      ``ProviderFetchOutcomeKind.REDIRECT`` with ``redirect_location`` so the resolver's
      SSRF policy validates every hop).
    - Enforce ``request.max_bytes`` and ``request.timeout_seconds`` without persisting
      raw response bytes to disk, cache, or logs.
    """

    def fetch(self, request: ProviderFetchRequest) -> ProviderFetchResponse:
        ...


class ProvenanceMetadataStorePort(Protocol):
    """Workspace-scoped metadata-only store port (strictly NO bare ``get_by_id``)."""

    def get_unit_resolution_target(
        self,
        access: WorkspaceAccessContext,
        content_unit_id: str,
    ) -> PersistedUnitResolutionTarget | None:
        """Return persisted non-raw provenance metadata for ``content_unit_id`` in ``access.workspace_id``."""
        ...

    def list_sibling_unit_anchors(
        self,
        access: WorkspaceAccessContext,
        source_object_revision_id: str,
    ) -> tuple[PersistedUnitResolutionTarget, ...]:
        """Return ordered non-raw sibling units on the same revision for ephemeral context."""
        ...

    def check_policy_fence_current(
        self,
        access: WorkspaceAccessContext,
        content_unit_id: str,
    ) -> PersistedUnitResolutionTarget | None:
        """Re-read live workspace/visibility/deletion policy state for TOCTOU pre-commit checks."""
        ...

    def record_resolution_metadata(
        self,
        access: WorkspaceAccessContext,
        metadata: ResolutionMetadata,
    ) -> None:
        """Persist strictly non-raw ResolutionMetadata (never raw text or mutable revisions)."""
        ...


class ResolutionAuditSinkPort(Protocol):
    """Optional non-raw audit sink receiving strictly ResolutionMetadata."""

    def record(
        self,
        access: WorkspaceAccessContext,
        metadata: ResolutionMetadata,
    ) -> None:
        ...


class RefetchResolverPort(Protocol):
    """Top-level resolver port invoked by HTTP endpoints and M4 Claim/Export pipelines."""

    def open_operation_session(
        self,
        access: WorkspaceAccessContext,
        *,
        operation_id: str,
        budget: OperationBudgetConfig | None = None,
    ) -> OperationRefetchSession:
        ...

    def resolve_unit(
        self,
        session: OperationRefetchSession,
        request: RefetchRequestInput,
    ) -> OperationMemoryResolution:
        ...
