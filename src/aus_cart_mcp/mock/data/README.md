# Mock data

Responses recorded from each retailer's public endpoints, trimmed to the fields
the adapters read. `*_guest.json`, `search_*.json` and `cart_empty.json` were
recorded anonymously (no account) on the date in docs/RETAILERS.md. Logged-in
shapes (`bootstrap_logged_in.json`, `update_ok.json`) are hand-written from
observed structure and hold no personal data.

`products_by_stockcode.json` was recorded on 2026-10-03 from `GET /apis/ui/products/888140,6073909`
through the owner's tenant and trimmed to public catalogue fields: no `HasBeenBoughtBefore`,
`IsInTrolley`, ad or diagnostics fields.
