"""Detecting recurring items from cart snapshots (CW-24).

The cart is the only live signal of what the household buys that costs almost
nothing extra to read. The scheduler snapshots it (hourly while non-empty, every
6 hours otherwise); unchanged carts are not stored twice.

A cart that goes from non-empty to empty closes a **shop episode** (the owner
checked out or cleared it; checkout itself is invisible to us), and the items
in it at that point count as bought. An episode also closes after
`episode_idle_days` without a change. Receipts, order history and email orders
(CW-40..42) add closed episodes from other sources to the same table, so one
scorer serves them all.

A product seen in at least 2 of the last 6 closed episodes is a **candidate**,
scored by recency-weighted share of episodes. Accepting a candidate tracks it
(`source=cart`); dismissing it is remembered.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from aus_cart_mcp.watch.types import Cart, Product
from aus_cart_mcp.watch.policy import Policy
from aus_cart_mcp.watch.store import Store, parse_time, utcnow
from aus_cart_mcp.watch.tracking import Tracker, TrackResult

EPISODES_CONSIDERED = 6
MIN_EPISODES_SEEN = 2
RECENCY_DECAY = 0.15  # weight of the n-th most recent episode = 1 / (1 + n * decay)


def cart_items(cart: Cart) -> list[dict]:
    return sorted(
        (
            {"product_id": line.product_id, "name": line.name, "quantity": line.quantity, "price": line.price}
            for line in cart.items
            if line.quantity > 0
        ),
        key=lambda item: item["product_id"],
    )


def process_snapshot(
    store: Store, retailer: str, cart: Cart, policy: Policy | None = None, *, now: datetime | None = None
) -> str:
    """Store a snapshot if the cart changed, and move shop episodes along. Returns a short note."""
    policy = policy or Policy()
    now = now or utcnow()
    items = cart_items(cart)
    last = store.last_cart_snapshot(retailer)
    episode = store.open_shop_episode(retailer)
    if last is not None and last["items"] == items:
        if episode is not None and now - parse_time(last["taken_at"]) >= timedelta(days=policy.episode_idle_days):
            store.close_shop_episode(episode["id"], now=now)
            return "unchanged; idle episode closed"
        return "unchanged"
    store.add_cart_snapshot(retailer, items, cart.subtotal, now=now)
    for item in items:
        store.upsert_product(retailer, item["product_id"], name=item["name"], now=now)
    if items:
        if episode is None:
            store.start_shop_episode(retailer, items, now=now)
            return f"{len(items)} items; episode started"
        store.update_shop_episode(episode["id"], items)
        return f"{len(items)} items"
    if episode is not None:
        store.close_shop_episode(episode["id"], now=now)
        return "cart emptied; episode closed"
    return "empty"


def candidates(store: Store, retailer: str) -> list[dict]:
    """Products bought repeatedly but not tracked, most confident first."""
    episodes = store.closed_shop_episodes(retailer, limit=EPISODES_CONSIDERED)
    if not episodes:
        return []
    weights = [1 / (1 + n * RECENCY_DECAY) for n in range(len(episodes))]
    total = sum(weights)
    decisions = store.candidate_decisions(retailer)
    seen: dict[str, dict] = {}
    for weight, episode in zip(weights, episodes, strict=True):
        for item in {i["product_id"]: i for i in episode["items"]}.values():
            entry = seen.setdefault(
                item["product_id"],
                {"product_id": item["product_id"], "name": item.get("name") or "", "seen": 0, "score": 0.0},
            )
            entry["seen"] += 1
            entry["score"] += weight
            if entry.get("price") is None and item.get("price") is not None:
                entry["price"] = item["price"]
    out = []
    for entry in seen.values():
        if entry["seen"] < MIN_EPISODES_SEEN or entry["product_id"] in decisions:
            continue
        if store.tracked_item(retailer, entry["product_id"]):
            continue
        out.append(
            {
                "retailer": retailer,
                "product_id": entry["product_id"],
                "name": entry["name"] or (store.get_product(retailer, entry["product_id"]) or {}).get("name", ""),
                "price": entry.get("price"),
                "seen": entry["seen"],
                "episodes": len(episodes),
                "confidence": round(entry["score"] / total, 3),
            }
        )
    out.sort(key=lambda c: (-c["confidence"], c["name"]))
    return out


def accept_candidate(tracker: Tracker, product_id: str, *, now: datetime | None = None) -> TrackResult:
    """Track a candidate without a retailer call: its last cart line is the first observation."""
    match = next((c for c in candidates(tracker.store, tracker.retailer) if c["product_id"] == product_id), None)
    if match is None:
        return TrackResult(message=f"{product_id} is not a current candidate")
    product = tracker.store.cached_product(tracker.retailer, product_id, max_age=tracker.cache_ttl) or Product(
        product_id=product_id, name=match["name"], price=match.get("price")
    )
    return tracker.track_product(product, source="cart", now=now)


def dismiss_candidate(store: Store, retailer: str, product_id: str) -> None:
    store.dismiss_candidate(retailer, product_id)
