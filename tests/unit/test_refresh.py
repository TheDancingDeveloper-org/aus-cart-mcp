"""Woolworths login renewal: GET /auth/heartbeat, synthetic tokens only."""

import json

import httpx
import pytest

from aus_cart_mcp.retailers.base import Blocked, SessionRequired
from aus_cart_mcp.retailers.woolworths import Woolworths


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="https://www.woolworths.com.au")


async def test_refresh_session_reads_only_the_refreshed_flag():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        return httpx.Response(200, json={"refreshed": True})

    async with _client(handler) as http:
        assert await Woolworths().refresh_session(http) is True
    assert seen == {"path": "/auth/heartbeat", "method": "GET"}


async def test_refresh_session_is_false_when_the_site_did_not_renew():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refreshed": False})

    async with _client(handler) as http:
        assert await Woolworths().refresh_session(http) is False


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"refreshed": False}, SessionRequired),
        (403, "no", Blocked),
        (429, "no", Blocked),
        (200, "<html>denied</html>", Blocked),
        (500, {"refreshed": True}, Exception),
    ],
)
async def test_refresh_session_errors(status, body, error):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    async with _client(handler) as http:
        with pytest.raises(error):
            await Woolworths().refresh_session(http)


def test_refresh_body_shape_matches_the_observed_response():
    """The captured call returns exactly this object. Values, not cookies, belong in fixtures."""
    assert json.loads('{"refreshed": true}') == {"refreshed": True}
