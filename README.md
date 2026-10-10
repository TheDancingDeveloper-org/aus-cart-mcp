# aus_cartwatch

A grocery price tracker for one household. It watches the Woolworths prices of
the things you buy, tells you when they are on sale or heavily discounted, and
adds them to your own Woolworths cart when you say so.

It never talks to Woolworths directly: every retailer request goes through
[aus-cart-mcp](https://github.com/TheDancingDeveloper-org/aus-cart-mcp), whose
gateway rate-limits, caps and meters the traffic. aus_cartwatch also serves its
own MCP endpoint, so Claude and myaiagent can ask it about tracked items and deals.

> Personal use on your own account only. Read [docs/LEGAL.md](docs/LEGAL.md).

## Status

`incubating`, MVP1, live on node b since 2026-10-03 (26 items tracked).

What it does:
- **Tracks items:** by search with multi-select, by product id or link, from the cart, or
  from a receipt (by hand for now).
- **Refreshes prices** once a day within a hard daily budget: one Woolworths request per
  20 items, through a never-signed-in aus-cart-mcp tenant.
- **Detects sales and heavy discounts** (30% or more).
- **Alerts** on the UI's Alerts page and the MCP `recent_alerts` tool, with add-to-cart. Telegram delivery is ready but deferred.
- **Suggests items** the household keeps buying.
- **Serves** a phone-friendly web UI (with photos and the current cart) and an MCP endpoint.

Cart features (Cart page, add to cart) need the owner's Woolworths session, and that
login lasts 60 minutes (WI-800). Reconnect it in the myaiagent app first.

## Quick start

```sh
uv sync
uv run pytest --cov
export AUS_CARTWATCH_DB=./dev.db AUS_CARTWATCH_AUS_CART_URL=http://localhost:8080/mcp
export AUS_CARTWATCH_AUS_CART_KEY=...          # signed-in tenant: the cart
export AUS_CARTWATCH_AUS_CART_PRICES_KEY=...   # never-signed-in tenant: prices and search
uv run python -m aus_cartwatch track "full cream milk"       # lists candidates
uv run python -m aus_cartwatch track 888140 --target 4.00     # track by id
uv run python -m aus_cartwatch refresh
uv run python -m aus_cartwatch tracked
echo 'a household password' | uv run python -m aus_cartwatch hash-password   # → AUS_CARTWATCH_UI_PASSWORD_HASH
PORT=8081 uv run python -m aus_cartwatch serve                # UI on http://localhost:8081, MCP on /mcp
```

## Surfaces

- **Web UI** (`/`): tracked items with photos and sparklines, deals, the current
  Woolworths cart, suggestions, item history, search with multi-select tracking,
  settings, runs and the budget ledger. Password-protected.
- **MCP** (`/mcp`): see [docs/MCP.md](docs/MCP.md) for the tools and how to register it in
  myaiagent or Claude Code.
- **Telegram**: alerts with *Add 1 / Add 2 / Snooze* buttons.
- **CLI**: `python -m aus_cartwatch --help`.
- **Ops**: `/healthz` (JSON) and `/metrics` (Prometheus text).

## More

- [AGENTS.md](AGENTS.md): layout, invariants, commands, configuration, deploy.
- [docs/DESIGN.md](docs/DESIGN.md): the thesis, the two tenants, the traffic budget, analytics.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): prod stack, secrets, exposure, backups, health.
- [docs/MCP.md](docs/MCP.md): the MCP tools and client registration.
- [docs/LEGAL.md](docs/LEGAL.md): the legal position.
