# aus_cartwatch over MCP

aus_cartwatch serves MCP (Streamable HTTP) at `/mcp` on the same port as the web
UI. Every request needs `Authorization: Bearer <key>`; the key's SHA-256 must be
in `AUS_CARTWATCH_MCP_KEYS`, or the key itself in `AUS_CARTWATCH_MCP_KEY`. With
neither set, `/mcp` refuses everything.

Results are JSON text. `discount` is a fraction of the baseline price (0.5 =
half price). Every tool takes an optional `retailer`, which defaults to the
only configured one (Woolworths).

## Tools

| Tool | What it does | Agent-visible |
|---|---|---|
| `list_tracked(include_archived=false)` | Tracked items: current price, baseline, discount, heavy, on special, target, last observed | yes |
| `track_item(query \| product_id \| url, label?, target_price?, threshold?)` | Track a product; a query returns up to 10 candidates instead | yes |
| `untrack_item(product_id)` | Stop tracking (history kept) | yes |
| `price_history(product_id or name, days=90)` | Observed prices (at most 200 points), the stats block, recent sales; `name` matches tracked items | yes |
| `current_deals(min_discount=0, only_tracked=true)` | Open sale episodes, biggest discount first | yes |
| `suggest_items()` | Products bought repeatedly but not tracked, with a confidence score | yes |
| `add_to_cart(product_id, quantity=1)` | Add a known product to the owner's real cart (capped; the owner checks out) | yes |
| `recent_alerts(limit=20, unsent_only=false)` | Recent alerts with whether each was delivered (alerts queue here when no notifier is configured) | yes |
| `budget_status()` | Today's upstream requests against the cap, last refresh, backoff | yes |
| `run_refresh(force=false)` | Run a price refresh now | **hide** (admin) |

With the `aus_cartwatch` server name, the longest prefixed tool name
(`aus_cartwatch__current_deals`) is 28 characters, inside myaiagent's 64.

## Registered in myaiagent (CW-50, 2026-10-05)

Registered for the owner through myaiagent's API (`POST /api/mcp/servers`), as server 2:

- **Name:** `aus_cartwatch`, so tools appear as `aus_cartwatch__<tool>`.
- **URL:** `http://aus-cartwatch:8080/mcp`, the internal address on the shared
  `prod-myaiagent_default` network. Traffic never leaves node b.
- **Header:** `Authorization: Bearer <HOMELAB_AUS_CARTWATCH_MCP_KEY>`, stored encrypted by myaiagent.
- **Hidden tools (owner decision: read and add-to-cart only):** `run_refresh`, `track_item`, `untrack_item`.
- **Verified with myaiagent `73fa959`** through its chat API:
  - "what am I tracking", "what's on sale", "unsent alerts": 1 tool call each;
  - a price history by name: was 3 calls, and now takes `name` directly.
- **Add to cart** through the agent needs a Woolworths session under an hour old (Vogt WI-800).

## Claude Code

`.mcp.json` in a project (the key comes from the environment, never the file):

```json
{
  "mcpServers": {
    "aus_cartwatch": {
      "type": "http",
      "url": "https://aus-cartwatch.sprooty.com/mcp",
      "headers": { "Authorization": "Bearer ${AUS_CARTWATCH_MCP_KEY}" }
    }
  }
}
```

Then ask, for example, "what's on sale from my tracked groceries?".
