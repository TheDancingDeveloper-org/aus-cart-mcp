"""Shared fixtures. Every layer except `live/` runs offline against the mock retailers."""

from __future__ import annotations

import httpx
import pytest

from aus_cart_mcp.gateway import Gateway, Limits
from aus_cart_mcp.mock import woolworths as mock_woolworths
from aus_cart_mcp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "aus-cart.db", "test-secret")


@pytest.fixture()
def mock_state():
    return mock_woolworths.MockState()


@pytest.fixture()
def mock_app(mock_state):
    return mock_woolworths.create_app(mock_state)


class FakeClock:
    """A controllable monotonic clock; `sleep` advances it instead of waiting."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def make_gateway(store, mock_app, clock):
    def factory(**limits) -> Gateway:
        params = {"min_interval": 0, "jitter": 0, **limits}
        return Gateway(
            store,
            Limits(**params),
            transport_factory=lambda: httpx.ASGITransport(app=mock_app),
            clock=clock,
            sleep=clock.sleep,
        )

    return factory


@pytest.fixture()
def gateway(make_gateway):
    return make_gateway()


def pytest_configure(config):
    config.addinivalue_line("markers", "live: talks to real retailer sites (opt-in: AUS_CART_MCP_LIVE=1)")
