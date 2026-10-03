# aus-cart-mcp

**An MCP server that lets an AI assistant search Australian retailers and manage
*your own* online cart.** Woolworths is supported today; Coles, Amazon Australia,
Kmart and BIG W are next ([supported retailers](docs/RETAILERS.md)).

Ask your assistant to *"add 3L milk, a dozen eggs and wholemeal bread to my
Woolworths cart"*. It searches, picks products and fills your real cart. Today
the tools cover search and the cart; you review and finish the order with the
retailer.

> ⚠️ **Unofficial.** Not affiliated with or endorsed by any retailer. It calls the
> same endpoints the retailers' own websites use, which their terms may not allow.
> Read [docs/LEGAL.md](docs/LEGAL.md) before using it, and especially before
> offering it to anyone else.

## Features

- **MCP over Streamable HTTP** (`/mcp`), so it works with any MCP client: Claude
  Desktop, Claude Code, your own agent.
- **Tools:**

  | Tool | What it does |
  |---|---|
  | `list_retailers` | the retailer index |
  | `search_products` | search a retailer's catalogue |
  | `get_cart` | show the cart |
  | `add_to_cart` | add products to the cart |
  | `set_cart_quantities` | set exact quantities; 0 removes |
  | `connection_status` | whether the retailer session is connected |
  | `connect_session` / `disconnect_session` | connect or forget a retailer session (admin) |
  | `get_products` | look up to 100 products by id in one or a few upstream requests |
  | `get_product_image` | a product's photo (base64); fetch once and keep a copy |
  | `usage_summary` | metered usage for this account |

- **Multi-tenant:**
  - each customer has their own API key, stored only as a SHA-256 hash;
  - their retailer sessions are encrypted at rest (Fernet);
  - a customer's tools only ever act for them.
- **Polite by design.** For each retailer, requests are serialised and spaced,
  there's a daily cap, and a circuit breaker pauses everything for 30 minutes
  when the site pushes back. It never evades bot protection.
- **Metered.** Every tool call records its tool, retailer, outcome and the
  number of upstream requests it made, which is the basis for pay-per-use.
- **Sign in on the retailer's site.** Customers sign in on the retailer's own site, on
  their own device, and the resulting session cookies are handed over.

## Quick start

```sh
# Docker
docker run -d --name aus-cart-mcp -p 8080:8080 -v aus-cart:/data \
  -e AUS_CART_MCP_SECRET="$(openssl rand -base64 48)" \
  ghcr.io/thedancingdeveloper-org/aus-cart-mcp:latest
docker exec aus-cart-mcp python -m aus_cart_mcp tenant create me   # prints your API key once

# or from source
uv sync
AUS_CART_MCP_SECRET=dev AUS_CART_MCP_DB=./dev.db uv run python -m aus_cart_mcp serve
```

Point your MCP client at `http://localhost:8080/mcp` with the header
`Authorization: Bearer <your key>`.

### Connecting a retailer account

`list_retailers` gives each retailer a `login_url` and a `cookie_url`. Your app:
1. opens `login_url` in an in-app browser;
2. lets you sign in;
3. reads the cookies for `cookie_url`;
4. calls `connect_session` with them.

Hide `connect_session` and `disconnect_session` from the model: they're for
your app, not the agent.

### Try it without touching a real retailer

```sh
uv run python -m aus_cart_mcp.mock            # a mock Woolworths on :8090
AUS_CART_MCP_WOOLWORTHS_BASE_URL=http://127.0.0.1:8090 \
AUS_CART_MCP_SECRET=dev uv run python -m aus_cart_mcp serve
```

Sign in at `http://127.0.0.1:8090/shop/securelogin` to get a session.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUS_CART_MCP_SECRET` | *(required)* | Key that encrypts stored retailer sessions. Keep it in a secret manager. |
| `AUS_CART_MCP_DB` | `/data/aus-cart.db` | SQLite file for tenants, sessions and usage. |
| `PORT` | `8080` | HTTP port. |
| `AUS_CART_MCP_<RETAILER>_BASE_URL` | – | Point a retailer at a mock (dev and tests). |

Customers: `python -m aus_cart_mcp tenant create|rotate|disable|enable <name>` and `tenant list`.

## Development

```sh
uv sync
uv run ruff format --check . && uv run ruff check .
uv run pytest --cov                     # unit, contract, integration and e2e, all offline
AUS_CART_MCP_LIVE=1 uv run pytest -m live   # read-only smoke test against real sites
```

The tests come in layers:
- `unit/`: pure logic; the gateway policy runs on a fake clock;
- `contract/`: every retailer adapter must pass this against its mock;
- `integration/`: the gateway and the MCP server over real HTTP;
- `e2e/`: real sockets, a sign-in on the mock retailer, then a fill and clear of the cart;
- `live/`: opt-in and read-only.

Adding a retailer is a checklist: see [docs/RETAILERS.md](docs/RETAILERS.md#adding-a-retailer).

## AI-friendly repository

This repo is built to be worked on by AI coding agents as well as people:

- **[AGENTS.md](AGENTS.md)** holds the operating manual for agents: layout,
  invariants, commands and the definition of done. [CLAUDE.md](CLAUDE.md)
  points Claude Code at it.
- **[llms.txt](llms.txt)** is a compact map of the repo and docs for LLMs.
- **Guard-rails as tests:**
  - the contract suite tells an agent exactly what a new retailer must satisfy;
  - the index test keeps docs and code in step;
  - a coverage floor stops untested code from landing.
- **One-command checks** (`ruff` and `pytest`) that run offline in seconds, so
  an agent can verify its own work.
- **Small, single-purpose modules** with docstrings that state each one's job
  and its limits.

## Licence

[AGPL-3.0](LICENSE). If you run a modified version as a network service, you
must offer its source to the people using it.
