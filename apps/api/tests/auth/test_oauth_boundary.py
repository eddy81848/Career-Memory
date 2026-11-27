"""Unit & contract tests for OAuth state single-use, replay defense, and orphan secret cleanup."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from career_memory_api.auth.jwt_verifier import LiveSessionStatus, VerifiedJwtClaims
from career_memory_api.auth.oauth import (
    ExchangedOAuthToken,
    InMemoryOAuthBoundaryStore,
    InMemorySecretStore,
    OAuthConnectionCoordinator,
    OAuthPersistenceFailedError,
    OAuthProvider,
    OAuthStateInvalidOrReplayedError,
    OrphanSecretCleaner,
    OrphanSecretCleanupStatus,
)
from career_memory_api.auth.return_path import InvalidReturnPathError
from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    AuthenticatedUser,
    AuthSubject,
    SessionTerminatedError,
    WorkspaceAccessContext,
    WorkspaceMembership,
    WorkspaceMembershipDeniedError,
)


def _access(
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
    role: str = "OWNER",
) -> WorkspaceAccessContext:
    uid = user_id or str(uuid.uuid4())
    wid = workspace_id or str(uuid.uuid4())
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=uid,
            auth_subject=AuthSubject(
                issuer="https://auth.career-memory.local/auth/v1",
                subject=f"sub-{uid}",
            ),
        ),
        membership=WorkspaceMembership(
            workspace_id=wid,
            user_id=uid,
            role=role,
        ),
    )


class _StubTokenExchanger:
    def __init__(
        self,
        *,
        access_token: str = "gho_secret_oauth_token_999999",
        external_account_id: str = "gh-acct-42",
        scopes: tuple[str, ...] = ("read:user", "public_repo"),
        error: Exception | None = None,
    ) -> None:
        self._access_token = access_token
        self._external_account_id = external_account_id
        self._scopes = scopes
        self._error = error
        self.calls: list[dict[str, str]] = []

    def exchange_authorization_code(
        self,
        *,
        provider: OAuthProvider,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> ExchangedOAuthToken:
        self.calls.append(
            {
                "provider": provider.value,
                "code": code,
                "pkce_code_verifier": pkce_code_verifier,
                "redirect_uri": redirect_uri,
            }
        )
        if self._error is not None:
            raise self._error
        return ExchangedOAuthToken(
            provider=provider,
            external_account_id=self._external_account_id,
            access_token=self._access_token,
            scopes=self._scopes,
        )


def test_oauth_start_and_callback_happy_path_and_single_use_replay_block() -> None:
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    store = InMemoryOAuthBoundaryStore()
    secrets_store = InMemorySecretStore()
    exchanger = _StubTokenExchanger()
    coordinator = OAuthConnectionCoordinator(
        boundary_store=store,
        secret_store=secrets_store,
        token_exchanger=exchanger,
        clock=lambda: now,
    )
    access = _access(role="OWNER")

    started = coordinator.start_connect(
        access,
        provider=OAuthProvider.GITHUB,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    # Raw state MUST NOT be persisted in the state store
    assert started.state not in store.raw_state_dump()
    assert len(store.states) == 1

    raw_code = "gh_auth_code_secret_abc123"
    result = coordinator.handle_callback(
        access,
        provider=OAuthProvider.GITHUB,
        state=started.state,
        code=raw_code,
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
        request_id="req-cb-1",
    )

    assert result.return_path == "/settings/sources"
    assert result.source_account.workspace_id == access.workspace_id
    assert result.source_account.external_account_id == "gh-acct-42"
    assert result.source_account.status == "ACTIVE"
    # Raw access token MUST NOT be stored in source_accounts DB record
    assert "gho_secret_oauth_token_999999" not in repr(result.source_account)
    assert "gho_secret_oauth_token_999999" not in result.source_account.token_secret_ref
    assert (
        secrets_store.get_secret(result.source_account.token_secret_ref)
        == "gho_secret_oauth_token_999999"
    )
    # One-time PKCE verifier was deleted from secret store after exchange
    assert len(secrets_store.all_refs()) == 1
    assert len(exchanger.calls) == 1

    # Replay with the exact same state MUST fail before touching token exchanger
    with pytest.raises(OAuthStateInvalidOrReplayedError):
        coordinator.handle_callback(
            access,
            provider=OAuthProvider.GITHUB,
            state=started.state,
            code="gh_auth_code_replay",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
            request_id="req-cb-2",
        )
    assert len(exchanger.calls) == 1
    assert len(store.source_accounts) == 1


def test_oauth_rejects_viewer_role_open_redirect_expired_and_cross_workspace_state() -> None:
    current_time = [datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)]
    store = InMemoryOAuthBoundaryStore()
    secrets_store = InMemorySecretStore()
    exchanger = _StubTokenExchanger()
    coordinator = OAuthConnectionCoordinator(
        boundary_store=store,
        secret_store=secrets_store,
        token_exchanger=exchanger,
        clock=lambda: current_time[0],
    )

    # VIEWER cannot start or finish OAuth connect
    viewer = _access(role="VIEWER")
    with pytest.raises(WorkspaceMembershipDeniedError):
        coordinator.start_connect(
            viewer,
            provider=OAuthProvider.GITHUB,
            return_path="/settings",
            redirect_uri="https://app.career-memory.local/cb",
        )

    owner = _access(role="OWNER")
    # Open redirect rejected before storing anything
    for evil in ("https://evil.example", "//evil.example/steal"):
        with pytest.raises(InvalidReturnPathError):
            coordinator.start_connect(
                owner,
                provider=OAuthProvider.GITHUB,
                return_path=evil,
                redirect_uri="https://app.career-memory.local/cb",
            )
    assert len(store.states) == 0
    assert len(secrets_store.all_refs()) == 0

    # Cross-workspace or cross-user callback cannot consume owner's state
    started = coordinator.start_connect(
        owner,
        provider=OAuthProvider.GITHUB,
        return_path="/settings",
        redirect_uri="https://app.career-memory.local/cb",
        ttl_seconds=60,
    )
    other_ws_access = _access(user_id=owner.user_id, role="OWNER")
    with pytest.raises(OAuthStateInvalidOrReplayedError):
        coordinator.handle_callback(
            other_ws_access,
            provider=OAuthProvider.GITHUB,
            state=started.state,
            code="code-1",
            redirect_uri="https://app.career-memory.local/cb",
            request_id="req-cross-ws",
        )

    # Expired state rejected
    current_time[0] += timedelta(seconds=61)
    with pytest.raises(OAuthStateInvalidOrReplayedError):
        coordinator.handle_callback(
            owner,
            provider=OAuthProvider.GITHUB,
            state=started.state,
            code="code-2",
            redirect_uri="https://app.career-memory.local/cb",
            request_id="req-expired",
        )
    assert len(exchanger.calls) == 0


def test_oauth_db_failure_after_token_exchange_cleans_orphan_secret_idempotently_and_redacts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    raw_token = "gho_super_sensitive_orphan_token_xyz"
    raw_code = "gh_sensitive_code_xyz"
    store = InMemoryOAuthBoundaryStore(
        fail_next_source_account_write=RuntimeError(
            f"db commit failed while saving code={raw_code} token={raw_token}"
        )
    )
    secrets_store = InMemorySecretStore()
    exchanger = _StubTokenExchanger(access_token=raw_token)
    coordinator = OAuthConnectionCoordinator(
        boundary_store=store,
        secret_store=secrets_store,
        token_exchanger=exchanger,
        clock=lambda: now,
    )
    access = _access(role="OWNER")

    started = coordinator.start_connect(
        access,
        provider=OAuthProvider.GITHUB,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/cb",
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(OAuthPersistenceFailedError) as exc_info:
            coordinator.handle_callback(
                access,
                provider=OAuthProvider.GITHUB,
                state=started.state,
                code=raw_code,
                redirect_uri="https://app.career-memory.local/cb",
                request_id="req-fail-db",
            )

    # Orphan token AND PKCE verifier must be completely gone from SecretStore
    assert secrets_store.all_refs() == frozenset()
    assert len(store.source_accounts) == 0

    cleanup = exc_info.value.cleanup_record
    assert cleanup is not None
    assert cleanup.status is OrphanSecretCleanupStatus.DELETED
    assert cleanup.attempt_count == 1
    assert len(store.cleanup_records) == 1

    # 0 bytes of raw token, code, or state anywhere in exception, cleanup record, or logs
    combined_artifacts = " ".join(
        [
            str(exc_info.value),
            repr(cleanup),
            cleanup.failure_reason,
            str(cleanup.redacted_metadata),
            caplog.text,
        ]
    )
    for forbidden in (raw_token, raw_code, started.state):
        assert forbidden not in combined_artifacts

    # Idempotent repeated cleanup call succeeds cleanly without duplicating records
    cleaner = OrphanSecretCleaner(
        secret_store=secrets_store,
        cleanup_store=store,
        clock=lambda: now,
    )
    second = cleaner.cleanup_orphan_secret(
        workspace_id=access.workspace_id,
        actor_user_id=access.user_id,
        provider=OAuthProvider.GITHUB,
        token_secret_ref=cleanup.token_secret_ref,
        failure_reason="retry cleanup",
        metadata={"request_id": "req-fail-db", "access_token": raw_token},
    )
    assert second.status is OrphanSecretCleanupStatus.DELETED
    assert len(store.cleanup_records) == 1
    assert raw_token not in str(second.redacted_metadata)


def test_orphan_secret_cleanup_records_retry_pending_when_secret_store_delete_fails() -> None:
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    raw_token = "gho_retry_pending_token_123"
    store = InMemoryOAuthBoundaryStore(
        fail_next_source_account_write=RuntimeError("db write failed")
    )
    secrets_store = InMemorySecretStore()
    secrets_store.fail_delete_once_for_namespace("github-oauth-token")
    exchanger = _StubTokenExchanger(access_token=raw_token)
    coordinator = OAuthConnectionCoordinator(
        boundary_store=store,
        secret_store=secrets_store,
        token_exchanger=exchanger,
        clock=lambda: now,
    )
    access = _access(role="OWNER")
    started = coordinator.start_connect(
        access,
        provider=OAuthProvider.GITHUB,
        return_path="/settings",
        redirect_uri="https://app.career-memory.local/cb",
    )

    with pytest.raises(OAuthPersistenceFailedError) as exc_info:
        coordinator.handle_callback(
            access,
            provider=OAuthProvider.GITHUB,
            state=started.state,
            code="code-retry-1",
            redirect_uri="https://app.career-memory.local/cb",
            request_id="req-retry-1",
        )

    cleanup = exc_info.value.cleanup_record
    assert cleanup is not None
    assert cleanup.status is OrphanSecretCleanupStatus.RETRY_PENDING
    assert secrets_store.get_secret(cleanup.token_secret_ref) == raw_token

    # Subsequent retry deletes secret and transitions cleanup row to DELETED
    cleaner = OrphanSecretCleaner(
        secret_store=secrets_store,
        cleanup_store=store,
        clock=lambda: now + timedelta(seconds=15),
    )
    retried = cleaner.cleanup_orphan_secret(
        workspace_id=access.workspace_id,
        actor_user_id=access.user_id,
        provider=OAuthProvider.GITHUB,
        token_secret_ref=cleanup.token_secret_ref,
        failure_reason="retry_worker",
        metadata={"request_id": "req-retry-1"},
    )
    assert retried.status is OrphanSecretCleanupStatus.DELETED
    assert retried.attempt_count == 2
    assert secrets_store.get_secret(cleanup.token_secret_ref) is None


def test_oauth_callback_blocks_before_side_effects_on_live_session_failure() -> None:
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    store = InMemoryOAuthBoundaryStore()
    secrets_store = InMemorySecretStore()
    exchanger = _StubTokenExchanger()
    access = _access(role="OWNER")

    base_coord = OAuthConnectionCoordinator(
        boundary_store=store,
        secret_store=secrets_store,
        token_exchanger=exchanger,
        clock=lambda: now,
    )
    started = base_coord.start_connect(
        access,
        provider=OAuthProvider.GITHUB,
        return_path="/settings",
        redirect_uri="https://app.career-memory.local/cb",
    )

    class _UnavailableSessionVerifier:
        def verify_live_session(self, claims: VerifiedJwtClaims) -> LiveSessionStatus:
            raise AccessVerificationUnavailableError("auth check down")

    class _TerminatedSessionVerifier:
        def verify_live_session(self, claims: VerifiedJwtClaims) -> LiveSessionStatus:
            return LiveSessionStatus.SESSION_TERMINATED

    claims = VerifiedJwtClaims(
        auth_subject=access.user.auth_subject,
        issuer=access.user.auth_subject.issuer,
        subject=access.user.auth_subject.subject,
        audience=("authenticated",),
        expires_at=now + timedelta(minutes=5),
    )

    coord_unavail = OAuthConnectionCoordinator(
        boundary_store=store,
        secret_store=secrets_store,
        token_exchanger=exchanger,
        live_session_verifier=_UnavailableSessionVerifier(),
        clock=lambda: now,
    )
    with pytest.raises(AccessVerificationUnavailableError):
        coord_unavail.handle_callback(
            access,
            provider=OAuthProvider.GITHUB,
            state=started.state,
            code="code-blocked",
            redirect_uri="https://app.career-memory.local/cb",
            request_id="req-unavail",
            verified_claims=claims,
        )
    # 0 writes, 0 token exchanges, state NOT consumed
    assert len(exchanger.calls) == 0
    assert all(s.consumed_at is None for s in store.states.values())

    coord_term = OAuthConnectionCoordinator(
        boundary_store=store,
        secret_store=secrets_store,
        token_exchanger=exchanger,
        live_session_verifier=_TerminatedSessionVerifier(),
        clock=lambda: now,
    )
    with pytest.raises(SessionTerminatedError):
        coord_term.handle_callback(
            access,
            provider=OAuthProvider.GITHUB,
            state=started.state,
            code="code-blocked",
            redirect_uri="https://app.career-memory.local/cb",
            request_id="req-term",
            verified_claims=claims,
        )
    assert len(exchanger.calls) == 0
    assert all(s.consumed_at is None for s in store.states.values())
