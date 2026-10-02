"""The supported-retailer index.

``RETAILERS`` holds the adapters that work today. ``CATALOGUE`` also lists the
planned ones, so the index (``list_retailers`` and docs/RETAILERS.md) shows
where the project is going without pretending they work.
"""

from __future__ import annotations

from aus_cart_mcp.retailers.base import Retailer, RetailerError, RetailerInfo
from aus_cart_mcp.retailers.woolworths import Woolworths

RETAILERS: dict[str, Retailer] = {r.key: r for r in (Woolworths(),)}

_PLANNED = (
    RetailerInfo("coles", "Coles", "AU", "planned", (), "https://www.coles.com.au", "", ""),
    RetailerInfo("amazon_au", "Amazon Australia", "AU", "planned", (), "https://www.amazon.com.au", "", ""),
    RetailerInfo("kmart", "Kmart Australia", "AU", "planned", (), "https://www.kmart.com.au", "", ""),
    RetailerInfo("bigw", "BIG W", "AU", "planned", (), "https://www.bigw.com.au", "", ""),
)

CATALOGUE: tuple[RetailerInfo, ...] = tuple(r.info for r in RETAILERS.values()) + _PLANNED


def get(key: str) -> Retailer:
    retailer = RETAILERS.get((key or "").strip().lower())
    if retailer is None:
        planned = {r.key for r in _PLANNED}
        if (key or "").strip().lower() in planned:
            raise RetailerError(f"'{key}' is planned but not supported yet")
        raise RetailerError(f"unknown retailer '{key}'; supported: {', '.join(sorted(RETAILERS))}")
    return retailer
