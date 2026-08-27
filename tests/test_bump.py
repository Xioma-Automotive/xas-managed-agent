"""DECIDE-13: an untouched order is displaced ONLY when the planner authorizes it.

Without `may_move.also`, an order that needs help stays late even though an
untouched one is sitting on the only on-time car. With `also` naming that order,
the solver displaces it — but only because doing so lowers total cost, and only
after paying `break_cost` for the promise it disturbs.

Two things this file exists to hold down after the 2026-08-26 rework:
  * removing the time fence did NOT weaken the no-bump default. The default is
    the free-set rule, and it still refuses to touch a settled order.
  * removing the fence DID fix authorised bumping, which the fence used to cancel
    before the authorisation was even read.
"""

from datetime import date

import pytest

from xas_allocation.session import bump_candidates
from xas_allocation.snapshot import Order, Snapshot, Vehicle
from xas_allocation.solver import CFG, solve, tardiness

NOW = date(2026, 8, 3)
PROMISED = date(2026, 9, 14)
NEAR_PROMISED = date(2026, 8, 10)  # inside what used to be the frozen fence
LATE = date(2026, 10, 14)
ON_TIME = date(2026, 9, 14)


def _order(oid: str, customer_id: str, promised: date = PROMISED) -> Order:
    # `oid` is the full order key: VSO + car line.
    so_id, line = oid.rsplit("-", 1)
    return Order(
        so_id=so_id,
        line=int(line),
        customer=customer_id,
        customer_id=customer_id,
        sales_model="SM1",
        delivery_date=promised,
        price=40000,
    )


def _vehicle(vid: str, planned: date) -> Vehicle:
    return Vehicle(
        vehicle_id=vid,
        vehicle_classification="Vehicle",
        sales_model="SM1",
        eta_dealer=planned,
    )


def _snapshot(promised: date = PROMISED) -> Snapshot:
    """HI is late on its car; LO (untouched) holds the only on-time car; one spare
    late car exists for LO to fall back to."""
    return Snapshot(
        orders=[_order("SO-HI-1", "CUST-001", promised), _order("SO-LO-1", "CUST-002", promised)],
        vehicles=[
            _vehicle("VEH-HI-LATE", LATE),
            _vehicle("VEH-LO-GOOD", promised),  # lands exactly on the promise
            _vehicle("VEH-SPARE-LATE", LATE),
        ],
        allocations={"SO-HI-1": "VEH-HI-LATE", "SO-LO-1": "VEH-LO-GOOD"},
        disruption={"disrupted_orders": ["SO-HI-1"]},
        now=NOW,
    )


# The realistic authorisation, and the ONLY thing that makes a bump worth its
# cost now that no order is heavier than another by default: the planner says
# which order matters, then says who may be displaced for it. Two orders of equal
# weight swapping the same lateness is a wash the solver correctly declines.
URGENT_HI = {"priority": [{"order": "SO-HI-1", "step": "urgent"}]}
AUTH = {**URGENT_HI, "may_move": {"also": {"orders": ["SO-LO-1"]}}}


def test_without_authorization_no_bump_and_the_late_order_stays_late():
    snap = _snapshot()
    result = solve(snap, {}, churn_price=0)
    vehicles = snap.vehicle_by_id()
    assert result.plan["SO-LO-1"] == "VEH-LO-GOOD", "untouched order must not move uninvited"
    assert tardiness(snap.order_by_key()["SO-HI-1"], vehicles[result.plan["SO-HI-1"]]) > 0
    # ...and the agent can see the bump it would need to propose.
    assert any(c["row"] == "SO-LO-1" for c in bump_candidates(snap, result))


def test_the_no_bump_default_survived_the_fence_removal():
    """The fence used to be a second wall in front of a settled order promised
    within 14 days. It is gone; the free-set rule alone must still hold that order
    still, both when nothing is steered and when the turn is sliced."""
    snap = _snapshot(NEAR_PROMISED)
    for steer in ({}, {"may_move": {"only": {"customers": ["CUST-001", "CUST-002"]}}}):
        result = solve(snap, steer, churn_price=0)
        assert result.plan["SO-LO-1"] == "VEH-LO-GOOD", f"bumped uninvited under {steer}"


def test_an_authorized_bump_is_declined_when_it_buys_nothing():
    """Both orders weigh the same, so moving 30 days of lateness from one to the
    other is a wash — and it costs a break. Authorisation is permission, never an
    instruction: the solver still has to be better off."""
    snap = _snapshot()
    result = solve(snap, {"may_move": {"also": {"orders": ["SO-LO-1"]}}}, churn_price=0)
    assert result.plan["SO-LO-1"] == "VEH-LO-GOOD"


def test_authorized_bump_rescues_the_order_that_needs_help():
    snap = _snapshot()
    result = solve(snap, AUTH, churn_price=0)
    vehicles = snap.vehicle_by_id()
    assert result.plan["SO-HI-1"] == "VEH-LO-GOOD", "the late order should get the good car"
    assert tardiness(snap.order_by_key()["SO-HI-1"], vehicles[result.plan["SO-HI-1"]]) == 0
    assert result.plan["SO-LO-1"] != "VEH-LO-GOOD", "the authorized order was bumped off it"


def test_an_authorized_bump_works_close_to_delivery_too():
    """The fence's actual bug: it fired BEFORE the authorisation check, so a bump
    a planner had explicitly authorised silently no-oped whenever the victim was
    near delivery. Three did, on 2026-08-25."""
    snap = _snapshot(NEAR_PROMISED)
    result = solve(snap, AUTH, churn_price=0)
    assert result.plan["SO-HI-1"] == "VEH-LO-GOOD"


def test_the_priority_step_decides_who_gets_the_one_good_car():
    """Priority is the planner's step, not a letter on the record: the SAME book
    and the SAME authorisation give opposite answers depending on who was named."""
    snap = _snapshot()
    assert solve(snap, AUTH, churn_price=0).plan["SO-HI-1"] == "VEH-LO-GOOD"
    unnamed = {"may_move": AUTH["may_move"]}
    assert solve(snap, unnamed, churn_price=0).plan["SO-LO-1"] == "VEH-LO-GOOD"


def test_an_unknown_priority_step_is_an_error_not_a_shrug():
    """A mistyped step that silently fell back to 'normal' would look like the
    instruction had been applied."""
    with pytest.raises(ValueError, match="unknown priority step"):
        solve(_snapshot(), {"priority": [{"order": "SO-HI-1", "step": "critical"}]})


def test_break_cost_can_block_an_authorized_bump(monkeypatch):
    """break_cost prices the VICTIM's kept promise. With the config's default the
    rescue is worth paying for; make it prohibitive and the solver declines. It is
    config, not steering — a planner does not type this number, which is why it
    left the override object on 2026-08-26."""
    snap = _snapshot()
    assert solve(snap, AUTH, churn_price=0).plan["SO-HI-1"] == "VEH-LO-GOOD"

    monkeypatch.setitem(CFG["break_cost"], "hard", 100_000.0)
    blocked = solve(snap, AUTH, churn_price=0)
    assert blocked.plan["SO-LO-1"] == "VEH-LO-GOOD", "on-time hard allocation kept"
    assert (
        tardiness(snap.order_by_key()["SO-HI-1"], snap.vehicle_by_id()[blocked.plan["SO-HI-1"]]) > 0
    )
