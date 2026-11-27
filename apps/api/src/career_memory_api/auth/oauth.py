"""OAuth connect state single-use boundary, callback replay defense, and orphan secret cleanup."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlencode

import psycopg

from career_memory_api.auth.jwt_verifier import (
    LiveSessionStatus,
    LiveSessionVerifierPort,
    VerifiedJwtClaims,
)
from career_memory_api.auth.postgres_guard import (
    SourceAccountRecord,
    WorkspaceRole,
    workspace_scoped_transaction,
)
from career_memory_api.auth.redaction import redact_mapping, redact_text
from career_memory_api.auth.return_path import validate_return_path
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    SessionTerminatedError,
    WorkspaceAccessContext,
    WorkspaceMembershipDeniedError,
)

logger = logging.getLogger(__name__)

_PKCE_NAMESPACE = "github-oauth-pkce"
_TOKEN_NAMESPACE = "github-oauth-token"
_CONNECT_ALLOWED_ROLES = frozenset({WorkspaceRole.OWNER.value, WorkspaceRole.MEMBER.value})


class OAuthProvider(StrEnum):
    """Supported external source OAuth providers."""

    GITHUB = "GITHUB"


class OrphanSecretCleanupStatus(StrEnum):
    """Audit/outbox status for orphan secret deletion after DB failure."""

    DELETED = "DELETED"
    RETRY_PENDING = "RETRY_PENDING"


class OAuthBoundaryError(Exception):
    """Base error for OAuth connect/callback boundary rejections."""


class OAuthStateInvalidOrReplayedError(OAuthBoundaryError):
    """Raised when an OAuth state is missing, expired, mismatched, or already consumed."""


class OAuthTokenExchangeError(OAuthBoundaryError):
    """Raised when exchanging an authorization code fails."""


class OAuthPersistenceFailedError(OAuthBoundaryError):
    """Raised when DB persistence fails after token exchange; triggers orphan cleanup."""

    def __init__(
        self,
        message: str,
        *,
        cleanup_record: OrphanSecretCleanupRecord | None = None,
    ) -> None:
        super().__init__(redact_text(message))
        self.cleanup_record = cleanup_record


@dataclass(frozen=True, slots=True)
class OAuthStateRecord:
    """Stored OAuth state metadata (hashes & secret refs only; never raw state/verifier)."""

    state_hash: str
    workspace_id: str
    actor_user_id: str
    provider: OAuthProvider
    pkce_code_challenge: str
    pkce_code_verifier_ref: str
    return_path: str
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None
    consumed_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthConnectStartResult:
    """Result handed back to start the browser OAuth redirect."""

    provider: OAuthProvider
    authorization_url: str
    state: str = field(repr=False)
    pkce_code_challenge: str
    return_path: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"OAuthConnectStartResult(provider={self.provider!r}, "
            f"state=<redacted>, return_path={self.return_path!r}, "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class ExchangedOAuthToken:
    """Provider token exchange payload with access_token redacted from repr/str."""

    provider: OAuthProvider
    external_account_id: str
    access_token: str = field(repr=False)
    scopes: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            f"ExchangedOAuthToken(provider={self.provider!r}, "
            f"external_account_id={self.external_account_id!r}, "
            f"access_token=<redacted>, scopes={self.scopes!r})"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class OrphanSecretCleanupRecord:
    """Redacted audit/outbox record for orphan secret cleanup."""

    id: str
    workspace_id: str
    actor_user_id: str
    provider: OAuthProvider
    token_secret_ref: str
    status: OrphanSecretCleanupStatus
    attempt_count: int
    failure_reason: str
    redacted_metadata: Mapping[str, Any]
    created_at: datetime
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OAuthCallbackResult:
    """Result returned after verified OAuth callback and source account persistence."""

    source_account: SourceAccountRecord
    return_path: str


class SecretStorePort(Protocol):
    """Port for external secret storage (KMS / Secret Manager adapter in production)."""

    def store_secret(
        self,
        namespace: str,
        secret_value: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Persist a secret and return an opaque ``secret_ref``."""
        ...

    def get_secret(self, secret_ref: str) -> str | None:
        """Retrieve a secret by opaque ``secret_ref``, or ``None`` if absent."""
        ...

    def delete_secret(self, secret_ref: str) -> bool:
        """Idempotently delete a secret; return True if existed, False if already absent."""
        ...


class OAuthTokenExchangerPort(Protocol):
    """Port for exchanging an OAuth authorization code + PKCE verifier for a token."""

    def exchange_authorization_code(
        self,
        *,
        provider: OAuthProvider,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> ExchangedOAuthToken:
        ...


class OAuthBoundaryStorePort(Protocol):
    """Workspace-scoped persistence port for OAuth state, source accounts, and orphan cleanups."""

    def create_state(
        self, access: WorkspaceAccessContext, record: OAuthStateRecord
    ) -> None:
        ...

    def consume_state_atomically(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        state_hash: str,
        now: datetime,
        request_id: str,
    ) -> OAuthStateRecord | None:
        ...

    def upsert_active_source_account(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        external_account_id: str,
        token_secret_ref: str,
        scopes: tuple[str, ...],
        now: datetime,
    ) -> SourceAccountRecord:
        ...

    def record_orphan_secret_cleanup(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        provider: OAuthProvider,
        token_secret_ref: str,
        status: OrphanSecretCleanupStatus,
        failure_reason: str,
        redacted_metadata: Mapping[str, Any],
        now: datetime,
    ) -> OrphanSecretCleanupRecord:
        ...


def hash_oauth_state(raw_state: str) -> str:
    """Return lowercase SHA-256 hex digest of raw OAuth state."""
    return hashlib.sha256(raw_state.encode("utf-8")).hexdigest()


def derive_pkce_s256_challenge(code_verifier: str) -> str:
    """Compute RFC 7636 S256 PKCE code challenge."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class InMemorySecretStore:
    """Thread-safe deterministic in-memory ``SecretStorePort`` for local tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: dict[str, str] = {}
        self._fail_delete_once_namespaces: set[str] = set()

    def fail_delete_once_for_namespace(self, namespace: str) -> None:
        """Inject a single transient delete failure for cleanup-retry testing."""
        with self._lock:
            self._fail_delete_once_namespaces.add(namespace)

    def store_secret(
        self,
        namespace: str,
        secret_value: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        safe_ns = "".join(ch for ch in namespace if ch.isalnum() or ch in "-_")
        ref = f"secret://{safe_ns}/{uuid.uuid4()}"
        with self._lock:
            self._secrets[ref] = secret_value
        return ref

    def get_secret(self, secret_ref: str) -> str | None:
        with self._lock:
            return self._secrets.get(secret_ref)

    def delete_secret(self, secret_ref: str) -> bool:
        with self._lock:
            for ns in list(self._fail_delete_once_namespaces):
                if f"secret://{ns}/" in secret_ref:
                    self._fail_delete_once_namespaces.remove(ns)
                    raise RuntimeError("Transient secret store delete failure.")
            return self._secrets.pop(secret_ref, None) is not None

    def all_refs(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._secrets.keys())


class InMemoryOAuthBoundaryStore:
    """Thread-safe in-memory ``OAuthBoundaryStorePort`` for fast unit & contract tests."""

    def __init__(
        self,
        *,
        fail_next_source_account_write: Exception | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.states: dict[str, OAuthStateRecord] = {}
        self.source_accounts: dict[tuple[str, str, str], SourceAccountRecord] = {}
        self.cleanup_records: dict[tuple[str, str], OrphanSecretCleanupRecord] = {}
        self._fail_next_source_account_write = fail_next_source_account_write

    def raw_state_dump(self) -> str:
        with self._lock:
            return repr(self.states)

    def create_state(
        self, access: WorkspaceAccessContext, record: OAuthStateRecord
    ) -> None:
        if (
            record.workspace_id != access.workspace_id
            or record.actor_user_id != access.user_id
        ):
            raise WorkspaceMembershipDeniedError("OAuth state context mismatch.")
        with self._lock:
            self.states[record.state_hash] = record

    def consume_state_atomically(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        state_hash: str,
        now: datetime,
        request_id: str,
    ) -> OAuthStateRecord | None:
        with self._lock:
            existing = self.states.get(state_hash)
            if existing is None:
                return None
            if (
                existing.workspace_id != access.workspace_id
                or existing.actor_user_id != access.user_id
                or existing.provider is not provider
                or existing.consumed_at is not None
                or existing.expires_at <= now
            ):
                return None
            consumed = OAuthStateRecord(
                state_hash=existing.state_hash,
                workspace_id=existing.workspace_id,
                actor_user_id=existing.actor_user_id,
                provider=existing.provider,
                pkce_code_challenge=existing.pkce_code_challenge,
                pkce_code_verifier_ref=existing.pkce_code_verifier_ref,
                return_path=existing.return_path,
                expires_at=existing.expires_at,
                created_at=existing.created_at,
                consumed_at=now,
                consumed_request_id=request_id,
            )
            self.states[state_hash] = consumed
            return consumed

    def upsert_active_source_account(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        external_account_id: str,
        token_secret_ref: str,
        scopes: tuple[str, ...],
        now: datetime,
    ) -> SourceAccountRecord:
        with self._lock:
            if self._fail_next_source_account_write is not None:
                err = self._fail_next_source_account_write
                self._fail_next_source_account_write = None
                raise err
            key = (access.workspace_id, provider.value, external_account_id)
            existing = self.source_accounts.get(key)
            record = SourceAccountRecord(
                id=existing.id if existing is not None else str(uuid.uuid4()),
                workspace_id=access.workspace_id,
                provider=provider.value,
                external_account_id=external_account_id,
                token_secret_ref=token_secret_ref,
                scopes=scopes,
                status="ACTIVE",
                created_at=existing.created_at if existing is not None else now,
                revoked_at=None,
            )
            self.source_accounts[key] = record
            return record

    def record_orphan_secret_cleanup(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        provider: OAuthProvider,
        token_secret_ref: str,
        status: OrphanSecretCleanupStatus,
        failure_reason: str,
        redacted_metadata: Mapping[str, Any],
        now: datetime,
    ) -> OrphanSecretCleanupRecord:
        safe_reason = redact_text(failure_reason)
        safe_meta = redact_mapping(redacted_metadata)
        with self._lock:
            key = (workspace_id, token_secret_ref)
            existing = self.cleanup_records.get(key)
            attempt_count = 1 if existing is None else existing.attempt_count + 1
            resolved_at = (
                now
                if status is OrphanSecretCleanupStatus.DELETED
                else (existing.resolved_at if existing is not None else None)
            )
            record = OrphanSecretCleanupRecord(
                id=existing.id if existing is not None else str(uuid.uuid4()),
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                provider=provider,
                token_secret_ref=token_secret_ref,
                status=status,
                attempt_count=attempt_count,
                failure_reason=safe_reason,
                redacted_metadata=safe_meta,
                created_at=existing.created_at if existing is not None else now,
                resolved_at=resolved_at,
            )
            self.cleanup_records[key] = record
            return record


class PostgresOAuthBoundaryStore:
    """PostgreSQL ``OAuthBoundaryStorePort`` enforcing atomic state consumption and FORCE RLS."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def create_state(
        self, access: WorkspaceAccessContext, record: OAuthStateRecord
    ) -> None:
        with workspace_scoped_transaction(self._conn, access) as cur:
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
                    record.state_hash,
                    access.workspace_id,
                    access.user_id,
                    record.provider.value,
                    record.pkce_code_challenge,
                    record.pkce_code_verifier_ref,
                    record.return_path,
                    record.expires_at,
                    record.created_at,
                ),
            )

    def consume_state_atomically(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        state_hash: str,
        now: datetime,
        request_id: str,
    ) -> OAuthStateRecord | None:
        with workspace_scoped_transaction(self._conn, access) as cur:
            cur.execute(
                """
                UPDATE public.oauth_connect_states
                SET consumed_at = %s,
                    consumed_request_id = %s
                WHERE state_hash = %s
                  AND workspace_id = %s::uuid
                  AND actor_user_id = %s::uuid
                  AND provider = %s
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
                    request_id,
                    state_hash,
                    access.workspace_id,
                    access.user_id,
                    provider.value,
                    now,
                ),
            )
            row = cur.fetchone()

        if row is None:
            return None

        return OAuthStateRecord(
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

    def upsert_active_source_account(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        external_account_id: str,
        token_secret_ref: str,
        scopes: tuple[str, ...],
        now: datetime,
    ) -> SourceAccountRecord:
        account_id = str(uuid.uuid4())
        with workspace_scoped_transaction(self._conn, access) as cur:
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
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, 'ACTIVE', %s, NULL)
                ON CONFLICT (workspace_id, provider, external_account_id)
                DO UPDATE SET
                    token_secret_ref = EXCLUDED.token_secret_ref,
                    scopes = EXCLUDED.scopes,
                    status = 'ACTIVE',
                    revoked_at = NULL
                RETURNING
                    id::text,
                    workspace_id::text,
                    provider,
                    external_account_id,
                    token_secret_ref,
                    scopes,
                    status,
                    created_at,
                    revoked_at
                """,
                (
                    account_id,
                    access.workspace_id,
                    provider.value,
                    external_account_id,
                    token_secret_ref,
                    list(scopes),
                    now,
                ),
            )
            row = cur.fetchone()
            assert row is not None

        return SourceAccountRecord(
            id=row[0],
            workspace_id=row[1],
            provider=row[2],
            external_account_id=row[3],
            token_secret_ref=row[4],
            scopes=tuple(row[5] or ()),
            status=row[6],
            created_at=row[7],
            revoked_at=row[8],
        )

    def record_orphan_secret_cleanup(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        provider: OAuthProvider,
        token_secret_ref: str,
        status: OrphanSecretCleanupStatus,
        failure_reason: str,
        redacted_metadata: Mapping[str, Any],
        now: datetime,
    ) -> OrphanSecretCleanupRecord:
        cleanup_id = str(uuid.uuid4())
        safe_reason = redact_text(failure_reason)
        safe_meta = redact_mapping(redacted_metadata)
        resolved_at = now if status is OrphanSecretCleanupStatus.DELETED else None

        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        set_config('app.current_workspace_id', %s, true),
                        set_config('app.current_user_id', %s, true)
                    """,
                    (workspace_id, actor_user_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.orphan_secret_cleanups (
                        id,
                        workspace_id,
                        actor_user_id,
                        provider,
                        token_secret_ref,
                        status,
                        attempt_count,
                        failure_reason,
                        redacted_metadata,
                        created_at,
                        updated_at,
                        resolved_at
                    )
                    VALUES (
                        %s::uuid,
                        %s::uuid,
                        %s::uuid,
                        %s,
                        %s,
                        %s,
                        1,
                        %s,
                        %s::jsonb,
                        %s,
                        %s,
                        %s
                    )
                    ON CONFLICT (workspace_id, token_secret_ref)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        attempt_count = public.orphan_secret_cleanups.attempt_count + 1,
                        failure_reason = EXCLUDED.failure_reason,
                        redacted_metadata = EXCLUDED.redacted_metadata,
                        updated_at = EXCLUDED.updated_at,
                        resolved_at = COALESCE(
                            EXCLUDED.resolved_at,
                            public.orphan_secret_cleanups.resolved_at
                        )
                    RETURNING
                        id::text,
                        workspace_id::text,
                        actor_user_id::text,
                        provider,
                        token_secret_ref,
                        status,
                        attempt_count,
                        failure_reason,
                        redacted_metadata,
                        created_at,
                        resolved_at
                    """,
                    (
                        cleanup_id,
                        workspace_id,
                        actor_user_id,
                        provider.value,
                        token_secret_ref,
                        status.value,
                        safe_reason,
                        json.dumps(safe_meta),
                        now,
                        now,
                        resolved_at,
                    ),
                )
                row = cur.fetchone()
                assert row is not None

        return OrphanSecretCleanupRecord(
            id=row[0],
            workspace_id=row[1],
            actor_user_id=row[2],
            provider=OAuthProvider(row[3]),
            token_secret_ref=row[4],
            status=OrphanSecretCleanupStatus(row[5]),
            attempt_count=int(row[6]),
            failure_reason=row[7],
            redacted_metadata=row[8] if isinstance(row[8], dict) else {},
            created_at=row[9],
            resolved_at=row[10],
        )


class OrphanSecretCleaner:
    """Idempotent orphan secret deletion + redacted audit/outbox recorder."""

    def __init__(
        self,
        *,
        secret_store: SecretStorePort,
        cleanup_store: OAuthBoundaryStorePort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._secret_store = secret_store
        self._cleanup_store = cleanup_store
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def cleanup_orphan_secret(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        provider: OAuthProvider,
        token_secret_ref: str,
        failure_reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> OrphanSecretCleanupRecord:
        now = self._clock()
        safe_reason = redact_text(failure_reason)
        safe_meta = redact_mapping(dict(metadata or {}))

        try:
            existed = self._secret_store.delete_secret(token_secret_ref)
            status = OrphanSecretCleanupStatus.DELETED
            safe_meta["secret_existed_before_delete"] = bool(existed)
        except Exception as exc:  # noqa: BLE001
            status = OrphanSecretCleanupStatus.RETRY_PENDING
            safe_meta["delete_error"] = redact_text(f"{type(exc).__name__}: {exc}")
            logger.warning(
                "Orphan secret deletion failed and queued for retry: workspace=%s ref=%s",
                workspace_id,
                token_secret_ref,
            )

        return self._cleanup_store.record_orphan_secret_cleanup(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            provider=provider,
            token_secret_ref=token_secret_ref,
            status=status,
            failure_reason=safe_reason,
            redacted_metadata=safe_meta,
            now=now,
        )


class OAuthConnectionCoordinator:
    """Coordinates OAuth connect start, single-use callback, and orphan secret cleanup."""

    def __init__(
        self,
        *,
        boundary_store: OAuthBoundaryStorePort,
        secret_store: SecretStorePort,
        token_exchanger: OAuthTokenExchangerPort,
        live_session_verifier: LiveSessionVerifierPort | None = None,
        clock: Callable[[], datetime] | None = None,
        authorize_base_urls: Mapping[OAuthProvider, str] | None = None,
    ) -> None:
        self._boundary_store = boundary_store
        self._secret_store = secret_store
        self._token_exchanger = token_exchanger
        self._live_session_verifier = live_session_verifier
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._cleaner = OrphanSecretCleaner(
            secret_store=secret_store,
            cleanup_store=boundary_store,
            clock=self._clock,
        )
        self._authorize_base_urls: dict[OAuthProvider, str] = {
            OAuthProvider.GITHUB: "https://github.com/login/oauth/authorize",
            **(dict(authorize_base_urls) if authorize_base_urls else {}),
        }

    def _enforce_role_and_live_session(
        self,
        access: WorkspaceAccessContext,
        *,
        allowed_roles: frozenset[str],
        verified_claims: VerifiedJwtClaims | None,
    ) -> None:
        if access.role not in allowed_roles:
            raise WorkspaceMembershipDeniedError(
                "Workspace role does not permit external source connection mutations."
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

    def start_connect(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        return_path: str,
        redirect_uri: str,
        ttl_seconds: int = 600,
        allowed_roles: frozenset[str] = _CONNECT_ALLOWED_ROLES,
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> OAuthConnectStartResult:
        """Create a single-use hashed OAuth state + PKCE verifier ref."""
        self._enforce_role_and_live_session(
            access,
            allowed_roles=allowed_roles,
            verified_claims=verified_claims,
        )
        safe_return_path = validate_return_path(return_path)
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("ttl_seconds must be between 1 and 3600.")

        now = self._clock()
        expires_at = now + timedelta(seconds=ttl_seconds)
        raw_state = secrets.token_urlsafe(32)
        state_hash = hash_oauth_state(raw_state)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = derive_pkce_s256_challenge(code_verifier)

        verifier_ref = self._secret_store.store_secret(
            _PKCE_NAMESPACE,
            code_verifier,
            metadata={"workspace_id": access.workspace_id, "provider": provider.value},
        )
        record = OAuthStateRecord(
            state_hash=state_hash,
            workspace_id=access.workspace_id,
            actor_user_id=access.user_id,
            provider=provider,
            pkce_code_challenge=code_challenge,
            pkce_code_verifier_ref=verifier_ref,
            return_path=safe_return_path,
            expires_at=expires_at,
            created_at=now,
        )
        try:
            self._boundary_store.create_state(access, record)
        except Exception:
            # Clean up PKCE verifier ref on DB insert failure so nothing orphans.
            try:
                self._secret_store.delete_secret(verifier_ref)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to delete PKCE verifier secret after state insert failure: ref=%s",
                    verifier_ref,
                )
            raise

        base_url = self._authorize_base_urls[provider]
        query = urlencode(
            {
                "state": raw_state,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return OAuthConnectStartResult(
            provider=provider,
            authorization_url=f"{base_url}?{query}",
            state=raw_state,
            pkce_code_challenge=code_challenge,
            return_path=safe_return_path,
            expires_at=expires_at,
        )

    def handle_callback(
        self,
        access: WorkspaceAccessContext,
        *,
        provider: OAuthProvider,
        state: str,
        code: str,
        redirect_uri: str,
        request_id: str,
        allowed_roles: frozenset[str] = _CONNECT_ALLOWED_ROLES,
        verified_claims: VerifiedJwtClaims | None = None,
    ) -> OAuthCallbackResult:
        """Consume state atomically, exchange code, store secret ref, and cleanup on DB failure."""
        self._enforce_role_and_live_session(
            access,
            allowed_roles=allowed_roles,
            verified_claims=verified_claims,
        )
        if not isinstance(state, str) or not state.strip():
            raise OAuthStateInvalidOrReplayedError("OAuth state is missing.")
        if not isinstance(code, str) or not code.strip():
            raise OAuthTokenExchangeError("OAuth authorization code is missing.")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be non-empty.")

        now = self._clock()
        state_hash = hash_oauth_state(state.strip())

        consumed = self._boundary_store.consume_state_atomically(
            access,
            provider=provider,
            state_hash=state_hash,
            now=now,
            request_id=request_id.strip(),
        )
        if consumed is None:
            raise OAuthStateInvalidOrReplayedError(
                "OAuth state is invalid, expired, or already consumed."
            )

        safe_return_path = validate_return_path(consumed.return_path)
        verifier_ref = consumed.pkce_code_verifier_ref
        try:
            code_verifier = self._secret_store.get_secret(verifier_ref)
            if not code_verifier:
                raise OAuthStateInvalidOrReplayedError(
                    "OAuth PKCE verifier is unavailable or already consumed."
                )
            try:
                exchanged = self._token_exchanger.exchange_authorization_code(
                    provider=provider,
                    code=code.strip(),
                    pkce_code_verifier=code_verifier,
                    redirect_uri=redirect_uri,
                )
            except OAuthBoundaryError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise OAuthTokenExchangeError(
                    redact_text(f"OAuth token exchange failed: {type(exc).__name__}")
                ) from exc
        finally:
            try:
                self._secret_store.delete_secret(verifier_ref)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Unable to delete one-time PKCE verifier ref=%s", verifier_ref
                )

        token_secret_ref = self._secret_store.store_secret(
            _TOKEN_NAMESPACE,
            exchanged.access_token,
            metadata={
                "workspace_id": access.workspace_id,
                "provider": provider.value,
                "external_account_id": exchanged.external_account_id,
            },
        )

        try:
            source_account = self._boundary_store.upsert_active_source_account(
                access,
                provider=provider,
                external_account_id=exchanged.external_account_id,
                token_secret_ref=token_secret_ref,
                scopes=exchanged.scopes,
                now=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001
            redacted_failure = redact_text(f"{type(exc).__name__}: {exc}")
            logger.warning(
                "Source account persistence failed after token exchange; cleaning up orphan secret "
                "workspace=%s ref=%s reason=%s",
                access.workspace_id,
                token_secret_ref,
                redacted_failure,
            )
            cleanup_record = self._cleaner.cleanup_orphan_secret(
                workspace_id=access.workspace_id,
                actor_user_id=access.user_id,
                provider=provider,
                token_secret_ref=token_secret_ref,
                failure_reason=redacted_failure,
                metadata={
                    "request_id": request_id.strip(),
                    "provider": provider.value,
                    "external_account_id": exchanged.external_account_id,
                    "state_hash": state_hash,
                },
            )
            raise OAuthPersistenceFailedError(
                "Failed to persist connected source account; orphan token cleanup executed.",
                cleanup_record=cleanup_record,
            ) from exc

        return OAuthCallbackResult(
            source_account=source_account,
            return_path=safe_return_path,
        )
