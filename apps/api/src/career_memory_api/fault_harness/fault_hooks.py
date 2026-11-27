"""T-005B Fault injection hooks, in-transaction crash wrapper, and REF-04/06 canary leak scanner."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg

from career_memory_api.backfill_engine.models import BackfillStreamName
from career_memory_api.fault_harness.scenario_schema import FaultInjectionPoint
from career_memory_api.initial_import.hooks import FinalizeHookContext, PageHookContext


class InjectedFaultCrash(RuntimeError):
    """Deterministic worker crash raised at an explicit T-005B injection point."""

    def __init__(
        self,
        *,
        point: FaultInjectionPoint,
        message: str,
        stream_name: BackfillStreamName | None = None,
        page_number: int | None = None,
    ) -> None:
        super().__init__(f"[{point.value}] {message}")
        self.point = point
        self.stream_name = stream_name
        self.page_number = page_number


@dataclass(slots=True)
class B004LifecycleFaultHook:
    """Production `BackfillLifecycleHookPort` driver for B-004 fault & drift injection."""

    on_before_page: Callable[[PageHookContext], None] | None = None
    on_after_page: Callable[[PageHookContext], None] | None = None
    on_before_finalize: Callable[[FinalizeHookContext], None] | None = None
    on_after_finalize: Callable[[FinalizeHookContext], None] | None = None
    trace: list[str] = field(default_factory=list)
    active_stream: BackfillStreamName | None = None
    active_page_number: int | None = None
    active_attempt: int = 1

    def before_page_commit(self, ctx: PageHookContext) -> None:
        self.active_stream = ctx.stream_name
        self.active_page_number = ctx.page_number
        self.active_attempt = ctx.attempt
        self.trace.append(
            f"before_page_commit:{ctx.stream_name.value}:p{ctx.page_number}:a{ctx.attempt}"
        )
        if self.on_before_page is not None:
            self.on_before_page(ctx)

    def after_page_commit(self, ctx: PageHookContext) -> None:
        self.trace.append(
            f"after_page_commit:{ctx.stream_name.value}:p{ctx.page_number}:a{ctx.attempt}"
        )
        if self.on_after_page is not None:
            self.on_after_page(ctx)

    def before_finalize(self, ctx: FinalizeHookContext) -> None:
        self.trace.append(f"before_finalize:a{ctx.attempt}")
        if self.on_before_finalize is not None:
            self.on_before_finalize(ctx)

    def after_finalize(self, ctx: FinalizeHookContext) -> None:
        self.trace.append(f"after_finalize:a{ctx.attempt}")
        if self.on_after_finalize is not None:
            self.on_after_finalize(ctx)


@dataclass(slots=True)
class InsidePageTxFaultInjector:
    """Fires a single-shot crash inside `PostgresBackfillPageCommitter`'s active PG18 transaction.

    Attached via `PostgresBackfillPageCommitter(fault_injector=injector)` and paired with
    `B004LifecycleFaultHook` to target an exact `(stream_name, page_number)` right after
    `source_objects`, `source_object_revisions`, `sync_checkpoints`, and
    `sync_run_page_observations` statements execute — immediately BEFORE PostgreSQL COMMIT!
    """

    hook: B004LifecycleFaultHook
    target_stream: BackfillStreamName
    target_page: int
    target_stage: str = "after_checkpoint_update"
    armed: bool = True
    triggered_count: int = 0

    def __call__(self, stage: str) -> None:
        if (
            self.armed
            and stage == self.target_stage
            and self.hook.active_stream is self.target_stream
            and self.hook.active_page_number == self.target_page
        ):
            self.armed = False
            self.triggered_count += 1
            raise InjectedFaultCrash(
                point=FaultInjectionPoint.INSIDE_PAGE_TX_BEFORE_COMMIT,
                stream_name=self.target_stream,
                page_number=self.target_page,
                message=(
                    f"Simulated worker crash inside PG18 page TX at stage={stage!r} "
                    f"for {self.target_stream.value} page {self.target_page} prior to COMMIT."
                ),
            )


class CapturingLogHandler(logging.Handler):
    """In-memory log handler capturing formatted records for REF-04/06 canary auditing."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # noqa: BLE001
            self.records.append(str(record.msg))


@dataclass(frozen=True, slots=True)
class CanaryAuditResult:
    """Comprehensive REF-04/06 canary scan result across DB tables, logs, API payloads, and reports."""

    canaries_checked: tuple[str, ...]
    tables_scanned: int
    rows_scanned: int
    log_records_scanned: int
    api_payloads_scanned: int
    canary_hits: int
    content_units_plan_c_compliant: bool
    leak_locations: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.canary_hits == 0 and self.content_units_plan_c_compliant

    def to_dict(self) -> dict[str, Any]:
        return {
            "canary_count_checked": len(self.canaries_checked),
            "tables_scanned": self.tables_scanned,
            "canary_hits": self.canary_hits,
            "content_units_plan_c_compliant": self.content_units_plan_c_compliant,
            "passed": self.passed,
            "leak_locations": list(self.leak_locations),
        }


def audit_canaries_and_plan_c_compliance(
    admin_conn: psycopg.Connection,
    *,
    canaries: Iterable[str],
    log_records: Iterable[str] = (),
    api_response_texts: Iterable[str] = (),
    report_texts: Iterable[str] = (),
) -> CanaryAuditResult:
    """Scan EVERY user table in PostgreSQL + logs + API responses + reports for canary leakage."""
    canary_tuple = tuple(sorted(set(c for c in canaries if c)))
    leak_locations: list[str] = []
    tables_scanned = 0
    rows_scanned = 0
    content_units_compliant = True

    with admin_conn.cursor() as cur:
        cur.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename ASC
            """
        )
        tables = [str(r[0]) for r in cur.fetchall()]
        tables_scanned = len(tables)

        for tbl in tables:
            cur.execute(f'SELECT row_to_json(t)::text FROM public."{tbl}" t')
            for idx, (row_text,) in enumerate(cur.fetchall()):
                rows_scanned += 1
                if row_text is None:
                    continue
                for marker in canary_tuple:
                    if marker in row_text:
                        leak_locations.append(f"pg:public.{tbl}[{idx}]")

        # Explicit Plan C raw-null compliance on public.content_units
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.content_units
            WHERE text_content IS NOT NULL
               OR content_ref IS NOT NULL
               OR retention_class <> 'NONE'
               OR expires_at IS NOT NULL
            """
        )
        non_compliant_units = int(cur.fetchone()[0])
        if non_compliant_units > 0:
            content_units_compliant = False
            leak_locations.append(f"pg:public.content_units:non_compliant_count={non_compliant_units}")

    logs_list = list(log_records)
    for idx, entry in enumerate(logs_list):
        for marker in canary_tuple:
            if marker in entry:
                leak_locations.append(f"log[{idx}]")

    api_list = list(api_response_texts)
    for idx, payload in enumerate(api_list):
        for marker in canary_tuple:
            if marker in payload:
                leak_locations.append(f"api_response[{idx}]")

    for idx, rep in enumerate(report_texts):
        for marker in canary_tuple:
            if marker in rep:
                leak_locations.append(f"report[{idx}]")

    return CanaryAuditResult(
        canaries_checked=canary_tuple,
        tables_scanned=tables_scanned,
        rows_scanned=rows_scanned,
        log_records_scanned=len(logs_list),
        api_payloads_scanned=len(api_list),
        canary_hits=len([loc for loc in leak_locations if not loc.startswith("pg:public.content_units:non_compliant")]),
        content_units_plan_c_compliant=content_units_compliant,
        leak_locations=tuple(leak_locations),
    )


__all__ = [
    "B004LifecycleFaultHook",
    "CanaryAuditResult",
    "CapturingLogHandler",
    "InjectedFaultCrash",
    "InsidePageTxFaultInjector",
    "audit_canaries_and_plan_c_compliance",
]
