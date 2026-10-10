"""The analytics definitions (docs/DESIGN.md § Price analytics), pinned on synthetic histories."""

from datetime import timedelta

import pytest

from aus_cartwatch import analytics
from aus_cartwatch.auscart import Product
from aus_cartwatch.tracking import observe
from tests.conftest import T0


def obs(day: int, price: float | None, *, special: bool = False, was: float | None = None, available: bool = True):
    return {
        "observed_at": (T0 + timedelta(days=day)).isoformat(),
        "price": price,
        "on_special": int(special),
        "was_price": was,
        "available": int(available),
    }


def history(*prices, special_from: int | None = None):
    return [obs(i, p, special=special_from is not None and i >= special_from) for i, p in enumerate(prices)]


NOW = T0 + timedelta(days=10)


def test_steady_price_has_baseline_and_no_discount():
    rows = history(4.0, 4.0, 4.0, 4.0)
    assert analytics.baseline(rows, now=NOW) == 4.0
    assert analytics.discount(4.0, 4.0) == 0.0
    assert not analytics.is_on_sale(rows[-1], 4.0)


def test_sparse_history_without_was_price_has_no_baseline():
    assert analytics.baseline(history(4.0, 2.0), now=NOW) is None
    assert analytics.baseline([], now=NOW) is None
    stats = analytics.item_stats(history(4.0, 2.0), [], now=NOW)
    assert stats["baseline"] is None and stats["discount"] == 0 and not stats["heavy"]


def test_was_price_wins_even_on_sparse_history():
    rows = [obs(0, 5.5, special=True, was=11.0)]
    assert analytics.baseline(rows, now=NOW) == 11.0
    assert analytics.discount(5.5, 11.0) == 0.5


def test_was_price_not_above_price_is_ignored():
    rows = [obs(0, 4.0), obs(1, 4.0), obs(2, 4.0, was=4.0)]
    assert analytics.baseline(rows, now=NOW) == 4.0


def test_half_price_week_is_heavy():
    rows = history(10.0, 10.0, 10.0, 5.0, special_from=3)
    base = analytics.baseline(rows, now=NOW)
    assert base == 10.0  # specials are excluded from the median
    assert analytics.discount(5.0, base) == 0.5
    stats = analytics.item_stats(rows, [], now=NOW)
    assert stats["heavy"] and stats["on_sale"] and stats["change"] == -5.0


def test_price_rise_is_not_a_discount():
    rows = history(4.0, 4.0, 4.0, 5.0)
    base = analytics.baseline(rows, now=NOW)
    assert analytics.discount(5.0, base) == 0.0
    assert not analytics.is_on_sale(rows[-1], base)


def test_all_special_history_falls_back_to_the_highest_price():
    rows = history(6.0, 5.0, 5.0, special_from=0)
    assert analytics.baseline(rows, now=NOW) == 6.0


def test_old_regular_prices_drop_out_of_the_window():
    rows = [obs(-100, 9.0), obs(-90, 9.0), obs(0, 4.0, special=True), obs(1, 4.0, special=True)]
    assert analytics.baseline(rows, now=NOW) == 9.0  # no regular price in 60 days: highest observed


def test_threshold_override_and_target():
    rows = history(10.0, 10.0, 10.0, 8.0)
    stats = analytics.item_stats(rows, [], threshold=0.15, target_price=8.0, now=NOW)
    assert stats["discount"] == 0.2 and stats["heavy"] and stats["target_met"]
    assert not analytics.item_stats(rows, [], now=NOW)["heavy"]


def test_windows_and_sale_cadence():
    rows = history(*[4.0] * 20)
    episodes = [
        {"started_at": (T0 + timedelta(days=d)).isoformat(), "low_price": low}
        for d, low in ((0, 2.0), (14, 2.5), (28, 2.0))
    ]
    stats = analytics.item_stats(rows, episodes, now=T0 + timedelta(days=19))
    assert stats["d7"] == {"min": 4.0, "max": 4.0, "median": 4.0}
    assert stats["sales"] == 3 and stats["avg_days_between_sales"] == 14.0 and stats["typical_sale_price"] == 2.0
    assert stats["last_sale"].startswith((T0 + timedelta(days=28)).date().isoformat())
    assert analytics.item_stats([], [], now=NOW)["d90"] is None


@pytest.mark.parametrize("bad", [None, 0, -1])
def test_discount_without_a_usable_baseline_is_zero(bad):
    assert analytics.discount(1.0, bad) == 0.0
    assert analytics.discount(None, 4.0) == 0.0


def test_sale_episode_lifecycle(store):
    def see(day, price, special=False, was=None):
        p = Product("1", "Coffee", price, was_price=was, on_special=special)
        return observe(store, "woolworths", p, now=T0 + timedelta(days=day))

    assert see(0, 10.0).kind == "none"
    see(1, 10.0)
    see(2, 10.0)
    started = see(3, 7.0, special=True)
    assert started.kind == "started"
    assert see(4, 5.0, special=True).kind == "continued"
    episode = store.get_sale_episode(started.episode_id)
    assert episode["low_price"] == 5.0 and episode["max_discount"] == 0.5 and episode["baseline"] == 10.0
    assert see(5, 10.0).kind == "ended"
    assert see(6, 9.5).kind == "none"  # 5% below baseline is not a sale
    assert see(7, 8.0).kind == "started"  # 20% below baseline without a special flag is
    assert analytics.update_sale_episode(store, "woolworths", "nothing").kind == "none"


def test_recorded_woolworths_half_price_is_heavy(store):
    """The >=30% rule on a Woolworths half-price special as aus-cart-mcp reports it (with CW-11's was_price)."""
    recorded = {
        "product_id": "209080",
        "name": "Moccona Classic Medium Roast Freeze Dried Instant Coffee",
        "price": 10.0,
        "was_price": 20.0,
        "unit_price": "$5.00 / 100g",
        "size": "200g",
        "available": True,
        "on_special": True,
        "url": "https://www.woolworths.com.au/shop/productdetails/209080",
    }
    store.upsert_product("woolworths", "209080", name="Moccona")
    store.track("woolworths", "209080")
    observe(store, "woolworths", Product.from_dict(recorded), now=T0)
    stats = store.item_stats("woolworths", "209080")
    assert stats["baseline"] == 20.0 and stats["discount"] == 0.5 and stats["heavy"]
    assert stats["unit_price_value"] == 5.0 and stats["unit_price_unit"] == "100g"
