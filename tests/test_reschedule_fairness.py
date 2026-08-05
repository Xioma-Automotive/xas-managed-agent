"""DECIDE-11: an already-rescheduled order is protected from being bumped again.

`times_rescheduled` (reschedules our repair loop caused) escalates W(o), so when
two otherwise-identical orders contend for one on-time vehicle, the solver gives
it to the one that has already been bumped — spreading the pain instead of
hitting the same dealer every cycle.
"""

from datetime import date

from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import effective_weight, solve, tardiness

NOW = date(2026, 8, 3)
PROMISED = date(2026, 9, 14)  # 42 days out — not frozen
ON_TIME = date(2026, 9, 14)
LATE = date(2026, 10, 14)


def _order(oid: str, times_rescheduled: int) -> Order:
    return Order(
        order_id=oid,
        so_id=oid.rsplit("-", 1)[0],
        customer="Dealer",
        customer_id="CUST-001",
        sales_model="SM1",
        priority="C",
        promised_date=PROMISED,
        eta_date=PROMISED,
        price=40000,
        n_prior_delays=0,
        days_backordered=0,
        times_rescheduled=times_rescheduled,
    )


def _unit(vid: str, planned: date) -> Unit:
    return Unit(
        vehicle_id=vid,
        kind="vehicle",
        sales_model="SM1",
        planned_delivery_date=planned,
        location_state="sea",
        po_ref="PO-150-1-1",
        pdn="PDN-150",
        committed=False,
    )


def test_weight_escalates_with_reschedules():
    base = _order("SO-A", 0)
    bumped = _order("SO-B", 2)
    assert effective_weight(bumped, {}) > effective_weight(base, {})


def test_already_bumped_order_wins_the_on_time_vehicle():
    a = _order("SO-A", 2)  # already rescheduled twice — protect it
    b = _order("SO-B", 0)  # never rescheduled
    units = [
        _unit("VEH-GOOD", ON_TIME),  # the single on-time vehicle (scarce)
        _unit("VEH-LATE-A", LATE),
        _unit("VEH-LATE-B", LATE),
    ]
    snap = Snapshot(
        orders=[a, b],
        units=units,
        incumbent={"SO-A": "VEH-LATE-A", "SO-B": "VEH-LATE-B"},
        disruption={"disrupted_orders": ["SO-A", "SO-B"]},
        now=NOW,
    )
    result = solve(snap, {}, lam=0)
    by_id = snap.unit_by_id()
    assert result.plan["SO-A"] == "VEH-GOOD", "the already-bumped order should be protected"
    assert tardiness(a, by_id[result.plan["SO-A"]]) == 0
    assert tardiness(b, by_id[result.plan["SO-B"]]) > 0
