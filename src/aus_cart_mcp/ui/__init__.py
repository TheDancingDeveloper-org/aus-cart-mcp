"""Layer 3: the optional cartwatch pages and desktop retailer connect.

Off unless ``AUS_CART_MCP_FEATURES`` includes ``ui``, which also turns ``watch``
on. Served only at ``/ui``; a process without this layer answers 404 there.
"""
