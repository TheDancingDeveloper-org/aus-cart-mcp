"""Gateway policy with a fake clock: spacing, daily cap, breaker, cache expiry, cookie persistence."""

import pytest

from aus_cart_mcp.mock.woolworths import signed_in_cookie
from aus_cart_mcp.retailers.base import Blocked, RetailerError
from aus_cart_mcp.server import parse_cookie_header


async def test_requests_are_spaced_by_min_interval(store, make_gateway, clock):
    gateway = make_gateway(min_interval=1.5)
    tenant, _ = store.create_tenant_sync("t")
    await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)  # warm-up + search
    await gateway.search(tenant, "woolworths", "bread", limit=3, specials_only=False)
    assert clock.slept and all(s == pytest.approx(1.5) for s in clock.slept)


async def test_daily_cap_stops_upstream_calls(store, make_gateway, mock_state):
    gateway = make_gateway(daily_cap=2)
    tenant, _ = store.create_tenant_sync("t")
    await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)  # 2 requests
    with pytest.raises(RetailerError, match="daily request cap"):
        await gateway.search(tenant, "woolworths", "bread", limit=3, specials_only=False)


async def test_breaker_trips_on_block_fails_fast_then_recovers(store, make_gateway, mock_state, clock):
    gateway = make_gateway(cooloff=600)
    tenant, _ = store.create_tenant_sync("t")
    mock_state.blocked = True
    with pytest.raises(Blocked):
        await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)
    mock_state.blocked = False
    sent = len(mock_state.requests)
    with pytest.raises(Blocked, match="paused"):
        await gateway.search(tenant, "woolworths", "eggs", limit=3, specials_only=False)
    assert len(mock_state.requests) == sent  # nothing went out while paused
    assert "paused" in (await gateway.status(tenant, "woolworths")).get("paused", "")
    clock.now += 601
    assert await gateway.search(tenant, "woolworths", "eggs", limit=3, specials_only=False)


async def test_search_cache_expires(store, make_gateway, mock_state, clock):
    gateway = make_gateway(search_ttl=60)
    tenant, _ = store.create_tenant_sync("t")
    await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)
    sent = len(mock_state.requests)
    await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)
    assert len(mock_state.requests) == sent
    clock.now += 61
    await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)
    assert len(mock_state.requests) == sent + 1


async def test_cart_cache_is_dropped_on_write(store, gateway, mock_state):
    tenant, _ = store.create_tenant_sync("t")
    await gateway.connect(tenant, "woolworths", parse_cookie_header(signed_in_cookie(mock_state)))
    assert (await gateway.cart(tenant, "woolworths")).items == []
    cart = await gateway.add(tenant, "woolworths", {"888140": 1})
    assert [i.product_id for i in cart.items] == ["888140"]
    assert [i.product_id for i in (await gateway.cart(tenant, "woolworths")).items] == ["888140"]


async def test_refreshed_cookies_are_persisted(store, gateway, mock_state):
    tenant, _ = store.create_tenant_sync("t")
    header = signed_in_cookie(mock_state)
    await gateway.connect(tenant, "woolworths", {k: v for k, v in parse_cookie_header(header).items() if k != "bm_sz"})
    stored = await store.get_session(tenant.id, "woolworths")
    assert "mock-wow-session" in stored["cookies"]


async def test_photos_are_metered_and_cached(store, make_gateway, mock_state):
    gateway = make_gateway()
    tenant, _ = store.create_tenant_sync("t")
    data, content_type = await gateway.image(tenant, "woolworths", "888140")
    assert content_type == "image/jpeg" and data.startswith(b"\xff\xd8")
    sent = len(mock_state.requests)
    assert await gateway.image(tenant, "woolworths", "888140") == (data, content_type)
    assert len(mock_state.requests) == sent  # served from the cache
    usage = await store.usage_summary(tenant.id, 1)
    assert usage["by_tool"]["get_product_image"]["calls"] == 2


async def test_a_refused_photo_does_not_pause_the_api(store, make_gateway, mock_state):
    from aus_cart_mcp.retailers.base import RetailerError

    gateway = make_gateway()
    tenant, _ = store.create_tenant_sync("t")
    mock_state.blocked = True
    with pytest.raises(RetailerError):
        await gateway.image(tenant, "woolworths", "888140")
    mock_state.blocked = False
    assert await gateway.search(tenant, "woolworths", "milk", limit=1, specials_only=False)


async def test_products_are_batched_cached_and_seeded_by_search(store, make_gateway, mock_state):
    gateway = make_gateway()
    tenant, _ = store.create_tenant_sync("t")
    many = list(mock_state.catalogue)
    for i in range(45):  # pad the mock catalogue so one call needs several chunks
        mock_state.catalogue[900000 + i] = {**mock_state.catalogue[many[0]], "Stockcode": 900000 + i}
    ids = [str(900000 + i) for i in range(45)]
    before = len(mock_state.requests)
    products = await gateway.products(tenant, "woolworths", [*ids, "1"])
    assert [p.product_id for p in products] == ids  # input order, unknown id left out
    product_calls = [r for r in mock_state.requests[before:] if "/apis/ui/products/" in r]
    assert len(product_calls) == 3  # 45 ids in chunks of 20
    again = len(mock_state.requests)
    assert len(await gateway.products(tenant, "woolworths", ids[:5])) == 5
    assert len(mock_state.requests) == again  # all cached
    found = await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)
    seeded = len(mock_state.requests)
    await gateway.products(tenant, "woolworths", [p.product_id for p in found])
    assert len(mock_state.requests) == seeded  # search results seed the per-id cache
    usage = await store.usage_summary(tenant.id, 1)
    assert usage["by_tool"]["get_products"]["upstream_requests"] == 4  # 3 chunks + the new connection's warm-up


async def test_was_price_is_reported(store, gateway):
    tenant, _ = store.create_tenant_sync("t")
    milk = (await gateway.products(tenant, "woolworths", ["888140"]))[0]
    assert milk.was_price == 4.95 and milk.price == 4.95


async def test_an_idle_guest_connection_starts_afresh(store, make_gateway, mock_state, clock):
    gateway = make_gateway(search_ttl=0)
    tenant, _ = store.create_tenant_sync("prices")
    await gateway.search(tenant, "woolworths", "milk", limit=1, specials_only=False)
    warmups = mock_state.requests.count("GET /")
    clock.now += 60
    await gateway.search(tenant, "woolworths", "milk", limit=1, specials_only=False)
    assert mock_state.requests.count("GET /") == warmups  # still the same visitor
    clock.now += 31 * 60
    await gateway.search(tenant, "woolworths", "milk", limit=1, specials_only=False)
    assert mock_state.requests.count("GET /") == warmups + 1  # a new visitor: fresh cookies


async def _trip_breaker(gateway, tenant, mock_state):
    from aus_cart_mcp.retailers.base import Blocked

    mock_state.blocked = True
    with pytest.raises(Blocked):
        await gateway.search(tenant, "woolworths", "milk", limit=1, specials_only=False)
    mock_state.blocked = False
    with pytest.raises(Blocked, match="paused"):
        await gateway.search(tenant, "woolworths", "bread", limit=1, specials_only=False)


async def test_a_reconnect_is_checked_once_during_a_pause_and_clears_it(store, make_gateway, mock_state, clock):
    gateway = make_gateway(search_ttl=0)
    tenant, _ = store.create_tenant_sync("t")
    await _trip_breaker(gateway, tenant, mock_state)
    shopper = await gateway.connect(tenant, "woolworths", parse_cookie_header(signed_in_cookie(mock_state)))
    assert shopper.logged_in
    assert await gateway.search(tenant, "woolworths", "milk", limit=1, specials_only=False)  # pause cleared


async def test_a_refused_reconnect_check_restarts_the_pause_and_is_rate_limited(store, make_gateway, mock_state, clock):
    from aus_cart_mcp.retailers.base import Blocked

    gateway = make_gateway(search_ttl=0)
    tenant, _ = store.create_tenant_sync("t")
    await _trip_breaker(gateway, tenant, mock_state)
    mock_state.blocked = True
    before = len(mock_state.requests)
    with pytest.raises(Blocked):
        await gateway.connect(tenant, "woolworths", parse_cookie_header(signed_in_cookie(mock_state)))
    assert len(mock_state.requests) == before + 1  # one check, no retry
    mock_state.blocked = False
    clock.now += 60
    with pytest.raises(Blocked, match="just checked"):  # a second reconnect within 5 minutes is not checked
        await gateway.connect(tenant, "woolworths", parse_cookie_header(signed_in_cookie(mock_state)))
    assert len(mock_state.requests) == before + 1
    clock.now += 5 * 60
    assert (await gateway.connect(tenant, "woolworths", parse_cookie_header(signed_in_cookie(mock_state)))).logged_in
