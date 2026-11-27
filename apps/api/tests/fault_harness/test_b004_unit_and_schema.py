"""Unit and schema contract tests for B-004 Fault Harness (T-005A & T-005C read-only guarantees)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from career_memory_api.fault_harness import (
    DEFAULT_OBSERVED_AT_ISO,
    EXPECTED_SNAPSHOT_SCHEMA_VERSION,
    SCENARIO_SCHEMA_VERSION,
    SCENARIO_SPECS,
    FaultInjectionPoint,
    ScenarioSpec,
    ScenarioValidationError,
    build_10page_canonical_provider,
    compare_logical_snapshots,
    load_expected_snapshot,
    validate_expected_snapshot,
    validate_scenario_spec,
)
from career_memory_api.github_provider.transport import WireHttpRequest

REPO_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_SNAPSHOTS_DIR = REPO_ROOT / "contracts" / "fault-harness" / "expected_snapshots"
EVIDENCE_BUNDLE_PATH = REPO_ROOT / "contracts" / "fault-harness" / "b004_evidence_bundle.v1.json"
CLI_SCRIPT_PATH = REPO_ROOT / "scripts" / "run_b004_fault_harness.py"


def test_all_16_scenario_specs_registered_and_valid() -> None:
    assert len(SCENARIO_SPECS) == 16
    ids = [s.scenario_id for s in SCENARIO_SPECS]
    assert ids == [f"S{i:02d}" for i in range(1, 17)]
    branches = {s.default_branch for s in SCENARIO_SPECS}
    # Never hardcode 'master' only — all 3 branch styles exercised
    assert {"trunk", "main", "master"}.issubset(branches)
    for spec in SCENARIO_SPECS:
        validate_scenario_spec(spec)


def test_scenario_validator_rejects_secrets_and_invalid_versions() -> None:
    bad_ver = ScenarioSpec(
        scenario_id="S01",
        title="Bad version",
        requirements=("SYNC-01",),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.NONE,),
        description="Test invalid schema version",
        schema_version="wrong-version",
    )
    with pytest.raises(ScenarioValidationError, match="schema_version"):
        validate_scenario_spec(bad_ver)

    bad_secret = ScenarioSpec(
        scenario_id="S01",
        title="Leaked secret",
        requirements=("SYNC-01",),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.NONE,),
        description="Accidentally contains ghp_1234567890abcdefghij1234567890 token",
        schema_version=SCENARIO_SCHEMA_VERSION,
    )
    with pytest.raises(ScenarioValidationError, match="forbidden real-secret"):
        validate_scenario_spec(bad_secret)

    bad_time = ScenarioSpec(
        scenario_id="S01",
        title="Non-UTC time",
        requirements=("SYNC-01",),
        default_branch="main",
        observed_at_iso="2026-11-27T12:00:00+09:00",
        injection_points=(FaultInjectionPoint.NONE,),
        description="Must end in Z",
    )
    with pytest.raises(ScenarioValidationError, match="UTC ISO-8601"):
        validate_scenario_spec(bad_time)


def test_deterministic_fake_provider_produces_identical_wire_bytes_and_plants_canaries() -> None:
    p1 = build_10page_canonical_provider(scenario_id="S01", canary_id="S01", default_branch="trunk")
    p2 = build_10page_canonical_provider(scenario_id="S01", canary_id="S01", default_branch="trunk")

    req = WireHttpRequest(
        method="GET",
        url=f"https://api.github.com/repositories/{p1.repo_id}/pulls?state=closed&base=trunk&page=10",
        headers={"Accept": "application/vnd.github+json"},
        timeout_seconds=5.0,
        max_bytes=1024 * 1024,
    )
    r1 = p1.handle_wire_request(req)
    r2 = p2.handle_wire_request(req)

    assert r1.status_code == 200
    assert r1.body == r2.body
    assert r1.headers == r2.headers
    assert p1.canaries.raw_pr_body.encode("utf-8") in r1.body


def test_expected_snapshots_validate_cleanly_and_contain_zero_canaries() -> None:
    for idx in range(1, 17):
        sid = f"S{idx:02d}"
        snap_path = EXPECTED_SNAPSHOTS_DIR / f"{sid}.json"
        loaded = load_expected_snapshot(snap_path)
        assert loaded["schema_version"] == EXPECTED_SNAPSHOT_SCHEMA_VERSION
        assert loaded["scenario_id"] == sid
        assert "CANARY_B004_" not in snap_path.read_text(encoding="utf-8")

    # Reject any expected snapshot containing a canary marker
    poisoned = load_expected_snapshot(EXPECTED_SNAPSHOTS_DIR / "S01.json")
    poisoned["title"] = "CANARY_B004_RAW_PR_BODY_S01_9F42A1"
    with pytest.raises(ScenarioValidationError, match="canary"):
        validate_expected_snapshot(poisoned)


def test_comparator_reports_human_readable_diffs_and_cli_has_no_update_snapshots_flag() -> None:
    expected = {"sync_runs": [{"status": "SUCCESS"}], "outbox_summary": {"import_ready_count": 1}}
    actual = {"sync_runs": [{"status": "PARTIAL"}], "outbox_summary": {"import_ready_count": 0}}
    cmp_res = compare_logical_snapshots(
        scenario_id="S02",
        injection_points=(FaultInjectionPoint.BEFORE_PAGE_TX,),
        expected=expected,
        actual=actual,
    )
    assert cmp_res.matched is False
    assert len(cmp_res.diffs) == 2
    rendered = cmp_res.format_report()
    assert "S02" in rendered
    assert "BEFORE_PAGE_TX" in rendered
    assert "$.sync_runs[0].status" in rendered
    assert "$.outbox_summary.import_ready_count" in rendered

    # T-005C prohibition: no automatic expected-snapshot overwrite flag anywhere in CLI or comparator!
    cli_source = CLI_SCRIPT_PATH.read_text(encoding="utf-8")
    assert "add_argument(\n        \"--update-snapshots\"" not in cli_source
    assert "--write-snapshots" not in cli_source
    bundle_data = json.loads(EVIDENCE_BUNDLE_PATH.read_text(encoding="utf-8"))
    assert bundle_data["snapshot_auto_update_supported"] is False
    assert "CANARY_B004_" not in EVIDENCE_BUNDLE_PATH.read_text(encoding="utf-8")
