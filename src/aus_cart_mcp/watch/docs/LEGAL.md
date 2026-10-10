# Legal position

aus_cartwatch reaches Woolworths only through aus-cart-mcp, so aus-cart-mcp's
legal position applies in full. Read it:
[aus-cart-mcp docs/LEGAL.md](https://github.com/TheDancingDeveloper-org/aus-cart-mcp/blob/main/docs/LEGAL.md).

In short, for this project:

- **Personal use, on the owner's own account.** It is not offered to anyone
  else, hosted for other households, or sold.
- **No evasion.** No proxy rotation, no fingerprint spoofing, no CAPTCHA or
  challenge solving. When aus-cart-mcp reports a block, aus_cartwatch stops and
  waits; it does not retry around it.
- **Polling adds steady traffic,** so the traffic budget in
  [DESIGN.md](DESIGN.md#traffic-budget-design-rule) is a requirement, enforced in code.
- **No checkout.** It adds items to the cart; a person reviews and pays on the
  retailer's site.
- Product photos: a tracked item's photo is fetched once through aus-cart-mcp and kept;
  search-result photos are loaded by the owner's own browser from Woolworths' image server,
  as the Woolworths site itself does.
- Receipt photos (CW-40) are stored only on node b (mode 600) and sent only to the OCR model
  through OpenRouter. Card, member and approval numbers are never stored in the extracted data.
- Prices and availability come from the retailer and may be wrong or out of date.

aus_cartwatch is unofficial and not affiliated with or endorsed by Woolworths.
It talks to aus-cart-mcp (AGPL-3.0) over the network and does not include its
code in the shipped image, so it stays private (see WI-746). aus-cart-mcp is a
development-only dependency, used by the test suite.
