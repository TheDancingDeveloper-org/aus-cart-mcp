"""Layer 2: price tracking, history and alerts.

Off unless ``AUS_CART_MCP_FEATURES`` includes ``watch``. Empty until the
aus-cartwatch subtree lands. Every record this layer keeps is keyed by
(tenant, retailer): product ids are retailer-scoped, tools take a ``retailer``
argument, and retailer constants live on the retailer adapter.
"""
