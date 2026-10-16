"""Retailer constants live on the adapter and take a per-tenant override."""

import pytest

from aus_cart_mcp.core.service import CoreService
from aus_cart_mcp.features import parse
from aus_cart_mcp.retailers.base import RetailerConstants, RetailerError
from aus_cart_mcp.retailers.woolworths import Woolworths


def test_woolworths_publishes_its_own_constants():
    assert Woolworths.constants == RetailerConstants(delivery_threshold=50.0, specials_day="wednesday")


def test_override_replaces_only_what_the_tenant_sets():
    got = Woolworths.constants.overridden({"delivery_threshold": "75", "specials_day": "tuesday", "nope": 1})
    assert got == RetailerConstants(delivery_threshold=75.0, specials_day="tuesday")


def test_a_bad_override_is_ignored():
    got = Woolworths.constants.overridden({"delivery_threshold": "nope", "specials_day": 3})
    assert got == Woolworths.constants


def test_service_reads_the_adapter(monkeypatch):
    service = CoreService(store=None, gateway=None, selected=parse("core"))
    assert service.constants_for("woolworths").delivery_threshold == 50.0
    assert service.constants_for("woolworths", {"delivery_threshold": 0}).delivery_threshold == 0.0
    with pytest.raises(RetailerError):
        service.constants_for("no-such-retailer")
