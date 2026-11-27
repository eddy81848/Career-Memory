"""Unit tests for M2-T206 FrozenImportPlan roundtrip, BackfillLifecycleHookPort, and app mounting."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.backfill_engine.models import BackfillStreamName
from career_memory_api.github_provider import (
    GitHubRepositorySnapshot,
    ImportPlanLimits,
    build_frozen_import_plan,
)
from career_memory_api.initial_import import (
    FinalizeHookContext,
    NoopBackfillLifecycleHook,
    PageHookContext,
    frozen_import_plan_from_plan_spec,
)
from career_memory_api.schema_contracts.enums import ResourceVisibility


def test_frozen_import_plan_roundtrip_preserves_exact_fingerprint() -> None:
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    snap = GitHubRepositorySnapshot(
        provider_resource_id="123456",
        owner="acme",
        name="core",
        full_name="acme/core",
        canonical_url="https://github.com/acme/core",
        visibility=ResourceVisibility.PUBLIC,
        default_branch="trunk",
        default_branch_head_sha="a" * 40,
        observed_at=now,
    )
    plan = build_frozen_import_plan(
        source_resource_id="res-001",
        repo_snapshot=snap,
        cutoff_at=now,
        backfill_from="2026-01-01",
        limits=ImportPlanLimits(max_objects=250, max_requests=100, per_page=50),
        plan_id="plan-001",
    )
    reconstructed = frozen_import_plan_from_plan_spec(plan.to_plan_spec_dict())
    assert reconstructed == plan
    assert reconstructed.plan_fingerprint() == plan.plan_fingerprint()
    assert reconstructed.default_branch == "trunk"
    assert reconstructed.start_head_sha == "a" * 40


def test_noop_lifecycle_hook_accepts_all_boundary_contexts() -> None:
    hook = NoopBackfillLifecycleHook()
    page_ctx = PageHookContext(
        workspace_id="ws-1",
        sync_run_id="run-1",
        source_resource_id="res-1",
        run_version=1,
        stream_name=BackfillStreamName.PRS,
        page_number=1,
        request_cursor=None,
        next_cursor="page:2",
        checkpoint_version=0,
    )
    fin_ctx = FinalizeHookContext(
        workspace_id="ws-1",
        sync_run_id="run-1",
        source_resource_id="res-1",
        run_version=1,
        plan_id="plan-1",
    )
    assert hook.before_page_commit(page_ctx) is None
    assert hook.after_page_commit(page_ctx) is None
    assert hook.before_finalize(fin_ctx) is None
    assert hook.after_finalize(fin_ctx) is None


def test_create_app_mounts_optional_github_connect_router_preserving_defaults() -> None:
    # Default call without new args remains 100% backward compatible
    default_app = create_app()
    with TestClient(default_app) as client:
        assert client.get("/healthz").status_code == 200

    # Optional github_connect_router mounts cleanly
    router = APIRouter()

    @router.get("/v1/source-accounts")
    def _dummy_accounts() -> dict[str, list[object]]:
        return {"items": []}

    mounted_app = create_app(
        github_connect_router=router,
        install_github_connect_handlers=True,
    )
    with TestClient(mounted_app) as client:
        resp = client.get("/v1/source-accounts")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "private, no-store"
        assert resp.json() == {"items": []}
