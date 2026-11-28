"""Live 127.0.0.1 loopback server mounting the REAL M5 career_memory_api FastAPI app + PG18.

Runs strictly against an ephemeral local PostgreSQL 18 cluster (migrations 0010..0080,
cm_api_app & cm_worker_app NOBYPASSRLS + FORCE RLS) and mounts the production M5 + M4 routers:
- `create_contracts_router` backed by `PostgresContractBoundaryService`:
  - `GET /v1/search` (`PostgresSearchService`)
  - `POST /v1/source-resources/{id}/deletion-preview` (`PostgresDeletionService`)
  - `POST /v1/source-resources/{id}/deletion-confirm` (`PostgresDeletionService`)
- `create_deletion_router` (`PostgresDeletionService` -> `GET /v1/deletion-jobs/{id}`)
- `create_budgets_router` (`PostgresQuotaBudgetService` -> `GET /v1/quota-status`)
- `create_claims_router` (`PostgresClaimService`)
- `create_exports_router` (`PostgresExportService`)
- `create_resolver_router` (`RefetchResolverService`)
- `EdgeOriginAndCacheMiddleware` via `install_edge_guard`

Zero external network calls; ₩0 spend. Guarantees full PG18 cluster teardown on exit.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from career_memory_api.budgets import (  # noqa: E402
    BudgetOperationKind,
    DefaultQuotaPolicyConfig,
    PostgresQuotaBudgetService,
    QuotaMetricLimit,
    create_budgets_router,
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
    FixtureProviderContentFetcher,
    _ApiConnectionProvenanceStore,
)
from career_memory_api.claims import (  # noqa: E402
    PostgresClaimService,
    create_claims_router,
)
from career_memory_api.contracts_api.router import (  # noqa: E402
    PostgresContractBoundaryService,
    create_contracts_router,
)
from career_memory_api.deletion import (  # noqa: E402
    PostgresDeletionService,
    create_deletion_router,
)
from career_memory_api.edge_guard import (  # noqa: E402
    DEFAULT_ALLOWED_API_PREFIXES,
    CorsCsrfConfig,
    EdgeGuardConfig,
    EdgeRuntimeCaps,
    ManualEdgePauseController,
    OriginAuthConfig,
    install_edge_guard,
)
from career_memory_api.exports import (  # noqa: E402
    PostgresExportService,
    create_exports_router,
)
from career_memory_api.fault_matrix.runner import (  # noqa: E402
    EphemeralFaultMatrixPg18Cluster,
)
from career_memory_api.llm_gate import LlmProviderMode  # noqa: E402
from career_memory_api.resolver import (  # noqa: E402
    OperationBudgetConfig,
    RefetchResolverService,
    create_resolver_router,
)
from career_memory_api.schema_contracts.enums import QuotaMetricKind  # noqa: E402
from career_memory_api.search import (  # noqa: E402
    PostgresSearchIndexer,
    PostgresSearchService,
)

LIVE_M5_ORIGIN_SECRET = "live-m5-pg18-origin-auth-secret"
RAW_CANARY_REPO_A = "LIVE_M5_REPO_A_RAW_CANARY_NEVER_PERSIST_7731"
RAW_CANARY_REPO_B = "LIVE_M5_REPO_B_RAW_CANARY_NEVER_PERSIST_8842"


class LiveMemoryOpsBackendState:
    """Live PG18-backed state container for M5 FE<->BE HTTP interoperability tests."""

    def __init__(self, cluster: EphemeralFaultMatrixPg18Cluster) -> None:
        self.cluster = cluster
        self.fetcher = FixtureProviderContentFetcher()
        self.access: WorkspaceAccessContext | None = None
        self.seeded: dict[str, Any] = {}
        self.edge_controller = ManualEdgePauseController()

        self.policy = DefaultQuotaPolicyConfig(
            limits={
                QuotaMetricKind.IMPORT_OBJECTS: QuotaMetricLimit(
                    soft_warning_limit=80, hard_cutoff_limit=95
                ),
                QuotaMetricKind.PROVIDER_REQUESTS: QuotaMetricLimit(
                    soft_warning_limit=80, hard_cutoff_limit=95
                ),
                QuotaMetricKind.REFETCH_BYTES: QuotaMetricLimit(
                    soft_warning_limit=80000, hard_cutoff_limit=95000
                ),
                QuotaMetricKind.EXPORT_BUILDS: QuotaMetricLimit(
                    soft_warning_limit=8, hard_cutoff_limit=10
                ),
                QuotaMetricKind.SEARCH_QUERIES: QuotaMetricLimit(
                    soft_warning_limit=800, hard_cutoff_limit=950
                ),
                QuotaMetricKind.ESTIMATED_COST_KRW: QuotaMetricLimit(
                    soft_warning_limit=0, hard_cutoff_limit=0
                ),
                QuotaMetricKind.LLM_TOKENS: QuotaMetricLimit(
                    soft_warning_limit=0, hard_cutoff_limit=0
                ),
            }
        )

        self.store = _ApiConnectionProvenanceStore(cluster.api_connection_factory())
        self.resolver = RefetchResolverService(
            metadata_store=self.store,
            fetcher=self.fetcher,
            default_budget=OperationBudgetConfig(),
        )
        self.search_indexer = PostgresSearchIndexer(
            connection_factory=cluster.api_connection_factory()
        )
        self.search_service = PostgresSearchService(
            api_connection_factory=cluster.api_connection_factory()
        )
        self.deletion_service = PostgresDeletionService(
            api_connection_factory=cluster.api_connection_factory(),
            worker_connection_factory=cluster.worker_connection_factory(),
        )
        self.budget_service = PostgresQuotaBudgetService(
            api_connection_factory=cluster.api_connection_factory(),
            policy_config=self.policy,
        )
        self.claim_service = PostgresClaimService(
            api_connection_factory=cluster.api_connection_factory(),
            resolver=self.resolver,
            search_indexer=self.search_indexer,
            default_provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=True,
        )
        self.export_service = PostgresExportService(
            api_connection_factory=cluster.api_connection_factory(),
            resolver=self.resolver,
        )
        self.contracts_service = PostgresContractBoundaryService(
            claims_service=self.claim_service,
            search_service=self.search_service,
            deletion_service=self.deletion_service,
        )

    def _seed_single_repo(
        self,
        cur: Any,
        *,
        ws_id: str,
        acct_id: str,
        user_id: str,
        ident_id: str,
        repo_name: str,
        provider_resource_id: str,
        tag: str,
        raw_text: str,
        work_event_title: str,
        fact_text: str,
        claim_text: str,
        extra_event_title: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        raw_bytes = raw_text.encode("utf-8")
        canon = canonicalize_source_bytes(raw_bytes)
        assert canon.document is not None
        loc = build_span_locator(canon.document.text, 0, len(canon.document.text))
        rev_hash = sha256_prefixed_text(canon.document.text)
        unit_hash = sha256_prefixed_text(canon.document.text)

        res_id = str(uuid.uuid4())
        obj_id = str(uuid.uuid4())
        rev_id = str(uuid.uuid4())
        cu_id = str(uuid.uuid4())
        ev_id = str(uuid.uuid4())
        fact_id = str(uuid.uuid4())
        contrib_id = str(uuid.uuid4())
        gen_run_id = str(uuid.uuid4())
        val_run_id = str(uuid.uuid4())
        claim_id = str(uuid.uuid4())
        atom_id = str(uuid.uuid4())
        export_id = str(uuid.uuid4())
        digest = hashlib.sha256(f"live-m5-{tag}".encode("utf-8")).hexdigest()

        self.fetcher.register_revision_bytes(rev_id, raw_bytes)

        cur.execute(
            """
            INSERT INTO public.source_resources (
                id, workspace_id, source_account_id, provider_resource_id,
                resource_type, name, canonical_url, visibility,
                processing_mode, raw_storage_mode, raw_retention_seconds,
                status, policy_version
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s,
                'GITHUB_REPOSITORY', %s, %s, 'PUBLIC',
                'PUBLIC_CLOUD', 'NONE', NULL, 'ACTIVE', 'policy-v1'
            )
            """,
            (res_id, ws_id, acct_id, provider_resource_id, repo_name, f"https://github.com/{repo_name}"),
        )
        cur.execute(
            """
            INSERT INTO public.source_objects (
                id, workspace_id, source_resource_id, object_type,
                provider_object_id, lifecycle_state
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s, 'ACTIVE')
            """,
            (obj_id, ws_id, res_id, f"pr-{tag}"),
        )
        cur.execute(
            """
            INSERT INTO public.source_object_revisions (
                id, workspace_id, source_object_id, provider_revision_id,
                content_hash, hash_policy_version, observed_at,
                is_tombstone, normalized_metadata, connector_version, schema_version
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s,
                %s, %s, %s, FALSE, '{}'::jsonb, 'gh-v1', '1.0'
            )
            """,
            (rev_id, ws_id, obj_id, f"rev-{tag}", rev_hash, CANONICALIZATION_VERSION, now),
        )
        cur.execute(
            """
            INSERT INTO public.content_units (
                id, workspace_id, source_object_revision_id, unit_type,
                anchor, text_content, content_ref, content_hash,
                canonicalization_version, retention_class, expires_at,
                ordinal, policy_state, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s,
                %s, NULL, NULL, %s,
                %s, 'NONE', NULL,
                0, 'ACTIVE', %s
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
                1, %s, 'CONFIRMED', 'USER',
                %s, %s, %s, %s
            )
            """,
            (ev_id, ws_id, obj_id, work_event_title, now - timedelta(hours=2), now, now, now),
        )
        cur.execute(
            """
            INSERT INTO public.event_memberships (
                id, workspace_id, event_id, source_object_id,
                relation_class, relation_type, origin, status, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                'MUST_LINK', 'PRIMARY_ANCHOR', 'SOURCE', 'ACTIVE', %s
            )
            """,
            (str(uuid.uuid4()), ws_id, ev_id, obj_id, now),
        )

        extra_event_id: str | None = None
        if extra_event_title:
            obj2_id = str(uuid.uuid4())
            extra_event_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.source_objects (
                    id, workspace_id, source_resource_id, object_type,
                    provider_object_id, lifecycle_state
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', %s, 'ACTIVE')
                """,
                (obj2_id, ws_id, res_id, f"pr-{tag}-2"),
            )
            cur.execute(
                """
                INSERT INTO public.work_events (
                    id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                    version, title, status, created_by,
                    occurred_start_at, occurred_end_at, created_at, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, 'PR',
                    1, %s, 'CONFIRMED', 'USER',
                    %s, %s, %s, %s
                )
                """,
                (
                    extra_event_id,
                    ws_id,
                    obj2_id,
                    extra_event_title,
                    now - timedelta(hours=1),
                    now,
                    now,
                    now,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.event_memberships (
                    id, workspace_id, event_id, source_object_id,
                    relation_class, relation_type, origin, status, created_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    'MUST_LINK', 'PRIMARY_ANCHOR', 'SOURCE', 'ACTIVE', %s
                )
                """,
                (str(uuid.uuid4()), ws_id, extra_event_id, obj2_id, now),
            )

        cur.execute(
            """
            INSERT INTO public.facts (
                id, workspace_id, event_id, fact_type, subject_identity_id,
                predicate, object_text, status, extractor_version, version
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, 'ACTION', %s::uuid,
                'implemented_webhook', %s, 'SUPPORTED', 'v1', 1
            )
            """,
            (fact_id, ws_id, ev_id, ident_id, fact_text),
        )
        cur.execute(
            """
            INSERT INTO public.fact_support (
                id, workspace_id, fact_id, content_unit_id, support_type,
                validator, validator_version, status
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', 'RULE', 'v1', 'ACTIVE')
            """,
            (str(uuid.uuid4()), ws_id, fact_id, cu_id),
        )
        cur.execute(
            """
            INSERT INTO public.contributions (
                id, workspace_id, event_id, identity_id, role, origin, status
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'IMPLEMENTED', 'RULE', 'SOURCE_SUPPORTED')
            """,
            (contrib_id, ws_id, ev_id, ident_id),
        )
        cur.execute(
            """
            INSERT INTO public.contribution_support (
                id, workspace_id, contribution_id, fact_id, status
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'ACTIVE')
            """,
            (str(uuid.uuid4()), ws_id, contrib_id, fact_id),
        )
        cur.execute(
            """
            INSERT INTO public.generation_runs (
                id, workspace_id, event_id, run_type, model, prompt_version,
                algorithm_version, input_hash, status
            ) VALUES
                (%s::uuid, %s::uuid, %s::uuid, 'CLAIM_DRAFT_GENERATION', 'det-v1', 'p1', 'a1', %s, 'SUCCEEDED'),
                (%s::uuid, %s::uuid, %s::uuid, 'CLAIM_SUPPORT_VALIDATION', 'det-v1', 'p1', 'a1', %s, 'SUCCEEDED')
            """,
            (gen_run_id, ws_id, ev_id, f"sha256:{digest}", val_run_id, ws_id, ev_id, f"sha256:{digest}"),
        )
        cur.execute(
            """
            INSERT INTO public.claims (
                id, workspace_id, event_id, generator_run_id, version, claim_text,
                status, last_verification_run_id, approved_at, last_verified_at, created_at, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid, 1, %s,
                'APPROVED', %s::uuid, %s, %s, %s, %s
            )
            """,
            (claim_id, ws_id, ev_id, gen_run_id, claim_text, val_run_id, now, now, now, now),
        )
        cur.execute(
            """
            INSERT INTO public.claim_atoms (
                id, workspace_id, claim_id, ordinal, atom_type, atom_text, status
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, 0, 'ACTION', %s, 'SUPPORTED')
            """,
            (atom_id, ws_id, claim_id, claim_text),
        )
        cur.execute(
            """
            INSERT INTO public.claim_atom_support (
                id, workspace_id, claim_atom_id, fact_id, support_type, validation_run_id, status
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'DIRECT', %s::uuid, 'ACTIVE')
            """,
            (str(uuid.uuid4()), ws_id, atom_id, fact_id, val_run_id),
        )
        cur.execute(
            """
            INSERT INTO public.exports (
                id, workspace_id, source_resource_id, requested_by_user_id,
                format, export_policy, status, idempotency_key,
                input_hash, policy_version, manifest, artifact_payload, completed_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                'MARKDOWN', 'SOURCE_VERIFIED_ONLY', 'READY', %s,
                %s, 'v1', '{"excludes_raw_source_quotes":true}'::jsonb,
                '{"markdown":"# Verified Claim Summary"}'::jsonb, %s
            )
            """,
            (export_id, ws_id, res_id, user_id, str(uuid.uuid4()), f"sha256:{digest}", now),
        )
        cur.execute(
            """
            INSERT INTO public.export_claims (
                id, workspace_id, export_id, claim_id, claim_version,
                computed_verification_at_export, ordinal
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, 1, 'SOURCE_VERIFIED', 0)
            """,
            (str(uuid.uuid4()), ws_id, export_id, claim_id),
        )

        return {
            "source_resource_id": res_id,
            "repository_full_name": repo_name,
            "event_id": ev_id,
            "extra_event_id": extra_event_id,
            "fact_id": fact_id,
            "claim_id": claim_id,
            "export_id": export_id,
            "content_unit_id": cu_id,
        }

    def reset_and_seed(self) -> dict[str, Any]:
        self.cluster.truncate_all()
        now = datetime.now(UTC)
        user_id = str(uuid.uuid4())
        ws_id = str(uuid.uuid4())
        acct_id = str(uuid.uuid4())
        ident_id = str(uuid.uuid4())
        map_id = str(uuid.uuid4())

        access = WorkspaceAccessContext(
            user=AuthenticatedUser(
                user_id=user_id,
                auth_subject=AuthSubject(
                    issuer="https://auth.local/v1", subject=f"live-m5-{user_id}"
                ),
            ),
            membership=WorkspaceMembership(
                workspace_id=ws_id, user_id=user_id, role="OWNER"
            ),
        )
        self.access = access

        with self.cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                        VALUES (%s::uuid, %s, %s, 'm5-ops@example.test', 'Seoyeon Ops')
                        """,
                        (user_id, access.user.auth_subject.issuer, access.user.auth_subject.subject),
                    )
                    cur.execute(
                        "INSERT INTO public.workspaces (id, owner_user_id, name) VALUES (%s::uuid, %s::uuid, 'Live M5 Workspace')",
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
                            %s::uuid, %s::uuid, 'GITHUB', 'gh-acct-live-m5',
                            'vault://live-m5/token', ARRAY['public_repo']::text[], 'ACTIVE'
                        )
                        """,
                        (acct_id, ws_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.identities (
                            id, workspace_id, display_name, identity_type, canonical_user_id, status
                        ) VALUES (%s::uuid, %s::uuid, 'Seoyeon Ops', 'PERSON', %s::uuid, 'ACTIVE')
                        """,
                        (ident_id, ws_id, user_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.external_identity_mappings (
                            id, workspace_id, identity_id, provider, external_user_id,
                            username, verification_method, status, verified_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'GITHUB', 'gh-uid-501',
                            'seoyeon-ops', 'OAUTH_ACCOUNT', 'VERIFIED', %s
                        )
                        """,
                        (map_id, ws_id, ident_id, now),
                    )

                    repo_a = self._seed_single_repo(
                        cur,
                        ws_id=ws_id,
                        acct_id=acct_id,
                        user_id=user_id,
                        ident_id=ident_id,
                        repo_name="acme/ledger-engine",
                        provider_resource_id="50101",
                        tag="alpha",
                        raw_text=f"# PR #501\nWebhook retry queue hardening.\n{RAW_CANARY_REPO_A}\n",
                        work_event_title="PR #501: Ledger Webhook Retry Queue Hardening",
                        fact_text="Implemented ledger webhook idempotent retry queue with exponential backoff",
                        claim_text="Built ledger webhook retry queue eliminating duplicate settlement charges",
                        extra_event_title="PR #502: Ledger Webhook HMAC Signature Guard",
                    )
                    repo_b = self._seed_single_repo(
                        cur,
                        ws_id=ws_id,
                        acct_id=acct_id,
                        user_id=user_id,
                        ident_id=ident_id,
                        repo_name="acme/portfolio-telemetry",
                        provider_resource_id="50202",
                        tag="beta",
                        raw_text=f"# PR #601\nPortfolio webhook telemetry.\n{RAW_CANARY_REPO_B}\n",
                        work_event_title="PR #601: Portfolio Webhook Latency Telemetry",
                        fact_text="Instrumented portfolio webhook delivery latency percentile histograms",
                        claim_text="Delivered portfolio webhook telemetry dashboard with sub-second alerting",
                    )

        # Build live PG18 FTS index across both repositories
        self.search_indexer.rebuild_workspace(ws_id, access=access)
        # Initialize ALLOWED quota ledger rows
        self.budget_service.get_quota_status(access)

        self.seeded = {
            "workspace_id": ws_id,
            "origin_auth_secret": LIVE_M5_ORIGIN_SECRET,
            "repo_a": repo_a,
            "repo_b": repo_b,
            "canary_a": RAW_CANARY_REPO_A,
            "canary_b": RAW_CANARY_REPO_B,
        }
        return dict(self.seeded)

    def set_quota_mode(self, mode: str) -> dict[str, Any]:
        assert self.access is not None
        ws_id = self.access.workspace_id
        # Ensure period ledgers exist first
        self.budget_service.get_quota_status(self.access)
        with self.cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if mode == "ALLOWED":
                        cur.execute(
                            "UPDATE public.workspace_quota_ledgers SET used_value = 10 WHERE workspace_id = %s::uuid AND metric_kind = 'IMPORT_OBJECTS'",
                            (ws_id,),
                        )
                        cur.execute(
                            "UPDATE public.runtime_pause_controls SET pause_state = 'ACTIVE', reason_code = NULL WHERE workspace_id = %s::uuid",
                            (ws_id,),
                        )
                    elif mode == "WARNING_80_PERCENT":
                        cur.execute(
                            "UPDATE public.workspace_quota_ledgers SET used_value = 80 WHERE workspace_id = %s::uuid AND metric_kind = 'IMPORT_OBJECTS'",
                            (ws_id,),
                        )
                        cur.execute(
                            "UPDATE public.runtime_pause_controls SET pause_state = 'ACTIVE', reason_code = NULL WHERE workspace_id = %s::uuid",
                            (ws_id,),
                        )
                    elif mode == "CUTOFF_95_PERCENT":
                        cur.execute(
                            "UPDATE public.workspace_quota_ledgers SET used_value = 95 WHERE workspace_id = %s::uuid AND metric_kind = 'IMPORT_OBJECTS'",
                            (ws_id,),
                        )
                        cur.execute(
                            "UPDATE public.runtime_pause_controls SET pause_state = 'PAUSED_QUOTA', reason_code = 'QUOTA_CUTOFF_IMPORT_OBJECTS' WHERE workspace_id = %s::uuid",
                            (ws_id,),
                        )
                    else:
                        raise ValueError(f"Unsupported quota mode: {mode}")
        status = self.budget_service.get_quota_status(self.access)
        return status.model_dump(mode="json")

    def execute_deletion_job(
        self, deletion_job_id: str, *, fail_component: str | None = None
    ) -> dict[str, Any]:
        assert self.access is not None

        def _fault(comp: str) -> None:
            if fail_component and comp == fail_component:
                raise RuntimeError(f"SIMULATED_{fail_component}_FAILURE")

        res = self.deletion_service.execute_deletion_job(
            self.access.workspace_id,
            deletion_job_id,
            access=self.access,
            fault_injector=_fault if fail_component else None,
        )
        return res.model_dump(mode="json")


class _LiveM5AccessProvider:
    def __init__(self, state: LiveMemoryOpsBackendState) -> None:
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


def build_app(state: LiveMemoryOpsBackendState) -> Any:
    def _resolve_access(_request: Request) -> WorkspaceAccessContext:
        assert state.access is not None
        return state.access

    app = create_app(
        access_provider=_LiveM5AccessProvider(state),
        budgets_router=create_budgets_router(
            service=state.budget_service, resolve_access=_resolve_access
        ),
        deletion_router=create_deletion_router(
            service=state.deletion_service, resolve_access=_resolve_access
        ),
        claims_router=create_claims_router(
            service=state.claim_service, resolve_access=_resolve_access
        ),
        exports_router=create_exports_router(
            service=state.export_service, resolve_access=_resolve_access
        ),
        resolver_router=create_resolver_router(state.resolver),
        contracts_router=create_contracts_router(
            service=state.contracts_service, resolve_access=_resolve_access
        ),
        install_contract_handlers=True,
        install_claims_handlers=True,
        install_exports_handlers=True,
        install_deletion_handlers=True,
        install_budgets_handlers=True,
    )

    @app.post("/__control/reset")
    async def control_reset() -> dict[str, Any]:
        return state.reset_and_seed()

    @app.post("/__control/set-quota-mode")
    async def control_set_quota_mode(request: Request) -> dict[str, Any]:
        body = await request.json()
        mode = str(body.get("mode", "ALLOWED"))
        return state.set_quota_mode(mode)

    @app.post("/__control/execute-deletion-job")
    async def control_execute_deletion_job(request: Request) -> dict[str, Any]:
        body = await request.json()
        job_id = str(body["deletion_job_id"])
        fail_component = body.get("fail_component")
        return state.execute_deletion_job(
            job_id, fail_component=str(fail_component) if fail_component else None
        )

    edge_cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(LIVE_M5_ORIGIN_SECRET,),
            block_direct_run_app_hosts=True,
        ),
        cors_csrf=CorsCsrfConfig(
            allowed_web_origins=frozenset({"http://127.0.0.1", "http://localhost:3100"})
        ),
        caps=EdgeRuntimeCaps(
            rate_limit_per_window=500,
            max_concurrent_requests=32,
        ),
        allowed_api_prefixes=DEFAULT_ALLOWED_API_PREFIXES | {"/__control"},
    )
    install_edge_guard(app, edge_cfg, controller=state.edge_controller)
    return app


def main() -> int:
    port = _find_free_port()
    with EphemeralFaultMatrixPg18Cluster(
        repo_root=REPO_ROOT, prefix="cm-live-m5-wire-pg18-"
    ) as cluster:
        state = LiveMemoryOpsBackendState(cluster)
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
