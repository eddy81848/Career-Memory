"""Isolated test-only 127.0.0.1 FastAPI stub for M4 Claim Review HTTP contract tests.

Binds strictly to 127.0.0.1 on an OS-assigned ephemeral port (0) and implements
all 4 canonical backend route families for T-406:
  1. Facts:
     - GET  /v1/facts?event_id=...&include_superseded=false
     - GET  /v1/facts/{fact_id}/supports
     - POST /v1/facts/{fact_id}/actions/approve
     - POST /v1/facts/{fact_id}/actions/reject
     - PATCH /v1/facts/{fact_id}
  2. Claims:
     - GET  /v1/claims?event_id=...&include_superseded=false
     - POST /v1/claims/generations
     - GET  /v1/claims/{claim_id}
     - GET  /v1/claims/{claim_id}/supports
     - PATCH /v1/claims/{claim_id}
     - POST /v1/claims/{claim_id}/actions/revalidate
     - POST /v1/claims/{claim_id}/actions/approve
     - POST /v1/claims/{claim_id}/actions/reject
  3. Plan C Ephemeral Content Unit Viewer:
     - GET  /v1/content-units/{content_unit_id}/content?workspace_id=...&include_context=true
  4. Exports:
     - GET    /v1/exports
     - POST   /v1/exports
     - GET    /v1/exports/{export_id}
     - DELETE /v1/exports/{export_id}
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
FORBIDDEN_WRITE_KEYS = {
    "verification",
    "computed_verification",
    "source_verified",
    "status",
    "extracted_span",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def make_initial_state() -> dict[str, Any]:
    facts = {
        "fact_hmac_01": {
            "fact_id": "fact_hmac_01",
            "workspace_id": "ws_http_m4",
            "event_id": "evt_comp_301",
            "version": 1,
            "supersedes_fact_id": None,
            "fact_type": "ACTION",
            "subject_identity_id": "id_seoyeon_01",
            "predicate": "implemented",
            "object_text": "HMAC-SHA256 webhook signature middleware",
            "normalized_value": {
                "completion_state": "COMPLETED",
                "scope": "INDIVIDUAL",
                "environment_scope": "PRODUCTION",
            },
            "fact_metadata": {},
            "status": "SUPPORTED",
            "extractor_version": "fact-extractor-v1",
            "direct_support_count": 1,
            "context_support_count": 0,
            "contradicts_support_count": 0,
            "etag": '"v1"',
            "created_at": "2026-09-12T09:00:00Z",
            "updated_at": "2026-09-12T09:05:00Z",
        },
        "fact_cand_02": {
            "fact_id": "fact_cand_02",
            "workspace_id": "ws_http_m4",
            "event_id": "evt_comp_301",
            "version": 1,
            "supersedes_fact_id": None,
            "fact_type": "RESULT",
            "subject_identity_id": "id_seoyeon_01",
            "predicate": "added_replay_window",
            "object_text": "5-minute replay timestamp window",
            "normalized_value": {
                "completion_state": "COMPLETED",
                "scope": "INDIVIDUAL",
                "environment_scope": "PRODUCTION",
            },
            "fact_metadata": {},
            "status": "CANDIDATE",
            "extractor_version": "fact-extractor-v1",
            "direct_support_count": 0,
            "context_support_count": 1,
            "contradicts_support_count": 0,
            "etag": '"v1"',
            "created_at": "2026-09-12T09:10:00Z",
            "updated_at": "2026-09-12T09:10:00Z",
        },
    }

    fact_supports = {
        "fact_hmac_01": [
            {
                "support_id": "fs_hmac_01",
                "content_unit_id": "cu_matched_01",
                "source_object_revision_id": "rev_01",
                "unit_type": "DIFF_HUNK",
                "support_type": "DIRECT",
                "status": "ACTIVE",
                "validator": "HYBRID",
                "validator_version": "fact-validator-v1",
                "locator": {
                    "schema": "cm-locator-v1",
                    "file_path": "src/webhook/signature.ts",
                    "start_line": 18,
                    "end_line": 22,
                    "start_char": 0,
                    "end_char": 200,
                    "span_sha256": "sha256:c1093ef18ab21094",
                    "source_url": "https://github.com/demo/ledger/pull/88",
                },
                "revision_content_hash": "sha256:8f41c0d9e3a2b710",
                "revision_observed_at": "2026-08-21T06:00:00Z",
                "revision_is_tombstone": False,
                "last_checked_at": "2026-11-28T03:50:00Z",
                "last_resolution_status": "MATCHED",
                "support_metadata": {},
                "created_at": "2026-09-12T09:05:00Z",
            }
        ],
        "fact_cand_02": [],
    }

    claims = {
        "clm_http_01": {
            "claim_id": "clm_http_01",
            "workspace_id": "ws_http_m4",
            "event_id": "evt_comp_301",
            "version": 1,
            "supersedes_claim_id": None,
            "claim_text": "Implemented HMAC-SHA256 webhook signature middleware in production.",
            "status": "SUPPORT_VALIDATED",
            "computed_verification": "SOURCE_VERIFIED",
            "atom_count": 1,
            "supported_atom_count": 1,
            "user_confirmed_atom_count": 0,
            "unsupported_atom_count": 0,
            "stale_atom_count": 0,
            "approvable": True,
            "approval_block_reasons": [],
            "support_graph_epoch": 3,
            "approved_at": None,
            "last_verified_at": "2026-11-28T03:50:00Z",
            "generator_run_id": "run_gen_01",
            "last_verification_run_id": "run_ver_01",
            "created_at": "2026-11-27T09:00:00Z",
            "updated_at": "2026-11-28T03:50:00Z",
            "atoms": [
                {
                    "atom_id": "atom_h01_1",
                    "ordinal": 1,
                    "atom_type": "ACTION",
                    "atom_text": "Implemented HMAC-SHA256 webhook signature middleware",
                    "status": "SUPPORTED",
                }
            ],
        },
        "clm_http_mixed": {
            "claim_id": "clm_http_mixed",
            "workspace_id": "ws_http_m4",
            "event_id": "evt_comp_301",
            "version": 1,
            "supersedes_claim_id": None,
            "claim_text": "Implemented HMAC middleware and coordinated partner key rollout.",
            "status": "SUPPORT_VALIDATED",
            "computed_verification": "MIXED",
            "atom_count": 2,
            "supported_atom_count": 1,
            "user_confirmed_atom_count": 1,
            "unsupported_atom_count": 0,
            "stale_atom_count": 0,
            "approvable": True,
            "approval_block_reasons": [],
            "support_graph_epoch": 4,
            "approved_at": None,
            "last_verified_at": "2026-11-28T03:55:00Z",
            "generator_run_id": "run_gen_02",
            "last_verification_run_id": "run_ver_02",
            "created_at": "2026-11-27T10:00:00Z",
            "updated_at": "2026-11-28T03:55:00Z",
            "atoms": [
                {
                    "atom_id": "atom_hm_1",
                    "ordinal": 1,
                    "atom_type": "ACTION",
                    "atom_text": "Implemented HMAC middleware",
                    "status": "SUPPORTED",
                },
                {
                    "atom_id": "atom_hm_2",
                    "ordinal": 2,
                    "atom_type": "ROLE",
                    "atom_text": "Coordinated partner key rollout",
                    "status": "USER_CONFIRMED",
                },
            ],
        },
    }

    claim_supports = {
        "clm_http_01": {
            "claim_id": "clm_http_01",
            "version": 1,
            "event_id": "evt_comp_301",
            "status": "SUPPORT_VALIDATED",
            "computed_verification": "SOURCE_VERIFIED",
            "support_graph_epoch": 3,
            "atoms": [
                {
                    "atom_id": "atom_h01_1",
                    "ordinal": 1,
                    "atom_type": "ACTION",
                    "atom_text": "Implemented HMAC-SHA256 webhook signature middleware",
                    "status": "SUPPORTED",
                    "supports": [
                        {
                            "claim_atom_support_id": "cas_h01_1",
                            "support_type": "DIRECT",
                            "status": "ACTIVE",
                            "validation_run_id": "run_ver_01",
                            "fact": {
                                "fact_id": "fact_hmac_01",
                                "version": 1,
                                "fact_type": "ACTION",
                                "predicate": "implemented",
                                "object_text": "HMAC-SHA256 webhook signature middleware",
                                "status": "SUPPORTED",
                                "normalized_value": facts["fact_hmac_01"]["normalized_value"],
                            },
                            "fact_supports": [
                                {
                                    "fact_support_id": "fs_hmac_01",
                                    "support_type": "DIRECT",
                                    "status": "ACTIVE",
                                    "validator": "HYBRID",
                                    "validator_version": "fact-validator-v1",
                                    "content_unit_id": "cu_matched_01",
                                    "unit_type": "DIFF_HUNK",
                                    "locator": fact_supports["fact_hmac_01"][0]["locator"],
                                    "source_object_revision_id": "rev_01",
                                    "revision_content_hash": "sha256:8f41c0d9e3a2b710",
                                    "revision_observed_at": "2026-08-21T06:00:00Z",
                                    "revision_is_tombstone": False,
                                    "last_checked_at": "2026-11-28T03:50:00Z",
                                    "last_resolution_status": "MATCHED",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "clm_http_mixed": {
            "claim_id": "clm_http_mixed",
            "version": 1,
            "event_id": "evt_comp_301",
            "status": "SUPPORT_VALIDATED",
            "computed_verification": "MIXED",
            "support_graph_epoch": 4,
            "atoms": [],
        },
    }

    return {
        "facts": facts,
        "fact_supports": fact_supports,
        "claims": claims,
        "claim_supports": claim_supports,
        "exports": {},
        "request_log": [],
        "seq": 500,
    }


class StubStore:
    def __init__(self) -> None:
        self.data = make_initial_state()

    def reset(self) -> None:
        self.data = make_initial_state()


store = StubStore()
app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


def make_headers(request: Request, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(NO_STORE_HEADERS)
    req_id = request.headers.get("x-request-id") or "req-stub"
    headers["X-Request-ID"] = req_id
    if extra:
        headers.update(extra)
    return headers


def record_request(request: Request, status_code: int, body: Any = None) -> None:
    store.data["request_log"].append(
        {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "authorization": request.headers.get("authorization"),
            "workspace_id": request.headers.get("x-workspace-id"),
            "if_match": request.headers.get("if-match"),
            "idempotency_key": request.headers.get("idempotency-key"),
            "request_id": request.headers.get("x-request-id"),
            "status": status_code,
            "body": body,
        }
    )


def reject_forbidden_keys(request: Request, body: dict[str, Any]) -> JSONResponse | None:
    bad = [k for k in body.keys() if k.lower() in FORBIDDEN_WRITE_KEYS]
    if bad:
        record_request(request, 422, body)
        return JSONResponse(
            status_code=422,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "PROHIBITED_WRITE_KEY",
                    "message": f"Forbidden keys: {bad}",
                    "retryable": False,
                }
            },
        )
    return None


# --- Control Endpoints ---


@app.post("/__control/reset")
async def control_reset():
    store.reset()
    return {"ok": True}


@app.post("/__control/bump-claim-version")
async def control_bump_claim(request: Request):
    body = await request.json()
    c = store.data["claims"][body["claim_id"]]
    c["version"] += 1
    c["support_graph_epoch"] += 1
    c["updated_at"] = now_iso()
    return {"ok": True, "version": c["version"]}


@app.get("/__control/requests")
async def control_requests():
    return {"requests": list(store.data["request_log"])}


# --- 1. Facts (/v1/facts*) ---


@app.get("/v1/facts")
async def list_facts(
    request: Request,
    event_id: str | None = Query(default=None),
    include_superseded: bool = Query(default=False),
):
    items = [
        f
        for f in store.data["facts"].values()
        if (not event_id or f["event_id"] == event_id)
        and (include_superseded or f["status"] != "SUPERSEDED")
    ]
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request),
        content={"items": items, "page": {"next_cursor": None, "has_more": False}},
    )


@app.get("/v1/facts/{fact_id}/supports")
async def get_fact_supports(fact_id: str, request: Request):
    f = store.data["facts"].get(fact_id)
    if not f:
        record_request(request, 404)
        return JSONResponse(
            status_code=404,
            headers=make_headers(request),
            content={"error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found"}},
        )
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request),
        content={"fact": f, "items": store.data["fact_supports"].get(fact_id, [])},
    )


@app.post("/v1/facts/{fact_id}/actions/approve")
async def approve_fact(fact_id: str, request: Request):
    body = await request.json()
    bad = reject_forbidden_keys(request, body)
    if bad:
        return bad
    f = store.data["facts"].get(fact_id)
    if not f:
        record_request(request, 404, body)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    f["status"] = "USER_CONFIRMED"
    f["updated_at"] = now_iso()
    record_request(request, 200, body)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request, {"ETag": f["etag"]}),
        content={"fact": f, "idempotent_replay": False},
    )


@app.post("/v1/facts/{fact_id}/actions/reject")
async def reject_fact(fact_id: str, request: Request):
    body = await request.json()
    f = store.data["facts"].get(fact_id)
    if not f:
        record_request(request, 404, body)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    f["status"] = "REJECTED"
    f["updated_at"] = now_iso()
    record_request(request, 200, body)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request, {"ETag": f["etag"]}),
        content={"fact": f, "invalidated_counts": {"contributions": 0, "contribution_supports": 0, "claim_atoms": 0, "claim_atom_supports": 0, "claims": 0, "exports": 0}},
    )


@app.patch("/v1/facts/{fact_id}")
async def edit_fact(fact_id: str, request: Request):
    body = await request.json()
    bad = reject_forbidden_keys(request, body)
    if bad:
        return bad
    prev = store.data["facts"].get(fact_id)
    if not prev:
        record_request(request, 404, body)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    prev["status"] = "SUPERSEDED"
    store.data["seq"] += 1
    new_id = f"fact_v{prev['version'] + 1}_{store.data['seq']}"
    created = deepcopy(prev)
    created["fact_id"] = new_id
    created["version"] = prev["version"] + 1
    created["supersedes_fact_id"] = fact_id
    created["predicate"] = body["predicate"]
    created["object_text"] = body["object_text"]
    created["status"] = "CANDIDATE"
    created["etag"] = f'"v{created["version"]}"'
    created["updated_at"] = now_iso()
    store.data["facts"][new_id] = created
    store.data["fact_supports"][new_id] = []
    record_request(request, 200, body)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request, {"ETag": created["etag"]}),
        content={"fact": created, "superseded_fact_id": fact_id, "superseded_version": prev["version"]},
    )


# --- 2. Claims (/v1/claims*) ---


@app.get("/v1/claims")
async def list_claims(
    request: Request,
    event_id: str | None = Query(default=None),
    include_superseded: bool = Query(default=False),
):
    items = []
    for c in store.data["claims"].values():
        if event_id and c["event_id"] != event_id:
            continue
        if not include_superseded and c["status"] == "SUPERSEDED":
            continue
        summary = {k: v for k, v in c.items() if k != "atoms"}
        items.append(summary)
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request),
        content={"items": items, "page": {"next_cursor": None, "has_more": False}},
    )


@app.post("/v1/claims/generations")
async def generate_claims(request: Request):
    body = await request.json()
    record_request(request, 200, body)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request),
        content={
            "event_id": body["confirmed_event_id"],
            "generation_status": "MANUAL_ONLY_BLOCKED",
            "reason_code": "HOSTED_LLM_DISABLED_BY_DEFAULT",
            "free_alternatives": [
                "Reduce scope",
                "Manual retry after quota reset",
                "Non-AI curation / manual draft",
                "Synthetic fixture preview in prototype mode",
            ],
            "claims": [],
        },
    )


@app.get("/v1/claims/{claim_id}")
async def get_claim_detail(claim_id: str, request: Request):
    c = store.data["claims"].get(claim_id)
    if not c:
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request, {"ETag": f'"v{c["version"]}"'}),
        content=c,
    )


@app.get("/v1/claims/{claim_id}/supports")
async def get_claim_supports(claim_id: str, request: Request):
    s = store.data["claim_supports"].get(claim_id)
    if not s:
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    record_request(request, 200)
    return JSONResponse(status_code=200, headers=make_headers(request), content=s)


@app.patch("/v1/claims/{claim_id}")
async def edit_claim(claim_id: str, request: Request):
    body = await request.json()
    bad = reject_forbidden_keys(request, body)
    if bad:
        return bad
    prev = store.data["claims"].get(claim_id)
    if not prev:
        record_request(request, 404, body)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    if body.get("expected_version") != prev["version"]:
        record_request(request, 409, body)
        return JSONResponse(
            status_code=409,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "CLAIM_VERSION_CONFLICT",
                    "message": f"Expected v{body.get('expected_version')}, current v{prev['version']}",
                    "retryable": False,
                    "details": {
                        "claim_id": claim_id,
                        "current_version": prev["version"],
                        "current_status": prev["status"],
                    },
                }
            },
        )
    prev["status"] = "SUPERSEDED"
    store.data["seq"] += 1
    new_id = f"{claim_id}_v{prev['version'] + 1}_{store.data['seq']}"
    new_ver = prev["version"] + 1
    draft = deepcopy(prev)
    draft["claim_id"] = new_id
    draft["version"] = new_ver
    draft["supersedes_claim_id"] = claim_id
    draft["claim_text"] = body["claim_text"]
    draft["status"] = "DRAFT"
    draft["computed_verification"] = "UNVERIFIED"
    draft["approvable"] = False
    draft["approval_block_reasons"] = ["NEEDS_REVALIDATION"]
    draft["approved_at"] = None
    draft["updated_at"] = now_iso()
    store.data["claims"][new_id] = draft
    if claim_id in store.data["claim_supports"]:
        sup = deepcopy(store.data["claim_supports"][claim_id])
        sup["claim_id"] = new_id
        sup["version"] = new_ver
        sup["status"] = "DRAFT"
        sup["computed_verification"] = "UNVERIFIED"
        store.data["claim_supports"][new_id] = sup
    record_request(request, 200, body)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request, {"ETag": f'"v{new_ver}"'}),
        content={
            "claim_id": new_id,
            "workspace_id": draft["workspace_id"],
            "event_id": draft["event_id"],
            "version": new_ver,
            "claim_text": draft["claim_text"],
            "status": "DRAFT",
            "computed_verification": "UNVERIFIED",
        },
    )


@app.post("/v1/claims/{claim_id}/actions/revalidate")
async def revalidate_claim(claim_id: str, request: Request):
    body = await request.json()
    c = store.data["claims"].get(claim_id)
    if not c:
        record_request(request, 404, body)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    if body.get("expected_version") != c["version"]:
        record_request(request, 409, body)
        return JSONResponse(
            status_code=409,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "CLAIM_VERSION_CONFLICT",
                    "message": "Version conflict",
                    "details": {"claim_id": claim_id, "current_version": c["version"], "current_status": c["status"]},
                }
            },
        )
    c["status"] = "SUPPORT_VALIDATED"
    c["computed_verification"] = "SOURCE_VERIFIED"
    c["approvable"] = True
    c["approval_block_reasons"] = []
    c["last_verified_at"] = now_iso()
    c["support_graph_epoch"] += 1
    record_request(request, 200, body)
    return JSONResponse(status_code=200, headers=make_headers(request), content=c)


@app.post("/v1/claims/{claim_id}/actions/approve")
async def approve_claim(claim_id: str, request: Request):
    body = await request.json()
    bad = reject_forbidden_keys(request, body)
    if bad:
        return bad
    c = store.data["claims"].get(claim_id)
    if not c:
        record_request(request, 404, body)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    if body.get("expected_version") != c["version"]:
        record_request(request, 409, body)
        return JSONResponse(
            status_code=409,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "CLAIM_VERSION_CONFLICT",
                    "message": f"Expected v{body.get('expected_version')}, server has v{c['version']}",
                    "retryable": False,
                    "details": {
                        "claim_id": claim_id,
                        "current_version": c["version"],
                        "current_status": c["status"],
                    },
                }
            },
        )
    c["status"] = "APPROVED"
    c["approved_at"] = now_iso()
    record_request(request, 200, body)
    return JSONResponse(status_code=200, headers=make_headers(request), content=c)


@app.post("/v1/claims/{claim_id}/actions/reject")
async def reject_claim(claim_id: str, request: Request):
    body = await request.json()
    c = store.data["claims"].get(claim_id)
    if not c:
        record_request(request, 404, body)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    c["status"] = "REJECTED"
    c["approvable"] = False
    record_request(request, 200, body)
    return JSONResponse(status_code=200, headers=make_headers(request), content=c)


# --- 3. Plan C Ephemeral Content Unit Viewer ---


@app.get("/v1/content-units/{content_unit_id}/content")
async def get_content_unit_content(
    content_unit_id: str,
    request: Request,
    workspace_id: str = Query(...),
    include_context: bool = Query(default=True),
):
    ts = now_iso()
    if content_unit_id == "cu_matched_01":
        record_request(request, 200)
        return JSONResponse(
            status_code=200,
            headers=make_headers(request),
            content={
                "content_unit_id": content_unit_id,
                "unit_type": "DIFF_HUNK",
                "content": "const ok = timingSafeEqual(expectedBuf, sigBuf);",
                "context": {
                    "section": "Signature Check",
                    "file_path": "src/webhook/signature.ts",
                    "previous_file_path": None,
                    "hunk_header": "@@ -18,4 +18,12 @@",
                    "preceding_text": "// verify hmac",
                    "following_text": "return ok;",
                }
                if include_context
                else None,
                "source_resolution": {
                    "status": "MATCHED",
                    "checked_at": ts,
                    "expected_revision_hash": "sha256:8f41c0d9e3a2b710",
                    "observed_revision_hash": "sha256:8f41c0d9e3a2b710",
                    "retryable": False,
                    "reason_code": None,
                },
            },
        )
    if content_unit_id == "cu_drift_409":
        record_request(request, 409)
        return JSONResponse(
            status_code=409,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "SOURCE_REVISION_MISMATCH",
                    "message": "Revision hash drifted",
                    "retryable": False,
                    "request_id": request.headers.get("x-request-id"),
                },
                "source_resolution": {
                    "status": "REVISION_MISMATCH",
                    "checked_at": ts,
                    "expected_revision_hash": "sha256:aaa",
                    "observed_revision_hash": "sha256:bbb",
                    "retryable": False,
                    "reason_code": "REVISION_HASH_MISMATCH",
                },
            },
        )
    if content_unit_id == "cu_policy_403":
        record_request(request, 403)
        return JSONResponse(
            status_code=403,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "SOURCE_POLICY_BLOCKED",
                    "message": "Visibility changed to private",
                    "retryable": False,
                    "request_id": request.headers.get("x-request-id"),
                },
                "source_resolution": {
                    "status": "POLICY_BLOCKED",
                    "checked_at": ts,
                    "expected_revision_hash": "sha256:aaa",
                    "observed_revision_hash": None,
                    "retryable": False,
                    "reason_code": "VISIBILITY_NOT_PUBLIC",
                },
            },
        )
    if content_unit_id == "cu_limit_422":
        record_request(request, 422)
        return JSONResponse(
            status_code=422,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "SOURCE_FETCH_LIMIT",
                    "message": "Max unit bytes exceeded",
                    "retryable": False,
                    "request_id": request.headers.get("x-request-id"),
                },
                "source_resolution": {
                    "status": "PARTIAL_WITH_LIMIT",
                    "checked_at": ts,
                    "expected_revision_hash": "sha256:aaa",
                    "observed_revision_hash": None,
                    "retryable": False,
                    "reason_code": "UNIT_BYTES_LIMIT_EXCEEDED",
                },
            },
        )
    if content_unit_id == "cu_unavail_503":
        record_request(request, 503)
        return JSONResponse(
            status_code=503,
            headers=make_headers(request),
            content={
                "error": {
                    "code": "SOURCE_UNAVAILABLE",
                    "message": "Upstream 503",
                    "retryable": True,
                    "request_id": request.headers.get("x-request-id"),
                },
                "source_resolution": {
                    "status": "UNAVAILABLE",
                    "checked_at": ts,
                    "expected_revision_hash": "sha256:aaa",
                    "observed_revision_hash": None,
                    "retryable": True,
                    "reason_code": "PROVIDER_RATE_LIMIT_OR_503",
                },
            },
        )
    record_request(request, 503)
    return JSONResponse(
        status_code=503,
        headers=make_headers(request),
        content={
            "error": {
                "code": "SOURCE_UNAVAILABLE",
                "message": "Provider returned 404",
                "retryable": False,
                "request_id": request.headers.get("x-request-id"),
            },
            "source_resolution": {
                "status": "UNAVAILABLE",
                "checked_at": ts,
                "expected_revision_hash": "sha256:aaa",
                "observed_revision_hash": None,
                "retryable": False,
                "reason_code": "PROVIDER_OBJECT_NOT_FOUND",
            },
        },
    )


# --- 4. Exports (/v1/exports*) ---


@app.get("/v1/exports")
async def list_exports(request: Request):
    items = [
        {k: v for k, v in e.items() if k not in ("rendered_markdown", "rendered_json", "manifest")}
        for e in store.data["exports"].values()
        if e["status"] != "DELETED"
    ]
    record_request(request, 200)
    return JSONResponse(status_code=200, headers=make_headers(request), content={"items": items})


@app.post("/v1/exports")
async def create_export(request: Request):
    body = await request.json()
    store.data["seq"] += 1
    exp_id = f"exp_http_{store.data['seq']}"
    fmt = body["format"]
    policy = body["export_policy"]
    claim_ids = body["claim_ids"]
    ts = now_iso()
    citations = [
        {
            "claim_id": cid,
            "claim_version": store.data["claims"][cid]["version"],
            "computed_verification": store.data["claims"][cid]["computed_verification"],
            "source_url": "https://github.com/demo/ledger/pull/88",
            "locator": {"schema": "cm-locator-v1", "file_path": "src/webhook/signature.ts", "start_line": 18, "end_line": 22},
            "revision_content_hash": "sha256:8f41c0d9e3a2b710",
            "observed_at": "2026-08-21T06:00:00Z",
            "checked_at": ts,
        }
        for cid in claim_ids
        if cid in store.data["claims"]
    ]
    exp = {
        "export_id": exp_id,
        "workspace_id": "ws_http_m4",
        "format": fmt,
        "export_policy": policy,
        "status": "ACTIVE",
        "claim_ids": claim_ids,
        "invalidation_reason": None,
        "created_at": ts,
        "rendered_markdown": f"# Export {exp_id}\n- Zero raw quotes" if fmt == "MARKDOWN" else None,
        "rendered_json": {"export_id": exp_id, "contains_raw_quotes": False, "citations": citations} if fmt == "JSON" else None,
        "manifest": {
            "export_policy": policy,
            "claim_count": len(citations),
            "includes_user_confirmed": policy == "ALLOW_USER_CONFIRMED",
            "contains_raw_quotes": False,
            "citations": citations,
        },
    }
    store.data["exports"][exp_id] = exp
    record_request(request, 201, body)
    return JSONResponse(status_code=201, headers=make_headers(request), content=exp)


@app.get("/v1/exports/{export_id}")
async def get_export_detail(export_id: str, request: Request):
    exp = store.data["exports"].get(export_id)
    if not exp or exp["status"] == "DELETED":
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    record_request(request, 200)
    return JSONResponse(status_code=200, headers=make_headers(request), content=exp)


@app.delete("/v1/exports/{export_id}")
async def delete_export(export_id: str, request: Request):
    exp = store.data["exports"].get(export_id)
    if not exp:
        record_request(request, 404)
        return JSONResponse(status_code=404, headers=make_headers(request), content={"error": {"code": "RESOURCE_NOT_FOUND"}})
    exp["status"] = "DELETED"
    ts = now_iso()
    record_request(request, 200)
    return JSONResponse(
        status_code=200,
        headers=make_headers(request),
        content={"export_id": export_id, "status": "DELETED", "deleted_at": ts},
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
