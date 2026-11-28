"""Stage 3A deterministic ContextBuilder (T-404).

Selects strictly eligible same-Event SUPPORTED Facts with active DIRECT provenance,
enforces deterministic ordering, deduplication, and explicit byte/token budget fences
without ever silently dropping required support or including Event summaries / prior drafts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from career_memory_api.claims.models import (
    DEFAULT_CONTEXT_MAX_BYTES,
    DEFAULT_CONTEXT_MAX_TOKENS,
    ClaimError,
    ContextBuildStatus,
)
from career_memory_api.facts.models import FactSemanticAttributes, MetricDetailsModel
from career_memory_api.schema_contracts.enums import (
    FactStatus,
    FactType,
    ResourceVisibility,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys


def _estimate_tokens(byte_count: int) -> int:
    return max(1, (byte_count + 3) // 4) if byte_count > 0 else 0


@dataclass(frozen=True, slots=True)
class EligibleFactContextItem:
    """Single non-raw canonical Fact candidate evaluated by Stage 3A ContextBuilder."""

    fact_id: str
    event_id: str
    version: int
    fact_type: FactType
    predicate: str
    object_text: str
    status: FactStatus
    subject_identity_id: str | None
    normalized_value: dict[str, Any] | None
    metric_details: MetricDetailsModel | None
    semantic: FactSemanticAttributes
    has_active_direct_support: bool
    has_active_contradicts: bool
    active_direct_fact_support_ids: tuple[str, ...]
    active_content_unit_ids: tuple[str, ...]
    active_revision_ids: tuple[str, ...]
    unit_policy_states: dict[str, str] = field(default_factory=dict)
    revision_tombstone_flags: dict[str, bool] = field(default_factory=dict)
    source_resource_id: str = "unknown-resource"
    visibility: ResourceVisibility = ResourceVisibility.PUBLIC
    deletion_fenced: bool = False
    input_hash: str = ""

    def canonical_input_hash(self) -> str:
        if self.input_hash:
            return self.input_hash
        canonical = json.dumps(
            {
                "event_id": self.event_id,
                "fact_id": self.fact_id,
                "fact_type": self.fact_type.value,
                "object_text": self.object_text,
                "predicate": self.predicate,
                "status": self.status.value,
                "version": self.version,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def to_manifest_entry(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fact_id": self.fact_id,
            "version": self.version,
            "fact_type": self.fact_type.value,
            "predicate": self.predicate,
            "object_text": self.object_text,
            "subject_identity_id": self.subject_identity_id,
            "completion_state": self.semantic.completion_state.value,
            "scope": self.semantic.scope.value,
            "environment_scope": self.semantic.environment_scope.value,
            "causal_basis": self.semantic.causal_basis.value,
            "fact_support_ids": list(self.active_direct_fact_support_ids),
            "content_unit_ids": list(self.active_content_unit_ids),
            "source_revision_ids": list(self.active_revision_ids),
        }
        if self.metric_details is not None:
            payload["metric_details"] = self.metric_details.model_dump(mode="json")
        if self.normalized_value is not None:
            payload["normalized_value"] = dict(self.normalized_value)
        assert_no_forbidden_raw_keys(payload, field_name="ContextBuilder.manifest_entry")
        return payload


@dataclass(frozen=True, slots=True)
class ClaimContextBundle:
    """Deterministic Stage 3A ContextBuilder output passed to ClaimGenerator."""

    event_id: str
    event_status: WorkEventStatus
    anchor_mode: WorkEventAnchorMode
    status: ContextBuildStatus
    facts: tuple[EligibleFactContextItem, ...]
    excluded_fact_ids: tuple[str, ...]
    dropped_due_to_budget_fact_ids: tuple[str, ...]
    total_bytes: int
    estimated_tokens: int
    input_hash: str
    source_resource_id: str
    visibility: ResourceVisibility
    reason_code: str | None = None

    def to_manifest_json(self) -> str:
        payload = {
            "event_id": self.event_id,
            "anchor_mode": self.anchor_mode.value,
            "facts": [f.to_manifest_entry() for f in self.facts],
        }
        assert_no_forbidden_raw_keys(payload, field_name="ClaimContextBundle")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ClaimContextBuilder:
    """Deterministic Stage 3A ContextBuilder (T-404).

    Guarantees:
    - Target WorkEvent MUST be CONFIRMED and NOT an active member of a composite event.
    - Includes ONLY active, non-superseded same-event SUPPORTED Facts with active DIRECT
      fact_support, 0 CONTRADICTS, ACTIVE ContentUnit, non-tombstoned Revision, and zero
      deletion/policy fence.
    - Orders deterministically by ``(fact_type.value, fact_id)`` and deduplicates by
      ``(fact_id, version, canonical_input_hash)``.
    - Never silently drops required facts when byte/token budgets are exceeded.
    """

    def build(
        self,
        *,
        event_id: str,
        event_status: WorkEventStatus,
        anchor_mode: WorkEventAnchorMode,
        is_active_composite_member: bool,
        facts: Sequence[EligibleFactContextItem],
        requested_fact_ids: Sequence[str] | None = None,
        max_bytes: int = DEFAULT_CONTEXT_MAX_BYTES,
        max_tokens: int = DEFAULT_CONTEXT_MAX_TOKENS,
        deletion_fenced: bool = False,
    ) -> ClaimContextBundle:
        if deletion_fenced:
            raise ClaimError(
                status_code=409,
                code="DELETION_PENDING",
                message="Claim generation blocked while a linked source resource deletion is active.",
            )
        if event_status is not WorkEventStatus.CONFIRMED:
            raise ClaimError(
                status_code=409,
                code="EVENT_NOT_CONFIRMED",
                message=(
                    f"Target WorkEvent must be CONFIRMED before claim generation "
                    f"(current status: {event_status.value})."
                ),
            )
        if is_active_composite_member:
            raise ClaimError(
                status_code=409,
                code="EVENT_IS_COMPOSITE_MEMBER",
                message=(
                    "WorkEvent is an active member of a USER_COMPOSITE event; generate claims "
                    "on the composite event instead."
                ),
            )

        eligible: list[EligibleFactContextItem] = []
        excluded: list[str] = []
        seen_dedupe_keys: set[tuple[str, int, str]] = set()

        for item in facts:
            if not self._is_fact_eligible(item, target_event_id=event_id):
                excluded.append(item.fact_id)
                continue
            dedupe_key = (item.fact_id, item.version, item.canonical_input_hash())
            if dedupe_key in seen_dedupe_keys:
                continue
            seen_dedupe_keys.add(dedupe_key)
            eligible.append(item)

        eligible.sort(key=lambda f: (f.fact_type.value, f.fact_id))

        # Validate explicit requested_fact_ids subset strictly against eligible facts
        if requested_fact_ids is not None:
            eligible_by_id = {f.fact_id: f for f in eligible}
            missing_or_ineligible = [
                fid for fid in requested_fact_ids if fid not in eligible_by_id
            ]
            if missing_or_ineligible:
                raise ClaimError(
                    status_code=422,
                    code="INELIGIBLE_INPUT_FACT_IDS",
                    message=(
                        "All input_fact_ids must be active same-event SUPPORTED facts with "
                        "active DIRECT provenance."
                    ),
                    details={"ineligible_fact_ids": sorted(set(missing_or_ineligible))},
                )
            selected_set = set(requested_fact_ids)
            eligible = [f for f in eligible if f.fact_id in selected_set]
            eligible.sort(key=lambda f: (f.fact_type.value, f.fact_id))

        source_resource_id = (
            eligible[0].source_resource_id if eligible else "no-eligible-resource"
        )
        visibility = (
            eligible[0].visibility if eligible else ResourceVisibility.PUBLIC
        )

        if not eligible:
            empty_hash = self._compute_bundle_hash(event_id=event_id, facts=())
            return ClaimContextBundle(
                event_id=event_id,
                event_status=event_status,
                anchor_mode=anchor_mode,
                status=ContextBuildStatus.EMPTY,
                facts=(),
                excluded_fact_ids=tuple(sorted(set(excluded))),
                dropped_due_to_budget_fact_ids=(),
                total_bytes=0,
                estimated_tokens=0,
                input_hash=empty_hash,
                source_resource_id=source_resource_id,
                visibility=visibility,
                reason_code="NO_ELIGIBLE_SUPPORTED_FACTS",
            )

        # Measure full required manifest size without silently dropping required facts
        manifest_entries = [f.to_manifest_entry() for f in eligible]
        serialized = json.dumps(
            {"event_id": event_id, "facts": manifest_entries},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        total_bytes = len(serialized.encode("utf-8"))
        est_tokens = _estimate_tokens(total_bytes)

        if total_bytes > max_bytes or est_tokens > max_tokens:
            # Never silently drop required facts: signal PARTIAL_WITH_LIMIT / MANUAL_ONLY
            fitting_ids: set[str] = set()
            running_bytes = len(json.dumps({"event_id": event_id, "facts": []}).encode("utf-8"))
            for item, entry in zip(eligible, manifest_entries, strict=True):
                entry_bytes = len(
                    json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                ) + 2
                if (
                    running_bytes + entry_bytes <= max_bytes
                    and _estimate_tokens(running_bytes + entry_bytes) <= max_tokens
                ):
                    running_bytes += entry_bytes
                    fitting_ids.add(item.fact_id)
            dropped = tuple(f.fact_id for f in eligible if f.fact_id not in fitting_ids)
            budget_status = (
                ContextBuildStatus.MANUAL_ONLY
                if not fitting_ids
                else ContextBuildStatus.PARTIAL_WITH_LIMIT
            )
            return ClaimContextBundle(
                event_id=event_id,
                event_status=event_status,
                anchor_mode=anchor_mode,
                status=budget_status,
                facts=tuple(eligible),
                excluded_fact_ids=tuple(sorted(set(excluded))),
                dropped_due_to_budget_fact_ids=dropped,
                total_bytes=total_bytes,
                estimated_tokens=est_tokens,
                input_hash=self._compute_bundle_hash(event_id=event_id, facts=tuple(eligible)),
                source_resource_id=source_resource_id,
                visibility=visibility,
                reason_code="CONTEXT_BUDGET_EXCEEDED",
            )

        bundle_hash = self._compute_bundle_hash(event_id=event_id, facts=tuple(eligible))
        return ClaimContextBundle(
            event_id=event_id,
            event_status=event_status,
            anchor_mode=anchor_mode,
            status=ContextBuildStatus.READY,
            facts=tuple(eligible),
            excluded_fact_ids=tuple(sorted(set(excluded))),
            dropped_due_to_budget_fact_ids=(),
            total_bytes=total_bytes,
            estimated_tokens=est_tokens,
            input_hash=bundle_hash,
            source_resource_id=source_resource_id,
            visibility=visibility,
            reason_code=None,
        )

    @staticmethod
    def _is_fact_eligible(item: EligibleFactContextItem, *, target_event_id: str) -> bool:
        if item.event_id != target_event_id:
            return False
        if item.status is not FactStatus.SUPPORTED:
            return False
        if item.deletion_fenced:
            return False
        if not item.has_active_direct_support or item.has_active_contradicts:
            return False
        if not item.active_direct_fact_support_ids or not item.active_content_unit_ids:
            return False
        # Require at least one active, non-tombstoned provenance path
        has_valid_unit = any(
            item.unit_policy_states.get(cu_id) == "ACTIVE"
            for cu_id in item.active_content_unit_ids
        )
        if not has_valid_unit:
            return False
        has_non_tombstone_rev = any(
            not item.revision_tombstone_flags.get(rev_id, True)
            for rev_id in item.active_revision_ids
        )
        if not has_non_tombstone_rev:
            return False
        return True

    @staticmethod
    def _compute_bundle_hash(
        *,
        event_id: str,
        facts: tuple[EligibleFactContextItem, ...],
    ) -> str:
        canonical = json.dumps(
            {
                "event_id": event_id,
                "facts": [
                    {
                        "fact_id": f.fact_id,
                        "input_hash": f.canonical_input_hash(),
                        "version": f.version,
                    }
                    for f in facts
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


__all__ = [
    "ClaimContextBuilder",
    "ClaimContextBundle",
    "EligibleFactContextItem",
]
