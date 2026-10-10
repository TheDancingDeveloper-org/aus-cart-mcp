"""Receipt reading and matching. Fixtures are synthetic; the matching cases are the real misses of 2026-10-03."""

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aus_cartwatch import receipts
from aus_cartwatch.auscart import Blocked, Product
from aus_cartwatch.tracking import Tracker
from tests.conftest import T0, FakeAusCart, product

R = "woolworths"
SYNTHETIC = {
    "store": "Woolworths Test Store",
    "purchased_at": "2026-10-03T11:03",
    "total": 31.25,
    "lines": [
        {"raw_name": "Carrot", "quantity": 0.522, "unit_price": 2.6, "line_total": 1.36, "weighed": True},
        {"raw_name": "Dairy Farmers Full Cream Milk 2L", "quantity": 1, "unit_price": 4.7, "line_total": 4.7},
        {"raw_name": "WW Toasted Muesli Hny & Cin 750g", "quantity": 2, "unit_price": 4.35, "line_total": 8.7,
         "weighed": True},
        {"raw_name": "Blueberry 170g", "quantity": 1, "unit_price": 4.4, "line_total": 4.4},
        {"raw_name": "Card 4111 1111 1111 1111", "quantity": 1, "line_total": 12.09},
        {"raw_name": "", "line_total": 1.0},
    ],
}  # fmt: skip


def test_normalise_derives_weighed_masks_digits_and_dates():
    out = receipts.normalise_extraction(SYNTHETIC)
    assert [line["weighed"] for line in out["lines"]] == [True, False, False, False, False]  # not the model's flag
    assert "4111" not in out["lines"][4]["raw_name"] and "•" in out["lines"][4]["raw_name"]
    assert out["lines"][4]["unit_price"] == 12.09  # derived from the total
    assert out["purchased_at"] == "2026-10-03T01:03:00+00:00"  # 11:03 Sydney, still AEST (+10) until 4 Oct
    with pytest.raises(receipts.ReceiptError):
        receipts.normalise_extraction({"lines": [{"raw_name": "x"}]})
    with pytest.raises(receipts.ReceiptError):
        receipts.normalise_extraction({"nope": 1})
    assert receipts.normalise_extraction({**SYNTHETIC, "purchased_at": "yesterday"})["purchased_at"] is None


def test_query_for_expands_pos_abbreviations():
    assert receipts.query_for("WW Toasted Muesli Hny & Cin 750g") == "woolworths toasted muesli honey cin 750g".replace(
        "cin", "cinnamon"
    )
    assert receipts.query_for("Onion Brown 1kg P/P") == "onion brown 1kg"
    assert receipts.query_for("Mainland Buttersoft Sprd Butter 375g") == "mainland buttersoft spread butter 375g"


def p(pid, name, price, size="", available=True):
    return Product(pid, name, price, size=size, available=available)


def test_the_2026_10_03_misses():
    blueberries = p("53192", "Blueberries Punnet 170g", 4.4, "170g")
    strawberries = p("144607", "Strawberries Punnet 250g", 4.5, "250g")
    candle = p("1137932732", "Demeter Atmosphere Soy Candle - Blueberry 170g", 36.1, "170g")
    match = receipts.best_match("Blueberry 170g", 4.4, [strawberries, candle, blueberries])
    assert match.status == "good" and match.product.product_id == "53192"
    assert receipts.best_match("Blueberry 170g", 4.4, [candle]).status == "none"  # marketplace rejected
    assert receipts.best_match("Blueberry 170g", 4.4, [strawberries]).status == "none"  # price alone is not enough

    chicken = p("969723", "Woolworths RSPCA Approved Chicken Breast Fillet 1.5kg - 2.2kg", 24.2, "1.5kg - 2.2kg")
    match = receipts.best_match("WW RSPCA Chicken Breast Fillets Bulk Pk", 19.94, [chicken])
    assert match.status == "check" and "variable-weight" in match.note

    gone = p("53192", "Blueberries Punnet 170g", None, "170g", available=False)
    assert receipts.best_match("Blueberry 170g", 4.4, [gone]).status == "check"

    milk = p("88436", "Dairy Farmers Full Cream Milk 2L", 4.7, "2L")
    milk3 = p("888140", "Dairy Farmers Full Cream Milk 3L", 6.2, "3L")
    match = receipts.best_match("Dairy Farmers Full Cream Milk 2L", 4.7, [milk3, milk])
    assert match.status == "good" and match.product.product_id == "88436"  # size decides
    pricey = receipts.best_match("Dairy Farmers Full Cream Milk 2L", 3.0, [milk])
    assert pricey.status == "check" and "online $4.70 vs paid $3.00" in pricey.note
    assert receipts.best_match("anything", 1.0, []).status == "none"


def openrouter(payload, status=200):
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


async def test_extract_calls_openrouter_and_parses():
    reply = {"choices": [{"message": {"content": "```json\n" + json.dumps(SYNTHETIC) + "\n```"}}]}
    out = await receipts.extract(b"jpeg", "image/jpeg", key="k", model="m", transport=openrouter(reply))
    assert len(out["lines"]) == 5 and out["store"] == "Woolworths Test Store"
    with pytest.raises(receipts.ReceiptError, match="HTTP 402"):
        await receipts.extract(b"jpeg", "image/jpeg", key="k", transport=openrouter({}, 402))
    with pytest.raises(receipts.ReceiptError, match="not receipt JSON"):
        await receipts.extract(b"jpeg", "image/jpeg", key="k", transport=openrouter({"choices": []}))
    with pytest.raises(receipts.ReceiptError, match="not configured"):
        await receipts.extract(b"jpeg", "image/jpeg", key="")

    def down(request):
        raise httpx.ConnectError("no", request=request)

    with pytest.raises(receipts.ReceiptError, match="unreachable"):
        await receipts.extract(b"jpeg", "image/jpeg", key="k", transport=httpx.MockTransport(down))


def test_save_dedupes_and_protects(store, tmp_path):
    first = receipts.save(store, b"\xff\xd8photo", "image/jpeg", directory=str(tmp_path))
    again = receipts.save(store, b"\xff\xd8photo", "image/jpeg", directory=str(tmp_path))
    assert not first.duplicate and again.duplicate and again.receipt_id == first.receipt_id
    path = store.get_receipt(first.receipt_id)["path"]
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    for data, kind, message in [(b"x", "application/pdf", "photo"), (b"", "image/png", "empty"),
                                (b"x" * (receipts.MAX_BYTES + 1), "image/png", "12 MB")]:  # fmt: skip
        with pytest.raises(receipts.ReceiptError, match=message):
            receipts.save(store, data, kind, directory=str(tmp_path))


async def fake_extractor(data, content_type):
    return receipts.normalise_extraction(SYNTHETIC)


async def test_read_respects_the_daily_cap(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_OCR_DAILY_CAP", "1")
    one = receipts.save(store, b"one", "image/jpeg", directory=str(tmp_path)).receipt_id
    two = receipts.save(store, b"two", "image/jpeg", directory=str(tmp_path)).receipt_id
    await receipts.read(store, one, extractor=fake_extractor)
    assert store.get_receipt(one)["lines_sum"] == 31.25 and len(store.receipt_lines(one)) == 5
    with pytest.raises(receipts.ReceiptError, match="daily limit"):
        await receipts.read(store, two, extractor=fake_extractor)

    async def failing(data, content_type):
        raise receipts.ReceiptError("blurry")

    monkeypatch.setenv("AUS_CARTWATCH_OCR_DAILY_CAP", "5")
    with pytest.raises(receipts.ReceiptError):
        await receipts.read(store, two, extractor=failing)
    assert store.get_receipt(two)["status"] == "failed"
    with pytest.raises(receipts.ReceiptError):
        await receipts.read(store, 999, extractor=fake_extractor)


@pytest.fixture()
def catalogue():
    return FakeAusCart(
        {
            "88436": product("88436", "Dairy Farmers Full Cream Milk 2L", 4.7, size="2L"),
            "743268": product("743268", "Woolworths Toasted Honey & Cinnamon Muesli 750g", 4.35, size="750g"),
            "53192": product("53192", "Blueberries Punnet 170g", 4.4, size="170g"),
        }
    )


async def test_match_then_confirm(store, tmp_path, catalogue, clock):
    receipt_id = receipts.save(store, b"r", "image/jpeg", directory=str(tmp_path)).receipt_id
    await receipts.read(store, receipt_id, extractor=fake_extractor)
    tracker = Tracker(store, catalogue, R)
    counts = await receipts.match_receipt(store, tracker, receipt_id, retailer=R, budget_left=lambda: True,
                                          sleep=clock.sleep)  # fmt: skip
    statuses = {line["raw_name"][:12]: line["match_status"] for line in store.receipt_lines(receipt_id)}
    assert statuses == {"Carrot": "skipped", "Dairy Farmer": "good", "WW Toasted M": "good", "Blueberry 17": "good",
                        "Card •••••••": "none"}  # fmt: skip
    assert counts["searched"] == 4 and clock.slept == [2.5, 2.5, 2.5]
    assert store.get_receipt(receipt_id)["status"] == "matched"

    lines = {line["raw_name"][:5]: line for line in store.receipt_lines(receipt_id)}
    result = receipts.confirm(store, receipt_id, {lines["Dairy"]["id"]: "88436", lines["WW To"]["id"]: "743268",
                                                  999: "1", lines["Blueb"]["id"]: "nope"}, retailer=R)  # fmt: skip
    assert result == {"tracked": 2, "created": 2}
    assert {i["product_id"] for i in store.list_tracked()} == {"88436", "743268"}
    assert store.list_tracked()[0]["source"] == "receipt"
    assert store.receipt_alias(R, "ww toasted muesli hny & cin 750g") == "743268"
    paid = store.observations(R, "743268")
    assert [(o["price"], o["source"]) for o in paid] == [(4.35, "receipt")] and paid[0]["observed_at"].startswith(
        "2026-10-03"
    )
    assert store.closed_shop_episodes(R)[0]["source"] == "receipt"
    assert store.get_receipt(receipt_id)["status"] == "confirmed"
    with pytest.raises(receipts.ReceiptError):
        receipts.confirm(store, 999, {}, retailer=R)

    # The next receipt with the same line is matched from the alias, with no search at all.
    second = receipts.save(store, b"r2", "image/jpeg", directory=str(tmp_path)).receipt_id
    await receipts.read(store, second, extractor=fake_extractor)
    calls = len(catalogue.calls)
    await receipts.match_receipt(store, Tracker(store, catalogue, R), second, retailer=R, budget_left=lambda: True,
                                 sleep=clock.sleep)  # fmt: skip
    assert {
        line["match_status"] for line in store.receipt_lines(second) if line["raw_name"].startswith(("Dairy", "WW"))
    } == {"alias"}
    assert len(catalogue.calls) == calls  # aliases and the 24 h search cache: no Woolworths request


async def test_match_stops_at_the_budget_and_on_a_block(store, tmp_path, catalogue, clock):
    receipt_id = receipts.save(store, b"r", "image/jpeg", directory=str(tmp_path)).receipt_id
    await receipts.read(store, receipt_id, extractor=fake_extractor)
    counts = await receipts.match_receipt(store, Tracker(store, catalogue, R), receipt_id, retailer=R,
                                          budget_left=lambda: False, sleep=clock.sleep)  # fmt: skip
    assert counts["left"] == 4 and store.get_receipt(receipt_id)["status"] == "extracted"
    catalogue.fail_with = Blocked("refusing (HTTP 403)")
    counts = await receipts.match_receipt(store, Tracker(store, catalogue, R), receipt_id, retailer=R,
                                          budget_left=lambda: True, sleep=clock.sleep)  # fmt: skip
    assert counts.get("blocked") and "refused" in store.get_receipt(receipt_id)["error"]
    assert catalogue.calls.count("search_products") == 1  # stopped at the first refusal


def test_ocr_cap_counts_local_days(store, monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_OCR_DAILY_CAP", "1")
    store.start_run("ocr", now=T0)
    assert not receipts.ocr_allowed_today(store, now=T0 + timedelta(hours=1))
    assert receipts.ocr_allowed_today(store, now=T0 + timedelta(days=1))
    assert datetime.now(UTC)  # noqa


def test_prod_receipt_misses_2026_10_05():
    """The four misses from the first prod receipt run, as fixed cases."""
    tortilla = p("233280", "Old El Paso Burrito Tortilla Jumbo Wraps 6 pack", 7.0, "6 pack")
    match = receipts.best_match("Old El Paso Jumbo Flour Tortilla 6pk450g", 7.0, [tortilla])
    assert match.status in ("good", "check") and match.product.product_id == "233280"
    assert receipts.query_for("Old El Paso Jumbo Flour Tortilla 6pk450g").endswith("6pk 450g")

    mince = p("268508", "Market Value Beef Mince 1.8kg", 23.0, "1.8kg")
    assert receipts.best_match("Market Value Beef Mince Bulk 1.8kg", 23.0, [mince]).status == "good"

    cherry = p("149620", "Woolworths Cherry Tomatoes Punnet 250g", 3.0, "250g")
    assert receipts.best_match("Tomato Cherry Red 250g P/P", 3.0, [cherry]).status == "good"

    small = p("6078623", "Woolworths RSPCA Approved Chicken Breast Fillets 650g", 9.4, "650g")
    bulk = p("969723", "Woolworths RSPCA Approved Chicken Breast Fillet 1.5kg - 2.2kg", 24.2, "1.5kg - 2.2kg")
    match = receipts.best_match("WW RSPCA Chicken Breast Fillets Bulk Pk", 19.94, [small, bulk])
    assert match.product.product_id == "969723" and match.status == "check"  # range: the owner checks the size


def test_reset_matches(store, tmp_path):
    rid = receipts.save(store, b"x", "image/jpeg", directory=str(tmp_path)).receipt_id
    store.add_receipt_lines(rid, [{"raw_name": "A", "quantity": 1, "line_total": 1.0},
                                  {"raw_name": "B", "quantity": 0.5, "line_total": 1.0, "weighed": True}])  # fmt: skip
    first = store.receipt_lines(rid)[0]
    store.set_line_match(first["id"], "good", product_id="1")
    assert receipts.reset_matches(store, rid) == 1
    assert [line["match_status"] for line in store.receipt_lines(rid)] == ["pending", "skipped"]
