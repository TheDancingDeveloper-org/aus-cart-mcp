"""Gateway against the mock retailer: sessions, isolation, metering."""

import pytest

from aus_cart_mcp.mock.woolworths import signed_in_cookie
from aus_cart_mcp.retailers.base import RetailerError, SessionRequired
from aus_cart_mcp.server import parse_cookie_header


async def test_guest_search_is_metered(store, gateway):
    tenant, _ = store.create_tenant_sync("owner")
    products = await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)
    assert products and products[0].url.startswith("https://www.woolworths.com.au/shop/productdetails/")
    await gateway.search(tenant, "woolworths", "milk", limit=3, specials_only=False)  # cached
    usage = await store.usage_summary(tenant.id, 1)
    assert usage["by_tool"]["search_products"] == {"calls": 2, "succeeded": 2, "upstream_requests": 2}


async def test_cart_needs_a_logged_in_session(store, gateway, mock_state):
    tenant, _ = store.create_tenant_sync("owner")
    with pytest.raises(SessionRequired, match="connect"):
        await gateway.cart(tenant, "woolworths")
    with pytest.raises(SessionRequired, match="not logged in"):
        await gateway.connect(tenant, "woolworths", {"mock-wow-session": "forged"})
    assert await store.get_session(tenant.id, "woolworths") is None


async def test_connect_add_set_status_disconnect(store, gateway, mock_state):
    tenant, _ = store.create_tenant_sync("owner")
    shopper = await gateway.connect(tenant, "woolworths", parse_cookie_header(signed_in_cookie(mock_state, "Sam")))
    assert shopper.first_name == "Sam"
    await gateway.add(tenant, "woolworths", {"888140": 1})
    cart = await gateway.add(tenant, "woolworths", {"888140": 1})
    assert [(i.product_id, i.quantity) for i in cart.items] == [("888140", 2)]
    cart = await gateway.set_quantities(tenant, "woolworths", {"888140": 0})
    assert cart.items == []
    status = await gateway.status(tenant, "woolworths")
    assert status["connected"] is True and status["first_name"] == "Sam"
    await gateway.disconnect(tenant, "woolworths")
    assert (await gateway.status(tenant, "woolworths"))["connected"] is False


async def test_customers_are_isolated(store, gateway, mock_state):
    alice, _ = store.create_tenant_sync("alice")
    bob, _ = store.create_tenant_sync("bob")
    await gateway.connect(alice, "woolworths", parse_cookie_header(signed_in_cookie(mock_state)))
    await gateway.add(alice, "woolworths", {"888140": 1})
    with pytest.raises(SessionRequired):
        await gateway.cart(bob, "woolworths")
    assert (await store.usage_summary(bob.id, 1))["by_tool"]["get_cart"]["succeeded"] == 0


async def test_unknown_and_planned_retailers(store, gateway):
    tenant, _ = store.create_tenant_sync("owner")
    with pytest.raises(RetailerError, match="unknown retailer"):
        await gateway.search(tenant, "nope", "milk", limit=1, specials_only=False)
    with pytest.raises(RetailerError, match="planned"):
        await gateway.search(tenant, "coles", "milk", limit=1, specials_only=False)
