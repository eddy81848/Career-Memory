"""Workspace-scoped provenance traceability and computed claim verification helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from career_memory_api.schema_contracts.enums import ClaimVerification


CLAIM_PROVENANCE_CHAIN_SQL = """
SELECT
    workspace_id,
    claim_id,
    event_id,
    claim_version,
    claim_status,
    claim_atom_id,
    atom_ordinal,
    atom_type,
    claim_atom_status,
    claim_atom_support_id,
    claim_atom_support_type,
    claim_atom_support_status,
    fact_id,
    fact_type,
    fact_status,
    fact_support_id,
    fact_support_type,
    fact_support_status,
    validator,
    validator_version,
    content_unit_id,
    content_unit_type,
    content_unit_ordinal,
    content_unit_hash,
    canonicalization_version,
    content_unit_anchor,
    content_unit_policy_state,
    source_object_revision_id,
    source_object_id,
    provider_revision_id,
    revision_content_hash,
    hash_policy_version,
    revision_observed_at,
    revision_is_tombstone
FROM public.v_claim_provenance_chain
WHERE workspace_id = %s
  AND claim_id = %s
ORDER BY atom_ordinal ASC, fact_id ASC, content_unit_ordinal ASC
""".strip()

CLAIM_VERIFICATION_SUMMARY_SQL = """
SELECT
    workspace_id,
    claim_id,
    event_id,
    claim_version,
    claim_status,
    last_verified_at,
    last_verification_run_id,
    atom_count,
    source_backed_atom_count,
    user_confirmed_atom_count,
    computed_verification
FROM public.v_claim_verification_summary
WHERE workspace_id = %s
  AND claim_id = %s
""".strip()


@dataclass(frozen=True, slots=True)
class ProvenanceChainRow:
    """Metadata-only provenance link from Claim down to SourceObjectRevision."""

    workspace_id: UUID
    claim_id: UUID
    event_id: UUID
    claim_version: int
    claim_status: str
    claim_atom_id: UUID
    atom_ordinal: int
    atom_type: str
    claim_atom_status: str
    claim_atom_support_id: UUID
    claim_atom_support_type: str
    claim_atom_support_status: str
    fact_id: UUID
    fact_type: str
    fact_status: str
    fact_support_id: UUID
    fact_support_type: str
    fact_support_status: str
    validator: str
    validator_version: str
    content_unit_id: UUID
    content_unit_type: str
    content_unit_ordinal: int
    content_unit_hash: str
    canonicalization_version: str
    content_unit_anchor: dict[str, Any]
    content_unit_policy_state: str
    source_object_revision_id: UUID
    source_object_id: UUID
    provider_revision_id: str | None
    revision_content_hash: str
    hash_policy_version: str
    revision_observed_at: datetime
    revision_is_tombstone: bool


@dataclass(frozen=True, slots=True)
class ClaimVerificationSummary:
    """Read-only computed verification readout for a Claim."""

    workspace_id: UUID
    claim_id: UUID
    event_id: UUID
    claim_version: int
    claim_status: str
    last_verified_at: datetime | None
    last_verification_run_id: UUID | None
    atom_count: int
    source_backed_atom_count: int
    user_confirmed_atom_count: int
    computed_verification: ClaimVerification


def set_workspace_rls_context(conn: Any, workspace_id: str | UUID) -> None:
    """Set transaction-local ``app.current_workspace_id`` GUC for RLS."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_workspace_id', %s, true)",
            (str(workspace_id),),
        )


def clear_workspace_rls_context(conn: Any) -> None:
    """Clear transaction-local ``app.current_workspace_id`` GUC (fail-closed)."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_workspace_id', '', true)")


def fetch_claim_provenance_chain(
    conn: Any,
    *,
    workspace_id: str | UUID,
    claim_id: str | UUID,
) -> list[ProvenanceChainRow]:
    """Fetch full metadata-only provenance chain for a Claim within a Workspace."""
    with conn.cursor() as cur:
        cur.execute(CLAIM_PROVENANCE_CHAIN_SQL, (str(workspace_id), str(claim_id)))
        rows = cur.fetchall()
    return [ProvenanceChainRow(*row) for row in rows]


def fetch_claim_verification_summary(
    conn: Any,
    *,
    workspace_id: str | UUID,
    claim_id: str | UUID,
) -> ClaimVerificationSummary | None:
    """Fetch computed verification status for a Claim within a Workspace."""
    with conn.cursor() as cur:
        cur.execute(CLAIM_VERIFICATION_SUMMARY_SQL, (str(workspace_id), str(claim_id)))
        row = cur.fetchone()
    if row is None:
        return None
    return ClaimVerificationSummary(
        workspace_id=row[0],
        claim_id=row[1],
        event_id=row[2],
        claim_version=row[3],
        claim_status=row[4],
        last_verified_at=row[5],
        last_verification_run_id=row[6],
        atom_count=row[7],
        source_backed_atom_count=row[8],
        user_confirmed_atom_count=row[9],
        computed_verification=ClaimVerification(row[10]),
    )
