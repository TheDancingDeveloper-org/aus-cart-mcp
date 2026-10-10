# Design notes

## ADR: three layers in one repo

aus-cart-mcp is one product with three layers, selected at runtime by
`AUS_CART_MCP_FEATURES` and installable as pip extras. One image ships all of
them; the flag decides which run.

| Layer | Package | Depends on | Owns |
|---|---|---|---|
| `core` | `aus_cart_mcp.core` (code still at the package root) | nothing above it | gateway, retailer adapters, tenants, cart tools |
| `watch` | `aus_cart_mcp.watch` | `core` | tracking, history, alerts, the scheduler |
| `ui` | `aus_cart_mcp.ui` | `watch` | the cartwatch pages and desktop retailer connect |

Rules:

- `core` does not import `watch` or `ui`. `watch` does not import `ui`.
  A test scans the source, so a forbidden import fails even when no test runs it.
- `ui` implies `watch`, because the pages read the watch data.
- Every `watch` record is keyed by (tenant, retailer). Product ids are
  retailer-scoped, every watch tool takes a `retailer` argument, and anything
  specific to one retailer (thresholds, specials day) lives on that retailer's
  adapter rather than in `watch`.
- The household shopping list and checkout stay outside this repo. Checkout is
  never automated.

`server_info` and `/healthz` report the enabled layers, so a deployment can
show what it is actually running.
