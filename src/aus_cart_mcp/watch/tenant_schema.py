"""Backfill tenant_id / retailer on watch tables created before WI-1192.

Runs after the numbered SQL migrations. Idempotent: tables that already have
``tenant_id`` are left alone. Existing rows become tenant ``myaiagent-owner``
and retailer ``woolworths``.
"""

from __future__ import annotations

import re
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


def _create_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row[0]


def _widen(match: re.Match) -> str:
    cols = [c.strip() for c in match.group(2).split(",")]
    if "retailer" not in cols:
        return match.group(0)
    return match.group(1) + "tenant_id, " + match.group(2)


def _with_tenant(sql: str, table: str, extra: str | None) -> str:
    """Copy the original CREATE TABLE, prefixing tenant_id and widening its keys.

    ``extra`` replaces the table's own PRIMARY KEY or UNIQUE clause when the
    tenant rebuild changes the natural key. Foreign keys that named the old
    key gain tenant_id on both sides, and a reference that SQLite rewrote to
    the renamed ``_<parent>_pre_tenant`` table is pointed back at the parent
    the rebuild has already recreated.
    """
    body = sql[sql.index("(") :]
    if "retailer" not in _columns_from_sql(body):
        body = body.replace("(", "(retailer TEXT NOT NULL, ", 1)
    body = body.replace("(", "(tenant_id TEXT NOT NULL, ", 1)
    if extra:
        body = re.sub(r",?\s*PRIMARY KEY\s*\([^)]*\)", "", body, count=1, flags=re.I)
        body = re.sub(r",?\s*UNIQUE\s*\([^)]*\)", "", body, count=1, flags=re.I)
        body = body.rstrip().removesuffix(")").rstrip().rstrip(",") + f", {extra})"
    body = re.sub(r"(FOREIGN KEY\s*\()([^)]*)", _widen, body, flags=re.I)
    body = re.sub(r"(REFERENCES\s+\"?\w+\"?\s*\()([^)]*)", _widen, body, flags=re.I)
    body = re.sub(r'REFERENCES\s+"_(\w+)_pre_tenant"', r"REFERENCES \1", body, flags=re.I)
    return f"CREATE TABLE {table} {body}"


def _columns_from_sql(body: str) -> list[str]:
    return re.findall(r"^\s*(\w+)\s+", body, flags=re.M)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def ensure(conn: sqlite3.Connection) -> None:
    """Add tenant_id (and a default retailer) where the pre-WI-1192 schema lacks them."""
    pending = [table for table in REBUILD if _table_exists(conn, table) and "tenant_id" not in _columns(conn, table)]
    if not pending:
        return
    # A child may be rebuilt after its parent, so its old FK would not match the
    # widened parent key until _widen rewrites it. SQLite checks foreign keys
    # immediately unless this connection was opened with them off.
    previous = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in pending:
            extra = REBUILD[table]
            cols = _columns(conn, table)
            select = [f"'{OWNER}'", *cols]
            if "retailer" not in cols:
                select.insert(1, f"'{DEFAULT_RETAILER}'")
            conn.execute(f"ALTER TABLE {table} RENAME TO _{table}_pre_tenant")
            original = _create_sql(conn, f"_{table}_pre_tenant").replace(f"_{table}_pre_tenant", table, 1)
            conn.execute(_with_tenant(original, table, extra))
            dest = ["tenant_id", *cols] if "retailer" in cols else ["tenant_id", "retailer", *cols]
            conn.execute(
                f"INSERT INTO {table} ({', '.join(dest)}) SELECT {', '.join(select)} FROM _{table}_pre_tenant"
            )
            conn.execute(f"DROP TABLE _{table}_pre_tenant")
    finally:
        conn.execute(f"PRAGMA foreign_keys={int(previous)}")
