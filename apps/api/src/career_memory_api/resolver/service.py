"""Option C Refetch-Only Resolver service, OperationRefetchSession, and approval/export gates."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    ContentUnitParseResult,
    ContentUnitType,
    EphemeralCanonicalDocument,
    EphemeralResolvedUnit,
    OriginalSourceLocator,
    PersistedContentUnitMetadata,
    SkipReasonCode,
    SourceObjectKind,
    SpanLocator,
    canonicalize_source_bytes,
    extract_verified_span,
    parse_content_units,
    sha256_prefixed_text,
)
from career_memory_api.resolver.models import (
    EphemeralUnitContext,
    ExportGatePrecondition,
    GateOperationKind,
    GateVerificationResult,
    OperationMemoryResolution,
    PersistedUnitResolutionTarget,
    ProviderFetchOutcomeKind,
    ProviderFetchRequest,
    ProviderFetchResponse,
    RefetchRequestInput,
    ResolutionErrorMapping,
    ResolutionMetadata,
    ResolutionStatus,
    map_resolution_status_to_error,
    normalize_sha256_hash,
)
from career_memory_api.resolver.policy import (
    EndpointSecurityPolicy,
    OperationBudgetConfig,
    build_provider_endpoint_url,
    evaluate_target_policy,
)
from career_memory_api.resolver.ports import (
    ProvenanceMetadataStorePort,
    ProviderContentFetcherPort,
    ResolutionAuditSinkPort,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

_RETRYABLE_FETCH_KINDS: frozenset[ProviderFetchOutcomeKind] = frozenset(
    {
        ProviderFetchOutcomeKind.RATE_LIMITED,
        ProviderFetchOutcomeKind.SERVER_ERROR,
        ProviderFetchOutcomeKind.TIMEOUT,
        ProviderFetchOutcomeKind.CONNECTION_ERROR,
    }
)

_LIMIT_SKIP_CODES: frozenset[SkipReasonCode] = frozenset(
    {
        SkipReasonCode.OVERSIZED_INPUT,
        SkipReasonCode.DIFF_BOMB_LIMIT,
        SkipReasonCode.LINE_LENGTH_EXCEEDED,
    }
)

_UNIT_TYPE_TO_SOURCE_KIND: dict[ContentUnitType, SourceObjectKind] = {
    ContentUnitType.PR_TITLE: SourceObjectKind.PR_TITLE,
    ContentUnitType.PR_BODY_SECTION: SourceObjectKind.PR_BODY,
    ContentUnitType.ISSUE_SECTION: SourceObjectKind.ISSUE_BODY,
    ContentUnitType.COMMENT: SourceObjectKind.COMMENT,
    ContentUnitType.REVIEW_COMMENT: SourceObjectKind.REVIEW_COMMENT,
    ContentUnitType.COMMIT_MESSAGE: SourceObjectKind.COMMIT_MESSAGE,
    ContentUnitType.DIFF_HUNK: SourceObjectKind.UNIFIED_DIFF,
    ContentUnitType.SYMBOL_CHANGE: SourceObjectKind.SYMBOL_CHANGE,
}


class OperationRefetchSession:
    """Operation-scoped ephemeral memory boundary, budget tracker, and revision dedupe cache.

    All ephemeral canonical documents and opened ``OperationMemoryResolution`` handles
    are cleared immediately when ``close()`` or context-manager ``__exit__`` runs
    (including on exception or cancellation).
    """

    __slots__ = (
        "_access",
        "_operation_id",
        "_budget",
        "_lock",
        "_requests_used",
        "_bytes_read",
        "_active_fetches",
        "_revision_docs",
        "_parsed_cache",
        "_handles",
        "_matched_by_unit_id",
        "_closed",
    )

    def __init__(
        self,
        access: WorkspaceAccessContext,
        *,
        operation_id: str,
        budget: OperationBudgetConfig | None = None,
    ) -> None:
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string.")
        self._access = access
        self._operation_id = operation_id.strip()
        self._budget = budget or OperationBudgetConfig()
        self._lock = threading.Lock()
        self._requests_used = 0
        self._bytes_read = 0
        self._active_fetches = 0
        self._revision_docs: dict[tuple[str, str], EphemeralCanonicalDocument] = {}
        self._parsed_cache: dict[tuple[str, str, SourceObjectKind], ContentUnitParseResult] = {}
        self._handles: list[OperationMemoryResolution] = []
        self._matched_by_unit_id: dict[str, ResolutionMetadata] = {}
        self._closed = False

    @property
    def access(self) -> WorkspaceAccessContext:
        return self._access

    @property
    def workspace_id(self) -> str:
        return self._access.workspace_id

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def budget(self) -> OperationBudgetConfig:
        return self._budget

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def requests_used(self) -> int:
        with self._lock:
            return self._requests_used

    @property
    def bytes_read(self) -> int:
        with self._lock:
            return self._bytes_read

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("OperationRefetchSession is already closed.")

    def get_cached_document(
        self, source_object_revision_id: str, expected_revision_hash: str
    ) -> EphemeralCanonicalDocument | None:
        with self._lock:
            self._assert_open()
            return self._revision_docs.get((source_object_revision_id, expected_revision_hash))

    def put_cached_document(
        self,
        source_object_revision_id: str,
        expected_revision_hash: str,
        document: EphemeralCanonicalDocument,
    ) -> None:
        with self._lock:
            self._assert_open()
            self._revision_docs[(source_object_revision_id, expected_revision_hash)] = document

    def get_or_parse_units(
        self,
        source_object_revision_id: str,
        expected_revision_hash: str,
        document: EphemeralCanonicalDocument,
        source_kind: SourceObjectKind,
    ) -> ContentUnitParseResult | None:
        key = (source_object_revision_id, expected_revision_hash, source_kind)
        with self._lock:
            self._assert_open()
            cached = self._parsed_cache.get(key)
        if cached is not None:
            return cached
        try:
            parsed = parse_content_units(document, source_kind)
        except Exception:  # noqa: BLE001
            return None
        with self._lock:
            if not self._closed:
                self._parsed_cache[key] = parsed
        return parsed

    def try_acquire_concurrency_slot(self) -> bool:
        with self._lock:
            self._assert_open()
            if self._active_fetches >= self._budget.max_concurrency:
                return False
            self._active_fetches += 1
            return True

    def release_concurrency_slot(self) -> None:
        with self._lock:
            if self._active_fetches > 0:
                self._active_fetches -= 1

    def reserve_request_slot(self) -> tuple[bool, int]:
        """Atomically check and reserve 1 request slot + return remaining byte allowance."""
        with self._lock:
            self._assert_open()
            if self._requests_used + 1 > self._budget.max_requests:
                return False, 0
            remaining_total = self._budget.max_total_bytes - self._bytes_read
            if remaining_total <= 0 or self._budget.max_unit_bytes <= 0:
                return False, -1
            self._requests_used += 1
            return True, min(self._budget.max_unit_bytes, remaining_total)

    def add_bytes_read(self, count: int) -> int:
        with self._lock:
            self._bytes_read += max(0, count)
            return self._bytes_read

    def register_handle(self, handle: OperationMemoryResolution) -> OperationMemoryResolution:
        with self._lock:
            if self._closed:
                handle.close()
            else:
                self._handles.append(handle)
        return handle

    def record_matched(self, metadata: ResolutionMetadata) -> None:
        if metadata.status is not ResolutionStatus.MATCHED:
            return
        with self._lock:
            self._matched_by_unit_id[metadata.content_unit_id] = metadata

    def get_matched_metadata(self, content_unit_id: str) -> ResolutionMetadata | None:
        with self._lock:
            return self._matched_by_unit_id.get(content_unit_id)

    def close(self) -> None:
        """Clear all ephemeral documents, parsed units, and close every resolution handle."""
        with self._lock:
            self._closed = True
            self._revision_docs.clear()
            self._parsed_cache.clear()
            handles = list(self._handles)
            self._handles.clear()
        for handle in handles:
            handle.close()

    def __enter__(self) -> OperationRefetchSession:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def __getstate__(self) -> Any:
        raise TypeError("OperationRefetchSession is strictly ephemeral and cannot be serialized.")

    def __repr__(self) -> str:
        return (
            "OperationRefetchSession("
            f"operation_id={self._operation_id!r}, "
            f"workspace_id={self._access.workspace_id!r}, "
            f"requests_used={self._requests_used}, "
            f"bytes_read={self._bytes_read}, "
            f"closed={self._closed})"
        )

    def __str__(self) -> str:
        return repr(self)


class InMemoryProviderContentFetcher:
    """Deterministic offline ProviderContentFetcherPort for M1 tests and verification."""

    def __init__(
        self,
        handler: Callable[[ProviderFetchRequest], ProviderFetchResponse],
    ) -> None:
        self._handler = handler
        self._lock = threading.Lock()
        self.requests: list[ProviderFetchRequest] = []

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def fetch(self, request: ProviderFetchRequest) -> ProviderFetchResponse:
        with self._lock:
            self.requests.append(request)
        return self._handler(request)

    @classmethod
    def from_single_bytes(
        cls,
        raw_bytes: bytes,
        *,
        mime_type: str = "text/markdown",
        provider_visibility: str = "PUBLIC",
    ) -> InMemoryProviderContentFetcher:
        def _handle(_req: ProviderFetchRequest) -> ProviderFetchResponse:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                http_status=200,
                raw_bytes=raw_bytes,
                bytes_read=len(raw_bytes),
                mime_type=mime_type,
                provider_visibility=provider_visibility,
            )

        return cls(_handle)

    @classmethod
    def from_constant_response(
        cls, response: ProviderFetchResponse
    ) -> InMemoryProviderContentFetcher:
        return cls(lambda _req: response)

    @classmethod
    def from_sequence(
        cls, responses: Sequence[ProviderFetchResponse]
    ) -> InMemoryProviderContentFetcher:
        items = list(responses)
        idx_box = [0]
        seq_lock = threading.Lock()

        def _handle(_req: ProviderFetchRequest) -> ProviderFetchResponse:
            with seq_lock:
                i = idx_box[0]
                if i < len(items):
                    idx_box[0] += 1
                    return items[i]
                return items[-1]

        return cls(_handle)


def _extract_span_locator_from_anchor(
    anchor: Mapping[str, Any], *, canonicalization_version: str
) -> SpanLocator | None:
    raw_loc = anchor.get("locator")
    source_map: Mapping[str, Any] = raw_loc if isinstance(raw_loc, Mapping) else anchor
    required = (
        "char_start",
        "char_end",
        "char_length",
        "utf8_byte_start",
        "utf8_byte_end",
        "utf8_byte_length",
        "utf16_start",
        "utf16_end",
        "line_start",
        "line_end",
        "span_sha256",
    )
    if not all(k in source_map for k in required):
        return None
    try:
        return SpanLocator.from_mapping(
            dict(source_map),
            canonicalization_version=canonicalization_version,
        )
    except (ValueError, TypeError, KeyError):
        return None


def _build_original_locator_from_anchor(anchor: Mapping[str, Any]) -> OriginalSourceLocator:
    raw = anchor.get("original_locator")
    src = raw if isinstance(raw, Mapping) else anchor
    return OriginalSourceLocator(
        section=src.get("section"),
        heading_level=src.get("heading_level"),
        heading_sha256=src.get("heading_sha256"),
        file_path=src.get("file_path"),
        previous_file_path=src.get("previous_file_path"),
        is_renamed=bool(src.get("is_renamed", False)),
        hunk_header=src.get("hunk_header"),
        old_start=src.get("old_start"),
        old_lines=src.get("old_lines"),
        new_start=src.get("new_start"),
        new_lines=src.get("new_lines"),
        comment_id=src.get("comment_id"),
        thread_id=src.get("thread_id"),
        commit_sha=src.get("commit_sha"),
        original_commit_sha=src.get("original_commit_sha"),
        diff_side=src.get("diff_side"),
        source_line_start=src.get("source_line_start"),
        source_line_end=src.get("source_line_end"),
        original_line_start=src.get("original_line_start"),
        original_line_end=src.get("original_line_end"),
        matched_hunk_unit_id=src.get("matched_hunk_unit_id"),
        symbol_name=src.get("symbol_name"),
        symbol_kind=src.get("symbol_kind"),
    )


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


class RefetchResolverService:
    """Option C on-demand refetch resolver enforcing policy, SSRF, budget, hash, and locator checks."""

    def __init__(
        self,
        *,
        metadata_store: ProvenanceMetadataStorePort,
        fetcher: ProviderContentFetcherPort,
        security_policy: EndpointSecurityPolicy | None = None,
        default_budget: OperationBudgetConfig | None = None,
        audit_sink: ResolutionAuditSinkPort | None = None,
        endpoint_builder: Callable[[PersistedUnitResolutionTarget], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._metadata_store = metadata_store
        self._fetcher = fetcher
        self._security_policy = security_policy or EndpointSecurityPolicy()
        self._default_budget = default_budget or OperationBudgetConfig()
        self._audit_sink = audit_sink
        self._endpoint_builder = endpoint_builder or build_provider_endpoint_url
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def metadata_store(self) -> ProvenanceMetadataStorePort:
        return self._metadata_store

    def open_operation_session(
        self,
        access: WorkspaceAccessContext,
        *,
        operation_id: str,
        budget: OperationBudgetConfig | None = None,
    ) -> OperationRefetchSession:
        return OperationRefetchSession(
            access,
            operation_id=operation_id,
            budget=budget or self._default_budget,
        )

    def _finalize(
        self,
        session: OperationRefetchSession,
        access: WorkspaceAccessContext,
        metadata: ResolutionMetadata,
        *,
        ephemeral_unit: EphemeralResolvedUnit | None = None,
        ephemeral_span: Any = None,
        ephemeral_context: EphemeralUnitContext | None = None,
    ) -> OperationMemoryResolution:
        self._metadata_store.record_resolution_metadata(access, metadata)
        if self._audit_sink is not None:
            self._audit_sink.record(access, metadata)
        if metadata.status is ResolutionStatus.MATCHED:
            session.record_matched(metadata)
        handle = OperationMemoryResolution(
            metadata=metadata,
            ephemeral_unit=ephemeral_unit,
            ephemeral_span=ephemeral_span,
            ephemeral_context=ephemeral_context,
        )
        return session.register_handle(handle)

    def _build_context(
        self,
        session: OperationRefetchSession,
        access: WorkspaceAccessContext,
        target: PersistedUnitResolutionTarget,
        document: EphemeralCanonicalDocument,
        expected_rev_hash: str,
        original_locator: OriginalSourceLocator,
    ) -> EphemeralUnitContext:
        preceding_text: str | None = None
        following_text: str | None = None

        siblings = self._metadata_store.list_sibling_unit_anchors(
            access, target.source_object_revision_id
        )
        if siblings:
            ordered = sorted(siblings, key=lambda item: item.unit_ordinal)
            prev_sibling = None
            next_sibling = None
            for idx, item in enumerate(ordered):
                if item.content_unit_id == target.content_unit_id:
                    if idx > 0:
                        prev_sibling = ordered[idx - 1]
                    if idx + 1 < len(ordered):
                        next_sibling = ordered[idx + 1]
                    break
            if prev_sibling is not None:
                prev_loc = _extract_span_locator_from_anchor(
                    prev_sibling.anchor,
                    canonicalization_version=prev_sibling.canonicalization_version,
                )
                if prev_loc is not None:
                    prev_res = extract_verified_span(
                        document, prev_loc, expected_source_content_sha256=expected_rev_hash
                    )
                    if prev_res.verified and prev_res.span is not None:
                        preceding_text = prev_res.span.text
            if next_sibling is not None:
                next_loc = _extract_span_locator_from_anchor(
                    next_sibling.anchor,
                    canonicalization_version=next_sibling.canonicalization_version,
                )
                if next_loc is not None:
                    next_res = extract_verified_span(
                        document, next_loc, expected_source_content_sha256=expected_rev_hash
                    )
                    if next_res.verified and next_res.span is not None:
                        following_text = next_res.span.text

        if preceding_text is None and following_text is None:
            source_kind = _UNIT_TYPE_TO_SOURCE_KIND.get(target.unit_type)
            if source_kind is not None:
                parsed = session.get_or_parse_units(
                    target.source_object_revision_id, expected_rev_hash, document, source_kind
                )
                if parsed is not None and 0 <= target.unit_ordinal < len(parsed.units):
                    if target.unit_ordinal > 0:
                        preceding_text = parsed.units[target.unit_ordinal - 1].text
                    if target.unit_ordinal + 1 < len(parsed.units):
                        following_text = parsed.units[target.unit_ordinal + 1].text

        return EphemeralUnitContext(
            section=original_locator.section,
            file_path=original_locator.file_path,
            previous_file_path=original_locator.previous_file_path,
            hunk_header=original_locator.hunk_header,
            _preceding_unit_text=preceding_text,
            _following_unit_text=following_text,
        )

    def resolve_unit(
        self,
        session: OperationRefetchSession,
        request: RefetchRequestInput,
    ) -> OperationMemoryResolution:
        """Execute strict Option C §4 refetch resolution order for a single Content Unit."""
        if session.closed:
            raise RuntimeError("Cannot resolve unit on a closed OperationRefetchSession.")
        if request.access.workspace_id != session.workspace_id:
            raise WorkspaceResourceNotFoundError()

        now = self._clock()
        access = request.access

        # Step ① Workspace & provenance lookup + ownership concealment
        target = self._metadata_store.get_unit_resolution_target(access, request.content_unit_id)
        if target is None or target.workspace_id != access.workspace_id:
            raise WorkspaceResourceNotFoundError()

        if (
            (request.source_resource_id is not None and request.source_resource_id != target.source_resource_id)
            or (request.source_object_id is not None and request.source_object_id != target.source_object_id)
            or (
                request.source_object_revision_id is not None
                and request.source_object_revision_id != target.source_object_revision_id
            )
        ):
            raise WorkspaceResourceNotFoundError()

        expected_rev_hash = normalize_sha256_hash(
            target.expected_revision_hash, field_name="expected_revision_hash"
        )
        expected_unit_hash = normalize_sha256_hash(
            target.expected_unit_hash, field_name="expected_unit_hash"
        )

        # Caller-provided expectation cross-check
        if request.expected_revision_hash is not None:
            req_rev_hash = normalize_sha256_hash(
                request.expected_revision_hash, field_name="request.expected_revision_hash"
            )
            if req_rev_hash != expected_rev_hash:
                return self._finalize(
                    session,
                    access,
                    ResolutionMetadata(
                        status=ResolutionStatus.REVISION_MISMATCH,
                        checked_at=now,
                        operation_id=session.operation_id,
                        source_object_revision_id=target.source_object_revision_id,
                        content_unit_id=target.content_unit_id,
                        expected_revision_hash=req_rev_hash,
                        observed_revision_hash=expected_rev_hash,
                        expected_unit_hash=expected_unit_hash,
                        observed_unit_hash=None,
                        canonicalization_version=target.canonicalization_version,
                        parser_version=target.parser_version,
                        retryable=False,
                        reason_code="CALLER_EXPECTED_REVISION_HASH_MISMATCH",
                        requests_used=0,
                        bytes_read=0,
                        deduped=False,
                        fetch_attempted=False,
                    ),
                )

        if request.expected_unit_hash is not None:
            req_unit_hash = normalize_sha256_hash(
                request.expected_unit_hash, field_name="request.expected_unit_hash"
            )
            if req_unit_hash != expected_unit_hash:
                return self._finalize(
                    session,
                    access,
                    ResolutionMetadata(
                        status=ResolutionStatus.INVALID_LOCATOR,
                        checked_at=now,
                        operation_id=session.operation_id,
                        source_object_revision_id=target.source_object_revision_id,
                        content_unit_id=target.content_unit_id,
                        expected_revision_hash=expected_rev_hash,
                        observed_revision_hash=None,
                        expected_unit_hash=req_unit_hash,
                        observed_unit_hash=expected_unit_hash,
                        canonicalization_version=target.canonicalization_version,
                        parser_version=target.parser_version,
                        retryable=False,
                        reason_code="CALLER_EXPECTED_UNIT_HASH_MISMATCH",
                        requests_used=0,
                        bytes_read=0,
                        deduped=False,
                        fetch_attempted=False,
                    ),
                )

        # Policy / deletion / visibility / raw-storage pre-check (BEFORE any fetch!)
        policy_ok, policy_reason = evaluate_target_policy(target)
        if not policy_ok:
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.POLICY_BLOCKED,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=None,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=None,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code=policy_reason,
                    requests_used=0,
                    bytes_read=0,
                    deduped=False,
                    fetch_attempted=False,
                ),
            )

        if (
            target.canonicalization_version != CANONICALIZATION_VERSION
            or target.parser_version != PARSER_VERSION
        ):
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.INVALID_LOCATOR,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=None,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=None,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code="UNSUPPORTED_CANONICALIZATION_OR_PARSER_VERSION",
                    requests_used=0,
                    bytes_read=0,
                    deduped=False,
                    fetch_attempted=False,
                ),
            )

        # Step ② Server-owned endpoint construction & SSRF guard (BEFORE any fetch!)
        try:
            endpoint_url = self._endpoint_builder(target)
        except ValueError:
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.POLICY_BLOCKED,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=None,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=None,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code="INVALID_PROVIDER_IDENTIFIER",
                    requests_used=0,
                    bytes_read=0,
                    deduped=False,
                    fetch_attempted=False,
                ),
            )

        endpoint_check = self._security_policy.validate_endpoint_url(endpoint_url)
        if not endpoint_check.allowed or endpoint_check.normalized_url is None:
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.POLICY_BLOCKED,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=None,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=None,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code=endpoint_check.reason_code or "ENDPOINT_POLICY_BLOCKED",
                    requests_used=0,
                    bytes_read=0,
                    deduped=False,
                    fetch_attempted=False,
                ),
            )

        # Step ③ Operation-scoped dedupe cache check (REF-08a)
        cached_doc = session.get_cached_document(target.source_object_revision_id, expected_rev_hash)
        local_requests = 0
        local_bytes = 0
        fetch_attempted = False
        deduped = cached_doc is not None
        document: EphemeralCanonicalDocument | None = cached_doc

        if document is None:
            if not session.try_acquire_concurrency_slot():
                return self._finalize(
                    session,
                    access,
                    ResolutionMetadata(
                        status=ResolutionStatus.PARTIAL_WITH_LIMIT,
                        checked_at=now,
                        operation_id=session.operation_id,
                        source_object_revision_id=target.source_object_revision_id,
                        content_unit_id=target.content_unit_id,
                        expected_revision_hash=expected_rev_hash,
                        observed_revision_hash=None,
                        expected_unit_hash=expected_unit_hash,
                        observed_unit_hash=None,
                        canonicalization_version=target.canonicalization_version,
                        parser_version=target.parser_version,
                        retryable=False,
                        reason_code="CONCURRENCY_LIMIT_EXCEEDED",
                        requests_used=0,
                        bytes_read=0,
                        deduped=False,
                        fetch_attempted=False,
                    ),
                )

            try:
                budget = session.budget
                current_url = endpoint_check.normalized_url
                is_diff = target.unit_type is ContentUnitType.DIFF_HUNK
                file_path = target.anchor.get("file_path")
                retries_used = 0
                redirects_used = 0
                fetch_response: ProviderFetchResponse | None = None

                while True:
                    reserved, allowed_bytes = session.reserve_request_slot()
                    if not reserved:
                        limit_reason = (
                            "BYTE_LIMIT_EXCEEDED"
                            if allowed_bytes == -1
                            else "REQUEST_LIMIT_EXCEEDED"
                        )
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.PARTIAL_WITH_LIMIT,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code=limit_reason,
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=fetch_attempted,
                            ),
                        )

                    local_requests += 1
                    fetch_attempted = True
                    fetch_req = ProviderFetchRequest(
                        provider=target.source_account_provider,
                        endpoint_url=current_url,
                        provider_resource_id=target.provider_resource_id,
                        resource_name=target.resource_name,
                        object_type=target.object_type,
                        provider_object_id=target.provider_object_id,
                        provider_revision_id=target.provider_revision_id,
                        token_secret_ref=target.source_account_token_secret_ref,
                        operation_id=session.operation_id,
                        max_bytes=allowed_bytes,
                        timeout_seconds=budget.timeout_seconds,
                        source_object_revision_id=target.source_object_revision_id,
                        expected_revision_hash=expected_rev_hash,
                        file_path=file_path if isinstance(file_path, str) else None,
                        is_diff=is_diff,
                    )
                    fetch_response = self._fetcher.fetch(fetch_req)
                    observed_bytes = (
                        len(fetch_response.raw_bytes)
                        if fetch_response.raw_bytes is not None
                        else max(0, fetch_response.bytes_read)
                    )
                    local_bytes += observed_bytes
                    total_session_bytes = session.add_bytes_read(observed_bytes)

                    if fetch_response.kind is ProviderFetchOutcomeKind.REDIRECT:
                        max_hops = min(self._security_policy.max_redirects, budget.max_redirects)
                        if redirects_used >= max_hops:
                            return self._finalize(
                                session,
                                access,
                                ResolutionMetadata(
                                    status=ResolutionStatus.POLICY_BLOCKED,
                                    checked_at=now,
                                    operation_id=session.operation_id,
                                    source_object_revision_id=target.source_object_revision_id,
                                    content_unit_id=target.content_unit_id,
                                    expected_revision_hash=expected_rev_hash,
                                    observed_revision_hash=None,
                                    expected_unit_hash=expected_unit_hash,
                                    observed_unit_hash=None,
                                    canonicalization_version=target.canonicalization_version,
                                    parser_version=target.parser_version,
                                    retryable=False,
                                    reason_code="REDIRECT_LIMIT_EXCEEDED",
                                    requests_used=local_requests,
                                    bytes_read=local_bytes,
                                    deduped=False,
                                    fetch_attempted=True,
                                ),
                            )
                        redir_check = self._security_policy.validate_redirect_target(
                            current_url, fetch_response.redirect_location
                        )
                        if not redir_check.allowed or redir_check.normalized_url is None:
                            return self._finalize(
                                session,
                                access,
                                ResolutionMetadata(
                                    status=ResolutionStatus.POLICY_BLOCKED,
                                    checked_at=now,
                                    operation_id=session.operation_id,
                                    source_object_revision_id=target.source_object_revision_id,
                                    content_unit_id=target.content_unit_id,
                                    expected_revision_hash=expected_rev_hash,
                                    observed_revision_hash=None,
                                    expected_unit_hash=expected_unit_hash,
                                    observed_unit_hash=None,
                                    canonicalization_version=target.canonicalization_version,
                                    parser_version=target.parser_version,
                                    retryable=False,
                                    reason_code="REDIRECT_BLOCKED",
                                    requests_used=local_requests,
                                    bytes_read=local_bytes,
                                    deduped=False,
                                    fetch_attempted=True,
                                ),
                            )
                        current_url = redir_check.normalized_url
                        redirects_used += 1
                        continue

                    if fetch_response.kind in _RETRYABLE_FETCH_KINDS:
                        if retries_used < budget.max_retries:
                            retries_used += 1
                            continue
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.UNAVAILABLE,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=True,
                                reason_code=fetch_response.reason_code
                                or f"PROVIDER_{fetch_response.kind.value}",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    if fetch_response.kind is ProviderFetchOutcomeKind.POLICY_DENIED:
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.POLICY_BLOCKED,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code=fetch_response.reason_code or "PROVIDER_ACCESS_DENIED",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    if fetch_response.kind is ProviderFetchOutcomeKind.NOT_FOUND:
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.UNAVAILABLE,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code=fetch_response.reason_code or "PROVIDER_OBJECT_NOT_FOUND",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    if (
                        fetch_response.kind
                        in (
                            ProviderFetchOutcomeKind.PARTIAL_CONTENT,
                            ProviderFetchOutcomeKind.LIMIT_EXCEEDED,
                        )
                        or fetch_response.truncated
                    ):
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.PARTIAL_WITH_LIMIT,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code=fetch_response.reason_code
                                or (
                                    "PARTIAL_RESPONSE_TRUNCATED"
                                    if fetch_response.kind is ProviderFetchOutcomeKind.PARTIAL_CONTENT
                                    else "BYTE_LIMIT_EXCEEDED"
                                ),
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    # OK response checks
                    if (
                        fetch_response.provider_visibility is not None
                        and fetch_response.provider_visibility.strip().upper() != "PUBLIC"
                    ):
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.POLICY_BLOCKED,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code="RESOURCE_VISIBILITY_CHANGED",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    raw_bytes = fetch_response.raw_bytes
                    if raw_bytes is None:
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.UNAVAILABLE,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=True,
                                reason_code="EMPTY_PROVIDER_PAYLOAD",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    if (
                        len(raw_bytes) > budget.max_unit_bytes
                        or total_session_bytes > budget.max_total_bytes
                    ):
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.PARTIAL_WITH_LIMIT,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code="BYTE_LIMIT_EXCEEDED",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    # Step ④ Deterministic canonicalization & Revision hash comparison (REF-01, REF-02)
                    canon_result = canonicalize_source_bytes(
                        raw_bytes,
                        file_path=file_path if isinstance(file_path, str) else None,
                        mime_type=fetch_response.mime_type,
                        is_diff=is_diff,
                    )
                    if canon_result.status != "CANONICALIZED" or canon_result.document is None:
                        skip = canon_result.skip_record
                        skip_code = skip.reason_code if skip is not None else None
                        is_limit_skip = skip_code in _LIMIT_SKIP_CODES
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=(
                                    ResolutionStatus.PARTIAL_WITH_LIMIT
                                    if is_limit_skip
                                    else ResolutionStatus.INVALID_LOCATOR
                                ),
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=None,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code=(
                                    f"CANONICALIZATION_{skip_code.value}"
                                    if skip_code is not None
                                    else "CANONICALIZATION_FAILED"
                                ),
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    document = canon_result.document
                    if (
                        budget.max_tokens is not None
                        and _estimate_tokens(document.text) > budget.max_tokens
                    ):
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.PARTIAL_WITH_LIMIT,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=document.metadata.content_sha256,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code="TOKEN_LIMIT_EXCEEDED",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    if document.metadata.content_sha256 != expected_rev_hash:
                        # DATA-01 / REF-02: NEVER overwrite historical revision with drifted bytes
                        return self._finalize(
                            session,
                            access,
                            ResolutionMetadata(
                                status=ResolutionStatus.REVISION_MISMATCH,
                                checked_at=now,
                                operation_id=session.operation_id,
                                source_object_revision_id=target.source_object_revision_id,
                                content_unit_id=target.content_unit_id,
                                expected_revision_hash=expected_rev_hash,
                                observed_revision_hash=document.metadata.content_sha256,
                                expected_unit_hash=expected_unit_hash,
                                observed_unit_hash=None,
                                canonicalization_version=target.canonicalization_version,
                                parser_version=target.parser_version,
                                retryable=False,
                                reason_code="REVISION_HASH_MISMATCH",
                                requests_used=local_requests,
                                bytes_read=local_bytes,
                                deduped=False,
                                fetch_attempted=True,
                            ),
                        )

                    session.put_cached_document(
                        target.source_object_revision_id, expected_rev_hash, document
                    )
                    break
            finally:
                session.release_concurrency_slot()

        assert document is not None

        # Step ⑤ Exact Unit & Span locator verification (REF-01, REF-02)
        if (
            target.anchor.get("anchor_resolution_state") in ("OUTDATED_UNRESOLVED", "STALE_ANCHOR")
            or target.anchor.get("support_eligible") is False
        ):
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.INVALID_LOCATOR,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=document.metadata.content_sha256,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=None,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code="ANCHOR_OUTDATED_OR_INELIGIBLE",
                    requests_used=local_requests,
                    bytes_read=local_bytes,
                    deduped=deduped,
                    fetch_attempted=fetch_attempted,
                ),
            )

        unit_locator = _extract_span_locator_from_anchor(
            target.anchor, canonicalization_version=target.canonicalization_version
        )
        if unit_locator is None:
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.INVALID_LOCATOR,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=document.metadata.content_sha256,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=None,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code="MALFORMED_UNIT_ANCHOR",
                    requests_used=local_requests,
                    bytes_read=local_bytes,
                    deduped=deduped,
                    fetch_attempted=fetch_attempted,
                ),
            )

        unit_verification = extract_verified_span(
            document,
            unit_locator,
            expected_source_content_sha256=expected_rev_hash,
        )
        if not unit_verification.verified or unit_verification.span is None:
            observed_u_hash: str | None = None
            if 0 <= unit_locator.char_start < unit_locator.char_end <= len(document.text):
                observed_u_hash = sha256_prefixed_text(
                    document.text[unit_locator.char_start : unit_locator.char_end]
                )
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.INVALID_LOCATOR,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=document.metadata.content_sha256,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=observed_u_hash,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code=f"UNIT_LOCATOR_{'_'.join(unit_verification.error_codes)}",
                    requests_used=local_requests,
                    bytes_read=local_bytes,
                    deduped=deduped,
                    fetch_attempted=fetch_attempted,
                ),
            )

        observed_unit_hash = sha256_prefixed_text(unit_verification.span.text)
        if (
            unit_locator.span_sha256 != expected_unit_hash
            or observed_unit_hash != expected_unit_hash
        ):
            return self._finalize(
                session,
                access,
                ResolutionMetadata(
                    status=ResolutionStatus.INVALID_LOCATOR,
                    checked_at=now,
                    operation_id=session.operation_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=expected_rev_hash,
                    observed_revision_hash=document.metadata.content_sha256,
                    expected_unit_hash=expected_unit_hash,
                    observed_unit_hash=observed_unit_hash,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                    retryable=False,
                    reason_code="UNIT_HASH_MISMATCH",
                    requests_used=local_requests,
                    bytes_read=local_bytes,
                    deduped=deduped,
                    fetch_attempted=fetch_attempted,
                ),
            )

        # Optional caller-requested sub-span verification within the verified unit
        verified_span = unit_verification.span
        if request.span_locator is not None:
            sub_res = extract_verified_span(
                document,
                request.span_locator,
                expected_source_content_sha256=expected_rev_hash,
            )
            within_unit = (
                request.span_locator.char_start >= unit_locator.char_start
                and request.span_locator.char_end <= unit_locator.char_end
            )
            if not sub_res.verified or sub_res.span is None or not within_unit:
                return self._finalize(
                    session,
                    access,
                    ResolutionMetadata(
                        status=ResolutionStatus.INVALID_LOCATOR,
                        checked_at=now,
                        operation_id=session.operation_id,
                        source_object_revision_id=target.source_object_revision_id,
                        content_unit_id=target.content_unit_id,
                        expected_revision_hash=expected_rev_hash,
                        observed_revision_hash=document.metadata.content_sha256,
                        expected_unit_hash=expected_unit_hash,
                        observed_unit_hash=observed_unit_hash,
                        canonicalization_version=target.canonicalization_version,
                        parser_version=target.parser_version,
                        retryable=False,
                        reason_code=(
                            "SUBSPAN_OUTSIDE_UNIT_BOUNDS"
                            if not within_unit
                            else f"SUBSPAN_LOCATOR_{'_'.join(sub_res.error_codes)}"
                        ),
                        requests_used=local_requests,
                        bytes_read=local_bytes,
                        deduped=deduped,
                        fetch_attempted=fetch_attempted,
                    ),
                )
            verified_span = sub_res.span

        original_locator = _build_original_locator_from_anchor(target.anchor)
        persisted_meta = PersistedContentUnitMetadata(
            unit_id=target.content_unit_id,
            unit_index=target.unit_ordinal,
            unit_type=target.unit_type,
            source_content_sha256=expected_rev_hash,
            locator=unit_locator,
            original_locator=original_locator,
            parser_version=target.parser_version,
            canonicalization_version=target.canonicalization_version,
        )
        ephemeral_unit = EphemeralResolvedUnit(
            metadata=persisted_meta,
            _text=unit_verification.span.text,
        )
        ephemeral_context = (
            self._build_context(
                session, access, target, document, expected_rev_hash, original_locator
            )
            if request.include_context
            else None
        )

        metadata = ResolutionMetadata(
            status=ResolutionStatus.MATCHED,
            checked_at=now,
            operation_id=session.operation_id,
            source_object_revision_id=target.source_object_revision_id,
            content_unit_id=target.content_unit_id,
            expected_revision_hash=expected_rev_hash,
            observed_revision_hash=document.metadata.content_sha256,
            expected_unit_hash=expected_unit_hash,
            observed_unit_hash=observed_unit_hash,
            canonicalization_version=target.canonicalization_version,
            parser_version=target.parser_version,
            retryable=False,
            reason_code=None,
            requests_used=local_requests,
            bytes_read=local_bytes,
            deduped=deduped,
            fetch_attempted=fetch_attempted,
        )
        return self._finalize(
            session,
            access,
            metadata,
            ephemeral_unit=ephemeral_unit,
            ephemeral_span=verified_span,
            ephemeral_context=ephemeral_context,
        )


def verify_required_supports_for_gate(
    session: OperationRefetchSession,
    required_units: Sequence[str | RefetchRequestInput],
    *,
    operation_kind: GateOperationKind = GateOperationKind.APPROVAL,
    resolver: RefetchResolverService | None = None,
    check_before_commit_guard: (
        Callable[[WorkspaceAccessContext, tuple[ResolutionMetadata, ...]], tuple[bool, str | None]]
        | None
    ) = None,
    export_precondition: ExportGatePrecondition | None = None,
    clock: Callable[[], datetime] | None = None,
) -> GateVerificationResult:
    """Enforce Option C §3·5·7 approval, SOURCE_VERIFIED, and Export refetch gates (REF-07).

    Guarantees:
    - Historical ``last_verified_at`` alone NEVER satisfies a gate: every required unit must
      achieve ``ResolutionStatus.MATCHED`` inside the current ``OperationRefetchSession``.
    - Export preconditions (approved claim & exact approved version) are enforced even when
      live refetch succeeds (REF-07f / REF-07g).
    - Pre-commit TOCTOU guard re-checks live workspace/visibility/deletion policy state after
      refetch succeeds and before commit/export emission (REF-07e).
    """
    now = (clock or (lambda: datetime.now(UTC)))()
    if session.closed:
        return GateVerificationResult(
            allowed=False,
            operation_kind=operation_kind,
            operation_id=session.operation_id,
            checked_at=now,
            resolutions=(),
            error=ResolutionErrorMapping(
                http_status=409,
                code="REFETCH_SESSION_CLOSED",
                message="Operation refetch session is closed.",
                retryable=False,
            ),
            reason_code="REFETCH_SESSION_CLOSED",
        )

    if not required_units:
        return GateVerificationResult(
            allowed=False,
            operation_kind=operation_kind,
            operation_id=session.operation_id,
            checked_at=now,
            resolutions=(),
            error=ResolutionErrorMapping(
                http_status=409,
                code="NO_REQUIRED_SUPPORTS",
                message="At least one required support unit is necessary to verify this gate.",
                retryable=False,
            ),
            reason_code="NO_REQUIRED_SUPPORTS",
        )

    # REF-07f / REF-07g export precondition check
    if operation_kind is GateOperationKind.EXPORT_DOWNLOAD and export_precondition is not None:
        if not export_precondition.claim_version_approved:
            return GateVerificationResult(
                allowed=False,
                operation_kind=operation_kind,
                operation_id=session.operation_id,
                checked_at=now,
                resolutions=(),
                error=ResolutionErrorMapping(
                    http_status=409,
                    code="CLAIM_NOT_APPROVED",
                    message="Export requires an explicitly approved claim version.",
                    retryable=False,
                ),
                reason_code="CLAIM_NOT_APPROVED",
            )
        if export_precondition.approved_claim_version != export_precondition.requested_claim_version:
            return GateVerificationResult(
                allowed=False,
                operation_kind=operation_kind,
                operation_id=session.operation_id,
                checked_at=now,
                resolutions=(),
                error=ResolutionErrorMapping(
                    http_status=409,
                    code="APPROVED_CLAIM_VERSION_MISMATCH",
                    message="Requested export claim version does not match the approved claim version.",
                    retryable=False,
                ),
                reason_code="APPROVED_CLAIM_VERSION_MISMATCH",
            )

    collected: list[ResolutionMetadata] = []
    unit_ids: list[str] = []

    for item in required_units:
        if isinstance(item, str):
            unit_id = item
            req = RefetchRequestInput(
                access=session.access,
                content_unit_id=unit_id,
                operation_id=session.operation_id,
                include_context=False,
            )
        else:
            unit_id = item.content_unit_id
            req = item

        unit_ids.append(unit_id)
        existing_match = session.get_matched_metadata(unit_id)
        if existing_match is not None and existing_match.operation_id == session.operation_id:
            collected.append(existing_match)
            continue

        # REF-07d: If no live refetch occurred in this session and no resolver is provided, BLOCK!
        if resolver is None:
            return GateVerificationResult(
                allowed=False,
                operation_kind=operation_kind,
                operation_id=session.operation_id,
                checked_at=now,
                resolutions=tuple(collected),
                error=ResolutionErrorMapping(
                    http_status=409,
                    code="REFETCH_REQUIRED_IN_CURRENT_OPERATION",
                    message="A live refetch in the current operation is required; historical verification is insufficient.",
                    retryable=False,
                ),
                blocked_unit_id=unit_id,
                reason_code="REFETCH_REQUIRED_IN_CURRENT_OPERATION",
            )

        with resolver.resolve_unit(session, req) as resolution:
            meta = resolution.metadata
            collected.append(meta)
            if meta.status is not ResolutionStatus.MATCHED:
                return GateVerificationResult(
                    allowed=False,
                    operation_kind=operation_kind,
                    operation_id=session.operation_id,
                    checked_at=now,
                    resolutions=tuple(collected),
                    error=map_resolution_status_to_error(meta),
                    blocked_unit_id=unit_id,
                    reason_code=meta.reason_code,
                )

    resolutions_tuple = tuple(collected)

    # REF-07e: Live DB policy-fence TOCTOU re-check right before commit
    if resolver is not None:
        for unit_id in unit_ids:
            live_target = resolver.metadata_store.check_policy_fence_current(
                session.access, unit_id
            )
            if live_target is None:
                return GateVerificationResult(
                    allowed=False,
                    operation_kind=operation_kind,
                    operation_id=session.operation_id,
                    checked_at=now,
                    resolutions=resolutions_tuple,
                    error=ResolutionErrorMapping(
                        http_status=403,
                        code="SOURCE_POLICY_BLOCKED",
                        message="Source access or workspace ownership changed before commit.",
                        retryable=False,
                    ),
                    blocked_unit_id=unit_id,
                    reason_code="TOCTOU_POLICY_CHANGED_BEFORE_COMMIT",
                )
            still_allowed, _reason = evaluate_target_policy(live_target)
            if not still_allowed:
                return GateVerificationResult(
                    allowed=False,
                    operation_kind=operation_kind,
                    operation_id=session.operation_id,
                    checked_at=now,
                    resolutions=resolutions_tuple,
                    error=ResolutionErrorMapping(
                        http_status=403,
                        code="SOURCE_POLICY_BLOCKED",
                        message="Source visibility, consent, or deletion state changed before commit.",
                        retryable=False,
                    ),
                    blocked_unit_id=unit_id,
                    reason_code="TOCTOU_POLICY_CHANGED_BEFORE_COMMIT",
                )

    if check_before_commit_guard is not None:
        guard_ok, guard_reason = check_before_commit_guard(session.access, resolutions_tuple)
        if not guard_ok:
            return GateVerificationResult(
                allowed=False,
                operation_kind=operation_kind,
                operation_id=session.operation_id,
                checked_at=now,
                resolutions=resolutions_tuple,
                error=ResolutionErrorMapping(
                    http_status=403,
                    code="SOURCE_POLICY_BLOCKED",
                    message="Pre-commit security or policy guard rejected the operation.",
                    retryable=False,
                ),
                reason_code=guard_reason or "TOCTOU_PRE_COMMIT_GUARD_FAILED",
            )

    return GateVerificationResult(
        allowed=True,
        operation_kind=operation_kind,
        operation_id=session.operation_id,
        checked_at=now,
        resolutions=resolutions_tuple,
        error=None,
        blocked_unit_id=None,
        reason_code=None,
    )
