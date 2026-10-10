# AGENTS.md: working on aus_cartwatch

This is the operating manual for AI coding agents, and for humans in a hurry.
Read [docs/DESIGN.md](docs/DESIGN.md) for the why and [docs/LEGAL.md](docs/LEGAL.md)
before changing what the service does.

## What this is

A grocery price tracker for one household. It tracks items the household buys,
records their Woolworths price over time, alerts on sales and heavy discounts,
and can add an item to the owner's own Woolworths cart. It is an **MCP client of
aus-cart-mcp** (its only path to a retailer) and an **MCP server of its own**,
so Claude and myaiagent can use it.

Work is tracked in Vogt, project `aus-cartwatch`, items labelled `cartwatch`
(`CW-nn` in titles). WI-746 is the source of truth for names.

## Layout

| Path | Responsibility |
|---|---|
| `src/aus_cartwatch/auscart.py` | The **only** way out to a retailer: the typed aus-cart-mcp MCP client and its error mapping |
| `src/aus_cartwatch/budget.py` | Binds a client to the budget ledger and picks the tenant (prices or cart) |
| `src/aus_cartwatch/scheduler.py` | Refresh slots, cart snapshots and the budget governor (cap, shared-tenant guard, blocked backoff) |
| `src/aus_cartwatch/tracking.py` | Nominating items and recording observations |
| `src/aus_cartwatch/analytics.py` | Baseline, discount, sale episodes, the stats block |
| `src/aus_cartwatch/detect.py` | Cart snapshots → shop episodes → "track this?" candidates |
| `src/aus_cartwatch/alerts/` | Alert rules and delivery (`__init__`), Telegram and webhook notifiers |
| `src/aus_cartwatch/receipts.py` | Receipt scans: OCR through OpenRouter, matching rules, confirmation (CW-40) |
| `src/aus_cartwatch/cart.py` | Add to cart: known products only, capped, confirmed, session/blocked handling |
| `src/aus_cartwatch/service.py` | Wires store, client factory, scheduler, notifiers and button actions together |
| `src/aus_cartwatch/server.py` | HTTP app: MCP tools at `/mcp` (bearer auth), `/healthz`, `/metrics`, lifespan |
| `src/aus_cartwatch/web/` | Web UI routes, login, templates |
| `src/aus_cartwatch/policy.py` | Household preferences (cadence, thresholds, quiet hours) stored in `settings` |
| `src/aus_cartwatch/store.py` | SQLite: migration runner and every repository function |
| `src/aus_cartwatch/migrations/` | Numbered SQL migrations (`NNNN_name.sql`), applied once, in order |
| `src/aus_cartwatch/config.py` | Environment settings (`AUS_CARTWATCH_*`, plus `PORT`) |
| `tests/unit` | Pure logic and policy, with a fake clock and an in-memory aus-cart (`fake` fixture) |
| `tests/integration` | The whole HTTP app over real sockets with the fake aus-cart |
| `tests/e2e` | Real sockets: the pinned aus-cart-mcp server and its mock Woolworths (`aus_cart` fixture) |

## Invariants (never break these)

1. **All retailer access goes through aus-cart-mcp**, via `auscart.py`. No
   module opens a connection to a retailer site, scrapes, or imports retailer
   endpoints. aus-cart-mcp's gateway (spacing, cap, breaker, metering) must
   always apply.
2. **No bot-protection evasion,** inherited from aus-cart-mcp: when it reports a
   block, the run is skipped, not retried.
3. **The daily budget is enforced in code**, not in config comments: the
   scheduler refuses to start a call that would exceed `AUS_CARTWATCH_DAILY_UPSTREAM_CAP`
   upstream requests for the day, and skips the run when the shared tenant is
   past half of aus-cart-mcp's own cap.
4. **Secrets never reach logs or errors:** `AUS_CARTWATCH_AUS_CART_KEY`, bot
   tokens, retailer cookies.
5. **No checkout.** aus_cartwatch adds to the cart; the person reviews and pays
   on the retailer's site. No payment, delivery slots or addresses.
6. **Migrations are append-only.** Never edit a migration that has shipped;
   add the next number. No `;` inside comments or strings: the runner splits on it.
8. **Prices never use the owner's session.** Price traffic goes through the anonymous
   prices tenant (`purpose="prices"`); only cart reads and writes use the signed-in tenant.
   Price runs and cart runs back off separately.
7. **Cache before calling.** Lookups and searches check the product cache first;
   only scheduled refreshes deliberately skip it. A photo is fetched once per product.

## Commands

```sh
uv sync                                       # install (Python 3.13+)
uv run ruff format . && uv run ruff check .   # format and lint
uv run pytest --cov                           # all layers offline, coverage floor 85%
uv run pytest tests/e2e                       # just the aus-cart-mcp end-to-end layer
AUS_CARTWATCH_DB=./dev.db uv run python -m aus_cartwatch db migrate
AUS_CARTWATCH_DB=./dev.db uv run python -m aus_cartwatch serve
uv run python -m aus_cartwatch --help           # track, tracked, stats, candidates, refresh, add, ...
uv run python -m aus_cartwatch auscart status   # needs AUS_CARTWATCH_AUS_CART_URL / _KEY
uv run python -m aus_cartwatch db stats        # row counts and file size; `db compact` applies retention
```

For local work against a real wire format, run aus-cart-mcp's mock and server
from its own checkout (`python -m aus_cart_mcp.mock`, then `serve` with
`AUS_CART_MCP_WOOLWORTHS_BASE_URL`) and set `AUS_CARTWATCH_AUS_CART_URL` and
`AUS_CARTWATCH_AUS_CART_KEY`.

## Definition of done

- `ruff format --check`, `ruff check` and `pytest --cov` all pass, with coverage at least 85%.
- New behaviour has tests in the lowest layer that can prove it. Anything that
  calls aus-cart-mcp has an e2e test through the `aus_cart` fixture.
- A schema change is a new migration file with a test.
- A change to MCP tool names or arguments updates docs/MCP.md.
- A change to an analytics definition updates docs/DESIGN.md § Price analytics and its pinning test.
- A change in what is sent to a retailer, or how often, updates docs/DESIGN.md
  § Traffic budget.
- Work is recorded on its Vogt item; the branch is bound to it (`wi-<n>`).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUS_CARTWATCH_DB` | `/data/aus-cartwatch.db` | SQLite file |
| `AUS_CARTWATCH_AUS_CART_URL` | `http://grocery-mcp:8080/mcp` | aus-cart-mcp endpoint |
| `AUS_CARTWATCH_AUS_CART_KEY` | (none) | aus-cart-mcp key of the owner's signed-in tenant, for the cart (secret) |
| `AUS_CARTWATCH_AUS_CART_PRICES_KEY` | the cart key | key of a never-signed-in tenant, for prices and search (secret) |
| `AUS_CARTWATCH_RETAILER` | `woolworths` | the retailer tracked (MVP1: one) |
| `AUS_CARTWATCH_DAILY_UPSTREAM_CAP` | `60` | aus_cartwatch's upstream retailer requests per day |
| `AUS_CARTWATCH_GATEWAY_DAILY_CAP` | `2000` | aus-cart-mcp's own daily cap (for the shared-tenant guard) |
| `AUS_CARTWATCH_SHARED_CAP_FRACTION` | `0.5` | skip a run when the tenant has used this share of the gateway cap |
| `AUS_CARTWATCH_TZ` | `Australia/Sydney` | schedule and quiet-hours time zone |
| `AUS_CARTWATCH_SCHEDULER` | `on` | run the refresh/snapshot/alert loop inside `serve` |
| `AUS_CARTWATCH_MCP_KEY` / `_MCP_KEYS` | (none) | key, or SHA-256 digests, allowed on `/mcp` (secret) |
| `AUS_CARTWATCH_UI_PASSWORD_HASH` | (none) | web UI password hash (`python -m aus_cartwatch hash-password`) |
| `AUS_CARTWATCH_SECRET` | (none) | signs the web UI session cookie (secret) |
| `AUS_CARTWATCH_TELEGRAM_TOKEN` | (none) | Telegram bot token (secret) |
| `AUS_CARTWATCH_TELEGRAM_CHAT_IDS` | (none) | allowed chat ids, comma-separated |
| `AUS_CARTWATCH_WEBHOOK_URL` | (none) | optional JSON webhook for alerts |
| `AUS_CARTWATCH_IMMEDIATE_ON_SALE` | off | send on-sale alerts at once instead of in the digest |
| `AUS_CARTWATCH_ENABLE_AUTO_ADD` | off | allow the per-item auto-add flag (not wired in MVP1) |
| `AUS_CARTWATCH_MAX_ADD_QUANTITY` | `6` | per add-to-cart cap |
| `AUS_CARTWATCH_PUBLIC_URL` | (none) | web UI base URL, for links in alerts |
| `AUS_CARTWATCH_PRODUCT_CACHE_HOURS` | `24` | reuse seen products and searches for this long |
| `AUS_CARTWATCH_FETCH_PHOTOS` | off | store photos server-side (the image CDN refused node b) |
| `AUS_CARTWATCH_OPENROUTER_KEY` | (none) | OpenRouter key for receipt OCR (secret) |
| `AUS_CARTWATCH_OCR_MODEL` | `google/gemini-2.5-flash-lite` | vision model for receipts |
| `AUS_CARTWATCH_OCR_DAILY_CAP` | `10` | receipt readings per day |
| `AUS_CARTWATCH_RECEIPTS_DIR` | `/data/receipts` | where receipt photos are kept |
| `AUS_CARTWATCH_BACKUP_DIR` | (none) | nightly SQLite backup directory (03:30 local) |
| `AUS_CARTWATCH_BACKUP_KEEP` | `14` | backups kept |
| `AUS_CARTWATCH_HOST` | `0.0.0.0` | listen address |
| `PORT` | `8080` | listen port |

## Deploy

CI (`.github/workflows/ci.yml`) tests every PR on the org's self-hosted runners,
publishes `ghcr.io/thedancingdeveloper-org/aus-cartwatch:{sha,latest}` from
`main`, and deploys only when the repo variable `DEPLOY_ENABLED` is `true`
(same `DEPLOY_*` variables and secrets as aus-cart-mcp). The prod stack is
`prod/aus-cartwatch` in the ops repo, Komodo `prod-aus-cartwatch` on node b. Runbook:
[docs/OPERATIONS.md](docs/OPERATIONS.md).
