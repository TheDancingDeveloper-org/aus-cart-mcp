"""The MCP server over real Streamable HTTP (uvicorn) with the gateway on the mock retailer."""

import asyncio
import json
import socket

import httpx
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aus_cart_mcp.mock.woolworths import signed_in_cookie
from aus_cart_mcp.server import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
async def base_url(store, gateway):
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(store, gateway=gateway), host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


def client(base: str, key: str) -> Client:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    return Client(streamable_http_client(f"{base}/mcp", http_client=http))


def text(result) -> str:
    return result.content[0].text


async def test_health_is_open_and_mcp_needs_a_valid_key(base_url, store):
    _, key = store.create_tenant_sync("owner")
    async with httpx.AsyncClient() as http:
        health = (await http.get(f"{base_url}/healthz")).json()
        assert health["ok"] is True and health["retailers"] == ["woolworths"]
        assert (await http.post(f"{base_url}/mcp", json={})).status_code == 401
        assert (
            await http.post(f"{base_url}/mcp", json={}, headers={"Authorization": "Bearer acm_nope"})
        ).status_code == 401
        assert (
            await http.post(f"{base_url}/mcp", json={}, headers={"Authorization": f"Basic {key}"})
        ).status_code == 401
    store.set_disabled_sync("owner", True)
    async with httpx.AsyncClient() as http:
        disabled = await http.post(f"{base_url}/mcp", json={}, headers={"Authorization": f"Bearer {key}"})
        assert disabled.status_code == 401


async def test_tool_list_and_retailer_index(base_url, store):
    _, key = store.create_tenant_sync("owner")
    async with client(base_url, key) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {
            "list_retailers",
            "search_products",
            "get_cart",
            "add_to_cart",
            "set_cart_quantities",
            "connection_status",
            "connect_session",
            "disconnect_session",
            "usage_summary",
            "get_product_image",
            "get_trolley",
            "add_to_trolley",
            "set_trolley_quantities",
        } <= names
        supported = json.loads(text(await c.call_tool("list_retailers", {})))
        assert [r["key"] for r in supported] == ["woolworths"]
        everyone = json.loads(text(await c.call_tool("list_retailers", {"include_planned": True})))
        assert {r["key"] for r in everyone} >= {"woolworths", "coles", "amazon_au", "kmart", "bigw"}


async def test_full_flow_errors_and_deprecated_aliases(base_url, store, mock_state):
    _, key = store.create_tenant_sync("owner")
    async with client(base_url, key) as c:
        found = json.loads(text(await c.call_tool("search_products", {"query": "milk 3L", "limit": 2})))
        product_id = found[0]["product_id"]

        denied = await c.call_tool("get_cart", {})
        assert denied.is_error and "connect" in text(denied).lower()

        ok = await c.call_tool("connect_session", {"cookie_header": signed_in_cookie(mock_state, "Kim")})
        assert json.loads(text(ok))["first_name"] == "Kim"

        added = json.loads(
            text(await c.call_tool("add_to_cart", {"items": [{"product_id": product_id, "quantity": 2}]}))
        )
        assert added["items"][0]["quantity"] == 2

        bad = await c.call_tool("add_to_cart", {"items": [{"product_id": product_id, "quantity": 500}]})
        assert bad.is_error and "between 0 and 99" in text(bad)

        # 0.1 aliases still work.
        legacy = json.loads(text(await c.call_tool("get_trolley", {})))
        assert legacy["items"][0]["product_id"] == product_id
        cleared = json.loads(
            text(await c.call_tool("set_cart_quantities", {"items": [{"product_id": product_id, "quantity": 0}]}))
        )
        assert cleared["items"] == []

        planned = await c.call_tool("search_products", {"query": "milk", "retailer": "coles"})
        assert planned.is_error and "planned" in text(planned)

        usage = json.loads(text(await c.call_tool("usage_summary", {"days": 1})))
        assert usage["calls"] >= 5 and "add_to_cart" in usage["by_tool"]


async def test_two_customers_cannot_see_each_others_cart(base_url, store, mock_state):
    _, alice = store.create_tenant_sync("alice")
    _, bob = store.create_tenant_sync("bob")
    async with client(base_url, alice) as c:
        await c.call_tool("connect_session", {"cookie_header": signed_in_cookie(mock_state, "Alice")})
        await c.call_tool("add_to_cart", {"items": [{"product_id": "888140", "quantity": 1}]})
    async with client(base_url, bob) as c:
        denied = await c.call_tool("get_cart", {})
        assert denied.is_error


async def test_product_photo_tool(base_url, store):
    import base64

    _, key = store.create_tenant_sync("owner")
    async with client(base_url, key) as c:
        found = json.loads(text(await c.call_tool("search_products", {"query": "milk", "limit": 1})))
        assert found[0]["image_url"]
        photo = json.loads(text(await c.call_tool("get_product_image", {"product_id": found[0]["product_id"]})))
        assert photo["content_type"] == "image/jpeg"
        assert base64.b64decode(photo["data_base64"]).startswith(b"\xff\xd8")
        missing = await c.call_tool("get_product_image", {"product_id": "999999999"})
        assert missing.is_error and "no photo" in text(missing)
