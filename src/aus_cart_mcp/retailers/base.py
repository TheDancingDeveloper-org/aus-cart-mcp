"""Retailer-neutral types and the contract every retailer adapter implements.

An adapter maps one retailer's own web endpoints onto plain HTTP calls. It never
owns a client, a session store, a throttle or a cache: the gateway does. To add a
retailer, implement ``Retailer``, register it in ``retailers/__init__.py``, add
recorded fixtures under ``tests/fixtures/<key>/`` and pass ``tests/contract``.
See docs/RETAILERS.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol

import httpx

Capability = Literal["search", "products.read", "cart.read", "cart.write"]
Status = Literal["supported", "experimental", "planned"]


class RetailerError(Exception):
    """The retailer refused or changed shape; the message is safe to show a customer."""


class SessionRequired(RetailerError):
    """No logged-in retailer session for this customer: connect one first."""


class Blocked(RetailerError):
    """The retailer is refusing automated requests; back off, never retry around it."""


@dataclass
class Product:
    product_id: str
    name: str
    price: float | None
    unit_price: str = ""
    size: str = ""
    available: bool = True
    on_special: bool = False
    url: str = ""
    image_url: str = ""
    was_price: float | None = None  # the regular price while on special (None when not reported)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CartItem:
    product_id: str
    name: str
    quantity: float
    price: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Cart:
    items: list[CartItem] = field(default_factory=list)
    subtotal: float | None = None
    total: float | None = None
    savings: float | None = None

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "subtotal": self.subtotal,
            "total": self.total,
            "savings": self.savings,
        }


@dataclass
class Shopper:
    logged_in: bool
    first_name: str = ""


@dataclass(frozen=True)
class RetailerInfo:
    """What the supported-retailer index (docs/RETAILERS.md, list_retailers) says about a retailer."""

    key: str
    name: str
    country: str
    status: Status
    capabilities: tuple[Capability, ...]
    site: str
    login_url: str
    cookie_url: str
    session: str = "browser-cookies"  # how a customer connects: sign in on their own device, hand over cookies
    verified: str = ""  # date the adapter was last verified against the live site

    def to_dict(self) -> dict:
        return {**asdict(self), "capabilities": list(self.capabilities)}


class Retailer(Protocol):
    """One retailer's mapping onto plain HTTP."""

    info: RetailerInfo
    base_url: str
    user_agent: str

    @property
    def key(self) -> str: ...

    def has_bot_cookies(self, cookies: dict[str, str]) -> bool: ...

    async def warm(self, http: httpx.AsyncClient) -> None: ...

    async def shopper(self, http: httpx.AsyncClient) -> Shopper: ...

    async def search(
        self, http: httpx.AsyncClient, query: str, *, limit: int, specials_only: bool
    ) -> list[Product]: ...

    async def products(self, http: httpx.AsyncClient, product_ids: list[str]) -> list[Product]:
        """Products by id, as many per upstream request as the retailer allows. Unknown ids are left out."""
        ...

    async def cart(self, http: httpx.AsyncClient) -> Cart: ...

    async def set_quantities(self, http: httpx.AsyncClient, quantities: dict[str, float]) -> None: ...

    async def image(self, http: httpx.AsyncClient, product_id: str) -> tuple[bytes, str]:
        """The product's photo as `(bytes, content type)`."""
        ...
