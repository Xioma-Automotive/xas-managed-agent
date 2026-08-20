"""The pull comes from a callable source, host-side (DECIDE-7).

Guards the seam:
  1. the scenario-engine fake returns the rich contract and it flattens;
  2. the source is selected by env, defaulting to the offline fake;
  3. the real source refuses to construct without the app-MCP config;
  4. `map_response` — the filter+translate half of the real source — is PURE and
     is exercised against a CAPTURED real response (`tests/fixtures/xas_sample.json`,
     read-only from dev 2026-08-20), so the mapping is tested with no network.

(4) is the part worth the fixture: the field names are the whole risk. The app
MCP asks for `DueDate` where XAS stores `DueDateTime` and silently returns no
dates at all on all 25 VSOs — a fixture is what stops us shipping the same class
of bug.
"""

import json
from datetime import date
from pathlib import Path

import pytest

import datasource
from xas_allocation.flatten import flatten

CONTRACT_KEYS = {"meta", "vsos", "vehicles", "disruption"}

FIXTURE = Path(__file__).parent / "fixtures" / "xas_sample.json"
NOW = date(2026, 8, 20)  # the day the fixture was captured


def _sample() -> tuple[list[dict], list[dict]]:
    raw = json.loads(FIXTURE.read_text())
    return raw["vsos"]["list"], raw["vehicles"]["records"]


def _mapped() -> dict:
    orders, vehicles = _sample()
    return datasource.map_response(orders, vehicles, NOW)


def test_scenario_source_returns_the_rich_contract():
    rich = datasource.ScenarioEngineSource().pull()
    assert CONTRACT_KEYS <= set(rich), "scenario pull is missing contract keys"
    # and it is actually flatten-able into a non-empty snapshot
    snap = flatten(rich)
    assert snap.orders and snap.units and snap.incumbent


def test_scenario_source_matches_committed_dataset():
    """The default fake reads the committed dataset — stable and offline, so the
    determinism suite and this source agree byte-for-byte."""
    rich = datasource.ScenarioEngineSource().pull()
    committed = json.loads(datasource.DATASET_PATH.read_text())
    assert rich == committed


def test_scenario_source_can_regenerate():
    rich = datasource.ScenarioEngineSource(regenerate=True, seed=20).pull()
    assert CONTRACT_KEYS <= set(rich)


def test_get_source_defaults_to_scenario(monkeypatch):
    monkeypatch.delenv("XAS_DATA_SOURCE", raising=False)
    assert isinstance(datasource.get_source(), datasource.ScenarioEngineSource)


def test_get_source_selects_the_app_mcp(monkeypatch):
    monkeypatch.setenv("XAS_DATA_SOURCE", "xas")
    for name in datasource.appmcp_auth.REQUIRED_ENV:
        monkeypatch.setenv(name, "set")
    src = datasource.get_source()
    assert isinstance(src, datasource.AppMcpSource)
    assert src.url == datasource.appmcp_auth.APPMCP_URL


def test_the_real_source_needs_the_app_mcp_config(monkeypatch):
    """The real pull reads through the app MCP, so it needs the same host-side
    config the reporting lane does — the login AND the bearer's encryption key."""
    monkeypatch.setenv("XAS_DATA_SOURCE", "xas")
    for name in datasource.appmcp_auth.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="MCP_TOKEN_ENC_KEY"):
        datasource.get_source()


def test_a_field_the_mcp_never_returns_is_named_not_guessed_at():
    """The MCP projects. A field absent from EVERY row means the allowlist omits
    it (widen the MCP); a field absent from SOME rows means the tenant has not
    filled it in (data entry). Both produce an empty funnel; the fixes differ."""
    rows = [{"SalesModelCode": "SM", "DueDateTime": "x"}, {"SalesModelCode": "SM2"}]
    # DueDateTime is on one row, so it is data entry, not a projection gap
    assert datasource.missing_projection(rows, ("SalesModelCode", "DueDateTime")) == []
    # VehicleDMSCode is on neither, so the MCP is not returning it
    assert datasource.missing_projection(rows, ("VehicleDMSCode",)) == ["VehicleDMSCode"]
    # nothing collected at all tells us nothing about the projection
    assert datasource.missing_projection([], ("SalesModelCode",)) == []


# --- map_response: filter + translate, against a captured real response -------


def test_mapped_pull_satisfies_the_rich_contract_and_flattens():
    rich = _mapped()
    assert CONTRACT_KEYS <= set(rich)
    snap = flatten(rich)
    # every surviving order carries a promise and a model; every unit a date
    assert snap.orders, "the sample should still yield at least one order"
    assert all(o.sales_model and o.delivery_date for o in snap.orders)
    assert all(u.sales_model and u.eta_dealer for u in snap.units)


def test_an_order_without_a_model_is_dropped_with_a_reason():
    """A VSO with no SalesModelCode has nothing to match a car on — the whole
    point of the filter. It must be counted, not silently missing."""
    orders, vehicles = _sample()
    rich = datasource.map_response(orders, vehicles, NOW)
    excluded = rich["meta"]["excluded"]
    assert excluded["orders_seen"] == len(orders)
    assert excluded["orders_kept"] == len(rich["vsos"])
    assert excluded["order_drops"]["no_model_on_the_order"] > 0
    assert (
        excluded["orders_kept"] + sum(excluded["order_drops"].values()) == excluded["orders_seen"]
    )


def test_the_promise_comes_from_DueDateTime():
    """The field the app MCP gets wrong. `DueDate` does not exist on a VSO."""
    orders, vehicles = _sample()
    dated = [o for o in orders if o.get("DueDateTime")]
    assert dated, "fixture must contain at least one dated VSO"
    assert not any(o.get("DueDate") for o in orders), "XAS stores DueDateTime, not DueDate"
    rich = datasource.map_response(orders, vehicles, NOW)
    assert all(len(v["DeliveryDate"]) == 10 for v in rich["vsos"])


def test_the_join_key_is_SalesModel_not_ModelId():
    """An order names a trim/colour code; ModelId.Code is the model above it and
    matches nothing. Getting this backwards backorders every order."""
    orders, vehicles = _sample()
    rich = datasource.map_response(orders, vehicles, NOW)
    wanted = {v["SalesModelCode"] for v in rich["vsos"]}
    assert wanted, "fixture must contain a model-bearing VSO"
    # the pull keeps only cars some order wants, so every unit matches by key
    assert {u["SalesModel"] for u in rich["vehicles"]} <= wanted
    # and at least one of those matched on SalesModel, not on the fallback
    assert any(datasource._text(v.get("SalesModel")) in wanted for v in vehicles), (
        "the sample should join on SalesModel"
    )


def test_status_splits_future_from_real_by_name_not_code():
    """Code `02` is 'On The Way' (future) on some rows and 'Available For Sale '
    — trailing space — on others. Bucketing by code merges the two."""
    assert datasource.status_bucket({"Status": {"Code": "02", "Name": "On The Way"}}) == "future"
    assert (
        datasource.status_bucket({"Status": {"Code": "02", "Name": "Available For Sale "}})
        == "real"
    )
    assert datasource.status_bucket({"Status": {"Code": "07", "Name": "Customer"}}) is None
    assert datasource.status_bucket({}) is None


def test_a_real_car_is_available_now_and_a_future_one_needs_a_date():
    assert datasource.unit_eta({}, NOW, "real") == NOW.isoformat()
    assert datasource.unit_eta({"EtaDealer": "2026-10-01T00:00:00Z"}, NOW, "future") == "2026-10-01"
    # AvailableBy is the fallback — the field the tenant actually fills
    assert datasource.unit_eta({"AvailableBy": "2026-09-30T00:00:00.000Z"}, NOW, "future") == (
        "2026-09-30"
    )
    assert datasource.unit_eta({}, NOW, "future") == ""


def test_a_double_booked_vehicle_yields_no_incumbent_for_anyone():
    """Vehicle 10831 is claimed by three VSOs in dev. An incumbent that
    double-books is not a valid matching; the solver would trip on its input."""
    orders = [
        {
            "JobEntryNum": "1",
            "SalesModelCode": "SM",
            "DueDateTime": "2026-12-01T00:00:00Z",
            "VehicleDMSCode": "10831",
        },
        {
            "JobEntryNum": "2",
            "SalesModelCode": "SM",
            "DueDateTime": "2026-12-01T00:00:00Z",
            "VehicleDMSCode": "10831",
        },
    ]
    vehicles = [{"VehicleCode": "10831", "SalesModel": "SM", "Status": {"Name": "In Stock"}}]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert rich["meta"]["conflicts"] == [{"vehicle": "10831", "orders": ["1", "2"]}]
    assert flatten(rich).incumbent == {}
    assert rich["meta"]["excluded"]["link_drops"]["double_booked_vehicle"] == 2


def test_the_disruption_is_derived_not_declared():
    """XAS records no delay manifest, but solver.partition builds the free set
    from disrupted_orders — so a car landing past its promise must show up."""
    orders = [
        {
            "JobEntryNum": "9",
            "SalesModelCode": "SM",
            "DueDateTime": "2026-09-01T00:00:00Z",
            "VehicleDMSCode": "V1",
        },
    ]
    vehicles = [
        {
            "VehicleCode": "V1",
            "SalesModel": "SM",
            "Status": {"Name": "Ordered"},
            "EtaDealer": "2026-10-15T00:00:00Z",
        },
    ]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert rich["disruption"]["disrupted_orders"] == ["9-1"]


def test_a_car_no_order_wants_is_pruned():
    orders = [
        {"JobEntryNum": "9", "SalesModelCode": "WANTED", "DueDateTime": "2026-12-01T00:00:00Z"},
    ]
    vehicles = [
        {"VehicleCode": "A", "SalesModel": "WANTED", "Status": {"Name": "In Stock"}},
        {"VehicleCode": "B", "SalesModel": "OTHER", "Status": {"Name": "In Stock"}},
    ]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert [u["VehicleCode"] for u in rich["vehicles"]] == ["A"]
    assert rich["meta"]["excluded"]["unit_drops"]["no_order_wants_this_model"] == 1


def test_an_order_with_no_matching_car_is_kept_and_named():
    """Unfilled demand is a real answer ('no compatible car free'), not a drop."""
    orders = [
        {
            "JobEntryNum": "9",
            "SalesModelCode": "NOBODY_HAS_IT",
            "DueDateTime": "2026-12-01T00:00:00Z",
        },
    ]
    rich = datasource.map_response(orders, [], NOW)
    assert len(rich["vsos"]) == 1
    assert rich["meta"]["excluded"]["orders_with_no_eligible_car"] == ["9-1"]


def test_map_response_is_pure():
    orders, vehicles = _sample()
    a = datasource.map_response(orders, vehicles, NOW)
    b = datasource.map_response(orders, vehicles, NOW)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_census_reads_the_funnel_off_any_pull():
    text = datasource.census(_mapped())
    assert "orders" in text and "vehicles" in text
    assert datasource.census(datasource.ScenarioEngineSource().pull())


def test_get_source_rejects_unknown(monkeypatch):
    monkeypatch.setenv("XAS_DATA_SOURCE", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="unknown"):
        datasource.get_source()
