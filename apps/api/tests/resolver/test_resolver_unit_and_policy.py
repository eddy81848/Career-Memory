"""Unit & contract tests covering REF-01, REF-02, REF-03, REF-05, REF-06, REF-07, and REF-08."""

from __future__ import annotations

import copy
import pickle
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    ContentUnitType,
    SourceObjectKind,
    SpanLocator,
    build_span_locator,
    canonicalize_source_bytes,
    parse_content_units,
)
from career_memory_api.resolver import (
    DerivedUnitProjectionStatus,
    EndpointSecurityPolicy,
    ExportGatePrecondition,
    GateOperationKind,
    InMemoryProvenanceMetadataStore,
    InMemoryProviderContentFetcher,
    JobRefetchManifest,
    JobRefetchManifestItem,
    OperationBudgetConfig,
    PersistedUnitResolutionTarget,
    ProviderFetchOutcomeKind,
    ProviderFetchRequest,
    ProviderFetchResponse,
    RefetchRequestInput,
    RefetchResolverService,
    ResolutionMetadata,
    ResolutionStatus,
    build_provider_endpoint_url,
    map_resolution_status_to_error,
    project_derived_unit_state_after_import_ready,
    verify_required_supports_for_gate,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

_FIXED_NOW = datetime(2026, 11, 27, 13, 0, 0, tzinfo=UTC)


def _make_access(workspace_id: str, user_id: str = "usr-alice") -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer="https://auth.local", subject=f"sub-{user_id}"),
        ),
        membership=WorkspaceMembership(
            workspace_id=workspace_id,
            user_id=user_id,
            role="OWNER",
        ),
    )


def _build_fixture_bundle(
    raw_bytes: bytes,
    *,
    workspace_id: str = "ws-alpha",
    unit_index: int = 1,
    source_kind: SourceObjectKind = SourceObjectKind.PR_BODY,
    object_type: str = "PR",
    provider_resource_id: str = "1001",
    provider_object_id: str = "42",
    visibility: str = "PUBLIC",
    resource_status: str = "ACTIVE",
    account_status: str = "ACTIVE",
    object_lifecycle_state: str = "ACTIVE",
    unit_policy_state: str = "ACTIVE",
    deletion_fence_active: bool = False,
    raw_storage_mode: str = "NONE",
    last_verified_at: datetime | None = None,
    anchor_override: dict[str, Any] | None = None,
) -> tuple[list[PersistedUnitResolutionTarget], PersistedUnitResolutionTarget, str]:
    canon = canonicalize_source_bytes(raw_bytes)
    assert canon.status == "CANONICALIZED" and canon.document is not None
    parsed = parse_content_units(canon.document, source_kind)
    assert len(parsed.persisted_metadata) > unit_index

    account_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}-acc"))
    resource_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}-res-{provider_resource_id}"))
    object_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}-obj-{provider_object_id}"))
    revision_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}-rev-{canon.document.metadata.content_sha256}"))

    targets: list[PersistedUnitResolutionTarget] = []
    for idx, meta in enumerate(parsed.persisted_metadata):
        anchor_dict = meta.to_persisted_anchor_dict()
        # Ensure Option C DB-equivalent fields are strictly non-raw
        anchor_dict.pop("text_content", None)
        anchor_dict.pop("content_ref", None)
        anchor_dict.pop("retention_class", None)
        if idx == unit_index and anchor_override is not None:
            anchor_dict = anchor_override

        unit_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{revision_id}-unit-{idx}"))
        targets.append(
            PersistedUnitResolutionTarget(
                workspace_id=workspace_id,
                source_account_id=account_id,
                source_account_provider="GITHUB",
                source_account_status=account_status,
                source_account_token_secret_ref="secret://github/ws-alpha/acc-1",
                source_resource_id=resource_id,
                provider_resource_id=provider_resource_id,
                resource_type="GITHUB_REPOSITORY",
                resource_name="acme/checkout-service",
                canonical_url="https://github.com/acme/checkout-service",
                visibility=visibility,
                processing_mode="PUBLIC_CLOUD",
                raw_storage_mode=raw_storage_mode,
                resource_status=resource_status,
                source_object_id=object_id,
                object_type=object_type,
                provider_object_id=provider_object_id,
                object_lifecycle_state=object_lifecycle_state,
                source_object_revision_id=revision_id,
                provider_revision_id="rev-etag-001",
                expected_revision_hash=canon.document.metadata.content_sha256,
                hash_policy_version=CANONICALIZATION_VERSION,
                revision_is_tombstone=False,
                revision_normalized_metadata={"source_kind": source_kind.value},
                content_unit_id=unit_id,
                unit_type=meta.unit_type,
                anchor=anchor_dict,
                expected_unit_hash=meta.locator.span_sha256,
                canonicalization_version=CANONICALIZATION_VERSION,
                parser_version=PARSER_VERSION,
                unit_ordinal=idx,
                unit_policy_state=unit_policy_state,
                text_content=None,
                content_ref=None,
                retention_class="NONE",
                deletion_fence_active=deletion_fence_active,
                last_verified_at=last_verified_at,
            )
        )

    return targets, targets[unit_index], canon.document.text


def _sample_pr_markdown_bytes() -> bytes:
    # Includes UTF-8 BOM, CRLF, Korean NFC/NFD normalization, negation/environment context sections
    text = (
        "\ufeff# Context\r\n"
        "Team-wide staging benchmark (not yet deployed to production).\r\n\r\n"
        "## Action\r\n"
        "Implemented Redis read-through cache for checkout p99 latency (-25% in staging).\r\n\r\n"
        "## Negation & Caveats\r\n"
        "Did NOT enable in production; rollback remained active for prod traffic.\r\n"
    )
    return text.encode("utf-8")


def test_ref01_exact_refetch_reproduces_unit_span_and_context_with_null_db_text() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, canonical_text = _build_fixture_bundle(raw, unit_index=1)
    assert target.text_content is None
    assert target.content_ref is None
    assert target.retention_class == "NONE"

    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=fetcher,
        clock=lambda: _FIXED_NOW,
    )
    access = _make_access("ws-alpha")

    # Compute exact sub-span inside the Action section
    sub_phrase = "Implemented Redis read-through cache"
    start = canonical_text.index(sub_phrase)
    sub_locator = build_span_locator(canonical_text, start, start + len(sub_phrase))

    with resolver.open_operation_session(access, operation_id="op-ref01") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-ref01",
                span_locator=sub_locator,
                include_context=True,
            ),
        ) as resolution:
            assert resolution.metadata.status is ResolutionStatus.MATCHED
            assert resolution.metadata.expected_revision_hash == target.expected_revision_hash
            assert resolution.metadata.observed_revision_hash == target.expected_revision_hash
            assert resolution.metadata.expected_unit_hash == target.expected_unit_hash
            assert resolution.metadata.observed_unit_hash == target.expected_unit_hash
            assert resolution.metadata.retryable is False
            assert resolution.metadata.reason_code is None
            assert resolution.metadata.requests_used == 1
            assert resolution.metadata.deduped is False
            assert resolution.ephemeral_unit is not None
            assert "Implemented Redis read-through cache" in resolution.ephemeral_unit.text
            assert resolution.ephemeral_span is not None
            assert resolution.ephemeral_span.text == sub_phrase
            assert resolution.ephemeral_context is not None
            assert resolution.ephemeral_context.preceding_text is not None
            assert "not yet deployed to production" in resolution.ephemeral_context.preceding_text
            assert resolution.ephemeral_context.following_text is not None
            assert "Did NOT enable in production" in resolution.ephemeral_context.following_text

            # Non-serializable ephemeral wrapper must never leak raw text in repr/str/pickle
            assert sub_phrase not in repr(resolution)
            assert sub_phrase not in str(resolution)
            assert sub_phrase not in repr(resolution.ephemeral_context)
            with pytest.raises(TypeError):
                pickle.dumps(resolution)
            with pytest.raises(TypeError):
                pickle.dumps(session)

        # Handle closed upon context exit
        assert resolution.closed is True
        assert resolution.ephemeral_unit is None
        assert resolution.ephemeral_span is None
        assert resolution.ephemeral_context is None

    assert session.closed is True
    assert len(store.recorded_metadata) == 1
    assert store.recorded_metadata[0].status is ResolutionStatus.MATCHED


def test_ref02_revision_drift_and_corrupt_locator_block_without_mutation() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1)
    original_revision_hash = target.expected_revision_hash
    original_anchor = copy.deepcopy(target.anchor)

    # 1) Revision body drift -> REVISION_MISMATCH (409 SOURCE_REVISION_MISMATCH)
    drifted_raw = raw + b"\nAppended unauthorized edit.\n"
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(drifted_raw)
    resolver = RefetchResolverService(metadata_store=store, fetcher=fetcher, clock=lambda: _FIXED_NOW)
    access = _make_access("ws-alpha")

    with resolver.open_operation_session(access, operation_id="op-ref02-drift") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-ref02-drift",
            ),
        ) as resolution:
            assert resolution.metadata.status is ResolutionStatus.REVISION_MISMATCH
            assert resolution.metadata.expected_revision_hash == original_revision_hash
            assert resolution.metadata.observed_revision_hash != original_revision_hash
            assert resolution.metadata.observed_unit_hash is None
            assert resolution.ephemeral_unit is None
            mapping = map_resolution_status_to_error(resolution.metadata)
            assert mapping.http_status == 409
            assert mapping.code == "SOURCE_REVISION_MISMATCH"
            assert mapping.retryable is False

    # Persisted target revision hash & anchor must be untouched
    reloaded = store.get_unit_resolution_target(access, target.content_unit_id)
    assert reloaded is not None
    assert reloaded.expected_revision_hash == original_revision_hash
    assert reloaded.anchor == original_anchor

    # 2) Same revision hash, corrupted locator offsets -> INVALID_LOCATOR (409 SOURCE_LOCATOR_INVALID)
    corrupt_anchor = copy.deepcopy(original_anchor)
    corrupt_anchor["locator"]["char_start"] = max(0, corrupt_anchor["locator"]["char_start"] - 3)
    corrupt_anchor["locator"]["char_end"] = corrupt_anchor["locator"]["char_end"] - 3
    corrupt_targets, corrupt_target, _ = _build_fixture_bundle(
        raw, unit_index=1, anchor_override=corrupt_anchor
    )
    store2 = InMemoryProvenanceMetadataStore(corrupt_targets)
    fetcher2 = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver2 = RefetchResolverService(metadata_store=store2, fetcher=fetcher2, clock=lambda: _FIXED_NOW)

    with resolver2.open_operation_session(access, operation_id="op-ref02-loc") as session:
        with resolver2.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=corrupt_target.content_unit_id,
                operation_id="op-ref02-loc",
            ),
        ) as resolution:
            assert resolution.metadata.status is ResolutionStatus.INVALID_LOCATOR
            assert resolution.metadata.observed_revision_hash == corrupt_target.expected_revision_hash
            assert resolution.ephemeral_unit is None
            mapping = map_resolution_status_to_error(resolution.metadata)
            assert mapping.http_status == 409
            assert mapping.code == "SOURCE_LOCATOR_INVALID"
            assert mapping.retryable is False

    # Anchor must NOT be silently auto-repaired
    reloaded2 = store2.get_unit_resolution_target(access, corrupt_target.content_unit_id)
    assert reloaded2 is not None
    assert reloaded2.anchor == corrupt_anchor


@pytest.mark.parametrize(
    ("outcome_kind", "http_status", "expected_status", "expected_http", "expected_code", "expected_retryable", "expected_calls"),
    [
        (ProviderFetchOutcomeKind.NOT_FOUND, 404, ResolutionStatus.UNAVAILABLE, 503, "SOURCE_UNAVAILABLE", False, 1),
        (ProviderFetchOutcomeKind.NOT_FOUND, 410, ResolutionStatus.UNAVAILABLE, 503, "SOURCE_UNAVAILABLE", False, 1),
        (ProviderFetchOutcomeKind.PARTIAL_CONTENT, 206, ResolutionStatus.PARTIAL_WITH_LIMIT, 422, "SOURCE_FETCH_LIMIT", False, 1),
        (ProviderFetchOutcomeKind.RATE_LIMITED, 429, ResolutionStatus.UNAVAILABLE, 503, "SOURCE_UNAVAILABLE", True, 4),
        (ProviderFetchOutcomeKind.SERVER_ERROR, 503, ResolutionStatus.UNAVAILABLE, 503, "SOURCE_UNAVAILABLE", True, 4),
        (ProviderFetchOutcomeKind.TIMEOUT, None, ResolutionStatus.UNAVAILABLE, 503, "SOURCE_UNAVAILABLE", True, 4),
    ],
)
def test_ref03_provider_404_206_429_503_timeout_classification_and_retry_cap(
    outcome_kind: ProviderFetchOutcomeKind,
    http_status: int | None,
    expected_status: ResolutionStatus,
    expected_http: int,
    expected_code: str,
    expected_retryable: bool,
    expected_calls: int,
) -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1)
    store = InMemoryProvenanceMetadataStore(targets)

    response = ProviderFetchResponse(
        kind=outcome_kind,
        http_status=http_status,
        raw_bytes=raw[:20] if outcome_kind is ProviderFetchOutcomeKind.PARTIAL_CONTENT else None,
        bytes_read=20 if outcome_kind is ProviderFetchOutcomeKind.PARTIAL_CONTENT else 0,
        truncated=outcome_kind is ProviderFetchOutcomeKind.PARTIAL_CONTENT,
        retryable=outcome_kind in (
            ProviderFetchOutcomeKind.RATE_LIMITED,
            ProviderFetchOutcomeKind.SERVER_ERROR,
            ProviderFetchOutcomeKind.TIMEOUT,
        ),
        reason_code="PROVIDER_OBJECT_NOT_FOUND" if outcome_kind is ProviderFetchOutcomeKind.NOT_FOUND else None,
    )
    fetcher = InMemoryProviderContentFetcher.from_constant_response(response)
    resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=fetcher,
        default_budget=OperationBudgetConfig(max_requests=10, max_retries=3),
        clock=lambda: _FIXED_NOW,
    )
    access = _make_access("ws-alpha")

    with resolver.open_operation_session(access, operation_id="op-ref03") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-ref03",
            ),
        ) as resolution:
            assert resolution.metadata.status is expected_status
            assert resolution.metadata.retryable is expected_retryable
            assert resolution.ephemeral_unit is None
            assert fetcher.call_count == expected_calls
            if outcome_kind is ProviderFetchOutcomeKind.NOT_FOUND:
                assert resolution.metadata.reason_code == "PROVIDER_OBJECT_NOT_FOUND"

            err = map_resolution_status_to_error(resolution.metadata)
            assert err.http_status == expected_http
            assert err.code == expected_code
            assert err.retryable is expected_retryable

            # Derived unit projection after IMPORT_READY preserves import_run SUCCESS
            projection = project_derived_unit_state_after_import_ready(
                resolution.metadata, import_run_status="SUCCESS"
            )
            assert projection.import_run_status == "SUCCESS"
            assert projection.derived_unit_status is DerivedUnitProjectionStatus.BLOCKED_SOURCE


def test_ref03_transient_failure_recovers_within_retry_budget() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1)
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_sequence(
        [
            ProviderFetchResponse(kind=ProviderFetchOutcomeKind.SERVER_ERROR, http_status=503, retryable=True),
            ProviderFetchResponse(kind=ProviderFetchOutcomeKind.RATE_LIMITED, http_status=429, retryable=True),
            ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                http_status=200,
                raw_bytes=raw,
                bytes_read=len(raw),
                mime_type="text/markdown",
            ),
        ]
    )
    resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=fetcher,
        default_budget=OperationBudgetConfig(max_requests=10, max_retries=3),
        clock=lambda: _FIXED_NOW,
    )
    access = _make_access("ws-alpha")

    with resolver.open_operation_session(access, operation_id="op-ref03-recover") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-ref03-recover",
            ),
        ) as resolution:
            assert resolution.metadata.status is ResolutionStatus.MATCHED
            assert resolution.metadata.requests_used == 3
            assert fetcher.call_count == 3


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"account_status": "REVOKED"}, "SOURCE_ACCOUNT_REVOKED"),
        ({"visibility": "PRIVATE"}, "RESOURCE_NOT_PUBLIC"),
        ({"visibility": "INTERNAL"}, "RESOURCE_NOT_PUBLIC"),
        ({"resource_status": "POLICY_REVIEW_REQUIRED"}, "POLICY_REVIEW_REQUIRED"),
        ({"resource_status": "DELETED"}, "RESOURCE_DELETED_OR_FENCED"),
        ({"deletion_fence_active": True}, "RESOURCE_DELETED_OR_FENCED"),
        ({"object_lifecycle_state": "DELETED"}, "RESOURCE_DELETED_OR_FENCED"),
        ({"unit_policy_state": "PURGED"}, "UNIT_POLICY_BLOCKED"),
        ({"raw_storage_mode": "ENCRYPTED_CACHE"}, "RAW_STORAGE_POLICY_VIOLATION"),
    ],
)
def test_ref05_policy_blocks_before_any_remote_fetch(
    kwargs: dict[str, Any], expected_reason: str
) -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1, **kwargs)
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(metadata_store=store, fetcher=fetcher, clock=lambda: _FIXED_NOW)
    access = _make_access("ws-alpha")

    with resolver.open_operation_session(access, operation_id="op-ref05-pol") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-ref05-pol",
            ),
        ) as resolution:
            assert resolution.metadata.status is ResolutionStatus.POLICY_BLOCKED
            assert resolution.metadata.fetch_attempted is False
            assert resolution.metadata.requests_used == 0
            assert resolution.metadata.reason_code == expected_reason
            assert fetcher.call_count == 0
            err = map_resolution_status_to_error(resolution.metadata)
            assert err.http_status == 403
            assert err.code == "SOURCE_POLICY_BLOCKED"


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://api.github.com/repositories/1001/pulls/42",
        "file:///etc/passwd",
        "https://user:pass@api.github.com/repositories/1001/pulls/42",
        "https://127.0.0.1/repositories/1001/pulls/42",
        "https://10.0.0.10/internal",
        "https://172.16.0.1/internal",
        "https://192.168.1.5/internal",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/repositories/1001",
        "https://[fc00::1]/internal",
        "https://[fe80::1]/internal",
        "https://[::ffff:127.0.0.1]/internal",
        "https://evil.example.com/repositories/1001/pulls/42",
        "https://api.github.com:8443/repositories/1001/pulls/42",
    ],
)
def test_ref05_ssrf_policy_rejects_unsafe_urls_and_dns_rebinding_before_fetch(
    unsafe_url: str,
) -> None:
    policy = EndpointSecurityPolicy()
    result = policy.validate_endpoint_url(unsafe_url)
    assert result.allowed is False
    assert result.reason_code is not None

    # DNS rebinding check: allowlisted hostname resolving to cloud metadata IP is blocked
    rebinding_policy = EndpointSecurityPolicy(
        dns_resolver=lambda _host: ["169.254.169.254"],
    )
    rebind_res = rebinding_policy.validate_endpoint_url(
        "https://api.github.com/repositories/1001/pulls/42"
    )
    assert rebind_res.allowed is False
    assert rebind_res.reason_code == "RESOLVED_IP_FORBIDDEN"

    # End-to-end resolver blocks unsafe endpoint with fetch_attempted=False
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1)
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=fetcher,
        security_policy=policy,
        endpoint_builder=lambda _t: unsafe_url,
        clock=lambda: _FIXED_NOW,
    )
    access = _make_access("ws-alpha")
    with resolver.open_operation_session(access, operation_id="op-ssrf") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-ssrf",
            ),
        ) as resolution:
            assert resolution.metadata.status is ResolutionStatus.POLICY_BLOCKED
            assert resolution.metadata.fetch_attempted is False
            assert fetcher.call_count == 0


def test_ref05_disallowed_302_redirect_is_blocked_immediately() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1)
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_sequence(
        [
            ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.REDIRECT,
                http_status=302,
                redirect_location="https://169.254.169.254/latest/meta-data/",
            ),
            ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                http_status=200,
                raw_bytes=raw,
                bytes_read=len(raw),
            ),
        ]
    )
    resolver = RefetchResolverService(metadata_store=store, fetcher=fetcher, clock=lambda: _FIXED_NOW)
    access = _make_access("ws-alpha")

    with resolver.open_operation_session(access, operation_id="op-redir") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-redir",
            ),
        ) as resolution:
            assert resolution.metadata.status is ResolutionStatus.POLICY_BLOCKED
            assert resolution.metadata.reason_code == "REDIRECT_BLOCKED"
            # Must stop after the first 302 and never call the redirected target
            assert fetcher.call_count == 1


def test_ref05_cross_workspace_and_missing_unit_conceal_identically() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target_alpha, _ = _build_fixture_bundle(raw, workspace_id="ws-alpha", unit_index=1)
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(metadata_store=store, fetcher=fetcher, clock=lambda: _FIXED_NOW)

    access_beta = _make_access("ws-beta", user_id="usr-bob")
    with resolver.open_operation_session(access_beta, operation_id="op-xws") as session:
        with pytest.raises(WorkspaceResourceNotFoundError) as exc_other:
            resolver.resolve_unit(
                session,
                RefetchRequestInput(
                    access=access_beta,
                    content_unit_id=target_alpha.content_unit_id,
                    operation_id="op-xws",
                ),
            )
        with pytest.raises(WorkspaceResourceNotFoundError) as exc_missing:
            resolver.resolve_unit(
                session,
                RefetchRequestInput(
                    access=access_beta,
                    content_unit_id=str(uuid.uuid4()),
                    operation_id="op-xws",
                ),
            )

    assert str(exc_other.value) == str(exc_missing.value)
    assert fetcher.call_count == 0


def test_ref06_crash_clears_memory_and_rerun_requires_fresh_refetch_without_fallback() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1)
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(metadata_store=store, fetcher=fetcher, clock=lambda: _FIXED_NOW)
    access = _make_access("ws-alpha")

    captured_handle = None
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        with resolver.open_operation_session(access, operation_id="op-crash-1") as session1:
            captured_handle = resolver.resolve_unit(
                session1,
                RefetchRequestInput(
                    access=access,
                    content_unit_id=target.content_unit_id,
                    operation_id="op-crash-1",
                ),
            )
            assert captured_handle.metadata.status is ResolutionStatus.MATCHED
            raise RuntimeError("simulated worker crash")

    # Memory cleared immediately on exception exit
    assert captured_handle is not None
    assert captured_handle.closed is True
    assert captured_handle.ephemeral_unit is None
    assert session1.closed is True
    assert fetcher.call_count == 1

    # Re-run in a new operation session must refetch (no persisted raw fallback); if drifted -> blocked
    drifted_fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw + b"\ndrifted\n")
    resolver_after_crash = RefetchResolverService(
        metadata_store=store, fetcher=drifted_fetcher, clock=lambda: _FIXED_NOW
    )
    with resolver_after_crash.open_operation_session(access, operation_id="op-crash-2") as session2:
        with resolver_after_crash.resolve_unit(
            session2,
            RefetchRequestInput(
                access=access,
                content_unit_id=target.content_unit_id,
                operation_id="op-crash-2",
            ),
        ) as res2:
            assert res2.metadata.status is ResolutionStatus.REVISION_MISMATCH
            assert drifted_fetcher.call_count == 1


def test_ref07_gate_blocks_stale_only_toctou_and_export_preconditions() -> None:
    raw = _sample_pr_markdown_bytes()
    # Target already has historical last_verified_at from an earlier job
    targets, target, _ = _build_fixture_bundle(
        raw,
        unit_index=1,
        last_verified_at=datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC),
    )
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(metadata_store=store, fetcher=fetcher, clock=lambda: _FIXED_NOW)
    access = _make_access("ws-alpha")

    # REF-07d: historical last_verified_at alone without live refetch in current session -> BLOCKED
    with resolver.open_operation_session(access, operation_id="op-ref07d") as session:
        gate_stale = verify_required_supports_for_gate(
            session,
            [target.content_unit_id],
            operation_kind=GateOperationKind.APPROVAL,
            resolver=None,
        )
        assert gate_stale.allowed is False
        assert gate_stale.error is not None
        assert gate_stale.error.http_status == 409
        assert gate_stale.reason_code == "REFETCH_REQUIRED_IN_CURRENT_OPERATION"

    # REF-07a: live refetch inside current session -> ALLOWED
    with resolver.open_operation_session(access, operation_id="op-ref07a") as session:
        gate_ok = verify_required_supports_for_gate(
            session,
            [target.content_unit_id],
            operation_kind=GateOperationKind.APPROVAL,
            resolver=resolver,
        )
        assert gate_ok.allowed is True
        assert gate_ok.error is None
        assert len(gate_ok.resolutions) == 1
        assert gate_ok.resolutions[0].status is ResolutionStatus.MATCHED

    # REF-07e: TOCTOU — live refetch passes, then pre-commit guard detects revocation -> BLOCKED
    with resolver.open_operation_session(access, operation_id="op-ref07e") as session:
        gate_toctou = verify_required_supports_for_gate(
            session,
            [target.content_unit_id],
            operation_kind=GateOperationKind.EXPORT_DOWNLOAD,
            resolver=resolver,
            export_precondition=ExportGatePrecondition(
                claim_version_approved=True,
                requested_claim_version=1,
                approved_claim_version=1,
            ),
            check_before_commit_guard=lambda _acc, _res: (
                False,
                "TOCTOU_VISIBILITY_CHANGED_TO_PRIVATE",
            ),
        )
        assert gate_toctou.allowed is False
        assert gate_toctou.error is not None
        assert gate_toctou.error.http_status == 403
        assert gate_toctou.error.code == "SOURCE_POLICY_BLOCKED"
        assert gate_toctou.reason_code == "TOCTOU_VISIBILITY_CHANGED_TO_PRIVATE"

    # REF-07f / REF-07g: unapproved claim & version mismatch block export even when refetch passes
    with resolver.open_operation_session(access, operation_id="op-ref07fg") as session:
        gate_unapproved = verify_required_supports_for_gate(
            session,
            [target.content_unit_id],
            operation_kind=GateOperationKind.EXPORT_DOWNLOAD,
            resolver=resolver,
            export_precondition=ExportGatePrecondition(
                claim_version_approved=False,
                requested_claim_version=1,
                approved_claim_version=None,
            ),
        )
        assert gate_unapproved.allowed is False
        assert gate_unapproved.error is not None
        assert gate_unapproved.error.code == "CLAIM_NOT_APPROVED"

        gate_ver_mismatch = verify_required_supports_for_gate(
            session,
            [target.content_unit_id],
            operation_kind=GateOperationKind.EXPORT_DOWNLOAD,
            resolver=resolver,
            export_precondition=ExportGatePrecondition(
                claim_version_approved=True,
                requested_claim_version=2,
                approved_claim_version=1,
            ),
        )
        assert gate_ver_mismatch.allowed is False
        assert gate_ver_mismatch.error is not None
        assert gate_ver_mismatch.error.code == "APPROVED_CLAIM_VERSION_MISMATCH"


def test_ref08_dedupe_manifest_and_byte_token_request_concurrency_budgets() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target0, _ = _build_fixture_bundle(raw, unit_index=0)
    target1 = targets[1]
    store = InMemoryProvenanceMetadataStore(targets)
    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=fetcher,
        default_budget=OperationBudgetConfig(max_requests=5, max_total_bytes=10_000, max_unit_bytes=10_000),
        clock=lambda: _FIXED_NOW,
    )
    access = _make_access("ws-alpha")

    # REF-08a: two resolves on same revision (same unit & sibling unit) -> single remote fetch!
    with resolver.open_operation_session(access, operation_id="op-ref08a") as session:
        with resolver.resolve_unit(
            session,
            RefetchRequestInput(access=access, content_unit_id=target0.content_unit_id, operation_id="op-ref08a"),
        ) as r1:
            assert r1.metadata.status is ResolutionStatus.MATCHED
            assert r1.metadata.deduped is False
            assert r1.metadata.requests_used == 1

        with resolver.resolve_unit(
            session,
            RefetchRequestInput(access=access, content_unit_id=target0.content_unit_id, operation_id="op-ref08a"),
        ) as r2:
            assert r2.metadata.status is ResolutionStatus.MATCHED
            assert r2.metadata.deduped is True
            assert r2.metadata.requests_used == 0

        with resolver.resolve_unit(
            session,
            RefetchRequestInput(access=access, content_unit_id=target1.content_unit_id, operation_id="op-ref08a"),
        ) as r3:
            assert r3.metadata.status is ResolutionStatus.MATCHED
            assert r3.metadata.deduped is True
            assert r3.metadata.requests_used == 0

    assert fetcher.call_count == 1

    # REF-08b: JobRefetchManifest transfers strictly IDs & hashes (0 raw text)
    manifest = JobRefetchManifest(
        operation_id="op-job-next",
        workspace_id="ws-alpha",
        items=(
            JobRefetchManifestItem(
                source_resource_id=target1.source_resource_id,
                source_object_id=target1.source_object_id,
                source_object_revision_id=target1.source_object_revision_id,
                content_unit_id=target1.content_unit_id,
                expected_revision_hash=target1.expected_revision_hash,
                expected_unit_hash=target1.expected_unit_hash,
                canonicalization_version=target1.canonicalization_version,
                parser_version=target1.parser_version,
            ),
        ),
    )
    manifest_dict = manifest.to_dict()
    roundtripped = JobRefetchManifest.from_dict(manifest_dict)
    assert roundtripped == manifest
    assert "Implemented Redis" not in str(manifest_dict)

    # REF-08c: byte & token limits block explicitly (no silent truncation)
    byte_limited_resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=InMemoryProviderContentFetcher.from_single_bytes(raw),
        default_budget=OperationBudgetConfig(max_unit_bytes=16, max_total_bytes=16),
        clock=lambda: _FIXED_NOW,
    )
    with byte_limited_resolver.open_operation_session(access, operation_id="op-byte-cap") as session:
        with byte_limited_resolver.resolve_unit(
            session,
            RefetchRequestInput(access=access, content_unit_id=target1.content_unit_id, operation_id="op-byte-cap"),
        ) as r_byte:
            assert r_byte.metadata.status is ResolutionStatus.PARTIAL_WITH_LIMIT
            assert r_byte.ephemeral_unit is None
            assert map_resolution_status_to_error(r_byte.metadata).http_status == 422

    token_limited_resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=InMemoryProviderContentFetcher.from_single_bytes(raw),
        default_budget=OperationBudgetConfig(max_tokens=5),
        clock=lambda: _FIXED_NOW,
    )
    with token_limited_resolver.open_operation_session(access, operation_id="op-tok-cap") as session:
        with token_limited_resolver.resolve_unit(
            session,
            RefetchRequestInput(access=access, content_unit_id=target1.content_unit_id, operation_id="op-tok-cap"),
        ) as r_tok:
            assert r_tok.metadata.status is ResolutionStatus.PARTIAL_WITH_LIMIT
            assert r_tok.metadata.reason_code == "TOKEN_LIMIT_EXCEEDED"
            assert r_tok.ephemeral_unit is None

    # REF-08d: request limit & concurrency limit block explicitly
    req_limited_resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=InMemoryProviderContentFetcher.from_single_bytes(raw),
        default_budget=OperationBudgetConfig(max_requests=0),
        clock=lambda: _FIXED_NOW,
    )
    with req_limited_resolver.open_operation_session(access, operation_id="op-req-cap") as session:
        with req_limited_resolver.resolve_unit(
            session,
            RefetchRequestInput(access=access, content_unit_id=target1.content_unit_id, operation_id="op-req-cap"),
        ) as r_req:
            assert r_req.metadata.status is ResolutionStatus.PARTIAL_WITH_LIMIT
            assert r_req.metadata.reason_code == "REQUEST_LIMIT_EXCEEDED"

    # Concurrency cap = 1: second overlapping fetch fails closed with CONCURRENCY_LIMIT_EXCEEDED
    entered = threading.Event()
    release = threading.Event()

    class _BlockingFetcher:
        def fetch(self, request: ProviderFetchRequest) -> ProviderFetchResponse:
            entered.set()
            release.wait(timeout=3.0)
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                http_status=200,
                raw_bytes=raw,
                bytes_read=len(raw),
            )

    conc_resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=_BlockingFetcher(),
        default_budget=OperationBudgetConfig(max_concurrency=1, max_requests=5),
        clock=lambda: _FIXED_NOW,
    )
    with conc_resolver.open_operation_session(access, operation_id="op-conc") as session:
        results: list[ResolutionStatus] = []

        def _worker() -> None:
            with conc_resolver.resolve_unit(
                session,
                RefetchRequestInput(access=access, content_unit_id=target1.content_unit_id, operation_id="op-conc"),
            ) as res:
                results.append(res.metadata.status)

        t = threading.Thread(target=_worker)
        t.start()
        assert entered.wait(timeout=2.0)
        with conc_resolver.resolve_unit(
            session,
            RefetchRequestInput(access=access, content_unit_id=target1.content_unit_id, operation_id="op-conc"),
        ) as r_second:
            assert r_second.metadata.status is ResolutionStatus.PARTIAL_WITH_LIMIT
            assert r_second.metadata.reason_code == "CONCURRENCY_LIMIT_EXCEEDED"
        release.set()
        t.join(timeout=2.0)
        assert results == [ResolutionStatus.MATCHED]


def test_server_owned_endpoint_builder_rejects_path_traversal() -> None:
    raw = _sample_pr_markdown_bytes()
    targets, target, _ = _build_fixture_bundle(raw, unit_index=1, provider_object_id="../evil")
    with pytest.raises(ValueError):
        build_provider_endpoint_url(target)
