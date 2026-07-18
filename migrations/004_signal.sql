CREATE TABLE IF NOT EXISTS signals (
    signal_id VARCHAR PRIMARY KEY CHECK (length(signal_id) = 26),
    trade_date DATE NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    strategy_id VARCHAR NOT NULL,
    strategy_version VARCHAR NOT NULL,
    parameter_hash VARCHAR NOT NULL,
    universe_hash VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL UNIQUE,
    status VARCHAR NOT NULL CHECK (status IN ('VALID', 'BLOCKED')),
    risk_state VARCHAR NOT NULL CHECK (risk_state IN ('NORMAL', 'WARNING', 'BLOCKED')),
    target_cash_weight DECIMAL(8, 6) NOT NULL CHECK (
        target_cash_weight >= 0 AND target_cash_weight <= 1
    ),
    data_as_of DATE NOT NULL,
    dataset_id VARCHAR,
    quality_report_id VARCHAR,
    blocked_reason VARCHAR,
    warnings JSON NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_signals_trade_date ON signals (trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);

CREATE TABLE IF NOT EXISTS target_positions (
    target_id VARCHAR PRIMARY KEY CHECK (length(target_id) = 26),
    signal_id VARCHAR NOT NULL CHECK (length(signal_id) = 26),
    symbol VARCHAR NOT NULL,
    action VARCHAR NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD')),
    current_weight DECIMAL(8, 6) NOT NULL CHECK (current_weight >= 0),
    target_weight DECIMAL(8, 6) NOT NULL CHECK (target_weight >= 0 AND target_weight <= 1),
    weight_delta DECIMAL(9, 6) NOT NULL,
    reference_close DECIMAL(20, 6),
    score DECIMAL(20, 8),
    reason_codes JSON NOT NULL DEFAULT '[]',
    UNIQUE (signal_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_target_positions_signal ON target_positions (signal_id);
