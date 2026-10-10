"""Watch tools appear only when the feature flag includes watch, and stay per tenant."""

import json

import pytest

from aus_cart_mcp import features
from aus_cart_mcp.server import build
from aus_cart_mcp.watch.register import WATCH_TOOLS


def _listed(mcp) -> set[str]:
    return {tool.name for tool in mcp._tool_manager.list_tools()}


def test_core_alone_does_not_register_watch_tools(store, gateway):
    mcp, _ = build(store, gateway, features.parse("core"))
    listed = _listed(mcp)
    assert "search_products" in listed
    assert "track_item" not in listed
    assert not set(WATCH_TOOLS) & listed


def test_watch_flag_registers_the_watch_tools(store, gateway):
    mcp, _ = build(store, gateway, features.parse("core,watch"))
    listed = _listed(mcp)
    assert set(WATCH_TOOLS) <= listed
    assert "add_to_cart" in listed  # core cart tool stays; watch does not replace it


class _Request:
    def __init__(self, tenant):
        self.state = type("S", (), {"tenant": tenant})()


class _RequestContext:
    def __init__(self, tenant):
        self.request = _Request(tenant)


@pytest.mark.asyncio
async def test_tracked_item_is_invisible_to_the_other_tenant(store, gateway):
    from mcp.server.mcpserver import Context

    mcp, gw = build(store, gateway, features.parse("core,watch"))
    alice, _ = store.create_tenant_sync("alice")
    bob, _ = store.create_tenant_sync("bob")
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    found = await gw.search(alice, "woolworths", "milk", limit=1, specials_only=False)
    product_id = found[0].product_id

    async def call(name, tenant, arguments):
        ctx = Context(request_context=_RequestContext(tenant))
        result = await tools[name].run(arguments, ctx, convert_result=False)
        return json.loads(result)

    tracked = await call("track_item", alice, {"product_id": product_id})
    assert tracked["tracked"] is True
    assert tracked["tenant"] == "alice"
    assert tracked["price"] is not None

    alice_list = await call("list_tracked", alice, {})
    assert [item["product_id"] for item in alice_list["items"]] == [product_id]

    bob_list = await call("list_tracked", bob, {})
    assert bob_list["items"] == []

    refreshed = await call("run_refresh", alice, {})
    assert refreshed["refreshed"] == 1
    assert (await call("run_refresh", bob, {}))["refreshed"] == 0
