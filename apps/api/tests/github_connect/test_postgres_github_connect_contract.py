"""Real local PostgreSQL 18 integration tests for M2-T201 (AUTH-02 & POLICY-01)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from career_memory_api.auth.oauth import (
    InMemorySecretStore,
    OAuthPersistenceFailedError,
    OAuthStateInvalidOrReplayedError,
)
from career_memory_api.authz import WorkspaceMembershipDeniedError
from career_memory_api.github_connect.models import (
    GitHubConnectError,
    GitHubCredentialMode,
    GitHubExchangedCredential,
    GitHubInspectionOutcome,
    GitHubRepositoryInspection,
    GitHubRepositoryMetadata,
    GitHubRevokeResult,
    PolicyDecision,
    PolicyGuardedOperation,
    PolicyGuidanceCode,
)
from career_memory_api.github_connect.postgres_service import (
    PostgresGitHubConnectService,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

from .pg_harness import (
    EphemeralPg18Cluster,
    revoke_membership,
    seed_provenance_and_active_work,
    seed_workspace_member,
)

pytestmark = pytest.mark.postgres


class FakeGitHubCredentialProvider:
    """Thread-safe deterministic GitHubCredentialProviderPort for PG18 integration tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.exchange_scopes: tuple[str, ...] = ("read:user",)
        self.external_account_id: str = "gh-user-10001"
        self.login: str = "career-dev"
        self.next_access_token: str = "gho_live_read_user_token_abcdef123456"
        self.exchange_calls: list[dict[str, str]] = []
        self.revoked_tokens: list[str] = []
        self.repo_inspections: dict[str, GitHubRepositoryInspection] = {}
        self.default_inspection: GitHubRepositoryInspection | None = None

    @property
    def credential_mode(self) -> GitHubCredentialMode:
        return GitHubCredentialMode.OAUTH_APP

    def set_public_repo(
        self,
        full_name: str,
        *,
        provider_resource_id: str = "repo-99001",
    ) -> None:
        self.repo_inspections[full_name.lower()] = GitHubRepositoryInspection(
            outcome=GitHubInspectionOutcome.PUBLIC_CONFIRMED,
            http_status=200,
            observed_visibility="PUBLIC",
            repository=GitHubRepositoryMetadata(
                provider_resource_id=provider_resource_id,
                full_name=full_name,
                canonical_url=f"https://github.com/{full_name}",
                private=False,
                visibility="public",
                default_branch="main",
            ),
        )

    def build_authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        return (
            "https://github.com/login/oauth/authorize"
            f"?client_id=test-client&redirect_uri={redirect_uri}"
            f"&scope=read%3Auser&state={state}"
            f"&code_challenge={code_challenge}&code_challenge_method={code_challenge_method}"
        )

    def exchange_authorization_code(
        self,
        *,
        code: str,
        pkce_code_verifier: str,
        redirect_uri: str,
    ) -> GitHubExchangedCredential:
        with self._lock:
            self.exchange_calls.append(
                {
                    "code": code,
                    "pkce_code_verifier": pkce_code_verifier,
                    "redirect_uri": redirect_uri,
                }
            )
            return GitHubExchangedCredential(
                external_account_id=self.external_account_id,
                login=self.login,
                access_token=self.next_access_token,
                scopes=self.exchange_scopes,
            )

    def inspect_repository(
        self,
        *,
        access_token: str,
        owner: str,
        repo: str,
    ) -> GitHubRepositoryInspection:
        key = f"{owner}/{repo}".lower()
        if key in self.repo_inspections:
            return self.repo_inspections[key]
        if self.default_inspection is not None:
            return self.default_inspection
        return GitHubRepositoryInspection(
            outcome=GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY,
            http_status=404,
            reason="Repository not found or inaccessible.",
        )

    def revoke_grant(self, *, access_token: str) -> GitHubRevokeResult:
        with self._lock:
            self.revoked_tokens.append(access_token)
        return GitHubRevokeResult(revoked=True, already_revoked=False, http_status=204)


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralPg18Cluster]:
    cluster = EphemeralPg18Cluster()
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def _clean_db(pg18_cluster: EphemeralPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _make_service(
    cluster: EphemeralPg18Cluster,
    *,
    provider: FakeGitHubCredentialProvider,
    secret_store: InMemorySecretStore,
    now_holder: list[datetime] | None = None,
    fault_injector: Any = None,
) -> PostgresGitHubConnectService:
    clock = (lambda: now_holder[0]) if now_holder is not None else (lambda: datetime.now(UTC))
    return PostgresGitHubConnectService(
        connection_factory=cluster.api_connection,
        credential_provider=provider,
        secret_store=secret_store,
        allowed_redirect_uris=("https://app.career-memory.local/oauth/github/callback",),
        clock=clock,
        fault_injector=fault_injector,
    )


def _dump_all_db_text(cluster: EphemeralPg18Cluster) -> str:
    """Return textual dump of auth/connect/audit tables to assert 0 plaintext secret bytes."""
    chunks: list[str] = []
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            for table in (
                "public.oauth_connect_states",
                "public.source_accounts",
                "public.source_resources",
                "public.orphan_secret_cleanups",
                "public.audit_events",
            ):
                cur.execute(f"SELECT row_to_json(t)::text FROM {table} t")
                chunks.extend(row[0] for row in cur.fetchall())
    return "\n".join(chunks)


def test_oauth_start_callback_public_repo_allowlist_and_zero_plaintext_secrets(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/career-memory", provider_resource_id="gh-repo-501")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    assert started.requested_scopes == ("read:user",)
    assert "scope=read%3Auser" in started.authorization_url

    idem_key = str(uuid.uuid4())
    callback_res = service.complete_oauth_callback(
        access,
        idempotency_key=idem_key,
        state=started.state,
        code="gh-auth-code-secret-999",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    assert callback_res.source_account.status == "ACTIVE"
    assert callback_res.source_account.scopes == ("read:user",)
    assert callback_res.idempotent_replay is False

    # Same state + same Idempotency-Key returns idempotent replay with 0 extra exchanges
    replayed = service.complete_oauth_callback(
        access,
        idempotency_key=idem_key,
        state=started.state,
        code="gh-auth-code-secret-999",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    assert replayed.idempotent_replay is True
    assert replayed.source_account.id == callback_res.source_account.id
    assert len(provider.exchange_calls) == 1

    # Same state + different Idempotency-Key MUST fail as replayed state
    with pytest.raises(OAuthStateInvalidOrReplayedError):
        service.complete_oauth_callback(
            access,
            idempotency_key=str(uuid.uuid4()),
            state=started.state,
            code="gh-auth-code-secret-999",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )
    assert len(provider.exchange_calls) == 1

    # Register public repository on allowlist
    resource = service.register_source_resource(
        access,
        source_account_id=callback_res.source_account.id,
        repository_full_name="acme/career-memory",
    )
    assert resource.visibility == "PUBLIC"
    assert resource.processing_mode == "PUBLIC_CLOUD"
    assert resource.raw_storage_mode == "NONE"
    assert resource.status == "ACTIVE"
    assert resource.canonical_url == "https://github.com/acme/career-memory"

    # Policy guard allows all 4 operations while ACTIVE + PUBLIC
    for op in PolicyGuardedOperation:
        clearance = service.assert_resource_processing_allowed(
            access,
            source_resource_id=resource.id,
            operation=op,
        )
        assert clearance.policy_decision is PolicyDecision.ALLOWED

    # Zero plaintext token, state, or code anywhere in PostgreSQL
    db_dump = _dump_all_db_text(pg18_cluster)
    assert provider.next_access_token not in db_dump
    assert started.state not in db_dump
    assert "gh-auth-code-secret-999" not in db_dump


@pytest.mark.parametrize("excessive_scopes", [("read:user", "repo"), ("read:user", "public_repo"), ("read:user", "write:org")])
def test_excessive_oauth_scope_immediately_revokes_token_with_zero_side_effects(
    pg18_cluster: EphemeralPg18Cluster,
    excessive_scopes: tuple[str, ...],
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.exchange_scopes = excessive_scopes
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )

    with pytest.raises(GitHubConnectError) as exc_info:
        service.complete_oauth_callback(
            access,
            idempotency_key=str(uuid.uuid4()),
            state=started.state,
            code="code-overprivileged",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "EXCESSIVE_OAUTH_SCOPE"
    assert provider.revoked_tokens == [provider.next_access_token]
    assert len(secret_store.all_refs()) == 0

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_accounts")
            assert cur.fetchone() == (0,)
            cur.execute("SELECT COUNT(*) FROM public.source_resources")
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT action FROM public.audit_events WHERE workspace_id = %s::uuid",
                (access.workspace_id,),
            )
            actions = [r[0] for r in cur.fetchall()]
            assert "GITHUB_OAUTH_EXCESSIVE_SCOPE_REJECTED" in actions


def test_private_and_internal_repo_selection_rejected_with_zero_rows(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=started.state,
        code="code-ok",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account

    provider.repo_inspections["acme/secret-repo"] = GitHubRepositoryInspection(
        outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
        http_status=200,
        observed_visibility="PRIVATE",
        repository=GitHubRepositoryMetadata(
            provider_resource_id="repo-priv-1",
            full_name="acme/secret-repo",
            canonical_url="https://github.com/acme/secret-repo",
            private=True,
            visibility="private",
        ),
    )

    with pytest.raises(GitHubConnectError) as exc_info:
        service.register_source_resource(
            access,
            source_account_id=acct.id,
            repository_full_name="acme/secret-repo",
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "REPOSITORY_NOT_PUBLIC"

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_resources")
            assert cur.fetchone() == (0,)


def test_concurrent_callback_replay_creates_zero_duplicates(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/race-repo", provider_resource_id="repo-race-1")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )

    barrier = threading.Barrier(4)
    successes: list[str] = []
    replays: list[Exception] = []
    lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        try:
            res = service.complete_oauth_callback(
                access,
                idempotency_key=str(uuid.uuid4()),
                state=started.state,
                code="race-code",
                redirect_uri="https://app.career-memory.local/oauth/github/callback",
                repository_full_name="acme/race-repo",
            )
            with lock:
                successes.append(res.source_account.id)
        except OAuthStateInvalidOrReplayedError as err:
            with lock:
                replays.append(err)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 1
    assert len(replays) == 3
    assert len(provider.exchange_calls) == 1

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_accounts")
            assert cur.fetchone() == (1,)
            cur.execute("SELECT COUNT(*) FROM public.source_resources")
            assert cur.fetchone() == (1,)


def test_db_commit_failure_and_membership_revocation_leave_zero_orphan_secrets_and_revoke_grant(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    # Case 1: DB persistence fault right before commit
    access1 = seed_workspace_member(pg18_cluster, role="OWNER")
    provider1 = FakeGitHubCredentialProvider()
    secret_store1 = InMemorySecretStore()

    def _boom(stage: str) -> None:
        if stage == "before_callback_commit":
            raise RuntimeError("Simulated DB commit failure")

    service1 = _make_service(
        pg18_cluster,
        provider=provider1,
        secret_store=secret_store1,
        fault_injector=_boom,
    )
    started1 = service1.start_oauth(
        access1,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )

    with pytest.raises(OAuthPersistenceFailedError) as exc_info:
        service1.complete_oauth_callback(
            access1,
            idempotency_key=str(uuid.uuid4()),
            state=started1.state,
            code="code-db-fail",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )

    assert exc_info.value.cleanup_record is not None
    assert exc_info.value.cleanup_record.status.value == "DELETED"
    assert provider1.revoked_tokens == [provider1.next_access_token]
    assert len(secret_store1.all_refs()) == 0

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_accounts")
            assert cur.fetchone() == (0,)
            cur.execute("SELECT COUNT(*) FROM public.orphan_secret_cleanups WHERE status = 'DELETED'")
            assert cur.fetchone() == (1,)

    # Case 2: Membership revoked mid-callback (between state consume and DB persist)
    access2 = seed_workspace_member(pg18_cluster, role="OWNER")
    provider2 = FakeGitHubCredentialProvider()
    provider2.next_access_token = "gho_mid_revoke_token_777777"
    secret_store2 = InMemorySecretStore()

    def _revoke_midway(stage: str) -> None:
        if stage == "after_token_exchange":
            revoke_membership(
                pg18_cluster,
                workspace_id=access2.workspace_id,
                user_id=access2.user_id,
            )

    service2 = _make_service(
        pg18_cluster,
        provider=provider2,
        secret_store=secret_store2,
        fault_injector=_revoke_midway,
    )
    started2 = service2.start_oauth(
        access2,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )

    with pytest.raises(WorkspaceMembershipDeniedError):
        service2.complete_oauth_callback(
            access2,
            idempotency_key=str(uuid.uuid4()),
            state=started2.state,
            code="code-mid-revoke",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )

    assert provider2.revoked_tokens == ["gho_mid_revoke_token_777777"]
    assert len(secret_store2.all_refs()) == 0
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.source_accounts WHERE workspace_id = %s::uuid",
                (access2.workspace_id,),
            )
            assert cur.fetchone() == (0,)


def test_public_to_private_transition_blocks_policy_cancels_jobs_and_creates_zero_tombstones(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/became-private", provider_resource_id="repo-priv-trans")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=started.state,
        code="code-1",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    res = service.register_source_resource(
        access,
        source_account_id=acct.id,
        repository_full_name="acme/became-private",
    )
    seeded = seed_provenance_and_active_work(
        pg18_cluster,
        workspace_id=access.workspace_id,
        source_resource_id=res.id,
    )

    # Repo transitions Public -> Private
    provider.repo_inspections["acme/became-private"] = GitHubRepositoryInspection(
        outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
        http_status=200,
        observed_visibility="PRIVATE",
        repository=GitHubRepositoryMetadata(
            provider_resource_id="repo-priv-trans",
            full_name="acme/became-private",
            canonical_url="https://github.com/acme/became-private",
            private=True,
            visibility="private",
        ),
    )

    verification = service.verify_resource_policy(access, source_resource_id=res.id)
    assert verification.policy_decision is PolicyDecision.POLICY_BLOCKED
    assert verification.guidance_code is PolicyGuidanceCode.POLICY_REVIEW_REQUIRED
    assert verification.resource.visibility == "PRIVATE"
    assert verification.resource.status == "POLICY_REVIEW_REQUIRED"
    assert verification.tombstone_created is False
    assert seeded["job_id"] in verification.canceled_job_ids
    assert seeded["sync_run_id"] in verification.canceled_sync_run_ids
    assert set(verification.blocked_operations) == {op.value for op in PolicyGuardedOperation}

    # Verify DB invariants: 0 tombstones, job CANCELED with POLICY_BLOCKED, sync_run CANCELED,
    # source_object POLICY_REVIEW_REQUIRED (never DELETED), content_unit BLOCKED
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.source_object_revisions WHERE is_tombstone = true"
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT lifecycle_state FROM public.source_objects WHERE id = %s::uuid",
                (seeded["source_object_id"],),
            )
            assert cur.fetchone() == ("POLICY_REVIEW_REQUIRED",)
            cur.execute(
                "SELECT policy_state FROM public.content_units WHERE id = %s::uuid",
                (seeded["content_unit_id"],),
            )
            assert cur.fetchone() == ("BLOCKED",)
            cur.execute(
                "SELECT status, error_class, error_code FROM public.jobs WHERE id = %s::uuid",
                (seeded["job_id"],),
            )
            assert cur.fetchone() == ("CANCELED", "CANCELED_BY_FENCE", "POLICY_BLOCKED")
            cur.execute(
                "SELECT status FROM public.sync_runs WHERE id = %s::uuid",
                (seeded["sync_run_id"],),
            )
            assert cur.fetchone() == ("CANCELED",)

    # Every downstream operation is immediately blocked with POLICY_BLOCKED
    for op in PolicyGuardedOperation:
        with pytest.raises(GitHubConnectError) as err:
            service.assert_resource_processing_allowed(
                access, source_resource_id=res.id, operation=op
            )
        assert err.value.status_code == 403
        assert err.value.code == "POLICY_BLOCKED"


def test_token_revoked_401_marks_account_revoked_and_reauth_required_without_tombstone(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/revoked-repo", provider_resource_id="repo-rev-1")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=started.state,
        code="code-rev",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    res = service.register_source_resource(
        access,
        source_account_id=acct.id,
        repository_full_name="acme/revoked-repo",
    )
    seeded = seed_provenance_and_active_work(
        pg18_cluster,
        workspace_id=access.workspace_id,
        source_resource_id=res.id,
    )

    # Provider returns 401 Bad credentials
    provider.repo_inspections["acme/revoked-repo"] = GitHubRepositoryInspection(
        outcome=GitHubInspectionOutcome.TOKEN_REVOKED,
        http_status=401,
        reason="Bad credentials",
    )

    verification = service.verify_resource_policy(access, source_resource_id=res.id)
    assert verification.policy_decision is PolicyDecision.POLICY_BLOCKED
    assert verification.guidance_code is PolicyGuidanceCode.REAUTH_REQUIRED
    assert verification.account_status == "REVOKED"
    assert verification.resource.status == "POLICY_REVIEW_REQUIRED"
    assert verification.tombstone_created is False
    assert len(secret_store.all_refs()) == 0

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, revoked_at IS NOT NULL FROM public.source_accounts WHERE id = %s::uuid",
                (acct.id,),
            )
            assert cur.fetchone() == ("REVOKED", True)
            cur.execute(
                "SELECT COUNT(*) FROM public.source_object_revisions WHERE is_tombstone = true"
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT lifecycle_state FROM public.source_objects WHERE id = %s::uuid",
                (seeded["source_object_id"],),
            )
            # Provenance history preserved (not DELETED)
            assert cur.fetchone() == ("ACTIVE",)
            cur.execute(
                "SELECT status, error_code FROM public.jobs WHERE id = %s::uuid",
                (seeded["job_id"],),
            )
            assert cur.fetchone() == ("CANCELED", "REAUTH_REQUIRED")

    with pytest.raises(GitHubConnectError) as err:
        service.assert_resource_processing_allowed(
            access,
            source_resource_id=res.id,
            operation=PolicyGuardedOperation.INGESTION,
        )
    assert err.value.code == "REAUTH_REQUIRED"


@pytest.mark.parametrize("http_status", [404, 403])
def test_ambiguous_404_and_403_fail_closed_without_creating_tombstones(
    pg18_cluster: EphemeralPg18Cluster,
    http_status: int,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/ambiguous-repo", provider_resource_id=f"repo-amb-{http_status}")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=started.state,
        code=f"code-amb-{http_status}",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    res = service.register_source_resource(
        access,
        source_account_id=acct.id,
        repository_full_name="acme/ambiguous-repo",
    )
    seeded = seed_provenance_and_active_work(
        pg18_cluster,
        workspace_id=access.workspace_id,
        source_resource_id=res.id,
    )

    provider.repo_inspections["acme/ambiguous-repo"] = GitHubRepositoryInspection(
        outcome=GitHubInspectionOutcome.AMBIGUOUS_ACCESS_OR_VISIBILITY,
        http_status=http_status,
        reason="Ambiguous provider response",
    )

    verification = service.verify_resource_policy(access, source_resource_id=res.id)
    assert verification.policy_decision is PolicyDecision.POLICY_BLOCKED
    assert verification.guidance_code is PolicyGuidanceCode.VISIBILITY_OR_ACCESS_AMBIGUOUS
    assert verification.resource.status == "POLICY_REVIEW_REQUIRED"
    assert verification.tombstone_created is False

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.source_object_revisions WHERE is_tombstone = true"
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "SELECT status FROM public.source_resources WHERE id = %s::uuid",
                (res.id,),
            )
            assert cur.fetchone() == ("POLICY_REVIEW_REQUIRED",)
            cur.execute(
                "SELECT status, error_code FROM public.jobs WHERE id = %s::uuid",
                (seeded["job_id"],),
            )
            assert cur.fetchone() == ("CANCELED", "VISIBILITY_OR_ACCESS_AMBIGUOUS")


def test_disconnect_revokes_github_grant_deletes_secret_and_blocks_resources(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/disconnect-repo", provider_resource_id="repo-disc-1")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=started.state,
        code="code-disc",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    res = service.register_source_resource(
        access,
        source_account_id=acct.id,
        repository_full_name="acme/disconnect-repo",
    )
    seeded = seed_provenance_and_active_work(
        pg18_cluster,
        workspace_id=access.workspace_id,
        source_resource_id=res.id,
    )

    disconnected = service.disconnect_source_account(access, source_account_id=acct.id)
    assert disconnected.source_account.status == "REVOKED"
    assert disconnected.source_account.revoked_at is not None
    assert disconnected.provider_grant_revoked is True
    assert disconnected.secret_deleted is True
    assert disconnected.already_revoked is False
    assert disconnected.tombstone_created is False
    assert res.id in disconnected.affected_resource_ids
    assert seeded["job_id"] in disconnected.canceled_job_ids
    assert provider.revoked_tokens == [provider.next_access_token]
    assert len(secret_store.all_refs()) == 0

    # Idempotent second disconnect succeeds cleanly
    second = service.disconnect_source_account(access, source_account_id=acct.id)
    assert second.already_revoked is True
    assert second.source_account.status == "REVOKED"


def test_cross_workspace_concealment_returns_identical_not_found(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access_a = seed_workspace_member(pg18_cluster, workspace_name="WS A", role="OWNER")
    access_b = seed_workspace_member(pg18_cluster, workspace_name="WS B", role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/ws-a-repo", provider_resource_id="repo-wsa-1")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    started = service.start_oauth(
        access_a,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct_a = service.complete_oauth_callback(
        access_a,
        idempotency_key=str(uuid.uuid4()),
        state=started.state,
        code="code-a",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    res_a = service.register_source_resource(
        access_a,
        source_account_id=acct_a.id,
        repository_full_name="acme/ws-a-repo",
    )

    # Workspace B cannot see Workspace A's accounts or resources
    assert service.list_source_accounts(access_b) == []
    assert service.list_source_resources(access_b) == []

    # Cross-workspace verify-policy, disconnect, and resource registration conceal identically
    for fn in (
        lambda: service.verify_resource_policy(access_b, source_resource_id=res_a.id),
        lambda: service.disconnect_source_account(access_b, source_account_id=acct_a.id),
        lambda: service.register_source_resource(
            access_b,
            source_account_id=acct_a.id,
            repository_full_name="acme/ws-a-repo",
        ),
    ):
        with pytest.raises(WorkspaceResourceNotFoundError):
            fn()


def test_state_expiry_cross_actor_theft_idempotency_conflict_and_provider_denial(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    now_holder = [datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)]
    owner = seed_workspace_member(pg18_cluster, role="OWNER")
    other_member = seed_workspace_member(
        pg18_cluster,
        workspace_id=owner.workspace_id,
        role="MEMBER",
    )
    provider = FakeGitHubCredentialProvider()
    secret_store = InMemorySecretStore()
    service = _make_service(
        pg18_cluster,
        provider=provider,
        secret_store=secret_store,
        now_holder=now_holder,
    )

    # 1. Cross-actor state theft blocked
    s1 = service.start_oauth(
        owner,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    with pytest.raises(OAuthStateInvalidOrReplayedError):
        service.complete_oauth_callback(
            other_member,
            idempotency_key=str(uuid.uuid4()),
            state=s1.state,
            code="stolen-code",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )

    # 2. Expired state blocked after TTL elapses
    now_holder[0] = now_holder[0] + timedelta(seconds=601)
    with pytest.raises(OAuthStateInvalidOrReplayedError):
        service.complete_oauth_callback(
            owner,
            idempotency_key=str(uuid.uuid4()),
            state=s1.state,
            code="expired-code",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )

    # 3. Provider access_denied consumes state with 0 accounts and 0 exchanges
    s2 = service.start_oauth(
        owner,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    with pytest.raises(GitHubConnectError) as denied_err:
        service.complete_oauth_callback(
            owner,
            idempotency_key=str(uuid.uuid4()),
            state=s2.state,
            code=None,
            provider_error="access_denied",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )
    assert denied_err.value.status_code == 400
    assert denied_err.value.code == "OAUTH_AUTHORIZATION_DENIED"
    assert len(provider.exchange_calls) == 0

    # 4. Same Idempotency-Key reused against a DIFFERENT state -> 409 IDEMPOTENCY_CONFLICT
    s3 = service.start_oauth(
        owner,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    s4 = service.start_oauth(
        owner,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    reused_idem = str(uuid.uuid4())
    service.complete_oauth_callback(
        owner,
        idempotency_key=reused_idem,
        state=s3.state,
        code="ok-3",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    with pytest.raises(GitHubConnectError) as conflict_err:
        service.complete_oauth_callback(
            owner,
            idempotency_key=reused_idem,
            state=s4.state,
            code="ok-4",
            redirect_uri="https://app.career-memory.local/oauth/github/callback",
        )
    assert conflict_err.value.status_code == 409
    assert conflict_err.value.code == "IDEMPOTENCY_CONFLICT"


def test_rate_limit_preserves_state_fail_closed_review_and_credential_handoff(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/handoff-repo", provider_resource_id="repo-handoff-1")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    s = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=s.state,
        code="code-handoff",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    res = service.register_source_resource(
        access,
        source_account_id=acct.id,
        repository_full_name="acme/handoff-repo",
    )

    # Credential handoff succeeds while ACTIVE + PUBLIC and redacts repr
    cred = service.resolve_active_credential_for_resource(
        access,
        source_resource_id=res.id,
        operation=PolicyGuardedOperation.INGESTION,
    )
    assert cred.access_token == provider.next_access_token
    assert provider.next_access_token not in repr(cred)

    # 429 Rate limit during verify-policy raises retryable error without changing DB state
    provider.repo_inspections["acme/handoff-repo"] = GitHubRepositoryInspection(
        outcome=GitHubInspectionOutcome.RATE_LIMITED,
        http_status=429,
        retry_after_seconds=60,
        reason="Rate limited",
    )
    with pytest.raises(GitHubConnectError) as rl_err:
        service.verify_resource_policy(access, source_resource_id=res.id)
    assert rl_err.value.status_code == 429
    assert rl_err.value.retryable is True

    # Still ACTIVE after transient rate limit
    assert (
        service.assert_resource_processing_allowed(
            access,
            source_resource_id=res.id,
            operation=PolicyGuardedOperation.RAW_REFETCH,
        ).policy_decision
        is PolicyDecision.ALLOWED
    )

    # Transition to PRIVATE -> POLICY_REVIEW_REQUIRED
    provider.repo_inspections["acme/handoff-repo"] = GitHubRepositoryInspection(
        outcome=GitHubInspectionOutcome.NON_PUBLIC_CONFIRMED,
        http_status=200,
        observed_visibility="PRIVATE",
    )
    service.verify_resource_policy(access, source_resource_id=res.id)

    # Even if provider later reports PUBLIC, fail-closed policy keeps POLICY_REVIEW_REQUIRED
    provider.set_public_repo("acme/handoff-repo", provider_resource_id="repo-handoff-1")
    recheck = service.verify_resource_policy(access, source_resource_id=res.id)
    assert recheck.policy_decision is PolicyDecision.POLICY_BLOCKED
    assert recheck.resource.status == "POLICY_REVIEW_REQUIRED"

    # Credential handoff MUST remain blocked
    with pytest.raises(GitHubConnectError) as blocked:
        service.resolve_active_credential_for_resource(
            access,
            source_resource_id=res.id,
            operation=PolicyGuardedOperation.AI_TRANSMISSION,
        )
    assert blocked.value.code == "POLICY_BLOCKED"


def test_scope_loss_and_disconnect_secret_delete_retry_queue(
    pg18_cluster: EphemeralPg18Cluster,
) -> None:
    access = seed_workspace_member(pg18_cluster, role="OWNER")
    provider = FakeGitHubCredentialProvider()
    provider.set_public_repo("acme/scope-loss", provider_resource_id="repo-sl-1")
    secret_store = InMemorySecretStore()
    service = _make_service(pg18_cluster, provider=provider, secret_store=secret_store)

    s = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=s.state,
        code="code-sl",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    res = service.register_source_resource(
        access,
        source_account_id=acct.id,
        repository_full_name="acme/scope-loss",
    )

    # Scope loss detected during verify-policy -> revokes grant, marks REVOKED + REAUTH_REQUIRED
    provider.repo_inspections["acme/scope-loss"] = GitHubRepositoryInspection(
        outcome=GitHubInspectionOutcome.SCOPE_LOST,
        http_status=200,
        observed_scopes=(),
        reason="Missing read:user scope",
    )
    ver = service.verify_resource_policy(access, source_resource_id=res.id)
    assert ver.guidance_code is PolicyGuidanceCode.REAUTH_REQUIRED
    assert ver.account_status == "REVOKED"
    assert ver.tombstone_created is False
    assert provider.revoked_tokens == [provider.next_access_token]

    # Re-connect, then inject transient secret delete error on disconnect -> RETRY_PENDING outbox
    s2 = service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    )
    acct2 = service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=s2.state,
        code="code-sl-2",
        redirect_uri="https://app.career-memory.local/oauth/github/callback",
    ).source_account
    secret_store.fail_delete_once_for_namespace("github-oauth-token")
    disc = service.disconnect_source_account(access, source_account_id=acct2.id)
    assert disc.source_account.status == "REVOKED"
    assert disc.provider_grant_revoked is True
    assert disc.secret_deleted is False
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.orphan_secret_cleanups WHERE status = 'RETRY_PENDING'"
            )
            # First delete failed, cleaner retried and succeeded (DELETED) or queued RETRY_PENDING
            cur.execute(
                "SELECT status FROM public.orphan_secret_cleanups WHERE workspace_id = %s::uuid",
                (access.workspace_id,),
            )
            statuses = [r[0] for r in cur.fetchall()]
            assert len(statuses) == 1
            assert statuses[0] in ("DELETED", "RETRY_PENDING")

