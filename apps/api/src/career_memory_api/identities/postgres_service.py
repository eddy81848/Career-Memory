"""PostgreSQL 18 GitHub Identity Mapping, Eligibility & Atomic Invalidation Service (T-401, T-401A).

Enforces:
- ID-01: Provider stable ``external_user_id`` precedence and verification method gate
  (``OAUTH_ACCOUNT``/``PROVIDER_ASSOCIATION`` -> ``VERIFIED``; ``USER_CONFIRMATION`` ->
  ``USER_CONFIRMED``, never ``VERIFIED``; username/email similarity -> ``HEURISTIC``).
- ID-02: Account rename updates existing canonical Identity/mapping without duplicate
  Identity rows and records append-only ``IDENTITY_USERNAME_RENAMED`` audit events.
- ID-03: Noreply / committer / co-author / bot separation; zero plaintext email persistence.
- ID-04: Downstream attribution eligibility helper blocking heuristic, conflicted, ambiguous,
  revoked, and bot identities from ``SOURCE_VERIFIED``.
- ID-05: Atomic revocation & conflict invalidation cascade inside a single
  ``workspace_scoped_transaction`` with ``FOR UPDATE`` row locks, real-time workspace
  membership + deletion fence re-verification before commit, and injectable
  ``before_commit_hook`` for deterministic concurrency testing.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceMembershipDeniedError
from career_memory_api.identities.models import (
    ConfirmIdentityMappingRequest,
    ConfirmIdentityMappingResponse,
    ExternalIdentityMappingRecord,
    GitHubActorObservation,
    GitHubActorRole,
    IdentityAttributionBlockReason,
    IdentityAttributionEligibility,
    IdentityError,
    IdentityInvalidationCounts,
    IdentityListResponse,
    IdentityWithMappingsRecord,
    ResolveGitHubIdentityResult,
    RevokeIdentityMappingRequest,
    RevokeIdentityMappingResponse,
    hash_email,
    is_bot_actor,
    parse_github_noreply_email,
)
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.schema_contracts.enums import (
    ExternalIdentityMappingStatus,
    IdentityStatus,
    IdentityType,
    IdentityVerificationMethod,
    ReviewActionType,
    ReviewTargetType,
    SourceProvider,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
BeforeCommitHook = Callable[..., None]

_MAPPING_SELECT_COLUMNS = """
    id::text,
    identity_id::text,
    provider,
    external_user_id,
    email_hash,
    username,
    verification_method,
    status,
    confidence_reason,
    verified_at,
    revoked_at,
    created_at
"""


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _compute_request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _invoke_before_commit_hook(
    hook: BeforeCommitHook | None,
    *,
    action: str,
    cur: psycopg.Cursor,
) -> None:
    if hook is None:
        return
    sig = inspect.signature(hook)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(params) >= 2:
        hook(action, cur)
    elif len(params) == 1:
        hook(action)
    else:
        hook()


def _row_to_mapping_record(row: tuple[Any, ...]) -> ExternalIdentityMappingRecord:
    return ExternalIdentityMappingRecord(
        mapping_id=str(row[0]),
        identity_id=str(row[1]),
        provider=SourceProvider(str(row[2])),
        external_user_id=str(row[3]) if row[3] is not None else None,
        email_hash=str(row[4]) if row[4] is not None else None,
        username=str(row[5]) if row[5] is not None else None,
        verification_method=IdentityVerificationMethod(str(row[6])),
        status=ExternalIdentityMappingStatus(str(row[7])),
        confidence_reason=str(row[8]) if row[8] is not None else None,
        verified_at=row[9],
        revoked_at=row[10],
        created_at=row[11],
    )


def _compute_eligibility_from_rows(
    *,
    identity_id: str,
    identity_type: IdentityType,
    identity_status: IdentityStatus,
    mapping_statuses: list[ExternalIdentityMappingStatus],
) -> IdentityAttributionEligibility:
    verified_cnt = sum(1 for s in mapping_statuses if s is ExternalIdentityMappingStatus.VERIFIED)
    user_conf_cnt = sum(
        1 for s in mapping_statuses if s is ExternalIdentityMappingStatus.USER_CONFIRMED
    )
    heur_cnt = sum(1 for s in mapping_statuses if s is ExternalIdentityMappingStatus.HEURISTIC)
    conf_cnt = sum(1 for s in mapping_statuses if s is ExternalIdentityMappingStatus.CONFLICTED)
    revoked_cnt = sum(1 for s in mapping_statuses if s is ExternalIdentityMappingStatus.REVOKED)

    base_kwargs = {
        "identity_id": identity_id,
        "identity_type": identity_type,
        "identity_status": identity_status,
        "active_verified_mapping_count": verified_cnt,
        "active_user_confirmed_mapping_count": user_conf_cnt,
        "active_heuristic_mapping_count": heur_cnt,
        "active_conflicted_mapping_count": conf_cnt,
        "revoked_mapping_count": revoked_cnt,
    }

    if identity_type is IdentityType.BOT:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_IS_BOT,
        )

    if identity_status is IdentityStatus.REVOKED:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_REVOKED,
        )

    if conf_cnt > 0:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_CONFLICTED,
        )

    if identity_status is IdentityStatus.AMBIGUOUS or identity_type is not IdentityType.PERSON:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_AMBIGUOUS,
        )

    if identity_status is not IdentityStatus.ACTIVE:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_AMBIGUOUS,
        )

    if verified_cnt > 0:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=True,
            can_support_user_confirmed=True,
            block_reason_code=None,
        )

    if user_conf_cnt > 0:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=True,
            block_reason_code=None,
        )

    if heur_cnt > 0:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_HEURISTIC_ONLY,
        )

    if revoked_cnt > 0:
        return IdentityAttributionEligibility(
            **base_kwargs,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_REVOKED,
        )

    return IdentityAttributionEligibility(
        **base_kwargs,
        can_support_source_verified=False,
        can_support_user_confirmed=False,
        block_reason_code=IdentityAttributionBlockReason.IDENTITY_HEURISTIC_ONLY,
    )


def evaluate_identity_attribution(
    conn_or_cur: psycopg.Connection | psycopg.Cursor,
    workspace_id: str,
    identity_id: str,
) -> IdentityAttributionEligibility:
    """Evaluate downstream attribution eligibility for an Identity inside a workspace (ID-04).

    Accepts either an active ``psycopg.Cursor`` (within a transaction) or a
    ``psycopg.Connection``. Never leaks cross-workspace existence.
    """
    ws_uuid = _parse_uuid_or_none(workspace_id)
    id_uuid = _parse_uuid_or_none(identity_id)
    if ws_uuid is None or id_uuid is None:
        return IdentityAttributionEligibility(
            identity_id=identity_id,
            can_support_source_verified=False,
            can_support_user_confirmed=False,
            block_reason_code=IdentityAttributionBlockReason.IDENTITY_NOT_FOUND,
        )

    def _run(cur: psycopg.Cursor) -> IdentityAttributionEligibility:
        cur.execute(
            """
            SELECT id::text, identity_type, status
            FROM public.identities
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (ws_uuid, id_uuid),
        )
        id_row = cur.fetchone()
        if id_row is None:
            return IdentityAttributionEligibility(
                identity_id=id_uuid,
                can_support_source_verified=False,
                can_support_user_confirmed=False,
                block_reason_code=IdentityAttributionBlockReason.IDENTITY_NOT_FOUND,
            )

        cur.execute(
            """
            SELECT status
            FROM public.external_identity_mappings
            WHERE workspace_id = %s::uuid
              AND identity_id = %s::uuid
            """,
            (ws_uuid, id_uuid),
        )
        statuses = [ExternalIdentityMappingStatus(str(r[0])) for r in cur.fetchall()]
        return _compute_eligibility_from_rows(
            identity_id=str(id_row[0]),
            identity_type=IdentityType(str(id_row[1])),
            identity_status=IdentityStatus(str(id_row[2])),
            mapping_statuses=statuses,
        )

    if isinstance(conn_or_cur, psycopg.Cursor):
        return _run(conn_or_cur)
    with conn_or_cur.cursor() as cur:
        return _run(cur)


class PostgresIdentityService:
    """Transactional PG18 service for GitHub identity mapping, confirmation, and atomic invalidation."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        before_commit_hook: BeforeCommitHook | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._before_commit_hook = before_commit_hook
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    # =========================================================================
    # 1. Read API: GET /v1/identities
    # =========================================================================
    def list_identities(self, access: WorkspaceAccessContext) -> IdentityListResponse:
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                cur.execute(
                    """
                    SELECT
                        id::text,
                        workspace_id::text,
                        display_name,
                        identity_type,
                        canonical_user_id::text,
                        status,
                        created_at,
                        updated_at
                    FROM public.identities
                    WHERE workspace_id = %s::uuid
                    ORDER BY created_at ASC, id ASC
                    """,
                    (access.workspace_id,),
                )
                id_rows = cur.fetchall()
                if not id_rows:
                    return IdentityListResponse(items=[])

                cur.execute(
                    f"""
                    SELECT {_MAPPING_SELECT_COLUMNS}
                    FROM public.external_identity_mappings
                    WHERE workspace_id = %s::uuid
                    ORDER BY created_at ASC, id ASC
                    """,
                    (access.workspace_id,),
                )
                mappings_by_identity: dict[str, list[ExternalIdentityMappingRecord]] = {}
                for mrow in cur.fetchall():
                    rec = _row_to_mapping_record(mrow)
                    mappings_by_identity.setdefault(rec.identity_id, []).append(rec)

                items: list[IdentityWithMappingsRecord] = []
                for row in id_rows:
                    ident_id = str(row[0])
                    ident_type = IdentityType(str(row[3]))
                    ident_status = IdentityStatus(str(row[5]))
                    m_list = mappings_by_identity.get(ident_id, [])
                    eligibility = _compute_eligibility_from_rows(
                        identity_id=ident_id,
                        identity_type=ident_type,
                        identity_status=ident_status,
                        mapping_statuses=[m.status for m in m_list],
                    )
                    items.append(
                        IdentityWithMappingsRecord(
                            identity_id=ident_id,
                            workspace_id=str(row[1]),
                            display_name=str(row[2]),
                            identity_type=ident_type,
                            canonical_user_id=str(row[4]) if row[4] is not None else None,
                            status=ident_status,
                            created_at=row[6],
                            updated_at=row[7],
                            mappings=m_list,
                            attribution_eligibility=eligibility,
                        )
                    )
                return IdentityListResponse(items=items)

    # =========================================================================
    # 2. Core GitHub Identity Resolution & Rename (ID-01, ID-02, ID-03)
    # =========================================================================
    def resolve_or_upsert_github_identity(
        self,
        access: WorkspaceAccessContext,
        *,
        observation: GitHubActorObservation,
    ) -> ResolveGitHubIdentityResult:
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                result = self.resolve_github_actor_in_tx(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user_id,
                    observation=observation,
                    now=self._now_fn(),
                )
                _invoke_before_commit_hook(
                    self._before_commit_hook,
                    action="resolve_github_identity",
                    cur=cur,
                )
                self._assert_live_workspace_membership(cur, access)
                return result

    def resolve_github_actor_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        actor_user_id: str | None,
        observation: GitHubActorObservation,
        now: datetime | None = None,
    ) -> ResolveGitHubIdentityResult:
        """Resolve or upsert a GitHub actor inside an active workspace-scoped transaction."""
        ts = now or self._now_fn()
        ws_uuid = _parse_uuid_or_none(workspace_id)
        if ws_uuid is None:
            raise IdentityError(
                status_code=422,
                code="INVALID_WORKSPACE_ID",
                message="workspace_id must be a valid UUID.",
            )

        # Immediately hash raw email and parse noreply hints; never persist raw email!
        noreply = parse_github_noreply_email(observation.email)
        computed_email_hash: str | None = observation.email_hash
        if computed_email_hash is None and observation.email and observation.email.strip():
            computed_email_hash = hash_email(observation.email)

        clean_username = (observation.username or "").strip() or None
        if clean_username is None and noreply and noreply.login:
            clean_username = noreply.login

        clean_ext_id = (observation.external_user_id or "").strip() or None
        noreply_numeric_hint: str | None = None
        if noreply and noreply.has_numeric_id and noreply.numeric_user_id:
            if (
                clean_ext_id is None
                and observation.provider_associated
                and observation.actor_role is not GitHubActorRole.CO_AUTHOR
            ):
                clean_ext_id = noreply.numeric_user_id
            else:
                noreply_numeric_hint = noreply.numeric_user_id

        if clean_ext_id is None and clean_username is None and computed_email_hash is None:
            raise IdentityError(
                status_code=422,
                code="MISSING_EXTERNAL_IDENTIFIER",
                message="At least one of external_user_id, username, or email/email_hash is required.",
            )

        detected_bot = is_bot_actor(
            username=clean_username,
            display_name=observation.display_name,
            provider_actor_type=observation.provider_actor_type,
            email=observation.email,
            actor_role=observation.actor_role,
        )
        identity_type = IdentityType.BOT if detected_bot else IdentityType.PERSON
        effective_actor_role = (
            GitHubActorRole.BOT if detected_bot else observation.actor_role
        )

        # Verification method & target status gate (ID-01, ID-03)
        can_provider_verify = (
            clean_ext_id is not None
            and (
                observation.is_oauth_account
                or (
                    observation.provider_associated
                    and effective_actor_role
                    not in (GitHubActorRole.CO_AUTHOR, GitHubActorRole.COMMITTER)
                )
            )
        )
        verification_method = (
            IdentityVerificationMethod.OAUTH_ACCOUNT
            if (observation.is_oauth_account and clean_ext_id is not None)
            else IdentityVerificationMethod.PROVIDER_ASSOCIATION
        )
        desired_status = (
            ExternalIdentityMappingStatus.VERIFIED
            if can_provider_verify
            else ExternalIdentityMappingStatus.HEURISTIC
        )
        confidence_reason = self._build_confidence_reason(
            desired_status=desired_status,
            verification_method=verification_method,
            actor_role=effective_actor_role,
            noreply=noreply,
            detected_bot=detected_bot,
        )

        evidence_meta: dict[str, Any] = {
            "actor_role": effective_actor_role.value,
            "is_bot": detected_bot,
            "provider_associated": bool(observation.provider_associated),
        }
        if noreply is not None:
            evidence_meta["noreply_format"] = (
                "WEB_FLOW"
                if noreply.is_web_flow
                else ("NUMERIC_PLUS_LOGIN" if noreply.has_numeric_id else "LOGIN_ONLY")
            )
        if noreply_numeric_hint is not None:
            evidence_meta["noreply_numeric_user_id_hint"] = noreply_numeric_hint
        if observation.source_object_id:
            evidence_meta["source_object_id"] = observation.source_object_id
        if observation.source_resource_id:
            evidence_meta["source_resource_id"] = observation.source_resource_id
        assert_no_forbidden_raw_keys(evidence_meta, field_name="evidence_metadata")

        # ---------------------------------------------------------------------
        # Precedence Step 1: Stable external_user_id lookup (ID-01, ID-02)
        # ---------------------------------------------------------------------
        if clean_ext_id is not None:
            cur.execute(
                f"""
                SELECT {_MAPPING_SELECT_COLUMNS}, evidence_metadata
                FROM public.external_identity_mappings
                WHERE workspace_id = %s::uuid
                  AND provider = 'GITHUB'
                  AND external_user_id = %s
                  AND status <> 'REVOKED'
                ORDER BY
                    CASE status
                        WHEN 'VERIFIED' THEN 0
                        WHEN 'USER_CONFIRMED' THEN 1
                        WHEN 'HEURISTIC' THEN 2
                        ELSE 3
                    END ASC,
                    created_at ASC
                LIMIT 1
                FOR UPDATE
                """,
                (ws_uuid, clean_ext_id),
            )
            existing_ext = cur.fetchone()
            if existing_ext is not None:
                existing_rec = _row_to_mapping_record(existing_ext[:12])
                existing_meta = dict(existing_ext[12] or {})

                # Check if caller tried to bind an already-verified external_user_id to a DIFFERENT identity
                target_id_uuid = _parse_uuid_or_none(observation.target_identity_id)
                if (
                    target_id_uuid is not None
                    and target_id_uuid != existing_rec.identity_id
                    and existing_rec.status is ExternalIdentityMappingStatus.VERIFIED
                ):
                    return self._record_conflicted_mapping_in_tx(
                        cur,
                        workspace_id=ws_uuid,
                        actor_user_id=actor_user_id,
                        identity_id=target_id_uuid,
                        colliding_identity_ids=[existing_rec.identity_id, target_id_uuid],
                        external_user_id=clean_ext_id,
                        email_hash=computed_email_hash,
                        username=clean_username,
                        verification_method=verification_method,
                        confidence_reason="EXTERNAL_USER_ID_CONFLICT",
                        evidence_meta=evidence_meta,
                        now=ts,
                    )

                # Lock parent identity row
                cur.execute(
                    """
                    SELECT display_name, identity_type, canonical_user_id::text, status
                    FROM public.identities
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    FOR UPDATE
                    """,
                    (ws_uuid, existing_rec.identity_id),
                )
                id_row = cur.fetchone()
                assert id_row is not None
                cur_display_name = str(id_row[0])
                cur_id_type = IdentityType(str(id_row[1]))
                cur_canonical_user = str(id_row[2]) if id_row[2] is not None else None
                cur_id_status = IdentityStatus(str(id_row[3]))

                renamed = False
                prev_username = existing_rec.username
                new_username = existing_rec.username
                if clean_username and prev_username and clean_username != prev_username:
                    renamed = True
                    new_username = clean_username
                    prev_list = list(existing_meta.get("previous_usernames") or [])
                    if prev_username not in prev_list:
                        prev_list.append(prev_username)
                    existing_meta["previous_usernames"] = prev_list
                elif clean_username and prev_username is None:
                    new_username = clean_username

                new_email_hash = existing_rec.email_hash or computed_email_hash
                new_status = existing_rec.status
                new_method = existing_rec.verification_method
                new_verified_at = existing_rec.verified_at
                if (
                    existing_rec.status is ExternalIdentityMappingStatus.HEURISTIC
                    and desired_status is ExternalIdentityMappingStatus.VERIFIED
                ):
                    new_status = ExternalIdentityMappingStatus.VERIFIED
                    new_method = verification_method
                    new_verified_at = ts

                existing_meta.update(
                    {k: v for k, v in evidence_meta.items() if k != "previous_usernames"}
                )
                assert_no_forbidden_raw_keys(existing_meta, field_name="evidence_metadata")

                cur.execute(
                    f"""
                    UPDATE public.external_identity_mappings
                    SET username = %s,
                        email_hash = %s,
                        verification_method = %s,
                        status = %s,
                        verified_at = %s,
                        evidence_metadata = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    RETURNING {_MAPPING_SELECT_COLUMNS}
                    """,
                    (
                        new_username,
                        new_email_hash,
                        new_method.value,
                        new_status.value,
                        new_verified_at,
                        Jsonb(existing_meta),
                        ws_uuid,
                        existing_rec.mapping_id,
                    ),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None
                updated_rec = _row_to_mapping_record(updated_row)

                # Update identity metadata (updated_at, display_name if matched old username, canonical_user_id)
                updated_display = cur_display_name
                if renamed and prev_username and cur_display_name == prev_username and new_username:
                    updated_display = new_username
                updated_canonical = cur_canonical_user
                if (
                    updated_canonical is None
                    and observation.canonical_user_id
                    and new_status is ExternalIdentityMappingStatus.VERIFIED
                    and cur_id_type is IdentityType.PERSON
                    and cur_id_status is IdentityStatus.ACTIVE
                ):
                    updated_canonical = _parse_uuid_or_none(observation.canonical_user_id)

                if (
                    renamed
                    or updated_display != cur_display_name
                    or updated_canonical != cur_canonical_user
                ):
                    cur.execute(
                        """
                        UPDATE public.identities
                        SET display_name = %s,
                            canonical_user_id = %s::uuid,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (
                            updated_display,
                            updated_canonical,
                            ts,
                            ws_uuid,
                            existing_rec.identity_id,
                        ),
                    )

                if renamed:
                    rename_meta = {
                        "mapping_id": existing_rec.mapping_id,
                        "provider": SourceProvider.GITHUB.value,
                        "external_user_id": clean_ext_id,
                        "previous_username": prev_username,
                        "new_username": new_username,
                    }
                    self._insert_audit_event(
                        cur,
                        workspace_id=ws_uuid,
                        actor_type=(
                            AuditActorType.USER if actor_user_id else AuditActorType.WORKER
                        ),
                        actor_id=actor_user_id,
                        action="IDENTITY_USERNAME_RENAMED",
                        target_type="IDENTITY",
                        target_id=existing_rec.identity_id,
                        redacted_metadata=rename_meta,
                        created_at=ts,
                    )

                eligibility = evaluate_identity_attribution(
                    cur, ws_uuid, existing_rec.identity_id
                )
                return ResolveGitHubIdentityResult(
                    identity_id=existing_rec.identity_id,
                    identity_type=cur_id_type,
                    identity_status=cur_id_status,
                    mapping=updated_rec,
                    attribution_eligibility=eligibility,
                    created_identity=False,
                    renamed_username=renamed,
                    previous_username=prev_username if renamed else None,
                    conflict_detected=False,
                )

        # ---------------------------------------------------------------------
        # Precedence Step 2: Mutable username / email_hash lookup (collision check)
        # ---------------------------------------------------------------------
        matched_rows: list[tuple[Any, ...]] = []
        if clean_username is not None or computed_email_hash is not None:
            cur.execute(
                f"""
                SELECT {_MAPPING_SELECT_COLUMNS}
                FROM public.external_identity_mappings
                WHERE workspace_id = %s::uuid
                  AND provider = 'GITHUB'
                  AND status <> 'REVOKED'
                  AND (
                      (%s::text IS NOT NULL AND lower(username) = lower(%s::text))
                      OR (%s::text IS NOT NULL AND email_hash = %s::text)
                  )
                ORDER BY created_at ASC, id ASC
                FOR UPDATE
                """,
                (
                    ws_uuid,
                    clean_username,
                    clean_username,
                    computed_email_hash,
                    computed_email_hash,
                ),
            )
            matched_rows = cur.fetchall()

        matched_records = [_row_to_mapping_record(r) for r in matched_rows]
        distinct_identity_ids = sorted({m.identity_id for m in matched_records})

        # Case 2A: Ambiguous collision across >= 2 distinct identities without winning external_user_id
        if len(distinct_identity_ids) >= 2 and not can_provider_verify:
            primary_id = distinct_identity_ids[0]
            return self._record_conflicted_mapping_in_tx(
                cur,
                workspace_id=ws_uuid,
                actor_user_id=actor_user_id,
                identity_id=primary_id,
                colliding_identity_ids=distinct_identity_ids,
                external_user_id=clean_ext_id,
                email_hash=computed_email_hash,
                username=clean_username,
                verification_method=verification_method,
                confidence_reason="AMBIGUOUS_IDENTIFIER_COLLISION",
                evidence_meta=evidence_meta,
                now=ts,
            )

        # Case 2B: Single matched identity with NO external_user_id conflict -> reuse if heuristic/same
        if len(distinct_identity_ids) == 1 and not can_provider_verify:
            matched_ident_id = distinct_identity_ids[0]
            # Check for external_user_id conflict on that identity
            conflicting_ext = [
                m
                for m in matched_records
                if clean_ext_id is not None
                and m.external_user_id is not None
                and m.external_user_id != clean_ext_id
            ]
            if conflicting_ext:
                return self._record_conflicted_mapping_in_tx(
                    cur,
                    workspace_id=ws_uuid,
                    actor_user_id=actor_user_id,
                    identity_id=matched_ident_id,
                    colliding_identity_ids=[matched_ident_id],
                    external_user_id=clean_ext_id,
                    email_hash=computed_email_hash,
                    username=clean_username,
                    verification_method=verification_method,
                    confidence_reason="EXTERNAL_USER_ID_MISMATCH_ON_USERNAME",
                    evidence_meta=evidence_meta,
                    now=ts,
                )

            # Reuse matching mapping idempotently without elevating HEURISTIC -> VERIFIED
            cur.execute(
                """
                SELECT identity_type, status
                FROM public.identities
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                FOR UPDATE
                """,
                (ws_uuid, matched_ident_id),
            )
            id_row = cur.fetchone()
            assert id_row is not None
            existing_match = matched_records[0]
            eligibility = evaluate_identity_attribution(cur, ws_uuid, matched_ident_id)
            return ResolveGitHubIdentityResult(
                identity_id=matched_ident_id,
                identity_type=IdentityType(str(id_row[0])),
                identity_status=IdentityStatus(str(id_row[1])),
                mapping=existing_match,
                attribution_eligibility=eligibility,
                created_identity=False,
                renamed_username=False,
                conflict_detected=False,
            )

        # Case 2C: Provider-verified observation whose username collided with a DIFFERENT external_user_id
        # or heuristic mapping -> stable external_user_id creates its own canonical Identity, and any
        # stale heuristic-only mapping with the recycled username becomes CONFLICTED.
        if can_provider_verify and matched_records:
            for stale_heur in matched_records:
                if (
                    stale_heur.external_user_id is None
                    and stale_heur.status is ExternalIdentityMappingStatus.HEURISTIC
                ):
                    cur.execute(
                        """
                        UPDATE public.external_identity_mappings
                        SET status = 'CONFLICTED',
                            confidence_reason = 'USERNAME_REASSIGNED_TO_VERIFIED_EXTERNAL_USER'
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (ws_uuid, stale_heur.mapping_id),
                    )
                    cur.execute(
                        """
                        UPDATE public.identities
                        SET status = 'AMBIGUOUS',
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status = 'ACTIVE'
                        """,
                        (ts, ws_uuid, stale_heur.identity_id),
                    )

        # ---------------------------------------------------------------------
        # Step 3: Create new canonical Identity + external_identity_mapping
        # ---------------------------------------------------------------------
        display_name = (
            (observation.display_name or "").strip()
            or clean_username
            or (f"github:{clean_ext_id}" if clean_ext_id else "GitHub Contributor")
        )
        canonical_uid = (
            _parse_uuid_or_none(observation.canonical_user_id)
            if (
                desired_status is ExternalIdentityMappingStatus.VERIFIED
                and identity_type is IdentityType.PERSON
            )
            else None
        )

        # Check if canonical_user_id already owns an ACTIVE identity in this workspace (partial unique index)
        new_identity_id: str | None = None
        created_identity = False
        if canonical_uid is not None:
            cur.execute(
                """
                SELECT id::text
                FROM public.identities
                WHERE workspace_id = %s::uuid
                  AND canonical_user_id = %s::uuid
                  AND status = 'ACTIVE'
                FOR UPDATE
                """,
                (ws_uuid, canonical_uid),
            )
            existing_canon = cur.fetchone()
            if existing_canon is not None:
                new_identity_id = str(existing_canon[0])

        if new_identity_id is None:
            new_identity_id = str(uuid.uuid4())
            created_identity = True
            cur.execute(
                """
                INSERT INTO public.identities (
                    id, workspace_id, display_name, identity_type,
                    canonical_user_id, status, created_at, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s, %s,
                    %s::uuid, 'ACTIVE', %s, %s
                )
                """,
                (
                    new_identity_id,
                    ws_uuid,
                    display_name,
                    identity_type.value,
                    canonical_uid,
                    ts,
                    ts,
                ),
            )

        new_mapping_id = str(uuid.uuid4())
        cur.execute(
            f"""
            INSERT INTO public.external_identity_mappings (
                id, workspace_id, identity_id, provider,
                external_user_id, email_hash, username,
                verification_method, status, confidence_reason,
                evidence_metadata, verified_at, revoked_at, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, 'GITHUB',
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, NULL, %s
            )
            RETURNING {_MAPPING_SELECT_COLUMNS}
            """,
            (
                new_mapping_id,
                ws_uuid,
                new_identity_id,
                clean_ext_id,
                computed_email_hash,
                clean_username,
                verification_method.value,
                desired_status.value,
                confidence_reason,
                Jsonb(evidence_meta),
                ts if desired_status is ExternalIdentityMappingStatus.VERIFIED else None,
                ts,
            ),
        )
        inserted_row = cur.fetchone()
        assert inserted_row is not None
        inserted_rec = _row_to_mapping_record(inserted_row)

        eligibility = evaluate_identity_attribution(cur, ws_uuid, new_identity_id)
        return ResolveGitHubIdentityResult(
            identity_id=new_identity_id,
            identity_type=identity_type,
            identity_status=IdentityStatus.ACTIVE,
            mapping=inserted_rec,
            attribution_eligibility=eligibility,
            created_identity=created_identity,
            renamed_username=False,
            conflict_detected=False,
        )

    # =========================================================================
    # 3. Confirm Mapping: HEURISTIC PERSON -> USER_CONFIRMED (never VERIFIED!)
    # =========================================================================
    def confirm_identity_mapping(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        request: ConfirmIdentityMappingRequest,
    ) -> ConfirmIdentityMappingResponse:
        map_uuid = _parse_uuid_or_none(mapping_id)
        if map_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.CONFIRM.value,
                "mapping_id": map_uuid,
                "reason": (request.reason or "").strip() or None,
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked_mapping, locked_identity, evidence_meta = self._lock_mapping_and_identity(
                    cur, workspace_id=access.workspace_id, mapping_id=map_uuid
                )
                self._assert_no_active_deletion_fence(
                    cur,
                    workspace_id=access.workspace_id,
                    identity_id=locked_mapping.identity_id,
                    evidence_metadata=evidence_meta,
                )

                replay_meta = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.CONFIRM,
                    mapping_id=map_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay_meta is not None:
                    eligibility = evaluate_identity_attribution(
                        cur, access.workspace_id, locked_mapping.identity_id
                    )
                    return ConfirmIdentityMappingResponse(
                        identity_id=locked_mapping.identity_id,
                        mapping=locked_mapping,
                        attribution_eligibility=eligibility,
                        idempotent_replay=True,
                    )

                ident_type = IdentityType(str(locked_identity["identity_type"]))
                ident_status = IdentityStatus(str(locked_identity["status"]))

                if ident_type is IdentityType.BOT:
                    raise IdentityError(
                        status_code=422,
                        code="BOT_IDENTITY_CONFIRMATION_FORBIDDEN",
                        message="Bot identities cannot be user-confirmed for human attribution.",
                    )
                if (
                    locked_mapping.status is ExternalIdentityMappingStatus.REVOKED
                    or ident_status is IdentityStatus.REVOKED
                ):
                    raise IdentityError(
                        status_code=409,
                        code="MAPPING_REVOKED",
                        message="Revoked identity mappings cannot be confirmed.",
                    )
                if (
                    locked_mapping.status is ExternalIdentityMappingStatus.CONFLICTED
                    or ident_status is IdentityStatus.AMBIGUOUS
                ):
                    raise IdentityError(
                        status_code=409,
                        code="MAPPING_CONFLICTED",
                        message="Conflicted or ambiguous identity mappings cannot be confirmed.",
                    )
                if locked_mapping.status is ExternalIdentityMappingStatus.VERIFIED:
                    raise IdentityError(
                        status_code=409,
                        code="MAPPING_ALREADY_VERIFIED",
                        message="Mapping is already provider-VERIFIED.",
                    )
                if locked_mapping.status is ExternalIdentityMappingStatus.USER_CONFIRMED:
                    raise IdentityError(
                        status_code=409,
                        code="MAPPING_ALREADY_CONFIRMED",
                        message="Mapping is already USER_CONFIRMED.",
                    )

                ts = self._now_fn()
                cur.execute(
                    f"""
                    UPDATE public.external_identity_mappings
                    SET verification_method = 'USER_CONFIRMATION',
                        status = 'USER_CONFIRMED',
                        verified_at = %s,
                        confidence_reason = 'USER_CONFIRMED_MAPPING'
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    RETURNING {_MAPPING_SELECT_COLUMNS}
                    """,
                    (ts, access.workspace_id, map_uuid),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None
                updated_mapping = _row_to_mapping_record(updated_row)

                action_meta = {
                    "request_fingerprint": fingerprint,
                    "identity_id": updated_mapping.identity_id,
                    "previous_status": locked_mapping.status.value,
                    "new_status": updated_mapping.status.value,
                    "verification_method": IdentityVerificationMethod.USER_CONFIRMATION.value,
                }
                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=map_uuid,
                    action=ReviewActionType.CONFIRM,
                    actor_user_id=access.user_id,
                    idempotency_key=idempotency_key,
                    reason=(request.reason or "").strip() or None,
                    action_metadata=action_meta,
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="IDENTITY_MAPPING_USER_CONFIRMED",
                    target_type="IDENTITY_MAPPING",
                    target_id=map_uuid,
                    redacted_metadata={
                        "identity_id": updated_mapping.identity_id,
                        "previous_status": locked_mapping.status.value,
                        "new_status": ExternalIdentityMappingStatus.USER_CONFIRMED.value,
                    },
                    created_at=ts,
                )

                _invoke_before_commit_hook(
                    self._before_commit_hook,
                    action="confirm_identity_mapping",
                    cur=cur,
                )
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence(
                    cur,
                    workspace_id=access.workspace_id,
                    identity_id=updated_mapping.identity_id,
                    evidence_metadata=evidence_meta,
                )

                eligibility = evaluate_identity_attribution(
                    cur, access.workspace_id, updated_mapping.identity_id
                )
                return ConfirmIdentityMappingResponse(
                    identity_id=updated_mapping.identity_id,
                    mapping=updated_mapping,
                    attribution_eligibility=eligibility,
                    idempotent_replay=False,
                )

    # =========================================================================
    # 4. Atomic Revocation & Conflict Invalidation Cascade (ID-05, T-401A)
    # =========================================================================
    def revoke_identity_mapping(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        request: RevokeIdentityMappingRequest,
    ) -> RevokeIdentityMappingResponse:
        return self._invalidate_identity_mapping(
            access,
            mapping_id=mapping_id,
            idempotency_key=idempotency_key,
            reason=request.reason,
            target_status=ExternalIdentityMappingStatus.REVOKED,
            confidence_reason="REVOKED_BY_USER",
            hook_action="revoke_identity_mapping",
        )

    def mark_identity_mapping_conflicted(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        reason: str,
        confidence_reason: str = "PROVIDER_IDENTITY_CONFLICT",
    ) -> RevokeIdentityMappingResponse:
        cleaned_reason = reason.strip()
        if not cleaned_reason:
            raise IdentityError(
                status_code=422,
                code="INVALID_CONFLICT_REASON",
                message="reason must not be blank.",
            )
        return self._invalidate_identity_mapping(
            access,
            mapping_id=mapping_id,
            idempotency_key=idempotency_key,
            reason=cleaned_reason,
            target_status=ExternalIdentityMappingStatus.CONFLICTED,
            confidence_reason=confidence_reason,
            hook_action="mark_identity_mapping_conflicted",
        )

    def _invalidate_identity_mapping(
        self,
        access: WorkspaceAccessContext,
        *,
        mapping_id: str,
        idempotency_key: str,
        reason: str,
        target_status: ExternalIdentityMappingStatus,
        confidence_reason: str,
        hook_action: str,
    ) -> RevokeIdentityMappingResponse:
        map_uuid = _parse_uuid_or_none(mapping_id)
        if map_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.REJECT.value,
                "target_status": target_status.value,
                "mapping_id": map_uuid,
                "reason": reason,
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked_mapping, _locked_identity, evidence_meta = self._lock_mapping_and_identity(
                    cur, workspace_id=access.workspace_id, mapping_id=map_uuid
                )
                self._assert_no_active_deletion_fence(
                    cur,
                    workspace_id=access.workspace_id,
                    identity_id=locked_mapping.identity_id,
                    evidence_metadata=evidence_meta,
                )

                replay_meta = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.REJECT,
                    mapping_id=map_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay_meta is not None:
                    counts_dict = dict(replay_meta.get("invalidated_counts") or {})
                    eligibility = evaluate_identity_attribution(
                        cur, access.workspace_id, locked_mapping.identity_id
                    )
                    return RevokeIdentityMappingResponse(
                        identity_id=locked_mapping.identity_id,
                        mapping=locked_mapping,
                        attribution_eligibility=eligibility,
                        invalidated_counts=IdentityInvalidationCounts(
                            contributions=int(counts_dict.get("contributions", 0)),
                            facts=int(counts_dict.get("facts", 0)),
                            claim_atoms=int(counts_dict.get("claim_atoms", 0)),
                            claims=int(counts_dict.get("claims", 0)),
                            exports=int(counts_dict.get("exports", 0)),
                        ),
                        idempotent_replay=True,
                    )

                if locked_mapping.status is ExternalIdentityMappingStatus.REVOKED:
                    raise IdentityError(
                        status_code=409,
                        code="MAPPING_ALREADY_REVOKED",
                        message="Identity mapping is already REVOKED.",
                    )
                if (
                    target_status is ExternalIdentityMappingStatus.CONFLICTED
                    and locked_mapping.status is ExternalIdentityMappingStatus.CONFLICTED
                ):
                    raise IdentityError(
                        status_code=409,
                        code="MAPPING_ALREADY_CONFLICTED",
                        message="Identity mapping is already CONFLICTED.",
                    )

                ts = self._now_fn()
                if target_status is ExternalIdentityMappingStatus.REVOKED:
                    cur.execute(
                        f"""
                        UPDATE public.external_identity_mappings
                        SET status = 'REVOKED',
                            revoked_at = %s,
                            confidence_reason = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        RETURNING {_MAPPING_SELECT_COLUMNS}
                        """,
                        (ts, confidence_reason, access.workspace_id, map_uuid),
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE public.external_identity_mappings
                        SET status = 'CONFLICTED',
                            verified_at = NULL,
                            confidence_reason = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        RETURNING {_MAPPING_SELECT_COLUMNS}
                        """,
                        (confidence_reason, access.workspace_id, map_uuid),
                    )
                updated_row = cur.fetchone()
                assert updated_row is not None
                updated_mapping = _row_to_mapping_record(updated_row)

                # Update parent identity status if needed before computing eligibility
                self._sync_parent_identity_status_after_invalidation(
                    cur,
                    workspace_id=access.workspace_id,
                    identity_id=updated_mapping.identity_id,
                    target_status=target_status,
                    now=ts,
                )

                eligibility = evaluate_identity_attribution(
                    cur, access.workspace_id, updated_mapping.identity_id
                )

                # Atomically cascade STALE / INVALIDATED across dependent records (ID-05)
                invalidated_counts = self._cascade_downstream_invalidation(
                    cur,
                    workspace_id=access.workspace_id,
                    identity_id=updated_mapping.identity_id,
                    eligibility=eligibility,
                    now=ts,
                )

                counts_payload = invalidated_counts.model_dump()
                action_meta = {
                    "request_fingerprint": fingerprint,
                    "identity_id": updated_mapping.identity_id,
                    "previous_status": locked_mapping.status.value,
                    "new_status": updated_mapping.status.value,
                    "invalidated_counts": counts_payload,
                }
                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=map_uuid,
                    action=ReviewActionType.REJECT,
                    actor_user_id=access.user_id,
                    idempotency_key=idempotency_key,
                    reason=reason,
                    action_metadata=action_meta,
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action=(
                        "IDENTITY_MAPPING_REVOKED"
                        if target_status is ExternalIdentityMappingStatus.REVOKED
                        else "IDENTITY_MAPPING_CONFLICTED"
                    ),
                    target_type="IDENTITY_MAPPING",
                    target_id=map_uuid,
                    redacted_metadata={
                        "identity_id": updated_mapping.identity_id,
                        "reason": reason,
                        "previous_status": locked_mapping.status.value,
                        "new_status": updated_mapping.status.value,
                        "invalidated_counts": counts_payload,
                    },
                    created_at=ts,
                )

                dedupe_key = (
                    f"identity_invalidation:{access.workspace_id}:{map_uuid}:"
                    f"{target_status.value.lower()}:{idempotency_key}"
                )
                outbox_payload = {
                    "identity_id": updated_mapping.identity_id,
                    "mapping_id": map_uuid,
                    "trigger": target_status.value,
                    "reason": reason,
                    "invalidated_counts": counts_payload,
                }
                assert_no_forbidden_raw_keys(outbox_payload, field_name="outbox_payload")
                cur.execute(
                    """
                    INSERT INTO public.outbox_events (
                        workspace_id, event_type, aggregate_type, aggregate_id,
                        dedupe_key, payload, status, created_at
                    ) VALUES (
                        %s::uuid, 'IDENTITY_ATTRIBUTION_INVALIDATED', 'IDENTITY', %s::uuid,
                        %s, %s, 'PENDING', %s
                    )
                    """,
                    (
                        access.workspace_id,
                        updated_mapping.identity_id,
                        dedupe_key,
                        Jsonb(outbox_payload),
                        ts,
                    ),
                )

                _invoke_before_commit_hook(
                    self._before_commit_hook,
                    action=hook_action,
                    cur=cur,
                )
                # Final TOCTOU re-verification right before commit!
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence(
                    cur,
                    workspace_id=access.workspace_id,
                    identity_id=updated_mapping.identity_id,
                    evidence_metadata=evidence_meta,
                )

                return RevokeIdentityMappingResponse(
                    identity_id=updated_mapping.identity_id,
                    mapping=updated_mapping,
                    attribution_eligibility=eligibility,
                    invalidated_counts=invalidated_counts,
                    idempotent_replay=False,
                )

    # =========================================================================
    # Internal Transactional Helpers
    # =========================================================================
    @staticmethod
    def _build_confidence_reason(
        *,
        desired_status: ExternalIdentityMappingStatus,
        verification_method: IdentityVerificationMethod,
        actor_role: GitHubActorRole,
        noreply: Any,
        detected_bot: bool,
    ) -> str:
        if detected_bot:
            return "BOT_ACTOR_NON_HUMAN"
        if desired_status is ExternalIdentityMappingStatus.VERIFIED:
            return (
                "OAUTH_PROVIDER_ACCOUNT_VERIFIED"
                if verification_method is IdentityVerificationMethod.OAUTH_ACCOUNT
                else "GITHUB_PROVIDER_USER_ID_VERIFIED"
            )
        if actor_role is GitHubActorRole.CO_AUTHOR:
            return "CO_AUTHOR_TRAILER_UNVERIFIED"
        if actor_role is GitHubActorRole.COMMITTER:
            return "COMMITTER_ROLE_HEURISTIC"
        if noreply is not None and not noreply.has_numeric_id:
            return "NOREPLY_LOGIN_WITHOUT_PROVIDER_ID"
        return "USERNAME_OR_EMAIL_HEURISTIC"

    def _record_conflicted_mapping_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        actor_user_id: str | None,
        identity_id: str,
        colliding_identity_ids: list[str],
        external_user_id: str | None,
        email_hash: str | None,
        username: str | None,
        verification_method: IdentityVerificationMethod,
        confidence_reason: str,
        evidence_meta: dict[str, Any],
        now: datetime,
    ) -> ResolveGitHubIdentityResult:
        # Mark all colliding active identities as AMBIGUOUS
        cur.execute(
            """
            UPDATE public.identities
            SET status = 'AMBIGUOUS',
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND id = ANY(%s::uuid[])
              AND status NOT IN ('REVOKED', 'MERGED')
            """,
            (now, workspace_id, colliding_identity_ids),
        )
        mapping_id = str(uuid.uuid4())
        conflict_meta = dict(evidence_meta)
        conflict_meta["colliding_identity_ids"] = colliding_identity_ids
        assert_no_forbidden_raw_keys(conflict_meta, field_name="evidence_metadata")

        cur.execute(
            f"""
            INSERT INTO public.external_identity_mappings (
                id, workspace_id, identity_id, provider,
                external_user_id, email_hash, username,
                verification_method, status, confidence_reason,
                evidence_metadata, verified_at, revoked_at, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, 'GITHUB',
                %s, %s, %s,
                %s, 'CONFLICTED', %s,
                %s, NULL, NULL, %s
            )
            RETURNING {_MAPPING_SELECT_COLUMNS}
            """,
            (
                mapping_id,
                workspace_id,
                identity_id,
                external_user_id,
                email_hash,
                username,
                verification_method.value,
                confidence_reason,
                Jsonb(conflict_meta),
                now,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        rec = _row_to_mapping_record(row)

        self._insert_audit_event(
            cur,
            workspace_id=workspace_id,
            actor_type=AuditActorType.USER if actor_user_id else AuditActorType.WORKER,
            actor_id=actor_user_id,
            action="IDENTITY_MAPPING_CONFLICTED",
            target_type="IDENTITY_MAPPING",
            target_id=mapping_id,
            redacted_metadata={
                "identity_id": identity_id,
                "colliding_identity_ids": colliding_identity_ids,
                "confidence_reason": confidence_reason,
            },
            created_at=now,
        )

        cur.execute(
            """
            SELECT identity_type, status
            FROM public.identities
            WHERE workspace_id = %s::uuid AND id = %s::uuid
            """,
            (workspace_id, identity_id),
        )
        id_row = cur.fetchone()
        assert id_row is not None
        eligibility = evaluate_identity_attribution(cur, workspace_id, identity_id)
        return ResolveGitHubIdentityResult(
            identity_id=identity_id,
            identity_type=IdentityType(str(id_row[0])),
            identity_status=IdentityStatus(str(id_row[1])),
            mapping=rec,
            attribution_eligibility=eligibility,
            created_identity=False,
            renamed_username=False,
            conflict_detected=True,
        )

    @staticmethod
    def _assert_live_workspace_membership(
        cur: psycopg.Cursor, access: WorkspaceAccessContext
    ) -> None:
        cur.execute(
            """
            SELECT 1
            FROM public.workspace_memberships
            WHERE workspace_id = %s::uuid
              AND user_id = %s::uuid
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (access.workspace_id, access.user_id),
        )
        if cur.fetchone() is None:
            raise WorkspaceMembershipDeniedError(
                "Workspace membership is no longer active."
            )

    @staticmethod
    def _lock_mapping_and_identity(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        mapping_id: str,
    ) -> tuple[ExternalIdentityMappingRecord, dict[str, Any], dict[str, Any]]:
        cur.execute(
            """
            SELECT
                eim.id::text,
                eim.identity_id::text,
                eim.provider,
                eim.external_user_id,
                eim.email_hash,
                eim.username,
                eim.verification_method,
                eim.status,
                eim.confidence_reason,
                eim.verified_at,
                eim.revoked_at,
                eim.created_at,
                eim.evidence_metadata,
                i.display_name,
                i.identity_type,
                i.canonical_user_id::text,
                i.status
            FROM public.external_identity_mappings eim
            INNER JOIN public.identities i
                ON i.workspace_id = eim.workspace_id
               AND i.id = eim.identity_id
            WHERE eim.workspace_id = %s::uuid
              AND eim.id = %s::uuid
            FOR UPDATE OF eim, i
            """,
            (workspace_id, mapping_id),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceResourceNotFoundError()

        mapping_rec = _row_to_mapping_record(row[:12])
        evidence_meta = dict(row[12] or {})
        identity_info = {
            "display_name": str(row[13]),
            "identity_type": str(row[14]),
            "canonical_user_id": str(row[15]) if row[15] is not None else None,
            "status": str(row[16]),
        }

        # Lock sibling mappings in deterministic UUID order to prevent confirm/revoke races
        cur.execute(
            """
            SELECT id
            FROM public.external_identity_mappings
            WHERE workspace_id = %s::uuid
              AND identity_id = %s::uuid
            ORDER BY id ASC
            FOR UPDATE
            """,
            (workspace_id, mapping_rec.identity_id),
        )
        cur.fetchall()

        return mapping_rec, identity_info, evidence_meta

    @staticmethod
    def _assert_no_active_deletion_fence(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        identity_id: str,
        evidence_metadata: dict[str, Any],
    ) -> None:
        resource_ids: list[str] = []
        raw_res = evidence_metadata.get("source_resource_id")
        parsed_res = _parse_uuid_or_none(raw_res if isinstance(raw_res, str) else None)
        if parsed_res is not None:
            resource_ids.append(parsed_res)

        cur.execute(
            """
            SELECT 1
            FROM public.deletion_jobs dj
            WHERE dj.workspace_id = %s::uuid
              AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL')
              AND (
                  (cardinality(%s::uuid[]) > 0 AND dj.source_resource_id = ANY(%s::uuid[]))
                  OR EXISTS (
                      SELECT 1
                      FROM public.source_objects so
                      INNER JOIN public.work_events we
                          ON we.workspace_id = so.workspace_id
                         AND we.primary_anchor_source_object_id = so.id
                      LEFT JOIN public.contributions co
                          ON co.workspace_id = we.workspace_id
                         AND co.event_id = we.id
                         AND co.identity_id = %s::uuid
                      LEFT JOIN public.facts f
                          ON f.workspace_id = we.workspace_id
                         AND f.event_id = we.id
                         AND f.subject_identity_id = %s::uuid
                      WHERE so.workspace_id = dj.workspace_id
                        AND so.source_resource_id = dj.source_resource_id
                        AND (co.id IS NOT NULL OR f.id IS NOT NULL)
                  )
              )
            LIMIT 1
            """,
            (workspace_id, resource_ids, resource_ids, identity_id, identity_id),
        )
        if cur.fetchone() is not None:
            raise IdentityError(
                status_code=409,
                code="DELETION_PENDING",
                message="Identity mutation blocked while a linked source resource deletion is active.",
            )

    @staticmethod
    def _check_idempotency_replay(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        action: ReviewActionType,
        mapping_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT action_metadata
            FROM public.review_actions
            WHERE workspace_id = %s::uuid
              AND action = %s
              AND target_type = %s
              AND target_id = %s::uuid
              AND idempotency_key = %s
            LIMIT 1
            """,
            (
                workspace_id,
                action.value,
                ReviewTargetType.IDENTITY_MAPPING.value,
                mapping_id,
                idempotency_key,
            ),
        )
        row = cur.fetchone()
        if row is None:
            return None
        meta = dict(row[0] or {})
        if meta.get("request_fingerprint") != fingerprint:
            raise IdentityError(
                status_code=409,
                code="IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key was already used with a different request payload.",
            )
        return meta

    @staticmethod
    def _sync_parent_identity_status_after_invalidation(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        identity_id: str,
        target_status: ExternalIdentityMappingStatus,
        now: datetime,
    ) -> None:
        cur.execute(
            """
            SELECT status
            FROM public.external_identity_mappings
            WHERE workspace_id = %s::uuid
              AND identity_id = %s::uuid
            """,
            (workspace_id, identity_id),
        )
        statuses = [ExternalIdentityMappingStatus(str(r[0])) for r in cur.fetchall()]
        has_conflicted = any(s is ExternalIdentityMappingStatus.CONFLICTED for s in statuses)
        has_non_revoked = any(s is not ExternalIdentityMappingStatus.REVOKED for s in statuses)

        if target_status is ExternalIdentityMappingStatus.CONFLICTED or has_conflicted:
            cur.execute(
                """
                UPDATE public.identities
                SET status = 'AMBIGUOUS',
                    updated_at = %s
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                  AND status NOT IN ('REVOKED', 'MERGED')
                """,
                (now, workspace_id, identity_id),
            )
        elif not has_non_revoked:
            cur.execute(
                """
                UPDATE public.identities
                SET status = 'REVOKED',
                    updated_at = %s
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                  AND status <> 'MERGED'
                """,
                (now, workspace_id, identity_id),
            )

    @staticmethod
    def _cascade_downstream_invalidation(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        identity_id: str,
        eligibility: IdentityAttributionEligibility,
        now: datetime,
    ) -> IdentityInvalidationCounts:
        # Determine which downstream backing statuses lost eligibility
        lost_source = not eligibility.can_support_source_verified
        lost_user = not eligibility.can_support_user_confirmed

        if not lost_source and not lost_user:
            return IdentityInvalidationCounts()

        contrib_statuses: list[str] = []
        fact_statuses: list[str] = []
        atom_statuses: list[str] = []
        if lost_source:
            contrib_statuses.append("SOURCE_SUPPORTED")
            fact_statuses.append("SUPPORTED")
            atom_statuses.append("SUPPORTED")
        if lost_user:
            contrib_statuses.append("USER_CONFIRMED")
            fact_statuses.append("USER_CONFIRMED")
            atom_statuses.append("USER_CONFIRMED")

        # 1. Contributions -> STALE
        cur.execute(
            """
            UPDATE public.contributions
            SET status = 'STALE'
            WHERE workspace_id = %s::uuid
              AND identity_id = %s::uuid
              AND status = ANY(%s::text[])
            RETURNING id::text, event_id::text
            """,
            (workspace_id, identity_id, contrib_statuses),
        )
        contrib_rows = cur.fetchall()
        invalidated_contrib_ids = [str(r[0]) for r in contrib_rows]
        invalidated_contrib_event_ids = sorted({str(r[1]) for r in contrib_rows})

        # 2. Facts -> STALE (subject_identity_id = identity_id OR linked via active contribution_support)
        cur.execute(
            """
            UPDATE public.facts
            SET status = 'STALE',
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND status = ANY(%s::text[])
              AND (
                  subject_identity_id = %s::uuid
                  OR (
                      cardinality(%s::uuid[]) > 0
                      AND id IN (
                          SELECT cs.fact_id
                          FROM public.contribution_support cs
                          WHERE cs.workspace_id = %s::uuid
                            AND cs.contribution_id = ANY(%s::uuid[])
                            AND cs.status = 'ACTIVE'
                      )
                  )
              )
            RETURNING id::text
            """,
            (
                now,
                workspace_id,
                fact_statuses,
                identity_id,
                invalidated_contrib_ids,
                workspace_id,
                invalidated_contrib_ids,
            ),
        )
        invalidated_fact_ids = [str(r[0]) for r in cur.fetchall()]

        # 3. Claim atoms -> STALE (dependent on invalidated facts OR ROLE atoms on invalidated contribution events)
        invalidated_atom_ids: list[str] = []
        affected_claim_ids: set[str] = set()
        if invalidated_fact_ids or invalidated_contrib_event_ids:
            cur.execute(
                """
                UPDATE public.claim_atoms ca
                SET status = 'STALE'
                WHERE ca.workspace_id = %s::uuid
                  AND ca.status = ANY(%s::text[])
                  AND (
                      (
                          cardinality(%s::uuid[]) > 0
                          AND EXISTS (
                              SELECT 1
                              FROM public.claim_atom_support cas
                              WHERE cas.workspace_id = ca.workspace_id
                                AND cas.claim_atom_id = ca.id
                                AND cas.fact_id = ANY(%s::uuid[])
                                AND cas.status = 'ACTIVE'
                          )
                      )
                      OR (
                          ca.atom_type = 'ROLE'
                          AND cardinality(%s::uuid[]) > 0
                          AND EXISTS (
                              SELECT 1
                              FROM public.claims c
                              WHERE c.workspace_id = ca.workspace_id
                                AND c.id = ca.claim_id
                                AND c.event_id = ANY(%s::uuid[])
                          )
                      )
                  )
                RETURNING ca.id::text, ca.claim_id::text
                """,
                (
                    workspace_id,
                    atom_statuses,
                    invalidated_fact_ids,
                    invalidated_fact_ids,
                    invalidated_contrib_event_ids,
                    invalidated_contrib_event_ids,
                ),
            )
            for atom_id, claim_id in cur.fetchall():
                invalidated_atom_ids.append(str(atom_id))
                affected_claim_ids.add(str(claim_id))

        # 4. Parent claims in (APPROVED, SUPPORT_VALIDATED) -> STALE
        invalidated_claim_ids: list[str] = []
        if affected_claim_ids:
            cur.execute(
                """
                UPDATE public.claims
                SET status = 'STALE',
                    updated_at = %s
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                  AND status IN ('APPROVED', 'SUPPORT_VALIDATED')
                RETURNING id::text
                """,
                (now, workspace_id, sorted(affected_claim_ids)),
            )
            invalidated_claim_ids = [str(r[0]) for r in cur.fetchall()]

        # 5. Dependent exports in (PENDING, READY) -> INVALIDATED
        invalidated_export_ids: list[str] = []
        if invalidated_claim_ids:
            cur.execute("SELECT to_regclass('public.exports') IS NOT NULL")
            has_exports_row = cur.fetchone()
            if has_exports_row and bool(has_exports_row[0]):
                cur.execute(
                    """
                    UPDATE public.exports
                    SET status = 'INVALIDATED',
                        invalidation_reason = 'IDENTITY_MAPPING_INVALIDATED',
                        invalidated_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND status IN ('PENDING', 'READY')
                      AND id IN (
                          SELECT ec.export_id
                          FROM public.export_claims ec
                          WHERE ec.workspace_id = %s::uuid
                            AND ec.claim_id = ANY(%s::uuid[])
                      )
                    RETURNING id::text
                    """,
                    (
                        now,
                        now,
                        workspace_id,
                        workspace_id,
                        invalidated_claim_ids,
                    ),
                )
                invalidated_export_ids = [str(r[0]) for r in cur.fetchall()]

        return IdentityInvalidationCounts(
            contributions=len(invalidated_contrib_ids),
            facts=len(invalidated_fact_ids),
            claim_atoms=len(invalidated_atom_ids),
            claims=len(invalidated_claim_ids),
            exports=len(invalidated_export_ids),
        )

    @staticmethod
    def _insert_review_action(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        target_id: str,
        action: ReviewActionType,
        actor_user_id: str | None,
        idempotency_key: str,
        reason: str | None,
        action_metadata: dict[str, Any],
        created_at: datetime,
    ) -> str:
        assert_no_forbidden_raw_keys(action_metadata, field_name="action_metadata")
        review_action_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO public.review_actions (
                id, workspace_id, target_type, target_id, action,
                actor_user_id, from_version, to_version, idempotency_key,
                reason, action_metadata, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid, %s,
                %s::uuid, NULL, NULL, %s,
                %s, %s, %s
            )
            """,
            (
                review_action_id,
                workspace_id,
                ReviewTargetType.IDENTITY_MAPPING.value,
                target_id,
                action.value,
                _parse_uuid_or_none(actor_user_id),
                idempotency_key,
                reason,
                Jsonb(action_metadata),
                created_at,
            ),
        )
        return review_action_id

    @staticmethod
    def _insert_audit_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        action: str,
        target_type: str,
        target_id: str,
        redacted_metadata: dict[str, Any],
        created_at: datetime,
    ) -> str:
        assert_no_forbidden_raw_keys(redacted_metadata, field_name="redacted_metadata")
        audit_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO public.audit_events (
                id, workspace_id, actor_type, actor_id,
                action, target_type, target_id, redacted_metadata, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid,
                %s, %s, %s::uuid, %s, %s
            )
            """,
            (
                audit_id,
                workspace_id,
                actor_type.value,
                _parse_uuid_or_none(actor_id),
                action,
                target_type,
                target_id,
                Jsonb(redacted_metadata),
                created_at,
            ),
        )
        return audit_id


__all__ = [
    "BeforeCommitHook",
    "ConnectionFactory",
    "PostgresIdentityService",
    "evaluate_identity_attribution",
]
