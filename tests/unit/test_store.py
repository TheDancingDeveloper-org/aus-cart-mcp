import sqlite3

import pytest

from aus_cart_mcp.store import KEY_PREFIX, Store, hash_key


async def test_keys_are_hashed_and_resolve(store, tmp_path):
    tenant, key = store.create_tenant_sync("alice")
    assert key.startswith(KEY_PREFIX)
    assert (await store.tenant_for_key(key)).id == tenant.id
    raw = sqlite3.connect(tmp_path / "aus-cart.db").execute("SELECT key_hash FROM tenants").fetchone()[0]
    assert raw == hash_key(key) and key not in raw


async def test_unknown_malformed_and_disabled_keys_are_rejected(store):
    _, key = store.create_tenant_sync("alice")
    assert await store.tenant_for_key("acm_nope") is None
    assert await store.tenant_for_key("Bearer x") is None
    assert await store.tenant_for_key("") is None
    store.set_disabled_sync("alice", True)
    assert await store.tenant_for_key(key) is None
    store.set_disabled_sync("alice", False)
    assert await store.tenant_for_key(key) is not None


async def test_rotation_invalidates_the_old_key(store):
    _, old = store.create_tenant_sync("alice")
    new = store.rotate_key_sync("alice")
    assert await store.tenant_for_key(old) is None
    assert (await store.tenant_for_key(new)).name == "alice"
    with pytest.raises(KeyError):
        store.rotate_key_sync("nobody")


async def test_legacy_prefix_keys_still_work(store):
    """Keys issued before the rename (gmcp_…) keep working."""
    tenant, _ = store.create_tenant_sync("legacy")
    legacy = "gmcp_legacy-key-value"
    with store._connect() as db:
        db.execute("UPDATE tenants SET key_hash = ? WHERE id = ?", (hash_key(legacy), tenant.id))
    assert (await store.tenant_for_key(legacy)).id == tenant.id


async def test_sessions_are_encrypted_and_unreadable_with_another_secret(store, tmp_path):
    tenant, _ = store.create_tenant_sync("alice")
    await store.put_session(tenant.id, "woolworths", {"cookies": {"session": "super-secret"}}, new=True)
    raw = sqlite3.connect(tmp_path / "aus-cart.db").execute("SELECT data FROM sessions").fetchone()[0]
    assert b"super-secret" not in raw
    assert (await store.get_session(tenant.id, "woolworths"))["cookies"]["session"] == "super-secret"
    other = Store(tmp_path / "aus-cart.db", "a-different-secret")
    assert await other.get_session(tenant.id, "woolworths") is None


def test_refuses_to_start_without_a_secret(tmp_path):
    with pytest.raises(RuntimeError, match="SECRET"):
        Store(tmp_path / "x.db", "")


async def test_usage_summary_counts_per_tool_and_window(store):
    tenant, _ = store.create_tenant_sync("alice")
    await store.record_usage(tenant.id, "search_products", "woolworths", True, 2)
    await store.record_usage(tenant.id, "search_products", "woolworths", False, 1)
    await store.record_usage(tenant.id, "get_cart", "woolworths", True, 1)
    with store._connect() as db:
        db.execute(
            "INSERT INTO usage (tenant_id, tool, retailer, ok, upstream_requests, at) "
            "VALUES (?, 'old', '', 1, 9, '2000-01-01T00:00:00+00:00')",
            (tenant.id,),
        )
    summary = await store.usage_summary(tenant.id, 30)
    assert summary["calls"] == 3 and summary["upstream_requests"] == 4
    assert summary["by_tool"]["search_products"] == {"calls": 2, "succeeded": 1, "upstream_requests": 3}
