import pytest

from aus_cart_mcp.watch.auscart import (
    AusCartClient,
    Blocked,
    Cart,
    Product,
    RetailerError,
    SessionRequired,
    Unavailable,
    classify,
    parse_unit_price,
)
from tests.conftest import free_port

# Recorded aus-cart-mcp 0.2 tool results and error texts.
SEARCH_ROW = {
    "product_id": "888140",
    "name": "Woolworths Full Cream Milk",
    "price": 4.95,
    "unit_price": "$1.65 / 1L",
    "size": "3L",
    "available": True,
    "on_special": False,
    "url": "https://www.woolworths.com.au/shop/productdetails/888140",
}


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("woolworths is blocking automated requests; paused for about 29 more minutes", Blocked),
        ("Woolworths is refusing requests (HTTP 403)", Blocked),
        ("Woolworths returned a web page instead of data (a bot challenge?)", Blocked),
        ("daily request cap for woolworths reached; try again tomorrow", Blocked),
        ("Woolworths isn't connected for this account; connect a session first", SessionRequired),
        ("the saved Woolworths session has expired; reconnect it", SessionRequired),
        ("quantity for 1 must be between 0 and 99", RetailerError),
    ],
)
def test_classify(message, kind):
    error = classify(message)
    assert type(error) is kind and str(error) == message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$1.65 / 1L", (1.65, "1L")),
        ("$0.95 / 100g", (0.95, "100g")),
        ("$0.30 / 100ML", (0.3, "100mL")),
        ("$12.40 / 1KG", (12.4, "1kg")),
        ("$1,234.00 / 1EA", (1234.0, "1ea")),
        ("$3.00 / each", (3.0, "1each")),
        ("", (None, "")),
        ("n/a", (None, "")),
    ],
)
def test_parse_unit_price(text, expected):
    assert parse_unit_price(text) == expected


def test_product_from_search_row():
    product = Product.from_dict(SEARCH_ROW)
    assert product.product_id == "888140" and product.price == 4.95 and product.was_price is None
    assert (product.unit_price_value, product.unit_price_unit) == (1.65, "1L")


def test_product_prefers_numeric_fields_when_present():
    product = Product.from_dict(
        {**SEARCH_ROW, "was_price": 6.0, "unit_price_value": 0.165, "unit_price_unit": "100mL", "price": None}
    )
    assert product.was_price == 6.0 and product.price is None
    assert (product.unit_price_value, product.unit_price_unit) == (0.165, "100mL")


def test_cart_from_dict():
    cart = Cart.from_dict(
        {"items": [{"product_id": "1", "name": "A", "quantity": 2, "price": 1.5}], "subtotal": 3.0, "total": 3.0}
    )
    assert cart.quantity_of("1") == 2 and cart.quantity_of("2") == 0 and cart.total == 3.0


def test_repr_redacts_key():
    assert "secret" not in repr(AusCartClient("http://x/mcp", "secret"))
    assert "(none)" in repr(AusCartClient("http://x/mcp", ""))


async def test_call_outside_session_is_refused():
    with pytest.raises(RuntimeError):
        await AusCartClient("http://127.0.0.1:9/mcp", "k").call("list_retailers")


async def test_unreachable_server_retries_once_then_unavailable(caplog):
    client = AusCartClient(f"http://127.0.0.1:{free_port()}/mcp", "k", retry_delay=0)
    with pytest.raises(Unavailable):
        async with client:
            pass
    assert "retrying once" in caplog.text
    assert "'k'" not in caplog.text
