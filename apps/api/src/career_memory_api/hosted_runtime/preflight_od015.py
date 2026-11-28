"""Pure read-only OD-015 Cloud Pre-Flight Gate Evaluator (OD-015 / ADR-020 / T-007F §3).

Security & Zero-Mutation Invariants:
- NEVER mutates org/project security settings (especially ``containerscanning.googleapis.com``).
- Emits deterministic redacted ``GO`` / ``NO_GO`` JSON verdicts.
- Prepends ``CLOUDSDK_METRICS_ENVIRONMENT`` attribution when invoking read-only ``gcloud`` CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

TIER1_FREE_REGIONS: frozenset[str] = frozenset({"us-central1", "us-east1", "us-west1"})
PAID_CONTAINER_SCANNING_API = "containerscanning.googleapis.com"

MAX_ARTIFACT_REGISTRY_MB = 250.0
MAX_SECRET_MANAGER_ENABLED_VERSIONS = 4
MAX_VPC_CONNECTOR_COUNT = 0
MAX_CLOUD_RUN_MIN_INSTANCES = 0
MAX_CLOUD_RUN_MAX_INSTANCES = 2
MAX_CLOUD_RUN_CPU = 1.0
MAX_CLOUD_RUN_MEMORY_MIB = 512
HARD_KRW_MONTHLY_CEILING = 20_000


@dataclass(frozen=True, slots=True)
class CloudRunServiceCapSpec:
    """Bounded Cloud Run service resource specification for OD-015 audit."""

    service_name: str
    min_instances: int = 0
    max_instances: int = 2
    cpu: float = 1.0
    memory_mib: int = 512
    estimated_monthly_requests: int = 20_000
    estimated_avg_request_seconds: float = 0.25


@dataclass(frozen=True, slots=True)
class CloudPreflightObservation:
    """Read-only observation snapshot of GCP project/region headroom for OD-015."""

    project_id: str
    region: str
    enabled_services: frozenset[str] = field(default_factory=frozenset)
    artifact_registry_total_mb: float = 0.0
    secret_manager_enabled_versions: int = 0
    vpc_connector_count: int = 0
    cloud_run_services: tuple[CloudRunServiceCapSpec, ...] = (
        CloudRunServiceCapSpec(service_name="cm-api", min_instances=0, max_instances=2, cpu=1.0, memory_mib=512),
        CloudRunServiceCapSpec(service_name="cm-worker", min_instances=0, max_instances=1, cpu=1.0, memory_mib=512),
        CloudRunServiceCapSpec(service_name="cm-web", min_instances=0, max_instances=2, cpu=1.0, memory_mib=512),
    )
    fx_krw_per_usd: float = 1500.0
    tax_and_buffer_multiplier: float = 1.25
    extra_monthly_usd_estimate: float = 0.0


@dataclass(frozen=True, slots=True)
class PreflightCheckResult:
    """Single deterministic check result in the OD-015 pre-flight verdict."""

    check_id: str
    passed: bool
    severity: str
    message: str
    observed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "observed": dict(self.observed),
        }


@dataclass(frozen=True, slots=True)
class Od015PreflightReport:
    """Redacted OD-015 Cloud Pre-Flight Gate Report (always 0 mutations)."""

    project_id: str
    region: str
    verdict: str  # "GO" | "NO_GO"
    mutations_performed: int
    estimated_monthly_krw_worst_case: int
    hard_krw_monthly_ceiling: int
    checks: tuple[PreflightCheckResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": "OD-015-CLOUD-PREFLIGHT",
            "project_id": self.project_id,
            "region": self.region,
            "verdict": self.verdict,
            "mutations_performed": self.mutations_performed,
            "estimated_monthly_krw_worst_case": self.estimated_monthly_krw_worst_case,
            "hard_krw_monthly_ceiling": self.hard_krw_monthly_ceiling,
            "checks": [c.to_dict() for c in self.checks],
        }


def estimate_monthly_krw_worst_case(obs: CloudPreflightObservation) -> int:
    """Conservative monthly KRW worst-case estimate with Tier-1 Free Tier & tax/FX buffer."""
    # Tier 1 Free Tier monthly allowances: 180,000 vCPU-sec, 360,000 GiB-sec, 2,000,000 requests
    is_tier1 = obs.region.strip().lower() in TIER1_FREE_REGIONS
    total_vcpu_sec = 0.0
    total_gib_sec = 0.0
    total_requests = 0

    for svc in obs.cloud_run_services:
        # Always-on min_instances burns 2,592,000 sec/month (30 days)
        idle_sec = float(max(0, svc.min_instances)) * 30.0 * 86_400.0
        req_sec = float(max(0, svc.estimated_monthly_requests)) * max(
            0.01, float(svc.estimated_avg_request_seconds)
        )
        active_sec = idle_sec + req_sec
        total_vcpu_sec += active_sec * max(0.25, float(svc.cpu))
        total_gib_sec += active_sec * (max(128, int(svc.memory_mib)) / 1024.0)
        total_requests += max(0, int(svc.estimated_monthly_requests))

    free_vcpu_sec = 180_000.0 if is_tier1 else 0.0
    free_gib_sec = 360_000.0 if is_tier1 else 0.0
    free_requests = 2_000_000 if is_tier1 else 0

    billable_vcpu_sec = max(0.0, total_vcpu_sec - free_vcpu_sec)
    billable_gib_sec = max(0.0, total_gib_sec - free_gib_sec)
    billable_requests = max(0, total_requests - free_requests)

    # Conservative Cloud Run SKU rates (USD):
    # Tier 1: $0.000024/vCPU-s, $0.0000025/GiB-s, $0.40/million requests; Tier 2 ~1.4x
    rate_mult = 1.0 if is_tier1 else 1.4
    compute_usd = (
        billable_vcpu_sec * 0.000024 * rate_mult
        + billable_gib_sec * 0.0000025 * rate_mult
        + (billable_requests / 1_000_000.0) * 0.40 * rate_mult
    )

    # Artifact Registry: > 500 MB billed at $0.10/GB/month
    ar_overage_gb = max(0.0, (obs.artifact_registry_total_mb - 500.0) / 1024.0)
    ar_usd = ar_overage_gb * 0.10

    # Secret Manager: > 6 active versions billed at $0.06/version/month
    secret_overage = max(0, obs.secret_manager_enabled_versions - 6)
    secret_usd = float(secret_overage) * 0.06

    # Serverless VPC Connector: ~$15/month minimum for 2x e2-micro always-on instances
    vpc_usd = float(max(0, obs.vpc_connector_count)) * 15.0

    # Paid Container Scanning: conservative floor if enabled ($0.26/image x pushes)
    scanning_usd = (
        5.0 if PAID_CONTAINER_SCANNING_API in obs.enabled_services else 0.0
    )

    total_usd = (
        compute_usd
        + ar_usd
        + secret_usd
        + vpc_usd
        + scanning_usd
        + max(0.0, float(obs.extra_monthly_usd_estimate))
    )
    buffered_krw = total_usd * max(1000.0, float(obs.fx_krw_per_usd)) * max(
        1.0, float(obs.tax_and_buffer_multiplier)
    )
    return int(round(buffered_krw))


def evaluate_od015_preflight(
    obs: CloudPreflightObservation,
    *,
    allow_tier2_region_override: bool = False,
) -> Od015PreflightReport:
    """Evaluate all 6 read-only OD-015 Free-Tier / ₩20,000 safety checks."""
    checks: list[PreflightCheckResult] = []
    region_lower = obs.region.strip().lower()

    # 1. Region Tier 1 Free Tier eligibility
    region_ok = region_lower in TIER1_FREE_REGIONS or allow_tier2_region_override
    checks.append(
        PreflightCheckResult(
            check_id="OD015_REGION_TIER1_FREE_TIER",
            passed=region_ok,
            severity="CRITICAL" if not region_ok else "INFO",
            message=(
                f"Region {obs.region!r} is Tier 1 Free-Tier eligible."
                if region_lower in TIER1_FREE_REGIONS
                else (
                    f"Region {obs.region!r} is Tier 2 (owner override enabled)."
                    if allow_tier2_region_override
                    else f"Region {obs.region!r} is NOT in Tier 1 Free Tier {sorted(TIER1_FREE_REGIONS)}."
                )
            ),
            observed={
                "region": obs.region,
                "tier1_regions": sorted(TIER1_FREE_REGIONS),
                "owner_override": allow_tier2_region_override,
            },
        )
    )

    # 2. Paid Container Scanning check (READ-ONLY; NEVER mutate org/project security settings!)
    scanning_enabled = PAID_CONTAINER_SCANNING_API in {
        s.strip().lower() for s in obs.enabled_services
    }
    checks.append(
        PreflightCheckResult(
            check_id="OD015_PAID_CONTAINER_SCANNING_DISABLED",
            passed=not scanning_enabled,
            severity="CRITICAL" if scanning_enabled else "INFO",
            message=(
                f"IMMEDIATE NO_GO: {PAID_CONTAINER_SCANNING_API} is enabled on project "
                f"{obs.project_id!r}. Do NOT disable shared project security APIs; use an isolated sandbox."
                if scanning_enabled
                else f"{PAID_CONTAINER_SCANNING_API} is not enabled (read-only check)."
            ),
            observed={"containerscanning_enabled": scanning_enabled},
        )
    )

    # 3. Artifact Registry total storage <= 250 MB (>= 250 MB teardown headroom under 500 MB Free Tier)
    ar_ok = obs.artifact_registry_total_mb <= MAX_ARTIFACT_REGISTRY_MB
    checks.append(
        PreflightCheckResult(
            check_id="OD015_ARTIFACT_REGISTRY_HEADROOM",
            passed=ar_ok,
            severity="HIGH" if not ar_ok else "INFO",
            message=(
                f"Artifact Registry usage {obs.artifact_registry_total_mb:.1f} MB <= {MAX_ARTIFACT_REGISTRY_MB:.0f} MB "
                "(>= 250 MB teardown headroom under 500 MB Free Tier)."
                if ar_ok
                else f"Artifact Registry usage {obs.artifact_registry_total_mb:.1f} MB exceeds {MAX_ARTIFACT_REGISTRY_MB:.0f} MB cap."
            ),
            observed={
                "artifact_registry_total_mb": round(obs.artifact_registry_total_mb, 2),
                "max_allowed_mb": MAX_ARTIFACT_REGISTRY_MB,
            },
        )
    )

    # 4. Secret Manager enabled secret versions <= 4 (under 6 Free Tier versions)
    secrets_ok = obs.secret_manager_enabled_versions <= MAX_SECRET_MANAGER_ENABLED_VERSIONS
    checks.append(
        PreflightCheckResult(
            check_id="OD015_SECRET_MANAGER_VERSION_HEADROOM",
            passed=secrets_ok,
            severity="HIGH" if not secrets_ok else "INFO",
            message=(
                f"Secret Manager enabled versions {obs.secret_manager_enabled_versions} <= {MAX_SECRET_MANAGER_ENABLED_VERSIONS} "
                "(headroom under 6 Free Tier versions)."
                if secrets_ok
                else f"Secret Manager enabled versions {obs.secret_manager_enabled_versions} exceeds {MAX_SECRET_MANAGER_ENABLED_VERSIONS}."
            ),
            observed={
                "secret_manager_enabled_versions": obs.secret_manager_enabled_versions,
                "max_allowed_versions": MAX_SECRET_MANAGER_ENABLED_VERSIONS,
            },
        )
    )

    # 5. Zero paid Serverless VPC Connectors
    vpc_ok = obs.vpc_connector_count <= MAX_VPC_CONNECTOR_COUNT
    checks.append(
        PreflightCheckResult(
            check_id="OD015_ZERO_SERVERLESS_VPC_CONNECTORS",
            passed=vpc_ok,
            severity="CRITICAL" if not vpc_ok else "INFO",
            message=(
                "0 paid Serverless VPC Connectors present."
                if vpc_ok
                else f"Found {obs.vpc_connector_count} paid Serverless VPC Connector(s); must be 0."
            ),
            observed={"vpc_connector_count": obs.vpc_connector_count},
        )
    )

    # 6. Cloud Run caps & conservative KRW worst-case budget (< ₩20,000)
    cap_violations: list[str] = []
    for svc in obs.cloud_run_services:
        if svc.min_instances > MAX_CLOUD_RUN_MIN_INSTANCES:
            cap_violations.append(
                f"{svc.service_name}:min_instances={svc.min_instances}>{MAX_CLOUD_RUN_MIN_INSTANCES}"
            )
        if svc.max_instances > MAX_CLOUD_RUN_MAX_INSTANCES:
            cap_violations.append(
                f"{svc.service_name}:max_instances={svc.max_instances}>{MAX_CLOUD_RUN_MAX_INSTANCES}"
            )
        if svc.cpu > MAX_CLOUD_RUN_CPU:
            cap_violations.append(f"{svc.service_name}:cpu={svc.cpu}>{MAX_CLOUD_RUN_CPU}")
        if svc.memory_mib > MAX_CLOUD_RUN_MEMORY_MIB:
            cap_violations.append(
                f"{svc.service_name}:memory_mib={svc.memory_mib}>{MAX_CLOUD_RUN_MEMORY_MIB}"
            )

    estimated_krw = estimate_monthly_krw_worst_case(obs)
    budget_ok = (len(cap_violations) == 0) and (estimated_krw < HARD_KRW_MONTHLY_CEILING)
    checks.append(
        PreflightCheckResult(
            check_id="OD015_CLOUD_RUN_CAPS_AND_KRW_BUDGET",
            passed=budget_ok,
            severity="CRITICAL" if not budget_ok else "INFO",
            message=(
                f"Cloud Run caps valid and worst-case estimate ₩{estimated_krw:,} < ₩{HARD_KRW_MONTHLY_CEILING:,}."
                if budget_ok
                else (
                    f"Cloud Run cap/budget violation: estimate=₩{estimated_krw:,} "
                    f"(ceiling ₩{HARD_KRW_MONTHLY_CEILING:,}); cap_violations={cap_violations}."
                )
            ),
            observed={
                "estimated_monthly_krw_worst_case": estimated_krw,
                "hard_krw_monthly_ceiling": HARD_KRW_MONTHLY_CEILING,
                "cap_violations": cap_violations,
            },
        )
    )

    all_passed = all(c.passed for c in checks)
    return Od015PreflightReport(
        project_id=obs.project_id,
        region=obs.region,
        verdict="GO" if all_passed else "NO_GO",
        mutations_performed=0,
        estimated_monthly_krw_worst_case=estimated_krw,
        hard_krw_monthly_ceiling=HARD_KRW_MONTHLY_CEILING,
        checks=tuple(checks),
    )


def _gcloud_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "").strip()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
        f"{existing} datacloud.antigravity" if existing else "datacloud.antigravity"
    )
    return env


def _run_gcloud_json(args: list[str], *, timeout_seconds: float = 20.0) -> Any:
    proc = subprocess.run(
        ["gcloud", *args, "--format=json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=_gcloud_env(),
    )
    text = proc.stdout.strip()
    return json.loads(text) if text else []


def collect_gcp_preflight_observation(
    *,
    project_id: str,
    region: str,
) -> CloudPreflightObservation:
    """Collect read-only GCP telemetry via ``gcloud`` (0 mutations)."""
    services_raw = _run_gcloud_json(
        ["services", "list", "--enabled", f"--project={project_id}"]
    )
    enabled_services = frozenset(
        str(item.get("config", {}).get("name") or item.get("name") or "").strip().lower()
        for item in (services_raw if isinstance(services_raw, list) else [])
    )

    repos_raw = _run_gcloud_json(
        ["artifacts", "repositories", "list", f"--project={project_id}"]
    )
    total_bytes = 0
    for repo in repos_raw if isinstance(repos_raw, list) else []:
        size_val = repo.get("sizeBytes")
        if size_val is not None:
            try:
                total_bytes += int(size_val)
            except (TypeError, ValueError):
                pass
    ar_total_mb = total_bytes / (1024.0 * 1024.0)

    secrets_raw = _run_gcloud_json(
        ["secrets", "list", f"--project={project_id}"]
    )
    enabled_versions = 0
    for sec in secrets_raw if isinstance(secrets_raw, list) else []:
        sec_name = str(sec.get("name", "")).rsplit("/", 1)[-1]
        if not sec_name:
            continue
        versions_raw = _run_gcloud_json(
            [
                "secrets",
                "versions",
                "list",
                sec_name,
                "--filter=state=ENABLED",
                f"--project={project_id}",
            ]
        )
        if isinstance(versions_raw, list):
            enabled_versions += len(versions_raw)

    vpc_count = 0
    try:
        vpc_raw = _run_gcloud_json(
            [
                "compute",
                "networks",
                "vpc-access",
                "connectors",
                "list",
                f"--region={region}",
                f"--project={project_id}",
            ]
        )
        if isinstance(vpc_raw, list):
            vpc_count = len(vpc_raw)
    except subprocess.CalledProcessError:
        # API not enabled => 0 VPC connectors
        vpc_count = 0

    return CloudPreflightObservation(
        project_id=project_id,
        region=region,
        enabled_services=enabled_services,
        artifact_registry_total_mb=ar_total_mb,
        secret_manager_enabled_versions=enabled_versions,
        vpc_connector_count=vpc_count,
    )


__all__ = [
    "CloudPreflightObservation",
    "CloudRunServiceCapSpec",
    "HARD_KRW_MONTHLY_CEILING",
    "MAX_ARTIFACT_REGISTRY_MB",
    "MAX_SECRET_MANAGER_ENABLED_VERSIONS",
    "Od015PreflightReport",
    "PAID_CONTAINER_SCANNING_API",
    "PreflightCheckResult",
    "TIER1_FREE_REGIONS",
    "collect_gcp_preflight_observation",
    "estimate_monthly_krw_worst_case",
    "evaluate_od015_preflight",
]
