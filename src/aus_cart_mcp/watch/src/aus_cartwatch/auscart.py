"""The aus-cart-mcp client: aus_cartwatch's only path to a retailer.

aus_cartwatch never talks to Woolworths itself. Every retailer read or cart write
is an MCP tool call on aus-cart-mcp, so its gateway (spacing, daily cap, circuit
breaker, metering) always applies.

Errors are mapped from the server's tool error text (aus-cart-mcp 0.2 returns no
error codes) to `SessionRequired`, `Blocked`, `RetailerError` and `Unavailable`.
`Blocked` is never retried; a transport failure is retried once.

Every call is reported to `on_call(tool, upstream_requests, outcome)` so the
budget ledger counts what reached the retailer. aus-cart-mcp 0.2 does not
return a per-call upstream count, so a call that can reach the retailer counts
as 1; the scheduler reconciles against `usage_summary` per run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx
import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aus_cartwatch import config

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 2_000_000
DEFAULT_RETAILER = "woolworths"
# Tools that never reach a retailer (aus-cart-mcp answers them from its own state).
LOCAL_TOOLS = frozenset({"list_retailers", "usage_summary"})
# Preferred name first; the `*_trolley` names are aus-cart-mcp's pre-0.3 aliases.
CART_TOOLS = {
    "get_cart": ("get_cart", "get_trolley"),
    "add_to_cart": ("add_to_cart", "add_to_trolley"),
    "set_cart_quantities": ("set_cart_quantities", "set_trolley_quantities"),
}
TRANSPORT_ERRORS = (httpx.TransportError, httpx2.TransportError, ConnectionError, OSError, TimeoutError)

OnCall = Callable[[str, int, str], None]


class AusCartError(Exception):
    """Base class: a call to aus-cart-mcp failed. The message is safe to show the owner."""


class RetailerError(AusCartError):
    """The retailer or aus-cart-mcp refused the request (bad input, changed shape, cap reached)."""


class SessionRequired(AusCartError):
    """The retailer session is missing or expired; the owner must reconnect it in the myaiagent app."""


class Blocked(AusCartError):
    """The retailer is refusing automated requests, or the gateway is paused. Never retry around it."""


class Unavailable(AusCartError):
    """aus-cart-mcp could not be reached (transport failure or timeout)."""


_SESSION = re.compile(r"isn't connected|connect a session|session has expired|reconnect it|unauthenticated", re.I)
_BLOCKED = re.compile(r"blocking automated|refusing requests|bot challenge|daily request cap", re.I)


def classify(message: str) -> AusCartError:
    """Map an aus-cart-mcp tool error message to an exception."""
    if _BLOCKED.search(message):
        return Blocked(message)
    if _SESSION.search(message):
        return SessionRequired(message)
    return RetailerError(message)


_CUP = re.compile(r"\$\s*([\d.,]+)\s*/\s*([\d.]*)\s*([a-zA-Z]+)")


def parse_unit_price(text: str) -> tuple[float | None, str]:
    """`"$12.40 / 1L"` → `(12.4, "1L")`; `"$0.95 / 100g"` → `(0.95, "100g")`. Unparseable → `(None, "")`."""
    match = _CUP.search(text or "")
    if not match:
        return None, ""
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None, ""
    amount, unit = match.group(2) or "1", match.group(3)
    unit = {"l": "L", "ml": "mL"}.get(unit.lower(), unit.lower())
    return value, f"{amount}{unit}"


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    price: float | None
    was_price: float | None = None
    unit_price: str = ""
    unit_price_value: float | None = None
    unit_price_unit: str = ""
    size: str = ""
    available: bool = True
    on_special: bool = False
    url: str = ""
    image_url: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Product:
        unit_text = str(data.get("unit_price") or "")
        value, unit = parse_unit_price(unit_text)
        if isinstance(data.get("unit_price_value"), int | float):  # aus-cart-mcp CW-11 numeric fields
            value, unit = float(data["unit_price_value"]), str(data.get("unit_price_unit") or unit)
        was = data.get("was_price")
        price = data.get("price")
        return cls(
            product_id=str(data["product_id"]),
            name=str(data.get("name") or ""),
            price=float(price) if isinstance(price, int | float) else None,
            was_price=float(was) if isinstance(was, int | float) else None,
            unit_price=unit_text,
            unit_price_value=value,
            unit_price_unit=unit,
            size=str(data.get("size") or ""),
            available=bool(data.get("available", True)),
            on_special=bool(data.get("on_special", False)),
            url=str(data.get("url") or ""),
            image_url=str(data.get("image_url") or ""),
        )


@dataclass(frozen=True)
class CartLine:
    product_id: str
    name: str
    quantity: float
    price: float | None


@dataclass(frozen=True)
class Cart:
    items: list[CartLine] = field(default_factory=list)
    subtotal: float | None = None
    total: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Cart:
        return cls(
            items=[
                CartLine(str(i["product_id"]), str(i.get("name") or ""), float(i.get("quantity") or 0), i.get("price"))
                for i in data.get("items") or []
            ],
            subtotal=data.get("subtotal"),
            total=data.get("total"),
        )

    def quantity_of(self, product_id: str) -> float:
        return sum(line.quantity for line in self.items if line.product_id == product_id)


@dataclass(frozen=True)
class Usage:
    days: int
    calls: int
    upstream_requests: int
    by_tool: dict[str, dict]


@dataclass(frozen=True)
class Connection:
    retailer: str
    connected: bool
    first_name: str = ""
    paused: str = ""  # the gateway breaker is open: its message, else empty
    error: str = ""


class AusCartClient:
    """One MCP session to aus-cart-mcp. Use as ``async with AusCartClient(...) as c: await c.search_products(...)``."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        timeout: float = 60.0,
        on_call: OnCall | None = None,
        retry_delay: float = 2.0,
    ):
        self.url = url or config.aus_cart_url()
        self._key = key if key is not None else config.aus_cart_key()
        self.timeout = timeout
        self.on_call = on_call
        self.retry_delay = retry_delay
        self._client: Client | None = None
        self._tools: set[str] = set()

    def __repr__(self) -> str:
        return f"AusCartClient(url={self.url!r}, key={'***' if self._key else '(none)'})"

    # ── session ───────────────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        for attempt in (1, 2):
            try:
                await self._open()
                return self
            except Exception as exc:
                await self._close(None, None, None)
                if not _is_transport(exc) or attempt == 2:
                    raise Unavailable(f"aus-cart-mcp is unreachable at {self.url}: {_describe(exc)}") from exc
                log.warning("aus-cart-mcp unreachable, retrying once: %s", _describe(exc))
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
                log.debug("closing aus-cart-mcp session failed: %s", _describe(close_exc))

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

    # ── raw call ──────────────────────────────────────────────────────────

    async def call(self, tool: str, arguments: dict[str, Any] | None = None, *, upstream: int | None = None) -> Any:
        """Call a tool and decode its JSON text result, mapping errors and reporting the upstream cost."""
        if self._client is None:
            raise RuntimeError("AusCartClient is not open; use `async with`")
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
                            raise Unavailable(f"aus-cart-mcp call {tool} failed: {_describe(exc)}") from exc
                        raise
                    log.warning("aus-cart-mcp %s failed in transport, retrying once: %s", tool, _describe(exc))
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
            log.info("aus-cart-mcp %s outcome=%s upstream=%s", tool, outcome, cost)
            if self.on_call is not None:
                self.on_call(tool, cost, outcome)

    # ── typed wrappers ────────────────────────────────────────────────────

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
        """Look products up by id. Uses aus-cart-mcp's batch `get_products` when present (CW-10).

        Without it, each id costs one search (by its known name, else by the id) and a Stockcode match.
        `max_searches` caps that fallback; ids beyond the cap are left out of the result.
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
        """A product photo `(bytes, content type)`. Needs aus-cart-mcp with `get_product_image`."""
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


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _describe(exc.exceptions[0])
    return f"{type(exc).__name__}: {exc}"[:300]
