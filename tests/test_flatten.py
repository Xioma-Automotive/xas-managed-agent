"""`flatten` is the pure pull->snapshot hop — the "flatten + freeze" step, and the
only part of the data path that runs inside the sandbox.

The invariant needs it to be deterministic code, not model judgment. These tests
pin that: the same two payloads give a byte-identical snapshot; one order row is
one order keyed by its own id; supply is one car pool; the allocations come from
each order's own `VehicleCode`; and every order the derived set calls late really
does run late in the snapshot.

The payloads are built the way production builds them — `datasource.translate`
over a committed scenario directory — so a change to the mapping cannot pass here
and fail there.
"""

import json

import datasource
from xas_allocation.flatten import flatten, flatten_paths
from xas_allocation.solver import tardiness

FIXTURE = "scenario-unallocated"


def _payloads(scenario: str = FIXTURE) -> tuple[dict, dict]:
    pull = datasource.get_source(scenario).pull()
    return datasource.orders_payload(pull), datasource.vehicles_payload(pull)


def test_flatten_is_deterministic():
    orders, vehicles = _payloads("scenario-mixed")
    a = json.dumps(flatten(orders, vehicles).as_dict(), sort_keys=True)
    b = json.dumps(flatten(orders, vehicles).as_dict(), sort_keys=True)
    assert a == b


def test_one_order_row_is_one_order_keyed_by_its_own_id():
    """The grain, and the whole of it: no cards, no lines, no `Quantity`."""
    orders_doc, vehicles_doc = _payloads("scenario-mixed")
    snap = flatten(orders_doc, vehicles_doc)
    assert len(snap.orders) == len(orders_doc["orders"])
    assert all(o.key == o.order_id for o in snap.orders)
    # and every key is distinct — a collapse here silently loses demand
    assert len({o.key for o in snap.orders}) == len(snap.orders)


def test_the_promise_is_the_orders_date_and_the_arrival_is_the_cars():
    """Confusing the two compares a date with itself, and nothing is ever late."""
    snap = flatten(
        {
            "now": "2026-08-25",
            "meta": {},
            "orders": [
                {
                    "OrderId": "900001",
                    "SalesModel": "SM-A",
                    "DeliveryDate": "2026-09-01",
                    "VehicleCode": "CAR-1",
                }
            ],
        },
        {"vehicles": [{"VehicleCode": "CAR-1", "SalesModel": "SM-A", "EtaDealer": "2026-09-20"}]},
    )
    order = snap.orders[0]
    assert order.delivery_date.isoformat() == "2026-09-01"
    assert snap.vehicles[0].eta_dealer.isoformat() == "2026-09-20"
    assert tardiness(order, snap.vehicles[0]) == 19
    assert snap.disruption["disrupted_orders"] == ["900001"]


def test_an_order_holding_no_car_has_no_allocation_and_is_not_late():
    """An order with no car needs no manifest — `partition` frees anything
    unassigned, so it must not be counted as a late arrival too."""
    orders_doc, vehicles_doc = _payloads(FIXTURE)
    snap = flatten(orders_doc, vehicles_doc)
    car_less = [o.key for o in snap.orders if o.key not in snap.allocations]
    assert len(snap.orders) == 10 and len(car_less) == 8
    assert snap.disruption["disrupted_orders"] == []


def test_allocations_come_from_the_orders_own_vehicle_code():
    orders_doc, vehicles_doc = _payloads("scenario-delayed")
    snap = flatten(orders_doc, vehicles_doc)
    by_id = {o["OrderId"]: o["VehicleCode"] for o in orders_doc["orders"]}
    assert snap.allocations
    for key, vid in snap.allocations.items():
        assert by_id[key] == vid
    # a car cannot serve two orders: the input must already be a matching
    assert len(set(snap.allocations.values())) == len(snap.allocations)


def test_an_allocation_to_a_car_that_did_not_survive_is_dropped_and_counted():
    snap = flatten(
        {
            "now": "2026-08-25",
            "meta": {},
            "orders": [
                {
                    "OrderId": "900002",
                    "SalesModel": "SM-A",
                    "DeliveryDate": "2026-09-01",
                    "VehicleCode": "GONE",
                }
            ],
        },
        {"vehicles": [{"VehicleCode": "CAR-1", "SalesModel": "SM-A", "EtaDealer": "2026-08-30"}]},
    )
    assert snap.allocations == {}
    assert snap.meta["excluded"]["flatten_skips"] == {"allocation_to_a_dropped_vehicle": 1}


def test_a_row_missing_what_makes_it_solvable_is_skipped_not_defaulted():
    """A fabricated date or model would silently move the plan. The host filters
    these already; this is the backstop, and it must COUNT what it drops."""
    snap = flatten(
        {
            "now": "2026-08-25",
            "meta": {},
            "orders": [
                {"OrderId": "900003", "SalesModel": "", "DeliveryDate": "2026-09-01"},
                {"OrderId": "900004", "SalesModel": "SM-A", "DeliveryDate": ""},
                {"OrderId": "", "SalesModel": "SM-A", "DeliveryDate": "2026-09-01"},
            ],
        },
        {
            "vehicles": [
                {"VehicleCode": "CAR-1", "SalesModel": "", "EtaDealer": "2026-08-30"},
                {"VehicleCode": "CAR-2", "SalesModel": "SM-A", "EtaDealer": ""},
            ]
        },
    )
    assert snap.orders == [] and snap.vehicles == []
    assert snap.meta["excluded"]["flatten_skips"] == {
        "order_without_a_model": 1,
        "order_without_a_promised_date": 1,
        "order_without_an_id": 1,
        "vehicle_without_a_model": 1,
        "vehicle_without_an_arrival_date": 1,
    }


def test_every_order_called_late_really_is_late():
    orders_doc, vehicles_doc = _payloads("scenario-mixed")
    snap = flatten(orders_doc, vehicles_doc)
    orders, vehicles = snap.order_by_key(), snap.vehicle_by_id()
    late = snap.disruption["disrupted_orders"]
    assert late
    for key in late:
        assert tardiness(orders[key], vehicles[snap.allocations[key]]) > 0
    # and nothing late is missing from it
    for key, vid in snap.allocations.items():
        if tardiness(orders[key], vehicles[vid]) > 0:
            assert key in late


def test_the_host_and_the_sandbox_derive_the_same_late_set():
    """`translate` (host, for the tool summary) and `flatten` (sandbox, for the
    solver) both derive it. They must agree, or the agent is shown a count the
    snapshot does not have."""
    for scenario in ("scenario-unallocated", "scenario-delayed", "scenario-mixed"):
        pull = datasource.get_source(scenario).pull()
        snap = flatten(datasource.orders_payload(pull), datasource.vehicles_payload(pull))
        assert snap.disruption["disrupted_orders"] == pull["disruption"]["disrupted_orders"]


def test_flatten_paths_reads_the_two_files(tmp_path):
    """The mounted case: two paths in, one snapshot out."""
    orders_doc, vehicles_doc = _payloads(FIXTURE)
    (tmp_path / "orders.json").write_text(json.dumps(orders_doc))
    (tmp_path / "vehicles.json").write_text(json.dumps(vehicles_doc))
    snap = flatten_paths(tmp_path / "orders.json", tmp_path / "vehicles.json")
    assert len(snap.orders) == 10 and len(snap.vehicles) == 13
    assert snap.now.isoformat() == "2026-08-25"


def test_the_pull_date_comes_from_the_scenario_not_the_clock():
    """A static file plus a wall clock means the same rows mean something new
    tomorrow — and the tests drift a day at a time."""
    orders_doc, _ = _payloads(FIXTURE)
    assert orders_doc["now"] == "2026-08-25"
