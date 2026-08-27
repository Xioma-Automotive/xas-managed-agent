"""The planner-facing report is jargon-free and tells the truth about what is left.

Two things this guards:
  1. A settled, on-time order keeps its car and its car stays out of the pool —
     protected by not being in the free set, since the time fence that used to be
     a second wall was removed on 2026-08-26. An order that is close to delivery
     and already LATE is repaired like any other: the promise is broken either
     way, so refusing to try just leaves a better free car unused.
  2. the reply carries NO solver internals (churn price, objective, Pareto,
     min-cost, arcs) — the jargon the planner should never see.
"""

import json
from dataclasses import replace
from datetime import date

from xas_allocation.session import (
    bump_candidates,
    discrepancy_report,
    exclusion_note,
    plan_rows,
    planner_report,
    repair_and_report,
    run_cycle,
)
from xas_allocation.snapshot import Order, Snapshot, Vehicle

NOW = date(2026, 8, 3)
# MOV:  promised far out, car late -> repaired.
# NEAR: promised days away and its car is late -> repaired too, same as any other.
# KEPT: promised days away and its car is on time -> settled, so nobody touches it.
MOV_PROMISED = date(2026, 9, 30)
NEAR_PROMISED = date(2026, 8, 10)
KEPT_PROMISED = date(2026, 8, 10)
UT_PROMISED = date(2026, 9, 30)

# Anything the report might legitimately print is fine; these are the tokens that
# would mean solver internals leaked into a planner reply.
JARGON = ["λ", "lambda", "objective", "pareto", "allocations", "min-cost", "arc", "sweep"]


def _order(oid: str, model: str, promised: date) -> Order:
    return Order(order_id=oid, sales_model=model, delivery_date=promised)


def _vehicle(vid: str, model: str, planned: date) -> Vehicle:
    return Vehicle(vehicle_id=vid, sales_model=model, eta_dealer=planned)


def _snapshot() -> Snapshot:
    return Snapshot(
        orders=[
            _order("MOV", "SM1", MOV_PROMISED),  # late, far from delivery
            _order("NEAR", "SM2", NEAR_PROMISED),  # late AND days from delivery
            _order("KEPT", "SM4", KEPT_PROMISED),  # settled and on time -> untouched
            _order("UT", "SM3", UT_PROMISED),  # settled and on time -> untouched
        ],
        vehicles=[
            _vehicle("VEH-MOV-LATE", "SM1", date(2026, 10, 20)),  # the late car MOV holds
            _vehicle("VEH-GOOD", "SM1", date(2026, 9, 14)),  # a spare that rescues MOV
            _vehicle("VEH-NEAR-LATE", "SM2", date(2026, 8, 25)),  # the late car NEAR holds
            _vehicle("VEH-KEPT-OK", "SM4", date(2026, 8, 10)),  # KEPT's on-time car — hands off
            _vehicle("VEH-UT-GOOD", "SM3", date(2026, 9, 14)),  # UT's on-time car
        ],
        allocations={
            "MOV": "VEH-MOV-LATE",
            "NEAR": "VEH-NEAR-LATE",
            "KEPT": "VEH-KEPT-OK",
            "UT": "VEH-UT-GOOD",
        },
        # Derived, not declared: the manifest ("30 days on 2 vehicles") went with
        # the fabricated source on 2026-08-27 — nothing records one.
        disruption={"disrupted_orders": ["MOV", "NEAR"]},
        now=NOW,
    )


def test_a_settled_order_keeps_its_car_and_its_car_stays_out_of_the_pool():
    """Untouched has to mean its CAR is untouched too — otherwise the solver can
    hand it to someone else and the settled order silently loses it. Nothing walls
    it off; it is simply not in the free set."""
    snap = _snapshot()
    cyc = run_cycle(snap)
    assert cyc.chosen.plan["KEPT"] == "VEH-KEPT-OK"
    assert cyc.chosen.plan["UT"] == "VEH-UT-GOOD"


def test_discrepancy_report_offers_every_late_order_including_one_near_delivery():
    """NEAR-1 is days from delivery and late. It used to be written off as "locked
    in"; now it is offered like any other, because a late order is always worth
    trying."""
    report = discrepancy_report(_snapshot())
    assert "may get these back on track" in report
    assert "NEAR" in report
    assert "locked in" not in report.lower()


def test_planner_report_fixes_what_it_can():
    snap = _snapshot()
    cyc = run_cycle(snap)
    report = planner_report(snap, cyc.chosen, {})
    # the repairable order got the good car and reads on time...
    assert "VEH-GOOD" in report
    assert "1 of 2 delayed orders now on time" in report
    # ...and the near-delivery late one is still named, with a real reason
    assert "NEAR" in report


def test_a_steered_churn_price_is_honoured_even_at_zero():
    """`churn_price: 0` is a real steer (the first sweep value), not "unset".

    A falsiness test here silently substitutes the mid-sweep default, so the
    planner asks for max churn and gets a compromise plan with no complaint."""
    snap = _snapshot()
    assert run_cycle(snap, {"churn_price": 0}).chosen_churn_price == 0
    assert run_cycle(snap, {"churn_price": 999}).chosen_churn_price == 999, "off-sweep re-solves"
    assert run_cycle(snap).chosen_churn_price == 25, "no steer -> middle of the sweep"


def test_report_is_jargon_free():
    report = repair_and_report(_snapshot())
    low = report.lower()
    leaked = [t for t in JARGON if t.lower() in low]
    assert not leaked, f"solver jargon leaked into planner reply: {leaked}"


def test_a_late_order_is_moved_not_walled_off():
    """DECIDE-3: taking a car off an order is priced, never forbidden — and this
    order's promise is already broken, so here it is free."""
    snap = _snapshot()
    assert run_cycle(snap).chosen.plan["MOV"] == "VEH-GOOD"


# --------------------------------------------------------------------------
# An order can be BOTH moved and still late -- a bump victim, or a move that
# only narrowed the gap. It belongs in both tables (what we did / what needs a
# call), so the overlap is MARKED, never dropped: dropping it from the call list
# would hide the one order that moved and still failed.
# --------------------------------------------------------------------------


def _moved_but_late_snapshot() -> Snapshot:
    """One disrupted order whose best free car is an improvement and still late."""
    return Snapshot(
        orders=[_order("MOV", "SM1", date(2026, 9, 1))],
        vehicles=[
            _vehicle("VEH-VERY-LATE", "SM1", date(2026, 10, 20)),
            _vehicle("VEH-LESS-LATE", "SM1", date(2026, 9, 20)),
        ],
        allocations={"MOV": "VEH-VERY-LATE"},
        disruption={"disrupted_orders": ["MOV"]},
        now=NOW,
    )


def test_moved_but_still_late_is_in_both_tables_and_marked():
    report = repair_and_report(_moved_but_late_snapshot())
    moved, call_list = report.split("**Still needs your call**")
    assert "VEH-LESS-LATE" in moved, "the swap must show in what-I-moved"
    assert "MOV ↑moved" in call_list, "and the row must stay on the call list, marked"
    assert "not a second count" in call_list, "the marker needs its one-line legend"


def test_no_marker_when_nothing_moved_and_stayed_late():
    """NEAR-1 in the base fixture is late and has no compatible car to move to: no
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
        "order_drops": {"no_model": 23, "no_promised_date": 1},
        "vehicles_seen": 432,
        "vehicles_kept": 10,
        "vehicle_drops": {"no_arrival_date": 21},
        "orders_with_no_eligible_car": ["502391"],
    },
    "conflicts": [{"vehicle": "10831", "orders": ["502323", "502324", "502325"]}],
}


def _snapshot_with(meta: dict) -> Snapshot:
    """A snapshot with nothing late and every order holding a car, so only the
    exclusion note can show up."""
    order = Order(order_id="502361", sales_model="T6480J1BXLX0018", delivery_date=MOV_PROMISED)
    vehicle = Vehicle(vehicle_id="930103", sales_model="T6480J1BXLX0018", eta_dealer=MOV_PROMISED)
    return Snapshot(
        orders=[order],
        vehicles=[vehicle],
        allocations={order.key: vehicle.vehicle_id},
        disruption={},
        now=NOW,
        meta=meta,
    )


def test_the_excluded_orders_are_reported_in_plain_words():
    note = exclusion_note(_snapshot_with(EXCLUDED_META))
    assert "24 of 25 orders are not in this plan" in note
    assert "no model on the order" in note
    assert "no promised date" in note
    # a reason CODE must never reach the planner
    assert "no_model" not in note
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
    assert report.startswith("**24 of 25 orders are not in this plan")


def test_a_pull_that_filtered_nothing_says_nothing():
    """A clean book excludes nothing and leaves nobody without a car, so the note
    must be silent rather than printing a row of zeroes."""
    assert exclusion_note(_snapshot_with({})) == ""
    assert discrepancy_report(_snapshot_with({})).startswith("No orders are late")


def test_an_order_holding_no_car_is_named_even_when_nothing_is_late():
    """The pure-unallocated book: every order needs a car and none is late, so
    "No orders are late" on its own would read as "nothing to do"."""
    snap = _snapshot_with({})
    snap.allocations = {}
    note = exclusion_note(snap)
    assert "1 of 1 orders hold no car yet" in note
    assert "502361" in note
    assert "they need allocating, not repairing" in note


def test_the_drops_are_the_orders_and_say_what_to_do_about_them():
    """There is no "the system is not returning this field" case any more: a CSV
    column either is in the header — checked at read time, `datasource.read_rows`
    raises naming it — or every row has it. So a drop IS an incomplete order."""
    note = exclusion_note(_snapshot_with(EXCLUDED_META))
    assert "need completing in the system" in note
    assert "not returning" not in note


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
    # the settled order is recorded keeping its car, and nothing reads as bumped
    kept = next(r for r in saved["allocations"] if r["order"] == "KEPT")
    assert kept["now_car"] == "VEH-KEPT-OK" and kept["bumped"] is False
    # and the config that priced it is named, so the plan can be traced to it
    assert saved["solver_version"]


def test_a_bump_is_only_ever_offered_on_a_settled_order():
    """Offering to displace an order that is already late would spend the planner's
    authorisation on nobody's gain — that order is in the free set already."""
    snap = _snapshot()
    cyc = run_cycle(snap, {})
    for c in bump_candidates(snap, cyc.chosen):
        victim, target = c["row"], c["would_rescue"]
        assert victim not in snap.disruption["disrupted_orders"]
        assert snap.allocations.get(victim), f"{victim} has no car to give up"
        assert target != victim


def test_bump_candidates_are_offered_lightest_first():
    """The planner is shown who could be displaced in the order they should think
    about it: whoever they have said matters least, which with nothing steered is
    everyone equally and so is simply key order."""
    snap = _snapshot()
    cyc = run_cycle(snap, {})
    steer = {"priority": [{"order": "KEPT", "step": "urgent"}]}
    rows = [c["row"] for c in bump_candidates(snap, cyc.chosen, steer)]
    if "KEPT" in rows and len(rows) > 1:
        assert rows[-1] == "KEPT", "the order the planner called urgent is offered last"


def test_the_fleet_wide_authorisation_is_said_in_plain_words():
    """`_who` takes a filter; handed `True` it raises and the whole report dies.
    So the boolean form must be phrased BEFORE `_who` is ever reached. The
    planner also has to see that this permission is for one turn, since it is the
    only key that expires."""
    snap = _snapshot()
    steer = {"may_move": {"also": True}}
    cyc = run_cycle(snap, steer)
    report = planner_report(snap, cyc.chosen, steer)
    assert "anyone still settled" in report
    assert "this turn" in report
    low = report.lower()
    leaked = [t for t in JARGON if t.lower() in low]
    assert not leaked, f"solver jargon leaked into planner reply: {leaked}"


def _named(snap: Snapshot, names: dict[str, str]) -> Snapshot:
    """The same book with a client on each order — one client holding two of them,
    which is the case the agent has to group rather than treat as one order."""
    return Snapshot(
        orders=[replace(o, customer=names.get(o.key, "")) for o in snap.orders],
        vehicles=snap.vehicles,
        allocations=snap.allocations,
        disruption=snap.disruption,
        now=snap.now,
    )


NAMES = {"MOV": "Delek Motors Fleet", "NEAR": "Delek Motors Fleet", "KEPT": "Shira Peretz"}


def test_every_planner_facing_table_names_the_client():
    """The planner is asked which clients should come first, so they must be able
    to read the client off the tables they are answering from — the late list,
    what moved, and the call list. An id-only table forces them to look the name
    up in another system, which is where a wrong order gets prioritised."""
    # NEAR is deliberately left unnamed and it is late, so it appears in both
    # tables: an order with no client must render a dash, never an empty cell,
    # which reads as a broken table rather than as missing data.
    snap = _named(_snapshot(), {"MOV": "Delek Motors Fleet", "KEPT": "Shira Peretz"})
    cyc = run_cycle(snap)

    for table in (discrepancy_report(snap), planner_report(snap, cyc.chosen)):
        assert "| Customer |" in table
        assert "Delek Motors Fleet" in table
        assert "| — |" in table


def test_the_client_is_in_the_plan_file_and_the_bump_list():
    """plan.json is where every follow-up is answered from, so "which of Delek's
    orders moved?" has to be a read of the file, not a re-derivation. And the bump
    list is what the agent asks WITH — the planner cannot judge a displacement
    without knowing whose order it is."""
    snap = _named(_snapshot(), NAMES)
    cyc = run_cycle(snap)
    rows = plan_rows(snap, cyc.chosen)
    by_order = {r["order"]: r for r in rows}
    assert by_order["MOV"]["customer"] == "Delek Motors Fleet"
    assert by_order["NEAR"]["customer"] == "Delek Motors Fleet"
    # unnamed orders are represented, not dropped
    assert by_order["UT"]["customer"] == ""
    # one client, two orders: grouping is the agent's job and the data supports it
    assert sum(1 for r in rows if r["customer"] == "Delek Motors Fleet") == 2

    for cand in bump_candidates(snap, cyc.chosen):
        assert "customer" in cand


def test_the_steering_summary_still_renders_a_may_move_filter():
    """Regression guard: the client-name helper was briefly named `_who` too and
    shadowed the filter renderer, so any turn with `may_move.only` set would have
    crashed on the headline — a path no test covered."""
    snap = _named(_snapshot(), NAMES)
    override = {"may_move": {"only": {"orders": ["MOV"]}, "also": True}}
    head = planner_report(snap, run_cycle(snap, override).chosen, override).splitlines()[0]
    assert "working only MOV" in head
    assert "bumping" in head


# --- The planner channel -----------------------------------------------------
# `web.py` drops every builtin tool result, so a report printed in the sandbox
# reaches nobody. Text inside these markers is forwarded to the planner's screen,
# which is what stops the agent retyping every table into its own reply.


def test_show_wraps_text_and_planner_span_takes_it_back_out():
    from xas_allocation.planner_channel import planner_span, show

    wrapped = show("**Done** — 3 of 4 fixed.")
    assert planner_span(wrapped) == "**Done** — 3 of 4 fixed."


def test_planner_span_ignores_output_that_was_never_marked():
    from xas_allocation.planner_channel import planner_span

    assert planner_span("Successfully installed ortools-9.15.6755") is None
    assert planner_span("") is None


def test_planner_span_keeps_the_markdown_table_intact():
    from xas_allocation.planner_channel import planner_span, show

    body = planner_span(show(repair_and_report(_snapshot())))
    assert body is not None
    assert "| Order | Customer | Model |" in body
    assert "<<<" not in body


def test_show_survives_stdout_noise_around_the_span():
    from xas_allocation.planner_channel import planner_span, show

    printed = "WARNING: pip as root\n" + show("the table") + "\nwrote plan.json"
    assert planner_span(printed) == "the table"
