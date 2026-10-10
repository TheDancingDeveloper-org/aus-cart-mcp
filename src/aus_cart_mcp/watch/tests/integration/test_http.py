"""The whole HTTP app over real sockets: MCP tools with bearer auth, the UI behind its login, health and metrics.

aus-cart-mcp is replaced by the in-memory fake here; tests/e2e drives the real one.
"""

import json
import re
from datetime import timedelta

import httpx
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aus_cartwatch import __version__, config
from aus_cartwatch.server import create_app, thin
from aus_cartwatch.service import Service
from aus_cartwatch.store import bundled_migrations
from aus_cartwatch.web.auth import hash_password
from tests.conftest import T0, serve, stop

KEY = "test-mcp-key"
PASSWORD = "household password"


@pytest.fixture()
async def app_url(store, fake, clock, monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_UI_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("AUS_CARTWATCH_SECRET", "cookie-secret")
    service = Service(
        store, client_factory=fake.factory(store, clock=clock), notifiers=[], clock=clock, sleep=clock.sleep
    )
    app = create_app(service=service, mcp_key_hashes=[config.hash_key(KEY)], start_background=False)
    server = await serve(app)
    try:
        yield server[2]
    finally:
        await stop(server[:2])


def mcp(url, key=KEY):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"} if key else {})
    return Client(streamable_http_client(f"{url}/mcp", http_client=http))


async def call(client, tool, args=None):
    result = await client.call_tool(tool, args or {})
    text = result.content[0].text
    return (json.loads(text) if not result.is_error else text), result.is_error


async def test_healthz_and_metrics(app_url):
    async with httpx.AsyncClient(base_url=app_url) as http:
        health = (await http.get("/healthz")).json()
        metrics = (await http.get("/metrics")).text
    assert health["ok"] and health["version"] == __version__
    assert health["schema_version"] == len(bundled_migrations())
    assert health["daily_cap"] == 60 and health["upstream_today"] == 0 and health["last_refresh"] is None
    assert "aus_cartwatch_daily_upstream_cap 60" in metrics and "aus_cartwatch_tracked_items 0" in metrics


@pytest.mark.parametrize("key", [None, "wrong"])
async def test_mcp_refuses_missing_or_wrong_keys(app_url, key):
    async with httpx.AsyncClient(base_url=app_url) as http:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        response = await http.post("/mcp", headers=headers, json={})
    assert response.status_code == 401


async def test_mcp_tools_end_to_end(app_url, fake):
    async with mcp(app_url) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"list_tracked", "track_item", "untrack_item", "price_history", "current_deals", "suggest_items",
                "add_to_cart", "budget_status", "run_refresh"} <= names  # fmt: skip
        assert all(len(f"aus_cartwatch__{n}") <= 64 for n in names)

        candidates, _ = await call(c, "track_item", {"query": "milk"})
        assert candidates["candidates"][0]["product_id"] == "1"
        tracked, _ = await call(c, "track_item", {"product_id": "1", "target_price": 4.0})
        assert tracked["created"] and tracked["tracked"]["price"] == 4.95
        await call(c, "track_item", {"url": "https://www.woolworths.com.au/shop/productdetails/2/bread"})
        _, error = await call(c, "track_item", {})
        assert error
        _, error = await call(c, "track_item", {"product_id": "1", "threshold": 3})
        assert error
        _, error = await call(c, "list_tracked", {"retailer": "coles"})
        assert error

        fake.set_price("1", 2.0, special=True, was=4.95)
        refresh, _ = await call(c, "run_refresh")
        assert refresh["outcome"] == "ok" and refresh["observed"] == 2

        items, _ = await call(c, "list_tracked")
        milk = next(i for i in items if i["product_id"] == "1")
        assert milk["price"] == 2.0 and milk["heavy"] and milk["target_met"]
        deals, _ = await call(c, "current_deals", {"min_discount": 0.3})
        assert [d["product_id"] for d in deals] == ["1"]
        history, _ = await call(c, "price_history", {"product_id": "1"})
        assert [p["price"] for p in history["points"]] == [4.95, 2.0] and history["stats"]["baseline"] == 4.95
        _, error = await call(c, "price_history", {"product_id": "nope"})
        assert error

        added, _ = await call(c, "add_to_cart", {"product_id": "1", "quantity": 2})
        assert added["ok"] and fake.cart == {"1": 2}
        _, error = await call(c, "add_to_cart", {"product_id": "1", "quantity": 50})
        assert error

        suggestions, _ = await call(c, "suggest_items")
        assert suggestions == []
        status, _ = await call(c, "budget_status")
        assert status["upstream_today"] >= 4 and status["last_refresh"]["outcome"] == "ok"
        untracked, _ = await call(c, "untrack_item", {"product_id": "2"})
        assert untracked["untracked"]


def test_thin_keeps_the_last_point():
    points = [{"i": i} for i in range(1000)]
    thinned = thin(points, 200)
    assert len(thinned) == 200 and thinned[-1] == {"i": 999} and thin(points[:5]) == points[:5]


async def login(http):
    response = await http.post("/login", data={"password": PASSWORD})
    assert response.status_code in (200, 303) and response.url.path == "/"


async def test_ui_requires_login(app_url):
    async with httpx.AsyncClient(base_url=app_url) as http:
        assert (await http.get("/")).headers["location"] == "/login"
        assert (await http.post("/items/1/add")).status_code == 303
        assert (await http.post("/login", data={"password": "nope"})).status_code == 401
        page = (await http.get("/login")).text
    assert "locked" not in page


async def test_ui_locked_without_a_password(app_url, monkeypatch):
    monkeypatch.delenv("AUS_CARTWATCH_UI_PASSWORD_HASH")
    async with httpx.AsyncClient(base_url=app_url) as http:
        assert "The web UI is locked" in (await http.get("/login")).text
        assert (await http.post("/login", data={"password": PASSWORD})).status_code == 401


async def test_ui_pages_and_actions(app_url, fake, store):
    async with httpx.AsyncClient(base_url=app_url, follow_redirects=True) as http:
        await login(http)
        assert "Nothing tracked yet" in (await http.get("/")).text
        found = (await http.post("/track", data={"reference": "bread"})).text
        assert "Pick what to track" in found and "Wholemeal Bread" in found and "Fresh from Woolworths" in found
        assert 'name="product_id" value="2"' in found and "wowproductimages/medium/000002.jpg" in found
        again = (await http.post("/track", data={"reference": "bread"})).text
        assert "Shown from the local cache" in again
        assert fake.calls.count("search_products") == 1
        page = (await http.post("/track", data={"reference": "2"})).text
        assert "now tracking Wholemeal Bread" in page
        assert fake.calls.count("search_products") == 1  # tracked from the cached search
        assert (await http.get("/images/woolworths/2")).status_code == 404  # not fetched server-side by default
        assert "wowproductimages/medium/000002.jpg" in (await http.get("/")).text  # the browser loads it
        fake.cart = {"1": 1, "3": 2}
        page = (await http.post("/track/cart")).text
        assert "Tracked 2 new items from the cart" in page
        assert "missing" not in (await http.post("/track", data={"reference": ""})).text

        fake.set_price("2", 1.75, special=True, was=3.5)
        assert "Refresh ok" in (await http.post("/runs/refresh")).text
        index = (await http.get("/")).text
        assert "50% off" in index and "Full Cream Milk" in index
        item_id = store.tracked_item("woolworths", "2")["id"]
        item = (await http.get(f"/items/{item_id}")).text
        assert "Wholemeal Bread" in item and "<svg" in item
        assert "Added 1 × Wholemeal Bread" in (await http.post(f"/items/{item_id}/add", data={"quantity": "1"})).text
        assert "Target saved" in (await http.post(f"/items/{item_id}/target", data={"target_price": "2"})).text
        assert "Not saved" in (await http.post(f"/items/{item_id}/target", data={"target_price": "-1"})).text
        assert "Threshold saved" in (await http.post(f"/items/{item_id}/threshold", data={"threshold": "25"})).text
        assert "Snoozed" in (await http.post(f"/items/{item_id}/snooze")).text
        deals = (await http.get("/deals")).text
        assert "Wholemeal Bread" in deals and "50% off" in deals
        assert "Suggestions" in (await http.get("/candidates")).text
        settings = (await http.get("/settings")).text
        assert "Budget today" in settings and "nowhere (no notifier configured)" in settings
        assert "Settings saved" in (await http.post("/settings", data={"jitter_minutes": "5"})).text
        assert "Not saved" in (await http.post("/settings", data={"jitter_minutes": "x"})).text
        runs = (await http.get("/runs")).text
        assert "search_products" in runs
        assert "Stopped tracking" in (await http.post(f"/items/{item_id}/untrack")).text
        assert (await http.get("/items/99999")).status_code == 404
        assert (await http.post("/items/x/add")).status_code == 404
        assert (await http.post(f"/items/{item_id}/explode")).status_code == 400
        # nothing secret is rendered anywhere
        for path in ("/", "/settings", "/runs", "/deals", f"/items/{item_id}"):
            text = (await http.get(path)).text
            assert KEY not in text and "cookie-secret" not in text and not re.search(r"scrypt[:$]", text)
        await http.post("/logout")
        assert (await http.get("/")).url.path == "/login"


async def test_ui_multi_select(app_url, fake, store):
    async with httpx.AsyncClient(base_url=app_url, follow_redirects=True) as http:
        assert (await http.get("/images/woolworths/1")).status_code == 401
        await login(http)
        assert "Tick at least one" in (await http.post("/track/selected", data={})).text
        await http.post("/track", data={"reference": "milk"})
        page = (await http.post("/track/selected", data={"product_id": ["1", "3", "999"]})).text
        assert "Tracked 2 new items" in page and "not found" in page
        assert {i["product_id"] for i in store.list_tracked()} == {"1", "3"}
        assert "already tracked" in (await http.post("/track", data={"reference": "milk"})).text
        assert "updated 1" in (await http.post("/track/selected", data={"product_id": "1"})).text
        page = (await http.post("/track", data={"reference": "bread"})).text
        assert "Track all 1" in page
        assert (
            "Tracked 1 new item"
            in (await http.post("/track/selected", data={"select": "all", "all_product_id": "2"})).text
        )


async def test_stored_photos_are_served(app_url, store):
    store.upsert_product("woolworths", "1", name="Milk")
    store.track("woolworths", "1")
    store.put_image("woolworths", "1", b"\xff\xd8x", "image/jpeg")
    async with httpx.AsyncClient(base_url=app_url, follow_redirects=True) as http:
        await login(http)
        photo = await http.get("/images/woolworths/1")
        assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
        assert '/images/woolworths/1"' in (await http.get("/")).text
        assert (await http.get("/images/woolworths/999")).status_code == 404


async def test_cart_page(app_url, fake, store):
    async with httpx.AsyncClient(base_url=app_url, follow_redirects=True) as http:
        await login(http)
        assert "No cart read yet" in (await http.get("/cart")).text
        fake.cart = {"1": 2, "3": 1}
        page = (await http.post("/cart/refresh")).text
        assert "Cart read from Woolworths" in page and "Full Cream Milk" in page and "× 2" in page
        assert "wowproductimages/medium/000001.jpg" in page and "wowproductimages/medium/000003.jpg" in page
        assert "Tracked 1 new item" in (await http.post("/cart/track", data={"product_id": "1"})).text
        assert (
            "Tracked 1 new item" in (await http.post("/cart/track", data={"select": "all", "all_product_id": "3"})).text
        )
        assert "Tick at least one" in (await http.post("/cart/track", data={})).text
        page = (await http.get("/cart")).text
        assert "Track all untracked" not in page and page.count("· tracked") == 2
        fake.session = False
        assert "session expired" in (await http.post("/cart/refresh")).text
        fake.session = True
        fake.fail_with = __import__("aus_cartwatch.auscart", fromlist=["Blocked"]).Blocked("refusing (HTTP 403)")
        assert "refusing requests right now" in (await http.post("/cart/refresh")).text


async def test_ui_candidates_flow(app_url, store):
    from aus_cartwatch import detect
    from aus_cartwatch.auscart import Cart, CartLine

    for _ in range(2):
        detect.process_snapshot(store, "woolworths", Cart([CartLine("3", "Free Range Eggs 12pk", 1, 6.8)]))
        detect.process_snapshot(store, "woolworths", Cart([]))
    async with httpx.AsyncClient(base_url=app_url, follow_redirects=True) as http:
        await login(http)
        assert "Free Range Eggs" in (await http.get("/candidates")).text
        assert "now tracking" in (await http.post("/candidates/3/track")).text
        detect.process_snapshot(store, "woolworths", Cart([CartLine("2", "Bread", 1, 3.5)]))
        detect.process_snapshot(store, "woolworths", Cart([]))
        detect.process_snapshot(store, "woolworths", Cart([CartLine("2", "Bread", 1, 3.5)]))
        detect.process_snapshot(store, "woolworths", Cart([]))
        assert "Dismissed" in (await http.post("/candidates/2/dismiss")).text


async def test_alerts_page_and_tool(app_url, store, fake):
    from aus_cartwatch import alerts as alert_rules
    from aus_cartwatch.auscart import Product
    from aus_cartwatch.tracking import observe

    store.upsert_product("woolworths", "1", name="Full Cream Milk 3L")
    store.track("woolworths", "1")
    observe(store, "woolworths", Product("1", "Full Cream Milk 3L", 2.0, was_price=4.0, on_special=True))
    alert_rules.evaluate(store, "woolworths")
    alert_rules.operator(store, "operator:x", "something to look at")
    heavy = next(a for a in store.recent_alerts() if a["kind"] == "heavy_discount")
    async with httpx.AsyncClient(base_url=app_url, follow_redirects=True) as http:
        await login(http)
        page = (await http.get("/alerts")).text
        assert "heavy discount" in page and "not sent" in page and "something to look at" in page
        assert "No notifier is configured" in page
        added = (await http.post(f"/alerts/{heavy['id']}/add", data={"quantity": "2"})).text
        assert "Added 2 × Full Cream Milk" in added and fake.cart == {"1": 2}
        assert "acted on" in (await http.get("/alerts")).text
        assert "Snoozed" in (await http.post(f"/alerts/{heavy['id']}/snooze")).text
    async with mcp(app_url) as c:
        listed, _ = await call(c, "recent_alerts", {"unsent_only": True})
        assert {a["kind"] for a in listed} >= {"heavy_discount", "operator"} and not any(a["delivered"] for a in listed)


async def test_price_history_by_name(app_url, fake):
    async with mcp(app_url) as c:
        await call(c, "track_item", {"product_id": "1"})
        await call(c, "track_item", {"product_id": "2"})
        history, _ = await call(c, "price_history", {"name": "full cream milk"})
        assert history["product_id"] == "1" and history["currency"] == "AUD"
        _, error = await call(c, "price_history", {"name": "nothing like this"})
        assert error
        _, error = await call(c, "price_history", {})
        assert error
        await call(c, "track_item", {"product_id": "3"})
        ambiguous, error = await call(c, "price_history", {"name": "e"})  # matches several items
        assert error and "give product_id" in ambiguous
        items, _ = await call(c, "list_tracked")
        assert "unit_price" in items[0]


async def test_receipt_upload_match_and_confirm(store, clock, tmp_path, monkeypatch):
    import asyncio

    from tests.conftest import FakeAusCart, product
    from tests.unit.test_receipts import fake_extractor

    monkeypatch.setenv("AUS_CARTWATCH_UI_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("AUS_CARTWATCH_SECRET", "cookie-secret")
    monkeypatch.setenv("AUS_CARTWATCH_RECEIPTS_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv("AUS_CARTWATCH_OPENROUTER_KEY", "k")
    prices = FakeAusCart({"88436": product("88436", "Dairy Farmers Full Cream Milk 2L", 4.7, size="2L")})
    service = Service(store, client_factory=prices.factory(store, clock=clock), notifiers=[], clock=clock,
                      receipt_extractor=fake_extractor, tz="Australia/Sydney")  # fmt: skip
    service.scheduler.sleep = lambda seconds: asyncio.sleep(0)
    server = await serve(create_app(service=service, mcp_key_hashes=[], start_background=False))
    try:
        async with httpx.AsyncClient(base_url=server[2], follow_redirects=True) as http:
            await login(http)
            assert "Read receipt" in (await http.get("/receipts")).text
            files = {"photo": ("receipt.jpg", b"\xff\xd8synthetic", "image/jpeg")}
            page = await http.post("/receipts", files=files)
            receipt_id = int(page.url.path.rsplit("/", 1)[1])
            for _ in range(50):
                if store.get_receipt(receipt_id)["status"] != "matching" and all(
                    line["match_status"] != "pending" for line in store.receipt_lines(receipt_id)
                ):
                    break
                await asyncio.sleep(0.05)
            page = (await http.get(f"/receipts/{receipt_id}")).text
            assert "loose produce" in page and "Dairy Farmers Full Cream Milk 2L" in page and "checked" in page
            milk = next(line for line in store.receipt_lines(receipt_id) if line["product_id"] == "88436")
            saved = (await http.post(f"/receipts/{receipt_id}/confirm", data={f"line_{milk['id']}": "88436"})).text
            assert "1 lines tracked (1 new)" in saved
            again = await http.post("/receipts", files=files)  # the same photo: no second OCR call
            assert again.url.path == f"/receipts/{receipt_id}"
            assert "Choose a photo" in (await http.post("/receipts", data={"x": "1"})).text
            bad = await http.post("/receipts", files={"photo": ("r.pdf", b"%PDF", "application/pdf")})
            assert "Could not read the receipt" in bad.text
            assert (await http.get("/receipts/999")).status_code == 404
            assert "Matching the remaining" in (await http.post(f"/receipts/{receipt_id}/match")).text
            assert "Woolworths Test Store" in (await http.get("/receipts")).text
    finally:
        await stop(server[:2])
    assert store.runs_since("ocr", T0 - timedelta(days=30)) == 1
