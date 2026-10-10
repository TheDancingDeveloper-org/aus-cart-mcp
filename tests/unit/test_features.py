"""The feature flag selects layers and refuses unknown names."""

import pytest

from aus_cart_mcp.features import FeaturesError, parse


def test_core_is_the_default_and_always_on():
    assert parse("").layers == ["core"]
    assert parse("watch").layers == ["core", "watch"]


def test_ui_implies_watch():
    selected = parse("core, ui")
    assert selected.layers == ["core", "watch", "ui"]
    assert selected.has("ui")


def test_subflags_are_reported_separately():
    selected = parse("watch,telegram,receipts")
    assert selected.to_dict() == {"layers": ["core", "watch"], "flags": ["receipts", "telegram"]}


def test_unknown_feature_is_rejected():
    with pytest.raises(FeaturesError):
        parse("core,billing")
