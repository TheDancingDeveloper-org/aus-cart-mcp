"""Shared fixtures. Every layer runs offline: e2e drives the real aus-cart-mcp against its mock Woolworths."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import uvicorn

from aus_cart_mcp.watch import budget
from aus_cart_mcp.watch.auscart import (
    AusCartClient,
    Blocked,
    Cart,
    CartLine,
    Product,
    RetailerError,
    SessionRequired,
    Usage,
)
from aus_cart_mcp.watch.store import Store


@pytest.fixture()
def store(tmp_path):
    """A migrated store on a temporary file."""
    store = Store(tmp_path / "aus-cartwatch.db")
    store.migrate()
    yield store
    store.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app) -> tuple[uvicorn.Server, asyncio.Task, str]:
    """Run an ASGI app on a real loopback socket for the duration of a test."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.05)
    return server, task, f"http://127.0.0.1:{port}"


async def stop(*servers: tuple[uvicorn.Server, asyncio.Task]) -> None:
    for server, task in servers:
        server.should_exit = True
        await task


@dataclass
class AusCartStack:
    """A running aus-cart-mcp (pinned dev dependency) wired to its mock Woolworths, with one tenant."""

    url: str
    key: str
    state: Any  # aus_cart_mcp.mock.woolworths.MockState: the "retailer" side, for assertions and price changes
    retailer_url: str

    async def connect_session(self, name: str = "Jordan") -> None:
        """Sign the tenant in, as the owner does from the myaiagent app."""
        from aus_cart_mcp.mock import woolworths as mock

        async with AusCartClient(self.url, self.key) as client:
            await client.call("connect_session", {"cookie_header": mock.signed_in_cookie(self.state, name)})

    def set_price(self, product_id: str, price: float, *, was: float | None = None, special: bool = False) -> None:
        product = self.state.catalogue[int(product_id)]
        product.update(Price=price, WasPrice=was if was is not None else price, IsOnSpecial=special)

    def block(self, on: bool = True) -> None:
        self.state.blocked = on


@pytest.fixture()
async def aus_cart(tmp_path, monkeypatch):
    from aus_cart_mcp.gateway import Limits
    from aus_cart_mcp.mock import woolworths as mock
    from aus_cart_mcp.server import create_app
    from aus_cart_mcp.store import Store as AusCartStore

    state = mock.MockState()
    retailer = await serve(mock.create_app(state))
    monkeypatch.setenv("AUS_CART_MCP_WOOLWORTHS_BASE_URL", retailer[2])
    upstream_store = AusCartStore(tmp_path / "aus-cart.db", "test-secret")
    _, key = upstream_store.create_tenant_sync("aus-cartwatch-test")
    # search_ttl=0: every search reaches the mock, so tests see price changes immediately.
    server = await serve(create_app(upstream_store, Limits(min_interval=0, jitter=0, search_ttl=0, cooloff=0)))
    try:
        yield AusCartStack(f"{server[2]}/mcp", key, state, retailer[2])
    finally:
        await stop(server[:2], retailer[:2])


T0 = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)  # Tuesday 6 Oct 09:00 in Sydney (AEDT, UTC+11)


class Clock:
    """A controllable UTC clock; `sleep` advances it instead of waiting."""

    def __init__(self, start: datetime = T0):
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> datetime:
        self.now += timedelta(**delta)
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def clock():
    return Clock()


class FakeAusCart:
    """An in-memory stand-in for aus-cart-mcp with the typed client's interface.

    Behaves like `AusCartClient`: an async context manager, every call reported to `on_call`
    (1 upstream request for retailer calls, 0 for usage_summary), errors as the client's exceptions.
    """

    def __init__(self, products: dict[str, Product] | None = None, *, tools: set[str] | None = None):
        self.products = dict(products or {})
        self.tools = set(tools or {"search_products", "get_cart", "add_to_cart", "usage_summary", "get_product_image"})
        self.cart: dict[str, float] = {}
        self.calls: list[str] = []
        self.usage = 0  # the tenant's upstream count today, as usage_summary reports it
        self.extra_upstream = 0  # hidden upstream requests per call (warm-ups), for reconcile tests
        self.fail_with: Exception | None = None
        self.on_call = None
        self.session = True
        self.batch_error: Exception | None = None

    def factory(self, store, retailer="woolworths", clock=None):
        def make(run_id=None):
            self.on_call = budget.ledger_hook(store, retailer, run_id, **({"clock": clock} if clock else {}))
            return self

        return make

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def _call(self, tool: str, upstream: int = 1) -> None:
        self.calls.append(tool)
        if self.fail_with is not None and tool != "usage_summary":
            error, self.fail_with = self.fail_with, self.fail_with if getattr(self, "sticky", True) else None
            if self.on_call:
                self.on_call(tool, upstream, "blocked" if isinstance(error, Blocked) else "error")
            raise error
        self.usage += upstream + (self.extra_upstream if upstream else 0)
        if self.on_call:
            self.on_call(tool, upstream, "ok")

    async def usage_summary(self, days: int = 1) -> Usage:
        self._call("usage_summary", 0)
        return Usage(days, len(self.calls), self.usage, {})

    async def search_products(self, query: str, *, limit: int = 10, retailer: str = "woolworths", **_):
        self._call("search_products")
        if query in self.products:  # Woolworths search finds a product by its stockcode
            return [self.products[query]]
        words = query.lower().split()
        return [
            p for p in self.products.values() if all(w in p.name.lower() or w[:-1] in p.name.lower() for w in words)
        ][:limit]

    async def get_products(self, product_ids, *, names=None, retailer="woolworths", batch=True, **_):
        ids = list(product_ids)
        if batch and "get_products" in self.tools:
            self._call("get_products")
            if self.batch_error is not None:
                raise self.batch_error
            return {i: self.products[i] for i in ids if i in self.products}
        out = {}
        for product_id in ids:
            for hit in await self.search_products((names or {}).get(product_id) or product_id, limit=20):
                if hit.product_id in ids:
                    out.setdefault(hit.product_id, hit)
        return out

    async def get_product_image(self, product_id: str, *, retailer: str = "woolworths"):
        self._call("get_product_image")
        if product_id not in self.products:
            raise RetailerError(f"Woolworths has no photo for {product_id}")
        return b"\xff\xd8fake-jpeg-" + product_id.encode(), "image/jpeg"

    def _cart(self) -> Cart:
        return Cart(
            [CartLine(pid, self.products[pid].name, q, self.products[pid].price) for pid, q in self.cart.items()],
            subtotal=sum(q * (self.products[pid].price or 0) for pid, q in self.cart.items()),
        )

    async def get_cart(self, *, retailer: str = "woolworths") -> Cart:
        self._call("get_cart")
        if not self.session:
            raise SessionRequired("the saved Woolworths session has expired; reconnect it")
        return self._cart()

    async def add_to_cart(self, items: dict[str, float], *, retailer: str = "woolworths") -> Cart:
        self._call("add_to_cart")
        if not self.session:
            raise SessionRequired("the saved Woolworths session has expired; reconnect it")
        for pid, quantity in items.items():
            self.cart[pid] = self.cart.get(pid, 0) + quantity
        return self._cart()

    def set_price(self, product_id: str, price: float, *, special: bool = False, was: float | None = None) -> None:
        p = self.products[product_id]
        self.products[product_id] = Product(
            p.product_id, p.name, price, was_price=was, size=p.size, on_special=special, url=p.url,
            unit_price_value=p.unit_price_value, unit_price_unit=p.unit_price_unit,
        )  # fmt: skip


def product(pid: str, name: str, price: float, **kw) -> Product:
    image = f"https://cdn0.woolworths.media/content/wowproductimages/medium/{int(pid):06d}.jpg"
    url = f"https://www.woolworths.com.au/shop/productdetails/{pid}"
    return Product(pid, name, price, size=kw.pop("size", ""), url=url, image_url=image, **kw)


@pytest.fixture()
def fake():
    return FakeAusCart(
        {
            "1": product("1", "Full Cream Milk 3L", 4.95),
            "2": product("2", "Wholemeal Bread", 3.50),
            "3": product("3", "Free Range Eggs 12pk", 6.80),
        }
    )
