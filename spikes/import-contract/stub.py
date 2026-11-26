"""In-process FastAPI fixture for the documented initial-import behavior."""

from copy import deepcopy
from dataclasses import dataclass
from threading import Lock

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from contract import CONTRACT, validator_for

INITIAL_REQUEST = validator_for({"$ref": "#/components/schemas/InitialImportRequest"})
REQUEST_KEY = validator_for(CONTRACT["components"]["parameters"]["IdempotencyKey"]["schema"])
ACTIVE_STATES = {"PENDING", "RUNNING"}

# Fixture transport only. These statuses and payload keys do not define the API.
FIXTURE_HTTP = {
    "invalid-request": 422,
    "unauthenticated-fixture": 401,
    "resource-unavailable": 404,
    "SYNC_ALREADY_RUNNING": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "already-imported": 409,
    "new-plan-required": 409,
    "retry-disallowed": 409,
}


@dataclass(frozen=True)
class FixtureActor:
    workspace_id: str
    allowed_resources: frozenset[str]


@dataclass
class Run:
    id: str
    job_id: str
    source_resource_id: str
    status: dict

    def receipt(self):
        return {
            "job_id": self.job_id,
            "status": "PENDING",
            "target_type": "SYNC_RUN",
            "target_id": self.id,
        }


class FixtureRejection(Exception):
    def __init__(self, outcome, existing_run_id=None):
        super().__init__(outcome)
        self.outcome = outcome
        self.existing_run_id = existing_run_id


class FixtureStore:
    def __init__(self, resources):
        self.resources = set(resources)
        self.runs = {}
        self.run_for_resource = {}
        self.replays = {}
        self.lock = Lock()

    def observe(self, workspace_id, run_id, observation):
        """Set a synthetic provider/job outcome directly from a test."""
        with self.lock:
            run = self.runs[workspace_id, run_id]
            run.status = {**deepcopy(observation), "plan_id": run.status["plan_id"]}

    def require_resource(self, actor, source_resource_id):
        scope = (actor.workspace_id, source_resource_id)
        if scope not in self.resources or source_resource_id not in actor.allowed_resources:
            raise FixtureRejection("resource-unavailable")
        return scope

    def find_run(self, actor, run_id):
        run = self.runs.get((actor.workspace_id, run_id))
        if run is None:
            raise FixtureRejection("resource-unavailable")
        self.require_resource(actor, run.source_resource_id)
        return run

    def replay(self, workspace_id, operation, key, fingerprint):
        if key is None:
            return None
        previous = self.replays.get((workspace_id, operation, key))
        if previous is None:
            return None
        previous_fingerprint, run = previous
        if previous_fingerprint != fingerprint:
            raise FixtureRejection("IDEMPOTENCY_CONFLICT")
        return run

    def record_replay(self, workspace_id, operation, key, fingerprint, run):
        if key is not None:
            self.replays[workspace_id, operation, key] = (fingerprint, run)

    def create(self, actor, key, payload):
        source_resource_id = payload["source_resource_id"]
        scope = self.require_resource(actor, source_resource_id)
        fingerprint = (
            source_resource_id,
            payload["mode"],
            payload.get("backfill_from"),
            payload.get("limits", {}).get("max_objects"),
        )
        replayed = self.replay(actor.workspace_id, "create", key, fingerprint)
        if replayed is not None:
            return replayed.receipt()

        existing = self.run_for_resource.get(scope)
        if existing is not None:
            state = existing.status["status"]
            if state in ACTIVE_STATES:
                raise FixtureRejection("SYNC_ALREADY_RUNNING", existing.id)
            if state == "SUCCESS":
                raise FixtureRejection("already-imported")
            raise FixtureRejection("new-plan-required")

        ordinal = len(self.runs) + 1
        run = Run(
            id=f"synthetic-run-{ordinal}",
            job_id=f"synthetic-job-{ordinal}",
            source_resource_id=source_resource_id,
            status={
                "status": "PENDING",
                "plan_id": f"synthetic-plan-{ordinal}",
                "import_ready": False,
                "processing_status": "NOT_STARTED",
                "retry": {"allowed": False},
            },
        )
        self.runs[actor.workspace_id, run.id] = run
        self.run_for_resource[scope] = run
        self.record_replay(actor.workspace_id, "create", key, fingerprint, run)
        return run.receipt()

    def retry(self, actor, run_id, key):
        run = self.find_run(actor, run_id)
        if self.replay(actor.workspace_id, "retry", key, run.id) is not None:
            return

        status = run.status
        if (
            status["status"] != "PARTIAL"
            or status.get("failure_reason") != "TRANSIENT_PROVIDER_ERROR"
            or status.get("source_drift", False)
            or not status["retry"]["allowed"]
            or not status["retry"].get("from_safe_checkpoint")
        ):
            raise FixtureRejection("retry-disallowed")

        # Keep the plan, committed page and range progress for the same run.
        status.update(status="PENDING", retry={"allowed": False})
        status.pop("failure_reason")
        self.record_replay(actor.workspace_id, "retry", key, run.id, run)


def read_fixture_key(request):
    key = request.headers.get("Idempotency-Key")
    if key is not None and not REQUEST_KEY.is_valid(key):
        raise FixtureRejection("invalid-request")
    return key


def create_app(store, actor=None):
    # Only the calling test can supply the actor. No app instance or live
    # authentication entry point is provided for serving this fixture.
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    def require_actor():
        if actor is None:
            raise FixtureRejection("unauthenticated-fixture")
        return actor

    @app.exception_handler(FixtureRejection)
    async def handle_rejection(_request, error):
        body = {"fixture_outcome": error.outcome}
        if error.existing_run_id is not None:
            body["fixture_existing_run_id"] = error.existing_run_id
        return JSONResponse(body, status_code=FIXTURE_HTTP[error.outcome])

    @app.post("/v1/sync-runs", status_code=202)
    async def create_import(request: Request):
        current_actor = require_actor()
        try:
            payload = await request.json()
        except ValueError:
            raise FixtureRejection("invalid-request") from None
        if not INITIAL_REQUEST.is_valid(payload) or "mode" not in payload:
            raise FixtureRejection("invalid-request")
        key = read_fixture_key(request)
        with store.lock:
            return store.create(current_actor, key, payload)

    @app.get("/v1/sync-runs/{id}")
    def import_status(id: str):
        current_actor = require_actor()
        with store.lock:
            return deepcopy(store.find_run(current_actor, id).status)

    @app.post("/v1/sync-runs/{id}/retry")
    def retry_import(id: str, request: Request):
        current_actor = require_actor()
        key = read_fixture_key(request)
        with store.lock:
            store.retry(current_actor, id, key)
        return Response(status_code=204)

    return app
