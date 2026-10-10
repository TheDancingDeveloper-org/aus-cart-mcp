"""Nominating items to track (CW-23), and recording an observation of a product.

Used by the CLI, the MCP server and the web UI. A query returns candidates; a
product id or a Woolworths product URL resolves to one product; tracking the
same product twice updates it. Lookups go through aus-cart-mcp and are counted
in the budget ledger by the client they are given.

Cache first: every product seen keeps its latest details, and a search is
remembered, for `AUS_CARTWATCH_PRODUCT_CACHE_HOURS` (default 24). Re-showing a
search, or tracking from its results, does not ask Woolworths again; scheduled
refreshes always do. A tracked item's photo is fetched once and stored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aus_cartwatch import analytics, config
from aus_cartwatch.auscart import AusCartClient, AusCartError, Cart, Product
from aus_cartwatch.store import Store, parse_time

log = logging.getLogger(__name__)

PRODUCT_URL = re.compile(r"/shop/productdetails/(\d+)")
PRODUCT_ID = re.compile(r"^\d{1,12}$")


def parse_reference(text: str) -> tuple[str, str]:
    """`("id", "888140")` for a product id or product URL, else `("query", text)`."""
    text = text.strip()
    if PRODUCT_ID.match(text):
        return "id", str(int(text))
    match = PRODUCT_URL.search(text)
    if match:
        return "id", str(int(match.group(1)))
    return "query", text


def observe(
    store: Store,
    retailer: str,
    product: Product,
    *,
    source: str = "refresh",
    run_id: str | None = None,
    now: datetime | None = None,
) -> analytics.EpisodeChange:
    """Record one observation of a product, fold it into sale episodes and refresh the item's stats."""
    store.remember_product(retailer, product, now=now)
    store.add_observation(
        retailer,
        product.product_id,
        price=product.price,
        was_price=product.was_price,
        on_special=product.on_special,
        available=product.available,
        unit_price_value=product.unit_price_value,
        unit_price_unit=product.unit_price_unit,
        source=source,
        run_id=run_id,
        now=now,
    )
    change = analytics.update_sale_episode(store, retailer, product.product_id, now=now)
    analytics.refresh_item_stats(store, retailer, product.product_id, now=now)
    return change


@dataclass
class TrackResult:
    item: dict | None = None
    created: bool = False
    candidates: list[Product] = field(default_factory=list)
    message: str = ""


class Tracker:
    def __init__(self, store: Store, client: AusCartClient | None, retailer: str, *, cache_hours: float | None = None):
        self.store, self.client, self.retailer = store, client, retailer
        self.cache_ttl = timedelta(hours=config.product_cache_hours() if cache_hours is None else cache_hours)
        self.from_cache = False  # whether the last search/resolve was answered locally

    async def search(self, query: str, *, limit: int = 10) -> list[Product]:
        cached = self.store.cached_search(self.retailer, query, max_age=self.cache_ttl)
        if cached is not None:
            self.from_cache = True
            return cached[:limit]
        self.from_cache = False
        found = await self.client.search_products(query, limit=limit, retailer=self.retailer)
        for product in found:
            self.store.remember_product(self.retailer, product)
        self.store.cache_search(self.retailer, query, [p.product_id for p in found])
        return found

    async def lookup(self, product_id: str) -> Product | None:
        """One product by id: the cache if fresh, else aus-cart-mcp."""
        cached = self.store.cached_product(self.retailer, product_id, max_age=self.cache_ttl)
        if cached is not None:
            self.from_cache = True
            return cached
        self.from_cache = False
        known = self.store.get_product(self.retailer, product_id)
        names = {product_id: known["name"]} if known and known["name"] else None
        found = await self.client.get_products([product_id], names=names, retailer=self.retailer)
        for product in found.values():
            self.store.remember_product(self.retailer, product)
        return found.get(product_id)

    async def resolve(self, reference: str, *, limit: int = 10) -> list[Product]:
        kind, value = parse_reference(reference)
        if kind == "id":
            product = await self.lookup(value)
            return [product] if product else []
        return await self.search(value, limit=limit)

    async def ensure_image(self, product_id: str) -> bool:
        """Fetch and store a product's photo once. Best effort: a failure leaves it for the next refresh."""
        if not config.fetch_photos() or self.client is None:
            return False
        if self.store.get_image(self.retailer, product_id) is not None:
            return False
        if not self.client.has_tool("get_product_image"):
            return False
        try:
            data, content_type = await self.client.get_product_image(product_id, retailer=self.retailer)
        except AusCartError as exc:
            log.info("no photo for %s yet: %s", product_id, exc)
            return False
        self.store.put_image(self.retailer, product_id, data, content_type)
        return True

    def _snapshot_time(self, product_id: str) -> datetime | None:
        row = self.store.get_product(self.retailer, product_id)
        return parse_time(row["snapshot_at"]) if row and row.get("snapshot_at") else None

    async def track(
        self,
        reference: str,
        *,
        label: str | None = None,
        target_price: float | None = None,
        threshold: float | None = None,
        now: datetime | None = None,
    ) -> TrackResult:
        """Track by id or URL; a query returns candidates to choose from instead."""
        kind, value = parse_reference(reference)
        products = await self.resolve(reference)
        if kind == "query":
            return TrackResult(candidates=products, message=f"{len(products)} candidates; track one by its id")
        if not products:
            return TrackResult(message=f"product {value} was not found at {self.retailer}")
        observed_at = self._snapshot_time(value) if self.from_cache else now
        latest = self.store.latest_observation(self.retailer, value)
        already_seen = (
            self.from_cache and latest is not None and observed_at is not None
            and parse_time(latest["observed_at"]) >= observed_at
        )  # fmt: skip
        result = self.track_product(
            products[0],
            label=label,
            target_price=target_price,
            threshold=threshold,
            now=observed_at,
            observe_first=not already_seen,
        )
        await self.ensure_image(value)
        return result

    async def track_many(self, product_ids: list[str]) -> list[TrackResult]:
        """Track several products at once (e.g. ticked in search results); cached ones cost nothing."""
        results = []
        for product_id in dict.fromkeys(str(p).strip() for p in product_ids if str(p).strip()):
            results.append(await self.track(product_id))
        return results

    def track_product(
        self,
        product: Product,
        *,
        source: str = "manual",
        label: str | None = None,
        target_price: float | None = None,
        threshold: float | None = None,
        now: datetime | None = None,
        observe_first: bool = True,
    ) -> TrackResult:
        """Track a product already looked up, recording that lookup as its first observation."""
        if observe_first:
            observe(self.store, self.retailer, product, source=f"track:{source}", now=now)
        else:
            self.store.remember_product(self.retailer, product, now=now)
        item, created = self.store.track(
            self.retailer,
            product.product_id,
            source=source,
            label=label,
            target_price=target_price,
            threshold=threshold,
            now=now,
        )
        analytics.refresh_item_stats(self.store, self.retailer, product.product_id, now=now)
        verb = "now tracking" if created else "updated"
        return TrackResult(item=item, created=created, message=f"{verb} {product.name} ({product.product_id})")

    def track_cart_lines(self, product_ids: list[str], *, now: datetime | None = None) -> list[TrackResult]:
        """Track lines of the last cart snapshot, with no retailer call (the snapshot is the observation)."""
        snapshot = self.store.last_cart_snapshot(self.retailer)
        lines = {i["product_id"]: i for i in (snapshot or {}).get("items", [])}
        results = []
        for product_id in dict.fromkeys(product_ids):
            line = lines.get(product_id)
            if line is None:
                results.append(TrackResult(message=f"{product_id} is not in the last cart snapshot"))
                continue
            cached = self.store.cached_product(self.retailer, product_id, max_age=self.cache_ttl)
            product = cached or Product(product_id=product_id, name=line.get("name") or "", price=line.get("price"))
            results.append(self.track_product(product, source="cart", now=now))
        return results

    async def track_from_cart(self, *, now: datetime | None = None, cart: Cart | None = None) -> list[TrackResult]:
        """Track every cart line not already tracked (`source=cart`). The cart read is the first observation."""
        cart = cart or await self.client.get_cart(retailer=self.retailer)
        results = []
        for line in cart.items:
            if self.store.tracked_item(self.retailer, line.product_id):
                continue
            product = Product(product_id=line.product_id, name=line.name, price=line.price)
            results.append(self.track_product(product, source="cart", now=now))
        for result in results:
            await self.ensure_image(result.item["product_id"])
        return results


def untrack(store: Store, retailer: str, product_id: str) -> bool:
    return store.archive(retailer, product_id)


def set_target_price(store: Store, retailer: str, product_id: str, price: float | None) -> bool:
    if price is not None and price <= 0:
        raise ValueError("target price must be positive")
    changed = store.update_tracked(retailer, product_id, target_price=price)
    if changed:
        analytics.refresh_item_stats(store, retailer, product_id)
    return changed


def set_threshold(store: Store, retailer: str, product_id: str, threshold: float | None) -> bool:
    if threshold is not None and not 0 < threshold < 1:
        raise ValueError("threshold is a fraction between 0 and 1, e.g. 0.3")
    changed = store.update_tracked(retailer, product_id, discount_threshold=threshold)
    if changed:
        analytics.refresh_item_stats(store, retailer, product_id)
    return changed


def list_tracked(store: Store, retailer: str | None = None, *, include_archived: bool = False) -> list[dict]:
    """Tracked items with their materialised stats."""
    items = store.list_tracked(retailer=retailer, include_archived=include_archived)
    for item in items:
        item["stats"] = store.item_stats(item["retailer"], item["product_id"]) or {}
    return items
