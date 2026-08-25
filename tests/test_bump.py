"""DECIDE-13: an untouched order is bumped ONLY when the planner authorizes it.

Without `bump`, a disrupted high-priority row stays late even though an untouched
low-priority row is sitting on the one on-time vehicle. With `bump` authorizing
that row, the solver displaces it (it's cheaper to make the C-priority row late
than the A-priority one) and the disrupted row is rescued.
"""

from datetime import date

from xas_allocation.session import bump_candidates
from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import solve, tardiness

NOW = date(2026, 8, 3)
PROMISED = date(2026, 9, 14)  # 42 days out — not frozen
LATE = date(2026, 10, 14)
ON_TIME = date(2026, 9, 14)


def _order(oid: str, customer_id: str, priority: str) -> Order:
    # `oid` is the full order key: VSO + car line.
    so_id, line = oid.rsplit("-", 1)
    return Order(
        so_id=so_id,
        line=int(line),
        customer=customer_id,
        customer_id=customer_id,
        sales_model="SM1",
        priority=priority,
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
    # HI (A) is disrupted onto a late vehicle; LO (C, untouched) holds the only
    # on-time vehicle; one spare late vehicle exists for LO to fall back to.
    return Snapshot(
        orders=[_order("SO-HI-1-1", "CUST-001", "A"), _order("SO-LO-1-1", "CUST-002", "C")],
        units=[
            _unit("VEH-HI-LATE", LATE),
            _unit("VEH-LO-GOOD", ON_TIME),
            _unit("VEH-SPARE-LATE", LATE),
        ],
        incumbent={"SO-HI-1-1": "VEH-HI-LATE", "SO-LO-1-1": "VEH-LO-GOOD"},
        disruption={"disrupted_orders": ["SO-HI-1-1"]},
        now=NOW,
    )


def test_without_authorization_no_bump_high_priority_stays_late():
    snap = _snapshot()
    result = solve(snap, {}, lam=0)
    units = snap.unit_by_id()
    assert result.plan["SO-LO-1-1"] == "VEH-LO-GOOD", "untouched row must not move uninvited"
    assert tardiness(snap.order_by_key()["SO-HI-1-1"], units[result.plan["SO-HI-1-1"]]) > 0
    # ...and the agent can see the bump it would need to propose.
    cands = bump_candidates(snap, result)
    assert any(c["row"] == "SO-LO-1-1" for c in cands)


def test_authorized_bump_rescues_the_disrupted_row():
    snap = _snapshot()
    result = solve(snap, {"bump": {"orders": ["SO-LO-1-1"]}}, lam=0)
    units = snap.unit_by_id()
    assert result.plan["SO-HI-1-1"] == "VEH-LO-GOOD", (
        "disrupted A-priority row should get the good car"
    )
    assert tardiness(snap.order_by_key()["SO-HI-1-1"], units[result.plan["SO-HI-1-1"]]) == 0
    assert result.plan["SO-LO-1-1"] != "VEH-LO-GOOD", "the C-priority row was bumped off it"


def test_break_cost_can_block_an_authorized_bump():
    """DECIDE-3: bumping the on-time LO row off its hard vehicle costs
    BREAK_COST['hard']. With the default it's worth paying to rescue the disrupted
    A-priority row; make it prohibitive and the solver declines — LO keeps its car
    and HI stays late. The bump victim's kept promise is what the break prices."""
    snap = _snapshot()
    auth = {"bump": {"orders": ["SO-LO-1-1"]}}
    # Default: the rescue is worth the hard break.
    assert solve(snap, auth, lam=0).plan["SO-HI-1-1"] == "VEH-LO-GOOD"
    # Prohibitive break: disturbing LO's on-time hard allocation is no longer worth
    # it, so HI stays on its late car.
    blocked = solve(snap, {**auth, "break_cost": {"hard": 100000}}, lam=0)
    assert blocked.plan["SO-LO-1-1"] == "VEH-LO-GOOD", "on-time hard allocation kept"
    assert (
        tardiness(snap.order_by_key()["SO-HI-1-1"], snap.unit_by_id()[blocked.plan["SO-HI-1-1"]])
        > 0
    )
