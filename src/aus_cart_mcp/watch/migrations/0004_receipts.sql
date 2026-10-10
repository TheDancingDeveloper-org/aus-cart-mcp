-- CW-40 receipts: an uploaded receipt, its OCR'd lines with their Woolworths matches, and remembered
-- line-name to product mappings so the same abbreviation never needs matching twice.
CREATE TABLE receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    status TEXT NOT NULL,
    store_name TEXT NOT NULL DEFAULT '',
    purchased_at TEXT,
    total REAL,
    lines_sum REAL,
    ocr_model TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT
);

CREATE TABLE receipt_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    raw_name TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL,
    line_total REAL,
    weighed INTEGER NOT NULL DEFAULT 0,
    promo INTEGER NOT NULL DEFAULT 0,
    match_status TEXT NOT NULL DEFAULT 'pending',
    product_id TEXT,
    product_name TEXT NOT NULL DEFAULT '',
    product_price REAL,
    score REAL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_receipt_lines_receipt ON receipt_lines (receipt_id, line_no);

CREATE TABLE receipt_aliases (
    retailer TEXT NOT NULL,
    raw_key TEXT NOT NULL,
    product_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (retailer, raw_key)
);
