"""`datasource` is the one mapping: the export's two CSVs -> the two mounted
payloads.

It runs HOST-SIDE, before the session exists, and it is the only place a row is
filtered or a field renamed. `translate` is pure — no clock, no filesystem — so
most of these feed rows in directly; the rest read the committed scenario
directories, which are what production reads.

What the tests are guarding, in order of how quietly it would fail:

  * **the promise is the ORDER's date, the arrival is the CAR's.** Swap them and
    nothing is ever late; nothing else in the pipeline would notice.
  * **`Available For Sale` has a REAL trailing space on most rows** and appears
    both ways in one file. Compare unstripped and free cars vanish from supply.
  * **eligibility is `SalesModel` on both sides**, never `modelId.code`, which
    holds the model above it and matches no order.
  * **every dropped row is counted by reason**, because a plan over the survivors
    presented as the whole book is the worst thing this pipeline can do.
  * **a missing column raises**, naming it — the CSV equivalent of the app MCP's
    projection gap, except there is no "absent from some rows" case to tell apart.
"""

from datetime import date
from pathlib import Path

import pytest

import datasource
from xas_allocation.flatten import flatten

NOW = date(2026, 8, 25)
DATA = Path(datasource.DATA_DIR)


def _order_row(**over) -> dict:
    row = {
        "OrderId": "500001",
        "vehicleCode": "",
        "modelId.name": "OMODA9 PHEV Premium",
        "SalesModel": "T6480J1BXLX0018",
        "etaDealer": "2026-09-20T00:00:00.000Z",
    }
    row.update(over)
    return row


def _vehicle_row(**over) -> dict:
    row = {
        "vehicleCode": "1004316",
        "vin": "LNNBDDEH2TG042271",
        "modelId.code": "T6480J1XXLX0018",
        "modelId.name": "OMODA9 PHEV Premium",
        "SalesModel": "T6480J1BXLX0018",
        "inv status label": "Sea Transit",
        "status.code": "2",
        "status.name": "Available For Sale ",
        "availableBy": "2026-09-22T12:00:00.000Z",
    }
    row.update(over)
    return row


def _pull(orders, vehicles, now=NOW):
    return datasource.translate(orders, vehicles, now=now)


# --- the two dates -----------------------------------------------------------


def test_the_promise_comes_from_the_order_and_the_arrival_from_the_car():
    pull = _pull(
        [_order_row(vehicleCode="1004316", etaDealer="2026-09-20T00:00:00.000Z")],
        [_vehicle_row(availableBy="2026-10-05T00:00:00.000Z")],
    )
    assert pull["orders"][0]["DeliveryDate"] == "2026-09-20"
    assert pull["vehicles"][0]["EtaDealer"] == "2026-10-05"
    # and the order is late BECAUSE the car lands after the order's own date
    assert pull["disruption"]["disrupted_orders"] == ["500001"]


def test_the_time_of_day_is_dropped_but_the_day_is_not_shifted():
    """The export stamps midnight, noon and 22:00. Only the day matters to a
    promise, and a timezone hop would move an order a day out of step with the
    planner's calendar."""
    pull = _pull(
        [_order_row(etaDealer="2026-09-20T22:00:00.000Z")],
        [_vehicle_row(availableBy="2026-09-22T12:00:00.000Z")],
    )
    assert pull["orders"][0]["DeliveryDate"] == "2026-09-20"
    assert pull["vehicles"][0]["EtaDealer"] == "2026-09-22"


# --- supply and the trailing space -------------------------------------------


def test_both_spellings_of_available_for_sale_are_free_supply():
    """'Available For Sale ' (trailing space) and 'Available For Sale' both occur
    in ONE file — 152 and 8 rows of the mixed scenario. Comparing unstripped
    silently drops the second group out of the free pool."""
    padded = _vehicle_row(vehicleCode="A", status_name=None)
    padded["status.name"] = "Available For Sale "
    bare = _vehicle_row(vehicleCode="B")
    bare["status.name"] = "Available For Sale"
    assert datasource.is_available(padded) and datasource.is_available(bare)
    assert datasource.in_pool(padded) and datasource.in_pool(bare)
    pull = _pull([_order_row()], [padded, bare])
    assert {v["VehicleCode"] for v in pull["vehicles"]} == {"A", "B"}


def test_a_car_held_by_an_order_is_still_supply():
    """An allocated car is not out of the pool — it is the car that order holds,
    and the solver may reassign it at a price. Only delivered/registered/demo
    stock leaves."""
    pull = _pull(
        [_order_row(vehicleCode="1004316")],
        [_vehicle_row(**{"status.name": "Dealer Order Confirmation", "status.code": "7"})],
    )
    assert [v["VehicleCode"] for v in pull["vehicles"]] == ["1004316"]
    assert pull["orders"][0]["VehicleCode"] == "1004316"


def test_a_car_out_of_the_pool_is_dropped_with_a_reason():
    pull = _pull([_order_row()], [_vehicle_row(**{"status.name": "Delivered"})])
    assert pull["vehicles"] == []
    assert pull["meta"]["excluded"]["vehicle_drops"] == {"out_of_pool_status": 1}


# --- eligibility -------------------------------------------------------------


def test_the_join_key_is_SalesModel_not_the_model_above_it():
    """`modelId.code` is the MODEL (T6480J1XXLX0018); the order names the full
    trim/colour code (T6480J1BXLX0018). Joining on the wrong one leaves every
    order with no car."""
    pull = _pull([_order_row()], [_vehicle_row()])
    assert pull["orders"][0]["SalesModel"] == pull["vehicles"][0]["SalesModel"]
    assert pull["vehicles"][0]["SalesModel"] != "T6480J1XXLX0018"
    assert pull["meta"]["excluded"]["orders_with_no_eligible_car"] == []


def test_a_vehicle_with_no_sales_model_is_dropped_rather_than_falling_back():
    pull = _pull([_order_row()], [_vehicle_row(SalesModel="")])
    assert pull["vehicles"] == []
    assert pull["meta"]["excluded"]["vehicle_drops"]["no_model"] == 1


# --- the funnel --------------------------------------------------------------


def test_an_order_with_no_promised_date_is_dropped_with_a_reason():
    pull = _pull([_order_row(etaDealer=""), _order_row(OrderId="500002")], [_vehicle_row()])
    assert [o["OrderId"] for o in pull["orders"]] == ["500002"]
    assert pull["meta"]["excluded"]["order_drops"] == {"no_promised_date": 1}
    assert pull["meta"]["excluded"]["orders_seen"] == 2
    assert pull["meta"]["excluded"]["orders_kept"] == 1


def test_an_order_with_no_model_is_dropped_with_a_reason():
    pull = _pull([_order_row(SalesModel="")], [_vehicle_row()])
    assert pull["orders"] == []
    assert pull["meta"]["excluded"]["order_drops"] == {"no_model": 1}


def test_a_car_no_order_wants_is_pruned():
    """Lossless with eligibility as equality, and it keeps the mounted file small.
    This is the one filter that has to go if eligibility ever stops being equality."""
    pull = _pull(
        [_order_row()],
        [_vehicle_row(), _vehicle_row(vehicleCode="OTHER", SalesModel="SOMETHING-ELSE")],
    )
    assert [v["VehicleCode"] for v in pull["vehicles"]] == ["1004316"]
    assert pull["meta"]["excluded"]["vehicle_drops"]["no_order_wants_this_model"] == 1


def test_an_order_with_no_matching_car_is_kept_and_named():
    """NOT a drop: unfilled demand is real, and the solver surfaces it. But the
    reply has to be able to say WHICH."""
    pull = _pull([_order_row(SalesModel="NOBODY-STOCKS-THIS")], [_vehicle_row()])
    assert len(pull["orders"]) == 1
    assert pull["meta"]["excluded"]["orders_with_no_eligible_car"] == ["500001"]


def test_a_duplicate_order_id_raises_rather_than_collapsing():
    """Two rows with one id is demand this pull cannot represent. Failing here
    names the file; `Snapshot.order_by_key` would only raise later, further from
    the cause."""
    with pytest.raises(ValueError, match="duplicate OrderId"):
        _pull([_order_row(), _order_row()], [_vehicle_row()])


# --- allocations -------------------------------------------------------------


def test_a_double_booked_car_yields_no_allocation_for_anyone():
    """A car claimed by two orders is not a valid matching and would trip the
    solver's self-check on its own INPUT. Both orders become unallocated demand,
    and the clash rides in meta rather than being swallowed."""
    pull = _pull(
        [
            _order_row(OrderId="500001", vehicleCode="1004316"),
            _order_row(OrderId="500002", vehicleCode="1004316"),
        ],
        [_vehicle_row()],
    )
    assert all(o["VehicleCode"] == "" for o in pull["orders"])
    assert pull["meta"]["conflicts"] == [{"vehicle": "1004316", "orders": ["500001", "500002"]}]
    assert pull["meta"]["excluded"]["link_drops"]["double_booked_vehicle"] == 2


def test_an_allocation_to_a_car_not_in_the_file_is_dropped_and_counted():
    pull = _pull([_order_row(vehicleCode="NOT-HERE")], [_vehicle_row()])
    assert pull["orders"][0]["VehicleCode"] == ""
    assert pull["meta"]["excluded"]["link_drops"]["vehicle_not_in_the_file"] == 1


def test_the_disruption_is_derived_not_declared():
    """Nothing in the export records "this shipment slipped 21 days" — the carve
    scripts bake the slip into `availableBy`. So the affected demand is derived:
    an allocated order whose car now lands past its promise."""
    pull = _pull(
        [
            _order_row(OrderId="LATE", vehicleCode="CAR-LATE"),
            _order_row(OrderId="OKAY", vehicleCode="CAR-OKAY"),
            _order_row(OrderId="NOCAR"),
        ],
        [
            _vehicle_row(vehicleCode="CAR-LATE", availableBy="2026-10-05T00:00:00.000Z"),
            _vehicle_row(vehicleCode="CAR-OKAY", availableBy="2026-09-01T00:00:00.000Z"),
        ],
    )
    # LATE only: an on-time order needs no repair, and an order with no car needs
    # no manifest — `partition` frees anything unassigned already.
    assert pull["disruption"]["disrupted_orders"] == ["LATE"]


# --- purity, and the payload split -------------------------------------------


def test_translate_is_pure():
    orders, vehicles = [_order_row()], [_vehicle_row()]
    before = (repr(orders), repr(vehicles))
    a = _pull(orders, vehicles)
    b = _pull(orders, vehicles)
    assert a == b, "same rows must give the same pull"
    assert (repr(orders), repr(vehicles)) == before, "input rows must not be mutated"


def test_the_two_payloads_carry_the_whole_pull_between_them():
    """What the host mounts has to be everything the sandbox needs: the demand,
    the pull date and the provenance in one, the supply in the other."""
    pull = datasource.get_source("scenario-mixed").pull()
    orders_doc = datasource.orders_payload(pull)
    vehicles_doc = datasource.vehicles_payload(pull)
    assert set(orders_doc) == {"now", "meta", "orders"}
    assert set(vehicles_doc) == {"vehicles"}
    snap = flatten(orders_doc, vehicles_doc)
    assert len(snap.orders) == len(pull["orders"])
    assert len(snap.vehicles) == len(pull["vehicles"])


# --- reading a scenario directory --------------------------------------------


def test_the_committed_scenarios_are_all_readable_and_solvable_shaped():
    """The three carves, and the counts each one is supposed to pose.

    Every book is three classes — no car, a late car, a car that arrives on time —
    and the third is the control group: without it a plan that moves everything
    cannot be told apart from one that moves only what it should. So each scenario
    is pinned on all three, not just on the disturbance it is named after.
    """
    expected = {
        "scenario-unallocated": {"orders": 10, "holding_no_car": 8, "late": 0, "on_time": 2},
        "scenario-delayed": {"orders": 10, "holding_no_car": 0, "late": 8, "on_time": 2},
        "scenario-mixed": {"orders": 10, "holding_no_car": 4, "late": 4, "on_time": 2},
    }
    assert set(datasource.scenarios()) == set(expected)
    for name, counts in expected.items():
        pull = datasource.get_source(name).pull()
        no_car = sum(1 for o in pull["orders"] if not o["VehicleCode"])
        late = len(pull["disruption"]["disrupted_orders"])
        assert len(pull["orders"]) == counts["orders"], name
        assert no_car == counts["holding_no_car"], name
        assert late == counts["late"], name
        assert len(pull["orders"]) - no_car - late == counts["on_time"], name


def test_a_missing_column_raises_and_names_it():
    """The CSV equivalent of a projection gap — except a header is checkable, so
    it fails at read time with the file in hand rather than as an empty funnel."""
    with pytest.raises(ValueError, match="missing column"):
        datasource.read_rows(DATA / "scenario-mixed" / "orders.csv", ("NotAColumn",))


def test_the_pull_date_is_the_scenarios_own_and_never_the_clock():
    """A static file plus a wall clock means the same rows mean something new
    tomorrow: an order late by 3 days becomes late by 4 with nothing changed."""
    assert datasource.scenario_now(DATA / "scenario-mixed") == NOW
    assert datasource.get_source("scenario-mixed").pull()["now"] == "2026-08-25"


def test_the_pull_date_can_be_overridden_for_a_what_if(monkeypatch):
    monkeypatch.setenv("XAS_PULL_NOW", "2026-09-01")
    assert datasource.scenario_now(DATA / "scenario-mixed") == date(2026, 9, 1)


def test_the_default_scenario_is_the_mixed_one_unless_the_environment_says(monkeypatch):
    monkeypatch.delenv("XAS_SCENARIO", raising=False)
    assert datasource.default_scenario() == "scenario-mixed"
    monkeypatch.setenv("XAS_SCENARIO", "scenario-delayed")
    assert datasource.default_scenario() == "scenario-delayed"


def test_an_unknown_scenario_is_refused_rather_than_silently_defaulted(monkeypatch):
    monkeypatch.setenv("XAS_SCENARIO", "scenario-nope")
    with pytest.raises(RuntimeError, match="scenario-nope"):
        datasource.default_scenario()


def test_census_reads_the_funnel_off_any_pull():
    text = datasource.census(datasource.get_source("scenario-mixed").pull())
    assert "orders   10 read  ->  10 usable" in text
    assert "holding no car: 4" in text
    assert "already late: 4" in text


def test_the_client_name_survives_into_the_snapshot_as_a_label():
    """`customer.name` is on every row of the export, and a planner steers by
    client ("prioritise Delek Motors") long before they steer by id. Carried
    end to end — pull, then flatten — so the agent can resolve a name to the
    orders that hold it. A LABEL only: no filter, no price."""
    pull = datasource.translate(
        [_order_row(**{"customer.name": " Delek Motors Fleet "})], [_vehicle_row()], now=NOW
    )
    assert pull["orders"][0]["Customer"] == "Delek Motors Fleet"

    snap = flatten({"now": pull["now"], "orders": pull["orders"]}, {"vehicles": pull["vehicles"]})
    assert snap.order_by_key()["500001"].customer == "Delek Motors Fleet"


def test_an_order_with_no_client_name_still_allocates():
    """The column is optional — absent, or blank on a row — because it prices
    nothing. Dropping such an order would lose real demand over a display field."""
    pull = datasource.translate([_order_row()], [_vehicle_row()], now=NOW)
    assert pull["orders"][0]["Customer"] == ""
    snap = flatten({"now": pull["now"], "orders": pull["orders"]}, {"vehicles": pull["vehicles"]})
    assert snap.order_by_key()["500001"].customer == ""


def test_the_committed_scenarios_carry_client_names():
    """Guards the real files, not a fixture: a re-carve that dropped the column
    would leave the skill promising something the data no longer has."""
    pull = datasource.ScenarioSource(DATA / "scenario-mixed").pull()
    named = [o for o in pull["orders"] if o["Customer"]]
    assert named, "the mixed scenario must carry client names"
    # one client holding several orders is the case the agent must group, not the
    # exception -- if this ever stops being true the grouping guidance is untested
    assert len({o["Customer"] for o in named}) < len(named)
