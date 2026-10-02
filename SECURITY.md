# Security

## Reporting

Please report vulnerabilities privately via GitHub's **Report a vulnerability**
(Security tab), not in public issues. Expect an acknowledgement within a week.

## Model

- **Customer API keys**
  - Random 256-bit, shown once, stored only as SHA-256 hashes.
  - Can be rotated or disabled.
  - Every `/mcp` request needs one, and tools act only for that key's customer.
- **Retailer sessions**
  - Cookies captured after the customer signs in on the retailer's own site.
  - Fernet-encrypted at rest with a key derived from `AUS_CART_MCP_SECRET`, which
    belongs in a secret manager, never on the data volume.
  - Rotating the secret makes stored sessions unreadable, so customers reconnect.
- **Sign-in:** today customers sign in on the retailer's own site, so the server doesn't handle retailer passwords.
- **Scope:** tools act only on the customer's own retailer account, through the session they connected.
- **Admin tools:** `connect_session` and `disconnect_session` take session
  material. MCP clients must hide them from models and must not log their arguments.
- **Network:** run it behind TLS. It's designed to sit on a private network next to
  its client, or behind a reverse proxy.
