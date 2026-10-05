# Supported retailers

This index lists each retailer aus-cart-mcp works with, or plans to. The same
data is served live by the `list_retailers` tool, which takes
`include_planned=true`. A test (`tests/unit/test_index.py`) fails if this page
and the code disagree.

| Key | Retailer | Country | Status | Search | Read cart | Write cart | Session | Last verified |
|---|---|---|---|---|---|---|---|---|
| `woolworths` | Woolworths | AU | **supported** | ✅ | ✅ | ✅ | browser cookies | 2026-10-02 |
| `coles` | Coles | AU | planned | | | | | |
| `amazon_au` | Amazon Australia | AU | planned | | | | | |
| `kmart` | Kmart Australia | AU | planned | | | | | |
| `bigw` | BIG W | AU | planned | | | | | |

**Status:**
- **supported:** passes the contract suite against its mock and was verified against the live site on the date shown.
- **experimental:** works, but the live site is still being validated.
- **planned:** on the roadmap. Calls return a clear "planned but not supported yet" error.

**Session:** how a customer connects their account. "Browser cookies" means the
customer signs in on the retailer's own site, on their own device (for example in
an app's in-app browser). The app then hands the resulting cookies to `connect_session`.
With this method the server doesn't handle retailer passwords.

## Woolworths (`woolworths`)

- **Endpoints:** the same ones woolworths.com.au's own web app calls:
  - `POST /apis/ui/Search/products` for search;
  - `GET /apis/ui/products/<id>,<id>,…` for products by stockcode, 20 per request (verified read-only 2026-10-03);
  - `GET /apis/ui/Trolley` for the cart;
  - `POST /api/v3/ui/trolley/update` to set quantities, where 0 removes;
  - `GET /api/ui/v2/bootstrap` for who is signed in;
  - `GET cdn0.woolworths.media/content/wowproductimages/medium/<stockcode>.jpg` for product photos
    (fetched on request through the gateway; clients should keep their own copy).
- **Product fields:** `price`, `was_price` (`WasPrice`), `savings` (`SavingsAmount`, else was − price), `unit_price` (the cup string) and its numeric form `unit_price_value` / `unit_price_unit` (`CupPrice`/`CupMeasure`, else parsed from the string; units normalised to `1L`, `100mL`, `1kg`, `100g`, `1ea`), `image_url`.
- **Session:** `login_url` = `https://www.woolworths.com.au/shop/securelogin`, `cookie_url` = `https://www.woolworths.com.au/`.
- **Limits:**
  - one request every ~1.5–2 s per retailer, shared by all customers;
  - at most 30 products per cart update;
  - a 30-minute pause after any block.
- **Covered today:** search and the cart.
- **Mock and fixtures:** `aus_cart_mcp.mock.woolworths`, built from responses recorded on the verified date (`src/aus_cart_mcp/mock/data/woolworths/`).

## Adding a retailer

1. **Record fixtures.** Capture anonymous responses from the retailer's public
   endpoints (search, guest session, empty cart) into
   `src/aus_cart_mcp/mock/data/<key>/`, trimmed to the fields you read. Never
   commit personal data. Logged-in shapes are hand-written from observed structure.
2. **Write the mock.** `src/aus_cart_mcp/mock/<key>.py` exposes `MockState`,
   `create_app(state)` and `signed_in_cookie(state)`. Register it in `mock/__init__.py`.
3. **Write the adapter.** `src/aus_cart_mcp/retailers/<key>.py` implements
   `retailers.base.Retailer`, with a `RetailerInfo` (status `experimental` first).
   Register it in `retailers/__init__.py` and remove it from `_PLANNED`.
4. **Pass the contract suite.** Run `uv run pytest tests/contract`. It runs every
   adapter against its mock: search shape, guest vs signed-in, cart round trip,
   block detection, and batch limits.
5. **Verify live (read-only).** Run `AUS_CART_MCP_LIVE=1 uv run pytest -m live`,
   which only searches. Then set `verified` to today and the status to `supported`.
6. **Update this page.** Add the row and a section like the Woolworths one above.

Read [LEGAL.md](LEGAL.md) first. Some retailers forbid automated access. That
decides whether a retailer belongs here at all, and it means we never evade
bot protection.
