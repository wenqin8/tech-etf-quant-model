CREATE TABLE IF NOT EXISTS strategy_definitions (
    strategy_id VARCHAR PRIMARY KEY CHECK (length(strategy_id) = 26),
    strategy_key VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    parameter_schema JSON NOT NULL,
    code_hash VARCHAR NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (strategy_key, version)
);

CREATE INDEX IF NOT EXISTS idx_strategy_definitions_key ON strategy_definitions (strategy_key);
CREATE INDEX IF NOT EXISTS idx_strategy_definitions_active ON strategy_definitions (active);
