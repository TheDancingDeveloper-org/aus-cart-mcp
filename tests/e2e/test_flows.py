"""MVP1 flows end to end: aus_cartwatch → real aus-cart-mcp → mock Woolworths, alerts through a fake Telegram."""

import httpx

from aus_cartwatch import budget
from aus_cartwatch.alerts.telegram import TelegramNotifier
from aus_cartwatch.service import Service
from tests.unit.test_notifiers import TOKEN, FakeBot, press

MILK, CHOC = "888140", "6073909"


def service_for(stack, store, clock, **kw):
    def factory(run_id=None):
        return budget.client(store, "woolworths", run_id, clock=clock, url=stack.url, key=stack.key, retry_delay=0)

    return Service(store, client_factory=factory, clock=clock, sleep=clock.sleep, tz="Australia/Sydney", **kw)


async def test_track_from_cart_creates_items_observations_and_ledger_rows(aus_cart, store, clock, monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_FETCH_PHOTOS", "1")
    await aus_cart.connect_session()
    service = service_for(aus_cart, store, clock, notifiers=[])
    async with service.client() as client:
        await client.add_to_cart({MILK: 1, CHOC: 2})
    results = await service.track_from_cart()
    assert sorted(r.item["product_id"] for r in results) == sorted([MILK, CHOC])
    assert all(r.item["source"] == "cart" for r in results)
    assert len(store.observations("woolworths", MILK)) == 1 and len(store.observations("woolworths", CHOC)) == 1
    tools = [r["tool"] for r in reversed(store.ledger())]
    assert tools[-3:] == ["get_cart", "get_product_image", "get_product_image"]  # one cart read, then each photo once
    assert store.has_images("woolworths") == {MILK, CHOC}
    assert await service.track_from_cart() == []  # already tracked: nothing new, no duplicates


async def test_sale_alert_button_adds_to_the_real_cart(aus_cart, store, clock):
    await aus_cart.connect_session()
    bot = FakeBot()
    telegram = TelegramNotifier(TOKEN, [42], transport=httpx.MockTransport(bot))
    service = service_for(aus_cart, store, clock, notifiers=[telegram], telegram=telegram)
    result = await service.track(MILK)
    assert result.created
    for _ in range(2):
        clock.advance(hours=12)
        assert (await service.scheduler.refresh()).outcome == "ok"

    aus_cart.set_price(MILK, 2.45, was=4.95, special=True)  # half price this week
    clock.advance(hours=12)
    run = await service.scheduler.refresh()
    assert run.outcome == "ok" and run.observed == 1
    stats = store.item_stats("woolworths", MILK)
    assert stats["heavy"] and stats["discount"] == 0.5051

    clock.advance(hours=12)  # past quiet hours and the digest time
    await service.tick()
    sent = [body for method, body in bot.requests if method == "sendMessage"]
    assert any("Heavy discount" in body["text"] and body["chat_id"] == 42 for body in sent)
    heavy = next(a for a in store.recent_alerts() if a["kind"] == "heavy_discount")

    bot.updates = [press(1, 42, f"add:{heavy['id']}:2")]
    assert await telegram.poll_once(store, service.handle_action, timeout=0) == 1
    async with service.client() as client:
        assert (await client.get_cart()).quantity_of(MILK) == 2
    assert store.get_alert(heavy["id"])["acted_at"]
    assert bot.requests[-1][1]["text"].endswith("2 in the cart now")

    bot.updates = [press(2, 42, f"snooze:{heavy['id']}")]
    await telegram.poll_once(store, service.handle_action, timeout=0)
    assert store.tracked_item("woolworths", MILK)["snoozed_until"]
    assert await service.handle_action("add:99999:1") == "That alert is no longer known"
    assert await service.handle_action(f"bogus:{heavy['id']}") == "Unknown action"
    assert await service.handle_action(f"add:{heavy['id']}:x") == "Bad quantity"

    usage_by_run = {r["id"]: r["upstream_requests"] for r in store.recent_runs(kind="refresh")}
    assert all(0 < n <= 3 for n in usage_by_run.values())  # one search (+ warm-up) per run, reconciled


async def test_forced_breaker_trip_skips_backs_off_and_pages_after_three(aus_cart, store, clock):
    service = service_for(aus_cart, store, clock, notifiers=[])
    await service.track(MILK)
    aus_cart.block()
    outcomes = []
    for _ in range(3):
        outcomes.append((await service.scheduler.refresh()).outcome)
        clock.advance(minutes=30)
        outcomes.append((await service.scheduler.refresh()).outcome)  # inside the 2 h backoff
        clock.advance(minutes=100)
    assert outcomes == ["blocked", "skipped"] * 3
    blocked_calls = [r for r in store.ledger() if r["outcome"] == "blocked"]
    assert len(blocked_calls) == 3  # one call per run, never a retry storm
    operator = [a for a in store.recent_alerts() if a["kind"] == "operator"]
    assert len(operator) == 1 and "3 refresh runs in a row failed" in operator[0]["payload"]["text"]
    aus_cart.block(False)
    clock.advance(minutes=200)
    assert (await service.scheduler.refresh()).outcome == "ok"
    assert service.scheduler.failure_streak() == []


async def test_session_expired_add_tells_the_owner(aus_cart, store, clock):
    service = service_for(aus_cart, store, clock, notifiers=[])
    await service.track(MILK)
    outcome = await service.add_to_cart(MILK, 1, reason="cli")
    assert outcome.outcome == "session_required" and "myaiagent app" in outcome.message
    clock.advance(minutes=5)
    assert any(a["kind"] == "operator" for a in store.recent_alerts())


def test_clock_is_used_for_ledger_rows(store, clock):
    hook = budget.ledger_hook(store, "woolworths", "r", clock=clock)
    hook("search_products", 1, "ok")
    assert store.ledger()[0]["at"] == clock().isoformat(timespec="seconds")


async def test_photo_is_fetched_once_through_aus_cart_mcp(aus_cart, store, clock, monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_FETCH_PHOTOS", "1")
    service = service_for(aus_cart, store, clock, notifiers=[])
    products, from_cache = await service.search("full cream milk")
    assert not from_cache and products[0].image_url
    assert (await service.search("full cream milk"))[1] is True
    result = await service.track(MILK)
    assert result.created
    data, content_type = store.get_image("woolworths", MILK)
    assert content_type == "image/jpeg" and data.startswith(b"\xff\xd8")
    photo_requests = [r for r in aus_cart.state.requests if "wowproductimages" in r]
    await service.track(MILK)
    assert [r for r in aus_cart.state.requests if "wowproductimages" in r] == photo_requests


async def test_a_refused_photo_does_not_block_the_cart(aus_cart, store, clock, monkeypatch):
    """Prod 2026-10-03: the image CDN refused node b and the breaker paused everything. Never again."""
    monkeypatch.setenv("AUS_CARTWATCH_FETCH_PHOTOS", "1")
    await aus_cart.connect_session()
    service = service_for(aus_cart, store, clock, notifiers=[])
    await service.search("full cream milk")
    aus_cart.block()
    async with service.client() as client:
        from aus_cartwatch.tracking import Tracker

        assert not await Tracker(store, client, "woolworths").ensure_image(MILK)
    aus_cart.block(False)
    outcome = await service.add_to_cart(MILK, 1, reason="ui")
    assert outcome.ok, outcome.message


async def test_a_refresh_is_one_batch_request(aus_cart, store, clock):
    service = service_for(aus_cart, store, clock, notifiers=[])
    for product_id in (MILK, CHOC):
        await service.track(product_id)
    before = [r for r in aus_cart.state.requests if "/apis/ui/" in r]
    clock.advance(days=1)
    result = await service.scheduler.refresh()
    calls = [r for r in aus_cart.state.requests if "/apis/ui/" in r][len(before) :]
    assert result.outcome == "ok" and result.observed == 2
    assert len(calls) == 1 and calls[0].startswith("GET /apis/ui/products/")  # both items, one upstream request
