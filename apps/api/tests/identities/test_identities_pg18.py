"""Real PostgreSQL 18 integration & concurrency tests for M4 Wave 1A Identities (T-401, T-401A, ID-01..05).

Verifies:
- ID-01: Verification method gate (OAUTH_ACCOUNT/PROVIDER_ASSOCIATION -> VERIFIED; USER_CONFIRMATION -> USER_CONFIRMED, never VERIFIED; username/email -> HEURISTIC)
- ID-02: Account rename updates canonical Identity/mapping without duplicate Identity row + audit_events log
- ID-03: Noreply email / committer (web-flow) / co-author / bot separation
- ID-04: Heuristic, conflicted, ambiguous, revoked, and bot identities strictly blocked from SOURCE_VERIFIED attribution
- ID-05: Atomic revocation & conflict invalidation cascade across contributions, facts, claim_atoms, claims (APPROVED -> STALE), and exports (READY -> INVALIDATED) + review_actions + audit_events + outbox_events
- HTTP routes (/v1/identities, confirm, revoke) with Cache-Control: private, no-store, cross-workspace 404 concealment, and idempotent replay
- Concurrency (threading.Barrier): concurrent revoke/confirm race + mid-flight membership revocation rollback via before_commit_hook
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.identities import (
    ConfirmIdentityMappingRequest,
    GitHubActorObservation,
    GitHubActorRole,
    IdentityAttributionBlockReason,
    IdentityError,
    PostgresIdentityService,
    RevokeIdentityMappingRequest,
    create_identities_router,
    evaluate_identity_attribution,
)
from career_memory_api.schema_contracts.enums import (
    ExternalIdentityMappingStatus,
    IdentityStatus,
    IdentityType,
    IdentityVerificationMethod,
)
from tests.backfill_engine.pg_harness import (
    MIGRATIONS_0010_TO_0050,
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATION_0060 = REPO_ROOT / "db" / "migrations" / "0060_m3_event_versions_and_constraints.sql"
MIGRATION_0070 = REPO_ROOT / "db" / "migrations" / "0070_m4_fact_versions_and_exports.sql"
MIGRATIONS_0010_TO_0070 = (*MIGRATIONS_0010_TO_0050, MIGRATION_0060, MIGRATION_0070)


def _sha256(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _access_for(seed: dict[str, str]) -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=seed["user_id"],
            auth_subject=AuthSubject(
                issuer="https://auth.local/v1",
                subject=f"sub-{seed['user_id']}",
            ),
        ),
        membership=WorkspaceMembership(
            workspace_id=seed["workspace_id"],
            user_id=seed["user_id"],
            role="OWNER",
        ),
    )


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m4-identities-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0070:
            cluster.apply_sql_file(mig)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def clean_tables(pg18_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _seed_downstream_chain_for_identity(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    source_resource_id: str,
    user_id: str,
    identity_id: str,
    contribution_status: str = "SOURCE_SUPPORTED",
    fact_status: str = "SUPPORTED",
) -> dict[str, str]:
    obj_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    contrib_id = str(uuid.uuid4())
    role_fact_id = str(uuid.uuid4())
    action_fact_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    atom_role_id = str(uuid.uuid4())
    atom_action_id = str(uuid.uuid4())
    export_id = str(uuid.uuid4())
    export_claim_id = str(uuid.uuid4())

    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type, provider_object_id
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s)
                    """,
                    (obj_id, workspace_id, source_resource_id, f"pr-{obj_id[:8]}"),
                )
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode, title, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', 'Auth Hardening PR', 'CONFIRMED')
                    """,
                    (event_id, workspace_id, obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.contributions (
                        id, workspace_id, event_id, identity_id, role, origin, status
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'IMPLEMENTED', 'RULE', %s)
                    """,
                    (contrib_id, workspace_id, event_id, identity_id, contribution_status),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, subject_identity_id,
                        predicate, object_text, status, extractor_version
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'ROLE', %s::uuid,
                        'implemented', 'Implemented OAuth PKCE validation', %s, 'v1'
                    )
                    """,
                    (role_fact_id, workspace_id, event_id, identity_id, fact_status),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, subject_identity_id,
                        predicate, object_text, status, extractor_version
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'ACTION', NULL,
                        'added_pkce', 'Added S256 PKCE verifier', 'SUPPORTED', 'v1'
                    )
                    """,
                    (action_fact_id, workspace_id, event_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, version, claim_text, status
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 1,
                        'Implemented OAuth PKCE S256 validation for GitHub connect flow.', 'APPROVED'
                    )
                    """,
                    (claim_id, workspace_id, event_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.claim_atoms (
                        id, workspace_id, claim_id, ordinal, atom_type, atom_text, status
                    ) VALUES
                        (%s::uuid, %s::uuid, %s::uuid, 0, 'ROLE', 'Implemented OAuth PKCE', 'SUPPORTED'),
                        (%s::uuid, %s::uuid, %s::uuid, 1, 'ACTION', 'Added S256 PKCE verifier', 'SUPPORTED')
                    """,
                    (atom_role_id, workspace_id, claim_id, atom_action_id, workspace_id, claim_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.claim_atom_support (
                        workspace_id, claim_atom_id, fact_id, support_type, status
                    ) VALUES
                        (%s::uuid, %s::uuid, %s::uuid, 'DIRECT', 'ACTIVE'),
                        (%s::uuid, %s::uuid, %s::uuid, 'DIRECT', 'ACTIVE')
                    """,
                    (
                        workspace_id,
                        atom_role_id,
                        role_fact_id,
                        workspace_id,
                        atom_action_id,
                        action_fact_id,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.exports (
                        id, workspace_id, source_resource_id, requested_by_user_id,
                        format, export_policy, status, idempotency_key, input_hash, policy_version
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        'MARKDOWN', 'SOURCE_VERIFIED_ONLY', 'READY', %s, %s, 'v1'
                    )
                    """,
                    (
                        export_id,
                        workspace_id,
                        source_resource_id,
                        user_id,
                        str(uuid.uuid4()),
                        _sha256(f"exp-{claim_id}"),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.export_claims (
                        id, workspace_id, export_id, claim_id, claim_version,
                        computed_verification_at_export, ordinal
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, 1, 'SOURCE_VERIFIED', 0
                    )
                    """,
                    (export_claim_id, workspace_id, export_id, claim_id),
                )
    return {
        "event_id": event_id,
        "contribution_id": contrib_id,
        "role_fact_id": role_fact_id,
        "action_fact_id": action_fact_id,
        "claim_id": claim_id,
        "atom_role_id": atom_role_id,
        "atom_action_id": atom_action_id,
        "export_id": export_id,
    }


def test_id01_id02_provider_precedence_rename_without_duplicate_and_no_raw_email(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    service = PostgresIdentityService(api_connection_factory=pg18_cluster.api_connection)

    # 1. OAuth account observation -> VERIFIED
    first = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="424242",
            username="alice-old",
            display_name="Alice Engineer",
            email="Alice.Old@Example.com",
            provider_actor_type="User",
            provider_associated=True,
            is_oauth_account=True,
            canonical_user_id=seed["user_id"],
        ),
    )
    assert first.created_identity is True
    assert first.renamed_username is False
    assert first.mapping.status is ExternalIdentityMappingStatus.VERIFIED
    assert first.mapping.verification_method is IdentityVerificationMethod.OAUTH_ACCOUNT
    assert first.mapping.username == "alice-old"
    assert first.mapping.email_hash is not None and first.mapping.email_hash.startswith("sha256:")
    assert first.attribution_eligibility.can_support_source_verified is True
    assert first.attribution_eligibility.block_reason_code is None

    # 2. ID-02: Same stable external_user_id observed with renamed username -> NO duplicate Identity
    renamed = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="424242",
            username="alice-new",
            display_name="Alice Engineer",
            provider_actor_type="User",
            provider_associated=True,
        ),
    )
    assert renamed.created_identity is False
    assert renamed.renamed_username is True
    assert renamed.identity_id == first.identity_id
    assert renamed.mapping.mapping_id == first.mapping.mapping_id
    assert renamed.mapping.username == "alice-new"
    assert renamed.mapping.status is ExternalIdentityMappingStatus.VERIFIED

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.identities WHERE workspace_id = %s::uuid",
                (seed["workspace_id"],),
            )
            assert cur.fetchone() == (1,)
            cur.execute(
                """
                SELECT action, redacted_metadata
                FROM public.audit_events
                WHERE workspace_id = %s::uuid
                  AND action = 'IDENTITY_USERNAME_RENAMED'
                """,
                (seed["workspace_id"],),
            )
            audit_rows = cur.fetchall()
            assert len(audit_rows) == 1
            meta = audit_rows[0][1]
            assert meta["previous_username"] == "alice-old"
            assert meta["new_username"] == "alice-new"
            assert "Alice.Old@Example.com" not in str(meta)


def test_id03_id04_noreply_coauthor_bot_and_heuristic_blocked_from_source_verified(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    service = PostgresIdentityService(api_connection_factory=pg18_cluster.api_connection)

    # 1. Login-only noreply without provider_user_id -> HEURISTIC, blocked from SOURCE_VERIFIED
    noreply_heur = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.COMMIT_AUTHOR,
            username="carol",
            email="carol@users.noreply.github.com",
            provider_associated=False,
        ),
    )
    assert noreply_heur.mapping.status is ExternalIdentityMappingStatus.HEURISTIC
    assert noreply_heur.attribution_eligibility.can_support_source_verified is False
    assert noreply_heur.attribution_eligibility.can_support_user_confirmed is False
    assert (
        noreply_heur.attribution_eligibility.block_reason_code
        is IdentityAttributionBlockReason.IDENTITY_HEURISTIC_ONLY
    )

    # 2. Numeric noreply WITH provider_associated=True -> extracts external_user_id -> VERIFIED
    noreply_ver = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.COMMIT_AUTHOR,
            email="777888+dave@users.noreply.github.com",
            provider_associated=True,
        ),
    )
    assert noreply_ver.mapping.external_user_id == "777888"
    assert noreply_ver.mapping.username == "dave"
    assert noreply_ver.mapping.status is ExternalIdentityMappingStatus.VERIFIED
    assert noreply_ver.attribution_eligibility.can_support_source_verified is True

    # 3. Co-author trailer without provider verification -> HEURISTIC (never VERIFIED)
    co_author = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.CO_AUTHOR,
            display_name="Eve Pair",
            email="999000+eve@users.noreply.github.com",
            provider_associated=False,
        ),
    )
    assert co_author.mapping.status is ExternalIdentityMappingStatus.HEURISTIC
    assert co_author.attribution_eligibility.can_support_source_verified is False

    # Confirming the heuristic co-author yields USER_CONFIRMED (NEVER VERIFIED!)
    confirmed = service.confirm_identity_mapping(
        access,
        mapping_id=co_author.mapping.mapping_id,
        idempotency_key=str(uuid.uuid4()),
        request=ConfirmIdentityMappingRequest(reason="That was my pair commit"),
    )
    assert confirmed.mapping.status is ExternalIdentityMappingStatus.USER_CONFIRMED
    assert confirmed.mapping.verification_method is IdentityVerificationMethod.USER_CONFIRMATION
    assert confirmed.attribution_eligibility.can_support_source_verified is False
    assert confirmed.attribution_eligibility.can_support_user_confirmed is True

    # 4. BOT actor -> IdentityType.BOT, strictly blocked from attribution and USER_CONFIRMATION
    bot_res = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="49699333",
            username="dependabot[bot]",
            provider_actor_type="Bot",
            provider_associated=True,
        ),
    )
    assert bot_res.identity_type is IdentityType.BOT
    assert bot_res.attribution_eligibility.can_support_source_verified is False
    assert bot_res.attribution_eligibility.can_support_user_confirmed is False
    assert (
        bot_res.attribution_eligibility.block_reason_code
        is IdentityAttributionBlockReason.IDENTITY_IS_BOT
    )

    with pytest.raises(IdentityError) as bot_exc:
        service.confirm_identity_mapping(
            access,
            mapping_id=bot_res.mapping.mapping_id,
            idempotency_key=str(uuid.uuid4()),
            request=ConfirmIdentityMappingRequest(reason="try confirming bot"),
        )
    assert bot_exc.value.status_code == 422
    assert bot_exc.value.code == "BOT_IDENTITY_CONFIRMATION_FORBIDDEN"

    # 5. Direct evaluate_identity_attribution helper checks missing identity -> IDENTITY_NOT_FOUND
    with pg18_cluster.api_connection() as conn:
        with workspace_scoped_transaction(conn, access) as cur:
            missing_eval = evaluate_identity_attribution(
                cur, seed["workspace_id"], str(uuid.uuid4())
            )
            assert missing_eval.can_support_source_verified is False
            assert missing_eval.block_reason_code is IdentityAttributionBlockReason.IDENTITY_NOT_FOUND


def test_id05_revocation_and_conflict_cascade_invalidates_contributions_facts_claims_exports(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    service = PostgresIdentityService(api_connection_factory=pg18_cluster.api_connection)

    # Setup verified identity + full downstream chain (contribution -> role fact -> claim -> READY export)
    resolved = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="555111",
            username="frank-dev",
            provider_associated=True,
            is_oauth_account=True,
            canonical_user_id=seed["user_id"],
        ),
    )
    chain = _seed_downstream_chain_for_identity(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        user_id=seed["user_id"],
        identity_id=resolved.identity_id,
    )

    # Revoke via HTTP router to verify wire contract + idempotent replay
    app = create_app(
        identities_router=create_identities_router(
            service=service,
            resolve_access=lambda _req: access,
        ),
        install_identities_handlers=True,
    )
    client = TestClient(app)
    idem_key = str(uuid.uuid4())

    resp = client.post(
        f"/v1/identities/mappings/{resolved.mapping.mapping_id}/actions/revoke",
        headers={"Idempotency-Key": idem_key, "X-Request-ID": "req-revoke-1"},
        json={"reason": "Compromised or misattributed GitHub account"},
    )
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "private, no-store"
    assert resp.headers["X-Request-ID"] == "req-revoke-1"
    body = resp.json()
    assert body["idempotent_replay"] is False
    assert body["mapping"]["status"] == "REVOKED"
    assert body["attribution_eligibility"]["can_support_source_verified"] is False
    assert body["attribution_eligibility"]["block_reason_code"] == "IDENTITY_REVOKED"
    assert body["invalidated_counts"] == {
        "contributions": 1,
        "facts": 1,
        "claim_atoms": 1,
        "claims": 1,
        "exports": 1,
    }

    # Verify DB state across all cascaded tables + review_actions + audit_events + outbox_events
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM public.contributions WHERE id = %s::uuid",
                (chain["contribution_id"],),
            )
            assert cur.fetchone() == ("STALE",)
            cur.execute(
                "SELECT status FROM public.facts WHERE id = %s::uuid",
                (chain["role_fact_id"],),
            )
            assert cur.fetchone() == ("STALE",)
            # Unrelated non-identity ACTION fact stays SUPPORTED
            cur.execute(
                "SELECT status FROM public.facts WHERE id = %s::uuid",
                (chain["action_fact_id"],),
            )
            assert cur.fetchone() == ("SUPPORTED",)
            cur.execute(
                "SELECT status FROM public.claim_atoms WHERE id = %s::uuid",
                (chain["atom_role_id"],),
            )
            assert cur.fetchone() == ("STALE",)
            cur.execute(
                "SELECT status FROM public.claims WHERE id = %s::uuid",
                (chain["claim_id"],),
            )
            assert cur.fetchone() == ("STALE",)
            cur.execute(
                "SELECT status, invalidation_reason, invalidated_at IS NOT NULL FROM public.exports WHERE id = %s::uuid",
                (chain["export_id"],),
            )
            assert cur.fetchone() == ("INVALIDATED", "IDENTITY_MAPPING_INVALIDATED", True)
            cur.execute(
                """
                SELECT COUNT(*) FROM public.review_actions
                WHERE workspace_id = %s::uuid
                  AND target_type = 'IDENTITY_MAPPING'
                  AND target_id = %s::uuid
                  AND action = 'REJECT'
                """,
                (seed["workspace_id"], resolved.mapping.mapping_id),
            )
            assert cur.fetchone() == (1,)
            cur.execute(
                """
                SELECT event_type, aggregate_type, aggregate_id::text
                FROM public.outbox_events
                WHERE workspace_id = %s::uuid
                  AND event_type = 'IDENTITY_ATTRIBUTION_INVALIDATED'
                """,
                (seed["workspace_id"],),
            )
            assert cur.fetchall() == [
                ("IDENTITY_ATTRIBUTION_INVALIDATED", "IDENTITY", resolved.identity_id)
            ]

    # Exact idempotent replay returns identical counts and idempotent_replay=True
    replay_resp = client.post(
        f"/v1/identities/mappings/{resolved.mapping.mapping_id}/actions/revoke",
        headers={"Idempotency-Key": idem_key},
        json={"reason": "Compromised or misattributed GitHub account"},
    )
    assert replay_resp.status_code == 200
    assert replay_resp.json()["idempotent_replay"] is True
    assert replay_resp.json()["invalidated_counts"] == body["invalidated_counts"]

    # Same idempotency key with DIFFERENT reason -> 409 IDEMPOTENCY_CONFLICT
    conflict_key_resp = client.post(
        f"/v1/identities/mappings/{resolved.mapping.mapping_id}/actions/revoke",
        headers={"Idempotency-Key": idem_key},
        json={"reason": "Different reason"},
    )
    assert conflict_key_resp.status_code == 409
    assert conflict_key_resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # Cross-workspace concealment (ws B trying to revoke ws A's mapping -> 404 RESOURCE_NOT_FOUND)
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="beta/other")
    access_b = _access_for(seed_b)
    app_b = create_app(
        identities_router=create_identities_router(
            service=service,
            resolve_access=lambda _req: access_b,
        ),
        install_identities_handlers=True,
    )
    resp_cross = TestClient(app_b).post(
        f"/v1/identities/mappings/{resolved.mapping.mapping_id}/actions/revoke",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"reason": "cross tenant probe"},
    )
    assert resp_cross.status_code == 404
    assert resp_cross.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    # Test mark_identity_mapping_conflicted cascade on a second identity
    resolved2 = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="666222",
            username="grace-dev",
            provider_associated=True,
        ),
    )
    chain2 = _seed_downstream_chain_for_identity(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        user_id=seed["user_id"],
        identity_id=resolved2.identity_id,
    )
    conflicted = service.mark_identity_mapping_conflicted(
        access,
        mapping_id=resolved2.mapping.mapping_id,
        idempotency_key=str(uuid.uuid4()),
        reason="Conflicting external_user_id detected during sync",
    )
    assert conflicted.mapping.status is ExternalIdentityMappingStatus.CONFLICTED
    assert conflicted.attribution_eligibility.can_support_source_verified is False
    assert (
        conflicted.attribution_eligibility.block_reason_code
        is IdentityAttributionBlockReason.IDENTITY_CONFLICTED
    )
    assert conflicted.invalidated_counts.contributions == 1
    assert conflicted.invalidated_counts.facts == 1
    assert conflicted.invalidated_counts.claims == 1
    assert conflicted.invalidated_counts.exports == 1

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM public.identities WHERE id = %s::uuid",
                (resolved2.identity_id,),
            )
            assert cur.fetchone() == (IdentityStatus.AMBIGUOUS.value,)
            cur.execute(
                "SELECT status FROM public.exports WHERE id = %s::uuid",
                (chain2["export_id"],),
            )
            assert cur.fetchone() == ("INVALIDATED",)


def test_concurrency_barrier_revoke_race_and_mid_flight_membership_revocation_rollback(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    base_service = PostgresIdentityService(api_connection_factory=pg18_cluster.api_connection)

    resolved = base_service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="888999",
            username="race-user",
            provider_associated=True,
        ),
    )
    chain = _seed_downstream_chain_for_identity(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        user_id=seed["user_id"],
        identity_id=resolved.identity_id,
    )

    # 1. Mid-flight membership revocation during before_commit_hook -> full rollback!
    entered_hook = threading.Event()
    membership_revoked = threading.Event()

    def _pause_before_commit(_action: str, _cur: Any) -> None:
        entered_hook.set()
        assert membership_revoked.wait(timeout=5.0)

    racy_service = PostgresIdentityService(
        api_connection_factory=pg18_cluster.api_connection,
        before_commit_hook=_pause_before_commit,
    )
    results: dict[str, Any] = {}

    def _run_revoke() -> None:
        try:
            racy_service.revoke_identity_mapping(
                access,
                mapping_id=resolved.mapping.mapping_id,
                idempotency_key=str(uuid.uuid4()),
                request=RevokeIdentityMappingRequest(reason="revoking during membership race"),
            )
            results["outcome"] = "committed"
        except Exception as exc:  # noqa: BLE001
            results["error"] = exc

    t = threading.Thread(target=_run_revoke)
    t.start()
    assert entered_hook.wait(timeout=5.0)

    # Revoke workspace membership concurrently on separate admin connection
    with pg18_cluster.admin_connection() as aconn:
        with aconn.transaction():
            with aconn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.workspace_memberships
                    SET revoked_at = now()
                    WHERE workspace_id = %s::uuid AND user_id = %s::uuid
                    """,
                    (seed["workspace_id"], seed["user_id"]),
                )
    membership_revoked.set()
    t.join(timeout=10.0)

    assert isinstance(results.get("error"), WorkspaceMembershipDeniedError)

    # Verify complete rollback: mapping still VERIFIED, claim still APPROVED, export still READY
    with pg18_cluster.admin_connection() as aconn:
        with aconn.cursor() as cur:
            cur.execute(
                "SELECT status FROM public.external_identity_mappings WHERE id = %s::uuid",
                (resolved.mapping.mapping_id,),
            )
            assert cur.fetchone() == ("VERIFIED",)
            cur.execute(
                "SELECT status FROM public.claims WHERE id = %s::uuid",
                (chain["claim_id"],),
            )
            assert cur.fetchone() == ("APPROVED",)
            cur.execute(
                "SELECT status FROM public.exports WHERE id = %s::uuid",
                (chain["export_id"],),
            )
            assert cur.fetchone() == ("READY",)

            # Restore membership for the next barrier test
            cur.execute(
                """
                UPDATE public.workspace_memberships
                SET revoked_at = NULL
                WHERE workspace_id = %s::uuid AND user_id = %s::uuid
                """,
                (seed["workspace_id"], seed["user_id"]),
            )
        aconn.commit()

    # 2. Barrier concurrency: two threads race with DIFFERENT idempotency keys to revoke same mapping
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, Any]] = []
    lock = threading.Lock()

    def _race_worker(tag: str) -> None:
        barrier.wait(timeout=5.0)
        try:
            res = base_service.revoke_identity_mapping(
                access,
                mapping_id=resolved.mapping.mapping_id,
                idempotency_key=str(uuid.uuid4()),
                request=RevokeIdentityMappingRequest(reason=f"concurrent revoke {tag}"),
            )
            with lock:
                outcomes.append(("ok", res))
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(("err", exc))

    t1 = threading.Thread(target=_race_worker, args=("A",))
    t2 = threading.Thread(target=_race_worker, args=("B",))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    ok_outcomes = [o for o in outcomes if o[0] == "ok"]
    err_outcomes = [o for o in outcomes if o[0] == "err"]
    assert len(ok_outcomes) == 1
    assert len(err_outcomes) == 1
    assert isinstance(err_outcomes[0][1], IdentityError)
    assert err_outcomes[0][1].status_code == 409
    assert err_outcomes[0][1].code == "MAPPING_ALREADY_REVOKED"


def test_ambiguous_collision_deletion_fence_and_list_identities_http(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    access = _access_for(seed)
    service = PostgresIdentityService(api_connection_factory=pg18_cluster.api_connection)

    # 1. Seed two distinct verified identities, then observe a heuristic email_hash/username collision
    id_a = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="1001",
            username="shared-handle",
            email="a@example.com",
            provider_associated=True,
        ),
    )
    id_b = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.PR_AUTHOR,
            external_user_id="1002",
            username="other-handle",
            email="shared@example.com",
            provider_associated=True,
        ),
    )
    assert id_a.identity_id != id_b.identity_id

    # Heuristic observation matching id_a by username AND id_b by email -> CONFLICTED + AMBIGUOUS!
    collided = service.resolve_or_upsert_github_identity(
        access,
        observation=GitHubActorObservation(
            actor_role=GitHubActorRole.COMMIT_AUTHOR,
            username="shared-handle",
            email="shared@example.com",
            provider_associated=False,
        ),
    )
    assert collided.conflict_detected is True
    assert collided.mapping.status is ExternalIdentityMappingStatus.CONFLICTED
    assert collided.identity_status is IdentityStatus.AMBIGUOUS
    assert collided.attribution_eligibility.can_support_source_verified is False
    assert (
        collided.attribution_eligibility.block_reason_code
        is IdentityAttributionBlockReason.IDENTITY_CONFLICTED
    )

    # Confirming conflicted mapping is rejected with 409 MAPPING_CONFLICTED
    with pytest.raises(IdentityError) as conf_exc:
        service.confirm_identity_mapping(
            access,
            mapping_id=collided.mapping.mapping_id,
            idempotency_key=str(uuid.uuid4()),
            request=ConfirmIdentityMappingRequest(reason="try confirming conflict"),
        )
    assert conf_exc.value.status_code == 409
    assert conf_exc.value.code == "MAPPING_CONFLICTED"

    # 2. GET /v1/identities exposes redacted email_hash (never raw email) + attribution_eligibility
    app = create_app(
        identities_router=create_identities_router(
            service=service,
            resolve_access=lambda _req: access,
        ),
        install_identities_handlers=True,
    )
    client = TestClient(app)
    resp = client.get("/v1/identities")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "private, no-store"
    raw_json_str = resp.text
    assert "a@example.com" not in raw_json_str
    assert "shared@example.com" not in raw_json_str
    assert len(resp.json()["items"]) == 2

    # 3. Active deletion_jobs fence blocks confirm/revoke on linked identity with 409 DELETION_PENDING
    _seed_downstream_chain_for_identity(
        pg18_cluster,
        workspace_id=seed["workspace_id"],
        source_resource_id=seed["source_resource_id"],
        user_id=seed["user_id"],
        identity_id=id_b.identity_id,
    )
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.deletion_jobs (
                        workspace_id, source_resource_id, status
                    ) VALUES (%s::uuid, %s::uuid, 'RUNNING')
                    """,
                    (seed["workspace_id"], seed["source_resource_id"]),
                )

    with pytest.raises(IdentityError) as del_exc:
        service.revoke_identity_mapping(
            access,
            mapping_id=id_b.mapping.mapping_id,
            idempotency_key=str(uuid.uuid4()),
            request=RevokeIdentityMappingRequest(reason="blocked by deletion fence"),
        )
    assert del_exc.value.status_code == 409
    assert del_exc.value.code == "DELETION_PENDING"
