CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id VARCHAR PRIMARY KEY CHECK (length(account_id) = 26),
    name VARCHAR NOT NULL UNIQUE,
    base_currency VARCHAR NOT NULL DEFAULT 'CNY',
    initial_cash DECIMAL(20, 4) NOT NULL CHECK (initial_cash > 0),
    cash_balance DECIMAL(20, 4) NOT NULL CHECK (cash_balance >= 0),
    status VARCHAR NOT NULL CHECK (status IN ('ACTIVE', 'FROZEN', 'RESET')),
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id VARCHAR PRIMARY KEY CHECK (length(order_id) = 26),
    account_id VARCHAR NOT NULL CHECK (length(account_id) = 26),
    signal_id VARCHAR,
    symbol VARCHAR NOT NULL,
    side VARCHAR NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    order_type VARCHAR NOT NULL CHECK (order_type IN ('MARKET_AT_NEXT_OPEN', 'MANUAL')),
    status VARCHAR NOT NULL CHECK (
        status IN ('PROPOSED', 'CONFIRMED', 'FILLED', 'CANCELLED', 'REJECTED')
    ),
    idempotency_key VARCHAR NOT NULL UNIQUE,
    proposed_price DECIMAL(20, 6),
    reject_reason VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_account ON paper_orders (account_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders (status);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id VARCHAR PRIMARY KEY CHECK (length(fill_id) = 26),
    order_id VARCHAR NOT NULL UNIQUE CHECK (length(order_id) = 26),
    trade_date DATE NOT NULL,
    fill_time TIMESTAMPTZ NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    price DECIMAL(20, 6) NOT NULL CHECK (price > 0),
    commission DECIMAL(20, 4) NOT NULL CHECK (commission >= 0),
    slippage_cost DECIMAL(20, 4) NOT NULL DEFAULT 0 CHECK (slippage_cost >= 0),
    other_cost DECIMAL(20, 4) NOT NULL DEFAULT 0 CHECK (other_cost >= 0),
    cash_delta DECIMAL(20, 4) NOT NULL,
    source VARCHAR NOT NULL CHECK (source IN ('NEXT_OPEN', 'MANUAL')),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_fills_trade_date ON paper_fills (trade_date);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id VARCHAR NOT NULL CHECK (length(account_id) = 26),
    symbol VARCHAR NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    available_quantity BIGINT NOT NULL CHECK (available_quantity >= 0),
    pending_quantity BIGINT NOT NULL DEFAULT 0 CHECK (pending_quantity >= 0),
    pending_date DATE,
    average_cost DECIMAL(20, 6) NOT NULL DEFAULT 0 CHECK (average_cost >= 0),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, symbol),
    CHECK (available_quantity + pending_quantity = quantity)
);

CREATE TABLE IF NOT EXISTS nav_snapshots (
    snapshot_id VARCHAR PRIMARY KEY CHECK (length(snapshot_id) = 26),
    account_id VARCHAR NOT NULL CHECK (length(account_id) = 26),
    trade_date DATE NOT NULL,
    cash DECIMAL(20, 4) NOT NULL CHECK (cash >= 0),
    market_value DECIMAL(20, 4) NOT NULL CHECK (market_value >= 0),
    total_equity DECIMAL(20, 4) NOT NULL CHECK (total_equity >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, trade_date)
);
