"""End-to-end PostgreSQL 18 tests for FullstackPostgresRuntime (Track 1 / Issue #35)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.authz import UnconfiguredWorkspaceAccessProvider
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.fullstack_runtime import (
    FullstackPostgresRuntime,
    FullstackRuntimeConfig,
    GitHubProviderMode,
    create_local_dev_fullstack_app,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    assert_no_forbidden_secret_keys,
)


def _audit_all_46_tables_plan_c(
    runtime: FullstackPostgresRuntime,
    *,
    forbidden_raw_substrings: tuple[str, ...],
) -> tuple[int, int, int, int]:
    """Audit all public tables in PG18 for Plan C raw text, secret keys, and raw body leaks."""
    raw_violations = 0
    secret_violations = 0
    raw_substring_hits = 0

    with runtime.db.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
            all_tables = [str(r[0]) for r in cur.fetchall()]
            assert len(all_tables) >= 46, f"Expected >= 46 tables, got {len(all_tables)}"

            cur.execute(
                """
                SELECT
                    (SELECT count(*) FROM public.content_units WHERE text_content IS NOT NULL OR content_ref IS NOT NULL)
                    + (SELECT count(*) FROM public.fact_support WHERE extracted_span IS NOT NULL)
                    + (SELECT count(*) FROM public.source_resources WHERE raw_storage_mode <> 'NONE' OR raw_retention_seconds IS NOT NULL)
                """
            )
            raw_violations += int(cur.fetchone()[0])  # type: ignore[index]

            # Inspect every JSONB column across all 46 tables for forbidden raw/secret keys
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type = 'jsonb'
                ORDER BY table_name, column_name
                """
            )
            jsonb_cols = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
            for tbl, col in jsonb_cols:
                # exports.artifact_payload is intentionally allowed to hold non-raw markdown/json artifact
                if tbl == "exports" and col == "artifact_payload":
                    continue
                cur.execute(
                    f'SELECT "{col}" FROM public."{tbl}" WHERE "{col}" IS NOT NULL'  # noqa: S608
                )
                for (val,) in cur.fetchall():
                    if isinstance(val, (dict, list)):
                        try:
                            assert_no_forbidden_raw_keys(val, field_name=f"{tbl}.{col}")
                        except ValueError:
                            raw_violations += 1
                        try:
                            assert_no_forbidden_secret_keys(val, field_name=f"{tbl}.{col}")
                        except ValueError:
                            secret_violations += 1

            # Ensure raw PR/review bodies from SamplePublicGitHubCatalog are never stored in any table
            for tbl in all_tables:
                cur.execute(f'SELECT row_to_json(t)::text FROM public."{tbl}" t')  # noqa: S608
                for (row_json,) in cur.fetchall():
                    if not row_json:
                        continue
                    for needle in forbidden_raw_substrings:
                        if needle in row_json:
                            raw_substring_hits += 1

    return len(all_tables), raw_violations, secret_violations, raw_substring_hits


def test_default_create_app_and_m1_contract_app_remain_fail_closed_and_pure() -> None:
    """Confirm create_app() remains fail-closed and M1 OpenAPI has zero /local-dev routes."""
    default_app = create_app()
    assert isinstance(
        default_app.state.workspace_access_guard.provider,
        UnconfiguredWorkspaceAccessProvider,
    )

    m1_app = create_m1_contract_app()
    paths = {getattr(route, "path", "") for route in m1_app.routes}
    assert not any("local-dev" in p for p in paths)
    assert not any("byok" in p.lower() for p in paths)
    assert not any("subscription" in p.lower() for p in paths)


@pytest.mark.postgres
def test_fullstack_runtime_pg18_full_journey_and_plan_c_audit() -> None:
    """Full end-to-end PG18 verification of FullstackPostgresRuntime in local_sample mode."""
    config = FullstackRuntimeConfig(
        github_mode=GitHubProviderMode.LOCAL_SAMPLE,
        worker_poll_interval_seconds=0.1,
        start_background_worker=True,
    )

    with FullstackPostgresRuntime(config=config) as runtime:
        app = create_local_dev_fullstack_app(runtime)

        with TestClient(app) as client:
            # 1. GET /local-dev/bootstrap (private, no-store)
            boot_resp = client.get("/local-dev/bootstrap")
            assert boot_resp.status_code == 200
            assert "no-store" in boot_resp.headers.get("cache-control", "").lower()
            boot: dict[str, Any] = boot_resp.json()
            assert boot["mode"] == "local_sample"
            assert boot["workspace_id"] == runtime.workspace_id
            assert boot["access_token"]
            assert boot["default_confirmed_event_id"]
            assert len(boot["resources"]) == 2

            auth_headers = {
                "Authorization": f"Bearer {boot['access_token']}",
                "X-Workspace-ID": boot["workspace_id"],
            }

            # Verify real ES256 JWT + PG18 FORCE RLS fail-closed boundary
            assert client.get("/v1/work-events").status_code == 401
            bad_ws_resp = client.get(
                "/v1/work-events",
                headers={
                    "Authorization": f"Bearer {boot['access_token']}",
                    "X-Workspace-ID": str(uuid.uuid4()),
                },
            )
            assert bad_ws_resp.status_code == 403

            # 2. Trigger live POST /v1/sync-runs for seoyeon-dev/search-gateway
            sg_res = next(
                r
                for r in boot["resources"]
                if r["repository_full_name"] == "seoyeon-dev/search-gateway"
            )
            sg_resource_id = sg_res["source_resource_id"]

            sync_resp = client.post(
                "/v1/sync-runs",
                headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "source_resource_id": sg_resource_id,
                    "mode": "INITIAL_BACKFILL",
                },
            )
            assert sync_resp.status_code == 202, sync_resp.text
            status_url = sync_resp.json()["status_url"]

            # Wait for background worker to drive SyncRun -> SUCCESS + COMPLETE
            deadline = time.monotonic() + 25.0
            final_status: dict[str, Any] = {}
            while time.monotonic() < deadline:
                poll_resp = client.get(status_url, headers=auth_headers)
                assert poll_resp.status_code == 200
                final_status = poll_resp.json()
                if (
                    final_status["status"] == "SUCCESS"
                    and final_status["processing_status"] == "COMPLETE"
                ):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f"SyncRun did not complete in time: {final_status}")
            assert final_status["import_ready"] is True

            # 3. Verify WorkEvents + Facts + SearchEntries populated for seoyeon-dev/search-gateway
            fact_deadline = time.monotonic() + 15.0
            sg_events: list[dict[str, Any]] = []
            sg_facts: list[dict[str, Any]] = []
            target_event: dict[str, Any] = {}
            target_event_id = ""
            while time.monotonic() < fact_deadline:
                sg_events_resp = client.get(
                    f"/v1/work-events?source_resource_id={sg_resource_id}",
                    headers=auth_headers,
                )
                assert sg_events_resp.status_code == 200
                sg_events = sg_events_resp.json()["items"]
                if len(sg_events) >= 2:
                    target_event = sg_events[0]
                    target_event_id = target_event["event_id"]
                    facts_resp = client.get(
                        f"/v1/facts?event_id={target_event_id}",
                        headers=auth_headers,
                    )
                    assert facts_resp.status_code == 200
                    sg_facts = facts_resp.json()["items"]
                    if len(sg_facts) >= 1:
                        break
                time.sleep(0.1)
            assert len(sg_events) >= 2
            assert len(sg_facts) >= 1
            fact_id = sg_facts[0]["fact_id"]

            # 4. Confirm the WorkEvent via POST /v1/work-events/{id}/actions/confirm
            confirm_resp = client.post(
                f"/v1/work-events/{target_event_id}/actions/confirm",
                headers={
                    **auth_headers,
                    "Idempotency-Key": str(uuid.uuid4()),
                    "If-Match": f'"v{target_event["version"]}"',
                },
                json={
                    "expected_version": target_event["version"],
                    "reason": "Confirmed during PG18 fullstack integration test",
                },
            )
            assert confirm_resp.status_code == 200, confirm_resp.text
            assert confirm_resp.json()["status"] == "CONFIRMED"

            # 5. Generate & Approve a Claim for the confirmed event
            gen_resp = client.post(
                "/v1/claims/generations",
                headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={"confirmed_event_id": target_event_id},
            )
            assert gen_resp.status_code in (200, 201), gen_resp.text
            gen_data = gen_resp.json()
            assert gen_data["generation_status"] == "SUCCEEDED"
            assert len(gen_data["claims"]) >= 1
            claim = gen_data["claims"][0]
            claim_id = claim["claim_id"]
            claim_version = claim["version"]
            assert claim["computed_verification"] == "SOURCE_VERIFIED"

            approve_resp = client.post(
                f"/v1/claims/{claim_id}/actions/approve",
                headers={
                    **auth_headers,
                    "Idempotency-Key": str(uuid.uuid4()),
                    "If-Match": f'"v{claim_version}"',
                },
                json={"expected_version": claim_version},
            )
            assert approve_resp.status_code == 200, approve_resp.text
            assert approve_resp.json()["status"] == "APPROVED"

            # 6. Fetch ephemeral raw content via Plan C GET /v1/content-units/{id}/content
            supports_resp = client.get(
                f"/v1/facts/{fact_id}/supports", headers=auth_headers
            )
            assert supports_resp.status_code == 200
            cu_id = supports_resp.json()["supports"][0]["content_unit_id"]

            ephemeral_resp = client.get(
                f"/v1/content-units/{cu_id}/content", headers=auth_headers
            )
            assert ephemeral_resp.status_code == 200, ephemeral_resp.text
            assert "no-store" in ephemeral_resp.headers.get("cache-control", "").lower()
            ephemeral_data = ephemeral_resp.json()
            assert ephemeral_data["source_resolution"]["status"] == "MATCHED"
            assert len(ephemeral_data["content"]) > 20

            # 7. Create a zero-quote Export via POST /v1/exports
            export_resp = client.post(
                "/v1/exports",
                headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "claim_ids": [claim_id],
                    "format": "MARKDOWN",
                    "export_policy": "SOURCE_VERIFIED_ONLY",
                },
            )
            assert export_resp.status_code == 201, export_resp.text
            export_data = export_resp.json()
            assert export_data["status"] == "READY"
            assert export_data["manifest"]["excludes_raw_source_quotes"] is True

            # 8. Search via GET /v1/search & check GET /v1/quota-status
            search_resp = client.get(
                "/v1/search?q=cursor", headers=auth_headers
            )
            assert search_resp.status_code == 200, search_resp.text
            assert len(search_resp.json()["items"]) >= 1

            quota_resp = client.get("/v1/quota-status", headers=auth_headers)
            assert quota_resp.status_code == 200
            quota_data = quota_resp.json()
            assert quota_data["paid_fallback_allowed"] is False

            # 9. Audit all 46 PG18 tables BEFORE deletion: 0 Plan C raw/secret/body leaks!
            # Use distinctive raw-body-only phrases from SamplePublicGitHubCatalog (never in titles/facts)
            raw_only_sentinels = (
                "Added fail-closed clock-skew rejection outside +/- 300s window. Refs #11.",
                "Confirmed constant-time HMAC check and benchmarked p99 drop from 180ms to 42ms.",
                "EXPLAIN ANALYZE verified p95 < 28ms on 50k search_documents rows.",
            )
            tbl_count, raw_v, sec_v, sub_hits = _audit_all_46_tables_plan_c(
                runtime, forbidden_raw_substrings=raw_only_sentinels
            )
            assert tbl_count >= 46
            assert raw_v == 0, f"Plan C raw violations detected: {raw_v}"
            assert sec_v == 0, f"Secret key violations detected: {sec_v}"
            assert sub_hits == 0, f"Raw source body strings leaked into DB: {sub_hits}"

            # 10. Execute OD-003 deletion-preview -> deletion-confirm on search-gateway
            preview_resp = client.post(
                f"/v1/source-resources/{sg_resource_id}/deletion-preview",
                headers=auth_headers,
            )
            assert preview_resp.status_code == 200, preview_resp.text
            preview = preview_resp.json()
            assert preview["impact_counts"]["work_events"] >= 2

            confirm_del_resp = client.post(
                f"/v1/source-resources/{sg_resource_id}/deletion-confirm",
                headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "preview_token": preview["preview_token"],
                    "expected_resource_version": preview["resource_version"],
                    "expected_impact_scope_version": preview["impact_scope_version"],
                    "acknowledge_irreversible_purge": True,
                },
            )
            assert confirm_del_resp.status_code == 202, confirm_del_resp.text
            del_job_id = confirm_del_resp.json()["deletion_job_id"]

            # Wait for background deletion worker to complete all 5 components -> SUCCEEDED
            del_deadline = time.monotonic() + 20.0
            del_status: dict[str, Any] = {}
            while time.monotonic() < del_deadline:
                st_resp = client.get(
                    f"/v1/deletion-jobs/{del_job_id}", headers=auth_headers
                )
                assert st_resp.status_code == 200
                del_status = st_resp.json()
                if del_status["status"] == "SUCCEEDED":
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f"Deletion job did not finish in time: {del_status}")

            assert del_status["user_visible_state"] == "DELETED"
            # Verify ledger-engine search hits remain intact while search-gateway hits are gone
            post_del_ledger = client.get("/v1/search?q=webhook", headers=auth_headers)
            assert post_del_ledger.status_code == 200
            assert len(post_del_ledger.json()["items"]) >= 1

            post_del_sg = client.get(
                f"/v1/search?q=cursor&source_resource_id={sg_resource_id}",
                headers=auth_headers,
            )
            assert post_del_sg.status_code == 200
            assert post_del_sg.json()["items"] == []
