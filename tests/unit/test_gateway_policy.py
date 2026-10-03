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
