"""The pull comes from a callable source, host-side (DECIDE-7).

Guards the seam:
  1. the scenario-engine fake fabricates the app MCP's own response shapes and
     maps them with the SAME `map_response` the live pull uses;
  2. the source is selected by env, defaulting to the offline fake;
  3. the real source refuses to construct without the app-MCP config;
  4. `map_response` — the filter+translate half of the real source — is PURE and
     is exercised against a CAPTURED real response (`tests/fixtures/xas_sample.json`,
     read-only from dev 2026-08-20), so the mapping is tested with no network.

(4) is the part worth the fixture: the field names are the whole risk. The app
MCP asks for `DueDate` where XAS stores `DueDateTime` and silently returns no
dates at all on all 25 VSOs — a fixture is what stops us shipping the same class
of bug.

**The grain is the line, and the capture predates it.** One `ModelItem` jobitem
is one order, keyed `{JobKey}-{LineNum}` — but the MCP's list projection returns
no `jobitems` at all, so not one captured card can carry a car line
(`test_the_projection_returns_no_car_lines_at_all` pins exactly that, and it is a
finding, not a fixture problem: the live allocation pull is EMPTY until the MCP is
widened — see `docs/mcp-field-spec.md`). So the card side of the mapping tests is
hand-built while the VEHICLE side stays the capture, which is where the join-key
risk actually lives. Re-capturing against a jobitems-bearing tool is the fix, and
it needs that tool to exist first.
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

# A SalesModel that really is on the captured vehicle rows (10 In Stock, 6 On The
# Way), so a hand-built card can join against real records rather than mock ones.
CAPTURED_MODEL = "T5040UECLMQ0009"
LATER = "2026-12-01T00:00:00Z"


def _sample() -> tuple[list[dict], list[dict]]:
    raw = json.loads(FIXTURE.read_text())
    return raw["vsos"]["list"], raw["vehicles"]["records"]


def _car(line: int, code: str, **extra) -> dict:
    """One `ModelItem` jobitem — the allocatable grain."""
    return {
        "LineNum": line,
        "JobItemCode": code,
        "JobItemType": "ModelItem",
        "JobItemStatus": "Open",
        "Quantity": 1,
        **extra,
    }


def _config(line: int) -> dict:
    """A non-car line. Every real card carries one; the type filter drops it."""
    return {"LineNum": line, "JobItemCode": "CCO", "JobItemType": "Configuration"}


def _card(key: str, lines: list[dict], due: str = LATER, **extra) -> dict:
    return {"JobEntryNum": key, "DueDateTime": due, "jobitems": lines, **extra}


def _mapped() -> dict:
    """The captured vehicles under hand-built cards that want CAPTURED_MODEL."""
    _, vehicles = _sample()
    orders = [
        _card("100", [_car(1, CAPTURED_MODEL), _config(2)]),
        _card("101", [_car(1, CAPTURED_MODEL), _car(2, CAPTURED_MODEL)]),
    ]
    return datasource.map_response(orders, vehicles, NOW)


def test_scenario_source_returns_the_rich_contract():
    rich = datasource.ScenarioEngineSource().pull()
    assert CONTRACT_KEYS <= set(rich), "scenario pull is missing contract keys"
    # and it is actually flatten-able into a non-empty snapshot
    snap = flatten(rich)
    assert snap.orders and snap.vehicles and snap.allocations


def test_scenario_source_matches_committed_dataset():
    """The default fake reads the committed MCP payloads and maps them; the
    committed `pull.json` is that same mapping's output, so the two agree
    byte-for-byte. `pull.json` is DERIVED, not authored."""
    rich = datasource.ScenarioEngineSource().pull()
    committed = json.loads(datasource.DATASET_PATH.read_text())
    assert rich == committed


def test_scenario_source_can_regenerate():
    rich = datasource.ScenarioEngineSource(regenerate=True, seed=20).pull()
    assert CONTRACT_KEYS <= set(rich)


def test_the_fake_and_the_committed_payloads_are_the_same_world():
    """Regenerating in memory and reading the committed files must agree — the
    files are the fake's only durable form, and a drift between them would show
    up as a plan that changes when nobody changed anything."""
    live = datasource.ScenarioEngineSource(regenerate=True, seed=20).pull()
    on_disk = datasource.ScenarioEngineSource().pull()
    assert json.dumps(live, sort_keys=True) == json.dumps(on_disk, sort_keys=True)


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


# --- what the captured response actually proves --------------------------------


def test_the_projection_returns_no_car_lines_at_all():
    """THE finding, not a fixture defect: the list projection carries no
    `jobitems`, so every captured card drops for having no car line and the live
    allocation pull is empty. A projection gap, absent from every row — the
    opposite fix from a tenant that has not filled a field in."""
    orders, vehicles = _sample()
    assert orders, "fixture must contain cards"
    assert not any(datasource.card_lines(card) for card in orders)
    # named as a gap by the real constant, not just by an ad-hoc tuple
    assert "jobitems" in datasource.missing_projection(orders, datasource.REQUIRED_CARD_FIELDS)

    rich = datasource.map_response(orders, vehicles, NOW)
    excluded = rich["meta"]["excluded"]
    assert rich["vsos"] == []
    assert excluded["orders_seen"] == len(orders)
    assert excluded["orders_kept"] == 0
    assert excluded["order_drops"]["no_car_line"] > 0
    # the funnel still accounts for every row it was handed
    assert (
        excluded["orders_kept"] + sum(excluded["order_drops"].values()) == excluded["orders_seen"]
    )


def test_the_promise_comes_from_DueDateTime():
    """The field the app MCP gets wrong. `DueDate` does not exist on a VSO."""
    orders, _ = _sample()
    dated = [o for o in orders if o.get("DueDateTime")]
    assert dated, "fixture must contain at least one dated VSO"
    assert not any(o.get("DueDate") for o in orders), "XAS stores DueDateTime, not DueDate"
    rich = _mapped()
    assert rich["vsos"], "the hand-built cards should survive"
    assert all(len(v["DeliveryDate"]) == 10 for v in rich["vsos"])


def test_mapped_pull_satisfies_the_rich_contract_and_flattens():
    rich = _mapped()
    assert CONTRACT_KEYS <= set(rich)
    snap = flatten(rich)
    # every surviving order carries a promise and a model; every vehicle a date
    assert snap.orders, "the sample should still yield at least one order"
    assert all(o.sales_model and o.delivery_date for o in snap.orders)
    assert all(u.sales_model and u.eta_dealer for u in snap.vehicles)


def test_the_join_key_is_SalesModel_not_ModelId():
    """An order names a trim/colour code; ModelId.Code is the model above it and
    matches nothing. Getting this backwards backorders every order."""
    _, vehicles = _sample()
    rich = _mapped()
    wanted = {item["SalesModelCode"] for v in rich["vsos"] for item in v["JobItems"]}
    assert wanted, "the hand-built cards must name a model"
    # the pull keeps only cars some order wants, so every vehicle matches by key
    assert {u["SalesModel"] for u in rich["vehicles"]} <= wanted
    # and those matched on SalesModel, not on the ModelId fallback
    assert any(datasource._text(v.get("SalesModel")) in wanted for v in vehicles)
    assert not any(
        datasource._text((v.get("ModelId") or {}).get("Code")) in wanted for v in vehicles
    ), "the captured ModelId.Code must not be what matched"


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
    assert datasource.vehicle_eta({}, NOW, "real") == NOW.isoformat()
    # AvailableBy is the PRIMARY source: it is the field the tenant fills (19
    # vehicles fleet-wide vs 3 for EtaDealer), so preferring the schema's nominal
    # field left nearly every future car undateable and therefore dropped.
    assert datasource.vehicle_eta({"AvailableBy": "2026-09-30T00:00:00.000Z"}, NOW, "future") == (
        "2026-09-30"
    )
    # EtaDealer is the fallback, still read when AvailableBy is blank.
    assert (
        datasource.vehicle_eta({"EtaDealer": "2026-10-01T00:00:00Z"}, NOW, "future") == "2026-10-01"
    )
    # Both set: AvailableBy wins. This is the assertion that pins the precedence
    # — either field alone passes whichever way round the code reads them.
    both = {"AvailableBy": "2026-09-30T00:00:00.000Z", "EtaDealer": "2026-10-01T00:00:00Z"}
    assert datasource.vehicle_eta(both, NOW, "future") == "2026-09-30"
    assert datasource.vehicle_eta({}, NOW, "future") == ""


# --- the grain: one ModelItem line is one order -------------------------------


def test_one_car_line_is_one_order():
    """A card with two car lines is two orders, keyed {JobKey}-{LineNum}, and its
    Configuration line is dropped by TYPE — the only thing separating them."""
    orders = [_card("7", [_car(1, "SM"), _car(2, "SM"), _config(3)])]
    vehicles = [{"VehicleCode": "V1", "SalesModel": "SM", "Status": {"Name": "In Stock"}}]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert [item["LineNum"] for item in rich["vsos"][0]["JobItems"]] == [1, 2]
    excluded = rich["meta"]["excluded"]
    assert excluded["lines_kept"] == 2
    assert excluded["line_drops"] == {"not_a_car_line": 1}
    snap = flatten(rich)
    assert sorted(o.key for o in snap.orders) == ["7-1", "7-2"]


def test_a_card_with_no_car_line_is_dropped_with_a_reason():
    orders = [_card("7", [_config(1)])]
    rich = datasource.map_response(orders, [], NOW)
    assert rich["vsos"] == []
    assert rich["meta"]["excluded"]["order_drops"]["no_car_line"] == 1
    assert rich["meta"]["excluded"]["line_drops"]["not_a_car_line"] == 1


def test_a_line_without_a_model_is_dropped_with_a_reason():
    """A car line with no model code has nothing to match a car on — the whole
    point of the filter. It must be counted, not silently missing."""
    orders = [_card("7", [_car(1, "SM"), {"LineNum": 2, "JobItemType": "ModelItem"}])]
    vehicles = [{"VehicleCode": "V1", "SalesModel": "SM", "Status": {"Name": "In Stock"}}]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert rich["meta"]["excluded"]["line_drops"]["no_model_on_the_line"] == 1
    assert rich["meta"]["excluded"]["lines_kept"] == 1


def test_a_dead_line_is_not_live_demand():
    orders = [
        _card(
            "7",
            [
                _car(1, "SM"),
                _car(2, "SM", JobItemStatus="Closed"),
                _car(3, "SM", IsDeleted=True),
            ],
        )
    ]
    vehicles = [{"VehicleCode": "V1", "SalesModel": "SM", "Status": {"Name": "In Stock"}}]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert rich["meta"]["excluded"]["line_drops"] == {"closed_line": 1, "deleted_line": 1}
    assert rich["meta"]["excluded"]["lines_kept"] == 1


def test_the_header_model_code_is_never_read():
    """The card's own `SalesModelCode` disagrees with the line on real data, and
    the detail shape does not carry it at all. The LINE is the eligibility key."""
    orders = [_card("7", [_car(1, "LINE")], SalesModelCode="HEADER", ModelCode="HEADER")]
    vehicles = [
        {"VehicleCode": "V1", "SalesModel": "LINE", "Status": {"Name": "In Stock"}},
        {"VehicleCode": "V2", "SalesModel": "HEADER", "Status": {"Name": "In Stock"}},
    ]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert rich["meta"]["sales_models"] == ["LINE"]
    assert [u["VehicleCode"] for u in rich["vehicles"]] == ["V1"]


def test_a_cancelled_card_is_dropped():
    orders = [_card("7", [_car(1, "SM")], isCanceled=True)]
    rich = datasource.map_response(orders, [], NOW)
    assert rich["vsos"] == []
    assert rich["meta"]["excluded"]["order_drops"]["cancelled"] == 1


# --- the allocations ----------------------------------------------------------


def test_a_double_booked_vehicle_yields_no_allocation_for_anyone():
    """Vehicle 10831 is claimed by three VSOs in dev. An allocation that
    double-books is not a valid matching; the solver would trip on its input.

    The claim arrives the only way the live MCP offers one — the CARD's
    `VehicleDMSCode` — which applies just to a single-car card. The
    Configuration line must not count toward that "single", or the conflict scan
    and the translate step disagree and the double-booking goes unrecorded.
    """
    orders = [
        _card("1", [_car(1, "SM"), _config(2)], VehicleDMSCode="10831"),
        _card("2", [_car(1, "SM"), _config(2)], VehicleDMSCode="10831"),
    ]
    vehicles = [{"VehicleCode": "10831", "SalesModel": "SM", "Status": {"Name": "In Stock"}}]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert rich["meta"]["conflicts"] == [{"vehicle": "10831", "orders": ["1-1", "2-1"]}]
    assert flatten(rich).allocations == {}
    assert rich["meta"]["excluded"]["link_drops"]["double_booked_vehicle"] == 2


def test_the_card_fallback_needs_a_single_car_line():
    """One header field cannot say WHICH of two car lines owns the car, and
    guessing would invent an allocation."""
    two = [_card("1", [_car(1, "SM"), _car(2, "SM")], VehicleDMSCode="V1")]
    one = [_card("1", [_car(1, "SM"), _config(9)], VehicleDMSCode="V1")]
    vehicles = [{"VehicleCode": "V1", "SalesModel": "SM", "Status": {"Name": "In Stock"}}]
    assert flatten(datasource.map_response(two, vehicles, NOW)).allocations == {}
    assert flatten(datasource.map_response(one, vehicles, NOW)).allocations == {"1-1": "V1"}


def test_the_line_link_beats_the_card_fallback():
    orders = [
        _card(
            "1",
            [_car(1, "SM", VehicleId={"Code": "V1"}), _car(2, "SM", AllocatedVehicleCode="V2")],
            VehicleDMSCode="V9",
        )
    ]
    vehicles = [
        {"VehicleCode": "V1", "SalesModel": "SM", "Status": {"Name": "In Stock"}},
        {
            "VehicleCode": "V2",
            "SalesModel": "SM",
            "Status": {"Name": "Ordered"},
            "EtaDealer": "2026-11-01T00:00:00Z",
        },
    ]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert flatten(rich).allocations == {"1-1": "V1", "1-2": "V2"}


def test_the_disruption_is_derived_not_declared():
    """XAS records no delay manifest, but solver.partition builds the free set
    from disrupted_orders — so a car landing past its promise must show up."""
    orders = [_card("9", [_car(1, "SM")], due="2026-09-01T00:00:00Z", VehicleDMSCode="V1")]
    vehicles = [
        {
            "VehicleCode": "V1",
            "SalesModel": "SM",
            "Status": {"Name": "Ordered"},
            "EtaDealer": "2026-10-15T00:00:00Z",
        },
    ]
    rich = datasource.map_response(orders, vehicles, NOW)
    # Per CAR — the vehicle is what slipped, and it carries exactly one car.
    assert rich["disruption"]["disrupted_orders"] == ["9-1"]


def test_an_on_time_car_is_not_disrupted():
    orders = [_card("9", [_car(1, "SM")], due="2026-11-01T00:00:00Z", VehicleDMSCode="V1")]
    vehicles = [
        {
            "VehicleCode": "V1",
            "SalesModel": "SM",
            "Status": {"Name": "Ordered"},
            "EtaDealer": "2026-10-15T00:00:00Z",
        },
    ]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert rich["disruption"]["disrupted_orders"] == []


# --- pruning and unfilled demand ---------------------------------------------


def test_a_car_no_order_wants_is_pruned():
    orders = [_card("9", [_car(1, "WANTED")])]
    vehicles = [
        {"VehicleCode": "A", "SalesModel": "WANTED", "Status": {"Name": "In Stock"}},
        {"VehicleCode": "B", "SalesModel": "OTHER", "Status": {"Name": "In Stock"}},
    ]
    rich = datasource.map_response(orders, vehicles, NOW)
    assert [u["VehicleCode"] for u in rich["vehicles"]] == ["A"]
    assert rich["meta"]["excluded"]["vehicle_drops"]["no_order_wants_this_model"] == 1


def test_an_order_with_no_matching_car_is_kept_and_named():
    """Unfilled demand is a real answer ('no compatible car free'), not a drop."""
    orders = [_card("9", [_car(1, "NOBODY_HAS_IT")])]
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
