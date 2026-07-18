CREATE TABLE IF NOT EXISTS task_runs (
    task_run_id VARCHAR PRIMARY KEY CHECK (length(task_run_id) = 26),
    task_name VARCHAR NOT NULL,
    scheduled_for TIMESTAMPTZ,
    status VARCHAR NOT NULL CHECK (
        status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'BLOCKED')
    ),
    lock_key VARCHAR,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    result_summary JSON NOT NULL DEFAULT '{}',
    error_code VARCHAR,
    error_message VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_task_runs_name ON task_runs (task_name);
CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs (status);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id VARCHAR PRIMARY KEY CHECK (length(event_id) = 26),
    task_run_id VARCHAR,
    event_type VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_id VARCHAR,
    severity VARCHAR NOT NULL CHECK (severity IN ('INFO', 'WARN', 'ERROR')),
    payload JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_task ON audit_events (task_run_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events (event_type);
