"""The running service: one object that the HTTP app, the MCP tools, the UI and the CLI share.

It owns the store, the aus-cart-mcp client factory (every client writes to the
budget ledger), the scheduler, the notifiers and the alert-button handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime

from aus_cart_mcp.watch import alerts, budget, cart, config, detect, receipts, tracking
from aus_cart_mcp.watch.alerts.telegram import TelegramNotifier
from aus_cart_mcp.watch.alerts.webhook import WebhookNotifier
from aus_cart_mcp.watch.types import AusCartClient
from aus_cart_mcp.watch.scheduler import Scheduler
from aus_cart_mcp.watch.store import Store, utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 60.0


def default_notifiers() -> tuple[list[alerts.Notifier], TelegramNotifier | None]:
    notifiers: list[alerts.Notifier] = []
    telegram = None
    if config.telegram_token() and config.telegram_chat_ids():
        telegram = TelegramNotifier(config.telegram_token(), config.telegram_chat_ids())
        notifiers.append(telegram)
    if config.webhook_url():
        notifiers.append(WebhookNotifier(config.webhook_url()))
    return notifiers, telegram


class Service:
    def __init__(
        self,
        store: Store,
        *,
        retailer: str | None = None,
        client_factory: Callable[[str | None], AusCartClient] | None = None,
        cart_client_factory: Callable[[str | None], AusCartClient] | None = None,
        notifiers: list[alerts.Notifier] | None = None,
        telegram: TelegramNotifier | None = None,
        clock: Callable[[], datetime] = utcnow,
        receipt_extractor=None,
        **scheduler_options,
    ):
        self.store = store
        self.retailer = retailer or config.retailer()
        # Prices and search use the anonymous tenant; cart actions use the owner's signed-in tenant.
        self.client_factory = client_factory or (
            lambda run_id=None: budget.client(store, self.retailer, run_id, clock=clock, purpose="prices")
        )
        self.cart_client_factory = (
            cart_client_factory
            or client_factory
            or (lambda run_id=None: budget.client(store, self.retailer, run_id, clock=clock, purpose="cart"))
        )
        if notifiers is None:
            notifiers, telegram = default_notifiers()
        self.notifiers = notifiers
        self.telegram = telegram
        self.clock = clock
        self.scheduler = Scheduler(
            store,
            self.client_factory,
            cart_client_factory=self.cart_client_factory,
            retailer=self.retailer,
            clock=clock,
            **scheduler_options,
        )
        self._tasks: list[asyncio.Task] = []
        self._receipt_tasks: set[asyncio.Task] = set()
        self.receipt_extractor = receipt_extractor or receipts.extract

    # ── actions shared by every surface ───────────────────────────────────

    def client(self) -> AusCartClient:
        """A client for prices and search (the anonymous tenant)."""
        return self.client_factory(None)

    def cart_client(self) -> AusCartClient:
        """A client for the owner's cart (the signed-in tenant)."""
        return self.cart_client_factory(None)

    async def add_to_cart(
        self, product_id: str, quantity: float = 1, *, reason: str, alert_id: int | None = None
    ) -> cart.CartOutcome:
        async with self.cart_client() as client:
            return await cart.add(
                self.store, client, self.retailer, product_id, quantity, reason=reason, alert_id=alert_id
            )

    async def track(self, reference: str, **options) -> tracking.TrackResult:
        async with self.client() as client:
            return await tracking.Tracker(self.store, client, self.retailer).track(reference, **options)

    async def search(self, query: str, *, limit: int = 10) -> tuple[list, bool]:
        """Search, answered from the cache when the same search ran recently. Returns (products, from_cache)."""
        tracker = tracking.Tracker(self.store, None, self.retailer)
        cached = self.store.cached_search(self.retailer, query, max_age=tracker.cache_ttl)
        if cached is not None:
            return cached[:limit], True
        async with self.client() as client:
            tracker.client = client
            return await tracker.search(query, limit=limit), False

    async def track_many(self, product_ids: list[str]) -> list[tracking.TrackResult]:
        async with self.client() as client:
            return await tracking.Tracker(self.store, client, self.retailer).track_many(product_ids)

    async def track_from_cart(self) -> list[tracking.TrackResult]:
        async with self.cart_client() as client:
            return await tracking.Tracker(self.store, client, self.retailer).track_from_cart()

    def accept_candidate(self, product_id: str) -> tracking.TrackResult:
        # No retailer call: the candidate's last cart line is its first observation.
        return detect.accept_candidate(tracking.Tracker(self.store, None, self.retailer), product_id)  # type: ignore[arg-type]

    # ── receipts (CW-40) ──────────────────────────────────────────────────

    async def upload_receipt(self, data: bytes, content_type: str) -> int:
        """Store a receipt photo, read it (OCR) if it is new, and start matching its lines in the background."""
        upload = receipts.save(self.store, data, content_type)
        receipt = self.store.get_receipt(upload.receipt_id)
        if receipt["status"] in ("uploaded", "failed"):
            await receipts.read(self.store, upload.receipt_id, extractor=self.receipt_extractor)
        self.start_matching(upload.receipt_id)
        return upload.receipt_id

    def start_matching(self, receipt_id: int) -> None:
        if any(line["match_status"] == "pending" for line in self.store.receipt_lines(receipt_id)):
            task = asyncio.create_task(self.match_receipt(receipt_id), name=f"receipt-{receipt_id}")
            self._receipt_tasks.add(task)
            task.add_done_callback(self._receipt_tasks.discard)

    async def match_receipt(self, receipt_id: int) -> dict:
        async with self.client() as client:
            tracker = tracking.Tracker(self.store, client, self.retailer)
            return await receipts.match_receipt(
                self.store,
                tracker,
                receipt_id,
                retailer=self.retailer,
                budget_left=lambda: self.scheduler.remaining_today(self.clock()) > 0,
                sleep=self.scheduler.sleep,
            )

    async def handle_action(self, action: str) -> str:
        """An alert button: `add:<alert id>:<quantity>` or `snooze:<alert id>`."""
        verb, _, rest = action.partition(":")
        alert_id_text, _, quantity_text = rest.partition(":")
        try:
            alert = self.store.get_alert(int(alert_id_text))
        except ValueError:
            alert = None
        if alert is None or not alert.get("product_id"):
            return "That alert is no longer known"
        if verb == "add":
            try:
                quantity = float(quantity_text or 1)
            except ValueError:
                return "Bad quantity"
            outcome = await self.add_to_cart(
                alert["product_id"], quantity, reason=f"alert:{alert['id']}", alert_id=alert["id"]
            )
            return outcome.message
        if verb == "snooze":
            self.store.mark_alert_acted(alert["id"])
            return cart.snooze(self.store, alert["retailer"], alert["product_id"])
        return "Unknown action"

    # ── background work ───────────────────────────────────────────────────

    async def tick(self) -> None:
        await self.scheduler.tick()
        await alerts.dispatch(self.store, self.notifiers, now=self.clock())

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("service tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def start(self) -> None:
        """Start the scheduler loop and Telegram button polling (called from the app lifespan)."""
        if config.scheduler_enabled():
            self._tasks.append(asyncio.create_task(self._loop(), name="aus_cartwatch-scheduler"))
        if self.telegram is not None:
            self._tasks.append(
                asyncio.create_task(self.telegram.poll_forever(self.store, self.handle_action), name="telegram")
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
