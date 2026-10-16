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
