-- WI-1192: every watch row belongs to a product-server tenant and a retailer.
-- Existing single-household rows are the owner's Woolworths data.
-- New installs already have tenant_id from the store rewriter; this rebuild
-- only runs against tables created by 0002-0004 (no tenant_id column).

CREATE TABLE IF NOT EXISTS watch_settings (
    tenant_id TEXT NOT NULL,
    retailer TEXT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE (tenant_id, retailer, key)
);

-- The rebuild is applied by Python in `tenant_schema.ensure` because SQLite
-- cannot conditionally rebuild a table from pure SQL without knowing whether
-- tenant_id already exists. This file records the migration version and
-- creates watch_settings, which 0001-0004 never did.
