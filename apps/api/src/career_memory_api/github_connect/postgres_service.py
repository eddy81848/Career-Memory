"""PostgreSQL 18 service for M2-T201 GitHub OAuth Connect, Allowlist & Policy Transitions."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from career_memory_api.auth.jwt_verifier import (
    LiveSessionStatus,
    LiveSessionVerifierPort,
    VerifiedJwtClaims,
)
from career_memory_api.auth.oauth import (
    OAuthPersistenceFailedError,
    OAuthProvider,
    OAuthStateInvalidOrReplayedError,
    OAuthStateRecord,
    OAuthTokenExchangeError,
    OrphanSecretCleaner,
    PostgresOAuthBoundaryStore,
    SecretStorePort,
    derive_pkce_s256_challenge,
    hash_oauth_state,
)
from career_memory_api.auth.postgres_guard import (
    WorkspaceRole,
    workspace_scoped_transaction,
)
from career_memory_api.auth.redaction import redact_text
from career_memory_api.auth.return_path import validate_return_path
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    SessionTerminatedError,
    WorkspaceAccessContext,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.github_connect.models import (
    ALL_BLOCKED_OPERATIONS,
    REQUIRED_GITHUB_OAUTH_SCOPE,
    GitHubConnectError,
    GitHubInspectionOutcome,
    GitHubOAuthCallbackResult,
    GitHubOAuthStartResult,
    GitHubRepositoryInspection,
    PolicyDecision,
    PolicyGuardedOperation,
    PolicyGuidanceCode,
    PolicyTransitionKind,
    PolicyVerificationResult,
    ResolvedGitHubCredential,
    SourceAccountDisconnectResult,
    SourceAccountView,
    SourceResourcePolicyClearance,
    SourceResourceRecord,
    parse_repository_full_name,
    sanitize_audit_metadata,
    validate_minimum_github_scopes,
)
from career_memory_api.github_connect.ports import GitHubCredentialProviderPort
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.jobs.postgres_runtime import is_deletion_fence_active
from career_memory_api.sync_runs.models import parse_idempotency_key
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

logger = logging.getLogger(__name__)

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]

_PKCE_NAMESPACE = "github-oauth-pkce"
_TOKEN_NAMESPACE = "github-oauth-token"
_MUTATION_ALLOWED_ROLES = frozenset({WorkspaceRole.OWNER.value, WorkspaceRole.MEMBER.value})
_READ_ALLOWED_ROLES = frozenset(
    {WorkspaceRole.OWNER.value, WorkspaceRole.MEMBER.value, WorkspaceRole.VIEWER.value}
)
_DEFAULT_POLICY_VERSION = "v1"


def _try_normalize_uuid(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def _require_live_membership(
    cur: psycopg.Cursor,
    access: WorkspaceAccessContext,
    *,
    allowed_roles: frozenset[str],
) -> str:
    """Verify current unrevoked Workspace membership and role inside the active transaction."""
    cur.execute(
        """
        SELECT role
        FROM public.workspace_memberships
        WHERE workspace_id = %s::uuid
          AND user_id = %s::uuid
          AND revoked_at IS NULL
        """,
        (access.workspace_id, access.user_id),
    )
    row = cur.fetchone()
    if row is None:
        raise WorkspaceMembershipDeniedError(
            "Access to the selected workspace has been revoked."
        )
    role = str(row[0])
    if role not in allowed_roles:
        raise WorkspaceMembershipDeniedError(
            "Workspace membership role does not permit this operation."
        )
    return role


def _insert_audit_event(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    actor_type: AuditActorType,
    actor_id: str | None,
    action: str,
    target_type: str,
    target_id: str,
    metadata: Mapping[str, Any],
    created_at: datetime,
) -> None:
    safe_meta = sanitize_audit_metadata(metadata)
    cur.execute(
        """
        INSERT INTO public.audit_events (
            id, workspace_id, actor_type, actor_id,
            action, target_type, target_id, redacted_metadata, created_at
        )
        VALUES (%s::uuid, %s::uuid, %s, %s::uuid, %s, %s, %s::uuid, %s::jsonb, %s)
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            actor_type.value,
            actor_id,
            action,
            target_type,
            target_id,
            json.dumps(safe_meta, sort_keys=True),
            created_at,
        ),
    )


def _row_to_account_view(row: tuple[Any, ...]) -> SourceAccountView:
    return SourceAccountView(
        id=str(row[0]),
        workspace_id=str(row[1]),
        provider=str(row[2]),
        external_account_id=str(row[3]),
        scopes=tuple(row[4] or ()),
        status=str(row[5]),
        created_at=row[6],
        revoked_at=row[7],
    )


def _derive_resource_guidance(
    *,
    resource_status: str,
    account_status: str,
) -> PolicyGuidanceCode | None:
    if account_status == "REVOKED":
        return PolicyGuidanceCode.REAUTH_REQUIRED
    if resource_status == "POLICY_REVIEW_REQUIRED":
        return PolicyGuidanceCode.POLICY_REVIEW_REQUIRED
    return None


def _row_to_resource(
    row: tuple[Any, ...],
    *,
    guidance_code: PolicyGuidanceCode | None = None,
) -> SourceResourceRecord:
    return SourceResourceRecord(
        id=str(row[0]),
        workspace_id=str(row[1]),
        source_account_id=str(row[2]),
        provider_resource_id=str(row[3]),
        resource_type=str(row[4]),
        name=str(row[5]),
        canonical_url=str(row[6]),
        visibility=str(row[7]),
        processing_mode=str(row[8]),
        raw_storage_mode=str(row[9]),
        raw_retention_seconds=int(row[10]) if row[10] is not None else None,
        status=str(row[11]),
        policy_version=str(row[12]),
        consent_version=str(row[13]) if row[13] is not None else None,
        last_visibility_checked_at=row[14],
        created_at=row[15],
        guidance_code=guidance_code,
    )


_SOURCE_RESOURCE_SELECT_COLUMNS = """
    sr.id::text,
    sr.workspace_id::text,
    sr.source_account_id::text,
    sr.provider_resource_id,
    sr.resource_type,
    sr.name,
    sr.canonical_url,
    sr.visibility,
    sr.processing_mode,
    sr.raw_storage_mode,
    sr.raw_retention_seconds,
    sr.status,
    sr.policy_version,
    sr.consent_version,
    sr.last_visibility_checked_at,
    sr.created_at
"""


def _cancel_active_work_for_resources(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    source_resource_ids: tuple[str, ...],
    error_code: str,
    blocked_processing_status: str,
    now: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Cancel active jobs and sync_runs for the given resources without deleting provenance."""
    if not source_resource_ids:
        return (), ()

    # Cancel PENDING / RUNNING / RETRY_WAIT jobs attached to any sync_run of these resources
    # or referencing the resource in input_manifest.
    cur.execute(
        """
        UPDATE public.jobs j
        SET status = 'CANCELED',
            lease_owner = NULL,
            lease_token = NULL,
            lease_until = NULL,
            error_class = 'CANCELED_BY_FENCE',
            error_code = %s,
            updated_at = %s,
            completed_at = %s
        WHERE j.workspace_id = %s::uuid
          AND j.status IN ('PENDING', 'RUNNING', 'RETRY_WAIT')
          AND (
              j.target_sync_run_id IN (
                  SELECT sr.id
                  FROM public.sync_runs sr
                  WHERE sr.workspace_id = %s::uuid
                    AND sr.source_resource_id = ANY(%s::uuid[])
              )
              OR (j.input_manifest ->> 'source_resource_id') = ANY(%s::text[])
          )
        RETURNING j.id::text
        """,
        (
            error_code,
            now,
            now,
            workspace_id,
            workspace_id,
            list(source_resource_ids),
            list(source_resource_ids),
        ),
    )
    canceled_job_ids = tuple(str(r[0]) for r in cur.fetchall())

    # Cancel active PENDING / RUNNING sync_runs (strictly obeying 0040 CHECK invariants).
    cur.execute(
        """
        UPDATE public.sync_runs
        SET status = 'CANCELED',
            import_ready = false,
            processing_status = 'NOT_STARTED',
            retry_allowed = false,
            failure_reason_code = NULL,
            source_drift = false,
            run_version = run_version + 1,
            last_terminal_transition_at = %s,
            updated_at = %s
        WHERE workspace_id = %s::uuid
          AND source_resource_id = ANY(%s::uuid[])
          AND status IN ('PENDING', 'RUNNING')
        RETURNING id::text
        """,
        (now, now, workspace_id, list(source_resource_ids)),
    )
    canceled_sync_run_ids = tuple(str(r[0]) for r in cur.fetchall())

    # Block completed SUCCESS sync_runs so downstream AI/export pipelines see the policy block.
    cur.execute(
        """
        UPDATE public.sync_runs
        SET processing_status = %s,
            run_version = run_version + 1,
            updated_at = %s
        WHERE workspace_id = %s::uuid
          AND source_resource_id = ANY(%s::uuid[])
          AND status = 'SUCCESS'
          AND processing_status NOT IN ('BLOCKED_POLICY', 'BLOCKED_SOURCE')
        """,
        (blocked_processing_status, now, workspace_id, list(source_resource_ids)),
    )

    return canceled_job_ids, canceled_sync_run_ids


def _block_provenance_for_visibility_policy(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    source_resource_id: str,
) -> None:
    """Mark active objects/content_units as policy-blocked without creating tombstones."""
    cur.execute(
        """
        UPDATE public.source_objects
        SET lifecycle_state = 'POLICY_REVIEW_REQUIRED'
        WHERE workspace_id = %s::uuid
          AND source_resource_id = %s::uuid
          AND lifecycle_state = 'ACTIVE'
        """,
        (workspace_id, source_resource_id),
    )
    cur.execute(
        """
        UPDATE public.content_units cu
        SET policy_state = 'BLOCKED'
        WHERE cu.workspace_id = %s::uuid
          AND cu.policy_state = 'ACTIVE'
          AND cu.source_object_revision_id IN (
              SELECT rev.id
              FROM public.source_object_revisions rev
              JOIN public.source_objects obj
                ON obj.workspace_id = rev.workspace_id
               AND obj.id = rev.source_object_id
              WHERE rev.workspace_id = %s::uuid
                AND obj.source_resource_id = %s::uuid
          )
        """,
        (workspace_id, workspace_id, source_resource_id),
    )


class PostgresGitHubConnectService:
    """Workspace-scoped PostgreSQL 18 service for GitHub OAuth Connect & Policy Guards."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory,
        credential_provider: GitHubCredentialProviderPort,
        secret_store: SecretStorePort,
        allowed_redirect_uris: tuple[str, ...],
        live_session_verifier: LiveSessionVerifierPort | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_injector: Callable[[str], None] | None = None,
        default_policy_version: str = _DEFAULT_POLICY_VERSION,
        max_active_resources_per_workspace: int | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._credential_provider = credential_provider
        self._secret_store = secret_store
        self._allowed_redirect_uris = frozenset(
            uri.strip() for uri in allowed_redirect_uris if uri.strip()
        )
        if not self._allowed_redirect_uris:
            raise ValueError("At least one allowed redirect_uri must be configured.")
        self._live_session_verifier = live_session_verifier
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_injector = fault_injector
        self._default_policy_version = default_policy_version
        self._max_active_resources_per_workspace = max_active_resources_per_workspace

    def _maybe_inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _validate_redirect_uri(self, redirect_uri: str) -> str:
        if not isinstance(redirect_uri, str) or not redirect_uri.strip():
            raise GitHubConnectError(
                status_code=400,
                code="INVALID_REDIRECT_URI",
                message="redirect_uri is required.",
            )
        normalized = redirect_uri.strip()
        if normalized not in self._allowed_redirect_uris:
            raise GitHubConnectError(
                status_code=400,
                code="INVALID_REDIRECT_URI",
                message="The requested redirect_uri is not on the server allowlist.",
            )
        return normalized

    def _enforce_role_and_live_session(
        self,
        access: WorkspaceAccessContext,
        *,
        allowed_roles: frozenset[str],
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> None:
        if access.role not in allowed_roles:
            raise WorkspaceMembershipDeniedError(
                "Workspace role does not permit this operation."
            )
        if self._live_session_verifier is not None:
            if verified_claims is None:
                raise AccessVerificationUnavailableError(
                    "Verified JWT claims are required for live session verification."
                )
            try:
                status = self._live_session_verifier.verify_live_session(verified_claims)
            except (SessionTerminatedError, AccessVerificationUnavailableError):
                raise
            except Exception as exc:  # noqa: BLE001
                raise AccessVerificationUnavailableError(
                    "Live session verification failed unexpectedly."
                ) from exc
            if status is LiveSessionStatus.SESSION_TERMINATED:
                raise SessionTerminatedError("The session has ended.")
            if status is not LiveSessionStatus.ACTIVE:
                raise AccessVerificationUnavailableError(
                    "Live session verification returned an unknown state."
                )

    def _best_effort_revoke_grant(self, access_token: str) -> bool:
        try:
            revoke_res = self._credential_provider.revoke_grant(access_token=access_token)
            return bool(revoke_res.revoked)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GitHub OAuth grant revocation failed: %s",
                redact_text(type(exc).__name__),
            )
            return False

    def _cleanup_orphan_secret(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        token_secret_ref: str,
        failure_reason: str,
        metadata: Mapping[str, Any],
    ) -> Any:
        with self._connection_factory() as cleanup_conn:
            boundary_store = PostgresOAuthBoundaryStore(cleanup_conn)
            cleaner = OrphanSecretCleaner(
                secret_store=self._secret_store,
                cleanup_store=boundary_store,
                clock=self._clock,
            )
            return cleaner.cleanup_orphan_secret(
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                provider=OAuthProvider.GITHUB,
                token_secret_ref=token_secret_ref,
                failure_reason=failure_reason,
                metadata=sanitize_audit_metadata(metadata),
            )

    def start_oauth(
        self,
        access: WorkspaceAccessContext,
        *,
        return_path: str,
        redirect_uri: str,
        ttl_seconds: int = 600,
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> GitHubOAuthStartResult:
        """Create a single-use hashed OAuth state + PKCE verifier ref with ``read:user`` only."""
        self._enforce_role_and_live_session(
            access,
            allowed_roles=_MUTATION_ALLOWED_ROLES,
            verified_claims=verified_claims,
        )
        safe_return_path = validate_return_path(return_path)
        safe_redirect_uri = self._validate_redirect_uri(redirect_uri)
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_REQUEST_BODY",
                message="ttl_seconds must be between 1 and 3600.",
            )

        now = self._clock()
        expires_at = now + timedelta(seconds=ttl_seconds)
        raw_state = secrets.token_urlsafe(32)
        state_hash = hash_oauth_state(raw_state)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = derive_pkce_s256_challenge(code_verifier)

        verifier_ref = self._secret_store.store_secret(
            _PKCE_NAMESPACE,
            code_verifier,
            metadata={"workspace_id": access.workspace_id, "provider": "GITHUB"},
        )
        state_record = OAuthStateRecord(
            state_hash=state_hash,
            workspace_id=access.workspace_id,
            actor_user_id=access.user_id,
            provider=OAuthProvider.GITHUB,
            pkce_code_challenge=code_challenge,
            pkce_code_verifier_ref=verifier_ref,
            return_path=safe_return_path,
            expires_at=expires_at,
            created_at=now,
        )

        try:
            with self._connection_factory() as conn:
                with workspace_scoped_transaction(conn, access) as cur:
                    _require_live_membership(
                        cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                    )
                    cur.execute(
                        """
                        INSERT INTO public.oauth_connect_states (
                            state_hash,
                            workspace_id,
                            actor_user_id,
                            provider,
                            pkce_code_challenge,
                            pkce_code_verifier_ref,
                            return_path,
                            expires_at,
                            created_at
                        )
                        VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            state_record.state_hash,
                            access.workspace_id,
                            access.user_id,
                            state_record.provider.value,
                            state_record.pkce_code_challenge,
                            state_record.pkce_code_verifier_ref,
                            state_record.return_path,
                            state_record.expires_at,
                            state_record.created_at,
                        ),
                    )
                    _insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.USER,
                        actor_id=access.user_id,
                        action="GITHUB_OAUTH_STARTED",
                        target_type="WORKSPACE",
                        target_id=access.workspace_id,
                        metadata={
                            "provider": "GITHUB",
                            "credential_mode": self._credential_provider.credential_mode.value,
                            "requested_scopes": [REQUIRED_GITHUB_OAUTH_SCOPE],
                            "state_hash": state_hash,
                            "return_path": safe_return_path,
                        },
                        created_at=now,
                    )
        except Exception:
            try:
                self._secret_store.delete_secret(verifier_ref)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Unable to delete PKCE verifier after start_oauth failure: ref=%s",
                    verifier_ref,
                )
            raise

        authorization_url = self._credential_provider.build_authorization_url(
            state=raw_state,
            redirect_uri=safe_redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        return GitHubOAuthStartResult(
            provider="GITHUB",
            credential_mode=self._credential_provider.credential_mode,
            authorization_url=authorization_url,
            requested_scopes=(REQUIRED_GITHUB_OAUTH_SCOPE,),
            state=raw_state,
            pkce_code_challenge=code_challenge,
            return_path=safe_return_path,
            expires_at=expires_at,
        )

    def _lookup_idempotent_callback_replay(
        self,
        access: WorkspaceAccessContext,
        *,
        state_hash: str,
        idempotency_key: str,
    ) -> GitHubOAuthCallbackResult | None:
        """Check whether an already-consumed state matches an idempotent retry or conflict."""
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                # Detect Idempotency-Key reused against a DIFFERENT state_hash.
                cur.execute(
                    """
                    SELECT state_hash, return_path
                    FROM public.oauth_connect_states
                    WHERE workspace_id = %s::uuid
                      AND actor_user_id = %s::uuid
                      AND provider = 'GITHUB'
                      AND consumed_request_id = %s
                    """,
                    (access.workspace_id, access.user_id, idempotency_key),
                )
                by_idem = cur.fetchone()
                if by_idem is None:
                    return None

                recorded_state_hash, recorded_return_path = str(by_idem[0]), str(by_idem[1])
                if recorded_state_hash != state_hash:
                    raise GitHubConnectError(
                        status_code=409,
                        code="IDEMPOTENCY_CONFLICT",
                        message="Idempotency-Key was already used for a different OAuth state.",
                    )

                # Replay is allowed ONLY if the original callback actually committed
                # a GITHUB_OAUTH_CONNECTED audit record for this exact (state_hash, idempotency_key).
                cur.execute(
                    """
                    SELECT target_id::text, redacted_metadata
                    FROM public.audit_events
                    WHERE workspace_id = %s::uuid
                      AND action = 'GITHUB_OAUTH_CONNECTED'
                      AND target_type = 'SOURCE_ACCOUNT'
                      AND redacted_metadata ->> 'idempotency_key' = %s
                      AND redacted_metadata ->> 'state_hash' = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (access.workspace_id, idempotency_key, state_hash),
                )
                audit_row = cur.fetchone()
                if audit_row is None:
                    return None

                account_id = str(audit_row[0])
                audit_meta = audit_row[1] if isinstance(audit_row[1], dict) else {}
                cur.execute(
                    """
                    SELECT
                        id::text, workspace_id::text, provider, external_account_id,
                        scopes, status, created_at, revoked_at
                    FROM public.source_accounts
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (access.workspace_id, account_id),
                )
                acct_row = cur.fetchone()
                if acct_row is None:
                    return None

                resource_record: SourceResourceRecord | None = None
                resource_id = audit_meta.get("source_resource_id")
                if isinstance(resource_id, str) and _try_normalize_uuid(resource_id):
                    cur.execute(
                        f"""
                        SELECT {_SOURCE_RESOURCE_SELECT_COLUMNS}
                        FROM public.source_resources sr
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.id = %s::uuid
                        """,
                        (access.workspace_id, resource_id),
                    )
                    res_row = cur.fetchone()
                    if res_row is not None:
                        acct_view = _row_to_account_view(acct_row)
                        resource_record = _row_to_resource(
                            res_row,
                            guidance_code=_derive_resource_guidance(
                                resource_status=str(res_row[11]),
                                account_status=acct_view.status,
                            ),
                        )

                return GitHubOAuthCallbackResult(
                    source_account=_row_to_account_view(acct_row),
                    source_resource=resource_record,
                    return_path=validate_return_path(recorded_return_path),
                    idempotent_replay=True,
                )

    def complete_oauth_callback(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        state: str,
        code: str | None,
        redirect_uri: str,
        repository_full_name: str | None = None,
        provider_error: str | None = None,
        policy_version: str | None = None,
        consent_version: str | None = None,
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> GitHubOAuthCallbackResult:
        """Atomically consume 1-time state, enforce ``read:user`` scope, and persist account.

        Guarantees AUTH-02 invariants:
        - Single-use ``oauth_connect_states`` consumption prevents concurrent/replay duplicates.
        - Excessive scopes immediately revoke the token at GitHub with 0 DB account/resource writes.
        - DB persistence failures or mid-callback membership revocation roll back cleanly,
          revoke the token at GitHub, delete the secret, and record ``orphan_secret_cleanups``.
        """
        self._enforce_role_and_live_session(
            access,
            allowed_roles=_MUTATION_ALLOWED_ROLES,
            verified_claims=verified_claims,
        )
        try:
            normalized_idem_key = parse_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise GitHubConnectError(
                status_code=422,
                code="INVALID_IDEMPOTENCY_KEY",
                message=str(exc),
            ) from exc

        safe_redirect_uri = self._validate_redirect_uri(redirect_uri)
        if not isinstance(state, str) or not state.strip():
            raise OAuthStateInvalidOrReplayedError("OAuth state is missing.")

        parsed_repo_pair: tuple[str, str] | None = None
        if repository_full_name is not None:
            parsed_repo_pair = parse_repository_full_name(repository_full_name)

        now = self._clock()
        state_hash = hash_oauth_state(state.strip())

        # Step 1: Atomically claim single-use state row (blocks concurrent replay before GitHub I/O).
        consumed: OAuthStateRecord | None = None
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                # Guard against Idempotency-Key conflict with another already-consumed state.
                cur.execute(
                    """
                    SELECT state_hash
                    FROM public.oauth_connect_states
                    WHERE workspace_id = %s::uuid
                      AND actor_user_id = %s::uuid
                      AND provider = 'GITHUB'
                      AND consumed_request_id = %s
                      AND state_hash <> %s
                    """,
                    (access.workspace_id, access.user_id, normalized_idem_key, state_hash),
                )
                if cur.fetchone() is not None:
                    raise GitHubConnectError(
                        status_code=409,
                        code="IDEMPOTENCY_CONFLICT",
                        message="Idempotency-Key was already used for a different OAuth state.",
                    )

                cur.execute(
                    """
                    UPDATE public.oauth_connect_states
                    SET consumed_at = %s,
                        consumed_request_id = %s
                    WHERE state_hash = %s
                      AND workspace_id = %s::uuid
                      AND actor_user_id = %s::uuid
                      AND provider = 'GITHUB'
                      AND consumed_at IS NULL
                      AND expires_at > %s
                    RETURNING
                        state_hash,
                        workspace_id::text,
                        actor_user_id::text,
                        provider,
                        pkce_code_challenge,
                        pkce_code_verifier_ref,
                        return_path,
                        expires_at,
                        created_at,
                        consumed_at,
                        consumed_request_id
                    """,
                    (
                        now,
                        normalized_idem_key,
                        state_hash,
                        access.workspace_id,
                        access.user_id,
                        now,
                    ),
                )
                row = cur.fetchone()
                if row is not None:
                    consumed = OAuthStateRecord(
                        state_hash=row[0],
                        workspace_id=row[1],
                        actor_user_id=row[2],
                        provider=OAuthProvider(row[3]),
                        pkce_code_challenge=row[4],
                        pkce_code_verifier_ref=row[5],
                        return_path=row[6],
                        expires_at=row[7],
                        created_at=row[8],
                        consumed_at=row[9],
                        consumed_request_id=row[10],
                    )

        if consumed is None:
            replay = self._lookup_idempotent_callback_replay(
                access,
                state_hash=state_hash,
                idempotency_key=normalized_idem_key,
            )
            if replay is not None:
                return replay
            raise OAuthStateInvalidOrReplayedError(
                "OAuth state is invalid, expired, or already consumed."
            )

        safe_return_path = validate_return_path(consumed.return_path)
        verifier_ref = consumed.pkce_code_verifier_ref

        # Handle explicit provider denial (e.g. user clicked Cancel / access_denied).
        if provider_error is not None and provider_error.strip():
            try:
                self._secret_store.delete_secret(verifier_ref)
            except Exception:  # noqa: BLE001
                logger.warning("Unable to delete PKCE verifier on provider error: ref=%s", verifier_ref)
            self._record_nonfatal_audit(
                access,
                action="GITHUB_OAUTH_PROVIDER_DENIED",
                target_type="WORKSPACE",
                target_id=access.workspace_id,
                metadata={
                    "provider": "GITHUB",
                    "provider_error": redact_text(provider_error.strip()),
                    "state_hash": state_hash,
                    "idempotency_key": normalized_idem_key,
                },
                now=self._clock(),
            )
            raise GitHubConnectError(
                status_code=400,
                code="OAUTH_AUTHORIZATION_DENIED",
                message="GitHub authorization was canceled or denied by the provider.",
                details={"provider_error": redact_text(provider_error.strip())},
            )

        if not isinstance(code, str) or not code.strip():
            try:
                self._secret_store.delete_secret(verifier_ref)
            except Exception:  # noqa: BLE001
                pass
            raise OAuthTokenExchangeError("OAuth authorization code is missing.")

        # Step 2: Retrieve and immediately burn the one-time PKCE verifier, then exchange code.
        try:
            code_verifier = self._secret_store.get_secret(verifier_ref)
            if not code_verifier:
                raise OAuthStateInvalidOrReplayedError(
                    "OAuth PKCE verifier is unavailable or already consumed."
                )
            try:
                exchanged = self._credential_provider.exchange_authorization_code(
                    code=code.strip(),
                    pkce_code_verifier=code_verifier,
                    redirect_uri=safe_redirect_uri,
                )
            except OAuthTokenExchangeError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise OAuthTokenExchangeError(
                    redact_text(f"OAuth token exchange failed: {type(exc).__name__}")
                ) from exc
        finally:
            try:
                self._secret_store.delete_secret(verifier_ref)
            except Exception:  # noqa: BLE001
                logger.warning("Unable to delete one-time PKCE verifier ref=%s", verifier_ref)

        # Step 3: Enforce OD-011 minimum scope policy BEFORE any secret or DB persistence.
        scope_check = validate_minimum_github_scopes(exchanged.scopes)
        if not scope_check.is_valid:
            token_revoked = self._best_effort_revoke_grant(exchanged.access_token)
            if scope_check.disallowed_scopes:
                self._record_nonfatal_audit(
                    access,
                    action="GITHUB_OAUTH_EXCESSIVE_SCOPE_REJECTED",
                    target_type="WORKSPACE",
                    target_id=access.workspace_id,
                    metadata={
                        "provider": "GITHUB",
                        "policy_action": PolicyGuidanceCode.POLICY_REVIEW_REQUIRED.value,
                        "granted_scopes": list(scope_check.granted_scopes),
                        "disallowed_scopes": list(scope_check.disallowed_scopes),
                        "token_revoked": token_revoked,
                        "side_effects": 0,
                        "state_hash": state_hash,
                        "idempotency_key": normalized_idem_key,
                    },
                    now=self._clock(),
                )
                raise GitHubConnectError(
                    status_code=403,
                    code="EXCESSIVE_OAUTH_SCOPE",
                    message=(
                        "GitHub OAuth token contained excessive scopes outside the "
                        "read:user allowlist and was immediately revoked."
                    ),
                    details={
                        "policy_action": PolicyGuidanceCode.POLICY_REVIEW_REQUIRED.value,
                        "disallowed_scopes": list(scope_check.disallowed_scopes),
                        "token_revoked": token_revoked,
                        "side_effects": 0,
                    },
                )

            self._record_nonfatal_audit(
                access,
                action="GITHUB_OAUTH_INSUFFICIENT_SCOPE_REJECTED",
                target_type="WORKSPACE",
                target_id=access.workspace_id,
                metadata={
                    "provider": "GITHUB",
                    "guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value,
                    "missing_scopes": list(scope_check.missing_required_scopes),
                    "token_revoked": token_revoked,
                    "state_hash": state_hash,
                    "idempotency_key": normalized_idem_key,
                },
                now=self._clock(),
            )
            raise GitHubConnectError(
                status_code=403,
                code="INSUFFICIENT_OAUTH_SCOPE",
                message="GitHub OAuth token did not grant the required read:user scope.",
                details={
                    "guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value,
                    "missing_scopes": list(scope_check.missing_required_scopes),
                    "token_revoked": token_revoked,
                },
            )

        # Optional: if caller requested atomic single public repository selection in callback,
        # inspect repository visibility BEFORE storing the secret / committing DB rows.
        repo_inspection: GitHubRepositoryInspection | None = None
        if parsed_repo_pair is not None:
            owner, repo_name = parsed_repo_pair
            repo_inspection = self._credential_provider.inspect_repository(
                access_token=exchanged.access_token,
                owner=owner,
                repo=repo_name,
            )
            if repo_inspection.outcome is not GitHubInspectionOutcome.PUBLIC_CONFIRMED:
                self._best_effort_revoke_grant(exchanged.access_token)
                self._raise_for_failed_selection_inspection(
                    repo_inspection,
                    repository_full_name=f"{owner}/{repo_name}",
                )

        self._maybe_inject_fault("after_token_exchange")

        # Step 4: Store exchanged access token in SecretStorePort (opaque ref only in DB).
        token_secret_ref = self._secret_store.store_secret(
            _TOKEN_NAMESPACE,
            exchanged.access_token,
            metadata={
                "workspace_id": access.workspace_id,
                "provider": "GITHUB",
                "external_account_id": exchanged.external_account_id,
            },
        )

        # Step 5: Single PostgreSQL transaction for live membership check + source_accounts
        # + optional source_resources + audit_events.
        commit_now = self._clock()
        previous_secret_ref: str | None = None
        try:
            with self._connection_factory() as conn:
                with workspace_scoped_transaction(conn, access) as cur:
                    _require_live_membership(
                        cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                    )
                    cur.execute(
                        """
                        SELECT token_secret_ref
                        FROM public.source_accounts
                        WHERE workspace_id = %s::uuid
                          AND provider = 'GITHUB'
                          AND external_account_id = %s
                        """,
                        (access.workspace_id, exchanged.external_account_id),
                    )
                    prev_row = cur.fetchone()
                    if prev_row is not None and str(prev_row[0]) != token_secret_ref:
                        previous_secret_ref = str(prev_row[0])

                    new_account_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO public.source_accounts (
                            id,
                            workspace_id,
                            provider,
                            external_account_id,
                            token_secret_ref,
                            scopes,
                            status,
                            created_at,
                            revoked_at
                        )
                        VALUES (%s::uuid, %s::uuid, 'GITHUB', %s, %s, %s, 'ACTIVE', %s, NULL)
                        ON CONFLICT (workspace_id, provider, external_account_id)
                        DO UPDATE SET
                            token_secret_ref = EXCLUDED.token_secret_ref,
                            scopes = EXCLUDED.scopes,
                            status = 'ACTIVE',
                            revoked_at = NULL
                        RETURNING
                            id::text, workspace_id::text, provider, external_account_id,
                            scopes, status, created_at, revoked_at
                        """,
                        (
                            new_account_id,
                            access.workspace_id,
                            exchanged.external_account_id,
                            token_secret_ref,
                            list(scope_check.granted_scopes),
                            commit_now,
                        ),
                    )
                    acct_row = cur.fetchone()
                    assert acct_row is not None
                    account_view = _row_to_account_view(acct_row)

                    resource_record: SourceResourceRecord | None = None
                    if repo_inspection is not None and repo_inspection.repository is not None:
                        resource_record = self._upsert_public_source_resource_in_tx(
                            cur,
                            access=access,
                            source_account_id=account_view.id,
                            inspection=repo_inspection,
                            policy_version=policy_version or self._default_policy_version,
                            consent_version=consent_version,
                            now=commit_now,
                        )

                    audit_meta: dict[str, Any] = {
                        "provider": "GITHUB",
                        "credential_mode": self._credential_provider.credential_mode.value,
                        "external_account_id": account_view.external_account_id,
                        "scopes": list(account_view.scopes),
                        "state_hash": state_hash,
                        "idempotency_key": normalized_idem_key,
                    }
                    if resource_record is not None:
                        audit_meta["source_resource_id"] = resource_record.id
                        audit_meta["repository"] = resource_record.name

                    _insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.USER,
                        actor_id=access.user_id,
                        action="GITHUB_OAUTH_CONNECTED",
                        target_type="SOURCE_ACCOUNT",
                        target_id=account_view.id,
                        metadata=audit_meta,
                        created_at=commit_now,
                    )
                    self._maybe_inject_fault("before_callback_commit")
        except WorkspaceMembershipDeniedError as exc:
            # Mid-callback membership revocation: revoke GitHub grant + clean up orphan secret!
            provider_revoked = self._best_effort_revoke_grant(exchanged.access_token)
            self._cleanup_orphan_secret(
                workspace_id=access.workspace_id,
                actor_user_id=access.user_id,
                token_secret_ref=token_secret_ref,
                failure_reason=f"WorkspaceMembershipDeniedError: {exc}",
                metadata={
                    "provider": "GITHUB",
                    "external_account_id": exchanged.external_account_id,
                    "state_hash": state_hash,
                    "idempotency_key": normalized_idem_key,
                    "provider_grant_revoked": provider_revoked,
                },
            )
            raise
        except GitHubConnectError:
            provider_revoked = self._best_effort_revoke_grant(exchanged.access_token)
            self._cleanup_orphan_secret(
                workspace_id=access.workspace_id,
                actor_user_id=access.user_id,
                token_secret_ref=token_secret_ref,
                failure_reason="GitHubConnectError during callback persistence",
                metadata={
                    "provider": "GITHUB",
                    "external_account_id": exchanged.external_account_id,
                    "state_hash": state_hash,
                    "idempotency_key": normalized_idem_key,
                    "provider_grant_revoked": provider_revoked,
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001
            provider_revoked = self._best_effort_revoke_grant(exchanged.access_token)
            redacted_failure = redact_text(f"{type(exc).__name__}: {exc}")
            logger.warning(
                "GitHub OAuth persistence failed; running orphan secret cleanup "
                "workspace=%s ref=%s reason=%s",
                access.workspace_id,
                token_secret_ref,
                redacted_failure,
            )
            cleanup_record = self._cleanup_orphan_secret(
                workspace_id=access.workspace_id,
                actor_user_id=access.user_id,
                token_secret_ref=token_secret_ref,
                failure_reason=redacted_failure,
                metadata={
                    "provider": "GITHUB",
                    "external_account_id": exchanged.external_account_id,
                    "state_hash": state_hash,
                    "idempotency_key": normalized_idem_key,
                    "provider_grant_revoked": provider_revoked,
                },
            )
            raise OAuthPersistenceFailedError(
                "Failed to persist GitHub connection; orphan token cleanup executed.",
                cleanup_record=cleanup_record,
            ) from exc

        # Post-commit rotation hygiene: remove superseded secret ref if re-connecting.
        if previous_secret_ref is not None:
            try:
                self._secret_store.delete_secret(previous_secret_ref)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Unable to delete rotated previous secret ref=%s", previous_secret_ref
                )

        return GitHubOAuthCallbackResult(
            source_account=account_view,
            source_resource=resource_record,
            return_path=safe_return_path,
            idempotent_replay=False,
        )

    def _record_nonfatal_audit(
        self,
        access: WorkspaceAccessContext,
        *,
        action: str,
        target_type: str,
        target_id: str,
        metadata: Mapping[str, Any],
        now: datetime,
    ) -> None:
        try:
            with self._connection_factory() as conn:
                with workspace_scoped_transaction(conn, access) as cur:
                    _insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.USER,
                        actor_id=access.user_id,
                        action=action,
                        target_type=target_type,
                        target_id=target_id,
                        metadata=metadata,
                        created_at=now,
                    )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Non-fatal audit insert skipped: workspace=%s action=%s",
                access.workspace_id,
                action,
            )

    def _raise_for_failed_selection_inspection(
        self,
        inspection: GitHubRepositoryInspection,
        *,
        repository_full_name: str,
    ) -> None:
        if inspection.outcome is GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED:
            raise GitHubConnectError(
                status_code=403,
                code="REPOSITORY_NOT_PUBLIC",
                message=(
                    "Only strictly public GitHub repositories (PUBLIC_CLOUD) may be "
                    "registered in MVP."
                ),
                details={
                    "repository": repository_full_name,
                    "visibility": inspection.observed_visibility or "PRIVATE",
                    "processing_mode_required": "PUBLIC_CLOUD",
                },
            )
        if inspection.outcome in (
            GitHubInspectionOutcome.TOKEN_REVOKED,
            GitHubInspectionOutcome.SCOPE_LOST,
        ):
            raise GitHubConnectError(
                status_code=403,
                code="REAUTH_REQUIRED",
                message="GitHub authorization is no longer valid; re-authentication is required.",
                details={
                    "guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value,
                    "account_status": "REVOKED",
                },
            )
        if inspection.outcome is GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED:
            raise GitHubConnectError(
                status_code=403,
                code="EXCESSIVE_OAUTH_SCOPE",
                message="Observed GitHub token scopes exceed the read:user allowlist.",
                details={
                    "policy_action": PolicyGuidanceCode.POLICY_REVIEW_REQUIRED.value,
                },
            )
        if inspection.outcome is GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY:
            raise GitHubConnectError(
                status_code=403,
                code="VISIBILITY_OR_ACCESS_AMBIGUOUS",
                message=(
                    "Repository could not be verified as public (provider returned 404/403)."
                ),
                details={
                    "guidance_code": PolicyGuidanceCode.VISIBILITY_OR_ACCESS_AMBIGUOUS.value,
                    "provider_http_status": inspection.http_status,
                },
            )
        if inspection.outcome is GitHubInspectionOutcome.RATE_LIMITED:
            raise GitHubConnectError(
                status_code=429,
                code="GITHUB_RATE_LIMITED",
                message="GitHub API rate limit exceeded; retry later.",
                retryable=True,
                details={
                    "retry_after_seconds": inspection.retry_after_seconds,
                },
            )
        raise GitHubConnectError(
            status_code=503,
            code="GITHUB_PROVIDER_UNAVAILABLE",
            message="GitHub repository metadata could not be verified temporarily.",
            retryable=True,
        )

    def _upsert_public_source_resource_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        access: WorkspaceAccessContext,
        source_account_id: str,
        inspection: GitHubRepositoryInspection,
        policy_version: str,
        consent_version: str | None,
        now: datetime,
    ) -> SourceResourceRecord:
        assert inspection.outcome is GitHubInspectionOutcome.PUBLIC_CONFIRMED
        assert inspection.repository is not None
        repo_meta = inspection.repository
        if not repo_meta.is_strictly_public:
            raise GitHubConnectError(
                status_code=403,
                code="REPOSITORY_NOT_PUBLIC",
                message="Only public GitHub repositories are allowed.",
            )

        cur.execute(
            """
            SELECT id::text, status
            FROM public.source_resources
            WHERE workspace_id = %s::uuid
              AND source_account_id = %s::uuid
              AND provider_resource_id = %s
            """,
            (access.workspace_id, source_account_id, repo_meta.provider_resource_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            existing_id, existing_status = str(existing[0]), str(existing[1])
            if existing_status == "DELETED" or is_deletion_fence_active(
                cur, access.workspace_id, existing_id
            ):
                raise GitHubConnectError(
                    status_code=409,
                    code="RESOURCE_DELETION_IN_PROGRESS",
                    message="This repository has an active or completed deletion fence.",
                )
            if existing_status == "POLICY_REVIEW_REQUIRED":
                raise GitHubConnectError(
                    status_code=403,
                    code="POLICY_BLOCKED",
                    message=(
                        "This repository is locked in POLICY_REVIEW_REQUIRED and cannot "
                        "be reactivated automatically."
                    ),
                    details={
                        "guidance_code": PolicyGuidanceCode.POLICY_REVIEW_REQUIRED.value,
                        "resource_status": existing_status,
                    },
                )
            cur.execute(
                f"""
                UPDATE public.source_resources sr
                SET name = %s,
                    canonical_url = %s,
                    last_visibility_checked_at = %s
                WHERE sr.workspace_id = %s::uuid
                  AND sr.id = %s::uuid
                RETURNING {_SOURCE_RESOURCE_SELECT_COLUMNS}
                """,
                (
                    repo_meta.full_name,
                    repo_meta.canonical_url,
                    now,
                    access.workspace_id,
                    existing_id,
                ),
            )
            updated_row = cur.fetchone()
            assert updated_row is not None
            return _row_to_resource(updated_row, guidance_code=None)

        if self._max_active_resources_per_workspace is not None:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.source_resources
                WHERE workspace_id = %s::uuid
                  AND status = 'ACTIVE'
                """,
                (access.workspace_id,),
            )
            count_row = cur.fetchone()
            active_count = int(count_row[0]) if count_row is not None else 0
            if active_count >= self._max_active_resources_per_workspace:
                raise GitHubConnectError(
                    status_code=409,
                    code="ALLOWLIST_CAPACITY_REACHED",
                    message="Workspace has reached its active public repository allowlist limit.",
                    details={"max_active_resources": self._max_active_resources_per_workspace},
                )

        new_resource_id = str(uuid.uuid4())
        cur.execute(
            f"""
            INSERT INTO public.source_resources AS sr (
                id,
                workspace_id,
                source_account_id,
                provider_resource_id,
                resource_type,
                name,
                canonical_url,
                visibility,
                processing_mode,
                raw_storage_mode,
                raw_retention_seconds,
                status,
                policy_version,
                consent_version,
                last_visibility_checked_at,
                created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s,
                'GITHUB_REPOSITORY', %s, %s, 'PUBLIC',
                'PUBLIC_CLOUD', 'NONE', NULL,
                'ACTIVE', %s, %s, %s, %s
            )
            RETURNING {_SOURCE_RESOURCE_SELECT_COLUMNS}
            """,
            (
                new_resource_id,
                access.workspace_id,
                source_account_id,
                repo_meta.provider_resource_id,
                repo_meta.full_name,
                repo_meta.canonical_url,
                policy_version,
                consent_version,
                now,
                now,
            ),
        )
        inserted_row = cur.fetchone()
        assert inserted_row is not None
        resource = _row_to_resource(inserted_row, guidance_code=None)
        _insert_audit_event(
            cur,
            workspace_id=access.workspace_id,
            actor_type=AuditActorType.USER,
            actor_id=access.user_id,
            action="SOURCE_RESOURCE_REGISTERED",
            target_type="SOURCE_RESOURCE",
            target_id=resource.id,
            metadata={
                "source_account_id": source_account_id,
                "provider_resource_id": resource.provider_resource_id,
                "repository": resource.name,
                "visibility": resource.visibility,
                "processing_mode": resource.processing_mode,
                "raw_storage_mode": resource.raw_storage_mode,
                "policy_version": resource.policy_version,
            },
            created_at=now,
        )
        return resource

    def _revoke_account_for_auth_failure(
        self,
        access: WorkspaceAccessContext,
        *,
        source_account_id: str,
        token_secret_ref: str,
        access_token: str | None,
        action: str,
        error_code: str,
        reason: str,
        revoke_at_provider: bool,
        now: datetime,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Atomically mark account REVOKED, block linked resources, cancel active work, 0 tombstones."""
        if revoke_at_provider and access_token:
            self._best_effort_revoke_grant(access_token)

        secret_deleted = False
        try:
            self._secret_store.delete_secret(token_secret_ref)
            secret_deleted = True
        except Exception as exc:  # noqa: BLE001
            self._cleanup_orphan_secret(
                workspace_id=access.workspace_id,
                actor_user_id=access.user_id,
                token_secret_ref=token_secret_ref,
                failure_reason=f"Secret deletion failed during {action}: {type(exc).__name__}",
                metadata={"source_account_id": source_account_id, "action": action},
            )

        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                cur.execute(
                    """
                    UPDATE public.source_accounts
                    SET status = 'REVOKED',
                        revoked_at = COALESCE(revoked_at, %s)
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (now, access.workspace_id, source_account_id),
                )
                cur.execute(
                    """
                    UPDATE public.source_resources
                    SET status = 'POLICY_REVIEW_REQUIRED',
                        last_visibility_checked_at = %s
                    WHERE workspace_id = %s::uuid
                      AND source_account_id = %s::uuid
                      AND status <> 'DELETED'
                    RETURNING id::text
                    """,
                    (now, access.workspace_id, source_account_id),
                )
                affected_resource_ids = tuple(str(r[0]) for r in cur.fetchall())
                canceled_job_ids, canceled_sync_run_ids = _cancel_active_work_for_resources(
                    cur,
                    workspace_id=access.workspace_id,
                    source_resource_ids=affected_resource_ids,
                    error_code=error_code,
                    blocked_processing_status="BLOCKED_SOURCE",
                    now=now,
                )
                _insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=access.user_id,
                    action=action,
                    target_type="SOURCE_ACCOUNT",
                    target_id=source_account_id,
                    metadata={
                        "reason": redact_text(reason),
                        "guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value,
                        "secret_deleted": secret_deleted,
                        "affected_resource_count": len(affected_resource_ids),
                        "canceled_job_count": len(canceled_job_ids),
                        "canceled_sync_run_count": len(canceled_sync_run_ids),
                        "tombstone_created": False,
                    },
                    created_at=now,
                )

        return affected_resource_ids, canceled_job_ids, canceled_sync_run_ids

    def register_source_resource(
        self,
        access: WorkspaceAccessContext,
        *,
        source_account_id: str,
        repository_full_name: str,
        policy_version: str | None = None,
        consent_version: str | None = None,
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> SourceResourceRecord:
        """Validate a single explicit public GitHub repo and register it on the allowlist."""
        self._enforce_role_and_live_session(
            access,
            allowed_roles=_MUTATION_ALLOWED_ROLES,
            verified_claims=verified_claims,
        )
        account_uuid = _try_normalize_uuid(source_account_id)
        if account_uuid is None:
            raise WorkspaceResourceNotFoundError()

        owner, repo_name = parse_repository_full_name(repository_full_name)
        now = self._clock()

        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                cur.execute(
                    """
                    SELECT id::text, status, scopes, token_secret_ref
                    FROM public.source_accounts
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (access.workspace_id, account_uuid),
                )
                acct_row = cur.fetchone()
                if acct_row is None:
                    raise WorkspaceResourceNotFoundError()
                account_status = str(acct_row[1])
                account_scopes = tuple(acct_row[2] or ())
                token_secret_ref = str(acct_row[3])

        if account_status != "ACTIVE":
            raise GitHubConnectError(
                status_code=403,
                code="REAUTH_REQUIRED",
                message="Source account is revoked; re-authentication is required.",
                details={
                    "guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value,
                    "account_status": account_status,
                },
            )

        scope_check = validate_minimum_github_scopes(account_scopes)
        if not scope_check.is_valid:
            raise GitHubConnectError(
                status_code=403,
                code="EXCESSIVE_OAUTH_SCOPE" if scope_check.disallowed_scopes else "REAUTH_REQUIRED",
                message="Source account scopes do not satisfy OD-011 minimum scope policy.",
            )

        access_token = self._secret_store.get_secret(token_secret_ref)
        if not access_token:
            self._revoke_account_for_auth_failure(
                access,
                source_account_id=account_uuid,
                token_secret_ref=token_secret_ref,
                access_token=None,
                action="SOURCE_ACCOUNT_TOKEN_MISSING",
                error_code="REAUTH_REQUIRED",
                reason="Stored OAuth secret is unavailable.",
                revoke_at_provider=False,
                now=now,
            )
            raise GitHubConnectError(
                status_code=403,
                code="REAUTH_REQUIRED",
                message="GitHub credential is unavailable; re-authentication is required.",
                details={"guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value},
            )

        inspection = self._credential_provider.inspect_repository(
            access_token=access_token,
            owner=owner,
            repo=repo_name,
        )
        if inspection.outcome in (
            GitHubInspectionOutcome.TOKEN_REVOKED,
            GitHubInspectionOutcome.SCOPE_LOST,
            GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED,
        ):
            self._revoke_account_for_auth_failure(
                access,
                source_account_id=account_uuid,
                token_secret_ref=token_secret_ref,
                access_token=access_token,
                action=(
                    "SOURCE_ACCOUNT_EXCESSIVE_SCOPE_REVOKED"
                    if inspection.outcome is GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED
                    else "SOURCE_ACCOUNT_TOKEN_REVOKED"
                ),
                error_code="REAUTH_REQUIRED",
                reason=inspection.reason,
                revoke_at_provider=(
                    inspection.outcome is not GitHubInspectionOutcome.TOKEN_REVOKED
                ),
                now=now,
            )
            self._raise_for_failed_selection_inspection(
                inspection, repository_full_name=f"{owner}/{repo_name}"
            )

        if inspection.outcome is not GitHubInspectionOutcome.PUBLIC_CONFIRMED:
            self._record_nonfatal_audit(
                access,
                action="SOURCE_RESOURCE_SELECTION_REJECTED",
                target_type="SOURCE_ACCOUNT",
                target_id=account_uuid,
                metadata={
                    "repository": f"{owner}/{repo_name}",
                    "outcome": inspection.outcome.value,
                    "observed_visibility": inspection.observed_visibility,
                    "provider_http_status": inspection.http_status,
                },
                now=now,
            )
            self._raise_for_failed_selection_inspection(
                inspection, repository_full_name=f"{owner}/{repo_name}"
            )

        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                return self._upsert_public_source_resource_in_tx(
                    cur,
                    access=access,
                    source_account_id=account_uuid,
                    inspection=inspection,
                    policy_version=(policy_version or self._default_policy_version).strip(),
                    consent_version=consent_version.strip() if consent_version else None,
                    now=now,
                )

    def list_source_accounts(
        self,
        access: WorkspaceAccessContext,
    ) -> list[SourceAccountView]:
        """List connected source accounts in the verified Workspace without exposing secrets."""
        self._enforce_role_and_live_session(access, allowed_roles=_READ_ALLOWED_ROLES)
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(cur, access, allowed_roles=_READ_ALLOWED_ROLES)
                cur.execute(
                    """
                    SELECT
                        id::text, workspace_id::text, provider, external_account_id,
                        scopes, status, created_at, revoked_at
                    FROM public.source_accounts
                    WHERE workspace_id = %s::uuid
                    ORDER BY created_at DESC, id ASC
                    """,
                    (access.workspace_id,),
                )
                return [_row_to_account_view(row) for row in cur.fetchall()]

    def list_source_resources(
        self,
        access: WorkspaceAccessContext,
        *,
        source_account_id: str | None = None,
    ) -> list[SourceResourceRecord]:
        """List non-deleted source resources in the verified Workspace."""
        self._enforce_role_and_live_session(access, allowed_roles=_READ_ALLOWED_ROLES)
        account_uuid: str | None = None
        if source_account_id is not None:
            account_uuid = _try_normalize_uuid(source_account_id)
            if account_uuid is None:
                return []

        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(cur, access, allowed_roles=_READ_ALLOWED_ROLES)
                if account_uuid is not None:
                    cur.execute(
                        f"""
                        SELECT {_SOURCE_RESOURCE_SELECT_COLUMNS}, sa.status
                        FROM public.source_resources sr
                        JOIN public.source_accounts sa
                          ON sa.workspace_id = sr.workspace_id
                         AND sa.id = sr.source_account_id
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.source_account_id = %s::uuid
                          AND sr.status <> 'DELETED'
                        ORDER BY sr.created_at DESC, sr.id ASC
                        """,
                        (access.workspace_id, account_uuid),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT {_SOURCE_RESOURCE_SELECT_COLUMNS}, sa.status
                        FROM public.source_resources sr
                        JOIN public.source_accounts sa
                          ON sa.workspace_id = sr.workspace_id
                         AND sa.id = sr.source_account_id
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.status <> 'DELETED'
                        ORDER BY sr.created_at DESC, sr.id ASC
                        """,
                        (access.workspace_id,),
                    )
                rows = cur.fetchall()

        results: list[SourceResourceRecord] = []
        for row in rows:
            account_status = str(row[-1])
            resource_status = str(row[11])
            results.append(
                _row_to_resource(
                    row[:-1],
                    guidance_code=_derive_resource_guidance(
                        resource_status=resource_status,
                        account_status=account_status,
                    ),
                )
            )
        return results

    def verify_resource_policy(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> PolicyVerificationResult:
        """Re-verify GitHub visibility & token health and apply T-201A / POLICY-01 transitions.

        Strictly distinguishes permission/visibility transitions from external deletion:
        NEVER creates ``source_object_revisions.is_tombstone = true`` or ``DELETED`` states.
        """
        self._enforce_role_and_live_session(
            access,
            allowed_roles=_MUTATION_ALLOWED_ROLES,
            verified_claims=verified_claims,
        )
        resource_uuid = _try_normalize_uuid(source_resource_id)
        if resource_uuid is None:
            raise WorkspaceResourceNotFoundError()

        now = self._clock()
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                cur.execute(
                    f"""
                    SELECT
                        {_SOURCE_RESOURCE_SELECT_COLUMNS},
                        sa.status,
                        sa.token_secret_ref
                    FROM public.source_resources sr
                    JOIN public.source_accounts sa
                      ON sa.workspace_id = sr.workspace_id
                     AND sa.id = sr.source_account_id
                    WHERE sr.workspace_id = %s::uuid
                      AND sr.id = %s::uuid
                    """,
                    (access.workspace_id, resource_uuid),
                )
                row = cur.fetchone()
                if row is None or str(row[11]) == "DELETED" or is_deletion_fence_active(
                    cur, access.workspace_id, resource_uuid
                ):
                    raise WorkspaceResourceNotFoundError()
                current_resource = _row_to_resource(row[:-2])
                account_status = str(row[-2])
                token_secret_ref = str(row[-1])

        # Case 1: Account already revoked or token missing -> REAUTH_REQUIRED, 0 tombstones.
        access_token = (
            self._secret_store.get_secret(token_secret_ref)
            if account_status == "ACTIVE"
            else None
        )
        if account_status != "ACTIVE" or not access_token:
            _, canceled_jobs, canceled_runs = self._revoke_account_for_auth_failure(
                access,
                source_account_id=current_resource.source_account_id,
                token_secret_ref=token_secret_ref,
                access_token=None,
                action="SOURCE_ACCOUNT_REAUTH_REQUIRED",
                error_code="REAUTH_REQUIRED",
                reason="Source account is revoked or credential secret is absent.",
                revoke_at_provider=False,
                now=now,
            )
            refreshed = self._load_resource_or_conceal(
                access, resource_uuid, guidance_code=PolicyGuidanceCode.REAUTH_REQUIRED
            )
            return PolicyVerificationResult(
                resource=refreshed,
                account_status="REVOKED",
                policy_decision=PolicyDecision.POLICY_BLOCKED,
                guidance_code=PolicyGuidanceCode.REAUTH_REQUIRED,
                transition=PolicyTransitionKind.TOKEN_REVOKED,
                tombstone_created=False,
                canceled_job_ids=canceled_jobs,
                canceled_sync_run_ids=canceled_runs,
                blocked_operations=ALL_BLOCKED_OPERATIONS,
                checked_at=now,
            )

        owner, repo_name = parse_repository_full_name(current_resource.name)
        inspection = self._credential_provider.inspect_repository(
            access_token=access_token,
            owner=owner,
            repo=repo_name,
        )

        # Case 2: Transient provider failure / rate limit -> fail closed without destructive transition.
        if inspection.outcome is GitHubInspectionOutcome.RATE_LIMITED:
            raise GitHubConnectError(
                status_code=429,
                code="GITHUB_RATE_LIMITED",
                message="GitHub API rate limit exceeded during policy verification.",
                retryable=True,
                details={"retry_after_seconds": inspection.retry_after_seconds},
            )
        if inspection.outcome is GitHubInspectionOutcome.PROVIDER_UNAVAILABLE:
            raise GitHubConnectError(
                status_code=503,
                code="GITHUB_PROVIDER_UNAVAILABLE",
                message="GitHub API temporarily unavailable during policy verification.",
                retryable=True,
            )

        # Case 3: 401 token revocation / scope loss / excessive scope drift -> REVOKED + REAUTH_REQUIRED.
        if inspection.outcome in (
            GitHubInspectionOutcome.TOKEN_REVOKED,
            GitHubInspectionOutcome.SCOPE_LOST,
            GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED,
        ):
            transition_map = {
                GitHubInspectionOutcome.TOKEN_REVOKED: PolicyTransitionKind.TOKEN_REVOKED,
                GitHubInspectionOutcome.SCOPE_LOST: PolicyTransitionKind.SCOPE_LOST,
                GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED: (
                    PolicyTransitionKind.EXCESSIVE_SCOPE_DETECTED
                ),
            }
            action_map = {
                GitHubInspectionOutcome.TOKEN_REVOKED: "SOURCE_ACCOUNT_TOKEN_REVOKED",
                GitHubInspectionOutcome.SCOPE_LOST: "SOURCE_ACCOUNT_SCOPE_LOST",
                GitHubInspectionOutcome.EXCESSIVE_SCOPE_DETECTED: (
                    "SOURCE_ACCOUNT_EXCESSIVE_SCOPE_REVOKED"
                ),
            }
            _, canceled_jobs, canceled_runs = self._revoke_account_for_auth_failure(
                access,
                source_account_id=current_resource.source_account_id,
                token_secret_ref=token_secret_ref,
                access_token=access_token,
                action=action_map[inspection.outcome],
                error_code="REAUTH_REQUIRED",
                reason=inspection.reason,
                revoke_at_provider=(
                    inspection.outcome is not GitHubInspectionOutcome.TOKEN_REVOKED
                ),
                now=now,
            )
            refreshed = self._load_resource_or_conceal(
                access, resource_uuid, guidance_code=PolicyGuidanceCode.REAUTH_REQUIRED
            )
            return PolicyVerificationResult(
                resource=refreshed,
                account_status="REVOKED",
                policy_decision=PolicyDecision.POLICY_BLOCKED,
                guidance_code=PolicyGuidanceCode.REAUTH_REQUIRED,
                transition=transition_map[inspection.outcome],
                tombstone_created=False,
                canceled_job_ids=canceled_jobs,
                canceled_sync_run_ids=canceled_runs,
                blocked_operations=ALL_BLOCKED_OPERATIONS,
                checked_at=now,
            )

        # Case 4: Confirmed Public -> Private / Internal transition -> POLICY_REVIEW_REQUIRED, 0 tombstones.
        if inspection.outcome is GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED:
            new_visibility = inspection.observed_visibility or "PRIVATE"
            with self._connection_factory() as conn:
                with workspace_scoped_transaction(conn, access) as cur:
                    _require_live_membership(
                        cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                    )
                    cur.execute(
                        f"""
                        UPDATE public.source_resources sr
                        SET visibility = %s,
                            status = 'POLICY_REVIEW_REQUIRED',
                            last_visibility_checked_at = %s
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.id = %s::uuid
                          AND sr.status <> 'DELETED'
                        RETURNING {_SOURCE_RESOURCE_SELECT_COLUMNS}
                        """,
                        (new_visibility, now, access.workspace_id, resource_uuid),
                    )
                    updated_row = cur.fetchone()
                    if updated_row is None:
                        raise WorkspaceResourceNotFoundError()

                    canceled_jobs, canceled_runs = _cancel_active_work_for_resources(
                        cur,
                        workspace_id=access.workspace_id,
                        source_resource_ids=(resource_uuid,),
                        error_code="POLICY_BLOCKED",
                        blocked_processing_status="BLOCKED_POLICY",
                        now=now,
                    )
                    _block_provenance_for_visibility_policy(
                        cur,
                        workspace_id=access.workspace_id,
                        source_resource_id=resource_uuid,
                    )
                    _insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=access.user_id,
                        action="SOURCE_RESOURCE_VISIBILITY_TRANSITIONED_NON_PUBLIC",
                        target_type="SOURCE_RESOURCE",
                        target_id=resource_uuid,
                        metadata={
                            "previous_visibility": current_resource.visibility,
                            "observed_visibility": new_visibility,
                            "new_status": "POLICY_REVIEW_REQUIRED",
                            "guidance_code": PolicyGuidanceCode.POLICY_REVIEW_REQUIRED.value,
                            "canceled_job_count": len(canceled_jobs),
                            "canceled_sync_run_count": len(canceled_runs),
                            "blocked_operations": list(ALL_BLOCKED_OPERATIONS),
                            "tombstone_created": False,
                        },
                        created_at=now,
                    )
                    updated_resource = _row_to_resource(
                        updated_row,
                        guidance_code=PolicyGuidanceCode.POLICY_REVIEW_REQUIRED,
                    )

            return PolicyVerificationResult(
                resource=updated_resource,
                account_status="ACTIVE",
                policy_decision=PolicyDecision.POLICY_BLOCKED,
                guidance_code=PolicyGuidanceCode.POLICY_REVIEW_REQUIRED,
                transition=PolicyTransitionKind.PUBLIC_TO_NON_PUBLIC,
                tombstone_created=False,
                canceled_job_ids=canceled_jobs,
                canceled_sync_run_ids=canceled_runs,
                blocked_operations=ALL_BLOCKED_OPERATIONS,
                checked_at=now,
            )

        # Case 5: Ambiguous 404 / non-rate-limit 403 -> fail-closed POLICY_REVIEW_REQUIRED, 0 tombstones.
        if inspection.outcome is GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY:
            with self._connection_factory() as conn:
                with workspace_scoped_transaction(conn, access) as cur:
                    _require_live_membership(
                        cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                    )
                    cur.execute(
                        f"""
                        UPDATE public.source_resources sr
                        SET status = 'POLICY_REVIEW_REQUIRED',
                            last_visibility_checked_at = %s
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.id = %s::uuid
                          AND sr.status <> 'DELETED'
                        RETURNING {_SOURCE_RESOURCE_SELECT_COLUMNS}
                        """,
                        (now, access.workspace_id, resource_uuid),
                    )
                    updated_row = cur.fetchone()
                    if updated_row is None:
                        raise WorkspaceResourceNotFoundError()

                    canceled_jobs, canceled_runs = _cancel_active_work_for_resources(
                        cur,
                        workspace_id=access.workspace_id,
                        source_resource_ids=(resource_uuid,),
                        error_code="VISIBILITY_OR_ACCESS_AMBIGUOUS",
                        blocked_processing_status="BLOCKED_POLICY",
                        now=now,
                    )
                    _block_provenance_for_visibility_policy(
                        cur,
                        workspace_id=access.workspace_id,
                        source_resource_id=resource_uuid,
                    )
                    _insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=access.user_id,
                        action="SOURCE_RESOURCE_VISIBILITY_OR_ACCESS_AMBIGUOUS",
                        target_type="SOURCE_RESOURCE",
                        target_id=resource_uuid,
                        metadata={
                            "provider_http_status": inspection.http_status,
                            "new_status": "POLICY_REVIEW_REQUIRED",
                            "guidance_code": (
                                PolicyGuidanceCode.VISIBILITY_OR_ACCESS_AMBIGUOUS.value
                            ),
                            "canceled_job_count": len(canceled_jobs),
                            "canceled_sync_run_count": len(canceled_runs),
                            "blocked_operations": list(ALL_BLOCKED_OPERATIONS),
                            "tombstone_created": False,
                        },
                        created_at=now,
                    )
                    updated_resource = _row_to_resource(
                        updated_row,
                        guidance_code=PolicyGuidanceCode.VISIBILITY_OR_ACCESS_AMBIGUOUS,
                    )

            return PolicyVerificationResult(
                resource=updated_resource,
                account_status="ACTIVE",
                policy_decision=PolicyDecision.POLICY_BLOCKED,
                guidance_code=PolicyGuidanceCode.VISIBILITY_OR_ACCESS_AMBIGUOUS,
                transition=PolicyTransitionKind.VISIBILITY_OR_ACCESS_AMBIGUOUS,
                tombstone_created=False,
                canceled_job_ids=canceled_jobs,
                canceled_sync_run_ids=canceled_runs,
                blocked_operations=ALL_BLOCKED_OPERATIONS,
                checked_at=now,
            )

        # Case 6: PUBLIC_CONFIRMED. Fail-closed: never silently auto-unblock a resource
        # already locked in POLICY_REVIEW_REQUIRED.
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                cur.execute(
                    f"""
                    UPDATE public.source_resources sr
                    SET last_visibility_checked_at = %s
                    WHERE sr.workspace_id = %s::uuid
                      AND sr.id = %s::uuid
                      AND sr.status <> 'DELETED'
                    RETURNING {_SOURCE_RESOURCE_SELECT_COLUMNS}
                    """,
                    (now, access.workspace_id, resource_uuid),
                )
                updated_row = cur.fetchone()
                if updated_row is None:
                    raise WorkspaceResourceNotFoundError()

                is_active = str(updated_row[11]) == "ACTIVE" and str(updated_row[7]) == "PUBLIC"
                guidance = None if is_active else PolicyGuidanceCode.POLICY_REVIEW_REQUIRED
                _insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="SOURCE_RESOURCE_POLICY_VERIFIED",
                    target_type="SOURCE_RESOURCE",
                    target_id=resource_uuid,
                    metadata={
                        "observed_visibility": "PUBLIC",
                        "resource_status": str(updated_row[11]),
                        "policy_decision": (
                            PolicyDecision.ALLOWED.value
                            if is_active
                            else PolicyDecision.POLICY_BLOCKED.value
                        ),
                    },
                    created_at=now,
                )
                updated_resource = _row_to_resource(updated_row, guidance_code=guidance)

        return PolicyVerificationResult(
            resource=updated_resource,
            account_status="ACTIVE",
            policy_decision=(
                PolicyDecision.ALLOWED if is_active else PolicyDecision.POLICY_BLOCKED
            ),
            guidance_code=guidance,
            transition=(
                PolicyTransitionKind.UNCHANGED_PUBLIC
                if is_active
                else PolicyTransitionKind.ALREADY_POLICY_REVIEW_REQUIRED
            ),
            tombstone_created=False,
            canceled_job_ids=(),
            canceled_sync_run_ids=(),
            blocked_operations=() if is_active else ALL_BLOCKED_OPERATIONS,
            checked_at=now,
        )

    def _load_resource_or_conceal(
        self,
        access: WorkspaceAccessContext,
        resource_uuid: str,
        *,
        guidance_code: PolicyGuidanceCode | None,
    ) -> SourceResourceRecord:
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(cur, access, allowed_roles=_READ_ALLOWED_ROLES)
                cur.execute(
                    f"""
                    SELECT {_SOURCE_RESOURCE_SELECT_COLUMNS}
                    FROM public.source_resources sr
                    WHERE sr.workspace_id = %s::uuid
                      AND sr.id = %s::uuid
                    """,
                    (access.workspace_id, resource_uuid),
                )
                row = cur.fetchone()
                if row is None:
                    raise WorkspaceResourceNotFoundError()
                return _row_to_resource(row, guidance_code=guidance_code)

    def disconnect_source_account(
        self,
        access: WorkspaceAccessContext,
        *,
        source_account_id: str,
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> SourceAccountDisconnectResult:
        """Idempotently disconnect a GitHub source account, revoke grant, and delete secret."""
        self._enforce_role_and_live_session(
            access,
            allowed_roles=_MUTATION_ALLOWED_ROLES,
            verified_claims=verified_claims,
        )
        account_uuid = _try_normalize_uuid(source_account_id)
        if account_uuid is None:
            raise WorkspaceResourceNotFoundError()

        now = self._clock()
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                cur.execute(
                    """
                    SELECT
                        id::text, workspace_id::text, provider, external_account_id,
                        scopes, status, created_at, revoked_at, token_secret_ref
                    FROM public.source_accounts
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (access.workspace_id, account_uuid),
                )
                row = cur.fetchone()
                if row is None:
                    raise WorkspaceResourceNotFoundError()
                already_revoked = str(row[5]) == "REVOKED"
                token_secret_ref = str(row[8])

        access_token = self._secret_store.get_secret(token_secret_ref)
        provider_grant_revoked = False
        if access_token:
            provider_grant_revoked = self._best_effort_revoke_grant(access_token)
        elif already_revoked:
            provider_grant_revoked = True

        secret_deleted = False
        try:
            self._secret_store.delete_secret(token_secret_ref)
            secret_deleted = True
        except Exception as exc:  # noqa: BLE001
            self._cleanup_orphan_secret(
                workspace_id=access.workspace_id,
                actor_user_id=access.user_id,
                token_secret_ref=token_secret_ref,
                failure_reason=f"Disconnect secret delete failed: {type(exc).__name__}",
                metadata={"source_account_id": account_uuid},
            )

        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(
                    cur, access, allowed_roles=_MUTATION_ALLOWED_ROLES
                )
                cur.execute(
                    """
                    UPDATE public.source_accounts
                    SET status = 'REVOKED',
                        revoked_at = COALESCE(revoked_at, %s)
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    RETURNING
                        id::text, workspace_id::text, provider, external_account_id,
                        scopes, status, created_at, revoked_at
                    """,
                    (now, access.workspace_id, account_uuid),
                )
                updated_acct_row = cur.fetchone()
                if updated_acct_row is None:
                    raise WorkspaceResourceNotFoundError()
                account_view = _row_to_account_view(updated_acct_row)

                cur.execute(
                    """
                    UPDATE public.source_resources
                    SET status = 'POLICY_REVIEW_REQUIRED',
                        last_visibility_checked_at = %s
                    WHERE workspace_id = %s::uuid
                      AND source_account_id = %s::uuid
                      AND status <> 'DELETED'
                    RETURNING id::text
                    """,
                    (now, access.workspace_id, account_uuid),
                )
                affected_resource_ids = tuple(str(r[0]) for r in cur.fetchall())
                canceled_jobs, canceled_runs = _cancel_active_work_for_resources(
                    cur,
                    workspace_id=access.workspace_id,
                    source_resource_ids=affected_resource_ids,
                    error_code="ACCOUNT_DISCONNECTED",
                    blocked_processing_status="BLOCKED_SOURCE",
                    now=now,
                )
                _insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="SOURCE_ACCOUNT_DISCONNECTED",
                    target_type="SOURCE_ACCOUNT",
                    target_id=account_uuid,
                    metadata={
                        "provider": "GITHUB",
                        "already_revoked": already_revoked,
                        "provider_grant_revoked": provider_grant_revoked,
                        "secret_deleted": secret_deleted,
                        "affected_resource_count": len(affected_resource_ids),
                        "canceled_job_count": len(canceled_jobs),
                        "canceled_sync_run_count": len(canceled_runs),
                        "tombstone_created": False,
                    },
                    created_at=now,
                )

        return SourceAccountDisconnectResult(
            source_account=account_view,
            provider_grant_revoked=provider_grant_revoked,
            secret_deleted=secret_deleted,
            already_revoked=already_revoked,
            affected_resource_ids=affected_resource_ids,
            canceled_job_ids=canceled_jobs,
            canceled_sync_run_ids=canceled_runs,
            tombstone_created=False,
        )

    def assert_resource_processing_allowed(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        operation: PolicyGuardedOperation,
    ) -> SourceResourcePolicyClearance:
        """Fail-closed policy guard for ingestion, raw refetch, AI transmission, and export."""
        self._enforce_role_and_live_session(access, allowed_roles=_READ_ALLOWED_ROLES)
        resource_uuid = _try_normalize_uuid(source_resource_id)
        if resource_uuid is None:
            raise WorkspaceResourceNotFoundError()

        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(cur, access, allowed_roles=_READ_ALLOWED_ROLES)
                cur.execute(
                    f"""
                    SELECT
                        {_SOURCE_RESOURCE_SELECT_COLUMNS},
                        sa.status,
                        sa.scopes
                    FROM public.source_resources sr
                    JOIN public.source_accounts sa
                      ON sa.workspace_id = sr.workspace_id
                     AND sa.id = sr.source_account_id
                    WHERE sr.workspace_id = %s::uuid
                      AND sr.id = %s::uuid
                    """,
                    (access.workspace_id, resource_uuid),
                )
                row = cur.fetchone()
                if row is None or str(row[11]) == "DELETED" or is_deletion_fence_active(
                    cur, access.workspace_id, resource_uuid
                ):
                    raise WorkspaceResourceNotFoundError()
                account_status = str(row[-2])
                account_scopes = tuple(row[-1] or ())
                resource = _row_to_resource(
                    row[:-2],
                    guidance_code=_derive_resource_guidance(
                        resource_status=str(row[11]),
                        account_status=account_status,
                    ),
                )

        if account_status != "ACTIVE":
            raise GitHubConnectError(
                status_code=403,
                code="REAUTH_REQUIRED",
                message=(
                    f"Operation {operation.value} blocked because the connected GitHub "
                    "account is revoked."
                ),
                details={
                    "guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value,
                    "operation": operation.value,
                    "account_status": account_status,
                },
            )

        scope_check = validate_minimum_github_scopes(account_scopes)
        if not scope_check.is_valid:
            raise GitHubConnectError(
                status_code=403,
                code="POLICY_BLOCKED",
                message=(
                    f"Operation {operation.value} blocked because account scopes violate "
                    "OD-011 minimum scope policy."
                ),
                details={
                    "guidance_code": PolicyGuidanceCode.POLICY_REVIEW_REQUIRED.value,
                    "operation": operation.value,
                },
            )

        if (
            resource.status != "ACTIVE"
            or resource.visibility != "PUBLIC"
            or resource.processing_mode != "PUBLIC_CLOUD"
            or resource.raw_storage_mode != "NONE"
        ):
            raise GitHubConnectError(
                status_code=403,
                code="POLICY_BLOCKED",
                message=(
                    f"Operation {operation.value} is blocked by source resource policy "
                    f"(status={resource.status}, visibility={resource.visibility})."
                ),
                details={
                    "guidance_code": PolicyGuidanceCode.POLICY_REVIEW_REQUIRED.value,
                    "operation": operation.value,
                    "resource_status": resource.status,
                    "visibility": resource.visibility,
                },
            )

        return SourceResourcePolicyClearance(
            resource=resource,
            source_account_id=resource.source_account_id,
            policy_decision=PolicyDecision.ALLOWED,
            operation=operation,
        )

    def resolve_active_credential_for_resource(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        operation: PolicyGuardedOperation = PolicyGuardedOperation.INGESTION,
    ) -> ResolvedGitHubCredential:
        """Internal helper for M2 Round 2 orchestrator/provider to fetch a policy-cleared token."""
        clearance = self.assert_resource_processing_allowed(
            access,
            source_resource_id=source_resource_id,
            operation=operation,
        )
        with self._connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                _require_live_membership(cur, access, allowed_roles=_READ_ALLOWED_ROLES)
                cur.execute(
                    """
                    SELECT token_secret_ref, scopes
                    FROM public.source_accounts
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                      AND status = 'ACTIVE'
                    """,
                    (access.workspace_id, clearance.source_account_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise GitHubConnectError(
                        status_code=403,
                        code="REAUTH_REQUIRED",
                        message="Connected GitHub account is no longer active.",
                        details={"guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value},
                    )
                token_secret_ref = str(row[0])
                scopes = tuple(row[1] or ())

        access_token = self._secret_store.get_secret(token_secret_ref)
        if not access_token:
            raise GitHubConnectError(
                status_code=403,
                code="REAUTH_REQUIRED",
                message="GitHub access token is unavailable; re-authentication is required.",
                details={"guidance_code": PolicyGuidanceCode.REAUTH_REQUIRED.value},
            )
        return ResolvedGitHubCredential(
            source_account_id=clearance.source_account_id,
            source_resource=clearance.resource,
            access_token=access_token,
            scopes=scopes,
        )
