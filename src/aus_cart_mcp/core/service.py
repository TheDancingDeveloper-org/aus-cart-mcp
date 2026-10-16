"""The core service: search and the customer's own cart, retailer-neutral.

MCP tools and HTTP routes are adapters over this. They must not reach the
gateway except through here, and this module must not import watch or ui.
"""

from __future__ import annotations

from aus_cart_mcp import __version__
from aus_cart_mcp.features import Features
from aus_cart_mcp.gateway import Gateway
from aus_cart_mcp.retailers import RETAILERS
from aus_cart_mcp.retailers.base import RetailerConstants, RetailerError
from aus_cart_mcp.store import Store


class CoreService:
    """What the core tools do, without knowing they are MCP tools."""

    def __init__(self, store: Store, gateway: Gateway, selected: Features):
        self.store = store
        self.gateway = gateway
        self.selected = selected

    def info(self) -> dict:
        return {
            "version": __version__,
            "features": self.selected.to_dict(),
            "retailers": sorted(RETAILERS),
        }

    def constants_for(self, retailer: str, overrides: dict | None = None) -> RetailerConstants:
        """That retailer's constants, with one tenant's overrides applied.

        ``overrides`` is the caller's per-tenant map for this retailer
        (``delivery_threshold``, ``specials_day``). Unknown or badly typed
        keys are ignored. Watch code asks here instead of hard-coding them.
        """
        adapter = RETAILERS.get(retailer)
        if adapter is None:
            raise RetailerError(f"unknown retailer {retailer}")
        return adapter.constants.overridden(overrides or {})
