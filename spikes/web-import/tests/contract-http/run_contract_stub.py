"""Test-only local HTTP runner for CONTRACT's FastAPI stub on 127.0.0.1.

Strictly read-only against CONTRACT commit a0314f2cbcb33f1d34ed486db7b1009dcb4adc59:
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
        self.store = FixtureStore(set())
        self.actor: FixtureActor | None = None
        self.app = create_app(self.store, self.actor)
        self.request_log: list[dict[str, Any]] = []

    def reset(
        self,
        resources: list[list[str]],
        actor_spec: dict[str, Any] | None,
    ) -> None:
        self.store = FixtureStore({(ws, res) for ws, res in resources})
        self.actor = build_actor(actor_spec)
        self.app = create_app(self.store, self.actor)
        self.request_log = []

    def set_actor(self, actor_spec: dict[str, Any] | None) -> None:
        self.actor = build_actor(actor_spec)
        self.app = create_app(self.store, self.actor)


state = HarnessState()


async def contract_asgi_proxy(scope, receive, send):
    """Forwards HTTP traffic directly to CONTRACT's untouched `create_app` instance.

    Records method/path/Idempotency-Key/body_length/status in memory for test
    assertions. Does NOT inspect or forward any synthetic identity headers.
    """
    if scope["type"] != "http":
        await state.app(scope, receive, send)
        return

    body_chunks: list[bytes] = []
    body_complete = False

    async def recording_receive():
        nonlocal body_complete
        message = await receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            if body:
                body_chunks.append(body)
            if not message.get("more_body", False):
                body_complete = True
        return message

    status_code_holder: dict[str, int | None] = {"status": None}

    async def recording_send(message):
        if message["type"] == "http.response.start":
            status_code_holder["status"] = int(message["status"])
        await send(message)

    await state.app(scope, recording_receive, recording_send)

    headers_map = {
        k.decode("latin1").lower(): v.decode("latin1")
        for k, v in scope.get("headers", [])
    }
    state.request_log.append(
        {
            "method": scope["method"],
            "path": scope["path"],
            "idempotency_key": headers_map.get("idempotency-key"),
            "request_id": headers_map.get("x-request-id"),
            "body_length": sum(len(chunk) for chunk in body_chunks),
            "status": status_code_holder["status"],
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
