"""Durable Job worker runner connecting `PostgresJobRuntime` with `InitialBackfillOrchestrator`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.initial_import.derived_units import (
    DerivedUnitIngestionProcessor,
    DerivedUnitIngestionResult,
)
from career_memory_api.initial_import.hooks import PageHookContext
from career_memory_api.initial_import.orchestrator import (
    InitialBackfillExecutionResult,
    InitialBackfillOrchestrator,
)
from career_memory_api.jobs.models import (
    ClaimedJob,
    DeletionFenceActiveError,
    JobStatus,
    StaleJobLeaseError,
)
from career_memory_api.jobs.postgres_runtime import PostgresJobRuntime
from career_memory_api.sync_runs.models import SyncRunStatus


@dataclass(frozen=True, slots=True)
class WorkerStepResult:
    """Outcome of claiming and executing one `INITIAL_BACKFILL_SYNC` job."""

    claimed_job: ClaimedJob
    job_final_status: JobStatus
    execution: InitialBackfillExecutionResult
    derived_units: DerivedUnitIngestionResult | None = None
    effect_applied: bool = False


class InitialBackfillJobWorker:
    """Claims durable `INITIAL_BACKFILL_SYNC` jobs from `PostgresJobRuntime` and executes them.

    - Heartbeats job lease on every committed page (raising immediately if lease was stolen
      or deletion fence activated).
    - Completes job with 1-time side-effect ledger on `SUCCESS` and optionally triggers
      `DerivedUnitIngestionProcessor` strictly after `IMPORT_READY` is committed.
    - Leaves `PARTIAL` (`retry_allowed=True`) runs cleanly awaiting explicit
      `POST /v1/sync-runs/{id}/retry` (marking the exhausted attempt job `FAILED_TERMINAL`).
    - Propagates unhandled exceptions (such as fault-harness crash injections from
      `BackfillLifecycleHookPort`) without mutating the already-committed checkpoints.
    """

    def __init__(
        self,
        *,
        job_runtime: PostgresJobRuntime,
        orchestrator: InitialBackfillOrchestrator,
        derived_unit_processor: DerivedUnitIngestionProcessor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._job_runtime = job_runtime
        self._orchestrator = orchestrator
        self._derived_unit_processor = derived_unit_processor
        self._clock = clock or (lambda: datetime.now(UTC))

    def claim_and_execute_next(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        access: WorkspaceAccessContext | None = None,
        lease_duration: timedelta = timedelta(seconds=60),
        run_derived_units: bool = True,
        now: datetime | None = None,
    ) -> WorkerStepResult | None:
        """Claim and execute the next ready `INITIAL_BACKFILL_SYNC` job, or return None."""
        ts = now or self._clock()
        claimed = self._job_runtime.claim_next_job(
            workspace_id,
            worker_id=worker_id,
            lease_duration=lease_duration,
            now=ts,
        )
        if claimed is None:
            return None

        return self.execute_claimed_job(
            claimed,
            access=access,
            lease_extension=lease_duration,
            run_derived_units=run_derived_units,
            now=ts,
        )

    def execute_claimed_job(
        self,
        claimed: ClaimedJob,
        *,
        access: WorkspaceAccessContext | None = None,
        lease_extension: timedelta = timedelta(seconds=60),
        run_derived_units: bool = True,
        now: datetime | None = None,
    ) -> WorkerStepResult:
        """Execute an already-claimed `ClaimedJob` with lease heartbeat and terminal settlement."""
        ts = now or self._clock()
        job = claimed.job
        if job.job_type != "INITIAL_BACKFILL_SYNC" or job.target_sync_run_id is None:
            raise ValueError(
                f"Unsupported job_type={job.job_type!r} or missing target_sync_run_id."
            )

        def _on_page_committed(ctx: PageHookContext) -> None:
            self._job_runtime.heartbeat_and_checkpoint(
                job.workspace_id,
                job_id=job.id,
                lease_token=claimed.lease_token,
                checkpoint={
                    "stream_name": ctx.stream_name.value,
                    "last_committed_page": ctx.page_number,
                    "checkpoint_version": ctx.checkpoint_version,
                },
                lease_extension=lease_extension,
                now=self._clock(),
            )

        execution = self._orchestrator.execute_sync_run(
            workspace_id=job.workspace_id,
            sync_run_id=job.target_sync_run_id,
            access=access,
            attempt=job.attempt,
            on_page_committed=_on_page_committed,
            now=ts,
        )

        derived_res: DerivedUnitIngestionResult | None = None
        effect_applied = False
        final_job_status: JobStatus

        if execution.status is SyncRunStatus.SUCCESS:
            try:
                completed_job, effect_applied = self._job_runtime.complete_job_with_effect(
                    job.workspace_id,
                    job_id=job.id,
                    lease_token=claimed.lease_token,
                    effect_metadata={
                        "sync_run_id": execution.sync_run_id,
                        "status": SyncRunStatus.SUCCESS.value,
                        "import_ready_outbox_id": execution.import_ready_outbox_id,
                    },
                    now=self._clock(),
                    sync_run_success=False,
                )
                final_job_status = completed_job.status
            except DeletionFenceActiveError:
                final_job_status = JobStatus.CANCELED
            except StaleJobLeaseError:
                final_job_status = JobStatus.CANCELED

            if (
                run_derived_units
                and self._derived_unit_processor is not None
                and execution.import_ready
            ):
                derived_res = self._derived_unit_processor.process_import_ready_run(
                    workspace_id=job.workspace_id,
                    sync_run_id=execution.sync_run_id,
                    now=self._clock(),
                )
        elif execution.status is SyncRunStatus.CANCELED:
            try:
                failed_job = self._job_runtime.fail_job(
                    job.workspace_id,
                    job_id=job.id,
                    lease_token=claimed.lease_token,
                    error_code=execution.failure_reason_code or "SYNC_RUN_CANCELED",
                    retryable=False,
                    now=self._clock(),
                )
                final_job_status = failed_job.status
            except (StaleJobLeaseError, DeletionFenceActiveError):
                final_job_status = JobStatus.CANCELED
        else:
            # PARTIAL (including retry_allowed=True awaiting explicit /retry),
            # PARTIAL_WITH_LIMIT, or FAILED -> finalize current job cleanly as FAILED_TERMINAL.
            try:
                failed_job = self._job_runtime.fail_job(
                    job.workspace_id,
                    job_id=job.id,
                    lease_token=claimed.lease_token,
                    error_code=execution.failure_reason_code or execution.status.value,
                    retryable=False,
                    now=self._clock(),
                )
                final_job_status = failed_job.status
            except (StaleJobLeaseError, DeletionFenceActiveError):
                final_job_status = JobStatus.CANCELED

        return WorkerStepResult(
            claimed_job=claimed,
            job_final_status=final_job_status,
            execution=execution,
            derived_units=derived_res,
            effect_applied=effect_applied,
        )


__all__ = [
    "InitialBackfillJobWorker",
    "WorkerStepResult",
]
