"""T-203A PaginationSafeguard: cursor cycles, HTTP 200 corruption, limits & deduplication."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from career_memory_api.backfill_engine.models import (
    OPTIONAL_BACKFILL_STREAMS,
    BackfillFailureReasonCode,
    BackfillLimits,
    BackfillStreamName,
    ItemOutcomeKind,
    ItemOutcomeRecord,
    NormalizedPageItem,
    PageValidationDecision,
    ProviderPageEnvelope,
    SkipReasonCode,
)
from career_memory_api.schema_contracts.enums import PrAssociationState, SourceObjectType
from career_memory_api.schema_contracts.metadata_guard import (
    RawTextPersistenceViolationError,
    assert_no_forbidden_raw_keys,
    validate_sha256_hash,
)
from career_memory_api.sync_runs.models import SyncRunStatus

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_OPTIONAL_ACCESS_ERROR_STATUSES = frozenset({403, 404, 410})
_START_CURSOR_SENTINEL = "__START__"


def _parse_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        candidate = raw.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def compute_page_fingerprint(
    stream_name: BackfillStreamName,
    items: tuple[NormalizedPageItem, ...],
    next_cursor: str | None,
) -> str:
    """Compute a deterministic SHA-256 fingerprint over normalized item identities and hashes."""
    canonical_items = sorted(
        (
            item.object_type.value,
            item.provider_object_id,
            item.content_hash,
        )
        for item in items
    )
    payload = {
        "stream": stream_name.value,
        "items": canonical_items,
        "next_cursor": next_cursor,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _compute_content_only_fingerprint(
    stream_name: BackfillStreamName,
    items: tuple[NormalizedPageItem, ...],
) -> str:
    """Fingerprint ignoring cursor jitter to detect infinite identical-page pagination loops."""
    canonical_items = sorted(
        (
            item.object_type.value,
            item.provider_object_id,
            item.content_hash,
        )
        for item in items
    )
    payload = {"stream": stream_name.value, "items": canonical_items}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PaginationSafeguard:
    """Stateful per-run T-203A pagination, partial-response, and budget safeguard.

    Enforces SYNC-02/18/19/21:
    - Detects immediate ``A -> A`` and multi-step ``A -> B -> A`` cursor cycles, plus
      identical-page loops with mutated cursors, rejecting with ``PARTIAL`` /
      ``CURSOR_CYCLE_DETECTED`` (``retry_allowed=False``).
    - Rejects HTTP 200 responses with missing identifiers/hashes/timestamps, declared count
      mismatches, truncation flags, missing next_cursor when has_next_page=True, or Plan C
      forbidden raw keys as ``PARTIAL`` / ``MALFORMED_OR_PARTIAL_PAGE``.
    - Allows optional ``CONTEXT_ISSUES`` stream access errors to be recorded as explicit
      exclusions without blocking required streams.
    - Enforces bounded object, request, and byte budgets with ``PARTIAL_WITH_LIMIT`` and
      explicit ``processed_range`` / ``remaining_range``.
    - Deduplicates boundary-overlapping items idempotently.
    """

    def __init__(
        self,
        limits: BackfillLimits | None = None,
        *,
        initial_visited_cursors: Mapping[BackfillStreamName, set[str]] | None = None,
        initial_visited_fingerprints: Mapping[BackfillStreamName, set[str]] | None = None,
        initial_unique_objects: set[tuple[str, str]] | None = None,
        initial_requests: int = 0,
        initial_bytes: int = 0,
    ) -> None:
        self._limits = limits or BackfillLimits()
        self._visited_cursors: dict[BackfillStreamName, set[str]] = {
            stream: set(initial_visited_cursors.get(stream, ()))
            if initial_visited_cursors
            else set()
            for stream in BackfillStreamName
        }
        self._visited_content_fingerprints: dict[BackfillStreamName, set[str]] = {
            stream: set(initial_visited_fingerprints.get(stream, ()))
            if initial_visited_fingerprints
            else set()
            for stream in BackfillStreamName
        }
        self._unique_objects: set[tuple[str, str]] = (
            set(initial_unique_objects) if initial_unique_objects else set()
        )
        self._requests_total = initial_requests
        self._bytes_total = initial_bytes
        self._last_processed_pages: dict[BackfillStreamName, int] = {}

    @property
    def limits(self) -> BackfillLimits:
        return self._limits

    @property
    def unique_objects_total(self) -> int:
        return len(self._unique_objects)

    @property
    def requests_total(self) -> int:
        return self._requests_total

    @property
    def bytes_total(self) -> int:
        return self._bytes_total

    def export_stream_cursor_state(self, stream_name: BackfillStreamName) -> dict[str, Any]:
        """Export bounded cursor/fingerprint state for persistence in sync_checkpoints."""
        return {
            "visited_cursor_count": len(self._visited_cursors[stream_name]),
            "visited_fingerprint_count": len(self._visited_content_fingerprints[stream_name]),
            "last_processed_page": self._last_processed_pages.get(stream_name),
        }

    def _validate_and_normalize_items(
        self, raw_items: tuple[dict[str, Any], ...]
    ) -> tuple[tuple[NormalizedPageItem, ...] | None, str | None]:
        deduped: dict[tuple[str, str, str], NormalizedPageItem] = {}
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, Mapping):
                return None, f"item[{index}] is not a JSON object"

            obj_type_raw = raw.get("object_type")
            if not isinstance(obj_type_raw, str):
                return None, f"item[{index}] missing object_type"
            try:
                obj_type = SourceObjectType(obj_type_raw)
            except ValueError:
                return None, f"item[{index}] invalid object_type={obj_type_raw!r}"

            provider_object_id = raw.get("provider_object_id")
            if not isinstance(provider_object_id, str) or not provider_object_id.strip():
                return None, f"item[{index}] missing non-empty provider_object_id"
            provider_object_id = provider_object_id.strip()

            content_hash_raw = raw.get("content_hash")
            try:
                content_hash = validate_sha256_hash(content_hash_raw)  # type: ignore[arg-type]
            except ValueError as exc:
                return None, f"item[{index}] invalid content_hash: {exc}"

            provider_updated_at = _parse_timestamp(raw.get("provider_updated_at"))
            observed_at = _parse_timestamp(raw.get("observed_at"))
            if provider_updated_at is None and observed_at is None:
                return None, f"item[{index}] missing required timestamp (provider_updated_at/observed_at)"

            commit_sha: str | None = None
            if obj_type == SourceObjectType.COMMIT:
                sha_raw = raw.get("commit_sha")
                if not isinstance(sha_raw, str) or not _COMMIT_SHA_RE.match(sha_raw.strip().lower()):
                    return None, f"item[{index}] COMMIT missing valid commit_sha"
                commit_sha = sha_raw.strip().lower()
            elif raw.get("commit_sha") is not None:
                sha_raw = raw.get("commit_sha")
                if not isinstance(sha_raw, str) or not _COMMIT_SHA_RE.match(sha_raw.strip().lower()):
                    return None, f"item[{index}] invalid commit_sha format"
                commit_sha = sha_raw.strip().lower()

            metadata_raw = raw.get("normalized_metadata", {})
            if not isinstance(metadata_raw, Mapping):
                return None, f"item[{index}] normalized_metadata must be a JSON object"
            try:
                assert_no_forbidden_raw_keys(metadata_raw, field_name="normalized_metadata")
            except RawTextPersistenceViolationError as exc:
                return None, f"item[{index}] Plan C violation: {exc}"

            pr_assoc_raw = raw.get("pr_association_state")
            pr_assoc_state: PrAssociationState | None = None
            if pr_assoc_raw is not None:
                try:
                    pr_assoc_state = PrAssociationState(str(pr_assoc_raw))
                except ValueError:
                    return None, f"item[{index}] invalid pr_association_state={pr_assoc_raw!r}"

            item = NormalizedPageItem(
                object_type=obj_type,
                provider_object_id=provider_object_id,
                content_hash=content_hash,
                normalized_metadata=dict(metadata_raw),
                hash_policy_version=str(raw.get("hash_policy_version") or "v1"),
                connector_version=str(raw.get("connector_version") or "github-m2-v1"),
                schema_version=str(raw.get("schema_version") or "v1"),
                provider_revision_id=(
                    str(raw["provider_revision_id"])
                    if raw.get("provider_revision_id") is not None
                    else None
                ),
                provider_updated_at=provider_updated_at,
                is_tombstone=bool(raw.get("is_tombstone", False)),
                pr_association_state=pr_assoc_state,
                commit_sha=commit_sha,
                observed_at=observed_at,
            )
            deduped[(obj_type.value, provider_object_id, content_hash)] = item

        return tuple(deduped.values()), None

    def _validate_skipped_items(
        self, skipped: tuple[ItemOutcomeRecord, ...]
    ) -> tuple[tuple[ItemOutcomeRecord, ...] | None, str | None]:
        for idx, rec in enumerate(skipped):
            try:
                assert_no_forbidden_raw_keys(rec.outcome_metadata, field_name="outcome_metadata")
            except RawTextPersistenceViolationError as exc:
                return None, f"skipped_items[{idx}] Plan C violation: {exc}"
        return skipped, None

    def _format_processed_range(self, stream_name: BackfillStreamName) -> str:
        last_page = self._last_processed_pages.get(stream_name, 0)
        return (
            f"stream={stream_name.value},committed_pages=0..{last_page},"
            f"unique_objects={len(self._unique_objects)},requests={self._requests_total}"
        )

    def _format_remaining_range(
        self, envelope: ProviderPageEnvelope, *, reason: str
    ) -> str:
        cursor_desc = envelope.request_cursor if envelope.request_cursor is not None else "START"
        return (
            f"stream={envelope.stream_name.value},from_page={envelope.page_number},"
            f"cursor={cursor_desc},reason={reason}"
        )

    def evaluate_page(self, envelope: ProviderPageEnvelope) -> PageValidationDecision:
        """Validate one provider page against cycle, corruption, Plan C, and budget rules."""
        stream = envelope.stream_name
        empty_fp = compute_page_fingerprint(stream, (), envelope.next_cursor)

        if envelope.page_number < 1:
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=empty_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message="page_number must be >= 1.",
            )

        # 1. Optional stream (CONTEXT_ISSUES) explicit access exclusion
        if stream in OPTIONAL_BACKFILL_STREAMS and (
            envelope.optional_access_error is not None
            or envelope.http_status in _OPTIONAL_ACCESS_ERROR_STATUSES
        ):
            exclusion = ItemOutcomeRecord(
                stream_name=stream,
                object_type="STREAM",
                provider_object_id=f"stream:{stream.value}:page:{envelope.page_number}",
                outcome_kind=ItemOutcomeKind.EXCLUDED,
                reason_code=SkipReasonCode.OPTIONAL_CONTEXT_UNAVAILABLE.value,
                affected_range=f"page={envelope.page_number}",
                retryable=False,
                outcome_metadata={
                    "http_status": envelope.http_status,
                    "access_error": envelope.optional_access_error or f"HTTP_{envelope.http_status}",
                },
            )
            self._requests_total += 1
            return PageValidationDecision(
                accepted=True,
                page_fingerprint=empty_fp,
                deduplicated_items=(),
                item_outcomes=(exclusion,),
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
            )

        # 2. Non-200 responses on required streams
        if envelope.http_status != 200:
            if envelope.http_status in _TRANSIENT_HTTP_STATUSES:
                return PageValidationDecision(
                    accepted=False,
                    page_fingerprint=empty_fp,
                    terminal_status=SyncRunStatus.PARTIAL,
                    failure_reason_code=BackfillFailureReasonCode.TRANSIENT_PROVIDER_ERROR,
                    retry_allowed=True,
                    unique_objects_total=len(self._unique_objects),
                    requests_total=self._requests_total,
                    bytes_total=self._bytes_total,
                    diagnostic_message=f"Transient provider HTTP {envelope.http_status}.",
                )
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=empty_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message=f"Unexpected provider HTTP {envelope.http_status}.",
            )

        # 3. HTTP 200 partial/truncated/corrupted envelope validation
        if envelope.is_truncated:
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=empty_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message="HTTP 200 response flagged is_truncated=True.",
            )

        if (
            envelope.declared_item_count is not None
            and envelope.declared_item_count != len(envelope.items)
        ):
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=empty_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message=(
                    f"Declared item count {envelope.declared_item_count} != "
                    f"observed {len(envelope.items)} on HTTP 200."
                ),
            )

        if envelope.has_next_page and (
            envelope.next_cursor is None or not str(envelope.next_cursor).strip()
        ):
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=empty_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message="has_next_page=True without a valid next_cursor.",
            )

        # 4. Validate and normalize all items + approved skips
        normalized_items, item_err = self._validate_and_normalize_items(envelope.items)
        if item_err is not None or normalized_items is None:
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=empty_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message=item_err,
            )

        validated_skips, skip_err = self._validate_skipped_items(envelope.skipped_items)
        if skip_err is not None or validated_skips is None:
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=empty_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message=skip_err,
            )

        page_fp = compute_page_fingerprint(stream, normalized_items, envelope.next_cursor)
        content_fp = _compute_content_only_fingerprint(stream, normalized_items)

        # 5. Cursor cycle & identical-page loop detection (A -> A and A -> B -> A)
        req_cursor_key = (
            envelope.request_cursor
            if envelope.request_cursor is not None
            else _START_CURSOR_SENTINEL
        )
        visited = self._visited_cursors[stream]

        # Immediate self-repeat (A -> A)
        if (
            envelope.next_cursor is not None
            and envelope.request_cursor is not None
            and envelope.next_cursor == envelope.request_cursor
        ):
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=page_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.CURSOR_CYCLE_DETECTED,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message=(
                    f"Immediate cursor self-loop detected: {envelope.request_cursor!r} -> "
                    f"{envelope.next_cursor!r}."
                ),
            )

        # Multi-step cycle (A -> B -> A)
        if envelope.next_cursor is not None and envelope.next_cursor in visited:
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=page_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.CURSOR_CYCLE_DETECTED,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message=(
                    f"Multi-step cursor cycle detected: next_cursor={envelope.next_cursor!r} "
                    "was already visited."
                ),
            )

        # Identical non-empty page loop with mutated cursor
        if (
            normalized_items
            and envelope.has_next_page
            and content_fp in self._visited_content_fingerprints[stream]
        ):
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=page_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.CURSOR_CYCLE_DETECTED,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message="Identical page content fingerprint repeated with has_next_page=True.",
            )

        if len(visited) >= self._limits.max_cursor_history_per_stream:
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=page_fp,
                terminal_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.CURSOR_CYCLE_DETECTED,
                retry_allowed=False,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message="Cursor history tracking budget exceeded.",
            )

        # 6. Budget limit enforcement (PARTIAL_WITH_LIMIT; zero promotion)
        projected_requests = self._requests_total + 1
        projected_bytes = self._bytes_total + max(0, envelope.payload_bytes)
        page_object_keys = {
            (item.object_type.value, item.provider_object_id) for item in normalized_items
        }
        projected_unique_objects = self._unique_objects | page_object_keys

        limit_reason: str | None = None
        if projected_requests > self._limits.max_requests:
            limit_reason = f"max_requests({self._limits.max_requests})"
        elif projected_bytes > self._limits.max_bytes:
            limit_reason = f"max_bytes({self._limits.max_bytes})"
        elif len(projected_unique_objects) > self._limits.max_objects:
            limit_reason = f"max_objects({self._limits.max_objects})"

        if limit_reason is not None:
            processed_range = self._format_processed_range(stream)
            remaining_range = self._format_remaining_range(envelope, reason=limit_reason)
            limit_outcome = ItemOutcomeRecord(
                stream_name=stream,
                object_type="STREAM",
                provider_object_id=f"stream:{stream.value}:page:{envelope.page_number}",
                outcome_kind=ItemOutcomeKind.EXCLUDED,
                reason_code=SkipReasonCode.LIMIT_EXCEEDED.value,
                affected_range=remaining_range,
                retryable=False,
                outcome_metadata={
                    "limit_dimension": limit_reason,
                    "processed_range": processed_range,
                    "remaining_range": remaining_range,
                },
            )
            return PageValidationDecision(
                accepted=False,
                page_fingerprint=page_fp,
                deduplicated_items=(),
                item_outcomes=(limit_outcome,),
                terminal_status=SyncRunStatus.PARTIAL_WITH_LIMIT,
                failure_reason_code=BackfillFailureReasonCode.LIMIT_EXCEEDED,
                retry_allowed=False,
                processed_range=processed_range,
                remaining_range=remaining_range,
                unique_objects_total=len(self._unique_objects),
                requests_total=self._requests_total,
                bytes_total=self._bytes_total,
                diagnostic_message=f"Import limit exceeded: {limit_reason}.",
            )

        # 7. Commit in-memory safeguard progression ONLY when page is accepted
        visited.add(req_cursor_key)
        if normalized_items:
            self._visited_content_fingerprints[stream].add(content_fp)
        self._unique_objects = projected_unique_objects
        self._requests_total = projected_requests
        self._bytes_total = projected_bytes
        self._last_processed_pages[stream] = envelope.page_number

        return PageValidationDecision(
            accepted=True,
            page_fingerprint=page_fp,
            deduplicated_items=normalized_items,
            item_outcomes=validated_skips,
            unique_objects_total=len(self._unique_objects),
            requests_total=self._requests_total,
            bytes_total=self._bytes_total,
        )
