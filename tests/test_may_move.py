"""`may_move` — who is in play this turn, and the precedence between its parts.

The DEFAULT free set is the orders that need help: late, or with no car. A
settled, on-time order is not in it, and that is the whole of its protection —
the time fence that used to be a second wall was removed on 2026-08-26 because
it fired BEFORE the authorisation check and silently cancelled bumps a planner
had asked for.

Three adjustments, and the precedence is part of the contract:
    never  beats  only  beats  also
`only` narrows (it bounds the whole turn), `also` widens inside it, `never`
removes absolutely. The behaviour change worth pinning is `only`: the `scope`
key it replaced REPLACED the default set, so a scope freed settled on-time
orders nobody had authorised anyone to touch.
"""

from datetime import date

from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import solve

NOW = date(2026, 8, 3)
PROMISED = date(2026, 9, 14)
LATE = date(2026, 10, 14)
ON_TIME = date(2026, 9, 14)


def _order(oid: str, customer_id: str) -> Order:
    # `oid` is the full order key: VSO + car line.
    so_id, line = oid.rsplit("-", 1)
    return Order(
        so_id=so_id,
        line=int(line),
        customer="Dealer",
        customer_id=customer_id,
        sales_model="SM1",
        delivery_date=PROMISED,
        price=40000,
    )


def _unit(vid: str, planned: date) -> Unit:
    return Unit(
        vehicle_id=vid,
        vehicle_classification="Vehicle",
        sales_model="SM1",
        eta_dealer=planned,
    )


def _snapshot() -> Snapshot:
    """Two customers, both LATE on their incumbent; two on-time spares exist."""
    return Snapshot(
        orders=[_order("SO-A-1", "CUST-001"), _order("SO-B-1", "CUST-002")],
        units=[
            _unit("VEH-LATE-A", LATE),
            _unit("VEH-LATE-B", LATE),
            _unit("VEH-GOOD-1", ON_TIME),
            _unit("VEH-GOOD-2", ON_TIME),
        ],
        incumbent={"SO-A-1": "VEH-LATE-A", "SO-B-1": "VEH-LATE-B"},
        disruption={"disrupted_orders": ["SO-A-1", "SO-B-1"]},
        now=NOW,
    )


def _settled_snapshot() -> Snapshot:
    """Nobody needs help: both orders hold an on-time car. A spare sits unused."""
    snap = _snapshot()
    snap.units = [
        _unit("VEH-OK-A", ON_TIME),
        _unit("VEH-OK-B", ON_TIME),
        _unit("VEH-SPARE", date(2026, 9, 1)),  # earlier, so churn would be tempting
    ]
    snap.incumbent = {"SO-A-1": "VEH-OK-A", "SO-B-1": "VEH-OK-B"}
    snap.disruption = {"disrupted_orders": []}
    return snap


def test_the_default_repairs_everyone_who_needs_help():
    result = solve(_snapshot(), {}, churn_price=0)
    assert result.plan["SO-A-1"] != "VEH-LATE-A"
    assert result.plan["SO-B-1"] != "VEH-LATE-B"


def test_a_settled_book_is_left_alone_with_no_steering():
    """The free set is the protection. Nobody is late, so nobody moves — even
    though a car arriving two weeks earlier is sitting free."""
    snap = _settled_snapshot()
    result = solve(snap, {}, churn_price=0)
    assert result.n_changes == 0
    assert result.plan == snap.incumbent


def test_only_narrows_the_default_it_does_not_replace_it():
    """The hole `scope` had: it REPLACED the free set, so naming a settled dealer
    freed their on-time orders and the solver churned them for a car arriving
    sooner. `only` narrows instead, so a settled book stays settled however it is
    sliced."""
    snap = _settled_snapshot()
    result = solve(snap, {"may_move": {"only": {"customers": ["CUST-001"]}}}, churn_price=0)
    assert result.n_changes == 0, "narrowing must never free an order nobody authorised"
    assert result.plan == snap.incumbent


def test_only_holds_everyone_outside_it_even_when_they_need_help():
    snap = _snapshot()
    result = solve(snap, {"may_move": {"only": {"customers": ["CUST-001"]}}}, churn_price=0)
    assert result.plan["SO-A-1"] != "VEH-LATE-A", "the order in play is repaired"
    assert result.plan["SO-B-1"] == "VEH-LATE-B", "an order outside `only` must not move"


def test_only_by_date_window():
    snap = _snapshot()
    outside = solve(
        snap,
        {"may_move": {"only": {"from_date": "2026-01-01", "to_date": "2026-01-31"}}},
        churn_price=0,
    )
    assert outside.n_changes == 0
    inside = solve(
        snap,
        {"may_move": {"only": {"from_date": "2026-09-01", "to_date": "2026-09-30"}}},
        churn_price=0,
    )
    assert inside.plan["SO-A-1"] != "VEH-LATE-A"
    assert inside.plan["SO-B-1"] != "VEH-LATE-B"


def test_never_protects_an_order_that_is_itself_in_trouble():
    """`never` is the only way to hold a LATE order still — "I already called that
    customer". Everything else in the object would leave it in the default set."""
    snap = _snapshot()
    result = solve(snap, {"may_move": {"never": ["SO-A-1"]}}, churn_price=0)
    assert result.plan["SO-A-1"] == "VEH-LATE-A", "never must beat the default free set"
    assert result.plan["SO-B-1"] != "VEH-LATE-B", "and touch nobody else"


def test_never_beats_a_permission_granted_in_the_same_breath():
    snap = _snapshot()
    both = {"may_move": {"also": {"orders": ["SO-A-1"]}, "never": ["SO-A-1"]}}
    assert solve(snap, both, churn_price=0).plan["SO-A-1"] == "VEH-LATE-A"


def test_only_bounds_a_permission_too():
    """`only` beats `also`: an order authorised for displacement that falls
    outside the turn's slice still does not move."""
    snap = _settled_snapshot()
    steer = {
        "may_move": {
            "only": {"customers": ["CUST-001"]},
            "also": {"customers": ["CUST-002"]},
        }
    }
    assert solve(snap, steer, churn_price=0).plan["SO-B-1"] == "VEH-OK-B"


def test_a_whole_vso_can_be_named_instead_of_a_line():
    snap = _snapshot()
    result = solve(snap, {"may_move": {"never": ["SO-A"]}}, churn_price=0)
    assert result.plan["SO-A-1"] == "VEH-LATE-A", "naming the VSO must reach its lines"
