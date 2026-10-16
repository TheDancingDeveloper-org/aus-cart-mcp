"""Watch service: tracked prices for one tenant, over the core gateway.

MCP tools are adapters over this. They do not touch the ledger or the store
path themselves. This module must not import ui.
"""

from __future__ import annotations

from aus_cart_mcp.core.service import CoreService
from aus_cart_mcp.gateway import Gateway
from aus_cart_mcp.store import Store, Tenant
from aus_cart_mcp.watch.ledger import Ledger


class WatchService:
    def __init__(self, store: Store, gateway: Gateway, core: CoreService):
        self.store = store
        self.gateway = gateway
        self.core = core
        self.ledger = Ledger(store.path)

    async def list_tracked(self, tenant: Tenant, retailer: str) -> list[dict]:
        return [
            {"product_id": row.product_id, "name": row.name, "price": row.price}
            for row in self.ledger.list(tenant.id, retailer)
        ]

    async def track(self, tenant: Tenant, retailer: str, product_id: str) -> dict:
        found = await self._price(tenant, retailer, product_id)
        name = found.name if found is not None else ""
        price = found.price if found is not None else None
        self.ledger.track(tenant.id, retailer, product_id, name=name, price=price)
        return {"product_id": product_id, "tracked": True, "price": price}

    async def untrack(self, tenant: Tenant, retailer: str, product_id: str) -> dict:
        self.ledger.untrack(tenant.id, retailer, product_id)
        return {"product_id": product_id, "tracked": False}

    async def price_history(self, tenant: Tenant, retailer: str, product_id: str) -> list[dict]:
        """The current price only. A history of observations is not stored yet."""
        rows = [row for row in self.ledger.list(tenant.id, retailer) if row.product_id == product_id]
        if not rows or rows[0].price is None:
            return []
        return [{"price": rows[0].price, "name": rows[0].name}]

    async def current_deals(self, tenant: Tenant, retailer: str) -> list[dict]:
        return [
            {"product_id": row.product_id, "name": row.name, "price": row.price}
            for row in self.ledger.list(tenant.id, retailer)
            if row.price is not None
        ]

    async def refresh(self, tenant: Tenant, retailer: str) -> int:
        refreshed = 0
        for row in self.ledger.list(tenant.id, retailer):
            found = await self._price(tenant, retailer, row.product_id)
            if found is None:
                continue
            if self.ledger.refresh(tenant.id, retailer, row.product_id, name=found.name, price=found.price):
                refreshed += 1
        return refreshed

    async def budget(self, tenant: Tenant, retailer: str) -> dict:
        summary = await self.store.usage_summary(tenant.id, days=1)
        return {"retailer": retailer, "upstream_today": summary["upstream_requests"]}

    async def _price(self, tenant: Tenant, retailer: str, product_id: str):
        products = await self.gateway.search(tenant, retailer, product_id, limit=10, specials_only=False)
        return next((product for product in products if product.product_id == product_id), None)
