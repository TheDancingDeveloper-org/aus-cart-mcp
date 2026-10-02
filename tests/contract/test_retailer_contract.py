"""Every supported retailer adapter must pass this suite against its mock.

A mock module (aus_cart_mcp.mock.<key>) provides `MockState`, `create_app(state)` and
`signed_in_cookie(state)`. Adding a retailer means adding an adapter and a mock built
from recorded responses. Nothing else in this file changes.
"""

import httpx
import pytest

from aus_cart_mcp.mock import MOCKS
from aus_cart_mcp.retailers import RETAILERS
from aus_cart_mcp.retailers.base import Blocked, Cart, Product, RetailerError, SessionRequired
from aus_cart_mcp.server import parse_cookie_header

KEYS = sorted(RETAILERS)


@pytest.fixture(params=KEYS)
def setup(request):
    retailer = RETAILERS[request.param]
    mock = MOCKS[request.param]
    state = mock.MockState()
    app = mock.create_app(state)

    def client(cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://mock",
            transport=httpx.ASGITransport(app=app),
            cookies=cookies or {},
            headers={"User-Agent": retailer.user_agent},
        )

    return retailer, mock, state, client


async def test_warm_sets_bot_cookies(setup):
    retailer, _, _, client = setup
    async with client() as http:
        await retailer.warm(http)
        assert retailer.has_bot_cookies({c.name: c.value for c in http.cookies.jar})


async def test_guest_is_not_logged_in(setup):
    retailer, _, _, client = setup
    async with client() as http:
        assert (await retailer.shopper(http)).logged_in is False


async def test_search_returns_well_formed_products(setup):
    retailer, _, _, client = setup
    async with client() as http:
        products = await retailer.search(http, "milk", limit=3, specials_only=False)
    assert 0 < len(products) <= 3
    for p in products:
        assert isinstance(p, Product)
        assert p.product_id and isinstance(p.product_id, str)
        assert p.name
        assert p.price is None or p.price >= 0
        assert p.url.startswith("https://")


async def test_logged_in_cart_round_trip(setup):
    retailer, mock, state, client = setup
    cookies = parse_cookie_header(mock.signed_in_cookie(state))
    async with client(cookies) as http:
        shopper = await retailer.shopper(http)
        assert shopper.logged_in and shopper.first_name
        product = (await retailer.search(http, "milk", limit=1, specials_only=False))[0]
        empty = await retailer.cart(http)
        assert isinstance(empty, Cart) and empty.items == []
        await retailer.set_quantities(http, {product.product_id: 2})
        cart = await retailer.cart(http)
        assert [(i.product_id, i.quantity) for i in cart.items] == [(product.product_id, 2)]
        assert cart.total is not None and cart.total > 0
        await retailer.set_quantities(http, {product.product_id: 0})
        assert (await retailer.cart(http)).items == []


async def test_bot_block_is_reported_as_blocked(setup):
    retailer, _, state, client = setup
    state.blocked = True
    async with client() as http:
        with pytest.raises(Blocked):
            await retailer.search(http, "milk", limit=1, specials_only=False)


async def test_too_many_items_in_one_update_is_refused(setup):
    retailer, mock, state, client = setup
    async with client(parse_cookie_header(mock.signed_in_cookie(state))) as http:
        with pytest.raises(RetailerError):
            await retailer.set_quantities(http, {str(1000 + i): 1 for i in range(100)})


async def test_error_types_are_distinguishable(setup):
    """401 means reconnect; 403/429 or a web page means back off. Both are RetailerError."""
    assert issubclass(SessionRequired, RetailerError) and issubclass(Blocked, RetailerError)
    assert not issubclass(SessionRequired, Blocked)
