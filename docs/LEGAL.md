# Legal position: read before using or redistributing

aus-cart-mcp is **unofficial**. It isn't affiliated with, endorsed by or supported
by Woolworths or any other retailer. All trademarks belong to their owners.

## What it does

The retailers it supports publish no public API for a customer's cart. This
project calls the same endpoints a retailer's own website calls, using the
customer's own signed-in session.

## Retailer terms

Retailer website terms commonly prohibit automated access. Woolworths' terms,
for example, prohibit "any robot, spider, site search and retrieval application
or other mechanism to retrieve or index any portion of the Site", and reverse
engineering.

- **Using it on your own account** is your decision and your risk. The retailer
  may suspend accounts it believes are automated.
- **Offering it to others, particularly for money,** is a different and larger risk:
  - **Contract and IP:** a retailer may treat it as inducing breach of its terms,
    or object to the use of its trademarks.
  - **Privacy:** you would be holding other people's retailer sessions, which
    brings privacy-law obligations (in Australia, the Privacy Act and APPs),
    security duties, and liability for mistakes such as a mis-filled cart.
  - **Scale:** many customers' traffic from one service is exactly what bot
    protection is built to stop.

The durable route for a product is a **partner agreement** with each retailer.
Take legal advice before charging anyone.

## How it behaves

- **No evasion of bot protection:** no proxy rotation, no fingerprint spoofing,
  and no CAPTCHA or challenge solving. The server sends an ordinary browser user
  agent because the sites serve their data API only to browsers. When a site
  blocks us, every customer pauses for 30 minutes.
- Requests are serialised and spaced per retailer, with a daily cap.
- Customer sessions are encrypted at rest, and API keys are stored only as hashes.
- Every tool call is metered per customer.

## Scheduled traffic (the watch layer)

The `watch` layer, off unless `AUS_CART_MCP_FEATURES` names it, originates
scheduled requests of its own: price refreshes and alert checks, not only
requests a person just asked for. That traffic is budgeted per tenant and per
retailer, and it stops when a retailer blocks us. It is the customer's own
data being re-read on a schedule they turned on, and it carries the same
obligations as any other request this service makes.

## No warranty

This software is provided as is, under the [AGPL-3.0](../LICENSE), with no
warranty. Prices, availability and cart contents come from the retailer and may
be wrong or out of date. Always review your cart before checking out.
