CREATE TABLE IF NOT EXISTS quality_reports (
    report_id VARCHAR PRIMARY KEY CHECK (length(report_id) = 26),
    batch_id VARCHAR NOT NULL CHECK (length(batch_id) = 26),
    ruleset_version VARCHAR NOT NULL,
    gate_status VARCHAR NOT NULL CHECK (
        gate_status IN ('PASSED', 'PASSED_WITH_WARNINGS', 'FAILED')
    ),
    checked_rows BIGINT NOT NULL DEFAULT 0 CHECK (checked_rows >= 0),
    blocking_count INTEGER NOT NULL DEFAULT 0 CHECK (blocking_count >= 0),
    error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    warning_count INTEGER NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
    info_count INTEGER NOT NULL DEFAULT 0 CHECK (info_count >= 0),
    generated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_reports_batch ON quality_reports (batch_id);
CREATE INDEX IF NOT EXISTS idx_quality_reports_status ON quality_reports (gate_status);

CREATE TABLE IF NOT EXISTS quality_issues (
    issue_id VARCHAR PRIMARY KEY CHECK (length(issue_id) = 26),
    report_id VARCHAR NOT NULL CHECK (length(report_id) = 26),
    batch_id VARCHAR NOT NULL CHECK (length(batch_id) = 26),
    rule_code VARCHAR NOT NULL,
    severity VARCHAR NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'BLOCKING')),
    symbol VARCHAR,
    trade_date DATE,
    observed_value JSON NOT NULL DEFAULT '{}',
    expected_value JSON NOT NULL DEFAULT '{}',
    status VARCHAR NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ACCEPTED', 'RESOLVED')),
    message VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_quality_issues_report ON quality_issues (report_id);
CREATE INDEX IF NOT EXISTS idx_quality_issues_batch ON quality_issues (batch_id);
CREATE INDEX IF NOT EXISTS idx_quality_issues_rule ON quality_issues (rule_code);
CREATE INDEX IF NOT EXISTS idx_quality_issues_severity ON quality_issues (severity);

CREATE OR REPLACE VIEW v_quality_gate AS
SELECT
    batch_id,
    report_id,
    gate_status,
    blocking_count,
    error_count,
    warning_count,
    info_count,
    generated_at
FROM quality_reports;
