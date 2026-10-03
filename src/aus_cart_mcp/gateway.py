"""The only way out to retailers: sessions, throttling, caching, breaker, metering.

Per retailer (the egress IP is shared by every customer):
  - requests are serialised and spaced (``min_interval`` + jitter) with a daily cap
  - a circuit breaker: once the retailer blocks us (403/429/HTML challenge), all
    calls fail fast for ``cooloff``; we back off, we never try to get around it
  - the product-search cache (catalogue data is the same for everyone)

Per customer and retailer:
  - one HTTP client and cookie jar, loaded from the encrypted store and saved
    back only when the jar changes
  - login-state and cart caches (the cart is dropped on every write)
  - operations are serialised, so a customer's adds never interleave

Every tool call is metered: the tool, retailer, outcome and the number of
upstream requests it cost.
"""

from __future__ import annotations

import asyncio
import datetime
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from aus_cart_mcp import config
from aus_cart_mcp.retailers import get as get_retailer
from aus_cart_mcp.retailers.base import Blocked, Cart, Product, Retailer, RetailerError, SessionRequired, Shopper
from aus_cart_mcp.store import Store, Tenant

T = TypeVar("T")


@dataclass
class Limits:
    min_interval: float = 1.5
    jitter: float = 0.5
    daily_cap: int = 2000
    cooloff: float = 30 * 60
    search_ttl: float = 6 * 3600
    shopper_ttl: float = 5 * 60
    cart_ttl: float = 30


@dataclass
class _Throttle:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = -1e9
    day: datetime.date = datetime.date.min
    count: int = 0
    blocked_until: float = 0.0


class _MeteredTransport(httpx.AsyncBaseTransport):
    """Each upstream request passes the retailer's breaker, cap and spacing, and is counted."""

    def __init__(self, gateway: Gateway, retailer: str, inner: httpx.AsyncBaseTransport):
        self._gateway, self._retailer, self._inner = gateway, retailer, inner
        self.count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._gateway._before_request(self._retailer)
        self.count += 1
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class _Conn:
    marker: str
    http: httpx.AsyncClient
    transport: _MeteredTransport
    saved_jar: dict[str, str]


class Gateway:
    def __init__(
        self,
        store: Store,
        limits: Limits | None = None,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] = httpx.AsyncHTTPTransport,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.store = store
        self.limits = limits or Limits()
        self._transport_factory, self._clock, self._sleep = transport_factory, clock, sleep
        self._throttles: dict[str, _Throttle] = {}
        self._conns: dict[tuple[int | None, str], _Conn] = {}
        self._locks: dict[tuple[int | None, str], asyncio.Lock] = {}
        self._cache: dict[tuple, tuple[float, Any]] = {}

    # ── throttle + breaker (per retailer) ─────────────────────────────────

    def _throttle(self, retailer: str) -> _Throttle:
        return self._throttles.setdefault(retailer, _Throttle())

    async def _before_request(self, retailer: str) -> None:
        t = self._throttle(retailer)
        async with t.lock:
            now = self._clock()
            if now < t.blocked_until:
                raise Blocked(self._paused_message(retailer, now))
            today = datetime.datetime.now(datetime.UTC).date()
            if today != t.day:
                t.day, t.count = today, 0
            if t.count >= self.limits.daily_cap:
                raise RetailerError(f"daily request cap for {retailer} reached; try again tomorrow")
            wait = t.last_request + self.limits.min_interval + random.uniform(0, self.limits.jitter) - now
            if wait > 0:
                await self._sleep(wait)
            t.last_request = self._clock()
            t.count += 1

    def _paused_message(self, retailer: str, now: float) -> str:
        minutes = max(1, round((self._throttle(retailer).blocked_until - now) / 60))
        return f"{retailer} is blocking automated requests; paused for about {minutes} more minutes"

    def _trip(self, retailer: str) -> None:
        self._throttle(retailer).blocked_until = self._clock() + self.limits.cooloff
        for key in [k for k in self._conns if k[1] == retailer]:  # start clean after the pause
            self._conns.pop(key, None)

    # ── cache ─────────────────────────────────────────────────────────────

    def _cached(self, key: tuple) -> Any | None:
        hit = self._cache.get(key)
        if hit is None or hit[0] < self._clock():
            self._cache.pop(key, None)
            return None
        return hit[1]

    def _remember(self, key: tuple, value: Any, ttl: float) -> Any:
        self._cache[key] = (self._clock() + ttl, value)
        return value

    def _forget(self, tenant_id: int, retailer: str, *kinds: str) -> None:
        for kind in kinds:
            self._cache.pop((kind, tenant_id, retailer), None)

    # ── connections ───────────────────────────────────────────────────────

    async def _conn(self, retailer: Retailer, tenant_id: int | None, stored: dict | None) -> _Conn:
        key = (tenant_id, retailer.key)
        marker = (stored or {}).get("captured_at") or "guest"
        conn = self._conns.get(key)
        if conn is None or conn.marker != marker:
            if conn is not None:
                await conn.http.aclose()
            cookies = dict((stored or {}).get("cookies") or {})
            transport = _MeteredTransport(self, retailer.key, self._transport_factory())
            http = httpx.AsyncClient(
                base_url=config.base_url_override(retailer.key) or retailer.base_url,
                headers={"User-Agent": retailer.user_agent, "Accept-Language": "en-AU,en;q=0.9"},
                cookies=cookies,
                timeout=20.0,
                follow_redirects=True,
                transport=transport,
            )
            conn = _Conn(marker, http, transport, cookies)
            self._conns[key] = conn
            if tenant_id is not None:
                self._forget(tenant_id, retailer.key, "shopper", "cart")
            if not retailer.has_bot_cookies(cookies):
                await retailer.warm(http)
        return conn

    async def _persist(self, tenant: Tenant, retailer: Retailer, conn: _Conn, stored: dict | None) -> None:
        if stored is None:
            return
        jar = {c.name: c.value for c in conn.http.cookies.jar}
        if jar and jar != conn.saved_jar:
            data = {k: v for k, v in stored.items() if k not in ("captured_at", "last_used_at")}
            await self.store.put_session(tenant.id, retailer.key, {**data, "cookies": jar})
            conn.saved_jar = jar

    async def _shopper(self, tenant: Tenant, retailer: Retailer, conn: _Conn) -> Shopper:
        key = ("shopper", tenant.id, retailer.key)
        cached = self._cached(key)
        if cached is not None:
            return cached
        return self._remember(key, await retailer.shopper(conn.http), self.limits.shopper_ttl)

    async def _require_login(self, tenant: Tenant, retailer: Retailer, conn: _Conn, stored: dict | None) -> None:
        if stored is None:
            raise SessionRequired(f"{retailer.info.name} isn't connected for this account; connect a session first")
        if not (await self._shopper(tenant, retailer, conn)).logged_in:
            raise SessionRequired(f"the saved {retailer.info.name} session has expired; reconnect it")

    # ── one metered operation ─────────────────────────────────────────────

    async def _run(
        self,
        tenant: Tenant,
        retailer_key: str,
        tool: str,
        fn: Callable[[Retailer, _Conn, dict | None], Awaitable[T]],
        *,
        guest_ok: bool = False,
    ) -> T:
        retailer = get_retailer(retailer_key)
        stored = await self.store.get_session(tenant.id, retailer.key)
        conn_owner = tenant.id if stored is not None or not guest_ok else None
        lock = self._locks.setdefault((conn_owner, retailer.key), asyncio.Lock())
        upstream, ok = 0, False
        async with lock:
            conn = None
            try:
                prior = self._conns.get((conn_owner, retailer.key))
                conn = await self._conn(retailer, conn_owner, stored)
                before = conn.transport.count if conn is prior else 0  # a new conn's warm-up counts too
                try:
                    result = await fn(retailer, conn, stored)
                finally:
                    upstream = conn.transport.count - before
                await self._persist(tenant, retailer, conn, stored)
                ok = True
                return result
            except Blocked:
                if self._clock() >= self._throttle(retailer.key).blocked_until:
                    self._trip(retailer.key)
                raise
            finally:
                await self.store.record_usage(tenant.id, tool, retailer.key, ok, upstream)

    # ── public operations ─────────────────────────────────────────────────

    async def connect(self, tenant: Tenant, retailer_key: str, cookies: dict[str, str]) -> Shopper:
        retailer = get_retailer(retailer_key)
        if not cookies:
            raise SessionRequired("no cookies in the captured session")
        await self.store.put_session(tenant.id, retailer.key, {"cookies": cookies}, new=True)
        shopper = await self._run(tenant, retailer.key, "connect_session", lambda r, c, s: r.shopper(c.http))
        if not shopper.logged_in:
            await self.disconnect(tenant, retailer.key)
            raise SessionRequired(f"that session is not logged in to {retailer.info.name}; sign in first")
        self._remember(("shopper", tenant.id, retailer.key), shopper, self.limits.shopper_ttl)
        return shopper

    async def disconnect(self, tenant: Tenant, retailer_key: str) -> None:
        retailer = get_retailer(retailer_key)
        await self.store.delete_session(tenant.id, retailer.key)
        conn = self._conns.pop((tenant.id, retailer.key), None)
        if conn is not None:
            await conn.http.aclose()
        self._forget(tenant.id, retailer.key, "shopper", "cart")

    async def status(self, tenant: Tenant, retailer_key: str) -> dict:
        retailer = get_retailer(retailer_key)
        stored = await self.store.get_session(tenant.id, retailer.key)
        info: dict[str, Any] = {"retailer": retailer.key, "connected": False}
        now = self._clock()
        if now < self._throttle(retailer.key).blocked_until:
            info["paused"] = self._paused_message(retailer.key, now)
        if stored is None:
            return info
        info.update(captured_at=stored.get("captured_at"), last_used_at=stored.get("last_used_at"))

        async def fn(r: Retailer, conn: _Conn, s: dict | None) -> Shopper:
            return await self._shopper(tenant, r, conn)

        try:
            shopper = await self._run(tenant, retailer.key, "connection_status", fn)
        except RetailerError as exc:
            return {**info, "error": str(exc)}
        if not shopper.logged_in:
            return {**info, "error": "the saved session has expired; reconnect"}
        return {**info, "connected": True, "first_name": shopper.first_name}

    async def search(
        self, tenant: Tenant, retailer_key: str, query: str, *, limit: int, specials_only: bool
    ) -> list[Product]:
        retailer = get_retailer(retailer_key)
        key = ("search", retailer.key, " ".join(query.lower().split()), specials_only)
        cached = self._cached(key)
        if cached is not None and len(cached) >= min(limit, 10):
            await self.store.record_usage(tenant.id, "search_products", retailer.key, True, 0)
            return cached[:limit]

        async def fn(r: Retailer, conn: _Conn, s: dict | None) -> list[Product]:
            return await r.search(conn.http, query, limit=max(limit, 10), specials_only=specials_only)

        results = await self._run(tenant, retailer.key, "search_products", fn, guest_ok=True)
        for product in results:  # a later get_products for these ids costs nothing
            self._remember(("product", retailer.key, product.product_id), product, self.limits.search_ttl)
        return self._remember(key, results, self.limits.search_ttl)[:limit]

    async def products(self, tenant: Tenant, retailer_key: str, product_ids: list[str]) -> list[Product]:
        """Products by id, in input order; ids the retailer does not know are left out.

        Each id is cached for the search TTL; only uncached ids go upstream, batched by the adapter."""
        retailer = get_retailer(retailer_key)
        ids = list(dict.fromkeys(str(i) for i in product_ids))
        found = {i: p for i in ids if (p := self._cached(("product", retailer.key, i))) is not None}
        wanted = [i for i in ids if i not in found]
        if wanted:

            async def fn(r: Retailer, conn: _Conn, s: dict | None) -> list[Product]:
                return await r.products(conn.http, wanted)

            for product in await self._run(tenant, retailer.key, "get_products", fn, guest_ok=True):
                found[product.product_id] = self._remember(
                    ("product", retailer.key, product.product_id), product, self.limits.search_ttl
                )
        else:
            await self.store.record_usage(tenant.id, "get_products", retailer.key, True, 0)
        return [found[i] for i in ids if i in found]

    async def image(self, tenant: Tenant, retailer_key: str, product_id: str) -> tuple[bytes, str]:
        """A product photo, through the same spacing, cap, breaker and metering as everything else.

        Photos rarely change: callers should fetch once and keep their own copy."""
        retailer = get_retailer(retailer_key)
        key = ("image", retailer.key, str(product_id))
        cached = self._cached(key)
        if cached is not None:
            await self.store.record_usage(tenant.id, "get_product_image", retailer.key, True, 0)
            return cached

        async def fn(r: Retailer, conn: _Conn, s: dict | None) -> tuple[bytes, str]:
            return await r.image(conn.http, str(product_id))

        result = await self._run(tenant, retailer.key, "get_product_image", fn, guest_ok=True)
        return self._remember(key, result, self.limits.search_ttl)

    async def cart(self, tenant: Tenant, retailer_key: str) -> Cart:
        retailer = get_retailer(retailer_key)
        key = ("cart", tenant.id, retailer.key)
        cached = self._cached(key)
        if cached is not None:
            await self.store.record_usage(tenant.id, "get_cart", retailer.key, True, 0)
            return cached

        async def fn(r: Retailer, conn: _Conn, s: dict | None) -> Cart:
            await self._require_login(tenant, r, conn, s)
            return await r.cart(conn.http)

        return self._remember(key, await self._run(tenant, retailer.key, "get_cart", fn), self.limits.cart_ttl)

    async def add(self, tenant: Tenant, retailer_key: str, wanted: dict[str, float]) -> Cart:
        """Add on top of what is already in the cart."""
        retailer = get_retailer(retailer_key)

        async def fn(r: Retailer, conn: _Conn, s: dict | None) -> Cart:
            await self._require_login(tenant, r, conn, s)
            current = {i.product_id: i.quantity for i in (await r.cart(conn.http)).items}
            await r.set_quantities(conn.http, {p: current.get(p, 0) + q for p, q in wanted.items() if q > 0})
            self._forget(tenant.id, r.key, "cart")
            return await r.cart(conn.http)

        cart = await self._run(tenant, retailer.key, "add_to_cart", fn)
        return self._remember(("cart", tenant.id, retailer.key), cart, self.limits.cart_ttl)

    async def set_quantities(self, tenant: Tenant, retailer_key: str, quantities: dict[str, float]) -> Cart:
        retailer = get_retailer(retailer_key)

        async def fn(r: Retailer, conn: _Conn, s: dict | None) -> Cart:
            await self._require_login(tenant, r, conn, s)
            await r.set_quantities(conn.http, quantities)
            self._forget(tenant.id, r.key, "cart")
            return await r.cart(conn.http)

        cart = await self._run(tenant, retailer.key, "set_cart_quantities", fn)
        return self._remember(("cart", tenant.id, retailer.key), cart, self.limits.cart_ttl)
