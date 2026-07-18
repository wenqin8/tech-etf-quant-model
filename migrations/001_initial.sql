CREATE TABLE IF NOT EXISTS instruments (
    symbol VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    exchange VARCHAR NOT NULL CHECK (exchange IN ('SSE', 'SZSE')),
    asset_class VARCHAR NOT NULL DEFAULT 'ETF' CHECK (asset_class = 'ETF'),
    status VARCHAR NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DELISTED', 'DISABLED')),
    category VARCHAR NOT NULL,
    benchmark_symbol VARCHAR,
    lot_size INTEGER NOT NULL DEFAULT 100 CHECK (lot_size > 0),
    price_tick DECIMAL(12, 6) NOT NULL CHECK (price_tick > 0),
    list_date DATE,
    delist_date DATE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_version VARCHAR NOT NULL,
    metadata JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (delist_date IS NULL OR list_date IS NULL OR delist_date >= list_date)
);

CREATE INDEX IF NOT EXISTS idx_instruments_category ON instruments (category);
CREATE INDEX IF NOT EXISTS idx_instruments_active ON instruments (active);

CREATE TABLE IF NOT EXISTS data_batches (
    batch_id VARCHAR PRIMARY KEY CHECK (length(batch_id) = 26),
    provider VARCHAR NOT NULL CHECK (provider IN ('TUSHARE', 'AKSHARE', 'CSV')),
    dataset VARCHAR NOT NULL,
    requested_start DATE,
    requested_end DATE,
    fetched_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL CHECK (
        status IN ('FETCHING', 'VALIDATING', 'ACTIVE', 'REJECTED', 'SUPERSEDED')
    ),
    row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    file_count INTEGER NOT NULL DEFAULT 0 CHECK (file_count >= 0),
    checksum VARCHAR,
    schema_version VARCHAR NOT NULL,
    parent_batch_id VARCHAR,
    error_summary VARCHAR,
    metadata JSON NOT NULL DEFAULT '{}',
    CHECK (requested_end IS NULL OR requested_start IS NULL OR requested_end >= requested_start),
    CHECK (parent_batch_id IS NULL OR length(parent_batch_id) = 26)
);

CREATE INDEX IF NOT EXISTS idx_data_batches_provider_dataset
    ON data_batches (provider, dataset);
CREATE INDEX IF NOT EXISTS idx_data_batches_status ON data_batches (status);

CREATE TABLE IF NOT EXISTS trading_calendar (
    exchange VARCHAR NOT NULL CHECK (exchange IN ('SSE', 'SZSE')),
    cal_date DATE NOT NULL,
    is_open BOOLEAN NOT NULL,
    previous_open_date DATE,
    next_open_date DATE,
    source VARCHAR NOT NULL CHECK (source IN ('TUSHARE', 'AKSHARE', 'CSV')),
    batch_id VARCHAR NOT NULL CHECK (length(batch_id) = 26),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (exchange, cal_date),
    CHECK (previous_open_date IS NULL OR previous_open_date < cal_date),
    CHECK (next_open_date IS NULL OR next_open_date > cal_date)
);

CREATE INDEX IF NOT EXISTS idx_trading_calendar_open
    ON trading_calendar (exchange, is_open, cal_date);

CREATE TABLE IF NOT EXISTS data_files (
    -- batch_id references data_batches.batch_id logically; the constraint is enforced in the
    -- application layer because DuckDB forbids updating a parent row that a child still references,
    -- which every batch status transition requires.
    batch_id VARCHAR NOT NULL CHECK (length(batch_id) = 26),
    layer VARCHAR NOT NULL CHECK (layer IN ('RAW', 'CANONICAL', 'DERIVED', 'ARTIFACT')),
    dataset VARCHAR NOT NULL,
    file_path VARCHAR NOT NULL,
    checksum VARCHAR NOT NULL CHECK (length(checksum) = 64),
    row_count BIGINT NOT NULL CHECK (row_count >= 0),
    schema_version VARCHAR NOT NULL,
    min_trade_date DATE,
    max_trade_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (batch_id, layer, file_path),
    CHECK (
        max_trade_date IS NULL
        OR min_trade_date IS NULL
        OR max_trade_date >= min_trade_date
    )
);

CREATE INDEX IF NOT EXISTS idx_data_files_dataset_layer
    ON data_files (dataset, layer);

CREATE OR REPLACE VIEW v_active_daily_bar_files AS
SELECT
    files.batch_id,
    files.file_path,
    files.checksum,
    files.row_count,
    files.schema_version,
    files.min_trade_date,
    files.max_trade_date,
    batches.fetched_at
FROM data_files AS files
JOIN data_batches AS batches ON batches.batch_id = files.batch_id
WHERE files.layer = 'CANONICAL'
  AND files.dataset = 'daily_bars'
  AND batches.status = 'ACTIVE';

CREATE OR REPLACE VIEW v_daily_bars AS
SELECT
    CAST(NULL AS VARCHAR) AS symbol,
    CAST(NULL AS DATE) AS trade_date,
    CAST(NULL AS VARCHAR) AS exchange,
    CAST(NULL AS DECIMAL(20, 6)) AS open,
    CAST(NULL AS DECIMAL(20, 6)) AS high,
    CAST(NULL AS DECIMAL(20, 6)) AS low,
    CAST(NULL AS DECIMAL(20, 6)) AS close,
    CAST(NULL AS DECIMAL(20, 6)) AS pre_close,
    CAST(NULL AS DECIMAL(24, 4)) AS volume,
    CAST(NULL AS DECIMAL(24, 4)) AS amount,
    CAST(NULL AS DECIMAL(20, 8)) AS adj_factor,
    CAST(NULL AS BOOLEAN) AS is_suspended,
    CAST(NULL AS VARCHAR) AS source,
    CAST(NULL AS VARCHAR) AS batch_id,
    CAST(NULL AS TIMESTAMPTZ) AS ingested_at
WHERE FALSE;
