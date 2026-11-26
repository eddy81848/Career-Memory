"""Test-only local HTTP runner for CONTRACT's FastAPI stub on 127.0.0.1.

Strictly read-only against CONTRACT commit 1d754a5bc33cf166d92b1c1ce8568031859ff0a4
(OpenAPI 0.3.0-draft):
- Imports `FixtureActor`, `FixtureStore`, and `create_app` directly from CONTRACT's
  `spikes/import-contract/stub.py`.
- Binds ONLY to 127.0.0.1 on OS-assigned ephemeral ports.
- NEVER reads synthetic user or workspace identity from HTTP request headers;
  `FixtureActor` is injected strictly in Python via `create_app(store, actor)`.
- Exposes a separate loopback control server ONLY for test setup/observations
  (never connected to WEB).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request

DEFAULT_CONTRACT_DIR = (
    "/Users/eddy81848/orca/workspaces/Career Memory/"
    "T-106-Import-Contract/spikes/import-contract"
)
CONTRACT_IMPORT_DIR = Path(
    os.environ.get("CONTRACT_IMPORT_DIR", DEFAULT_CONTRACT_DIR)
)
if not (CONTRACT_IMPORT_DIR / "stub.py").is_file():
    raise RuntimeError(
        f"CONTRACT stub.py not found at {CONTRACT_IMPORT_DIR / 'stub.py'}"
    )

sys.path.insert(0, str(CONTRACT_IMPORT_DIR))

from contract import CONTRACT  # noqa: E402
from stub import FixtureActor, FixtureStore, create_app  # noqa: E402

STATUS_EXAMPLES = CONTRACT["paths"]["/v1/sync-runs/{id}"]["get"]["responses"]["200"][
    "content"
]["application/json"]["examples"]


def build_actor(actor_spec: dict[str, Any] | None) -> FixtureActor | None:
    if actor_spec is None:
        return None
    return FixtureActor(
        workspace_id=str(actor_spec["workspace_id"]),
        allowed_resources=frozenset(actor_spec.get("allowed_resources", [])),
        unreadable_runs=frozenset(actor_spec.get("unreadable_runs", [])),
    )


class HarnessState:
    def __init__(self) -> None:
        self.clock_offset = timedelta(0)
        self.store = FixtureStore(set(), clock=self.now)
        self.actor: FixtureActor | None = None
        self.app = create_app(self.store, self.actor)
        self.request_log: list[dict[str, Any]] = []
        self.queued_http_responses: list[dict[str, Any]] = []

    def now(self) -> datetime:
        return datetime.now(UTC) + self.clock_offset

    def reset(
        self,
        resources: list[list[str]],
        actor_spec: dict[str, Any] | None,
    ) -> None:
        self.clock_offset = timedelta(0)
        self.store = FixtureStore(
            {(ws, res) for ws, res in resources},
            clock=self.now,
        )
        self.actor = build_actor(actor_spec)
        self.app = create_app(self.store, self.actor)
        self.request_log = []
        self.queued_http_responses = []

    def set_actor(self, actor_spec: dict[str, Any] | None) -> None:
        self.actor = build_actor(actor_spec)
        self.app = create_app(self.store, self.actor)

    def advance_clock(self, *, hours: float = 0.0, seconds: float = 0.0) -> None:
        self.clock_offset += timedelta(hours=hours, seconds=seconds)

    def pop_matching_queued_response(
        self, method: str, full_path: str
    ) -> dict[str, Any] | None:
        for idx, spec in enumerate(self.queued_http_responses):
            expected_method = spec.get("method")
            expected_prefix = spec.get("path_prefix")
            if expected_method and expected_method != method:
                continue
            if expected_prefix and not full_path.startswith(expected_prefix):
                continue
            return self.queued_http_responses.pop(idx)
        return None


state = HarnessState()


async def contract_asgi_proxy(scope, receive, send):
    """Forwards HTTP traffic directly to CONTRACT's untouched `create_app` instance.

    Records method/path/query_string/Idempotency-Key/body_length/status in memory
    for test assertions. Does NOT inspect or forward any synthetic identity headers.
    """
    if scope["type"] != "http":
        await state.app(scope, receive, send)
        return

    query_string = scope.get("query_string", b"").decode("latin1")
    full_path = f"{scope['path']}?{query_string}" if query_string else scope["path"]
    headers_map = {
        k.decode("latin1").lower(): v.decode("latin1")
        for k, v in scope.get("headers", [])
    }

    queued = state.pop_matching_queued_response(scope["method"], full_path)
    if queued is not None:
        # Drain request body cleanly before replying with injected HTTP error
        body_len = 0
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body_len += len(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            else:
                break

        raw_body = str(queued.get("body", "")).encode("utf-8")
        response_headers: list[tuple[bytes, bytes]] = []
        if queued.get("content_type"):
            response_headers.append(
                (b"content-type", str(queued["content_type"]).encode("latin1"))
            )
        response_headers.append((b"content-length", str(len(raw_body)).encode("latin1")))
        status_code = int(queued["status"])

        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": response_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": raw_body,
                "more_body": False,
            }
        )
        state.request_log.append(
            {
                "method": scope["method"],
                "path": scope["path"],
                "query_string": query_string,
                "full_path": full_path,
                "idempotency_key": headers_map.get("idempotency-key"),
                "request_id": headers_map.get("x-request-id"),
                "body_length": body_len,
                "status": status_code,
                "injected": True,
            }
        )
        return

    body_chunks: list[bytes] = []

    async def recording_receive():
        message = await receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            if body:
                body_chunks.append(body)
        return message

    status_code_holder: dict[str, int | None] = {"status": None}

    async def recording_send(message):
        if message["type"] == "http.response.start":
            status_code_holder["status"] = int(message["status"])
        await send(message)

    await state.app(scope, recording_receive, recording_send)

    state.request_log.append(
        {
            "method": scope["method"],
            "path": scope["path"],
            "query_string": query_string,
            "full_path": full_path,
            "idempotency_key": headers_map.get("idempotency-key"),
            "request_id": headers_map.get("x-request-id"),
            "body_length": sum(len(chunk) for chunk in body_chunks),
            "status": status_code_holder["status"],
            "injected": False,
        }
    )


control_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


@control_app.post("/__control/reset")
async def control_reset(request: Request):
    body = await request.json()
    state.reset(
        resources=body.get("resources", []),
        actor_spec=body.get("actor"),
    )
    return {"ok": True}


@control_app.post("/__control/set-actor")
async def control_set_actor(request: Request):
    body = await request.json()
    state.set_actor(body.get("actor"))
    return {"ok": True}


@control_app.post("/__control/advance-clock")
async def control_advance_clock(request: Request):
    body = await request.json()
    state.advance_clock(
        hours=float(body.get("hours", 0.0)),
        seconds=float(body.get("seconds", 0.0)),
    )
    return {"ok": True, "now": state.now().isoformat()}


@control_app.post("/__control/queue-http-response")
async def control_queue_http_response(request: Request):
    body = await request.json()
    state.queued_http_responses.append(
        {
            "status": int(body["status"]),
            "body": body.get("body", ""),
            "content_type": body.get("content_type"),
            "method": body.get("method"),
            "path_prefix": body.get("path_prefix"),
        }
    )
    return {"ok": True}


@control_app.post("/__control/observe")
async def control_observe(request: Request):
    body = await request.json()
    workspace_id = body["workspace_id"]
    run_id = body["run_id"]
    if "example" in body and body["example"] is not None:
        observation = STATUS_EXAMPLES[body["example"]]["value"]
    else:
        observation = body["observation"]
    state.store.observe(workspace_id, run_id, observation)
    return {"ok": True}


@control_app.post("/__control/seed-create")
async def control_seed_create(request: Request):
    """Seeds an existing run directly in FixtureStore using a Python FixtureActor."""
    body = await request.json()
    actor = build_actor(body["actor"])
    assert actor is not None
    with state.store.lock:
        receipt = state.store.create(actor, body["key"], body["payload"])
    return {"ok": True, "receipt": receipt}


@control_app.get("/__control/state")
def control_state():
    with state.store.lock:
        runs_snapshot = {
            f"{ws}:{run_id}": dict(run.status)
            for (ws, run_id), run in state.store.runs.items()
        }
        create_replays = [
            key
            for (_ws, op, key) in state.store.replays.keys()
            if op == "create"
        ]
        retry_replays = [
            key
            for (_ws, op, key) in state.store.replays.keys()
            if op == "retry"
        ]
        return {
            "run_count": len(state.store.runs),
            "run_ids": [run_id for (_ws, run_id) in state.store.runs.keys()],
            "create_replay_count": len(create_replays),
            "retry_replay_count": len(retry_replays),
            "runs": runs_snapshot,
            "request_log": list(state.request_log),
            "clock_offset_seconds": state.clock_offset.total_seconds(),
        }


def bind_loopback_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


async def main() -> None:
    contract_sock = bind_loopback_socket()
    control_sock = bind_loopback_socket()
    contract_port = contract_sock.getsockname()[1]
    control_port = control_sock.getsockname()[1]

    contract_server = uvicorn.Server(
        uvicorn.Config(
            app=contract_asgi_proxy,
            host="127.0.0.1",
            port=contract_port,
            log_level="error",
            access_log=False,
        )
    )
    control_server = uvicorn.Server(
        uvicorn.Config(
            app=control_app,
            host="127.0.0.1",
            port=control_port,
            log_level="error",
            access_log=False,
        )
    )

    contract_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    control_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        contract_server.should_exit = True
        control_server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            pass

    contract_task = asyncio.create_task(contract_server.serve(sockets=[contract_sock]))
    control_task = asyncio.create_task(control_server.serve(sockets=[control_sock]))

    while not (contract_server.started and control_server.started):
        await asyncio.sleep(0.01)

    print(
        json.dumps(
            {
                "contractUrl": f"http://127.0.0.1:{contract_port}",
                "controlUrl": f"http://127.0.0.1:{control_port}",
            }
        ),
        flush=True,
    )

    try:
        await asyncio.gather(contract_task, control_task)
    finally:
        contract_sock.close()
        control_sock.close()


if __name__ == "__main__":
    asyncio.run(main())
