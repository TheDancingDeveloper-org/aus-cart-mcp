# Operations

Production runs on node b (winrarhost) as the Komodo stack **`prod-aus-cartwatch`**,
from `indexarr/ops` `prod/aus-cartwatch/docker-compose.yml`. The compose header
is the authoritative wiring; this page is the runbook.

## Deploy

A push to `main` runs CI. The `image` job publishes
`ghcr.io/thedancingdeveloper-org/aus-cartwatch:<sha>`, and the `deploy` job
fetches the ops repo's `scripts/komodo-deploy.sh`. That script rewrites the
image tag in `prod/aus-cartwatch/docker-compose.yml`, pushes the change and
calls Komodo `DeployStack`.

Repo settings, with the same sources as aus-cart-mcp:

- **Variables:** `DEPLOY_ENABLED=true`, `DEPLOY_SCRIPT_URL` (raw `komodo-deploy.sh`),
  `DEPLOY_STACK_NAME=prod-aus-cartwatch`, `DEPLOY_STACK_DIR=prod/aus-cartwatch`.
- **Secrets:** `DEPLOY_GIT_TOKEN`, `DEPLOY_GIT_ACCOUNT` (Forgejo, for the ops repo),
  `DEPLOY_API_KEY`, `DEPLOY_API_SECRET` (Komodo).

## Secrets

All secrets live in Infisical `apps/prod` and reach the stack as
`[[infisical://…]]` refs in the Komodo environment (Komodo caches them for 300 s).

| Variable | Infisical | Notes |
|---|---|---|
| `AUS_CART_KEY` | `HOMELAB_AUS_CARTWATCH_AUS_CART_KEY` | aus-cart-mcp key of tenant `myaiagent-owner` (WI-747). Rotating it in aus-cart-mcp breaks myaiagent too |
| `AUS_CART_PRICES_KEY` | `HOMELAB_AUS_CARTWATCH_PRICES_KEY` | aus-cart-mcp key of tenant `aus-cartwatch-prices` (never signed in): prices and search |
| `OPENROUTER_KEY` | `OpenRouter_Token` | receipt OCR (shared with myaiagent's OpenRouter use) |
| `MCP_KEY` | `HOMELAB_AUS_CARTWATCH_MCP_KEY` | bearer key for `/mcp` |
| `UI_PASSWORD_HASH` | `HOMELAB_AUS_CARTWATCH_UI_PASSWORD_HASH` | `scrypt:` hash; the password itself is `HOMELAB_AUS_CARTWATCH_UI_PASSWORD` |
| `SECRET` | `HOMELAB_AUS_CARTWATCH_SECRET` | signs the UI cookie |
| `TELEGRAM_TOKEN` | `HOMELAB_AUS_CARTWATCH_TELEGRAM_TOKEN` | the dedicated alert bot (WI-747) |
| `TELEGRAM_CHAT_IDS` | `MYIDENTITY_TELEGRAM_OWNER_USER_ID` | the owner's chat |

To rotate a minted secret, generate a new value straight into Infisical
(never through a transcript), wait 5 minutes, and redeploy the stack.

## Exposure

The app listens on `127.0.0.1:8126` on node b. The host Caddy
(`/mnt/2tnvme/docker/volumes/caddyv2/conf/Caddyfile`, hand-managed) serves
`aus-cartwatch.sprooty.com` with Cloudflare DNS-01 TLS and `import secure_route`.
The Cloudflare record is DNS-only and points at `192.168.1.75`, so the name works
only on the LAN and over the tailnet subnet route.

## Backups and restore

At 03:30 Sydney time the app writes an online, integrity-checked SQLite backup
to `/mnt/4tnvme/backups/aus-cartwatch` (container `/backups`) and keeps the
newest 14. If a backup fails, an operator alert fires. For a manual backup, run
`docker exec prod-aus-cartwatch-aus-cartwatch-1 python -m aus_cartwatch db backup`.

To restore:

1. Stop the stack in Komodo.
2. Copy the chosen backup to
   `/mnt/2tnvme/docker/volumes/aus-cartwatch/data/aus-cartwatch.db`, owned by
   `10001:10001` with mode `600`.
3. Delete `aus-cartwatch.db-wal` and `aus-cartwatch.db-shm` if present.
4. Start the stack and check `/healthz`.

To rehearse a restore without touching prod:

```sh
docker run --rm -v /mnt/4tnvme/backups/aus-cartwatch:/b:ro ghcr.io/thedancingdeveloper-org/aus-cartwatch:latest \
  sh -c 'cp /b/$(ls /b | tail -1) /tmp/r.db && AUS_CARTWATCH_DB=/tmp/r.db python -m aus_cartwatch db stats'
```

## Woolworths session (cart features)

The owner's login lasts 60 minutes (WI-800). Before using the Cart page or add to cart:

1. Reconnect Woolworths in the myaiagent app (More → MCP servers → grocery → Retailer accounts).
2. Within the hour, open `/cart` and press *Refresh from Woolworths*.

Price tracking does not depend on it: prices go through the anonymous `aus-cartwatch-prices`
tenant. If aus-cart-mcp is paused after a refusal ("paused for about N more minutes"), wait.
A restart of the `aus-cart-mcp` service in `prod-myaiagent` clears the pause, so use it only
to test a freshly connected session, never to push past a refusal.

## Open operator actions

- **Telegram alert bot:**
  1. Create the bot with @BotFather.
  2. Store its token as `apps/prod/HOMELAB_AUS_CARTWATCH_TELEGRAM_TOKEN`.
  3. Open the bot in Telegram and press **Start**: a bot can't message someone who hasn't
     started a chat with it.
  4. Run `scripts/enable-telegram.sh`. It checks the secret exists, adds the
     `TELEGRAM_TOKEN` ref to the Komodo env (Komodo fails a deploy on a missing ref, even a
     commented one), redeploys, and runs `python -m aus_cartwatch telegram check`, which
     reports the bot identity and chat reachability without sending a message.
  5. `telegram test` sends one test message. Alerts already pending (2026-10-05: three
     heavy-discount alerts) are delivered on the next pass.
- **WI-800:** a HAR recording from the owner's browser around the 60-minute mark, to find the
  site's token-refresh call.

## Health

- `/healthz` reports the last refresh, today's upstream count against the cap,
  and any backoff.
- `/metrics` exposes Prometheus gauges and counters (`aus_cartwatch_*`).
- The UI's Settings → Runs page shows every run and the budget ledger.
