"""FEATURES=core must advertise the same tool list as 0.2.x, plus server_info.

The three cart docstrings are pinned word for word so a layer change cannot
silently rewrite what a model sees in tools/list.
"""

from aus_cart_mcp import features
from aus_cart_mcp.server import build

CORE_TOOLS = {
    "server_info",
    "list_retailers",
    "search_products",
    "get_cart",
    "get_pending_cart",
    "sync_cart",
    "clear_pending",
    "add_to_cart",
    "set_cart_quantities",
    "get_trolley",
    "add_to_trolley",
    "set_trolley_quantities",
    "connection_status",
    "connect_session",
    "disconnect_session",
    "get_products",
    "get_product_image",
    "usage_summary",
}

DOCSTRINGS = {
    "get_cart": "Show the live cart, plus any writes still waiting to sync.",
    "get_pending_cart": (
        "Cart writes recorded but not yet on the retailer's cart.\n\n"
        "pending retries later. failed is permanent and is not retried. synced ops are omitted."
    ),
    "sync_cart": (
        "Push recorded writes. Re-reads the live cart and applies only the difference.\n\n"
        "If the retailer is paused this raises and the writes stay pending."
    ),
}


def test_core_tools_list_matches_0_2(store, gateway):
    mcp, _ = build(store, gateway, features.parse("core"))
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == CORE_TOOLS
    for name, text in DOCSTRINGS.items():
        assert tools[name].description == text
