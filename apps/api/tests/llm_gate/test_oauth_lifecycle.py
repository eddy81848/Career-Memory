"""INF-08 tests for Subscription OAuth / PKCE / OIDC / Consent / Refresh / Unlink lifecycle."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from career_memory_api.llm_gate.models import (
    ConnectionJobReference,
    LlmGateReasonCode,
    LlmLifecycleError,
    LlmProviderMode,
    LlmStageCode,
    RemoteRevocationStatus,
    SecretWipeStatus,
    SubscriptionConnectionStatus,
)
from career_memory_api.llm_gate.oauth_lifecycle import (
    InMemorySecretVault,
    OidcIdTokenClaims,
    RemoteRevokeResult,
    SubscriptionConnectionLifecycleManager,
    SubscriptionOAuthConfig,
    SubscriptionTokenGrant,
)

from .conftest import (
    ALLOWED_REDIRECT_URI,
    EXPECTED_OIDC_AUDIENCE,
    EXPECTED_OIDC_ISSUER,
    PLAN_INFERENCE_SCOPE,
    SOURCE_RESOURCE_A,
    FakeClock,
)


class MockSubscriptionExchanger:
    """Deterministic in-memory SubscriptionOAuthExchangePort for offline tests."""

    def __init__(
        self,
        *,
        clock: FakeClock,
        nonce_holder: list[str],
        issuer: str = EXPECTED_OIDC_ISSUER,
        audience: str = EXPECTED_OIDC_AUDIENCE,
        subject: str = "oidc-sub-alice-001",
        email: str | None = "alice@example.com",
        scopes: tuple[str, ...] = ("openid", PLAN_INFERENCE_SCOPE),
        plan_tier: str = "PLUS",
        plan_inference_eligible: bool = True,
        exp_offset_seconds: int = 600,
        refresh_barrier: threading.Barrier | None = None,
    ) -> None:
        self._clock = clock
        self._nonce_holder = nonce_holder
        self.issuer = issuer
        self.audience = audience
        self.subject = subject
        self.email = email
        self.scopes = scopes
        self.plan_tier = plan_tier
        self.plan_inference_eligible = plan_inference_eligible
        self.exp_offset_seconds = exp_offset_seconds
        self.refresh_barrier = refresh_barrier
        self.exchange_calls = 0
        self.refresh_calls = 0
        self._lock = threading.Lock()

    def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> SubscriptionTokenGrant:
        with self._lock:
            self.exchange_calls += 1
        now = self._clock()
        nonce = self._nonce_holder[-1] if self._nonce_holder else "missing-nonce"
        return SubscriptionTokenGrant(
            access_token=f"at_secret_exchange_{self.exchange_calls}_{code}",
            refresh_token=f"rt_secret_exchange_{self.exchange_calls}_{code}",
            expires_in_seconds=3600,
            granted_scopes=self.scopes,
            plan_tier=self.plan_tier,
            plan_inference_eligible=self.plan_inference_eligible,
            id_token_claims=OidcIdTokenClaims(
                iss=self.issuer,
                aud=self.audience,
                nonce=nonce,
                exp=int((now + timedelta(seconds=self.exp_offset_seconds)).timestamp()),
                sub=self.subject,
                email=self.email,
            ),
        )

    def refresh_tokens(
        self, *, refresh_token: str, external_subject: str
    ) -> SubscriptionTokenGrant:
        if self.refresh_barrier is not None:
            self.refresh_barrier.wait(timeout=5.0)
        with self._lock:
            self.refresh_calls += 1
            n = self.refresh_calls
        now = self._clock()
        return SubscriptionTokenGrant(
            access_token=f"at_secret_refreshed_{n}",
            refresh_token=f"rt_secret_refreshed_{n}",
            expires_in_seconds=3600,
            granted_scopes=self.scopes,
            plan_tier=self.plan_tier,
            plan_inference_eligible=self.plan_inference_eligible,
            id_token_claims=OidcIdTokenClaims(
                iss=self.issuer,
                aud=self.audience,
                nonce="refresh-unused",
                exp=int((now + timedelta(seconds=600)).timestamp()),
                sub=external_subject,
                email=self.email,
            ),
        )


class RecordingRemoteRevoker:
    """Records revoke calls and lets tests verify local-fence-first ordering and failures."""

    def __init__(
        self,
        *,
        manager: SubscriptionConnectionLifecycleManager | None = None,
        fail_with: Exception | None = None,
        confirmed: bool = True,
    ) -> None:
        self._manager = manager
        self._fail_with = fail_with
        self._confirmed = confirmed
        self.calls: list[dict[str, object]] = []
        self.observed_status_during_revoke: SubscriptionConnectionStatus | None = None
        self.observed_secret_still_in_vault_during_revoke: bool | None = None

    def revoke(
        self,
        *,
        provider: LlmProviderMode,
        external_subject: str,
        token_secret_ref: str,
        access_token: str | None,
        refresh_token: str | None,
    ) -> RemoteRevokeResult:
        if self._manager is not None:
            rec = self._manager.get_connection_by_subject(
                oidc_issuer=EXPECTED_OIDC_ISSUER, oidc_subject=external_subject
            )
            if rec is not None:
                self.observed_status_during_revoke = rec.status
            self.observed_secret_still_in_vault_during_revoke = (
                self._manager.secret_vault.get_secret(token_secret_ref) is not None
            )
        self.calls.append(
            {
                "provider": provider.value,
                "external_subject": external_subject,
                "token_secret_ref": token_secret_ref,
                "had_access_token": access_token is not None,
                "had_refresh_token": refresh_token is not None,
            }
        )
        if self._fail_with is not None:
            raise self._fail_with
        return RemoteRevokeResult(
            confirmed=self._confirmed,
            status=(
                RemoteRevocationStatus.CONFIRMED_REMOTE
                if self._confirmed
                else RemoteRevocationStatus.UNCONFIRMED_REMOTE
            ),
        )


def _make_manager(clock: FakeClock) -> tuple[SubscriptionConnectionLifecycleManager, InMemorySecretVault]:
    vault = InMemorySecretVault()
    cfg = SubscriptionOAuthConfig(
        allowed_redirect_uris=frozenset({ALLOWED_REDIRECT_URI}),
        expected_oidc_issuer=EXPECTED_OIDC_ISSUER,
        expected_oidc_audience=EXPECTED_OIDC_AUDIENCE,
        required_plan_inference_scopes=frozenset({PLAN_INFERENCE_SCOPE}),
    )
    return SubscriptionConnectionLifecycleManager(config=cfg, secret_vault=vault, clock=clock), vault


def test_connect_start_and_callback_pkce_oidc_and_single_use_state(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, vault = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)

    # Raw state and verifier must NOT appear in internal state dumps or repr.
    assert start.state not in manager.debug_state_repr()
    assert start.state not in repr(start)
    assert start.nonce not in manager.debug_state_repr()
    assert start.pkce_code_challenge != ""

    exchanger = MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce])
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="auth-code-001",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=exchanger,
    )
    assert conn.status is SubscriptionConnectionStatus.ACTIVE
    assert conn.connection_version == 1
    assert conn.token_secret_ref is not None
    # Plaintext tokens never appear in record repr/str/safe_dict
    assert "at_secret_exchange" not in repr(conn)
    assert "rt_secret_exchange" not in repr(conn)
    assert "at_secret_exchange" not in str(conn.to_safe_dict())
    assert "token_secret_ref" not in conn.to_safe_dict()

    # Replaying the exact same single-use state MUST fail closed.
    with pytest.raises(LlmLifecycleError) as replay_err:
        manager.complete_callback(
            owner_a_access,
            state=start.state,
            code="auth-code-002",
            redirect_uri=ALLOWED_REDIRECT_URI,
            exchanger=exchanger,
        )
    assert replay_err.value.code is LlmGateReasonCode.OAUTH_STATE_INVALID_OR_REPLAYED
    assert exchanger.exchange_calls == 1
    # Only the stored token secret ref remains in vault (ephemeral PKCE verifier wiped).
    assert len(vault.all_refs()) == 1


def test_rejects_unallowlisted_redirect_uri_and_pkce_mismatch(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, vault = _make_manager(fake_clock)
    with pytest.raises(LlmLifecycleError):
        manager.start_connect(
            owner_a_access,
            redirect_uri="https://evil.example.com/callback",
        )

    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    exchanger = MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce])
    with pytest.raises(LlmLifecycleError):
        manager.complete_callback(
            owner_a_access,
            state=start.state,
            code="auth-code-001",
            redirect_uri=ALLOWED_REDIRECT_URI,
            exchanger=exchanger,
            provided_code_verifier="wrong-pkce-verifier-value",
        )
    assert exchanger.exchange_calls == 0
    assert len(vault.all_refs()) == 0


@pytest.mark.parametrize("bad_field", ["iss", "aud", "nonce", "exp", "sub"])
def test_oidc_claim_verification_rejects_invalid_claims(
    fake_clock: FakeClock, owner_a_access, bad_field: str
) -> None:
    manager, vault = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    kwargs: dict[str, object] = {
        "clock": fake_clock,
        "nonce_holder": [start.nonce],
    }
    if bad_field == "iss":
        kwargs["issuer"] = "https://untrusted.issuer.example"
    elif bad_field == "aud":
        kwargs["audience"] = "wrong-client-id"
    elif bad_field == "nonce":
        kwargs["nonce_holder"] = ["wrong-nonce-value"]
    elif bad_field == "exp":
        kwargs["exp_offset_seconds"] = -120
    elif bad_field == "sub":
        kwargs["subject"] = ""
    exchanger = MockSubscriptionExchanger(**kwargs)  # type: ignore[arg-type]

    with pytest.raises(LlmLifecycleError):
        manager.complete_callback(
            owner_a_access,
            state=start.state,
            code="code-oidc-bad",
            redirect_uri=ALLOWED_REDIRECT_URI,
            exchanger=exchanger,
        )
    assert len(vault.all_refs()) == 0


def test_never_binds_accounts_by_email_similarity_alone(
    fake_clock: FakeClock, owner_a_access, member_a_access
) -> None:
    manager, _ = _make_manager(fake_clock)
    start1 = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn1 = manager.complete_callback(
        owner_a_access,
        state=start1.state,
        code="code-1",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(
            clock=fake_clock,
            nonce_holder=[start1.nonce],
            subject="oidc-sub-001",
            email="shared@example.com",
        ),
    )

    # Same email, different OIDC sub -> MUST create distinct connection, never bind/overwrite conn1!
    start2 = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn2 = manager.complete_callback(
        owner_a_access,
        state=start2.state,
        code="code-2",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(
            clock=fake_clock,
            nonce_holder=[start2.nonce],
            subject="oidc-sub-002",
            email="shared@example.com",
        ),
    )
    assert conn1.connection_id != conn2.connection_id
    assert conn1.oidc_subject == "oidc-sub-001"
    assert conn2.oidc_subject == "oidc-sub-002"

    # Non-owner in same workspace cannot use owner's connection
    with pytest.raises(LlmLifecycleError) as non_owner_err:
        manager.verify_connection_access(member_a_access, connection_id=conn1.connection_id)
    assert non_owner_err.value.code is LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH


def test_rejects_login_only_identity_only_scope_and_ineligible_plan(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, vault = _make_manager(fake_clock)
    start1 = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    with pytest.raises(LlmLifecycleError) as scope_err:
        manager.complete_callback(
            owner_a_access,
            state=start1.state,
            code="code-login-only",
            redirect_uri=ALLOWED_REDIRECT_URI,
            exchanger=MockSubscriptionExchanger(
                clock=fake_clock,
                nonce_holder=[start1.nonce],
                scopes=("openid", "profile", "email"),
            ),
        )
    assert scope_err.value.code is LlmGateReasonCode.LOGIN_ONLY_SCOPE_REJECTED
    assert len(vault.all_refs()) == 0

    start2 = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    with pytest.raises(LlmLifecycleError) as plan_err:
        manager.complete_callback(
            owner_a_access,
            state=start2.state,
            code="code-free-plan",
            redirect_uri=ALLOWED_REDIRECT_URI,
            exchanger=MockSubscriptionExchanger(
                clock=fake_clock,
                nonce_holder=[start2.nonce],
                plan_tier="FREE",
                plan_inference_eligible=False,
            ),
        )
    assert plan_err.value.code is LlmGateReasonCode.PLAN_ELIGIBILITY_UNVERIFIED
    assert len(vault.all_refs()) == 0


def test_cross_workspace_and_non_owner_usage_blocked(
    fake_clock: FakeClock, owner_a_access, member_a_access, owner_b_access
) -> None:
    manager, _ = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-ws-a",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce]),
    )
    for unauthorized in (member_a_access, owner_b_access):
        with pytest.raises(LlmLifecycleError) as err:
            manager.verify_connection_access(unauthorized, connection_id=conn.connection_id)
        assert err.value.code is LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH


def test_external_send_consent_per_repo_and_job_reference_non_raw(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, _ = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-consent",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce]),
    )

    # Missing acknowledgment -> rejected
    with pytest.raises(LlmLifecycleError) as ack_err:
        manager.grant_external_send_consent(
            owner_a_access,
            source_resource_id=SOURCE_RESOURCE_A,
            consent_version="consent-v1",
            acknowledged_public_unmasked_send=True,
            acknowledged_third_party_non_recallable=False,
        )
    assert ack_err.value.code is LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED

    consent = manager.grant_external_send_consent(
        owner_a_access,
        source_resource_id=SOURCE_RESOURCE_A,
        consent_version="consent-v1",
        acknowledged_public_unmasked_send=True,
        acknowledged_third_party_non_recallable=True,
    )
    assert consent.is_active is True

    job_ref = manager.issue_job_reference(
        owner_a_access,
        connection_id=conn.connection_id,
        source_resource_id=SOURCE_RESOURCE_A,
        stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
        input_hash="sha256:" + "a" * 64,
    )
    serialized = job_ref.to_dict()
    assert set(serialized.keys()) == {
        "connection_id",
        "connection_version",
        "owner_user_id",
        "workspace_id",
        "consent_version",
        "policy_version",
        "stage_code",
        "input_hash",
    }
    assert ConnectionJobReference.from_dict(serialized) == job_ref

    # Injecting raw text or secret keys into ConnectionJobReference.from_dict MUST fail
    for bad_key in ("body", "raw_text", "access_token", "authorization", "prompt"):
        tampered = dict(serialized)
        tampered[bad_key] = "forbidden"
        with pytest.raises(ValueError):
            ConnectionJobReference.from_dict(tampered)

    # Revoking consent blocks subsequent job reference issuance
    manager.revoke_external_send_consent(owner_a_access, source_resource_id=SOURCE_RESOURCE_A)
    with pytest.raises(LlmLifecycleError) as rev_err:
        manager.issue_job_reference(
            owner_a_access,
            connection_id=conn.connection_id,
            source_resource_id=SOURCE_RESOURCE_A,
            stage_code=LlmStageCode.STAGE_2A_FACT_CANDIDATE_EXTRACTION,
            input_hash="sha256:" + "b" * 64,
        )
    assert rev_err.value.code is LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REVOKED


def test_serialized_concurrent_refresh_with_barrier_refreshes_at_most_once(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, vault = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    exchanger = MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce])
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-refresh",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=exchanger,
    )
    old_secret_ref = conn.token_secret_ref
    assert old_secret_ref is not None

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            res = manager.refresh_connection_tokens(
                owner_a_access,
                connection_id=conn.connection_id,
                stale_version=1,
                exchanger=exchanger,
            )
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not errors
    assert len(results) == 2
    assert exchanger.refresh_calls == 1
    assert sum(1 for r in results if r.refreshed_remotely) == 1
    assert sum(1 for r in results if r.coalesced) == 1
    assert all(r.connection.connection_version == 2 for r in results)
    # Old secret wiped from vault, only new secret remains
    assert vault.get_secret(old_secret_ref) is None
    assert len(vault.all_refs()) == 1


def test_unlink_order_local_fence_then_remote_revoke_then_local_secret_wipe(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, vault = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-unlink",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce]),
    )
    revoker = RecordingRemoteRevoker(manager=manager, confirmed=True)
    result = manager.unlink_connection(
        owner_a_access,
        connection_id=conn.connection_id,
        revoker=revoker,
    )

    assert result.executed_steps == ("LOCAL_FENCE", "REMOTE_REVOKE", "LOCAL_SECRET_WIPE")
    # During Step 2 (REMOTE_REVOKE), Step 1 (LOCAL_FENCE) was ALREADY active!
    assert revoker.observed_status_during_revoke is SubscriptionConnectionStatus.REVOKED
    assert revoker.observed_secret_still_in_vault_during_revoke is True
    # After Step 3, secret is completely wiped and version bumped
    assert result.connection.status is SubscriptionConnectionStatus.REVOKED
    assert result.connection.connection_version == 2
    assert result.connection.remote_revocation_status is RemoteRevocationStatus.CONFIRMED_REMOTE
    assert result.connection.secret_wipe_status is SecretWipeStatus.WIPED
    assert result.connection.token_secret_ref is None
    assert len(vault.all_refs()) == 0


def test_unlink_remote_failure_keeps_local_fence_revoked_and_marks_unconfirmed_remote(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, vault = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-unlink-fail",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce]),
    )
    revoker = RecordingRemoteRevoker(
        manager=manager,
        fail_with=TimeoutError("remote revoke timed out with bearer at_secret_leak"),
    )
    result = manager.unlink_connection(
        owner_a_access,
        connection_id=conn.connection_id,
        revoker=revoker,
    )

    assert result.connection.status is SubscriptionConnectionStatus.REVOKED
    assert result.connection.remote_revocation_status is RemoteRevocationStatus.UNCONFIRMED_REMOTE
    assert result.connection.secret_wipe_status is SecretWipeStatus.WIPED
    assert len(vault.all_refs()) == 0
    assert "at_secret_leak" not in (result.remote_revocation_error or "")

    # Revoked connection can NEVER be verified as active again
    with pytest.raises(LlmLifecycleError) as err:
        manager.verify_connection_access(owner_a_access, connection_id=conn.connection_id)
    assert err.value.code is LlmGateReasonCode.CONNECTION_REVOKED_OR_INACTIVE


def test_unlink_secret_wipe_failure_tracks_retry_pending_while_keeping_revoked_fence(
    fake_clock: FakeClock, owner_a_access
) -> None:
    manager, vault = _make_manager(fake_clock)
    start = manager.start_connect(owner_a_access, redirect_uri=ALLOWED_REDIRECT_URI)
    conn = manager.complete_callback(
        owner_a_access,
        state=start.state,
        code="code-wipe-fail",
        redirect_uri=ALLOWED_REDIRECT_URI,
        exchanger=MockSubscriptionExchanger(clock=fake_clock, nonce_holder=[start.nonce]),
    )
    vault.fail_next_delete()
    revoker = RecordingRemoteRevoker(manager=manager, confirmed=True)
    result = manager.unlink_connection(
        owner_a_access,
        connection_id=conn.connection_id,
        revoker=revoker,
    )
    assert result.connection.status is SubscriptionConnectionStatus.REVOKED
    assert result.connection.secret_wipe_status is SecretWipeStatus.SECRET_WIPE_RETRY_PENDING
    assert result.connection.remote_revocation_status is RemoteRevocationStatus.CONFIRMED_REMOTE
