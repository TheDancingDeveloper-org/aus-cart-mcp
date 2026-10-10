# Design

Source: Vogt initiative "cartwatch: grocery price tracker on aus-cart-mcp"
(2026-10-03) and decisions WI-746 (names), WI-747 (tenancy, alerts) and WI-748
(placement). The initiative text predates the rename and says `cartwatch`;
WI-746 is the naming source of truth.

## Thesis

aus_cartwatch never talks to a retailer. It is:

- an **MCP client of aus-cart-mcp**, the only retailer path, so the gateway's
  throttle, daily cap, circuit breaker and metering always apply;
- an **MCP server of its own** (tracked items, history, deals, add to cart).
  myaiagent already has a generic MCP client, so exposing aus_cartwatch there
  is a registration (server name `aus_cartwatch`, tools `aus_cartwatch__<tool>`),
  not a code change in myaiagent.

Alerts go to a dedicated Telegram bot in MVP1 and through myaiagent push in
MVP2. A small tailnet-only web UI covers nomination and history until the agent
surface is in place.

## Decisions

- **Names (WI-746).** Repo `TheDancingDeveloper-org/aus-cartwatch` (private),
  image `ghcr.io/thedancingdeveloper-org/aus-cartwatch`, package `aus_cartwatch`,
  env prefix `AUS_CARTWATCH_`, Infisical `apps/prod/HOMELAB_AUS_CARTWATCH_*`,
  ops stack `prod/aus-cartwatch`, Komodo `prod-aus-cartwatch`, host
  `aus-cartwatch.sprooty.com` (tailnet-only). Licence: private, all rights reserved.
- **Tenancy (WI-747, amended 2026-10-03).** Two aus-cart-mcp tenants (see
  § Two aus-cart-mcp tenants): `aus-cartwatch-prices`, never signed in, carries all price
  traffic; the owner's `myaiagent-owner` (same session and cart as myaiagent) is used only
  for the cart. The original plan was one shared tenant. It changed once the owner's login
  turned out to last 60 minutes, and a stale session poisons every request. A new
  Telegram bot for MVP1 alerts (pending: the owner creates it).
- **Placement (WI-748).** Own stack on node b, joining the myaiagent stack
  network (declared external) to reach aus-cart-mcp at `aus-cart-mcp:8080`.
  Tailnet-only hostname through host Caddy. SQLite in a named volume.

## What aus-cart-mcp gives us (main `afac3bb`, 2026-10-03)

- Tools: `list_retailers`, `search_products(query, limit, specials_only)`,
  `get_products(product_ids)` (batch by id, 20 per upstream request, CW-10),
  `get_product_image(product_id)`, `get_cart`, `add_to_cart`, `set_cart_quantities`,
  `connection_status`, `connect_session`/`disconnect_session` (admin), `usage_summary`.
- `Product`: `product_id, name, price, was_price, unit_price (string), size, available,
  on_special, url, image_url`. No numeric unit price yet (the rest of CW-11);
  aus_cartwatch parses `unit_price`.
- Gateway limits: 1.5 s spacing + 0.5 s jitter per retailer, 2000 upstream
  requests/day per retailer, a 30-minute breaker when the data API refuses (a refused photo
  does not trip it), product/search cache 6 h, cart cache 30 s. An anonymous connection
  idle for 30 minutes starts afresh. Every tool call is metered per tenant.

## Traffic budget (design rule)

The budget is part of the design, not a tuning knob, and it is enforced in code
(CW-25). On top of aus-cart-mcp's gateway:

- **Batched refresh:** one upstream request per 20 tracked items, through aus-cart-mcp's
  `get_products` (CW-10, live since 2026-10-03). If the batch tool misbehaves, the run falls
  back to per-item search. A block always aborts the run.
- **Cadence:** one refresh a day at 06:30 local, shifted randomly by up to 20 minutes.
  This was reduced from two a day plus a Wednesday run on 2026-10-03, to keep traffic low.
  The weekly specials change on Wednesday before the morning run, so that run sees them.
  The searches it makes are cached by aus-cart-mcp for 6 hours, so a second run soon
  after would only re-read the cache. An extra weekly run (`specials_day`) and more
  daily times can be set in Settings.
- **Randomness is for spreading load, not disguise:** the jittered run times and
  aus-cart-mcp's request spacing are politeness. Nothing tries to make automated traffic
  look human (docs/LEGAL.md).
- **Daily cap:** `AUS_CARTWATCH_DAILY_UPSTREAM_CAP`, default **60** upstream requests.
  A call that would exceed it is not started.
- **Shared-tenant guard:** `usage_summary` is checked before each run; the run is
  skipped when the tenant is past **50%** of the gateway's daily cap.
- **Blocks:** a breaker or blocked response skips the run; it is never retried, and
  the next run of the same family backs off 2 hours. Price runs (refresh, photos) and cart
  runs (snapshots) back off **separately**: they use different tenants. A stale owner
  session being refused must not stop anonymous price checks. That happened on
  2026-10-04: the morning refresh was skipped after the overnight snapshots were refused.
  Three consecutive failed refreshes page the operator.
- **Cart snapshots:** on demand from the Cart page by default (`scheduled_snapshots` = 0).
  While the owner's login lasts an hour (WI-800), a scheduled read almost always meets an
  expired session, gets refused, and trips aus-cart-mcp's breaker for everyone. With the
  setting on: at most hourly while the cart is non-empty, every 6 hours otherwise.

## Counting upstream requests

aus-cart-mcp 0.2 does not say how many upstream requests a tool call made. The
client counts 1 for every call that can reach the retailer and 0 for
`list_retailers` and `usage_summary`. That under-counts: a fresh connection's
warm-up makes a first guest search cost 2. So each scheduled run reads
`usage_summary` before and after and writes the difference into the ledger as a
`reconcile` row. If myaiagent uses the shared tenant during a run, its traffic is
counted against aus_cartwatch too, which errs on the safe side.

## Two aus-cart-mcp tenants

- **Prices** (`AUS_CARTWATCH_AUS_CART_PRICES_KEY`, tenant `aus-cartwatch-prices`) has never
  signed in. Searches, batch lookups, daily refreshes, receipt matching and photos go
  through it. aus-cart-mcp starts a fresh anonymous connection when it has been idle for
  30 minutes, like a new visitor.
- **Cart** (`AUS_CARTWATCH_AUS_CART_KEY`, tenant `myaiagent-owner`) is used only for reading
  and filling the owner's cart.
- **Why** (measured 2026-10-03): the owner's Woolworths login token lasts 60 minutes (WI-800).
  Requests made with the stale stored session, searches included, were refused with HTTP 403
  by Woolworths' bot protection. A fresh anonymous connection from node b searched fine.
  aus-cart-mcp may reuse a session the owner hands it, but it cannot keep the bot-protection
  cookies valid without imitating a browser, which docs/LEGAL.md rules out. So price tracking
  must not depend on that session.

## Product cache and photos

- **Every product seen is cached.** Search results, lookups and refresh observations all
  update the product's latest details: name, size, price, was-price, special flag, unit
  price, link and photo URL.
- **Searches are cached.** A search is remembered by its normalised text.
- **What uses the cache:** for `AUS_CARTWATCH_PRODUCT_CACHE_HOURS` (default 24), repeating
  a search, tracking from its results (one or many at once) and tracking by id are answered
  locally, with no Woolworths request. The first observation of an item tracked from the
  cache is dated when it was actually seen.
- **What doesn't:** scheduled refreshes always ask Woolworths, because they exist to get
  fresh prices.
- **Photos are shown by the owner's browser** from Woolworths' image server
  (`cdn0.woolworths.media`), using the image URL cached with each product, as on
  woolworths.com.au. The browser keeps its own copy.
- **Server-side photo storage** (`AUS_CARTWATCH_FETCH_PHOTOS=1`) fetches a tracked item's
  photo once, through aus-cart-mcp's `get_product_image`, stores it in `product_images` and
  serves it from `/images/<retailer>/<id>`. Missing photos are filled in after each refresh
  slot by a separate `photos` run (at most 5 a run, within the daily cap). It is **off by
  default**. On 2026-10-03 the image server answered 403 to node b, and aus-cart-mcp (before
  `ad443c5`) treated that as a block, pausing all Woolworths traffic for 30 minutes. A refused
  photo is now just "no photo".

## Cart page

`/cart` shows the latest cart snapshot. It is read on demand with *Refresh from Woolworths*,
which needs the owner to have reconnected in the myaiagent app within the last hour (WI-800).
Scheduled reads are off by default (§ Traffic budget). Each line shows its photo, quantity,
price and whether it is tracked. Ticked lines, or all untracked lines, are tracked from the
snapshot itself, with no retailer call. Checkout stays on the Woolworths site.

## Receipt scans (CW-40, 2026-10-05)

Upload a photo on `/receipts` (the phone camera opens directly).

1. **Store.** The photo is stored once under `/data/receipts/<sha256>` (mode 600). A
   re-upload of the same photo reopens the existing receipt.
2. **Read.** The photo is read by `AUS_CARTWATCH_OCR_MODEL` (default `google/gemini-2.5-flash-lite`)
   through OpenRouter, with a strict-JSON prompt. This is capped at `AUS_CARTWATCH_OCR_DAILY_CAP`
   readings a day (default 10).
   - *Why this model (owner decision 2026-10-05):* on the 3 Oct receipt it got 30 of 30 lines
     exactly right (names, quantities, unit prices, totals, the 8 weighed lines) in 8 s, for
     about $0.001.
   - Qwen3-VL-32B swapped day and month. Both models invented quantities ("2 × $2.35" for a 2 L
     milk) until the prompt said quantity is 1 unless a `Qty` or `kg NET` line is printed.
   - Weighed is therefore derived from a fractional quantity, not taken from the model.
   - Digit runs of 9 or more are masked before storing (card, member and approval numbers).
3. **Match.** Lines are matched in the background through the prices tenant, using the rules
   below. A remembered alias is free and a search seen in the last 24 h is cached; new searches
   are 2.5 s apart, at most 25 per pass, within the daily budget. Weighed produce is skipped.
4. **Confirm.** The owner ticks lines (good and alias matches are pre-ticked) and saves:
   - the products are tracked (`source=receipt`);
   - each choice becomes an alias;
   - the prices paid are recorded as observations (`source=receipt`, dated at purchase);
   - the receipt becomes a shop episode for Suggestions.

Not done yet: PDFs, and upload from Telegram (deferred along with the bot).

## Receipt matching (learned 2026-10-03, for CW-40)

A receipt from 3 Oct (32 lines) was matched by hand through the prices tenant:

- 22 searches, no refusals, 20 exact matches at the price paid.
- The 8 loose produce lines priced per kg were skipped.
- The misses show what the importer must do:
  - **Match on name and size first, then use price as a sanity check.** Price alone
    picked strawberries for blueberries.
  - **Reject marketplace listings** (10-digit ids). "Blueberry 170g" matched a soy candle.
  - **Variable-weight packs** ("chicken breast bulk") match a weight range, not a price.
  - **An unavailable product has no price.** Show it as "check" rather than rank it last.
  - **Expand POS abbreviations before searching:** `WW` is Woolworths, `Hny & Cin` is
    honey & cinnamon, `Sprd` is spread, and `P/P` is dropped.

## Price analytics

Pinned by `tests/unit/test_analytics.py`:

- **Baseline** = the retailer's `was_price` when present and above the price.
  Otherwise, with at least 3 observations, the median of non-special prices
  over the last 60 days, or the highest observed price when every recent
  observation was on special. Fewer than 3 observations and no `was_price` →
  no baseline: no discount alert (an on-special alert can still fire).
- **Discount** = 1 − price / baseline, never negative. **Heavy** when ≥ the
  item's threshold (default 0.30, per item override).
- **Sale episode** starts when the item is on special or ≥10% below baseline,
  and ends when neither holds. It keeps the low price, baseline and max discount.
- **Stats block** (materialised in `item_stats` after every observation): price,
  baseline, discount, heavy, 7/30/90-day min/max/median, last sale, average days
  between sales, typical sale price, change since the last observation, unit
  price, target met.

Until aus-cart-mcp exposes `was_price` (CW-11), baselines come from history, so
an item needs three observations (about a day and a half) before its first
discount alert.

## Alerts

After each refresh: `on_sale` (one per episode, in the daily digest at 07:30),
`heavy_discount` (immediate, once per episode, supersedes an unsent `on_sale`),
`target_price` (immediate, once per episode or per target value) and `operator`
(failed-run streak, expired session, a day without a refresh). Quiet hours
21:00–07:00 hold everything but `operator`. Alerts are stored before sending and
retried on the next pass if every notifier fails. Snoozed items raise nothing.

**Telegram is deferred (owner decision 2026-10-05).** With no notifier configured, alerts
stay queued and are shown on the UI's Alerts page (with Add to cart and Snooze buttons) and
by the MCP tool `recent_alerts`. When a bot is added (`scripts/enable-telegram.sh`), the
queued alerts are delivered on the next pass.

Telegram is long-polled for button presses (the service is tailnet-only, so no
webhook). Only allow-listed chat ids are acted on.

## Web UI

Server-rendered Jinja2 with plain HTML forms and inline SVG charts. The item
specified Jinja2 + htmx; plain forms (POST, redirect) do everything MVP1 needs
without shipping a script, so htmx is left out. The UI is protected by one
household password (scrypt hash in `AUS_CARTWATCH_UI_PASSWORD_HASH`) and an
HMAC-signed, `SameSite=Strict`, `HttpOnly` cookie.

## Storage

Plain `sqlite3` on one connection (WAL, foreign keys on) rather than
`aiosqlite`, matching aus-cart-mcp. One household writes a few thousand rows a
week and every query is sub-millisecond. Retention (`db compact`): observations
2 years, cart snapshots 90 days, ledger 400 days.

## Out of scope

Checkout, payment, delivery slots, addresses; any bot-protection evasion;
selling or multi-household hosting; Coles or any second retailer before MVP3;
public exposure of the UI.
