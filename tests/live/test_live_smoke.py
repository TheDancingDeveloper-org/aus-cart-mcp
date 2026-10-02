"""Opt-in smoke tests against the real retailer sites. Read-only: search only, no account.

    AUS_CART_MCP_LIVE=1 uv run pytest -m live

They catch a retailer changing its endpoints. They never touch a cart, never sign
in, and send a handful of requests with the normal throttle.
"""

import os

import pytest

from aus_cart_mcp.gateway import Gateway
from aus_cart_mcp.retailers import RETAILERS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("AUS_CART_MCP_LIVE") != "1", reason="set AUS_CART_MCP_LIVE=1 to run"),
]


@pytest.mark.parametrize("key", sorted(RETAILERS))
async def test_search_and_guest_session_shape(store, key):
    gateway = Gateway(store)  # real network, default throttle
    tenant, _ = store.create_tenant_sync("live")
    products = await gateway.search(tenant, key, "milk", limit=3, specials_only=False)
    assert products and all(p.product_id and p.name for p in products)
    status = await gateway.status(tenant, key)
    assert status["connected"] is False
