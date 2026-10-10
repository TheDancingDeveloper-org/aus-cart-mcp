"""Backfill tenant_id / retailer on watch tables created before WI-1192.

Runs after the numbered SQL migrations. Idempotent: tables that already have
``tenant_id`` are left alone. Existing rows become tenant ``myaiagent-owner``
and retailer ``woolworths``.
"""

from __future__ import annotations

import sqlite3

OWNER = "myaiagent-owner"
DEFAULT_RETAILER = "woolworths"

# Tables whose natural key gains tenant_id. None means no extra unique key.
REBUILD: dict[str, str | None] = {
    "products": "PRIMARY KEY (tenant_id, retailer, product_id)",
    "tracked_items": "UNIQUE (tenant_id, retailer, product_id)",
    "observations": None,
    "cart_snapshots": None,
    "shop_episodes": None,
    "candidate_decisions": "PRIMARY KEY (tenant_id, retailer, product_id)",
    "sale_episodes": None,
    "alerts": None,
    "runs": None,
    "budget_ledger": None,
    "cart_actions": None,
    "item_stats": "PRIMARY KEY (tenant_id, retailer, product_id)",
    "product_images": "PRIMARY KEY (tenant_id, retailer, product_id)",
    "search_cache": "PRIMARY KEY (tenant_id, retailer, query)",
    "receipts": None,
    "receipt_lines": None,
    "receipt_aliases": None,
}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def ensure(conn: sqlite3.Connection) -> None:
    """Add tenant_id (and a default retailer) where the pre-WI-1192 schema lacks them."""
    for table, extra in REBUILD.items():
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        if "tenant_id" in cols:
            continue
        new_cols = ["tenant_id TEXT NOT NULL", *cols]
        select = [f"'{OWNER}'", *cols]
        if "retailer" not in cols:
            new_cols.insert(1, "retailer TEXT NOT NULL")
            select.insert(1, f"'{DEFAULT_RETAILER}'")
        tail = f", {extra}" if extra else ""
        conn.execute(f"ALTER TABLE {table} RENAME TO _{table}_pre_tenant")
        conn.execute(f"CREATE TABLE {table} ({', '.join(new_cols)}{tail})")
        dest = ["tenant_id", *cols] if "retailer" in cols else ["tenant_id", "retailer", *cols]
        conn.execute(f"INSERT INTO {table} ({', '.join(dest)}) SELECT {', '.join(select)} FROM _{table}_pre_tenant")
        conn.execute(f"DROP TABLE _{table}_pre_tenant")
