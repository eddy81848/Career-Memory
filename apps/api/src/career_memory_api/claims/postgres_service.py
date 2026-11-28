"""PostgreSQL 18 Claim Pipeline, Race-Fenced Approval, ER-012 Edit, and /v1/claims Service.

Implements M4 Wave 3A (T-404, T-405, T-405A, CLAIM-04..09, ER-012, REF-07a/d):
- Stage 3A ContextBuilder + ClaimGenerator (`CLAIM_DRAFT_GENERATION`)
- Stage 3B ClaimAtomizer (`CLAIM_ATOMIZATION`, `atomizer_run_id != generator_run_id`)
- Stage 3C ClaimSupportValidator (`CLAIM_SUPPORT_VALIDATION`, pairwise distinct run IDs)
- Read-only VerificationCalculator + deterministic `support_graph_epoch`
- ER-012 / CLAIM-08 non-overwrite versioned edit (preserving prior APPROVED rows as history)
- CLAIM-09 transactional `FOR UPDATE` approval race fence with `before_lock_hook` & `before_commit_hook`
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceMembershipDeniedError
from career_memory_api.claims.atomizer import (
    ClaimAtomDraftItem,
    ClaimAtomizationRequest,
    ClaimAtomizationResult,
    ClaimAtomizer,
    DeterministicClaimAtomizerAdapter,
)
from career_memory_api.claims.context_builder import (
    ClaimContextBuilder,
    ClaimContextBundle,
    EligibleFactContextItem,
)
from career_memory_api.claims.generator import (
    ClaimDraftGenerationRequest,
    ClaimDraftGenerationResult,
    ClaimGenerator,
)
from career_memory_api.claims.models import (
    DEFAULT_CONTEXT_MAX_BYTES,
    DEFAULT_CONTEXT_MAX_TOKENS,
    ApproveClaimActionRequest,
    ClaimAtomDetailItem,
    ClaimAtomProvenanceItem,
    ClaimAtomSupportProvenanceItem,
    ClaimCursorPayload,
    ClaimDetailResponse,
    ClaimError,
    ClaimGenerationStatus,
    ClaimListResponse,
    ClaimPageInfo,
    ClaimSummaryItem,
    ClaimSupportFactRef,
    ClaimSupportFactSupportRef,
    ClaimSupportsResponse,
    GenerateEventClaimsRequest,
    GenerateEventClaimsResponse,
    RejectClaimActionRequest,
    RevalidateClaimRequest,
    compute_claims_filter_hash,
    decode_and_verify_claim_cursor,
    encode_claim_cursor,
    utc_now,
)
from career_memory_api.claims.validator import (
    ClaimSupportValidationRequest,
    ClaimSupportValidationResult,
    ClaimSupportValidator,
    ContributionEvidenceSnapshot,
    DeterministicClaimValidatorAdapter,
    FactEvidenceSnapshot,
)
from career_memory_api.claims.verification import (
    ClaimVerificationEvaluation,
    VerificationCalculator,
)
from career_memory_api.contracts_api.models import (
    ClaimApproveRequest,
    ClaimBoundaryResponse,
    ClaimDraftCreateRequest,
    ClaimEditRequest,
)
from career_memory_api.facts.models import MetricDetailsModel, extract_semantic_attributes
from career_memory_api.identities.postgres_service import evaluate_identity_attribution
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.llm_gate import (
    BoundedStageRunner,
    HostedLlmApprovalConfig,
    LlmPolicyGate,
    LlmProviderMode,
    StageBudgetPolicy,
    StageExecutionAdapterPort,
    SyntheticFixtureStageAdapter,
    assert_no_raw_text_or_secret_keys,
)
from career_memory_api.resolver.models import (
    GateOperationKind,
    RefetchRequestInput,
    ResolutionStatus,
    format_utc_timestamp,
)
from career_memory_api.resolver.service import (
    RefetchResolverService,
    verify_required_supports_for_gate,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    ContentUnitType,
    ContributionRole,
    ContributionStatus,
    FactStatus,
    FactType,
    GenerationRunStatus,
    GenerationRunType,
    LinkStatus,
    ResourceVisibility,
    ReviewActionType,
    ReviewTargetType,
    SupportType,
    SupportValidator,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
BeforeCommitHook = Callable[..., None]
BeforeLockHook = Callable[..., None]

_CLAIM_SELECT_COLUMNS = """
    c.id::text,
    c.workspace_id::text,
    c.event_id::text,
    c.generator_run_id::text,
    c.version,
    c.claim_text,
    c.status,
    c.supersedes_claim_id::text,
    c.approved_at,
    c.last_verified_at,
    c.last_verification_run_id::text,
    c.created_at,
    c.updated_at
"""


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _compute_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _invoke_hook(hook: Callable[..., None] | None, *, action: str, arg2: Any = None) -> None:
    if hook is None:
        return
    sig = inspect.signature(hook)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(params) >= 2:
        hook(action, arg2)
    elif len(params) == 1:
        hook(action)
    else:
        hook()


def _default_stage_budget() -> StageBudgetPolicy:
    return StageBudgetPolicy(
        max_input_bytes=128 * 1024,
        max_calls=3,
        calls_used=0,
        max_repair_attempts=0,
        max_pre_spend_retries=0,
        overage_credit_disabled=True,
        quota_remaining_known_and_sufficient=True,
    )


def _extract_metric_details(
    normalized_value: dict[str, Any] | None,
    fact_metadata: dict[str, Any] | None,
) -> MetricDetailsModel | None:
    for container in (normalized_value, fact_metadata):
        if not isinstance(container, dict):
            continue
        candidate = container.get("metric_details")
        if isinstance(candidate, dict):
            try:
                return MetricDetailsModel.model_validate(candidate)
            except Exception:  # noqa: BLE001
                continue
        if all(
            k in container
            for k in (
                "value",
                "unit",
                "period",
                "source_basis",
                "environment_scope",
                "measurement_scope",
            )
        ):
            try:
                return MetricDetailsModel.model_validate(
                    {
                        k: container[k]
                        for k in (
                            "value",
                            "unit",
                            "period",
                            "source_basis",
                            "environment_scope",
                            "measurement_scope",
                        )
                    }
                )
            except Exception:  # noqa: BLE001
                continue
    return None


class PostgresClaimService:
    """PostgreSQL 18 transactional service for Stage 3A/3B/3C Claim pipeline and /v1/claims* API."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        resolver: RefetchResolverService | None = None,
        context_builder: ClaimContextBuilder | None = None,
        generator: ClaimGenerator | None = None,
        atomizer: ClaimAtomizer | None = None,
        validator: ClaimSupportValidator | None = None,
        verification_calculator: VerificationCalculator | None = None,
        default_adapter: StageExecutionAdapterPort | None = None,
        default_provider_mode: LlmProviderMode = LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
        allow_synthetic_fixture_mode: bool = False,
        before_lock_hook: BeforeLockHook | None = None,
        before_commit_hook: BeforeCommitHook | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._resolver = resolver
        default_runner = BoundedStageRunner(
            policy_gate=LlmPolicyGate(approval_config=HostedLlmApprovalConfig())
        )
        self._context_builder = context_builder or ClaimContextBuilder()
        self._generator = generator or ClaimGenerator(stage_runner=default_runner)
        self._atomizer = atomizer or ClaimAtomizer(stage_runner=default_runner)
        self._validator = validator or ClaimSupportValidator(stage_runner=default_runner)
        self._verification = verification_calculator or VerificationCalculator()
        self._default_adapter = default_adapter or SyntheticFixtureStageAdapter(fixtures_by_stage={})
        self._default_provider_mode = default_provider_mode
        self._allow_synthetic_fixture_mode = allow_synthetic_fixture_mode
        self._before_lock_hook = before_lock_hook
        self._before_commit_hook = before_commit_hook
        self._now_fn = now_fn or utc_now

    # =========================================================================
    # 1. Read Endpoints: GET /v1/claims, GET /v1/claims/{id}, GET /v1/claims/{id}/supports
    # =========================================================================
    def list_claims(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str | None = None,
        status: ClaimStatus | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ClaimListResponse:
        if limit < 1 or limit > 100:
            raise ClaimError(
                status_code=422,
                code="INVALID_PAGINATION_LIMIT",
                message="Query parameter 'limit' must be between 1 and 100.",
            )
        parsed_event_id: str | None = None
        if event_id is not None:
            parsed_event_id = _parse_uuid_or_none(event_id)
            if parsed_event_id is None:
                raise ClaimError(
                    status_code=422,
                    code="INVALID_EVENT_ID",
                    message="Query parameter 'event_id' must be a valid UUID.",
                )

        filter_hash = compute_claims_filter_hash(
            event_id=parsed_event_id,
            status=status,
            include_superseded=include_superseded,
        )
        decoded_cursor: ClaimCursorPayload | None = None
        if cursor is not None:
            decoded_cursor = decode_and_verify_claim_cursor(
                cursor,
                expected_workspace_id=access.workspace_id,
                expected_filter_hash=filter_hash,
            )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                where_clauses = ["c.workspace_id = %s::uuid"]
                params: list[Any] = [access.workspace_id]
                if parsed_event_id is not None:
                    where_clauses.append("c.event_id = %s::uuid")
                    params.append(parsed_event_id)
                if status is not None:
                    where_clauses.append("c.status = %s")
                    params.append(status.value)
                elif not include_superseded:
                    where_clauses.append("c.status <> 'SUPERSEDED'")
                if decoded_cursor is not None:
                    where_clauses.append("(c.created_at, c.id) < (%s, %s::uuid)")
                    params.extend([decoded_cursor.sort_ts, decoded_cursor.last_claim_id])

                params.append(limit + 1)
                cur.execute(
                    f"""
                    SELECT {_CLAIM_SELECT_COLUMNS}
                    FROM public.claims c
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY c.created_at DESC, c.id DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
                has_more = len(rows) > limit
                page_rows = rows[:limit]
                items = [
                    self._build_claim_summary(cur, workspace_id=access.workspace_id, row=row)
                    for row in page_rows
                ]
                next_cursor: str | None = None
                if has_more and items:
                    last = items[-1]
                    next_cursor = encode_claim_cursor(
                        ClaimCursorPayload(
                            workspace_id=access.workspace_id,
                            filter_hash=filter_hash,
                            sort_ts=last.created_at,
                            last_claim_id=last.claim_id,
                        )
                    )
                return ClaimListResponse(
                    items=items,
                    page=ClaimPageInfo(next_cursor=next_cursor, has_more=has_more),
                )

    def get_claim_detail(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
    ) -> ClaimDetailResponse:
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                row = self._select_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                return self._build_claim_detail(cur, workspace_id=access.workspace_id, row=row)

    def get_claim_supports(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
    ) -> ClaimSupportsResponse:
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                row = self._select_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                evaluation = self._verification.evaluate_claim(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )

                cur.execute(
                    """
                    SELECT id::text, ordinal, atom_type, atom_text, status
                    FROM public.claim_atoms
                    WHERE workspace_id = %s::uuid
                      AND claim_id = %s::uuid
                    ORDER BY ordinal ASC, id ASC
                    """,
                    (access.workspace_id, claim_uuid),
                )
                atom_rows = cur.fetchall()
                atom_ids = [str(r[0]) for r in atom_rows]

                cas_by_atom: dict[str, list[ClaimAtomSupportProvenanceItem]] = {
                    aid: [] for aid in atom_ids
                }
                if atom_ids:
                    cur.execute(
                        """
                        SELECT
                            cas.id::text,
                            cas.claim_atom_id::text,
                            cas.support_type,
                            cas.status,
                            cas.validation_run_id::text,
                            f.id::text,
                            f.version,
                            f.fact_type,
                            f.predicate,
                            f.object_text,
                            f.status,
                            f.normalized_value
                        FROM public.claim_atom_support cas
                        INNER JOIN public.facts f
                            ON f.workspace_id = cas.workspace_id
                           AND f.id = cas.fact_id
                        WHERE cas.workspace_id = %s::uuid
                          AND cas.claim_atom_id = ANY(%s::uuid[])
                        ORDER BY cas.created_at ASC, cas.id ASC
                        """,
                        (access.workspace_id, atom_ids),
                    )
                    cas_rows = cur.fetchall()
                    fact_ids = sorted({str(r[5]) for r in cas_rows})
                    fs_by_fact: dict[str, list[ClaimSupportFactSupportRef]] = {}
                    if fact_ids:
                        cur.execute(
                            """
                            SELECT
                                fs.id::text,
                                fs.fact_id::text,
                                fs.support_type,
                                fs.status,
                                fs.validator,
                                fs.validator_version,
                                fs.support_metadata,
                                cu.id::text,
                                cu.unit_type,
                                cu.anchor,
                                rev.id::text,
                                rev.content_hash,
                                rev.observed_at,
                                rev.is_tombstone
                            FROM public.fact_support fs
                            INNER JOIN public.content_units cu
                                ON cu.workspace_id = fs.workspace_id
                               AND cu.id = fs.content_unit_id
                            INNER JOIN public.source_object_revisions rev
                                ON rev.workspace_id = cu.workspace_id
                               AND rev.id = cu.source_object_revision_id
                            WHERE fs.workspace_id = %s::uuid
                              AND fs.fact_id = ANY(%s::uuid[])
                            ORDER BY fs.created_at ASC, fs.id ASC
                            """,
                            (access.workspace_id, fact_ids),
                        )
                        for frow in cur.fetchall():
                            sm = dict(frow[6] or {})
                            anchor = dict(frow[9] or {})
                            assert_no_forbidden_raw_keys(sm, field_name="support_metadata")
                            assert_no_forbidden_raw_keys(anchor, field_name="content_unit.anchor")
                            locator = (
                                dict(sm["verified_locator"])
                                if isinstance(sm.get("verified_locator"), dict)
                                else anchor
                            )
                            last_checked = sm.get("checked_at") or anchor.get("last_verified_at")
                            last_res_status = sm.get("resolution_status") or anchor.get(
                                "last_resolution_status"
                            )
                            fs_by_fact.setdefault(str(frow[1]), []).append(
                                ClaimSupportFactSupportRef(
                                    fact_support_id=str(frow[0]),
                                    support_type=SupportType(str(frow[2])),
                                    status=LinkStatus(str(frow[3])),
                                    validator=SupportValidator(str(frow[4])),
                                    validator_version=str(frow[5]),
                                    content_unit_id=str(frow[7]),
                                    unit_type=ContentUnitType(str(frow[8])),
                                    locator=locator,
                                    source_object_revision_id=str(frow[10]),
                                    revision_content_hash=str(frow[11]),
                                    revision_observed_at=frow[12],
                                    revision_is_tombstone=bool(frow[13]),
                                    last_checked_at=(
                                        str(last_checked) if last_checked is not None else None
                                    ),
                                    last_resolution_status=(
                                        str(last_res_status)
                                        if last_res_status is not None
                                        else None
                                    ),
                                )
                            )

                    for crow in cas_rows:
                        fid = str(crow[5])
                        nv = dict(crow[11]) if crow[11] is not None else None
                        if nv is not None:
                            assert_no_forbidden_raw_keys(nv, field_name="fact.normalized_value")
                        cas_by_atom[str(crow[1])].append(
                            ClaimAtomSupportProvenanceItem(
                                claim_atom_support_id=str(crow[0]),
                                support_type=SupportType(str(crow[2])),
                                status=LinkStatus(str(crow[3])),
                                validation_run_id=(
                                    str(crow[4]) if crow[4] is not None else None
                                ),
                                fact=ClaimSupportFactRef(
                                    fact_id=fid,
                                    version=int(crow[6]),
                                    fact_type=FactType(str(crow[7])),
                                    predicate=str(crow[8]),
                                    object_text=str(crow[9]),
                                    status=FactStatus(str(crow[10])),
                                    normalized_value=nv,
                                ),
                                fact_supports=fs_by_fact.get(fid, []),
                            )
                        )

                atoms_out = [
                    ClaimAtomProvenanceItem(
                        atom_id=str(ar[0]),
                        ordinal=int(ar[1]),
                        atom_type=ClaimAtomType(str(ar[2])),
                        atom_text=str(ar[3]),
                        status=ClaimAtomStatus(str(ar[4])),
                        supports=cas_by_atom.get(str(ar[0]), []),
                    )
                    for ar in atom_rows
                ]
                return ClaimSupportsResponse(
                    claim_id=claim_uuid,
                    version=int(row[4]),
                    event_id=str(row[2]),
                    status=ClaimStatus(str(row[6])),
                    computed_verification=evaluation.computed_verification,
                    support_graph_epoch=evaluation.support_graph_epoch,
                    atoms=atoms_out,
                )

    # =========================================================================
    # 2. Stage 3A / 3B / 3C Programmatic & HTTP Generation Pipeline
    # =========================================================================
    def build_claim_context(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        input_fact_ids: Sequence[str] | None = None,
        max_bytes: int = DEFAULT_CONTEXT_MAX_BYTES,
        max_tokens: int = DEFAULT_CONTEXT_MAX_TOKENS,
    ) -> ClaimContextBundle:
        evt_uuid = _parse_uuid_or_none(event_id)
        if evt_uuid is None:
            raise WorkspaceResourceNotFoundError()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                return self._build_context_in_tx(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=evt_uuid,
                    input_fact_ids=input_fact_ids,
                    max_bytes=max_bytes,
                    max_tokens=max_tokens,
                )

    def generate_claim_draft(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        input_fact_ids: Sequence[str] | None = None,
        budget: StageBudgetPolicy | None = None,
        adapter: StageExecutionAdapterPort | None = None,
        provider_mode: LlmProviderMode | None = None,
        allow_synthetic_fixture_mode: bool | None = None,
        generator_run_id: str | None = None,
        idempotency_key: str | None = None,
        max_bytes: int = DEFAULT_CONTEXT_MAX_BYTES,
        max_tokens: int = DEFAULT_CONTEXT_MAX_TOKENS,
    ) -> tuple[ClaimDraftGenerationResult, str | None]:
        bundle = self.build_claim_context(
            access,
            event_id=event_id,
            input_fact_ids=input_fact_ids,
            max_bytes=max_bytes,
            max_tokens=max_tokens,
        )
        res = self._generator.generate_draft(
            ClaimDraftGenerationRequest(
                access=access,
                context_bundle=bundle,
                budget=budget or _default_stage_budget(),
                adapter=adapter or self._default_adapter,
                provider_mode=provider_mode or self._default_provider_mode,
                allow_synthetic_fixture_mode=(
                    self._allow_synthetic_fixture_mode
                    if allow_synthetic_fixture_mode is None
                    else allow_synthetic_fixture_mode
                ),
                generator_run_id=generator_run_id,
            )
        )
        ts = self._now_fn()
        created_claim_id: str | None = None
        run_meta = dict(res.run_metadata)
        if idempotency_key is not None:
            run_meta["idempotency_key"] = idempotency_key

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=bundle.event_id
                )
                self._insert_generation_run(
                    cur,
                    run_id=res.generator_run_id,
                    workspace_id=access.workspace_id,
                    event_id=bundle.event_id,
                    run_type=GenerationRunType.CLAIM_DRAFT_GENERATION,
                    input_artifact_versions=res.input_artifact_versions,
                    model=res.model,
                    prompt_version=res.prompt_version,
                    algorithm_version=res.generator_version,
                    input_hash=res.input_hash,
                    status=res.run_status,
                    error_code=res.error_code,
                    run_metadata=run_meta,
                    now=ts,
                )
                if res.generation_status == "SUCCEEDED" and res.draft is not None:
                    created_claim_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO public.claims (
                            id, workspace_id, event_id, generator_run_id,
                            version, claim_text, status, created_at, updated_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            1, %s, 'DRAFT', %s, %s
                        )
                        """,
                        (
                            created_claim_id,
                            access.workspace_id,
                            bundle.event_id,
                            res.generator_run_id,
                            res.draft.claim_text,
                            ts,
                            ts,
                        ),
                    )
                    self._insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=access.user_id,
                        action="CLAIM_DRAFT_GENERATED",
                        target_type="CLAIM",
                        target_id=created_claim_id,
                        redacted_metadata={
                            "event_id": bundle.event_id,
                            "generator_run_id": res.generator_run_id,
                            "input_fact_ids": list(res.draft.input_fact_ids),
                            "version": 1,
                        },
                        created_at=ts,
                    )
        return res, created_claim_id

    def atomize_claim(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        budget: StageBudgetPolicy | None = None,
        adapter: StageExecutionAdapterPort | None = None,
        provider_mode: LlmProviderMode | None = None,
        allow_synthetic_fixture_mode: bool | None = None,
        atomizer_run_id: str | None = None,
    ) -> ClaimAtomizationResult:
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                row = self._select_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                event_id = str(row[2])
                gen_run_id = str(row[3]) if row[3] is not None else f"manual-edit-{claim_uuid}-v{row[4]}"
                claim_version = int(row[4])
                claim_text = str(row[5])
                res_id, vis = self._lookup_event_resource_info(
                    cur, workspace_id=access.workspace_id, event_id=event_id
                )

        atom_res = self._atomizer.atomize(
            ClaimAtomizationRequest(
                access=access,
                claim_id=claim_uuid,
                claim_version=claim_version,
                claim_text=claim_text,
                generator_run_id=gen_run_id,
                atomizer_run_id=atomizer_run_id,
                source_resource_id=res_id,
                visibility=vis,
                budget=budget or _default_stage_budget(),
                adapter=adapter or DeterministicClaimAtomizerAdapter(),
                provider_mode=provider_mode or LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=(
                    True if allow_synthetic_fixture_mode is None else allow_synthetic_fixture_mode
                ),
            )
        )
        ts = self._now_fn()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._insert_generation_run(
                    cur,
                    run_id=atom_res.atomizer_run_id,
                    workspace_id=access.workspace_id,
                    event_id=event_id,
                    run_type=GenerationRunType.CLAIM_ATOMIZATION,
                    input_artifact_versions=atom_res.input_artifact_versions,
                    model=atom_res.model,
                    prompt_version=atom_res.prompt_version,
                    algorithm_version=atom_res.atomizer_version,
                    input_hash=atom_res.input_hash,
                    status=atom_res.status,
                    error_code=atom_res.error_code,
                    run_metadata=atom_res.run_metadata,
                    now=ts,
                )
                if atom_res.status is GenerationRunStatus.SUCCEEDED:
                    # Replace atoms on unapproved claim version (fresh UNSUPPORTED rows)
                    cur.execute(
                        """
                        DELETE FROM public.claim_atoms
                        WHERE workspace_id = %s::uuid
                          AND claim_id = %s::uuid
                        """,
                        (access.workspace_id, claim_uuid),
                    )
                    for atom in atom_res.atoms:
                        cur.execute(
                            """
                            INSERT INTO public.claim_atoms (
                                id, workspace_id, claim_id, ordinal, atom_type,
                                atom_text, status, created_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s, %s,
                                %s, 'UNSUPPORTED', %s
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                access.workspace_id,
                                claim_uuid,
                                atom.ordinal,
                                atom.atom_type.value,
                                atom.atom_text,
                                ts,
                            ),
                        )
        return atom_res

    def validate_claim_support(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        atomizer_run_id: str,
        budget: StageBudgetPolicy | None = None,
        adapter: StageExecutionAdapterPort | None = None,
        provider_mode: LlmProviderMode | None = None,
        allow_synthetic_fixture_mode: bool | None = None,
        validation_run_id: str | None = None,
    ) -> ClaimDetailResponse:
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()

        # 1. Load claim, atoms, event facts, contributions, and identity eligibilities
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                row = self._select_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                event_id = str(row[2])
                gen_run_id = str(row[3]) if row[3] is not None else f"manual-edit-{claim_uuid}-v{row[4]}"
                claim_version = int(row[4])
                res_id, vis = self._lookup_event_resource_info(
                    cur, workspace_id=access.workspace_id, event_id=event_id
                )
                cur.execute(
                    """
                    SELECT id::text, ordinal, atom_type, atom_text
                    FROM public.claim_atoms
                    WHERE workspace_id = %s::uuid
                      AND claim_id = %s::uuid
                    ORDER BY ordinal ASC, id ASC
                    """,
                    (access.workspace_id, claim_uuid),
                )
                atom_db_rows = cur.fetchall()
                atom_drafts = tuple(
                    ClaimAtomDraftItem(
                        ordinal=int(r[1]),
                        atom_type=ClaimAtomType(str(r[2])),
                        atom_text=str(r[3]),
                    )
                    for r in atom_db_rows
                )
                atom_id_by_ordinal = {int(r[1]): str(r[0]) for r in atom_db_rows}

                fact_snapshots, contributions, identity_eligibilities = (
                    self._load_event_evidence_snapshots(
                        cur, workspace_id=access.workspace_id, event_id=event_id
                    )
                )

        # 2. Live Plan C refetch of all active DIRECT content_units on SUPPORTED facts
        candidate_unit_ids = sorted(
            {
                cu_id
                for f in fact_snapshots.values()
                if f.status is FactStatus.SUPPORTED
                for fs_id in f.active_direct_support_ids
                for (cu_id, _, u_policy, u_tomb) in [f.support_units[fs_id]]
                if u_policy == "ACTIVE" and not u_tomb
            }
        )
        matched_units: set[str] = set()
        if candidate_unit_ids and self._resolver is not None:
            op_id = f"stage3c-refetch-{claim_uuid}-{uuid.uuid4()}"
            with self._resolver.open_operation_session(access, operation_id=op_id) as session:
                for cu_id in candidate_unit_ids:
                    with self._resolver.resolve_unit(
                        session,
                        RefetchRequestInput(
                            access=access,
                            content_unit_id=cu_id,
                            operation_id=op_id,
                            include_context=False,
                        ),
                    ) as resolved:
                        if resolved.metadata.status is ResolutionStatus.MATCHED:
                            matched_units.add(cu_id)

        effective_adapter = adapter or DeterministicClaimValidatorAdapter(
            fact_snapshots=fact_snapshots,
            contributions=contributions,
        )
        val_res: ClaimSupportValidationResult = self._validator.validate(
            ClaimSupportValidationRequest(
                access=access,
                event_id=event_id,
                claim_id=claim_uuid,
                claim_version=claim_version,
                generator_run_id=gen_run_id,
                atomizer_run_id=atomizer_run_id,
                validation_run_id=validation_run_id,
                atoms=atom_drafts,
                fact_snapshots=fact_snapshots,
                contributions=contributions,
                identity_eligibilities=identity_eligibilities,
                matched_content_unit_ids=frozenset(matched_units),
                source_resource_id=res_id,
                visibility=vis,
                budget=budget or _default_stage_budget(),
                adapter=effective_adapter,
                provider_mode=provider_mode or LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
                allow_synthetic_fixture_mode=(
                    True if allow_synthetic_fixture_mode is None else allow_synthetic_fixture_mode
                ),
            )
        )

        # 3. Persist Stage 3C generation_runs, claim_atom_support, atom statuses, and claim status
        ts = self._now_fn()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked_claim = self._lock_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked_claim[2])
                )
                self._insert_generation_run(
                    cur,
                    run_id=val_res.validation_run_id,
                    workspace_id=access.workspace_id,
                    event_id=event_id,
                    run_type=GenerationRunType.CLAIM_SUPPORT_VALIDATION,
                    input_artifact_versions=val_res.input_artifact_versions,
                    model=val_res.model,
                    prompt_version=val_res.prompt_version,
                    algorithm_version=val_res.validator_version,
                    input_hash=val_res.input_hash,
                    status=val_res.run_status,
                    error_code=val_res.error_code,
                    run_metadata=val_res.run_metadata,
                    now=ts,
                )

                for outcome in val_res.atom_results:
                    aid = atom_id_by_ordinal.get(outcome.ordinal)
                    if aid is None:
                        continue
                    cur.execute(
                        """
                        UPDATE public.claim_atoms
                        SET status = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (outcome.status.value, access.workspace_id, aid),
                    )
                    cur.execute(
                        """
                        DELETE FROM public.claim_atom_support
                        WHERE workspace_id = %s::uuid
                          AND claim_atom_id = %s::uuid
                        """,
                        (access.workspace_id, aid),
                    )
                    seen_fact_ids: set[str] = set()
                    for binding in outcome.fact_bindings:
                        if binding.fact_id in seen_fact_ids:
                            continue
                        seen_fact_ids.add(binding.fact_id)
                        cur.execute(
                            """
                            INSERT INTO public.claim_atom_support (
                                id, workspace_id, claim_atom_id, fact_id,
                                support_type, validation_run_id, status, created_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s, %s::uuid, 'ACTIVE', %s
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                access.workspace_id,
                                aid,
                                binding.fact_id,
                                binding.support_type.value,
                                val_res.validation_run_id,
                                ts,
                            ),
                        )

                cur.execute(
                    f"""
                    UPDATE public.claims c
                    SET status = %s,
                        last_verified_at = %s,
                        last_verification_run_id = %s::uuid,
                        updated_at = %s
                    WHERE c.workspace_id = %s::uuid
                      AND c.id = %s::uuid
                    RETURNING {_CLAIM_SELECT_COLUMNS}
                    """,
                    (
                        val_res.next_claim_status.value,
                        ts,
                        val_res.validation_run_id,
                        ts,
                        access.workspace_id,
                        claim_uuid,
                    ),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None
                return self._build_claim_detail(
                    cur, workspace_id=access.workspace_id, row=updated_row
                )

    def generate_event_claims(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        request: GenerateEventClaimsRequest,
        budget: StageBudgetPolicy | None = None,
        adapter: StageExecutionAdapterPort | None = None,
        provider_mode: LlmProviderMode | None = None,
        allow_synthetic_fixture_mode: bool | None = None,
    ) -> GenerateEventClaimsResponse:
        evt_uuid = _parse_uuid_or_none(request.confirmed_event_id)
        if evt_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_fingerprint(
            {
                "action": "GENERATE_EVENT_CLAIMS",
                "confirmed_event_id": evt_uuid,
                "input_fact_ids": sorted(request.input_fact_ids)
                if request.input_fact_ids is not None
                else None,
            }
        )
        # Check idempotent replay in generation_runs
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                cur.execute(
                    """
                    SELECT id::text, run_metadata
                    FROM public.generation_runs
                    WHERE workspace_id = %s::uuid
                      AND event_id = %s::uuid
                      AND run_type = 'CLAIM_DRAFT_GENERATION'
                      AND run_metadata ->> 'idempotency_key' = %s
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (access.workspace_id, evt_uuid, idempotency_key),
                )
                prev_run = cur.fetchone()
                if prev_run is not None:
                    prev_meta = dict(prev_run[1] or {})
                    if prev_meta.get("request_fingerprint") not in (None, fingerprint):
                        raise ClaimError(
                            status_code=409,
                            code="IDEMPOTENCY_CONFLICT",
                            message="Idempotency-Key was already used with a different claim generation payload.",
                        )
                    prev_gen_status = ClaimGenerationStatus(
                        str(prev_meta.get("generation_status", ClaimGenerationStatus.DRAFT_INVALID.value))
                    )
                    cur.execute(
                        f"""
                        SELECT {_CLAIM_SELECT_COLUMNS}
                        FROM public.claims c
                        WHERE c.workspace_id = %s::uuid
                          AND c.generator_run_id = %s::uuid
                        ORDER BY c.created_at ASC
                        """,
                        (access.workspace_id, str(prev_run[0])),
                    )
                    prev_claims = [
                        self._build_claim_detail(cur, workspace_id=access.workspace_id, row=r)
                        for r in cur.fetchall()
                    ]
                    return GenerateEventClaimsResponse(
                        event_id=evt_uuid,
                        generation_status=prev_gen_status,
                        reason_code=prev_meta.get("error_code"),
                        free_alternatives=list(prev_meta.get("free_alternatives", [])),
                        claims=prev_claims,
                    )

        # Execute Stage 3A
        gen_res, created_claim_id = self.generate_claim_draft(
            access,
            event_id=evt_uuid,
            input_fact_ids=request.input_fact_ids,
            budget=budget,
            adapter=adapter,
            provider_mode=provider_mode,
            allow_synthetic_fixture_mode=allow_synthetic_fixture_mode,
            idempotency_key=idempotency_key,
        )
        # Annotate fingerprint + free_alternatives onto the run_metadata for idempotency replay
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                cur.execute(
                    """
                    UPDATE public.generation_runs
                    SET run_metadata = run_metadata || %s::jsonb
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (
                        json.dumps(
                            {
                                "request_fingerprint": fingerprint,
                                "free_alternatives": list(gen_res.free_alternatives),
                            }
                        ),
                        access.workspace_id,
                        gen_res.generator_run_id,
                    ),
                )

        if gen_res.generation_status != "SUCCEEDED" or created_claim_id is None:
            return GenerateEventClaimsResponse(
                event_id=evt_uuid,
                generation_status=ClaimGenerationStatus(gen_res.generation_status),
                reason_code=gen_res.error_code,
                free_alternatives=list(gen_res.free_alternatives),
                claims=[],
            )

        # Execute Stage 3B + Stage 3C automatically with distinct run IDs
        atom_res = self.atomize_claim(
            access,
            claim_id=created_claim_id,
            budget=budget,
            adapter=adapter if adapter is not None else None,
            provider_mode=provider_mode or LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=(
                True if allow_synthetic_fixture_mode is None else allow_synthetic_fixture_mode
            ),
        )
        if atom_res.status is not GenerationRunStatus.SUCCEEDED:
            detail = self.get_claim_detail(access, claim_id=created_claim_id)
            return GenerateEventClaimsResponse(
                event_id=evt_uuid,
                generation_status=ClaimGenerationStatus.DRAFT_INVALID,
                reason_code=atom_res.error_code,
                free_alternatives=[],
                claims=[detail],
            )

        detail = self.validate_claim_support(
            access,
            claim_id=created_claim_id,
            atomizer_run_id=atom_res.atomizer_run_id,
            budget=budget,
            adapter=adapter if adapter is not None else None,
            provider_mode=provider_mode or LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=(
                True if allow_synthetic_fixture_mode is None else allow_synthetic_fixture_mode
            ),
        )
        final_status = (
            ClaimGenerationStatus.SUCCEEDED
            if detail.status is ClaimStatus.SUPPORT_VALIDATED
            else ClaimGenerationStatus.DRAFT_INVALID
        )
        return GenerateEventClaimsResponse(
            event_id=evt_uuid,
            generation_status=final_status,
            reason_code=None if final_status is ClaimGenerationStatus.SUCCEEDED else "UNSUPPORTED_ATOMS_PRESENT",
            free_alternatives=[],
            claims=[detail],
        )

    # =========================================================================
    # 3. M1 ContractBoundaryPort Methods (POST /v1/claims/drafts, PATCH /v1/claims/{id}, POST /v1/claims/{id}/approve)
    # =========================================================================
    def create_claim_draft(
        self,
        access: WorkspaceAccessContext,
        request: ClaimDraftCreateRequest,
    ) -> ClaimBoundaryResponse:
        evt_uuid = _parse_uuid_or_none(request.confirmed_event_id)
        if evt_uuid is None:
            raise WorkspaceResourceNotFoundError()
        ts = self._now_fn()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                cur.execute(
                    """
                    SELECT status
                    FROM public.work_events
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (access.workspace_id, evt_uuid),
                )
                ev_row = cur.fetchone()
                if ev_row is None:
                    raise WorkspaceResourceNotFoundError()
                if WorkEventStatus(str(ev_row[0])) is not WorkEventStatus.CONFIRMED:
                    raise ClaimError(
                        status_code=409,
                        code="EVENT_NOT_CONFIRMED",
                        message="Target WorkEvent must be CONFIRMED to create a claim draft.",
                    )
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=evt_uuid
                )
                new_claim_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, version, claim_text, status,
                        created_at, updated_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 1, %s, 'DRAFT', %s, %s
                    )
                    """,
                    (
                        new_claim_id,
                        access.workspace_id,
                        evt_uuid,
                        request.claim_text.strip(),
                        ts,
                        ts,
                    ),
                )
                return ClaimBoundaryResponse(
                    claim_id=new_claim_id,
                    workspace_id=access.workspace_id,
                    event_id=evt_uuid,
                    version=1,
                    claim_text=request.claim_text.strip(),
                    status=ClaimStatus.DRAFT,
                    computed_verification=ClaimVerification.UNVERIFIED,
                )

    def edit_claim(
        self,
        access: WorkspaceAccessContext,
        claim_id: str,
        request: ClaimEditRequest,
    ) -> ClaimBoundaryResponse:
        """ER-012 / CLAIM-08 non-overwrite versioned Claim edit.

        - If edited row was DRAFT/DRAFT_INVALID/SUPPORT_VALIDATED/STALE -> marks it SUPERSEDED.
        - If edited row was APPROVED -> preserves prior APPROVED row as history!
        - Inserts NEW public.claims row with version = expected_version + 1,
          supersedes_claim_id = claim_id, status = 'DRAFT', approved_at = NULL,
          last_verified_at = NULL, last_verification_run_id = NULL, and ZERO copied
          claim_atoms / claim_atom_support rows.
        """
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()
        trimmed_text = request.claim_text.strip()
        if not trimmed_text:
            raise ClaimError(
                status_code=422,
                code="INVALID_CLAIM_EDIT_REQUEST",
                message="Edited claim_text must not be blank.",
            )
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked = self._lock_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                event_id = str(locked[2])
                current_version = int(locked[4])
                current_status = ClaimStatus(str(locked[6]))

                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=event_id
                )

                if current_version != request.expected_version:
                    raise ClaimError(
                        status_code=409,
                        code="CLAIM_VERSION_CONFLICT",
                        message=(
                            f"Expected claim version {request.expected_version}, "
                            f"but current version is {current_version}."
                        ),
                    )
                if current_status in (ClaimStatus.SUPERSEDED, ClaimStatus.REJECTED):
                    raise ClaimError(
                        status_code=409,
                        code="CLAIM_NOT_EDITABLE",
                        message=f"Claim in status {current_status.value!r} cannot be edited.",
                    )

                # ER-012: Preserve prior APPROVED row as history; mark non-approved editable rows SUPERSEDED
                if current_status is not ClaimStatus.APPROVED:
                    cur.execute(
                        """
                        UPDATE public.claims
                        SET status = 'SUPERSEDED',
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (ts, access.workspace_id, claim_uuid),
                    )

                new_claim_id = str(uuid.uuid4())
                new_version = current_version + 1
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, generator_run_id, version,
                        claim_text, status, supersedes_claim_id,
                        approved_at, last_verified_at, last_verification_run_id,
                        created_at, updated_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, NULL, %s,
                        %s, 'DRAFT', %s::uuid,
                        NULL, NULL, NULL,
                        %s, %s
                    )
                    """,
                    (
                        new_claim_id,
                        access.workspace_id,
                        event_id,
                        new_version,
                        trimmed_text,
                        claim_uuid,
                        ts,
                        ts,
                    ),
                )
                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=claim_uuid,
                    action=ReviewActionType.EDIT,
                    actor_user_id=access.user_id,
                    from_version=current_version,
                    to_version=new_version,
                    idempotency_key=str(uuid.uuid4()),
                    reason=request.edit_reason,
                    action_metadata={
                        "new_claim_id": new_claim_id,
                        "previous_status": current_status.value,
                        "prior_approved_preserved": current_status is ClaimStatus.APPROVED,
                    },
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="CLAIM_VERSION_EDITED",
                    target_type="CLAIM",
                    target_id=new_claim_id,
                    redacted_metadata={
                        "event_id": event_id,
                        "supersedes_claim_id": claim_uuid,
                        "from_version": current_version,
                        "to_version": new_version,
                        "prior_approved_preserved": current_status is ClaimStatus.APPROVED,
                    },
                    created_at=ts,
                )
                _invoke_hook(self._before_commit_hook, action="edit_claim", arg2=cur)
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=event_id
                )
                return ClaimBoundaryResponse(
                    claim_id=new_claim_id,
                    workspace_id=access.workspace_id,
                    event_id=event_id,
                    version=new_version,
                    claim_text=trimmed_text,
                    status=ClaimStatus.DRAFT,
                    computed_verification=ClaimVerification.UNVERIFIED,
                )

    def approve_claim(
        self,
        access: WorkspaceAccessContext,
        claim_id: str,
        request: ClaimApproveRequest,
    ) -> ClaimBoundaryResponse:
        """M1 POST /v1/claims/{claim_id}/approve route backed by full PG18 race-fenced approval."""
        detail = self.approve_claim_action(
            access,
            claim_id=claim_id,
            idempotency_key=str(uuid.uuid4()),
            request=ApproveClaimActionRequest(
                expected_version=request.expected_version,
                acknowledge_user_confirmed_atoms=request.acknowledge_unverified_atoms,
            ),
        )
        return ClaimBoundaryResponse(
            claim_id=detail.claim_id,
            workspace_id=detail.workspace_id,
            event_id=detail.event_id,
            version=detail.version,
            claim_text=detail.claim_text,
            status=detail.status,
            computed_verification=detail.computed_verification,
        )

    # =========================================================================
    # 4. Action Endpoints: Revalidate, Approve (CLAIM-09 Race Fence), Reject
    # =========================================================================
    def revalidate_claim_version(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: RevalidateClaimRequest,
        budget: StageBudgetPolicy | None = None,
        atomizer_adapter: StageExecutionAdapterPort | None = None,
        validator_adapter: StageExecutionAdapterPort | None = None,
        provider_mode: LlmProviderMode | None = None,
        allow_synthetic_fixture_mode: bool = True,
    ) -> ClaimDetailResponse:
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()
        fingerprint = _compute_fingerprint(
            {
                "action": "REVALIDATE_CLAIM",
                "claim_id": claim_uuid,
                "expected_version": request.expected_version,
            }
        )
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked = self._lock_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.REOPEN,
                    claim_id=claim_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    return self._build_claim_detail(
                        cur, workspace_id=access.workspace_id, row=locked
                    )
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked[2])
                )
                current_version = int(locked[4])
                current_status = ClaimStatus(str(locked[6]))
                if current_version != request.expected_version:
                    raise ClaimError(
                        status_code=409,
                        code="CLAIM_VERSION_CONFLICT",
                        message=(
                            f"Expected claim version {request.expected_version}, "
                            f"but current version is {current_version}."
                        ),
                    )
                if current_status in (
                    ClaimStatus.APPROVED,
                    ClaimStatus.SUPERSEDED,
                    ClaimStatus.REJECTED,
                ):
                    raise ClaimError(
                        status_code=409,
                        code="CLAIM_NOT_REVALIDATABLE",
                        message=f"Claim in status {current_status.value!r} cannot be revalidated.",
                    )

        atom_res = self.atomize_claim(
            access,
            claim_id=claim_uuid,
            budget=budget,
            adapter=atomizer_adapter,
            provider_mode=provider_mode or LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=allow_synthetic_fixture_mode,
        )
        if atom_res.status is not GenerationRunStatus.SUCCEEDED:
            raise ClaimError(
                status_code=422,
                code="CLAIM_ATOMIZATION_FAILED",
                message=f"Stage 3B atomization failed during revalidation: {atom_res.error_code}.",
            )

        detail = self.validate_claim_support(
            access,
            claim_id=claim_uuid,
            atomizer_run_id=atom_res.atomizer_run_id,
            budget=budget,
            adapter=validator_adapter,
            provider_mode=provider_mode or LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE,
            allow_synthetic_fixture_mode=allow_synthetic_fixture_mode,
        )
        ts = self._now_fn()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=claim_uuid,
                    action=ReviewActionType.REOPEN,
                    actor_user_id=access.user_id,
                    from_version=detail.version,
                    to_version=detail.version,
                    idempotency_key=idempotency_key,
                    reason="REVALIDATE_CLAIM_VERSION",
                    action_metadata={
                        "request_fingerprint": fingerprint,
                        "atomizer_run_id": atom_res.atomizer_run_id,
                        "validation_run_id": detail.last_verification_run_id,
                        "new_status": detail.status.value,
                        "computed_verification": detail.computed_verification.value,
                    },
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="CLAIM_REVALIDATED",
                    target_type="CLAIM",
                    target_id=claim_uuid,
                    redacted_metadata={
                        "version": detail.version,
                        "new_status": detail.status.value,
                        "computed_verification": detail.computed_verification.value,
                        "support_graph_epoch": detail.support_graph_epoch,
                    },
                    created_at=ts,
                )
        return detail

    def approve_claim_action(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: ApproveClaimActionRequest,
    ) -> ClaimDetailResponse:
        """Race-fenced Claim approval (CLAIM-09, T-405A, REF-07a/d)."""
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()
        fingerprint = _compute_fingerprint(
            {
                "acknowledge_user_confirmed_atoms": bool(request.acknowledge_user_confirmed_atoms),
                "action": "APPROVE_CLAIM",
                "claim_id": claim_uuid,
                "expected_support_graph_epoch": request.expected_support_graph_epoch,
                "expected_version": request.expected_version,
            }
        )

        # Phase 1: Pre-check, idempotency check, and support_graph_epoch snapshot
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                row = self._select_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.CONFIRM,
                    claim_id=claim_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    return self._build_claim_detail(
                        cur, workspace_id=access.workspace_id, row=row
                    )
                pre_eval = self._verification.evaluate_claim(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )

        self._assert_approval_preconditions(
            evaluation=pre_eval,
            expected_version=request.expected_version,
            expected_epoch=request.expected_support_graph_epoch,
            acknowledge_user_confirmed=request.acknowledge_user_confirmed_atoms,
        )

        # Phase 2: Current-operation live Plan C refetch (REF-07a / REF-07d)
        ts = self._now_fn()
        checked_at = ts
        matched_unit_ids: frozenset[str] = frozenset()
        if pre_eval.required_source_unit_ids:
            if self._resolver is None:
                raise ClaimError(
                    status_code=409,
                    code="REFETCH_REQUIRED_IN_CURRENT_OPERATION",
                    message=(
                        "Live Plan C refetch in the current operation is required before "
                        "approving a source-backed claim; historical verification is insufficient."
                    ),
                    details={"reason_code": "REFETCH_REQUIRED_IN_CURRENT_OPERATION"},
                )
            op_id = f"claim-approve-{claim_uuid}-{uuid.uuid4()}"
            with self._resolver.open_operation_session(access, operation_id=op_id) as session:
                gate_res = verify_required_supports_for_gate(
                    session,
                    pre_eval.required_source_unit_ids,
                    operation_kind=GateOperationKind.APPROVAL,
                    resolver=self._resolver,
                    clock=self._now_fn,
                )
            if not gate_res.allowed:
                err = gate_res.error
                raise ClaimError(
                    status_code=err.http_status if err is not None else 409,
                    code=err.code if err is not None else "SUPPORT_INCOMPLETE",
                    message=(
                        err.message
                        if err is not None
                        else "Live Plan C refetch blocked claim approval."
                    ),
                    retryable=err.retryable if err is not None else False,
                    details={
                        "reason_code": gate_res.reason_code,
                        "blocked_unit_id": gate_res.blocked_unit_id,
                    },
                )
            checked_at = gate_res.checked_at
            matched_unit_ids = frozenset(
                m.content_unit_id
                for m in gate_res.resolutions
                if m.status is ResolutionStatus.MATCHED
            )

        # Phase 3: Injectable pre-lock concurrency hook for CLAIM-09 threading.Barrier race tests
        _invoke_hook(self._before_lock_hook, action="approve_claim_before_lock", arg2=claim_uuid)

        # Phase 4: Single workspace_scoped_transaction with FOR UPDATE locks & epoch re-verification
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                # Lock claim + entire support graph FOR UPDATE in deterministic order
                locked_claim = self._lock_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                event_id = str(locked_claim[2])
                self._lock_claim_support_graph(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid, event_id=event_id
                )

                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.CONFIRM,
                    claim_id=claim_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    return self._build_claim_detail(
                        cur, workspace_id=access.workspace_id, row=locked_claim
                    )

                live_eval = self._verification.evaluate_claim(
                    cur,
                    workspace_id=access.workspace_id,
                    claim_id=claim_uuid,
                    current_op_matched_unit_ids=matched_unit_ids,
                )

                if live_eval.claim_version != request.expected_version:
                    raise ClaimError(
                        status_code=409,
                        code="CLAIM_VERSION_CONFLICT",
                        message=(
                            f"Expected claim version {request.expected_version}, "
                            f"but current version is {live_eval.claim_version}."
                        ),
                    )

                # CLAIM-09 epoch drift check (concurrent invalidation between pre-check/refetch and FOR UPDATE)
                if live_eval.support_graph_epoch != pre_eval.support_graph_epoch or (
                    request.expected_support_graph_epoch is not None
                    and live_eval.support_graph_epoch != request.expected_support_graph_epoch
                ):
                    raise ClaimError(
                        status_code=409,
                        code="SUPPORT_INCOMPLETE",
                        message=(
                            "Claim support graph changed concurrently before approval commit; "
                            "approval rejected fail-closed."
                        ),
                        details={
                            "reason_code": "CLAIM_NOT_APPROVABLE_EPOCH_DRIFT",
                            "expected_support_graph_epoch": pre_eval.support_graph_epoch,
                            "observed_support_graph_epoch": live_eval.support_graph_epoch,
                            "current_claim_status": live_eval.claim_status.value,
                        },
                    )

                self._assert_approval_preconditions(
                    evaluation=live_eval,
                    expected_version=request.expected_version,
                    expected_epoch=request.expected_support_graph_epoch,
                    acknowledge_user_confirmed=request.acknowledge_user_confirmed_atoms,
                )

                approve_ts = self._now_fn()
                cur.execute(
                    f"""
                    UPDATE public.claims c
                    SET status = 'APPROVED',
                        approved_at = %s,
                        last_verified_at = %s,
                        updated_at = %s
                    WHERE c.workspace_id = %s::uuid
                      AND c.id = %s::uuid
                    RETURNING {_CLAIM_SELECT_COLUMNS}
                    """,
                    (approve_ts, checked_at, approve_ts, access.workspace_id, claim_uuid),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None

                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=claim_uuid,
                    action=ReviewActionType.CONFIRM,
                    actor_user_id=access.user_id,
                    from_version=live_eval.claim_version,
                    to_version=live_eval.claim_version,
                    idempotency_key=idempotency_key,
                    reason=None,
                    action_metadata={
                        "request_fingerprint": fingerprint,
                        "support_graph_epoch": live_eval.support_graph_epoch,
                        "computed_verification": live_eval.computed_verification.value,
                        "checked_at": format_utc_timestamp(checked_at),
                    },
                    created_at=approve_ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="CLAIM_APPROVED",
                    target_type="CLAIM",
                    target_id=claim_uuid,
                    redacted_metadata={
                        "event_id": event_id,
                        "version": live_eval.claim_version,
                        "computed_verification": live_eval.computed_verification.value,
                        "support_graph_epoch": live_eval.support_graph_epoch,
                        "checked_at": format_utc_timestamp(checked_at),
                    },
                    created_at=approve_ts,
                )
                self._insert_outbox_event(
                    cur,
                    workspace_id=access.workspace_id,
                    event_type="CLAIM_APPROVED",
                    aggregate_type="CLAIM",
                    aggregate_id=claim_uuid,
                    dedupe_key=f"claim_approved:{access.workspace_id}:{claim_uuid}:{idempotency_key}",
                    payload={
                        "claim_id": claim_uuid,
                        "event_id": event_id,
                        "version": live_eval.claim_version,
                        "computed_verification": live_eval.computed_verification.value,
                        "support_graph_epoch": live_eval.support_graph_epoch,
                    },
                    created_at=approve_ts,
                )

                _invoke_hook(self._before_commit_hook, action="approve_claim", arg2=cur)
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=event_id
                )

                return self._build_claim_detail(
                    cur, workspace_id=access.workspace_id, row=updated_row
                )

    def reject_claim_action(
        self,
        access: WorkspaceAccessContext,
        *,
        claim_id: str,
        idempotency_key: str,
        request: RejectClaimActionRequest,
    ) -> ClaimDetailResponse:
        claim_uuid = _parse_uuid_or_none(claim_id)
        if claim_uuid is None:
            raise WorkspaceResourceNotFoundError()
        fingerprint = _compute_fingerprint(
            {
                "action": "REJECT_CLAIM",
                "claim_id": claim_uuid,
                "expected_version": request.expected_version,
                "reason": request.reason,
            }
        )
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked = self._lock_claim_row(
                    cur, workspace_id=access.workspace_id, claim_id=claim_uuid
                )
                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.REJECT,
                    claim_id=claim_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    return self._build_claim_detail(
                        cur, workspace_id=access.workspace_id, row=locked
                    )
                event_id = str(locked[2])
                current_version = int(locked[4])
                current_status = ClaimStatus(str(locked[6]))

                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=event_id
                )
                if current_version != request.expected_version:
                    raise ClaimError(
                        status_code=409,
                        code="CLAIM_VERSION_CONFLICT",
                        message=(
                            f"Expected claim version {request.expected_version}, "
                            f"but current version is {current_version}."
                        ),
                    )
                if current_status in (ClaimStatus.SUPERSEDED, ClaimStatus.REJECTED):
                    raise ClaimError(
                        status_code=409,
                        code="CLAIM_NOT_REJECTABLE",
                        message=f"Claim in status {current_status.value!r} cannot be rejected.",
                    )

                cur.execute(
                    f"""
                    UPDATE public.claims c
                    SET status = 'REJECTED',
                        updated_at = %s
                    WHERE c.workspace_id = %s::uuid
                      AND c.id = %s::uuid
                    RETURNING {_CLAIM_SELECT_COLUMNS}
                    """,
                    (ts, access.workspace_id, claim_uuid),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None

                # Cascade INVALIDATED to any PENDING/READY exports referencing this claim
                cur.execute(
                    """
                    UPDATE public.exports
                    SET status = 'INVALIDATED',
                        invalidation_reason = 'CLAIM_REJECTED',
                        invalidated_at = %s,
                        artifact_payload = NULL,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND status IN ('PENDING', 'READY')
                      AND id IN (
                          SELECT ec.export_id
                          FROM public.export_claims ec
                          WHERE ec.workspace_id = %s::uuid
                            AND ec.claim_id = %s::uuid
                      )
                    """,
                    (ts, ts, access.workspace_id, access.workspace_id, claim_uuid),
                )

                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=claim_uuid,
                    action=ReviewActionType.REJECT,
                    actor_user_id=access.user_id,
                    from_version=current_version,
                    to_version=current_version,
                    idempotency_key=idempotency_key,
                    reason=request.reason,
                    action_metadata={
                        "request_fingerprint": fingerprint,
                        "previous_status": current_status.value,
                        "new_status": ClaimStatus.REJECTED.value,
                    },
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="CLAIM_REJECTED",
                    target_type="CLAIM",
                    target_id=claim_uuid,
                    redacted_metadata={
                        "event_id": event_id,
                        "version": current_version,
                        "reason": request.reason,
                    },
                    created_at=ts,
                )
                self._insert_outbox_event(
                    cur,
                    workspace_id=access.workspace_id,
                    event_type="CLAIM_REJECTED",
                    aggregate_type="CLAIM",
                    aggregate_id=claim_uuid,
                    dedupe_key=f"claim_rejected:{access.workspace_id}:{claim_uuid}:{idempotency_key}",
                    payload={
                        "claim_id": claim_uuid,
                        "event_id": event_id,
                        "version": current_version,
                    },
                    created_at=ts,
                )

                _invoke_hook(self._before_commit_hook, action="reject_claim", arg2=cur)
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=event_id
                )
                return self._build_claim_detail(
                    cur, workspace_id=access.workspace_id, row=updated_row
                )

    # =========================================================================
    # Internal Helpers
    # =========================================================================
    @staticmethod
    def _assert_approval_preconditions(
        *,
        evaluation: ClaimVerificationEvaluation,
        expected_version: int,
        expected_epoch: str | None,
        acknowledge_user_confirmed: bool,
    ) -> None:
        if evaluation.claim_version != expected_version:
            raise ClaimError(
                status_code=409,
                code="CLAIM_VERSION_CONFLICT",
                message=(
                    f"Expected claim version {expected_version}, "
                    f"but current version is {evaluation.claim_version}."
                ),
            )
        if expected_epoch is not None and evaluation.support_graph_epoch != expected_epoch:
            raise ClaimError(
                status_code=409,
                code="SUPPORT_INCOMPLETE",
                message="Expected support_graph_epoch does not match current claim support state.",
                details={
                    "reason_code": "CLAIM_NOT_APPROVABLE_EPOCH_DRIFT",
                    "expected_support_graph_epoch": expected_epoch,
                    "observed_support_graph_epoch": evaluation.support_graph_epoch,
                },
            )
        if evaluation.deletion_fenced:
            raise ClaimError(
                status_code=409,
                code="DELETION_PENDING",
                message="Claim approval blocked while a linked source resource deletion is active.",
            )
        if evaluation.policy_blocked:
            raise ClaimError(
                status_code=403,
                code="SOURCE_POLICY_BLOCKED",
                message="Claim approval blocked because linked source provenance is policy-blocked.",
            )
        if not evaluation.approvable:
            primary_reason = (
                evaluation.approval_block_reasons[0]
                if evaluation.approval_block_reasons
                else "SUPPORT_INCOMPLETE"
            )
            raise ClaimError(
                status_code=409,
                code="SUPPORT_INCOMPLETE",
                message=f"Claim cannot be approved: {', '.join(evaluation.approval_block_reasons) or 'SUPPORT_INCOMPLETE'}.",
                details={
                    "reason_code": primary_reason,
                    "block_reasons": list(evaluation.approval_block_reasons),
                    "claim_status": evaluation.claim_status.value,
                    "computed_verification": evaluation.computed_verification.value,
                },
            )
        if (
            evaluation.computed_verification
            in (ClaimVerification.USER_CONFIRMED, ClaimVerification.MIXED)
            and not acknowledge_user_confirmed
        ):
            raise ClaimError(
                status_code=409,
                code="SUPPORT_INCOMPLETE",
                message=(
                    "Claim contains USER_CONFIRMED atoms; explicit acknowledgement "
                    "(acknowledge_user_confirmed_atoms=true) is required to approve."
                ),
                details={"reason_code": "USER_CONFIRMED_ATOMS_ACK_REQUIRED"},
            )

    def _build_context_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        input_fact_ids: Sequence[str] | None,
        max_bytes: int,
        max_tokens: int,
    ) -> ClaimContextBundle:
        cur.execute(
            """
            SELECT status, anchor_mode
            FROM public.work_events
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        ev_row = cur.fetchone()
        if ev_row is None:
            raise WorkspaceResourceNotFoundError()
        ev_status = WorkEventStatus(str(ev_row[0]))
        anchor_mode = WorkEventAnchorMode(str(ev_row[1]))

        cur.execute(
            """
            SELECT 1
            FROM public.event_composite_memberships
            WHERE workspace_id = %s::uuid
              AND member_event_id = %s::uuid
              AND status = 'ACTIVE'
            LIMIT 1
            """,
            (workspace_id, event_id),
        )
        is_member = cur.fetchone() is not None

        del_fenced = self._has_active_deletion_fence_for_event(
            cur, workspace_id=workspace_id, event_id=event_id
        )

        # Load all facts on this exact event_id (ContextBuilder filters strictly to SUPPORTED + active DIRECT)
        cur.execute(
            """
            SELECT
                f.id::text,
                f.event_id::text,
                f.version,
                f.fact_type,
                f.predicate,
                f.object_text,
                f.status,
                f.subject_identity_id::text,
                f.normalized_value,
                f.fact_metadata
            FROM public.facts f
            WHERE f.workspace_id = %s::uuid
              AND f.event_id = %s::uuid
            ORDER BY f.fact_type ASC, f.id ASC
            """,
            (workspace_id, event_id),
        )
        fact_rows = cur.fetchall()
        fact_ids = [str(r[0]) for r in fact_rows]

        support_rows_by_fact: dict[str, list[tuple[Any, ...]]] = {}
        if fact_ids:
            cur.execute(
                """
                SELECT
                    fs.fact_id::text,
                    fs.id::text,
                    fs.support_type,
                    fs.status,
                    cu.id::text,
                    cu.policy_state,
                    rev.id::text,
                    rev.is_tombstone,
                    so.source_resource_id::text,
                    sr.visibility,
                    sr.status
                FROM public.fact_support fs
                INNER JOIN public.content_units cu
                    ON cu.workspace_id = fs.workspace_id
                   AND cu.id = fs.content_unit_id
                INNER JOIN public.source_object_revisions rev
                    ON rev.workspace_id = cu.workspace_id
                   AND rev.id = cu.source_object_revision_id
                INNER JOIN public.source_objects so
                    ON so.workspace_id = rev.workspace_id
                   AND so.id = rev.source_object_id
                INNER JOIN public.source_resources sr
                    ON sr.workspace_id = so.workspace_id
                   AND sr.id = so.source_resource_id
                WHERE fs.workspace_id = %s::uuid
                  AND fs.fact_id = ANY(%s::uuid[])
                """,
                (workspace_id, fact_ids),
            )
            for srow in cur.fetchall():
                support_rows_by_fact.setdefault(str(srow[0]), []).append(srow)

        items: list[EligibleFactContextItem] = []
        for frow in fact_rows:
            fid = str(frow[0])
            nv = dict(frow[8]) if frow[8] is not None else None
            fm = dict(frow[9] or {})
            metric = _extract_metric_details(nv, fm)
            semantic = extract_semantic_attributes(nv, metric_details=metric)
            srows = support_rows_by_fact.get(fid, [])
            active_direct_fs: list[str] = []
            active_cus: list[str] = []
            active_revs: list[str] = []
            unit_policies: dict[str, str] = {}
            rev_tombs: dict[str, bool] = {}
            has_contra = False
            res_id = "unknown-resource"
            vis = ResourceVisibility.PUBLIC

            for sr in srows:
                stype = str(sr[2])
                sstat = str(sr[3])
                if sstat != "ACTIVE":
                    continue
                if stype == "CONTRADICTS":
                    has_contra = True
                elif stype == "DIRECT":
                    if str(sr[10]) != "ACTIVE":
                        continue
                    active_direct_fs.append(str(sr[1]))
                    active_cus.append(str(sr[4]))
                    active_revs.append(str(sr[6]))
                    unit_policies[str(sr[4])] = str(sr[5])
                    rev_tombs[str(sr[6])] = bool(sr[7])
                    res_id = str(sr[8])
                    vis = ResourceVisibility(str(sr[9]))

            items.append(
                EligibleFactContextItem(
                    fact_id=fid,
                    event_id=str(frow[1]),
                    version=int(frow[2]),
                    fact_type=FactType(str(frow[3])),
                    predicate=str(frow[4]),
                    object_text=str(frow[5]),
                    status=FactStatus(str(frow[6])),
                    subject_identity_id=str(frow[7]) if frow[7] is not None else None,
                    normalized_value=nv,
                    metric_details=metric,
                    semantic=semantic,
                    has_active_direct_support=bool(active_direct_fs),
                    has_active_contradicts=has_contra,
                    active_direct_fact_support_ids=tuple(sorted(set(active_direct_fs))),
                    active_content_unit_ids=tuple(sorted(set(active_cus))),
                    active_revision_ids=tuple(sorted(set(active_revs))),
                    unit_policy_states=unit_policies,
                    revision_tombstone_flags=rev_tombs,
                    source_resource_id=res_id,
                    visibility=vis,
                    deletion_fenced=del_fenced,
                )
            )

        return self._context_builder.build(
            event_id=event_id,
            event_status=ev_status,
            anchor_mode=anchor_mode,
            is_active_composite_member=is_member,
            facts=items,
            requested_fact_ids=input_fact_ids,
            max_bytes=max_bytes,
            max_tokens=max_tokens,
            deletion_fenced=del_fenced,
        )

    def _load_event_evidence_snapshots(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
    ) -> tuple[
        dict[str, FactEvidenceSnapshot],
        tuple[ContributionEvidenceSnapshot, ...],
        dict[str, Any],
    ]:
        cur.execute(
            """
            SELECT
                f.id::text,
                f.event_id::text,
                f.version,
                f.fact_type,
                f.status,
                f.predicate,
                f.object_text,
                f.subject_identity_id::text,
                f.normalized_value,
                f.fact_metadata
            FROM public.facts f
            WHERE f.workspace_id = %s::uuid
              AND f.event_id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        frows = cur.fetchall()
        fids = [str(r[0]) for r in frows]
        referenced_identities: set[str] = set()

        fs_by_fact: dict[str, list[tuple[Any, ...]]] = {}
        if fids:
            cur.execute(
                """
                SELECT
                    fs.fact_id::text,
                    fs.id::text,
                    fs.support_type,
                    fs.status,
                    cu.id::text,
                    rev.id::text,
                    cu.policy_state,
                    rev.is_tombstone
                FROM public.fact_support fs
                INNER JOIN public.content_units cu
                    ON cu.workspace_id = fs.workspace_id
                   AND cu.id = fs.content_unit_id
                INNER JOIN public.source_object_revisions rev
                    ON rev.workspace_id = cu.workspace_id
                   AND rev.id = cu.source_object_revision_id
                WHERE fs.workspace_id = %s::uuid
                  AND fs.fact_id = ANY(%s::uuid[])
                """,
                (workspace_id, fids),
            )
            for srow in cur.fetchall():
                fs_by_fact.setdefault(str(srow[0]), []).append(srow)

        snapshots: dict[str, FactEvidenceSnapshot] = {}
        for r in frows:
            fid = str(r[0])
            subj_id = str(r[7]) if r[7] is not None else None
            if subj_id is not None:
                referenced_identities.add(subj_id)
            nv = dict(r[8]) if r[8] is not None else None
            fm = dict(r[9] or {})
            metric = _extract_metric_details(nv, fm)
            semantic = extract_semantic_attributes(nv, metric_details=metric)

            direct_ids: list[str] = []
            contra_ids: list[str] = []
            support_units: dict[str, tuple[str, str, str, bool]] = {}
            for sr in fs_by_fact.get(fid, []):
                if str(sr[3]) != "ACTIVE":
                    continue
                fs_id = str(sr[1])
                support_units[fs_id] = (str(sr[4]), str(sr[5]), str(sr[6]), bool(sr[7]))
                if str(sr[2]) == "DIRECT":
                    direct_ids.append(fs_id)
                elif str(sr[2]) == "CONTRADICTS":
                    contra_ids.append(fs_id)

            snapshots[fid] = FactEvidenceSnapshot(
                fact_id=fid,
                event_id=str(r[1]),
                version=int(r[2]),
                fact_type=FactType(str(r[3])),
                status=FactStatus(str(r[4])),
                predicate=str(r[5]),
                object_text=str(r[6]),
                subject_identity_id=subj_id,
                normalized_value=nv,
                metric_details=metric,
                semantic=semantic,
                active_direct_support_ids=tuple(sorted(direct_ids)),
                active_contradicts_support_ids=tuple(sorted(contra_ids)),
                support_units=support_units,
            )

        cur.execute(
            """
            SELECT id::text, event_id::text, identity_id::text, role, status
            FROM public.contributions
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        crows = cur.fetchall()
        cids = [str(r[0]) for r in crows]
        for r in crows:
            referenced_identities.add(str(r[2]))

        cs_by_contrib: dict[str, list[str]] = {}
        if cids:
            cur.execute(
                """
                SELECT contribution_id::text, fact_id::text
                FROM public.contribution_support
                WHERE workspace_id = %s::uuid
                  AND contribution_id = ANY(%s::uuid[])
                  AND status = 'ACTIVE'
                """,
                (workspace_id, cids),
            )
            for cs_row in cur.fetchall():
                cs_by_contrib.setdefault(str(cs_row[0]), []).append(str(cs_row[1]))

        contributions = tuple(
            ContributionEvidenceSnapshot(
                contribution_id=str(r[0]),
                event_id=str(r[1]),
                identity_id=str(r[2]),
                role=ContributionRole(str(r[3])),
                status=ContributionStatus(str(r[4])),
                active_supporting_fact_ids=tuple(sorted(cs_by_contrib.get(str(r[0]), []))),
            )
            for r in crows
        )

        eligibilities = {
            ident_id: evaluate_identity_attribution(cur, workspace_id, ident_id)
            for ident_id in sorted(referenced_identities)
        }
        return snapshots, contributions, eligibilities

    def _build_claim_summary(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        row: tuple[Any, ...],
    ) -> ClaimSummaryItem:
        cid = str(row[0])
        ev = self._verification.evaluate_claim(cur, workspace_id=workspace_id, claim_id=cid)
        return ClaimSummaryItem(
            claim_id=cid,
            workspace_id=str(row[1]),
            event_id=str(row[2]),
            version=int(row[4]),
            supersedes_claim_id=str(row[7]) if row[7] is not None else None,
            claim_text=str(row[5]),
            status=ClaimStatus(str(row[6])),
            computed_verification=ev.computed_verification,
            atom_count=ev.atom_count,
            supported_atom_count=ev.supported_atom_count,
            user_confirmed_atom_count=ev.user_confirmed_atom_count,
            unsupported_atom_count=ev.unsupported_atom_count,
            stale_atom_count=ev.stale_atom_count,
            approvable=ev.approvable,
            approval_block_reasons=list(ev.approval_block_reasons),
            support_graph_epoch=ev.support_graph_epoch,
            approved_at=row[8],
            last_verified_at=row[9],
            created_at=row[11],
            updated_at=row[12],
        )

    def _build_claim_detail(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        row: tuple[Any, ...],
    ) -> ClaimDetailResponse:
        summary = self._build_claim_summary(cur, workspace_id=workspace_id, row=row)
        cid = summary.claim_id
        gen_run_id = str(row[3]) if row[3] is not None else None
        last_verif_run_id = str(row[10]) if row[10] is not None else None

        atom_meta_by_ord: dict[str, dict[str, Any]] = {}
        if last_verif_run_id is not None:
            cur.execute(
                """
                SELECT run_metadata
                FROM public.generation_runs
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (workspace_id, last_verif_run_id),
            )
            rm_row = cur.fetchone()
            if rm_row is not None and isinstance(rm_row[0], dict):
                details_map = rm_row[0].get("atom_validation_details")
                if isinstance(details_map, dict):
                    atom_meta_by_ord = {
                        str(k): dict(v) for k, v in details_map.items() if isinstance(v, dict)
                    }

        cur.execute(
            """
            SELECT id::text, ordinal, atom_type, atom_text, status
            FROM public.claim_atoms
            WHERE workspace_id = %s::uuid
              AND claim_id = %s::uuid
            ORDER BY ordinal ASC, id ASC
            """,
            (workspace_id, cid),
        )
        arows = cur.fetchall()
        aids = [str(r[0]) for r in arows]
        fids_by_atom: dict[str, list[str]] = {aid: [] for aid in aids}
        if aids:
            cur.execute(
                """
                SELECT claim_atom_id::text, fact_id::text
                FROM public.claim_atom_support
                WHERE workspace_id = %s::uuid
                  AND claim_atom_id = ANY(%s::uuid[])
                ORDER BY created_at ASC, id ASC
                """,
                (workspace_id, aids),
            )
            for aid, fid in cur.fetchall():
                fids_by_atom.setdefault(str(aid), []).append(str(fid))

        atoms = [
            ClaimAtomDetailItem(
                atom_id=str(ar[0]),
                ordinal=int(ar[1]),
                atom_type=ClaimAtomType(str(ar[2])),
                atom_text=str(ar[3]),
                status=ClaimAtomStatus(str(ar[4])),
                unsupported_reason=atom_meta_by_ord.get(str(ar[1]), {}).get("unsupported_reason"),
                fact_ids=fids_by_atom.get(str(ar[0]), []),
                role_contribution_grounding=atom_meta_by_ord.get(str(ar[1]), {}).get(
                    "role_contribution_grounding"
                ),
                metric_grounding=atom_meta_by_ord.get(str(ar[1]), {}).get("metric_grounding"),
            )
            for ar in arows
        ]
        return ClaimDetailResponse(
            **summary.model_dump(),
            generator_run_id=gen_run_id,
            last_verification_run_id=last_verif_run_id,
            atoms=atoms,
        )

    @staticmethod
    def _select_claim_row(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        claim_id: str,
    ) -> tuple[Any, ...]:
        cur.execute(
            f"""
            SELECT {_CLAIM_SELECT_COLUMNS}
            FROM public.claims c
            WHERE c.workspace_id = %s::uuid
              AND c.id = %s::uuid
            """,
            (workspace_id, claim_id),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceResourceNotFoundError()
        return row

    @staticmethod
    def _lock_claim_row(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        claim_id: str,
    ) -> tuple[Any, ...]:
        cur.execute(
            f"""
            SELECT {_CLAIM_SELECT_COLUMNS}
            FROM public.claims c
            WHERE c.workspace_id = %s::uuid
              AND c.id = %s::uuid
            FOR UPDATE OF c
            """,
            (workspace_id, claim_id),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceResourceNotFoundError()
        return row

    @staticmethod
    def _lock_claim_support_graph(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        claim_id: str,
        event_id: str,
    ) -> None:
        cur.execute(
            """
            SELECT id
            FROM public.claim_atoms
            WHERE workspace_id = %s::uuid AND claim_id = %s::uuid
            ORDER BY id ASC
            FOR UPDATE
            """,
            (workspace_id, claim_id),
        )
        atom_ids = [str(r[0]) for r in cur.fetchall()]
        if atom_ids:
            cur.execute(
                """
                SELECT id, fact_id
                FROM public.claim_atom_support
                WHERE workspace_id = %s::uuid AND claim_atom_id = ANY(%s::uuid[])
                ORDER BY id ASC
                FOR UPDATE
                """,
                (workspace_id, atom_ids),
            )
            fact_ids = sorted({str(r[1]) for r in cur.fetchall()})
            if fact_ids:
                cur.execute(
                    """
                    SELECT id
                    FROM public.facts
                    WHERE workspace_id = %s::uuid AND id = ANY(%s::uuid[])
                    ORDER BY id ASC
                    FOR UPDATE
                    """,
                    (workspace_id, fact_ids),
                )
                cur.execute(
                    """
                    SELECT id
                    FROM public.fact_support
                    WHERE workspace_id = %s::uuid AND fact_id = ANY(%s::uuid[])
                    ORDER BY id ASC
                    FOR UPDATE
                    """,
                    (workspace_id, fact_ids),
                )
        cur.execute(
            """
            SELECT id
            FROM public.contributions
            WHERE workspace_id = %s::uuid AND event_id = %s::uuid
            ORDER BY id ASC
            FOR UPDATE
            """,
            (workspace_id, event_id),
        )

    @staticmethod
    def _lookup_event_resource_info(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
    ) -> tuple[str, ResourceVisibility]:
        cur.execute(
            """
            SELECT sr.id::text, sr.visibility
            FROM public.work_events we
            LEFT JOIN public.source_objects so
                ON so.workspace_id = we.workspace_id
               AND so.id = we.primary_anchor_source_object_id
            LEFT JOIN public.source_resources sr
                ON sr.workspace_id = so.workspace_id
               AND sr.id = so.source_resource_id
            WHERE we.workspace_id = %s::uuid
              AND we.id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        row = cur.fetchone()
        if row is not None and row[0] is not None:
            return str(row[0]), ResourceVisibility(str(row[1]))
        return "event-resource", ResourceVisibility.PUBLIC

    @staticmethod
    def _assert_live_workspace_membership(
        cur: psycopg.Cursor, access: WorkspaceAccessContext
    ) -> None:
        cur.execute(
            """
            SELECT 1
            FROM public.workspace_memberships
            WHERE workspace_id = %s::uuid
              AND user_id = %s::uuid
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (access.workspace_id, access.user_id),
        )
        if cur.fetchone() is None:
            raise WorkspaceMembershipDeniedError("Workspace membership is no longer active.")

    @staticmethod
    def _has_active_deletion_fence_for_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
    ) -> bool:
        cur.execute(
            """
            SELECT 1
            FROM public.deletion_jobs dj
            INNER JOIN public.source_objects so
                ON so.workspace_id = dj.workspace_id
               AND so.source_resource_id = dj.source_resource_id
            LEFT JOIN public.work_events we
                ON we.workspace_id = so.workspace_id
               AND we.primary_anchor_source_object_id = so.id
               AND we.id = %s::uuid
            LEFT JOIN public.event_memberships em
                ON em.workspace_id = so.workspace_id
               AND em.source_object_id = so.id
               AND em.event_id = %s::uuid
               AND em.status = 'ACTIVE'
            WHERE dj.workspace_id = %s::uuid
              AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL')
              AND (we.id IS NOT NULL OR em.id IS NOT NULL)
            LIMIT 1
            """,
            (event_id, event_id, workspace_id),
        )
        return cur.fetchone() is not None

    def _assert_no_active_deletion_fence_for_event(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
    ) -> None:
        if self._has_active_deletion_fence_for_event(
            cur, workspace_id=workspace_id, event_id=event_id
        ):
            raise ClaimError(
                status_code=409,
                code="DELETION_PENDING",
                message="Claim operation blocked while a linked source resource deletion is active.",
            )

    @staticmethod
    def _check_idempotency_replay(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        action: ReviewActionType,
        claim_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT action_metadata
            FROM public.review_actions
            WHERE workspace_id = %s::uuid
              AND action = %s
              AND target_type = %s
              AND target_id = %s::uuid
              AND idempotency_key = %s
            LIMIT 1
            """,
            (
                workspace_id,
                action.value,
                ReviewTargetType.CLAIM.value,
                claim_id,
                idempotency_key,
            ),
        )
        row = cur.fetchone()
        if row is None:
            return None
        meta = dict(row[0] or {})
        if meta.get("request_fingerprint") != fingerprint:
            raise ClaimError(
                status_code=409,
                code="IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key was already used with a different claim action payload.",
            )
        return meta

    @staticmethod
    def _insert_generation_run(
        cur: psycopg.Cursor,
        *,
        run_id: str,
        workspace_id: str,
        event_id: str | None,
        run_type: GenerationRunType,
        input_artifact_versions: dict[str, Any],
        model: str,
        prompt_version: str,
        algorithm_version: str,
        input_hash: str,
        status: GenerationRunStatus,
        error_code: str | None,
        run_metadata: dict[str, Any],
        now: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(input_artifact_versions, field_name="input_artifact_versions")
        assert_no_forbidden_raw_keys(run_metadata, field_name="run_metadata")
        assert_no_raw_text_or_secret_keys(run_metadata, context="generation_runs.run_metadata")
        cur.execute(
            """
            INSERT INTO public.generation_runs (
                id, workspace_id, event_id, run_type, input_artifact_versions,
                model, prompt_version, algorithm_version, input_hash,
                status, error_code, run_metadata, started_at, completed_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                run_id,
                workspace_id,
                event_id,
                run_type.value,
                Jsonb(input_artifact_versions),
                model,
                prompt_version,
                algorithm_version,
                input_hash,
                status.value,
                error_code,
                Jsonb(run_metadata),
                now,
                now,
            ),
        )

    @staticmethod
    def _insert_review_action(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        target_id: str,
        action: ReviewActionType,
        actor_user_id: str | None,
        from_version: int | None,
        to_version: int | None,
        idempotency_key: str,
        reason: str | None,
        action_metadata: dict[str, Any],
        created_at: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(action_metadata, field_name="action_metadata")
        cur.execute(
            """
            INSERT INTO public.review_actions (
                id, workspace_id, target_type, target_id, action,
                actor_user_id, from_version, to_version, idempotency_key,
                reason, action_metadata, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid, %s,
                %s::uuid, %s, %s, %s,
                %s, %s, %s
            )
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                ReviewTargetType.CLAIM.value,
                target_id,
                action.value,
                _parse_uuid_or_none(actor_user_id),
                from_version,
                to_version,
                idempotency_key,
                reason,
                Jsonb(action_metadata),
                created_at,
            ),
        )

    @staticmethod
    def _insert_audit_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        action: str,
        target_type: str,
        target_id: str,
        redacted_metadata: dict[str, Any],
        created_at: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(redacted_metadata, field_name="redacted_metadata")
        cur.execute(
            """
            INSERT INTO public.audit_events (
                id, workspace_id, actor_type, actor_id,
                action, target_type, target_id, redacted_metadata, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid,
                %s, %s, %s::uuid, %s, %s
            )
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                actor_type.value,
                _parse_uuid_or_none(actor_id),
                action,
                target_type,
                target_id,
                Jsonb(redacted_metadata),
                created_at,
            ),
        )

    @staticmethod
    def _insert_outbox_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        dedupe_key: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(payload, field_name="outbox_payload")
        cur.execute(
            """
            INSERT INTO public.outbox_events (
                workspace_id, event_type, aggregate_type, aggregate_id,
                dedupe_key, payload, status, created_at
            ) VALUES (
                %s::uuid, %s, %s, %s::uuid,
                %s, %s, 'PENDING', %s
            )
            """,
            (
                workspace_id,
                event_type,
                aggregate_type,
                aggregate_id,
                dedupe_key,
                Jsonb(payload),
                created_at,
            ),
        )


__all__ = [
    "BeforeCommitHook",
    "BeforeLockHook",
    "ConnectionFactory",
    "PostgresClaimService",
]
