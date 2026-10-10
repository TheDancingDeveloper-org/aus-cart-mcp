"""Recurring-item detection replayed from a recorded sequence of cart snapshots."""

from datetime import timedelta

from aus_cart_mcp.watch import detect
from aus_cart_mcp.watch.auscart import Cart, CartLine
from aus_cart_mcp.watch.policy import Policy
from aus_cart_mcp.watch.tracking import Tracker
from tests.conftest import T0

MILK, BREAD, EGGS, CHIPS = "1", "2", "3", "4"
NAMES = {MILK: "Milk", BREAD: "Bread", EGGS: "Eggs", CHIPS: "Chips"}


def cart(*ids: str) -> Cart:
    return Cart([CartLine(pid, NAMES[pid], 1, 2.0) for pid in ids], subtotal=2.0 * len(ids))


def replay(store, snapshots):
    notes = []
    for hours, ids in snapshots:
        notes.append(detect.process_snapshot(store, "woolworths", cart(*ids), now=T0 + timedelta(hours=hours)))
    return notes


SHOPS = [
    (0, [MILK]),
    (1, [MILK, BREAD, CHIPS]),
    (2, [MILK, BREAD, CHIPS]),  # unchanged: not stored again
    (3, []),  # checkout: episode 1 closes with milk, bread, chips
    (100, [MILK, EGGS]),
    (101, [MILK, EGGS, CHIPS]),
    (102, []),  # episode 2: milk, eggs, chips
    (200, [BREAD]),
    (201, []),  # episode 3: bread
]


def test_snapshots_segment_into_episodes(store):
    notes = replay(store, SHOPS)
    assert notes[2] == "unchanged" and notes[3] == "cart emptied; episode closed"
    episodes = store.closed_shop_episodes("woolworths")
    assert [sorted(i["product_id"] for i in e["items"]) for e in episodes] == [
        sorted([BREAD]),
        sorted([CHIPS, EGGS, MILK]),
        sorted([BREAD, CHIPS, MILK]),
    ]
    assert store.stats()["rows"]["cart_snapshots"] == len(SHOPS) - 1


def test_candidates_two_shops_and_a_dismissal(store):
    replay(store, SHOPS)
    found = detect.candidates(store, "woolworths")
    assert [c["product_id"] for c in found] == [BREAD, CHIPS, MILK]  # bread is most recent, so most confident
    assert all(c["seen"] == 2 and c["episodes"] == 3 for c in found)
    detect.dismiss_candidate(store, "woolworths", CHIPS)
    tracker = Tracker(store, None, "woolworths")
    result = detect.accept_candidate(tracker, MILK, now=T0)
    assert result.created and result.item["source"] == "cart"
    assert [c["product_id"] for c in detect.candidates(store, "woolworths")] == [BREAD]
    assert detect.accept_candidate(tracker, EGGS).message.endswith("is not a current candidate")
    assert store.latest_observation("woolworths", MILK)["price"] == 2.0


def test_idle_episode_closes_after_a_week(store):
    detect.process_snapshot(store, "woolworths", cart(MILK), now=T0)
    assert detect.process_snapshot(store, "woolworths", cart(MILK), now=T0 + timedelta(days=3)) == "unchanged"
    note = detect.process_snapshot(store, "woolworths", cart(MILK), Policy(), now=T0 + timedelta(days=7))
    assert note == "unchanged; idle episode closed"
    assert detect.process_snapshot(store, "woolworths", cart(), now=T0 + timedelta(days=8)) == "empty"
    assert detect.candidates(store, "woolworths") == []


def test_no_episodes_no_candidates(store):
    assert detect.candidates(store, "woolworths") == []
