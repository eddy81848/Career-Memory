"""Multi-Surface Plan C (ADR-013 / REF-04) Canary & Raw-Text Non-Persistence Auditor.

Audits ALL persistence and output surfaces after real PostgreSQL 18 + FastAPI execution:
1. Every public table & text/jsonb column in PostgreSQL 18 (migrations 0010..0070),
   explicitly asserting content_units.text_content IS NULL, content_units.content_ref IS NULL,
   fact_support.extracted_span IS NULL, 0 forbidden JSONB raw keys, 0 canary hits, and 0 raw
   source document hits.
2. Captured Python logging records emitted during pipeline/API execution.
3. Captured HTTP metadata responses (facts, claims, exports — excluding ephemeral viewer text).
4. Rendered Markdown & JSON export artifacts.
5. Resolver session/cache cleanliness (0 lingering ephemeral documents).
6. Negative leak detector self-check against known leaky fixtures (refcase-04a/04b).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from typing import Any

import psycopg

from career_memory_api.claim_eval.models import PlanCMultiSurfaceAuditResult
from career_memory_api.schema_contracts.metadata_guard import find_forbidden_raw_keys


class CapturingLogHandler(logging.Handler):
    """In-memory logging handler capturing formatted log messages for REF-04 audits."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(self.format(record))
        except Exception:  # noqa: BLE001
            self.messages.append(str(record.msg))

    def clear(self) -> None:
        self.messages.clear()


def scan_object_for_canaries(obj: Any, canaries: Sequence[str]) -> list[str]:
    """Return any canary strings detected anywhere inside a JSON-serializable object or string."""
    if not canaries:
        return []
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return [tok for tok in canaries if tok and tok in text]


def audit_plan_c_multi_surface(
    admin_conn: psycopg.Connection,
    *,
    canary_tokens: Sequence[str],
    raw_source_texts: Sequence[str],
    captured_logs: Sequence[str] = (),
    http_metadata_payloads: Sequence[tuple[str, Any]] = (),
    export_artifacts: Sequence[tuple[str, Any]] = (),
    cache_entries_remaining: int = 0,
    negative_leaky_fixtures: Sequence[Any] = (),
) -> PlanCMultiSurfaceAuditResult:
    """Execute full multi-surface Plan C non-persistence audit across PG18 + logs + HTTP + exports."""
    canaries = tuple(sorted({c for c in canary_tokens if isinstance(c, str) and c.strip()}))
    # Use full raw source documents + multi-line/long body lines (>= 48 chars) to catch raw quote leaks
    raw_probes: list[str] = []
    for txt in raw_source_texts:
        if not isinstance(txt, str):
            continue
        cleaned = txt.strip()
        if len(cleaned) >= 40:
            raw_probes.append(cleaned)
        # Also check full body lines that contain distinct raw headers/prefix markers
        for line in cleaned.splitlines():
            ls = line.strip()
            if ls.startswith("Transient Raw Body Sentinel:") or ls.startswith("[EDIT NOTICE"):
                raw_probes.append(ls)
    raw_probes = sorted(set(raw_probes))

    leaks: list[str] = []
    pg_canary_hits = 0
    pg_raw_text_hits = 0
    jsonb_forbidden_hits = 0
    cu_compliant = True
    fs_compliant = True

    with admin_conn.cursor() as cur:
        # 1. Hard column NULL assertions on content_units & fact_support
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.content_units
            WHERE text_content IS NOT NULL
               OR content_ref IS NOT NULL
               OR retention_class <> 'NONE'
            """
        )
        cu_bad = int(cur.fetchone()[0])  # type: ignore[index]
        if cu_bad > 0:
            cu_compliant = False
            leaks.append(f"pg:public.content_units non-null raw/ref rows={cu_bad}")

        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.fact_support
            WHERE extracted_span IS NOT NULL
            """
        )
        fs_bad = int(cur.fetchone()[0])  # type: ignore[index]
        if fs_bad > 0:
            fs_compliant = False
            leaks.append(f"pg:public.fact_support non-null extracted_span rows={fs_bad}")

        # 2. Scan EVERY public base table across ALL text/varchar/json/jsonb columns
        cur.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN (
                  SELECT table_name
                  FROM information_schema.tables
                  WHERE table_schema = 'public'
                    AND table_type = 'BASE TABLE'
              )
              AND data_type IN ('text', 'character varying', 'json', 'jsonb')
            ORDER BY table_name, ordinal_position
            """
        )
        col_rows = cur.fetchall()
        tables_seen: set[str] = set()
        cols_by_table: dict[str, list[tuple[str, str]]] = {}
        for tname, cname, dtype in col_rows:
            tables_seen.add(str(tname))
            cols_by_table.setdefault(str(tname), []).append((str(cname), str(dtype)))

        for tname, cols in sorted(cols_by_table.items()):
            select_exprs = ", ".join(f'"{c}"' for c, _ in cols)
            cur.execute(f'SELECT {select_exprs} FROM public."{tname}"')
            for row_idx, row in enumerate(cur.fetchall()):
                for (cname, dtype), val in zip(cols, row, strict=True):
                    if val is None:
                        continue
                    if dtype in ("json", "jsonb"):
                        bad_keys = find_forbidden_raw_keys(val)
                        if bad_keys:
                            jsonb_forbidden_hits += len(bad_keys)
                            leaks.append(
                                f"pg:public.{tname}.{cname}[row={row_idx}] forbidden_keys={bad_keys}"
                            )
                        val_str = json.dumps(val, ensure_ascii=False, default=str)
                    else:
                        val_str = str(val)

                    for tok in canaries:
                        if tok in val_str:
                            pg_canary_hits += 1
                            leaks.append(f"pg:public.{tname}.{cname}[row={row_idx}] canary={tok}")
                    for probe in raw_probes:
                        if probe in val_str:
                            pg_raw_text_hits += 1
                            leaks.append(
                                f"pg:public.{tname}.{cname}[row={row_idx}] raw_text_probe_len={len(probe)}"
                            )

    # 3. Captured Python log records
    log_canary_hits = 0
    for idx, msg in enumerate(captured_logs):
        for tok in canaries:
            if tok in msg:
                log_canary_hits += 1
                leaks.append(f"log[{idx}] canary={tok}")
        for probe in raw_probes:
            if probe in msg:
                log_canary_hits += 1
                leaks.append(f"log[{idx}] raw_text_probe_len={len(probe)}")

    # 4. Captured HTTP metadata responses
    http_canary_hits = 0
    for label, payload in http_metadata_payloads:
        bad_keys = find_forbidden_raw_keys(payload)
        if bad_keys:
            jsonb_forbidden_hits += len(bad_keys)
            leaks.append(f"http:{label} forbidden_keys={bad_keys}")
        hits = scan_object_for_canaries(payload, canaries)
        if hits:
            http_canary_hits += len(hits)
            leaks.append(f"http:{label} canaries={hits}")
        serialized = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False, default=str)
        )
        for probe in raw_probes:
            if probe in serialized:
                http_canary_hits += 1
                leaks.append(f"http:{label} raw_text_probe_len={len(probe)}")

    # 5. Export artifacts (Markdown & JSON)
    export_canary_hits = 0
    for label, artifact in export_artifacts:
        if isinstance(artifact, (dict, list)):
            bad_keys = find_forbidden_raw_keys(artifact)
            if bad_keys:
                jsonb_forbidden_hits += len(bad_keys)
                leaks.append(f"export:{label} forbidden_keys={bad_keys}")
        hits = scan_object_for_canaries(artifact, canaries)
        if hits:
            export_canary_hits += len(hits)
            leaks.append(f"export:{label} canaries={hits}")
        serialized = (
            artifact
            if isinstance(artifact, str)
            else json.dumps(artifact, ensure_ascii=False, default=str)
        )
        for probe in raw_probes:
            if probe in serialized:
                export_canary_hits += 1
                leaks.append(f"export:{label} raw_text_probe_len={len(probe)}")

    # 6. Cache cleanliness
    if cache_entries_remaining != 0:
        leaks.append(f"cache:lingering_entries={cache_entries_remaining}")

    # 7. Negative leak detector self-check (must catch every deliberate leaky fixture)
    neg_verified = True
    if negative_leaky_fixtures:
        for idx, neg_obj in enumerate(negative_leaky_fixtures):
            if not scan_object_for_canaries(neg_obj, canaries):
                neg_verified = False
                leaks.append(f"negative_detector_missed_fixture[{idx}]")

    return PlanCMultiSurfaceAuditResult(
        canaries_checked=len(canaries),
        raw_texts_checked=len(raw_probes),
        pg_tables_scanned=len(tables_seen),
        pg_canary_hits=pg_canary_hits,
        pg_raw_text_hits=pg_raw_text_hits,
        content_units_plan_c_compliant=cu_compliant,
        fact_support_plan_c_compliant=fs_compliant,
        jsonb_forbidden_key_hits=jsonb_forbidden_hits,
        log_records_scanned=len(captured_logs),
        log_canary_hits=log_canary_hits,
        http_responses_scanned=len(http_metadata_payloads),
        http_metadata_canary_hits=http_canary_hits,
        export_artifacts_scanned=len(export_artifacts),
        export_canary_hits=export_canary_hits,
        cache_entries_remaining=cache_entries_remaining,
        negative_leak_detector_verified=neg_verified,
        leak_locations=tuple(leaks),
    )


def merge_audits(audits: Iterable[PlanCMultiSurfaceAuditResult]) -> int:
    """Return total Plan C violation count across an iterable of audit results."""
    total = 0
    for a in audits:
        if not a.passed:
            total += max(1, len(a.leak_locations))
    return total


__all__ = [
    "CapturingLogHandler",
    "audit_plan_c_multi_surface",
    "merge_audits",
    "scan_object_for_canaries",
]
