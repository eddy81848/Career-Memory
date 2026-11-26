"""T-007E Option C verification tests against REAL local PostgreSQL (synthetic provider only).

Verifies:
1. Option C minimal flow (REF-01, REF-02, REF-03):
   - Claim links to (revision_id, locator, expected_hash) only.
   - Live provider re-fetch runs OUTSIDE any DB transaction (pg_stat_activity = 0 open TXs).
   - Both Approval and Export re-read from the synthetic provider; prior Approval or prior
     successful Export NEVER skips re-verification on subsequent Export.
2. Failure classification & zero-publication backstop (REF-03, REF-04):
   - Hash change & locator change -> EVIDENCE_MISMATCH (HASH_MISMATCH / LOCATOR_NOT_FOUND).
   - 403 & policy/auth block -> POLICY_OR_AUTH_BLOCKED.
   - 404, 429, timeout, incomplete payload -> REFETCH_UNAVAILABLE; single 404 records
     UNVERIFIED_404 (NOT permanent deletion) and recovers if upstream returns 200 later.
   - Multi-evidence claim where 1 of 2 required refs fails -> 0 new approvals, 0 exports.
   - Failed verification NEVER marks the claim false and NEVER auto-updates expected_hash.
3. TOCTOU race between live re-fetch and final save (REF-04):
   - Source auth revocation, policy block, or claim edit occurring right after provider
     re-fetch succeeds is caught by final locked state/version re-check (0 stale approvals,
     0 stale exports).
4. Strict raw-source non-persistence (REF-07):
   - Runtime-generated random markers in synthetic source slices have 0 hits across all DB
     tables, job/outbox payloads, logs, provider/module memory caches, and test files;
     evidence records ONLY integer hit counts (never marker strings).
"""

import logging
import secrets
from pathlib import Path

import pytest

from t007a.db import connect, open_app_transactions
from t007a.option_c import (
    SyntheticSourceProvider,
    create_claim_with_refs,
    edit_claim_text,
    enqueue_export_job,
    option_c_counts,
    register_source_binding,
    scan_db_for_marker_hits,
    sha256_hex,
    update_source_binding,
    verify_and_approve_claim,
    verify_and_export_claim,
)
from t007a.worker import Worker

SPIKE_DIR = Path(__file__).resolve().parents[1]
REPO = "github:synthetic-org/option-c-repo"
REV_1 = "commit:deadbeef1111"
LOC_A = "src/engine.py#L10-L25"
LOC_B = "docs/adr-001.md#L1-L12"


def _make_runtime_marker(tag: str) -> str:
    """Generate a high-entropy runtime marker that NEVER appears in source code or evidence."""
    return f"T007E_RAW_CANARY_{tag}_{secrets.token_hex(16)}"


# =======================================================================================
# 1. Minimal Option C flow: out-of-TX re-fetch on Approval AND mandatory re-fetch on Export
# =======================================================================================
def test_option_c_happy_path_refetches_outside_tx_on_approval_and_every_export(db, evidence):
    src_a = f"def batch_worker():\n    return '{_make_runtime_marker('happyA')}'\n"
    src_b = f"# ADR-001\ndecision = '{_make_runtime_marker('happyB')}'\n"

    tx_during_fetch: list[int] = []

    class ObservingProvider(SyntheticSourceProvider):
        def fetch(self, repo_ref: str, revision_id: str, locator: str):
            tx_during_fetch.append(len(open_app_transactions(db)))
            return super().fetch(repo_ref, revision_id, locator)

    provider = ObservingProvider()
    provider.put_revision(REPO, REV_1, {LOC_A: src_a, LOC_B: src_b})

    source_id = register_source_binding(db, REPO)
    claim_id = create_claim_with_refs(
        db,
        claim_text="Implemented bounded batch worker per ADR-001",
        refs=[
            {"source_id": source_id, "revision_id": REV_1, "locator": LOC_A, "expected_hash": sha256_hex(src_a)},
            {"source_id": source_id, "revision_id": REV_1, "locator": LOC_B, "expected_hash": sha256_hex(src_b)},
        ],
    )

    # Approval re-fetches both refs outside any DB transaction.
    app_out = verify_and_approve_claim(db, claim_id, provider)
    assert app_out.passed is True and app_out.category == "PASSED"
    assert len(provider.fetch_calls) == 2
    assert tx_during_fetch == [0, 0]

    # First Export MUST re-fetch both refs again (total 4 provider fetches, all outside TX).
    exp_out_1 = verify_and_export_claim(db, claim_id, provider)
    assert exp_out_1.passed is True and exp_out_1.category == "PASSED"
    assert len(provider.fetch_calls) == 4
    assert tx_during_fetch == [0, 0, 0, 0]

    # Mutate upstream content AFTER approval + first export: second Export MUST NOT skip
    # re-verification and MUST block publication (exports_count stays 1, not 2).
    provider.put_revision(REPO, REV_1, {LOC_A: src_a + "# silently altered\n", LOC_B: src_b})
    exp_out_2 = verify_and_export_claim(db, claim_id, provider)
    assert exp_out_2.passed is False
    assert exp_out_2.category == "EVIDENCE_MISMATCH"
    assert exp_out_2.reason_code == "HASH_MISMATCH"
    assert len(provider.fetch_calls) == 5

    counts = option_c_counts(db, claim_id)
    evidence(
        "summary",
        {
            "provider_fetches": len(provider.fetch_calls),
            "open_tx_during_each_fetch": tx_during_fetch,
            "approvals_count": counts["approvals_count"],
            "exports_count": counts["exports_count"],
            "second_export_blocked_reason": f"{exp_out_2.category}:{exp_out_2.reason_code}",
        },
    )
    assert counts["approvals_count"] == 1
    assert counts["exports_count"] == 1


# =======================================================================================
# 2. Failure classification, single-404 non-deletion, no claim falsification / auto-update
# =======================================================================================
@pytest.mark.parametrize(
    ("scenario", "expected_category", "expected_reason", "expected_reachability"),
    [
        ("hash_changed", "EVIDENCE_MISMATCH", "HASH_MISMATCH", "OK"),
        ("locator_missing", "EVIDENCE_MISMATCH", "LOCATOR_NOT_FOUND", "OK"),
        ("http_403", "POLICY_OR_AUTH_BLOCKED", "PROVIDER_403_FORBIDDEN", "OK"),
        ("policy_blocked", "POLICY_OR_AUTH_BLOCKED", "SOURCE_POLICY_BLOCKED", "OK"),
        ("http_404", "REFETCH_UNAVAILABLE", "PROVIDER_404_UNCONFIRMED", "UNVERIFIED_404"),
        ("http_429", "REFETCH_UNAVAILABLE", "PROVIDER_429_RATE_LIMITED", "UNAVAILABLE"),
        ("timeout", "REFETCH_UNAVAILABLE", "PROVIDER_TIMEOUT", "UNAVAILABLE"),
        ("incomplete_payload", "REFETCH_UNAVAILABLE", "INCOMPLETE_PROVIDER_PAYLOAD", "UNAVAILABLE"),
    ],
)
def test_failure_taxonomy_blocks_approval_and_export_without_falsifying_or_rewriting(
    db,
    evidence,
    scenario,
    expected_category,
    expected_reason,
    expected_reachability,
):
    raw_ok = f"const x = '{_make_runtime_marker(scenario)}';"
    original_hash = sha256_hex(raw_ok)
    original_claim_text = "Reduced p95 latency via bounded queue"

    provider = SyntheticSourceProvider()
    provider.put_revision(REPO, REV_1, {LOC_A: raw_ok, LOC_B: "ok-secondary"})
    source_id = register_source_binding(db, REPO)

    # Create 2 required refs: LOC_B always succeeds, LOC_A triggers the scenario.
    # Proves partial verification (1 of 2 valid) still yields 0 approvals and 0 exports.
    claim_id = create_claim_with_refs(
        db,
        claim_text=original_claim_text,
        refs=[
            {"source_id": source_id, "revision_id": REV_1, "locator": LOC_B, "expected_hash": sha256_hex("ok-secondary")},
            {"source_id": source_id, "revision_id": REV_1, "locator": LOC_A, "expected_hash": original_hash},
        ],
    )

    def apply_fault():
        if scenario == "hash_changed":
            provider.put_revision(REPO, REV_1, {LOC_A: raw_ok + "//modified", LOC_B: "ok-secondary"})
        elif scenario == "locator_missing":
            provider.put_revision(REPO, REV_1, {"other/path.py#L1": raw_ok, LOC_B: "ok-secondary"})
        elif scenario == "http_403":
            provider.put_status(REPO, REV_1, 403)
        elif scenario == "policy_blocked":
            update_source_binding(db, source_id, policy_status="BLOCKED_POLICY")
        elif scenario == "http_404":
            provider.put_status(REPO, REV_1, 404)
        elif scenario == "http_429":
            provider.put_status(REPO, REV_1, 429)
        elif scenario == "timeout":
            provider.put_timeout(REPO, REV_1)
        elif scenario == "incomplete_payload":
            provider.put_revision(REPO, REV_1, {LOC_A: raw_ok, LOC_B: "ok-secondary"}, complete=False)

    # Gate 1: Approval under fault -> 0 approvals, 0 exports.
    apply_fault()
    app_blocked = verify_and_approve_claim(db, claim_id, provider)
    assert app_blocked.passed is False
    assert app_blocked.category == expected_category
    assert app_blocked.reason_code == expected_reason

    after_app_block = option_c_counts(db, claim_id)
    assert after_app_block["approvals_count"] == 0
    assert after_app_block["exports_count"] == 0
    # Claim is NOT marked false/deleted and expected_hash/locator are NOT auto-updated.
    assert after_app_block["claims"][0]["status"] == "DRAFT"
    assert {r["locator"]: r["expected_hash"] for r in after_app_block["refs"]}[LOC_A] == original_hash
    assert after_app_block["sources"][0]["reachability"] == expected_reachability

    # Restore valid upstream + policy so we can approve once, then verify Export ALSO blocks
    # with the exact same taxonomy when the fault recurs after approval.
    update_source_binding(db, source_id, policy_status="ALLOWED")
    provider.put_revision(REPO, REV_1, {LOC_A: raw_ok, LOC_B: "ok-secondary"})
    app_ok = verify_and_approve_claim(db, claim_id, provider)
    assert app_ok.passed is True

    apply_fault()
    exp_blocked = verify_and_export_claim(db, claim_id, provider)
    assert exp_blocked.passed is False
    assert exp_blocked.category == expected_category
    assert exp_blocked.reason_code == expected_reason

    final_counts = option_c_counts(db, claim_id)
    assert final_counts["approvals_count"] == 1  # NO new approval created on export
    assert final_counts["exports_count"] == 0  # 0 exports created or published
    assert {r["locator"]: r["expected_hash"] for r in final_counts["refs"]}[LOC_A] == original_hash

    # Specifically for 404: verify single 404 did NOT permanently tombstone the source —
    # when upstream returns 200 again, Export succeeds without re-binding.
    recovered_after_404 = False
    if scenario == "http_404":
        assert final_counts["sources"][0]["reachability"] == "UNVERIFIED_404"
        provider.put_revision(REPO, REV_1, {LOC_A: raw_ok, LOC_B: "ok-secondary"})
        exp_recovered = verify_and_export_claim(db, claim_id, provider)
        assert exp_recovered.passed is True
        recovered_after_404 = True

    evidence(
        "result",
        {
            "scenario": scenario,
            "approval_blocked": f"{app_blocked.category}:{app_blocked.reason_code}",
            "export_blocked": f"{exp_blocked.category}:{exp_blocked.reason_code}",
            "reachability_after_fault": expected_reachability,
            "exports_while_faulted": 0,
            "expected_hash_preserved": True,
            "recovered_after_single_404": recovered_after_404,
        },
    )


# =======================================================================================
# 3. TOCTOU race between live re-fetch and final save
# =======================================================================================
@pytest.mark.parametrize(
    ("gate", "mutation", "expected_category", "expected_reason"),
    [
        ("APPROVAL", "revoke_auth", "POLICY_OR_AUTH_BLOCKED", "SOURCE_AUTH_REVOKED_BEFORE_SAVE"),
        ("APPROVAL", "edit_claim", "STALE_STATE_CONFLICT", "CLAIM_MODIFIED_DURING_VERIFY"),
        ("EXPORT", "revoke_auth", "POLICY_OR_AUTH_BLOCKED", "SOURCE_AUTH_REVOKED_BEFORE_SAVE"),
        ("EXPORT", "block_policy", "POLICY_OR_AUTH_BLOCKED", "SOURCE_POLICY_BLOCKED_BEFORE_SAVE"),
        ("EXPORT", "edit_claim", "STALE_STATE_CONFLICT", "CLAIM_MODIFIED_DURING_VERIFY"),
    ],
)
def test_race_after_successful_refetch_before_save_blocks_stale_commit(
    db, evidence, gate, mutation, expected_category, expected_reason
):
    """Source text matches during out-of-TX re-fetch, then BEFORE final DB commit another
    actor revokes source auth, blocks policy, or edits the claim. Final locked re-check
    must block saving the stale approval or publishing the stale export."""
    raw = f"fn verify() {{ '{_make_runtime_marker(f'{gate}_{mutation}')}' }}"
    provider = SyntheticSourceProvider()
    provider.put_revision(REPO, REV_1, {LOC_A: raw})

    source_id = register_source_binding(db, REPO)
    claim_id = create_claim_with_refs(
        db,
        claim_text="Initial claim statement v1",
        refs=[{"source_id": source_id, "revision_id": REV_1, "locator": LOC_A, "expected_hash": sha256_hex(raw)}],
    )

    if gate == "EXPORT":
        assert verify_and_approve_claim(db, claim_id, provider).passed is True

    approvals_before = option_c_counts(db, claim_id)["approvals_count"]

    def concurrent_interleaving():
        if mutation == "revoke_auth":
            update_source_binding(db, source_id, auth_status="REVOKED")
        elif mutation == "block_policy":
            update_source_binding(db, source_id, policy_status="BLOCKED_POLICY")
        elif mutation == "edit_claim":
            edit_claim_text(db, claim_id, "Edited claim statement v2 during in-flight check")

    if gate == "APPROVAL":
        outcome = verify_and_approve_claim(
            db, claim_id, provider, after_refetch_hook=concurrent_interleaving
        )
    else:
        outcome = verify_and_export_claim(
            db, claim_id, provider, after_refetch_hook=concurrent_interleaving
        )

    counts = option_c_counts(db, claim_id)
    evidence(
        "race_outcome",
        {
            "gate": gate,
            "mutation": mutation,
            "passed": outcome.passed,
            "category": outcome.category,
            "reason_code": outcome.reason_code,
            "new_approvals": counts["approvals_count"] - approvals_before,
            "exports_count": counts["exports_count"],
        },
    )

    assert outcome.passed is False
    assert outcome.category == expected_category
    assert outcome.reason_code == expected_reason
    assert counts["approvals_count"] - approvals_before == 0
    assert counts["exports_count"] == 0


# =======================================================================================
# 4. End-to-end Worker Export + strict raw-source non-persistence scan (0 hits everywhere)
# =======================================================================================
def test_raw_source_markers_never_persist_in_db_jobs_logs_caches_or_files(
    db, evidence, caplog, tmp_path
):
    """Generates high-entropy runtime markers inside synthetic source text, exercises both
    passing and failing Approval + Worker-queued Export flows, and verifies 0 marker hits across:
      (a) every row of every PostgreSQL table (including jobs.payload, outbox, results,
          claim_approvals, claim_exports, verification_events),
      (b) captured Python log records,
      (c) provider/module in-memory audit structures after provider fixture teardown, and
      (d) test-created export artifact files.
    Evidence records ONLY integer hit counts, never the marker strings."""
    marker_pass = _make_runtime_marker("nonpersist_pass")
    marker_fail = _make_runtime_marker("nonpersist_fail")
    markers = [marker_pass, marker_fail]

    src_pass = f"class ExportedFeature:\n    secret_logic = '{marker_pass}'\n"
    src_fail = f"class AlteredFeature:\n    secret_logic = '{marker_fail}'\n"

    provider = SyntheticSourceProvider()
    provider.put_revision(REPO, REV_1, {LOC_A: src_pass})
    source_id = register_source_binding(db, REPO)
    claim_id = create_claim_with_refs(
        db,
        claim_text="Delivered ExportedFeature without storing source code",
        refs=[
            {
                "source_id": source_id,
                "revision_id": REV_1,
                "locator": LOC_A,
                "expected_hash": sha256_hex(src_pass),
            }
        ],
    )

    export_file = tmp_path / "published_export_manifest.json"

    def export_worker_processor(payload: dict) -> dict:
        assert payload["kind"] == "claim_export"
        out = verify_and_export_claim(db, claim_id, provider)
        return {"passed": out.passed, "reason_code": out.reason_code, "export_id": str(out.record_id)}

    with caplog.at_level(logging.DEBUG):
        # 1. Passing Approval
        app_res = verify_and_approve_claim(db, claim_id, provider)
        assert app_res.passed is True

        # 2. Queued Export via Worker (jobs + outbox + results)
        job_ok = enqueue_export_job(db, claim_id, idempotency_key="t007e-exp-job-ok")
        w = Worker(db, "w-option-c", process=export_worker_processor)
        w_out_1 = w.run_one(job_ok)
        assert w_out_1 is not None and w_out_1["outcome"] == "SUCCEEDED"

        # Write exported manifest from DB to a test file to scan file output as well.
        with connect(db, "t007a-observer") as conn:
            manifest_txt = conn.execute(
                "SELECT manifest::text AS m FROM claim_exports WHERE claim_id = %s",
                (claim_id,),
            ).fetchone()["m"]
        export_file.write_text(manifest_txt, encoding="utf-8")

        # 3. Failing Export when upstream mutates to marker_fail
        provider.put_revision(REPO, REV_1, {LOC_A: src_fail})
        job_fail = enqueue_export_job(db, claim_id, idempotency_key="t007e-exp-job-blocked")
        w_out_2 = w.run_one(job_fail)
        assert w_out_2 is not None and w_out_2["outcome"] == "SUCCEEDED"

    # Scan (a): All PostgreSQL tables (jobs, outbox, results, approvals, exports, events, ...)
    db_hits = scan_db_for_marker_hits(db, markers)

    # Scan (b): All captured log lines
    log_blob = "\n".join(caplog.messages) + "\n" + caplog.text
    log_hits = sum(log_blob.count(m) for m in markers)

    # Scan (c): Provider call-log / cache structures (excluding _store upstream mock fixture itself)
    provider_audit_blob = repr(provider.fetch_calls)
    cache_hits = sum(provider_audit_blob.count(m) for m in markers)

    # Scan (d): Test-generated files in tmp_path and spikes/t007a-worker/evidence
    file_hits = sum(export_file.read_text(encoding="utf-8").count(m) for m in markers)
    evidence_dir = SPIKE_DIR / "evidence"
    if evidence_dir.exists():
        for ef in evidence_dir.glob("*.json"):
            txt = ef.read_text(encoding="utf-8")
            file_hits += sum(txt.count(m) for m in markers)

    hit_summary = {
        "db_marker_hits": db_hits,
        "log_marker_hits": log_hits,
        "cache_audit_marker_hits": cache_hits,
        "file_marker_hits": file_hits,
        "exports_published": option_c_counts(db, claim_id)["exports_count"],
    }
    evidence("non_persistence_scan", hit_summary)

    assert db_hits == 0
    assert log_hits == 0
    assert cache_hits == 0
    assert file_hits == 0
    assert hit_summary["exports_published"] == 1
