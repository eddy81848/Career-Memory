"""T-005C Strictly READ-ONLY logical snapshot comparator and static expected-snapshot loader.

ABSOLUTE PROHIBITION (T-005C):
This module MUST NEVER write, overwrite, or auto-update expected snapshot files.
All snapshot comparisons are strictly read-only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from career_memory_api.fault_harness.scenario_schema import (
    FaultInjectionPoint,
    validate_expected_snapshot,
)


@dataclass(frozen=True, slots=True)
class ScenarioDiffEntry:
    """Single human-readable path-level mismatch between expected and actual logical snapshots."""

    path: str
    expected: Any
    actual: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Structured outcome of comparing an actual logical snapshot against a static expected snapshot."""

    scenario_id: str
    injection_points: tuple[FaultInjectionPoint, ...]
    matched: bool
    diffs: tuple[ScenarioDiffEntry, ...]

    def format_report(self) -> str:
        pts = ", ".join(p.value for p in self.injection_points)
        if self.matched:
            return f"[{self.scenario_id}] MATCHED (injection_points=[{pts}], diffs=0)"
        lines = [
            f"[{self.scenario_id}] SNAPSHOT MISMATCH (injection_points=[{pts}], diffs={len(self.diffs)}):"
        ]
        for idx, d in enumerate(self.diffs, start=1):
            lines.append(
                f"  {idx}. {d.path}: {d.message} | expected={d.expected!r} actual={d.actual!r}"
            )
        return "\n".join(lines)


def _diff_recursive(
    expected: Any,
    actual: Any,
    *,
    path: str,
    diffs: list[ScenarioDiffEntry],
) -> None:
    if type(expected) is not type(actual):
        # Allow int/float comparison only if identical type; JSON ints/bools must match strictly
        diffs.append(
            ScenarioDiffEntry(
                path=path,
                expected=expected,
                actual=actual,
                message=f"Type mismatch ({type(expected).__name__} != {type(actual).__name__})",
            )
        )
        return

    if isinstance(expected, Mapping):
        exp_keys = set(expected.keys())
        act_keys = set(actual.keys())
        for missing in sorted(exp_keys - act_keys):
            diffs.append(
                ScenarioDiffEntry(
                    path=f"{path}.{missing}",
                    expected=expected[missing],
                    actual="<MISSING>",
                    message="Key missing in actual snapshot",
                )
            )
        for extra in sorted(act_keys - exp_keys):
            diffs.append(
                ScenarioDiffEntry(
                    path=f"{path}.{extra}",
                    expected="<ABSENT>",
                    actual=actual[extra],
                    message="Unexpected extra key in actual snapshot",
                )
            )
        for key in sorted(exp_keys & act_keys):
            _diff_recursive(expected[key], actual[key], path=f"{path}.{key}", diffs=diffs)
        return

    if isinstance(expected, list):
        if len(expected) != len(actual):
            diffs.append(
                ScenarioDiffEntry(
                    path=path,
                    expected=f"len={len(expected)}",
                    actual=f"len={len(actual)}",
                    message="List length mismatch",
                )
            )
        for idx in range(min(len(expected), len(actual))):
            _diff_recursive(expected[idx], actual[idx], path=f"{path}[{idx}]", diffs=diffs)
        return

    if expected != actual:
        diffs.append(
            ScenarioDiffEntry(
                path=path,
                expected=expected,
                actual=actual,
                message="Value mismatch",
            )
        )


def compare_logical_snapshots(
    *,
    scenario_id: str,
    injection_points: Sequence[FaultInjectionPoint],
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> ComparisonResult:
    """Compare two normalized logical snapshot dictionaries strictly read-only."""
    diffs: list[ScenarioDiffEntry] = []
    _diff_recursive(dict(expected), dict(actual), path="$", diffs=diffs)
    return ComparisonResult(
        scenario_id=scenario_id,
        injection_points=tuple(injection_points),
        matched=(len(diffs) == 0),
        diffs=tuple(diffs),
    )


def load_expected_snapshot(path: Path) -> dict[str, Any]:
    """Read and validate a static expected snapshot JSON file from disk (read-only)."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Static expected snapshot not found (auto-generation prohibited): {path}"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    validate_expected_snapshot(raw)
    return dict(raw)


__all__ = [
    "ComparisonResult",
    "ScenarioDiffEntry",
    "compare_logical_snapshots",
    "load_expected_snapshot",
]
