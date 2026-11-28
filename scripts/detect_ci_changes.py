#!/usr/bin/env python3
"""Smart Path-Aware CI/CD Change Scope Classifier for Career Memory.

Determines the minimal sound set of direct + indirect tests, builds, migrations,
and Cloud Run deployments required for a given set of changed repository paths.

Fail-safe invariant:
- Known non-runtime paths (`docs/**`, `*.md`, `.gitignore`, offline evidence JSONs)
  skip unrelated heavy suites and Cloud Run deployments.
- Unrecognized top-level paths fail-safe to running full test suites rather than
  silently skipping verification.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_ZERO_SHA = "0" * 40

_WEB_TEST_ONLY_SUFFIXES = (
    ".test.ts",
    ".test.tsx",
    ".spec.ts",
    ".spec.tsx",
)


def _norm(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _is_web_test_or_config_only(path: str) -> bool:
    if not path.startswith("apps/web/"):
        return False
    if path.startswith(
        (
            "apps/web/tests/",
            "apps/web/e2e/",
            "apps/web/e2e-hosted/",
            "apps/web/src/test/",
        )
    ):
        return True
    if path.endswith(_WEB_TEST_ONLY_SUFFIXES):
        return True
    if path in (
        "apps/web/playwright.config.ts",
        "apps/web/playwright.hosted.config.ts",
        "apps/web/vitest.config.ts",
        "apps/web/vitest.contract-http.config.ts",
    ):
        return True
    return False


def _is_api_fast_only_test(path: str) -> bool:
    return path.startswith(
        (
            "apps/api/tests/pilot_gate/",
            "apps/api/tests/edge_guard/",
            "apps/api/tests/llm_gate/",
        )
    )


@dataclass(frozen=True, slots=True)
class ChangeScopeResult:
    changed_files: tuple[str, ...]
    run_static_gate: bool
    run_api_unit: bool
    run_api_pg18: bool
    run_m0_spike: bool
    run_web_test: bool
    run_web_contract_http: bool
    run_web_build: bool
    run_web_e2e: bool
    deploy_api: bool
    deploy_web: bool
    run_migrations: bool
    sync_ar_policy: bool

    @property
    def any_cd_work(self) -> bool:
        return (
            self.deploy_api
            or self.deploy_web
            or self.run_migrations
            or self.sync_ar_policy
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "changed_files_count": len(self.changed_files),
            "changed_files": list(self.changed_files),
            "run_static_gate": self.run_static_gate,
            "run_api_unit": self.run_api_unit,
            "run_api_pg18": self.run_api_pg18,
            "run_m0_spike": self.run_m0_spike,
            "run_web_test": self.run_web_test,
            "run_web_contract_http": self.run_web_contract_http,
            "run_web_build": self.run_web_build,
            "run_web_e2e": self.run_web_e2e,
            "deploy_api": self.deploy_api,
            "deploy_web": self.deploy_web,
            "run_migrations": self.run_migrations,
            "sync_ar_policy": self.sync_ar_policy,
            "any_cd_work": self.any_cd_work,
        }

    def to_github_outputs(self) -> dict[str, str]:
        return {
            k: ("true" if v else "false")
            for k, v in self.to_dict().items()
            if isinstance(v, bool)
        }


def classify_changed_paths(
    paths: Iterable[str],
    *,
    force_all: bool = False,
) -> ChangeScopeResult:
    """Classify direct and indirect CI/CD impact for ``paths``."""
    cleaned = tuple(sorted({_norm(p) for p in paths if _norm(p)}))

    if force_all:
        return ChangeScopeResult(
            changed_files=cleaned,
            run_static_gate=True,
            run_api_unit=True,
            run_api_pg18=True,
            run_m0_spike=True,
            run_web_test=True,
            run_web_contract_http=True,
            run_web_build=True,
            run_web_e2e=True,
            deploy_api=True,
            deploy_web=True,
            run_migrations=True,
            sync_ar_policy=True,
        )

    run_static_gate = True
    run_api_unit = False
    run_api_pg18 = False
    run_m0_spike = False
    run_web_test = False
    run_web_contract_http = False
    run_web_build = False
    run_web_e2e = False
    deploy_api = False
    deploy_web = False
    run_migrations = False
    sync_ar_policy = False

    for p in cleaned:
        # 0. Known pure documentation & git metadata -> static conformance/gate check only
        if p.startswith("docs/") or p.endswith(".md") or p in (".gitignore", "LICENSE"):
            continue

        # 0b. Root .dockerignore affects root-context `apps/api/Dockerfile` image build
        if p == ".dockerignore":
            run_api_unit = True
            run_api_pg18 = True
            deploy_api = True
            continue

        # 1. Shared OpenAPI / Conformance / AI schemas -> direct + indirect impact across API & Web
        if p.startswith(("contracts/import-v1/", "contracts/conformance/", "contracts/ai-schemas/")):
            run_api_unit = True
            run_api_pg18 = True
            run_web_test = True
            run_web_contract_http = True
            deploy_api = True
            if p.startswith("contracts/import-v1/"):
                run_m0_spike = True
                run_web_build = True
                run_web_e2e = True
                deploy_web = True
            continue

        # 1b. Offline gate/evidence contract JSONs -> verified by static gate + fast unit tests (< 1s)
        if p.startswith(
            (
                "contracts/pilot-gate/",
                "contracts/edge-guard/",
                "contracts/llm-gate/",
                "contracts/hosted-staging/",
            )
        ):
            run_api_unit = True
            continue

        # 2. Database migrations -> API + PG18 + live staging migrations + API/Worker deploy + FE contract-http
        if p.startswith("db/migrations/"):
            run_api_unit = True
            run_api_pg18 = True
            run_web_contract_http = True
            deploy_api = True
            run_migrations = True
            continue

        # 3. Frozen M0 import contract spike
        if p.startswith("spikes/import-contract/"):
            run_m0_spike = True
            continue

        # 4. Backend / Worker application & tests (apps/api/**)
        if p.startswith("apps/api/"):
            if p == "apps/api/.gitignore":
                continue
            run_api_unit = True
            if p.startswith("apps/api/tests/"):
                if not _is_api_fast_only_test(p):
                    run_api_pg18 = True
                continue

            if p.startswith(
                (
                    "apps/api/src/career_memory_api/pilot_gate/",
                    "apps/api/src/career_memory_api/edge_guard/ci_trust_audit.py",
                    "apps/api/src/career_memory_api/hosted_runtime/preflight_od015.py",
                )
            ):
                # Pure offline gate/CI-audit/preflight modules do not alter runtime API/Worker behavior
                continue

            run_api_pg18 = True
            deploy_api = True

            if p == "apps/api/src/career_memory_api/hosted_runtime/migration_runner.py":
                run_migrations = True

            # Indirect impact on Web BFF / HTTP contract layer when API routes/models/auth/edge_guard change
            if not p.startswith("apps/api/src/career_memory_api/hosted_runtime/worker_"):
                run_web_contract_http = True
            continue

        # 5. Frontend / BFF application & tests (apps/web/**)
        if p.startswith("apps/web/"):
            if p == "apps/web/.gitignore":
                continue
            run_web_test = True
            if p.startswith("apps/web/tests/contract-http/") or p.startswith("apps/web/src/adapters/"):
                run_web_contract_http = True
            if p.startswith("apps/web/e2e/") or p == "apps/web/playwright.config.ts":
                run_web_build = True
                run_web_e2e = True
            if not _is_web_test_or_config_only(p):
                run_web_build = True
                run_web_e2e = True
                deploy_web = True
            continue

        # 6. Operational scripts
        if p.startswith("scripts/"):
            run_api_unit = True
            if p in ("scripts/run_conformance_ci.py", "scripts/run_local_fullstack.py"):
                run_api_pg18 = True
            if p == "scripts/run_staging_migrations.py":
                run_api_pg18 = True
                run_migrations = True
            continue

        # 7. Artifact Registry cleanup policy & Cloudflare edge infra config
        if p == "infra/gcp/artifact-registry-cleanup-policy.json":
            sync_ar_policy = True
            continue
        if p.startswith("infra/cloudflare/"):
            continue

        # 8. CI/CD workflows -> audited by static gate (`detect-and-static` runs edge_guard + pilot_gate)
        if p.startswith(".github/workflows/"):
            continue

        # 9. Fail-safe fallback for any unrecognized future path: run full test suites (never skip silently)
        run_api_unit = True
        run_api_pg18 = True
        run_web_test = True
        run_web_contract_http = True
        run_web_build = True
        run_web_e2e = True

    if deploy_api or deploy_web:
        sync_ar_policy = True

    return ChangeScopeResult(
        changed_files=cleaned,
        run_static_gate=run_static_gate,
        run_api_unit=run_api_unit,
        run_api_pg18=run_api_pg18,
        run_m0_spike=run_m0_spike,
        run_web_test=run_web_test,
        run_web_contract_http=run_web_contract_http,
        run_web_build=run_web_build,
        run_web_e2e=run_web_e2e,
        deploy_api=deploy_api,
        deploy_web=deploy_web,
        run_migrations=run_migrations,
        sync_ar_policy=sync_ar_policy,
    )


def _git_diff_files(base: str, head: str, *, use_merge_base: bool = False) -> list[str]:
    if not base or base == _ZERO_SHA:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", f"{head}^"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            base = probe.stdout.strip()
        else:
            out = subprocess.check_output(["git", "ls-files"], text=True)
            return [line for line in out.splitlines() if line.strip()]

    # Verify `base` commit exists in local git object store; fail-safe to full tree if unreachable
    verify_base = subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify_base.returncode != 0:
        out = subprocess.check_output(["git", "ls-files"], text=True)
        return [line for line in out.splitlines() if line.strip()]

    diff_spec = f"{base}...{head}" if use_merge_base else f"{base}..{head}"
    out = subprocess.check_output(
        ["git", "diff", "--name-only", diff_spec],
        text=True,
    )
    return [line for line in out.splitlines() if line.strip()]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect minimal sound CI/CD scope from git diff.")
    parser.add_argument("--base", default="", help="Base commit SHA or ref.")
    parser.add_argument("--head", default="HEAD", help="Head commit SHA or ref.")
    parser.add_argument(
        "--merge-base",
        action="store_true",
        help="Use three-dot merge-base diff (`base...head`), ideal for pull requests.",
    )
    parser.add_argument("--force-all", action="store_true", help="Force all test/build/deploy scopes on.")
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Explicit list of changed file paths (overrides git diff, useful for tests).",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Optional path to $GITHUB_OUTPUT file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.files is not None:
        files = args.files
    else:
        files = _git_diff_files(
            args.base.strip(),
            args.head.strip(),
            use_merge_base=args.merge_base,
        )

    result = classify_changed_paths(files, force_all=args.force_all)
    gh_out = args.github_output or (
        Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    )
    if gh_out is not None:
        lines = [f"{k}={v}\n" for k, v in sorted(result.to_github_outputs().items())]
        with gh_out.open("a", encoding="utf-8") as fh:
            fh.writelines(lines)

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
