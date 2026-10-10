"""The HTTP app: aus_cartwatch's MCP server (`/mcp`, CW-29), the web UI (CW-30), `/healthz` and `/metrics`.

`/mcp` needs `Authorization: Bearer <key>` where the key's SHA-256 is in
`AUS_CARTWATCH_MCP_KEYS` (or the key is `AUS_CARTWATCH_MCP_KEY`); with no key
configured it refuses everything. The UI has its own password login. `/healthz`
and `/metrics` are open: they carry no personal data. Keep the listener on
loopback or the stack network; Caddy fronts it on the tailnet name.
"""

from __future__ import annotations

import contextlib
import hmac
import json
from collections.abc import AsyncIterator, Iterable
from datetime import timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from aus_cartwatch import __version__, config, detect, tracking, web
from aus_cartwatch.policy import Policy
from aus_cartwatch.service import Service
from aus_cartwatch.store import Store, utcnow

SERVER_NAME = "aus_cartwatch"
MAX_HISTORY_POINTS = 200

INSTRUCTIONS = (
    "The household's grocery price tracker (Woolworths). It records the prices of tracked items over time, "
    "detects sales and heavy discounts, suggests items the household buys repeatedly, and can add an item to "
    "the owner's own Woolworths cart. Use list_tracked and current_deals to answer price questions; track_item "
    "to start watching a product (a query returns candidates, then track one by product_id). add_to_cart acts "
    "on the real cart; quantities are capped and the owner checks out on the Woolworths site."
)


def thin(points: list[dict], limit: int = MAX_HISTORY_POINTS) -> list[dict]:
    """Evenly thin a series to at most `limit` points, always keeping the last one."""
    if len(points) <= limit:
        return points
    step = len(points) / limit
    out = [points[int(i * step)] for i in range(limit - 1)]
    return [*out, points[-1]]


def status_payload(service: Service) -> dict:
    store, scheduler = service.store, service.scheduler
    now = service.clock()
    last = next(iter(store.recent_runs(kind="refresh", limit=1)), None)
    return {
        "retailer": service.retailer,
        "upstream_today": scheduler.used_today(now),
        "daily_cap": scheduler.daily_cap,
        "last_refresh": (
            {k: last[k] for k in ("id", "started_at", "finished_at", "outcome", "note", "upstream_requests")}
            if last
            else None
        ),
        "failure_streak": len(scheduler.failure_streak()),
        "blocked_backoff": scheduler.precheck(now, Policy.load(store), force=True),
    }


def build(service: Service) -> MCPServer:
    store = service.store
    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)

    def check_retailer(retailer: str | None) -> str:
        if retailer and retailer != service.retailer:
            raise ToolError(f"only {service.retailer} is tracked")
        return service.retailer

    def dump(payload) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    def match_tracked(retailer: str, text: str) -> list[dict]:
        words = [w for w in text.lower().split() if w]
        items = store.list_tracked(retailer=retailer)
        exact = [i for i in items if all(w in f"{i.get('label') or ''} {i['name']} {i['size']}".lower() for w in words)]
        return exact

    def item_view(item: dict) -> dict:
        stats = item.get("stats") or {}
        return {
            "product_id": item["product_id"],
            "name": item.get("label") or item["name"],
            "size": item.get("size"),
            "price": stats.get("price"),
            "unit_price": (
                f"${stats['unit_price_value']:.2f} / {stats['unit_price_unit']}"
                if stats.get("unit_price_value")
                else None
            ),
            "baseline": stats.get("baseline"),
            "discount": stats.get("discount"),
            "heavy": stats.get("heavy"),
            "on_special": stats.get("on_special"),
            "target_price": item.get("target_price"),
            "target_met": stats.get("target_met"),
            "last_observed": stats.get("observed_at"),
            "url": item.get("url"),
            "archived": bool(item.get("archived_at")),
        }

    @mcp.tool()
    async def list_tracked(include_archived: bool = False, retailer: str | None = None) -> str:
        """Tracked items: price (AUD per pack), unit_price (per 100 g / 1 L / each), baseline (usual price),
        discount (fraction, 0.3 = 30% off), on_special, target and when last observed."""
        retailer = check_retailer(retailer)
        items = tracking.list_tracked(store, retailer, include_archived=include_archived)
        return dump([item_view(i) for i in items])

    @mcp.tool()
    async def track_item(
        query: str | None = None,
        product_id: str | None = None,
        url: str | None = None,
        label: str | None = None,
        target_price: float | None = None,
        threshold: float | None = None,
        retailer: str | None = None,
    ) -> str:
        """Start tracking a product. Give product_id or a Woolworths product url to track it directly; a
        query returns up to 10 candidates (track one by its product_id). threshold is the heavy-discount
        fraction, e.g. 0.3."""
        check_retailer(retailer)
        reference = product_id or url or query
        if not reference:
            raise ToolError("give query, product_id or url")
        if threshold is not None and not 0 < threshold < 1:
            raise ToolError("threshold is a fraction between 0 and 1")
        result = await service.track(reference, label=label, target_price=target_price, threshold=threshold)
        if result.candidates:
            return dump(
                {
                    "candidates": [
                        {"product_id": p.product_id, "name": p.name, "size": p.size, "price": p.price}
                        for p in result.candidates
                    ],
                    "message": result.message,
                }
            )
        if result.item is None:
            raise ToolError(result.message)
        return dump(
            {
                "tracked": item_view(
                    {**result.item, "stats": store.item_stats(service.retailer, result.item["product_id"])}
                ),
                "created": result.created,
            }
        )

    @mcp.tool()
    async def untrack_item(product_id: str, retailer: str | None = None) -> str:
        """Stop tracking a product (its history is kept)."""
        retailer = check_retailer(retailer)
        return dump({"untracked": tracking.untrack(store, retailer, product_id), "product_id": product_id})

    @mcp.tool()
    async def price_history(
        product_id: str | None = None, name: str | None = None, days: int = 90, retailer: str | None = None
    ) -> str:
        """A tracked product's observed prices (at most 200 points), stats and recent sales. Give product_id,
        or name (words matched against tracked items, e.g. "full cream milk"). Prices are AUD per pack."""
        retailer = check_retailer(retailer)
        if not product_id:
            if not name:
                raise ToolError("give product_id or name")
            matches = match_tracked(retailer, name)
            if not matches:
                raise ToolError(f"no tracked item matches '{name}'")
            if len(matches) > 1:
                options = ", ".join(f"{m['product_id']} {m.get('label') or m['name']}" for m in matches[:5])
                raise ToolError(f"'{name}' matches {len(matches)} tracked items; give product_id: {options}")
            product_id = matches[0]["product_id"]
        since = utcnow() - timedelta(days=max(1, min(days, 730)))
        points = [
            {
                "at": o["observed_at"],
                "price": o["price"],
                "was_price": o["was_price"],
                "on_special": bool(o["on_special"]),
            }
            for o in store.observations(retailer, product_id, since=since)
        ]
        product = store.get_product(retailer, product_id)
        if product is None:
            raise ToolError(f"{product_id} has never been observed")
        return dump(
            {
                "product_id": product_id,
                "name": product["name"],
                "size": product["size"],
                "currency": "AUD",
                "points": thin(points),
                "stats": store.item_stats(retailer, product_id),
                "sales": store.sale_episodes(retailer, product_id, limit=20),
            }
        )

    @mcp.tool()
    async def current_deals(min_discount: float = 0.0, only_tracked: bool = True, retailer: str | None = None) -> str:
        """Open sale episodes, biggest discount first. discount is a fraction of the baseline price."""
        retailer = check_retailer(retailer)
        deals = []
        for episode in store.open_sale_episodes():
            if episode["retailer"] != retailer or (episode["max_discount"] or 0) < min_discount:
                continue
            item = store.tracked_item(retailer, episode["product_id"])
            if only_tracked and item is None:
                continue
            stats = store.item_stats(retailer, episode["product_id"]) or {}
            deals.append(
                {
                    "product_id": episode["product_id"],
                    "name": (item or {}).get("label") or episode["name"],
                    "size": episode["size"],
                    "price": stats.get("price", episode["last_price"]),
                    "baseline": episode["baseline"],
                    "discount": episode["max_discount"],
                    "heavy": stats.get("heavy"),
                    "on_special": bool(episode["on_special"]),
                    "since": episode["started_at"],
                    "url": episode["url"],
                }
            )
        return dump(deals)

    @mcp.tool()
    async def suggest_items(retailer: str | None = None) -> str:
        """Products the household keeps buying but does not track yet, with a confidence score."""
        return dump(detect.candidates(store, check_retailer(retailer)))

    @mcp.tool()
    async def add_to_cart(product_id: str, quantity: int = 1, retailer: str | None = None) -> str:
        """Add a known product to the owner's REAL Woolworths cart (quantity capped at 6). The owner checks out
        on the Woolworths site. Fails with reconnect instructions if the Woolworths session has expired."""
        check_retailer(retailer)
        outcome = await service.add_to_cart(product_id, quantity, reason="mcp")
        if not outcome.ok:
            raise ToolError(outcome.message)
        return dump({"ok": True, "message": outcome.message, "quantity_in_cart": outcome.quantity_in_cart})

    @mcp.tool()
    async def recent_alerts(limit: int = 20, unsent_only: bool = False) -> str:
        """Recent alerts (on sale, heavy discount, target price, operator), newest first, with whether
        each was delivered. Alerts are kept here even when no notifier is configured."""
        from aus_cartwatch.alerts import describe

        rows = store.recent_alerts(limit=max(1, min(limit, 100)))
        if unsent_only:
            rows = [r for r in rows if not r["sent_at"]]
        return dump(
            [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "product_id": r["product_id"],
                    "text": describe(r),
                    "created_at": r["created_at"],
                    "delivered": bool(r["sent_at"]) and r["channel"] != "superseded",
                    "acted_at": r["acted_at"],
                }
                for r in rows
            ]
        )

    @mcp.tool()
    async def budget_status() -> str:
        """Today's upstream retailer requests against the daily cap, the last refresh run and any backoff."""
        return dump({"version": __version__, **status_payload(service)})

    @mcp.tool()
    async def run_refresh(force: bool = False) -> str:
        """ADMIN: run a price refresh now, within the budget (force bypasses the cap; logged). Hide from agents."""
        result = await service.scheduler.refresh(force=force, note="manual (mcp)")
        return dump(
            {"run_id": result.run_id, "outcome": result.outcome, "note": result.note, "observed": result.observed}
        )

    return mcp


class BearerAuth:
    """Allow `/mcp` only with a configured key; everything else passes through."""

    def __init__(self, app: ASGIApp, key_hashes: Iterable[str], protected_prefix: str = "/mcp"):
        self.app, self.hashes, self.prefix = app, {h.lower() for h in key_hashes}, protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        scheme, _, key = header.partition(" ")
        digest = config.hash_key(key.strip()) if scheme.lower() == "bearer" and key.strip() else ""
        if not digest or not any(hmac.compare_digest(digest, h) for h in self.hashes):
            response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def metrics_text(service: Service) -> str:
    store = service.store
    now = service.clock()
    lines = [
        "# HELP aus_cartwatch_runs_total Runs by kind and outcome (all time).",
        "# TYPE aus_cartwatch_runs_total counter",
    ]
    for row in store._all(
        "SELECT kind, COALESCE(outcome, 'running') AS outcome, COUNT(*) AS n FROM runs GROUP BY 1, 2"
    ):
        lines.append(f'aus_cartwatch_runs_total{{kind="{row["kind"]}",outcome="{row["outcome"]}"}} {row["n"]}')
    last_ok = store._one(
        "SELECT MAX(finished_at) AS at FROM runs WHERE kind = 'refresh' AND outcome IN ('ok', 'partial')"
    )["at"]
    from aus_cartwatch.store import parse_time

    pending = store._one("SELECT COUNT(*) AS n FROM alerts WHERE sent_at IS NULL")["n"]
    gauges = {
        "aus_cartwatch_upstream_requests_today": service.scheduler.used_today(now),
        "aus_cartwatch_daily_upstream_cap": service.scheduler.daily_cap,
        "aus_cartwatch_tracked_items": len(store.list_tracked()),
        "aus_cartwatch_alerts_pending": pending,
        "aus_cartwatch_open_sale_episodes": len(store.open_sale_episodes()),
        "aus_cartwatch_failure_streak": len(service.scheduler.failure_streak()),
        "aus_cartwatch_last_successful_refresh_timestamp_seconds": int(parse_time(last_ok).timestamp())
        if last_ok
        else 0,
    }
    for name, value in gauges.items():
        lines += [f"# TYPE {name} gauge", f"{name} {value}"]
    return "\n".join(lines) + "\n"


def create_app(
    store: Store | None = None,
    *,
    service: Service | None = None,
    mcp_key_hashes: Iterable[str] | None = None,
    start_background: bool = True,
) -> Starlette:
    """The ASGI app. Applies pending migrations first; tests inject a store or a whole service."""
    if service is None:
        store = store or Store(config.db_path())
        store.migrate()
        service = Service(store)
    else:
        service.store.migrate()
    mcp_app = build(service).streamable_http_app(stateless_http=True, json_response=True, host=config.host())

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "schema_version": service.store.schema_version(),
                **status_payload(service),
            }
        )

    async def metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(metrics_text(service), media_type="text/plain; version=0.0.4")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(app):
            if start_background:
                service.start()
            try:
                yield
            finally:
                await service.stop()

    routes = [*mcp_app.routes, Route("/healthz", healthz), Route("/metrics", metrics), *web.routes(service)]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.service = service
    app.add_middleware(BearerAuth, key_hashes=config.mcp_key_hashes() if mcp_key_hashes is None else mcp_key_hashes)
    return app
