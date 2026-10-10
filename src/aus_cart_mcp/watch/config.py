"""Runtime settings from the environment. Every name is ``AUS_CARTWATCH_*`` except ``PORT``.

Deployment facts and secrets live here. Household preferences that the owner
changes at runtime (cadence, thresholds, quiet hours) live in the database; see
`aus_cartwatch.policy`.
"""

from __future__ import annotations

import hashlib
import os

DEFAULT_DB = "/data/aus-cartwatch.db"
DEFAULT_AUS_CART_URL = "http://grocery-mcp:8080/mcp"
DEFAULT_DAILY_UPSTREAM_CAP = 60
DEFAULT_GATEWAY_DAILY_CAP = 2000  # aus-cart-mcp's gateway.Limits.daily_cap
DEFAULT_SHARED_CAP_FRACTION = 0.5
DEFAULT_TZ = "Australia/Sydney"
DEFAULT_MAX_ADD_QUANTITY = 6


def env(name: str, default: str = "") -> str:
    value = os.environ.get(f"AUS_CARTWATCH_{name}")
    return default if value is None else value


def flag(name: str, default: bool = False) -> bool:
    value = env(name)
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def db_path() -> str:
    return env("DB", DEFAULT_DB)


def retailer() -> str:
    """The retailer this household tracks (MVP1: Woolworths only)."""
    return env("RETAILER", "woolworths")


def aus_cart_url() -> str:
    """The aus-cart-mcp Streamable HTTP endpoint; the only path to a retailer."""
    return env("AUS_CART_URL", DEFAULT_AUS_CART_URL)


def aus_cart_key() -> str:
    """The aus-cart-mcp tenant API key. A secret: never log it."""
    return env("AUS_CART_KEY")


def aus_cart_prices_key() -> str:
    """The key of a never-signed-in aus-cart-mcp tenant for prices and search (falls back to the cart key).

    Prices need no login, and a stale stored session gets refused by Woolworths' bot protection, so price
    traffic goes through an anonymous tenant and only cart actions use the owner's signed-in tenant."""
    return env("AUS_CART_PRICES_KEY") or aus_cart_key()


def daily_upstream_cap() -> int:
    """aus_cartwatch's own ceiling on upstream retailer requests per retailer per day (enforced by the scheduler)."""
    return int(env("DAILY_UPSTREAM_CAP", str(DEFAULT_DAILY_UPSTREAM_CAP)))


def gateway_daily_cap() -> int:
    return int(env("GATEWAY_DAILY_CAP", str(DEFAULT_GATEWAY_DAILY_CAP)))


def shared_cap_fraction() -> float:
    """Skip a run when the shared aus-cart-mcp tenant has used more than this fraction of the gateway cap today."""
    return float(env("SHARED_CAP_FRACTION", str(DEFAULT_SHARED_CAP_FRACTION)))


def timezone() -> str:
    return env("TZ", DEFAULT_TZ)


def scheduler_enabled() -> bool:
    return flag("SCHEDULER", True)


def mcp_key_hashes() -> set[str]:
    """SHA-256 hex digests of the keys allowed on aus_cartwatch's own `/mcp`.

    `AUS_CARTWATCH_MCP_KEYS` holds digests (comma-separated); `AUS_CARTWATCH_MCP_KEY` holds one plain key,
    hashed here. With neither set, `/mcp` refuses every request.
    """
    hashes = {h.lower() for h in _csv(env("MCP_KEYS"))}
    plain = env("MCP_KEY")
    if plain:
        hashes.add(hash_key(plain))
    return hashes


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def ui_password_hash() -> str:
    return env("UI_PASSWORD_HASH")


def secret() -> str:
    """Signs the web UI session cookie."""
    return env("SECRET")


def telegram_token() -> str:
    return env("TELEGRAM_TOKEN")


def telegram_chat_ids() -> list[int]:
    return [int(x) for x in _csv(env("TELEGRAM_CHAT_IDS"))]


def webhook_url() -> str:
    return env("WEBHOOK_URL")


def immediate_on_sale() -> bool:
    return flag("IMMEDIATE_ON_SALE")


def auto_add_enabled() -> bool:
    return flag("ENABLE_AUTO_ADD")


def max_add_quantity() -> int:
    return int(env("MAX_ADD_QUANTITY", str(DEFAULT_MAX_ADD_QUANTITY)))


def public_url() -> str:
    """Where the web UI is reached (for links in alerts), e.g. https://aus-cartwatch.sprooty.com."""
    return env("PUBLIC_URL").rstrip("/")


def product_cache_hours() -> float:
    """How long a seen product or a search result is reused before Woolworths is asked again."""
    return float(env("PRODUCT_CACHE_HOURS", "24"))


def fetch_photos() -> bool:
    """Fetch and store photos server-side through aus-cart-mcp. Off by default: Woolworths' image CDN
    refused node b (2026-10-03), so the UI shows photos from the CDN in the owner's browser instead."""
    return flag("FETCH_PHOTOS", False)


def openrouter_key() -> str:
    """OpenRouter API key for receipt OCR (CW-40). A secret: never log it."""
    return env("OPENROUTER_KEY")


def openrouter_url() -> str:
    return env("OPENROUTER_URL", "https://openrouter.ai/api/v1").rstrip("/")


def ocr_model() -> str:
    """Vision model for receipts. Chosen 2026-10-05: 30/30 lines exact on a real receipt for about $0.001."""
    return env("OCR_MODEL", "google/gemini-2.5-flash-lite")


def ocr_daily_cap() -> int:
    return int(env("OCR_DAILY_CAP", "10"))


def receipts_dir() -> str:
    return env("RECEIPTS_DIR", "/data/receipts")


def backup_dir() -> str:
    """Where the nightly SQLite backup goes (empty: no scheduled backup)."""
    return env("BACKUP_DIR")


def backup_keep() -> int:
    return int(env("BACKUP_KEEP", "14"))


def port() -> int:
    return int(os.environ.get("PORT", "8080"))


def host() -> str:
    return env("HOST", "0.0.0.0")
