"""The planner-facing report is jargon-free and tells the truth about what's stuck.

Two things the run analysis flagged and this guards:
  1. `discrepancy_report` / `planner_report` must classify a broken order that
     can't be re-slotted (only the frozen fence is a hard wall now) as **locked
     in** — the turn-1 truth the live run hid until turn 4.
  2. the reply must carry NO solver internals (λ, objective, Pareto, incumbent,
     min-cost) — those are the jargon the planner shouldn't see.
"""

from datetime import date

from xas_allocation.session import (
    discrepancy_report,
    planner_report,
    repair_and_report,
    run_cycle,
)
from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import repairability

NOW = date(2026, 8, 3)
# MOV: promised far out -> liquid -> movable; FRZ: promised within 14d -> frozen.
MOV_PROMISED = date(2026, 9, 30)
FRZ_PROMISED = date(2026, 8, 10)
UT_PROMISED = date(2026, 9, 30)

# Anything the report might legitimately print is fine; these are the tokens that
# would mean solver internals leaked into a planner reply.
JARGON = ["λ", "lambda", "objective", "pareto", "incumbent", "min-cost", "arc", "sweep"]


def _order(oid: str, model: str, priority: str, promised: date) -> Order:
    so_id, line = oid.rsplit("-", 1)
    return Order(
        so_id=so_id,
        line=int(line),
        customer={"MOV": "Colmobil", "FRZ": "Delek", "UT": "Carasso"}.get(oid.split("-")[0], oid),
        customer_id=oid,
        sales_model=model,
        priority=priority,
        delivery_date=promised,
        price=40000,
        n_prior_delays=0,
        days_backordered=0,
    )


def _unit(vid: str, model: str, planned: date) -> Unit:
    return Unit(
        vehicle_id=vid,
        vehicle_classification="Vehicle",
        sales_model=model,
        eta_dealer=planned,
    )


def _snapshot() -> Snapshot:
    return Snapshot(
        orders=[
            _order("MOV-1", "SM1", "A", MOV_PROMISED),  # disrupted, repairable
            _order("FRZ-1", "SM2", "A", FRZ_PROMISED),  # disrupted, locked in (frozen)
            _order("UT-1", "SM3", "C", UT_PROMISED),  # untouched, on time
        ],
        units=[
            _unit("VEH-MOV-LATE", "SM1", date(2026, 10, 20)),  # MOV's late incumbent
            _unit("VEH-GOOD", "SM1", date(2026, 9, 14)),  # a spare that rescues MOV
            _unit("VEH-FRZ-LATE", "SM2", date(2026, 8, 25)),  # FRZ's late incumbent, stuck
            _unit("VEH-UT-GOOD", "SM3", date(2026, 9, 14)),  # UT's on-time car
        ],
        incumbent={"MOV-1": "VEH-MOV-LATE", "FRZ-1": "VEH-FRZ-LATE", "UT-1": "VEH-UT-GOOD"},
        disruption={
            "delay_days": 30,
            "delayed_vehicles": ["VEH-MOV-LATE", "VEH-FRZ-LATE"],
            "disrupted_orders": ["MOV-1", "FRZ-1"],
        },
        now=NOW,
    )


def test_repairability_classifies_frozen_and_movable():
    snap = _snapshot()
    units = snap.unit_by_id()
    orders = snap.order_by_key()
    assert repairability(orders["MOV-1"], NOW, units["VEH-MOV-LATE"]) == "movable"
    assert repairability(orders["FRZ-1"], NOW, units["VEH-FRZ-LATE"]) == "frozen"


def test_discrepancy_report_flags_locked_in_on_turn_1():
    report = discrepancy_report(_snapshot())
    assert "locked in" in report.lower()
    assert "can be repaired" in report.lower()
    # the frozen order is named on the locked-in side, not sold as fixable
    assert "FRZ-1" in report


def test_planner_report_fixes_movable_and_keeps_frozen_late():
    snap = _snapshot()
    cyc = run_cycle(snap)
    report = planner_report(snap, cyc.chosen, {})
    # the repairable order got the good car and reads on time...
    assert "VEH-GOOD" in report
    assert "1 of 2 delayed orders now on time" in report
    # ...the frozen one is surfaced as locked-in, still late, needing a call
    assert "FRZ-1" in report
    assert "locked in" in report.lower()


def test_report_is_jargon_free():
    report = repair_and_report(_snapshot())
    low = report.lower()
    leaked = [t for t in JARGON if t.lower() in low]
    assert not leaked, f"solver jargon leaked into planner reply: {leaked}"


def test_hard_vehicle_allocation_is_movable_not_locked():
    """DECIDE-3: a broken order riding a REAL (hard) vehicle is no longer walled
    off — it reads 'movable', because hard is expensive-but-movable, not a lock."""
    snap = _snapshot()
    orders = snap.order_by_key()
    units = snap.unit_by_id()
    # MOV rides VEH-MOV-LATE, a real vehicle (kind 'vehicle', so is_hard) that used
    # to be treated as committed/locked; now it is simply movable.
    assert units["VEH-MOV-LATE"].is_hard
    assert repairability(orders["MOV-1"], NOW, units["VEH-MOV-LATE"]) == "movable"


# --------------------------------------------------------------------------
# An order can be BOTH moved and still late -- a bump victim, or a move that
# only narrowed the gap. It belongs in both tables (what we did / what needs a
# call), so the overlap is MARKED, never dropped: dropping it from the call list
# would hide the one order that moved and still failed.
# --------------------------------------------------------------------------


def _moved_but_late_snapshot() -> Snapshot:
    """One disrupted order whose best free car is an improvement and still late."""
    return Snapshot(
        orders=[_order("MOV-1", "SM1", "A", date(2026, 9, 1))],
        units=[
            _unit("VEH-VERY-LATE", "SM1", date(2026, 10, 20)),
            _unit("VEH-LESS-LATE", "SM1", date(2026, 9, 20)),
        ],
        incumbent={"MOV-1": "VEH-VERY-LATE"},
        disruption={
            "delay_days": 30,
            "delayed_vehicles": ["VEH-VERY-LATE"],
            "disrupted_orders": ["MOV-1"],
        },
        now=NOW,
    )


def test_moved_but_still_late_is_in_both_tables_and_marked():
    report = repair_and_report(_moved_but_late_snapshot())
    moved, call_list = report.split("**Still needs your call**")
    assert "VEH-LESS-LATE" in moved, "the swap must show in what-I-moved"
    assert "MOV-1 ↑moved" in call_list, "and the row must stay on the call list, marked"
    assert "not a second count" in call_list, "the marker needs its one-line legend"


def test_no_marker_when_nothing_moved_and_stayed_late():
    """The frozen order in the base fixture is late but was never moved: no
    overlap, so no marker and no legend to explain one."""
    report = repair_and_report(_snapshot())
    assert "↑moved" not in report


def test_the_two_tables_say_what_they_are_for():
    report = repair_and_report(_snapshot())
    assert "**What I moved**" in report
    assert "**Still needs your call**" in report
