"""Register watch MCP tools on the product server when the watch layer is on.

Imported lazily from ``aus_cart_mcp.server.build`` so a core-only process never
imports the watch package at module level. Tool names match the cartwatch server.
Each takes ``retailer`` (default the only configured retailer).

The tools are adapters over ``WatchService``. They do not open the ledger or
read the store path themselves.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import Context

from aus_cart_mcp.retailers import RETAILERS
from aus_cart_mcp.retailers.base import RetailerError
from aus_cart_mcp.store import Tenant
from aus_cart_mcp.watch.service import WatchService

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


def register(mcp, tool, store, gateway, tenant_of) -> None:
    """Attach the watch tools. ``tool`` is the product server's error-wrapping decorator.

    ``tenant_of`` is the core adapter's tenant lookup. Watch tools use it
    instead of reading the request themselves.
    """
    watch = WatchService(store, gateway)

    def result_of(tenant: Tenant, retailer: str) -> dict:
        return {"tenant": tenant.name, "retailer": retailer}

    @tool()
    async def list_tracked(ctx: Context, retailer: str | None = None) -> str:
        """List items this account is tracking at the retailer."""
        tenant = await tenant_of(ctx)
        chosen = resolve_retailer(retailer)
        items = await watch.list_tracked(tenant, chosen)
        return json.dumps({**result_of(tenant, chosen), "items": items})

    @tool()
    async def track_item(product_id: str, ctx: Context, retailer: str | None = None) -> str:
        """Start watching a product's price for this account.

        Looks the id up through search. A hit is stored only when its product_id
        matches; otherwise the item is tracked with no price yet.
        """
        tenant = await tenant_of(ctx)
        chosen = resolve_retailer(retailer)
        if not str(product_id).strip():
            raise RetailerError("product_id is required")
        tracked = await watch.track(tenant, chosen, product_id)
        return json.dumps({**result_of(tenant, chosen), **tracked})

    @tool()
    async def untrack_item(product_id: str, ctx: Context, retailer: str | None = None) -> str:
        """Stop watching a product for this account."""
        tenant = await tenant_of(ctx)
        chosen = resolve_retailer(retailer)
        return json.dumps({**result_of(tenant, chosen), **(await watch.untrack(tenant, chosen, product_id))})

    @tool()
    async def price_history(product_id: str, ctx: Context, retailer: str | None = None) -> str:
        """The current price of a tracked product. A history of observations is not stored yet."""
        tenant = await tenant_of(ctx)
        chosen = resolve_retailer(retailer)
        points = await watch.price_history(tenant, chosen, product_id)
        return json.dumps({**result_of(tenant, chosen), "product_id": product_id, "points": points})

    @tool()
    async def current_deals(ctx: Context, retailer: str | None = None) -> str:
        """Tracked items this account has a price for."""
        tenant = await tenant_of(ctx)
        chosen = resolve_retailer(retailer)
        deals = await watch.current_deals(tenant, chosen)
        return json.dumps({**result_of(tenant, chosen), "deals": deals})

    @tool()
    async def suggest_items(ctx: Context, retailer: str | None = None) -> str:
        """Not available yet: suggestions need imported cart history, which this layer does not store."""
        tenant = await tenant_of(ctx)
        return json.dumps(
            {**result_of(tenant, resolve_retailer(retailer)), "available": False, "reason": "not implemented"}
        )

    @tool()
    async def recent_alerts(ctx: Context, retailer: str | None = None) -> str:
        """Not available yet: the in-process refresh does not emit alerts."""
        tenant = await tenant_of(ctx)
        return json.dumps(
            {**result_of(tenant, resolve_retailer(retailer)), "available": False, "reason": "not implemented"}
        )

    @tool()
    async def budget_status(ctx: Context, retailer: str | None = None) -> str:
        """Upstream requests this account has made today, from the core usage meter."""
        tenant = await tenant_of(ctx)
        chosen = resolve_retailer(retailer)
        budget = await watch.budget(tenant, chosen)
        return json.dumps({**result_of(tenant, chosen), "upstream_today": budget["upstream_today"]})

    @tool()
    async def run_refresh(ctx: Context, retailer: str | None = None) -> str:
        """ADMIN: refresh tracked prices for this account against the retailer.

        Not for agents: hide this tool from models, the same way as connect_session.
        """
        tenant = await tenant_of(ctx)
        chosen = resolve_retailer(retailer)
        refreshed = await watch.refresh(tenant, chosen)
        return json.dumps({**result_of(tenant, chosen), "refreshed": refreshed})
