from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from aus_cartwatch import alerts
from aus_cartwatch.auscart import Product
from aus_cartwatch.policy import Policy
from aus_cartwatch.tracking import observe
from tests.conftest import T0

SYD = ZoneInfo("Australia/Sydney")


def see(store, day, price, *, special=False, pid="1", name="Coffee"):
    observe(store, "woolworths", Product(pid, name, price, on_special=special, url="u"), now=T0 + timedelta(days=day))


def tracked(store, pid="1", **kw):
    store.upsert_product("woolworths", pid, name="Coffee")
    store.track("woolworths", pid, **kw)


class Recorder:
    name = "recorder"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, message):
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(message)


def evaluate(store, day=0, hour=0):
    return alerts.evaluate(store, "woolworths", Policy(), now=T0 + timedelta(days=day, hours=hour), tz=SYD)


def kinds(store):
    return sorted(a["kind"] for a in store.recent_alerts())


def test_one_alert_per_episode_and_the_heavy_upgrade(store):
    tracked(store)
    for day in range(3):
        see(store, day, 10.0)
    assert evaluate(store) == []
    see(store, 3, 8.0, special=True)
    evaluate(store, 3)
    assert kinds(store) == ["on_sale"]
    see(store, 4, 8.0, special=True)
    evaluate(store, 4)
    assert kinds(store) == ["on_sale"]  # same episode, no duplicate
    see(store, 5, 5.0, special=True)
    evaluate(store, 5)
    assert kinds(store) == ["heavy_discount", "on_sale"]  # the upgrade fires once
    see(store, 6, 10.0)
    see(store, 7, 5.0, special=True)
    evaluate(store, 7)
    assert kinds(store) == ["heavy_discount", "heavy_discount", "on_sale", "on_sale"]  # a new episode


def test_target_price_and_snooze(store):
    tracked(store, target_price=9.0)
    see(store, 0, 9.5)
    evaluate(store)
    assert kinds(store) == []
    see(store, 1, 9.0)
    evaluate(store, 1)
    assert kinds(store) == ["target_price"]
    evaluate(store, 2)
    assert kinds(store) == ["target_price"]
    tracked(store, pid="2")
    see(store, 0, 1.0, special=True, pid="2")
    store.update_tracked("woolworths", "2", snoozed_until=(T0 + timedelta(days=30)).isoformat())
    evaluate(store, 3)
    assert kinds(store) == ["target_price"]


def test_unavailable_and_unpriced_items_raise_nothing(store):
    tracked(store)
    observe(store, "woolworths", Product("1", "Coffee", None, on_special=True), now=T0)
    observe(store, "woolworths", Product("1", "Coffee", 1.0, on_special=True, available=False), now=T0)
    assert evaluate(store) == []


def test_quiet_hours_and_digest_timing():
    policy = Policy()
    evening = T0.replace(hour=11)  # 22:00 Sydney: quiet
    released = alerts.deliverable_at(evening, policy, SYD).astimezone(SYD)
    assert (released.hour, released.minute, released.day) == (7, 0, 6)
    noon = T0.replace(hour=1)  # 12:00 Sydney
    assert alerts.deliverable_at(noon, policy, SYD) == noon
    digest = alerts.next_digest(noon, policy, SYD).astimezone(SYD)
    assert (digest.hour, digest.minute, digest.day) == (7, 30, 6)
    early = T0.replace(hour=19)  # 06:00 Sydney: quiet, digest later today
    assert alerts.next_digest(early, policy, SYD).astimezone(SYD).hour == 7


def test_on_sale_waits_for_the_digest_unless_immediate(store, monkeypatch):
    tracked(store)
    see(store, 0, 5.0, special=True)
    evaluate(store, 0, hour=2)  # 11:00 Sydney
    alert = store.recent_alerts()[0]
    assert alert["due_at"] > (T0 + timedelta(hours=2)).isoformat()
    monkeypatch.setenv("AUS_CARTWATCH_IMMEDIATE_ON_SALE", "1")
    tracked(store, pid="2")
    see(store, 0, 1.0, special=True, pid="2")
    evaluate(store, 0, hour=2)
    assert store.recent_alerts()[0]["due_at"] == (T0 + timedelta(hours=2)).isoformat()


async def test_dispatch_groups_supersedes_and_retries(store):
    tracked(store)
    for day in range(3):
        see(store, day, 10.0)
    see(store, 3, 4.0, special=True)
    tracked(store, pid="2")
    see(store, 3, 2.0, special=True, pid="2")
    evaluate(store, 3)
    alerts.operator(store, "operator:x", "something broke", now=T0)
    later = T0 + timedelta(days=5)
    failing = Recorder(fail=True)
    assert await alerts.dispatch(store, [failing], now=later) == 0
    assert all(a["attempts"] == 1 and a["last_error"] for a in store.due_alerts(now=later))
    ok = Recorder()
    delivered = await alerts.dispatch(store, [failing, ok], now=later)
    assert delivered == 3  # heavy (item 1), operator, digest (item 2); item 1's on_sale is superseded
    assert sorted(m.kind for m in ok.sent) == ["digest", "heavy_discount", "operator"]
    heavy = next(m for m in ok.sent if m.kind == "heavy_discount")
    assert "60% off" in heavy.text and heavy.buttons[0][1].startswith("add:")
    assert next(m for m in ok.sent if m.kind == "operator").buttons == []
    assert {a["channel"] for a in store.recent_alerts()} == {"recorder", "superseded"}
    assert await alerts.dispatch(store, [ok], now=later) == 0


async def test_dispatch_without_notifiers_keeps_alerts_pending(store):
    alerts.operator(store, "operator:y", "hello", now=T0)
    assert await alerts.dispatch(store, [], now=T0) == 0
    assert len(store.due_alerts(now=T0)) == 1
    assert alerts.operator(store, "operator:y", "hello again", now=T0) is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("target_price", "🎯 Target price reached"), ("on_sale", "On sale"), ("weird", "weird")],
)
def test_describe(kind, expected):
    payload = {"name": "Coffee", "size": "200g", "price": 9.0, "baseline": 10.0, "discount": 0.1, "url": "",
               "unit_price": "$4.50 / 100g", "target_price": 9.0, "was_price": None}  # fmt: skip
    text = alerts.describe({"kind": kind, "payload": payload})
    assert text.startswith(expected) and "Coffee 200g: $9.00" in text and "(was $10.00, 10% off)" in text
