"""End to end over real sockets: mock retailer + aus-cart-mcp + an MCP client.

This mirrors production wiring: the server reaches the retailer over HTTP
(redirected to the mock with AUS_CART_MCP_WOOLWORTHS_BASE_URL), and the session
comes from a real sign-in on the retailer's login page.
"""

import asyncio
import json
import socket

import httpx
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aus_cart_mcp.gateway import Limits
from aus_cart_mcp.mock import woolworths as mock
from aus_cart_mcp.server import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app) -> tuple[uvicorn.Server, asyncio.Task, str]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task, f"http://127.0.0.1:{port}"


@pytest.fixture()
async def stack(store, monkeypatch):
    retailer, retailer_task, retailer_url = await serve(mock.create_app())
    monkeypatch.setenv("AUS_CART_MCP_WOOLWORTHS_BASE_URL", retailer_url)
    app_server, app_task, app_url = await serve(create_app(store, Limits(min_interval=0, jitter=0)))
    yield retailer_url, app_url
    for server, task in ((app_server, app_task), (retailer, retailer_task)):
        server.should_exit = True
        await task


async def test_sign_in_connect_search_fill_and_clear(stack, store):
    retailer_url, app_url = stack
    _, key = store.create_tenant_sync("owner")

    # The customer signs in on the retailer's own page; their app captures the cookies.
    async with httpx.AsyncClient(base_url=retailer_url) as browser:
        await browser.get("/")
        await browser.post("/shop/securelogin", data={"name": "Jordan"})
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in browser.cookies.jar)

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    async with Client(streamable_http_client(f"{app_url}/mcp", http_client=http)) as c:
        connected = json.loads((await c.call_tool("connect_session", {"cookie_header": cookie_header})).content[0].text)
        assert connected == {"connected": True, "retailer": "woolworths", "first_name": "Jordan"}

        found = json.loads((await c.call_tool("search_products", {"query": "full cream milk"})).content[0].text)
        milk = next(p for p in found if "Full Cream Milk" in p["name"])

        cart = json.loads(
            (await c.call_tool("add_to_cart", {"items": [{"product_id": milk["product_id"], "quantity": 2}]}))
            .content[0]
            .text
        )
        assert cart["items"] == [
            {"product_id": milk["product_id"], "name": milk["name"], "quantity": 2, "price": milk["price"]}
        ]
        assert cart["total"] == pytest.approx(2 * milk["price"])

        cleared = json.loads(
            (await c.call_tool("set_cart_quantities", {"items": [{"product_id": milk["product_id"], "quantity": 0}]}))
            .content[0]
            .text
        )
        assert cleared["items"] == []

        status = json.loads((await c.call_tool("connection_status", {})).content[0].text)
        assert status["connected"] is True and status["first_name"] == "Jordan"
