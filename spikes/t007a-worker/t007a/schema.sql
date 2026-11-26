-- T-007A PROTOTYPE SCHEMA. NOT the product/operational schema.
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
                     CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
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
                CHECK (outcome IN ('RUNNING', 'SUCCEEDED', 'LEASE_LOST', 'FAILED')),
    error_class text,
    claimed_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (job_id, attempt_no)
);
