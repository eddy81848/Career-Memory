"""Isolated test-only 127.0.0.1 FastAPI stub for M5 Memory Ops HTTP contract tests.

Binds strictly to 127.0.0.1 on an OS-assigned ephemeral port (0) and implements
all 5 canonical M5 backend endpoints:
  1. GET  /v1/search (OD-008 pagination, 400 INVALID_SEARCH_CURSOR, 422 PROHIBITED_PROJECT_ID_SCOPE)
  2. POST /v1/source-resources/{source_resource_id}/deletion-preview (OD-003)
  3. POST /v1/source-resources/{source_resource_id}/deletion-confirm (OD-003 202 + 409 + Idempotency-Key)
  4. GET  /v1/deletion-jobs/{deletion_job_id} (PARTIAL -> SUCCEEDED component receipts)
  5. GET  /v1/quota-status (ALLOWED, WARNING_80_PERCENT, CUTOFF_95_PERCENT)
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import signal
import socket
from typing import Any
import uuid

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

NO_STORE_HEADERS = {"Cache-Control": "private, no-store"}
FORBIDDEN_PROJECT_KEYS = {"project_id", "projectid", "project_ids", "projectids"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def compute_query_hash(q: str, entity_types: list[str] | None) -> str:
    canonical = json.dumps(
        {
            "entity_types": sorted(set(entity_types or [])),
            "q": " ".join(q.strip().split()),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def make_initial_state() -> dict[str, Any]:
    return {
        "index_version": "fts-index-v1",
        "ranking_version": "rank-bm25-v1",
        "quota_mode": "ALLOWED",
        "force_scope_drift_next_confirm": False,
        "resources": {
            "src_repo_ledger_01": {
                "source_resource_id": "src_repo_ledger_01",
                "resource_version": "rv_http_01",
                "impact_scope_version": "isv_http_01",
                "fence_active": False,
                "impact_counts": {
                    "work_events": 4,
                    "facts": 11,
                    "contributions": 4,
                    "claims_draft": 1,
                    "claims_edited": 2,
                    "claims_approved": 3,
                    "claims_historical_versions": 4,
                    "search_index_entries": 19,
                    "ephemeral_cache_entries": 2,
                    "server_exports": 2,
                },
            },
            "src_repo_portfolio_02": {
                "source_resource_id": "src_repo_portfolio_02",
                "resource_version": "rv_port_01",
                "impact_scope_version": "isv_port_01",
                "fence_active": False,
                "impact_counts": {
                    "work_events": 2,
                    "facts": 5,
                    "contributions": 2,
                    "claims_draft": 0,
                    "claims_edited": 1,
                    "claims_approved": 1,
                    "claims_historical_versions": 1,
                    "search_index_entries": 8,
                    "ephemeral_cache_entries": 1,
                    "server_exports": 1,
                },
            },
        },
        "hits": [
            {
                "entity_type": "WORK_EVENT",
                "entity_id": "evt_http_01",
                "source_resource_id": "src_repo_ledger_01",
                "title_or_summary": "Webhook HMAC-SHA256 verification middleware implementation",
                "score": 0.97,
            },
            {
                "entity_type": "FACT",
                "entity_id": "fact_http_01",
                "source_resource_id": "src_repo_ledger_01",
                "title_or_summary": "Reduced webhook p99 verification latency by 42%",
                "score": 0.93,
            },
            {
                "entity_type": "CLAIM",
                "entity_id": "clm_http_01",
                "source_resource_id": "src_repo_ledger_01",
                "title_or_summary": "Implemented constant-time webhook HMAC verification cutting p99 latency by 42%",
                "score": 0.91,
            },
            {
                "entity_type": "CLAIM",
                "entity_id": "clm_http_02",
                "source_resource_id": "src_repo_portfolio_02",
                "title_or_summary": "Webhook delivery telemetry dashboard standardized with OpenTelemetry",
                "score": 0.85,
            },
        ],
        "preview_tokens": {},
        "deletion_jobs": {},
        "idempotency_confirm": {},
        "seq": 1,
        "requests": [],
    }


STATE = make_initial_state()
app = FastAPI()


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    headers = {**NO_STORE_HEADERS, "X-Request-ID": request_id}
    if extra_headers:
        headers.update(extra_headers)
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": request_id,
    }
    if details is not None:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content={"error": payload}, headers=headers)


def record_request(request: Request, status: int, body: Any = None) -> None:
    STATE["requests"].append(
        {
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "authorization": request.headers.get("authorization"),
            "workspace_id": request.headers.get("x-workspace-id"),
            "idempotency_key": request.headers.get("idempotency-key"),
            "request_id": request.headers.get("x-request-id"),
            "status": status,
            "body": body,
        }
    )


# =============================================================================
# Control Endpoints (Test Harness Only)
# =============================================================================


@app.post("/__control/reset")
async def control_reset() -> JSONResponse:
    global STATE
    STATE = make_initial_state()
    return JSONResponse({"ok": True})


@app.post("/__control/bump-index-version")
async def control_bump_index_version() -> JSONResponse:
    STATE["index_version"] = "fts-index-v2"
    STATE["ranking_version"] = "rank-bm25-v2"
    return JSONResponse(
        {
            "index_version": STATE["index_version"],
            "ranking_version": STATE["ranking_version"],
        }
    )


@app.post("/__control/arm-scope-drift")
async def control_arm_scope_drift() -> JSONResponse:
    STATE["force_scope_drift_next_confirm"] = True
    return JSONResponse({"ok": True})


@app.post("/__control/complete-deletion-job")
async def control_complete_deletion_job(request: Request) -> JSONResponse:
    body = await request.json()
    job_id = body["deletion_job_id"]
    job = STATE["deletion_jobs"][job_id]
    ts = now_iso()
    for comp in job["components"]:
        comp["status"] = "SUCCEEDED"
        comp["attempt"] = max(1, comp["attempt"] + 1)
        comp["purged_count"] = max(1, comp["purged_count"] or 5)
        comp["last_error_code"] = None
        comp["updated_at"] = ts
        comp["completed_at"] = ts
    job["status"] = "SUCCEEDED"
    job["user_visible_state"] = "DELETED"
    job["failure_summary"] = {}
    job["completed_at"] = ts
    return JSONResponse({"ok": True})


@app.post("/__control/set-quota-mode")
async def control_set_quota_mode(request: Request) -> JSONResponse:
    body = await request.json()
    STATE["quota_mode"] = body["mode"]
    return JSONResponse({"mode": STATE["quota_mode"]})


@app.get("/__control/requests")
async def control_requests() -> JSONResponse:
    return JSONResponse({"requests": STATE["requests"]})


# =============================================================================
# 1. GET /v1/search (OD-008)
# =============================================================================


@app.get("/v1/search")
async def search_memory(
    request: Request,
    q: str = Query(..., min_length=1),
    source_resource_id: str | None = Query(default=None),
    entity_types: list[str] | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    ws_id = request.headers.get("x-workspace-id") or "ws_http_m5"

    for key in request.query_params.keys():
        if key.strip().lower() in FORBIDDEN_PROJECT_KEYS:
            record_request(request, 422)
            return error_response(
                422,
                "PROHIBITED_PROJECT_ID_SCOPE",
                "OD-008: project_id is globally prohibited.",
                request_id=req_id,
                details={"forbidden_paths": [f"$.{key}"]},
            )

    query_hash = compute_query_hash(q, entity_types)
    offset = 0
    if cursor:
        parts = cursor.split("|")
        if (
            len(parts) != 4
            or parts[0] != "cur"
            or parts[1] != STATE["index_version"]
            or parts[2] != STATE["ranking_version"]
        ):
            record_request(request, 400)
            return error_response(
                400,
                "INVALID_SEARCH_CURSOR",
                "Search cursor binding mismatch for: index_version, ranking_version",
                request_id=req_id,
                details={"mismatched_bindings": ["index_version", "ranking_version"]},
            )
        offset = int(parts[3])

    allowed_types = set(entity_types) if entity_types else None
    tokens = [tok.lower() for tok in q.strip().split() if tok.strip()]

    filtered = []
    for hit in STATE["hits"]:
        repo = STATE["resources"].get(hit["source_resource_id"])
        if not repo or repo["fence_active"]:
            continue
        if source_resource_id and hit["source_resource_id"] != source_resource_id:
            continue
        if allowed_types and hit["entity_type"] not in allowed_types:
            continue
        haystack = f"{hit['title_or_summary']} {hit['entity_id']}".lower()
        if all(tok in haystack for tok in tokens):
            filtered.append(hit)

    page = filtered[offset : offset + limit]
    next_cursor = (
        f"cur|{STATE['index_version']}|{STATE['ranking_version']}|{offset + limit}"
        if offset + limit < len(filtered)
        else None
    )

    payload = {
        "scope": {
            "workspace_id": ws_id,
            "source_resource_id": source_resource_id,
        },
        "query_hash": query_hash,
        "index_version": STATE["index_version"],
        "ranking_version": STATE["ranking_version"],
        "items": page,
        "next_cursor": next_cursor,
    }
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        content=payload,
        headers={**NO_STORE_HEADERS, "X-Request-ID": req_id},
    )


# =============================================================================
# 2 & 3. POST /v1/source-resources/{id}/deletion-preview & deletion-confirm
# =============================================================================


@app.post("/v1/source-resources/{source_resource_id}/deletion-preview")
async def preview_source_deletion(
    source_resource_id: str, request: Request
) -> JSONResponse:
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    repo = STATE["resources"].get(source_resource_id)
    if not repo:
        record_request(request, 404)
        return error_response(
            404, "RESOURCE_NOT_FOUND", "Source resource not found.", request_id=req_id
        )

    token = f"prev_{source_resource_id}_{repo['resource_version']}_{STATE['seq']}"
    STATE["seq"] += 1
    STATE["preview_tokens"][token] = {
        "source_resource_id": source_resource_id,
        "resource_version": repo["resource_version"],
        "impact_scope_version": repo["impact_scope_version"],
    }

    payload = {
        "source_resource_id": source_resource_id,
        "resource_version": repo["resource_version"],
        "impact_scope_version": repo["impact_scope_version"],
        "impact_counts": deepcopy(repo["impact_counts"]),
        "non_recoverable_scopes": {
            "already_downloaded_exports": True,
            "provider_hosted_originals": True,
            "notice": "Previously downloaded local export files and remote provider-hosted source originals cannot be recalled by server-side deletion.",
        },
        "preview_token": token,
        "expires_at": "2026-11-28T07:00:00Z",
    }
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        content=payload,
        headers={**NO_STORE_HEADERS, "X-Request-ID": req_id},
    )


@app.post("/v1/source-resources/{source_resource_id}/deletion-confirm")
async def confirm_source_deletion(
    source_resource_id: str, request: Request
) -> JSONResponse:
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    ws_id = request.headers.get("x-workspace-id") or "ws_http_m5"
    idem_key = request.headers.get("idempotency-key")
    body = await request.json()

    if not idem_key:
        record_request(request, 422, body)
        return error_response(
            422,
            "MISSING_IDEMPOTENCY_KEY",
            "Idempotency-Key header is required.",
            request_id=req_id,
        )

    if idem_key in STATE["idempotency_confirm"]:
        record_request(request, 202, body)
        return JSONResponse(
            status_code=202,
            content=STATE["idempotency_confirm"][idem_key],
            headers={**NO_STORE_HEADERS, "X-Request-ID": req_id},
        )

    repo = STATE["resources"].get(source_resource_id)
    if not repo:
        record_request(request, 404, body)
        return error_response(
            404, "RESOURCE_NOT_FOUND", "Source resource not found.", request_id=req_id
        )

    if STATE["force_scope_drift_next_confirm"]:
        STATE["force_scope_drift_next_confirm"] = False
        repo["resource_version"] = "rv_http_02"
        repo["impact_scope_version"] = "isv_http_02"
        repo["impact_counts"]["claims_edited"] += 1
        record_request(request, 409, body)
        return error_response(
            409,
            "DELETION_PREVIEW_SCOPE_CHANGED",
            "Target resource version or impact scope changed since preview.",
            request_id=req_id,
        )

    token_meta = STATE["preview_tokens"].get(body.get("preview_token", ""))
    if (
        not token_meta
        or token_meta["source_resource_id"] != source_resource_id
        or token_meta["resource_version"] != repo["resource_version"]
        or body.get("expected_resource_version") != repo["resource_version"]
        or body.get("expected_impact_scope_version") != repo["impact_scope_version"]
    ):
        record_request(request, 409, body)
        return error_response(
            409,
            "DELETION_PREVIEW_SCOPE_CHANGED",
            "Preview token does not match current resource/impact scope version.",
            request_id=req_id,
        )

    job_id = f"del_http_job_{STATE['seq']}"
    STATE["seq"] += 1
    repo["fence_active"] = True
    ts = now_iso()

    accepted = {
        "deletion_job_id": job_id,
        "source_resource_id": source_resource_id,
        "status": "PENDING",
        "fence_active": True,
    }
    STATE["idempotency_confirm"][idem_key] = accepted

    STATE["deletion_jobs"][job_id] = {
        "deletion_job_id": job_id,
        "workspace_id": ws_id,
        "source_resource_id": source_resource_id,
        "status": "PARTIAL",
        "user_visible_state": "DELETION_PENDING",
        "fence_active": True,
        "checkpoint": {"last_completed_component": "EPHEMERAL_CACHE"},
        "failure_summary": {"failed_component": "FTS_INDEX"},
        "components": [
            {
                "component": "JOBS_AND_OUTBOX",
                "status": "SUCCEEDED",
                "attempt": 1,
                "purged_count": 2,
                "last_error_code": None,
                "receipt_metadata": {},
                "updated_at": ts,
                "completed_at": ts,
            },
            {
                "component": "EXPORTS",
                "status": "SUCCEEDED",
                "attempt": 1,
                "purged_count": repo["impact_counts"]["server_exports"],
                "last_error_code": None,
                "receipt_metadata": {},
                "updated_at": ts,
                "completed_at": ts,
            },
            {
                "component": "FTS_INDEX",
                "status": "FAILED",
                "attempt": 1,
                "purged_count": 5,
                "last_error_code": "FTS_CLEANUP_TRANSIENT_TIMEOUT",
                "receipt_metadata": {},
                "updated_at": ts,
                "completed_at": None,
            },
            {
                "component": "EPHEMERAL_CACHE",
                "status": "SUCCEEDED",
                "attempt": 1,
                "purged_count": repo["impact_counts"]["ephemeral_cache_entries"],
                "last_error_code": None,
                "receipt_metadata": {},
                "updated_at": ts,
                "completed_at": ts,
            },
            {
                "component": "AUTHORITATIVE_DOMAIN",
                "status": "PENDING",
                "attempt": 0,
                "purged_count": 0,
                "last_error_code": None,
                "receipt_metadata": {},
                "updated_at": ts,
                "completed_at": None,
            },
        ],
        "requested_by": "usr_http_m5",
        "requested_at": ts,
        "completed_at": None,
    }

    record_request(request, 202, body)
    return JSONResponse(
        status_code=202,
        content=accepted,
        headers={**NO_STORE_HEADERS, "X-Request-ID": req_id},
    )


# =============================================================================
# 4. GET /v1/deletion-jobs/{deletion_job_id}
# =============================================================================


@app.get("/v1/deletion-jobs/{deletion_job_id}")
async def get_deletion_job_status(
    deletion_job_id: str, request: Request
) -> JSONResponse:
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    job = STATE["deletion_jobs"].get(deletion_job_id)
    if not job:
        record_request(request, 404)
        return error_response(
            404, "RESOURCE_NOT_FOUND", "Deletion job not found.", request_id=req_id
        )
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        content=job,
        headers={**NO_STORE_HEADERS, "X-Request-ID": req_id},
    )


# =============================================================================
# 5. GET /v1/quota-status (T-503 / INF-06)
# =============================================================================


@app.get("/v1/quota-status")
async def get_quota_status(
    request: Request,
    source_resource_id: str | None = Query(default=None),
) -> JSONResponse:
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    ws_id = request.headers.get("x-workspace-id") or "ws_http_m5"
    mode = STATE["quota_mode"]
    ts = now_iso()

    if mode == "WARNING_80_PERCENT":
        payload = {
            "workspace_id": ws_id,
            "period_key": "2026-11",
            "admission_state": "WARNING_80_PERCENT",
            "pause_state": "ACTIVE",
            "pause_reason_code": "BUDGET_80_PERCENT_WARNING",
            "circuit_breaker_state": "CLOSED",
            "high_cost_operations_allowed": True,
            "safety_operations_allowed": True,
            "paid_fallback_allowed": False,
            "retry_after_seconds": None,
            "free_alternatives": [
                "Reduce repository/time range scope.",
                "Use manual review and edit mode without LLM generation.",
            ],
            "meters": [
                {
                    "metric_kind": "IMPORT_OBJECTS",
                    "used_value": 840,
                    "soft_warning_limit": 800,
                    "hard_cutoff_limit": 950,
                    "utilization_percent": 84.0,
                    "status": "WARNING",
                },
                {
                    "metric_kind": "SEARCH_QUERIES",
                    "used_value": 410,
                    "soft_warning_limit": 400,
                    "hard_cutoff_limit": 475,
                    "utilization_percent": 82.0,
                    "status": "WARNING",
                },
            ],
            "checked_at": ts,
        }
    elif mode == "CUTOFF_95_PERCENT":
        payload = {
            "workspace_id": ws_id,
            "period_key": "2026-11",
            "admission_state": "CUTOFF_95_PERCENT",
            "pause_state": "PAUSED_QUOTA",
            "pause_reason_code": "BUDGET_95_PERCENT_CUTOFF",
            "circuit_breaker_state": "OPEN",
            "high_cost_operations_allowed": False,
            "safety_operations_allowed": True,
            "paid_fallback_allowed": False,
            "retry_after_seconds": 900,
            "free_alternatives": [
                "Reduce scope and retry after cooldown.",
                "Source deletion (OD-003) and account disconnect remain available.",
            ],
            "meters": [
                {
                    "metric_kind": "IMPORT_OBJECTS",
                    "used_value": 960,
                    "soft_warning_limit": 800,
                    "hard_cutoff_limit": 950,
                    "utilization_percent": 96.0,
                    "status": "CUTOFF",
                },
                {
                    "metric_kind": "ESTIMATED_COST_KRW",
                    "used_value": 9600,
                    "soft_warning_limit": 8000,
                    "hard_cutoff_limit": 9500,
                    "utilization_percent": 96.0,
                    "status": "CUTOFF",
                },
            ],
            "checked_at": ts,
        }
    else:
        payload = {
            "workspace_id": ws_id,
            "period_key": "2026-11",
            "admission_state": "ALLOWED",
            "pause_state": "ACTIVE",
            "pause_reason_code": None,
            "circuit_breaker_state": "CLOSED",
            "high_cost_operations_allowed": True,
            "safety_operations_allowed": True,
            "paid_fallback_allowed": False,
            "retry_after_seconds": None,
            "free_alternatives": [],
            "meters": [
                {
                    "metric_kind": "IMPORT_OBJECTS",
                    "used_value": 120,
                    "soft_warning_limit": 800,
                    "hard_cutoff_limit": 950,
                    "utilization_percent": 12.0,
                    "status": "OK",
                }
            ],
            "checked_at": ts,
        }

    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        content=payload,
        headers={**NO_STORE_HEADERS, "X-Request-ID": req_id},
    )


async def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, setattr, server, "should_exit", True)
        except NotImplementedError:
            pass

    async def announce_when_ready() -> None:
        while not server.started:
            await asyncio.sleep(0.02)
        print(json.dumps({"baseUrl": f"http://127.0.0.1:{port}"}), flush=True)

    task = asyncio.create_task(announce_when_ready())
    try:
        await server.serve(sockets=[sock])
    finally:
        task.cancel()
        sock.close()


if __name__ == "__main__":
    asyncio.run(main())
