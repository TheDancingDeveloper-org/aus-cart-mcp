"""Pending cart: record every write, sync when the retailer isn't paused, never double-add."""

import pytest

from aus_cart_mcp.gateway import Gateway, Limits
from aus_cart_mcp.mock.woolworths import signed_in_cookie
from aus_cart_mcp.retailers.base import Blocked
from aus_cart_mcp.server import parse_cookie_header


async def _connect(store, gateway, mock_state, name="t"):
    tenant, _ = store.create_tenant_sync(name)
    await gateway.connect(tenant, "woolworths", parse_cookie_header(signed_in_cookie(mock_state, name)))
    return tenant


async def test_add_while_paused_is_pending_and_syncs_after(store, make_gateway, mock_state, clock):
    gateway = make_gateway(cooloff=600)
    tenant = await _connect(store, gateway, mock_state)
    gateway._trip("woolworths")
    outcome = await gateway.add(tenant, "woolworths", {"888140": 2})
    assert outcome["status"] == "pending"
    assert outcome["retry_after"] == 600
    assert "paus" in outcome["reason"]
    assert (await gateway.pending(tenant, "woolworths"))[0]["state"] == "pending"
    sent = len(mock_state.requests)
    with pytest.raises(Blocked):
        await gateway.sync(tenant, "woolworths")
    assert len(mock_state.requests) == sent  # nothing went out while paused

    clock.now += 601
    cart = await gateway.sync(tenant, "woolworths")
    assert [(i.product_id, i.quantity) for i in cart.items] == [("888140", 2)]
    assert await gateway.pending(tenant, "woolworths") == []


async def test_reconciliation_does_not_double_add(store, gateway, mock_state):
    tenant = await _connect(store, gateway, mock_state)
    first = await gateway.add(tenant, "woolworths", {"888140": 1})
    assert first["status"] == "synced"
    cart = await gateway.sync(tenant, "woolworths")
    assert [(i.product_id, i.quantity) for i in cart.items] == [("888140", 1)]
    second = await gateway.add(tenant, "woolworths", {"888140": 1})
    assert second["cart"]["items"][0]["quantity"] == 2


async def test_permanent_failure_is_surfaced(store, gateway, mock_state):
    tenant = await _connect(store, gateway, mock_state)
    mock_state.reject_update = True
    outcome = await gateway.add(tenant, "woolworths", {"888140": 1})
    assert outcome["status"] == "pending" and outcome["permanent"] is True
    assert "can't be added" in outcome["reason"]
    ops = await gateway.pending(tenant, "woolworths")
    assert ops[0]["state"] == "failed"
    mock_state.reject_update = False
    cart = await gateway.sync(tenant, "woolworths")
    assert cart.items == []


async def test_queue_survives_restart(store, make_gateway, mock_state, clock):
    gateway = make_gateway(cooloff=600)
    tenant = await _connect(store, gateway, mock_state)
    gateway._trip("woolworths")
    await gateway.add(tenant, "woolworths", {"888140": 3})
    restarted = Gateway(
        store,
        Limits(min_interval=0, jitter=0, cooloff=600),
        transport_factory=gateway._transport_factory,
        clock=clock,
        sleep=clock.sleep,
    )
    ops = await restarted.pending(tenant, "woolworths")
    assert [(o["product_id"], o["quantity"], o["state"]) for o in ops] == [("888140", 3, "pending")]
    clock.now += 601
    cart = await restarted.sync(tenant, "woolworths")
    assert [(i.product_id, i.quantity) for i in cart.items] == [("888140", 3)]


async def test_pending_ops_are_isolated_per_tenant(store, make_gateway, mock_state):
    gateway = make_gateway(cooloff=600)
    alice = await _connect(store, gateway, mock_state, "alice")
    bob = await _connect(store, gateway, mock_state, "bob")
    gateway._trip("woolworths")
    await gateway.add(alice, "woolworths", {"888140": 1})
    assert await gateway.pending(bob, "woolworths") == []
    assert len(await gateway.pending(alice, "woolworths")) == 1


async def test_newer_write_supersedes(store, make_gateway, mock_state):
    gateway = make_gateway(cooloff=600)
    tenant = await _connect(store, gateway, mock_state)
    gateway._trip("woolworths")
    await gateway.add(tenant, "woolworths", {"888140": 1})
    await gateway.set_quantities(tenant, "woolworths", {"888140": 5})
    ops = await store.list_pending(tenant.id, "woolworths", states=("pending", "superseded"))
    assert [o["state"] for o in ops] == ["superseded", "pending"]
    assert ops[1]["op"] == "set" and ops[1]["quantity"] == 5


async def test_two_paused_adds_of_the_same_product_both_land(store, make_gateway, mock_state, clock):
    gateway = make_gateway(cooloff=600)
    tenant = await _connect(store, gateway, mock_state)
    gateway._trip("woolworths")
    await gateway.add(tenant, "woolworths", {"888140": 2})
    await gateway.add(tenant, "woolworths", {"888140": 1})
    ops = await store.list_pending(tenant.id, "woolworths", states=("pending",))
    assert [o["quantity"] for o in ops] == [2, 1]
    clock.now += 601
    cart = await gateway.sync(tenant, "woolworths")
    assert [(i.product_id, i.quantity) for i in cart.items] == [("888140", 3)]


async def test_a_set_replaces_paused_adds_and_is_not_applied_twice(store, make_gateway, mock_state, clock):
    gateway = make_gateway(cooloff=600)
    tenant = await _connect(store, gateway, mock_state)
    gateway._trip("woolworths")
    await gateway.add(tenant, "woolworths", {"888140": 2})
    await gateway.set_quantities(tenant, "woolworths", {"888140": 5})
    ops = await store.list_pending(tenant.id, "woolworths", states=("pending", "superseded"))
    assert [o["state"] for o in ops] == ["superseded", "pending"]
    clock.now += 601
    cart = await gateway.sync(tenant, "woolworths")
    assert [(i.product_id, i.quantity) for i in cart.items] == [("888140", 5)]
    again = await gateway.sync(tenant, "woolworths")
    assert [(i.product_id, i.quantity) for i in again.items] == [("888140", 5)]


async def test_one_refused_product_does_not_fail_the_other(store, gateway, mock_state):
    tenant = await _connect(store, gateway, mock_state)
    mock_state.reject_stockcodes = {888140}
    outcome = await gateway.add(tenant, "woolworths", {"888140": 1, "888141": 2})
    assert outcome["status"] == "pending" and outcome["permanent"] is True
    ops = {o["product_id"]: o["state"] for o in await gateway.pending(tenant, "woolworths")}
    assert ops == {"888140": "failed"}
    cart = outcome["cart"]
    assert [(i["product_id"], i["quantity"]) for i in cart["items"]] == [("888141", 2)]


async def test_get_cart_reports_pending_ops_while_paused(store, make_gateway, mock_state):
    gateway = make_gateway(cooloff=600)
    tenant = await _connect(store, gateway, mock_state)
    gateway._trip("woolworths")
    await gateway.add(tenant, "woolworths", {"888140": 1})
    rows = await gateway.store.list_pending(tenant.id, "woolworths")
    assert rows and rows[0]["state"] == "pending"
    # Failed rows are kept, not purged with the settled ones.
    await gateway.store.mark_pending(rows[0]["id"], "failed", "no")
    assert await gateway.store.purge_pending(older_than_days=0) == 0


async def test_paused_adds_stack_when_the_cart_is_cached(store, make_gateway, mock_state, clock):
    gateway = make_gateway(cooloff=600, cart_ttl=3600)
    tenant = await _connect(store, gateway, mock_state)
    await gateway.set_quantities(tenant, "woolworths", {"888140": 1})
    gateway._trip("woolworths")
    await gateway.add(tenant, "woolworths", {"888140": 1})
    await gateway.add(tenant, "woolworths", {"888140": 1})
    clock.now += 601
    cart = await gateway.sync(tenant, "woolworths")
    assert [(i.product_id, i.quantity) for i in cart.items] == [("888140", 3)]


async def test_a_refused_product_keeps_the_batch_pending(store, gateway, mock_state):
    tenant = await _connect(store, gateway, mock_state)
    mock_state.reject_stockcodes = {888140}
    outcome = await gateway.add(tenant, "woolworths", {"888140": 1, "888141": 2})
    assert outcome["status"] == "pending" and outcome["permanent"] is True
    assert [(i["product_id"], i["quantity"]) for i in outcome["cart"]["items"]] == [("888141", 2)]


async def test_sync_cart_does_not_report_synced_when_a_product_was_refused(store, gateway, mock_state):
    """Blocker 1: sync_cart's own status must count a failed row, not only pending ones."""
    import json

    from mcp.server.mcpserver import Context

    from aus_cart_mcp.server import build

    tenant = await _connect(store, gateway, mock_state)
    mock_state.reject_stockcodes = {888140}
    added = await gateway.add(tenant, "woolworths", {"888140": 1})
    assert added["status"] == "pending" and added["permanent"] is True
    mcp, _ = build(store, gateway)

    class _State:
        tenant = None

    class _Request:
        state = _State()

    class _RC:
        request = _Request()

    ctx = Context(request_context=_RC())
    ctx.request_context.request.state.tenant = tenant
    result = await mcp.call_tool("sync_cart", {"retailer": "woolworths"}, context=ctx)
    payload = json.loads(result.content[0].text)
    assert payload["status"] != "synced"
    assert payload["status"] == "pending"
    assert payload["permanent"] is True
    assert payload["reason"]


async def test_a_failed_row_does_not_poison_later_adds_and_can_be_cleared(store, gateway, mock_state):
    """Blocker 2: a refusal is scoped to that product, and clear_pending removes it."""
    tenant = await _connect(store, gateway, mock_state)
    mock_state.reject_stockcodes = {888140}
    refused = await gateway.add(tenant, "woolworths", {"888140": 1})
    assert refused["permanent"] is True
    mock_state.reject_stockcodes = set()
    later = await gateway.add(tenant, "woolworths", {"888142": 1})
    assert later["status"] == "synced"
    assert later.get("permanent") is not True
    still = await gateway.pending(tenant, "woolworths")
    assert [o["product_id"] for o in still] == ["888140"]
    cleared = await gateway.clear_failed(tenant, "woolworths", "888140")
    assert cleared["cleared"] == 1
    assert await gateway.pending(tenant, "woolworths") == []


async def test_failed_ops_are_purged_after_the_ttl(store, gateway, mock_state):
    tenant = await _connect(store, gateway, mock_state)
    mock_state.reject_update = True
    await gateway.add(tenant, "woolworths", {"888140": 1})
    failed = (await gateway.pending(tenant, "woolworths"))[0]
    assert failed["state"] == "failed"
    # Brand new, so a cutoff of "older than now" does not take it.
    assert await store.purge_pending(older_than_days=0) == 0
    db = store._connect()
    try:
        db.execute(
            "UPDATE pending_ops SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (failed["id"],),
        )
        db.commit()
    finally:
        db.close()
    assert await store.purge_pending(older_than_days=0) == 1
    assert await gateway.pending(tenant, "woolworths") == []
