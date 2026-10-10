-- CW-22 domain tables. Every table is keyed by `retailer` so a second retailer is a new value, not a migration.
-- Times are ISO-8601 UTC strings.

CREATE TABLE products (
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (retailer, product_id)
);

CREATE TABLE tracked_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    label TEXT,
    source TEXT NOT NULL CHECK (source IN ('manual', 'cart', 'receipt', 'order', 'email')),
    target_price REAL,
    discount_threshold REAL,
    auto_add INTEGER NOT NULL DEFAULT 0,
    snoozed_until TEXT,
    created_at TEXT NOT NULL,
    archived_at TEXT,
    UNIQUE (retailer, product_id),
    FOREIGN KEY (retailer, product_id) REFERENCES products (retailer, product_id)
);

CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price REAL,
    was_price REAL,
    on_special INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 1,
    unit_price_value REAL,
    unit_price_unit TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'refresh',
    run_id TEXT
);
CREATE INDEX ix_observations_item_time ON observations (retailer, product_id, observed_at);

CREATE TABLE cart_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    retailer TEXT NOT NULL,
    items TEXT NOT NULL,
    subtotal REAL
);
CREATE INDEX ix_cart_snapshots_time ON cart_snapshots (retailer, taken_at);

CREATE TABLE shop_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer TEXT NOT NULL,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    closed_at TEXT,
    items TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE candidate_decisions (
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('dismissed', 'tracked')),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (retailer, product_id)
);

CREATE TABLE sale_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    low_price REAL,
    last_price REAL,
    baseline REAL,
    max_discount REAL NOT NULL DEFAULT 0,
    on_special INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_sale_episodes_item ON sale_episodes (retailer, product_id, ended_at);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    retailer TEXT,
    product_id TEXT,
    episode_id INTEGER REFERENCES sale_episodes (id),
    created_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    sent_at TEXT,
    channel TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    acted_at TEXT
);
CREATE INDEX ix_alerts_pending ON alerts (sent_at, due_at);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    note TEXT,
    upstream_requests INTEGER NOT NULL DEFAULT 0,
    usage_before INTEGER,
    usage_after INTEGER
);
CREATE INDEX ix_runs_kind_time ON runs (kind, started_at);

CREATE TABLE budget_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    at TEXT NOT NULL,
    retailer TEXT NOT NULL,
    tool TEXT NOT NULL,
    upstream_requests INTEGER NOT NULL,
    outcome TEXT NOT NULL
);
CREATE INDEX ix_budget_ledger_time ON budget_ledger (retailer, at);

CREATE TABLE cart_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    reason TEXT NOT NULL,
    outcome TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE item_stats (
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    stats TEXT NOT NULL,
    PRIMARY KEY (retailer, product_id)
);
