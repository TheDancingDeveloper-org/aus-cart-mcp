-- Runtime settings (cadence, thresholds, quiet hours, chat ids). Domain tables follow in CW-22.
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
