"""Unit tests for M3 Work Events (T-302 relations, T-305 models, T-303 cursor & HTTP boundary)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.schema_contracts.enums import (
    ContributionRole,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.work_events.models import (
    WORK_EVENTS_NO_STORE_HEADER,
    WorkEventCursorPayload,
    WorkEventError,
    WorkEventPolicyState,
    compute_allowed_actions,
    compute_work_events_filter_hash,
    decode_and_verify_work_event_cursor,
    encode_work_event_cursor,
    format_work_event_etag,
)
from career_memory_api.work_events.relations import (
    canonicalize_event_pair,
    is_forbidden_must_link_relation_type,
)
from career_memory_api.work_events.router import (
    InMemoryWorkEventsReadService,
    create_work_events_router,
)


def _fake_access(workspace_id: str | None = None) -> WorkspaceAccessContext:
    ws_id = workspace_id or str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer="https://auth.local", subject=f"sub-{user_id}"),
        ),
        membership=WorkspaceMembership(
            workspace_id=ws_id,
            user_id=user_id,
            role="OWNER",
        ),
    )


def test_canonicalize_event_pair_is_order_invariant_and_rejects_self_or_invalid() -> None:
    id_low = "00000000-0000-4000-8000-000000000001"
    id_high = "ffffffff-ffff-4fff-8fff-ffffffffffff"

    assert canonicalize_event_pair(id_low, id_high) == (id_low, id_high)
    assert canonicalize_event_pair(id_high, id_low) == (id_low, id_high)
    assert canonicalize_event_pair(id_high.upper(), f"  {id_low}  ") == (id_low, id_high)

    with pytest.raises(ValueError, match="distinct"):
        canonicalize_event_pair(id_low, id_low)

    with pytest.raises(ValueError, match="valid UUID"):
        canonicalize_event_pair("not-a-uuid", id_high)


def test_is_forbidden_must_link_relation_type_enforces_event_04() -> None:
    for forbidden in (
        "ISSUE_REFERENCE",
        "issue_link",
        "TICKET_REFERENCE",
        "SHARED_TICKET",
        "FILE_OVERLAP",
        "SHARED_FILE",
        "BRANCH_HEURISTIC",
        "SAME_BRANCH",
        "TIME_PROXIMITY",
        "TEXT_SIMILARITY",
        "SEMANTIC_SIMILARITY",
        "   ",
    ):
        assert is_forbidden_must_link_relation_type(forbidden) is True

    for allowed in ("PRIMARY_ANCHOR", "PR_COMMIT", "PR_REVIEW", "PR_COMMENT"):
        assert is_forbidden_must_link_relation_type(allowed) is False


def test_format_work_event_etag_and_allowed_actions_matrix() -> None:
    assert format_work_event_etag(1) == '"v1"'
    assert format_work_event_etag(12) == '"v12"'
    with pytest.raises(ValueError):
        format_work_event_etag(0)

    # PR CANDIDATE -> full actions except DISSOLVE
    assert compute_allowed_actions(
        anchor_mode=WorkEventAnchorMode.PR,
        status=WorkEventStatus.CANDIDATE,
        policy_state=WorkEventPolicyState.ALLOWED,
        is_active_top_level=True,
    ) == ["CONFIRM", "EDIT", "SPLIT", "MERGE", "REJECT"]

    # PR CONFIRMED -> no CONFIRM
    assert compute_allowed_actions(
        anchor_mode=WorkEventAnchorMode.PR,
        status=WorkEventStatus.CONFIRMED,
        policy_state=WorkEventPolicyState.ALLOWED,
        is_active_top_level=True,
    ) == ["EDIT", "SPLIT", "MERGE", "REJECT"]

    # DIRECT_COMMIT CANDIDATE -> no SPLIT/DISSOLVE
    assert compute_allowed_actions(
        anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
        status=WorkEventStatus.CANDIDATE,
        policy_state=WorkEventPolicyState.ALLOWED,
        is_active_top_level=True,
    ) == ["CONFIRM", "EDIT", "MERGE", "REJECT"]

    # USER_COMPOSITE CONFIRMED -> EDIT, DISSOLVE, REJECT
    assert compute_allowed_actions(
        anchor_mode=WorkEventAnchorMode.USER_COMPOSITE,
        status=WorkEventStatus.CONFIRMED,
        policy_state=WorkEventPolicyState.ALLOWED,
        is_active_top_level=True,
    ) == ["EDIT", "DISSOLVE", "REJECT"]

    # USER_COMPOSITE STALE -> CONFIRM, EDIT, DISSOLVE, REJECT
    assert compute_allowed_actions(
        anchor_mode=WorkEventAnchorMode.USER_COMPOSITE,
        status=WorkEventStatus.STALE,
        policy_state=WorkEventPolicyState.ALLOWED,
        is_active_top_level=True,
    ) == ["CONFIRM", "EDIT", "DISSOLVE", "REJECT"]

    # Historical / non-top-level / policy-blocked / deletion-pending -> empty actions
    for mode in WorkEventAnchorMode:
        assert (
            compute_allowed_actions(
                anchor_mode=mode,
                status=WorkEventStatus.SUPERSEDED,
                policy_state=WorkEventPolicyState.ALLOWED,
                is_active_top_level=False,
            )
            == []
        )
        assert (
            compute_allowed_actions(
                anchor_mode=mode,
                status=WorkEventStatus.REJECTED,
                policy_state=WorkEventPolicyState.ALLOWED,
                is_active_top_level=False,
            )
            == []
        )
        assert (
            compute_allowed_actions(
                anchor_mode=mode,
                status=WorkEventStatus.CANDIDATE,
                policy_state=WorkEventPolicyState.POLICY_BLOCKED,
                is_active_top_level=True,
            )
            == []
        )
        assert (
            compute_allowed_actions(
                anchor_mode=mode,
                status=WorkEventStatus.CANDIDATE,
                policy_state=WorkEventPolicyState.DELETION_PENDING,
                is_active_top_level=True,
            )
            == []
        )


def test_work_events_hmac_cursor_binding_and_tamper_detection() -> None:
    ws_a = str(uuid.uuid4())
    ws_b = str(uuid.uuid4())
    evt_id = str(uuid.uuid4())
    ts = datetime(2026, 11, 28, 10, 0, 0, tzinfo=UTC)

    f_hash = compute_work_events_filter_hash(
        status=WorkEventStatus.CONFIRMED,
        source_resource_id=None,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 11, 28),
        role=ContributionRole.IMPLEMENTED,
    )
    cursor = encode_work_event_cursor(
        WorkEventCursorPayload(
            workspace_id=ws_a,
            filter_hash=f_hash,
            sort_ts=ts,
            last_event_id=evt_id,
        )
    )

    decoded = decode_and_verify_work_event_cursor(
        cursor,
        expected_workspace_id=ws_a,
        expected_filter_hash=f_hash,
    )
    assert decoded.workspace_id == ws_a
    assert decoded.last_event_id == evt_id
    assert decoded.sort_ts == ts

    # Different workspace -> 400 INVALID_WORK_EVENTS_CURSOR
    with pytest.raises(WorkEventError) as exc_ws:
        decode_and_verify_work_event_cursor(
            cursor,
            expected_workspace_id=ws_b,
            expected_filter_hash=f_hash,
        )
    assert exc_ws.value.status_code == 400
    assert exc_ws.value.code == "INVALID_WORK_EVENTS_CURSOR"

    # Altered filter -> 400 INVALID_WORK_EVENTS_CURSOR
    other_hash = compute_work_events_filter_hash(status=WorkEventStatus.CANDIDATE)
    with pytest.raises(WorkEventError) as exc_filter:
        decode_and_verify_work_event_cursor(
            cursor,
            expected_workspace_id=ws_a,
            expected_filter_hash=other_hash,
        )
    assert exc_filter.value.status_code == 400
    assert exc_filter.value.code == "INVALID_WORK_EVENTS_CURSOR"

    # Tampered signature or malformed cursor -> 400 INVALID_WORK_EVENTS_CURSOR
    for bad_cursor in ("no-dot-separator", cursor[:-3] + "AAA"):
        with pytest.raises(WorkEventError) as exc_bad:
            decode_and_verify_work_event_cursor(
                bad_cursor,
                expected_workspace_id=ws_a,
                expected_filter_hash=f_hash,
            )
        assert exc_bad.value.status_code == 400
        assert exc_bad.value.code == "INVALID_WORK_EVENTS_CURSOR"


def test_work_events_router_http_boundary_headers_auth_and_od008_guard() -> None:
    # 1. Fail-closed unconfigured access guard -> 401 when unauthenticated
    unauth_app = create_app(
        work_events_router=create_work_events_router(service=InMemoryWorkEventsReadService()),
        install_work_events_handlers=True,
    )
    with TestClient(unauth_app) as client:
        r_unauth = client.get("/v1/work-events")
        assert r_unauth.status_code == 401
        assert r_unauth.headers["Cache-Control"] == WORK_EVENTS_NO_STORE_HEADER
        assert r_unauth.json()["error"]["code"] == "UNAUTHENTICATED"

    # 2. Authenticated app enforces OD-008 project_id rejection, no-store, X-Request-ID, and 404 concealment
    access = _fake_access()
    app = create_app(
        work_events_router=create_work_events_router(
            service=InMemoryWorkEventsReadService(),
            resolve_access=lambda _req: access,
        ),
        install_work_events_handlers=True,
    )
    with TestClient(app) as client:
        req_id = "req-work-events-unit-01"
        ok = client.get("/v1/work-events", headers={"X-Request-ID": req_id})
        assert ok.status_code == 200
        assert ok.headers["Cache-Control"] == WORK_EVENTS_NO_STORE_HEADER
        assert ok.headers["X-Request-ID"] == req_id
        assert ok.json() == {
            "items": [],
            "page": {"next_cursor": None, "has_more": False},
            "counts": {
                "active_top_level_total": 0,
                "by_status": {"CANDIDATE": 0, "CONFIRMED": 0, "STALE": 0},
            },
        }

        # OD-008: project_id & project_ids rejected
        for bad_param in ("project_id", "project_ids"):
            rejected = client.get("/v1/work-events", params={bad_param: "proj-123"})
            assert rejected.status_code == 422
            assert rejected.headers["Cache-Control"] == WORK_EVENTS_NO_STORE_HEADER
            assert rejected.json()["error"]["code"] == "PROHIBITED_PROJECT_ID_SCOPE"

        # 404 concealment on detail endpoint omits details
        missing = client.get(
            f"/v1/work-events/{uuid.uuid4()}",
            headers={"X-Request-ID": "req-missing-1"},
        )
        assert missing.status_code == 404
        assert missing.headers["Cache-Control"] == WORK_EVENTS_NO_STORE_HEADER
        assert missing.headers["X-Request-ID"] == "req-missing-1"
        err_body = missing.json()["error"]
        assert err_body["code"] == "RESOURCE_NOT_FOUND"
        assert "details" not in err_body
