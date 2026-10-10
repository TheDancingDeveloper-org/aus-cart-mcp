"""Price analytics: baseline, discount, sale episodes and the per-item stats block (CW-26).

The definitions are pinned by tests and written down in docs/DESIGN.md § Price analytics:

- **Baseline** = the retailer's `was_price` when present and above the price;
  otherwise, with at least 3 observations, the median of non-special prices over
  the last 60 days, or the highest observed price when every recent observation
  was on special. Fewer than 3 observations and no `was_price` → no baseline.
- **Discount** = 1 − price / baseline (never negative). **Heavy** when ≥ the
  item's threshold (default 0.30).
- **Sale episode** starts when the item is on special or ≥10% below baseline,
  and ends when neither holds. One episode → at most one alert of each kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from aus_cart_mcp.watch.store import Store, parse_time, utcnow

BASELINE_WINDOW_DAYS = 60
MIN_OBSERVATIONS = 3
SALE_DROP = 0.10
DEFAULT_THRESHOLD = 0.30


def baseline(observations: list[dict], *, now: datetime | None = None) -> float | None:
    """The reference price for the latest observation in `observations` (oldest first)."""
    if not observations:
        return None
    latest = observations[-1]
    price, was = latest.get("price"), latest.get("was_price")
    if was is not None and price is not None and was > price:
        return float(was)
    priced = [o for o in observations if o.get("price") is not None]
    if len(priced) < MIN_OBSERVATIONS:
        return None
    since = (now or utcnow()) - timedelta(days=BASELINE_WINDOW_DAYS)
    recent = [o for o in priced if parse_time(o["observed_at"]) >= since]
    regular = [float(o["price"]) for o in recent if not o.get("on_special")]
    if regular:
        return float(median(regular))
    return max(float(o["price"]) for o in priced)


def discount(price: float | None, base: float | None) -> float:
    if price is None or not base or base <= 0:
        return 0.0
    return max(0.0, round(1 - price / base, 4))


def is_on_sale(observation: dict, base: float | None) -> bool:
    return bool(observation.get("on_special")) or discount(observation.get("price"), base) >= SALE_DROP


@dataclass(frozen=True)
class EpisodeChange:
    kind: str  # "started" | "continued" | "ended" | "none"
    episode_id: int | None


def update_sale_episode(store: Store, retailer: str, product_id: str, *, now: datetime | None = None) -> EpisodeChange:
    """Fold the latest observation of an item into its sale episodes."""
    history = store.observations(retailer, product_id)
    if not history:
        return EpisodeChange("none", None)
    latest = history[-1]
    base = baseline(history, now=now)
    open_episode = store.open_sale_episode(retailer, product_id)
    price = latest.get("price")
    if price is not None and latest.get("available", 1) and is_on_sale(latest, base):
        off = discount(price, base)
        if open_episode is None:
            episode = store.start_sale_episode(
                retailer,
                product_id,
                price=price,
                baseline=base,
                discount=off,
                on_special=bool(latest.get("on_special")),
                now=now,
            )
            return EpisodeChange("started", episode)
        store.update_sale_episode(open_episode["id"], price=price, discount=off, on_special=bool(latest["on_special"]))
        return EpisodeChange("continued", open_episode["id"])
    if open_episode is not None and price is not None:
        store.end_sale_episode(open_episode["id"], now=now)
        return EpisodeChange("ended", open_episode["id"])
    return EpisodeChange("none", open_episode["id"] if open_episode else None)


def _window(observations: list[dict], days: int, now: datetime) -> dict | None:
    since = now - timedelta(days=days)
    prices = [
        float(o["price"]) for o in observations if o.get("price") is not None and parse_time(o["observed_at"]) >= since
    ]
    if not prices:
        return None
    return {"min": min(prices), "max": max(prices), "median": float(median(prices))}


def item_stats(
    observations: list[dict],
    episodes: list[dict],
    *,
    target_price: float | None = None,
    threshold: float | None = None,
    now: datetime | None = None,
) -> dict:
    """The stats block for one item: current price, baseline, discount, windows, sale cadence, target."""
    now = now or utcnow()
    threshold = DEFAULT_THRESHOLD if threshold is None else threshold
    latest = observations[-1] if observations else {}
    previous = observations[-2] if len(observations) > 1 else {}
    price = latest.get("price")
    base = baseline(observations, now=now)
    off = discount(price, base)
    starts = sorted(parse_time(e["started_at"]) for e in episodes)
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(starts, starts[1:], strict=False)]
    lows = [float(e["low_price"]) for e in episodes if e.get("low_price") is not None]
    change = None
    if price is not None and previous.get("price") is not None:
        change = round(price - float(previous["price"]), 2)
    return {
        "price": price,
        "was_price": latest.get("was_price"),
        "on_special": bool(latest.get("on_special")),
        "available": bool(latest.get("available", 1)) if latest else None,
        "observed_at": latest.get("observed_at"),
        "unit_price_value": latest.get("unit_price_value"),
        "unit_price_unit": latest.get("unit_price_unit") or "",
        "baseline": base,
        "discount": off,
        "heavy": base is not None and off >= threshold,
        "threshold": threshold,
        "on_sale": bool(latest) and is_on_sale(latest, base),
        "change": change,
        "observations": len(observations),
        "d7": _window(observations, 7, now),
        "d30": _window(observations, 30, now),
        "d90": _window(observations, 90, now),
        "last_sale": max(starts).isoformat() if starts else None,
        "sales": len(episodes),
        "avg_days_between_sales": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "typical_sale_price": float(median(lows)) if lows else None,
        "target_price": target_price,
        "target_met": target_price is not None and price is not None and price <= target_price,
    }


def refresh_item_stats(store: Store, retailer: str, product_id: str, *, now: datetime | None = None) -> dict:
    """Recompute and store the materialised stats for one item (after each run, so reads never recompute)."""
    item = store.tracked_item(retailer, product_id, include_archived=True) or {}
    stats = item_stats(
        store.observations(retailer, product_id),
        store.sale_episodes(retailer, product_id, limit=500),
        target_price=item.get("target_price"),
        threshold=item.get("discount_threshold"),
        now=now,
    )
    store.put_item_stats(retailer, product_id, stats, now=now)
    return stats
