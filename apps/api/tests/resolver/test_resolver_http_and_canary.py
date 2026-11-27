"""HTTP contract tests for GET /v1/content-units/{id}/content and REF-04 canary leak audit."""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.authz import AuthenticatedUser, AuthSubject, WorkspaceMembership
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    SourceObjectKind,
    canonicalize_source_bytes,
    parse_content_units,
)
from career_memory_api.resolver import (
    InMemoryProvenanceMetadataStore,
    InMemoryProviderContentFetcher,
    JobRefetchManifest,
    JobRefetchManifestItem,
    OperationBudgetConfig,
    PersistedUnitResolutionTarget,
    ProviderFetchOutcomeKind,
    ProviderFetchResponse,
    RefetchResolverService,
    mount_resolver_routes,
)
from tests.synthetic_access import SyntheticWorkspaceAccessProvider

_CANARY_PRIMARY = "CANARY_RAW_SOURCE_DO_NOT_PERSIST_9F3A7C"
_CANARY_FAILURE = "CANARY_FAILURE_PATH_ZERO_LEAK_4E8B2D"
_FIXED_NOW = datetime(2026, 11, 27, 14, 0, 0, tzinfo=UTC)


def _make_provider() -> SyntheticWorkspaceAccessProvider:
    alice = AuthenticatedUser(
        user_id="usr-alice",
        auth_subject=AuthSubject(issuer="https://auth.local", subject="sub-alice"),
    )
    bob = AuthenticatedUser(
        user_id="usr-bob",
        auth_subject=AuthSubject(issuer="https://auth.local", subject="sub-bob"),
    )
    return SyntheticWorkspaceAccessProvider(
        users_by_token={"tok-alice": alice, "tok-bob": bob},
        memberships_by_pair={
            ("usr-alice", "ws-alpha"): WorkspaceMembership(
                workspace_id="ws-alpha", user_id="usr-alice", role="OWNER"
            ),
            ("usr-bob", "ws-beta"): WorkspaceMembership(
                workspace_id="ws-beta", user_id="usr-bob", role="OWNER"
            ),
        },
    )


def _make_targets(
    raw_bytes: bytes,
    *,
    workspace_id: str = "ws-alpha",
    visibility: str = "PUBLIC",
    resource_status: str = "ACTIVE",
    resource_salt: str = "default",
) -> tuple[list[PersistedUnitResolutionTarget], PersistedUnitResolutionTarget]:
    canon = canonicalize_source_bytes(raw_bytes)
    assert canon.status == "CANONICALIZED" and canon.document is not None
    parsed = parse_content_units(canon.document, SourceObjectKind.PR_BODY)
    revision_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"{workspace_id}-{resource_salt}-rev-{canon.document.metadata.content_sha256}",
        )
    )

    targets: list[PersistedUnitResolutionTarget] = []
    for idx, meta in enumerate(parsed.persisted_metadata):
        anchor = meta.to_persisted_anchor_dict()
        anchor.pop("text_content", None)
        anchor.pop("content_ref", None)
        anchor.pop("retention_class", None)
        targets.append(
            PersistedUnitResolutionTarget(
                workspace_id=workspace_id,
                source_account_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}-acc")),
                source_account_provider="GITHUB",
                source_account_status="ACTIVE",
                source_account_token_secret_ref="secret://github/ws-alpha/acc",
                source_resource_id=str(
                    uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}-{resource_salt}-res")
                ),
                provider_resource_id="1001",
                resource_type="GITHUB_REPOSITORY",
                resource_name="acme/service",
                canonical_url="https://github.com/acme/service",
                visibility=visibility,
                processing_mode="PUBLIC_CLOUD",
                raw_storage_mode="NONE",
                resource_status=resource_status,
                source_object_id=str(
                    uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}-{resource_salt}-obj")
                ),
                object_type="PR",
                provider_object_id="99",
                object_lifecycle_state="ACTIVE",
                source_object_revision_id=revision_id,
                provider_revision_id="rev-1",
                expected_revision_hash=canon.document.metadata.content_sha256,
                hash_policy_version=CANONICALIZATION_VERSION,
                revision_is_tombstone=False,
                revision_normalized_metadata={"source_kind": "PR_BODY"},
                content_unit_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{revision_id}-cu-{idx}")),
                unit_type=meta.unit_type,
                anchor=anchor,
                expected_unit_hash=meta.locator.span_sha256,
                canonicalization_version=CANONICALIZATION_VERSION,
                parser_version=PARSER_VERSION,
                unit_ordinal=idx,
                unit_policy_state="ACTIVE",
            )
        )
    return targets, targets[1]


def test_http_get_content_unit_content_success_errors_and_no_store_headers() -> None:
    raw = (
        f"# Summary\nBackground before main change.\n\n"
        f"## Details\nCore body with {_CANARY_PRIMARY} token.\n\n"
        f"## Caveats\nStaging only, not production.\n"
    ).encode("utf-8")
    targets_alpha, target_alpha = _make_targets(
        raw, workspace_id="ws-alpha", resource_salt="public"
    )
    targets_priv, target_priv = _make_targets(
        raw, workspace_id="ws-alpha", visibility="PRIVATE", resource_salt="private"
    )
    store = InMemoryProvenanceMetadataStore(targets_alpha + targets_priv)

    fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
    resolver = RefetchResolverService(metadata_store=store, fetcher=fetcher, clock=lambda: _FIXED_NOW)

    app = create_app(access_provider=_make_provider())
    mount_resolver_routes(app, resolver=resolver)

    with TestClient(app) as client:
        # 1) 200 OK with Cache-Control: private, no-store
        resp_ok = client.get(
            f"/v1/content-units/{target_alpha.content_unit_id}/content",
            params={"workspace_id": "ws-alpha"},
            headers={"Authorization": "Bearer tok-alice", "X-Request-ID": "req-200"},
        )
        assert resp_ok.status_code == 200
        assert resp_ok.headers["cache-control"] == "private, no-store"
        body = resp_ok.json()
        assert body["content_unit_id"] == target_alpha.content_unit_id
        assert body["unit_type"] == "PR_BODY_SECTION"
        assert _CANARY_PRIMARY in body["content"]
        assert body["context"]["preceding_text"] is not None
        assert body["context"]["following_text"] is not None
        assert body["source_resolution"] == {
            "status": "MATCHED",
            "checked_at": "2026-11-27T14:00:00Z",
            "expected_revision_hash": target_alpha.expected_revision_hash,
            "observed_revision_hash": target_alpha.expected_revision_hash,
            "retryable": False,
            "reason_code": None,
        }

        # 2) Unauthenticated -> 401 with private, no-store
        resp_401 = client.get(
            f"/v1/content-units/{target_alpha.content_unit_id}/content",
            params={"workspace_id": "ws-alpha"},
        )
        assert resp_401.status_code == 401
        assert resp_401.headers["cache-control"] == "private, no-store"
        assert resp_401.json()["error"]["code"] == "UNAUTHENTICATED"

        # 3) Cross-workspace membership denied -> 403 WORKSPACE_SCOPE_VIOLATION
        resp_ws = client.get(
            f"/v1/content-units/{target_alpha.content_unit_id}/content",
            params={"workspace_id": "ws-alpha"},
            headers={"Authorization": "Bearer tok-bob"},
        )
        assert resp_ws.status_code == 403
        assert resp_ws.headers["cache-control"] == "private, no-store"
        assert resp_ws.json()["error"]["code"] == "WORKSPACE_SCOPE_VIOLATION"

        # 4) Cross-workspace content_unit_id inside Bob's own workspace -> 404 identical to missing
        resp_xws = client.get(
            f"/v1/content-units/{target_alpha.content_unit_id}/content",
            params={"workspace_id": "ws-beta"},
            headers={"Authorization": "Bearer tok-bob", "X-Request-ID": "req-xws"},
        )
        resp_missing = client.get(
            f"/v1/content-units/{uuid.uuid4()}/content",
            params={"workspace_id": "ws-beta"},
            headers={"Authorization": "Bearer tok-bob", "X-Request-ID": "req-xws"},
        )
        assert resp_xws.status_code == 404
        assert resp_missing.status_code == 404
        assert resp_xws.headers["cache-control"] == "private, no-store"
        assert resp_missing.headers["cache-control"] == "private, no-store"
        assert resp_xws.json() == resp_missing.json()
        assert resp_xws.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        # 5) Policy blocked (PRIVATE resource) -> 403 SOURCE_POLICY_BLOCKED, zero raw text
        resp_priv = client.get(
            f"/v1/content-units/{target_priv.content_unit_id}/content",
            params={"workspace_id": "ws-alpha", "X-Request-ID": "ignored"},
            headers={"Authorization": "Bearer tok-alice", "X-Request-ID": "req-priv"},
        )
        assert resp_priv.status_code == 403
        assert resp_priv.headers["cache-control"] == "private, no-store"
        priv_body = resp_priv.json()
        assert priv_body["error"]["code"] == "SOURCE_POLICY_BLOCKED"
        assert priv_body["source_resolution"]["status"] == "POLICY_BLOCKED"
        assert _CANARY_PRIMARY not in resp_priv.text

        # 6) Arbitrary URL query parameter injection rejected -> 400 INVALID_REQUEST
        resp_url_param = client.get(
            f"/v1/content-units/{target_alpha.content_unit_id}/content",
            params={"workspace_id": "ws-alpha", "url": "https://evil.example.com/raw"},
            headers={"Authorization": "Bearer tok-alice"},
        )
        assert resp_url_param.status_code == 400
        assert resp_url_param.headers["cache-control"] == "private, no-store"
        assert resp_url_param.json()["error"]["code"] == "INVALID_REQUEST"

        # 7) X-Workspace-ID header works when verified, and conflicting query/header fails closed
        resp_header_ok = client.get(
            f"/v1/content-units/{target_alpha.content_unit_id}/content",
            headers={"Authorization": "Bearer tok-alice", "X-Workspace-ID": "ws-alpha"},
        )
        assert resp_header_ok.status_code == 200
        assert resp_header_ok.headers["cache-control"] == "private, no-store"

        resp_conflict = client.get(
            f"/v1/content-units/{target_alpha.content_unit_id}/content",
            params={"workspace_id": "ws-alpha"},
            headers={"Authorization": "Bearer tok-alice", "X-Workspace-ID": "ws-beta"},
        )
        assert resp_conflict.status_code == 403
        assert resp_conflict.headers["cache-control"] == "private, no-store"
        assert resp_conflict.json()["error"]["code"] == "WORKSPACE_SCOPE_VIOLATION"


def test_ref04_comprehensive_canary_zero_leak_audit_across_all_surfaces(
    caplog: Any,
) -> None:
    raw_ok = (
        f"# Header\nIntro\n\n## Sensitive Section\nSecret {_CANARY_PRIMARY} payload.\n"
    ).encode("utf-8")
    raw_drifted = (
        f"# Header\nIntro\n\n## Sensitive Section\nDrifted {_CANARY_FAILURE} payload.\n"
    ).encode("utf-8")
    targets, target = _make_targets(raw_ok, workspace_id="ws-alpha")
    store = InMemoryProvenanceMetadataStore(targets)

    # First run: 200 OK, second run: 409 REVISION_MISMATCH, third run: 422 PARTIAL_WITH_LIMIT, fourth: 503 UNAVAILABLE
    fetcher = InMemoryProviderContentFetcher.from_sequence(
        [
            ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                http_status=200,
                raw_bytes=raw_ok,
                bytes_read=len(raw_ok),
            ),
            ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.OK,
                http_status=200,
                raw_bytes=raw_drifted,
                bytes_read=len(raw_drifted),
            ),
            ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.PARTIAL_CONTENT,
                http_status=206,
                raw_bytes=raw_drifted[:32],
                bytes_read=32,
                truncated=True,
            ),
            ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.NOT_FOUND,
                http_status=404,
                reason_code="PROVIDER_OBJECT_NOT_FOUND",
            ),
        ]
    )
    resolver = RefetchResolverService(
        metadata_store=store,
        fetcher=fetcher,
        default_budget=OperationBudgetConfig(max_retries=0),
        clock=lambda: _FIXED_NOW,
    )
    app = create_app(access_provider=_make_provider())
    mount_resolver_routes(app, resolver=resolver)

    tmp_scan_dir = Path(tempfile.mkdtemp(prefix="cm-ref04-scan-"))
    try:
        with caplog.at_level(logging.DEBUG):
            with TestClient(app) as client:
                r200 = client.get(
                    f"/v1/content-units/{target.content_unit_id}/content",
                    params={"workspace_id": "ws-alpha"},
                    headers={"Authorization": "Bearer tok-alice"},
                )
                r409 = client.get(
                    f"/v1/content-units/{target.content_unit_id}/content",
                    params={"workspace_id": "ws-alpha"},
                    headers={"Authorization": "Bearer tok-alice"},
                )
                r422 = client.get(
                    f"/v1/content-units/{target.content_unit_id}/content",
                    params={"workspace_id": "ws-alpha"},
                    headers={"Authorization": "Bearer tok-alice"},
                )
                r503 = client.get(
                    f"/v1/content-units/{target.content_unit_id}/content",
                    params={"workspace_id": "ws-alpha"},
                    headers={"Authorization": "Bearer tok-alice"},
                )

        assert r200.status_code == 200
        assert _CANARY_PRIMARY in r200.json()["content"]
        assert r409.status_code == 409
        assert r422.status_code == 422
        assert r503.status_code == 503

        manifest = JobRefetchManifest(
            operation_id="op-audit",
            workspace_id="ws-alpha",
            items=(
                JobRefetchManifestItem(
                    source_resource_id=target.source_resource_id,
                    source_object_id=target.source_object_id,
                    source_object_revision_id=target.source_object_revision_id,
                    content_unit_id=target.content_unit_id,
                    expected_revision_hash=target.expected_revision_hash,
                    expected_unit_hash=target.expected_unit_hash,
                    canonicalization_version=target.canonicalization_version,
                    parser_version=target.parser_version,
                ),
            ),
        )

        # Build complete audit surfaces (excluding the ephemeral 200 response body itself)
        surfaces: dict[str, str] = {
            "persisted_store_targets": repr(store.dump_all_persisted_records()),
            "recorded_resolution_metadata": json.dumps(
                [m.to_dict() for m in store.recorded_metadata]
            ),
            "job_refetch_manifest": json.dumps(manifest.to_dict()),
            "captured_logs": "\n".join(rec.getMessage() for rec in caplog.records),
            "http_409_response": r409.text,
            "http_422_response": r422.text,
            "http_503_response": r503.text,
            "temp_dir_files": "\n".join(
                p.read_text(encoding="utf-8", errors="ignore")
                for p in tmp_scan_dir.rglob("*")
                if p.is_file()
            ),
        }

        for canary in (_CANARY_PRIMARY, _CANARY_FAILURE):
            for surface_name, text in surfaces.items():
                assert canary not in text, f"REF-04 LEAK: {canary} found in {surface_name}!"

        # Negative control: prove our scanner catches a synthetic leak
        surfaces["negative_control_leak"] = f"leaked {_CANARY_PRIMARY}"
        assert _CANARY_PRIMARY in surfaces["negative_control_leak"]
    finally:
        for p in sorted(tmp_scan_dir.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink(missing_ok=True)
            else:
                p.rmdir()
        tmp_scan_dir.rmdir()
