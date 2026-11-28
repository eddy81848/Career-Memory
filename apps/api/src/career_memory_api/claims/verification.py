"""Read-only Claim VerificationCalculator and deterministic support_graph_epoch (T-405A).

Computes ClaimVerification strictly read-only from ``public.v_claim_verification_summary``
plus active provenance, contributions, identity eligibility, deletion/policy fences, and
optional current-operation Plan C refetch matches. Never mutates persisted rows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from career_memory_api.identities.postgres_service import evaluate_identity_attribution
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimStatus,
    ClaimVerification,
)


def compute_support_graph_epoch(
    *,
    claim_id: str,
    claim_version: int,
    atoms_state: Sequence[Mapping[str, Any]],
    contribution_states: Sequence[Mapping[str, Any]],
    identity_states: Sequence[Mapping[str, Any]],
) -> str:
    """Compute canonical deterministic SHA-256 epoch over the complete Claim support graph."""
    normalized_atoms: list[dict[str, Any]] = []
    for atom in sorted(atoms_state, key=lambda a: str(a["atom_id"])):
        norm_supports: list[dict[str, Any]] = []
        for sup in sorted(atom.get("supports", ()), key=lambda s: str(s["cas_id"])):
            norm_fs = [
                {
                    "cu_hash": str(fs["cu_hash"]),
                    "cu_id": str(fs["cu_id"]),
                    "cu_policy_state": str(fs["cu_policy_state"]),
                    "fs_id": str(fs["fs_id"]),
                    "fs_status": str(fs["fs_status"]),
                    "fs_support_type": str(fs["fs_support_type"]),
                    "rev_hash": str(fs["rev_hash"]),
                    "rev_id": str(fs["rev_id"]),
                    "rev_is_tombstone": bool(fs["rev_is_tombstone"]),
                }
                for fs in sorted(sup.get("fact_supports", ()), key=lambda x: str(x["fs_id"]))
            ]
            norm_supports.append(
                {
                    "cas_id": str(sup["cas_id"]),
                    "cas_status": str(sup["cas_status"]),
                    "cas_support_type": str(sup["cas_support_type"]),
                    "fact_id": str(sup["fact_id"]),
                    "fact_status": str(sup["fact_status"]),
                    "fact_supports": norm_fs,
                    "fact_version": int(sup["fact_version"]),
                }
            )
        normalized_atoms.append(
            {
                "atom_id": str(atom["atom_id"]),
                "atom_status": str(atom["atom_status"]),
                "supports": norm_supports,
            }
        )

    normalized_contribs = [
        {
            "contribution_id": str(c["contribution_id"]),
            "identity_id": str(c["identity_id"]),
            "role": str(c["role"]),
            "status": str(c["status"]),
            "supports": sorted(
                (
                    {
                        "cs_id": str(cs["cs_id"]),
                        "cs_status": str(cs["cs_status"]),
                        "fact_id": str(cs["fact_id"]),
                    }
                    for cs in c.get("supports", ())
                ),
                key=lambda x: str(x["cs_id"]),
            ),
        }
        for c in sorted(contribution_states, key=lambda x: str(x["contribution_id"]))
    ]

    normalized_identities = [
        {
            "block_reason_code": (
                str(i["block_reason_code"]) if i.get("block_reason_code") is not None else None
            ),
            "can_support_source_verified": bool(i["can_support_source_verified"]),
            "can_support_user_confirmed": bool(i["can_support_user_confirmed"]),
            "identity_id": str(i["identity_id"]),
            "identity_status": str(i["identity_status"]),
            "identity_type": str(i["identity_type"]),
        }
        for i in sorted(identity_states, key=lambda x: str(x["identity_id"]))
    ]

    canonical = json.dumps(
        {
            "atoms": normalized_atoms,
            "claim_id": claim_id,
            "claim_version": int(claim_version),
            "contributions": normalized_contribs,
            "identities": normalized_identities,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ClaimVerificationEvaluation:
    """Read-only verification summary, epoch, and approvability diagnosis for one Claim."""

    claim_id: str
    claim_version: int
    claim_status: ClaimStatus
    computed_verification: ClaimVerification
    atom_count: int
    supported_atom_count: int
    user_confirmed_atom_count: int
    unsupported_atom_count: int
    stale_atom_count: int
    approvable: bool
    approval_block_reasons: tuple[str, ...]
    support_graph_epoch: str
    required_source_unit_ids: tuple[str, ...]
    has_identity_conflict: bool
    deletion_fenced: bool
    policy_blocked: bool


class VerificationCalculator:
    """Strictly read-only VerificationCalculator backed by PostgreSQL 18 views & provenance."""

    def evaluate_claim(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        claim_id: str,
        current_op_matched_unit_ids: frozenset[str] | None = None,
    ) -> ClaimVerificationEvaluation:
        cur.execute(
            """
            SELECT
                c.id::text,
                c.event_id::text,
                c.version,
                c.status,
                c.last_verification_run_id::text,
                v.atom_count,
                v.source_backed_atom_count,
                v.user_confirmed_atom_count,
                v.computed_verification
            FROM public.claims c
            LEFT JOIN public.v_claim_verification_summary v
                ON v.workspace_id = c.workspace_id
               AND v.claim_id = c.id
            WHERE c.workspace_id = %s::uuid
              AND c.id = %s::uuid
            """,
            (workspace_id, claim_id),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"Claim {claim_id} not found in workspace {workspace_id}.")

        event_id = str(row[1])
        claim_version = int(row[2])
        claim_status = ClaimStatus(str(row[3]))
        last_verif_run_id = str(row[4]) if row[4] is not None else None
        view_atom_count = int(row[5] or 0)
        view_source_backed = int(row[6] or 0)
        view_user_confirmed = int(row[7] or 0)
        view_verification = ClaimVerification(str(row[8] or ClaimVerification.UNVERIFIED.value))

        # Load atoms & support graph for deterministic epoch + granular status counts
        cur.execute(
            """
            SELECT id::text, ordinal, atom_type, status
            FROM public.claim_atoms
            WHERE workspace_id = %s::uuid
              AND claim_id = %s::uuid
            ORDER BY ordinal ASC, id ASC
            """,
            (workspace_id, claim_id),
        )
        atom_rows = cur.fetchall()
        atom_ids = [str(r[0]) for r in atom_rows]
        atom_status_map = {str(r[0]): ClaimAtomStatus(str(r[3])) for r in atom_rows}

        unsupported_count = sum(
            1 for st in atom_status_map.values() if st is ClaimAtomStatus.UNSUPPORTED
        )
        stale_count = sum(1 for st in atom_status_map.values() if st is ClaimAtomStatus.STALE)
        supported_count = sum(
            1 for st in atom_status_map.values() if st is ClaimAtomStatus.SUPPORTED
        )
        user_confirmed_count = sum(
            1 for st in atom_status_map.values() if st is ClaimAtomStatus.USER_CONFIRMED
        )

        # Load claim_atom_support -> facts -> fact_support -> content_units -> revisions
        supports_by_atom: dict[str, list[dict[str, Any]]] = {aid: [] for aid in atom_ids}
        referenced_identity_ids: set[str] = set()
        required_unit_ids: set[str] = set()
        policy_blocked = False
        referenced_resource_ids: set[str] = set()

        if atom_ids:
            cur.execute(
                """
                SELECT
                    cas.id::text,
                    cas.claim_atom_id::text,
                    cas.status,
                    cas.support_type,
                    f.id::text,
                    f.version,
                    f.status,
                    f.subject_identity_id::text
                FROM public.claim_atom_support cas
                INNER JOIN public.facts f
                    ON f.workspace_id = cas.workspace_id
                   AND f.id = cas.fact_id
                WHERE cas.workspace_id = %s::uuid
                  AND cas.claim_atom_id = ANY(%s::uuid[])
                ORDER BY cas.id ASC
                """,
                (workspace_id, atom_ids),
            )
            cas_rows = cur.fetchall()
            fact_ids = sorted({str(r[4]) for r in cas_rows})
            for r in cas_rows:
                if r[7] is not None:
                    referenced_identity_ids.add(str(r[7]))

            fs_by_fact: dict[str, list[dict[str, Any]]] = {}
            if fact_ids:
                cur.execute(
                    """
                    SELECT
                        fs.id::text,
                        fs.fact_id::text,
                        fs.status,
                        fs.support_type,
                        cu.id::text,
                        cu.content_hash,
                        cu.policy_state,
                        rev.id::text,
                        rev.content_hash,
                        rev.is_tombstone,
                        so.source_resource_id::text,
                        sr.status,
                        sr.visibility,
                        sa.status
                    FROM public.fact_support fs
                    INNER JOIN public.content_units cu
                        ON cu.workspace_id = fs.workspace_id
                       AND cu.id = fs.content_unit_id
                    INNER JOIN public.source_object_revisions rev
                        ON rev.workspace_id = cu.workspace_id
                       AND rev.id = cu.source_object_revision_id
                    INNER JOIN public.source_objects so
                        ON so.workspace_id = rev.workspace_id
                       AND so.id = rev.source_object_id
                    INNER JOIN public.source_resources sr
                        ON sr.workspace_id = so.workspace_id
                       AND sr.id = so.source_resource_id
                    INNER JOIN public.source_accounts sa
                        ON sa.workspace_id = sr.workspace_id
                       AND sa.id = sr.source_account_id
                    WHERE fs.workspace_id = %s::uuid
                      AND fs.fact_id = ANY(%s::uuid[])
                    ORDER BY fs.id ASC
                    """,
                    (workspace_id, fact_ids),
                )
                for frow in cur.fetchall():
                    fid = str(frow[1])
                    cu_id = str(frow[4])
                    cu_policy = str(frow[6])
                    rev_tomb = bool(frow[9])
                    res_id = str(frow[10])
                    res_status = str(frow[11])
                    res_vis = str(frow[12])
                    acc_status = str(frow[13])
                    referenced_resource_ids.add(res_id)

                    fs_entry = {
                        "fs_id": str(frow[0]),
                        "fs_status": str(frow[2]),
                        "fs_support_type": str(frow[3]),
                        "cu_id": cu_id,
                        "cu_hash": str(frow[5]),
                        "cu_policy_state": cu_policy,
                        "rev_id": str(frow[7]),
                        "rev_hash": str(frow[8]),
                        "rev_is_tombstone": rev_tomb,
                    }
                    fs_by_fact.setdefault(fid, []).append(fs_entry)

                    if str(frow[2]) == "ACTIVE" and str(frow[3]) == "DIRECT":
                        if (
                            cu_policy != "ACTIVE"
                            or rev_tomb
                            or res_status != "ACTIVE"
                            or res_vis != "PUBLIC"
                            or acc_status != "ACTIVE"
                        ):
                            policy_blocked = True

            for r in cas_rows:
                cas_id = str(r[0])
                aid = str(r[1])
                cas_status = str(r[2])
                cas_stype = str(r[3])
                fid = str(r[4])
                f_ver = int(r[5])
                f_status = str(r[6])
                f_supports = fs_by_fact.get(fid, [])
                supports_by_atom[aid].append(
                    {
                        "cas_id": cas_id,
                        "cas_status": cas_status,
                        "cas_support_type": cas_stype,
                        "fact_id": fid,
                        "fact_version": f_ver,
                        "fact_status": f_status,
                        "fact_supports": f_supports,
                    }
                )
                if (
                    atom_status_map.get(aid) is ClaimAtomStatus.SUPPORTED
                    and cas_status == "ACTIVE"
                    and cas_stype == "DIRECT"
                    and f_status == "SUPPORTED"
                ):
                    for fs in f_supports:
                        if fs["fs_status"] == "ACTIVE" and fs["fs_support_type"] == "DIRECT":
                            required_unit_ids.add(str(fs["cu_id"]))

        # Load event contributions & contribution_support
        cur.execute(
            """
            SELECT id::text, identity_id::text, role, status
            FROM public.contributions
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
            ORDER BY id ASC
            """,
            (workspace_id, event_id),
        )
        contrib_rows = cur.fetchall()
        contrib_ids = [str(r[0]) for r in contrib_rows]
        for r in contrib_rows:
            referenced_identity_ids.add(str(r[1]))

        cs_by_contrib: dict[str, list[dict[str, Any]]] = {}
        if contrib_ids:
            cur.execute(
                """
                SELECT id::text, contribution_id::text, fact_id::text, status
                FROM public.contribution_support
                WHERE workspace_id = %s::uuid
                  AND contribution_id = ANY(%s::uuid[])
                ORDER BY id ASC
                """,
                (workspace_id, contrib_ids),
            )
            for cs_row in cur.fetchall():
                cs_by_contrib.setdefault(str(cs_row[1]), []).append(
                    {
                        "cs_id": str(cs_row[0]),
                        "fact_id": str(cs_row[2]),
                        "cs_status": str(cs_row[3]),
                    }
                )

        contribution_states = [
            {
                "contribution_id": str(r[0]),
                "identity_id": str(r[1]),
                "role": str(r[2]),
                "status": str(r[3]),
                "supports": cs_by_contrib.get(str(r[0]), []),
            }
            for r in contrib_rows
        ]

        # Evaluate identity eligibilities
        identity_states: list[dict[str, Any]] = []
        has_identity_conflict = False
        for ident_id in sorted(referenced_identity_ids):
            elig = evaluate_identity_attribution(cur, workspace_id, ident_id)
            identity_states.append(
                {
                    "identity_id": elig.identity_id,
                    "identity_type": elig.identity_type.value,
                    "identity_status": elig.identity_status.value,
                    "can_support_source_verified": elig.can_support_source_verified,
                    "can_support_user_confirmed": elig.can_support_user_confirmed,
                    "block_reason_code": (
                        elig.block_reason_code.value
                        if elig.block_reason_code is not None
                        else None
                    ),
                }
            )
            if not elig.can_support_source_verified and elig.block_reason_code is not None:
                if elig.active_conflicted_mapping_count > 0 or not elig.can_support_user_confirmed:
                    has_identity_conflict = True

        # Check active deletion_jobs fence on event / referenced resources
        cur.execute(
            """
            SELECT 1
            FROM public.deletion_jobs dj
            INNER JOIN public.source_objects so
                ON so.workspace_id = dj.workspace_id
               AND so.source_resource_id = dj.source_resource_id
            LEFT JOIN public.work_events we
                ON we.workspace_id = so.workspace_id
               AND we.primary_anchor_source_object_id = so.id
               AND we.id = %s::uuid
            LEFT JOIN public.event_memberships em
                ON em.workspace_id = so.workspace_id
               AND em.source_object_id = so.id
               AND em.event_id = %s::uuid
               AND em.status = 'ACTIVE'
            WHERE dj.workspace_id = %s::uuid
              AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL')
              AND (
                  we.id IS NOT NULL
                  OR em.id IS NOT NULL
                  OR dj.source_resource_id = ANY(%s::uuid[])
              )
            LIMIT 1
            """,
            (event_id, event_id, workspace_id, sorted(referenced_resource_ids)),
        )
        deletion_fenced = cur.fetchone() is not None

        atoms_state = [
            {
                "atom_id": str(r[0]),
                "atom_status": str(r[3]),
                "supports": supports_by_atom.get(str(r[0]), []),
            }
            for r in atom_rows
        ]
        epoch = compute_support_graph_epoch(
            claim_id=claim_id,
            claim_version=claim_version,
            atoms_state=atoms_state,
            contribution_states=contribution_states,
            identity_states=identity_states,
        )

        # Compute read-only ClaimVerification (strictly fail-closed on STALE/conflict/refetch miss)
        computed = view_verification
        if (
            claim_status is ClaimStatus.STALE
            or stale_count > 0
            or unsupported_count > 0
            or view_atom_count == 0
            or policy_blocked
            or deletion_fenced
        ):
            computed = ClaimVerification.UNVERIFIED
        elif computed is ClaimVerification.SOURCE_VERIFIED:
            if has_identity_conflict or view_source_backed != view_atom_count:
                computed = ClaimVerification.UNVERIFIED
            elif current_op_matched_unit_ids is not None and not required_unit_ids.issubset(
                current_op_matched_unit_ids
            ):
                computed = ClaimVerification.UNVERIFIED

        # Compute approvable + block reasons
        block_reasons: list[str] = []
        if claim_status is ClaimStatus.APPROVED:
            block_reasons.append("CLAIM_ALREADY_APPROVED")
        elif claim_status is ClaimStatus.STALE:
            block_reasons.append("CLAIM_STALE")
        elif claim_status is ClaimStatus.DRAFT:
            block_reasons.append("UNVALIDATED_AFTER_EDIT" if claim_version > 1 else "CLAIM_NOT_VALIDATED")
        elif claim_status is ClaimStatus.DRAFT_INVALID:
            block_reasons.append("CLAIM_DRAFT_INVALID")
        elif claim_status in (ClaimStatus.REJECTED, ClaimStatus.SUPERSEDED):
            block_reasons.append(f"CLAIM_{claim_status.value}")

        if view_atom_count == 0:
            block_reasons.append("NO_CLAIM_ATOMS")
        if unsupported_count > 0:
            block_reasons.append("UNSUPPORTED_ATOMS_PRESENT")
        if stale_count > 0:
            block_reasons.append("STALE_ATOMS_PRESENT")
        if last_verif_run_id is None:
            block_reasons.append("MISSING_VALIDATION_RUN")
        if has_identity_conflict:
            block_reasons.append("IDENTITY_CONFLICT")
        if deletion_fenced:
            block_reasons.append("DELETION_PENDING")
        if policy_blocked:
            block_reasons.append("SOURCE_POLICY_BLOCKED")
        if computed is ClaimVerification.UNVERIFIED and "CLAIM_NOT_VALIDATED" not in block_reasons:
            if "UNVERIFIED_CLAIM" not in block_reasons and not block_reasons:
                block_reasons.append("UNVERIFIED_CLAIM")

        approvable = (
            claim_status is ClaimStatus.SUPPORT_VALIDATED
            and len(block_reasons) == 0
            and computed
            in (
                ClaimVerification.SOURCE_VERIFIED,
                ClaimVerification.USER_CONFIRMED,
                ClaimVerification.MIXED,
            )
        )

        return ClaimVerificationEvaluation(
            claim_id=claim_id,
            claim_version=claim_version,
            claim_status=claim_status,
            computed_verification=computed,
            atom_count=view_atom_count,
            supported_atom_count=supported_count,
            user_confirmed_atom_count=user_confirmed_count,
            unsupported_atom_count=unsupported_count,
            stale_atom_count=stale_count,
            approvable=approvable,
            approval_block_reasons=tuple(block_reasons),
            support_graph_epoch=epoch,
            required_source_unit_ids=tuple(sorted(required_unit_ids)),
            has_identity_conflict=has_identity_conflict,
            deletion_fenced=deletion_fenced,
            policy_blocked=policy_blocked,
        )


__all__ = [
    "ClaimVerificationEvaluation",
    "VerificationCalculator",
    "compute_support_graph_epoch",
]
