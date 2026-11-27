from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

from fastapi import Response
from fastapi.testclient import TestClient
from httpx import ReadError
from openapi_spec_validator import OpenAPIV31SpecValidator

from contract import CONTRACT, validator_for
from stub import FixtureActor, FixtureAuthFailure, FixtureStore, create_app

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
    responses = CONTRACT["paths"][path][method]["responses"]
    definition = responses.get(str(status_code))
    if definition is None:
        definition = responses[f"{status_code // 100}XX"]
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
                ("get", CREATE_PATH),
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
        retry_body = CONTRACT["paths"][RETRY_PATH]["post"]["requestBody"]
        self.assertTrue(retry_body["required"])
        self.assertEqual(retry_body["x-contract-status"], "APPROVED")
        request_schema = retry_body["content"]["application/json"]["schema"]
        self.assertIn("expected_run_version", resolve_reference(request_schema["$ref"])["required"])
        import_status = CONTRACT["components"]["schemas"]["ImportStatus"]
        self.assertIn("run_version", import_status["required"])
        self.assertEqual(import_status["properties"]["run_version"]["x-contract-status"], "APPROVED")

    def test_stale_version_envelope_is_specific_to_retry(self):
        retry_conflict = response_definition("post", RETRY_PATH, 409)["content"]["application/json"]
        stale_version = retry_conflict["examples"]["staleVersion"]["value"]
        self.assertEqual(stale_version["error"]["code"], "RESOURCE_VERSION_CONFLICT")
        validator_for(retry_conflict["schema"]).validate(stale_version)
        create_conflict = response_definition("post", CREATE_PATH, 409)["content"]["application/json"]
        self.assertFalse(validator_for(create_conflict["schema"]).is_valid(stale_version))

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
        self.now = datetime(2026, 10, 5, 9, tzinfo=UTC)
        self.store = FixtureStore(
            {
                ("workspace-a", "synthetic-repo"),
                ("workspace-a", "second-repo"),
                ("workspace-a", "denied-repo"),
                ("workspace-b", "other-repo"),
            },
            clock=lambda: self.now,
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

    def retry_import(self, run_id, expected_version, key=RETRY_KEY, client=None, **headers):
        if key is not None:
            headers["Idempotency-Key"] = key
        return (client or self.client).post(
            RETRY_PATH.format(id=run_id),
            headers=headers,
            json={"expected_run_version": expected_version},
        )

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
        return self.read_status(STATUS_PATH.format(id=run_id))

    def read_status(self, status_url):
        response = self.client.get(status_url)
        status = self.assert_response(response, "get", STATUS_PATH, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        return status

    def store_state(self):
        return deepcopy((
            self.store.runs, self.store.run_for_resource, self.store.replays, self.store.submitted_work
        ))

    def recover(self, source_resource_id="synthetic-repo", *, client=None):
        before = self.store_state()
        response = (client or self.client).get(
            CREATE_PATH, params={"source_resource_id": source_resource_id, "limit": "1"}
        )
        self.assertEqual(self.store_state(), before)
        return response

    def recovered_items(self, **kwargs):
        response = self.recover(**kwargs)
        result = self.assert_response(response, "get", CREATE_PATH, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        for item in result["items"]:
            self.assertEqual(item["status_url"], STATUS_PATH.format(id=item["target_id"]))
        return result["items"]

    def use_observation(self, run_id, name):
        self.store.observe(self.actor.workspace_id, run_id, STATUS["examples"][name]["value"])

    def assert_error(self, response, method, path, status, code, *, details=None):
        body = self.assert_response(response, method, path, status)
        error = body["error"]
        expected_fields = {"code", "message", "retryable", "request_id"}
        if details is not None:
            expected_fields.add("details")
            self.assertEqual(error["details"], details)
        self.assertEqual(set(error), expected_fields)
        self.assertEqual(error["code"], code)
        self.assertEqual(error["retryable"], code == "AUTH_VERIFICATION_UNAVAILABLE")
        self.assertTrue(error["message"])
        self.assertTrue(error["request_id"])
        self.assertEqual(error["request_id"], response.headers["X-Request-ID"])
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        return {key: value for key, value in error.items() if key != "request_id"}

    def assert_conflict(self, response, code, existing_id=None, path=CREATE_PATH):
        details = {"existing_run_id": existing_id} if existing_id is not None else None
        self.assert_error(response, "post", path, 409, code, details=details)

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
        self.assert_error(self.create_import(client=client), "post", CREATE_PATH, 404, "RESOURCE_NOT_FOUND")
        self.assert_error(
            client.get(STATUS_PATH.format(id=run_id)), "get", STATUS_PATH, 404, "RESOURCE_NOT_FOUND"
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
        self.assertEqual(len(self.store.submitted_work), 1)
        self.assertEqual(len(self.store.replays), 1)

    def test_retry_returns_same_polling_target_and_preserves_checkpoint(self):
        first = self.assert_receipt(self.create_import())
        run_id, _ = first
        self.use_observation(run_id, "transientPartial")
        partial = self.get_status(run_id)
        self.assertEqual(partial["retry"], {"allowed": True, "from_safe_checkpoint": True})

        headers = {"X-Request-ID": "synthetic-retry"}
        version = partial["run_version"]
        response = self.retry_import(run_id, version, **headers)
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
        self.assertEqual(self.assert_receipt(self.retry_import(run_id, version), RETRY_PATH), first)
        self.assertEqual(self.get_status(run_id), resumed)
        self.assert_conflict(
            self.retry_import(run_id, resumed["run_version"], key=OTHER_KEY),
            "SYNC_ALREADY_RUNNING", run_id, RETRY_PATH,
        )
        self.assertEqual(len(self.store.submitted_work), 2)

    def test_delayed_retry_for_a_previous_failure_cannot_resume_a_new_failure(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        failure_a = self.get_status(run_id)
        version_a = failure_a["run_version"]
        accepted = self.retry_import(run_id, version_a)
        self.assert_receipt(accepted, RETRY_PATH)
        self.assertEqual(len(self.store.submitted_work), 2)

        self.use_observation(run_id, "transientPartial")
        failure_b = self.get_status(run_id)
        version_b = failure_b["run_version"]
        self.assertGreater(version_b, version_a)
        self.assertEqual(failure_a["streams"], failure_b["streams"])
        self.assertEqual(failure_a["plan_id"], failure_b["plan_id"])
        before = self.store_state()

        replay = self.retry_import(run_id, version_a)
        self.assert_receipt(replay, RETRY_PATH)
        self.assertEqual(replay.json(), accepted.json())
        self.assertEqual(self.store_state(), before)

        self.assert_conflict(
            self.retry_import(run_id, version_b), "IDEMPOTENCY_CONFLICT", path=RETRY_PATH
        )
        self.assertEqual(self.store_state(), before)

        self.assert_conflict(
            self.retry_import(run_id, version_a, key=OTHER_KEY),
            "RESOURCE_VERSION_CONFLICT", path=RETRY_PATH,
        )
        self.assertEqual(self.get_status(run_id), failure_b)
        self.assertEqual(self.store_state(), before)

        selected, = self.recovered_items()
        self.assertEqual(selected["target_id"], run_id)
        self.assertEqual(selected["import_status"]["run_version"], version_b)
        accepted_b = self.retry_import(run_id, selected["import_status"]["run_version"], key=OTHER_KEY)
        self.assert_receipt(accepted_b, RETRY_PATH)
        resumed = self.get_status(run_id)
        self.assertEqual(resumed["status"], "PENDING")
        self.assertEqual(resumed["plan_id"], failure_b["plan_id"])
        self.assertEqual(resumed["streams"], failure_b["streams"])
        self.assertEqual(len(self.store.submitted_work), 3)

        after = self.store_state()
        replay_b = self.retry_import(run_id, version_b, key=OTHER_KEY)
        self.assert_receipt(replay_b, RETRY_PATH)
        self.assertEqual(replay_b.json(), accepted_b.json())
        self.assertEqual(self.store_state(), after)

    def test_concurrent_retry_requests_accept_only_one_state_transition(self):
        run_id = self.open_run()
        for keys, expected_statuses in (((OTHER_KEY, RETRY_KEY), [202, 409]), ((KEY, KEY), [202, 202])):
            with self.subTest(keys=keys):
                self.use_observation(run_id, "transientPartial")
                version = self.get_status(run_id)["run_version"]
                work_count = len(self.store.submitted_work)
                receipt_count = len(self.store.replays)
                barrier = Barrier(2, timeout=5)

                def submit(key):
                    with TestClient(create_app(self.store, self.actor)) as client:
                        barrier.wait()
                        return self.retry_import(run_id, version, key=key, client=client)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = sorted(pool.map(submit, keys), key=lambda response: response.status_code)
                self.assertEqual([response.status_code for response in responses], expected_statuses)
                self.assert_receipt(responses[0], RETRY_PATH)
                if keys[0] == keys[1]:
                    self.assert_receipt(responses[1], RETRY_PATH)
                    self.assertEqual(responses[0].json(), responses[1].json())
                else:
                    self.assert_conflict(responses[1], "RESOURCE_VERSION_CONFLICT", path=RETRY_PATH)
                self.assertEqual(len(self.store.submitted_work), work_count + 1)
                self.assertEqual(len(self.store.replays), receipt_count + 1)
                self.assertEqual(self.get_status(run_id)["run_version"], version + 1)

    def test_same_retry_key_for_another_run_conflicts(self):
        first = self.open_run()
        second = self.open_run(
            key=OTHER_KEY, body={**REQUEST["example"], "source_resource_id": "second-repo"}
        )
        for run_id in (first, second):
            self.use_observation(run_id, "transientPartial")
        self.assert_receipt(self.retry_import(first, self.get_status(first)["run_version"]), RETRY_PATH)
        self.assert_conflict(
            self.retry_import(second, self.get_status(second)["run_version"]),
            "IDEMPOTENCY_CONFLICT", path=RETRY_PATH,
        )
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
                before = self.store_state()
                self.assert_response(
                    self.retry_import(run_id, status["run_version"]), "post", RETRY_PATH, 409, empty=True
                )
                changed_range = {**REQUEST["example"], "backfill_from": "2026-01-01"}
                for body in (REQUEST["example"], changed_range):
                    self.assert_response(
                        self.create_import(key=OTHER_KEY, body=body),
                        "post", CREATE_PATH, 409, empty=True,
                    )
                self.assertEqual(self.get_status(run_id), status)
                self.assertEqual(self.store_state(), before)

    def test_transient_failure_without_safe_checkpoint_is_not_retryable(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        status = {**self.get_status(run_id), "retry": {"allowed": False}}
        self.store.observe(self.actor.workspace_id, run_id, status)
        status = self.get_status(run_id)
        before = self.store_state()
        self.assert_response(
            self.retry_import(run_id, status["run_version"]), "post", RETRY_PATH, 409, empty=True
        )
        self.assertEqual(self.store_state(), before)

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
        self.assert_response(
            self.retry_import(run_id, status["run_version"]), "post", RETRY_PATH, 409, empty=True
        )

    def test_checks_current_permissions_before_create_and_retry_replay(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        version = self.get_status(run_id)["run_version"]
        self.assert_receipt(self.retry_import(run_id, version), RETRY_PATH)
        before = self.store_state()
        revoked = FixtureActor(self.actor.workspace_id, self.actor.allowed_resources, workspace_access_revoked=True)
        client = TestClient(create_app(self.store, authenticate=lambda: revoked))
        for response, path in (
            (self.create_import(client=client), CREATE_PATH),
            (self.retry_import(run_id, version, client=client), RETRY_PATH),
        ):
            self.assert_error(response, "post", path, 403, "WORKSPACE_SCOPE_VIOLATION")
        repo_revoked = TestClient(create_app(self.store, FixtureActor("workspace-a", frozenset())))
        for response, path in (
            (self.create_import(client=repo_revoked), CREATE_PATH),
            (self.retry_import(run_id, version, client=repo_revoked), RETRY_PATH),
        ):
            self.assert_error(response, "post", path, 404, "RESOURCE_NOT_FOUND")
        self.assertEqual(self.store_state(), before)

    def test_conceals_unknown_foreign_and_unreadable_runs_equally(self):
        other_actor = FixtureActor("workspace-b", frozenset({"other-repo"}))
        other_client = TestClient(create_app(self.store, other_actor))
        foreign_body = {**REQUEST["example"], "source_resource_id": "other-repo"}
        self.assert_error(
            self.create_import(body=foreign_body), "post", CREATE_PATH, 404, "RESOURCE_NOT_FOUND"
        )
        local_run = self.open_run()
        foreign_run = self.open_run(body=foreign_body, client=other_client)
        restricted = FixtureActor(
            self.actor.workspace_id, self.actor.allowed_resources, frozenset({local_run})
        )
        client = TestClient(create_app(self.store, restricted))
        before = self.store_state()
        errors = []
        for candidate in (local_run, foreign_run, "unknown-run"):
            for response, method, path in (
                (client.get(STATUS_PATH.format(id=candidate)), "get", STATUS_PATH),
                (self.retry_import(candidate, 1, client=client), "post", RETRY_PATH),
            ):
                errors.append(self.assert_error(response, method, path, 404, "RESOURCE_NOT_FOUND"))
        self.assertEqual(errors, [errors[0]] * len(errors))
        self.assertEqual(self.store_state(), before)

    def test_missing_or_unclassified_auth_cannot_be_overridden_by_browser_input(self):
        before = self.store_state()
        for identity in (None, "SESSION_TERMINATED"):
            with self.subTest(identity=identity):
                client = TestClient(create_app(self.store, authenticate=lambda: identity))
                response = client.post(
                    CREATE_PATH,
                    headers={
                        "Authorization": "Bearer synthetic",
                        "X-Workspace-ID": "workspace-a",
                        "X-Auth-Outcome": "SESSION_TERMINATED",
                    },
                    json={**REQUEST["example"], "workspace_id": "workspace-a", "auth_outcome": "SESSION_TERMINATED"},
                )
                self.assert_response(response, "post", CREATE_PATH, 401, empty=True)
        self.assertEqual(self.store_state(), before)

    def test_injected_auth_results_use_their_approved_envelopes_before_work(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        version = self.get_status(run_id)["run_version"]
        create_body = {**REQUEST["example"], "source_resource_id": "second-repo"}
        before = self.store_state()
        identity = self.actor
        client = TestClient(create_app(self.store, authenticate=lambda: identity))
        revoked = FixtureActor(
            self.actor.workspace_id, self.actor.allowed_resources, workspace_access_revoked=True
        )
        for identity, status, code in (
            (FixtureAuthFailure.SESSION_TERMINATED, 401, "SESSION_TERMINATED"),
            (revoked, 403, "WORKSPACE_SCOPE_VIOLATION"),
            (FixtureAuthFailure.VERIFICATION_UNAVAILABLE, 503, "AUTH_VERIFICATION_UNAVAILABLE"),
        ):
            with self.subTest(code=code):
                responses = (
                    (self.create_import(key=OTHER_KEY, body=create_body, client=client), "post", CREATE_PATH),
                    (client.get(CREATE_PATH, params={"source_resource_id": "synthetic-repo"}), "get", CREATE_PATH),
                    (client.get(STATUS_PATH.format(id=run_id)), "get", STATUS_PATH),
                    (self.retry_import(run_id, version, client=client), "post", RETRY_PATH),
                )
                for response, method, path in responses:
                    self.assert_error(response, method, path, status, code)
                    self.assertNotIn("set-cookie", response.headers)
                self.assertEqual(self.store_state(), before)

        identity = self.actor
        response = client.get(STATUS_PATH.format(id=run_id))
        self.assertEqual(self.assert_response(response, "get", STATUS_PATH, 200)["run_version"], version)

    def test_missing_and_invalid_keys_are_rejected_on_both_mutations(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        status = self.get_status(run_id)
        for key in (None, "invalid"):
            with self.subTest(key=key):
                self.assert_response(self.create_import(key=key), "post", CREATE_PATH, 422, empty=True)
                self.assert_response(
                    self.retry_import(run_id, status["run_version"], key=key),
                    "post", RETRY_PATH, 422, empty=True
                )
        self.assertEqual(self.get_status(run_id), status)

    def test_retry_requires_version_and_rejects_bodyless_requests(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        path = RETRY_PATH.format(id=run_id)
        headers = {"Idempotency-Key": RETRY_KEY}
        before = self.store_state()
        responses = [
            self.client.post(path, headers=headers),
            self.client.post(path, headers=headers, json={}),
            self.client.post(path, headers=headers, json={"expected_run_version": 0}),
        ]
        for response in responses:
            self.assert_response(response, "post", RETRY_PATH, 422, empty=True)
        self.assertEqual(self.store_state(), before)

    def test_requires_explicit_mode_and_valid_date(self):
        valid = REQUEST["example"]
        without_mode = {k: v for k, v in valid.items() if k != "mode"}
        for body in (without_mode, {**valid, "mode": "INCREMENTAL"}, {**valid, "backfill_from": "2025-02-30"}):
            with self.subTest(body=body):
                self.assert_response(self.create_import(body=body), "post", CREATE_PATH, 422, empty=True)

    def test_recovers_lost_acceptance_without_a_key_in_a_new_client(self):
        app = create_app(self.store, self.actor)

        @app.middleware("http")
        async def drop_accepted_response(request, call_next):
            response = await call_next(request)
            self.assertEqual(response.status_code, 202)
            raise ReadError("Synthetic connection dropped after acceptance")

        with TestClient(app) as disconnected:
            with self.assertRaises(ReadError):
                self.create_import(client=disconnected)

        with TestClient(create_app(self.store, self.actor)) as reconnected:
            item, = self.recovered_items(client=reconnected)
        self.assertEqual(len(self.store.runs), 1)
        self.assertEqual(len(self.store.submitted_work), 1)
        self.assertEqual(item["import_status"]["status"], "PENDING")
        self.assertEqual(self.read_status(item["status_url"]), item["import_status"])
        before = self.store_state()
        # An explicit resend with the original key is still a replay.
        self.assertEqual(
            self.assert_receipt(self.create_import()), (item["target_id"], item["status_url"])
        )
        self.assertEqual(self.store_state(), before)

    def test_disconnect_before_acceptance_then_explicit_resend_can_submit_work(self):
        app = create_app(self.store, self.actor)

        @app.middleware("http")
        async def drop_before_acceptance(_request, _call_next):
            raise ReadError("Synthetic connection dropped before acceptance")

        with TestClient(app) as disconnected:
            before = self.store_state()
            with self.assertRaises(ReadError):
                self.create_import(client=disconnected)
            self.assertEqual(self.recovered_items(), [])
            self.assertEqual(self.store_state(), before)

            # Read-only recovery observed an empty list. The explicit POST is a new acceptance.
            run_id = self.open_run()
            self.assertEqual(len(self.store.submitted_work), 1)
            self.use_observation(run_id, "transientPartial")
            version = self.get_status(run_id)["run_version"]
            before = self.store_state()
            with self.assertRaises(ReadError):
                self.retry_import(run_id, version, client=disconnected)
            status = self.get_status(run_id)
            self.assertEqual(status["status"], "PARTIAL")
            self.assertEqual(status["run_version"], version)
            self.assertEqual(self.store_state(), before)

        self.assert_receipt(self.retry_import(run_id, version), RETRY_PATH)
        self.assertEqual(len(self.store.submitted_work), 2)

    def test_retry_502_leaves_acceptance_unconfirmed_until_read_or_explicit_replay(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        version = self.get_status(run_id)["run_version"]
        app = create_app(self.store, self.actor)

        @app.middleware("http")
        async def replace_accepted_response(request, call_next):
            response = await call_next(request)
            self.assertEqual(response.status_code, 202)
            return Response(status_code=502)

        with TestClient(app) as gateway:
            response = self.retry_import(run_id, version, client=gateway)
            self.assert_response(response, "post", RETRY_PATH, 502, empty=True)

        item, = self.recovered_items()
        self.assertEqual(item["target_id"], run_id)
        self.assertEqual(item["import_status"]["status"], "PENDING")
        self.assertEqual(item["import_status"]["streams"][0]["last_committed_page"], 7)
        self.assertEqual(len(self.store.submitted_work), 2)
        before = self.store_state()
        self.assertEqual(
            self.assert_receipt(self.retry_import(run_id, version), RETRY_PATH),
            (run_id, item["status_url"]),
        )
        self.assertEqual(self.store_state(), before)

    def test_recovers_a_terminal_run_and_reads_policy_changes_on_reentry(self):
        run_id = self.open_run()
        success = {
            **STATUS["examples"]["policyChangedAfterSuccess"]["value"],
            "processing_status": "COMPLETE",
        }
        self.store.observe(self.actor.workspace_id, run_id, success)
        first, = self.recovered_items()
        self.assertEqual(first["target_id"], run_id)
        self.assertEqual(first["import_status"]["processing_status"], "COMPLETE")
        finished_at = self.store.runs[self.actor.workspace_id, run_id].finished_at

        self.now += timedelta(hours=1)
        self.use_observation(run_id, "policyChangedAfterSuccess")
        refreshed = self.read_status(first["status_url"])
        self.assertEqual(refreshed["status"], "SUCCESS")
        self.assertTrue(refreshed["import_ready"])
        self.assertEqual(refreshed["processing_status"], "BLOCKED_POLICY")
        self.assertFalse(refreshed["retry"]["allowed"])
        latest, = self.recovered_items()
        self.assertEqual(latest["import_status"], refreshed)
        self.assertEqual(self.store.runs[self.actor.workspace_id, run_id].finished_at, finished_at)

    def test_recovery_prioritizes_a_readable_active_run_then_latest_terminal(self):
        first = self.open_run()
        self.use_observation(first, "transientPartial")
        self.store.simulate_future_abandonment(self.actor, first)
        second = self.open_run(key=OTHER_KEY)

        active, = self.recovered_items()
        self.assertEqual(active["target_id"], second)
        self.assertEqual(active["import_status"]["status"], "PENDING")
        restricted = FixtureActor(
            self.actor.workspace_id, self.actor.allowed_resources, frozenset({second})
        )
        restricted_client = TestClient(create_app(self.store, restricted))
        visible, = self.recovered_items(client=restricted_client)
        self.assertEqual(visible["target_id"], first)
        self.assertEqual(visible["import_status"]["retry"], {"allowed": False})

        # A same-time terminal transition is broken by newest creation.
        self.use_observation(second, "itemLimit")
        latest, = self.recovered_items()
        self.assertEqual(latest["target_id"], second)
        self.assertEqual(latest["import_status"]["status"], "PARTIAL_WITH_LIMIT")
        visible_terminal, = self.recovered_items(client=restricted_client)
        self.assertEqual(visible_terminal["target_id"], first)

    def test_recovery_empty_response_hides_unreadable_run_existence(self):
        run_id = self.open_run()
        restricted = FixtureActor(
            self.actor.workspace_id, self.actor.allowed_resources, frozenset({run_id})
        )
        client = TestClient(create_app(self.store, restricted))
        self.assertEqual(self.recovered_items(client=client), [])
        self.assertEqual(self.recovered_items(source_resource_id="second-repo"), [])
        default_limit = self.client.get(CREATE_PATH, params={"source_resource_id": "second-repo"})
        self.assertEqual(self.assert_response(default_limit, "get", CREATE_PATH, 200), {"items": []})

    def test_recovery_conceals_revoked_unknown_and_foreign_repositories_equally(self):
        self.open_run()
        other_actor = FixtureActor("workspace-b", frozenset({"other-repo"}))
        other_client = TestClient(create_app(self.store, other_actor))
        foreign_request = {**REQUEST["example"], "source_resource_id": "other-repo"}
        self.open_run(client=other_client, body=foreign_request)
        revoked = TestClient(create_app(self.store, FixtureActor("workspace-a", frozenset())))
        responses = (
            self.recover(client=revoked),
            self.recover(source_resource_id="unknown-repo"),
            self.recover(source_resource_id="other-repo"),
            self.recover(source_resource_id="denied-repo"),
        )
        errors = [
            self.assert_error(response, "get", CREATE_PATH, 404, "RESOURCE_NOT_FOUND")
            for response in responses
        ]
        self.assertEqual(errors, [errors[0]] * len(errors))
        anonymous = TestClient(create_app(self.store))
        self.assert_response(self.recover(client=anonymous), "get", CREATE_PATH, 401, empty=True)

    def test_read_paths_leave_state_unchanged_and_never_submit_work(self):
        run_id = self.open_run()
        self.use_observation(run_id, "transientPartial")
        before = self.store_state()
        with patch.object(self.store, "create", wraps=self.store.create) as create:
            with patch.object(self.store, "retry", wraps=self.store.retry) as retry:
                recovered, = self.recovered_items()
                self.assertEqual(recovered["import_status"], self.read_status(recovered["status_url"]))
        create.assert_not_called()
        retry.assert_not_called()
        self.assertEqual(self.store_state(), before)

    def test_recovery_rejects_missing_repository_or_unsupported_limit(self):
        for params in ({}, {"source_resource_id": ""}, {"source_resource_id": "synthetic-repo", "limit": "2"}):
            with self.subTest(params=params):
                response = self.client.get(CREATE_PATH, params=params)
                self.assert_response(response, "get", CREATE_PATH, 422, empty=True)

    def test_future_abandonment_fixture_seeds_replacement_history(self):
        first = self.open_run()
        original_plan = self.get_status(first)["plan_id"]
        self.use_observation(first, "transientPartial")
        self.assertTrue(self.get_status(first)["retry"]["allowed"])
        replacement = {**REQUEST["example"], "backfill_from": "2026-01-01"}
        self.assert_response(
            self.create_import(key=OTHER_KEY, body=replacement), "post", CREATE_PATH, 409, empty=True
        )

        self.store.simulate_future_abandonment(self.actor, first)
        self.assertEqual(self.get_status(first)["retry"], {"allowed": False})
        self.assert_response(
            self.retry_import(first, self.get_status(first)["run_version"]),
            "post", RETRY_PATH, 409, empty=True,
        )
        self.assert_conflict(
            self.create_import(key=KEY, body=replacement), "IDEMPOTENCY_CONFLICT"
        )
        self.assertEqual(self.open_run(), first)
        second = self.open_run(key=OTHER_KEY, body=replacement)
        self.assertNotEqual(second, first)
        self.assertNotEqual(self.get_status(second)["plan_id"], original_plan)

    def test_receipt_expires_at_24_hours_without_extending_on_replay(self):
        accepted = self.assert_receipt(self.create_import())
        run_id, _ = accepted
        self.use_observation(run_id, "transientPartial")
        version_a = self.get_status(run_id)["run_version"]
        self.assertEqual(
            self.assert_receipt(self.retry_import(run_id, version_a, key=KEY), RETRY_PATH), accepted
        )
        self.use_observation(run_id, "transientPartial")
        self.assertTrue(self.get_status(run_id)["retry"]["allowed"])

        self.now += timedelta(hours=24) - timedelta(seconds=1)
        before = self.store_state()
        self.assertEqual(self.assert_receipt(self.create_import()), accepted)
        self.assertEqual(
            self.assert_receipt(self.retry_import(run_id, version_a, key=KEY), RETRY_PATH), accepted
        )

        self.now += timedelta(seconds=1)
        self.assert_conflict(self.create_import(), "IDEMPOTENCY_KEY_EXPIRED")
        self.assert_conflict(
            self.retry_import(run_id, version_a, key=KEY),
            "IDEMPOTENCY_KEY_EXPIRED", path=RETRY_PATH,
        )
        revoked = TestClient(create_app(self.store, FixtureActor("workspace-a", frozenset())))
        self.assert_error(
            self.create_import(client=revoked), "post", CREATE_PATH, 404, "RESOURCE_NOT_FOUND"
        )
        self.assertEqual(self.store_state(), before)
