"""Scope DEFINES the working set: with a scope, only matching rows may move and
everything else stays pinned — the mechanism behind "allocate all Colmobil
orders for August" and behind a localized fix that leaves the rest of the book
untouched. Without a scope (and no disruption), nothing is free.
"""

from datetime import date

from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import solve

NOW = date(2026, 8, 3)
PROMISED = date(2026, 9, 14)  # 42 days out — not frozen
LATE = date(2026, 10, 14)
ON_TIME = date(2026, 9, 14)


def _order(oid: str, customer_id: str) -> Order:
    so_id, line = oid.rsplit("-", 1)
    return Order(
        so_id=so_id,
        line=int(line),
        customer="Dealer",
        customer_id=customer_id,
        sales_model="SM1",
        priority="B",
        delivery_date=PROMISED,
        price=40000,
        n_prior_delays=0,
        days_backordered=0,
    )


def _unit(vid: str, planned: date) -> Unit:
    return Unit(
        vehicle_id=vid,
        vehicle_classification="Vehicle",
        sales_model="SM1",
        eta_dealer=planned,
    )


def _snapshot() -> Snapshot:
    # Two customers, each on a late incumbent vehicle; two on-time spares exist.
    return Snapshot(
        orders=[_order("SO-A-1", "CUST-001"), _order("SO-B-1", "CUST-002")],
        units=[
            _unit("VEH-LATE-A", LATE),
            _unit("VEH-LATE-B", LATE),
            _unit("VEH-GOOD-1", ON_TIME),
            _unit("VEH-GOOD-2", ON_TIME),
        ],
        incumbent={"SO-A-1": "VEH-LATE-A", "SO-B-1": "VEH-LATE-B"},
        disruption={"disrupted_orders": []},  # nothing disrupted — only scope frees rows
        now=NOW,
    )


def test_no_scope_no_disruption_changes_nothing():
    result = solve(_snapshot(), {}, lam=0)
    assert result.n_changes == 0
    assert result.plan == _snapshot().incumbent


def test_scope_frees_only_matching_rows():
    snap = _snapshot()
    result = solve(snap, {"scope": {"customers": ["CUST-001"]}}, lam=0)
    # CUST-001's row was free -> moved to an on-time spare; CUST-002 stayed pinned.
    assert result.plan["SO-A-1"] != "VEH-LATE-A"
    assert result.plan["SO-B-1"] == "VEH-LATE-B", "out-of-scope row must not move"


def test_scope_by_date_window():
    snap = _snapshot()
    # A window that excludes the promised date leaves everyone pinned.
    empty = solve(snap, {"scope": {"from_date": "2026-01-01", "to_date": "2026-01-31"}}, lam=0)
    assert empty.n_changes == 0
    # A window that includes it frees both rows.
    both = solve(snap, {"scope": {"from_date": "2026-09-01", "to_date": "2026-09-30"}}, lam=0)
    assert both.plan["SO-A-1"] != "VEH-LATE-A"
    assert both.plan["SO-B-1"] != "VEH-LATE-B"
