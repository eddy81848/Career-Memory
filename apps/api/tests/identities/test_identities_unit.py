"""Unit tests for M4 Wave 1A GitHub identity resolution helpers & HTTP contracts (T-401, T-401A).

Covers:
- Never persisting raw email (hash_email normalization to sha256:<64 hex>)
- GitHub noreply email parsing (<id>+<login>@users.noreply.github.com vs <login>@users.noreply.github.com)
- Bot actor detection ([bot], github-actions, dependabot, renovate, provider_actor_type=Bot)
- Co-author trailer parsing without exposing plaintext email
- Actor role personal contribution eligibility separation (PR_AUTHOR/COMMIT_AUTHOR/REVIEWER vs COMMITTER/CO_AUTHOR/BOT)
- Router HTTP validation (Idempotency-Key, Cache-Control: private, no-store, OD-008 project_id block, Plan C raw keys)
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.identities import (
    GitHubActorRole,
    IdentityAttributionBlockReason,
    IdentityAttributionEligibility,
    IdentityError,
    classify_actor_contribution_eligibility,
    hash_email,
    is_bot_actor,
    parse_co_author_trailers,
    parse_github_noreply_email,
)
from career_memory_api.schema_contracts.enums import (
    ExportFormat,
    ExportPolicy,
    ExportStatus,
    ExportVerificationClass,
    FactStatus,
    IdentityType,
)


def _dummy_access() -> WorkspaceAccessContext:
    ws_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer="https://auth.local/v1", subject=f"sub-{user_id}"),
        ),
        membership=WorkspaceMembership(
            workspace_id=ws_id,
            user_id=user_id,
            role="OWNER",
        ),
    )


def test_m4_schema_contract_enums_include_fact_superseded_and_exports() -> None:
    assert FactStatus.SUPERSEDED == "SUPERSEDED"
    assert {e.value for e in FactStatus} == {
        "CANDIDATE",
        "SUPPORTED",
        "USER_CONFIRMED",
        "CONFLICTED",
        "STALE",
        "REJECTED",
        "SUPERSEDED",
    }
    assert {e.value for e in ExportFormat} == {"MARKDOWN", "JSON"}
    assert {e.value for e in ExportPolicy} == {"SOURCE_VERIFIED_ONLY", "ALLOW_USER_CONFIRMED"}
    assert {e.value for e in ExportStatus} == {
        "PENDING",
        "READY",
        "INVALIDATED",
        "FAILED",
        "DELETED",
    }
    assert {e.value for e in ExportVerificationClass} == {
        "SOURCE_VERIFIED",
        "USER_CONFIRMED",
        "MIXED",
    }


def test_hash_email_normalizes_whitespace_and_case() -> None:
    expected = "sha256:" + hashlib.sha256(b"alice@example.com").hexdigest()
    assert hash_email("  Alice@Example.COM \n") == expected
    assert "alice@example.com" not in expected

    with pytest.raises(ValueError):
        hash_email("   ")


def test_parse_github_noreply_email_distinguishes_numeric_id_vs_login_only() -> None:
    numeric = parse_github_noreply_email("12345678+OctoCat@users.noreply.github.com")
    assert numeric is not None
    assert numeric.numeric_user_id == "12345678"
    assert numeric.login == "OctoCat"
    assert numeric.has_numeric_id is True
    assert numeric.is_web_flow is False

    login_only = parse_github_noreply_email("octocat@users.noreply.github.com")
    assert login_only is not None
    assert login_only.numeric_user_id is None
    assert login_only.login == "octocat"
    assert login_only.has_numeric_id is False
    assert login_only.is_web_flow is False

    web_flow = parse_github_noreply_email("noreply@github.com")
    assert web_flow is not None
    assert web_flow.is_web_flow is True
    assert web_flow.has_numeric_id is False

    assert parse_github_noreply_email("alice@example.com") is None


def test_is_bot_actor_detects_bot_suffix_known_bots_and_provider_type() -> None:
    assert is_bot_actor(username="dependabot[bot]") is True
    assert is_bot_actor(username="renovate[bot]") is True
    assert is_bot_actor(username="github-actions[bot]") is True
    assert is_bot_actor(username="github-actions") is True
    assert is_bot_actor(username="dependabot") is True
    assert is_bot_actor(username="renovate") is True
    assert is_bot_actor(username="custom-helper", provider_actor_type="Bot") is True
    assert is_bot_actor(username="octocat", actor_role=GitHubActorRole.BOT) is True

    assert is_bot_actor(username="octocat", provider_actor_type="User") is False
    assert is_bot_actor(username="botanist", provider_actor_type="User") is False
    # web-flow is GitHub's merge committer (separated via COMMITTER role, not human author)
    assert is_bot_actor(username="web-flow", actor_role=GitHubActorRole.COMMITTER) is False


def test_parse_co_author_trailers_redacts_raw_email_and_extracts_noreply_hint() -> None:
    msg = (
        "Implement retry backoff\n\n"
        "Co-authored-by: Alice Dev <alice@example.com>\n"
        "co-authored-by: Bob Ops <987654+bob-ops@users.noreply.github.com>\n"
        "Co-authored-by: Carol <carol@users.noreply.github.com>\n"
    )
    trailers = parse_co_author_trailers(msg)
    assert len(trailers) == 3

    assert trailers[0].display_name == "Alice Dev"
    assert trailers[0].email_hash == hash_email("alice@example.com")
    assert trailers[0].noreply_numeric_user_id is None
    assert trailers[0].noreply_login is None
    assert not hasattr(trailers[0], "email")

    assert trailers[1].display_name == "Bob Ops"
    assert trailers[1].email_hash == hash_email("987654+bob-ops@users.noreply.github.com")
    assert trailers[1].noreply_numeric_user_id == "987654"
    assert trailers[1].noreply_login == "bob-ops"

    assert trailers[2].display_name == "Carol"
    assert trailers[2].noreply_numeric_user_id is None
    assert trailers[2].noreply_login == "carol"


def test_actor_role_contribution_eligibility_separates_author_reviewer_committer_coauthor_bot() -> None:
    # Human PR_AUTHOR / COMMIT_AUTHOR -> eligible for IMPLEMENTED
    pr_author = classify_actor_contribution_eligibility(
        actor_role=GitHubActorRole.PR_AUTHOR,
        identity_type=IdentityType.PERSON,
    )
    assert pr_author.eligible_for_implemented is True
    assert pr_author.eligible_for_reviewed is False
    assert pr_author.can_auto_verify_role is True

    reviewer = classify_actor_contribution_eligibility(
        actor_role=GitHubActorRole.REVIEWER,
        identity_type=IdentityType.PERSON,
    )
    assert reviewer.eligible_for_implemented is False
    assert reviewer.eligible_for_reviewed is True
    assert reviewer.can_auto_verify_role is True

    # COMMITTER (e.g. web-flow) -> never eligible for personal IMPLEMENTED/REVIEWED
    committer = classify_actor_contribution_eligibility(
        actor_role=GitHubActorRole.COMMITTER,
        identity_type=IdentityType.PERSON,
    )
    assert committer.eligible_for_implemented is False
    assert committer.eligible_for_reviewed is False
    assert committer.can_auto_verify_role is False

    # CO_AUTHOR -> never auto-verified IMPLEMENTED proof on its own
    co_author = classify_actor_contribution_eligibility(
        actor_role=GitHubActorRole.CO_AUTHOR,
        identity_type=IdentityType.PERSON,
    )
    assert co_author.eligible_for_implemented is False
    assert co_author.can_auto_verify_role is False

    # BOT -> strictly ineligible for any human personal attribution
    bot = classify_actor_contribution_eligibility(
        actor_role=GitHubActorRole.PR_AUTHOR,
        identity_type=IdentityType.BOT,
    )
    assert bot.eligible_for_implemented is False
    assert bot.eligible_for_reviewed is False
    assert bot.can_auto_verify_role is False


def test_identities_router_headers_validation_and_concealment() -> None:
    access = _dummy_access()
    app = create_m1_contract_app(resolve_access=lambda _req: access)
    client = TestClient(app)

    # GET /v1/identities returns Cache-Control: private, no-store
    resp = client.get("/v1/identities", headers={"X-Request-ID": "req-id-01"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "private, no-store"
    assert resp.headers["X-Request-ID"] == "req-id-01"
    assert resp.json() == {"items": []}

    # OD-008: project_id query param rejected
    resp_proj = client.get("/v1/identities?project_id=proj-1")
    assert resp_proj.status_code == 422
    assert resp_proj.json()["error"]["code"] == "PROHIBITED_PROJECT_ID_SCOPE"

    # Confirm without Idempotency-Key -> 422
    mapping_id = str(uuid.uuid4())
    resp_no_key = client.post(f"/v1/identities/mappings/{mapping_id}/actions/confirm", json={})
    assert resp_no_key.status_code == 422
    assert resp_no_key.headers["Cache-Control"] == "private, no-store"

    # Confirm with valid Idempotency-Key against empty in-memory service -> 404 concealed
    resp_404 = client.post(
        f"/v1/identities/mappings/{mapping_id}/actions/confirm",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"reason": "confirmed by user"},
    )
    assert resp_404.status_code == 404
    assert resp_404.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert resp_404.headers["Cache-Control"] == "private, no-store"

    # Revoke with blank reason -> 422
    resp_blank_reason = client.post(
        f"/v1/identities/mappings/{mapping_id}/actions/revoke",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"reason": "   "},
    )
    assert resp_blank_reason.status_code == 422

    # Plan C raw key rejected in request body
    resp_raw = client.post(
        f"/v1/identities/mappings/{mapping_id}/actions/revoke",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"reason": "compromised", "raw_text": "forbidden raw body"},
    )
    assert resp_raw.status_code == 422
    assert resp_raw.json()["error"]["code"] == "PROHIBITED_RAW_TEXT_PERSISTENCE"
