from aus_cartwatch import config

ALL = [
    "DB", "RETAILER", "AUS_CART_URL", "AUS_CART_KEY", "DAILY_UPSTREAM_CAP", "GATEWAY_DAILY_CAP",
    "SHARED_CAP_FRACTION", "TZ", "SCHEDULER", "MCP_KEYS", "MCP_KEY", "UI_PASSWORD_HASH", "SECRET",
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_IDS", "WEBHOOK_URL", "IMMEDIATE_ON_SALE", "ENABLE_AUTO_ADD",
    "MAX_ADD_QUANTITY", "PUBLIC_URL", "HOST",
]  # fmt: skip


def test_defaults(monkeypatch):
    for name in ALL:
        monkeypatch.delenv(f"AUS_CARTWATCH_{name}", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert config.db_path() == "/data/aus-cartwatch.db"
    assert config.retailer() == "woolworths"
    assert config.aus_cart_url() == "http://grocery-mcp:8080/mcp"
    assert config.aus_cart_key() == ""
    assert config.daily_upstream_cap() == 60
    assert config.gateway_daily_cap() == 2000
    assert config.shared_cap_fraction() == 0.5
    assert config.timezone() == "Australia/Sydney"
    assert config.scheduler_enabled() is True
    assert config.mcp_key_hashes() == set()
    assert config.ui_password_hash() == "" and config.secret() == ""
    assert config.telegram_token() == "" and config.telegram_chat_ids() == []
    assert config.webhook_url() == ""
    assert config.immediate_on_sale() is False and config.auto_add_enabled() is False
    assert config.max_add_quantity() == 6
    assert config.public_url() == ""
    assert config.port() == 8080
    assert config.host() == "0.0.0.0"


def test_environment_overrides(monkeypatch):
    values = {
        "DB": "/tmp/x.db",
        "RETAILER": "coles",
        "AUS_CART_URL": "http://127.0.0.1:9/mcp",
        "AUS_CART_KEY": "k",
        "DAILY_UPSTREAM_CAP": "12",
        "GATEWAY_DAILY_CAP": "100",
        "SHARED_CAP_FRACTION": "0.25",
        "TZ": "UTC",
        "SCHEDULER": "off",
        "MCP_KEYS": "ABC, def",
        "MCP_KEY": "plain",
        "TELEGRAM_CHAT_IDS": "1, -2",
        "IMMEDIATE_ON_SALE": "yes",
        "ENABLE_AUTO_ADD": "1",
        "MAX_ADD_QUANTITY": "3",
        "PUBLIC_URL": "https://x.example/",
        "HOST": "127.0.0.1",
    }
    for name, value in values.items():
        monkeypatch.setenv(f"AUS_CARTWATCH_{name}", value)
    monkeypatch.setenv("PORT", "9000")
    assert config.db_path() == "/tmp/x.db"
    assert config.retailer() == "coles"
    assert config.aus_cart_url() == "http://127.0.0.1:9/mcp"
    assert config.aus_cart_key() == "k"
    assert config.daily_upstream_cap() == 12 and config.gateway_daily_cap() == 100
    assert config.shared_cap_fraction() == 0.25
    assert config.timezone() == "UTC"
    assert config.scheduler_enabled() is False
    assert config.mcp_key_hashes() == {"abc", "def", config.hash_key("plain")}
    assert config.telegram_chat_ids() == [1, -2]
    assert config.immediate_on_sale() and config.auto_add_enabled()
    assert config.max_add_quantity() == 3
    assert config.public_url() == "https://x.example"
    assert config.host() == "127.0.0.1"
    assert config.port() == 9000
