"""MCP client for the watch layer: the only path from watch tools to a retailer.

Every retailer read or cart write is an MCP tool call on this server, so the
gateway (spacing, daily cap, circuit breaker, metering) always applies. Tests
point it at a loopback server wired to the mock retailer.

Errors are mapped from the server's tool error text to `SessionRequired`,
`Blocked`, `RetailerError` and `Unavailable`. `Blocked` is never retried; a
transport failure is retried once.

Every call is reported to `on_call(tool, upstream_requests, outcome)` so the
budget ledger counts what reached the retailer. A call that can reach the
retailer counts as 1 unless the payload says otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Iterable
from types import TracebackType
from typing import Any, Self

import httpx
from mcp.client.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aus_cart_mcp.watch import config

log = logging.getLogger("aus_cart_mcp.watch.auscart")

MAX_RESULT_CHARS = 2_000_000
DEFAULT_RETAILER = "woolworths"
# Tools that never reach a retailer (answered from this server's own state).
LOCAL_TOOLS = frozenset({"list_retailers", "usage_summary"})
# Preferred name first; the `*_trolley` names are pre-0.3 aliases.
CART_TOOLS = {
    "get_cart": ("get_cart", "get_trolley"),
    "add_to_cart": ("add_to_cart", "add_to_trolley"),
    "set_cart_quantities": ("set_cart_quantities", "set_trolley_quantities"),
}
TRANSPORT_ERRORS = (httpx.TransportError, ConnectionError, OSError, TimeoutError)


class AusCartClient:
    """One MCP session. Use as ``async with AusCartClient(...) as c: await c.search_products(...)``."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        timeout: float = 60.0,
        on_call=None,
        retry_delay: float = 2.0,
        **_kwargs,
    ):
        self.url = url if url is not None else config.aus_cart_url()
        self._key = key if key is not None else config.aus_cart_key()
        self.timeout = timeout
        self.on_call = on_call
        self.retry_delay = retry_delay
        self._client: Client | None = None
        self._tools: set[str] = set()

    def __repr__(self) -> str:
        shown = "(none)" if not self._key else "(redacted)"
        return f"AusCartClient(url={self.url!r}, key={shown})"

    async def __aenter__(self) -> Self:
        for attempt in (1, 2):
            try:
                await self._open()
                return self
            except Exception as exc:
                await self._close(None, None, None)
                if not _is_transport(exc) or attempt == 2:
                    raise Unavailable(f"aus-cart-mcp unreachable at {self.url}") from exc
                log.warning("aus-cart-mcp unreachable, retrying once")
                await asyncio.sleep(self.retry_delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _open(self) -> None:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        http = create_mcp_http_client(headers=headers)
        self._client = Client(streamable_http_client(self.url, http_client=http), read_timeout_seconds=self.timeout)
        await self._client.__aenter__()
        self._tools = {tool.name for tool in (await self._client.list_tools()).tools}

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._close(exc_type, exc, tb)

    async def _close(self, exc_type, exc, tb) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.__aexit__(exc_type, exc, tb)
            except Exception as close_exc:  # a broken transport must not mask the original error
                log.debug("closing aus-cart-mcp session failed: %s", type(close_exc).__name__)

    @property
    def tools(self) -> frozenset[str]:
        return frozenset(self._tools)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def _resolve(self, logical: str) -> str:
        for name in CART_TOOLS.get(logical, (logical,)):
            if name in self._tools:
                return name
        raise RetailerError(f"aus-cart-mcp at {self.url} has no {logical} tool")

    async def call(self, tool: str, arguments: dict[str, Any] | None = None, *, upstream: int | None = None) -> Any:
        """Call a tool and decode its JSON text result, mapping errors and reporting the upstream cost."""
        if self._client is None:
            raise RuntimeError("AusCartClient.call outside an open session")
        cost = upstream if upstream is not None else (0 if tool in LOCAL_TOOLS else 1)
        outcome = "error"
        try:
            for attempt in (1, 2):
                try:
                    result = await self._client.call_tool(tool, arguments or {})
                    break
                except Exception as exc:
                    if not _is_transport(exc) or attempt == 2:
                        if _is_transport(exc):
                            outcome = "unavailable"
                            raise Unavailable(f"aus-cart-mcp call {tool} failed") from exc
                        raise
                    log.warning("aus-cart-mcp %s failed in transport, retrying once", tool)
                    await asyncio.sleep(self.retry_delay)
            text = "".join(getattr(part, "text", "") for part in result.content)
            if len(text) > MAX_RESULT_CHARS:
                raise RetailerError(f"{tool} returned an oversized result ({len(text)} characters)")
            if result.is_error:
                error = classify(text or f"{tool} failed")
                outcome = {Blocked: "blocked", SessionRequired: "session_required"}.get(type(error), "error")
                raise error
            outcome = "ok"
            try:
                payload = json.loads(text)
            except ValueError:
                return text
            if (
                tool not in LOCAL_TOOLS
                and isinstance(payload, dict)
                and isinstance(payload.get("upstream_requests"), int)
            ):
                cost = payload["upstream_requests"]
            return payload
        finally:
            if self.on_call is not None:
                self.on_call(tool, cost, outcome)

    async def list_retailers(self) -> list[dict]:
        return await self.call("list_retailers")

    async def search_products(
        self, query: str, *, limit: int = 10, specials_only: bool = False, retailer: str = DEFAULT_RETAILER
    ) -> list[Product]:
        rows = await self.call(
            "search_products", {"query": query, "limit": limit, "specials_only": specials_only, "retailer": retailer}
        )
        return [Product.from_dict(row) for row in rows or []]

    async def get_products(
        self,
        product_ids: Iterable[str],
        *,
        names: dict[str, str] | None = None,
        retailer: str = DEFAULT_RETAILER,
        max_searches: int | None = None,
        batch: bool = True,
    ) -> dict[str, Product]:
        """Look products up by id. Uses a batch `get_products` tool when the server has one.

        Without it, each id costs one search (by its known name, else by the id).
        `max_searches` caps that fallback; ids beyond the cap are left out.
        """
        wanted = list(dict.fromkeys(str(i) for i in product_ids))
        if not wanted:
            return {}
        if batch and self.has_tool("get_products"):
            rows = await self.call("get_products", {"product_ids": wanted, "retailer": retailer})
            if isinstance(rows, dict):
                rows = rows.get("products") or []
            found = [Product.from_dict(row) for row in rows or []]
            return {p.product_id: p for p in found if p.product_id in wanted}
        names = names or {}
        out: dict[str, Product] = {}
        for index, product_id in enumerate(wanted):
            if max_searches is not None and index >= max_searches:
                break
            if product_id in out:
                continue
            hits = await self.search_products(names.get(product_id) or product_id, limit=20, retailer=retailer)
            for hit in hits:
                if hit.product_id in wanted:
                    out.setdefault(hit.product_id, hit)
        return out

    async def get_product_image(self, product_id: str, *, retailer: str = DEFAULT_RETAILER) -> tuple[bytes, str]:
        """A product photo `(bytes, content type)`. Needs a server with `get_product_image`."""
        if not self.has_tool("get_product_image"):
            raise RetailerError(f"aus-cart-mcp at {self.url} has no get_product_image tool")
        payload = await self.call("get_product_image", {"product_id": product_id, "retailer": retailer})
        try:
            data = base64.b64decode(payload["data_base64"], validate=True)
            content_type = str(payload["content_type"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RetailerError("aus-cart-mcp returned a malformed photo") from exc
        if not content_type.startswith("image/"):
            raise RetailerError("aus-cart-mcp returned something that is not an image")
        return data, content_type

    async def get_cart(self, *, retailer: str = DEFAULT_RETAILER) -> Cart:
        return Cart.from_dict(await self.call(self._resolve("get_cart"), {"retailer": retailer}))

    async def add_to_cart(self, items: dict[str, float], *, retailer: str = DEFAULT_RETAILER) -> Cart:
        payload = [{"product_id": pid, "quantity": qty} for pid, qty in items.items()]
        return Cart.from_dict(await self.call(self._resolve("add_to_cart"), {"items": payload, "retailer": retailer}))

    async def set_cart_quantities(self, items: dict[str, float], *, retailer: str = DEFAULT_RETAILER) -> Cart:
        payload = [{"product_id": pid, "quantity": qty} for pid, qty in items.items()]
        tool = self._resolve("set_cart_quantities")
        return Cart.from_dict(await self.call(tool, {"items": payload, "retailer": retailer}))

    async def connection_status(self, *, retailer: str = DEFAULT_RETAILER) -> Connection:
        raw = await self.call("connection_status", {"retailer": retailer})
        raw = raw if isinstance(raw, dict) else {}
        return Connection(
            retailer=str(raw.get("retailer") or retailer),
            connected=bool(raw.get("connected")),
            first_name=str(raw.get("first_name") or ""),
            paused=str(raw.get("paused") or ""),
            error=str(raw.get("error") or ""),
        )

    async def usage_summary(self, days: int = 1) -> Usage:
        raw = await self.call("usage_summary", {"days": days})
        return Usage(
            days=int(raw.get("days", days)),
            calls=int(raw.get("calls", 0)),
            upstream_requests=int(raw.get("upstream_requests", 0)),
            by_tool=dict(raw.get("by_tool") or {}),
        )


def _is_transport(exc: BaseException) -> bool:
    if isinstance(exc, TRANSPORT_ERRORS):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transport(e) for e in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and _is_transport(cause)


from aus_cart_mcp.watch.types import (  # noqa: E402
    Blocked,
    Cart,
    Connection,
    Product,
    RetailerError,
    SessionRequired,
    Unavailable,
    Usage,
    classify,
)
