"""Adding to the owner's cart (CW-28): one function behind the alert buttons, the UI, the CLI and MCP.

Only known products (tracked or observed before) can be added, so a typo cannot
put an arbitrary stockcode in the cart; quantities are capped. The add is
confirmed by re-reading the cart. An expired session produces the reconnect
instructions and one operator alert per day; a block stops at once. Nothing is
retried. There is no checkout: the owner reviews and pays on the retailer's site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aus_cartwatch import alerts, config
from aus_cartwatch.auscart import AusCartClient, AusCartError, Blocked, SessionRequired
from aus_cartwatch.store import Store, iso, utcnow

SNOOZE_DAYS = 28


@dataclass(frozen=True)
class CartOutcome:
    ok: bool
    outcome: str  # ok | unconfirmed | refused | session_required | blocked | error
    message: str
    quantity_in_cart: float | None = None


async def add(
    store: Store,
    client: AusCartClient,
    retailer: str,
    product_id: str,
    quantity: float = 1,
    *,
    reason: str,
    alert_id: int | None = None,
    now: datetime | None = None,
) -> CartOutcome:
    now = now or utcnow()
    product = store.get_product(retailer, str(product_id))
    cap = config.max_add_quantity()
    if product is None:
        outcome = CartOutcome(False, "refused", f"{product_id} is not a known product; track or search it first")
    elif not 0 < quantity <= cap:
        outcome = CartOutcome(False, "refused", f"quantity must be between 1 and {cap}")
    else:
        outcome = await _add(store, client, retailer, product, quantity, now=now)
    store.add_cart_action(retailer, str(product_id), quantity, reason, outcome.outcome, outcome.message, now=now)
    if alert_id is not None and outcome.ok:
        store.mark_alert_acted(alert_id, now=now)
    return outcome


async def _add(
    store: Store, client: AusCartClient, retailer: str, product: dict, quantity: float, *, now: datetime
) -> CartOutcome:
    name = product["name"] or product["product_id"]
    try:
        reported = (await client.add_to_cart({product["product_id"]: quantity}, retailer=retailer)).quantity_of(
            product["product_id"]
        )
        confirmed = (await client.get_cart(retailer=retailer)).quantity_of(product["product_id"])
    except SessionRequired:
        day = now.astimezone(ZoneInfo(config.timezone())).date().isoformat()
        alerts.operator(store, f"operator:session:{day}", alerts.SESSION_EXPIRED_HELP, now=now)
        return CartOutcome(False, "session_required", alerts.SESSION_EXPIRED_HELP)
    except Blocked as exc:
        return CartOutcome(False, "blocked", f"Woolworths is refusing requests right now; try later ({exc})")
    except AusCartError as exc:
        return CartOutcome(False, "error", f"could not add {name}: {exc}")
    if confirmed >= quantity:
        return CartOutcome(True, "ok", f"Added {quantity:g} × {name}; {confirmed:g} in the cart now", confirmed)
    return CartOutcome(
        False,
        "unconfirmed",
        f"Asked to add {quantity:g} × {name}, but the cart shows {confirmed:g} (aus-cart-mcp reported {reported:g})",
    )


def snooze(
    store: Store, retailer: str, product_id: str, *, days: int = SNOOZE_DAYS, now: datetime | None = None
) -> str:
    until = (now or utcnow()) + timedelta(days=days)
    store.update_tracked(retailer, product_id, snoozed_until=iso(until))
    return f"Snoozed for {days} days"
