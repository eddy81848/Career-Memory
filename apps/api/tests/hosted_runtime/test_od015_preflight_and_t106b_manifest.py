"""Unit tests for OD-015 pre-flight gate, T-106B release manifest/rollback CLI, and URL SSRF guards."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from career_memory_api.conformance.checks import default_repo_root
from career_memory_api.hosted_runtime.jwks_client import validate_pinned_auth_url
from career_memory_api.hosted_runtime.models import (
    HostedConfigurationError,
    HostedServiceRole,
)
from career_memory_api.hosted_runtime.preflight_od015 import (
    PAID_CONTAINER_SCANNING_API,
    CloudPreflightObservation,
    CloudRunServiceCapSpec,
    evaluate_od015_preflight,
)

REPO_ROOT = default_repo_root()


def _load_script_module(script_name: str) -> Any:
    import sys

    path = REPO_ROOT / "scripts" / script_name
    mod_name = script_name.replace(".py", "")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_od015_preflight_go_on_clean_us_central1_free_tier() -> None:
    obs = CloudPreflightObservation(
        project_id="cm-sandbox-001",
        region="us-central1",
        enabled_services=frozenset({"run.googleapis.com", "artifactregistry.googleapis.com"}),
        artifact_registry_total_mb=140.0,
        secret_manager_enabled_versions=3,
        vpc_connector_count=0,
    )
    report = evaluate_od015_preflight(obs)
    assert report.verdict == "GO"
    assert report.mutations_performed == 0
    assert report.estimated_monthly_krw_worst_case < 20_000
    assert all(c.passed for c in report.checks)


@pytest.mark.parametrize(
    ("override_kwargs", "expected_failed_check_id"),
    [
        ({"region": "asia-northeast3"}, "OD015_REGION_TIER1_FREE_TIER"),
        (
            {"enabled_services": frozenset({PAID_CONTAINER_SCANNING_API})},
            "OD015_PAID_CONTAINER_SCANNING_DISABLED",
        ),
        ({"artifact_registry_total_mb": 251.0}, "OD015_ARTIFACT_REGISTRY_HEADROOM"),
        ({"secret_manager_enabled_versions": 5}, "OD015_SECRET_MANAGER_VERSION_HEADROOM"),
        ({"vpc_connector_count": 1}, "OD015_ZERO_SERVERLESS_VPC_CONNECTORS"),
        (
            {
                "cloud_run_services": (
                    CloudRunServiceCapSpec(
                        service_name="cm-api", min_instances=1, max_instances=2, cpu=1.0, memory_mib=512
                    ),
                )
            },
            "OD015_CLOUD_RUN_CAPS_AND_KRW_BUDGET",
        ),
        (
            {"extra_monthly_usd_estimate": 15.0},
            "OD015_CLOUD_RUN_CAPS_AND_KRW_BUDGET",
        ),
    ],
)
def test_od015_preflight_no_go_cases_with_zero_mutations(
    override_kwargs: dict[str, Any],
    expected_failed_check_id: str,
) -> None:
    base = dict(
        project_id="cm-sandbox-001",
        region="us-central1",
        enabled_services=frozenset({"run.googleapis.com"}),
        artifact_registry_total_mb=100.0,
        secret_manager_enabled_versions=2,
        vpc_connector_count=0,
    )
    base.update(override_kwargs)
    report = evaluate_od015_preflight(CloudPreflightObservation(**base))
    assert report.verdict == "NO_GO"
    assert report.mutations_performed == 0
    failed_ids = {c.check_id for c in report.checks if not c.passed}
    assert expected_failed_check_id in failed_ids


def test_od015_cli_offline_observation_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = _load_script_module("check_od015_cloud_preflight.py")
    obs_file = tmp_path / "obs.json"
    obs_file.write_text(
        json.dumps(
            {
                "project_id": "cm-test-proj",
                "region": "us-central1",
                "enabled_services": ["run.googleapis.com"],
                "artifact_registry_total_mb": 120.0,
                "secret_manager_enabled_versions": 2,
                "vpc_connector_count": 0,
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(["--offline-observation-json", str(obs_file), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "GO"
    assert out["mutations_performed"] == 0


def test_t106b_release_manifest_enforces_sha256_digests_and_emits_rollback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_script_module("run_t106b_release_verify.py")
    commit_sha = "1" * 40
    api_digest = f"us-central1-docker.pkg.dev/cm-proj/cm/cm-api@sha256:{'a' * 64}"
    web_digest = f"us-central1-docker.pkg.dev/cm-proj/cm/cm-web@sha256:{'b' * 64}"
    prev_worker_digest = f"us-central1-docker.pkg.dev/cm-proj/cm/cm-api@sha256:{'c' * 64}"

    manifest_path = tmp_path / "manifest.json"
    rc = mod.main(
        [
            "--git-commit-sha",
            commit_sha,
            "--api-image-digest",
            api_digest,
            "--web-image-digest",
            web_digest,
            "--region",
            "us-central1",
            "--previous-api-revision",
            "cm-api-00010-abc",
            "--previous-web-revision",
            "cm-web-00010-def",
            "--previous-worker-digest",
            prev_worker_digest,
            "--output",
            str(manifest_path),
        ]
    )
    assert rc == 0
    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert parsed["api_image_digest"] == api_digest
    assert parsed["openapi_import_v1_blob_sha"] == "07594d75425744d6149250a9f7ff95ac24659f80"
    capsys.readouterr()

    # --print-rollback emits exact gcloud traffic shift & worker @sha256 digest restore commands
    rc_rb = mod.main(["--manifest", str(manifest_path), "--project-id", "cm-proj", "--print-rollback"])
    assert rc_rb == 0
    rb_out = capsys.readouterr().out
    assert "gcloud run services update-traffic cm-api" in rb_out
    assert "--to-revisions=cm-api-00010-abc=100" in rb_out
    assert "gcloud run services update-traffic cm-web" in rb_out
    assert "--to-revisions=cm-web-00010-def=100" in rb_out
    assert f"--image={prev_worker_digest}" in rb_out
    assert "CLOUDSDK_METRICS_ENVIRONMENT=" in rb_out

    # Mutable tags MUST be rejected!
    bad_manifest = mod.ReleaseManifestV1(
        git_commit_sha=commit_sha,
        api_image_digest="us-central1-docker.pkg.dev/cm-proj/cm/cm-api:latest",
        web_image_digest=web_digest,
    )
    with pytest.raises(mod.ReleaseManifestValidationError, match="@sha256:"):
        mod.validate_release_manifest(bad_manifest)


def test_pinned_auth_url_ssrf_and_role_parsing() -> None:
    assert validate_pinned_auth_url("https://proj.supabase.co/auth/v1/.well-known/jwks.json")
    for bad_url in (
        "http://proj.supabase.co/auth/v1/.well-known/jwks.json",
        "https://169.254.169.254/computeMetadata/v1/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://10.0.0.5/jwks.json",
        "https://192.168.1.10/jwks.json",
        "https://user:pass@proj.supabase.co/jwks.json",
    ):
        with pytest.raises(HostedConfigurationError):
            validate_pinned_auth_url(bad_url)

    assert HostedServiceRole.from_env("api") is HostedServiceRole.API
    assert HostedServiceRole.from_env("worker") is HostedServiceRole.WORKER
    with pytest.raises(HostedConfigurationError):
        HostedServiceRole.from_env("admin")


def test_t106b_smoke_checks_verify_origin_hmac_worker_oidc_and_postgrest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_script_module("run_t106b_release_verify.py")

    def _fake_probe(
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body_bytes: bytes | None = None,
        timeout_seconds: float = 20.0,
        follow_redirects: bool = True,
    ) -> tuple[int, dict[str, str], str, float]:
        _ = (body_bytes, timeout_seconds, follow_redirects)
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        cc = {"cache-control": "private, no-store"}
        if url.endswith("/api/health"):
            return 200, {"cache-control": "no-store"}, '{"status":"ok"}', 5.0
        if "cm-api.example" in url:
            if url.endswith("/openapi.json"):
                return 404, cc, '{"error":{"code":"ROUTE_NOT_DECLARED"}}', 2.0
            if url.endswith("/internal/worker/wake"):
                return 403, cc, '{"error":{"code":"PUBLIC_WORKER_ROUTE_FORBIDDEN"}}', 2.0
            sig = hdrs.get("x-cm-origin-auth", "")
            if not sig or sig.endswith("0" * 64):
                return 403, cc, '{"error":{"code":"ORIGIN_AUTH_REQUIRED"}}', 2.0
            if hdrs.get("origin") == "https://evil.example":
                return 403, cc, '{"error":{"code":"CSRF_ORIGIN_DENIED"}}', 2.0
            return 401, cc, '{"error":{"code":"UNAUTHENTICATED"}}', 3.0
        if "cm-worker.example" in url:
            if "authorization" not in hdrs:
                return 403, cc, "", 2.0
            if url.endswith("/v1/session"):
                return 404, cc, '{"error":{"code":"ROUTE_NOT_DECLARED"}}', 2.0
            if url.endswith("/internal/worker/recover"):
                return 200, cc, '{"status":"ok","workspaces_scanned":1}', 4.0
            return (
                200,
                cc,
                '{"status":"ok","workspace_id":"00000000-0000-4000-8000-000000000000"}',
                4.0,
            )
        if "cm-web.example" in url:
            if url.endswith("/auth/session"):
                return 200, cc, '{"authenticated":false}', 2.0
            if "/auth/login?next=//evil.example" in url:
                return 400, cc, '{"error":{"code":"INVALID_RETURN_PATH"}}', 2.0
            if "/auth/login" in url:
                return (
                    302,
                    {
                        "location": "https://supa.example/auth/v1/authorize?code_challenge=abc&code_challenge_method=s256",
                        "set-cookie": "__Host-cm_pkce=v1.a.b.c; Path=/; HttpOnly; Secure",
                    },
                    "",
                    2.0,
                )
            if url.endswith("/bff/v1/sync-runs"):
                return 403, cc, '{"error":{"code":"CSRF_VALIDATION_FAILED"}}', 2.0
        if "supa.example" in url:
            if url.endswith("/jwks.json"):
                return 200, {}, '{"keys":[]}', 2.0
            return 401, {}, '{"message":"No API key"}', 2.0
        raise AssertionError(f"Unexpected probe URL: {method} {url}")

    monkeypatch.setattr(mod, "_http_probe", _fake_probe)
    smoke = mod.run_smoke_checks(
        api_url="https://cm-api.example",
        origin_hmac_secret="test-live-hmac-secret-not-real-0123456789",
        worker_url="https://cm-worker.example",
        worker_oidc_token="fake-oidc-token",
        web_url="https://cm-web.example",
        supabase_url="https://supa.example",
        supabase_anon_key="fake-anon-key",
    )
    assert smoke["passed"] is True, f"Failed smoke checks: {[c for c in smoke['checks'] if not c['passed']]}"
    check_names = {c["check"] for c in smoke["checks"]}
    assert "api_signed_session_unauthenticated_401" in check_names
    assert "api_signed_work_events_unauthenticated_401" in check_names
    assert "api_signed_sync_runs_unauthenticated_401" in check_names
    assert "api_internal_worker_route_blocked_even_with_hmac_403" in check_names
    assert "worker_oidc_authorized_recover_200" in check_names
    assert "worker_oidc_authorized_wake_200" in check_names
    assert "supabase_postgrest_domain_table_bypass_blocked" in check_names
