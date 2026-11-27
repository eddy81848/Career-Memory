"""Production lifecycle hook boundary (`BackfillLifecycleHookPort`) for M2 initial import.

Exposes zero-overhead, default no-op observation/injection points right before and after
atomic page commits and finalization so fault-injection harnesses (e.g. B-004 T-005A~D)
can exercise the real production `InitialBackfillOrchestrator` and PostgreSQL migrations
(`0010`..`0050`) without test-only shadow models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from career_memory_api.backfill_engine.models import (
    BackfillStreamName,
    FinalizeRunResult,
    PageCommitResult,
)


@dataclass(frozen=True, slots=True)
class PageHookContext:
    """Immutable metadata handed to `before_page_commit` and `after_page_commit` hooks.

    Strictly Plan C non-raw: carries only identifiers, cursor metadata, counts, and commit results.
    """

    workspace_id: str
    sync_run_id: str
    source_resource_id: str
    run_version: int
    stream_name: BackfillStreamName
    page_number: int
    request_cursor: str | None
    next_cursor: str | None
    checkpoint_version: int
    attempt: int = 1
    item_count: int = 0
    page_fingerprint: str | None = None
    commit_result: PageCommitResult | None = None


@dataclass(frozen=True, slots=True)
class FinalizeHookContext:
    """Immutable metadata handed to `before_finalize` and `after_finalize` hooks."""

    workspace_id: str
    sync_run_id: str
    source_resource_id: str
    run_version: int
    plan_id: str
    attempt: int = 1
    pr_candidate_count: int = 0
    direct_commit_candidate_count: int = 0
    finalize_result: FinalizeRunResult | None = None


class BackfillLifecycleHookPort(Protocol):
    """Execution-boundary hook port invoked around DB transactions in `InitialBackfillOrchestrator`.

    Implementations may observe progress or raise exceptions (e.g. simulated worker crashes)
    or mutate external/DB state between boundaries in fault-injection tests.
    """

    def before_page_commit(self, ctx: PageHookContext) -> None:
        """Invoked immediately before `PostgresBackfillPageCommitter.commit_page_batch`."""
        ...

    def after_page_commit(self, ctx: PageHookContext) -> None:
        """Invoked immediately after `PostgresBackfillPageCommitter.commit_page_batch` returns."""
        ...

    def before_finalize(self, ctx: FinalizeHookContext) -> None:
        """Invoked immediately before `PostgresBackfillPageCommitter.finalize_import_run`."""
        ...

    def after_finalize(self, ctx: FinalizeHookContext) -> None:
        """Invoked immediately after `PostgresBackfillPageCommitter.finalize_import_run` returns."""
        ...


class NoopBackfillLifecycleHook:
    """Default production no-op implementation of `BackfillLifecycleHookPort`."""

    def before_page_commit(self, ctx: PageHookContext) -> None:
        return None

    def after_page_commit(self, ctx: PageHookContext) -> None:
        return None

    def before_finalize(self, ctx: FinalizeHookContext) -> None:
        return None

    def after_finalize(self, ctx: FinalizeHookContext) -> None:
        return None


__all__ = [
    "BackfillLifecycleHookPort",
    "FinalizeHookContext",
    "NoopBackfillLifecycleHook",
    "PageHookContext",
]
