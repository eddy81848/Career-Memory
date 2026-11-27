"""T-007E Option C prototype: live source re-verification at Approval & Export (no raw persistence).

Validates the Option C runtime rules in isolation (NOT product API, NOT B-004 completion):
1. Synthetic claims link to ``(revision_id, locator, expected_hash)`` — never raw source text.
   Both Approval and Export re-fetch from a synthetic in-memory provider *outside* any DB
   transaction, recompute the SHA-256 digest in transient memory, and discard the raw slice.
   Past approval or past successful re-fetch NEVER bypasses live re-verification on Export.
2. Failure taxonomy (never falsifies the claim and never auto-rewrites expected_hash/locator):
   - ``EVIDENCE_MISMATCH``: hash mismatch or locator missing/out-of-bounds at ``revision_id``.
   - ``POLICY_OR_AUTH_BLOCKED``: source binding revoked/policy-blocked or provider ``403``.
   - ``REFETCH_UNAVAILABLE``: provider ``404`` (marks ``UNVERIFIED_404``, NEVER permanently
     deleted from a single 404), ``429``, timeout, or incomplete/truncated payload.
   If *any* required evidence ref fails verification, new ``claim_approvals`` rows and
   ``claim_exports`` creation/publication are both ``0``.
3. TOCTOU race guard between out-of-TX re-fetch and final save:
   Re-checks ``claim_version``, ``approved_version`` (for Export), and every required
   source's ``(auth_status, policy_status, binding_version)`` inside the final short commit
   transaction with ``FOR UPDATE``. Blocks stale verification results when internal claim
   edits or source auth/policy revocations raced the re-fetch.
   NOTE: This internal CAS closes internal state races; it cannot physically prevent the
   external provider (e.g. GitHub) from mutating or deleting content millisecond-after-fetch.
4. Non-persistence of raw source content:
   Raw source slices never enter DB tables, ``jobs.payload`` / ``outbox``, structured logs,
   module caches, or evidence files.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Callable
from uuid import UUID, uuid4

from .db import connect

log = logging.getLogger("t007a.option_c")
APP_NAME = "t007a-worker"


def sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProviderResponse:
    status_code: int  # 200, 403, 404, 429
    revision_id: str | None = None
    locators: dict[str, str] | None = None  # locator -> raw text (in-memory synthetic fixture ONLY)
    complete: bool = True  # False simulates truncated / malformed upstream payload
    visibility: str = "PUBLIC"  # "PUBLIC" or "PRIVATE"


class ProviderTimeoutError(TimeoutError):
    """Synthetic upstream read/connect timeout during live re-fetch."""


class SyntheticSourceProvider:
    """Stateless-cache-free synthetic upstream source provider (NO caching of returned slices)."""

    def __init__(self):
        self._store: dict[tuple[str, str], ProviderResponse | BaseException] = {}
        self.fetch_calls: list[dict] = []

    def put_revision(
        self,
        repo_ref: str,
        revision_id: str,
        locators: dict[str, str],
        *,
        visibility: str = "PUBLIC",
        complete: bool = True,
    ) -> None:
        self._store[(repo_ref, revision_id)] = ProviderResponse(
            status_code=200,
            revision_id=revision_id,
            locators=dict(locators),
            complete=complete,
            visibility=visibility,
        )

    def put_status(self, repo_ref: str, revision_id: str, status_code: int) -> None:
        self._store[(repo_ref, revision_id)] = ProviderResponse(status_code=status_code)

    def put_timeout(self, repo_ref: str, revision_id: str) -> None:
        self._store[(repo_ref, revision_id)] = ProviderTimeoutError("upstream_timeout")

    def fetch(self, repo_ref: str, revision_id: str, locator: str) -> tuple[int, dict]:
        """Return ``(status_code, meta_plus_transient_slice)``.

        Records ONLY metadata (gate-safe ``repo_ref``, ``revision_id``, ``locator``) in
        ``fetch_calls`` — NEVER the raw text.
        """
        self.fetch_calls.append(
            {"repo_ref": repo_ref, "revision_id": revision_id, "locator": locator}
        )
        entry = self._store.get((repo_ref, revision_id))
        if entry is None:
            return 404, {"complete": True, "visibility": "PUBLIC", "locator_found": False, "text": None}
        if isinstance(entry, BaseException):
            raise entry
        if entry.status_code != 200:
            return entry.status_code, {
                "complete": entry.complete,
                "visibility": entry.visibility,
                "locator_found": False,
                "text": None,
            }
        if not entry.complete or entry.locators is None:
            return 200, {
                "complete": False,
                "visibility": entry.visibility,
                "locator_found": False,
                "text": None,
            }
        if locator not in entry.locators:
            return 200, {
                "complete": True,
                "visibility": entry.visibility,
                "locator_found": False,
                "text": None,
            }
        return 200, {
            "complete": True,
            "visibility": entry.visibility,
            "locator_found": True,
            "text": entry.locators[locator],
        }


@dataclass(frozen=True)
class GateOutcome:
    passed: bool
    gate: str  # "APPROVAL" or "EXPORT"
    category: str  # PASSED | EVIDENCE_MISMATCH | POLICY_OR_AUTH_BLOCKED | REFETCH_UNAVAILABLE | STALE_STATE_CONFLICT
    reason_code: str
    record_id: UUID | None = None  # approval_id or export_id when passed


def register_source_binding(
    dsn: str,
    repo_ref: str,
    *,
    auth_status: str = "ACTIVE",
    policy_status: str = "ALLOWED",
) -> UUID:
    source_id = uuid4()
    with connect(dsn, "t007a-api") as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO source_bindings (source_id, repo_ref, auth_status, policy_status)
            VALUES (%s, %s, %s, %s)
            """,
            (source_id, repo_ref, auth_status, policy_status),
        )
    return source_id


def update_source_binding(
    dsn: str,
    source_id: UUID,
    *,
    auth_status: str | None = None,
    policy_status: str | None = None,
) -> int:
    """Mutate source auth/policy state and bump binding_version (models revocation / policy block)."""
    with connect(dsn, "t007a-api") as conn, conn.transaction():
        row = conn.execute(
            """
            UPDATE source_bindings
               SET auth_status = COALESCE(%s, auth_status),
                   policy_status = COALESCE(%s, policy_status),
                   binding_version = binding_version + 1,
                   updated_at = now()
             WHERE source_id = %s
         RETURNING binding_version
            """,
            (auth_status, policy_status, source_id),
        ).fetchone()
    assert row is not None
    return row["binding_version"]


def create_claim_with_refs(
    dsn: str,
    *,
    claim_text: str,
    refs: list[dict],
) -> UUID:
    """Create a DRAFT claim linked ONLY to (source_id, revision_id, locator, expected_hash)."""
    claim_id = uuid4()
    with connect(dsn, "t007a-api") as conn, conn.transaction():
        conn.execute(
            "INSERT INTO career_claims (claim_id, claim_text) VALUES (%s, %s)",
            (claim_id, claim_text),
        )
        for ref in refs:
            conn.execute(
                """
                INSERT INTO claim_evidence_refs
                    (ref_id, claim_id, source_id, revision_id, locator, expected_hash, required)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    claim_id,
                    ref["source_id"],
                    ref["revision_id"],
                    ref["locator"],
                    ref["expected_hash"],
                    ref.get("required", True),
                ),
            )
    return claim_id


def edit_claim_text(dsn: str, claim_id: UUID, new_text: str) -> int:
    """User edits claim text: bumps claim_version and returns status to DRAFT."""
    with connect(dsn, "t007a-api") as conn, conn.transaction():
        row = conn.execute(
            """
            UPDATE career_claims
               SET claim_text = %s,
                   status = 'DRAFT',
                   claim_version = claim_version + 1,
                   updated_at = now()
             WHERE claim_id = %s
         RETURNING claim_version
            """,
            (new_text, claim_id),
        ).fetchone()
    assert row is not None
    return row["claim_version"]


def enqueue_export_job(dsn: str, claim_id: UUID, *, idempotency_key: str) -> UUID:
    """Submit an async export job via jobs+outbox containing ONLY IDs (never source text)."""
    job_id = uuid4()
    payload = {"kind": "claim_export", "claim_id": str(claim_id)}
    req_hash = sha256_hex(json.dumps(payload, sort_keys=True))
    with connect(dsn, "t007a-api") as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO jobs (id, idempotency_key, request_hash, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (job_id, idempotency_key, req_hash, json.dumps(payload)),
        )
        conn.execute("INSERT INTO outbox (job_id) VALUES (%s)", (job_id,))
    return job_id


def _load_snapshot_for_verification(dsn: str, claim_id: UUID) -> tuple[dict, list[dict]]:
    """Short read (autocommit, no transaction kept open) before external provider calls."""
    with connect(dsn, APP_NAME) as conn:
        claim = conn.execute(
            """
            SELECT claim_id, claim_text, status, claim_version, approved_version
              FROM career_claims WHERE claim_id = %s
            """,
            (claim_id,),
        ).fetchone()
        if claim is None:
            raise KeyError(claim_id)
        refs = conn.execute(
            """
            SELECT r.ref_id, r.source_id, r.revision_id, r.locator, r.expected_hash, r.required,
                   s.repo_ref, s.auth_status, s.policy_status, s.binding_version
              FROM claim_evidence_refs r
              JOIN source_bindings s ON s.source_id = r.source_id
             WHERE r.claim_id = %s
             ORDER BY r.created_at, r.ref_id
            """,
            (claim_id,),
        ).fetchall()
    return claim, refs


def _record_blocked_event(
    dsn: str,
    *,
    gate: str,
    claim_id: UUID,
    claim_version: int,
    category: str,
    reason_code: str,
    observed_hash: str | None = None,
    reachability_update: tuple[UUID, str] | None = None,
) -> GateOutcome:
    """Record blocked verification event without mutating expected_hash or claim truthfulness."""
    log.info(
        "option_c gate=%s claim=%s v=%s blocked category=%s reason=%s",
        gate,
        claim_id,
        claim_version,
        category,
        reason_code,
    )
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        conn.execute(
            "UPDATE career_claims SET last_check_status = %s, updated_at = now() WHERE claim_id = %s",
            (f"{category}:{reason_code}", claim_id),
        )
        if reachability_update is not None:
            src_id, reach = reachability_update
            conn.execute(
                "UPDATE source_bindings SET reachability = %s, updated_at = now() WHERE source_id = %s",
                (reach, src_id),
            )
        conn.execute(
            """
            INSERT INTO verification_events
                (event_id, gate, claim_id, claim_version, outcome, reason_code, observed_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid4(), gate, claim_id, claim_version, category, reason_code, observed_hash),
        )
    return GateOutcome(
        passed=False,
        gate=gate,
        category=category,
        reason_code=reason_code,
        record_id=None,
    )


def _verify_live_against_provider(
    dsn: str,
    *,
    gate: str,
    claim: dict,
    refs: list[dict],
    provider: SyntheticSourceProvider,
) -> GateOutcome | list[dict]:
    """Run strictly OUTSIDE any DB transaction. Returns blocked GateOutcome or verified ref metas."""
    required_refs = [r for r in refs if r["required"]]
    if not required_refs:
        return _record_blocked_event(
            dsn,
            gate=gate,
            claim_id=claim["claim_id"],
            claim_version=claim["claim_version"],
            category="EVIDENCE_MISMATCH",
            reason_code="NO_REQUIRED_EVIDENCE",
        )

    # Pre-check local auth/policy snapshot before hitting upstream.
    for ref in required_refs:
        if ref["auth_status"] != "ACTIVE":
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="POLICY_OR_AUTH_BLOCKED",
                reason_code="SOURCE_AUTH_REVOKED",
            )
        if ref["policy_status"] != "ALLOWED":
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="POLICY_OR_AUTH_BLOCKED",
                reason_code="SOURCE_POLICY_BLOCKED",
            )

    verified_metas: list[dict] = []
    for ref in required_refs:
        try:
            code, payload = provider.fetch(ref["repo_ref"], ref["revision_id"], ref["locator"])
        except ProviderTimeoutError:
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="REFETCH_UNAVAILABLE",
                reason_code="PROVIDER_TIMEOUT",
                reachability_update=(ref["source_id"], "UNAVAILABLE"),
            )

        if code == 403 or payload.get("visibility") != "PUBLIC":
            reason = "PROVIDER_403_FORBIDDEN" if code == 403 else "NON_PUBLIC_RESOURCE_BLOCKED"
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="POLICY_OR_AUTH_BLOCKED",
                reason_code=reason,
            )
        if code == 404:
            # Single 404 MUST NOT mark permanent deletion: record UNVERIFIED_404 only.
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="REFETCH_UNAVAILABLE",
                reason_code="PROVIDER_404_UNCONFIRMED",
                reachability_update=(ref["source_id"], "UNVERIFIED_404"),
            )
        if code == 429:
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="REFETCH_UNAVAILABLE",
                reason_code="PROVIDER_429_RATE_LIMITED",
                reachability_update=(ref["source_id"], "UNAVAILABLE"),
            )
        if code != 200 or not payload.get("complete"):
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="REFETCH_UNAVAILABLE",
                reason_code="INCOMPLETE_PROVIDER_PAYLOAD",
                reachability_update=(ref["source_id"], "UNAVAILABLE"),
            )
        if not payload.get("locator_found") or payload.get("text") is None:
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="EVIDENCE_MISMATCH",
                reason_code="LOCATOR_NOT_FOUND",
            )

        # Compute digest in transient local scope, then drop reference to raw slice immediately.
        observed_digest = sha256_hex(payload["text"])
        del payload

        if observed_digest != ref["expected_hash"]:
            return _record_blocked_event(
                dsn,
                gate=gate,
                claim_id=claim["claim_id"],
                claim_version=claim["claim_version"],
                category="EVIDENCE_MISMATCH",
                reason_code="HASH_MISMATCH",
                observed_hash=observed_digest,
            )

        verified_metas.append(
            {
                "ref_id": str(ref["ref_id"]),
                "source_id": str(ref["source_id"]),
                "binding_version": ref["binding_version"],
                "revision_id": ref["revision_id"],
                "locator": ref["locator"],
                "verified_hash": observed_digest,
            }
        )

    return verified_metas


def _recheck_internal_state_locked(
    conn,
    *,
    gate: str,
    claim_snapshot: dict,
    verified_metas: list[dict],
) -> str | None:
    """Inside final commit TX (FOR UPDATE): detect TOCTOU races since _load_snapshot_for_verification."""
    cur_claim = conn.execute(
        """
        SELECT status, claim_version, approved_version
          FROM career_claims
         WHERE claim_id = %s
           FOR UPDATE
        """,
        (claim_snapshot["claim_id"],),
    ).fetchone()
    if cur_claim is None:
        return "CLAIM_MISSING"
    if cur_claim["claim_version"] != claim_snapshot["claim_version"]:
        return "CLAIM_MODIFIED_DURING_VERIFY"
    if gate == "EXPORT":
        if cur_claim["status"] != "APPROVED" or cur_claim["approved_version"] != cur_claim["claim_version"]:
            return "CLAIM_NOT_CURRENTLY_APPROVED"

    # Re-lock all required refs + source_bindings and verify nothing changed or was added.
    cur_refs = conn.execute(
        """
        SELECT r.ref_id, r.source_id, r.expected_hash, r.locator, r.revision_id,
               s.auth_status, s.policy_status, s.binding_version
          FROM claim_evidence_refs r
          JOIN source_bindings s ON s.source_id = r.source_id
         WHERE r.claim_id = %s AND r.required = true
         ORDER BY r.created_at, r.ref_id
           FOR UPDATE OF r, s
        """,
        (claim_snapshot["claim_id"],),
    ).fetchall()
    if len(cur_refs) != len(verified_metas):
        return "EVIDENCE_SET_CHANGED_DURING_VERIFY"

    by_ref = {str(r["ref_id"]): r for r in cur_refs}
    for m in verified_metas:
        live = by_ref.get(m["ref_id"])
        if live is None:
            return "EVIDENCE_SET_CHANGED_DURING_VERIFY"
        if live["auth_status"] != "ACTIVE":
            return "SOURCE_AUTH_REVOKED_BEFORE_SAVE"
        if live["policy_status"] != "ALLOWED":
            return "SOURCE_POLICY_BLOCKED_BEFORE_SAVE"
        if live["binding_version"] != m["binding_version"]:
            return "SOURCE_BINDING_CHANGED_BEFORE_SAVE"
    return None


def verify_and_approve_claim(
    dsn: str,
    claim_id: UUID,
    provider: SyntheticSourceProvider,
    *,
    after_refetch_hook: Callable[[], None] | None = None,
    enforce_final_recheck: bool = True,
) -> GateOutcome:
    """Option C Approval gate: read snapshot -> live provider re-fetch outside TX -> locked CAS commit."""
    claim, refs = _load_snapshot_for_verification(dsn, claim_id)
    verified = _verify_live_against_provider(
        dsn, gate="APPROVAL", claim=claim, refs=refs, provider=provider
    )
    if isinstance(verified, GateOutcome):
        return verified

    # Deterministic hook for testing races between live re-fetch and final DB commit.
    if after_refetch_hook is not None:
        after_refetch_hook()

    approval_id = uuid4()
    conflict_reason: str | None = None
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        if enforce_final_recheck:
            conflict_reason = _recheck_internal_state_locked(
                conn, gate="APPROVAL", claim_snapshot=claim, verified_metas=verified
            )
        if conflict_reason is None:
            conn.execute(
                """
                INSERT INTO claim_approvals
                    (approval_id, claim_id, claim_version, verified_binding_state)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (approval_id, claim_id, claim["claim_version"], json.dumps(verified)),
            )
            conn.execute(
                """
                UPDATE career_claims
                   SET status = 'APPROVED',
                       approved_version = %s,
                       last_check_status = 'PASSED:APPROVED',
                       updated_at = now()
                 WHERE claim_id = %s
                """,
                (claim["claim_version"], claim_id),
            )
            conn.execute(
                """
                INSERT INTO verification_events
                    (event_id, gate, claim_id, claim_version, outcome, reason_code)
                VALUES (%s, 'APPROVAL', %s, %s, 'PASSED', 'ALL_EVIDENCE_VERIFIED')
                """,
                (uuid4(), claim_id, claim["claim_version"]),
            )

    if conflict_reason is not None:
        category = (
            "POLICY_OR_AUTH_BLOCKED"
            if "AUTH_REVOKED" in conflict_reason or "POLICY_BLOCKED" in conflict_reason
            else "STALE_STATE_CONFLICT"
        )
        return _record_blocked_event(
            dsn,
            gate="APPROVAL",
            claim_id=claim_id,
            claim_version=claim["claim_version"],
            category=category,
            reason_code=conflict_reason,
        )

    log.info("option_c gate=APPROVAL claim=%s v=%s passed", claim_id, claim["claim_version"])
    return GateOutcome(
        passed=True,
        gate="APPROVAL",
        category="PASSED",
        reason_code="ALL_EVIDENCE_VERIFIED",
        record_id=approval_id,
    )


def verify_and_export_claim(
    dsn: str,
    claim_id: UUID,
    provider: SyntheticSourceProvider,
    *,
    after_refetch_hook: Callable[[], None] | None = None,
    enforce_final_recheck: bool = True,
) -> GateOutcome:
    """Option C Export gate: MUST re-verify live source even if claim was previously APPROVED."""
    claim, refs = _load_snapshot_for_verification(dsn, claim_id)
    if claim["status"] != "APPROVED" or claim["approved_version"] != claim["claim_version"]:
        return _record_blocked_event(
            dsn,
            gate="EXPORT",
            claim_id=claim_id,
            claim_version=claim["claim_version"],
            category="STALE_STATE_CONFLICT",
            reason_code="CLAIM_NOT_CURRENTLY_APPROVED",
        )

    # Mandatory live re-fetch on EVERY export (never skipped due to prior approval).
    verified = _verify_live_against_provider(
        dsn, gate="EXPORT", claim=claim, refs=refs, provider=provider
    )
    if isinstance(verified, GateOutcome):
        return verified

    if after_refetch_hook is not None:
        after_refetch_hook()

    export_id = uuid4()
    manifest = {
        "claim_id": str(claim_id),
        "claim_version": claim["claim_version"],
        "claim_text": claim["claim_text"],
        "evidence": verified,  # contains ONLY revision_id, locator, verified_hash (no raw source)
    }
    conflict_reason: str | None = None
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        if enforce_final_recheck:
            conflict_reason = _recheck_internal_state_locked(
                conn, gate="EXPORT", claim_snapshot=claim, verified_metas=verified
            )
        if conflict_reason is None:
            conn.execute(
                """
                INSERT INTO claim_exports (export_id, claim_id, claim_version, manifest)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (export_id, claim_id, claim["claim_version"], json.dumps(manifest)),
            )
            conn.execute(
                """
                UPDATE career_claims
                   SET last_check_status = 'PASSED:EXPORTED', updated_at = now()
                 WHERE claim_id = %s
                """,
                (claim_id,),
            )
            conn.execute(
                """
                INSERT INTO verification_events
                    (event_id, gate, claim_id, claim_version, outcome, reason_code)
                VALUES (%s, 'EXPORT', %s, %s, 'PASSED', 'ALL_EVIDENCE_VERIFIED')
                """,
                (uuid4(), claim_id, claim["claim_version"]),
            )

    if conflict_reason is not None:
        category = (
            "POLICY_OR_AUTH_BLOCKED"
            if "AUTH_REVOKED" in conflict_reason or "POLICY_BLOCKED" in conflict_reason
            else "STALE_STATE_CONFLICT"
        )
        return _record_blocked_event(
            dsn,
            gate="EXPORT",
            claim_id=claim_id,
            claim_version=claim["claim_version"],
            category=category,
            reason_code=conflict_reason,
        )

    log.info("option_c gate=EXPORT claim=%s v=%s published", claim_id, claim["claim_version"])
    return GateOutcome(
        passed=True,
        gate="EXPORT",
        category="PASSED",
        reason_code="ALL_EVIDENCE_VERIFIED",
        record_id=export_id,
    )


def option_c_counts(dsn: str, claim_id: UUID | None = None) -> dict:
    """Observable DB counts for approvals, exports, claim status, and source reachability."""
    where = "WHERE claim_id = %s" if claim_id else ""
    params = (claim_id,) if claim_id else ()
    with connect(dsn, "t007a-observer") as conn:
        approvals = conn.execute(
            f"SELECT count(*) AS n FROM claim_approvals {where}", params
        ).fetchone()["n"]
        exports = conn.execute(
            f"SELECT count(*) AS n FROM claim_exports {where}", params
        ).fetchone()["n"]
        claims = conn.execute(
            f"SELECT status, claim_version, approved_version, last_check_status FROM career_claims {where}",
            params,
        ).fetchall()
        refs = conn.execute(
            f"SELECT revision_id, locator, expected_hash FROM claim_evidence_refs {where}",
            params,
        ).fetchall()
        sources = conn.execute(
            "SELECT auth_status, policy_status, reachability, binding_version FROM source_bindings"
        ).fetchall()
        events = conn.execute(
            f"SELECT gate, outcome, reason_code FROM verification_events {where} ORDER BY created_at",
            params,
        ).fetchall()
    return {
        "approvals_count": approvals,
        "exports_count": exports,
        "claims": claims,
        "refs": refs,
        "sources": sources,
        "events": events,
    }


def scan_db_for_marker_hits(dsn: str, runtime_markers: list[str]) -> int:
    """Serialize EVERY row in EVERY table of t007a_spike and count marker substring hits.

    Returns ONLY an integer hit count so runtime markers never enter evidence files.
    """
    tables = (
        "jobs",
        "outbox",
        "results",
        "job_attempts",
        "import_runs",
        "import_stream_checkpoints",
        "import_page_items",
        "import_ready_outbox",
        "source_bindings",
        "career_claims",
        "claim_evidence_refs",
        "claim_approvals",
        "claim_exports",
        "verification_events",
    )
    hits = 0
    with connect(dsn, "t007a-observer") as conn:
        for tbl in tables:
            rows = conn.execute(f"SELECT row_to_json(t)::text AS r FROM {tbl} t").fetchall()
            for row in rows:
                blob = row["r"]
                for m in runtime_markers:
                    hits += blob.count(m)
    return hits
