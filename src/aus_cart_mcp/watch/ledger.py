"""Per-tenant tracked items for the product server.

Rows are ``(tenant_id, retailer, product_id)``. A renamed or re-created tenant
does not inherit another tenant's rows, and one tenant cannot read another's.
Price refresh looks the product up through the core gateway (anonymous search);
it does not open the tenant's cart session.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_tracked (
    tenant_id INTEGER NOT NULL,
    retailer TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    price REAL,
    PRIMARY KEY (tenant_id, retailer, product_id)
);
"""


@dataclass(frozen=True)
class Tracked:
    tenant_id: int
    retailer: str
    product_id: str
    name: str
    price: float | None


class Ledger:
    def __init__(self, path: str):
        self._path = path
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def track(
        self, tenant_id: int, retailer: str, product_id: str, *, name: str = "", price: float | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO watch_tracked (tenant_id, retailer, product_id, name, price)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, retailer, product_id) DO UPDATE SET
                     name = excluded.name, price = excluded.price""",
                (tenant_id, retailer, product_id, name, price),
            )

    def untrack(self, tenant_id: int, retailer: str, product_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM watch_tracked WHERE tenant_id = ? AND retailer = ? AND product_id = ?",
                (tenant_id, retailer, product_id),
            )

    def list(self, tenant_id: int, retailer: str) -> list[Tracked]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT tenant_id, retailer, product_id, name, price FROM watch_tracked "
                "WHERE tenant_id = ? AND retailer = ?",
                (tenant_id, retailer),
            ).fetchall()
        return [Tracked(r["tenant_id"], r["retailer"], r["product_id"], r["name"], r["price"]) for r in rows]

    def refresh(self, tenant_id: int, retailer: str, product_id: str, *, name: str, price: float | None) -> bool:
        """Update a row that already belongs to this tenant. Returns False if it is not theirs."""
        with self._connect() as db:
            cur = db.execute(
                """UPDATE watch_tracked SET name = ?, price = ?
                   WHERE tenant_id = ? AND retailer = ? AND product_id = ?""",
                (name, price, tenant_id, retailer, product_id),
            )
            return cur.rowcount > 0
