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

from scenario_engine.generate import generate
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


def test_vsos_explode_into_car_lines():
    rich = _rich()
    snap = flatten(rich)
    expected_rows = sum(len(vso["JobItems"]) for vso in rich["vsos"])
    assert len(snap.orders) == expected_rows
    assert all(o.so_id and o.key == f"{o.so_id}-{o.line}" for o in snap.orders)


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
            key = f"{so_id}-{item['LineNum']}"
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
    for key in disrupted:
        assert tardiness(orders[key], units[snap.incumbent[key]]) > 0, key


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

    o1 = orders["VSO-77-1"]
    assert (o1.so_id, o1.line) == ("VSO-77", 1)
    assert o1.customer == "Colmobil" and o1.customer_id == "CUST-001"
    assert o1.priority == "A"
    assert o1.sales_model == "T5040"
    assert o1.delivery_date.isoformat() == "2026-09-14"  # from DeliveryDate
    assert o1.price == 45000.0  # Σ Prices[].GrossTotal

    # Hard incumbent via VehicleId.Code ↔ VehicleCode; soft via Alloc link.
    assert snap.incumbent["VSO-77-1"] == "11317"
    assert snap.incumbent["VSO-77-2"] == "FUT-9"

    real = units["11317"]
    assert real.vehicle_classification == "Vehicle" and real.is_hard
    assert real.sales_model == "T5040"  # from ModelId.Code
    assert real.eta_dealer.isoformat() == "2026-09-10"  # from EtaDealer

    future = units["FUT-9"]
    assert future.vehicle_classification == "Future" and not future.is_hard
    assert future.eta_dealer.isoformat() == "2026-10-01"
