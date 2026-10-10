"""Product cache, search cache, multi-select tracking and one-time photos (all against the fake aus-cart)."""

from datetime import timedelta

import pytest

from aus_cartwatch.policy import Policy
from aus_cartwatch.scheduler import Scheduler
from aus_cartwatch.tracking import Tracker
from tests.conftest import T0

R = "woolworths"


@pytest.fixture(autouse=True)
def fetch_photos(monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_FETCH_PHOTOS", "1")


def tracker(store, fake, hours=24):
    return Tracker(store, fake, R, cache_hours=hours)


async def test_repeat_search_is_served_from_the_cache(store, fake):
    t = tracker(store, fake)
    first = await t.search("milk")
    assert not t.from_cache and [p.product_id for p in first] == ["1"]
    again = await t.search("  MILK ")
    assert t.from_cache and again[0].image_url.endswith("000001.jpg") and again[0].price == 4.95
    assert fake.calls.count("search_products") == 1


async def test_search_cache_expires(store, fake):
    t = tracker(store, fake, hours=0)
    await t.search("milk")
    await t.search("milk")
    assert fake.calls.count("search_products") == 2


async def test_tracking_from_search_results_costs_nothing_more(store, fake):
    t = tracker(store, fake)
    await t.search("bread")
    calls = fake.calls.copy()
    result = await t.track("2")
    assert result.created and t.from_cache
    assert [c for c in fake.calls if c not in calls] == ["get_product_image"]  # only the one-off photo
    assert store.get_image(R, "2")[1] == "image/jpeg"
    assert len(store.observations(R, "2")) == 1
    await t.track("2")  # re-tracking from the cache neither duplicates the observation nor refetches the photo
    assert len(store.observations(R, "2")) == 1 and fake.calls.count("get_product_image") == 1


async def test_unknown_or_stale_ids_ask_aus_cart(store, fake):
    t = tracker(store, fake)
    assert (await t.lookup("3")).name.startswith("Free Range")
    assert not t.from_cache and fake.calls.count("search_products") == 1
    assert await t.lookup("999") is None


async def test_track_many(store, fake):
    t = tracker(store, fake)
    await t.search("milk")
    results = await t.track_many(["1", "3", "1", " ", "999"])
    assert [r.created for r in results] == [True, True, False]
    assert "not found" in results[2].message
    assert {i["product_id"] for i in store.list_tracked()} == {"1", "3"}
    assert store.has_images(R) == {"1", "3"}


async def test_photo_failures_are_left_for_later(store, fake):
    t = tracker(store, fake)
    fake.tools.discard("get_product_image")
    await t.track("1")
    assert store.get_image(R, "1") is None
    fake.tools.add("get_product_image")
    assert not await Tracker(store, None, R).ensure_image("1")
    del fake.products["1"]
    assert not await t.ensure_image("1")  # aus-cart-mcp has no photo: logged, not raised


async def test_refresh_backfills_missing_photos_within_budget(store, fake, clock):
    for pid in fake.products:
        store.upsert_product(R, pid, name=fake.products[pid].name)
        store.track(R, pid)
    scheduler = Scheduler(
        store, fake.factory(store, clock=clock), clock=clock, sleep=clock.sleep, tz="Australia/Sydney"
    )
    assert (await scheduler.refresh()).outcome == "ok"
    assert fake.calls.count("get_product_image") == 0  # price runs never fetch photos
    result = await scheduler.photos(limit=2)
    assert result.outcome == "ok" and result.note == "2 photos stored"
    assert len(scheduler.missing_photos()) == 1
    await scheduler.photos()
    assert store.has_images(R) == {"1", "2", "3"} and scheduler.missing_photos() == []
    await scheduler.photos()
    assert fake.calls.count("get_product_image") == 3  # never twice
    clock.now = clock.now.replace(hour=13)  # midnight in Sydney: no traffic
    assert (await scheduler.photos()).outcome == "skipped"


async def test_tick_fetches_photos_after_a_refresh_slot(store, fake, clock):
    store.upsert_product(R, "1", name="Milk")
    store.track(R, "1")
    scheduler = Scheduler(
        store, fake.factory(store, clock=clock), clock=clock, sleep=clock.sleep, tz="Australia/Sydney"
    )
    first = scheduler.slots(scheduler.local(clock()).date(), Policy())[0]
    clock.now = first.at + timedelta(minutes=1)
    kinds = [r.run_id.split("-")[0] for r in await scheduler.tick()]
    assert kinds[:2] == ["refresh", "photos"] and store.has_images(R) == {"1"}


def test_cached_product_respects_age(store, fake):
    store.remember_product(R, fake.products["1"], now=T0)
    assert store.cached_product(R, "1", max_age=timedelta(hours=1), now=T0 + timedelta(minutes=30))
    assert store.cached_product(R, "1", max_age=timedelta(hours=1), now=T0 + timedelta(hours=2)) is None
    assert store.cached_product(R, "nope", max_age=timedelta(hours=1)) is None
    store.cache_search(R, "milk", ["1", "gone"], now=T0)
    assert store.cached_search(R, "milk", max_age=timedelta(hours=1), now=T0) is None  # a member is unknown


async def test_photos_are_not_fetched_unless_enabled(store, fake, monkeypatch):
    monkeypatch.delenv("AUS_CARTWATCH_FETCH_PHOTOS")
    t = tracker(store, fake)
    await t.track("1")
    assert fake.calls.count("get_product_image") == 0 and store.get_image(R, "1") is None
    assert store.tracked_item(R, "1")["image_url"].endswith("000001.jpg")  # the browser shows this instead


def test_track_cart_lines_needs_no_retailer_call(store):
    from aus_cartwatch.auscart import Cart, CartLine
    from aus_cartwatch.detect import process_snapshot

    process_snapshot(store, R, Cart([CartLine("7", "Eggs", 2, 6.8), CartLine("8", "Bread", 1, 3.5)]))
    results = Tracker(store, None, R).track_cart_lines(["7", "9"])
    assert results[0].created and results[0].item["source"] == "cart"
    assert "not in the last cart snapshot" in results[1].message
    assert store.latest_observation(R, "7")["price"] == 6.8
