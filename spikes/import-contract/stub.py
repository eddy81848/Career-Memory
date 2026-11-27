"""In-process FastAPI fixture for the initial-import WEB review contract."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from threading import Lock
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from contract import CONTRACT, validator_for

INITIAL_REQUEST = validator_for({"$ref": "#/components/schemas/InitialImportRequest"})
RETRY_REQUEST = validator_for({"$ref": "#/components/schemas/RetryImportRequest"})
REQUEST_KEY = validator_for(CONTRACT["components"]["parameters"]["IdempotencyKey"]["schema"])
ACTIVE_STATES = {"PENDING", "RUNNING"}
RECEIPT_RETENTION = timedelta(hours=24)
READ_CACHE_CONTROL = "private, no-store"
ERROR_MESSAGES = {
    "SESSION_TERMINATED": "The session has ended.",
    "WORKSPACE_SCOPE_VIOLATION": "Access to the selected workspace has been revoked.",
    "RESOURCE_NOT_FOUND": "The requested resource was not found.",
    "AUTH_VERIFICATION_UNAVAILABLE": "Authentication could not be verified temporarily.",
    "SYNC_ALREADY_RUNNING": "An import is already active for the repository.",
    "RESOURCE_VERSION_CONFLICT": "The expected run version does not match the current run.",
    "IDEMPOTENCY_CONFLICT": "The request key identifies a different request.",
    "IDEMPOTENCY_KEY_EXPIRED": "The request key has expired.",
}


class FixtureAuthFailure(Enum):
    SESSION_TERMINATED = auto()
    VERIFICATION_UNAVAILABLE = auto()


@dataclass(frozen=True)
class FixtureActor:
    workspace_id: str
    allowed_resources: frozenset[str]
    unreadable_runs: frozenset[str] = frozenset()
    workspace_access_revoked: bool = False


@dataclass
class Run:
    id: str
    source_resource_id: str
    status: dict
    finished_at: datetime | None = None
    abandoned: bool = False
    version: int = 1

    def status_snapshot(self):
        return {**deepcopy(self.status), "run_version": self.version}

    def polling_target(self):
        return {"target_id": self.id, "status_url": f"/v1/sync-runs/{self.id}"}

    def receipt(self):
        return {"status": "PENDING", "target_type": "SYNC_RUN", **self.polling_target()}


@dataclass(frozen=True)
class ReplayEntry:
    request: dict | tuple[str, int]
    run_id: str
    receipt: dict
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
        self.submitted_work = []
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
            status = {**deepcopy(observation), "plan_id": run.status["plan_id"]}
            status.pop("run_version", None)
            if status != run.status:
                run.status = status
                run.version += 1

    def simulate_future_abandonment(self, actor, run_id):
        """Seed history for a future workflow outside the four HTTP routes."""
        with self.lock:
            run = self.find_run(actor, run_id)
            scope = (actor.workspace_id, run.source_resource_id)
            if self.run_for_resource[scope] is not run or run.status["status"] in (
                ACTIVE_STATES | {"SUCCESS"}
            ):
                raise FixtureRejection(409)
            run.abandoned = True
            run.status["retry"] = {"allowed": False}
            run.version += 1

    def require_resource(self, actor, source_resource_id):
        scope = (actor.workspace_id, source_resource_id)
        if scope not in self.resources or source_resource_id not in actor.allowed_resources:
            raise FixtureRejection(404, "RESOURCE_NOT_FOUND")
        return scope

    def find_run(self, actor, run_id):
        run = self.runs.get((actor.workspace_id, run_id))
        if run is None or run_id in actor.unreadable_runs:
            raise FixtureRejection(404, "RESOURCE_NOT_FOUND")
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
            "items": [{**selected.polling_target(), "import_status": selected.status_snapshot()}],
        }

    def replay(self, actor, operation, key, request):
        previous = self.replays.get((actor.workspace_id, operation, key))
        if previous is None:
            return None
        self.find_run(actor, previous.run_id)
        if self.clock() >= previous.expires_at:
            raise FixtureRejection(409, "IDEMPOTENCY_KEY_EXPIRED")
        if previous.request != request:
            raise FixtureRejection(409, "IDEMPOTENCY_CONFLICT")
        return deepcopy(previous.receipt)

    def accept(self, actor, operation, key, request, run):
        receipt = run.receipt()
        self.replays[actor.workspace_id, operation, key] = ReplayEntry(
            deepcopy(request), run.id, receipt, self.clock() + RECEIPT_RETENTION
        )
        # Tests count submissions here without starting a Worker.
        self.submitted_work.append((operation, run.id, run.version))
        return deepcopy(receipt)

    def create(self, actor, key, payload):
        source_resource_id = payload["source_resource_id"]
        scope = self.require_resource(actor, source_resource_id)
        replayed = self.replay(actor, "create", key, payload)
        if replayed is not None:
            return replayed

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
        return self.accept(actor, "create", key, payload, run)

    def retry(self, actor, run_id, key, expected_version):
        run = self.find_run(actor, run_id)
        request = (run_id, expected_version)
        replayed = self.replay(actor, "retry", key, request)
        if replayed is not None:
            return replayed

        # The route holds the store lock across version, eligibility and submission.
        if run.version != expected_version:
            raise FixtureRejection(409, "RESOURCE_VERSION_CONFLICT")
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
        run.version += 1
        return self.accept(actor, "retry", key, request, run)


def read_fixture_key(request):
    key = request.headers.get("Idempotency-Key")
    if not REQUEST_KEY.is_valid(key):
        raise FixtureRejection(422)
    return key


def create_app(store, actor=None, *, authenticate=None):
    # The test supplies this dependency. Request headers and bodies carry no identity.
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    resolve_identity = authenticate if authenticate is not None else lambda: actor

    def require_actor(identity=Depends(resolve_identity)):
        if identity is FixtureAuthFailure.SESSION_TERMINATED:
            raise FixtureRejection(401, "SESSION_TERMINATED")
        if identity is FixtureAuthFailure.VERIFICATION_UNAVAILABLE:
            raise FixtureRejection(503, "AUTH_VERIFICATION_UNAVAILABLE")
        if not isinstance(identity, FixtureActor):
            raise FixtureRejection(401)
        if identity.workspace_access_revoked:
            raise FixtureRejection(403, "WORKSPACE_SCOPE_VIOLATION")
        return identity

    @app.middleware("http")
    async def echo_request_id(request, call_next):
        response = await call_next(request)
        request_id = request.headers.get("X-Request-ID")
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(FixtureRejection)
    async def handle_rejection(request, error):
        if error.code is None:
            return Response(status_code=error.status_code)
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        body = {
            "code": error.code,
            "message": ERROR_MESSAGES[error.code],
            "retryable": error.code == "AUTH_VERIFICATION_UNAVAILABLE",
            "request_id": request_id,
        }
        if error.existing_run_id is not None:
            body["details"] = {"existing_run_id": error.existing_run_id}
        return JSONResponse(
            {"error": body},
            status_code=error.status_code,
            headers={"X-Request-ID": request_id, "Cache-Control": READ_CACHE_CONTROL},
        )

    @app.post("/v1/sync-runs", status_code=202)
    async def create_import(request: Request, current_actor=Depends(require_actor)):
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
    def recover_import(
        request: Request, response: Response, current_actor=Depends(require_actor)
    ):
        source_resource_id = request.query_params.get("source_resource_id")
        if not source_resource_id or request.query_params.get("limit", "1") != "1":
            raise FixtureRejection(422)
        with store.lock:
            result = store.recover(current_actor, source_resource_id)
        response.headers["Cache-Control"] = READ_CACHE_CONTROL
        return result

    @app.get("/v1/sync-runs/{id}", status_code=200)
    def import_status(id: str, response: Response, current_actor=Depends(require_actor)):
        with store.lock:
            result = store.find_run(current_actor, id).status_snapshot()
        response.headers["Cache-Control"] = READ_CACHE_CONTROL
        return result

    @app.post("/v1/sync-runs/{id}/retry", status_code=202)
    async def retry_import(id: str, request: Request, current_actor=Depends(require_actor)):
        key = read_fixture_key(request)
        try:
            payload = await request.json()
        except ValueError:
            raise FixtureRejection(422) from None
        if not RETRY_REQUEST.is_valid(payload):
            raise FixtureRejection(422)
        with store.lock:
            return store.retry(current_actor, id, key, payload["expected_run_version"])

    return app
