"""Live 127.0.0.1 loopback server mounting the REAL M4 career_memory_api FastAPI app + PG18.

Runs strictly against an ephemeral local PostgreSQL 18 cluster (migrations 0010..0070,
cm_api_app NOBYPASSRLS + FORCE RLS) and mounts the production M4 routers:
- `create_facts_router` (`PostgresFactService`)
- `create_claims_router` (`PostgresClaimService`)
- `create_exports_router` (`PostgresExportService`)
- `create_resolver_router` (`RefetchResolverService` + `PostgresProvenanceMetadataStore`)
- `create_contracts_router` (delegating `PATCH /v1/claims/{id}` to `PostgresClaimService.edit_claim`)

Zero external network calls; zero live external LLM calls (SYNTHETIC_FIXTURE_OFFLINE only).
Guarantees full PG18 cluster teardown on exit so zero postgres processes leak.
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Request
from psycopg.types.json import Jsonb

REPO_ROOT = Path(__file__).resolve().parents[4]
API_ROOT = REPO_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT / "src"))
sys.path.insert(0, str(API_ROOT))

from career_memory_api.app import create_app  # noqa: E402
from career_memory_api.authz import (  # noqa: E402
    AuthCredentials,
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.canonicalization import (  # noqa: E402
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    ContentUnitType,
    build_span_locator,
    canonicalize_source_bytes,
    sha256_prefixed_text,
)
from career_memory_api.claim_eval.runner import (  # noqa: E402
    DynamicStageFixtureAdapter,
    EphemeralClaimEvalPg18Cluster,
    FixtureProviderContentFetcher,
    _ApiConnectionProvenanceStore,
    _CompositeM4ContractService,
)
from career_memory_api.claims import (  # noqa: E402
    PostgresClaimService,
    create_claims_router,
)
from career_memory_api.conformance.ai_schemas import (  # noqa: E402
    SCHEMA_PACKAGE_VERSION,
    FactPromotionDecision,
)
from career_memory_api.contracts_api.router import create_contracts_router  # noqa: E402
from career_memory_api.exports import (  # noqa: E402
    PostgresExportService,
    create_exports_router,
)
from career_memory_api.facts import (  # noqa: E402
    PostgresFactService,
    create_facts_router,
)
from career_memory_api.fault_harness.runner import (  # noqa: E402
    PostgresDeletionBackedContractService,
)
from career_memory_api.jobs import PostgresJobRuntime  # noqa: E402
from career_memory_api.llm_gate import (  # noqa: E402
    LlmProviderMode,
    StageBudgetPolicy,
)
from career_memory_api.resolver import (  # noqa: E402
    OperationBudgetConfig,
    ProviderFetchOutcomeKind,
    ProviderFetchRequest,
    ProviderFetchResponse,
    RefetchResolverService,
    create_resolver_router,
)
from career_memory_api.schema_contracts.enums import (  # noqa: E402
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimVerification,
    FactStatus,
    FactType,
    ResourceVisibility,
    SupportType,
    SupportValidator,
)

RAW_SOURCE_CANARY_BODY = (
    "# PR #407: Checkout Retry Queue Hardening\n\n"
    "Implemented idempotent retry queue with exponential backoff for billing webhooks.\n"
    "Transient Raw Body Sentinel: LIVE_WIRE_CANARY_9E4B1A_DO_NOT_PERSIST\n"
)


def _budget() -> StageBudgetPolicy:
    return StageBudgetPolicy(
        max_input_bytes=256 * 1024,
        max_calls=50,
        calls_used=0,
        max_repair_attempts=0,
        max_pre_spend_retries=0,
        overage_credit_disabled=True,
        quota_remaining_known_and_sufficient=True,
    )


class LiveClaimReviewBackendState:
    def __init__(self, cluster: EphemeralClaimEvalPg18Cluster) -> None:
        self.cluster = cluster
        self.fetcher = FixtureProviderContentFetcher()
        self.access: WorkspaceAccessContext | None = None
        self.seeded: dict[str, Any] = {}
        self.refetch_mode: str = "MATCHED"

        self.store = _ApiConnectionProvenanceStore(cluster.api_connection)
        self.resolver = RefetchResolverService(
            metadata_store=self.store,
            fetcher=self.fetcher,
            default_budget=OperationBudgetConfig(),
        )
        self.fact_service = PostgresFactService(
            api_connection_factory=cluster.api_connection,
            resolver=self.resolver,
        )
        self.claim_service = PostgresClaimService(
            api_connection_factory=cluster.api_connection,
            resolver=self.resolver,
            default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
        self.export_service = PostgresExportService(
            api_connection_factory=cluster.api_connection,
            resolver=self.resolver,
        )
        job_runtime = PostgresJobRuntime(connection_factory=cluster.worker_connection)
        deletion_service = PostgresDeletionBackedContractService(
            job_runtime=job_runtime,
            now_fn=lambda: datetime.now(UTC),
        )
        self.contracts_service = _CompositeM4ContractService(
            claim_service=self.claim_service,
            deletion_service=deletion_service,
        )

    def set_refetch_mode(self, mode: str) -> None:
        self.refetch_mode = mode
        if mode == "MATCHED":
            self.fetcher.set_override_handler(None)
        elif mode == "REVISION_MISMATCH":
            mutated = (RAW_SOURCE_CANARY_BODY + "\n[UPSTREAM DRIFT EDIT]\n").encode("utf-8")

            def _drifted(_req: ProviderFetchRequest) -> ProviderFetchResponse:
                return ProviderFetchResponse(
                    kind=ProviderFetchOutcomeKind.OK,
                    raw_bytes=mutated,
                    http_status=200,
                    mime_type="text/markdown",
                    provider_visibility=ResourceVisibility.PUBLIC,
                )

            self.fetcher.set_override_handler(_drifted)
        else:
            raise ValueError(f"Unsupported refetch mode: {mode}")

    def reset_and_seed(self) -> dict[str, Any]:
        self.cluster.truncate_all()
        self.set_refetch_mode("MATCHED")

        now = datetime.now(UTC)
        user_id = str(uuid.uuid4())
        ws_id = str(uuid.uuid4())
        acct_id = str(uuid.uuid4())
        res_id = str(uuid.uuid4())
        obj_id = str(uuid.uuid4())
        rev_id = str(uuid.uuid4())
        cu_id = str(uuid.uuid4())
        ident_id = str(uuid.uuid4())
        map_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())

        access = WorkspaceAccessContext(
            user=AuthenticatedUser(
                user_id=user_id,
                auth_subject=AuthSubject(
                    issuer="https://auth.local/v1", subject=f"live-m4-{user_id}"
                ),
            ),
            membership=WorkspaceMembership(
                workspace_id=ws_id, user_id=user_id, role="OWNER"
            ),
        )
        self.access = access

        raw_bytes = RAW_SOURCE_CANARY_BODY.encode("utf-8")
        canon = canonicalize_source_bytes(raw_bytes)
        assert canon.document is not None
        canon_text = canon.document.text
        rev_hash = sha256_prefixed_text(canon_text)
        loc = build_span_locator(canon_text, 0, len(canon_text))
        unit_hash = sha256_prefixed_text(canon_text)
        self.fetcher.register_revision_bytes(rev_id, raw_bytes)

        with self.cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                        VALUES (%s::uuid, %s, %s, 'alex@example.test', 'Alex Kim')
                        """,
                        (user_id, access.user.auth_subject.issuer, access.user.auth_subject.subject),
                    )
                    cur.execute(
                        "INSERT INTO public.workspaces (id, owner_user_id, name) VALUES (%s::uuid, %s::uuid, 'Live M4 Workspace')",
                        (ws_id, user_id),
                    )
                    cur.execute(
                        "INSERT INTO public.workspace_memberships (workspace_id, user_id, role) VALUES (%s::uuid, %s::uuid, 'OWNER')",
                        (ws_id, user_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.source_accounts (
                            id, workspace_id, provider, external_account_id,
                            token_secret_ref, scopes, status
                        ) VALUES (
                            %s::uuid, %s::uuid, 'GITHUB', 'gh-acct-live-m4',
                            'vault://live-m4/token', ARRAY['public_repo']::text[], 'ACTIVE'
                        )
                        """,
                        (acct_id, ws_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.identities (
                            id, workspace_id, display_name, identity_type, canonical_user_id, status
                        ) VALUES (%s::uuid, %s::uuid, 'Alex Kim', 'PERSON', %s::uuid, 'ACTIVE')
                        """,
                        (ident_id, ws_id, user_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.external_identity_mappings (
                            id, workspace_id, identity_id, provider, external_user_id,
                            username, verification_method, status, verified_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'GITHUB', 'gh-uid-407',
                            'alex-kim', 'OAUTH_ACCOUNT', 'VERIFIED', %s
                        )
                        """,
                        (map_id, ws_id, ident_id, now),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.source_resources (
                            id, workspace_id, source_account_id, provider_resource_id,
                            resource_type, name, canonical_url, visibility,
                            processing_mode, raw_storage_mode, raw_retention_seconds,
                            status, policy_version
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'repo-live-m4',
                            'GITHUB_REPOSITORY', 'acme/checkout-service',
                            'https://github.com/acme/checkout-service', 'PUBLIC',
                            'PUBLIC_CLOUD', 'NONE', NULL, 'ACTIVE', 'policy-v1'
                        )
                        """,
                        (res_id, ws_id, acct_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.source_objects (
                            id, workspace_id, source_resource_id, object_type,
                            provider_object_id, lifecycle_state
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', '407', 'ACTIVE')
                        """,
                        (obj_id, ws_id, res_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.source_object_revisions (
                            id, workspace_id, source_object_id, provider_revision_id,
                            content_hash, hash_policy_version, observed_at,
                            is_tombstone, normalized_metadata, connector_version, schema_version
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'rev-pr-407',
                            %s, %s, %s, FALSE, '{}'::jsonb, 'gh-v1', '1.0'
                        )
                        """,
                        (rev_id, ws_id, obj_id, rev_hash, CANONICALIZATION_VERSION, now),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.content_units (
                            id, workspace_id, source_object_revision_id,
                            unit_type, anchor, text_content, content_ref,
                            content_hash, canonicalization_version,
                            retention_class, expires_at, ordinal, policy_state, created_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, NULL, NULL,
                            %s, %s,
                            'NONE', NULL, 0, 'ACTIVE', %s
                        )
                        """,
                        (
                            cu_id,
                            ws_id,
                            rev_id,
                            ContentUnitType.PR_BODY_SECTION.value,
                            Jsonb({"parser_version": PARSER_VERSION, "locator": loc.to_dict()}),
                            unit_hash,
                            CANONICALIZATION_VERSION,
                            now,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.work_events (
                            id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                            version, title, status, created_by,
                            occurred_start_at, occurred_end_at, created_at, updated_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'PR',
                            1, 'PR #407: Checkout Retry Queue Hardening', 'CONFIRMED', 'RULE',
                            %s, %s, %s, %s
                        )
                        """,
                        (event_id, ws_id, obj_id, now - timedelta(hours=2), now, now, now),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.event_memberships (
                            id, workspace_id, event_id, source_object_id,
                            relation_class, relation_type, origin, status, created_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            'MUST_LINK', 'PR_PRIMARY', 'RULE', 'ACTIVE', %s
                        )
                        """,
                        (str(uuid.uuid4()), ws_id, event_id, obj_id, now),
                    )

        # Stage 2A + 2B: Extract & validate a real SUPPORTED Fact
        fact_text = "Implemented idempotent retry queue with exponential backoff for billing webhooks."
        s2a = DynamicStageFixtureAdapter(
            lambda inp: [
                {
                    "schema_version": SCHEMA_PACKAGE_VERSION,
                    "extractor_run_id": inp.context_metadata["extractor_run_id"],
                    "extractor_version": inp.context_metadata["extractor_version"],
                    "source_object_revision_id": rev_id,
                    "content_unit_id": cu_id,
                    "locator": loc.to_dict(),
                    "fact_type": FactType.ACTION.value,
                    "predicate": "implemented_retry_queue",
                    "object_text": fact_text,
                    "normalized_value": {
                        "completion_state": "COMPLETED",
                        "scope": "INDIVIDUAL",
                        "environment_scope": "PRODUCTION",
                        "causal_basis": "UNSPECIFIED",
                    },
                    "status": FactStatus.CANDIDATE.value,
                    "subject_identity_id": ident_id,
                    "uncertainty_notes": None,
                }
            ]
        )
        ext = self.fact_service.extract_candidates_for_unit(
            access,
            event_id=event_id,
            content_unit_id=cu_id,
            budget=_budget(),
            adapter=s2a,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            operation_id="live-m4-s2a",
        )
        fact_id = ext.persisted_fact_ids[0]
        s2b = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "validator_run_id": inp.context_metadata["validator_run_id"],
                "extractor_run_id": ext.extractor_run_id,
                "fact_candidate_id": fact_id,
                "source_object_revision_id": rev_id,
                "content_unit_id": cu_id,
                "verified_locator": loc.to_dict(),
                "support_type": SupportType.DIRECT.value,
                "validator": SupportValidator.MODEL.value,
                "validator_version": inp.context_metadata["validator_version"],
                "promotion_decision": FactPromotionDecision.PROMOTE_SUPPORTED.value,
                "revision_and_locator_verified": True,
                "promoted_fact_id": fact_id,
                "rejection_reason": None,
            }
        )
        self.fact_service.validate_and_normalize_candidates(
            access,
            event_id=event_id,
            fact_ids=[fact_id],
            content_unit_id=cu_id,
            budget=_budget(),
            adapter=s2b,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
            candidate_locator_by_fact_id={fact_id: loc.to_dict()},
            operation_id="live-m4-s2b",
        )
        fs_id = self.fact_service.get_fact_supports(access, fact_id=fact_id).supports[0].support_id

        # Stage 3A + 3B + 3C: Generate & validate a SOURCE_VERIFIED Claim
        claim_text = "Implemented idempotent retry queue with exponential backoff for billing webhooks."
        s3a = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "generator_run_id": inp.context_metadata["generator_run_id"],
                "confirmed_event_id": event_id,
                "input_fact_ids": [fact_id],
                "claim_version": 1,
                "claim_text": claim_text,
                "status": "DRAFT",
            }
        )
        gen, verified_claim_id = self.claim_service.generate_claim_draft(
            access,
            event_id=event_id,
            input_fact_ids=[fact_id],
            budget=_budget(),
            adapter=s3a,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
        assert verified_claim_id is not None
        s3b = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "atomizer_run_id": inp.context_metadata["atomizer_run_id"],
                "generator_run_id": gen.generator_run_id,
                "claim_id": verified_claim_id,
                "claim_version": 1,
                "atoms": [
                    {
                        "ordinal": 0,
                        "atom_type": ClaimAtomType.ACTION.value,
                        "atom_text": claim_text,
                    }
                ],
            }
        )
        ares = self.claim_service.atomize_claim(
            access,
            claim_id=verified_claim_id,
            budget=_budget(),
            adapter=s3b,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
        s3c = DynamicStageFixtureAdapter(
            lambda inp: {
                "schema_version": SCHEMA_PACKAGE_VERSION,
                "validation_run_id": inp.context_metadata["validation_run_id"],
                "generator_run_id": gen.generator_run_id,
                "atomizer_run_id": ares.atomizer_run_id,
                "claim_id": verified_claim_id,
                "claim_version": 1,
                "atom_validations": [
                    {
                        "ordinal": 0,
                        "atom_type": ClaimAtomType.ACTION.value,
                        "status": ClaimAtomStatus.SUPPORTED.value,
                        "fact_bindings": [
                            {
                                "fact_id": fact_id,
                                "support_type": SupportType.DIRECT.value,
                                "fact_support_ids": [fs_id],
                                "source_object_revision_id": rev_id,
                                "content_unit_id": cu_id,
                            }
                        ],
                        "unsupported_reason": None,
                    }
                ],
                "computed_verification": ClaimVerification.SOURCE_VERIFIED.value,
            }
        )
        self.claim_service.validate_claim_support(
            access,
            claim_id=verified_claim_id,
            atomizer_run_id=ares.atomizer_run_id,
            budget=_budget(),
            adapter=s3c,
            provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )

        # Also seed an UNVERIFIED claim (unsupported atom) to verify fail-closed 409 approval block
        unsupported_claim_id = str(uuid.uuid4())
        unsupported_text = "Reduced company-wide API latency by 90% annually."
        with self.cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.claims (
                            id, workspace_id, event_id, generator_run_id,
                            version, claim_text, status, created_at, updated_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, NULL,
                            1, %s, 'DRAFT', %s, %s
                        )
                        """,
                        (unsupported_claim_id, ws_id, event_id, unsupported_text, now, now),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.claim_atoms (
                            id, workspace_id, claim_id, ordinal, atom_type,
                            atom_text, status, created_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 0, 'METRIC',
                            %s, 'UNSUPPORTED', %s
                        )
                        """,
                        (str(uuid.uuid4()), ws_id, unsupported_claim_id, unsupported_text, now),
                    )

        self.seeded = {
            "workspace_id": ws_id,
            "source_resource_id": res_id,
            "event_id": event_id,
            "fact_id": fact_id,
            "content_unit_id": cu_id,
            "verified_claim_id": verified_claim_id,
            "unsupported_claim_id": unsupported_claim_id,
            "canary_token": "LIVE_WIRE_CANARY_9E4B1A_DO_NOT_PERSIST",
        }
        return dict(self.seeded)


class _LiveWorkspaceAccessProvider:
    def __init__(self, state: LiveClaimReviewBackendState) -> None:
        self._state = state

    def verify_identity(self, credentials: AuthCredentials) -> AuthenticatedUser | None:
        del credentials
        return self._state.access.user if self._state.access else None

    def verify_workspace_membership(
        self, user: AuthenticatedUser, target_workspace_id: str
    ) -> WorkspaceMembership | None:
        if self._state.access is None or user.user_id != self._state.access.user.user_id:
            return None
        if target_workspace_id and target_workspace_id != self._state.access.workspace_id:
            return None
        return self._state.access.membership


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build_app(state: LiveClaimReviewBackendState) -> Any:
    def _resolve_access(_request: Request) -> WorkspaceAccessContext:
        assert state.access is not None
        return state.access

    app = create_app(
        access_provider=_LiveWorkspaceAccessProvider(state),
        facts_router=create_facts_router(
            service=state.fact_service, resolve_access=_resolve_access
        ),
        claims_router=create_claims_router(
            service=state.claim_service, resolve_access=_resolve_access
        ),
        exports_router=create_exports_router(
            service=state.export_service, resolve_access=_resolve_access
        ),
        resolver_router=create_resolver_router(state.resolver),
        install_contract_handlers=True,
        install_facts_handlers=True,
        install_claims_handlers=True,
        install_exports_handlers=True,
    )
    app.include_router(
        create_contracts_router(
            service=state.contracts_service, resolve_access=_resolve_access
        )
    )

    @app.post("/__control/reset")
    async def control_reset() -> dict[str, Any]:
        return state.reset_and_seed()

    @app.post("/__control/set-refetch-mode")
    async def control_set_refetch_mode(request: Request) -> dict[str, Any]:
        body = await request.json()
        mode = str(body.get("mode", "MATCHED"))
        state.set_refetch_mode(mode)
        return {"mode": state.refetch_mode}

    return app


def main() -> int:
    port = _find_free_port()
    with EphemeralClaimEvalPg18Cluster(
        repo_root=REPO_ROOT, prefix="cm-live-m4-wire-pg18-"
    ) as cluster:
        state = LiveClaimReviewBackendState(cluster)
        seeded = state.reset_and_seed()
        app = build_app(state)

        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        def _handle_signal(_signum: int, _frame: Any) -> None:
            server.should_exit = True

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        async def _serve() -> None:
            serve_task = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.05)
                if serve_task.done():
                    break
            print(
                json.dumps({"baseUrl": f"http://127.0.0.1:{port}", **seeded}),
                flush=True,
            )
            await serve_task

        asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
