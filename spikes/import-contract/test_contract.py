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
RECEIPT = CREATE["responses"]["202"]["content"]["application/json"]
STATUS = CONTRACT["paths"][STATUS_PATH]["get"]["responses"]["2XX"]["content"][
    "application/json"
]
KEY = "00000000-0000-4000-8000-000000000001"
OTHER_KEY = "00000000-0000-4000-8000-000000000002"


def objects_in(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects_in(child)


class OpenAPIContractTests(TestCase):
    def test_openapi_31_structure(self):
        OpenAPIV31SpecValidator(CONTRACT).validate()

    def test_all_references_resolve_without_network(self):
        references = [node["$ref"] for node in objects_in(CONTRACT) if "$ref" in node]
        self.assertTrue(references)
        for reference in references:
            with self.subTest(reference=reference):
                self.assertTrue(reference.startswith("#/"))
                target = CONTRACT
                for token in reference[2:].split("/"):
                    target = target[token.replace("~1", "/").replace("~0", "~")]
                self.assertIsInstance(target, dict)

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
        self.assertGreaterEqual(count, 11)

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
        for path in ("/v1/source-accounts/github", "/v1/source-accounts", "/v1/source-resources", RETRY_PATH):
            for method in ("get", "post"):
                operation = CONTRACT["paths"][path].get(method)
                if operation is None:
                    continue
                with self.subTest(path=path, method=method):
                    self.assertNotIn("requestBody", operation)
                    self.assertTrue(operation["x-pending-decisions"])
                    for response in operation["responses"].values():
                        self.assertNotIn("content", response)
        for method, path in operations:
            self.assertNotIn("409", CONTRACT["paths"][path][method]["responses"])


class ImportFixtureTests(TestCase):
    def setUp(self):
        self.store = FixtureStore(
            {
                ("workspace-a", "synthetic-repo"),
                ("workspace-a", "denied-repo"),
                ("workspace-b", "other-repo"),
            }
        )
        self.actor = FixtureActor("workspace-a", frozenset({"synthetic-repo", "other-repo"}))
        self.client = TestClient(create_app(self.store, self.actor))

    def create_import(self, key=KEY, body=None, client=None):
        return (client or self.client).post(
            CREATE_PATH,
            headers={"Idempotency-Key": key},
            json=REQUEST["example"] if body is None else body,
        )

    def open_run(self):
        response = self.create_import()
        self.assertEqual(response.status_code, 202)
        validator_for(RECEIPT["schema"]).validate(response.json())
        return response.json()["target_id"]

    def get_status(self, run_id):
        response = self.client.get(STATUS_PATH.format(id=run_id))
        self.assertTrue(response.is_success)
        validator_for(STATUS["schema"]).validate(response.json())
        return response.json()

    def use_observation(self, run_id, name):
        self.store.observe(self.actor.workspace_id, run_id, STATUS["examples"][name]["value"])

    def assert_fixture_outcome(self, response, expected, existing_id=None):
        self.assertFalse(response.is_success)
        body = {"fixture_outcome": expected}
        if existing_id is not None:
            body["fixture_existing_run_id"] = existing_id
        self.assertEqual(response.json(), body)

    def test_accepts_initial_import_and_reads_status(self):
        status = self.get_status(self.open_run())
        self.assertEqual(status["status"], "PENDING")
        self.assertFalse(status["import_ready"])
        self.assertEqual(status["processing_status"], "NOT_STARTED")

    def test_replays_same_creation_key_after_status_changes(self):
        first = self.create_import()
        receipt = first.json()
        self.use_observation(receipt["target_id"], "transientPartial")
        replay = self.create_import()
        self.assertTrue(replay.is_success)
        self.assertEqual(replay.json(), receipt)
        self.assertEqual(self.get_status(receipt["target_id"])["status"], "PARTIAL")

    def test_rejects_same_key_with_changed_request(self):
        self.open_run()
        other_request = {**REQUEST["example"], "limits": {"max_objects": 200}}
        self.assert_fixture_outcome(
            self.create_import(body=other_request), "IDEMPOTENCY_CONFLICT"
        )

    def test_rejects_distinct_request_while_pending_or_running(self):
        run_id = self.open_run()
        self.assert_fixture_outcome(
            self.create_import(key=OTHER_KEY), "SYNC_ALREADY_RUNNING", run_id
        )
        running = {**self.get_status(run_id), "status": "RUNNING"}
        self.store.observe(self.actor.workspace_id, run_id, running)
        self.assert_fixture_outcome(
            self.create_import(key=OTHER_KEY), "SYNC_ALREADY_RUNNING", run_id
        )

    def test_concurrent_submissions_open_only_one_run(self):
        barrier = Barrier(2, timeout=5)

        def submit(key):
            with TestClient(create_app(self.store, self.actor)) as client:
                barrier.wait()
                return self.create_import(key=key, client=client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, (KEY, OTHER_KEY)))
        accepted = [response for response in responses if response.is_success]
        refused = [response for response in responses if not response.is_success]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(refused), 1)
        self.assert_fixture_outcome(
            refused[0], "SYNC_ALREADY_RUNNING", accepted[0].json()["target_id"]
        )

    def test_resumes_the_same_plan_and_preserves_the_checkpoint(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        partial = self.get_status(run_id)
        self.assertEqual(partial["retry"], {"allowed": True, "from_safe_checkpoint": True})
        self.assertFalse(partial["import_ready"])

        path = RETRY_PATH.format(id=run_id)
        headers = {"Idempotency-Key": OTHER_KEY}
        self.assertTrue(self.client.post(path, headers=headers).is_success)
        resumed = self.get_status(run_id)
        self.assertEqual(resumed["status"], "PENDING")
        self.assertEqual(resumed["plan_id"], partial["plan_id"])
        self.assertEqual(resumed["streams"], partial["streams"])
        self.assertEqual(resumed["streams"][0]["last_committed_page"], 7)
        self.assertEqual(resumed["processed_range"], partial["processed_range"])
        self.assertTrue(self.client.post(path, headers=headers).is_success)
        self.assertEqual(self.get_status(run_id), resumed)
        self.assert_fixture_outcome(self.client.post(path), "retry-disallowed")

    def test_plan_drift_disallows_retry_and_requires_plan_abandonment(self):
        run_id = self.open_run()
        self.use_observation(run_id, "planDrift")
        status = self.get_status(run_id)
        self.assertEqual(status["failure_reason"], "PLAN_DRIFT")
        self.assertTrue(status["source_drift"])
        self.assertFalse(status["retry"]["allowed"])
        self.assert_fixture_outcome(
            self.client.post(RETRY_PATH.format(id=run_id)), "retry-disallowed"
        )
        self.assert_fixture_outcome(
            self.create_import(key=OTHER_KEY), "new-plan-required"
        )
        self.assertEqual(self.get_status(run_id), status)

    def test_limit_completion_reports_range_without_promoting_import(self):
        run_id = self.open_run()
        self.use_observation(run_id, "itemLimit")
        status = self.get_status(run_id)
        self.assertEqual(status["status"], "PARTIAL_WITH_LIMIT")
        self.assertTrue(status["processed_range"])
        self.assertTrue(status["remaining_range"])
        self.assertFalse(status["import_ready"])
        self.assertFalse(status["retry"]["allowed"])
        self.assertEqual(status["processing_status"], "NOT_STARTED")
        self.assert_fixture_outcome(
            self.client.post(RETRY_PATH.format(id=run_id)), "retry-disallowed"
        )
        self.assertEqual(self.get_status(run_id), status)

    def test_later_policy_block_preserves_historical_import_success(self):
        run_id = self.open_run()
        self.use_observation(run_id, "policyChangedAfterSuccess")
        status = self.get_status(run_id)
        self.assertEqual(status["status"], "SUCCESS")
        self.assertTrue(status["import_ready"])
        self.assertEqual(status["processing_status"], "BLOCKED_POLICY")
        self.assertFalse(status["retry"]["allowed"])
        self.assert_fixture_outcome(
            self.create_import(key=OTHER_KEY), "already-imported"
        )

    def test_checks_repository_permission_before_acceptance_and_replay(self):
        self.open_run()
        revoked = TestClient(
            create_app(self.store, FixtureActor("workspace-a", frozenset()))
        )
        self.assert_fixture_outcome(
            self.create_import(client=revoked), "resource-unavailable"
        )
        denied = {**REQUEST["example"], "source_resource_id": "denied-repo"}
        self.assert_fixture_outcome(
            self.create_import(body=denied), "resource-unavailable"
        )

    def test_prevents_cross_workspace_writes_reads_and_retries(self):
        other_actor = FixtureActor("workspace-b", frozenset({"other-repo"}))
        other_client = TestClient(create_app(self.store, other_actor))
        foreign_body = {**REQUEST["example"], "source_resource_id": "other-repo"}
        self.assert_fixture_outcome(
            self.create_import(body=foreign_body), "resource-unavailable"
        )
        local_run = self.open_run()
        foreign_run = self.create_import(body=foreign_body, client=other_client).json()[
            "target_id"
        ]
        self.assertNotEqual(local_run, foreign_run)
        for candidate in (foreign_run, "unknown-run"):
            self.assert_fixture_outcome(
                self.client.get(STATUS_PATH.format(id=candidate)),
                "resource-unavailable",
            )
            self.assert_fixture_outcome(
                self.client.post(RETRY_PATH.format(id=candidate)),
                "resource-unavailable",
            )

    def test_browser_headers_cannot_supply_fixture_identity(self):
        client = TestClient(create_app(self.store))
        response = client.post(
            CREATE_PATH,
            headers={"Authorization": "Bearer synthetic", "X-Workspace-ID": "workspace-a"},
            json={**REQUEST["example"], "workspace_id": "workspace-a"},
        )
        self.assert_fixture_outcome(response, "unauthenticated-fixture")

    def test_other_modes_and_invalid_dates_are_rejected_without_echo(self):
        for override in ({"mode": "INCREMENTAL"}, {"backfill_from": "2025-02-30"}):
            with self.subTest(override=override):
                self.assert_fixture_outcome(
                    self.create_import(body={**REQUEST["example"], **override}),
                    "invalid-request",
                )
