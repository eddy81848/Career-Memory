-- T-007A / T-007B / T-007C PROTOTYPE SCHEMA. NOT the product/operational schema.
-- Lives in its own schema so it can be dropped wholesale in throwaway databases.
DROP SCHEMA IF EXISTS t007a_spike CASCADE;
CREATE SCHEMA t007a_spike;
SET search_path = t007a_spike;

CREATE TABLE jobs (
    id               uuid PRIMARY KEY,
    -- One logical job per idempotency key. This is what makes redelivery converge.
    idempotency_key  text        NOT NULL UNIQUE,
    request_hash     text        NOT NULL,
    payload          jsonb       NOT NULL,
    status           text        NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REVIEW_REQUIRED')),
    terminal_reason  text,
    attempt_count    integer     NOT NULL DEFAULT 0,
    max_attempts     integer     NOT NULL DEFAULT 3,
    lease_owner      text,
    lease_token      uuid,
    lease_expires_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
CREATE INDEX jobs_claimable_idx ON jobs (created_at) WHERE status IN ('PENDING', 'RUNNING');

-- Written in the SAME transaction as the job. The external wake is only a hint;
-- this row is the durable "someone must process job X" record used for recovery.
CREATE TABLE outbox (
    job_id            uuid PRIMARY KEY REFERENCES jobs (id),
    topic             text        NOT NULL DEFAULT 'worker.wake',
    status            text        NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING', 'DISPATCHED', 'DONE')),
    dispatch_attempts integer     NOT NULL DEFAULT 0,
    recovery_attempts integer     NOT NULL DEFAULT 0,
    last_error        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    dispatched_at     timestamptz,
    last_recovery_at  timestamptz,
    done_at           timestamptz
);

-- PRIMARY KEY (job_id) is the backstop: at most one stored result per logical job.
CREATE TABLE results (
    job_id     uuid PRIMARY KEY REFERENCES jobs (id),
    attempt_no integer     NOT NULL,
    worker_id  text        NOT NULL,
    output     jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Processing record: one row per claim, queryable through the API.
CREATE TABLE job_attempts (
    job_id      uuid        NOT NULL REFERENCES jobs (id),
    attempt_no  integer     NOT NULL,
    worker_id   text        NOT NULL,
    lease_token uuid        NOT NULL,
    outcome     text        NOT NULL DEFAULT 'RUNNING'
                CHECK (outcome IN ('RUNNING', 'SUCCEEDED', 'LEASE_LOST', 'FAILED', 'EXPIRED', 'AMBIGUOUS_TIMEOUT')),
    error_class text,
    claimed_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (job_id, attempt_no)
);

-- =============================================================================
-- T-007B PROTOTYPE TABLES (single-repository initial import: page + checkpoint
-- atomicity, lease/checkpoint fencing, safe resume, single IMPORT_READY intent).
-- NOT the product operational schema or migration.
-- =============================================================================

CREATE TABLE import_runs (
    run_id           uuid PRIMARY KEY,
    job_id           uuid        NOT NULL UNIQUE REFERENCES jobs (id),
    repo_ref         text        NOT NULL,
    plan_id          text        NOT NULL,
    plan_fingerprint text        NOT NULL,
    required_streams text[]      NOT NULL CHECK (cardinality(required_streams) >= 1),
    status           text        NOT NULL DEFAULT 'RUNNING'
                     CHECK (status IN ('RUNNING', 'PARTIAL', 'SUCCEEDED', 'FAILED')),
    failure_reason   text,
    source_drift     boolean     NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);

CREATE TABLE import_stream_checkpoints (
    run_id             uuid        NOT NULL REFERENCES import_runs (run_id),
    stream_type        text        NOT NULL,
    status             text        NOT NULL DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'BLOCKED')),
    cursor             text,
    safe_checkpoint    boolean     NOT NULL DEFAULT false,
    checkpoint_version integer     NOT NULL DEFAULT 0,
    pages_committed    integer     NOT NULL DEFAULT 0,
    items_committed    integer     NOT NULL DEFAULT 0,
    updated_by_attempt integer,
    updated_by_worker  text,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, stream_type)
);

-- Page item deduplication backstop: (run_id, stream_type, external_id) is unique.
CREATE TABLE import_page_items (
    run_id             uuid        NOT NULL REFERENCES import_runs (run_id),
    stream_type        text        NOT NULL,
    external_id        text        NOT NULL,
    page_cursor        text        NOT NULL,
    checkpoint_version integer     NOT NULL,
    attempt_no         integer     NOT NULL,
    worker_id          text        NOT NULL,
    body               jsonb       NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, stream_type, external_id),
    FOREIGN KEY (run_id, stream_type)
        REFERENCES import_stream_checkpoints (run_id, stream_type)
);

-- Single-intent handoff record emitted ONLY when every required stream is COMPLETED.
-- PRIMARY KEY (run_id) guarantees at most 1 IMPORT_READY row even under completion races.
CREATE TABLE import_ready_outbox (
    run_id      uuid PRIMARY KEY REFERENCES import_runs (run_id),
    plan_id     text        NOT NULL,
    intent_type text        NOT NULL DEFAULT 'IMPORT_READY'
                CHECK (intent_type = 'IMPORT_READY'),
    attempt_no  integer     NOT NULL,
    worker_id   text        NOT NULL,
    total_items integer     NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- =============================================================================
-- T-007E PROTOTYPE TABLES (Option C: live source re-fetch + hash/locator check
-- at Approval and Export WITHOUT persisting raw source text).
-- NOT the product operational schema, NOT B-004 completion.
-- =============================================================================

CREATE TABLE source_bindings (
    source_id       uuid PRIMARY KEY,
    repo_ref        text        NOT NULL UNIQUE,
    auth_status     text        NOT NULL DEFAULT 'ACTIVE'
                    CHECK (auth_status IN ('ACTIVE', 'REVOKED')),
    policy_status   text        NOT NULL DEFAULT 'ALLOWED'
                    CHECK (policy_status IN ('ALLOWED', 'BLOCKED_POLICY')),
    reachability    text        NOT NULL DEFAULT 'OK'
                    CHECK (reachability IN ('OK', 'UNVERIFIED_404', 'UNAVAILABLE')),
    binding_version integer     NOT NULL DEFAULT 1,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE career_claims (
    claim_id          uuid PRIMARY KEY,
    claim_text        text        NOT NULL,
    status            text        NOT NULL DEFAULT 'DRAFT'
                      CHECK (status IN ('DRAFT', 'APPROVED')),
    claim_version     integer     NOT NULL DEFAULT 1,
    approved_version  integer,
    last_check_status text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Stores ONLY revision identifier, locator, and expected SHA-256 digest.
-- Raw source text / code / diffs MUST NEVER be stored here.
CREATE TABLE claim_evidence_refs (
    ref_id        uuid PRIMARY KEY,
    claim_id      uuid        NOT NULL REFERENCES career_claims (claim_id),
    source_id     uuid        NOT NULL REFERENCES source_bindings (source_id),
    revision_id   text        NOT NULL,
    locator       text        NOT NULL,
    expected_hash text        NOT NULL CHECK (length(expected_hash) = 64),
    required      boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE claim_approvals (
    approval_id            uuid PRIMARY KEY,
    claim_id               uuid        NOT NULL REFERENCES career_claims (claim_id),
    claim_version          integer     NOT NULL,
    verified_binding_state jsonb       NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (claim_id, claim_version)
);

CREATE TABLE claim_exports (
    export_id      uuid PRIMARY KEY,
    claim_id       uuid        NOT NULL REFERENCES career_claims (claim_id),
    claim_version  integer     NOT NULL,
    export_status  text        NOT NULL DEFAULT 'PUBLISHED'
                   CHECK (export_status IN ('PUBLISHED')),
    manifest       jsonb       NOT NULL,
    published_at   timestamptz NOT NULL DEFAULT now()
);

-- Audit log of blocked/passed verification attempts (status codes & hashes ONLY, never raw source).
CREATE TABLE verification_events (
    event_id      uuid PRIMARY KEY,
    gate          text        NOT NULL CHECK (gate IN ('APPROVAL', 'EXPORT')),
    claim_id      uuid        NOT NULL REFERENCES career_claims (claim_id),
    claim_version integer     NOT NULL,
    outcome       text        NOT NULL,
    reason_code   text        NOT NULL,
    observed_hash text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
