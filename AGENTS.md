# AGENTS.md: working on aus-cart-mcp

This is the operating manual for AI coding agents, and for humans in a hurry.
Read [docs/LEGAL.md](docs/LEGAL.md) before changing what the service does.

## What this is

An MCP server (Streamable HTTP at `/mcp`) that searches Australian retailers and
manages a customer's **own** online cart. It's multi-tenant: each customer has
an API key (stored hashed) and encrypted retailer sessions, and every tool call
is metered. Woolworths is supported; Coles, Amazon AU, Kmart and BIG W are
planned (see docs/RETAILERS.md).

## Layout

| Path | Responsibility |
|---|---|
| `src/aus_cart_mcp/server.py` | MCP tools, bearer auth middleware, `/healthz`, input validation |
| `src/aus_cart_mcp/gateway.py` | The **only** way out to retailers: sessions, throttle, daily cap, circuit breaker (data API only, not photos), search/product/cart caches, idle guest-connection recycling, metering |
| `src/aus_cart_mcp/retailers/base.py` | Retailer-neutral types (`Product`, `Cart`, `RetailerInfo`) and the `Retailer` protocol |
| `src/aus_cart_mcp/retailers/<key>.py` | One adapter per retailer, plain HTTP mapping only |
| `src/aus_cart_mcp/retailers/__init__.py` | The retailer index: `RETAILERS` (working) and `CATALOGUE` (including planned) |
| `src/aus_cart_mcp/store.py` | SQLite: tenants (hashed keys), Fernet-encrypted sessions, usage |
| `src/aus_cart_mcp/config.py` | Environment settings (`AUS_CART_MCP_*`, with `GROCERY_MCP_*` fallback) |
| `src/aus_cart_mcp/mock/` | Mock retailers built from recorded responses, used by tests and local dev |
| `tests/{unit,contract,integration,e2e,live}` | The test layers; see README |

## Invariants (never break these)

1. **Never evade bot protection.**
   - No proxy rotation, no fingerprint spoofing, no CAPTCHA or challenge solving.
   - When a retailer blocks us, the breaker pauses and we back off.
2. **All retailer traffic goes through `Gateway`.** Adapters never create clients,
   store sessions, sleep or cache.
3. **Admin tools stay admin-only.** `connect_session` and `disconnect_session`
   are for apps, and the docs must keep telling clients to hide them from models.
4. **Secrets never reach logs or errors:** cookie values, API keys, `AUS_CART_MCP_SECRET`.
5. **Recorded fixtures carry no personal data.**

## Commands

```sh
uv sync                                     # install (Python 3.13+)
uv run ruff format . && uv run ruff check . # format and lint
uv run pytest --cov                         # all offline layers, coverage floor 85%
uv run pytest tests/contract                # just the retailer contract suite
AUS_CART_MCP_LIVE=1 uv run pytest -m live   # read-only live smoke (needs network)
uv run python -m aus_cart_mcp.mock          # mock Woolworths on :8090
AUS_CART_MCP_SECRET=dev uv run python -m aus_cart_mcp serve
```

## Definition of done

- `ruff format --check`, `ruff check` and `pytest --cov` all pass, with coverage at least 85%.
- New behaviour has tests in the lowest layer that can prove it.
- A new or changed retailer:
  - passes `tests/contract`;
  - has its mock and recorded fixtures updated;
  - has its `RetailerInfo` and `docs/RETAILERS.md` updated, with `verified` set
    only after a live read-only check.
- A change to tool names or arguments:
  - updates the README tool table;
  - keeps deprecated aliases for one minor release.
- Security-relevant changes update docs/LEGAL.md or SECURITY.md.

## How sessions work today

Customers sign in on the retailer's own site, on their own device. Apps hand the
resulting cookies to `connect_session`; the server stores them encrypted.


Sessions expire on the retailer's schedule. A Woolworths login token lasts 60 minutes,
and ordinary calls don't renew it (Vogt WI-800). After that, calls made with the stale
session can be refused outright. Clients that only need prices should use a tenant
with **no** session, so every call is a fresh anonymous visitor. aus_cartwatch does this
with tenant `aus-cartwatch-prices`.
## Recipes

- **Add a retailer:** follow docs/RETAILERS.md § "Adding a retailer"
  (fixtures, then mock, then adapter, then contract, then live, then index).
- **Add a tool:** register it in `server.build()` with `@tool()`. Raise
  `RetailerError` for errors the user should see. Add an integration test in
  `tests/integration/test_http.py`.
- **Change a limit:** it lives in `gateway.Limits`. The policy tests in
  `tests/unit/test_gateway_policy.py` use a fake clock, so no real waits.
