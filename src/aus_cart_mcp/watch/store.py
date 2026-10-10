"""SQLite storage: migrations and the repository functions every other module uses.

Migrations are numbered SQL files in ``aus_cart_mcp.watch/migrations`` (``0001_init.sql``,
``0002_...``). Each runs once, in order, inside a transaction, and is recorded in
``schema_migrations``. They are applied by ``python -m aus_cart_mcp.watch db migrate``
and at server start. Shipped migrations are never edited.

One household, a few thousand rows a week: plain ``sqlite3`` on one connection
(WAL, foreign keys on), as aus-cart-mcp does, rather than an async driver. Every
query is sub-millisecond, so calling it from the event loop is fine. Times are
ISO-8601 UTC strings; functions that write a time take ``now`` for tests.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
SOURCES = ("manual", "cart", "receipt", "order", "email")
# Tables that belong to one tenant. `settings` is process-wide and stays unscoped.
_TENANT_SQL = re.compile(
    r"\b(?:products|tracked_items|observations|cart_snapshots|shop_episodes|candidate_decisions|"
    r"sale_episodes|alerts|runs|budget_ledger|cart_actions|item_stats|search_cache|product_images|"
    r"receipts|receipt_lines|receipt_aliases|watch_settings)\b",
    re.I,
)
_INSERT_COLS = re.compile(
    r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?:products|tracked_items|observations|cart_snapshots|"
    r"shop_episodes|candidate_decisions|sale_episodes|alerts|runs|budget_ledger|cart_actions|"
    r"item_stats|search_cache|product_images|receipts|receipt_lines|receipt_aliases|watch_settings))\s*\(",
    re.I,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).astimezone(UTC).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def bundled_migrations() -> list[Migration]:
    """The migrations shipped in the package, in version order."""
    out = []
    for entry in resources.files("aus_cart_mcp.watch.migrations").iterdir():
        match = MIGRATION_NAME.match(entry.name)
        if match:
            out.append(Migration(int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort(key=lambda m: m.version)
    versions = [m.version for m in out]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return out


class Store:
    def __init__(self, path: str | Path, migrations: list[Migration] | None = None, *, tenant_id: str = ""):
        self.path = Path(path)
        self.tenant_id = str(tenant_id)
        self.migrations = bundled_migrations() if migrations is None else migrations
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ── connection ────────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """A new connection (for tools and tests); the repository functions share `db`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = self.connect()
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _scoped(self, sql: str, params: Iterable[Any]) -> tuple[str, tuple]:
        """Stamp every tenant-owned statement with this store's tenant.

        Household settings stay global. Everything else is invisible across tenants,
        including rows written before a tenant was known (they keep the default '').
        """
        if not _TENANT_SQL.search(sql):
            return sql, tuple(params)
        tid = self.tenant_id
        out = sql
        vals = list(params)

        # INSERT column lists: tenant_id is the first column and the first value.
        def _insert(match: re.Match) -> str:
            return f"{match.group(1)}(tenant_id, "

        out = _INSERT_COLS.sub(_insert, out)
        if out != sql:
            vals.insert(0, tid)
            out = re.sub(r"\bVALUES\s*\(", "VALUES (?, ", out, count=1, flags=re.I)
        # Conflict targets and join keys name the tenant explicitly.
        out = out.replace("ON CONFLICT (", "ON CONFLICT (tenant_id, ")
        out = out.replace("USING (", "USING (tenant_id, ")
        # Reads and updates filter on it. INSERT...SELECT settings copies are not filtered.
        if not out.lstrip().upper().startswith("INSERT"):
            vals.append(tid)
            if re.search(r"\bWHERE\b", out, re.I):
                out = re.sub(r"\bWHERE\b", "WHERE tenant_id = ? AND", out, count=1, flags=re.I)
            else:
                out += " WHERE tenant_id = ?"
        elif "SELECT" in out.upper() and "WHERE" not in out.upper():
            pass
        return out, tuple(vals)

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        sql, params = self._scoped(sql, params)
        with self._lock:
            return [dict(row) for row in self.db.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        sql, params = self._scoped(sql, params)
        with self._lock:
            row = self.db.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        sql, params = self._scoped(sql, params)
        with self._lock:
            return self.db.execute(sql, tuple(params))

    # ── migrations ────────────────────────────────────────────────────────

    def migrate(self) -> list[str]:
        """Apply pending migrations; return the names applied (empty when up to date)."""
        applied: list[str] = []
        with self._lock:
            db = self.db
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "applied_at TEXT NOT NULL)"
            )
            done = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
            for migration in self.migrations:
                if migration.version in done:
                    continue
                db.execute("BEGIN")
                try:
                    for statement in _statements(migration.sql):
                        db.execute(statement)
                    db.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, iso()),
                    )
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                applied.append(migration.name)
        return applied

    def schema_version(self) -> int:
        """The highest applied migration, or 0 for a fresh file."""
        exists = self._one("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'")
        if not exists:
            return 0
        return self._one("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")["v"]

    # ── settings ──────────────────────────────────────────────────────────

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def settings(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self._all("SELECT key, value FROM settings ORDER BY key")}

    # ── products and tracked items ────────────────────────────────────────

    def upsert_product(
        self,
        retailer: str,
        product_id: str,
        *,
        name: str = "",
        size: str = "",
        url: str = "",
        unit: str = "",
        now: datetime | None = None,
    ) -> None:
        at = iso(now)
        self._exec(
            "INSERT INTO products (retailer, product_id, name, size, url, unit, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (retailer, product_id) DO UPDATE SET "
            "name = CASE WHEN excluded.name != '' THEN excluded.name ELSE products.name END, "
            "size = CASE WHEN excluded.size != '' THEN excluded.size ELSE products.size END, "
            "url = CASE WHEN excluded.url != '' THEN excluded.url ELSE products.url END, "
            "unit = CASE WHEN excluded.unit != '' THEN excluded.unit ELSE products.unit END, "
            "last_seen = excluded.last_seen",
            (retailer, product_id, name, size, url, unit, at, at),
        )

    def remember_product(self, retailer: str, product, *, now: datetime | None = None) -> None:
        """Keep a product's latest details (any `auscart.Product`) so later reads need no retailer call."""
        self.upsert_product(
            retailer,
            product.product_id,
            name=product.name,
            size=product.size,
            url=product.url,
            unit=product.unit_price_unit,
            now=now,
        )
        self._exec(
            "UPDATE products SET image_url = CASE WHEN ? != '' THEN ? ELSE image_url END, last_price = ?, "
            "last_was_price = ?, last_on_special = ?, last_available = ?, last_unit_price = ?, "
            "last_unit_price_value = ?, snapshot_at = ? WHERE retailer = ? AND product_id = ?",
            (
                product.image_url,
                product.image_url,
                product.price,
                product.was_price,
                int(product.on_special),
                int(product.available),
                product.unit_price,
                product.unit_price_value,
                iso(now),
                retailer,
                product.product_id,
            ),
        )

    def cached_product(self, retailer: str, product_id: str, *, max_age: timedelta, now: datetime | None = None):
        """The product as last seen, if seen within `max_age` (else None)."""
        from aus_cart_mcp.watch.types import Product

        row = self.get_product(retailer, product_id)
        if row is None or not row.get("snapshot_at"):
            return None
        if parse_time(row["snapshot_at"]) < (now or utcnow()) - max_age:
            return None
        return Product(
            product_id=row["product_id"],
            name=row["name"],
            price=row["last_price"],
            was_price=row["last_was_price"],
            unit_price=row["last_unit_price"],
            unit_price_value=row["last_unit_price_value"],
            unit_price_unit=row["unit"],
            size=row["size"],
            available=bool(row["last_available"]),
            on_special=bool(row["last_on_special"]),
            url=row["url"],
            image_url=row["image_url"],
        )

    def cache_search(self, retailer: str, query: str, product_ids: list[str], *, now: datetime | None = None) -> None:
        self._exec(
            "INSERT INTO search_cache (retailer, query, fetched_at, product_ids) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (retailer, query) DO UPDATE SET fetched_at = excluded.fetched_at, "
            "product_ids = excluded.product_ids",
            (retailer, _query_key(query), iso(now), json.dumps(product_ids)),
        )

    def cached_search(self, retailer: str, query: str, *, max_age: timedelta, now: datetime | None = None):
        """Products from a search made within `max_age`, in their original order (else None)."""
        row = self._one("SELECT * FROM search_cache WHERE retailer = ? AND query = ?", (retailer, _query_key(query)))
        if row is None or parse_time(row["fetched_at"]) < (now or utcnow()) - max_age:
            return None
        products = []
        for product_id in json.loads(row["product_ids"]):
            product = self.cached_product(retailer, product_id, max_age=max_age * 2, now=now)
            if product is None:
                return None  # a product has aged out: treat the whole search as stale
            products.append(product)
        return products

    def put_image(
        self, retailer: str, product_id: str, data: bytes, content_type: str, *, now: datetime | None = None
    ) -> None:
        self._exec(
            "INSERT INTO product_images (retailer, product_id, content_type, data, fetched_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (retailer, product_id) DO UPDATE SET content_type = excluded.content_type, "
            "data = excluded.data, fetched_at = excluded.fetched_at",
            (retailer, product_id, content_type, data, iso(now)),
        )

    def get_image(self, retailer: str, product_id: str) -> tuple[bytes, str] | None:
        row = self._one(
            "SELECT data, content_type FROM product_images WHERE retailer = ? AND product_id = ?",
            (retailer, product_id),
        )
        return (bytes(row["data"]), row["content_type"]) if row else None

    def has_images(self, retailer: str) -> set[str]:
        rows = self._all("SELECT product_id FROM product_images WHERE retailer = ?", (retailer,))
        return {r["product_id"] for r in rows}

    def get_product(self, retailer: str, product_id: str) -> dict | None:
        return self._one("SELECT * FROM products WHERE retailer = ? AND product_id = ?", (retailer, product_id))

    def track(
        self,
        retailer: str,
        product_id: str,
        *,
        source: str = "manual",
        label: str | None = None,
        target_price: float | None = None,
        threshold: float | None = None,
        now: datetime | None = None,
    ) -> tuple[dict, bool]:
        """Track a known product. Returns ``(item, created)``; re-tracking updates and un-archives, never duplicates."""
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        existing = self.tracked_item(retailer, product_id, include_archived=True)
        if existing is None:
            self._exec(
                "INSERT INTO tracked_items (retailer, product_id, label, source, target_price, discount_threshold, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (retailer, product_id, label, source, target_price, threshold, iso(now)),
            )
            created = True
        else:
            self._exec(
                "UPDATE tracked_items SET archived_at = NULL, label = COALESCE(?, label), "
                "target_price = COALESCE(?, target_price), discount_threshold = COALESCE(?, discount_threshold) "
                "WHERE id = ?",
                (label, target_price, threshold, existing["id"]),
            )
            created = False
        self._exec(
            "INSERT INTO candidate_decisions (retailer, product_id, decision, decided_at) "
            "VALUES (?, ?, 'tracked', ?) "
            "ON CONFLICT (retailer, product_id) DO UPDATE SET decision = 'tracked', decided_at = excluded.decided_at",
            (retailer, product_id, iso(now)),
        )
        return self.tracked_item(retailer, product_id), created

    def tracked_item(self, retailer: str, product_id: str, *, include_archived: bool = False) -> dict | None:
        sql = (
            "SELECT t.*, p.name, p.size, p.url, p.unit, p.image_url FROM tracked_items t "
            "JOIN products p USING (retailer, product_id) WHERE t.retailer = ? AND t.product_id = ?"
        )
        if not include_archived:
            sql += " AND t.archived_at IS NULL"
        return self._one(sql, (retailer, product_id))

    def tracked_item_by_id(self, item_id: int) -> dict | None:
        return self._one(
            "SELECT t.*, p.name, p.size, p.url, p.unit, p.image_url FROM tracked_items t "
            "JOIN products p USING (retailer, product_id) WHERE t.id = ?",
            (item_id,),
        )

    def list_tracked(self, *, retailer: str | None = None, include_archived: bool = False) -> list[dict]:
        sql = (
            "SELECT t.*, p.name, p.size, p.url, p.unit, p.image_url FROM tracked_items t "
            "JOIN products p USING (retailer, product_id) WHERE 1 = 1"
        )
        params: list[Any] = []
        if retailer:
            sql += " AND t.retailer = ?"
            params.append(retailer)
        if not include_archived:
            sql += " AND t.archived_at IS NULL"
        return self._all(sql + " ORDER BY COALESCE(t.label, p.name)", params)

    def archive(self, retailer: str, product_id: str, *, now: datetime | None = None) -> bool:
        cur = self._exec(
            "UPDATE tracked_items SET archived_at = ? WHERE retailer = ? AND product_id = ? AND archived_at IS NULL",
            (iso(now), retailer, product_id),
        )
        return cur.rowcount > 0

    def update_tracked(self, retailer: str, product_id: str, **fields: Any) -> bool:
        """Set `target_price`, `discount_threshold`, `label`, `snoozed_until` or `auto_add` (None clears)."""
        allowed = {"target_price", "discount_threshold", "label", "snoozed_until", "auto_add"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        if not fields:
            return False
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cur = self._exec(
            f"UPDATE tracked_items SET {assignments} WHERE retailer = ? AND product_id = ?",
            (*fields.values(), retailer, product_id),
        )
        return cur.rowcount > 0

    # ── observations ──────────────────────────────────────────────────────

    def add_observation(
        self,
        retailer: str,
        product_id: str,
        *,
        price: float | None,
        was_price: float | None = None,
        on_special: bool = False,
        available: bool = True,
        unit_price_value: float | None = None,
        unit_price_unit: str = "",
        source: str = "refresh",
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO observations (retailer, product_id, observed_at, price, was_price, on_special, available, "
            "unit_price_value, unit_price_unit, source, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                retailer,
                product_id,
                iso(now),
                price,
                was_price,
                int(on_special),
                int(available),
                unit_price_value,
                unit_price_unit,
                source,
                run_id,
            ),
        )
        return cur.lastrowid

    def observations(self, retailer: str, product_id: str, *, since: datetime | None = None) -> list[dict]:
        sql = "SELECT * FROM observations WHERE retailer = ? AND product_id = ?"
        params: list[Any] = [retailer, product_id]
        if since is not None:
            sql += " AND observed_at >= ?"
            params.append(iso(since))
        return self._all(sql + " ORDER BY observed_at, id", params)

    def observations_in_run(self, run_id: str) -> list[dict]:
        return self._all("SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,))

    def latest_observation(self, retailer: str, product_id: str) -> dict | None:
        return self._one(
            "SELECT * FROM observations WHERE retailer = ? AND product_id = ? ORDER BY observed_at DESC, id DESC "
            "LIMIT 1",
            (retailer, product_id),
        )

    # ── runs and the budget ledger ────────────────────────────────────────

    def start_run(self, kind: str, *, run_id: str | None = None, now: datetime | None = None) -> str:
        run_id = run_id or f"{kind}-{iso(now)}-{uuid.uuid4().hex[:6]}"
        self._exec(
            "INSERT INTO runs (id, kind, started_at) VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (run_id, kind, iso(now)),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        outcome: str,
        *,
        note: str = "",
        usage_before: int | None = None,
        usage_after: int | None = None,
        now: datetime | None = None,
    ) -> None:
        upstream = self._one(
            "SELECT COALESCE(SUM(upstream_requests), 0) AS n FROM budget_ledger WHERE run_id = ?", (run_id,)
        )["n"]
        self._exec(
            "UPDATE runs SET finished_at = ?, outcome = ?, note = ?, upstream_requests = ?, "
            "usage_before = COALESCE(?, usage_before), usage_after = COALESCE(?, usage_after) WHERE id = ?",
            (iso(now), outcome, note, upstream, usage_before, usage_after, run_id),
        )

    def get_run(self, run_id: str) -> dict | None:
        return self._one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def recent_runs(self, *, kind: str | None = None, limit: int = 20) -> list[dict]:
        if kind:
            return self._all(
                "SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC, rowid DESC LIMIT ?", (kind, limit)
            )
        return self._all("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))

    def unfinished_runs(self, kind: str) -> list[dict]:
        return self._all("SELECT * FROM runs WHERE kind = ? AND finished_at IS NULL ORDER BY started_at", (kind,))

    def record_call(
        self,
        retailer: str,
        tool: str,
        upstream_requests: int,
        outcome: str,
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO budget_ledger (run_id, at, retailer, tool, upstream_requests, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, iso(now), retailer, tool, upstream_requests, outcome),
        )

    def upstream_since(self, retailer: str, since: datetime) -> int:
        return self._one(
            "SELECT COALESCE(SUM(upstream_requests), 0) AS n FROM budget_ledger WHERE retailer = ? AND at >= ?",
            (retailer, iso(since)),
        )["n"]

    def ledger(self, *, run_id: str | None = None, limit: int = 200) -> list[dict]:
        if run_id:
            return self._all("SELECT * FROM budget_ledger WHERE run_id = ? ORDER BY id", (run_id,))
        return self._all("SELECT * FROM budget_ledger ORDER BY id DESC LIMIT ?", (limit,))

    # ── cart snapshots, shop episodes and candidates ──────────────────────

    def add_cart_snapshot(
        self, retailer: str, items: list[dict], subtotal: float | None, *, now: datetime | None = None
    ) -> int:
        cur = self._exec(
            "INSERT INTO cart_snapshots (taken_at, retailer, items, subtotal) VALUES (?, ?, ?, ?)",
            (iso(now), retailer, json.dumps(items, sort_keys=True), subtotal),
        )
        return cur.lastrowid

    def last_cart_snapshot(self, retailer: str) -> dict | None:
        row = self._one(
            "SELECT * FROM cart_snapshots WHERE retailer = ? ORDER BY taken_at DESC, id DESC LIMIT 1", (retailer,)
        )
        if row:
            row["items"] = json.loads(row["items"])
        return row

    def open_shop_episode(self, retailer: str, source: str = "cart") -> dict | None:
        row = self._one(
            "SELECT * FROM shop_episodes WHERE retailer = ? AND source = ? AND closed_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (retailer, source),
        )
        if row:
            row["items"] = json.loads(row["items"])
        return row

    def start_shop_episode(
        self, retailer: str, items: list[dict], *, source: str = "cart", now: datetime | None = None
    ) -> int:
        cur = self._exec(
            "INSERT INTO shop_episodes (retailer, source, started_at, items) VALUES (?, ?, ?, ?)",
            (retailer, source, iso(now), json.dumps(items, sort_keys=True)),
        )
        return cur.lastrowid

    def update_shop_episode(self, episode_id: int, items: list[dict]) -> None:
        self._exec("UPDATE shop_episodes SET items = ? WHERE id = ?", (json.dumps(items, sort_keys=True), episode_id))

    def close_shop_episode(self, episode_id: int, *, now: datetime | None = None) -> None:
        self._exec("UPDATE shop_episodes SET closed_at = ? WHERE id = ?", (iso(now), episode_id))

    def add_shop_episode(
        self,
        retailer: str,
        source: str,
        items: list[dict],
        *,
        started_at: datetime,
        closed_at: datetime,
    ) -> int:
        """A complete episode from another source (receipt, order, email)."""
        cur = self._exec(
            "INSERT INTO shop_episodes (retailer, source, started_at, closed_at, items) VALUES (?, ?, ?, ?, ?)",
            (retailer, source, iso(started_at), iso(closed_at), json.dumps(items, sort_keys=True)),
        )
        return cur.lastrowid

    def closed_shop_episodes(self, retailer: str, *, limit: int = 6) -> list[dict]:
        rows = self._all(
            "SELECT * FROM shop_episodes WHERE retailer = ? AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC, id DESC LIMIT ?",
            (retailer, limit),
        )
        for row in rows:
            row["items"] = json.loads(row["items"])
        return rows

    def candidate_decisions(self, retailer: str) -> dict[str, str]:
        rows = self._all("SELECT product_id, decision FROM candidate_decisions WHERE retailer = ?", (retailer,))
        return {row["product_id"]: row["decision"] for row in rows}

    def dismiss_candidate(self, retailer: str, product_id: str, *, now: datetime | None = None) -> None:
        self._exec(
            "INSERT INTO candidate_decisions (retailer, product_id, decision, decided_at) "
            "VALUES (?, ?, 'dismissed', ?) "
            "ON CONFLICT (retailer, product_id) DO UPDATE SET decision = 'dismissed', decided_at = excluded.decided_at",
            (retailer, product_id, iso(now)),
        )

    # ── sale episodes ─────────────────────────────────────────────────────

    def open_sale_episode(self, retailer: str, product_id: str) -> dict | None:
        return self._one(
            "SELECT * FROM sale_episodes WHERE retailer = ? AND product_id = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (retailer, product_id),
        )

    def start_sale_episode(
        self,
        retailer: str,
        product_id: str,
        *,
        price: float,
        baseline: float | None,
        discount: float,
        on_special: bool,
        now: datetime | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO sale_episodes (retailer, product_id, started_at, low_price, last_price, baseline, "
            "max_discount, on_special) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (retailer, product_id, iso(now), price, price, baseline, discount, int(on_special)),
        )
        return cur.lastrowid

    def update_sale_episode(self, episode_id: int, *, price: float, discount: float, on_special: bool) -> None:
        self._exec(
            "UPDATE sale_episodes SET low_price = MIN(low_price, ?), last_price = ?, "
            "max_discount = MAX(max_discount, ?), on_special = MAX(on_special, ?) WHERE id = ?",
            (price, price, discount, int(on_special), episode_id),
        )

    def end_sale_episode(self, episode_id: int, *, now: datetime | None = None) -> None:
        self._exec("UPDATE sale_episodes SET ended_at = ? WHERE id = ?", (iso(now), episode_id))

    def get_sale_episode(self, episode_id: int) -> dict | None:
        return self._one("SELECT * FROM sale_episodes WHERE id = ?", (episode_id,))

    def sale_episodes(self, retailer: str, product_id: str, *, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT * FROM sale_episodes WHERE retailer = ? AND product_id = ? ORDER BY started_at DESC LIMIT ?",
            (retailer, product_id, limit),
        )

    def open_sale_episodes(self) -> list[dict]:
        return self._all(
            "SELECT e.*, p.name, p.size, p.url, p.image_url FROM sale_episodes e "
            "JOIN products p USING (retailer, product_id) WHERE e.ended_at IS NULL ORDER BY e.max_discount DESC"
        )

    # ── alerts ────────────────────────────────────────────────────────────

    def add_alert(
        self,
        dedupe_key: str,
        kind: str,
        payload: dict,
        *,
        retailer: str | None = None,
        product_id: str | None = None,
        episode_id: int | None = None,
        due_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int | None:
        """Store an alert once per `dedupe_key`; returns its id, or None when it already exists."""
        cur = self._exec(
            "INSERT INTO alerts (dedupe_key, kind, retailer, product_id, episode_id, created_at, due_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (dedupe_key) DO NOTHING",
            (dedupe_key, kind, retailer, product_id, episode_id, iso(now), iso(due_at or now), json.dumps(payload)),
        )
        return cur.lastrowid if cur.rowcount else None

    def get_alert(self, alert_id: int) -> dict | None:
        row = self._one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    def due_alerts(self, *, now: datetime | None = None, max_attempts: int = 10) -> list[dict]:
        rows = self._all(
            "SELECT * FROM alerts WHERE sent_at IS NULL AND due_at <= ? AND attempts < ? ORDER BY due_at, id",
            (iso(now), max_attempts),
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def recent_alerts(self, *, limit: int = 50) -> list[dict]:
        rows = self._all("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def mark_alert_sent(self, alert_id: int, channel: str, *, now: datetime | None = None) -> None:
        self._exec(
            "UPDATE alerts SET sent_at = ?, channel = ?, attempts = attempts + 1, last_error = NULL WHERE id = ?",
            (iso(now), channel, alert_id),
        )

    def mark_alert_failed(self, alert_id: int, error: str) -> None:
        self._exec("UPDATE alerts SET attempts = attempts + 1, last_error = ? WHERE id = ?", (error[:500], alert_id))

    def mark_alert_acted(self, alert_id: int, *, now: datetime | None = None) -> None:
        self._exec("UPDATE alerts SET acted_at = COALESCE(acted_at, ?) WHERE id = ?", (iso(now), alert_id))

    # ── cart actions ──────────────────────────────────────────────────────

    def add_cart_action(
        self,
        retailer: str,
        product_id: str,
        quantity: float,
        reason: str,
        outcome: str,
        message: str = "",
        *,
        now: datetime | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO cart_actions (at, retailer, product_id, quantity, reason, outcome, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso(now), retailer, product_id, quantity, reason, outcome, message),
        )
        return cur.lastrowid

    def cart_actions(self, *, retailer: str | None = None, product_id: str | None = None, limit: int = 50) -> list:
        sql, params = "SELECT * FROM cart_actions WHERE 1 = 1", []
        if retailer:
            sql += " AND retailer = ?"
            params.append(retailer)
        if product_id:
            sql += " AND product_id = ?"
            params.append(product_id)
        return self._all(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])

    # ── materialised stats ────────────────────────────────────────────────

    def put_item_stats(self, retailer: str, product_id: str, stats: dict, *, now: datetime | None = None) -> None:
        self._exec(
            "INSERT INTO item_stats (retailer, product_id, computed_at, stats) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (retailer, product_id) DO UPDATE SET computed_at = excluded.computed_at, "
            "stats = excluded.stats",
            (retailer, product_id, iso(now), json.dumps(stats)),
        )

    def item_stats(self, retailer: str, product_id: str) -> dict | None:
        row = self._one("SELECT stats FROM item_stats WHERE retailer = ? AND product_id = ?", (retailer, product_id))
        return json.loads(row["stats"]) if row else None

    # ── receipts (CW-40) ──────────────────────────────────────────────────

    def receipt_by_sha(self, sha256: str) -> dict | None:
        return self._one("SELECT * FROM receipts WHERE sha256 = ?", (sha256,))

    def add_receipt(self, sha256: str, path: str, content_type: str, *, now: datetime | None = None) -> int:
        cur = self._exec(
            "INSERT INTO receipts (sha256, path, content_type, uploaded_at, status) VALUES (?, ?, ?, ?, 'uploaded')",
            (sha256, path, content_type, iso(now)),
        )
        return cur.lastrowid

    def update_receipt(self, receipt_id: int, **fields: Any) -> None:
        allowed = {"status", "store_name", "purchased_at", "total", "lines_sum", "ocr_model", "error", "confirmed_at"}
        if set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._exec(f"UPDATE receipts SET {assignments} WHERE id = ?", (*fields.values(), receipt_id))

    def get_receipt(self, receipt_id: int) -> dict | None:
        return self._one("SELECT * FROM receipts WHERE id = ?", (receipt_id,))

    def list_receipts(self, *, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT r.*, (SELECT COUNT(*) FROM receipt_lines l WHERE l.receipt_id = r.id) AS line_count "
            "FROM receipts r ORDER BY r.id DESC LIMIT ?",
            (limit,),
        )

    def add_receipt_lines(self, receipt_id: int, lines: list[dict]) -> None:
        for n, line in enumerate(lines, 1):
            self._exec(
                "INSERT INTO receipt_lines (receipt_id, line_no, raw_name, quantity, unit_price, line_total, weighed, "
                "promo, match_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    n,
                    line["raw_name"],
                    line["quantity"],
                    line.get("unit_price"),
                    line.get("line_total"),
                    int(bool(line.get("weighed"))),
                    int(bool(line.get("promo"))),
                    "skipped" if line.get("weighed") else "pending",
                ),
            )

    def receipt_lines(self, receipt_id: int) -> list[dict]:
        return self._all("SELECT * FROM receipt_lines WHERE receipt_id = ? ORDER BY line_no", (receipt_id,))

    def set_line_match(
        self,
        line_id: int,
        status: str,
        *,
        product_id: str | None = None,
        product_name: str = "",
        product_price: float | None = None,
        score: float | None = None,
        note: str = "",
    ) -> None:
        self._exec(
            "UPDATE receipt_lines SET match_status = ?, product_id = ?, product_name = ?, product_price = ?, "
            "score = ?, note = ? WHERE id = ?",
            (status, product_id, product_name, product_price, score, note, line_id),
        )

    def receipt_alias(self, retailer: str, raw_key: str) -> str | None:
        row = self._one(
            "SELECT product_id FROM receipt_aliases WHERE retailer = ? AND raw_key = ?", (retailer, raw_key)
        )
        return row["product_id"] if row else None

    def set_receipt_alias(self, retailer: str, raw_key: str, product_id: str, *, now: datetime | None = None) -> None:
        self._exec(
            "INSERT INTO receipt_aliases (retailer, raw_key, product_id, decided_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (retailer, raw_key) DO UPDATE SET product_id = excluded.product_id, "
            "decided_at = excluded.decided_at",
            (retailer, raw_key, product_id, iso(now)),
        )

    def runs_since(self, kind: str, since: datetime) -> int:
        return self._one("SELECT COUNT(*) AS n FROM runs WHERE kind = ? AND started_at >= ?", (kind, iso(since)))["n"]

    # ── maintenance ───────────────────────────────────────────────────────

    def stats(self) -> dict:
        tables = [
            "products",
            "tracked_items",
            "observations",
            "cart_snapshots",
            "shop_episodes",
            "sale_episodes",
            "alerts",
            "runs",
            "budget_ledger",
            "cart_actions",
            "product_images",
            "search_cache",
            "receipts",
            "receipt_lines",
        ]
        counts = {t: self._one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in tables}
        size = sum(os.path.getsize(p) for p in (self.path, Path(f"{self.path}-wal")) if os.path.exists(p))
        return {"schema_version": self.schema_version(), "rows": counts, "bytes": size}

    def backup(self, directory: str | Path, *, keep: int = 14, now: datetime | None = None) -> Path:
        """Online backup to `<directory>/aus-cartwatch-<UTC stamp>.db`, integrity-checked; keep the newest `keep`."""
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
        target = target_dir / f"aus-cartwatch-{stamp}.db"
        partial = target.with_suffix(".db.partial")
        destination = sqlite3.connect(partial)
        try:
            with self._lock:
                self.db.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup failed its integrity check")
        finally:
            destination.close()
        os.chmod(partial, 0o600)
        partial.replace(target)
        backups = sorted(target_dir.glob("aus-cartwatch-*.db"))
        for old in backups[: max(0, len(backups) - keep)]:
            old.unlink()
        return target

    def compact(
        self,
        *,
        observation_days: int = 730,
        snapshot_days: int = 90,
        ledger_days: int = 400,
        now: datetime | None = None,
    ) -> dict:
        """Apply retention and reclaim space. Returns the rows deleted per table."""
        moment = now or utcnow()
        deleted = {
            "observations": self._exec(
                "DELETE FROM observations WHERE observed_at < ?", (iso(moment - timedelta(days=observation_days)),)
            ).rowcount,
            "cart_snapshots": self._exec(
                "DELETE FROM cart_snapshots WHERE taken_at < ?", (iso(moment - timedelta(days=snapshot_days)),)
            ).rowcount,
            "budget_ledger": self._exec(
                "DELETE FROM budget_ledger WHERE at < ?", (iso(moment - timedelta(days=ledger_days)),)
            ).rowcount,
        }
        self._exec("PRAGMA wal_checkpoint(TRUNCATE)")
        self._exec("VACUUM")
        return deleted


def _query_key(query: str) -> str:
    return " ".join(query.lower().split())


def _statements(sql: str) -> list[str]:
    """Split a migration into statements on `;`: migrations must not use `;` in comments, strings or triggers."""
    return [s.strip() for s in sql.split(";") if s.strip() and not _comment_only(s)]


def _comment_only(chunk: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--") for line in chunk.splitlines())
