"""Isolated test-only 127.0.0.1 FastAPI stub for M3 Event Review HTTP contract tests.

Binds strictly to 127.0.0.1 on an OS-assigned ephemeral port (0) and implements
the M3 Event Review wire endpoints (T-303 / T-304 / ER-004):
  - GET  /v1/work-events
  - GET  /v1/work-events/{event_id}
  - POST /v1/work-events/{event_id}/actions/confirm
  - PATCH /v1/work-events/{event_id}
  - POST /v1/work-events/{event_id}/actions/reject
  - POST /v1/work-events/{event_id}/actions/split
  - POST /v1/work-events/actions/merge
  - POST /v1/work-events/{event_id}/actions/dissolve

Always returns `Cache-Control: private, no-store` and `ETag: "v{version}"` on
detail/mutation responses, enforces `If-Match` / `expected_version` with
`409 EVENT_VERSION_CONFLICT`, and enforces `CANNOT_LINK` and non-nested composite.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import signal
import socket
from typing import Any

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

NO_STORE_HEADERS = {"Cache-Control": "private, no-store"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def make_initial_events() -> dict[str, dict[str, Any]]:
    policy_active = {
        "status": "ACTIVE",
        "reason_code": None,
        "message": None,
        "raw_view_blocked": False,
        "external_ai_blocked": False,
        "export_blocked": False,
    }
    return {
        "evt_pr_104": {
            "event_id": "evt_pr_104",
            "workspace_id": "ws_http_01",
            "source_resource_id": "repo_demo_ledger_01",
            "anchor_mode": "PR",
            "status": "CANDIDATE",
            "version": 1,
            "etag": '"v1"',
            "is_active_top_level": True,
            "title": "PR #104: 정산 원장 멱등성 키 검증 강화",
            "problem": "중복 결제 웹훅 시 이중 적재 위험",
            "action": "멱등성 해시 락 추가",
            "outcome": "중복 삽입 0건",
            "created_by": "RULE",
            "occurred_start_at": "2026-09-10T02:15:00Z",
            "occurred_end_at": "2026-09-12T08:40:00Z",
            "reviewed_at": None,
            "updated_at": "2026-09-12T08:45:00Z",
            "review_flags": ["MIXED_PURPOSE_SIGNALS", "MULTIPLE_LINKED_ISSUES"],
            "allowed_actions": ["CONFIRM", "EDIT", "REJECT", "SPLIT", "MERGE"],
            "evidence_count": 2,
            "context_count": 1,
            "direct_commit_badge": None,
            "composite_summary": None,
            "policy_state": deepcopy(policy_active),
            "primary_anchor_source_object_id": "so_pr_104",
            "anchor_scope": {"kind": "PR", "file_paths": ["src/ledger/idempotency.ts", "src/worker/batch.ts"]},
            "evidence_memberships": [
                {
                    "membership_id": "mem_104_a",
                    "source_object_id": "so_pr_104",
                    "object_type": "PULL_REQUEST",
                    "provider_object_id": "PR#104",
                    "relation_class": "MUST_LINK",
                    "relation_type": "PRIMARY_ANCHOR",
                    "origin": "SOURCE",
                    "title": "Pull Request #104",
                    "file_paths": ["src/ledger/idempotency.ts"],
                },
                {
                    "membership_id": "mem_104_b",
                    "source_object_id": "so_commit_104_b",
                    "object_type": "COMMIT",
                    "provider_object_id": "b32d81f",
                    "relation_class": "MUST_LINK",
                    "relation_type": "PR_COMMIT",
                    "origin": "SOURCE",
                    "title": "refactor(worker): split batch runner",
                    "file_paths": ["src/worker/batch.ts"],
                },
            ],
            "context_relations": [
                {
                    "membership_id": "ctx_104_i41",
                    "source_object_id": "so_issue_41",
                    "object_type": "ISSUE",
                    "provider_object_id": "ISSUE#41",
                    "relation_class": "CONTEXT_ONLY",
                    "relation_type": "ISSUE_REFERENCE",
                    "origin": "SOURCE",
                    "title": "Issue #41: 중복 웹훅 방지",
                    "rationale": "참고 문맥 전용",
                }
            ],
            "cannot_link_constraints": {
                "source_object_ids": [],
                "peer_event_ids": [],
                "constraints": [],
            },
            "composite_details": None,
            "downstream_impact": {
                "fact_count": 3,
                "claim_count": 2,
                "export_count": 1,
                "stale_fact_count": 0,
                "stale_claim_count": 0,
                "processing_status": "COMPLETE",
            },
            "version_history": [
                {
                    "version": 1,
                    "status": "CANDIDATE",
                    "action": "CREATE_CANDIDATE",
                    "actor": "RULE",
                    "reason": "Merged PR #104",
                    "created_at": "2026-09-12T08:45:00Z",
                }
            ],
            "review_actions": [],
        },
        "evt_dc_209": {
            "event_id": "evt_dc_209",
            "workspace_id": "ws_http_01",
            "source_resource_id": "repo_demo_ledger_01",
            "anchor_mode": "DIRECT_COMMIT",
            "status": "CANDIDATE",
            "version": 1,
            "etag": '"v1"',
            "is_active_top_level": True,
            "title": "hotfix(ledger): 환율 반올림 테스트 보강",
            "problem": "반올림 경계값 회귀 방지 필요",
            "action": "trunk 기본 브랜치 직접 커밋 반영",
            "outcome": "CI 통과",
            "created_by": "RULE",
            "occurred_start_at": "2026-09-13T04:10:00Z",
            "occurred_end_at": "2026-09-13T04:10:00Z",
            "reviewed_at": None,
            "updated_at": "2026-09-13T04:15:00Z",
            "review_flags": ["DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED"],
            "allowed_actions": ["CONFIRM", "EDIT", "REJECT", "MERGE"],
            "evidence_count": 1,
            "context_count": 0,
            "direct_commit_badge": {
                "badge_code": "DIRECT_DEFAULT_BRANCH_NO_PR",
                "label": "PR 없음 · default branch 직접 반영",
                "default_branch": "trunk",
                "abbreviated_sha": "a8f3c1e",
                "committed_at": "2026-09-13T04:10:00Z",
                "pr_association_state": "NO_MATCH_OBSERVED",
                "no_match_observed_means_definitely_no_pr": False,
                "requires_user_confirmation": True,
            },
            "composite_summary": None,
            "policy_state": deepcopy(policy_active),
            "primary_anchor_source_object_id": "so_commit_dc209",
            "anchor_scope": {"kind": "DIRECT_COMMIT", "file_paths": ["src/ledger/rounding.test.ts"]},
            "evidence_memberships": [
                {
                    "membership_id": "mem_dc209_a",
                    "source_object_id": "so_commit_dc209",
                    "object_type": "COMMIT",
                    "provider_object_id": "a8f3c1e",
                    "relation_class": "MUST_LINK",
                    "relation_type": "PRIMARY_ANCHOR",
                    "origin": "SOURCE",
                    "title": "Direct commit a8f3c1e on trunk",
                    "file_paths": ["src/ledger/rounding.test.ts"],
                }
            ],
            "context_relations": [],
            "cannot_link_constraints": {
                "source_object_ids": [],
                "peer_event_ids": [],
                "constraints": [],
            },
            "composite_details": None,
            "downstream_impact": {
                "fact_count": 1,
                "claim_count": 1,
                "export_count": 0,
                "stale_fact_count": 0,
                "stale_claim_count": 0,
                "processing_status": "COMPLETE",
            },
            "version_history": [
                {
                    "version": 1,
                    "status": "CANDIDATE",
                    "action": "CREATE_CANDIDATE",
                    "actor": "RULE",
                    "reason": "Direct commit on trunk",
                    "created_at": "2026-09-13T04:15:00Z",
                }
            ],
            "review_actions": [],
        },
    }


class StubStore:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = make_initial_events()
        self.request_log: list[dict[str, Any]] = []
        self.seq = 500

    def reset(self) -> None:
        self.events = make_initial_events()
        self.request_log = []
        self.seq = 500


store = StubStore()
app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


def to_summary(detail: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "event_id",
        "workspace_id",
        "source_resource_id",
        "anchor_mode",
        "status",
        "version",
        "etag",
        "is_active_top_level",
        "title",
        "problem",
        "action",
        "outcome",
        "created_by",
        "occurred_start_at",
        "occurred_end_at",
        "reviewed_at",
        "updated_at",
        "review_flags",
        "allowed_actions",
        "evidence_count",
        "context_count",
        "direct_commit_badge",
        "composite_summary",
        "policy_state",
    ]
    return {k: deepcopy(detail[k]) for k in keys}


def record_request(request: Request, status_code: int) -> None:
    store.request_log.append(
        {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "if_match": request.headers.get("if-match"),
            "idempotency_key": request.headers.get("idempotency-key"),
            "request_id": request.headers.get("x-request-id"),
            "status": status_code,
        }
    )


def check_version_conflict(
    request: Request, event: dict[str, Any], expected_version: Any
) -> JSONResponse | None:
    if_match = request.headers.get("if-match")
    expected_etag = f'"v{event["version"]}"'
    if if_match != expected_etag or expected_version != event["version"]:
        record_request(request, 409)
        return JSONResponse(
            status_code=409,
            headers=NO_STORE_HEADERS,
            content={
                "error": {
                    "code": "EVENT_VERSION_CONFLICT",
                    "message": f'Version mismatch: expected v{expected_version}, current is v{event["version"]}',
                    "retryable": False,
                    "details": {
                        "event_id": event["event_id"],
                        "current_version": event["version"],
                        "current_status": event["status"],
                        "allowed_actions": event["allowed_actions"],
                    },
                }
            },
        )
    return None


@app.post("/__control/reset")
async def control_reset():
    store.reset()
    return {"ok": True}


@app.post("/__control/bump-version")
async def control_bump_version(request: Request):
    body = await request.json()
    ev = store.events[body["event_id"]]
    ev["version"] += 1
    ev["etag"] = f'"v{ev["version"]}"'
    ev["status"] = body.get("status", "CONFIRMED")
    ev["updated_at"] = now_iso()
    return {"ok": True, "version": ev["version"], "status": ev["status"]}


@app.get("/__control/requests")
async def control_requests():
    return {"requests": list(store.request_log)}


@app.get("/v1/work-events")
async def list_work_events(
    request: Request,
    limit: int = Query(default=50),
    status: str | None = Query(default=None),
    source_resource_id: str | None = Query(default=None),
):
    all_summaries = [to_summary(ev) for ev in store.events.values()]
    active_total = sum(
        1 for s in all_summaries if s["is_active_top_level"] and s["status"] != "SUPERSEDED"
    )
    by_status: dict[str, int] = {}
    for s in all_summaries:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1

    filtered = all_summaries
    if source_resource_id:
        filtered = [s for s in filtered if s["source_resource_id"] == source_resource_id]
    if status:
        filtered = [s for s in filtered if s["status"] == status]
    else:
        filtered = [s for s in filtered if s["is_active_top_level"] and s["status"] != "SUPERSEDED"]

    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers=NO_STORE_HEADERS,
        content={
            "items": filtered[:limit],
            "page": {"next_cursor": None, "has_more": False},
            "counts": {
                "active_top_level_total": active_total,
                "by_status": by_status,
            },
            "inbox_state": "READY",
        },
    )


@app.get("/v1/work-events/{event_id}")
async def get_work_event_detail(event_id: str, request: Request):
    ev = store.events.get(event_id)
    if not ev:
        record_request(request, 404)
        return JSONResponse(
            status_code=404,
            headers=NO_STORE_HEADERS,
            content={"error": {"code": "EVENT_NOT_FOUND", "message": "Not found"}},
        )
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers={**NO_STORE_HEADERS, "ETag": ev["etag"]},
        content=ev,
    )


@app.post("/v1/work-events/{event_id}/actions/confirm")
async def confirm_work_event(event_id: str, request: Request):
    ev = store.events.get(event_id)
    if not ev:
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=NO_STORE_HEADERS, content={"error": {"code": "EVENT_NOT_FOUND"}})
    body = await request.json()
    conflict = check_version_conflict(request, ev, body.get("expected_version"))
    if conflict:
        return conflict

    prev = ev["version"]
    ev["version"] += 1
    ev["etag"] = f'"v{ev["version"]}"'
    ev["status"] = "CONFIRMED"
    ts = now_iso()
    ev["reviewed_at"] = ts
    ev["updated_at"] = ts
    ev["version_history"].append(
        {"version": ev["version"], "status": "CONFIRMED", "action": "CONFIRM", "actor": "USER", "reason": body.get("reason"), "created_at": ts}
    )
    ev["review_actions"].append(
        {"action_id": f"ra_{store.seq}", "action": "CONFIRM", "actor": "USER", "from_version": prev, "to_version": ev["version"], "reason": body.get("reason"), "created_at": ts}
    )
    store.seq += 1
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers={**NO_STORE_HEADERS, "ETag": ev["etag"]},
        content=ev,
    )


@app.patch("/v1/work-events/{event_id}")
async def edit_work_event(event_id: str, request: Request):
    ev = store.events.get(event_id)
    if not ev:
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=NO_STORE_HEADERS, content={"error": {"code": "EVENT_NOT_FOUND"}})
    body = await request.json()
    conflict = check_version_conflict(request, ev, body.get("expected_version"))
    if conflict:
        return conflict

    ev["version"] += 1
    ev["etag"] = f'"v{ev["version"]}"'
    ev["status"] = "CANDIDATE"
    ev["title"] = body["title"]
    ev["problem"] = body.get("problem")
    ev["action"] = body.get("action")
    ev["outcome"] = body.get("outcome")
    ev["updated_at"] = now_iso()
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers={**NO_STORE_HEADERS, "ETag": ev["etag"]},
        content=ev,
    )


@app.post("/v1/work-events/{event_id}/actions/reject")
async def reject_work_event(event_id: str, request: Request):
    ev = store.events.get(event_id)
    if not ev:
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=NO_STORE_HEADERS, content={"error": {"code": "EVENT_NOT_FOUND"}})
    body = await request.json()
    conflict = check_version_conflict(request, ev, body.get("expected_version"))
    if conflict:
        return conflict

    ev["version"] += 1
    ev["etag"] = f'"v{ev["version"]}"'
    ev["status"] = "REJECTED"
    ev["allowed_actions"] = ["REOPEN"]
    ev["updated_at"] = now_iso()
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers={**NO_STORE_HEADERS, "ETag": ev["etag"]},
        content=ev,
    )


@app.post("/v1/work-events/actions/merge")
async def merge_work_events(request: Request):
    body = await request.json()
    members_req = body.get("members", [])
    if len(members_req) < 2:
        record_request(request, 422)
        return JSONResponse(status_code=422, headers=NO_STORE_HEADERS, content={"error": {"code": "TOO_FEW_EVENTS"}})

    members = []
    for ref in members_req:
        ev = store.events.get(ref["event_id"])
        if not ev:
            record_request(request, 404)
            return JSONResponse(status_code=404, headers=NO_STORE_HEADERS, content={"error": {"code": "EVENT_NOT_FOUND"}})
        if ev["version"] != ref["expected_version"]:
            record_request(request, 409)
            return JSONResponse(
                status_code=409,
                headers=NO_STORE_HEADERS,
                content={
                    "error": {
                        "code": "EVENT_VERSION_CONFLICT",
                        "message": "Member version conflict",
                        "details": {
                            "event_id": ev["event_id"],
                            "current_version": ev["version"],
                            "current_status": ev["status"],
                            "allowed_actions": ev["allowed_actions"],
                        },
                    }
                },
            )
        members.append(ev)

    store.seq += 1
    comp_id = f"evt_comp_{store.seq}"
    ts = now_iso()
    snapshots = []
    merged_evidence = []
    fact_sum = sum(m["downstream_impact"]["fact_count"] for m in members)
    claim_sum = sum(m["downstream_impact"]["claim_count"] for m in members)
    export_sum = sum(m["downstream_impact"]["export_count"] for m in members)

    for m in members:
        snapshots.append(
            {
                "member_event_id": m["event_id"],
                "member_version": m["version"],
                "anchor_mode": m["anchor_mode"],
                "title": m["title"],
                "status_at_merge": m["status"],
                "current_status": "SUPERSEDED",
                "evidence_count": len(m["evidence_memberships"]),
                "occurred_start_at": m["occurred_start_at"],
                "occurred_end_at": m["occurred_end_at"],
                "direct_commit_badge": deepcopy(m["direct_commit_badge"]),
            }
        )
        merged_evidence.extend(deepcopy(m["evidence_memberships"]))
        m["version"] += 1
        m["etag"] = f'"v{m["version"]}"'
        m["status"] = "SUPERSEDED"
        m["is_active_top_level"] = False
        m["allowed_actions"] = []
        m["updated_at"] = ts

    comp = {
        "event_id": comp_id,
        "workspace_id": members[0]["workspace_id"],
        "source_resource_id": members[0]["source_resource_id"],
        "anchor_mode": "USER_COMPOSITE",
        "status": "CANDIDATE",
        "version": 1,
        "etag": '"v1"',
        "is_active_top_level": True,
        "title": body.get("title", "Merged Composite"),
        "problem": None,
        "action": None,
        "outcome": None,
        "created_by": "USER",
        "occurred_start_at": members[0]["occurred_start_at"],
        "occurred_end_at": members[-1]["occurred_end_at"],
        "reviewed_at": None,
        "updated_at": ts,
        "review_flags": [],
        "allowed_actions": ["CONFIRM", "EDIT", "DISSOLVE"],
        "evidence_count": len(merged_evidence),
        "context_count": 0,
        "direct_commit_badge": None,
        "composite_summary": {
            "member_count": len(members),
            "badge_label": f"{len(members)}개 Event를 사용자가 병합",
            "merge_reason": body.get("reason", ""),
            "regeneration_status": "COMPLETE",
        },
        "policy_state": deepcopy(members[0]["policy_state"]),
        "primary_anchor_source_object_id": None,
        "anchor_scope": {"kind": "USER_COMPOSITE", "file_paths": []},
        "evidence_memberships": merged_evidence,
        "context_relations": [],
        "cannot_link_constraints": {"source_object_ids": [], "peer_event_ids": [], "constraints": []},
        "composite_details": {
            "member_count": len(members),
            "merge_reason": body.get("reason", ""),
            "regeneration_status": "COMPLETE",
            "superseded_by_composite_id": None,
            "members": snapshots,
        },
        "downstream_impact": {
            "fact_count": 0,
            "claim_count": 0,
            "export_count": 0,
            "stale_fact_count": fact_sum,
            "stale_claim_count": claim_sum,
            "processing_status": "COMPLETE",
        },
        "version_history": [
            {"version": 1, "status": "CANDIDATE", "action": "MERGE", "actor": "USER", "reason": body.get("reason"), "created_at": ts}
        ],
        "review_actions": [],
    }
    store.events[comp_id] = comp
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers={**NO_STORE_HEADERS, "ETag": comp["etag"]},
        content={
            "composite_event": comp,
            "superseded_member_ids": [m["event_id"] for m in members],
            "affected_downstream_impact": {
                "fact_count": fact_sum,
                "claim_count": claim_sum,
                "export_count": export_sum,
            },
        },
    )


@app.post("/v1/work-events/{event_id}/actions/dissolve")
async def dissolve_work_event(event_id: str, request: Request):
    comp = store.events.get(event_id)
    if not comp:
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=NO_STORE_HEADERS, content={"error": {"code": "EVENT_NOT_FOUND"}})
    body = await request.json()
    conflict = check_version_conflict(request, comp, body.get("expected_version"))
    if conflict:
        return conflict

    ts = now_iso()
    comp["version"] += 1
    comp["etag"] = f'"v{comp["version"]}"'
    comp["status"] = "SUPERSEDED"
    comp["is_active_top_level"] = False
    comp["allowed_actions"] = []
    comp["updated_at"] = ts

    restored = []
    for snap in comp.get("composite_details", {}).get("members", []):
        m = store.events.get(snap["member_event_id"])
        if not m:
            continue
        m["version"] += 1
        m["etag"] = f'"v{m["version"]}"'
        m["status"] = "CANDIDATE"
        m["is_active_top_level"] = True
        m["allowed_actions"] = ["CONFIRM", "EDIT", "REJECT", "MERGE"]
        m["updated_at"] = ts
        restored.append(m)

    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers={**NO_STORE_HEADERS, "ETag": comp["etag"]},
        content={
            "dissolved_composite": comp,
            "restored_members": restored,
            "stale_downstream_impact": {
                "stale_fact_count": comp["downstream_impact"]["fact_count"],
                "stale_claim_count": comp["downstream_impact"]["claim_count"],
                "stale_export_count": comp["downstream_impact"]["export_count"],
            },
        },
    )


def bind_loopback_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


async def main() -> None:
    sock = bind_loopback_socket()
    port = sock.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            pass

    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)

    print(json.dumps({"baseUrl": f"http://127.0.0.1:{port}"}), flush=True)

    try:
        await task
    finally:
        sock.close()


if __name__ == "__main__":
    asyncio.run(main())
