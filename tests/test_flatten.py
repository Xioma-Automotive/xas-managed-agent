"""`flatten` is the pure pull->snapshot hop — the "flatten + freeze" step, and the
only part of the data path that runs inside the sandbox.

The invariant needs it to be deterministic code, not model judgment. These tests
pin that: the same document gives a byte-identical snapshot; one order row is one
order keyed by its card and line; supply is one car pool; the allocations come
from each order's own `VehicleCode`; and every order the derived set calls late
really does run late in the snapshot.

The document is built the way production builds it — `datasource.translate` over a
committed scenario directory — so a change to the mapping cannot pass here and
fail there.
"""

import json

import datasource
from xas_allocation.flatten import flatten, flatten_path
from xas_allocation.solver import tardiness

FIXTURE = "scenario-unallocated"


def _document(scenario: str = FIXTURE) -> dict:
    return datasource.document(datasource.get_source(scenario).pull())


def _doc(orders: list[dict], vehicles: list[dict]) -> dict:
    """A hand-built document in the contract's shape, for the edge cases the
    committed scenarios deliberately do not contain."""
    return {
        "captured_at": "2026-08-25",
        "pull_id": "2026-09-06T09:12:03Z/test",
        "source": "test",
        "meta": {},
        "orders": orders,
        "vehicles": vehicles,
    }


def test_flatten_is_deterministic():
    doc = _document("scenario-mixed")
    a = json.dumps(flatten(doc).as_dict(), sort_keys=True)
    b = json.dumps(flatten(doc).as_dict(), sort_keys=True)
    assert a == b


def test_one_order_row_is_one_line_keyed_by_its_card_and_line():
    """The grain the contract sets: identity is `DMSJCNum` + `LineNum`. One line
    is still one wanted car — there is no `Quantity`."""
    doc = _document("scenario-mixed")
    snap = flatten(doc)
    assert len(snap.orders) == len(doc["orders"])
    assert all(o.key == f"{o.job_card}-{o.line}" for o in snap.orders)
    # and every key is distinct — a collapse here silently loses demand
    assert len({o.key for o in snap.orders}) == len(snap.orders)


def test_two_lines_of_one_card_stay_two_orders():
    """The reason the key is two levels. The mixed scenario really carries a card
    with two lines on it; collapsing them would drop a car nobody notices."""
    snap = flatten(_document("scenario-mixed"))
    shared = [c for c in {o.job_card for o in snap.orders} if len(snap.orders_on_card(c)) > 1]
    assert shared, "the mixed scenario no longer has a multi-line card to pin this on"
    for card in shared:
        lines = snap.orders_on_card(card)
        assert len({o.line for o in lines}) == len(lines)
        assert len({o.key for o in lines}) == len(lines)


def test_the_promise_is_the_orders_date_and_the_arrival_is_the_cars():
    """Confusing the two compares a date with itself, and nothing is ever late."""
    snap = flatten(
        _doc(
            [
                {
                    "DMSJCNum": "900001",
                    "LineNum": "1",
                    "SalesModel": "SM-A",
                    "DeliveryDate": "2026-09-01",
                    "VehicleCode": "CAR-1",
                }
            ],
            [{"VehicleCode": "CAR-1", "SalesModel": "SM-A", "AvailableBy": "2026-09-20"}],
        )
    )
    order = snap.orders[0]
    assert order.delivery_date.isoformat() == "2026-09-01"
    assert snap.vehicles[0].eta_dealer.isoformat() == "2026-09-20"
    assert tardiness(order, snap.vehicles[0]) == 19
    assert snap.disruption["disrupted_orders"] == ["900001-1"]


def test_a_line_number_the_dms_sends_as_a_number_makes_the_same_key():
    """The DMS sends `LineNum` as a JSON number and a CSV sends it as a string.
    A key that is `900001-1` on one side and `900001-1.0` on the other matches
    nothing and says nothing about why."""
    rows = [
        {
            "DMSJCNum": "900001",
            "LineNum": line,
            "SalesModel": "SM-A",
            "DeliveryDate": "2026-09-01",
        }
        for line in (1, "1")
    ]
    for row in rows:
        snap = flatten(_doc([row], []))
        assert snap.orders[0].key == "900001-1"


def test_the_contract_labels_ride_through_untouched():
    """Carried, never priced: the account's code, how firmly the customer is
    committed, and the DMS entry a write-back has to quote."""
    snap = flatten(
        _doc(
            [
                {
                    "DMSJCNum": "900001",
                    "LineNum": "3",
                    "DMSJCEntry": "100001",
                    "SalesModel": "SM-A",
                    "DeliveryDate": "2026-09-01",
                    "Accounts.Owner.AccountName": "Delek Motors Fleet",
                    "Accounts.Owner.AccountDMSCode": "C0007",
                    "AllocType": "soft",
                }
            ],
            [],
        )
    )
    order = snap.orders[0]
    assert order.customer == "Delek Motors Fleet"
    assert order.account_code == "C0007"
    assert order.alloc_type == "soft"
    assert order.entry == "100001"


def test_an_order_holding_no_car_has_no_allocation_and_is_not_late():
    """An order with no car needs no manifest — `partition` frees anything
    unassigned, so it must not be counted as a late arrival too."""
    snap = flatten(_document(FIXTURE))
    car_less = [o.key for o in snap.orders if o.key not in snap.allocations]
    assert len(snap.orders) == 10 and len(car_less) == 8
    assert snap.disruption["disrupted_orders"] == []


def test_allocations_come_from_the_orders_own_vehicle_code():
    doc = _document("scenario-delayed")
    snap = flatten(doc)
    by_key = {datasource.order_key(o): o["VehicleCode"] for o in doc["orders"]}
    assert snap.allocations
    for key, vid in snap.allocations.items():
        assert by_key[key] == vid
    # a car cannot serve two orders: the input must already be a matching
    assert len(set(snap.allocations.values())) == len(snap.allocations)


def test_an_allocation_to_a_car_that_did_not_survive_is_dropped_and_counted():
    snap = flatten(
        _doc(
            [
                {
                    "DMSJCNum": "900002",
                    "LineNum": "1",
                    "SalesModel": "SM-A",
                    "DeliveryDate": "2026-09-01",
                    "VehicleCode": "GONE",
                }
            ],
            [{"VehicleCode": "CAR-1", "SalesModel": "SM-A", "AvailableBy": "2026-08-30"}],
        )
    )
    assert snap.allocations == {}
    assert snap.meta["excluded"]["flatten_skips"] == {"allocation_to_a_dropped_vehicle": 1}


def test_a_row_missing_what_makes_it_solvable_is_skipped_not_defaulted():
    """A fabricated date or model would silently move the plan. The host filters
    these already; this is the backstop, and it must COUNT what it drops."""
    snap = flatten(
        _doc(
            [
                {
                    "DMSJCNum": "900003",
                    "LineNum": "1",
                    "SalesModel": "",
                    "DeliveryDate": "2026-09-01",
                },
                {"DMSJCNum": "900004", "LineNum": "1", "SalesModel": "SM-A", "DeliveryDate": ""},
                # a card with no line number is half a key, and half a key names
                # nothing — it is dropped, not guessed at
                {
                    "DMSJCNum": "900005",
                    "LineNum": "",
                    "SalesModel": "SM-A",
                    "DeliveryDate": "2026-09-01",
                },
            ],
            [
                {"VehicleCode": "CAR-1", "SalesModel": "", "AvailableBy": "2026-08-30"},
                {"VehicleCode": "CAR-2", "SalesModel": "SM-A", "AvailableBy": ""},
            ],
        )
    )
    assert snap.orders == [] and snap.vehicles == []
    assert snap.meta["excluded"]["flatten_skips"] == {
        "order_without_a_model": 1,
        "order_without_a_promised_date": 1,
        "order_without_a_card_or_line_number": 1,
        "vehicle_without_a_model": 1,
        "vehicle_without_an_arrival_date": 1,
    }


def test_every_order_called_late_really_is_late():
    snap = flatten(_document("scenario-mixed"))
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
        snap = flatten(datasource.document(pull))
        assert snap.disruption["disrupted_orders"] == pull["disruption"]["disrupted_orders"]


def test_flatten_path_reads_the_one_mounted_file(tmp_path):
    """The mounted case: one path in, one snapshot out. The contract is one
    document carrying both row streams."""
    path = tmp_path / "dms_allocation.json"
    path.write_text(json.dumps(_document(FIXTURE)))
    snap = flatten_path(path)
    assert len(snap.orders) == 10 and len(snap.vehicles) == 13
    assert snap.now.isoformat() == "2026-08-25"


def test_the_pull_headers_reach_the_snapshot():
    """`captured_at` / `pull_id` / `source` ride into `snapshot.meta` so an answer
    can say which pull it was built on without re-reading the file."""
    doc = _document(FIXTURE)
    meta = flatten(doc).meta
    assert meta["captured_at"] == doc["captured_at"] == "2026-08-25"
    assert meta["source"] == "scenario-unallocated"
    assert meta["pull_id"] == doc["pull_id"] and doc["pull_id"]


def test_the_pull_date_comes_from_the_scenario_not_the_clock():
    """A static file plus a wall clock means the same rows mean something new
    tomorrow — and the tests drift a day at a time."""
    assert _document(FIXTURE)["captured_at"] == "2026-08-25"
