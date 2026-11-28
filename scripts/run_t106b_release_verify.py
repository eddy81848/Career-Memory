#!/usr/bin/env python3
"""T-106B Immutable Release Manifest Validator, Smoke Probe, and Rollback Command Generator.

Enforces INF-09 / T-106B invariants:
- Rejects any container image reference missing an immutable ``@sha256:<64-hex>`` digest.
- Enforces immutable M0 OpenAPI blob SHA (``07594d75425744d6149250a9f7ff95ac24659f80``).
- Enforces OD-015 Cloud Run service caps (`min_instances == 0`, `max_instances <= 2`,
  `cpu <= 1`, `memory_mib <= 512`).
- Supports `--smoke-url` health/edge-guard verification and `--print-rollback` exact commands.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from career_memory_api.edge_guard.ci_trust_audit import (  # noqa: E402
    M0_IMMUTABLE_OPENAPI_BLOB_SHA,
)
from career_memory_api.hosted_runtime.preflight_od015 import (  # noqa: E402
    MAX_CLOUD_RUN_CPU,
    MAX_CLOUD_RUN_MAX_INSTANCES,
    MAX_CLOUD_RUN_MEMORY_MIB,
    MAX_CLOUD_RUN_MIN_INSTANCES,
    TIER1_FREE_REGIONS,
)

_GIT_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_IMAGE_RE = re.compile(
    r"^[a-z0-9.\-]+/[a-z0-9._/\-]+@sha256:[0-9a-f]{64}$"
)
_REVISION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}$")


class ReleaseManifestValidationError(ValueError):
    """Raised when a ReleaseManifestV1 violates digest pinning, caps, or OpenAPI pin rules."""


@dataclass(frozen=True, slots=True)
class ReleaseServiceCap:
    service_name: str
    min_instances: int = 0
    max_instances: int = 2
    cpu: float = 1.0
    memory_mib: int = 512

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "min_instances": self.min_instances,
            "max_instances": self.max_instances,
            "cpu": self.cpu,
            "memory_mib": self.memory_mib,
        }


@dataclass(frozen=True, slots=True)
class RollbackTargets:
    previous_api_revision: str
    previous_web_revision: str
    previous_worker_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_api_revision": self.previous_api_revision,
            "previous_web_revision": self.previous_web_revision,
            "previous_worker_digest": self.previous_worker_digest,
        }


@dataclass(frozen=True, slots=True)
class ReleaseManifestV1:
    """Redacted immutable release manifest for Cloud Run staging deployments (T-106B)."""

    git_commit_sha: str
    api_image_digest: str
    web_image_digest: str
    region: str = "us-central1"
    openapi_import_v1_blob_sha: str = M0_IMMUTABLE_OPENAPI_BLOB_SHA
    service_caps: tuple[ReleaseServiceCap, ...] = (
        ReleaseServiceCap("cm-api", min_instances=0, max_instances=2, cpu=1.0, memory_mib=512),
        ReleaseServiceCap("cm-worker", min_instances=0, max_instances=1, cpu=1.0, memory_mib=512),
        ReleaseServiceCap("cm-web", min_instances=0, max_instances=2, cpu=1.0, memory_mib=512),
    )
    rollback_targets: RollbackTargets = field(
        default_factory=lambda: RollbackTargets(
            previous_api_revision="cm-api-00001-prev",
            previous_web_revision="cm-web-00001-prev",
            previous_worker_digest=(
                "us-central1-docker.pkg.dev/example-proj/career-memory/cm-api@sha256:"
                + ("0" * 64)
            ),
        )
    )
    manifest_version: str = "release-manifest-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "git_commit_sha": self.git_commit_sha,
            "api_image_digest": self.api_image_digest,
            "web_image_digest": self.web_image_digest,
            "region": self.region,
            "openapi_import_v1_blob_sha": self.openapi_import_v1_blob_sha,
            "service_caps": [c.to_dict() for c in self.service_caps],
            "rollback_targets": self.rollback_targets.to_dict(),
        }


def validate_image_digest_ref(ref: str, *, field_name: str) -> str:
    cleaned = ref.strip()
    if not _DIGEST_IMAGE_RE.match(cleaned):
        raise ReleaseManifestValidationError(
            f"{field_name} must be an immutable '@sha256:<64-hex>' image reference "
            f"(mutable tags are forbidden); got {ref!r}."
        )
    return cleaned


def validate_release_manifest(
    manifest: ReleaseManifestV1,
    *,
    allow_tier2_region: bool = False,
) -> ReleaseManifestV1:
    """Validate all INF-09, OD-015, and M0 OpenAPI immutability invariants on a manifest."""
    if not _GIT_SHA40_RE.match(manifest.git_commit_sha.strip().lower()):
        raise ReleaseManifestValidationError(
            f"git_commit_sha must be a 40-char lowercase hex SHA; got {manifest.git_commit_sha!r}."
        )

    validate_image_digest_ref(manifest.api_image_digest, field_name="api_image_digest")
    validate_image_digest_ref(manifest.web_image_digest, field_name="web_image_digest")
    validate_image_digest_ref(
        manifest.rollback_targets.previous_worker_digest,
        field_name="rollback_targets.previous_worker_digest",
    )

    if not _REVISION_NAME_RE.match(manifest.rollback_targets.previous_api_revision.strip()):
        raise ReleaseManifestValidationError(
            f"Invalid previous_api_revision: {manifest.rollback_targets.previous_api_revision!r}."
        )
    if not _REVISION_NAME_RE.match(manifest.rollback_targets.previous_web_revision.strip()):
        raise ReleaseManifestValidationError(
            f"Invalid previous_web_revision: {manifest.rollback_targets.previous_web_revision!r}."
        )

    if manifest.openapi_import_v1_blob_sha != M0_IMMUTABLE_OPENAPI_BLOB_SHA:
        raise ReleaseManifestValidationError(
            f"openapi_import_v1_blob_sha mismatch: expected {M0_IMMUTABLE_OPENAPI_BLOB_SHA}, "
            f"got {manifest.openapi_import_v1_blob_sha!r}."
        )

    if not allow_tier2_region and manifest.region.strip().lower() not in TIER1_FREE_REGIONS:
        raise ReleaseManifestValidationError(
            f"Region {manifest.region!r} is outside Tier 1 Free Tier {sorted(TIER1_FREE_REGIONS)}."
        )

    if not manifest.service_caps:
        raise ReleaseManifestValidationError("service_caps must not be empty.")

    for cap in manifest.service_caps:
        if cap.min_instances > MAX_CLOUD_RUN_MIN_INSTANCES:
            raise ReleaseManifestValidationError(
                f"{cap.service_name}: min_instances={cap.min_instances} exceeds {MAX_CLOUD_RUN_MIN_INSTANCES}."
            )
        if cap.max_instances > MAX_CLOUD_RUN_MAX_INSTANCES:
            raise ReleaseManifestValidationError(
                f"{cap.service_name}: max_instances={cap.max_instances} exceeds {MAX_CLOUD_RUN_MAX_INSTANCES}."
            )
        if cap.cpu > MAX_CLOUD_RUN_CPU:
            raise ReleaseManifestValidationError(
                f"{cap.service_name}: cpu={cap.cpu} exceeds {MAX_CLOUD_RUN_CPU}."
            )
        if cap.memory_mib > MAX_CLOUD_RUN_MEMORY_MIB:
            raise ReleaseManifestValidationError(
                f"{cap.service_name}: memory_mib={cap.memory_mib} exceeds {MAX_CLOUD_RUN_MEMORY_MIB}."
            )

    return manifest


def parse_release_manifest_dict(data: dict[str, Any]) -> ReleaseManifestV1:
    caps_raw = data.get("service_caps") or []
    caps = tuple(
        ReleaseServiceCap(
            service_name=str(item["service_name"]),
            min_instances=int(item.get("min_instances", 0)),
            max_instances=int(item.get("max_instances", 2)),
            cpu=float(item.get("cpu", 1.0)),
            memory_mib=int(item.get("memory_mib", 512)),
        )
        for item in caps_raw
    )
    rb_raw = data.get("rollback_targets") or {}
    rb = RollbackTargets(
        previous_api_revision=str(rb_raw.get("previous_api_revision", "")),
        previous_web_revision=str(rb_raw.get("previous_web_revision", "")),
        previous_worker_digest=str(rb_raw.get("previous_worker_digest", "")),
    )
    return ReleaseManifestV1(
        git_commit_sha=str(data.get("git_commit_sha", "")),
        api_image_digest=str(data.get("api_image_digest", "")),
        web_image_digest=str(data.get("web_image_digest", "")),
        region=str(data.get("region", "us-central1")),
        openapi_import_v1_blob_sha=str(data.get("openapi_import_v1_blob_sha", "")),
        service_caps=caps,
        rollback_targets=rb,
        manifest_version=str(data.get("manifest_version", "release-manifest-v1")),
    )


def build_rollback_commands(
    manifest: ReleaseManifestV1,
    *,
    project_id: str,
    api_service_name: str = "cm-api",
    web_service_name: str = "cm-web",
    worker_service_name: str = "cm-worker",
) -> list[str]:
    """Emit exact Cloud Run instant traffic-shift & worker @sha256: digest restore commands."""
    validate_release_manifest(manifest, allow_tier2_region=True)
    attr = (
        'CLOUDSDK_METRICS_ENVIRONMENT="${CLOUDSDK_METRICS_ENVIRONMENT:+$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.antigravity"'
    )
    return [
        (
            f"{attr} gcloud run services update-traffic {api_service_name} "
            f"--project={project_id} --region={manifest.region} "
            f"--to-revisions={manifest.rollback_targets.previous_api_revision}=100"
        ),
        (
            f"{attr} gcloud run services update-traffic {web_service_name} "
            f"--project={project_id} --region={manifest.region} "
            f"--to-revisions={manifest.rollback_targets.previous_web_revision}=100"
        ),
        (
            f"{attr} gcloud run deploy {worker_service_name} "
            f"--project={project_id} --region={manifest.region} "
            f"--image={manifest.rollback_targets.previous_worker_digest} "
            "--no-allow-unauthenticated --min-instances=0 --max-instances=1 "
            "--cpu=1 --memory=512Mi"
        ),
    ]


def _http_probe(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 20.0,
    follow_redirects: bool = True,
) -> tuple[int, dict[str, str], str, float]:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # type: ignore[no-untyped-def]
            return None

    opener = (
        urllib.request.build_opener()
        if follow_redirects
        else urllib.request.build_opener(_NoRedirect)
    )
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), hdrs, resp.read(16384).decode("utf-8", errors="replace"), elapsed_ms
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else ())}
        body = ""
        try:
            if exc.fp is not None:
                body = exc.fp.read(8192).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        return int(exc.code), hdrs, body, elapsed_ms


def run_smoke_checks(
    *,
    api_url: str,
    worker_url: str | None = None,
    worker_oidc_token: str | None = None,
    web_url: str | None = None,
    supabase_url: str | None = None,
    supabase_anon_key: str | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Run live staging smoke & isolation assertions across cm-api, cm-worker, cm-web & Data API."""
    base_api = api_url.rstrip("/")
    results: list[dict[str, Any]] = []

    # 1. GET /api/health (fallback /healthz locally) -> 200 (captures first-call cold start latency, T-007F Step 3)
    st_health, hdr_health, _, lat_api_ms = _http_probe(
        f"{base_api}/api/health", method="GET", timeout_seconds=timeout_seconds
    )
    if st_health == 404:
        st_health, hdr_health, _, lat_api_ms = _http_probe(
            f"{base_api}/healthz", method="GET", timeout_seconds=timeout_seconds
        )
    results.append(
        {
            "check": "api_health_200",
            "passed": st_health == 200 and "no-store" in hdr_health.get("cache-control", "").lower(),
            "status_code": st_health,
            "cache_control": hdr_health.get("cache-control", ""),
            "latency_ms": lat_api_ms,
        }
    )

    # 2. Direct cm-api /v1/session without X-CM-Origin-Auth -> 403 ORIGIN_AUTH_REQUIRED + private, no-store
    st_direct, hdr_direct, body_direct, lat_direct_ms = _http_probe(
        f"{base_api}/v1/session", method="GET", timeout_seconds=timeout_seconds
    )
    cc_direct = hdr_direct.get("cache-control", "").lower()
    results.append(
        {
            "check": "api_direct_origin_auth_blocked_403",
            "passed": (
                st_direct == 403
                and "ORIGIN_AUTH_REQUIRED" in body_direct
                and "private" in cc_direct
                and "no-store" in cc_direct
            ),
            "status_code": st_direct,
            "cache_control": hdr_direct.get("cache-control", ""),
            "latency_ms": lat_direct_ms,
        }
    )

    # 3. Direct /internal/worker/wake on cm-api -> 403 PUBLIC_WORKER_ROUTE_FORBIDDEN
    st_api_worker, _, body_api_worker, _ = _http_probe(
        f"{base_api}/internal/worker/wake", method="POST", timeout_seconds=timeout_seconds
    )
    results.append(
        {
            "check": "api_internal_worker_route_blocked_403",
            "passed": (
                st_api_worker == 403 and "PUBLIC_WORKER_ROUTE_FORBIDDEN" in body_api_worker
            ),
            "status_code": st_api_worker,
        }
    )

    # 4. Private IAM cm-worker checks (T-007F Step 1 & 2)
    if worker_url:
        base_worker = worker_url.rstrip("/")
        st_worker, _, _, lat_worker_unauth_ms = _http_probe(
            f"{base_worker}/internal/worker/wake",
            method="POST",
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "worker_unauthenticated_blocked_401_403",
                "passed": st_worker in (401, 403),
                "status_code": st_worker,
                "latency_ms": lat_worker_unauth_ms,
            }
        )
        if worker_oidc_token and worker_oidc_token.strip():
            st_w_auth, _, body_w_auth, lat_worker_auth_ms = _http_probe(
                f"{base_worker}/internal/worker/recover",
                method="POST",
                headers={
                    "Authorization": f"Bearer {worker_oidc_token.strip()}",
                    "Content-Type": "application/json",
                },
                timeout_seconds=timeout_seconds,
            )
            results.append(
                {
                    "check": "worker_oidc_authorized_recover_200",
                    "passed": st_w_auth == 200 and '"status"' in body_w_auth,
                    "status_code": st_w_auth,
                    "latency_ms": lat_worker_auth_ms,
                }
            )

    # 5. Next.js BFF cm-web checks (OD-010 PKCE, __Host- HttpOnly/Secure cookie, CSRF, no-store)
    if web_url:
        base_web = web_url.rstrip("/")
        st_web_h, hdr_web_h, _, lat_web_ms = _http_probe(
            f"{base_web}/api/health", method="GET", timeout_seconds=timeout_seconds
        )
        if st_web_h == 404:
            st_web_h, hdr_web_h, _, lat_web_ms = _http_probe(
                f"{base_web}/healthz", method="GET", timeout_seconds=timeout_seconds
            )
        results.append(
            {
                "check": "web_health_200",
                "passed": st_web_h == 200 and "no-store" in hdr_web_h.get("cache-control", "").lower(),
                "status_code": st_web_h,
                "cache_control": hdr_web_h.get("cache-control", ""),
                "latency_ms": lat_web_ms,
            }
        )
        st_web_s, hdr_web_s, body_web_s, _ = _http_probe(
            f"{base_web}/auth/session", method="GET", timeout_seconds=timeout_seconds
        )
        cc_web_s = hdr_web_s.get("cache-control", "").lower()
        results.append(
            {
                "check": "web_auth_session_unauthenticated_200_no_store",
                "passed": (
                    st_web_s == 200
                    and '"authenticated":false' in body_web_s.replace(" ", "")
                    and "private" in cc_web_s
                    and "no-store" in cc_web_s
                ),
                "status_code": st_web_s,
                "cache_control": hdr_web_s.get("cache-control", ""),
            }
        )
        st_pkce, hdr_pkce, _, _ = _http_probe(
            f"{base_web}/auth/login?next=/event-review",
            method="GET",
            timeout_seconds=timeout_seconds,
            follow_redirects=False,
        )
        set_cookie_hdr = hdr_pkce.get("set-cookie", "")
        loc_hdr = hdr_pkce.get("location", "")
        results.append(
            {
                "check": "web_pkce_login_redirect_and_secure_httponly_cookie",
                "passed": (
                    st_pkce in (302, 303, 307)
                    and "code_challenge=" in loc_hdr
                    and "httponly" in set_cookie_hdr.lower()
                ),
                "status_code": st_pkce,
                "has_pkce_challenge": "code_challenge=" in loc_hdr,
                "has_httponly_cookie": "httponly" in set_cookie_hdr.lower(),
                "has_secure_cookie": "secure" in set_cookie_hdr.lower(),
            }
        )
        st_open_redir, _, _, _ = _http_probe(
            f"{base_web}/auth/login?next=//evil.example",
            method="GET",
            timeout_seconds=timeout_seconds,
            follow_redirects=False,
        )
        results.append(
            {
                "check": "web_open_redirect_rejected_400",
                "passed": st_open_redir == 400,
                "status_code": st_open_redir,
            }
        )
        st_csrf, _, _, _ = _http_probe(
            f"{base_web}/bff/v1/sync-runs",
            method="POST",
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "web_bff_cross_origin_csrf_blocked_403",
                "passed": st_csrf == 403,
                "status_code": st_csrf,
            }
        )

    # 6. Supabase Data API (PostgREST) direct domain table bypass blocked (OD006-5)
    if supabase_url and supabase_anon_key:
        supa_base = supabase_url.rstrip("/")
        st_rest, _, _, _ = _http_probe(
            f"{supa_base}/rest/v1/work_events?select=id&limit=1",
            method="GET",
            headers={
                "apikey": supabase_anon_key.strip(),
                "Authorization": f"Bearer {supabase_anon_key.strip()}",
            },
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "supabase_postgrest_domain_table_bypass_blocked",
                "passed": st_rest in (401, 403, 404),
                "status_code": st_rest,
            }
        )

    return {
        "passed": all(r["passed"] for r in results),
        "total_http_requests": len(results),
        "checks": results,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="T-106B Immutable Release Manifest Validator, Smoke & Rollback CLI"
    )
    parser.add_argument("--manifest", type=Path, help="Path to ReleaseManifestV1 JSON file.")
    parser.add_argument("--git-commit-sha", default=None)
    parser.add_argument("--api-image-digest", default=None)
    parser.add_argument("--web-image-digest", default=None)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--previous-api-revision", default="cm-api-00001-prev")
    parser.add_argument("--previous-web-revision", default="cm-web-00001-prev")
    parser.add_argument("--previous-worker-digest", default=None)
    parser.add_argument("--project-id", default="career-memory-staging")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--smoke-url", default=None, help="Live cm-api URL to smoke test.")
    parser.add_argument("--worker-url", default=None, help="Live cm-worker URL to smoke test.")
    parser.add_argument("--worker-oidc-token", default=None, help="OIDC token for cm-worker recover check.")
    parser.add_argument("--web-url", default=None, help="Live cm-web URL to smoke test.")
    parser.add_argument("--supabase-url", default=None, help="Supabase project URL for Data API isolation check.")
    parser.add_argument("--supabase-anon-key", default=None, help="Supabase anon key for Data API isolation check.")
    parser.add_argument(
        "--print-rollback",
        action="store_true",
        help="Print exact gcloud rollback commands for the manifest.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.smoke_url:
        smoke = run_smoke_checks(
            api_url=args.smoke_url,
            worker_url=args.worker_url,
            worker_oidc_token=args.worker_oidc_token,
            web_url=args.web_url,
            supabase_url=args.supabase_url,
            supabase_anon_key=args.supabase_anon_key,
        )
        print(json.dumps(smoke, indent=2, sort_keys=True))
        return 0 if smoke["passed"] else 1

    if args.manifest is not None:
        raw = json.loads(args.manifest.read_text(encoding="utf-8"))
        manifest = validate_release_manifest(parse_release_manifest_dict(raw))
    else:
        if not args.git_commit_sha or not args.api_image_digest or not args.web_image_digest:
            print(
                "ERROR: Provide --manifest <path> or --git-commit-sha, --api-image-digest, and --web-image-digest.",
                file=sys.stderr,
            )
            return 2
        prev_worker = args.previous_worker_digest or args.api_image_digest
        manifest = validate_release_manifest(
            ReleaseManifestV1(
                git_commit_sha=args.git_commit_sha,
                api_image_digest=args.api_image_digest,
                web_image_digest=args.web_image_digest,
                region=args.region,
                rollback_targets=RollbackTargets(
                    previous_api_revision=args.previous_api_revision,
                    previous_web_revision=args.previous_web_revision,
                    previous_worker_digest=prev_worker,
                ),
            )
        )

    if args.output is not None:
        args.output.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if args.print_rollback:
        cmds = build_rollback_commands(manifest, project_id=args.project_id)
        for cmd in cmds:
            print(cmd)
        return 0

    print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
