"""Register watch MCP tools on the product server when the watch layer is on.

Imported lazily from ``aus_cart_mcp.server.build`` so a core-only process never
imports the watch package at module level. Tool names match the cartwatch server.
Each takes ``retailer`` (default the only configured retailer).

Price lookups go through ``Gateway.search``, which uses the anonymous connection
when the tenant has no stored session. Cart writes stay on the core tools, which
require the tenant's own session.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import Context

from aus_cart_mcp.retailers import RETAILERS
from aus_cart_mcp.retailers.base import RetailerError
from aus_cart_mcp.store import Tenant
from aus_cart_mcp.watch.ledger import Ledger

WATCH_TOOLS = (
    "track_item",
    "untrack_item",
    "list_tracked",
    "price_history",
    "current_deals",
    "suggest_items",
    "recent_alerts",
    "budget_status",
    "run_refresh",
)


def resolve_retailer(retailer: str | None) -> str:
    if retailer:
        if retailer not in RETAILERS:
            raise RetailerError(f"unknown retailer {retailer}")
        return retailer
    if len(RETAILERS) == 1:
        return next(iter(RETAILERS))
    raise RetailerError("retailer is required when more than one is configured")


def register(mcp, tool, store, gateway) -> None:
    """Attach the watch tools. ``tool`` is the product server's error-wrapping decorator."""
    ledger = Ledger(store._path)  # same SQLite file as the product store; separate table

    def result_of(tenant: Tenant, retailer: str) -> dict:
        return {"tenant": tenant.name, "retailer": retailer}

    async def _price(tenant: Tenant, retailer: str, product_id: str):
        products = await gateway.search(tenant, retailer, product_id, limit=10, specials_only=False)
        match = next((p for p in products if p.product_id == product_id), None)
        if match is None and products:
            match = products[0]
        return match

    @tool()
    async def list_tracked(ctx: Context, retailer: str | None = None) -> str:
        """List items this account is tracking at the retailer."""
        tenant = await _tenant(ctx)
        chosen = resolve_retailer(retailer)
        items = [
            {"product_id": row.product_id, "name": row.name, "price": row.price}
            for row in ledger.list(tenant.name, chosen)
        ]
        return json.dumps({**result_of(tenant, chosen), "items": items})

    @tool()
    async def track_item(product_id: str, ctx: Context, retailer: str | None = None) -> str:
        """Start watching a product's price for this account. Looks the price up anonymously."""
        tenant = await _tenant(ctx)
        chosen = resolve_retailer(retailer)
        if not str(product_id).strip():
            raise RetailerError("product_id is required")
        found = await _price(tenant, chosen, product_id)
        name = found.name if found is not None else ""
        price = found.price if found is not None else None
        ledger.track(tenant.name, chosen, product_id, name=name, price=price)
        return json.dumps({**result_of(tenant, chosen), "product_id": product_id, "tracked": True, "price": price})

    @tool()
    async def untrack_item(product_id: str, ctx: Context, retailer: str | None = None) -> str:
        """Stop watching a product for this account."""
        tenant = await _tenant(ctx)
        chosen = resolve_retailer(retailer)
        ledger.untrack(tenant.name, chosen, product_id)
        return json.dumps({**result_of(tenant, chosen), "product_id": product_id, "tracked": False})

    @tool()
    async def price_history(product_id: str, ctx: Context, retailer: str | None = None) -> str:
        """Latest observed price for a product this account tracks."""
        tenant = await _tenant(ctx)
        chosen = resolve_retailer(retailer)
        rows = [row for row in ledger.list(tenant.name, chosen) if row.product_id == product_id]
        points = [{"price": row.price, "name": row.name} for row in rows]
        return json.dumps({**result_of(tenant, chosen), "product_id": product_id, "points": points})

    @tool()
    async def current_deals(ctx: Context, retailer: str | None = None) -> str:
        """Tracked items this account has a price for."""
        tenant = await _tenant(ctx)
        chosen = resolve_retailer(retailer)
        deals = [
            {"product_id": row.product_id, "name": row.name, "price": row.price}
            for row in ledger.list(tenant.name, chosen)
            if row.price is not None
        ]
        return json.dumps({**result_of(tenant, chosen), "deals": deals})

    @tool()
    async def suggest_items(ctx: Context, retailer: str | None = None) -> str:
        """Placeholder: suggestions need the cart history this account has not imported."""
        tenant = await _tenant(ctx)
        return json.dumps({**result_of(tenant, resolve_retailer(retailer)), "suggestions": []})

    @tool()
    async def recent_alerts(ctx: Context, retailer: str | None = None) -> str:
        """Placeholder: alerts are not emitted by the in-process refresh yet."""
        tenant = await _tenant(ctx)
        return json.dumps({**result_of(tenant, resolve_retailer(retailer)), "alerts": []})

    @tool()
    async def budget_status(ctx: Context, retailer: str | None = None) -> str:
        """This account's watch traffic is metered by the core gateway, not a second budget."""
        tenant = await _tenant(ctx)
        return json.dumps({**result_of(tenant, resolve_retailer(retailer)), "upstream_today": 0})

    @tool(name="run_refresh", description="ADMIN: refresh tracked prices for this account against the retailer.")
    async def run_refresh(ctx: Context, retailer: str | None = None) -> str:
        tenant = await _tenant(ctx)
        chosen = resolve_retailer(retailer)
        refreshed = 0
        for row in ledger.list(tenant.name, chosen):
            found = await _price(tenant, chosen, row.product_id)
            if found is None:
                continue
            if ledger.refresh(tenant.name, chosen, row.product_id, name=found.name, price=found.price):
                refreshed += 1
        return json.dumps({**result_of(tenant, chosen), "refreshed": refreshed})


async def _tenant(ctx: Context) -> Tenant:
    request = getattr(ctx.request_context, "request", None)
    tenant = getattr(getattr(request, "state", None), "tenant", None)
    if tenant is None:
        raise RetailerError("unauthenticated")
    return tenant
