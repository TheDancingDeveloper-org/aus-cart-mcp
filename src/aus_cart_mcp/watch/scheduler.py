"""Refresh runs, cart snapshots and the traffic-budget governor (CW-25).

The budget is enforced here, in code (docs/DESIGN.md § Traffic budget):

- no retailer traffic in the no-runs window (default 23:00–05:00 local);
- aus_cart_mcp.watch's own daily cap, counted from the ledger: a call that would
  exceed it is not started, and the run ends `partial`;
- the shared-tenant guard: `usage_summary` before each run, skip when the tenant
  has used more than `SHARED_CAP_FRACTION` of the gateway's daily cap;
- `Blocked` aborts the run at once, is never retried, and backs the next run off
  by `blocked_backoff_minutes`; a streak of failed runs raises an operator alert
  and halves the cadence until a run succeeds;
- each run reconciles the ledger against `usage_summary`, because aus-cart-mcp
  does not report per-call upstream counts.

Runs are keyed by `run_id` (one per schedule slot), so a slot runs once and a
crashed run resumes where it stopped. The clock and sleep are injectable so the
policy tests run on a fake clock.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aus_cart_mcp.watch import alerts, config, detect, tracking
from aus_cart_mcp.watch.policy import Policy, in_window
from aus_cart_mcp.watch.store import Store, parse_time, utcnow
from aus_cart_mcp.watch.types import AusCartClient, AusCartError, Blocked, SessionRequired, Unavailable

log = logging.getLogger(__name__)

FAILED = ("blocked", "unavailable", "error")
BATCH_SIZE = 20
PRICE_RUNS = ("refresh", "photos")
CART_RUNS = ("snapshot",)
PHOTOS_PER_RUN = 5

ClientFactory = Callable[[str | None], AusCartClient]


@dataclass(frozen=True)
class Slot:
    run_id: str
    at: datetime
    index: int  # 0-based position in the day; odd slots are dropped while the cadence is halved


@dataclass
class RunResult:
    run_id: str
    outcome: str  # ok | partial | skipped | blocked | unavailable | error
    note: str = ""
    observed: int = 0
    missing: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        store: Store,
        client_factory: ClientFactory,
        *,
        cart_client_factory: ClientFactory | None = None,
        retailer: str | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tz: str | None = None,
        daily_cap: int | None = None,
        shared_fraction: float | None = None,
        gateway_cap: int | None = None,
        after_refresh: Callable[[RunResult], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.client_factory = client_factory  # prices and search (anonymous tenant)
        self.cart_client_factory = cart_client_factory or client_factory  # cart reads (signed-in tenant)
        self.retailer = retailer or config.retailer()
        self.clock = clock
        self.sleep = sleep
        self.tz = ZoneInfo(tz or config.timezone())
        self.daily_cap = config.daily_upstream_cap() if daily_cap is None else daily_cap
        self.shared_fraction = config.shared_cap_fraction() if shared_fraction is None else shared_fraction
        self.gateway_cap = config.gateway_daily_cap() if gateway_cap is None else gateway_cap
        self.after_refresh = after_refresh

    # ── time helpers ──────────────────────────────────────────────────────

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.tz)

    def day_start(self, moment: datetime) -> datetime:
        local = self.local(moment)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def used_today(self, now: datetime) -> int:
        return self.store.upstream_since(self.retailer, self.day_start(now))

    def remaining_today(self, now: datetime) -> int:
        return max(0, self.daily_cap - self.used_today(now))

    def slots(self, day: date, policy: Policy) -> list[Slot]:
        """The day's refresh slots: the regular times plus the specials-day run, jittered, outside no-runs."""
        times = list(policy.refresh_clocks)
        if day.weekday() == policy.specials_day:
            times.append(policy.clock("specials_time"))
        out = []
        for clock in sorted(set(times)):
            name = f"{day.isoformat()}-{clock.strftime('%H%M')}"
            offset = random.Random(name).uniform(-policy.jitter_minutes, policy.jitter_minutes)
            at = datetime.combine(day, clock, tzinfo=self.tz) + timedelta(minutes=offset)
            if in_window(at.timetz().replace(tzinfo=None), policy.clock("no_runs_start"), policy.clock("no_runs_end")):
                continue
            out.append(Slot(f"refresh-{name}", at, len(out)))
        return out

    def quiet(self, now: datetime, policy: Policy) -> bool:
        local = self.local(now).time()
        return in_window(local, policy.clock("no_runs_start"), policy.clock("no_runs_end"))

    def failure_streak(self) -> list[dict]:
        """The latest consecutive failed refresh runs (skips do not break or extend a streak)."""
        streak = []
        for run in self.store.recent_runs(kind="refresh", limit=50):
            if run["outcome"] in (None, "skipped"):
                continue
            if run["outcome"] not in FAILED:
                break
            streak.append(run)
        return streak

    # ── governor ──────────────────────────────────────────────────────────

    def blocked_until(self, policy: Policy, kinds: tuple[str, ...] = PRICE_RUNS) -> datetime | None:
        """The end of the backoff after the latest blocked run of these kinds.

        Price runs (anonymous tenant) and cart runs (the owner's signed-in tenant) back off separately:
        a stale owner session being refused says nothing about anonymous price checks."""
        blocked = [
            parse_time(r["finished_at"])
            for r in self.store.recent_runs(limit=50)
            if r["outcome"] == "blocked" and r["finished_at"] and r["kind"] in kinds
        ]
        return max(blocked) + timedelta(minutes=policy.blocked_backoff_minutes) if blocked else None

    def precheck(self, now: datetime, policy: Policy, *, force: bool = False) -> str | None:
        """Why a run must not start (None when it may). `force` bypasses everything but a recent block."""
        until = self.blocked_until(policy)
        if until is not None and now < until:
            return f"backing off after a block until {self.local(until):%H:%M}"
        if force:
            return None
        if self.quiet(now, policy):
            return "no-runs window"
        if self.remaining_today(now) <= 0:
            return f"daily budget of {self.daily_cap} upstream requests used"
        return None

    # ── refresh ───────────────────────────────────────────────────────────

    async def refresh(self, *, run_id: str | None = None, force: bool = False, note: str = "") -> RunResult:
        now = self.clock()
        policy = Policy.load(self.store)
        run_id = self.store.start_run("refresh", run_id=run_id, now=now)
        if force:
            log.warning("refresh %s forced by the operator", run_id)
        reason = self.precheck(now, policy, force=force)
        if reason:
            return self._finish(RunResult(run_id, "skipped", reason), policy)
        result = RunResult(run_id, "ok", note)
        usage_before = usage_after = None
        try:
            async with self.client_factory(run_id) as client:
                usage_before = (await client.usage_summary(days=1)).upstream_requests
                limit = self.shared_fraction * self.gateway_cap
                if usage_before >= limit and not force:
                    result.outcome, result.note = "skipped", f"shared tenant at {usage_before}/{self.gateway_cap} today"
                else:
                    await self._poll(client, run_id, policy, result, force=force)
                try:
                    usage_after = (await client.usage_summary(days=1)).upstream_requests
                except AusCartError as exc:
                    log.warning("usage_summary after run %s failed: %s", run_id, exc)
        except Blocked as exc:
            result.outcome, result.note = "blocked", str(exc)
        except Unavailable as exc:
            result.outcome, result.note = "unavailable", str(exc)
        except AusCartError as exc:
            result.outcome, result.note = "error", str(exc)
        self._reconcile(run_id, usage_before, usage_after)
        self._finish(result, policy, usage_before=usage_before, usage_after=usage_after)
        if result.observed:
            alerts.evaluate(self.store, self.retailer, policy, now=self.clock(), tz=self.tz)
        if self.after_refresh is not None:
            await self.after_refresh(result)
        return result

    async def _poll(
        self, client: AusCartClient, run_id: str, policy: Policy, result: RunResult, *, force: bool
    ) -> None:
        done = {row["product_id"] for row in self.store.observations_in_run(run_id)}
        items = [i for i in self.store.list_tracked(retailer=self.retailer) if i["product_id"] not in done]
        items.sort(
            key=lambda i: (self.store.latest_observation(self.retailer, i["product_id"]) or {}).get("observed_at", "")
        )
        pending = [i["product_id"] for i in items]
        total = len(pending)
        names = {i["product_id"]: i["name"] for i in items}

        def budget_left() -> bool:
            return force or self.remaining_today(self.clock()) > 0

        batched = client.has_tool("get_products")
        if batched:
            for start in range(0, len(pending), BATCH_SIZE):
                if not budget_left():
                    break
                chunk = pending[start : start + BATCH_SIZE]
                try:
                    found = await client.get_products(chunk, retailer=self.retailer)
                except (Blocked, SessionRequired, Unavailable):
                    raise
                except AusCartError as exc:  # the batch tool misbehaving must not stop the run
                    log.warning("get_products failed, falling back to search: %s", exc)
                    pending, batched = pending[start:], False
                    break
                for product_id in chunk:
                    self._record(found.get(product_id), product_id, run_id, result)
        if not batched:
            searches = 0
            for product_id in pending:
                if not budget_left() or searches >= policy.max_searches_per_run:
                    break
                if searches:
                    await self.sleep(policy.search_spacing_seconds)
                found = await client.get_products([product_id], names=names, retailer=self.retailer, batch=False)
                searches += 1
                self._record(found.get(product_id), product_id, run_id, result)
        handled = result.observed + len(result.missing)
        if handled < total:
            result.outcome = "partial"
            result.note = f"budget stopped the run after {handled} of {total} items"

    def missing_photos(self) -> list[str]:
        have = self.store.has_images(self.retailer)
        return [i["product_id"] for i in self.store.list_tracked(retailer=self.retailer) if i["product_id"] not in have]

    async def photos(self, limit: int = PHOTOS_PER_RUN) -> RunResult:
        """Fetch photos for a few tracked items that have none: a run of its own, within the same budget."""
        now = self.clock()
        policy = Policy.load(self.store)
        run_id = self.store.start_run("photos", now=now)
        reason = self.precheck(now, policy)
        if reason:
            result = RunResult(run_id, "skipped", reason)
        else:
            result = RunResult(run_id, "ok")
            try:
                async with self.client_factory(run_id) as client:
                    tracker = tracking.Tracker(self.store, client, self.retailer)
                    for product_id in self.missing_photos()[:limit]:
                        if self.remaining_today(self.clock()) <= 0:
                            break
                        result.observed += await tracker.ensure_image(product_id)
                result.note = f"{result.observed} photos stored"
            except Blocked as exc:
                result.outcome, result.note = "blocked", str(exc)
            except AusCartError as exc:
                result.outcome, result.note = "error", str(exc)
        self.store.finish_run(run_id, result.outcome, note=result.note, now=self.clock())
        return result

    def _record(self, product, product_id: str, run_id: str, result: RunResult) -> None:
        if product is None:
            result.missing.append(product_id)
            return
        tracking.observe(self.store, self.retailer, product, run_id=run_id, now=self.clock())
        result.observed += 1

    def _reconcile(self, run_id: str, before: int | None, after: int | None) -> None:
        if before is None or after is None:
            return
        counted = sum(r["upstream_requests"] for r in self.store.ledger(run_id=run_id))
        extra = (after - before) - counted
        if extra > 0:
            self.store.record_call(self.retailer, "reconcile", extra, "ok", run_id=run_id, now=self.clock())

    def _finish(self, result: RunResult, policy: Policy, **usage) -> RunResult:
        note = result.note
        if result.missing:
            note = f"{note}; not found: {', '.join(result.missing)}".lstrip("; ")
        self.store.finish_run(result.run_id, result.outcome, note=note, now=self.clock(), **usage)
        log.info("refresh %s %s %s", result.run_id, result.outcome, note)
        now = self.clock()
        if result.outcome in FAILED:
            streak = self.failure_streak()
            if len(streak) >= policy.failure_streak_alert:
                first = streak[-1]["id"]
                alerts.operator(
                    self.store,
                    f"operator:streak:{first}",
                    f"{len(streak)} refresh runs in a row failed; latest: {result.outcome} ({result.note}). "
                    "The cadence is halved until a run succeeds.",
                    now=now,
                )
        if result.outcome == "skipped" and ("budget" in result.note or "shared tenant" in result.note):
            today = self.local(now).date().isoformat()
            ok_today = any(
                r["outcome"] in ("ok", "partial")
                and self.local(parse_time(r["started_at"])).date().isoformat() == today
                for r in self.store.recent_runs(kind="refresh", limit=10)
            )
            if not ok_today:
                alerts.operator(
                    self.store, f"operator:budget:{today}", f"No price refresh today: {result.note}.", now=now
                )
        return result

    # ── cart snapshots (CW-24) ────────────────────────────────────────────

    def snapshot_due(self, now: datetime, policy: Policy) -> bool:
        if not policy.scheduled_snapshots or self.quiet(now, policy) or self.remaining_today(now) <= 0:
            return False
        until = self.blocked_until(policy, CART_RUNS)
        if until is not None and now < until:
            return False
        last = self.store.last_cart_snapshot(self.retailer)
        last_try = next(iter(self.store.recent_runs(kind="snapshot", limit=1)), None)
        reference = max(
            [parse_time(x) for x in (last and last["taken_at"], last_try and last_try["started_at"]) if x],
            default=None,
        )
        if reference is None:
            return True
        minutes = policy.snapshot_minutes_active if last and last["items"] else policy.snapshot_minutes_idle
        return now - reference >= timedelta(minutes=minutes)

    async def snapshot(self) -> RunResult:
        now = self.clock()
        policy = Policy.load(self.store)
        run_id = self.store.start_run("snapshot", now=now)
        result = RunResult(run_id, "ok")
        try:
            async with self.cart_client_factory(run_id) as client:
                cart = await client.get_cart(retailer=self.retailer)
            result.note = detect.process_snapshot(self.store, self.retailer, cart, policy, now=now)
        except SessionRequired as exc:
            result.outcome, result.note = "session_required", str(exc)
            alerts.operator(
                self.store,
                f"operator:session:{self.local(now).date().isoformat()}",
                f"{alerts.SESSION_EXPIRED_HELP} ({exc})",
                now=now,
            )
        except Blocked as exc:
            result.outcome, result.note = "blocked", str(exc)
        except AusCartError as exc:
            result.outcome, result.note = "error", str(exc)
        self.store.finish_run(run_id, result.outcome, note=result.note, now=self.clock())
        return result

    # ── nightly backup ────────────────────────────────────────────────────

    def backup_due(self, now: datetime, policy: Policy, directory: str) -> bool:
        if not directory:
            return False
        local = self.local(now)
        clock = policy.clock("backup_time")
        if (local.hour, local.minute) < (clock.hour, clock.minute):
            return False
        last = next(iter(self.store.recent_runs(kind="backup", limit=1)), None)
        return last is None or self.local(parse_time(last["started_at"])).date() != local.date()

    def backup(self, directory: str, keep: int) -> RunResult:
        now = self.clock()
        run_id = self.store.start_run("backup", now=now)
        try:
            path = self.store.backup(directory, keep=keep, now=now)
            result = RunResult(run_id, "ok", path.name)
        except Exception as exc:  # disk full, permissions: report, do not crash the loop
            result = RunResult(run_id, "error", f"{type(exc).__name__}: {exc}")
            alerts.operator(
                self.store,
                f"operator:backup:{self.local(now).date().isoformat()}",
                f"Backup failed: {result.note}",
                now=now,
            )
        self.store.finish_run(run_id, result.outcome, note=result.note, now=self.clock())
        return result

    # ── the loop ──────────────────────────────────────────────────────────

    def due_slot(self, now: datetime, policy: Policy) -> Slot | None:
        """The latest slot that is due and has not run (older missed slots are skipped, not caught up)."""
        today = self.local(now).date()
        due = [s for s in self.slots(today, policy) if s.at <= now and now - s.at < timedelta(hours=6)]
        if not due:
            return None
        slot = due[-1]
        run = self.store.get_run(slot.run_id)
        if run is not None and run["finished_at"]:
            return None
        return slot

    async def tick(self) -> list[RunResult]:
        """One pass: the due refresh slot, a due snapshot, and alert delivery bookkeeping."""
        now = self.clock()
        policy = Policy.load(self.store)
        results = []
        slot = self.due_slot(now, policy)
        if slot is not None:
            if len(self.failure_streak()) >= policy.failure_streak_alert and slot.index % 2 == 1:
                run_id = self.store.start_run("refresh", run_id=slot.run_id, now=now)
                results.append(self._finish(RunResult(run_id, "skipped", "cadence halved after failures"), policy))
            else:
                results.append(await self.refresh(run_id=slot.run_id))
                if results[-1].outcome in ("ok", "partial") and config.fetch_photos() and self.missing_photos():
                    results.append(await self.photos())
        if self.snapshot_due(self.clock(), policy):
            results.append(await self.snapshot())
        if self.backup_due(self.clock(), policy, config.backup_dir()):
            results.append(self.backup(config.backup_dir(), config.backup_keep()))
        return results

    async def run_forever(self, interval: float = 60.0) -> None:
        log.info("scheduler started (retailer %s, cap %s/day)", self.retailer, self.daily_cap)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await self.sleep(interval)
