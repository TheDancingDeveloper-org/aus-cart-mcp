-- Product cache and photos: every product seen keeps its latest details, so tracking from search
-- results, re-showing a search or opening an item does not ask Woolworths again. Photos are fetched once.
ALTER TABLE products ADD COLUMN image_url TEXT NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN last_price REAL;
ALTER TABLE products ADD COLUMN last_was_price REAL;
ALTER TABLE products ADD COLUMN last_on_special INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN last_available INTEGER NOT NULL DEFAULT 1;
ALTER TABLE products ADD COLUMN last_unit_price TEXT NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN last_unit_price_value REAL;
ALTER TABLE products ADD COLUMN snapshot_at TEXT;

CREATE TABLE product_images (
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data BLOB NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (retailer, product_id)
);

CREATE TABLE search_cache (
    retailer TEXT NOT NULL,
    query TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    product_ids TEXT NOT NULL,
    PRIMARY KEY (retailer, query)
);
