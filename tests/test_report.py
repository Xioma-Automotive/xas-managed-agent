"""The planner-facing report is jargon-free and tells the truth about what's stuck.

Two things this guards:
  1. The fence protects a KEPT promise, not any promise. An order close to
     delivery whose car arrives ON TIME is untouchable, and its car cannot be
     given away. One that is close to delivery and already LATE is movable — the
     promise is broken either way, so refusing to try just leaves a better free
     car unused. (Before 2026-08-25 both were frozen, and on the fabricated book
     that left nine orders "locked in" with a better car standing idle.)
  2. the reply must carry NO solver internals (λ, objective, Pareto, incumbent,
     min-cost) — those are the jargon the planner shouldn't see.
"""

import json
from datetime import date

from xas_allocation.session import (
    bump_candidates,
    discrepancy_report,
    exclusion_note,
    planner_report,
    repair_and_report,
    run_cycle,
)
from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import is_locked_in, repairability

NOW = date(2026, 8, 3)
# MOV: promised far out, car late -> movable.
# NEAR: promised inside the 14-day fence AND its car is late -> STILL movable.
# KEPT: promised inside the fence and its car is on time -> locked in, untouchable.
MOV_PROMISED = date(2026, 9, 30)
NEAR_PROMISED = date(2026, 8, 10)
KEPT_PROMISED = date(2026, 8, 10)
UT_PROMISED = date(2026, 9, 30)

# Anything the report might legitimately print is fine; these are the tokens that
# would mean solver internals leaked into a planner reply.
JARGON = ["λ", "lambda", "objective", "pareto", "incumbent", "min-cost", "arc", "sweep"]


def _order(oid: str, model: str, priority: str, promised: date) -> Order:
    # `oid` is the full order key: VSO + car line.
    so_id, line = oid.rsplit("-", 1)
    return Order(
        so_id=so_id,
        line=int(line),
        customer={"MOV": "Colmobil", "NEAR": "Delek", "KEPT": "Talcar", "UT": "Carasso"}.get(
            oid.split("-")[0], oid
        ),
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
            _order("NEAR-1", "SM2", "A", NEAR_PROMISED),  # near delivery AND late -> movable
            _order("KEPT-1", "SM4", "A", KEPT_PROMISED),  # near delivery, on time -> untouchable
            _order("UT-1", "SM3", "C", UT_PROMISED),  # untouched, on time
        ],
        units=[
            _unit("VEH-MOV-LATE", "SM1", date(2026, 10, 20)),  # MOV's late incumbent
            _unit("VEH-GOOD", "SM1", date(2026, 9, 14)),  # a spare that rescues MOV
            _unit("VEH-NEAR-LATE", "SM2", date(2026, 8, 25)),  # NEAR's late incumbent
            _unit("VEH-KEPT-OK", "SM4", date(2026, 8, 10)),  # KEPT's on-time car — hands off
            _unit("VEH-UT-GOOD", "SM3", date(2026, 9, 14)),  # UT's on-time car
        ],
        incumbent={
            "MOV-1": "VEH-MOV-LATE",
            "NEAR-1": "VEH-NEAR-LATE",
            "KEPT-1": "VEH-KEPT-OK",
            "UT-1": "VEH-UT-GOOD",
        },
        disruption={
            "delay_days": 30,
            "delayed_vehicles": ["VEH-MOV-LATE", "VEH-NEAR-LATE"],
            "disrupted_orders": ["MOV-1", "NEAR-1"],
        },
        now=NOW,
    )


def test_the_fence_protects_a_kept_promise_not_a_broken_one():
    """The whole point of change A. Same promised date on both orders; the only
    difference is whether the car actually arrives on time."""
    snap = _snapshot()
    units, orders = snap.unit_by_id(), snap.order_by_key()
    # on time and close to delivery -> nobody may touch it
    assert repairability(orders["KEPT-1"], NOW, units["VEH-KEPT-OK"]) == "frozen"
    # SAME promised date, but the car is late -> worth trying
    assert repairability(orders["NEAR-1"], NOW, units["VEH-NEAR-LATE"]) == "movable"
    # and far-out-and-late is movable as it always was
    assert repairability(orders["MOV-1"], NOW, units["VEH-MOV-LATE"]) == "movable"


def test_a_locked_in_order_keeps_its_car_out_of_the_pool():
    """Untouchable has to mean its CAR is untouchable too — otherwise the solver
    can hand it to someone else and the protected order silently loses it."""
    snap = _snapshot()
    cyc = run_cycle(snap)
    assert cyc.chosen.plan["KEPT-1"] == "VEH-KEPT-OK"


def test_discrepancy_report_tries_a_late_order_close_to_delivery():
    report = discrepancy_report(_snapshot())
    assert "can be repaired" in report.lower()
    # NEAR-1 is inside the fence but late, so it is offered as repairable rather
    # than written off — the behaviour this change bought.
    repairable = report.split("can be repaired")[1]
    assert "NEAR-1" in repairable


def test_planner_report_fixes_what_it_can():
    snap = _snapshot()
    cyc = run_cycle(snap)
    report = planner_report(snap, cyc.chosen, {})
    # the repairable order got the good car and reads on time...
    assert "VEH-GOOD" in report
    assert "1 of 2 delayed orders now on time" in report
    # ...and the near-delivery late one is still named, with a real reason
    assert "NEAR-1" in report


def test_a_steered_lambda_is_honoured_even_at_zero():
    """`lambda: 0` is a real steer (the first sweep value), not "unset".

    A falsiness test here silently substitutes the mid-sweep default, so the
    planner asks for max churn and gets a compromise plan with no complaint."""
    snap = _snapshot()
    assert run_cycle(snap, {"lambda": 0}).chosen_lambda == 0
    assert run_cycle(snap, {"lambda": 999}).chosen_lambda == 999, "off-sweep re-solves"
    assert run_cycle(snap).chosen_lambda == 25, "no steer -> middle of the sweep"


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


# --- what is NOT in the plan (real data is patchy) ---------------------------

EXCLUDED_META = {
    "excluded": {
        "orders_seen": 25,
        "orders_kept": 1,
        "order_drops": {"no_model_on_the_order": 23, "no_promised_date": 1},
        "units_seen": 432,
        "units_kept": 10,
        "unit_drops": {"no_arrival_date": 21},
        "orders_with_no_eligible_car": ["502391-1"],
    },
    "conflicts": [{"vehicle": "10831", "orders": ["502323", "502324", "502325"]}],
}


def _snapshot_with(meta: dict) -> Snapshot:
    """A snapshot with nothing late, so only the exclusion note can show up."""
    order = Order(
        so_id="502361",
        line=1,
        customer="Colmobil",
        customer_id="CUST-001",
        sales_model="T5040UECLMQ0009",
        priority="C",
        delivery_date=MOV_PROMISED,
        price=0.0,
        n_prior_delays=0,
        days_backordered=0,
    )
    unit = Unit(
        vehicle_id="930103",
        vehicle_classification="Vehicle",
        sales_model="T5040UECLMQ0009",
        eta_dealer=MOV_PROMISED,
    )
    return Snapshot(
        orders=[order],
        units=[unit],
        incumbent={order.key: unit.vehicle_id},
        disruption={},
        now=NOW,
        meta=meta,
    )


def test_the_excluded_orders_are_reported_in_plain_words():
    note = exclusion_note(_snapshot_with(EXCLUDED_META))
    assert "24 of 25 sales orders are not in this plan" in note
    assert "no model on the order" in note
    assert "no promised date" in note
    # a reason CODE must never reach the planner
    assert "no_model_on_the_order" not in note
    assert "order_drops" not in note


def test_a_double_booked_car_is_surfaced_not_swallowed():
    note = exclusion_note(_snapshot_with(EXCLUDED_META))
    assert "Car 10831 is allocated to 3 orders at once" in note
    assert "502323" in note


def test_an_order_with_no_car_and_the_pool_size_are_both_named():
    note = exclusion_note(_snapshot_with(EXCLUDED_META))
    assert "no compatible car in stock or on order" in note
    assert "10 of 432" in note


def test_the_note_leads_the_turn_one_report():
    """It has to come BEFORE the discrepancy map — a planner who reads the plan
    first has already formed the wrong picture of how much it covers."""
    report = discrepancy_report(_snapshot_with(EXCLUDED_META))
    assert report.startswith("**24 of 25 sales orders are not in this plan")


def test_a_pull_that_filtered_nothing_says_nothing():
    """The fabricated dataset excludes nothing, so the note must be silent rather
    than printing a row of zeroes."""
    assert exclusion_note(_snapshot_with({})) == ""
    assert discrepancy_report(_snapshot_with({})).startswith("No orders are late")


def test_a_projection_gap_is_not_blamed_on_the_orders():
    """A field the MCP does not return cannot be fixed by completing an order, so
    the note must not send the planner into the app looking for it."""
    note = exclusion_note(
        _snapshot_with({**EXCLUDED_META, "projection_gaps": {"get_vehicles": ["SalesModel"]}})
    )
    assert "not returning some of the fields" in note
    assert "SalesModel" in note
    assert "not something you can correct on the orders themselves" in note
    # and it comes first — it explains every count under it
    assert note.index("not returning") < note.index("not in this plan")
    # the drop counts must NOT then be blamed on incomplete orders: with a gap,
    # every order looks incomplete because the field never arrived
    assert "these counts mean nothing until the system returns them" in note
    assert "need completing in the system" not in note


def test_without_a_projection_gap_the_drops_ARE_the_orders():
    note = exclusion_note(_snapshot_with(EXCLUDED_META))
    assert "need completing in the system" in note
    assert "counts mean nothing" not in note


def test_the_plan_is_written_to_a_file_not_just_reported(tmp_path):
    """Observed 2026-08-25: the only record of a turn's allocations was the
    markdown table in the transcript, which the agent then RETYPED into its reply
    — four turns running, and a later turn answered "show me the new allocations"
    from that prose rather than from data. A retyped table can lose a row or
    mistype a vehicle id, and nothing would catch it."""
    snap = _snapshot()
    out = tmp_path / "plan.json"
    repair_and_report(snap, {}, plan_path=out)
    saved = json.loads(out.read_text())

    # every order is in the file, whether it moved or not
    assert {r["order"] for r in saved["allocations"]} == set(snap.order_by_key())
    # and the cars in the file ARE the solver's plan, not a re-derivation
    cyc = run_cycle(snap, {})
    assert {
        r["order"]: r["now_car"] for r in saved["allocations"] if r["now_car"]
    } == cyc.chosen.plan
    # the protected order is recorded keeping its car, and nothing reads as bumped
    kept = next(r for r in saved["allocations"] if r["order"] == "KEPT-1")
    assert kept["now_car"] == "VEH-KEPT-OK" and kept["bumped"] is False


def test_a_bump_is_never_offered_for_an_order_that_cannot_accept_a_car(tmp_path):
    """Observed 2026-08-25: three authorized bumps all no-oped, because every
    rescue target was locked in — it could not take the freed car. The freed cars
    would have sat idle and the targets stayed late, so the authorization was
    spent on nothing."""
    snap = _snapshot()
    cyc = run_cycle(snap, {})
    for c in bump_candidates(snap, cyc.chosen):
        target = snap.order_by_key()[c["would_rescue"]]
        inc = snap.unit_by_id().get(snap.incumbent.get(c["would_rescue"], ""))
        assert not is_locked_in(target, snap.now, inc), (
            f"{c['row']} offered to rescue {c['would_rescue']}, which cannot move"
        )
