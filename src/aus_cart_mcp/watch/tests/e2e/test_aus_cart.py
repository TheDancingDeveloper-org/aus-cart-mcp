"""aus_cartwatch's client against the real aus-cart-mcp and its mock Woolworths, over real sockets."""

import json

import pytest
from mcp.server.mcpserver import MCPServer

from aus_cartwatch.auscart import AusCartClient, Blocked, RetailerError, SessionRequired
from aus_cartwatch.budget import client as ledgered_client
from tests.conftest import serve, stop

MILK = "888140"


async def test_guest_search_and_usage(aus_cart):
    calls = []
    async with AusCartClient(aus_cart.url, aus_cart.key, on_call=lambda *c: calls.append(c)) as client:
        assert "woolworths" in {r["key"] for r in await client.list_retailers()}
        found = await client.search_products("full cream milk")
        usage = await client.usage_summary(days=1)
    milk = next(p for p in found if p.product_id == MILK)
    assert milk.price == 4.95 and milk.unit_price_value is not None
    assert usage.calls >= 1 and usage.upstream_requests >= 1  # list_retailers is not metered
    assert calls == [("list_retailers", 0, "ok"), ("search_products", 1, "ok"), ("usage_summary", 0, "ok")]


async def test_get_products_batch_and_search_fallback(aus_cart):
    async with AusCartClient(aus_cart.url, aus_cart.key) as client:
        assert client.has_tool("get_products")
        batch = await client.get_products([MILK, "6073909", "1"])
        assert set(batch) == {MILK, "6073909"} and batch[MILK].was_price == 4.95
        searched = await client.get_products([MILK, "6073909"], names={MILK: "full cream milk"}, batch=False)
        capped = await client.get_products([MILK, "6073909"], max_searches=1, batch=False)
        assert await client.get_products([]) == {}
    assert set(searched) == {MILK, "6073909"}
    assert len(capped) >= 1 and MILK in capped


async def test_cart_requires_session_then_works(aus_cart):
    async with AusCartClient(aus_cart.url, aus_cart.key) as client:
        with pytest.raises(SessionRequired):
            await client.get_cart()
        status = await client.connection_status()
        assert not status.connected
    await aus_cart.connect_session("Jordan")
    async with AusCartClient(aus_cart.url, aus_cart.key) as client:
        status = await client.connection_status()
        assert status.connected and status.first_name == "Jordan"
        cart = await client.add_to_cart({MILK: 2})
        assert cart.quantity_of(MILK) == 2
        cart = await client.set_cart_quantities({MILK: 0})
        assert cart.quantity_of(MILK) == 0
        assert (await client.get_cart()).items == []


async def test_blocked_retailer_maps_to_blocked_and_is_not_retried(aus_cart):
    aus_cart.block()
    calls = []
    async with AusCartClient(aus_cart.url, aus_cart.key, on_call=lambda *c: calls.append(c)) as client:
        with pytest.raises(Blocked):
            await client.search_products("milk")
        status = await client.connection_status()
    assert calls[0] == ("search_products", 1, "blocked")
    assert [c[0] for c in calls].count("search_products") == 1
    assert status.retailer == "woolworths"


async def test_bad_input_is_a_retailer_error(aus_cart):
    async with AusCartClient(aus_cart.url, aus_cart.key) as client:
        with pytest.raises(RetailerError):
            await client.add_to_cart({})


async def test_wrong_key_is_refused(aus_cart):
    with pytest.raises(Exception):  # noqa: B017 - the SDK raises its own HTTP error type
        async with AusCartClient(aus_cart.url, "wrong"):
            pass


async def test_calls_reach_the_ledger(aus_cart, store):
    async with ledgered_client(store, "woolworths", "run-1", url=aus_cart.url, key=aus_cart.key) as client:
        await client.search_products("milk")
    assert [(r["tool"], r["upstream_requests"], r["run_id"]) for r in store.ledger(run_id="run-1")] == [
        ("search_products", 1, "run-1")
    ]


async def test_trolley_alias_fallback_for_older_servers():
    """A pre-0.3 server that only has the *_trolley names still works."""
    old = MCPServer("aus-cart")
    cart = {"items": [{"product_id": "1", "name": "Bread", "quantity": 1, "price": 3.5}], "subtotal": 3.5}

    @old.tool()
    async def get_trolley(retailer: str = "woolworths") -> str:
        return json.dumps(cart)

    @old.tool()
    async def add_to_trolley(items: list[dict], retailer: str = "woolworths") -> str:
        return json.dumps(cart)

    server = await serve(old.streamable_http_app(stateless_http=True, json_response=True))
    try:
        async with AusCartClient(f"{server[2]}/mcp", "") as client:
            assert (await client.get_cart()).quantity_of("1") == 1
            assert (await client.add_to_cart({"1": 1})).total is None
            with pytest.raises(RetailerError, match="no set_cart_quantities tool"):
                await client.set_cart_quantities({"1": 0})
    finally:
        await stop(server[:2])


async def test_auscart_status_cli(aus_cart, store, monkeypatch, capsys):
    from aus_cartwatch.__main__ import _auscart_status

    await aus_cart.connect_session("Jordan")
    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_URL", aus_cart.url)
    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_KEY", aus_cart.key)
    assert await _auscart_status(store) == 0
    out = capsys.readouterr().out
    assert "retailer    woolworths" in out and "connected as Jordan" in out and "usage today" in out
    assert aus_cart.key not in out


async def test_numeric_unit_price_from_aus_cart_mcp(aus_cart):
    async with AusCartClient(aus_cart.url, aus_cart.key) as client:
        milk = (await client.get_products([MILK]))[MILK]
    assert (milk.unit_price_value, milk.unit_price_unit) == (1.65, "1L")  # from CupPrice/CupMeasure, not parsing
