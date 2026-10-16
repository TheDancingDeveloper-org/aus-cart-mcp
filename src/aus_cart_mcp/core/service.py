"""The core service: search and the customer's own cart, retailer-neutral.

MCP tools and HTTP routes are adapters over this. They pass the tenant and the
arguments in, and they turn the result into a tool response. They do not call
the gateway themselves. This module must not import watch or ui.
"""

from __future__ import annotations

import base64

from aus_cart_mcp import __version__
from aus_cart_mcp.features import Features
from aus_cart_mcp.gateway import Gateway
from aus_cart_mcp.retailers import CATALOGUE, RETAILERS
from aus_cart_mcp.retailers.base import Blocked, RetailerConstants, RetailerError
from aus_cart_mcp.store import Store, Tenant

MAX_LIMIT = 30
MAX_PRODUCT_IDS = 100


class CoreService:
    """What the core tools do, without knowing they are MCP tools."""

    def __init__(self, store: Store, gateway: Gateway, selected: Features):
        self.store = store
        self.gateway = gateway
        self.selected = selected

    def info(self) -> dict:
        return {
            "version": __version__,
            "features": self.selected.to_dict(),
            "retailers": sorted(RETAILERS),
        }

    def retailers(self, *, include_planned: bool) -> list[dict]:
        return [row.to_dict() for row in CATALOGUE if include_planned or row.status != "planned"]

    def constants_for(self, retailer: str, overrides: dict | None = None) -> RetailerConstants:
        """That retailer's constants, with one tenant's overrides applied.

        ``overrides`` is the caller's per-tenant map for this retailer
        (``delivery_threshold``, ``specials_day``). Unknown or badly typed
        keys are ignored. Watch code asks here instead of hard-coding them.
        """
        adapter = RETAILERS.get(retailer)
        if adapter is None:
            raise RetailerError(f"unknown retailer {retailer}")
        return adapter.constants.overridden(overrides or {})

    def default_retailer(self) -> str:
        """The only configured retailer. An error when there is more than one and none was named."""
        keys = sorted(RETAILERS)
        if len(keys) == 1:
            return keys[0]
        raise RetailerError("name the retailer: more than one is configured")

    async def search(self, tenant: Tenant, retailer: str, query: str, *, limit: int, specials_only: bool) -> list[dict]:
        products = await self.gateway.search(
            tenant, retailer, query, limit=max(1, min(limit, MAX_LIMIT)), specials_only=specials_only
        )
        return [product.to_dict() for product in products]

    async def products(self, tenant: Tenant, retailer: str, product_ids: list[str]) -> dict:
        ids = [str(i).strip() for i in product_ids or [] if str(i).strip()]
        if not ids:
            raise RetailerError("no product ids given")
        if len(ids) > MAX_PRODUCT_IDS:
            raise RetailerError(f"at most {MAX_PRODUCT_IDS} product ids per call")
        products = await self.gateway.products(tenant, retailer, ids)
        known = {product.product_id for product in products}
        return {
            "products": [product.to_dict() for product in products],
            "missing": [i for i in ids if i not in known],
        }

    async def product_image(self, tenant: Tenant, retailer: str, product_id: str) -> dict:
        data, content_type = await self.gateway.image(tenant, retailer, product_id)
        return {
            "product_id": str(product_id),
            "content_type": content_type,
            "data_base64": base64.b64encode(data).decode(),
        }

    async def cart_view(self, tenant: Tenant, retailer: str) -> dict:
        try:
            live = (await self.gateway.cart(tenant, retailer)).to_dict()
        except Blocked as exc:
            live = {"items": [], "subtotal": None, "total": None, "savings": None, "unavailable": str(exc)}
        pending = _public(await self.gateway.store.list_pending(tenant.id, retailer))
        return {**live, "pending": pending}

    async def add(self, tenant: Tenant, retailer: str, quantities: dict[str, float]) -> dict:
        return _outcome(await self.gateway.add(tenant, retailer, quantities))

    async def set_quantities(self, tenant: Tenant, retailer: str, quantities: dict[str, float]) -> dict:
        return _outcome(await self.gateway.set_quantities(tenant, retailer, quantities))

    async def pending(self, tenant: Tenant, retailer: str) -> dict:
        return {"pending": _public(await self.gateway.pending(tenant, retailer))}

    async def sync(self, tenant: Tenant, retailer: str) -> dict:
        """Push recorded writes. A pause or a block is raised, not returned as pending.

        A refusal that is still on the books is not reported as synced.
        """
        cart = await self.gateway.sync(tenant, retailer)
        pending = _public(await self.store.list_pending(tenant.id, retailer))
        failed = [row for row in pending if row["state"] == "failed"]
        waiting = [row for row in pending if row["state"] == "pending"]
        if failed or waiting:
            payload = {
                **cart.to_dict(),
                "status": "pending",
                "reason": (failed or waiting)[0]["reason"]
                or ("the retailer rejected the change" if failed else "not synced yet"),
                "retry_after": None,
                "pending": pending,
            }
            if failed and not waiting:
                payload["permanent"] = True
            return payload
        return {**cart.to_dict(), "status": "synced", "reason": "", "retry_after": None, "pending": pending}

    async def clear_failed(self, tenant: Tenant, retailer: str, product_id: str | None) -> dict:
        return await self.gateway.clear_failed(tenant, retailer, product_id)

    async def status(self, tenant: Tenant, retailer: str) -> dict:
        return await self.gateway.status(tenant, retailer)

    async def connect(self, tenant: Tenant, retailer: str, cookies: dict[str, str]) -> dict:
        shopper = await self.gateway.connect(tenant, retailer, cookies)
        return {"connected": True, "retailer": retailer, "first_name": shopper.first_name}

    async def disconnect(self, tenant: Tenant, retailer: str) -> dict:
        await self.gateway.disconnect(tenant, retailer)
        return {"connected": False, "retailer": retailer}

    async def usage(self, tenant: Tenant, days: int) -> dict:
        return await self.store.usage_summary(tenant.id, days)


def _outcome(outcome: dict) -> dict:
    return {key: value for key, value in outcome.items() if key != "op_ids"}


def _public(rows: list[dict]) -> list[dict]:
    return [{key: value for key, value in row.items() if key != "base_quantity"} for row in rows]
