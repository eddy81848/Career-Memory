"""Deterministic static GitHub Actions CI trust-boundary auditor (T-507 / INF-09)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

M0_IMMUTABLE_OPENAPI_BLOB_SHA = "07594d75425744d6149250a9f7ff95ac24659f80"
M0_IMMUTABLE_OPENAPI_PATH = "contracts/import-v1/openapi.yaml"

PROHIBITED_PR_DEPLOY_SECRETS: frozenset[str] = frozenset(
    {
        "GCP_SA_KEY",
        "CLOUDFLARE_API_TOKEN",
        "SUPABASE_SERVICE_ROLE_KEY",
        "OPENAI_API_KEY",
    }
)

_SECRET_EXPR_RE = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}")
_UNTRUSTED_PR_EXPR_RE = re.compile(
    r"\$\{\{\s*github\.event\.(?:pull_request\.(?:title|body|head\.ref|head\.label)|"
    r"issue\.(?:title|body)|comment\.body)\s*\}\}"
)
_USES_RE = re.compile(r"^\s*-?\s*uses\s*:\s*['\"]?([^'\"\s#]+)['\"]?")
_IMAGE_REF_RE = re.compile(
    r"(?:--image[=\s]+|image\s*:\s*)['\"]?([a-z0-9.\-]+/[a-z0-9._/\-]+(?::[A-Za-z0-9._\-]+)?(?:@sha256:[0-9a-f]{64})?)['\"]?"
)
_DIGEST_SUFFIX_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
_SHA40_PIN_RE = re.compile(r"@[0-9a-f]{40}$")
_MUTABLE_ACTION_REF_RE = re.compile(r"@(?:main|master|HEAD|latest)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WorkflowAuditFinding:
    """Single deterministic violation discovered during CI workflow trust auditing."""

    rule_id: str
    severity: str
    message: str
    line_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "line_number": self.line_number,
        }


@dataclass(frozen=True, slots=True)
class WorkflowAuditResult:
    """Result of auditing a GitHub Actions PR conformance workflow against INF-09."""

    workflow_name: str
    passed: bool
    triggers: tuple[str, ...]
    top_level_permissions: dict[str, str]
    openapi_pin_present: bool
    secret_references: tuple[str, ...]
    findings: tuple[WorkflowAuditFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "passed": self.passed,
            "triggers": list(self.triggers),
            "top_level_permissions": dict(self.top_level_permissions),
            "openapi_pin_present": self.openapi_pin_present,
            "secret_references": list(self.secret_references),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True, slots=True)
class ReleaseWorkflowValidationResult:
    """Result of validating a future staging/prod release workflow manifest (INF-09)."""

    workflow_name: str
    passed: bool
    has_concurrency_group: bool
    cancel_in_progress: bool | None
    image_references: tuple[str, ...]
    findings: tuple[WorkflowAuditFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "passed": self.passed,
            "has_concurrency_group": self.has_concurrency_group,
            "cancel_in_progress": self.cancel_in_progress,
            "image_references": list(self.image_references),
            "findings": [f.to_dict() for f in self.findings],
        }


def _strip_inline_comment(line: str) -> str:
    """Remove trailing YAML `#` comments outside quoted strings."""
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


def _indent_level(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _extract_top_level_blocks(lines: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Split top-level YAML keys (`indent == 0`) into named sections with 1-based line numbers."""
    blocks: dict[str, list[tuple[int, str]]] = {}
    current_key: str | None = None
    for lineno, raw in enumerate(lines, start=1):
        cleaned = _strip_inline_comment(raw)
        if not cleaned.strip():
            continue
        indent = _indent_level(cleaned)
        if indent == 0 and ":" in cleaned:
            key = cleaned.split(":", 1)[0].strip().strip("'\"")
            current_key = key
            blocks.setdefault(key, []).append((lineno, cleaned))
        elif current_key is not None:
            blocks[current_key].append((lineno, cleaned))
    return blocks


def _parse_triggers(on_block: list[tuple[int, str]]) -> tuple[str, ...]:
    triggers: list[str] = []
    if not on_block:
        return ()
    _, first_line = on_block[0]
    after_colon = first_line.split(":", 1)[1].strip()
    if after_colon:
        if after_colon.startswith("[") and after_colon.endswith("]"):
            for item in after_colon[1:-1].split(","):
                cleaned = item.strip().strip("'\"")
                if cleaned:
                    triggers.append(cleaned)
        else:
            triggers.append(after_colon.strip("'\""))
        return tuple(dict.fromkeys(triggers))

    # Block mapping under `on:`: collect direct children at the smallest child indent.
    child_indents = [_indent_level(line) for _, line in on_block[1:] if _indent_level(line) > 0]
    if not child_indents:
        return ()
    min_child_indent = min(child_indents)
    for _, line in on_block[1:]:
        if _indent_level(line) == min_child_indent:
            stripped = line.strip()
            if stripped.startswith("- "):
                val = stripped[2:].strip().strip("'\"")
                if val:
                    triggers.append(val)
            elif ":" in stripped:
                key = stripped.split(":", 1)[0].strip().strip("'\"")
                if key:
                    triggers.append(key)
    return tuple(dict.fromkeys(triggers))


def _parse_permissions_block(
    perm_lines: list[tuple[int, str]],
) -> tuple[dict[str, str], str | None]:
    """Parse a `permissions:` section into ({scope: level}, shorthand_scalar)."""
    if not perm_lines:
        return {}, None
    _, first_line = perm_lines[0]
    after_colon = first_line.split(":", 1)[1].strip().strip("'\"")
    if after_colon:
        if after_colon == "{}":
            return {}, "{}"
        return {}, after_colon
    perms: dict[str, str] = {}
    base_indent = _indent_level(first_line)
    for _, line in perm_lines[1:]:
        if _indent_level(line) <= base_indent:
            break
        stripped = line.strip()
        if ":" in stripped:
            scope, level = stripped.split(":", 1)
            perms[scope.strip().strip("'\"")] = level.strip().strip("'\"")
    return perms, None


def _find_job_level_permissions(lines: list[str]) -> list[tuple[int, dict[str, str], str | None]]:
    results: list[tuple[int, dict[str, str], str | None]] = []
    idx = 0
    while idx < len(lines):
        cleaned = _strip_inline_comment(lines[idx])
        stripped = cleaned.strip()
        if _indent_level(cleaned) > 0 and stripped.startswith("permissions:"):
            base_indent = _indent_level(cleaned)
            block: list[tuple[int, str]] = [(idx + 1, cleaned)]
            j = idx + 1
            while j < len(lines):
                nxt = _strip_inline_comment(lines[j])
                if nxt.strip():
                    if _indent_level(nxt) <= base_indent:
                        break
                    block.append((j + 1, nxt))
                j += 1
            perms, scalar = _parse_permissions_block(block)
            results.append((idx + 1, perms, scalar))
            idx = j
            continue
        idx += 1
    return results


def audit_pr_workflow_text(
    content: str,
    *,
    workflow_name: str = "workflow.yml",
    require_openapi_pin: bool = True,
) -> WorkflowAuditResult:
    """Statically audit a GitHub Actions PR conformance workflow against INF-09 rules."""
    lines = content.splitlines()
    blocks = _extract_top_level_blocks(lines)
    findings: list[WorkflowAuditFinding] = []

    triggers = _parse_triggers(blocks.get("on", []))
    is_pr_workflow = bool({"pull_request", "pull_request_target"} & set(triggers))

    # 1. pull_request_target is strictly forbidden on PR workflows (untrusted checkout hazard).
    if "pull_request_target" in triggers:
        findings.append(
            WorkflowAuditFinding(
                rule_id="INF09_PULL_REQUEST_TARGET_FORBIDDEN",
                severity="CRITICAL",
                message=(
                    "pull_request_target trigger is forbidden because it exposes privileged "
                    "context to untrusted PR checkouts."
                ),
            )
        )

    # 2. Explicit top-level permissions check.
    top_perms_block = blocks.get("permissions", [])
    top_perms, top_scalar = _parse_permissions_block(top_perms_block)
    if not top_perms_block:
        findings.append(
            WorkflowAuditFinding(
                rule_id="INF09_PERMISSIONS_MISSING",
                severity="HIGH",
                message="Top-level `permissions:` block must be explicitly declared (`contents: read`).",
            )
        )
    else:
        if top_scalar is not None and top_scalar != "{}":
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_PERMISSIONS_BROAD_SHORTHAND",
                    severity="CRITICAL",
                    message=(
                        f"Broad permissions shorthand `{top_scalar}` is forbidden; "
                        "declare explicit minimal `contents: read`."
                    ),
                    line_number=top_perms_block[0][0],
                )
            )
        if top_perms.get("contents") != "read":
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_CONTENTS_READ_REQUIRED",
                    severity="HIGH",
                    message="Top-level permissions must explicitly set `contents: read`.",
                    line_number=top_perms_block[0][0],
                )
            )
        for scope, level in sorted(top_perms.items()):
            if level.lower() == "write":
                findings.append(
                    WorkflowAuditFinding(
                        rule_id="INF09_WRITE_PERMISSION_FORBIDDEN_ON_PR",
                        severity="CRITICAL",
                        message=(
                            f"Top-level permission `{scope}: write` is forbidden on PR conformance workflows."
                        ),
                        line_number=top_perms_block[0][0],
                    )
                )

    # 3. Job-level permission escalation check.
    for lineno, job_perms, job_scalar in _find_job_level_permissions(lines):
        if job_scalar is not None and job_scalar.lower() == "write-all":
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_PERMISSIONS_BROAD_SHORTHAND",
                    severity="CRITICAL",
                    message="Job-level `permissions: write-all` is forbidden.",
                    line_number=lineno,
                )
            )
        for scope, level in sorted(job_perms.items()):
            if is_pr_workflow and level.lower() == "write":
                findings.append(
                    WorkflowAuditFinding(
                        rule_id="INF09_WRITE_PERMISSION_FORBIDDEN_ON_PR",
                        severity="CRITICAL",
                        message=(
                            f"Job-level permission `{scope}: write` (including `id-token: write`) "
                            "is forbidden on pull_request workflows."
                        ),
                        line_number=lineno,
                    )
                )

    # 4. Zero cloud deploy secrets in PR conformance workflows while hosted deploy is OFF.
    discovered_secrets: list[str] = []
    for lineno, raw in enumerate(lines, start=1):
        cleaned = _strip_inline_comment(raw)
        if not cleaned.strip():
            continue
        for match in _SECRET_EXPR_RE.finditer(cleaned):
            secret_name = match.group(1)
            discovered_secrets.append(secret_name)
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_SECRET_FORBIDDEN_IN_PR_WORKFLOW",
                    severity="CRITICAL",
                    message=(
                        f"Secret reference `secrets.{secret_name}` is forbidden in local/PR "
                        "conformance workflows while hosted deploy is OFF."
                    ),
                    line_number=lineno,
                )
            )
        for prohibited_name in sorted(PROHIBITED_PR_DEPLOY_SECRETS):
            if re.search(rf"\b{re.escape(prohibited_name)}\b", cleaned):
                if prohibited_name not in discovered_secrets:
                    discovered_secrets.append(prohibited_name)
                findings.append(
                    WorkflowAuditFinding(
                        rule_id="INF09_PROHIBITED_CLOUD_SECRET_REFERENCE",
                        severity="CRITICAL",
                        message=(
                            f"Prohibited cloud/AI secret identifier `{prohibited_name}` "
                            "must not appear in PR conformance workflows."
                        ),
                        line_number=lineno,
                    )
                )
        if _UNTRUSTED_PR_EXPR_RE.search(cleaned):
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_UNTRUSTED_PR_EXPRESSION_INJECTION",
                    severity="HIGH",
                    message=(
                        "Direct interpolation of untrusted PR metadata expression detected; "
                        "pass through an env variable instead of inline shell expansion."
                    ),
                    line_number=lineno,
                )
            )

    # 5. Immutable M0 OpenAPI blob SHA check.
    non_comment_body = "\n".join(_strip_inline_comment(line) for line in lines)
    openapi_pin_present = (
        M0_IMMUTABLE_OPENAPI_BLOB_SHA in non_comment_body
        and M0_IMMUTABLE_OPENAPI_PATH in non_comment_body
    )
    if require_openapi_pin and not openapi_pin_present:
        findings.append(
            WorkflowAuditFinding(
                rule_id="INF09_IMMUTABLE_OPENAPI_PIN_MISSING",
                severity="HIGH",
                message=(
                    f"Workflow must verify immutable M0 OpenAPI blob SHA "
                    f"`{M0_IMMUTABLE_OPENAPI_BLOB_SHA}` for `{M0_IMMUTABLE_OPENAPI_PATH}`."
                ),
            )
        )

    return WorkflowAuditResult(
        workflow_name=workflow_name,
        passed=len(findings) == 0,
        triggers=triggers,
        top_level_permissions=top_perms,
        openapi_pin_present=openapi_pin_present,
        secret_references=tuple(dict.fromkeys(discovered_secrets)),
        findings=tuple(findings),
    )


def audit_workflow_file(
    workflow_path: Path,
    *,
    require_openapi_pin: bool = True,
) -> WorkflowAuditResult:
    """Read and audit a single GitHub Actions workflow YAML file."""
    content = workflow_path.read_text(encoding="utf-8")
    return audit_pr_workflow_text(
        content,
        workflow_name=workflow_path.name,
        require_openapi_pin=require_openapi_pin,
    )


def audit_workflows_directory(
    workflows_dir: Path,
    *,
    require_openapi_pin: bool = True,
) -> tuple[WorkflowAuditResult, ...]:
    """Audit every `.yml` / `.yaml` file under `.github/workflows/` deterministically."""
    if not workflows_dir.is_dir():
        raise FileNotFoundError(f"Workflows directory not found: {workflows_dir}")
    paths = sorted(
        p for p in workflows_dir.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    return tuple(
        audit_workflow_file(p, require_openapi_pin=require_openapi_pin) for p in paths
    )


def validate_release_workflow_manifest(
    content: str,
    *,
    workflow_name: str = "staging-release.yml",
) -> ReleaseWorkflowValidationResult:
    """Validate a future staging/release workflow manifest for INF-09 compliance.

    Enforces:
    - Never triggered by `pull_request` or `pull_request_target`.
    - Explicit minimal `permissions:` (never `write-all`).
    - Explicit `concurrency:` group with `cancel-in-progress: false`.
    - Immutable container image `@sha256:<64-hex>` digest pinning (no mutable tags).
    - No static long-lived `GCP_SA_KEY` secret (use OIDC WIF instead).
    - Actions pinned to immutable commit SHAs (never `@main`/`@master`/`@latest`).
    """
    lines = content.splitlines()
    blocks = _extract_top_level_blocks(lines)
    findings: list[WorkflowAuditFinding] = []

    triggers = _parse_triggers(blocks.get("on", []))
    for forbidden_trigger in ("pull_request", "pull_request_target"):
        if forbidden_trigger in triggers:
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_RELEASE_PR_TRIGGER_FORBIDDEN",
                    severity="CRITICAL",
                    message=(
                        f"Release workflow must not trigger on `{forbidden_trigger}`; "
                        "use protected `workflow_dispatch` or signed tag/branch promotion."
                    ),
                )
            )

    # Permissions check
    top_perms_block = blocks.get("permissions", [])
    top_perms, top_scalar = _parse_permissions_block(top_perms_block)
    if not top_perms_block:
        findings.append(
            WorkflowAuditFinding(
                rule_id="INF09_RELEASE_PERMISSIONS_MISSING",
                severity="HIGH",
                message="Release workflow must declare explicit minimal `permissions:`.",
            )
        )
    elif top_scalar is not None and top_scalar != "{}":
        findings.append(
            WorkflowAuditFinding(
                rule_id="INF09_RELEASE_PERMISSIONS_BROAD",
                severity="CRITICAL",
                message=f"Release workflow must not use broad permissions `{top_scalar}`.",
                line_number=top_perms_block[0][0],
            )
        )

    # Concurrency group check
    concurrency_block = blocks.get("concurrency", [])
    has_concurrency_group = False
    cancel_in_progress: bool | None = None
    if not concurrency_block:
        findings.append(
            WorkflowAuditFinding(
                rule_id="INF09_RELEASE_CONCURRENCY_MISSING",
                severity="HIGH",
                message="Release workflow must define a top-level `concurrency:` group.",
            )
        )
    else:
        for lineno, line in concurrency_block[1:]:
            stripped = line.strip()
            if stripped.startswith("group:"):
                val = stripped.split(":", 1)[1].strip()
                if val:
                    has_concurrency_group = True
            elif stripped.startswith("cancel-in-progress:"):
                val = stripped.split(":", 1)[1].strip().lower()
                if val == "false":
                    cancel_in_progress = False
                elif val == "true":
                    cancel_in_progress = True
        if not has_concurrency_group:
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_RELEASE_CONCURRENCY_GROUP_EMPTY",
                    severity="HIGH",
                    message="Release workflow `concurrency:` block must specify a non-empty `group:`.",
                    line_number=concurrency_block[0][0],
                )
            )
        if cancel_in_progress is not False:
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_RELEASE_CANCEL_IN_PROGRESS_MUST_BE_FALSE",
                    severity="HIGH",
                    message=(
                        "Release workflow must set `cancel-in-progress: false` to avoid "
                        "half-applied deployments."
                    ),
                    line_number=concurrency_block[0][0],
                )
            )

    # Scan image references, action pins, and static GCP_SA_KEY usage
    image_refs: list[str] = []
    for lineno, raw in enumerate(lines, start=1):
        cleaned = _strip_inline_comment(raw)
        if not cleaned.strip():
            continue

        if re.search(r"\bGCP_SA_KEY\b", cleaned):
            findings.append(
                WorkflowAuditFinding(
                    rule_id="INF09_STATIC_GCP_SA_KEY_FORBIDDEN",
                    severity="CRITICAL",
                    message="Static `GCP_SA_KEY` secret is forbidden; use OIDC Workload Identity Federation.",
                    line_number=lineno,
                )
            )

        for img_match in _IMAGE_REF_RE.finditer(cleaned):
            image_ref = img_match.group(1)
            image_refs.append(image_ref)
            if not _DIGEST_SUFFIX_RE.search(image_ref):
                findings.append(
                    WorkflowAuditFinding(
                        rule_id="INF09_IMAGE_DIGEST_PIN_REQUIRED",
                        severity="CRITICAL",
                        message=(
                            f"Container image `{image_ref}` must be pinned by immutable "
                            "`@sha256:<64-hex>` digest (mutable tags are forbidden)."
                        ),
                        line_number=lineno,
                    )
                )

        uses_match = _USES_RE.match(cleaned)
        if uses_match:
            action_ref = uses_match.group(1)
            if not action_ref.startswith("./"):
                if _MUTABLE_ACTION_REF_RE.search(action_ref) or not _SHA40_PIN_RE.search(
                    action_ref
                ):
                    findings.append(
                        WorkflowAuditFinding(
                            rule_id="INF09_ACTION_COMMIT_SHA_PIN_REQUIRED",
                            severity="HIGH",
                            message=(
                                f"Release workflow action `{action_ref}` must be pinned to a "
                                "40-character commit SHA."
                            ),
                            line_number=lineno,
                        )
                    )

    if not image_refs:
        findings.append(
            WorkflowAuditFinding(
                rule_id="INF09_IMAGE_REFERENCE_MISSING",
                severity="HIGH",
                message="Release workflow manifest must declare at least one `@sha256:` pinned image.",
            )
        )

    return ReleaseWorkflowValidationResult(
        workflow_name=workflow_name,
        passed=len(findings) == 0,
        has_concurrency_group=has_concurrency_group,
        cancel_in_progress=cancel_in_progress,
        image_references=tuple(image_refs),
        findings=tuple(findings),
    )
