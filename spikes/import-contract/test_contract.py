from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import TestCase

from fastapi.testclient import TestClient
from openapi_spec_validator import OpenAPIV31SpecValidator

from contract import CONTRACT, validator_for
from stub import FixtureActor, FixtureStore, create_app

CREATE_PATH = "/v1/sync-runs"
STATUS_PATH = "/v1/sync-runs/{id}"
RETRY_PATH = "/v1/sync-runs/{id}/retry"
CREATE = CONTRACT["paths"][CREATE_PATH]["post"]
REQUEST = CREATE["requestBody"]["content"]["application/json"]
STATUS = CONTRACT["paths"][STATUS_PATH]["get"]["responses"]["200"]["content"][
    "application/json"
]
KEY = "00000000-0000-4000-8000-000000000001"
OTHER_KEY = "00000000-0000-4000-8000-000000000002"
RETRY_KEY = "00000000-0000-4000-8000-000000000003"


def objects_in(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects_in(child)


def resolve_reference(reference):
    if not reference.startswith("#/"):
        raise ValueError(f"Expected a local reference: {reference}")
    target = CONTRACT
    for token in reference[2:].split("/"):
        target = target[token.replace("~1", "/").replace("~0", "~")]
    return target


def response_definition(method, path, status_code):
    definition = CONTRACT["paths"][path][method]["responses"][str(status_code)]
    if "$ref" in definition:
        return resolve_reference(definition["$ref"])
    return definition


class OpenAPIContractTests(TestCase):
    def test_openapi_31_structure(self):
        OpenAPIV31SpecValidator(CONTRACT).validate()

    def test_all_references_resolve_without_network(self):
        references = [node["$ref"] for node in objects_in(CONTRACT) if "$ref" in node]
        self.assertTrue(references)
        for reference in references:
            with self.subTest(reference=reference):
                self.assertIsInstance(resolve_reference(reference), dict)

    def test_every_openapi_example_satisfies_its_schema(self):
        count = 0
        for node in objects_in(CONTRACT):
            if "schema" not in node:
                continue
            validator = validator_for(node["schema"])
            if "example" in node:
                validator.validate(node["example"])
                count += 1
            for name, example in node.get("examples", {}).items():
                with self.subTest(example=name):
                    validator.validate(example["value"])
                    count += 1
        self.assertGreaterEqual(count, 12)

    def test_scope_and_pending_wire_stay_out_of_payload_schemas(self):
        operations = {
            (method, path)
            for path, item in CONTRACT["paths"].items()
            for method in ("get", "post", "put", "patch", "delete")
            if method in item
        }
        self.assertEqual(
            operations,
            {
                ("post", "/v1/source-accounts/github"),
                ("get", "/v1/source-accounts"),
                ("post", "/v1/source-resources"),
                ("get", "/v1/source-resources"),
                ("post", CREATE_PATH),
                ("get", STATUS_PATH),
                ("post", RETRY_PATH),
            },
        )
        for method, path in operations:
            if path.startswith("/v1/sync-runs"):
                continue
            with self.subTest(path=path, method=method):
                operation = CONTRACT["paths"][path][method]
                self.assertNotIn("requestBody", operation)
                self.assertTrue(operation["x-pending-decisions"])
                for response in operation["responses"].values():
                    self.assertNotIn("content", response)
                self.assertNotIn("409", operation["responses"])

    def test_mutations_require_uuid_keys_and_receipts_only_require_polling_fields(self):
        for path in (CREATE_PATH, RETRY_PATH):
            operation = CONTRACT["paths"][path]["post"]
            parameters = [
                resolve_reference(parameter["$ref"]) for parameter in operation["parameters"]
            ]
            key = next(parameter for parameter in parameters if parameter["name"] == "Idempotency-Key")
            self.assertTrue(key["required"])
            self.assertEqual(key["schema"], {"type": "string", "format": "uuid"})
            receipt = response_definition("post", path, 202)["content"]["application/json"]["schema"]
            self.assertEqual(set(resolve_reference(receipt["$ref"])["required"]), {
                "target_id", "status_url",
            })
        self.assertNotIn("requestBody", CONTRACT["paths"][RETRY_PATH]["post"])

    def test_status_schema_rejects_invalid_presence_and_state_combinations(self):
        validator = validator_for(STATUS["schema"])
        examples = {name: entry["value"] for name, entry in STATUS["examples"].items()}
        partial = examples["transientPartial"]
        limited = examples["itemLimit"]
        drift = examples["planDrift"]
        succeeded = examples["policyChangedAfterSuccess"]
        invalid = {
            "partial missing failure reason": {k: v for k, v in partial.items() if k != "failure_reason"},
            "limited missing remaining range": {k: v for k, v in limited.items() if k != "remaining_range"},
            "known fields cannot be null": {**limited, "processed_range": None},
            "allowed retry missing checkpoint": {**partial, "retry": {"allowed": True}},
            "drift allows retry": {**drift, "retry": partial["retry"]},
            "drift flag missing factual drift": {**drift, "source_drift": False},
            "closed retry reports checkpoint": {
                **limited, "retry": {"allowed": False, "from_safe_checkpoint": True},
            },
            "success missing readiness": {**succeeded, "import_ready": False},
            "incomplete run starts processing": {**partial, "processing_status": "RUNNING"},
        }
        for label, value in invalid.items():
            with self.subTest(case=label):
                self.assertFalse(validator.is_valid(value))

class ImportFixtureTests(TestCase):
    def setUp(self):
        self.store = FixtureStore(
            {
                ("workspace-a", "synthetic-repo"),
                ("workspace-a", "second-repo"),
                ("workspace-a", "denied-repo"),
                ("workspace-b", "other-repo"),
            }
        )
        self.actor = FixtureActor(
            "workspace-a", frozenset({"synthetic-repo", "second-repo", "other-repo"})
        )
        self.client = TestClient(create_app(self.store, self.actor))

    def create_import(self, key=KEY, body=None, client=None, **headers):
        if key is not None:
            headers["Idempotency-Key"] = key
        return (client or self.client).post(
            CREATE_PATH,
            headers=headers,
            json=REQUEST["example"] if body is None else body,
        )

    def retry_import(self, run_id, key=RETRY_KEY, client=None, **headers):
        if key is not None:
            headers["Idempotency-Key"] = key
        return (client or self.client).post(RETRY_PATH.format(id=run_id), headers=headers)

    def assert_response(self, response, method, path, status, *, empty=False):
        self.assertEqual(response.status_code, status, response.text)
        definition = response_definition(method, path, status)
        if empty:
            self.assertEqual(response.content, b"")
            return None
        self.assertEqual(response.headers["content-type"], "application/json")
        schema = definition["content"]["application/json"]["schema"]
        body = response.json()
        validator_for(schema).validate(body)
        return body

    def assert_receipt(self, response, path=CREATE_PATH):
        body = self.assert_response(response, "post", path, 202)
        self.assertEqual(body["status_url"], STATUS_PATH.format(id=body["target_id"]))
        return body["target_id"], body["status_url"]

    def open_run(self, **kwargs):
        run_id, _ = self.assert_receipt(self.create_import(**kwargs))
        return run_id

    def get_status(self, run_id):
        response = self.client.get(STATUS_PATH.format(id=run_id))
        return self.assert_response(response, "get", STATUS_PATH, 200)

    def use_observation(self, run_id, name):
        self.store.observe(self.actor.workspace_id, run_id, STATUS["examples"][name]["value"])

    def assert_conflict(self, response, code, existing_id=None, path=CREATE_PATH):
        body = self.assert_response(response, "post", path, 409)
        expected = {"code": code}
        if existing_id is not None:
            expected["details"] = {"existing_run_id": existing_id}
        self.assertEqual(body, {"error": expected})

    def test_accepts_initial_import_and_reads_status_using_receipt_path(self):
        headers = {"X-Request-ID": "synthetic-request-1"}
        accepted = self.create_import(**headers)
        _, status_url = self.assert_receipt(accepted)
        self.assertEqual(accepted.headers["X-Request-ID"], headers["X-Request-ID"])
        response = self.client.get(status_url, headers=headers)
        status = self.assert_response(response, "get", STATUS_PATH, 200)
        self.assertEqual(response.headers["X-Request-ID"], headers["X-Request-ID"])
        self.assertEqual(status["status"], "PENDING")
        self.assertFalse(status["import_ready"])
        self.assertEqual(status["processing_status"], "NOT_STARTED")
        self.assertNotIn("failure_reason", status)
        self.assertNotIn("streams", status)

    def test_replays_same_creation_key_after_status_changes(self):
        first = self.assert_receipt(self.create_import())
        self.use_observation(first[0], "transientPartial")
        body = dict(reversed(list(REQUEST["example"].items())))
        self.assertEqual(self.assert_receipt(self.create_import(body=body)), first)
        self.assertEqual(self.get_status(first[0])["status"], "PARTIAL")

    def test_rejects_same_creation_key_with_changed_request(self):
        self.open_run()
        different = {**REQUEST["example"], "limits": {"max_objects": 200}}
        self.assert_conflict(self.create_import(body=different), "IDEMPOTENCY_CONFLICT")

    def test_rejects_distinct_request_while_pending_or_running(self):
        run_id = self.open_run()
        self.assert_conflict(self.create_import(key=OTHER_KEY), "SYNC_ALREADY_RUNNING", run_id)
        running = {**self.get_status(run_id), "status": "RUNNING"}
        self.store.observe(self.actor.workspace_id, run_id, running)
        self.assert_conflict(self.create_import(key=OTHER_KEY), "SYNC_ALREADY_RUNNING", run_id)

    def test_existing_run_id_appears_only_when_run_is_readable(self):
        run_id = self.open_run()
        restricted = FixtureActor(
            self.actor.workspace_id, self.actor.allowed_resources, frozenset({run_id})
        )
        client = TestClient(create_app(self.store, restricted))
        self.assert_conflict(self.create_import(key=OTHER_KEY, client=client), "SYNC_ALREADY_RUNNING")
        self.assert_response(self.create_import(client=client), "post", CREATE_PATH, 403, empty=True)
        self.assert_response(
            client.get(STATUS_PATH.format(id=run_id)), "get", STATUS_PATH, 403, empty=True
        )

    def test_concurrent_submissions_open_only_one_run(self):
        barrier = Barrier(2, timeout=5)

        def submit(key):
            with TestClient(create_app(self.store, self.actor)) as client:
                barrier.wait()
                return self.create_import(key=key, client=client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, (KEY, OTHER_KEY)))
        self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
        accepted, refused = sorted(responses, key=lambda response: response.status_code)
        run_id, _ = self.assert_receipt(accepted)
        self.assert_conflict(refused, "SYNC_ALREADY_RUNNING", run_id)

    def test_retry_returns_same_polling_target_and_preserves_checkpoint(self):
        first = self.assert_receipt(self.create_import())
        run_id, _ = first
        self.use_observation(run_id, "transientPartial")
        partial = self.get_status(run_id)
        self.assertEqual(partial["retry"], {"allowed": True, "from_safe_checkpoint": True})

        headers = {"X-Request-ID": "synthetic-retry"}
        response = self.retry_import(run_id, **headers)
        self.assertEqual(response.headers["X-Request-ID"], headers["X-Request-ID"])
        self.assertEqual(self.assert_receipt(response, RETRY_PATH), first)
        resumed = self.get_status(run_id)
        self.assertEqual(resumed["status"], "PENDING")
        self.assertEqual(resumed["plan_id"], partial["plan_id"])
        self.assertEqual(resumed["streams"], partial["streams"])
        self.assertEqual(resumed["streams"][0]["last_committed_page"], 7)
        self.assertEqual(resumed["processed_range"], partial["processed_range"])
        self.assertNotIn("failure_reason", resumed)
        self.assertEqual(resumed["retry"], {"allowed": False})
        self.assertEqual(self.assert_receipt(self.retry_import(run_id), RETRY_PATH), first)
        self.assertEqual(self.get_status(run_id), resumed)
        self.assert_conflict(
            self.retry_import(run_id, key=OTHER_KEY), "SYNC_ALREADY_RUNNING", run_id, RETRY_PATH
        )

        # A replay also leaves a later observation in place.
        self.use_observation(run_id, "transientPartial")
        self.assertEqual(self.assert_receipt(self.retry_import(run_id), RETRY_PATH), first)
        self.assertEqual(self.get_status(run_id), partial)

    def test_same_retry_key_for_another_run_conflicts(self):
        first = self.open_run()
        second = self.open_run(
            key=OTHER_KEY, body={**REQUEST["example"], "source_resource_id": "second-repo"}
        )
        for run_id in (first, second):
            self.use_observation(run_id, "transientPartial")
        self.assert_receipt(self.retry_import(first), RETRY_PATH)
        self.assert_conflict(self.retry_import(second), "IDEMPOTENCY_CONFLICT", path=RETRY_PATH)
        self.assertEqual(self.get_status(second)["status"], "PARTIAL")

    def test_limit_drift_and_policy_block_are_200_statuses_with_retry_disabled(self):
        run_id = self.open_run()
        expected = {
            "itemLimit": ("PARTIAL_WITH_LIMIT", "SYNC_PARTIAL_WITH_LIMIT"),
            "planDrift": ("PARTIAL", "PLAN_DRIFT"),
            "importPolicyBlocked": ("PARTIAL", "SOURCE_POLICY_BLOCKED"),
        }
        for fixture, (state, reason) in expected.items():
            with self.subTest(fixture=fixture):
                self.use_observation(run_id, fixture)
                status = self.get_status(run_id)
                self.assertEqual(status["status"], state)
                self.assertEqual(status["failure_reason"], reason)
                self.assertEqual(status["source_drift"], fixture == "planDrift")
                self.assertEqual(status["retry"], {"allowed": False})
                self.assertFalse(status["import_ready"])
                self.assertEqual(status["processing_status"], "NOT_STARTED")
                if fixture == "itemLimit":
                    self.assertTrue(status["processed_range"])
                    self.assertTrue(status["remaining_range"])
                self.assert_response(self.retry_import(run_id), "post", RETRY_PATH, 409, empty=True)
                self.assert_response(
                    self.create_import(key=OTHER_KEY), "post", CREATE_PATH, 409, empty=True
                )
                self.assertEqual(self.get_status(run_id), status)

    def test_transient_failure_without_safe_checkpoint_is_not_retryable(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        status = {**self.get_status(run_id), "retry": {"allowed": False}}
        self.store.observe(self.actor.workspace_id, run_id, status)
        self.assert_response(self.retry_import(run_id), "post", RETRY_PATH, 409, empty=True)
        self.assertEqual(self.get_status(run_id), status)

    def test_later_policy_block_preserves_historical_import_success(self):
        run_id = self.open_run()
        self.use_observation(run_id, "policyChangedAfterSuccess")
        status = self.get_status(run_id)
        self.assertEqual(status["status"], "SUCCESS")
        self.assertTrue(status["import_ready"])
        self.assertEqual(status["processing_status"], "BLOCKED_POLICY")
        self.assertNotIn("failure_reason", status)
        self.assertEqual(status["retry"], {"allowed": False})
        self.assert_response(self.create_import(key=OTHER_KEY), "post", CREATE_PATH, 409, empty=True)
        self.assert_response(self.retry_import(run_id), "post", RETRY_PATH, 409, empty=True)

    def test_checks_repository_permissions_before_create_and_retry_replay(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        self.assert_receipt(self.retry_import(run_id), RETRY_PATH)
        revoked = TestClient(create_app(self.store, FixtureActor("workspace-a", frozenset())))
        self.assert_response(self.create_import(client=revoked), "post", CREATE_PATH, 403, empty=True)
        self.assert_response(
            self.retry_import(run_id, client=revoked), "post", RETRY_PATH, 403, empty=True
        )
        denied = {**REQUEST["example"], "source_resource_id": "denied-repo"}
        self.assert_response(self.create_import(body=denied), "post", CREATE_PATH, 403, empty=True)

    def test_prevents_cross_workspace_writes_reads_and_retries(self):
        other_actor = FixtureActor("workspace-b", frozenset({"other-repo"}))
        other_client = TestClient(create_app(self.store, other_actor))
        foreign_body = {**REQUEST["example"], "source_resource_id": "other-repo"}
        self.assert_response(self.create_import(body=foreign_body), "post", CREATE_PATH, 403, empty=True)
        local_run = self.open_run()
        foreign_run = self.open_run(body=foreign_body, client=other_client)
        self.assertNotEqual(local_run, foreign_run)
        for candidate in (foreign_run, "unknown-run"):
            self.assert_response(
                self.client.get(STATUS_PATH.format(id=candidate)), "get", STATUS_PATH, 403, empty=True
            )
            self.assert_response(
                self.retry_import(candidate), "post", RETRY_PATH, 403, empty=True
            )

    def test_browser_headers_cannot_supply_fixture_identity(self):
        client = TestClient(create_app(self.store))
        response = client.post(
            CREATE_PATH,
            headers={"Authorization": "Bearer synthetic", "X-Workspace-ID": "workspace-a"},
            json={**REQUEST["example"], "workspace_id": "workspace-a"},
        )
        self.assert_response(response, "post", CREATE_PATH, 401, empty=True)

    def test_missing_and_invalid_keys_are_rejected_on_both_mutations(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        status = self.get_status(run_id)
        for key in (None, "invalid"):
            with self.subTest(key=key):
                self.assert_response(self.create_import(key=key), "post", CREATE_PATH, 422, empty=True)
                self.assert_response(
                    self.retry_import(run_id, key=key), "post", RETRY_PATH, 422, empty=True
                )
        self.assertEqual(self.get_status(run_id), status)

    def test_retry_request_has_no_body(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        status = self.get_status(run_id)
        response = self.client.post(
            RETRY_PATH.format(id=run_id), headers={"Idempotency-Key": RETRY_KEY}, json={}
        )
        self.assert_response(response, "post", RETRY_PATH, 422, empty=True)
        self.assertEqual(self.get_status(run_id), status)

    def test_requires_explicit_mode_and_valid_date(self):
        valid = REQUEST["example"]
        without_mode = {k: v for k, v in valid.items() if k != "mode"}
        for body in (without_mode, {**valid, "mode": "INCREMENTAL"}, {**valid, "backfill_from": "2025-02-30"}):
            with self.subTest(body=body):
                self.assert_response(self.create_import(body=body), "post", CREATE_PATH, 422, empty=True)
