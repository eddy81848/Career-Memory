#!/usr/bin/env python3
"""Prune superseded Artifact Registry Docker digests while protecting active & rollback revisions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--protect-revision", action="append", default=[])
    parser.add_argument("--max-recent-per-service", type=int, default=2)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    existing = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "").strip()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
        f"{existing} datacloud.antigravity" if existing else "datacloud.antigravity"
    )
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"

    protected_rev_names = {r.strip() for r in args.protect_revision if r and r.strip()}
    keep: set[str] = set()

    for svc in ("cm-api", "cm-worker", "cm-web"):
        svc_doc = json.loads(
            subprocess.check_output(
                [
                    "gcloud",
                    "run",
                    "services",
                    "describe",
                    svc,
                    "--project",
                    args.project_id,
                    "--region",
                    args.region,
                    "--format=json",
                ],
                env=env,
                text=True,
            )
        )
        for tr in (svc_doc.get("status") or {}).get("traffic") or []:
            if tr.get("revisionName") and int(tr.get("percent") or 0) > 0:
                protected_rev_names.add(str(tr["revisionName"]))
        # Only protect spec.template image if no traffic split is reported yet
        if not ((svc_doc.get("status") or {}).get("traffic") or []):
            svc_containers = (((svc_doc.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
                "containers"
            ) or []
            if svc_containers and "@sha256:" in svc_containers[0].get("image", ""):
                keep.add("sha256:" + svc_containers[0]["image"].split("@sha256:", 1)[1])

        revs = json.loads(
            subprocess.check_output(
                [
                    "gcloud",
                    "run",
                    "revisions",
                    "list",
                    "--service",
                    svc,
                    "--project",
                    args.project_id,
                    "--region",
                    args.region,
                    "--limit=5",
                    "--format=json",
                ],
                env=env,
                text=True,
            )
        )
        max_recent = max(0, int(args.max_recent_per_service))
        kept_recent = 0
        for rev in revs:
            rev_name = str((rev.get("metadata") or {}).get("name") or "")
            containers = (rev.get("spec") or {}).get("containers") or []
            if not containers or "@sha256:" not in containers[0].get("image", ""):
                continue
            digest = "sha256:" + containers[0]["image"].split("@sha256:", 1)[1]
            if rev_name in protected_rev_names or kept_recent < max_recent:
                keep.add(digest)
                kept_recent += 1

    for pkg in ("cm-api", "cm-web"):
        repo_pkg = f"{args.region}-docker.pkg.dev/{args.project_id}/career-memory/{pkg}"
        imgs = json.loads(
            subprocess.check_output(
                ["gcloud", "artifacts", "docker", "images", "list", repo_pkg, "--format=json"],
                env=env,
                text=True,
            )
        )
        todo = [str(img["version"]) for img in imgs if img.get("version") not in keep]
        for _ in (1, 2):
            rem: list[str] = []
            for digest in todo:
                res = subprocess.run(
                    [
                        "gcloud",
                        "artifacts",
                        "docker",
                        "images",
                        "delete",
                        f"{repo_pkg}@{digest}",
                        "--delete-tags",
                        "--quiet",
                    ],
                    env=env,
                    capture_output=True,
                    check=False,
                )
                if res.returncode != 0:
                    rem.append(digest)
            todo = rem

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
