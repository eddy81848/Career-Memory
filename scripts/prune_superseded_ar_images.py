#!/usr/bin/env python3
"""Prune superseded Artifact Registry Docker digests beyond active + 1 rollback per service."""

from __future__ import annotations

import argparse
import json
import os
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--region", default="us-central1")
    args = parser.parse_args()

    env = dict(os.environ)
    existing = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "").strip()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
        f"{existing} datacloud.antigravity" if existing else "datacloud.antigravity"
    )
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"

    keep: set[str] = set()
    for svc in ("cm-api", "cm-worker", "cm-web"):
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
                    "--limit=2",
                    "--format=json",
                ],
                env=env,
                text=True,
            )
        )
        for rev in revs:
            containers = (rev.get("spec") or {}).get("containers") or []
            if containers and "@sha256:" in containers[0].get("image", ""):
                keep.add("sha256:" + containers[0]["image"].split("@sha256:", 1)[1])

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
