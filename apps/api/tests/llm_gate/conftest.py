"""Shared deterministic fixtures for T-408 / INF-08 llm_gate tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    LOCATOR_SPEC_VERSION,
    PARSER_VERSION,
    ContentUnitType,
    EphemeralResolvedUnit,
    EphemeralVerifiedSpan,
    OriginalSourceLocator,
    PersistedContentUnitMetadata,
    build_span_locator,
    canonicalize_source_bytes,
)
from career_memory_api.resolver.models import (
    OperationMemoryResolution,
    ResolutionMetadata,
    ResolutionStatus,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimVerification,
    ContributionRole,
    FactStatus,
    FactType,
    SupportType,
    SupportValidator,
)

WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"
USER_OWNER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_MEMBER_A = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_OWNER_B = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SOURCE_RESOURCE_A = "33333333-3333-4333-8333-333333333333"
SOURCE_REVISION_A = "44444444-4444-4444-8444-444444444444"
CONTENT_UNIT_A = "55555555-5555-4555-8555-555555555555"

ALLOWED_REDIRECT_URI = "https://app.career-memory.local/internal/subscription-callback"
EXPECTED_OIDC_ISSUER = "https://auth.openai.com"
EXPECTED_OIDC_AUDIENCE = "career-memory-subscription-client"
PLAN_INFERENCE_SCOPE = "chatgpt.subscription.inference"

FAKE_PUBLIC_SECRET_MARKER = "ghp_FAKEPUBLICFIXTUREMARKER000000000000"


class FakeClock:
    """Deterministic thread-safe UTC clock for lifecycle & expiry tests."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._lock = threading.Lock()
        self._now = initial or datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, *, seconds: int = 0) -> datetime:
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds)
            return self._now


def make_access(
    *,
    user_id: str,
    workspace_id: str,
    role: str = "OWNER",
    subject: str | None = None,
) -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(
                issuer="https://supabase.career-memory.local/auth/v1",
                subject=subject or f"sub-{user_id}",
            ),
        ),
        membership=WorkspaceMembership(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
        ),
    )


def make_operation_memory_resolution(
    *,
    text: str = "Implemented deterministic cache warming reducing p95 latency by 35% in production.",
    operation_id: str = "op-inf08-001",
    source_object_revision_id: str = SOURCE_REVISION_A,
    content_unit_id: str = CONTENT_UNIT_A,
    status: ResolutionStatus = ResolutionStatus.MATCHED,
    observed_revision_hash_override: str | None = None,
    observed_unit_hash_override: str | None = None,
    checked_at: datetime | None = None,
) -> tuple[OperationMemoryResolution, str, str]:
    """Build a real canonicalized OperationMemoryResolution and return (resolution, rev_hash, span_hash)."""
    canon = canonicalize_source_bytes(text.encode("utf-8"))
    assert canon.status == "CANONICALIZED" and canon.document is not None
    doc = canon.document
    rev_hash = doc.metadata.content_sha256
    locator = build_span_locator(doc.text, 0, len(doc.text))
    span_hash = locator.span_sha256

    unit_meta = PersistedContentUnitMetadata(
        unit_id=content_unit_id,
        unit_index=0,
        unit_type=ContentUnitType.PR_BODY_SECTION,
        source_content_sha256=rev_hash,
        locator=locator,
        original_locator=OriginalSourceLocator(section="Summary"),
        parser_version=PARSER_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
    )
    ephemeral_unit = EphemeralResolvedUnit(metadata=unit_meta, _text=doc.text)
    ephemeral_span = EphemeralVerifiedSpan(locator=locator, _text=doc.text)

    obs_rev = (
        observed_revision_hash_override
        if observed_revision_hash_override is not None
        else (rev_hash if status is ResolutionStatus.MATCHED else None)
    )
    obs_unit = (
        observed_unit_hash_override
        if observed_unit_hash_override is not None
        else (span_hash if status is ResolutionStatus.MATCHED else None)
    )
    meta = ResolutionMetadata(
        status=status,
        checked_at=checked_at or datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC),
        operation_id=operation_id,
        source_object_revision_id=source_object_revision_id,
        content_unit_id=content_unit_id,
        expected_revision_hash=rev_hash,
        observed_revision_hash=obs_rev,
        expected_unit_hash=span_hash,
        observed_unit_hash=obs_unit,
        canonicalization_version=CANONICALIZATION_VERSION,
        parser_version=PARSER_VERSION,
        retryable=False,
        reason_code=None if status is ResolutionStatus.MATCHED else status.value,
        requests_used=1,
        bytes_read=len(doc.text.encode("utf-8")),
        deduped=False,
        fetch_attempted=True,
    )
    resolution = OperationMemoryResolution(
        metadata=meta,
        ephemeral_unit=ephemeral_unit if status is ResolutionStatus.MATCHED else None,
        ephemeral_span=ephemeral_span if status is ResolutionStatus.MATCHED else None,
    )
    return resolution, rev_hash, span_hash


def make_valid_stage_payloads(
    *,
    locator_dict: dict[str, Any],
    source_object_revision_id: str = SOURCE_REVISION_A,
    content_unit_id: str = CONTENT_UNIT_A,
) -> dict[str, dict[str, Any]]:
    """Return valid T-106 schema-compliant payloads for all 5 AI pipeline stages."""
    return {
        "STAGE_2A_FACT_CANDIDATE_EXTRACTION": {
            "schema_version": "1.0.0",
            "extractor_run_id": "run-2a-001",
            "extractor_version": "fact-extractor-v1",
            "source_object_revision_id": source_object_revision_id,
            "content_unit_id": content_unit_id,
            "locator": locator_dict,
            "fact_type": FactType.ACTION.value,
            "predicate": "implemented",
            "object_text": "Implemented deterministic cache warming",
            "normalized_value": {"component": "cache-warmer"},
            "status": FactStatus.CANDIDATE.value,
        },
        "STAGE_2B_FACT_SUPPORT_VALIDATION": {
            "schema_version": "1.0.0",
            "validator_run_id": "run-2b-001",
            "extractor_run_id": "run-2a-001",
            "fact_candidate_id": "fact-cand-001",
            "source_object_revision_id": source_object_revision_id,
            "content_unit_id": content_unit_id,
            "verified_locator": locator_dict,
            "support_type": SupportType.DIRECT.value,
            "validator": SupportValidator.MODEL.value,
            "validator_version": "fact-validator-v1",
            "promotion_decision": "PROMOTE_SUPPORTED",
            "revision_and_locator_verified": True,
        },
        "STAGE_3A_CLAIM_GENERATION": {
            "schema_version": "1.0.0",
            "generator_run_id": "run-3a-001",
            "confirmed_event_id": "event-001",
            "input_fact_ids": ["fact-001", "fact-002"],
            "claim_version": 1,
            "claim_text": "Implemented deterministic cache warming in production.",
            "status": "DRAFT",
        },
        "STAGE_3B_CLAIM_ATOMIZATION": {
            "schema_version": "1.0.0",
            "atomizer_run_id": "run-3b-001",
            "generator_run_id": "run-3a-001",
            "claim_id": "claim-001",
            "claim_version": 1,
            "atoms": [
                {
                    "ordinal": 0,
                    "atom_type": ClaimAtomType.ACTION.value,
                    "atom_text": "Implemented deterministic cache warming",
                }
            ],
        },
        "STAGE_3C_CLAIM_SUPPORT_VALIDATION": {
            "schema_version": "1.0.0",
            "validation_run_id": "run-3c-001",
            "generator_run_id": "run-3a-001",
            "atomizer_run_id": "run-3b-001",
            "claim_id": "claim-001",
            "claim_version": 1,
            "atom_validations": [
                {
                    "ordinal": 0,
                    "atom_type": ClaimAtomType.ACTION.value,
                    "status": ClaimAtomStatus.SUPPORTED.value,
                    "fact_bindings": [
                        {
                            "fact_id": "fact-001",
                            "support_type": SupportType.DIRECT.value,
                            "fact_support_ids": ["fs-001"],
                            "source_object_revision_id": source_object_revision_id,
                            "content_unit_id": content_unit_id,
                        }
                    ],
                }
            ],
            "computed_verification": ClaimVerification.SOURCE_VERIFIED.value,
        },
    }


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def owner_a_access() -> WorkspaceAccessContext:
    return make_access(user_id=USER_OWNER_A, workspace_id=WORKSPACE_A, role="OWNER")


@pytest.fixture
def member_a_access() -> WorkspaceAccessContext:
    return make_access(user_id=USER_MEMBER_A, workspace_id=WORKSPACE_A, role="MEMBER")


@pytest.fixture
def owner_b_access() -> WorkspaceAccessContext:
    return make_access(user_id=USER_OWNER_B, workspace_id=WORKSPACE_B, role="OWNER")


def _unused_imports_guard() -> tuple[Any, Any]:
    return LOCATOR_SPEC_VERSION, ContributionRole.IMPLEMENTED
