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


@pytest.mark.asyncio
async def test_track_does_not_price_a_different_product(store, gateway):
    """A search hit that is not the asked-for id must not be stored against it."""
    from mcp.server.mcpserver import Context

    mcp, _gw = build(store, gateway, features.parse("core,watch"))
    alice, _ = store.create_tenant_sync("alice")
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    async def call(name, arguments):
        ctx = Context(request_context=_RequestContext(alice))
        result = await tools[name].run(arguments, ctx, convert_result=False)
        return json.loads(result)

    tracked = await call("track_item", {"product_id": "not-a-real-stockcode"})
    assert tracked["tracked"] is True
    assert tracked["price"] is None
    listed = await call("list_tracked", {})
    assert listed["items"] == [{"product_id": "not-a-real-stockcode", "name": "", "price": None}]


@pytest.mark.asyncio
async def test_budget_counts_real_upstream_and_placeholders_say_so(store, gateway):
    from mcp.server.mcpserver import Context

    mcp, gw = build(store, gateway, features.parse("core,watch"))
    alice, _ = store.create_tenant_sync("alice")
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    async def call(name, arguments):
        ctx = Context(request_context=_RequestContext(alice))
        result = await tools[name].run(arguments, ctx, convert_result=False)
        return json.loads(result)

    await gw.search(alice, "woolworths", "milk", limit=1, specials_only=False)
    budget = await call("budget_status", {})
    assert budget["upstream_today"] >= 1
    assert (await call("suggest_items", {}))["available"] is False
    assert (await call("recent_alerts", {}))["available"] is False


def test_a_renamed_tenant_does_not_share_rows(store, gateway):
    """The ledger key is the tenant id, not the display name."""
    from aus_cart_mcp.watch.ledger import Ledger

    alice, _ = store.create_tenant_sync("alice")
    ledger = Ledger(store.path)
    ledger.track(alice.id, "woolworths", "1", name="Milk", price=2.0)
    other, _ = store.create_tenant_sync("not-alice")
    assert other.id != alice.id
    assert ledger.list(other.id, "woolworths") == []
    assert ledger.list(alice.id, "woolworths")[0].name == "Milk"
