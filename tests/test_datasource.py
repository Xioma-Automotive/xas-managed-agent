"""`datasource` is the one mapping: the export's two CSVs -> the ONE mounted
document the "Allocation solve contract" specifies.

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
  * **identity is `DMSJCNum` + `LineNum`**, and one place builds it. Two spellings
    of one key match nothing and say nothing about why.
"""

import collections
from datetime import date
from pathlib import Path

import pytest

import datasource
from xas_allocation.flatten import flatten

NOW = date(2026, 8, 25)
DATA = Path(datasource.DATA_DIR)


def _order_row(**over) -> dict:
    """One export row AFTER `scenario_engine.dms_fields` — the shape `translate`
    actually reads. `DMSJCNum`/`LineNum` are the identity; `OrderId` is the
    vendor's own reference and rides along."""
    row = {
        "DMSJCNum": "900001",
        "LineNum": "1",
        "DMSJCEntry": "100001",
        "JobStatus.Code": "02",
        "JobStatus.Label": "In Process",
        "Accounts.Owner.AccountDMSCode": "C0001",
        "OrderId": "500001",
        "vehicleCode": "",
        "modelId.name": "OMODA9 PHEV Premium",
        "SalesModel": "T6480J1BXLX0018",
        "etaDealer": "2026-09-20T00:00:00.000Z",
        "allocationType": "hard",
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
        "inventoryStatus": "7",
        "inv status label": "Sea Transit",
        "status.code": "2",
        "status.name": "Available For Sale ",
        "availableBy": "2026-09-22T12:00:00.000Z",
        "InventoryEnteringDate": "",
    }
    row.update(over)
    return row


def _pull(orders, vehicles, now=NOW):
    return datasource.translate(orders, vehicles, now=now)


def _keys(pull) -> list[str]:
    return [datasource.order_key(o) for o in pull["orders"]]


# --- the two dates -----------------------------------------------------------


def test_the_promise_comes_from_the_order_and_the_arrival_from_the_car():
    pull = _pull(
        [_order_row(vehicleCode="1004316", etaDealer="2026-09-20T00:00:00.000Z")],
        [_vehicle_row(availableBy="2026-10-05T00:00:00.000Z")],
    )
    assert pull["orders"][0]["DeliveryDate"] == "2026-09-20"
    assert pull["vehicles"][0]["AvailableBy"] == "2026-10-05"
    # and the order is late BECAUSE the car lands after the order's own date
    assert pull["disruption"]["disrupted_orders"] == ["900001-1"]


def test_the_time_of_day_is_dropped_but_the_day_is_not_shifted():
    """The export stamps midnight, noon and 22:00. Only the day matters to a
    promise, and a timezone hop would move an order a day out of step with the
    planner's calendar."""
    pull = _pull(
        [_order_row(etaDealer="2026-09-20T22:00:00.000Z")],
        [_vehicle_row(availableBy="2026-09-22T12:00:00.000Z")],
    )
    assert pull["orders"][0]["DeliveryDate"] == "2026-09-20"
    assert pull["vehicles"][0]["AvailableBy"] == "2026-09-22"


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
    assert pull["meta"]["excluded"]["vehicle_drops"] == {"not_dealer_supply": 1}


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
    pull = _pull([_order_row(etaDealer=""), _order_row(LineNum="2")], [_vehicle_row()])
    assert _keys(pull) == ["900001-2"]
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
    assert pull["meta"]["excluded"]["orders_with_no_eligible_car"] == ["900001-1"]


def test_a_duplicate_card_and_line_raises_rather_than_collapsing():
    """Two rows with one card+line is demand this pull cannot represent. Failing
    here names the file; `Snapshot.order_by_key` would only raise later, further
    from the cause. Note that two rows sharing a CARD are fine — that is a
    two-line card, which is the ordinary case."""
    with pytest.raises(ValueError, match="duplicate card"):
        _pull([_order_row(), _order_row()], [_vehicle_row()])
    two_lines = _pull([_order_row(), _order_row(LineNum="2")], [_vehicle_row()])
    assert _keys(two_lines) == ["900001-1", "900001-2"]


# --- allocations -------------------------------------------------------------


def test_a_double_booked_car_yields_no_allocation_for_anyone():
    """A car claimed by two orders is not a valid matching and would trip the
    solver's self-check on its own INPUT. Both orders become unallocated demand,
    and the clash rides in meta rather than being swallowed."""
    pull = _pull(
        [
            _order_row(LineNum="1", vehicleCode="1004316"),
            _order_row(LineNum="2", vehicleCode="1004316"),
        ],
        [_vehicle_row()],
    )
    assert all(o["VehicleCode"] is None for o in pull["orders"])
    assert pull["meta"]["conflicts"] == [{"vehicle": "1004316", "orders": ["900001-1", "900001-2"]}]
    assert pull["meta"]["excluded"]["link_drops"]["double_booked_vehicle"] == 2


def test_an_allocation_to_a_car_not_in_the_file_is_dropped_and_counted():
    pull = _pull([_order_row(vehicleCode="NOT-HERE")], [_vehicle_row()])
    assert pull["orders"][0]["VehicleCode"] is None
    assert pull["meta"]["excluded"]["link_drops"]["vehicle_not_in_the_file"] == 1


def test_the_disruption_is_derived_not_declared():
    """Nothing in the export records "this shipment slipped 21 days" — the carve
    scripts bake the slip into `availableBy`. So the affected demand is derived:
    an allocated order whose car now lands past its promise."""
    pull = _pull(
        [
            _order_row(LineNum="1", vehicleCode="CAR-LATE"),
            _order_row(LineNum="2", vehicleCode="CAR-OKAY"),
            _order_row(LineNum="3"),
        ],
        [
            _vehicle_row(vehicleCode="CAR-LATE", availableBy="2026-10-05T00:00:00.000Z"),
            _vehicle_row(vehicleCode="CAR-OKAY", availableBy="2026-09-01T00:00:00.000Z"),
        ],
    )
    # line 1 only: an on-time order needs no repair, and an order with no car
    # needs no manifest — `partition` frees anything unassigned already.
    assert pull["disruption"]["disrupted_orders"] == ["900001-1"]


# --- purity, and the payload split -------------------------------------------


def test_translate_is_pure():
    orders, vehicles = [_order_row()], [_vehicle_row()]
    before = (repr(orders), repr(vehicles))
    a = _pull(orders, vehicles)
    b = _pull(orders, vehicles)
    assert a == b, "same rows must give the same pull"
    assert (repr(orders), repr(vehicles)) == before, "input rows must not be mutated"


def test_the_document_is_the_contracts_shape_and_carries_the_whole_pull():
    """One document, both streams, the three header fields on it — and the drop
    funnel, because the sandbox has to be able to say what the pull could not
    use. `disruption` is deliberately NOT in it: `flatten` re-derives it, and a
    second copy is a second thing to disagree."""
    pull = datasource.get_source("scenario-mixed").pull()
    doc = datasource.document(pull)
    assert set(doc) == {"captured_at", "pull_id", "source", "meta", "orders", "vehicles"}
    assert doc["captured_at"] == "2026-08-25" and doc["source"] == "scenario-mixed"
    assert doc["pull_id"]
    snap = flatten(doc)
    assert len(snap.orders) == len(pull["orders"])
    assert len(snap.vehicles) == len(pull["vehicles"])


def test_the_header_fields_are_top_level_and_no_longer_under_meta():
    """`source` and the pull date moved OUT of `meta` when the pull became one
    document, and a reader left on the old path raises `KeyError` at the moment a
    session is created — which is how it reached a browser as a 500 rather than a
    test failure. Pinned on both sides: present at the top, absent from `meta`."""
    pull = datasource.get_source("scenario-mixed").pull()
    for field in ("captured_at", "pull_id", "source"):
        assert pull[field], f"{field} must be a top-level header field"
        assert field not in pull["meta"], f"{field} must not be duplicated into meta"
    assert "now" not in pull and "now" not in pull["meta"], "the pull date is `captured_at`"


def test_every_column_the_contract_requires_is_on_every_row():
    """The contract's "columns that must be present" list, checked against the
    real files rather than a fixture. A column the carve stopped writing would
    otherwise surface as a field that is quietly absent in the sandbox."""
    doc = datasource.document(datasource.get_source("scenario-mixed").pull())
    for order in doc["orders"]:
        assert set(order) >= {
            "DMSJCNum",
            "LineNum",
            "DMSJCEntry",
            "SalesModel",
            "DeliveryDate",
            "VehicleCode",
            "Accounts.Owner.AccountName",
            "Accounts.Owner.AccountDMSCode",
            "AllocType",
            "JobStatus",
        }
        assert set(order["JobStatus"]) == {"Code", "Label"}
    for vehicle in doc["vehicles"]:
        assert set(vehicle) >= {
            "VehicleCode",
            "SalesModel",
            "AvailableBy",
            "InventoryEnteringDate",
            "Status",
            "InventoryStatus",
            *datasource.UNSOURCED_VEHICLE_FIELDS,
        }
        assert set(vehicle["Status"]) == {"Code", "Name"}


def test_the_columns_this_export_cannot_source_are_present_and_null():
    """Present because the contract requires them; NULL because a plausible value
    would read as real. A car with no `OpenDamage` on record must not come back
    looking undamaged."""
    doc = datasource.document(datasource.get_source("scenario-mixed").pull())
    for vehicle in doc["vehicles"]:
        for name in datasource.UNSOURCED_VEHICLE_FIELDS:
            assert vehicle[name] is None, name


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
    assert datasource.get_source("scenario-mixed").pull()["captured_at"] == "2026-08-25"


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
    orders that hold it. A LABEL only: no filter, no price. Its account CODE
    rides beside it, which is what a name should be resolved THROUGH."""
    pull = datasource.translate(
        [_order_row(**{"customer.name": " Delek Motors Fleet "})], [_vehicle_row()], now=NOW
    )
    assert pull["orders"][0]["Accounts.Owner.AccountName"] == "Delek Motors Fleet"
    assert pull["orders"][0]["Accounts.Owner.AccountDMSCode"] == "C0001"

    order = flatten(datasource.document(pull)).order_by_key()["900001-1"]
    assert order.customer == "Delek Motors Fleet" and order.account_code == "C0001"


def test_an_order_with_no_client_name_still_allocates():
    """The column is optional — absent, or blank on a row — because it prices
    nothing. Dropping such an order would lose real demand over a display field."""
    pull = datasource.translate([_order_row()], [_vehicle_row()], now=NOW)
    assert pull["orders"][0]["Accounts.Owner.AccountName"] == ""
    assert flatten(datasource.document(pull)).order_by_key()["900001-1"].customer == ""


def test_the_commitment_rides_through_and_prices_the_broken_promise():
    """`allocationType` -> `AllocType` -> `Order.alloc_type` -> `break_cost`.

    DECIDE-3 was re-split on 2026-09-09 on this column. The mapping is the whole
    reason it could be: the firmness has to survive the pull and the flatten to
    reach the one function that prices it."""
    from xas_allocation import solver

    pull = datasource.translate(
        [_order_row(allocationType="soft", vehicleCode="1004316")],
        # on time, so the break cost is the one a KEPT promise costs — the case
        # the retired hard/soft split used to make cheaper for a reservation
        [_vehicle_row(availableBy="2026-09-01T00:00:00.000Z")],
        now=NOW,
    )
    assert pull["orders"][0]["AllocType"] == "soft"
    snap = flatten(datasource.document(pull))
    soft = snap.order_by_key()["900001-1"]
    assert soft.alloc_type == "soft"
    assert solver.break_cost_of(soft, snap.vehicles[0]) == solver.CFG["break_cost"]["soft"]


def test_the_committed_scenarios_carry_client_names():
    """Guards the real files, not a fixture: a re-carve that dropped the column
    would leave the skill promising something the data no longer has."""
    pull = datasource.ScenarioSource(DATA / "scenario-mixed").pull()
    named = [o for o in pull["orders"] if o["Accounts.Owner.AccountName"]]
    assert named, "the mixed scenario must carry client names"
    # one client holding several orders is the case the agent must group, not the
    # exception -- if this ever stops being true the grouping guidance is untested
    assert len({o["Accounts.Owner.AccountName"] for o in named}) < len(named)


def test_the_committed_scenarios_carry_a_multi_line_card():
    """The card+line key only earns its keep if a card really has two lines on it.
    A carve that lost that would leave the card-level steering rule untested and
    the grain looking like ceremony."""
    pull = datasource.ScenarioSource(DATA / "scenario-mixed").pull()
    cards = collections.Counter(o["DMSJCNum"] for o in pull["orders"])
    assert max(cards.values()) > 1, "no card in the mixed scenario has two lines"


def test_the_order_key_is_built_in_one_place():
    """A key that is `900001-1` on the host and `900001-1.0` in the sandbox matches
    nothing and says nothing about why, so both sides go through one builder over
    the same two columns."""
    assert datasource.order_key({"DMSJCNum": "900001", "LineNum": 1}) == "900001-1"
    assert datasource.order_key({"DMSJCNum": "900001", "LineNum": " 2 "}) == "900001-2"
    # half a key names nothing, and is not guessed at
    assert datasource.order_key({"DMSJCNum": "900001", "LineNum": ""}) == ""
    assert datasource.order_key({"LineNum": "1"}) == ""


def test_the_same_car_twice_is_one_car():
    """The contract's `vehicles[]` names a car once per line that could take it —
    `candidates[].vehicle` repeats across lines, and a car already on a workspace
    row can come back as a candidate too. Supply is capacity-1, so a duplicate
    would be a second car to allocate that does not exist."""
    pull = datasource.translate(
        [_order_row()],
        [_vehicle_row(), _vehicle_row(), _vehicle_row(vehicleCode="OTHER-CAR")],
        now=NOW,
    )
    codes = [v["VehicleCode"] for v in pull["vehicles"]]
    assert len(codes) == len(set(codes)) == 2
    assert pull["meta"]["excluded"]["vehicle_drops"] == {"duplicate_vehicle": 1}
