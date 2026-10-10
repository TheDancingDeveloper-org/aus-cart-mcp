"""Prices and search go through the anonymous tenant; cart actions through the owner's signed-in tenant."""

from aus_cartwatch import config
from aus_cartwatch.service import Service
from tests.conftest import FakeAusCart, product


def two_tenants(store, clock):
    prices = FakeAusCart({"1": product("1", "Milk", 4.0), "2": product("2", "Bread", 3.0)})
    cart = FakeAusCart({"1": product("1", "Milk", 4.0), "2": product("2", "Bread", 3.0)})
    service = Service(
        store,
        client_factory=prices.factory(store, clock=clock),
        cart_client_factory=cart.factory(store, clock=clock),
        notifiers=[],
        clock=clock,
        sleep=clock.sleep,
        tz="Australia/Sydney",
    )
    return service, prices, cart


async def test_traffic_goes_to_the_right_tenant(store, clock):
    service, prices, cart = two_tenants(store, clock)
    await service.search("milk")
    await service.track("1")
    await service.scheduler.refresh()
    assert cart.calls == []  # nothing about prices touched the signed-in tenant
    assert "search_products" in prices.calls
    await service.add_to_cart("1", 1, reason="ui")
    await service.scheduler.snapshot()
    cart.cart = {"2": 1}
    await service.track_from_cart()
    assert set(cart.calls) == {"add_to_cart", "get_cart"}
    assert "add_to_cart" not in prices.calls and "get_cart" not in prices.calls


def test_prices_key_falls_back_to_the_cart_key(monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_KEY", "cart-key")
    monkeypatch.delenv("AUS_CARTWATCH_AUS_CART_PRICES_KEY", raising=False)
    assert config.aus_cart_prices_key() == "cart-key"
    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_PRICES_KEY", "prices-key")
    assert config.aus_cart_prices_key() == "prices-key"


def test_budget_client_picks_the_key(store, monkeypatch):
    from aus_cartwatch import budget

    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_KEY", "cart-key")
    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_PRICES_KEY", "prices-key")
    assert budget.client(store, "woolworths", purpose="prices")._key == "prices-key"
    assert budget.client(store, "woolworths")._key == "cart-key"
