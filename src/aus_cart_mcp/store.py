"""Customers (tenants), their encrypted retailer sessions, and per-call usage.

SQLite on the service's own volume. API keys are stored as SHA-256 hashes;
retailer sessions (cookie jars) are Fernet-encrypted with a key derived from
``AUS_CART_MCP_SECRET``, which lives in the secret manager, never on the volume.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_PREFIX = "acm_"
LEGACY_KEY_PREFIXES = ("gmcp_",)  # keys issued before the aus-cart-mcp rename stay valid

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    retailer TEXT NOT NULL,
    data BLOB NOT NULL,
    captured_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, retailer)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    retailer TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL,
    upstream_requests INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_tenant_at ON usage (tenant_id, at);
CREATE TABLE IF NOT EXISTS pending_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    retailer TEXT NOT NULL,
    op TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    base_quantity REAL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pending_open ON pending_ops (tenant_id, retailer, state, id);
"""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


@dataclass(frozen=True)
class Tenant:
    id: int
    name: str


class Store:
    def __init__(self, path: str | Path, secret: str):
        if not secret:
            raise RuntimeError("AUS_CART_MCP_SECRET is not set; refusing to store retailer sessions unencrypted")
        self._path = str(path)
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        self._lock = asyncio.Lock()
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=10)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    async def _run(self, fn):
        async with self._lock:
            return await asyncio.to_thread(self._call, fn)

    def _call(self, fn):
        with self._connect() as db:
            return fn(db)

    # ── tenants ───────────────────────────────────────────────────────────

    def create_tenant_sync(self, name: str) -> tuple[Tenant, str]:
        """Create a customer and return (tenant, api_key). The key is shown once, never stored."""
        api_key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO tenants (name, key_hash, created_at) VALUES (?, ?, ?)", (name, hash_key(api_key), _now())
            )
            return Tenant(cur.lastrowid, name), api_key

    def rotate_key_sync(self, name: str) -> str:
        api_key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._connect() as db:
            cur = db.execute("UPDATE tenants SET key_hash = ? WHERE name = ?", (hash_key(api_key), name))
            if cur.rowcount == 0:
                raise KeyError(name)
        return api_key

    def set_disabled_sync(self, name: str, disabled: bool) -> None:
        with self._connect() as db:
            cur = db.execute("UPDATE tenants SET disabled_at = ? WHERE name = ?", (_now() if disabled else None, name))
            if cur.rowcount == 0:
                raise KeyError(name)

    def list_tenants_sync(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT id, name, created_at, disabled_at FROM tenants ORDER BY id").fetchall()
        return [{"id": r[0], "name": r[1], "created_at": r[2], "disabled_at": r[3]} for r in rows]

    async def tenant_for_key(self, api_key: str) -> Tenant | None:
        if not api_key or not api_key.startswith((KEY_PREFIX, *LEGACY_KEY_PREFIXES)):
            return None
        digest = hash_key(api_key)

        def fn(db):
            return db.execute(
                "SELECT id, name FROM tenants WHERE key_hash = ? AND disabled_at IS NULL", (digest,)
            ).fetchone()

        row = await self._run(fn)
        return Tenant(row[0], row[1]) if row else None

    # ── retailer sessions ─────────────────────────────────────────────────

    async def get_session(self, tenant_id: int, retailer: str) -> dict | None:
        def fn(db):
            return db.execute(
                "SELECT data, captured_at, last_used_at FROM sessions WHERE tenant_id = ? AND retailer = ?",
                (tenant_id, retailer),
            ).fetchone()

        row = await self._run(fn)
        if row is None:
            return None
        try:
            data = json.loads(self._fernet.decrypt(row[0]))
        except InvalidToken:
            return None  # key rotated: treat as disconnected
        return {**data, "captured_at": row[1], "last_used_at": row[2]}

    async def put_session(self, tenant_id: int, retailer: str, data: dict, *, new: bool = False) -> None:
        blob = self._fernet.encrypt(json.dumps(data).encode())
        now = _now()

        def fn(db):
            if new:
                db.execute(
                    "INSERT OR REPLACE INTO sessions (tenant_id, retailer, data, captured_at, last_used_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (tenant_id, retailer, blob, now, now),
                )
            else:
                db.execute(
                    "UPDATE sessions SET data = ?, last_used_at = ? WHERE tenant_id = ? AND retailer = ?",
                    (blob, now, tenant_id, retailer),
                )

        await self._run(fn)

    async def delete_session(self, tenant_id: int, retailer: str) -> None:
        await self._run(
            lambda db: db.execute("DELETE FROM sessions WHERE tenant_id = ? AND retailer = ?", (tenant_id, retailer))
        )

    # ── usage (the metering a pay-per-use bill is built from) ─────────────

    async def record_usage(self, tenant_id: int, tool: str, retailer: str, ok: bool, upstream_requests: int) -> None:
        await self._run(
            lambda db: db.execute(
                "INSERT INTO usage (tenant_id, tool, retailer, ok, upstream_requests, at) VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, tool, retailer, int(ok), upstream_requests, _now()),
            )
        )

    async def usage_summary(self, tenant_id: int, days: int) -> dict:
        since = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)).isoformat(timespec="seconds")

        def fn(db):
            return db.execute(
                "SELECT tool, COUNT(*), SUM(ok), SUM(upstream_requests) FROM usage "
                "WHERE tenant_id = ? AND at >= ? GROUP BY tool ORDER BY tool",
                (tenant_id, since),
            ).fetchall()

        rows = await self._run(fn)
        tools = {r[0]: {"calls": r[1], "succeeded": r[2] or 0, "upstream_requests": r[3] or 0} for r in rows}
        return {
            "days": days,
            "calls": sum(t["calls"] for t in tools.values()),
            "upstream_requests": sum(t["upstream_requests"] for t in tools.values()),
            "by_tool": tools,
        }

    # ── pending cart (recorded writes, synced later) ──────────────────────

    PENDING_TTL_DAYS = 14

    async def enqueue_pending(
        self,
        tenant_id: int,
        retailer: str,
        op: str,
        items: dict[str, float],
        *,
        base: dict[str, float] | None = None,
    ) -> list[int]:
        """Record a cart write.

        An add keeps every earlier pending add. A set replaces the line, so it
        supersedes earlier pending ops for that product. Failed ops are never touched.
        """
        now = _now()
        bases = base or {}

        def fn(db):
            ids = []
            for product_id, quantity in items.items():
                if op == "set":
                    db.execute(
                        "UPDATE pending_ops SET state = 'superseded', updated_at = ? "
                        "WHERE tenant_id = ? AND retailer = ? AND product_id = ? AND state = 'pending'",
                        (now, tenant_id, retailer, product_id),
                    )
                cur = db.execute(
                    "INSERT INTO pending_ops "
                    "(tenant_id, retailer, op, product_id, quantity, base_quantity, state, reason, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)",
                    (tenant_id, retailer, op, product_id, quantity, bases.get(product_id), now, now),
                )
                ids.append(cur.lastrowid)
            return ids

        return await self._run(fn)

    async def list_pending(
        self, tenant_id: int, retailer: str, *, states: tuple[str, ...] = ("pending", "failed")
    ) -> list[dict]:
        placeholders = ",".join("?" * len(states))

        def fn(db):
            rows = db.execute(
                "SELECT id, op, product_id, quantity, base_quantity, state, reason, created_at, updated_at "
                f"FROM pending_ops WHERE tenant_id = ? AND retailer = ? AND state IN ({placeholders}) ORDER BY id",
                (tenant_id, retailer, *states),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "op": r[1],
                    "product_id": r[2],
                    "quantity": r[3],
                    "base_quantity": r[4],
                    "state": r[5],
                    "reason": r[6],
                    "created_at": r[7],
                    "updated_at": r[8],
                }
                for r in rows
            ]

        return await self._run(fn)

    async def mark_pending(self, op_id: int, state: str, reason: str = "") -> None:
        now = _now()
        await self._run(
            lambda db: db.execute(
                "UPDATE pending_ops SET state = ?, reason = ?, updated_at = ? WHERE id = ?",
                (state, reason, now, op_id),
            )
        )

    async def purge_pending(self, older_than_days: int | None = None) -> int:
        days = self.PENDING_TTL_DAYS if older_than_days is None else older_than_days
        cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)).isoformat(timespec="seconds")

        def fn(db):
            cur = db.execute(
                "DELETE FROM pending_ops WHERE state IN ('synced', 'superseded', 'failed') AND updated_at < ?",
                (cutoff,),
            )
            return cur.rowcount

        return await self._run(fn)
