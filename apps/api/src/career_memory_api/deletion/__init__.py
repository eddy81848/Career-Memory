"""M5 Source Deletion Workflow, Pre-Commit Fence & 5-Component Receipt Engine (T-502 / T-502A)."""

from career_memory_api.deletion.fence import (
    assert_event_not_deletion_fenced,
    assert_resources_not_deletion_fenced,
    find_event_linked_resource_ids,
)
from career_memory_api.deletion.models import (
    DELETION_COMPONENT_ORDER,
    DeletionComponentReceiptItem,
    DeletionError,
    DeletionJobStatusResponse,
    EphemeralCacheRegistry,
    project_user_visible_deletion_state,
)
from career_memory_api.deletion.postgres_service import (
    ComponentFaultInjector,
    PostgresDeletionService,
)
from career_memory_api.deletion.router import (
    DeletionReadPort,
    create_deletion_router,
    install_deletion_error_handlers,
)

__all__ = [
    "ComponentFaultInjector",
    "DELETION_COMPONENT_ORDER",
    "DeletionComponentReceiptItem",
    "DeletionError",
    "DeletionJobStatusResponse",
    "DeletionReadPort",
    "EphemeralCacheRegistry",
    "PostgresDeletionService",
    "assert_event_not_deletion_fenced",
    "assert_resources_not_deletion_fenced",
    "create_deletion_router",
    "find_event_linked_resource_ids",
    "install_deletion_error_handlers",
    "project_user_visible_deletion_state",
]
