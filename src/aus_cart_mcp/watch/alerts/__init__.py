"""Alert rules and delivery (CW-27).

Rules run after each refresh that observed something:

- `on_sale`: a sale episode is open on a tracked item. Batched into the daily
  digest unless `AUS_CARTWATCH_IMMEDIATE_ON_SALE` is set.
- `heavy_discount`: the episode's max discount reached the item's threshold.
  Immediate; fires once per episode even when `on_sale` already did (an upgrade),
  and an unsent `on_sale` for the same episode is folded into it.
- `target_price`: the price is at or under the item's target, once per episode
  (or once per target value outside a sale).
- `operator`: failed-run streaks, an expired retailer session, a day without a
  refresh. Immediate, and the only kind delivered in quiet hours.

Every alert is stored (unique `dedupe_key`) before it is sent, so a failed send
is retried on the next pass and never duplicated. Snoozed items raise nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from aus_cart_mcp.watch import config
from aus_cart_mcp.watch.analytics import DEFAULT_THRESHOLD
from aus_cart_mcp.watch.policy import Policy, in_window
from aus_cart_mcp.watch.store import Store, parse_time, utcnow

log = logging.getLogger(__name__)

SESSION_EXPIRED_HELP = (
    "Woolworths session expired: reconnect in the myaiagent app, More → MCP servers → grocery → Retailer accounts"
)
IMMEDIATE = ("operator", "heavy_discount", "target_price")


def _tz(tz: ZoneInfo | None) -> ZoneInfo:
    return tz or ZoneInfo(config.timezone())


def deliverable_at(moment: datetime, policy: Policy, tz: ZoneInfo | None = None) -> datetime:
    """`moment`, or the end of quiet hours when it falls inside them."""
    local = moment.astimezone(_tz(tz))
    start, end = policy.clock("quiet_start"), policy.clock("quiet_end")
    if not in_window(local.time(), start, end):
        return moment
    release = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if release <= local:
        release += timedelta(days=1)
    return release


def next_digest(moment: datetime, policy: Policy, tz: ZoneInfo | None = None) -> datetime:
    local = moment.astimezone(_tz(tz))
    clock = policy.clock("digest_time")
    at = local.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    if at < local:
        at += timedelta(days=1)
    return deliverable_at(at, policy, tz)


def operator(store: Store, key: str, message: str, *, now: datetime | None = None) -> int | None:
    """Raise an operator alert once per `key`."""
    alert_id = store.add_alert(key, "operator", {"text": message}, now=now)
    if alert_id:
        log.warning("operator alert: %s", message)
    return alert_id


def _payload(item: dict, stats: dict, episode: dict | None) -> dict:
    return {
        "item_id": item["id"],
        "product_id": item["product_id"],
        "name": item.get("label") or item.get("name") or item["product_id"],
        "size": item.get("size") or "",
        "url": item.get("url") or "",
        "price": stats.get("price"),
        "was_price": stats.get("was_price"),
        "baseline": stats.get("baseline"),
        "discount": round(episode["max_discount"], 4) if episode else stats.get("discount"),
        "unit_price": (
            f"${stats['unit_price_value']:.2f} / {stats['unit_price_unit']}" if stats.get("unit_price_value") else ""
        ),
        "on_special": stats.get("on_special"),
        "target_price": item.get("target_price"),
    }


def evaluate(
    store: Store,
    retailer: str,
    policy: Policy | None = None,
    *,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> list[int]:
    """Apply the rules to every tracked item; return the ids of alerts created."""
    policy = policy or Policy.load(store)
    now = now or utcnow()
    immediate = deliverable_at(now, policy, tz)
    created = []
    for item in store.list_tracked(retailer=retailer):
        snoozed = parse_time(item.get("snoozed_until"))
        if snoozed and snoozed > now:
            continue
        stats = store.item_stats(retailer, item["product_id"]) or {}
        if stats.get("price") is None or not stats.get("available", True):
            continue
        episode = store.open_sale_episode(retailer, item["product_id"])
        payload = _payload(item, stats, episode)
        threshold = item.get("discount_threshold") or policy.discount_threshold or DEFAULT_THRESHOLD
        base = f"{retailer}:{item['product_id']}"
        if episode is not None:
            due = immediate if config.immediate_on_sale() else next_digest(now, policy, tz)
            created.append(
                store.add_alert(
                    f"on_sale:{base}:{episode['id']}",
                    "on_sale",
                    payload,
                    retailer=retailer,
                    product_id=item["product_id"],
                    episode_id=episode["id"],
                    due_at=due,
                    now=now,
                )
            )
            if stats.get("baseline") and episode["max_discount"] >= threshold:
                created.append(
                    store.add_alert(
                        f"heavy_discount:{base}:{episode['id']}",
                        "heavy_discount",
                        {**payload, "threshold": threshold},
                        retailer=retailer,
                        product_id=item["product_id"],
                        episode_id=episode["id"],
                        due_at=immediate,
                        now=now,
                    )
                )
        if stats.get("target_met"):
            scope = episode["id"] if episode else f"regular:{item['target_price']}"
            created.append(
                store.add_alert(
                    f"target_price:{base}:{scope}",
                    "target_price",
                    payload,
                    retailer=retailer,
                    product_id=item["product_id"],
                    episode_id=episode["id"] if episode else None,
                    due_at=immediate,
                    now=now,
                )
            )
    return [a for a in created if a]


# ── delivery ──────────────────────────────────────────────────────────────


@dataclass
class Message:
    kind: str
    text: str  # plain text; notifiers may format it
    alerts: list[dict]
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, action), action e.g. "add:12:1"


class Notifier(Protocol):
    name: str

    async def send(self, message: Message) -> None: ...


def _money(value) -> str:
    return f"${value:.2f}" if isinstance(value, int | float) else "?"


def describe(alert: dict) -> str:
    p = alert["payload"]
    if alert["kind"] == "operator":
        return f"⚠️ aus_cartwatch: {p['text']}"
    name = f"{p['name']} {p.get('size') or ''}".strip()
    reference = p.get("was_price") if (p.get("was_price") or 0) > (p.get("price") or 0) else p.get("baseline")
    parts = [f"{name}: {_money(p.get('price'))}"]
    if reference:
        parts.append(f"(was {_money(reference)}, {round((p.get('discount') or 0) * 100)}% off)")
    if p.get("unit_price"):
        parts.append(f"· {p['unit_price']}")
    headline = {
        "on_sale": "On sale",
        "heavy_discount": "🔥 Heavy discount",
        "target_price": "🎯 Target price reached",
    }.get(alert["kind"], alert["kind"])
    if alert["kind"] == "target_price" and p.get("target_price"):
        parts.append(f"· target {_money(p['target_price'])}")
    line = f"{headline}: " + " ".join(parts)
    return f"{line}\n{p['url']}" if p.get("url") else line


def compose(alerts: list[dict]) -> list[Message]:
    """Group due alerts into messages: the on-sale digest, and one message for everything else."""
    messages = []
    digest = [a for a in alerts if a["kind"] == "on_sale"]
    for alert in alerts:
        if alert["kind"] == "on_sale":
            continue
        buttons = []
        if alert["kind"] != "operator":
            buttons = [
                ("Add 1 to cart", f"add:{alert['id']}:1"),
                ("Add 2", f"add:{alert['id']}:2"),
                ("Snooze 4 weeks", f"snooze:{alert['id']}"),
            ]
        messages.append(Message(alert["kind"], describe(alert), [alert], buttons))
    if digest:
        lines = [f"🛒 {len(digest)} tracked item{'s' if len(digest) != 1 else ''} on sale:"]
        lines += [describe(a).removeprefix("On sale: ") for a in digest]
        buttons = [(f"+1 {a['payload']['name'][:24]}", f"add:{a['id']}:1") for a in digest[:8]]
        messages.append(Message("digest", "\n\n".join(lines), digest, buttons))
    return messages


async def dispatch(store: Store, notifiers: list[Notifier], *, now: datetime | None = None) -> int:
    """Send every due alert; returns how many were delivered. Failures stay pending for the next pass."""
    now = now or utcnow()
    due = store.due_alerts(now=now)
    if not due:
        return 0
    # An on-sale alert whose episode already has a heavy-discount alert is folded into that upgrade.
    heavy_episodes = {a["episode_id"] for a in due if a["kind"] == "heavy_discount"}
    for alert in [a for a in due if a["kind"] == "on_sale" and a["episode_id"] in heavy_episodes]:
        store.mark_alert_sent(alert["id"], "superseded", now=now)
        due.remove(alert)
    if not notifiers:
        log.warning("%d alerts due but no notifier is configured", len(due))
        return 0
    delivered = 0
    for message in compose(due):
        sent_by, errors = [], []
        for notifier in notifiers:
            try:
                await notifier.send(message)
                sent_by.append(notifier.name)
            except Exception as exc:  # one notifier failing must not stop the others
                errors.append(f"{notifier.name}: {type(exc).__name__}: {exc}"[:200])
                log.warning("alert delivery via %s failed: %s", notifier.name, exc)
        for alert in message.alerts:
            if sent_by:
                store.mark_alert_sent(alert["id"], ",".join(sent_by), now=now)
                delivered += 1
            else:
                store.mark_alert_failed(alert["id"], "; ".join(errors))
    return delivered
