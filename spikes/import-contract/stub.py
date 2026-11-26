"""In-process FastAPI fixture for the initial-import WEB review contract."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from contract import CONTRACT, validator_for

INITIAL_REQUEST = validator_for({"$ref": "#/components/schemas/InitialImportRequest"})
REQUEST_KEY = validator_for(CONTRACT["components"]["parameters"]["IdempotencyKey"]["schema"])
ACTIVE_STATES = {"PENDING", "RUNNING"}
RECEIPT_RETENTION = timedelta(hours=24)
READ_CACHE_CONTROL = "private, no-store"


@dataclass(frozen=True)
class FixtureActor:
    workspace_id: str
    allowed_resources: frozenset[str]
    unreadable_runs: frozenset[str] = frozenset()


@dataclass
class Run:
    id: str
    source_resource_id: str
    status: dict
    finished_at: datetime | None = None
    abandoned: bool = False

    def polling_target(self):
        return {"target_id": self.id, "status_url": f"/v1/sync-runs/{self.id}"}

    def receipt(self):
        return {"status": "PENDING", "target_type": "SYNC_RUN", **self.polling_target()}


@dataclass(frozen=True)
class ReplayEntry:
    request: dict | str
    run_id: str
    expires_at: datetime


class FixtureRejection(Exception):
    def __init__(self, status_code, code=None, existing_run_id=None):
        super().__init__(code or status_code)
        self.status_code = status_code
        self.code = code
        self.existing_run_id = existing_run_id


class FixtureStore:
    def __init__(self, resources, *, clock=lambda: datetime.now(UTC)):
        self.resources = set(resources)
        self.clock = clock
        self.runs = {}
        self.run_for_resource = {}
        self.replays = {}
        self.lock = Lock()

    def observe(self, workspace_id, run_id, observation):
        """Set a synthetic provider/job outcome directly from a test."""
        with self.lock:
            run = self.runs[workspace_id, run_id]
            if run.abandoned:
                raise ValueError("Cannot add a provider observation to an abandoned plan")
            if observation["status"] in ACTIVE_STATES:
                run.finished_at = None
            elif run.finished_at is None:
                run.finished_at = self.clock()
            run.status = {**deepcopy(observation), "plan_id": run.status["plan_id"]}

    def abandon_plan(self, actor, run_id):
        """Simulate a separately confirmed abandonment. No HTTP route exposes it."""
        with self.lock:
            run = self.find_run(actor, run_id)
            scope = (actor.workspace_id, run.source_resource_id)
            if self.run_for_resource[scope] is not run or run.status["status"] in (
                ACTIVE_STATES | {"SUCCESS"}
            ):
                raise FixtureRejection(409)
            run.abandoned = True
            run.status["retry"] = {"allowed": False}

    def require_resource(self, actor, source_resource_id):
        scope = (actor.workspace_id, source_resource_id)
        if scope not in self.resources or source_resource_id not in actor.allowed_resources:
            raise FixtureRejection(403)
        return scope

    def find_run(self, actor, run_id):
        run = self.runs.get((actor.workspace_id, run_id))
        if run is None or run_id in actor.unreadable_runs:
            raise FixtureRejection(403)
        self.require_resource(actor, run.source_resource_id)
        return run

    def recover(self, actor, source_resource_id):
        self.require_resource(actor, source_resource_id)
        visible_runs = [
            run
            for (workspace_id, run_id), run in self.runs.items()
            if workspace_id == actor.workspace_id
            and run.source_resource_id == source_resource_id
            and run_id not in actor.unreadable_runs
        ]
        selected = next(
            (run for run in visible_runs if run.status["status"] in ACTIVE_STATES), None
        )
        if selected is None:
            # In an exact timestamp tie, the most recently created run wins.
            selected = max(reversed(visible_runs), key=lambda run: run.finished_at, default=None)

        if selected is None:
            return {"items": []}
        return {
            "items": [{**selected.polling_target(), "import_status": deepcopy(selected.status)}],
        }

    def replay(self, actor, operation, key, request):
        previous = self.replays.get((actor.workspace_id, operation, key))
        if previous is None:
            return None
        run = self.find_run(actor, previous.run_id)
        if self.clock() >= previous.expires_at:
            raise FixtureRejection(409, "IDEMPOTENCY_KEY_EXPIRED")
        if previous.request != request:
            raise FixtureRejection(409, "IDEMPOTENCY_CONFLICT")
        return run

    def record_replay(self, actor, operation, key, request, run):
        self.replays[actor.workspace_id, operation, key] = ReplayEntry(
            request, run.id, self.clock() + RECEIPT_RETENTION
        )

    def create(self, actor, key, payload):
        source_resource_id = payload["source_resource_id"]
        scope = self.require_resource(actor, source_resource_id)
        replayed = self.replay(actor, "create", key, payload)
        if replayed is not None:
            return replayed.receipt()

        existing = self.run_for_resource.get(scope)
        if existing is not None and not existing.abandoned:
            if existing.status["status"] in ACTIVE_STATES:
                # The repository scope is already checked. The run has its own read gate.
                readable_id = None if existing.id in actor.unreadable_runs else existing.id
                raise FixtureRejection(409, "SYNC_ALREADY_RUNNING", readable_id)
            raise FixtureRejection(409)

        ordinal = len(self.runs) + 1
        run = Run(
            id=f"synthetic-run-{ordinal}",
            source_resource_id=source_resource_id,
            status={
                "status": "PENDING",
                "plan_id": f"synthetic-plan-{ordinal}",
                "import_ready": False,
                "processing_status": "NOT_STARTED",
                "source_drift": False,
                "retry": {"allowed": False},
            },
        )
        self.runs[actor.workspace_id, run.id] = run
        self.run_for_resource[scope] = run
        self.record_replay(actor, "create", key, payload, run)
        return run.receipt()

    def retry(self, actor, run_id, key):
        run = self.find_run(actor, run_id)
        replayed = self.replay(actor, "retry", key, run_id)
        if replayed is not None:
            return replayed.receipt()

        status = run.status
        if status["status"] in ACTIVE_STATES:
            raise FixtureRejection(409, "SYNC_ALREADY_RUNNING", run.id)
        if (
            run.abandoned
            or status["status"] != "PARTIAL"
            or status.get("failure_reason") != "TRANSIENT_PROVIDER_ERROR"
            or status["source_drift"]
            or not status["retry"]["allowed"]
            or not status["retry"].get("from_safe_checkpoint")
        ):
            raise FixtureRejection(409)

        # Keep the plan, committed page and range progress for the same run.
        status.update(status="PENDING", retry={"allowed": False})
        status.pop("failure_reason")
        run.finished_at = None
        self.record_replay(actor, "retry", key, run_id, run)
        return run.receipt()


def read_fixture_key(request):
    key = request.headers.get("Idempotency-Key")
    if not REQUEST_KEY.is_valid(key):
        raise FixtureRejection(422)
    return key


def create_app(store, actor=None):
    # Only the calling test can supply the actor. There is no live app entry point.
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    def require_actor():
        if actor is None:
            raise FixtureRejection(401)
        return actor

    @app.middleware("http")
    async def echo_request_id(request, call_next):
        response = await call_next(request)
        request_id = request.headers.get("X-Request-ID")
        if request_id is not None:
            response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(FixtureRejection)
    async def handle_rejection(_request, error):
        if error.code is None:
            return Response(status_code=error.status_code)
        body = {"code": error.code}
        if error.existing_run_id is not None:
            body["details"] = {"existing_run_id": error.existing_run_id}
        return JSONResponse({"error": body}, status_code=error.status_code)

    @app.post("/v1/sync-runs", status_code=202)
    async def create_import(request: Request):
        current_actor = require_actor()
        key = read_fixture_key(request)
        try:
            payload = await request.json()
        except ValueError:
            raise FixtureRejection(422) from None
        if not INITIAL_REQUEST.is_valid(payload):
            raise FixtureRejection(422)
        with store.lock:
            return store.create(current_actor, key, payload)

    @app.get("/v1/sync-runs", status_code=200)
    def recover_import(request: Request, response: Response):
        current_actor = require_actor()
        source_resource_id = request.query_params.get("source_resource_id")
        if not source_resource_id or request.query_params.get("limit", "1") != "1":
            raise FixtureRejection(422)
        with store.lock:
            result = store.recover(current_actor, source_resource_id)
        response.headers["Cache-Control"] = READ_CACHE_CONTROL
        return result

    @app.get("/v1/sync-runs/{id}", status_code=200)
    def import_status(id: str, response: Response):
        current_actor = require_actor()
        with store.lock:
            result = deepcopy(store.find_run(current_actor, id).status)
        response.headers["Cache-Control"] = READ_CACHE_CONTROL
        return result

    @app.post("/v1/sync-runs/{id}/retry", status_code=202)
    async def retry_import(id: str, request: Request):
        current_actor = require_actor()
        key = read_fixture_key(request)
        if await request.body():
            raise FixtureRejection(422)
        with store.lock:
            return store.retry(current_actor, id, key)

    return app
