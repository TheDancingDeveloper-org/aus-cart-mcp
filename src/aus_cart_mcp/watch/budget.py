"""The traffic budget: every aus-cart-mcp call is written to the ledger, and the governor reads it back.

`ledger_hook` binds an `AusCartClient` to the ledger. The governor itself (daily
cap, shared-tenant guard, blocked backoff) is in `aus_cart_mcp.watch.scheduler`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from aus_cart_mcp.watch import config
from aus_cart_mcp.watch.store import Store, utcnow
from aus_cart_mcp.watch.types import AusCartClient, OnCall


def ledger_hook(
    store: Store, retailer: str, run_id: str | None = None, *, clock: Callable[[], datetime] = utcnow
) -> OnCall:
    def record(tool: str, upstream_requests: int, outcome: str) -> None:
        store.record_call(retailer, tool, upstream_requests, outcome, run_id=run_id, now=clock())

    return record


def client(
    store: Store,
    retailer: str,
    run_id: str | None = None,
    *,
    clock: Callable[[], datetime] = utcnow,
    purpose: str = "cart",
    **kwargs,
) -> AusCartClient:
    """An aus-cart-mcp client whose every call lands in the budget ledger.

    `purpose="prices"` uses the anonymous prices tenant; `"cart"` the owner's signed-in tenant."""
    if purpose == "prices" and "key" not in kwargs:
        kwargs["key"] = config.aus_cart_prices_key()
    return AusCartClient(on_call=ledger_hook(store, retailer, run_id, clock=clock), **kwargs)
