"""`flatten` is the pure rich->snapshot hop — the "flatten + freeze" step.

The invariant needs it to be deterministic code, not model judgment. These tests
pin that: same rich input -> byte-identical snapshot; VSOs explode into car
lines; supply is one vehicle pool of real + future vehicles; the incumbent comes
from each jobitem's allocation link (hard `VehicleId.Code` / soft Alloc); and
every order the disruption manifest names as broken really does run late in the
snapshot. One case feeds hand-written real-XAS-shaped records and asserts the
field mapping directly.
"""

import json

import datasource
from scenario_engine.generate import generate
from xas_allocation.flatten import flatten, flatten_default
from xas_allocation.solver import tardiness


def _rich():
    """The rich pull, via the SAME mapping the live source runs through: the
    engine fabricates the MCP's response shapes now, not the pull contract."""
    return datasource.map_world(generate(seed=20)["pull"])


def test_engine_is_deterministic():
    assert json.dumps(generate(seed=20), sort_keys=True) == json.dumps(
        generate(seed=20), sort_keys=True
    )


def test_flatten_is_deterministic():
    rich = _rich()
    a = json.dumps(flatten(rich).as_dict(), sort_keys=True)
    b = json.dumps(flatten(rich).as_dict(), sort_keys=True)
    assert a == b


def test_vsos_explode_into_cars_not_lines():
    """One wanted CAR is one order: a line's `Quantity` is expanded, so the count
    is Σ Quantity and never the number of lines."""
    rich = _rich()
    snap = flatten(rich)
    lines = [item for vso in rich["vsos"] for item in vso["JobItems"]]
    expected_cars = sum(int(item.get("Quantity") or 1) for item in lines)
    assert len(snap.orders) == expected_cars
    assert expected_cars > len(lines), "the fake must exercise Quantity > 1"
    assert all(o.so_id and o.key == f"{o.so_id}-{o.line}-{o.qty_index}" for o in snap.orders)
    # and every key is distinct — a collapse here silently loses demand
    assert len({o.key for o in snap.orders}) == len(snap.orders)


def test_a_multi_car_line_becomes_one_order_per_car():
    rich = {
        "meta": {"now": "2026-08-03"},
        "vsos": [
            {
                "JobKey": "VSO-9",
                "DeliveryDate": "2026-11-02",
                "JobPriority": {"Code": "B"},
                "Accounts": {"Owner": {"AccountName": "D", "AccountUUID": "C1"}},
                "JobItems": [
                    {
                        "JobItemType": "ModelItem",
                        "LineNum": 4,
                        "SalesModelCode": "SM1",
                        "Quantity": 3,
                        "Prices": [{"GrossTotal": 90000}],
                        "VehicleId": {"Code": "V1"},
                        "AllocQty": 3,
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
    assert [o.key for o in snap.orders] == ["VSO-9-4-1", "VSO-9-4-2", "VSO-9-4-3"]
    assert all(o.line_key == "VSO-9-4" for o in snap.orders)
    # One vehicle covers ONE car, whatever AllocQty claims — handing it to three
    # would double-book it and trip the solver's self-check on its own input.
    assert snap.incumbent == {"VSO-9-4-1": "V1"}
    assert snap.meta["excluded"]["flatten_skips"]["allocation_qty_not_resolvable_to_cars"] == 2
    # the line total is split across its cars, not repeated on each
    assert [o.price for o in snap.orders] == [30000.0, 30000.0, 30000.0]


def test_supply_is_one_pool_of_both_classifications():
    snap = flatten(_rich())
    classes = {u.vehicle_classification for u in snap.units}
    assert "Vehicle" in classes and "Future" in classes
    # is_hard tracks the classification exactly.
    for u in snap.units:
        assert u.is_hard == (u.vehicle_classification == "Vehicle"), u.vehicle_id


def test_incumbent_comes_from_allocation_links():
    rich = _rich()
    snap = flatten(rich)
    expected: dict[str, str] = {}
    for vso in rich["vsos"]:
        so_id = vso["JobKey"]
        for item in vso["JobItems"]:
            # The allocation covers the line's FIRST car; the rest are unfilled.
            key = f"{so_id}-{item['LineNum']}-1"
            hard = (item.get("VehicleId") or {}).get("Code")
            soft = item.get("AllocatedVehicleCode")
            if hard:
                expected[key] = hard
            elif soft:
                expected[key] = soft
    assert snap.incumbent == expected


def test_disrupted_orders_are_actually_late():
    """The manifest's broken orders must run late once flattened — otherwise the
    disruption the engine claims and the snapshot the solver sees disagree."""
    snap = flatten(_rich())
    orders = snap.order_by_key()
    units = snap.unit_by_id()
    disrupted = snap.disruption["disrupted_orders"]
    assert disrupted, "the scenario should break at least one order"
    # Named per CAR: what slips is a vehicle, and only the car riding it is late.
    for key in disrupted:
        assert tardiness(orders[key], units[snap.incumbent[key]]) > 0, key
    # and every car whose own vehicle is late is named — no silent omissions
    late = {k for k, uid in snap.incumbent.items() if tardiness(orders[k], units[uid]) > 0}
    assert set(disrupted) == late


def test_flatten_default_reads_the_bundled_dataset():
    snap = flatten_default()
    assert snap.orders and snap.units and snap.incumbent


def test_flatten_maps_real_xas_shaped_records():
    """Hand-built real-XAS records map 1:1 onto Order/Unit fields — the seam
    between the API vocabulary and the solver's."""
    rich = {
        "meta": {"now": "2026-08-03", "sales_models": ["T5040"]},
        "vsos": [
            {
                "JobKey": "VSO-77",
                "DMSJCEntry": "77",
                "DeliveryDate": "2026-09-14",
                "JobPriority": {"Code": "A"},
                "JobStatus": "Open",
                "Accounts": {
                    "Owner": {
                        "AccountName": "Colmobil",
                        "AccountUUID": "CUST-001",
                        "AccountDMSCode": "D001",
                    }
                },
                "ModelCode": "T5040",
                "SalesModelCode": "T5040",
                "JobItems": [
                    {
                        "JobItemType": "ModelItem",
                        "LineNum": 1,
                        "SalesModelCode": "T5040",
                        "Label": "JAECOO 7 4WD",
                        "Quantity": 1,
                        "Prices": [{"GrossTotal": 45000}],
                        # Hard-allocated: VehicleId.Code ↔ VehicleCode.
                        "VehicleId": {"Code": "11317"},
                        "AllocSourceClassification": "VGR",
                    },
                    {
                        "JobItemType": "ModelItem",
                        "LineNum": 2,
                        "SalesModelCode": "T5040",
                        "Label": "JAECOO 7 4WD",
                        "Quantity": 1,
                        "Prices": [{"GrossTotal": 45000}],
                        # Soft-allocated: no VehicleId, an Alloc link to a Future car.
                        "AllocSourceClassification": "VPO",
                        "AllocatedVehicleCode": "FUT-9",
                    },
                ],
            }
        ],
        "vehicles": [
            {
                "VehicleCode": "11317",
                "Vin": "VIN00011317",
                "ModelId": {"Code": "T5040", "Name": "JAECOO 7"},
                "Make": "Chery",
                "VehicleClassification": "Vehicle",
                "Status": {"Code": "05", "Name": "In Stock"},
                "InventoryStatus": "Available",
                "EtaDealer": "2026-09-10",
                "ExpectedCustomerDeliveryDate": "2026-09-10",
                "IsReserved": False,
                "Owner": "",
            },
            {
                "VehicleCode": "FUT-9",
                "Vin": "",
                "ModelId": {"Code": "T5040", "Name": "JAECOO 7"},
                "Make": "Chery",
                "VehicleClassification": "Future",
                "Status": {"Code": "01", "Name": "Ordered"},
                "InventoryStatus": "Future",
                "EtaDealer": "2026-10-01",
                "ExpectedCustomerDeliveryDate": "2026-10-01",
                "IsReserved": False,
                "Owner": "",
            },
        ],
        "disruption": {},
    }
    snap = flatten(rich)
    orders = snap.order_by_key()
    units = snap.unit_by_id()

    o1 = orders["VSO-77-1-1"]
    assert (o1.so_id, o1.line) == ("VSO-77", 1)
    assert o1.customer == "Colmobil" and o1.customer_id == "CUST-001"
    assert o1.priority == "A"
    assert o1.sales_model == "T5040"
    assert o1.delivery_date.isoformat() == "2026-09-14"  # from DeliveryDate
    assert o1.price == 45000.0  # Σ Prices[].GrossTotal

    # Hard incumbent via VehicleId.Code ↔ VehicleCode; soft via Alloc link.
    assert snap.incumbent["VSO-77-1-1"] == "11317"
    assert snap.incumbent["VSO-77-2-1"] == "FUT-9"

    real = units["11317"]
    assert real.vehicle_classification == "Vehicle" and real.is_hard
    assert real.sales_model == "T5040"  # from ModelId.Code
    assert real.eta_dealer.isoformat() == "2026-09-10"  # from EtaDealer

    future = units["FUT-9"]
    assert future.vehicle_classification == "Future" and not future.is_hard
    assert future.eta_dealer.isoformat() == "2026-10-01"
