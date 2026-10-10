# Changelog

## 0.3.0 (unreleased)

- Layers. `AUS_CART_MCP_FEATURES` selects `core` (default), `watch` and `ui`,
  with sub-flags `telegram` and `receipts`. One image, chosen at runtime.
  `[watch]` and `[ui]` extras. `server_info` and `/healthz` report the enabled
  layers. `/ui` is 404 until the `ui` layer is on. No change to cart behaviour.
