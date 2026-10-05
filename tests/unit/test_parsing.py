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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$12.40 / 1L", (12.4, "1L")),
        ("$1.50 / 100G", (1.5, "100g")),
        ("$0.85 / 1EA", (0.85, "1ea")),
        ("$0.30 / 100ML", (0.3, "100mL")),
        ("$1,234.00 / 1KG", (1234.0, "1kg")),
        ("", (None, "")),
        ("n/a", (None, "")),
    ],
)
def test_parse_unit_price(text, expected):
    from aus_cart_mcp.retailers.base import parse_unit_price

    assert parse_unit_price(text) == expected


def test_numeric_unit_price_savings_and_was_price():
    from aus_cart_mcp.retailers.woolworths import Woolworths

    w = Woolworths()
    regular = w._product({"Stockcode": 1, "Price": 4.95, "WasPrice": 4.95, "SavingsAmount": 0.0,
                          "CupPrice": 1.65, "CupMeasure": "1L", "CupString": "$1.65 / 1L"})  # fmt: skip
    assert (regular.unit_price_value, regular.unit_price_unit, regular.savings) == (1.65, "1L", 0.0)
    special = w._product({"Stockcode": 2, "Price": 2.45, "WasPrice": 3.5, "IsOnSpecial": True,
                          "CupPrice": None, "CupString": "$0.49 / 100G"})  # fmt: skip
    assert special.was_price == 3.5 and special.savings == 1.05 and special.unit_price_unit == "100g"
