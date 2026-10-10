import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from aus_cart_mcp.watch.store import Migration, Store, _statements, bundled_migrations

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def test_bundled_migrations_are_ordered_and_start_at_one():
    migrations = bundled_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "0001_init.sql"


def test_migrate_fresh_file_then_noop(tmp_path):
    store = Store(tmp_path / "fresh.db")
    assert store.schema_version() == 0
    applied = store.migrate()
    assert applied == [m.name for m in bundled_migrations()]
    assert store.schema_version() == len(bundled_migrations())
    assert store.migrate() == []
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    store.close()


def test_migrations_apply_on_a_populated_copy(tmp_path):
    first = bundled_migrations()[:1]
    old = Store(tmp_path / "old.db", first)
    old.migrate()
    old.set_setting("cadence", "2")
    old.close()
    upgraded = Store(tmp_path / "old.db")
    assert upgraded.migrate() == [m.name for m in bundled_migrations()[1:]]
    assert upgraded.get_setting("cadence") == "2"
    upgraded.close()


def test_migrate_applies_only_new_versions(tmp_path):
    first = Migration(1, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    second = Migration(2, "0002_b.sql", "-- comment\nCREATE TABLE b (y INTEGER);\nINSERT INTO b VALUES (1);")
    Store(tmp_path / "db", [first]).migrate()
    assert Store(tmp_path / "db", [first, second]).migrate() == ["0002_b.sql"]
    assert Store(tmp_path / "db", [first, second]).schema_version() == 2


def test_failed_migration_rolls_back(tmp_path):
    bad = Migration(1, "0001_bad.sql", "CREATE TABLE ok (x INTEGER); CREATE TABLE ok (x INTEGER);")
    store = Store(tmp_path / "db", [bad])
    with pytest.raises(sqlite3.OperationalError):
        store.migrate()
    names = {row["name"] for row in store._all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "ok" not in names
    assert store.schema_version() == 0


def test_statement_splitting_skips_comments_and_blanks():
    assert _statements("-- only a comment\n;\nSELECT 1;\n\n") == ["SELECT 1"]


def test_settings_roundtrip(store):
    assert store.get_setting("x") is None
    assert store.get_setting("x", "d") == "d"
    store.set_setting("x", "1")
    store.set_setting("x", "2")
    assert store.settings() == {"x": "2"}


def test_products_upsert_keeps_known_fields(store):
    store.upsert_product("woolworths", "888140", name="Milk 2L", size="2L", url="u", now=T0)
    store.upsert_product("woolworths", "888140", now=T0 + timedelta(days=1))
    row = store.get_product("woolworths", "888140")
    assert row["name"] == "Milk 2L" and row["size"] == "2L"
    assert row["first_seen"] < row["last_seen"]


def test_track_is_idempotent_and_unarchives(store):
    store.upsert_product("woolworths", "1", name="Bread")
    item, created = store.track("woolworths", "1", target_price=3.0)
    assert created and item["name"] == "Bread" and item["source"] == "manual"
    again, created = store.track("woolworths", "1", source="cart", threshold=0.4)
    assert not created and again["id"] == item["id"]
    assert again["target_price"] == 3.0 and again["discount_threshold"] == 0.4 and again["source"] == "manual"
    assert store.archive("woolworths", "1")
    assert not store.archive("woolworths", "1")
    assert store.tracked_item("woolworths", "1") is None
    assert store.list_tracked() == []
    assert len(store.list_tracked(include_archived=True)) == 1
    restored, created = store.track("woolworths", "1")
    assert not created and restored["archived_at"] is None
    assert store.candidate_decisions("woolworths") == {"1": "tracked"}
    assert store.tracked_item_by_id(item["id"])["product_id"] == "1"


def test_track_rejects_unknown_product_and_source(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.track("woolworths", "nope")
    store.upsert_product("woolworths", "1")
    with pytest.raises(ValueError):
        store.track("woolworths", "1", source="psychic")


def test_update_tracked(store):
    store.upsert_product("woolworths", "1", name="Bread")
    store.track("woolworths", "1")
    assert store.update_tracked("woolworths", "1", target_price=2.5, snoozed_until="2026-11-01T00:00:00+00:00")
    assert store.tracked_item("woolworths", "1")["target_price"] == 2.5
    assert store.update_tracked("woolworths", "1", target_price=None)
    assert store.tracked_item("woolworths", "1")["target_price"] is None
    assert not store.update_tracked("woolworths", "1")
    with pytest.raises(ValueError):
        store.update_tracked("woolworths", "1", price=2)


def test_observations_keep_every_row(store):
    for day in range(3):
        store.add_observation("woolworths", "1", price=4.0, run_id="r", now=T0 + timedelta(days=day))
    assert len(store.observations("woolworths", "1")) == 3
    assert len(store.observations("woolworths", "1", since=T0 + timedelta(days=1))) == 2
    assert store.latest_observation("woolworths", "1")["observed_at"].startswith("2026-10-03")


def test_runs_and_ledger(store):
    run = store.start_run("refresh", now=T0)
    store.record_call("woolworths", "search_products", 1, "ok", run_id=run, now=T0)
    store.record_call("woolworths", "search_products", 2, "ok", run_id=run, now=T0)
    store.record_call("woolworths", "usage_summary", 0, "ok", now=T0)
    store.finish_run(run, "ok", usage_before=10, usage_after=13, now=T0 + timedelta(minutes=1))
    row = store.get_run(run)
    assert row["upstream_requests"] == 3 and row["outcome"] == "ok" and row["usage_after"] == 13
    assert store.upstream_since("woolworths", T0) == 3
    assert store.upstream_since("woolworths", T0 + timedelta(seconds=1)) == 0
    assert len(store.ledger(run_id=run)) == 2 and len(store.ledger()) == 3
    assert store.recent_runs(kind="refresh")[0]["id"] == run
    assert store.recent_runs()[0]["id"] == run
    other = store.start_run("refresh", now=T0)
    assert [r["id"] for r in store.unfinished_runs("refresh")] == [other]
    assert store.start_run("refresh", run_id=other) == other


def test_cart_snapshots_and_shop_episodes(store):
    assert store.last_cart_snapshot("woolworths") is None
    store.add_cart_snapshot("woolworths", [{"product_id": "1", "quantity": 2}], 9.9, now=T0)
    assert store.last_cart_snapshot("woolworths")["items"] == [{"product_id": "1", "quantity": 2}]
    assert store.open_shop_episode("woolworths") is None
    episode = store.start_shop_episode("woolworths", [{"product_id": "1"}], now=T0)
    store.update_shop_episode(episode, [{"product_id": "1"}, {"product_id": "2"}])
    assert len(store.open_shop_episode("woolworths")["items"]) == 2
    store.close_shop_episode(episode, now=T0 + timedelta(hours=1))
    store.add_shop_episode("woolworths", "receipt", [{"product_id": "3"}], started_at=T0, closed_at=T0)
    closed = store.closed_shop_episodes("woolworths")
    assert [e["source"] for e in closed] == ["cart", "receipt"]
    store.dismiss_candidate("woolworths", "2")
    assert store.candidate_decisions("woolworths") == {"2": "dismissed"}


def test_sale_episodes(store):
    store.upsert_product("woolworths", "1", name="Coffee")
    episode = store.start_sale_episode("woolworths", "1", price=10, baseline=20, discount=0.5, on_special=True, now=T0)
    store.update_sale_episode(episode, price=12, discount=0.4, on_special=False)
    row = store.open_sale_episode("woolworths", "1")
    assert row["low_price"] == 10 and row["last_price"] == 12 and row["max_discount"] == 0.5 and row["on_special"] == 1
    assert [e["name"] for e in store.open_sale_episodes()] == ["Coffee"]
    store.end_sale_episode(episode, now=T0)
    assert store.open_sale_episode("woolworths", "1") is None
    assert store.get_sale_episode(episode)["ended_at"]
    assert len(store.sale_episodes("woolworths", "1")) == 1


def test_alerts_dedupe_and_delivery_state(store):
    first = store.add_alert("on_sale:w:1:1", "on_sale", {"a": 1}, retailer="w", product_id="1", now=T0)
    assert first is not None
    assert store.add_alert("on_sale:w:1:1", "on_sale", {"a": 2}, now=T0) is None
    later = store.add_alert("x", "operator", {}, due_at=T0 + timedelta(hours=1), now=T0)
    assert [a["id"] for a in store.due_alerts(now=T0)] == [first]
    store.mark_alert_failed(first, "boom")
    assert store.get_alert(first)["attempts"] == 1
    store.mark_alert_sent(first, "telegram", now=T0)
    assert store.due_alerts(now=T0 + timedelta(hours=2))[0]["id"] == later
    store.mark_alert_acted(first, now=T0)
    assert store.get_alert(first)["acted_at"] and store.get_alert(first)["payload"] == {"a": 1}
    assert store.due_alerts(now=T0 + timedelta(hours=2), max_attempts=0) == []
    assert len(store.recent_alerts()) == 2


def test_cart_actions_and_item_stats(store):
    store.add_cart_action("woolworths", "1", 2, "cli", "ok", now=T0)
    store.add_cart_action("woolworths", "2", 1, "ui", "session_required", "reconnect")
    assert len(store.cart_actions()) == 2
    assert len(store.cart_actions(retailer="woolworths", product_id="1")) == 1
    assert store.item_stats("woolworths", "1") is None
    store.put_item_stats("woolworths", "1", {"baseline": 4.0})
    store.put_item_stats("woolworths", "1", {"baseline": 5.0})
    assert store.item_stats("woolworths", "1") == {"baseline": 5.0}


def test_stats_and_compact(store):
    store.add_observation("woolworths", "1", price=1.0, now=T0 - timedelta(days=800))
    store.add_observation("woolworths", "1", price=1.0, now=T0)
    store.add_cart_snapshot("woolworths", [], None, now=T0 - timedelta(days=100))
    store.record_call("woolworths", "search_products", 1, "ok", now=T0 - timedelta(days=500))
    before = store.stats()
    assert before["rows"]["observations"] == 2 and before["bytes"] > 0
    deleted = store.compact(now=T0)
    assert deleted == {"observations": 1, "cart_snapshots": 1, "budget_ledger": 1}
    assert store.stats()["rows"]["observations"] == 1


def test_backup_is_consistent_and_pruned(store, tmp_path):
    store.upsert_product("woolworths", "1", name="Milk")
    target = tmp_path / "backups"
    paths = [store.backup(target, keep=2, now=T0 + timedelta(days=d)) for d in range(3)]
    assert sorted(p.name for p in target.iterdir()) == [p.name for p in paths[1:]]
    assert oct(paths[-1].stat().st_mode & 0o777) == "0o600"
    copy = Store(paths[-1])
    assert copy.get_product("woolworths", "1")["name"] == "Milk" and copy.schema_version() == store.schema_version()
    copy.close()
