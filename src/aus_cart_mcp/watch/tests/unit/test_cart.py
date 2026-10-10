from aus_cart_mcp.watch import cart
from aus_cart_mcp.watch.auscart import Blocked, RetailerError

R = "woolworths"


async def add(store, fake, pid="1", quantity=1, **kw):
    store.upsert_product(R, "1", name="Milk")
    return await cart.add(store, fake, R, pid, quantity, reason=kw.pop("reason", "cli"), **kw)


async def test_add_confirms_and_records(store, fake):
    alert = store.add_alert("k", "on_sale", {}, retailer=R, product_id="1")
    outcome = await add(store, fake, quantity=2, alert_id=alert, reason=f"alert:{alert}")
    assert outcome.ok and outcome.quantity_in_cart == 2 and "Added 2 × Milk" in outcome.message
    assert fake.calls == ["add_to_cart", "get_cart"]
    action = store.cart_actions()[0]
    assert (action["reason"], action["outcome"], action["quantity"]) == (f"alert:{alert}", "ok", 2)
    assert store.get_alert(alert)["acted_at"]


async def test_unknown_products_and_quantities_are_refused(store, fake):
    assert (await add(store, fake, pid="999")).outcome == "refused"
    assert (await add(store, fake, quantity=7)).outcome == "refused"
    assert (await add(store, fake, quantity=0)).outcome == "refused"
    assert fake.calls == []


async def test_session_expired_tells_the_owner_and_alerts_once_a_day(store, fake):
    fake.session = False
    first = await add(store, fake)
    second = await add(store, fake)
    assert first.outcome == second.outcome == "session_required"
    assert "reconnect in the myaiagent app" in first.message
    assert len([a for a in store.recent_alerts() if a["kind"] == "operator"]) == 1


async def test_blocked_and_errors_are_not_retried(store, fake):
    fake.fail_with = Blocked("refusing requests (HTTP 403)")
    assert (await add(store, fake)).outcome == "blocked"
    fake.fail_with = RetailerError("Woolworths: item unavailable")
    assert (await add(store, fake)).outcome == "error"
    assert fake.calls == ["add_to_cart", "add_to_cart"]


async def test_unconfirmed_add(store, fake):
    async def lost(items, *, retailer=R):
        fake._call("add_to_cart")
        return fake._cart()

    fake.add_to_cart = lost
    outcome = await add(store, fake)
    assert outcome.outcome == "unconfirmed" and not outcome.ok


def test_snooze(store):
    store.upsert_product(R, "1", name="Milk")
    store.track(R, "1")
    assert cart.snooze(store, R, "1") == "Snoozed for 28 days"
    assert store.tracked_item(R, "1")["snoozed_until"]
