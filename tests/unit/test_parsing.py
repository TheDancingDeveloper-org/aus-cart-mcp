import pytest

from aus_cart_mcp.retailers.base import RetailerError
from aus_cart_mcp.server import parse_cookie_header, parse_quantities


def test_cookie_header_parses_webview_output():
    assert parse_cookie_header("a=1; b=two; _abck=x~y") == {"a": "1", "b": "two", "_abck": "x~y"}


def test_cookie_header_tolerates_values_simplecookie_rejects():
    assert parse_cookie_header('weird="unterminated; ok=1') == {"weird": '"unterminated', "ok": "1"}


def test_empty_cookie_header():
    assert parse_cookie_header("") == {}


def test_quantities_sum_duplicates_and_default_to_one():
    assert parse_quantities([{"product_id": "1"}, {"product_id": "1", "quantity": 2}]) == {"1": 3}


@pytest.mark.parametrize(
    "items, message",
    [
        ([], "no items"),
        ([{"quantity": 1}], "needs a product_id"),
        ([{"product_id": "1", "quantity": "lots"}], "bad quantity"),
        ([{"product_id": "1", "quantity": 100}], "between 0 and 99"),
        ([{"product_id": "1", "quantity": -1}], "between 0 and 99"),
        (["not-an-object"], "must be an object"),
    ],
)
def test_quantities_reject_bad_input(items, message):
    with pytest.raises(RetailerError, match=message):
        parse_quantities(items)
