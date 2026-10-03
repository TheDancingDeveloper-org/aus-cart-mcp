"""aus-cart-mcp: an MCP server that searches Australian retailers and fills a customer's own cart.

Transport: Streamable HTTP at ``/mcp``. Every request needs ``Authorization:
Bearer <customer API key>``; tools act for that customer only. ``/healthz`` is open.
"""

from __future__ import annotations

import base64
import functools
import json
from http.cookies import SimpleCookie

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from aus_cart_mcp import __version__, config
from aus_cart_mcp.gateway import Gateway, Limits
from aus_cart_mcp.retailers import CATALOGUE, RETAILERS
from aus_cart_mcp.retailers.base import RetailerError
from aus_cart_mcp.store import Store, Tenant

SERVER_NAME = "aus-cart"
DEFAULT_RETAILER = "woolworths"
MAX_LIMIT = 30
MAX_PRODUCT_IDS = 100

INSTRUCTIONS = (
    "Search Australian retailers and manage the user's own online cart. Call list_retailers to see "
    "which retailers are supported. Search first, then add products by their product_id. "
    "add_to_cart adds on top of what is already in the cart; set_cart_quantities sets exact amounts "
    "(0 removes). The user reviews the cart and finishes the order with the retailer."
)


def parse_cookie_header(header: str) -> dict[str, str]:
    """`a=1; b=2` (what a WebView's cookie manager returns) to a dict."""
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:
        jar = SimpleCookie()
    if jar:
        return {k: m.value for k, m in jar.items()}
    out: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            out[name] = value
    return out


def parse_quantities(items: list[dict]) -> dict[str, float]:
    """Validate tool input `[{"product_id": "...", "quantity": n}]`; duplicates are summed."""
    out: dict[str, float] = {}
    for item in items or []:
        if not isinstance(item, dict):
            raise RetailerError("each item must be an object with product_id and quantity")
        product_id = str(item.get("product_id") or "").strip()
        if not product_id:
            raise RetailerError("each item needs a product_id (from search_products)")
        try:
            quantity = float(item.get("quantity", 1))
        except (TypeError, ValueError) as exc:
            raise RetailerError(f"bad quantity for {product_id}") from exc
        if quantity < 0 or quantity > 99:
            raise RetailerError(f"quantity for {product_id} must be between 0 and 99")
        out[product_id] = out.get(product_id, 0) + quantity
    if not out:
        raise RetailerError("no items given")
    return out


def build(store: Store, gateway: Gateway | None = None) -> tuple[MCPServer, Gateway]:
    gateway = gateway or Gateway(store)
    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)
    register = mcp.tool

    def tool(name: str | None = None, description: str | None = None):
        """Register a tool whose retailer errors reach the caller verbatim (the SDK masks other errors)."""

        def wrap(fn):
            @functools.wraps(fn)
            async def run(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except RetailerError as exc:
                    raise ToolError(str(exc)) from exc

            return register(name=name, description=description)(run)

        return wrap

    async def tenant_of(ctx: Context) -> Tenant:
        request = getattr(ctx.request_context, "request", None)
        tenant = getattr(getattr(request, "state", None), "tenant", None)
        if tenant is None:
            raise RetailerError("unauthenticated")
        return tenant

    def result(payload) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    @tool()
    async def list_retailers(include_planned: bool = False) -> str:
        """List retailers (the supported-retailer index). Use each `key` as `retailer` in other tools.

        login_url / cookie_url let an app connect a session: open login_url in an in-app browser,
        let the user sign in, then pass the cookies for cookie_url to connect_session."""
        rows = [r.to_dict() for r in CATALOGUE if include_planned or r.status != "planned"]
        return result(rows)

    @tool()
    async def search_products(
        query: str, ctx: Context, retailer: str = DEFAULT_RETAILER, limit: int = 10, specials_only: bool = False
    ) -> str:
        """Search a retailer's products. Returns product_id, name, price, unit price, size, special and stock."""
        tenant = await tenant_of(ctx)
        products = await gateway.search(
            tenant, retailer, query, limit=max(1, min(limit, MAX_LIMIT)), specials_only=specials_only
        )
        return result([p.to_dict() for p in products])

    async def _get_cart(ctx: Context, retailer: str) -> str:
        return result((await gateway.cart(await tenant_of(ctx), retailer)).to_dict())

    async def _add(items: list[dict], ctx: Context, retailer: str) -> str:
        return result((await gateway.add(await tenant_of(ctx), retailer, parse_quantities(items))).to_dict())

    async def _set(items: list[dict], ctx: Context, retailer: str) -> str:
        cart = await gateway.set_quantities(await tenant_of(ctx), retailer, parse_quantities(items))
        return result(cart.to_dict())

    @tool()
    async def get_cart(ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """Show the user's current cart at the retailer: items, quantities and totals."""
        return await _get_cart(ctx, retailer)

    @tool()
    async def add_to_cart(items: list[dict], ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """Add products to the user's cart. items: [{"product_id": "...", "quantity": 1}], up to 30.

        Quantities are added on top of what is already in the cart. Returns the updated cart."""
        return await _add(items, ctx, retailer)

    @tool()
    async def set_cart_quantities(items: list[dict], ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """Set exact quantities in the user's cart; quantity 0 removes the product. Returns the cart."""
        return await _set(items, ctx, retailer)

    # Deprecated aliases from 0.1 ("trolley" is Woolworths' word). Removed in 0.3.
    @tool(name="get_trolley", description="Deprecated alias of get_cart.")
    async def get_trolley(ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        return await _get_cart(ctx, retailer)

    @tool(name="add_to_trolley", description="Deprecated alias of add_to_cart.")
    async def add_to_trolley(items: list[dict], ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        return await _add(items, ctx, retailer)

    @tool(name="set_trolley_quantities", description="Deprecated alias of set_cart_quantities.")
    async def set_trolley_quantities(items: list[dict], ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        return await _set(items, ctx, retailer)

    @tool()
    async def connection_status(ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """Whether the user's retailer session is connected and logged in."""
        return result(await gateway.status(await tenant_of(ctx), retailer))

    @tool()
    async def connect_session(cookie_header: str, ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """ADMIN: store the user's logged-in retailer session (the site's cookie header, captured
        after they sign in on their own device). Not for agents: hide this tool from models."""
        shopper = await gateway.connect(await tenant_of(ctx), retailer, parse_cookie_header(cookie_header))
        return result({"connected": True, "retailer": retailer, "first_name": shopper.first_name})

    @tool()
    async def disconnect_session(ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """ADMIN: forget the user's stored retailer session."""
        await gateway.disconnect(await tenant_of(ctx), retailer)
        return result({"connected": False, "retailer": retailer})

    @tool()
    async def get_products(product_ids: list[str], ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """Look products up by product_id (up to 100), in one or a few upstream requests. Returns
        {"products": [...], "missing": [...]}: current price, was_price, special flag, unit price, size, stock."""
        ids = [str(i).strip() for i in product_ids or [] if str(i).strip()]
        if not ids:
            raise RetailerError("no product ids given")
        if len(ids) > MAX_PRODUCT_IDS:
            raise RetailerError(f"at most {MAX_PRODUCT_IDS} product ids per call")
        products = await gateway.products(await tenant_of(ctx), retailer, ids)
        known = {p.product_id for p in products}
        return result({"products": [p.to_dict() for p in products], "missing": [i for i in ids if i not in known]})

    @tool()
    async def get_product_image(product_id: str, ctx: Context, retailer: str = DEFAULT_RETAILER) -> str:
        """A product's photo as base64 with its content type. Photos rarely change: fetch once and keep a copy."""
        data, content_type = await gateway.image(await tenant_of(ctx), retailer, product_id)
        return result(
            {
                "product_id": str(product_id),
                "content_type": content_type,
                "data_base64": base64.b64encode(data).decode(),
            }
        )

    @tool()
    async def usage_summary(ctx: Context, days: int = 30) -> str:
        """This account's metered usage: tool calls and upstream requests over the last N days."""
        tenant = await tenant_of(ctx)
        return result(await store.usage_summary(tenant.id, max(1, min(days, 366))))

    return mcp, gateway


class BearerAuth:
    """Resolve the customer from `Authorization: Bearer <key>` before MCP sees the request."""

    def __init__(self, app: ASGIApp, store: Store, protected_prefix: str = "/mcp"):
        self.app, self.store, self.prefix = app, store, protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        scheme, _, key = header.partition(" ")
        tenant = await self.store.tenant_for_key(key.strip()) if scheme.lower() == "bearer" else None
        if tenant is None:
            response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["tenant"] = tenant
        await self.app(scope, receive, send)


def create_app(store: Store | None = None, limits: Limits | None = None, gateway: Gateway | None = None) -> Starlette:
    """The ASGI app. Tests inject a store and a gateway wired to a mock retailer."""
    store = store or Store(config.db_path(), config.secret())
    mcp, _ = build(store, gateway or Gateway(store, limits))
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "retailers": sorted(RETAILERS)})

    app.add_route("/healthz", healthz)
    app.add_middleware(BearerAuth, store=store)
    return app
