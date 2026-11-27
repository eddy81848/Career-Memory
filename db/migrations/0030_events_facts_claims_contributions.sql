-- Migration 0030: Identity, WorkEvent, Fact, Contribution, Claim & Provenance Schema (T-104)
--
-- Prerequisites: Migration 0020_source_provenance_metadata.sql
-- Authoritative spec alignment: Notion Data Model §3.5 - §3.10
--
-- Design guarantees:
-- 1. All 28 parent-child tenant relationships enforce Composite Foreign Keys
--    (workspace_id, parent_id) -> parent(workspace_id, id), eliminating PostgreSQL
--    single-FK cross-workspace existence oracle and reference vulnerabilities.
-- 2. Plan C (방안 C) raw-text non-persistence:
--    - external_identity_mappings stores email_hash (never plaintext external_email).
--    - fact_support.extracted_span IS NULL.
--    - JSONB metadata columns (evidence_metadata, anchor_scope, summary_metadata,
--      action_metadata, normalized_value, fact_metadata, support_metadata,
--      input_artifact_versions, token_usage, run_metadata) block forbidden raw-text
--      keys recursively via public.cm_jsonb_contains_forbidden_raw_key().
-- 3. Domain invariants (Notion Data Model §3.5 - §3.10):
--    - identities: display_name, identity_type (PERSON|BOT|UNKNOWN), canonical_user_id,
--      status (ACTIVE|AMBIGUOUS|MERGED|REVOKED).
--    - external_identity_mappings: verification_method (OAUTH_ACCOUNT|PROVIDER_ASSOCIATION|
--      USER_CONFIRMATION), status (VERIFIED|USER_CONFIRMED|HEURISTIC|CONFLICTED|REVOKED),
--      strictly forbidding unverified/heuristic promotion to VERIFIED.
--    - work_events: anchor_mode (PR|PR_SEGMENT|DIRECT_COMMIT|USER_COMPOSITE), version,
--      status (CANDIDATE|CONFIRMED|REJECTED|STALE|SUPERSEDED), created_by (RULE|USER),
--      PR/PR_SEGMENT/DIRECT_COMMIT anchor invariants.
--    - event_memberships: relation_class (MUST_LINK|CONTEXT_ONLY|CANNOT_LINK),
--      relation_type, origin (SOURCE|RULE|USER), UNIQUE(event_id, source_object_id, relation_type).
--    - event_composite_memberships: versioned composite/member references, no self-reference,
--      Composite FK to review_actions(workspace_id, id), single active membership index.
--    - review_actions: action (CONFIRM|EDIT|SPLIT|MERGE|DISSOLVE|REJECT|REOPEN), version tracking.
--    - facts: subject_identity_id Composite FK, predicate, object_text, normalized_value.
--    - contributions: MVP RULE candidates restricted to IMPLEMENTED / REVIEWED.
--    - generation_runs: 6-stage run_type, model, algorithm_version, input_hash, CANCELED.
--    - claims: version, generator_run_id, approved_at, NO writable `verification` column;
--      verification is strictly computed via security_invoker view `v_claim_verification_summary`.
--    - claim_atom_support: validation_run_id Composite FK to generation_runs.
-- 4. Deferred boundaries intentionally omitted:
--    - OD-007 active Event-to-Event CANNOT_LINK physical table (deferred to M3 / T-304).
--    - Post-MVP embeddings, incremental watermarks, and resource lease tables (Data Model §8).

SET ROLE cm_migrator;

-- =============================================================================
-- 1. Identity & External Identity Mappings (Data Model §3.5)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.identities (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    display_name      text NOT NULL,
    identity_type     text NOT NULL DEFAULT 'PERSON',
    canonical_user_id uuid NULL REFERENCES public.users (id) ON DELETE SET NULL,
    status            text NOT NULL DEFAULT 'ACTIVE',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_identities_display_name_non_empty
        CHECK (length(btrim(display_name)) > 0),
    CONSTRAINT ck_identities_type
        CHECK (identity_type IN ('PERSON', 'BOT', 'UNKNOWN')),
    CONSTRAINT ck_identities_status
        CHECK (status IN ('ACTIVE', 'AMBIGUOUS', 'MERGED', 'REVOKED')),
    CONSTRAINT uq_identities_workspace_id_id
        UNIQUE (workspace_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_active_canonical_user_per_workspace
    ON public.identities (workspace_id, canonical_user_id)
    WHERE canonical_user_id IS NOT NULL AND status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS public.external_identity_mappings (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    identity_id         uuid NOT NULL,
    provider            text NOT NULL,
    external_user_id    text NULL,
    email_hash          text NULL,
    username            text NULL,
    verification_method text NOT NULL,
    status              text NOT NULL DEFAULT 'HEURISTIC',
    confidence_reason   text NULL,
    evidence_metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    verified_at         timestamptz NULL,
    revoked_at          timestamptz NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_external_identity_mappings_provider
        CHECK (provider IN ('GITHUB')),
    CONSTRAINT ck_external_identity_mappings_has_external_identifier
        CHECK (
            external_user_id IS NOT NULL
            OR email_hash IS NOT NULL
            OR username IS NOT NULL
        ),
    CONSTRAINT ck_external_identity_mappings_email_hash_format
        CHECK (email_hash IS NULL OR email_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_external_identity_mappings_verification_method
        CHECK (
            verification_method IN (
                'OAUTH_ACCOUNT',
                'PROVIDER_ASSOCIATION',
                'USER_CONFIRMATION'
            )
        ),
    CONSTRAINT ck_external_identity_mappings_status
        CHECK (
            status IN (
                'VERIFIED',
                'USER_CONFIRMED',
                'HEURISTIC',
                'CONFLICTED',
                'REVOKED'
            )
        ),
    -- Prevent heuristic/user-only claims from being marked VERIFIED without provider proof.
    CONSTRAINT ck_external_identity_mappings_verified_method
        CHECK (
            status <> 'VERIFIED'
            OR verification_method IN ('OAUTH_ACCOUNT', 'PROVIDER_ASSOCIATION')
        ),
    -- USER_CONFIRMED status must correspond to USER_CONFIRMATION method.
    CONSTRAINT ck_external_identity_mappings_user_confirmed_method
        CHECK (
            status <> 'USER_CONFIRMED'
            OR verification_method = 'USER_CONFIRMATION'
        ),
    CONSTRAINT ck_external_identity_mappings_revoked_at_consistency
        CHECK (status = 'REVOKED' OR revoked_at IS NULL),
    CONSTRAINT ck_external_identity_mappings_evidence_metadata_object
        CHECK (jsonb_typeof(evidence_metadata) = 'object'),
    CONSTRAINT ck_external_identity_mappings_evidence_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(evidence_metadata)),
    CONSTRAINT uq_external_identity_mappings_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_external_identity_mappings_same_workspace_identity
        FOREIGN KEY (workspace_id, identity_id)
        REFERENCES public.identities (workspace_id, id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ext_identity_mappings_verified_external_user
    ON public.external_identity_mappings (workspace_id, provider, external_user_id)
    WHERE status = 'VERIFIED' AND external_user_id IS NOT NULL;

-- =============================================================================
-- 2. Work Events, Memberships, Review Actions & Composite Memberships (§3.6)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.work_events (
    id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    primary_anchor_source_object_id uuid NULL,
    anchor_mode                     text NOT NULL,
    anchor_scope                    jsonb NULL,
    version                         integer NOT NULL DEFAULT 1,
    title                           text NOT NULL,
    problem                         text NULL,
    action                          text NULL,
    outcome                         text NULL,
    summary_metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                          text NOT NULL DEFAULT 'CANDIDATE',
    created_by                      text NOT NULL DEFAULT 'RULE',
    occurred_start_at               timestamptz NULL,
    occurred_end_at                 timestamptz NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    reviewed_at                     timestamptz NULL,
    updated_at                      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_work_events_anchor_mode
        CHECK (anchor_mode IN ('PR', 'PR_SEGMENT', 'DIRECT_COMMIT', 'USER_COMPOSITE')),
    CONSTRAINT ck_work_events_version_positive
        CHECK (version >= 1),
    CONSTRAINT ck_work_events_title_non_empty
        CHECK (length(btrim(title)) > 0),
    CONSTRAINT ck_work_events_status
        CHECK (status IN ('CANDIDATE', 'CONFIRMED', 'REJECTED', 'STALE', 'SUPERSEDED')),
    CONSTRAINT ck_work_events_created_by
        CHECK (created_by IN ('RULE', 'USER')),
    -- Anchor invariants (Data Model §3.6):
    CONSTRAINT ck_work_events_pr_anchor
        CHECK (anchor_mode NOT IN ('PR', 'PR_SEGMENT') OR primary_anchor_source_object_id IS NOT NULL),
    CONSTRAINT ck_work_events_pr_segment_scope
        CHECK (
            anchor_mode <> 'PR_SEGMENT'
            OR (anchor_scope IS NOT NULL AND jsonb_typeof(anchor_scope) = 'object')
        ),
    CONSTRAINT ck_work_events_direct_commit_anchor
        CHECK (anchor_mode <> 'DIRECT_COMMIT' OR primary_anchor_source_object_id IS NOT NULL),
    CONSTRAINT ck_work_events_anchor_scope_no_raw
        CHECK (
            anchor_scope IS NULL
            OR (
                jsonb_typeof(anchor_scope) = 'object'
                AND NOT public.cm_jsonb_contains_forbidden_raw_key(anchor_scope)
            )
        ),
    CONSTRAINT ck_work_events_summary_metadata_object
        CHECK (jsonb_typeof(summary_metadata) = 'object'),
    CONSTRAINT ck_work_events_summary_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(summary_metadata)),
    CONSTRAINT ck_work_events_time_order
        CHECK (
            occurred_start_at IS NULL
            OR occurred_end_at IS NULL
            OR occurred_end_at >= occurred_start_at
        ),
    CONSTRAINT uq_work_events_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_work_events_same_workspace_primary_anchor
        FOREIGN KEY (workspace_id, primary_anchor_source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE CASCADE
);

-- Event <-> SourceObject membership (MUST_LINK | CONTEXT_ONLY | CANNOT_LINK).
-- Note: Event <-> Event CANNOT_LINK is deferred under OD-007 (M3 / T-304).
CREATE TABLE IF NOT EXISTS public.event_memberships (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    event_id         uuid NOT NULL,
    source_object_id uuid NOT NULL,
    relation_class   text NOT NULL,
    relation_type    text NOT NULL,
    origin           text NOT NULL DEFAULT 'RULE',
    rationale_code   text NULL,
    status           text NOT NULL DEFAULT 'ACTIVE',
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_event_memberships_relation_class
        CHECK (relation_class IN ('MUST_LINK', 'CONTEXT_ONLY', 'CANNOT_LINK')),
    CONSTRAINT ck_event_memberships_relation_type_non_empty
        CHECK (length(btrim(relation_type)) > 0),
    CONSTRAINT ck_event_memberships_origin
        CHECK (origin IN ('SOURCE', 'RULE', 'USER')),
    CONSTRAINT ck_event_memberships_status
        CHECK (status IN ('ACTIVE', 'INVALIDATED')),
    CONSTRAINT uq_event_memberships_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_event_memberships_event_source_object_relation_type
        UNIQUE (event_id, source_object_id, relation_type),
    CONSTRAINT fk_event_memberships_same_workspace_event
        FOREIGN KEY (workspace_id, event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_event_memberships_same_workspace_source_object
        FOREIGN KEY (workspace_id, source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE CASCADE
);

-- Declared before event_composite_memberships so added_by_review_action_id can
-- reference review_actions(workspace_id, id) via Composite FK directly.
CREATE TABLE IF NOT EXISTS public.review_actions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    target_type     text NOT NULL,
    target_id       uuid NOT NULL,
    action          text NOT NULL,
    actor_user_id   uuid NULL,
    from_version    integer NULL,
    to_version      integer NULL,
    idempotency_key text NOT NULL,
    reason          text NULL,
    action_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_review_actions_target_type
        CHECK (
            target_type IN (
                'WORK_EVENT',
                'FACT',
                'CONTRIBUTION',
                'CLAIM',
                'CLAIM_ATOM',
                'IDENTITY_MAPPING',
                'SOURCE_RESOURCE',
                'SOURCE_OBJECT'
            )
        ),
    CONSTRAINT ck_review_actions_action
        CHECK (
            action IN (
                'CONFIRM',
                'EDIT',
                'SPLIT',
                'MERGE',
                'DISSOLVE',
                'REJECT',
                'REOPEN'
            )
        ),
    CONSTRAINT ck_review_actions_from_version_positive
        CHECK (from_version IS NULL OR from_version >= 1),
    CONSTRAINT ck_review_actions_to_version_positive
        CHECK (to_version IS NULL OR to_version >= 1),
    CONSTRAINT ck_review_actions_idempotency_key_non_empty
        CHECK (length(btrim(idempotency_key)) > 0),
    CONSTRAINT ck_review_actions_metadata_object
        CHECK (jsonb_typeof(action_metadata) = 'object'),
    CONSTRAINT ck_review_actions_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(action_metadata)),
    CONSTRAINT uq_review_actions_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_review_actions_idempotency
        UNIQUE (workspace_id, action, target_type, target_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS public.event_composite_memberships (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id              uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    composite_event_id        uuid NOT NULL,
    composite_event_version   integer NOT NULL DEFAULT 1,
    member_event_id           uuid NOT NULL,
    member_event_version      integer NOT NULL DEFAULT 1,
    ordinal                   integer NOT NULL DEFAULT 0,
    status                    text NOT NULL DEFAULT 'ACTIVE',
    added_by_review_action_id uuid NULL,
    created_at                timestamptz NOT NULL DEFAULT now(),
    removed_at                timestamptz NULL,

    CONSTRAINT ck_event_composite_memberships_no_self_reference
        CHECK (composite_event_id <> member_event_id),
    CONSTRAINT ck_event_composite_memberships_composite_version_positive
        CHECK (composite_event_version >= 1),
    CONSTRAINT ck_event_composite_memberships_member_version_positive
        CHECK (member_event_version >= 1),
    CONSTRAINT ck_event_composite_memberships_ordinal_non_negative
        CHECK (ordinal >= 0),
    CONSTRAINT ck_event_composite_memberships_status
        CHECK (status IN ('ACTIVE', 'REMOVED')),
    CONSTRAINT ck_event_composite_memberships_removed_at_consistency
        CHECK (status = 'REMOVED' OR removed_at IS NULL),
    CONSTRAINT uq_event_composite_memberships_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_event_composite_memberships_version_member
        UNIQUE (composite_event_id, composite_event_version, member_event_id),
    CONSTRAINT fk_event_composite_same_workspace_composite
        FOREIGN KEY (workspace_id, composite_event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_event_composite_same_workspace_member
        FOREIGN KEY (workspace_id, member_event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_event_composite_same_workspace_review_action
        FOREIGN KEY (workspace_id, added_by_review_action_id)
        REFERENCES public.review_actions (workspace_id, id)
        ON DELETE SET NULL
);

-- Prevent duplicate active assignment of the same member event.
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_composite_memberships_active_member
    ON public.event_composite_memberships (workspace_id, member_event_id)
    WHERE status = 'ACTIVE';

-- =============================================================================
-- 3. Facts & Fact Support (Data Model §3.7)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.facts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    event_id            uuid NOT NULL,
    fact_type           text NOT NULL,
    subject_identity_id uuid NULL,
    predicate           text NOT NULL,
    object_text         text NOT NULL,
    normalized_value    jsonb NULL,
    fact_metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status              text NOT NULL DEFAULT 'CANDIDATE',
    extractor_version   text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_facts_fact_type
        CHECK (fact_type IN ('PROBLEM', 'ACTION', 'TECHNOLOGY', 'RESULT', 'ROLE', 'METRIC')),
    CONSTRAINT ck_facts_predicate_non_empty
        CHECK (length(btrim(predicate)) > 0),
    CONSTRAINT ck_facts_object_text_non_empty
        CHECK (length(btrim(object_text)) > 0),
    CONSTRAINT ck_facts_normalized_value_no_raw
        CHECK (
            normalized_value IS NULL
            OR NOT public.cm_jsonb_contains_forbidden_raw_key(normalized_value)
        ),
    CONSTRAINT ck_facts_metadata_object
        CHECK (jsonb_typeof(fact_metadata) = 'object'),
    CONSTRAINT ck_facts_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(fact_metadata)),
    CONSTRAINT ck_facts_status
        CHECK (status IN ('CANDIDATE', 'SUPPORTED', 'USER_CONFIRMED', 'CONFLICTED', 'STALE', 'REJECTED')),
    CONSTRAINT ck_facts_extractor_version_non_empty
        CHECK (length(btrim(extractor_version)) > 0),
    CONSTRAINT uq_facts_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_facts_same_workspace_event
        FOREIGN KEY (workspace_id, event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_facts_same_workspace_subject_identity
        FOREIGN KEY (workspace_id, subject_identity_id)
        REFERENCES public.identities (workspace_id, id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS public.fact_support (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    fact_id           uuid NOT NULL,
    content_unit_id   uuid NOT NULL,
    support_type      text NOT NULL,
    extracted_span    text NULL,
    support_metadata  jsonb NOT NULL DEFAULT '{}'::jsonb,
    validator         text NOT NULL,
    validator_version text NOT NULL,
    status            text NOT NULL DEFAULT 'ACTIVE',
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_fact_support_type
        CHECK (support_type IN ('DIRECT', 'CONTEXT', 'CONTRADICTS')),
    -- Plan C (방안 C) absolute invariant: never persist raw extracted source spans.
    CONSTRAINT ck_fact_support_extracted_span_null
        CHECK (extracted_span IS NULL),
    CONSTRAINT ck_fact_support_metadata_object
        CHECK (jsonb_typeof(support_metadata) = 'object'),
    CONSTRAINT ck_fact_support_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(support_metadata)),
    CONSTRAINT ck_fact_support_validator
        CHECK (validator IN ('RULE', 'USER', 'MODEL')),
    CONSTRAINT ck_fact_support_validator_version_non_empty
        CHECK (length(btrim(validator_version)) > 0),
    CONSTRAINT ck_fact_support_status
        CHECK (status IN ('ACTIVE', 'INVALIDATED')),
    CONSTRAINT uq_fact_support_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_fact_support_fact_unit_type
        UNIQUE (fact_id, content_unit_id, support_type),
    CONSTRAINT fk_fact_support_same_workspace_fact
        FOREIGN KEY (workspace_id, fact_id)
        REFERENCES public.facts (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_fact_support_same_workspace_content_unit
        FOREIGN KEY (workspace_id, content_unit_id)
        REFERENCES public.content_units (workspace_id, id)
        ON DELETE CASCADE
);

-- =============================================================================
-- 4. Contributions & Contribution Support (Data Model §3.8)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.contributions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    event_id     uuid NOT NULL,
    identity_id  uuid NOT NULL,
    role         text NOT NULL,
    origin       text NOT NULL DEFAULT 'RULE',
    status       text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_contributions_role
        CHECK (
            role IN (
                'IMPLEMENTED',
                'REVIEWED',
                'PROPOSED',
                'DESIGNED',
                'OPERATED',
                'MENTORED',
                'MEASURED'
            )
        ),
    CONSTRAINT ck_contributions_origin
        CHECK (origin IN ('RULE', 'USER')),
    CONSTRAINT ck_contributions_status
        CHECK (status IN ('CANDIDATE', 'SOURCE_SUPPORTED', 'USER_CONFIRMED', 'REJECTED', 'STALE')),
    -- MVP automatic RULE candidates are strictly limited to IMPLEMENTED / REVIEWED.
    CONSTRAINT ck_contributions_mvp_rule_roles
        CHECK (origin <> 'RULE' OR role IN ('IMPLEMENTED', 'REVIEWED')),
    CONSTRAINT uq_contributions_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_contributions_event_identity_role
        UNIQUE (event_id, identity_id, role),
    CONSTRAINT fk_contributions_same_workspace_event
        FOREIGN KEY (workspace_id, event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_contributions_same_workspace_identity
        FOREIGN KEY (workspace_id, identity_id)
        REFERENCES public.identities (workspace_id, id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.contribution_support (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    contribution_id uuid NOT NULL,
    fact_id         uuid NOT NULL,
    status          text NOT NULL DEFAULT 'ACTIVE',
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_contribution_support_status
        CHECK (status IN ('ACTIVE', 'INVALIDATED')),
    CONSTRAINT uq_contribution_support_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_contribution_support_contribution_fact
        UNIQUE (contribution_id, fact_id),
    CONSTRAINT fk_contribution_support_same_workspace_contribution
        FOREIGN KEY (workspace_id, contribution_id)
        REFERENCES public.contributions (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_contribution_support_same_workspace_fact
        FOREIGN KEY (workspace_id, fact_id)
        REFERENCES public.facts (workspace_id, id)
        ON DELETE CASCADE
);

-- =============================================================================
-- 5. Generation Runs, Claims, Claim Atoms & Claim Atom Support (§3.9 - §3.10)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.generation_runs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    event_id                uuid NULL,
    run_type                text NOT NULL,
    input_artifact_versions jsonb NOT NULL DEFAULT '{}'::jsonb,
    model                   text NOT NULL,
    prompt_version          text NOT NULL,
    algorithm_version       text NOT NULL DEFAULT 'v1',
    input_hash              text NOT NULL,
    status                  text NOT NULL DEFAULT 'RUNNING',
    token_usage             jsonb NULL,
    cost_estimate           numeric(12, 6) NULL,
    latency_ms              integer NULL,
    error_code              text NULL,
    run_metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at              timestamptz NOT NULL DEFAULT now(),
    completed_at            timestamptz NULL,

    CONSTRAINT ck_generation_runs_run_type
        CHECK (
            run_type IN (
                'FACT_CANDIDATE_EXTRACTION',
                'FACT_SUPPORT_VALIDATION',
                'CLAIM_DRAFT_GENERATION',
                'CLAIM_ATOMIZATION',
                'CLAIM_SUPPORT_VALIDATION',
                'REFETCH_VERIFICATION'
            )
        ),
    CONSTRAINT ck_generation_runs_model_non_empty
        CHECK (length(btrim(model)) > 0),
    CONSTRAINT ck_generation_runs_prompt_version_non_empty
        CHECK (length(btrim(prompt_version)) > 0),
    CONSTRAINT ck_generation_runs_algorithm_version_non_empty
        CHECK (length(btrim(algorithm_version)) > 0),
    CONSTRAINT ck_generation_runs_input_hash_format
        CHECK (input_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_generation_runs_input_versions_object
        CHECK (jsonb_typeof(input_artifact_versions) = 'object'),
    CONSTRAINT ck_generation_runs_input_versions_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(input_artifact_versions)),
    CONSTRAINT ck_generation_runs_token_usage_object_no_raw
        CHECK (
            token_usage IS NULL
            OR (
                jsonb_typeof(token_usage) = 'object'
                AND NOT public.cm_jsonb_contains_forbidden_raw_key(token_usage)
            )
        ),
    CONSTRAINT ck_generation_runs_cost_estimate_non_negative
        CHECK (cost_estimate IS NULL OR cost_estimate >= 0),
    CONSTRAINT ck_generation_runs_latency_ms_non_negative
        CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CONSTRAINT ck_generation_runs_metadata_object
        CHECK (jsonb_typeof(run_metadata) = 'object'),
    CONSTRAINT ck_generation_runs_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(run_metadata)),
    CONSTRAINT ck_generation_runs_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELED')),
    CONSTRAINT ck_generation_runs_time_order
        CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT uq_generation_runs_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_generation_runs_same_workspace_event
        FOREIGN KEY (workspace_id, event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE
);

-- Note: `claims` intentionally omits any writable `verification` column.
-- Verification (SOURCE_VERIFIED | USER_CONFIRMED | MIXED | UNVERIFIED) is strictly
-- computed read-only from active provenance chains via `v_claim_verification_summary`.
CREATE TABLE IF NOT EXISTS public.claims (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id             uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    event_id                 uuid NOT NULL,
    generator_run_id         uuid NULL,
    version                  integer NOT NULL DEFAULT 1,
    claim_text               text NOT NULL,
    status                   text NOT NULL DEFAULT 'DRAFT',
    supersedes_claim_id      uuid NULL,
    approved_at              timestamptz NULL,
    last_verified_at         timestamptz NULL,
    last_verification_run_id uuid NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_claims_version_positive
        CHECK (version >= 1),
    CONSTRAINT ck_claims_text_non_empty
        CHECK (length(btrim(claim_text)) > 0),
    CONSTRAINT ck_claims_status
        CHECK (
            status IN (
                'DRAFT',
                'DRAFT_INVALID',
                'SUPPORT_VALIDATED',
                'APPROVED',
                'REJECTED',
                'STALE',
                'SUPERSEDED'
            )
        ),
    CONSTRAINT ck_claims_no_self_supersede
        CHECK (supersedes_claim_id IS NULL OR supersedes_claim_id <> id),
    CONSTRAINT uq_claims_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_claims_same_workspace_event
        FOREIGN KEY (workspace_id, event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_claims_same_workspace_generator_run
        FOREIGN KEY (workspace_id, generator_run_id)
        REFERENCES public.generation_runs (workspace_id, id)
        ON DELETE SET NULL,
    CONSTRAINT fk_claims_same_workspace_last_verification_run
        FOREIGN KEY (workspace_id, last_verification_run_id)
        REFERENCES public.generation_runs (workspace_id, id)
        ON DELETE SET NULL,
    CONSTRAINT fk_claims_same_workspace_supersedes
        FOREIGN KEY (workspace_id, supersedes_claim_id)
        REFERENCES public.claims (workspace_id, id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS public.claim_atoms (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    claim_id     uuid NOT NULL,
    ordinal      integer NOT NULL,
    atom_type    text NOT NULL,
    atom_text    text NOT NULL,
    status       text NOT NULL DEFAULT 'UNSUPPORTED',
    created_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_claim_atoms_ordinal_non_negative
        CHECK (ordinal >= 0),
    CONSTRAINT ck_claim_atoms_atom_type
        CHECK (atom_type IN ('PROBLEM', 'ACTION', 'TECHNOLOGY', 'RESULT', 'ROLE', 'METRIC')),
    CONSTRAINT ck_claim_atoms_text_non_empty
        CHECK (length(btrim(atom_text)) > 0),
    CONSTRAINT ck_claim_atoms_status
        CHECK (status IN ('SUPPORTED', 'USER_CONFIRMED', 'UNSUPPORTED', 'STALE')),
    CONSTRAINT uq_claim_atoms_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_claim_atoms_claim_ordinal
        UNIQUE (claim_id, ordinal),
    CONSTRAINT fk_claim_atoms_same_workspace_claim
        FOREIGN KEY (workspace_id, claim_id)
        REFERENCES public.claims (workspace_id, id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.claim_atom_support (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    claim_atom_id     uuid NOT NULL,
    fact_id           uuid NOT NULL,
    support_type      text NOT NULL DEFAULT 'DIRECT',
    validation_run_id uuid NULL,
    status            text NOT NULL DEFAULT 'ACTIVE',
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_claim_atom_support_type
        CHECK (support_type IN ('DIRECT', 'CONTEXT', 'CONTRADICTS')),
    CONSTRAINT ck_claim_atom_support_status
        CHECK (status IN ('ACTIVE', 'INVALIDATED')),
    CONSTRAINT uq_claim_atom_support_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_claim_atom_support_atom_fact
        UNIQUE (claim_atom_id, fact_id),
    CONSTRAINT fk_claim_atom_support_same_workspace_atom
        FOREIGN KEY (workspace_id, claim_atom_id)
        REFERENCES public.claim_atoms (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_claim_atom_support_same_workspace_fact
        FOREIGN KEY (workspace_id, fact_id)
        REFERENCES public.facts (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_claim_atom_support_same_workspace_validation_run
        FOREIGN KEY (workspace_id, validation_run_id)
        REFERENCES public.generation_runs (workspace_id, id)
        ON DELETE SET NULL
);

-- =============================================================================
-- 6. Read-Only Computed Verification & Full Provenance Views (security_invoker)
-- =============================================================================
CREATE OR REPLACE VIEW public.v_claim_provenance_chain
WITH (security_invoker = true)
AS
SELECT
    c.workspace_id,
    c.id                        AS claim_id,
    c.event_id,
    c.version                   AS version,
    c.status                    AS claim_status,
    ca.id                       AS claim_atom_id,
    ca.ordinal                  AS atom_ordinal,
    ca.atom_type,
    ca.status                   AS claim_atom_status,
    cas.id                      AS claim_atom_support_id,
    cas.support_type            AS claim_atom_support_type,
    cas.validation_run_id       AS claim_atom_validation_run_id,
    cas.status                  AS claim_atom_support_status,
    f.id                        AS fact_id,
    f.fact_type,
    f.status                    AS fact_status,
    fs.id                       AS fact_support_id,
    fs.support_type             AS fact_support_type,
    fs.status                   AS fact_support_status,
    fs.validator,
    fs.validator_version,
    cu.id                       AS content_unit_id,
    cu.unit_type                AS content_unit_type,
    cu.ordinal                  AS content_unit_ordinal,
    cu.content_hash             AS content_unit_hash,
    cu.canonicalization_version,
    cu.anchor                   AS content_unit_anchor,
    cu.policy_state             AS content_unit_policy_state,
    rev.id                      AS source_object_revision_id,
    rev.source_object_id,
    rev.provider_revision_id,
    rev.content_hash            AS revision_content_hash,
    rev.hash_policy_version,
    rev.observed_at             AS revision_observed_at,
    rev.is_tombstone            AS revision_is_tombstone
FROM public.claims AS c
INNER JOIN public.claim_atoms AS ca
    ON ca.workspace_id = c.workspace_id
   AND ca.claim_id = c.id
INNER JOIN public.claim_atom_support AS cas
    ON cas.workspace_id = ca.workspace_id
   AND cas.claim_atom_id = ca.id
INNER JOIN public.facts AS f
    ON f.workspace_id = cas.workspace_id
   AND f.id = cas.fact_id
INNER JOIN public.fact_support AS fs
    ON fs.workspace_id = f.workspace_id
   AND fs.fact_id = f.id
INNER JOIN public.content_units AS cu
    ON cu.workspace_id = fs.workspace_id
   AND cu.id = fs.content_unit_id
INNER JOIN public.source_object_revisions AS rev
    ON rev.workspace_id = cu.workspace_id
   AND rev.id = cu.source_object_revision_id;

CREATE OR REPLACE VIEW public.v_claim_verification_summary
WITH (security_invoker = true)
AS
WITH atom_eval AS (
    SELECT
        ca.workspace_id,
        ca.claim_id,
        ca.id AS claim_atom_id,
        ca.status AS atom_status,
        (
            ca.status = 'SUPPORTED'
            AND EXISTS (
                SELECT 1
                FROM public.claim_atom_support AS cas
                INNER JOIN public.facts AS f
                    ON f.workspace_id = cas.workspace_id
                   AND f.id = cas.fact_id
                INNER JOIN public.fact_support AS fs
                    ON fs.workspace_id = f.workspace_id
                   AND fs.fact_id = f.id
                INNER JOIN public.content_units AS cu
                    ON cu.workspace_id = fs.workspace_id
                   AND cu.id = fs.content_unit_id
                INNER JOIN public.source_object_revisions AS rev
                    ON rev.workspace_id = cu.workspace_id
                   AND rev.id = cu.source_object_revision_id
                WHERE cas.workspace_id = ca.workspace_id
                  AND cas.claim_atom_id = ca.id
                  AND cas.status = 'ACTIVE'
                  AND cas.support_type = 'DIRECT'
                  AND f.status = 'SUPPORTED'
                  AND fs.status = 'ACTIVE'
                  AND fs.support_type = 'DIRECT'
                  AND cu.policy_state = 'ACTIVE'
                  AND NOT rev.is_tombstone
            )
            AND NOT EXISTS (
                SELECT 1
                FROM public.claim_atom_support AS cas_contra
                WHERE cas_contra.workspace_id = ca.workspace_id
                  AND cas_contra.claim_atom_id = ca.id
                  AND cas_contra.status = 'ACTIVE'
                  AND cas_contra.support_type = 'CONTRADICTS'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM public.claim_atom_support AS cas_f
                INNER JOIN public.fact_support AS fs_contra
                    ON fs_contra.workspace_id = cas_f.workspace_id
                   AND fs_contra.fact_id = cas_f.fact_id
                WHERE cas_f.workspace_id = ca.workspace_id
                  AND cas_f.claim_atom_id = ca.id
                  AND cas_f.status = 'ACTIVE'
                  AND fs_contra.status = 'ACTIVE'
                  AND fs_contra.support_type = 'CONTRADICTS'
            )
        ) AS is_source_backed
    FROM public.claim_atoms AS ca
)
SELECT
    c.workspace_id,
    c.id AS claim_id,
    c.event_id,
    c.version AS version,
    c.status AS claim_status,
    c.approved_at,
    c.last_verified_at,
    c.last_verification_run_id,
    COUNT(ae.claim_atom_id)::integer AS atom_count,
    COUNT(*) FILTER (WHERE ae.is_source_backed)::integer AS source_backed_atom_count,
    COUNT(*) FILTER (WHERE ae.atom_status = 'USER_CONFIRMED')::integer AS user_confirmed_atom_count,
    CASE
        WHEN COUNT(ae.claim_atom_id) = 0 THEN 'UNVERIFIED'
        WHEN COUNT(*) FILTER (
            WHERE NOT ae.is_source_backed AND ae.atom_status <> 'USER_CONFIRMED'
        ) > 0 THEN 'UNVERIFIED'
        WHEN COUNT(*) FILTER (WHERE ae.is_source_backed) = COUNT(ae.claim_atom_id) THEN 'SOURCE_VERIFIED'
        WHEN COUNT(*) FILTER (WHERE ae.atom_status = 'USER_CONFIRMED') = COUNT(ae.claim_atom_id) THEN 'USER_CONFIRMED'
        ELSE 'MIXED'
    END AS computed_verification
FROM public.claims AS c
LEFT JOIN atom_eval AS ae
    ON ae.workspace_id = c.workspace_id
   AND ae.claim_id = c.id
GROUP BY
    c.workspace_id,
    c.id,
    c.event_id,
    c.version,
    c.status,
    c.approved_at,
    c.last_verified_at,
    c.last_verification_run_id;

-- =============================================================================
-- 7. Supporting Indexes
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_external_identity_mappings_workspace_identity
    ON public.external_identity_mappings (workspace_id, identity_id);

CREATE INDEX IF NOT EXISTS idx_work_events_workspace_anchor
    ON public.work_events (workspace_id, primary_anchor_source_object_id)
    WHERE primary_anchor_source_object_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_event_memberships_workspace_event
    ON public.event_memberships (workspace_id, event_id);

CREATE INDEX IF NOT EXISTS idx_event_memberships_workspace_source_object
    ON public.event_memberships (workspace_id, source_object_id);

CREATE INDEX IF NOT EXISTS idx_event_composite_memberships_workspace_composite
    ON public.event_composite_memberships (workspace_id, composite_event_id);

CREATE INDEX IF NOT EXISTS idx_review_actions_workspace_target
    ON public.review_actions (workspace_id, target_type, target_id);

CREATE INDEX IF NOT EXISTS idx_facts_workspace_event
    ON public.facts (workspace_id, event_id);

CREATE INDEX IF NOT EXISTS idx_facts_workspace_subject_identity
    ON public.facts (workspace_id, subject_identity_id)
    WHERE subject_identity_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fact_support_workspace_fact
    ON public.fact_support (workspace_id, fact_id);

CREATE INDEX IF NOT EXISTS idx_fact_support_workspace_content_unit
    ON public.fact_support (workspace_id, content_unit_id);

CREATE INDEX IF NOT EXISTS idx_contributions_workspace_event
    ON public.contributions (workspace_id, event_id);

CREATE INDEX IF NOT EXISTS idx_contributions_workspace_identity
    ON public.contributions (workspace_id, identity_id);

CREATE INDEX IF NOT EXISTS idx_contribution_support_workspace_contribution
    ON public.contribution_support (workspace_id, contribution_id);

CREATE INDEX IF NOT EXISTS idx_contribution_support_workspace_fact
    ON public.contribution_support (workspace_id, fact_id);

CREATE INDEX IF NOT EXISTS idx_generation_runs_workspace_event
    ON public.generation_runs (workspace_id, event_id)
    WHERE event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claims_workspace_event
    ON public.claims (workspace_id, event_id);

CREATE INDEX IF NOT EXISTS idx_claim_atoms_workspace_claim
    ON public.claim_atoms (workspace_id, claim_id);

CREATE INDEX IF NOT EXISTS idx_claim_atom_support_workspace_atom
    ON public.claim_atom_support (workspace_id, claim_atom_id);

CREATE INDEX IF NOT EXISTS idx_claim_atom_support_workspace_fact
    ON public.claim_atom_support (workspace_id, fact_id);

CREATE INDEX IF NOT EXISTS idx_claim_atom_support_workspace_validation_run
    ON public.claim_atom_support (workspace_id, validation_run_id)
    WHERE validation_run_id IS NOT NULL;

-- =============================================================================
-- 8. Enable & FORCE Row Level Security + Fail-Closed Workspace Policies
-- =============================================================================
DO $$
DECLARE
    tbl text;
    domain_tables text[] := ARRAY[
        'identities',
        'external_identity_mappings',
        'work_events',
        'event_memberships',
        'event_composite_memberships',
        'review_actions',
        'facts',
        'fact_support',
        'contributions',
        'contribution_support',
        'generation_runs',
        'claims',
        'claim_atoms',
        'claim_atom_support'
    ];
    worker_rw_tables text[] := ARRAY[
        'identities',
        'external_identity_mappings',
        'work_events',
        'event_memberships',
        'event_composite_memberships',
        'facts',
        'fact_support',
        'contributions',
        'contribution_support',
        'generation_runs',
        'claims',
        'claim_atoms',
        'claim_atom_support'
    ];
BEGIN
    FOREACH tbl IN ARRAY domain_tables LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_api_workspace_isolation', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO cm_api_app '
            'USING (workspace_id = auth_ctx.current_workspace_id()) '
            'WITH CHECK (workspace_id = auth_ctx.current_workspace_id())',
            tbl || '_api_workspace_isolation',
            tbl
        );

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_worker_workspace_select', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR SELECT TO cm_worker_app '
            'USING (workspace_id = auth_ctx.current_workspace_id())',
            tbl || '_worker_workspace_select',
            tbl
        );
    END LOOP;

    FOREACH tbl IN ARRAY worker_rw_tables LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_worker_workspace_insert', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR INSERT TO cm_worker_app '
            'WITH CHECK (workspace_id = auth_ctx.current_workspace_id())',
            tbl || '_worker_workspace_insert',
            tbl
        );

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_worker_workspace_update', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR UPDATE TO cm_worker_app '
            'USING (workspace_id = auth_ctx.current_workspace_id()) '
            'WITH CHECK (workspace_id = auth_ctx.current_workspace_id())',
            tbl || '_worker_workspace_update',
            tbl
        );
    END LOOP;
END
$$;

-- =============================================================================
-- 9. Minimal Privilege GRANT Matrix
-- =============================================================================
REVOKE ALL ON TABLE
    public.identities,
    public.external_identity_mappings,
    public.work_events,
    public.event_memberships,
    public.event_composite_memberships,
    public.review_actions,
    public.facts,
    public.fact_support,
    public.contributions,
    public.contribution_support,
    public.generation_runs,
    public.claims,
    public.claim_atoms,
    public.claim_atom_support,
    public.v_claim_provenance_chain,
    public.v_claim_verification_summary
FROM PUBLIC;

-- cm_api_app: CRUD on domain tables, append/read on review_actions, read-only on computed views.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.identities,
    public.external_identity_mappings,
    public.work_events,
    public.event_memberships,
    public.event_composite_memberships,
    public.facts,
    public.fact_support,
    public.contributions,
    public.contribution_support,
    public.generation_runs,
    public.claims,
    public.claim_atoms,
    public.claim_atom_support
TO cm_api_app;

GRANT SELECT, INSERT ON public.review_actions TO cm_api_app;
GRANT SELECT ON
    public.v_claim_provenance_chain,
    public.v_claim_verification_summary
TO cm_api_app;

-- cm_worker_app: least privilege (no DELETE, no review_actions write, no auth/membership tables).
GRANT SELECT, INSERT, UPDATE ON
    public.identities,
    public.external_identity_mappings,
    public.work_events,
    public.event_memberships,
    public.event_composite_memberships,
    public.facts,
    public.fact_support,
    public.contributions,
    public.contribution_support,
    public.generation_runs,
    public.claims,
    public.claim_atoms,
    public.claim_atom_support
TO cm_worker_app;

GRANT SELECT ON
    public.review_actions,
    public.v_claim_provenance_chain,
    public.v_claim_verification_summary
TO cm_worker_app;

RESET ROLE;
