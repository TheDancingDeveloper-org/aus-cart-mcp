"""A mock Woolworths that speaks the same endpoints the adapter uses.

Used by the contract and end-to-end tests, and for local development without
touching the real site:

    python -m aus_cart_mcp.mock            # serves on http://127.0.0.1:8090
    AUS_CART_MCP_WOOLWORTHS_BASE_URL=http://127.0.0.1:8090 python -m aus_cart_mcp serve

The catalogue comes from recorded public search results (mock/data/woolworths).
Sign in at /shop/securelogin (any username) to get a logged-in session cookie.
Test hooks: POST /__mock/block {"on": true} makes every API call answer 403;
POST /__mock/reset clears carts and sessions.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from importlib import resources

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

SESSION_COOKIE = "mock-wow-session"
BOT_COOKIES = {"bm_sz": "mock", "_abck": "mock"}


def load(name: str) -> dict:
    return json.loads(resources.files("aus_cart_mcp.mock").joinpath(f"data/woolworths/{name}.json").read_text())


@dataclass
class MockState:
    sessions: dict[str, str] = field(default_factory=dict)  # session id -> first name
    carts: dict[str, dict[int, float]] = field(default_factory=dict)
    blocked: bool = False
    requests: list[str] = field(default_factory=list)
    catalogue: dict[int, dict] = field(default_factory=dict)

    def __post_init__(self):
        search = load("search_milk")
        for group in search["Products"]:
            for p in group["Products"]:
                self.catalogue[int(p["Stockcode"])] = p

    def reset(self) -> None:
        self.sessions.clear()
        self.carts.clear()
        self.blocked = False
        self.requests.clear()


def create_app(state: MockState | None = None) -> Starlette:
    state = state or MockState()

    def session_of(request: Request) -> str | None:
        sid = request.cookies.get(SESSION_COOKIE)
        return sid if sid in state.sessions else None

    def api(handler):
        async def wrapped(request: Request) -> Response:
            state.requests.append(f"{request.method} {request.url.path}")
            if state.blocked:
                return HTMLResponse("<html>Access denied</html>", status_code=403)
            return await handler(request)

        return wrapped

    async def home(request: Request) -> Response:
        state.requests.append("GET /")
        response = HTMLResponse("<html><body>Mock Woolworths</body></html>")
        for k, v in BOT_COOKIES.items():
            response.set_cookie(k, v)
        return response

    async def login_form(request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            sid = secrets.token_hex(8)
            state.sessions[sid] = str(form.get("name") or "Alex")
            response = HTMLResponse("<html><body>Signed in. You can close this page.</body></html>")
            response.set_cookie(SESSION_COOKIE, sid)
            return response
        return HTMLResponse(
            '<html><body><form method="post"><input name="name" value="Alex">'
            '<button type="submit">Sign in</button></form></body></html>'
        )

    @api
    async def bootstrap(request: Request) -> Response:
        sid = session_of(request)
        if sid is None:
            return JSONResponse(load("bootstrap_guest"))
        data = load("bootstrap_logged_in")
        data["ShopperRequest"]["FirstName"] = state.sessions[sid]
        return JSONResponse(data)

    @api
    async def search(request: Request) -> Response:
        body = await request.json()
        term = str(body.get("searchTerm") or "").lower()
        size = int(body.get("pageSize") or 24)
        hits = [
            p for p in state.catalogue.values() if all(w in str(p.get("DisplayName", "")).lower() for w in term.split())
        ]
        if not hits:
            hits = list(state.catalogue.values())  # the real site falls back to related products too
        return JSONResponse({"Products": [{"Products": [p]} for p in hits[:size]], "SearchResultsCount": len(hits)})

    def cart_payload(cart: dict[int, float]) -> dict:
        items, total = [], 0.0
        for code, qty in cart.items():
            p = state.catalogue.get(code, {"DisplayName": f"Product {code}", "Price": 1.0})
            items.append(
                {
                    "Stockcode": code,
                    "DisplayName": p["DisplayName"],
                    "QuantityInTrolley": qty,
                    "SalePrice": p.get("Price"),
                }
            )
            total += qty * float(p.get("Price") or 0)
        totals = {**load("cart_empty")["Totals"], "SubTotal": round(total, 2), "Total": round(total, 2)}
        return {"AvailableItems": items, "Totals": totals}

    @api
    async def trolley(request: Request) -> Response:
        sid = session_of(request)
        return JSONResponse(cart_payload(state.carts.get(sid, {}) if sid else {}))

    @api
    async def update(request: Request) -> Response:
        sid = session_of(request)
        if sid is None:
            return JSONResponse({"Message": "Please sign in"}, status_code=200)
        cart = state.carts.setdefault(sid, {})
        for item in (await request.json()).get("items") or []:
            code, qty = int(item["stockcode"]), float(item["quantity"])
            if qty <= 0:
                cart.pop(code, None)
            else:
                cart[code] = qty
        return JSONResponse(load("update_ok"))

    async def block(request: Request) -> Response:
        state.blocked = bool((await request.json()).get("on", True))
        return JSONResponse({"blocked": state.blocked})

    async def reset(request: Request) -> Response:
        state.reset()
        return JSONResponse({"reset": True})

    app = Starlette(
        routes=[
            Route("/", home),
            Route("/shop/securelogin", login_form, methods=["GET", "POST"]),
            Route("/api/ui/v2/bootstrap", bootstrap),
            Route("/apis/ui/Search/products", search, methods=["POST"]),
            Route("/apis/ui/Trolley", trolley),
            Route("/api/v3/ui/trolley/update", update, methods=["POST"]),
            Route("/__mock/block", block, methods=["POST"]),
            Route("/__mock/reset", reset, methods=["POST"]),
        ]
    )
    app.state.mock = state
    return app


def signed_in_cookie(state: MockState, name: str = "Alex") -> str:
    """A logged-in cookie header, as an app would capture after sign-in (for tests)."""
    sid = secrets.token_hex(8)
    state.sessions[sid] = name
    return f"{SESSION_COOKIE}={sid}; " + "; ".join(f"{k}={v}" for k, v in BOT_COOKIES.items())
