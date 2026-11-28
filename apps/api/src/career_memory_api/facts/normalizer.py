"""CanonicalFactNormalizer & MVP Contributions Rules (Data Model §3.7–3.8, CLAIM-05).

Enforces:
- Fact promotion to ``SUPPORTED`` ONLY when:
  1. At least one ``ACTIVE`` ``fact_support`` row has ``support_type == 'DIRECT'``,
  2. Zero ``ACTIVE`` ``fact_support`` rows have ``support_type == 'CONTRADICTS'``, AND
  3. Identity & type gates pass (``evaluate_identity_attribution.can_support_source_verified``).
- Any ``ACTIVE`` ``CONTRADICTS`` support transitions the Fact to ``CONFLICTED``.
- Deterministic structured canonical key deduplication (NEVER fuzzy/embedding/LLM text merging).
- MVP automatic ``RULE`` contributions strictly restricted to ``IMPLEMENTED`` and ``REVIEWED``;
  ``SOURCE_SUPPORTED`` requires an ``ACTIVE`` ``VERIFIED`` ``PERSON`` identity AND at least one
  ``ACTIVE`` ``contribution_support`` pointing to a ``SUPPORTED`` Fact with ``DIRECT`` support.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Set
from dataclasses import dataclass

from career_memory_api.facts.models import (
    ContributionRecord,
    FactAttributionScope,
    FactError,
    FactSemanticAttributes,
)
from career_memory_api.identities.models import IdentityAttributionEligibility
from career_memory_api.schema_contracts.enums import (
    MVP_RULE_CONTRIBUTION_ROLES,
    ContributionRole,
    ContributionStatus,
    FactStatus,
    FactType,
    RecordOrigin,
    SupportType,
)

_WS_RE = re.compile(r"\s+")


def normalize_structured_text_key(value: str) -> str:
    """Normalize structured predicate/object strings deterministically (no fuzzy matching)."""
    return _WS_RE.sub(" ", value.strip().lower())


def build_canonical_fact_key(
    *,
    workspace_id: str,
    event_id: str,
    fact_type: FactType,
    subject_identity_id: str | None,
    predicate: str,
    object_text: str,
    semantic: FactSemanticAttributes,
) -> str:
    """Compute deterministic canonical deduplication key within a single WorkEvent.

    Uses strictly structured fields:
    ``(workspace_id, event_id, fact_type, subject_identity_id, normalized_predicate,
      normalized_object_key, completion_state, scope, environment_scope)``.
    Never merges distinct values by fuzzy, embedding, or LLM similarity.
    """
    payload = {
        "completion_state": semantic.completion_state.value,
        "environment_scope": semantic.environment_scope.value,
        "event_id": event_id,
        "fact_type": fact_type.value,
        "normalized_object_key": normalize_structured_text_key(object_text),
        "normalized_predicate": normalize_structured_text_key(predicate),
        "scope": semantic.scope.value,
        "subject_identity_id": subject_identity_id,
        "workspace_id": workspace_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ContributionCandidateInput:
    """Candidate Contribution evaluated for MVP rule role & CLAIM-05 support gating."""

    workspace_id: str
    event_id: str
    identity_id: str
    role: ContributionRole
    origin: RecordOrigin = RecordOrigin.RULE
    supporting_fact_ids: tuple[str, ...] = ()


class CanonicalFactNormalizer:
    """Pure deterministic normalizer for Fact status promotion and Contribution gating."""

    @staticmethod
    def requires_verified_identity(
        *,
        fact_type: FactType,
        subject_identity_id: str | None,
        semantic: FactSemanticAttributes,
    ) -> bool:
        return (
            subject_identity_id is not None
            or fact_type is FactType.ROLE
            or semantic.scope is FactAttributionScope.INDIVIDUAL
        )

    @staticmethod
    def compute_fact_status(
        *,
        active_support_types: Iterable[SupportType],
        identity_required: bool,
        identity_eligibility: IdentityAttributionEligibility | None = None,
        current_status: FactStatus = FactStatus.CANDIDATE,
    ) -> FactStatus:
        """Compute canonical Fact status from active fact_support types and identity gate."""
        if current_status in (FactStatus.REJECTED, FactStatus.SUPERSEDED):
            return current_status

        supports = list(active_support_types)
        has_contradicts = any(s is SupportType.CONTRADICTS for s in supports)
        if has_contradicts:
            return FactStatus.CONFLICTED

        has_direct = any(s is SupportType.DIRECT for s in supports)
        if has_direct:
            if identity_required:
                if (
                    identity_eligibility is None
                    or not identity_eligibility.can_support_source_verified
                ):
                    return (
                        FactStatus.STALE
                        if current_status is FactStatus.SUPPORTED
                        else FactStatus.CANDIDATE
                    )
            return FactStatus.SUPPORTED

        if current_status is FactStatus.SUPPORTED:
            return FactStatus.STALE
        if current_status is FactStatus.USER_CONFIRMED:
            return FactStatus.USER_CONFIRMED
        return FactStatus.CANDIDATE

    @staticmethod
    def evaluate_contribution(
        candidate: ContributionCandidateInput,
        *,
        identity_eligibility: IdentityAttributionEligibility | None,
        supported_direct_fact_ids: Set[str],
        contribution_id: str = "",
    ) -> ContributionRecord:
        """Validate MVP RULE role restriction and CLAIM-05 concrete contribution support."""
        if (
            candidate.origin is RecordOrigin.RULE
            and candidate.role not in MVP_RULE_CONTRIBUTION_ROLES
        ):
            raise FactError(
                status_code=422,
                code="PROHIBITED_MVP_RULE_CONTRIBUTION_ROLE",
                message=(
                    f"Automatic RULE contribution role {candidate.role.value!r} is forbidden in MVP; "
                    "only IMPLEMENTED and REVIEWED are permitted."
                ),
            )

        verified_facts = tuple(
            fid for fid in candidate.supporting_fact_ids if fid in supported_direct_fact_ids
        )
        can_source_support = (
            identity_eligibility is not None
            and identity_eligibility.can_support_source_verified
            and len(verified_facts) > 0
        )
        status = (
            ContributionStatus.SOURCE_SUPPORTED
            if can_source_support
            else ContributionStatus.CANDIDATE
        )
        return ContributionRecord(
            contribution_id=contribution_id,
            workspace_id=candidate.workspace_id,
            event_id=candidate.event_id,
            identity_id=candidate.identity_id,
            role=candidate.role,
            origin=candidate.origin,
            status=status,
            supporting_fact_ids=verified_facts,
        )


__all__ = [
    "CanonicalFactNormalizer",
    "ContributionCandidateInput",
    "build_canonical_fact_key",
    "normalize_structured_text_key",
]
