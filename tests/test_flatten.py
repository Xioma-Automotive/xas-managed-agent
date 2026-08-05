"""`flatten` is the pure rich->snapshot hop — the "flatten + freeze" step.

The invariant needs it to be deterministic code, not model judgment. These tests
pin that: same rich input -> byte-identical snapshot; SOs explode into rows;
supply is the union of vehicles and PO-line slots; `committed` is derived from
`location_state`; the incumbent comes from the allocation links; and every order
the disruption manifest names as broken really does run late in the snapshot.
"""

import json

from scenario_engine.generate import generate
from xas_allocation.decisions import COMMIT_POINT_STATES
from xas_allocation.flatten import flatten, flatten_default
from xas_allocation.solver import tardiness


def _rich():
    return generate(seed=20)["pull"]


def test_engine_is_deterministic():
    assert json.dumps(generate(seed=20), sort_keys=True) == json.dumps(
        generate(seed=20), sort_keys=True
    )


def test_flatten_is_deterministic():
    rich = _rich()
    a = json.dumps(flatten(rich).as_dict(), sort_keys=True)
    b = json.dumps(flatten(rich).as_dict(), sort_keys=True)
    assert a == b


def test_sos_explode_into_rows():
    rich = _rich()
    snap = flatten(rich)
    expected_rows = sum(len(so["rows"]) for so in rich["sos"])
    assert len(snap.orders) == expected_rows
    assert all(o.so_id and o.order_id.startswith(o.so_id) for o in snap.orders)


def test_supply_unions_both_kinds():
    snap = flatten(_rich())
    kinds = {u.kind for u in snap.units}
    assert "vehicle" in kinds and "po_line" in kinds


def test_committed_is_derived_from_location_state():
    snap = flatten(_rich())
    for u in snap.units:
        assert u.committed == (u.location_state in COMMIT_POINT_STATES), u.vehicle_id
        if u.kind == "po_line":
            assert not u.committed, "a future PO-line slot is never committed"


def test_incumbent_comes_from_allocation_links():
    rich = _rich()
    snap = flatten(rich)
    expected = {
        row["row_id"]: row["current_supply_id"]
        for so in rich["sos"]
        for row in so["rows"]
        if row["current_supply_id"]
    }
    assert snap.incumbent == expected


def test_disrupted_orders_are_actually_late():
    """The manifest's broken rows must run late once flattened — otherwise the
    disruption the engine claims and the snapshot the solver sees disagree."""
    snap = flatten(_rich())
    orders = snap.order_by_id()
    units = snap.unit_by_id()
    disrupted = snap.disruption["disrupted_orders"]
    assert disrupted, "the scenario should break at least one row"
    for oid in disrupted:
        assert tardiness(orders[oid], units[snap.incumbent[oid]]) > 0, oid


def test_flatten_default_reads_the_bundled_dataset():
    snap = flatten_default()
    assert snap.orders and snap.units and snap.incumbent
