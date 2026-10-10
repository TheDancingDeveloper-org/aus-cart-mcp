"""Household preferences, stored in the `settings` table with these defaults.

The owner changes them from the web UI (`/settings`); deployment facts and
secrets stay in the environment (`aus_cart_mcp.watch.config`).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import time

from aus_cart_mcp.watch.store import Store


def _clock(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


@dataclass(frozen=True)
class Policy:
    # One refresh a day keeps Woolworths traffic low; prices mostly change with the weekly specials
    # (Wednesday), which the morning run already sees.
    refresh_times: str = "06:30"  # local times, comma-separated
    specials_day: int = -1  # an extra run on this weekday (Monday = 0, Wednesday = 2); -1 = none
    specials_time: str = "09:00"
    jitter_minutes: int = 20
    no_runs_start: str = "23:00"  # no retailer traffic in this window
    no_runs_end: str = "05:00"
    # Scheduled cart snapshots (1) or only on demand from the Cart page (0). Off while the owner's
    # Woolworths login lasts an hour (WI-800): a scheduled read almost always meets an expired session.
    scheduled_snapshots: int = 0
    snapshot_minutes_active: int = 60  # cart snapshots while the cart is non-empty
    snapshot_minutes_idle: int = 360
    episode_idle_days: int = 7  # a cart unchanged this long closes its shop episode
    search_spacing_seconds: float = 2.0  # on top of the gateway's own spacing, for the search fallback
    max_searches_per_run: int = 25
    blocked_backoff_minutes: int = 120
    failure_streak_alert: int = 3
    discount_threshold: float = 0.30
    quiet_start: str = "21:00"  # alerts other than operator alerts wait until quiet_end
    quiet_end: str = "07:00"
    digest_time: str = "07:30"
    backup_time: str = "03:30"  # nightly SQLite backup, when AUS_CARTWATCH_BACKUP_DIR is set

    @classmethod
    def load(cls, store: Store) -> Policy:
        stored = store.settings()
        values = {}
        for f in fields(cls):
            raw = stored.get(f"policy.{f.name}")
            if raw is None:
                continue
            try:
                values[f.name] = type(getattr(cls, f.name))(raw)
            except ValueError:
                continue
        return cls(**values)

    @staticmethod
    def save(store: Store, **values: str) -> None:
        names = {f.name: f for f in fields(Policy)}
        for name, value in values.items():
            if name not in names:
                raise ValueError(f"unknown setting {name}")
            kind = type(getattr(Policy, name))
            parsed = kind(value)  # raises ValueError on a bad value
            if name.endswith(("_time", "_start", "_end")):
                _clock(str(value))
            if name == "refresh_times":
                [_clock(t) for t in str(value).split(",") if t.strip()]
            store.set_setting(f"policy.{name}", str(parsed))

    @property
    def refresh_clocks(self) -> list[time]:
        return [_clock(t) for t in self.refresh_times.split(",") if t.strip()]

    def clock(self, name: str) -> time:
        return _clock(getattr(self, name))


def in_window(moment: time, start: time, end: time) -> bool:
    """Whether a local time falls in [start, end), where the window may cross midnight."""
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end
