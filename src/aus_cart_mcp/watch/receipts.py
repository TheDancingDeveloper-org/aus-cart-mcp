"""Receipt scans (CW-40): photo → line items (OpenRouter vision model) → Woolworths matches → tracking.

The flow:

1. **Upload.** The image is stored once by its SHA-256 (mode 600) and sent only to OpenRouter.
2. **Extraction.** A vision model returns strict JSON (store, date, total, lines). Long digit runs
   (card numbers, member numbers) are masked before anything is stored.
3. **Matching.** Each non-weighed line is matched in the background through the anonymous prices
   tenant, within the daily budget. A remembered alias costs nothing; a recent search is answered from
   the cache. The rules come from the hand-matched receipt of 2026-10-03 (docs/DESIGN.md § Receipt
   matching): name and size first, price only as a sanity check, no marketplace listings, and
   variable-weight packs and unavailable products shown as "check" rather than guessed.
4. **Confirmation.** The owner ticks lines. Ticked products are tracked (`source=receipt`), the choice is
   remembered as an alias, the receipt becomes a shop episode for suggestions, and the prices paid are
   recorded as observations (`source=receipt`).

Weighed produce lines (a fractional quantity priced per kg) are skipped: loose produce has no stable
online product to track.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from aus_cart_mcp.watch import analytics, config
from aus_cart_mcp.watch.store import Store, utcnow
from aus_cart_mcp.watch.tracking import Tracker
from aus_cart_mcp.watch.types import AusCartError, Blocked, Product

log = logging.getLogger(__name__)

MAX_BYTES = 12_000_000
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
SEARCH_SPACING_SECONDS = 2.5
MAX_SEARCHES_PER_PASS = 25
PRICE_TOLERANCE = 0.25
GOOD, CHECK = 0.75, 0.5

PROMPT = """You read Australian supermarket receipts. Return ONLY a JSON object:
{"store": str, "purchased_at": "YYYY-MM-DDTHH:MM" or null, "total": number or null,
 "lines": [{"raw_name": str, "quantity": number, "unit_price": number or null, "line_total": number,
            "weighed": bool, "promo": bool}]}
Dates on Australian receipts are day/month/year.
Rules: one entry per purchased item line, in receipt order. quantity is 1 and unit_price equals
line_total UNLESS a separate "Qty N @ $X each" or "N kg NET @ $X/kg" line is printed directly under the item;
never split a price into units yourself (a "2L" or "2Kg" in a product name is a pack size, not a quantity).
A "Qty N @ $X each" line belongs to the item above it (quantity N, unit_price X). A "0.522 kg NET @ $2.60/kg"
line belongs to the item above it (quantity 0.522, unit_price 2.60, weighed true). Only those "kg NET" lines
are weighed; a product sold by the pack is not. A leading "^" marks a promotional price (promo true).
Copy raw_name exactly as printed.
Ignore subtotal, payment, card, terminal, rewards and loyalty lines entirely: never output card numbers,
member numbers, approval codes or names."""

ABBREVIATIONS = {
    "ww": "woolworths",
    "hny": "honey",
    "cin": "cinnamon",
    "sprd": "spread",
    "pk": "pack",
    "mlk": "milk",
    "choc": "chocolate",
    "org": "organic",
}
NOISE = {"p/p", "pp", "loose", "pack", "x", "each", "ea"}
SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", re.I)
DIGITS = re.compile(r"\d[\d -]{7,}\d")


class ReceiptError(Exception):
    """A receipt could not be read; the message is safe to show the owner."""


# ── extraction ────────────────────────────────────────────────────────────


def mask(text: str) -> str:
    """Hide long digit runs (card, member and approval numbers) in anything we store."""
    return DIGITS.sub(lambda m: "•" * len(m.group(0)), text or "")


def _number(value) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def normalise_extraction(data: dict) -> dict:
    """Validate the model's JSON. Weighed is derived from a fractional quantity, not trusted from the model."""
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        raise ReceiptError("the model did not return receipt lines")
    lines = []
    for raw in data["lines"]:
        if not isinstance(raw, dict):
            continue
        name = mask(str(raw.get("raw_name") or "").strip())
        total = _number(raw.get("line_total"))
        if not name or total is None:
            continue
        quantity = _number(raw.get("quantity")) or 1.0
        unit = _number(raw.get("unit_price"))
        if unit is None:
            unit = round(total / quantity, 2) if quantity else total
        lines.append(
            {
                "raw_name": name,
                "quantity": quantity,
                "unit_price": unit,
                "line_total": total,
                "weighed": quantity != int(quantity),
                "promo": bool(raw.get("promo")),
            }
        )
    if not lines:
        raise ReceiptError("no item lines could be read from the receipt")
    purchased = str(data.get("purchased_at") or "") or None
    if purchased:
        try:
            parsed = datetime.fromisoformat(purchased)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(config.timezone()))
            purchased = parsed.astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            purchased = None
    return {
        "store": mask(str(data.get("store") or "").strip())[:80],
        "purchased_at": purchased,
        "total": _number(data.get("total")),
        "lines": lines,
    }


async def extract(
    data: bytes,
    content_type: str,
    *,
    key: str | None = None,
    model: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Send the image to the OpenRouter vision model and return the normalised extraction."""
    key = key if key is not None else config.openrouter_key()
    if not key:
        raise ReceiptError("receipt reading is not configured (AUS_CARTWATCH_OPENROUTER_KEY)")
    body = {
        "model": model or config.ocr_model(),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{content_type};base64,{base64.b64encode(data).decode()}"},
                    },
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "X-Title": "aus_cart_mcp.watch receipts"}
    async with httpx.AsyncClient(transport=transport, timeout=120) as http:
        try:
            response = await http.post(f"{config.openrouter_url()}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ReceiptError(f"the receipt reader is unreachable ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise ReceiptError(f"the receipt reader refused the request (HTTP {response.status_code})")
    try:
        text = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except (KeyError, IndexError, ValueError, TypeError):
        raise ReceiptError("the receipt reader returned something that is not receipt JSON") from None
    return normalise_extraction(parsed)


# ── matching ──────────────────────────────────────────────────────────────


def _split_glued(text: str) -> str:
    """POS text glues pack counts to sizes ("6pk450g"): split letters from a following digit."""
    return re.sub(r"(?<=[a-z])(?=\d)", " ", text.lower())


def raw_key(raw_name: str) -> str:
    return " ".join(raw_name.lower().split())


def query_for(raw_name: str) -> str:
    """POS text → a search query: expand abbreviations, drop P/P and other noise."""
    words = []
    for word in re.findall(r"[a-z0-9.&/]+", _split_glued(raw_name)):
        if word in NOISE or word == "&":
            continue
        words.append(ABBREVIATIONS.get(word, word))
    return " ".join(words)


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    """Word stems, so 'fillets'/'fillet' and 'tomatoes'/'tomato' compare equal."""
    out: set[str] = set()
    for word in re.findall(r"[a-z0-9.]+", _split_glued(text)):
        if len(word) == 1 and not word.isdigit():
            continue  # stray letters ("P/P" → "p")
        word = ABBREVIATIONS.get(word, word)
        if word in NOISE:
            continue
        out.add(_stem(word))
    return out


def _sizes(text: str) -> set[tuple[float, str]]:
    out = set()
    for amount, unit in SIZE.findall(_split_glued(text)):
        value, unit = float(amount), unit.lower()
        if unit == "kg":
            value, unit = value * 1000, "g"
        elif unit == "l":
            value, unit = value * 1000, "ml"
        out.add((round(value, 1), unit))
    return out


def is_marketplace(product: Product) -> bool:
    """Woolworths stockcodes are up to 7 digits; marketplace (third-party) listings use longer ids."""
    return len(product.product_id) > 8


def variable_weight(raw_name: str, product: Product) -> bool:
    """Priced by weight within a range ("1.5kg - 2.2kg"), so the paid price legitimately differs."""
    text = f"{product.name} {product.size}".lower()
    return bool(re.search(r"\d\s*(kg|g)\s*-\s*\d", text)) or "per kg" in text


@dataclass(frozen=True)
class Match:
    status: str  # good | check | none
    product: Product | None
    score: float
    note: str = ""


def score(raw_name: str, product: Product) -> float:
    want = _tokens(raw_name)
    if not want:
        return 0.0
    return len(want & _tokens(f"{product.name} {product.size}")) / len(want)


def best_match(raw_name: str, unit_price: float | None, candidates: list[Product]) -> Match:
    """Name and size first; price only as a sanity check (the 2026-10-03 rules)."""
    options = [p for p in candidates if not is_marketplace(p)]
    if not options:
        return Match("none", None, 0.0, "no Woolworths product found")
    want_sizes = _sizes(raw_name)

    def rank(p: Product) -> tuple[float, float]:
        have_sizes = _sizes(f"{p.name} {p.size}")
        size_ok = not want_sizes or not have_sizes or bool(want_sizes & have_sizes)  # only a conflict counts
        closeness = -abs(p.price - unit_price) / unit_price if p.price and unit_price else -1.0
        return (round(score(raw_name, p) * (1.0 if size_ok else 0.6), 3), closeness)

    best = max(options, key=rank)
    value = round(rank(best)[0], 3)
    if value < CHECK:
        return Match("none", best, value, "no close name match")
    if best.price is None or not best.available:
        return Match("check", best, value, "unavailable online right now; check it is the right product")
    if unit_price and not variable_weight(raw_name, best):
        if abs(best.price - unit_price) / unit_price > PRICE_TOLERANCE:
            return Match("check", best, value, f"online ${best.price:.2f} vs paid ${unit_price:.2f}")
    if variable_weight(raw_name, best):
        return Match("check", best, value, "variable-weight pack; check the size")
    return Match("good" if value >= GOOD else "check", best, value)


Sleep = Callable[[float], Awaitable[None]]


async def match_receipt(
    store: Store,
    tracker: Tracker,
    receipt_id: int,
    *,
    retailer: str,
    budget_left: Callable[[], bool],
    sleep: Sleep = asyncio.sleep,
    max_searches: int = MAX_SEARCHES_PER_PASS,
) -> dict:
    """Match pending lines: aliases first (free), then cached or new searches, spaced and within budget."""
    store.update_receipt(receipt_id, status="matching")
    counts = {"alias": 0, "matched": 0, "searched": 0, "left": 0}
    searches = 0
    try:
        for line in store.receipt_lines(receipt_id):
            if line["match_status"] != "pending":
                continue
            alias = store.receipt_alias(retailer, raw_key(line["raw_name"]))
            if alias:
                product = tracker.store.cached_product(retailer, alias, max_age=timedelta(days=3650))
                store.set_line_match(
                    line["id"],
                    "alias",
                    product_id=alias,
                    product_name=(product.name if product else ""),
                    product_price=(product.price if product else None),
                    score=1.0,
                    note="matched before",
                )
                counts["alias"] += 1
                continue
            query = query_for(line["raw_name"])
            cached = store.cached_search(retailer, query, max_age=tracker.cache_ttl)
            if cached is None:
                if searches >= max_searches or not budget_left():
                    counts["left"] += 1
                    continue
                if searches:
                    await sleep(SEARCH_SPACING_SECONDS)
                searches += 1
            candidates = cached if cached is not None else await tracker.search(query, limit=10)
            match = best_match(line["raw_name"], line["unit_price"], candidates)
            store.set_line_match(
                line["id"],
                match.status,
                product_id=match.product.product_id if match.product else None,
                product_name=match.product.name if match.product else "",
                product_price=match.product.price if match.product else None,
                score=match.score,
                note=match.note,
            )
            counts["matched"] += 1
    except Blocked as exc:
        store.update_receipt(receipt_id, status="extracted", error=f"Woolworths refused a search: {exc}")
        return {**counts, "searched": searches, "blocked": True}
    except AusCartError as exc:
        store.update_receipt(receipt_id, status="extracted", error=f"search failed: {exc}")
        return {**counts, "searched": searches}
    counts["searched"] = searches
    store.update_receipt(receipt_id, status="extracted" if counts["left"] else "matched", error="")
    return counts


def reset_matches(store: Store, receipt_id: int) -> int:
    """Put every matched line back to pending (not weighed ones), e.g. after the rules improve."""
    count = 0
    for line in store.receipt_lines(receipt_id):
        if line["match_status"] not in ("skipped", "pending"):
            store.set_line_match(line["id"], "pending")
            count += 1
    return count


# ── upload and confirmation ──────────────────────────────────────────────


@dataclass(frozen=True)
class Upload:
    receipt_id: int
    duplicate: bool


def save(store: Store, data: bytes, content_type: str, *, directory: str | None = None) -> Upload:
    """Store the image once (by SHA-256, mode 600). A re-upload of the same image returns the existing receipt."""
    if content_type not in IMAGE_TYPES:
        raise ReceiptError("upload a photo of the receipt (JPEG, PNG, WebP or HEIC)")
    if not data:
        raise ReceiptError("the upload was empty")
    if len(data) > MAX_BYTES:
        raise ReceiptError("the photo is larger than 12 MB")
    digest = hashlib.sha256(data).hexdigest()
    existing = store.receipt_by_sha(digest)
    if existing:
        return Upload(existing["id"], True)
    folder = Path(directory or config.receipts_dir())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.{content_type.split('/')[1]}"
    path.write_bytes(data)
    os.chmod(path, 0o600)
    return Upload(store.add_receipt(digest, str(path), content_type), False)


def ocr_allowed_today(store: Store, *, now: datetime | None = None) -> bool:
    local = (now or utcnow()).astimezone(ZoneInfo(config.timezone()))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return store.runs_since("ocr", start) < config.ocr_daily_cap()


async def read(store: Store, receipt_id: int, *, extractor=extract, now: datetime | None = None) -> dict:
    """Run OCR on a stored receipt (counted against the daily OCR cap) and store its lines."""
    receipt = store.get_receipt(receipt_id)
    if receipt is None:
        raise ReceiptError("unknown receipt")
    if not ocr_allowed_today(store, now=now):
        raise ReceiptError(f"the daily limit of {config.ocr_daily_cap()} receipt readings is used up")
    run_id = store.start_run("ocr", now=now)
    try:
        data = Path(receipt["path"]).read_bytes()
        extraction = await extractor(data, receipt["content_type"])
    except ReceiptError as exc:
        store.finish_run(run_id, "error", note=str(exc), now=now)
        store.update_receipt(receipt_id, status="failed", error=str(exc))
        raise
    store.finish_run(run_id, "ok", note=f"receipt {receipt_id}: {len(extraction['lines'])} lines", now=now)
    lines_sum = round(sum(line["line_total"] for line in extraction["lines"]), 2)
    store.add_receipt_lines(receipt_id, extraction["lines"])
    store.update_receipt(
        receipt_id,
        status="extracted",
        store_name=extraction["store"],
        purchased_at=extraction["purchased_at"],
        total=extraction["total"],
        lines_sum=lines_sum,
        ocr_model=config.ocr_model(),
        error="",
    )
    return extraction


def confirm(store: Store, receipt_id: int, chosen: dict[int, str], *, retailer: str) -> dict:
    """Track the chosen product for each ticked line, remember aliases, and record the shop and prices paid."""
    receipt = store.get_receipt(receipt_id)
    if receipt is None:
        raise ReceiptError("unknown receipt")
    lines = {line["id"]: line for line in store.receipt_lines(receipt_id)}
    when = datetime.fromisoformat(receipt["purchased_at"]) if receipt["purchased_at"] else utcnow()
    tracker = Tracker(store, None, retailer)
    tracked = created = 0
    bought = []
    for line_id, product_id in chosen.items():
        line = lines.get(line_id)
        if line is None or not product_id:
            continue
        product = store.cached_product(retailer, product_id, max_age=timedelta(days=3650))
        if product is None:
            continue
        result = tracker.track_product(product, source="receipt", observe_first=False)
        created += result.created
        tracked += 1
        store.set_receipt_alias(retailer, raw_key(line["raw_name"]), product_id)
        if line["unit_price"] is not None:
            store.add_observation(
                retailer,
                product_id,
                price=line["unit_price"],
                on_special=bool(line["promo"]),
                source="receipt",
                now=when,
            )
        analytics.refresh_item_stats(store, retailer, product_id)
        bought.append({"product_id": product_id, "name": product.name, "quantity": line["quantity"]})
    if bought:
        store.add_shop_episode(retailer, "receipt", bought, started_at=when, closed_at=when)
    store.update_receipt(receipt_id, status="confirmed", confirmed_at=utcnow().isoformat(timespec="seconds"))
    return {"tracked": tracked, "created": created}
