"""Watch-layer retailer types.

These shapes come from the subtree-imported aus-cartwatch client. The HTTP MCP
client that used to live beside them is not part of the host: the product server
talks to retailers only through `aus_cart_mcp.gateway.Gateway` (`upstream.py`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

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


from aus_cart_mcp.watch.client import AusCartClient as AusCartClient  # noqa: E402  (budget imports it from here)
