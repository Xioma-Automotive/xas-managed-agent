"""`Quantity` is real demand: a line wanting 3 cars is 3 orders.

The solver already models one order as one capacity-1 demand node, so expansion
lives entirely in `flatten` — but it moves the order KEY to three levels
(`{so_id}-{line}-{n}`), and that is where the silent failures are. This file pins
the ones that do not raise on their own:

  * a duplicated key does not error, it COLLAPSES — two cars become one and the
    plan quietly under-serves a customer;
  * an instruction naming a LINE (which the override schema tells the agent to
    prefer) must match every car of it, or the pin does nothing at all;
  * what slips is a VEHICLE, so the affected demand is per CAR — a line's cars can
    come from different shipments, and one slipping says nothing about the rest;
    a manifest named more coarsely still has to resolve, or nothing is freed;
  * the frozen fence must not strand demand that never had a car.
"""

from datetime import date

import pytest

import alloc_tools
from xas_allocation import decisions as D
from xas_allocation.flatten import flatten
from xas_allocation.session import car_range, group_by_line, line_label, line_sizes
from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import (
    disrupted_order_keys,
    names_order,
    not_before_for,
    partition,
    solve,
)

NOW = date(2026, 8, 3)
LIQUID = date(2026, 11, 1)  # ~90 days out — well outside the fence


def _order(so_id: str, line: int, qty_index: int, promised: date = LIQUID) -> Order:
    return Order(
        so_id=so_id,
        line=line,
        qty_index=qty_index,
        customer="Dealer",
        customer_id="CUST-001",
        sales_model="SM1",
        priority="B",
        delivery_date=promised,
        price=40000,
        n_prior_delays=0,
        days_backordered=0,
    )


def _unit(vid: str, eta: date = LIQUID) -> Unit:
    return Unit(
        vehicle_id=vid,
        vehicle_classification="Vehicle",
        sales_model="SM1",
        eta_dealer=eta,
    )


def _snapshot(orders, units, incumbent=None, disrupted=None, now=NOW) -> Snapshot:
    return Snapshot(
        orders=orders,
        units=units,
        incumbent=incumbent or {},
        disruption={"disrupted_orders": disrupted or []},
        now=now,
    )


# --- the key is unique, and a collapse is loud --------------------------------


def test_a_duplicated_order_key_is_refused_not_collapsed():
    """`order_by_key` is how the whole solver reads demand. Two orders sharing a
    key would silently become one — a car of paid-for demand disappearing with no
    error anywhere. That is the failure a botched expansion produces."""
    snap = _snapshot([_order("SO-A", 1, 1), _order("SO-A", 1, 1)], [_unit("V1")])
    with pytest.raises(ValueError, match="duplicate order keys"):
        snap.order_by_key()


def test_the_three_key_levels():
    o = _order("VSO-4000", 2, 3)
    assert o.key == "VSO-4000-2-3"
    assert o.line_key == "VSO-4000-2"
    assert o.so_id == "VSO-4000"
    assert names_order(o, {"VSO-4000-2-3"}), "its own car"
    assert names_order(o, {"VSO-4000-2"}), "its line"
    assert names_order(o, {"VSO-4000"}), "its whole VSO"
    assert not names_order(o, {"VSO-4000-2-1"}), "a sibling car is not this one"
    assert not names_order(o, set()), "an empty instruction names nobody"


# --- an instruction naming a line reaches every car of it ---------------------


def test_a_forbid_on_the_line_pins_every_car_of_it():
    orders = [_order("SO-A", 1, n) for n in (1, 2, 3)]
    snap = _snapshot(orders, [_unit("V1"), _unit("V2"), _unit("V3")])
    rp = partition(snap, {"forbid": [{"order": "SO-A-1", "action": "no_move"}]})
    assert rp.free_orders == [], "a line-level no_move must hold all three cars"


def test_a_defer_on_the_line_reaches_every_car_of_it():
    """The override schema tells the agent to prefer the LINE level, so a defer
    given there has to free each of the line's cars and carry its `not_before` to
    each — read by exact key it would silently apply to none of them.

    (`not_before` is a PRICED penalty, not a wall, so this checks that the date
    reaches the cars, not that the solver always obeys it.)"""
    orders = [_order("SO-A", 1, n) for n in (1, 2)]
    snap = _snapshot(
        orders,
        [_unit("V1"), _unit("V2")],
        incumbent={"SO-A-1-1": "V1", "SO-A-1-2": "V2"},
    )
    override = {"pins": [{"order": "SO-A-1", "action": "defer", "not_before": "2026-10-01"}]}
    rp = partition(snap, override)
    assert rp.free_orders == ["SO-A-1-1", "SO-A-1-2"], "the defer must free both cars"
    for o in orders:
        assert not_before_for(rp.not_before, o) == date(2026, 10, 1)


def test_a_defer_on_one_car_beats_one_on_its_line():
    """Most specific wins, so a planner can carve out a single car."""
    not_before = {"SO-A": date(2026, 9, 1), "SO-A-1": date(2026, 10, 1)}
    assert not_before_for(not_before, _order("SO-A", 1, 1)) == date(2026, 10, 1)
    assert not_before_for(not_before, _order("SO-A", 2, 1)) == date(2026, 9, 1)
    not_before["SO-A-1-2"] = date(2026, 11, 1)
    assert not_before_for(not_before, _order("SO-A", 1, 2)) == date(2026, 11, 1)
    assert not_before_for(not_before, _order("SO-A", 1, 1)) == date(2026, 10, 1)
    assert not_before_for({}, _order("SO-A", 1, 1)) is None


def test_a_scope_on_the_vso_frees_all_its_lines():
    orders = [_order("SO-A", 1, 1), _order("SO-A", 2, 1), _order("SO-B", 1, 1)]
    snap = _snapshot(orders, [_unit("V1"), _unit("V2"), _unit("V3")])
    rp = partition(snap, {"scope": {"orders": ["SO-A"]}})
    assert rp.free_orders == ["SO-A-1-1", "SO-A-2-1"]


# --- the disruption manifest is line-grained ---------------------------------


def test_a_coarsely_named_disruption_still_frees_the_right_cars():
    """The manifest is derived per CAR — what slips is a vehicle, and only the car
    riding it is late. But a manifest named at LINE level (hand-written, or an
    older snapshot) must still resolve: compared raw against car keys it matches
    nothing, nothing is freed, and the report reads '0 of 0 delayed orders' over a
    broken book."""
    orders = [_order("SO-A", 1, n) for n in (1, 2)] + [_order("SO-B", 1, 1)]
    snap = _snapshot(
        orders,
        [_unit("V1"), _unit("V2"), _unit("V3")],
        incumbent={"SO-A-1-1": "V1", "SO-A-1-2": "V2", "SO-B-1-1": "V3"},
        disrupted=["SO-A-1"],  # the LINE, as the manifest names it
    )
    assert disrupted_order_keys(snap) == {"SO-A-1-1", "SO-A-1-2"}
    rp = partition(snap, {})
    assert rp.free_orders == ["SO-A-1-1", "SO-A-1-2"]
    assert rp.pinned == {"SO-B-1-1": "V3"}


# --- the frozen fence protects allocations, not empty demand ------------------


def test_an_unallocated_order_inside_the_fence_can_still_be_filled():
    """The fence stops churning an EXISTING allocation near delivery. An order
    with no car has nothing to protect, and freezing it makes it a permanent
    backorder — while landing in neither `plan` nor `unfilled`, so `_self_check`
    reports not-ok with an empty violation list."""
    frozen_date = date(NOW.year, NOW.month, NOW.day + D.FROZEN_MAX_DAYS - 1)
    orders = [_order("SO-A", 1, 1, frozen_date), _order("SO-A", 1, 2, frozen_date)]
    snap = _snapshot(orders, [_unit("V1", NOW), _unit("V2", NOW)])
    result = solve(snap, {}, lam=0)
    assert result.self_check["ok"], result.self_check["violations"]
    assert result.self_check["every_order_placed"]
    assert set(result.plan) == {"SO-A-1-1", "SO-A-1-2"}


def test_an_allocated_order_inside_the_fence_stays_put():
    frozen_date = date(NOW.year, NOW.month, NOW.day + D.FROZEN_MAX_DAYS - 1)
    snap = _snapshot(
        [_order("SO-A", 1, 1, frozen_date)],
        [_unit("V-LATE", date(2026, 10, 1)), _unit("V-GOOD", NOW)],
        incumbent={"SO-A-1-1": "V-LATE"},
        disrupted=["SO-A-1"],
    )
    rp = partition(snap, {})
    assert rp.free_orders == [], "a frozen allocation is not re-slottable"
    assert solve(snap, {}, lam=0).plan == {"SO-A-1-1": "V-LATE"}


# --- what the agent is told about demand -------------------------------------


def test_the_summary_counts_cars_not_lines():
    """The tool summary and the snapshot the agent solves are compared. Counting
    lines here and cars there is the same number disagreeing with itself."""
    rich = {
        "meta": {"now": "2026-08-03"},
        "vsos": [
            {
                "JobKey": "VSO-1",
                "DeliveryDate": "2026-11-01",
                "JobPriority": {"Code": "B"},
                "Accounts": {"Owner": {"AccountName": "Colmobil", "AccountUUID": "CUST-001"}},
                "JobItems": [
                    {"LineNum": 1, "SalesModelCode": "SM1", "Quantity": 4},
                    {"LineNum": 2, "SalesModelCode": "SM1", "Quantity": 1},
                ],
            }
        ],
        "vehicles": [],
        "disruption": {},
    }
    summary = alloc_tools.summarize(rich)
    assert summary["car_lines"] == 2
    assert summary["orders"] == 5, "4 + 1 cars, not 2 lines"
    assert summary["orders"] == len(flatten(rich).orders)


def test_a_multi_car_line_never_double_books_its_one_vehicle():
    """`AllocQty` may claim the whole line is committed, but the pull resolves one
    vehicle. Giving it to every car would double-book it — and the solver's
    self-check fires on its own INPUT, before it has done anything wrong."""
    rich = {
        "meta": {"now": "2026-08-03"},
        "vsos": [
            {
                "JobKey": "VSO-1",
                "DeliveryDate": "2026-11-01",
                "JobPriority": {"Code": "B"},
                "Accounts": {"Owner": {"AccountName": "D", "AccountUUID": "CUST-001"}},
                "JobItems": [
                    {
                        "LineNum": 1,
                        "SalesModelCode": "SM1",
                        "Quantity": 4,
                        "VehicleId": {"Code": "V1"},
                        "AllocQty": 4,
                    }
                ],
            }
        ],
        "vehicles": [
            {
                "VehicleCode": "V1",
                "SalesModel": "SM1",
                "VehicleClassification": "Vehicle",
                "EtaDealer": "2026-10-01",
            }
        ],
        "disruption": {},
    }
    snap = flatten(rich)
    assert list(snap.incumbent) == ["VSO-1-1-1"]
    assert len(set(snap.incumbent.values())) == len(snap.incumbent)
    result = solve(snap, {}, lam=0)
    assert result.self_check["ok"], result.self_check["violations"]
    # three cars have no vehicle to be had, and that is surfaced, not swallowed
    assert result.unfilled == ["VSO-1-1-2", "VSO-1-1-3", "VSO-1-1-4"]


# --- the report's arithmetic -------------------------------------------------


def test_a_lines_groups_account_for_every_car_exactly_once():
    """Grouping is a presentation step, so it must be conservative: the rows a
    line contributes have to sum to its car count, with no car counted twice.
    A regrouping bug here would over- or under-report demand in the one output a
    planner actually reads."""
    orders = [_order("SO-A", 1, n) for n in (1, 2, 3)] + [_order("SO-B", 1, 1)]
    snap = _snapshot(orders, [_unit("V1")])
    sizes = line_sizes(snap)
    assert sizes == {"SO-A-1": 3, "SO-B-1": 1}

    # split the line across two rows, as a real report does when its cars were
    # treated differently
    keys = [o.key for o in orders]
    groups = group_by_line(keys, lambda k: (snap.order_by_key()[k].line_key, k != "SO-A-1-1"))
    by_line: dict[str, int] = {}
    seen: set[str] = set()
    for group, cols in groups:
        assert not (set(group) & seen), "a car appears in two rows"
        seen.update(group)
        by_line[cols[0]] = by_line.get(cols[0], 0) + len(group)
    assert by_line == sizes
    assert seen == set(keys)


def test_the_label_names_which_cars_not_how_many():
    """A line's cars can be satisfied from different shipments and so land on
    different dates. The label has to say WHICH cars a row is about — a count
    ("2 of its 3 cars") cannot distinguish two rows of the same line, and reads
    as an ordinal besides."""
    assert line_label("VSO-4000-1", [1], 1) == "VSO-4000-1"
    assert line_label("VSO-4000-2", [1, 2, 3], 5) == "VSO-4000-2 — cars 1-3 of 5"
    assert line_label("VSO-4000-2", [4], 5) == "VSO-4000-2 — car 4 of 5"
    assert line_label("VSO-4000-2", [1, 3], 5) == "VSO-4000-2 — cars 1, 3 of 5"
    assert line_label("VSO-4000-2", [1, 2, 3, 4, 5], 5) == "VSO-4000-2 — all 5 cars"


def test_car_ranges_collapse_runs_but_not_gaps():
    assert car_range([3]) == "car 3"
    assert car_range([1, 2, 3]) == "cars 1-3"
    assert car_range([3, 1, 2]) == "cars 1-3", "order of the input must not matter"
    assert car_range([1, 3, 4, 5, 8]) == "cars 1, 3-5, 8"
