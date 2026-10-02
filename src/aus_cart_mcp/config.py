"""Runtime settings from the environment.

``AUS_CART_MCP_*`` names are canonical. The pre-rename ``GROCERY_MCP_*`` names
are still read as a fallback, so an existing deployment keeps its key and data.
"""

from __future__ import annotations

import os

DEFAULT_DB = "/data/aus-cart.db"


def env(name: str, default: str = "") -> str:
    value = os.environ.get(f"AUS_CART_MCP_{name}")
    if value is None:
        value = os.environ.get(f"GROCERY_MCP_{name}")
    return default if value is None else value


def db_path() -> str:
    return env("DB", DEFAULT_DB)


def secret() -> str:
    return env("SECRET")


def base_url_override(retailer_key: str) -> str | None:
    """`AUS_CART_MCP_<KEY>_BASE_URL` points a retailer at a mock (local dev, end-to-end tests)."""
    return os.environ.get(f"AUS_CART_MCP_{retailer_key.upper()}_BASE_URL") or None


def port() -> int:
    return int(os.environ.get("PORT", "8080"))
