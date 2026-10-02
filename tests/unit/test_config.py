from aus_cart_mcp import config


def test_new_names_win_and_old_names_still_work(monkeypatch):
    monkeypatch.delenv("AUS_CART_MCP_SECRET", raising=False)
    monkeypatch.setenv("GROCERY_MCP_SECRET", "old")
    assert config.secret() == "old"
    monkeypatch.setenv("AUS_CART_MCP_SECRET", "new")
    assert config.secret() == "new"


def test_defaults_and_overrides(monkeypatch):
    for name in ("AUS_CART_MCP_DB", "GROCERY_MCP_DB", "AUS_CART_MCP_WOOLWORTHS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert config.db_path() == config.DEFAULT_DB
    assert config.base_url_override("woolworths") is None
    monkeypatch.setenv("AUS_CART_MCP_WOOLWORTHS_BASE_URL", "http://127.0.0.1:8090")
    assert config.base_url_override("woolworths") == "http://127.0.0.1:8090"
