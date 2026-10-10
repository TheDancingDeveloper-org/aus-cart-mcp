"""Which product layers this process runs.

One image ships every layer. ``AUS_CART_MCP_FEATURES`` selects them at runtime:

- ``core`` — search and the customer's own cart (always on; the 0.2 behaviour);
- ``watch`` — price tracking, history, alerts (off unless named);
- ``ui`` — the optional cartwatch pages (off unless named).

Sub-flags sit in the same list and do nothing until their layer exists:
``telegram`` (watch's Telegram notifier) and ``receipts`` (receipt OCR).

``core`` may not import ``watch`` or ``ui``. ``watch`` may not import ``ui``.
"""

from __future__ import annotations

from aus_cart_mcp import config

LAYERS = ("core", "watch", "ui")
SUBFLAGS = ("telegram", "receipts")
KNOWN = LAYERS + SUBFLAGS

DEFAULT = "core"


class FeaturesError(ValueError):
    """``AUS_CART_MCP_FEATURES`` names something this build does not understand."""


class Features:
    def __init__(self, enabled: frozenset[str]):
        self.enabled = enabled

    def has(self, name: str) -> bool:
        return name in self.enabled

    @property
    def layers(self) -> list[str]:
        return [name for name in LAYERS if name in self.enabled]

    def to_dict(self) -> dict:
        return {"layers": self.layers, "flags": sorted(self.enabled & set(SUBFLAGS))}


def parse(raw: str) -> Features:
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = sorted({name for name in names if name not in KNOWN})
    if unknown:
        raise FeaturesError(f"unknown feature {', '.join(unknown)}; known: {', '.join(KNOWN)}")
    enabled = set(names) or {"core"}
    enabled.add("core")
    if "ui" in enabled:
        enabled.add("watch")
    return Features(frozenset(enabled))


def load() -> Features:
    return parse(config.env("FEATURES", DEFAULT))
