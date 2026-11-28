#!/usr/bin/env python3
"""Comprehensive Live Deployed Server E2E Verification Suite (Post-Deploy CD & Nightly CI).

Verifies live deployed Cloud Run instances (`cm-web`, `cm-api`, `cm-worker`), live Supabase
Auth + PostgREST isolation, and read-only OD-015 Free-Tier Cloud Pre-Flight caps:
1. Live `cm-api` Origin-HMAC Signed Contract Probes (`X-CM-Origin-Auth` unsigned -> 403,
   tampered -> 403, valid HMAC + unauthenticated Bearer -> 401 live auth verifier reached,
   `/internal/worker/wake` even with HMAC -> 403 `PUBLIC_WORKER_ROUTE_FORBIDDEN`).
2. Live `cm-worker` Private IAM + OIDC + `:6543` Supavisor Pooler Probes (unauthenticated
   -> 401/403, OIDC `POST /internal/worker/recover` & `POST /internal/worker/wake` -> 200 JSON
   with `:6543` `cm_worker_app` FORCE RLS connectivity).
3. Headless Chromium Playwright Live Hosted Browser & BFF Security E2E (`apps/web/e2e-hosted/`).
4. Live Supabase PostgREST Isolation (`OD006-5`) + Read-Only `OD-015` Cloud Pre-Flight (`GO`).

Zero-secret invariant:
- Secret/token values are held strictly in process memory and NEVER printed, echoed, or persisted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from career_memory_api.auth.redaction import SENSITIVE_KEY_NAMES, redact_text  # noqa: E402
from career_memory_api.hosted_runtime.preflight_od015 import (  # noqa: E402
    collect_gcp_preflight_observation,
    evaluate_od015_preflight,
)


def _load_t106b_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "run_t106b_release_verify",
        _REPO_ROOT / "scripts" / "run_t106b_release_verify.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load scripts/run_t106b_release_verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_t106b_release_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


def _gcloud_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "").strip()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
        f"{existing} datacloud.antigravity" if existing else "datacloud.antigravity"
    )
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    return env


def _run_gcloud_capture(args: list[str], *, timeout_seconds: float = 30.0) -> str:
    proc = subprocess.run(
        ["gcloud", *args],
        env=_gcloud_env(),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        err_msg = redact_text((proc.stderr or "").strip() or f"exit={proc.returncode}")
        raise RuntimeError(f"gcloud {args[0]} failed: {err_msg}")
    return proc.stdout.strip()


def _describe_cloud_run_service_json(
    service_name: str, *, project_id: str, region: str
) -> dict[str, Any]:
    raw = _run_gcloud_capture(
        [
            "run",
            "services",
            "describe",
            service_name,
            "--project",
            project_id,
            "--region",
            region,
            "--format=json",
        ]
    )
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected service description payload for {service_name}.")
    return parsed


def _extract_service_env_var(service_doc: dict[str, Any], env_name: str) -> str:
    containers = (
        ((service_doc.get("spec") or {}).get("template") or {}).get("spec") or {}
    ).get("containers") or []
    if not containers or not isinstance(containers[0], dict):
        return ""
    for item in containers[0].get("env") or []:
        if isinstance(item, dict) and item.get("name") == env_name:
            val = item.get("value")
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _fetch_origin_hmac_secret(project_id: str, secret_name: str = "cm-origin-hmac-secret") -> str:
    return _run_gcloud_capture(
        [
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={secret_name}",
            f"--project={project_id}",
        ]
    )


def _mint_worker_oidc_token(
    worker_url: str, *, deploy_service_account: str | None = None
) -> str:
    if deploy_service_account and deploy_service_account.strip():
        try:
            return _run_gcloud_capture(
                [
                    "auth",
                    "print-identity-token",
                    f"--impersonate-service-account={deploy_service_account.strip()}",
                    f"--audiences={worker_url}",
                    "--include-email",
                ]
            )
        except RuntimeError:
            pass
    return _run_gcloud_capture(
        [
            "auth",
            "print-identity-token",
            f"--audiences={worker_url}",
            "--include-email",
        ]
    )


def _run_playwright_hosted_e2e(web_url: str, *, timeout_seconds: float = 180.0) -> dict[str, Any]:
    web_dir = _REPO_ROOT / "apps" / "web"
    env = dict(os.environ)
    env["HOSTED_WEB_URL"] = web_url
    proc = subprocess.run(
        ["npm", "run", "test:e2e:hosted"],
        cwd=str(web_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    safe_stdout = redact_text((proc.stdout or "").strip())
    safe_stderr = redact_text((proc.stderr or "").strip())
    if safe_stdout:
        print(safe_stdout)
    if proc.returncode != 0 and safe_stderr:
        print(safe_stderr, file=sys.stderr)
    return {
        "passed": proc.returncode == 0,
        "exit_code": proc.returncode,
        "target_web_url": web_url,
    }


def _assert_report_has_no_sensitive_keys(payload: Any) -> None:
    stack: list[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).strip().lower() in SENSITIVE_KEY_NAMES:
                    raise ValueError(f"Refusing to emit sensitive key in report: {k!r}")
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


def run_live_server_e2e(
    *,
    project_id: str,
    region: str = "us-central1",
    api_url: str | None = None,
    web_url: str | None = None,
    worker_url: str | None = None,
    supabase_url: str | None = None,
    supabase_anon_key: str | None = None,
    origin_hmac_secret: str | None = None,
    worker_oidc_token: str | None = None,
    deploy_service_account: str | None = None,
    fetch_gcp_credentials: bool = False,
    require_origin_hmac: bool = True,
    require_worker_oidc: bool = True,
    skip_playwright: bool = False,
    skip_od015: bool = False,
    phase_label: str = "Live Deployed Server E2E",
) -> dict[str, Any]:
    """Execute full multi-layer live server E2E verification and return a redacted report."""
    api_doc: dict[str, Any] | None = None
    web_doc: dict[str, Any] | None = None

    resolved_api_url = (api_url or "").strip()
    resolved_web_url = (web_url or "").strip()
    resolved_worker_url = (worker_url or "").strip()
    resolved_supabase_url = (supabase_url or os.environ.get("SUPABASE_URL") or "").strip()
    resolved_anon_key = (
        supabase_anon_key
        or os.environ.get("STAGING_SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
        or ""
    ).strip()
    resolved_hmac = (origin_hmac_secret or os.environ.get("CM_ORIGIN_HMAC_SECRET") or "").strip()
    resolved_oidc = (worker_oidc_token or os.environ.get("WORKER_OIDC_TOKEN") or "").strip()

    if not resolved_api_url or (fetch_gcp_credentials and not resolved_anon_key):
        api_doc = _describe_cloud_run_service_json(
            "cm-api", project_id=project_id, region=region
        )
        if not resolved_api_url:
            resolved_api_url = str((api_doc.get("status") or {}).get("url") or "").strip()
        if not resolved_anon_key:
            resolved_anon_key = _extract_service_env_var(api_doc, "SUPABASE_ANON_KEY")

    if not resolved_web_url or not resolved_supabase_url:
        web_doc = _describe_cloud_run_service_json(
            "cm-web", project_id=project_id, region=region
        )
        if not resolved_web_url:
            resolved_web_url = str((web_doc.get("status") or {}).get("url") or "").strip()
        if not resolved_supabase_url:
            resolved_supabase_url = _extract_service_env_var(web_doc, "SUPABASE_URL")

    if not resolved_worker_url:
        worker_doc = _describe_cloud_run_service_json(
            "cm-worker", project_id=project_id, region=region
        )
        resolved_worker_url = str((worker_doc.get("status") or {}).get("url") or "").strip()

    if fetch_gcp_credentials:
        if require_origin_hmac and not resolved_hmac:
            resolved_hmac = _fetch_origin_hmac_secret(project_id)
        if require_worker_oidc and not resolved_oidc and resolved_worker_url:
            resolved_oidc = _mint_worker_oidc_token(
                resolved_worker_url,
                deploy_service_account=deploy_service_account,
            )

    if require_origin_hmac and not resolved_hmac:
        raise RuntimeError(
            "Missing live X-CM-Origin-Auth HMAC secret (set CM_ORIGIN_HMAC_SECRET or pass --fetch-gcp-credentials)."
        )
    if require_worker_oidc and not resolved_oidc:
        raise RuntimeError(
            "Missing live cm-worker OIDC token (set WORKER_OIDC_TOKEN or pass --fetch-gcp-credentials)."
        )

    t106b = _load_t106b_module()
    print(f"=== [{phase_label}] 1/3 Cross-Service Origin-HMAC / Worker-OIDC / PostgREST E2E ===")
    smoke_report: dict[str, Any] = t106b.run_smoke_checks(
        api_url=resolved_api_url,
        origin_hmac_secret=resolved_hmac or None,
        worker_url=resolved_worker_url or None,
        worker_oidc_token=resolved_oidc or None,
        web_url=resolved_web_url or None,
        supabase_url=resolved_supabase_url or None,
        supabase_anon_key=resolved_anon_key or None,
    )
    for chk in smoke_report["checks"]:
        mark = "PASS" if chk["passed"] else "FAIL"
        print(f"  [{mark}] {chk['check']} (status={chk.get('status_code')})")

    playwright_report: dict[str, Any] = {"passed": True, "skipped": True}
    if not skip_playwright:
        print(f"=== [{phase_label}] 2/3 Headless Chromium Playwright Live Hosted Browser & BFF E2E ===")
        playwright_report = _run_playwright_hosted_e2e(resolved_web_url)
        playwright_report["skipped"] = False

    od015_summary: dict[str, Any] = {"passed": True, "skipped": True}
    if not skip_od015:
        print(f"=== [{phase_label}] 3/3 Read-Only OD-015 Cloud Pre-Flight Gate ===")
        obs = collect_gcp_preflight_observation(project_id=project_id, region=region)
        preflight = evaluate_od015_preflight(obs)
        od015_summary = {
            "passed": preflight.verdict == "GO",
            "skipped": False,
            "verdict": preflight.verdict,
            "mutations_performed": preflight.mutations_performed,
            "estimated_monthly_krw_worst_case": preflight.estimated_monthly_krw_worst_case,
            "checks_passed": sum(1 for c in preflight.checks if c.passed),
            "checks_total": len(preflight.checks),
        }
        print(
            f"  [{'PASS' if od015_summary['passed'] else 'FAIL'}] OD-015 verdict={preflight.verdict} "
            f"({od015_summary['checks_passed']}/{od015_summary['checks_total']} checks, "
            f"mutations={preflight.mutations_performed})"
        )

    overall_passed = (
        bool(smoke_report["passed"])
        and bool(playwright_report["passed"])
        and bool(od015_summary["passed"])
    )
    report: dict[str, Any] = {
        "phase_label": phase_label,
        "passed": overall_passed,
        "project_id": project_id,
        "region": region,
        "targets": {
            "api_url": resolved_api_url,
            "web_url": resolved_web_url,
            "worker_url": resolved_worker_url,
            "supabase_url": resolved_supabase_url,
            "origin_hmac_verified": bool(resolved_hmac),
            "worker_oidc_verified": bool(resolved_oidc),
        },
        "http_contract_and_isolation": smoke_report,
        "playwright_hosted_browser_e2e": playwright_report,
        "od015_preflight": od015_summary,
    }
    _assert_report_has_no_sensitive_keys(report)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run comprehensive Live Deployed Server E2E verification against Cloud Run & Supabase."
    )
    parser.add_argument("--project-id", default="career-memory-516011")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--web-url", default=None)
    parser.add_argument("--worker-url", default=None)
    parser.add_argument("--supabase-url", default=None)
    parser.add_argument("--supabase-anon-key", default=None)
    parser.add_argument("--deploy-service-account", default=None)
    parser.add_argument(
        "--fetch-gcp-credentials",
        action="store_true",
        help="Fetch cm-origin-hmac-secret from Secret Manager and mint cm-worker OIDC token in-memory via gcloud.",
    )
    parser.add_argument(
        "--no-require-origin-hmac",
        dest="require_origin_hmac",
        action="store_false",
        default=True,
    )
    parser.add_argument(
        "--no-require-worker-oidc",
        dest="require_worker_oidc",
        action="store_false",
        default=True,
    )
    parser.add_argument("--skip-playwright", action="store_true")
    parser.add_argument("--skip-od015", action="store_true")
    parser.add_argument("--phase-label", default="Live Deployed Server E2E")
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = run_live_server_e2e(
            project_id=args.project_id,
            region=args.region,
            api_url=args.api_url,
            web_url=args.web_url,
            worker_url=args.worker_url,
            supabase_url=args.supabase_url,
            supabase_anon_key=args.supabase_anon_key,
            deploy_service_account=args.deploy_service_account,
            fetch_gcp_credentials=args.fetch_gcp_credentials,
            require_origin_hmac=args.require_origin_hmac,
            require_worker_oidc=args.require_worker_oidc,
            skip_playwright=args.skip_playwright,
            skip_od015=args.skip_od015,
            phase_label=args.phase_label,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Live server E2E failed: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(
        f"[Live E2E Summary] phase={report['phase_label']!r} passed={report['passed']} "
        f"http_checks={report['http_contract_and_isolation']['total_http_requests']} "
        f"playwright_passed={report['playwright_hosted_browser_e2e']['passed']} "
        f"od015_passed={report['od015_preflight']['passed']}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
