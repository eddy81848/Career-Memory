"""PostgreSQL transaction-local RLS context binding and Workspace-scoped guards."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import psycopg

from career_memory_api.auth.jwt_verifier import (
    JwtAccessTokenVerifier,
    LiveSessionVerifierPort,
)
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError


class WorkspaceRole(StrEnum):
    """Canonical Workspace membership roles matching migration 0010."""

    OWNER = "OWNER"
    MEMBER = "MEMBER"
    VIEWER = "VIEWER"


@dataclass(frozen=True, slots=True)
class SourceAccountRecord:
    """Workspace-scoped source account metadata (never stores raw provider tokens)."""

    id: str
    workspace_id: str
    provider: str
    external_account_id: str
    token_secret_ref: str
    scopes: tuple[str, ...]
    status: str
    created_at: datetime
    revoked_at: datetime | None = None


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


def bind_postgres_workspace_context(
    cur: psycopg.Cursor, access: WorkspaceAccessContext
) -> None:
    """Bind transaction-local ``app.current_workspace_id`` and ``app.current_user_id``.

    Uses ``set_config(..., true)`` (``SET LOCAL`` semantics) so context resets
    automatically on commit or rollback, preventing cross-tenant leakage on
    pooled/reused connections.
    """
    workspace_uuid = _normalize_uuid(access.workspace_id, "workspace_id")
    user_uuid = _normalize_uuid(access.user_id, "user_id")
    cur.execute(
        """
        SELECT
            set_config('app.current_workspace_id', %s, true),
            set_config('app.current_user_id', %s, true)
        """,
        (workspace_uuid, user_uuid),
    )


def bind_postgres_identity_lookup_context(
    cur: psycopg.Cursor, auth_subject: AuthSubject
) -> None:
    """Bind transaction-local verified ``(issuer, sub)`` for RLS-enforced ``users`` lookup."""
    cur.execute(
        """
        SELECT
            set_config('app.current_auth_issuer', %s, true),
            set_config('app.current_auth_subject', %s, true)
        """,
        (auth_subject.issuer, auth_subject.subject),
    )


@contextmanager
def workspace_scoped_transaction(
    conn: psycopg.Connection, access: WorkspaceAccessContext
) -> Iterator[psycopg.Cursor]:
    """Open a PostgreSQL transaction and bind transaction-local Workspace/User GUCs."""
    with conn.transaction():
        with conn.cursor() as cur:
            bind_postgres_workspace_context(cur, access)
            yield cur


class PostgresWorkspaceAccessProvider:
    """Real PostgreSQL ``WorkspaceAccessProvider`` backed by JWT verification + FORCE RLS."""

    def __init__(
        self,
        conn: psycopg.Connection,
        token_verifier: JwtAccessTokenVerifier,
        *,
        live_session_verifier: LiveSessionVerifierPort | None = None,
    ) -> None:
        self._conn = conn
        self._token_verifier = token_verifier
        self._live_session_verifier = live_session_verifier

    def verify_identity(
        self, credentials: AuthCredentials
    ) -> AuthenticatedUser | None:
        claims = self._token_verifier.verify(
            credentials.bearer_token,
            live_session_verifier=self._live_session_verifier,
        )
        try:
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    bind_postgres_identity_lookup_context(cur, claims.auth_subject)
                    cur.execute(
                        """
                        SELECT id::text, auth_issuer, auth_subject
                        FROM public.users
                        WHERE auth_issuer = %s
                          AND auth_subject = %s
                        """,
                        (claims.auth_subject.issuer, claims.auth_subject.subject),
                    )
                    row = cur.fetchone()
        except (
            UnauthenticatedError,
            SessionTerminatedError,
            AccessVerificationUnavailableError,
        ):
            raise
        except Exception as exc:  # noqa: BLE001
            raise AccessVerificationUnavailableError(
                "User identity lookup failed due to database unavailability."
            ) from exc

        if row is None:
            return None

        return AuthenticatedUser(
            user_id=row[0],
            auth_subject=AuthSubject(issuer=row[1], subject=row[2]),
        )

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        try:
            workspace_uuid = _normalize_uuid(target_workspace_id, "target_workspace_id")
            user_uuid = _normalize_uuid(user.user_id, "user_id")
        except ValueError:
            return None

        try:
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('app.current_user_id', %s, true)",
                        (user_uuid,),
                    )
                    cur.execute(
                        """
                        SELECT workspace_id::text, user_id::text, role
                        FROM public.workspace_memberships
                        WHERE workspace_id = %s::uuid
                          AND user_id = %s::uuid
                          AND revoked_at IS NULL
                        """,
                        (workspace_uuid, user_uuid),
                    )
                    row = cur.fetchone()
        except AccessVerificationUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AccessVerificationUnavailableError(
                "Workspace membership verification failed due to database unavailability."
            ) from exc

        if row is None:
            return None

        return WorkspaceMembership(
            workspace_id=row[0],
            user_id=row[1],
            role=row[2],
        )


class PostgresSourceAccountRepository:
    """Workspace-scoped PostgreSQL repository for ``public.source_accounts``.

    Intentionally exposes NO bare ``get_by_id(id)`` method; every operation
    requires a server-verified ``WorkspaceAccessContext`` as its first argument
    and executes inside a transaction-local RLS-scoped transaction.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get_by_workspace_and_id(
        self,
        access: WorkspaceAccessContext,
        source_account_id: str,
    ) -> SourceAccountRecord | None:
        try:
            account_uuid = _normalize_uuid(source_account_id, "source_account_id")
        except ValueError:
            return None

        with workspace_scoped_transaction(self._conn, access) as cur:
            cur.execute(
                """
                SELECT
                    id::text,
                    workspace_id::text,
                    provider,
                    external_account_id,
                    token_secret_ref,
                    scopes,
                    status,
                    created_at,
                    revoked_at
                FROM public.source_accounts
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (access.workspace_id, account_uuid),
            )
            row = cur.fetchone()

        if row is None:
            return None

        record = SourceAccountRecord(
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
        if record.workspace_id != access.workspace_id:
            return None
        return record

    def read_or_conceal(
        self,
        access: WorkspaceAccessContext,
        source_account_id: str,
    ) -> SourceAccountRecord:
        """Return the account or raise identical ``WorkspaceResourceNotFoundError``."""
        record = self.get_by_workspace_and_id(access, source_account_id)
        if record is None:
            raise WorkspaceResourceNotFoundError()
        return record

    def list_by_workspace(
        self,
        access: WorkspaceAccessContext,
    ) -> list[SourceAccountRecord]:
        with workspace_scoped_transaction(self._conn, access) as cur:
            cur.execute(
                """
                SELECT
                    id::text,
                    workspace_id::text,
                    provider,
                    external_account_id,
                    token_secret_ref,
                    scopes,
                    status,
                    created_at,
                    revoked_at
                FROM public.source_accounts
                WHERE workspace_id = %s::uuid
                ORDER BY created_at DESC, id ASC
                """,
                (access.workspace_id,),
            )
            rows = cur.fetchall()

        return [
            SourceAccountRecord(
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
            for row in rows
            if row[1] == access.workspace_id
        ]
