"""Tailnet-only web UI (CW-30): tracked items, deals, candidates, item history, settings and runs.

Server-rendered Jinja2 with plain HTML forms (POST, then redirect), no build step
and no JavaScript, so it works on a phone in a supermarket aisle. Charts are
inline SVG. A household password protects every page (`AUS_CARTWATCH_UI_PASSWORD_HASH`);
without one configured the UI stays locked. Secrets are never rendered.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from aus_cartwatch import __version__, config, detect, receipts, tracking
from aus_cartwatch.alerts import describe as describe_alert
from aus_cartwatch.policy import Policy
from aus_cartwatch.store import parse_time, utcnow
from aus_cartwatch.web import auth

if TYPE_CHECKING:
    from aus_cartwatch.service import Service

WOOLWORTHS_IMAGES = "https://cdn0.woolworths.media/content/wowproductimages/medium"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def money(value) -> str:
    return f"${value:.2f}" if isinstance(value, int | float) else "–"


def percent(value) -> str:
    return f"{round((value or 0) * 100)}%"


templates.env.filters["money"] = money
templates.env.filters["percent"] = percent
templates.env.filters["when"] = lambda v: parse_time(v).astimezone().strftime("%a %d %b %H:%M") if v else "–"


def sparkline(points: list[float | None], *, width: int = 120, height: int = 28) -> str:
    """An inline SVG polyline of prices (None gaps skipped)."""
    values = [p for p in points if p is not None]
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1
    step = width / (len(values) - 1)
    coords = " ".join(
        f"{i * step:.1f},{height - 2 - (v - low) / span * (height - 4):.1f}" for i, v in enumerate(values)
    )
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        f'aria-label="price trend"><polyline fill="none" stroke="currentColor" stroke-width="1.5" '
        f'points="{coords}"/></svg>'
    )


def chart(observations: list[dict], *, width: int = 640, height: int = 200) -> str:
    """A full history chart: price line, special observations marked, min/max labels."""
    rows = [o for o in observations if o.get("price") is not None]
    if len(rows) < 2:
        return ""
    prices = [o["price"] for o in rows]
    low, high = min(prices), max(prices)
    span = (high - low) or 1
    pad = 24
    step = (width - 2 * pad) / (len(rows) - 1)

    def y(value: float) -> float:
        return height - pad - (value - low) / span * (height - 2 * pad)

    points = " ".join(f"{pad + i * step:.1f},{y(o['price']):.1f}" for i, o in enumerate(rows))
    dots = "".join(
        f'<circle cx="{pad + i * step:.1f}" cy="{y(o["price"]):.1f}" r="3" class="special"/>'
        for i, o in enumerate(rows)
        if o.get("on_special")
    )
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="price history">'
        f'<text x="2" y="{y(high) + 4:.0f}" class="axis">{money(high)}</text>'
        f'<text x="2" y="{y(low) + 4:.0f}" class="axis">{money(low)}</text>'
        f'<polyline fill="none" stroke="currentColor" stroke-width="2" points="{points}"/>{dots}</svg>'
    )


def _secret() -> str:
    return config.secret() or hashlib.sha256(f"acw-ui:{config.ui_password_hash()}".encode()).hexdigest()


def _authed(request: Request) -> bool:
    return bool(config.ui_password_hash()) and auth.valid(_secret(), request.cookies.get(auth.COOKIE))


def routes(service: Service) -> list[Route]:
    store = service.store
    retailer = service.retailer

    def photo_src(product_retailer: str, product_id: str, image_url: str | None = None) -> str:
        """A stored photo if we have one, else the retailer's image URL (loaded by the browser), else ''."""
        if product_id in stored_photos(product_retailer):
            return f"/images/{product_retailer}/{product_id}"
        if not image_url:
            image_url = (store.get_product(product_retailer, product_id) or {}).get("image_url") or ""
        if not image_url and product_retailer == "woolworths" and str(product_id).isdigit():
            # Cart lines carry no photo link: Woolworths photos live at a fixed path by stockcode.
            image_url = f"{WOOLWORTHS_IMAGES}/{int(product_id):06d}.jpg"
        return image_url if image_url.startswith("https://") else ""

    def stored_photos(product_retailer: str) -> set[str]:
        return store.has_images(product_retailer)

    def page(request: Request, name: str, **context) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        flash = request.query_params.get("msg")
        return templates.TemplateResponse(
            request,
            name,
            {"version": __version__, "flash": flash, "nav": name.split(".")[0], "photo_src": photo_src, **context},
        )

    def back(path: str, message: str) -> RedirectResponse:
        from urllib.parse import quote

        return RedirectResponse(f"{path}?msg={quote(message)}", status_code=303)

    def item_or_404(item_id: str) -> dict | None:
        try:
            return store.tracked_item_by_id(int(item_id))
        except ValueError:
            return None

    async def login(request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            stored = config.ui_password_hash()
            if stored and auth.verify_password(str(form.get("password") or ""), stored):
                response = RedirectResponse("/", status_code=303)
                response.set_cookie(
                    auth.COOKIE,
                    auth.issue(_secret()),
                    max_age=auth.SESSION_SECONDS,
                    httponly=True,
                    samesite="strict",
                    secure=request.url.scheme == "https",
                )
                return response
            return templates.TemplateResponse(
                request, "login.html", {"error": "Wrong password", "locked": not stored}, status_code=401
            )
        return templates.TemplateResponse(request, "login.html", {"locked": not config.ui_password_hash()})

    async def logout(request: Request) -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE)
        return response

    async def home(request: Request) -> Response:
        since = utcnow() - timedelta(days=90)
        items = tracking.list_tracked(store, retailer)
        for item in items:
            item["spark"] = sparkline(
                [o["price"] for o in store.observations(retailer, item["product_id"], since=since)]
            )
        return page(
            request,
            "index.html",
            items=items,
            max_quantity=config.max_add_quantity(),
            photos=store.has_images(retailer),
        )

    async def deals(request: Request) -> Response:
        rows = []
        for episode in store.open_sale_episodes():
            item = store.tracked_item(episode["retailer"], episode["product_id"])
            stats = store.item_stats(episode["retailer"], episode["product_id"]) or {}
            rows.append({**episode, "item": item, "stats": stats})
        return page(
            request, "deals.html", deals=rows, max_quantity=config.max_add_quantity(), photos=store.has_images(retailer)
        )

    async def candidates(request: Request) -> Response:
        return page(
            request, "candidates.html", candidates=detect.candidates(store, retailer), photos=store.has_images(retailer)
        )

    async def item(request: Request) -> Response:
        row = item_or_404(request.path_params["item_id"])
        if row is None:
            return HTMLResponse("Not found", status_code=404)
        observations = store.observations(row["retailer"], row["product_id"])
        return page(
            request,
            "item.html",
            item=row,
            stats=store.item_stats(row["retailer"], row["product_id"]) or {},
            chart=chart(observations[-500:]),
            observations=list(reversed(observations[-30:])),
            episodes=store.sale_episodes(row["retailer"], row["product_id"]),
            actions=store.cart_actions(retailer=row["retailer"], product_id=row["product_id"], limit=20),
            max_quantity=config.max_add_quantity(),
            photo=store.get_image(row["retailer"], row["product_id"]) is not None,
        )

    async def item_action(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        row = item_or_404(request.path_params["item_id"])
        if row is None:
            return HTMLResponse("Not found", status_code=404)
        form = await request.form()
        action = request.path_params["action"]
        target = str(form.get("next") or f"/items/{row['id']}")
        if not target.startswith("/"):
            target = "/"
        try:
            if action == "add":
                outcome = await service.add_to_cart(row["product_id"], float(form.get("quantity") or 1), reason="ui")
                return back(target, outcome.message)
            if action == "target":
                value = str(form.get("target_price") or "").strip()
                tracking.set_target_price(store, row["retailer"], row["product_id"], float(value) if value else None)
                return back(target, "Target saved")
            if action == "threshold":
                value = str(form.get("threshold") or "").strip()
                tracking.set_threshold(store, row["retailer"], row["product_id"], float(value) / 100 if value else None)
                return back(target, "Threshold saved")
            if action == "snooze":
                from aus_cartwatch.cart import snooze

                return back(target, snooze(store, row["retailer"], row["product_id"]))
            if action == "untrack":
                tracking.untrack(store, row["retailer"], row["product_id"])
                return back("/", f"Stopped tracking {row['name']}")
        except ValueError as exc:
            return back(target, f"Not saved: {exc}")
        return HTMLResponse("Unknown action", status_code=400)

    async def track(request: Request) -> Response:
        if request.method == "GET":
            return page(request, "track.html", candidates=[], query="", from_cache=False)
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        reference = str(form.get("reference") or "").strip()
        if not reference:
            return back("/track", "Type a search, a product id or a product link")
        try:
            if tracking.parse_reference(reference)[0] == "query":
                products, from_cache = await service.search(reference)
                tracked = {i["product_id"] for i in store.list_tracked(retailer=retailer)}
                return page(
                    request, "track.html", candidates=products, query=reference, from_cache=from_cache, tracked=tracked
                )
            result = await service.track(reference)
        except Exception as exc:  # shown to the owner; aus-cart-mcp messages are safe to show
            return back("/track", f"Lookup failed: {exc}")
        return back("/" if result.item else "/track", result.message)

    async def track_selected(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        field = "all_product_id" if form.get("select") == "all" else "product_id"
        ids = [str(v) for v in form.getlist(field)]
        if not ids:
            return back("/track", "Tick at least one product")
        try:
            results = await service.track_many(ids)
        except Exception as exc:
            return back("/track", f"Tracking failed: {exc}")
        created = sum(r.created for r in results)
        missing = [r.message for r in results if r.item is None]
        message = f"Tracked {created} new item{'s' if created != 1 else ''}"
        if len(results) - created - len(missing):
            message += f", updated {len(results) - created - len(missing)}"
        if missing:
            message += f"; not found: {'; '.join(missing)}"
        return back("/", message)

    async def cart_page(request: Request) -> Response:
        snapshot = store.last_cart_snapshot(retailer)
        tracked = {i["product_id"] for i in store.list_tracked(retailer=retailer)}
        return page(request, "cart.html", snapshot=snapshot, tracked=tracked)

    async def cart_refresh(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        result = await service.scheduler.snapshot()
        message = {
            "ok": f"Cart read from Woolworths: {result.note}",
            "session_required": "Woolworths session expired: reconnect it in the myaiagent app",
            "blocked": f"Woolworths is refusing requests right now; try later ({result.note})",
        }.get(result.outcome, f"Could not read the cart: {result.note}")
        return back("/cart", message)

    async def cart_track(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        field = "all_product_id" if form.get("select") == "all" else "product_id"
        ids = [str(v) for v in form.getlist(field)]
        if not ids:
            return back("/cart", "Tick at least one product")
        results = tracking.Tracker(store, None, retailer).track_cart_lines(ids)
        created = sum(r.created for r in results)
        return back("/cart", f"Tracked {created} new item{'s' if created != 1 else ''} from the cart")

    async def image(request: Request) -> Response:
        if not _authed(request):
            return Response(status_code=401)
        found = store.get_image(request.path_params["retailer"], request.path_params["product_id"])
        if found is None:
            return Response(status_code=404)
        data, content_type = found
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=604800"})

    async def track_cart(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        try:
            results = await service.track_from_cart()
        except Exception as exc:
            return back("/track", f"Could not read the cart: {exc}")
        return back("/", f"Tracked {sum(r.created for r in results)} new items from the cart")

    async def candidate_action(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        product_id, action = request.path_params["product_id"], request.path_params["action"]
        if action == "track":
            return back("/candidates", service.accept_candidate(product_id).message)
        detect.dismiss_candidate(store, retailer, product_id)
        return back("/candidates", "Dismissed")

    async def settings(request: Request) -> Response:
        if request.method == "POST":
            if not _authed(request):
                return RedirectResponse("/login", status_code=303)
            form = await request.form()
            values = {k: str(v).strip() for k, v in form.items() if str(v).strip()}
            try:
                Policy.save(store, **values)
            except (ValueError, TypeError) as exc:
                return back("/settings", f"Not saved: {exc}")
            return back("/settings", "Settings saved")
        from aus_cartwatch.server import status_payload

        return page(
            request,
            "settings.html",
            policy=Policy.load(store),
            status=status_payload(service),
            notifiers=[n.name for n in service.notifiers],
            chat_count=len(config.telegram_chat_ids()),
        )

    async def alerts_page(request: Request) -> Response:
        rows = store.recent_alerts(limit=100)
        for row in rows:
            row["text"] = describe_alert(row)
        return page(request, "alerts.html", alerts=rows, notifiers=[n.name for n in service.notifiers])

    async def alert_action(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        action = f"{request.path_params['action']}:{request.path_params['alert_id']}:{form.get('quantity') or 1}"
        message = await service.handle_action(
            action if request.path_params["action"] == "add" else action.rsplit(":", 1)[0]
        )
        return back("/alerts", message)

    async def receipts_page(request: Request) -> Response:
        if request.method == "POST":
            if not _authed(request):
                return RedirectResponse("/login", status_code=303)
            form = await request.form()
            upload = form.get("photo")
            if upload is None or not hasattr(upload, "read"):
                return back("/receipts", "Choose a photo of the receipt")
            data = await upload.read()
            try:
                receipt_id = await service.upload_receipt(data, upload.content_type or "")
            except receipts.ReceiptError as exc:
                return back("/receipts", f"Could not read the receipt: {exc}")
            return back(f"/receipts/{receipt_id}", "Receipt read; matching its lines to Woolworths products")
        return page(request, "receipts.html", receipts=store.list_receipts(), ocr_ready=bool(config.openrouter_key()))

    async def receipt_page(request: Request) -> Response:
        receipt = store.get_receipt(request.path_params["receipt_id"])
        if receipt is None:
            return HTMLResponse("Not found", status_code=404)
        lines = store.receipt_lines(receipt["id"])
        tracked = {i["product_id"] for i in store.list_tracked(retailer=retailer)}
        counts: dict[str, int] = {}
        for line in lines:
            counts[line["match_status"]] = counts.get(line["match_status"], 0) + 1
        return page(request, "receipt.html", receipt=receipt, lines=lines, tracked=tracked, counts=counts)

    async def receipt_action(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        receipt_id = request.path_params["receipt_id"]
        if store.get_receipt(receipt_id) is None:
            return HTMLResponse("Not found", status_code=404)
        if request.path_params["action"] == "match":
            service.start_matching(receipt_id)
            return back(f"/receipts/{receipt_id}", "Matching the remaining lines")
        if request.path_params["action"] == "rematch":
            receipts.reset_matches(store, receipt_id)
            service.start_matching(receipt_id)
            return back(f"/receipts/{receipt_id}", "Matching again (recent searches come from the cache)")
        form = await request.form()
        chosen = {}
        for key, value in form.multi_items():
            if key.startswith("line_") and str(value).strip():
                chosen[int(key.removeprefix("line_"))] = str(value).strip()
        result = receipts.confirm(store, receipt_id, chosen, retailer=retailer)
        return back(
            f"/receipts/{receipt_id}",
            f"Saved: {result['tracked']} lines tracked ({result['created']} new); the shop is recorded for suggestions",
        )

    async def runs(request: Request) -> Response:
        return page(request, "runs.html", runs=store.recent_runs(limit=60), ledger=store.ledger(limit=100))

    async def refresh_now(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        result = await service.scheduler.refresh(note="manual (ui)")
        return back("/runs", f"Refresh {result.outcome}: {result.observed} observed. {result.note}".strip())

    return [
        Route("/", home),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/deals", deals),
        Route("/candidates", candidates),
        Route("/candidates/{product_id}/{action:str}", candidate_action, methods=["POST"]),
        Route("/items/{item_id}", item),
        Route("/items/{item_id}/{action:str}", item_action, methods=["POST"]),
        Route("/track", track, methods=["GET", "POST"]),
        Route("/track/cart", track_cart, methods=["POST"]),
        Route("/track/selected", track_selected, methods=["POST"]),
        Route("/images/{retailer}/{product_id}", image),
        Route("/cart", cart_page),
        Route("/cart/refresh", cart_refresh, methods=["POST"]),
        Route("/cart/track", cart_track, methods=["POST"]),
        Route("/settings", settings, methods=["GET", "POST"]),
        Route("/runs", runs),
        Route("/alerts", alerts_page),
        Route("/receipts", receipts_page, methods=["GET", "POST"]),
        Route("/receipts/{receipt_id:int}", receipt_page),
        Route("/receipts/{receipt_id:int}/{action:str}", receipt_action, methods=["POST"]),
        Route("/alerts/{alert_id:int}/{action:str}", alert_action, methods=["POST"]),
        Route("/runs/refresh", refresh_now, methods=["POST"]),
    ]
