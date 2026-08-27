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

The filter dimensions are `models`, `orders` and a promised-date range. The
`customers` dimension went on 2026-08-27 with the customer column the export
never had, so the slices here are by MODEL — two orders, two models.
"""

from datetime import date

from xas_allocation.snapshot import Order, Snapshot, Vehicle
from xas_allocation.solver import solve

NOW = date(2026, 8, 3)
PROMISED = date(2026, 9, 14)
LATE = date(2026, 10, 14)
ON_TIME = date(2026, 9, 14)

# One order id, one order, one car. No line suffix since 2026-08-27.
ORDER_A, ORDER_B = "500001", "500002"
MODEL_A, MODEL_B = "SM-A", "SM-B"


def _order(oid: str, model: str) -> Order:
    return Order(order_id=oid, sales_model=model, delivery_date=PROMISED)


def _vehicle(vid: str, planned: date, model: str) -> Vehicle:
    return Vehicle(vehicle_id=vid, sales_model=model, eta_dealer=planned)


def _snapshot() -> Snapshot:
    """Two orders on two models, both LATE on the car they hold; each has an
    on-time spare of its own model, so either can be repaired independently."""
    return Snapshot(
        orders=[_order(ORDER_A, MODEL_A), _order(ORDER_B, MODEL_B)],
        vehicles=[
            _vehicle("VEH-LATE-A", LATE, MODEL_A),
            _vehicle("VEH-LATE-B", LATE, MODEL_B),
            _vehicle("VEH-GOOD-A", ON_TIME, MODEL_A),
            _vehicle("VEH-GOOD-B", ON_TIME, MODEL_B),
        ],
        allocations={ORDER_A: "VEH-LATE-A", ORDER_B: "VEH-LATE-B"},
        disruption={"disrupted_orders": [ORDER_A, ORDER_B]},
        now=NOW,
    )


def _settled_snapshot() -> Snapshot:
    """Nobody needs help: both orders hold an on-time car. Spares sit unused,
    arriving EARLIER, so churn would be tempting if anything freed them."""
    snap = _snapshot()
    snap.vehicles = [
        _vehicle("VEH-OK-A", ON_TIME, MODEL_A),
        _vehicle("VEH-OK-B", ON_TIME, MODEL_B),
        _vehicle("VEH-SPARE-A", date(2026, 9, 1), MODEL_A),
        _vehicle("VEH-SPARE-B", date(2026, 9, 1), MODEL_B),
    ]
    snap.allocations = {ORDER_A: "VEH-OK-A", ORDER_B: "VEH-OK-B"}
    snap.disruption = {"disrupted_orders": []}
    return snap


def test_the_default_repairs_everyone_who_needs_help():
    result = solve(_snapshot(), {}, churn_price=0)
    assert result.plan[ORDER_A] != "VEH-LATE-A"
    assert result.plan[ORDER_B] != "VEH-LATE-B"


def test_a_settled_book_is_left_alone_with_no_steering():
    """The free set is the protection. Nobody is late, so nobody moves — even
    though a car arriving two weeks earlier is sitting free."""
    snap = _settled_snapshot()
    result = solve(snap, {}, churn_price=0)
    assert result.n_changes == 0
    assert result.plan == snap.allocations


def test_only_narrows_the_default_it_does_not_replace_it():
    """The hole `scope` had: it REPLACED the free set, so naming a settled slice
    freed its on-time orders and the solver churned them for a car arriving
    sooner. `only` narrows instead, so a settled book stays settled however it is
    sliced."""
    snap = _settled_snapshot()
    result = solve(snap, {"may_move": {"only": {"models": [MODEL_A]}}}, churn_price=0)
    assert result.n_changes == 0, "narrowing must never free an order nobody authorised"
    assert result.plan == snap.allocations


def test_only_holds_everyone_outside_it_even_when_they_need_help():
    snap = _snapshot()
    result = solve(snap, {"may_move": {"only": {"models": [MODEL_A]}}}, churn_price=0)
    assert result.plan[ORDER_A] != "VEH-LATE-A", "the order in play is repaired"
    assert result.plan[ORDER_B] == "VEH-LATE-B", "an order outside `only` must not move"


def test_only_by_date_window():
    """A date range is against the PROMISED date — "just fix August" is about what
    was promised then, not what arrives then."""
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
    assert inside.plan[ORDER_A] != "VEH-LATE-A"
    assert inside.plan[ORDER_B] != "VEH-LATE-B"


def test_only_by_order_id():
    snap = _snapshot()
    result = solve(snap, {"may_move": {"only": {"orders": [ORDER_A]}}}, churn_price=0)
    assert result.plan[ORDER_A] != "VEH-LATE-A"
    assert result.plan[ORDER_B] == "VEH-LATE-B"


def test_never_protects_an_order_that_is_itself_in_trouble():
    """`never` is the only way to hold a LATE order still — "I already called that
    customer". Everything else in the object would leave it in the default set."""
    snap = _snapshot()
    result = solve(snap, {"may_move": {"never": [ORDER_A]}}, churn_price=0)
    assert result.plan[ORDER_A] == "VEH-LATE-A", "never must beat the default free set"
    assert result.plan[ORDER_B] != "VEH-LATE-B", "and touch nobody else"


def test_never_beats_a_permission_granted_in_the_same_breath():
    snap = _snapshot()
    both = {"may_move": {"also": {"orders": [ORDER_A]}, "never": [ORDER_A]}}
    assert solve(snap, both, churn_price=0).plan[ORDER_A] == "VEH-LATE-A"


def test_only_bounds_a_permission_too():
    """`only` beats `also`: an order authorised for displacement that falls
    outside the turn's slice still does not move."""
    snap = _settled_snapshot()
    steer = {"may_move": {"only": {"models": [MODEL_A]}, "also": {"models": [MODEL_B]}}}
    assert solve(snap, steer, churn_price=0).plan[ORDER_B] == "VEH-OK-B"


def test_an_order_is_named_by_its_whole_id_and_nothing_less():
    """One key level: a name that is not the id exactly matches nothing. A partial
    id must not protect an order by accident — a silent near-match is how an
    instruction looks applied when it did nothing."""
    snap = _snapshot()
    result = solve(snap, {"may_move": {"never": ["50000"]}}, churn_price=0)
    assert result.plan[ORDER_A] != "VEH-LATE-A"
    assert result.plan[ORDER_B] != "VEH-LATE-B"
