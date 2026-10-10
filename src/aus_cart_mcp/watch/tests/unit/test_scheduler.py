"""Governor policy on a fake clock: cap, shared-tenant guard, blocked abort and backoff, quiet hours, slots."""

from datetime import date, timedelta

import pytest

from aus_cart_mcp.watch.auscart import Blocked, Unavailable
from aus_cart_mcp.watch.policy import Policy
from aus_cart_mcp.watch.scheduler import Scheduler
from tests.conftest import FakeAusCart, product


def make(store, fake, clock, **kw):
    Policy.save(store, scheduled_snapshots="1")  # the policy tests exercise scheduled snapshots too
    for pid, p in fake.products.items():
        store.upsert_product("woolworths", pid, name=p.name)
        store.track("woolworths", pid)
    return Scheduler(
        store, fake.factory(store, clock=clock), clock=clock, sleep=clock.sleep, tz="Australia/Sydney", **kw
    )


async def test_refresh_observes_every_item_and_spaces_searches(store, fake, clock):
    scheduler = make(store, fake, clock, daily_cap=60)
    result = await scheduler.refresh()
    assert result.outcome == "ok" and result.observed == 3
    assert fake.calls.count("search_products") == 3
    assert clock.slept == [2.0, 2.0]
    run = store.get_run(result.run_id)
    assert run["upstream_requests"] == 3 and run["usage_before"] == 0 and run["usage_after"] == 3
    assert all(store.latest_observation("woolworths", pid)["run_id"] == result.run_id for pid in "123")


async def test_daily_cap_stops_the_run_and_the_next_one(store, fake, clock):
    scheduler = make(store, fake, clock, daily_cap=2)
    first = await scheduler.refresh()
    assert first.outcome == "partial" and first.observed == 2
    assert "budget stopped the run after 2 of 3" in store.get_run(first.run_id)["note"]
    second = await scheduler.refresh()
    assert second.outcome == "skipped" and "daily budget" in second.note
    assert fake.calls.count("search_products") == 2
    assert store.recent_alerts() == []  # a partial run counts as a refresh today
    clock.advance(days=1)
    assert (await scheduler.refresh()).outcome == "partial"  # a new local day resets the cap


async def test_budget_skip_raises_an_operator_alert_when_nothing_ran_today(store, fake, clock):
    scheduler = make(store, fake, clock, daily_cap=1)
    store.record_call("woolworths", "search_products", 1, "ok", now=clock())  # e.g. a manual track used it up
    result = await scheduler.refresh()
    assert result.outcome == "skipped"
    assert [a["dedupe_key"].split(":")[1] for a in store.recent_alerts()] == ["budget"]


async def test_force_bypasses_the_cap(store, fake, clock):
    scheduler = make(store, fake, clock, daily_cap=0)
    assert (await scheduler.refresh()).outcome == "skipped"
    assert (await scheduler.refresh(force=True)).outcome == "ok"


async def test_shared_tenant_guard_skips_without_retailer_traffic(store, fake, clock):
    scheduler = make(store, fake, clock, gateway_cap=100, shared_fraction=0.5)
    fake.usage = 50
    result = await scheduler.refresh()
    assert result.outcome == "skipped" and "shared tenant at 50/100" in result.note
    assert "search_products" not in fake.calls


async def test_blocked_aborts_without_retry_and_backs_off(store, fake, clock):
    scheduler = make(store, fake, clock)
    fake.fail_with = Blocked("woolworths is blocking automated requests; paused for about 30 more minutes")
    result = await scheduler.refresh()
    assert result.outcome == "blocked"
    assert fake.calls.count("search_products") == 1
    fake.fail_with = None
    clock.advance(minutes=60)
    skipped = await scheduler.refresh()
    assert skipped.outcome == "skipped" and "backing off" in skipped.note
    assert (await scheduler.refresh(force=True)).outcome == "skipped"  # force does not override a block
    clock.advance(minutes=61)
    assert (await scheduler.refresh()).outcome == "ok"


async def test_failure_streak_alerts_once_and_halves_the_cadence(store, fake, clock):
    scheduler = make(store, fake, clock)
    Policy.save(store, refresh_times="06:30,18:30")  # two runs a day, so there is a second slot to drop
    fake.fail_with = Unavailable("aus-cart-mcp is unreachable")
    for _ in range(3):
        assert (await scheduler.refresh()).outcome == "unavailable"
    alerts = [a for a in store.recent_alerts() if a["kind"] == "operator"]
    assert len(alerts) == 1 and "3 refresh runs in a row failed" in alerts[0]["payload"]["text"]
    assert len(scheduler.failure_streak()) == 3
    await scheduler.refresh()
    assert len([a for a in store.recent_alerts() if a["kind"] == "operator"]) == 1  # same streak, same alert
    # Halved cadence: the second slot of the day is skipped while the streak lasts.
    policy = Policy.load(store)
    clock.now = scheduler.slots(scheduler.local(clock()).date(), policy)[1].at + timedelta(minutes=1)
    results = await scheduler.tick()
    assert results[0].outcome == "skipped" and "cadence halved" in results[0].note


async def test_quiet_hours(store, fake, clock):
    scheduler = make(store, fake, clock)
    clock.now = clock.now.replace(hour=13)  # 00:00 in Sydney
    result = await scheduler.refresh()
    assert result.outcome == "skipped" and result.note == "no-runs window"
    assert fake.calls == []


async def test_reconcile_counts_hidden_upstream_requests(store, fake, clock):
    scheduler = make(store, fake, clock)
    fake.extra_upstream = 1  # every retailer call costs one more request than the client can see
    result = await scheduler.refresh()
    ledger = store.ledger(run_id=result.run_id)
    assert [r["tool"] for r in ledger if r["tool"] == "reconcile"] == ["reconcile"]
    assert store.get_run(result.run_id)["upstream_requests"] == 6 == fake.usage


async def test_a_crashed_run_resumes_where_it_stopped(store, fake, clock):
    scheduler = make(store, fake, clock)
    run_id = store.start_run("refresh", run_id="refresh-slot", now=clock())
    first = sorted(fake.products)[0]
    store.add_observation("woolworths", first, price=1.0, run_id=run_id, now=clock())
    result = await scheduler.refresh(run_id=run_id)
    assert result.observed == 2 and fake.calls.count("search_products") == 2


async def test_batched_refresh_of_100_items_costs_at_most_5_requests(store, clock):
    fake = FakeAusCart({str(i): product(str(i), f"Item {i}", 1.0 + i / 100) for i in range(100)})
    fake.tools.add("get_products")
    scheduler = make(store, fake, clock)
    result = await scheduler.refresh()
    assert result.outcome == "ok" and result.observed == 100
    assert store.get_run(result.run_id)["upstream_requests"] <= 5
    assert clock.slept == []


async def test_missing_products_are_noted(store, fake, clock):
    scheduler = make(store, fake, clock)
    store.upsert_product("woolworths", "999", name="Discontinued thing")
    store.track("woolworths", "999")
    result = await scheduler.refresh()
    assert result.missing == ["999"] and "not found: 999" in store.get_run(result.run_id)["note"]


def test_slots_jitter_and_specials_day(store, fake, clock):
    scheduler = make(store, fake, clock)
    assert [s.run_id[-4:] for s in scheduler.slots(date(2026, 10, 7), Policy())] == ["0630"]  # default: once a day
    policy = Policy(refresh_times="06:30,18:30", specials_day=2, specials_time="07:00")
    tuesday, wednesday = date(2026, 10, 6), date(2026, 10, 7)
    assert len(scheduler.slots(tuesday, policy)) == 2
    slots = scheduler.slots(wednesday, policy)
    assert [s.run_id[-4:] for s in slots] == ["0630", "0700", "1830"]
    for slot in slots:
        local = scheduler.local(slot.at)
        planned = local.replace(hour=int(slot.run_id[-4:-2]), minute=int(slot.run_id[-2:]), second=0, microsecond=0)
        assert abs((local - planned).total_seconds()) <= 20 * 60
    assert scheduler.slots(wednesday, policy) == slots  # deterministic per day
    late = Policy(refresh_times="23:30")
    assert scheduler.slots(tuesday, late) == []


async def test_tick_runs_the_due_slot_once_and_snapshots(store, fake, clock):
    scheduler = make(store, fake, clock)
    policy = Policy.load(store)
    first = scheduler.slots(scheduler.local(clock()).date(), policy)[0]
    clock.now = first.at + timedelta(minutes=5)
    results = await scheduler.tick()
    assert [r.run_id for r in results][:1] == [first.run_id]
    assert {r.run_id.split("-")[0] for r in results} == {"refresh", "snapshot"}  # photos only with FETCH_PHOTOS
    again = await scheduler.tick()
    assert again == []  # slot done, snapshot not due yet
    clock.advance(hours=7)
    assert any(r.run_id.startswith("snapshot") for r in await scheduler.tick())


async def test_snapshot_session_expiry_raises_one_operator_alert_per_day(store, fake, clock):
    scheduler = make(store, fake, clock)
    fake.session = False
    assert (await scheduler.snapshot()).outcome == "session_required"
    clock.advance(hours=1)
    await scheduler.snapshot()
    operator = [a for a in store.recent_alerts() if a["kind"] == "operator"]
    assert len(operator) == 1 and "reconnect in the myaiagent app" in operator[0]["payload"]["text"]


async def test_snapshot_cadence(store, fake, clock):
    scheduler = make(store, fake, clock)
    policy = Policy(scheduled_snapshots=1)
    assert scheduler.snapshot_due(clock(), policy)
    fake.cart = {"1": 2}
    await scheduler.snapshot()
    clock.advance(minutes=30)
    assert not scheduler.snapshot_due(clock(), policy)
    clock.advance(minutes=31)
    assert scheduler.snapshot_due(clock(), policy)  # hourly while the cart is in use
    fake.cart = {}
    await scheduler.snapshot()
    clock.advance(hours=2)
    assert not scheduler.snapshot_due(clock(), policy)  # 6-hourly while empty
    clock.advance(hours=5)
    assert scheduler.snapshot_due(clock(), policy)


@pytest.mark.parametrize("error", [Blocked("refusing requests (HTTP 403)"), Unavailable("down")])
async def test_snapshot_errors_are_recorded(store, fake, clock, error):
    scheduler = make(store, fake, clock)
    fake.fail_with = error
    result = await scheduler.snapshot()
    assert result.outcome in ("blocked", "error")


async def test_nightly_backup_once_a_day(store, fake, clock, tmp_path, monkeypatch):
    scheduler = make(store, fake, clock)
    policy = Policy()
    monkeypatch.setenv("AUS_CARTWATCH_BACKUP_DIR", str(tmp_path / "b"))
    assert not scheduler.backup_due(clock(), policy, "")
    clock.now = clock.now.replace(hour=16, minute=0)  # 03:00 Sydney: before backup_time
    assert not scheduler.backup_due(clock(), policy, str(tmp_path / "b"))
    clock.advance(minutes=45)
    results = await scheduler.tick()
    assert [r.outcome for r in results if r.run_id.startswith("backup")] == ["ok"]
    assert not scheduler.backup_due(clock(), policy, str(tmp_path / "b"))
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    assert scheduler.backup(str(blocker), 14).outcome == "error"
    assert any(a["dedupe_key"].startswith("operator:backup:") for a in store.recent_alerts())


async def test_a_blocked_snapshot_backs_off_snapshots_but_not_price_refreshes(store, fake, clock):
    scheduler = make(store, fake, clock)
    policy = Policy.load(store)
    fake.fail_with = Blocked("Woolworths is refusing requests (HTTP 403)")
    assert (await scheduler.snapshot()).outcome == "blocked"
    fake.fail_with = None
    clock.advance(minutes=90)
    assert not scheduler.snapshot_due(clock(), policy)
    assert (await scheduler.refresh()).outcome == "ok"  # prices use a different tenant
    clock.advance(minutes=31)
    clock.advance(hours=4)  # the idle cadence counts from the blocked attempt
    assert scheduler.snapshot_due(clock(), policy)


async def test_a_blocked_refresh_backs_off_refreshes_only(store, fake, clock):
    scheduler = make(store, fake, clock)
    fake.fail_with = Blocked("Woolworths is refusing requests (HTTP 403)")
    assert (await scheduler.refresh()).outcome == "blocked"
    fake.fail_with = None
    assert "backing off" in (await scheduler.refresh()).note
    assert (await scheduler.snapshot()).outcome == "ok"


def test_scheduled_snapshots_are_off_by_default(store, fake, clock):
    scheduler = Scheduler(store, fake.factory(store, clock=clock), clock=clock, tz="Australia/Sydney")
    Policy.save(store, scheduled_snapshots="0")
    assert not scheduler.snapshot_due(clock(), Policy.load(store))
    assert Policy().scheduled_snapshots == 0


async def test_a_failing_batch_tool_falls_back_to_search(store, fake, clock):
    from aus_cart_mcp.watch.auscart import RetailerError

    fake.tools.add("get_products")
    fake.batch_error = RetailerError("Woolworths returned an unexpected response shape")
    scheduler = make(store, fake, clock)
    result = await scheduler.refresh()
    assert result.outcome == "ok" and result.observed == 3
    assert fake.calls.count("get_products") == 1 and fake.calls.count("search_products") == 3


async def test_a_blocked_batch_aborts_the_run(store, fake, clock):
    fake.tools.add("get_products")
    fake.batch_error = Blocked("Woolworths is refusing requests (HTTP 403)")
    result = await make(store, fake, clock).refresh()
    assert result.outcome == "blocked" and fake.calls.count("search_products") == 0
