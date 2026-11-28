#!/usr/bin/env python3
"""T-106B Immutable Release Manifest Validator, Smoke Probe, and Rollback Command Generator.

Enforces INF-09 / T-106B invariants:
- Rejects any container image reference missing an immutable ``@sha256:<64-hex>`` digest.
- Enforces immutable M0 OpenAPI blob SHA (``07594d75425744d6149250a9f7ff95ac24659f80``).
- Enforces OD-015 Cloud Run service caps (`min_instances == 0`, `max_instances <= 2`,
  `cpu <= 1`, `memory_mib <= 512`).
- Supports `--smoke-url` live Cross-Service Origin-HMAC / Worker-OIDC / BFF / PostgREST E2E
  verification and `--print-rollback` exact commands.
"""

from __future__ import annotations

import argparse
import json
import os
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
from career_memory_api.edge_guard.models import (  # noqa: E402
    DEFAULT_ORIGIN_AUTH_HEADER,
    sign_origin_auth_header,
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
_SYNTHETIC_PROBE_WORKSPACE_ID = "00000000-0000-4000-8000-000000000000"


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
    body_bytes: bytes | None = None,
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
    req_headers: dict[str, str] = dict(headers or {})
    if not any(k.lower() == "user-agent" for k in req_headers):
        req_headers["User-Agent"] = "CareerMemory-LiveVerify/1.0"
    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
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
    origin_hmac_secret: str | None = None,
    worker_url: str | None = None,
    worker_oidc_token: str | None = None,
    web_url: str | None = None,
    supabase_url: str | None = None,
    supabase_anon_key: str | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Run live staging contract, security & isolation assertions across cm-api, cm-worker, cm-web & Supabase."""
    base_api = api_url.rstrip("/")
    results: list[dict[str, Any]] = []

    # 1. GET /api/health (fallback /healthz locally) -> 200
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

    # 2. Direct cm-api /v1/session, /v1/work-events, /v1/sync-runs without X-CM-Origin-Auth -> 403 ORIGIN_AUTH_REQUIRED
    for check_name, method, path, payload in (
        ("api_direct_origin_auth_blocked_403", "GET", "/v1/session", None),
        ("api_direct_work_events_origin_auth_blocked_403", "GET", "/v1/work-events", None),
        ("api_direct_sync_runs_origin_auth_blocked_403", "POST", "/v1/sync-runs", b"{}"),
    ):
        probe_headers = {"Content-Type": "application/json"} if payload is not None else None
        st_direct, hdr_direct, body_direct, lat_direct_ms = _http_probe(
            f"{base_api}{path}",
            method=method,
            headers=probe_headers,
            body_bytes=payload,
            timeout_seconds=timeout_seconds,
        )
        cc_direct = hdr_direct.get("cache-control", "").lower()
        results.append(
            {
                "check": check_name,
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

    # 3. Direct /internal/worker/wake & undeclared /openapi.json on cm-api
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
    st_undeclared, _, body_undeclared, _ = _http_probe(
        f"{base_api}/openapi.json", method="GET", timeout_seconds=timeout_seconds
    )
    results.append(
        {
            "check": "api_undeclared_openapi_route_blocked_404",
            "passed": st_undeclared == 404 and "ROUTE_NOT_DECLARED" in body_undeclared,
            "status_code": st_undeclared,
        }
    )

    # 3b. Live cm-api X-CM-Origin-Auth HMAC-signed contract probes (using live secret in memory, never logged)
    cleaned_hmac = (origin_hmac_secret or "").strip()
    if cleaned_hmac:
        now_ts = int(time.time())
        st_bad_hmac, _, body_bad_hmac, _ = _http_probe(
            f"{base_api}/v1/session",
            method="GET",
            headers={DEFAULT_ORIGIN_AUTH_HEADER: f"t={now_ts},sig={'0' * 64}"},
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "api_tampered_origin_hmac_blocked_403",
                "passed": st_bad_hmac == 403 and "ORIGIN_AUTH_REQUIRED" in body_bad_hmac,
                "status_code": st_bad_hmac,
            }
        )

        for check_name, method, path, payload in (
            ("api_signed_session_unauthenticated_401", "GET", "/v1/session", None),
            ("api_signed_work_events_unauthenticated_401", "GET", "/v1/work-events", None),
            ("api_signed_sync_runs_unauthenticated_401", "POST", "/v1/sync-runs", b"{}"),
        ):
            sig_hdr = sign_origin_auth_header(
                cleaned_hmac,
                method=method,
                path=path,
                timestamp=int(time.time()),
            )
            signed_headers: dict[str, str] = {
                DEFAULT_ORIGIN_AUTH_HEADER: sig_hdr,
                "Authorization": "Bearer invalid.e2e.jwt",
                "X-Workspace-ID": _SYNTHETIC_PROBE_WORKSPACE_ID,
                "Accept": "application/json",
            }
            if payload is not None:
                signed_headers["Content-Type"] = "application/json"
            st_sig, hdr_sig, _, lat_sig_ms = _http_probe(
                f"{base_api}{path}",
                method=method,
                headers=signed_headers,
                body_bytes=payload,
                timeout_seconds=timeout_seconds,
            )
            cc_sig = hdr_sig.get("cache-control", "").lower()
            results.append(
                {
                    "check": check_name,
                    "passed": (
                        st_sig == 401
                        and "private" in cc_sig
                        and "no-store" in cc_sig
                    ),
                    "status_code": st_sig,
                    "cache_control": hdr_sig.get("cache-control", ""),
                    "latency_ms": lat_sig_ms,
                }
            )

        sig_csrf = sign_origin_auth_header(
            cleaned_hmac,
            method="POST",
            path="/v1/sync-runs",
            timestamp=int(time.time()),
        )
        st_api_csrf, _, body_api_csrf, _ = _http_probe(
            f"{base_api}/v1/sync-runs",
            method="POST",
            headers={
                DEFAULT_ORIGIN_AUTH_HEADER: sig_csrf,
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
            body_bytes=b"{}",
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "api_signed_sync_runs_evil_origin_csrf_403",
                "passed": st_api_csrf == 403 and "CSRF_ORIGIN_DENIED" in body_api_csrf,
                "status_code": st_api_csrf,
            }
        )

        sig_worker = sign_origin_auth_header(
            cleaned_hmac,
            method="POST",
            path="/internal/worker/wake",
            timestamp=int(time.time()),
        )
        st_worker_hmac, _, body_worker_hmac, _ = _http_probe(
            f"{base_api}/internal/worker/wake",
            method="POST",
            headers={
                DEFAULT_ORIGIN_AUTH_HEADER: sig_worker,
                "Content-Type": "application/json",
            },
            body_bytes=b"{}",
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "api_internal_worker_route_blocked_even_with_hmac_403",
                "passed": (
                    st_worker_hmac == 403
                    and "PUBLIC_WORKER_ROUTE_FORBIDDEN" in body_worker_hmac
                ),
                "status_code": st_worker_hmac,
            }
        )

    # 4. Private IAM cm-worker checks (IAM + OIDC + :6543 Supavisor cm_worker_app FORCE RLS connectivity)
    if worker_url:
        base_worker = worker_url.rstrip("/")
        st_worker_wake_unauth, _, _, lat_worker_unauth_ms = _http_probe(
            f"{base_worker}/internal/worker/wake",
            method="POST",
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "worker_unauthenticated_blocked_401_403",
                "passed": st_worker_wake_unauth in (401, 403),
                "status_code": st_worker_wake_unauth,
                "latency_ms": lat_worker_unauth_ms,
            }
        )
        st_worker_rec_unauth, _, _, _ = _http_probe(
            f"{base_worker}/internal/worker/recover",
            method="POST",
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "worker_unauthenticated_recover_blocked_401_403",
                "passed": st_worker_rec_unauth in (401, 403),
                "status_code": st_worker_rec_unauth,
            }
        )
        if worker_oidc_token and worker_oidc_token.strip():
            oidc_bearer = f"Bearer {worker_oidc_token.strip()}"
            recover_payload = json.dumps(
                {
                    "workspace_id": _SYNTHETIC_PROBE_WORKSPACE_ID,
                    "max_slices": 1,
                    "outbox_limit": 5,
                }
            ).encode("utf-8")
            st_w_rec, _, body_w_rec, lat_worker_rec_ms = _http_probe(
                f"{base_worker}/internal/worker/recover",
                method="POST",
                headers={
                    "Authorization": oidc_bearer,
                    "Content-Type": "application/json",
                },
                body_bytes=recover_payload,
                timeout_seconds=timeout_seconds,
            )
            parsed_rec: dict[str, Any] = {}
            try:
                parsed_rec = json.loads(body_w_rec) if body_w_rec else {}
            except Exception:  # noqa: BLE001
                parsed_rec = {}
            results.append(
                {
                    "check": "worker_oidc_authorized_recover_200",
                    "passed": (
                        st_w_rec == 200
                        and parsed_rec.get("status") == "ok"
                        and parsed_rec.get("workspaces_scanned") == 1
                    ),
                    "status_code": st_w_rec,
                    "workspaces_scanned": parsed_rec.get("workspaces_scanned"),
                    "latency_ms": lat_worker_rec_ms,
                }
            )

            wake_payload = json.dumps(
                {
                    "workspace_id": _SYNTHETIC_PROBE_WORKSPACE_ID,
                    "max_slices": 1,
                }
            ).encode("utf-8")
            st_w_wake, _, body_w_wake, lat_worker_wake_ms = _http_probe(
                f"{base_worker}/internal/worker/wake",
                method="POST",
                headers={
                    "Authorization": oidc_bearer,
                    "Content-Type": "application/json",
                },
                body_bytes=wake_payload,
                timeout_seconds=timeout_seconds,
            )
            parsed_wake: dict[str, Any] = {}
            try:
                parsed_wake = json.loads(body_w_wake) if body_w_wake else {}
            except Exception:  # noqa: BLE001
                parsed_wake = {}
            results.append(
                {
                    "check": "worker_oidc_authorized_wake_200",
                    "passed": (
                        st_w_wake == 200
                        and parsed_wake.get("status") == "ok"
                        and parsed_wake.get("workspace_id") == _SYNTHETIC_PROBE_WORKSPACE_ID
                    ),
                    "status_code": st_w_wake,
                    "latency_ms": lat_worker_wake_ms,
                }
            )

            st_w_v1, _, body_w_v1, _ = _http_probe(
                f"{base_worker}/v1/session",
                method="GET",
                headers={"Authorization": oidc_bearer},
                timeout_seconds=timeout_seconds,
            )
            results.append(
                {
                    "check": "worker_public_v1_route_denied_404",
                    "passed": st_w_v1 == 404 and "ROUTE_NOT_DECLARED" in body_w_v1,
                    "status_code": st_w_v1,
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
                    and "code_challenge_method=s256" in loc_hdr.lower()
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
            body_bytes=b"{}",
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "web_bff_cross_origin_csrf_blocked_403",
                "passed": st_csrf == 403,
                "status_code": st_csrf,
            }
        )

    # 6. Supabase Data API (PostgREST) direct domain table bypass blocked (OD006-5) + JWKS health
    if supabase_url:
        supa_base = supabase_url.rstrip("/")
        st_jwks, _, body_jwks, _ = _http_probe(
            f"{supa_base}/auth/v1/.well-known/jwks.json",
            method="GET",
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "check": "supabase_auth_jwks_reachable_200",
                "passed": st_jwks == 200 and '"keys"' in body_jwks,
                "status_code": st_jwks,
            }
        )
        if supabase_anon_key and supabase_anon_key.strip():
            anon_hdrs = {
                "apikey": supabase_anon_key.strip(),
                "Authorization": f"Bearer {supabase_anon_key.strip()}",
            }
            for check_name, table_name in (
                ("supabase_postgrest_domain_table_bypass_blocked", "work_events"),
                ("supabase_postgrest_claims_bypass_blocked", "claims"),
                ("supabase_postgrest_workspaces_bypass_blocked", "workspaces"),
            ):
                st_rest, _, _, _ = _http_probe(
                    f"{supa_base}/rest/v1/{table_name}?select=id&limit=1",
                    method="GET",
                    headers=anon_hdrs,
                    timeout_seconds=timeout_seconds,
                )
                results.append(
                    {
                        "check": check_name,
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
    parser.add_argument(
        "--origin-hmac-secret",
        default=None,
        help="Optional X-CM-Origin-Auth HMAC secret (defaults to CM_ORIGIN_HMAC_SECRET env var; never printed).",
    )
    parser.add_argument("--worker-url", default=None, help="Live cm-worker URL to smoke test.")
    parser.add_argument("--worker-oidc-token", default=None, help="OIDC token for cm-worker recover/wake checks.")
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
        resolved_hmac = args.origin_hmac_secret or os.environ.get("CM_ORIGIN_HMAC_SECRET")
        resolved_oidc = args.worker_oidc_token or os.environ.get("WORKER_OIDC_TOKEN")
        resolved_anon = (
            args.supabase_anon_key
            or os.environ.get("STAGING_SUPABASE_ANON_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
        )
        smoke = run_smoke_checks(
            api_url=args.smoke_url,
            origin_hmac_secret=resolved_hmac,
            worker_url=args.worker_url,
            worker_oidc_token=resolved_oidc,
            web_url=args.web_url,
            supabase_url=args.supabase_url,
            supabase_anon_key=resolved_anon,
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
