#!/usr/bin/env python3
"""Read-only OD-015 Cloud Pre-Flight Gate CLI (0 mutations, ADR-020 / T-007F §3).

Checks:
1. Tier 1 Free-Tier region eligibility (`us-central1`, `us-east1`, `us-west1`).
2. Paid Container Scanning (`containerscanning.googleapis.com`) disabled (read-only; NEVER mutates).
3. Artifact Registry storage <= 250 MB (>= 250 MB teardown headroom under 500 MB Free Tier).
4. Secret Manager enabled versions <= 4 (under 6 Free Tier versions).
5. Serverless VPC Connector count == 0.
6. Cloud Run caps (`min-instances=0`, `max-instances<=2`, `cpu<=1`, `memory<=512Mi`) and
   conservative monthly KRW worst-case estimate < ₩20,000.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from career_memory_api.hosted_runtime.preflight_od015 import (  # noqa: E402
    CloudPreflightObservation,
    CloudRunServiceCapSpec,
    collect_gcp_preflight_observation,
    evaluate_od015_preflight,
)


def _load_offline_observation(path: Path) -> CloudPreflightObservation:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    services_raw = raw.get("cloud_run_services")
    services: tuple[CloudRunServiceCapSpec, ...]
    if isinstance(services_raw, list) and services_raw:
        services = tuple(
            CloudRunServiceCapSpec(
                service_name=str(item["service_name"]),
                min_instances=int(item.get("min_instances", 0)),
                max_instances=int(item.get("max_instances", 2)),
                cpu=float(item.get("cpu", 1.0)),
                memory_mib=int(item.get("memory_mib", 512)),
                estimated_monthly_requests=int(item.get("estimated_monthly_requests", 20_000)),
                estimated_avg_request_seconds=float(
                    item.get("estimated_avg_request_seconds", 0.25)
                ),
            )
            for item in services_raw
        )
    else:
        services = CloudPreflightObservation(project_id="tmp", region="us-central1").cloud_run_services

    return CloudPreflightObservation(
        project_id=str(raw["project_id"]),
        region=str(raw["region"]),
        enabled_services=frozenset(str(s) for s in raw.get("enabled_services", [])),
        artifact_registry_total_mb=float(raw.get("artifact_registry_total_mb", 0.0)),
        secret_manager_enabled_versions=int(raw.get("secret_manager_enabled_versions", 0)),
        vpc_connector_count=int(raw.get("vpc_connector_count", 0)),
        cloud_run_services=services,
        fx_krw_per_usd=float(raw.get("fx_krw_per_usd", 1500.0)),
        tax_and_buffer_multiplier=float(raw.get("tax_and_buffer_multiplier", 1.25)),
        extra_monthly_usd_estimate=float(raw.get("extra_monthly_usd_estimate", 0.0)),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only OD-015 Cloud Pre-Flight Cost & Security Gate CLI"
    )
    parser.add_argument("--project-id", help="Target GCP project ID.")
    parser.add_argument(
        "--region",
        default="us-central1",
        help="Target GCP region (default: us-central1 Tier 1 Free Tier).",
    )
    parser.add_argument(
        "--allow-tier2-override",
        action="store_true",
        help="Explicit owner override permitting a Tier 2 region.",
    )
    parser.add_argument(
        "--offline-observation-json",
        type=Path,
        help="Evaluate a local JSON observation file instead of invoking gcloud.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Print deterministic redacted JSON verdict only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.offline_observation_json is not None:
        obs = _load_offline_observation(args.offline_observation_json)
    else:
        if not args.project_id:
            print(
                "ERROR: --project-id is required unless --offline-observation-json is provided.",
                file=sys.stderr,
            )
            return 2
        obs = collect_gcp_preflight_observation(
            project_id=args.project_id.strip(),
            region=args.region.strip(),
        )

    report = evaluate_od015_preflight(
        obs,
        allow_tier2_region_override=bool(args.allow_tier2_override),
    )

    if args.emit_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"[OD-015 Pre-Flight] project={report.project_id} region={report.region} "
            f"verdict={report.verdict} worst_case_krw=₩{report.estimated_monthly_krw_worst_case:,} "
            f"(ceiling=₩{report.hard_krw_monthly_ceiling:,}, mutations={report.mutations_performed})"
        )
        for check in report.checks:
            mark = "PASS" if check.passed else "FAIL"
            print(f"  [{mark}] {check.check_id}: {check.message}")

    return 0 if report.verdict == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
