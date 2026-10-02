"""The supported-retailer index: registry, catalogue and docs/RETAILERS.md agree."""

import pathlib
import re

import pytest

from aus_cart_mcp.mock import MOCKS
from aus_cart_mcp.retailers import CATALOGUE, RETAILERS, get
from aus_cart_mcp.retailers.base import RetailerError

DOC = pathlib.Path(__file__).parents[2] / "docs" / "RETAILERS.md"


def test_keys_are_unique_and_snake_case():
    keys = [r.key for r in CATALOGUE]
    assert len(keys) == len(set(keys))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", k) for k in keys)


def test_supported_retailers_are_complete():
    for retailer in RETAILERS.values():
        info = retailer.info
        assert info.status in ("supported", "experimental")
        assert set(info.capabilities) >= {"search"}
        assert info.login_url.startswith("https://") and info.cookie_url.startswith("https://")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", info.verified), f"{info.key} needs a verified date"
        assert retailer.key in MOCKS, f"{info.key} needs a mock in aus_cart_mcp.mock for the contract suite"


def test_planned_retailers_are_listed_but_not_callable():
    planned = [r for r in CATALOGUE if r.status == "planned"]
    assert {r.key for r in planned} >= {"coles", "amazon_au", "kmart", "bigw"}
    for r in planned:
        with pytest.raises(RetailerError, match="planned"):
            get(r.key)


def test_docs_index_lists_every_retailer():
    text = DOC.read_text()
    for r in CATALOGUE:
        assert f"`{r.key}`" in text, f"docs/RETAILERS.md is missing {r.key}"
