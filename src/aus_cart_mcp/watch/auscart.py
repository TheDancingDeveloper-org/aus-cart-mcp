"""Compatibility stand-in for the deleted HTTP MCP client.

Watch used to talk to a separate aus-cart-mcp process. Inside the product server
the core Gateway is called in-process. This module re-exports the client and the
error types so subtree tests that import ``aus_cart_mcp.watch.auscart`` collect.
"""

from aus_cart_mcp.watch.client import AusCartClient
from aus_cart_mcp.watch.types import (
    Blocked,
    Cart,
    Product,
    RetailerError,
    SessionRequired,
    Unavailable,
    classify,
    parse_unit_price,
)

__all__ = [
    "AusCartClient",
    "Blocked",
    "Cart",
    "Product",
    "RetailerError",
    "SessionRequired",
    "Unavailable",
    "classify",
    "parse_unit_price",
]
