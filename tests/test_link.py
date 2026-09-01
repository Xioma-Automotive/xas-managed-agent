"""The deep link, and the one property that makes it worth having: the page it
opens shows the set the number was counted over.

Everything here is that claim, taken apart. A link is only trustworthy if the
filter survives the trip into a URL and back — so the round trip is the spine of
this file, run over both dialects, because the vehicles/accounts one rewrites the
filter on the way out and is only correct if the app's adapter rewrites it back.

The rest are the ways a link can be wrong while LOOKING right, which is the only
failure mode that matters here. A planner cannot audit a URL; they click it and
believe what loads.

- A raw `$` returns an empty page instead of an error, and every vehicle and
  account filter needs one.
- A viewer-relative filter re-scopes to whoever opens the link.
- The agent's own `count: 1` would hand a human a one-row page under a total of 89.

`link.py` runs in the sandbox and imports nothing from this repo, so it is loaded
by path here exactly as `resolve.py` is.
"""

import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import phrasebook  # repo root, host-side builder

_spec = importlib.util.spec_from_file_location(
    "link", REPO_ROOT / "skills" / "xas-reporting" / "link.py"
)
link = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(link)


def query_of(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}


# --------------------------------------------------------------------------
# The round trip. A link is the query, not a second query that agrees with it.
# --------------------------------------------------------------------------


def test_a_job_card_filter_survives_the_url_verbatim():
    """The job-card page forwards `filter` to the backend untouched, so what goes
    into the URL is what runs. This is the whole guarantee for that lane."""
    sent = {"JobClassification": "VRV", "DMSJCEntry": ["6813", "6815", "6816"]}
    assert link.parse_url(link.list_url("/vehicle_planning", sent)) == sent


def test_a_date_range_filter_survives_the_url():
    """The nested object case — `{start, end}` inside a filter, the shape every
    "opened in July" question produces."""
    sent = {
        "JobClassification": "Service",
        "CreateDateTime": {"start": "2026-08-01T21:00:00Z", "end": "2026-08-30T21:00:00Z"},
    }
    assert link.parse_url(link.list_url("/job_cards", sent)) == sent


def test_a_vehicle_filter_round_trips_through_the_page_adapter():
    """The vehicles lane, end to end: this file writes PascalCase with explicit
    operators, and the app's `coreAdaptedFilter` lowercases each dotted segment
    back. `_adapter` below is that step, so the assertion is the real round trip
    rather than a restatement of what `adapt_for_core` did."""
    sent = {"vehicleClassification": "Vehicle", "make.code": {"$in": ["202509221629"]}}
    in_url = link.parse_url(link.list_url("/vehicles", sent))

    assert in_url == {
        "VehicleClassification": {"$in": ["Vehicle"]},
        "Make.Code": {"$in": ["202509221629"]},
    }
    # What the backend receives, once the page has lowercased it again.
    assert _adapter(in_url) == {
        "vehicleClassification": {"$in": ["Vehicle"]},
        "make.code": {"$in": ["202509221629"]},
    }


def _adapter(filter_obj: dict) -> dict:
    """The app's passthrough branch, as the page runs it: every value here is
    already an all-`$` object, so `coreAdaptedFilter` applies lcFirst per dotted
    segment and changes nothing else."""

    def lc(part: str) -> str:  # the app's own `lcFirst`
        return part[:1].lower() + part[1:]

    return {
        key if key.startswith("$") else ".".join(lc(p) for p in key.split(".")): value
        for key, value in filter_obj.items()
    }


def test_wrapping_a_bare_value_is_what_reaches_the_passthrough_branch():
    """A bare value would be re-wrapped by the page from the tenant's field TYPE —
    `$like` for a string, a substring match reported as an exact one. Wrapping it
    here means the adapter has nothing left to decide."""
    assert link.adapt_for_core({"status.code": "Available"}) == {
        "Status.Code": {"$in": ["Available"]}
    }
    assert link.is_operator_object({"$in": ["Available"]})
    assert not link.is_operator_object({})
    assert not link.is_operator_object(["$in"])


def test_an_account_and_or_clause_is_left_exactly_as_written():
    """`$and` contents are not descended into by the adapter, so the camelCase
    inside must survive untouched — it is what the backend reads."""
    sent = {"$and": {"type": {"$in": ["customer"]}, "status": {"$in": ["New"]}}}
    assert link.parse_url(link.list_url("/accounts", sent)) == sent


def test_uc_first_does_not_lower_case_the_rest_of_the_name():
    """`.title()`/`.capitalize()` would turn `DMSJCEntry` into `Dmsjcentry`, which
    matches no field and returns an empty page."""
    assert link.uc_first("dMSJCEntry") == "DMSJCEntry"
    assert link.adapt_for_core({"vin": "X"}) == {"Vin": {"$in": ["X"]}}


# --------------------------------------------------------------------------
# The silent-wrong failures.
# --------------------------------------------------------------------------


def test_the_dollar_sign_is_percent_encoded():
    """A raw `$` in the query string returns "No results" rather than an error —
    verified against the live app on a filter matching five cars. Every vehicle
    and account link carries `$in`, so this single character is the difference
    between the right page and a page that says there is nothing there."""
    url = link.list_url("/vehicles", {"make.code": {"$in": ["MB"]}})
    assert "%24in" in url
    assert "$" not in url


def test_a_space_becomes_percent_20_and_not_a_plus():
    """`+` would be read back as a literal plus by the page's own parser, so a
    classification with a space in it would match nothing."""
    url = link.list_url("/job_cards", {"Accounts.Owner.AccountName": "Best dealer"})
    assert "%20" in url and "+" not in url
    assert link.parse_url(url)["Accounts.Owner.AccountName"] == "Best dealer"


def test_an_ampersand_in_a_value_does_not_split_the_query():
    """Unencoded it would end the `filter` parameter and start a new one, and the
    page would parse the truncated remainder as the whole filter."""
    url = link.list_url("/job_cards", {"Accounts.Owner.AccountName": "Smith & Sons"})
    assert set(query_of(url)) == {"paging", "filter", "sort", "kpi"}
    assert link.parse_url(url)["Accounts.Owner.AccountName"] == "Smith & Sons"


@pytest.mark.parametrize("key", ["Branch", "MyJobCards"])
def test_a_viewer_relative_filter_is_refused(key, monkeypatch):
    """`true` here means "whoever is logged in". The agent is the integration
    account and the planner is not, so the page would answer a different question
    under the number just printed. There is no encoding that fixes this."""
    assert link.viewer_relative({key: True}) == key
    monkeypatch.setattr(
        link.sys, "argv", ["link.py", "--route", "/job_cards", "--filter", f'{{"{key}": true}}']
    )
    with pytest.raises(SystemExit) as exit_info:
        link.main()
    assert key in str(exit_info.value)


def test_a_branch_named_by_id_is_still_linkable():
    """Only the boolean is viewer-relative. An explicit branch id means the same
    branch to everyone, which is exactly what the skill already requires."""
    sent = {"Branch": ["69f07fdaf930e4ee6d524dc1"]}
    assert link.viewer_relative(sent) is None
    assert link.parse_url(link.list_url("/job_cards", sent)) == sent


# --------------------------------------------------------------------------
# The link is for a human, so its paging is not the agent's.
# --------------------------------------------------------------------------


def test_the_link_pages_for_a_reader_not_for_the_count_that_produced_it():
    """A count question runs `count: 1` because it only reads the total. Passing
    that through would put a one-row page under an answer of 89."""
    url = link.list_url("/job_cards", {"JobClassification": "Service"})
    assert json.loads(query_of(url)["paging"]) == {"page": 1, "count": link.LINK_PAGE_SIZE}
    assert link.LINK_PAGE_SIZE > 1


def test_the_link_carries_no_kpi_and_no_legacy_paging_params():
    """`page`/`pageSize` as separate params are read by nothing — a link built
    with those silently falls back to defaults. A `kpi` id makes the page a saved
    view, which is a different question than the one just answered."""
    query = query_of(link.list_url("/job_cards", {"JobClassification": "Service"}))
    assert query["kpi"] == ""
    assert "page" not in query and "pageSize" not in query


def test_sort_defaults_to_empty_and_is_carried_when_given():
    assert json.loads(query_of(link.list_url("/job_cards", {}))["sort"]) == {}
    url = link.list_url("/job_cards", {}, {"CreateDateTime": "desc"})
    assert json.loads(query_of(url)["sort"]) == {"CreateDateTime": "desc"}


# --------------------------------------------------------------------------
# Detail pages.
# --------------------------------------------------------------------------


def test_every_link_is_relative():
    """The answer is read inside the app, so a path resolves against the host the
    planner is already on. An absolute one hard-codes a tenant's origin into a
    bundle shared by all of them, and it would not match the detail links the agent
    writes by hand, which have no host to know."""
    assert link.APP_BASE_URL == ""
    assert link.list_url("/job_cards", {"JobClassification": "Service"}).startswith("/job_cards?")


def test_the_detail_shapes_live_in_the_skill_not_in_this_file():
    """A detail page is a path and an id, so the agent writes it inline and link.py
    builds only the SET link. The three shapes are pinned in SKILL.md instead —
    nothing here may grow a second, drifting copy of them."""
    assert not hasattr(link, "detail_url")
    skill = (REPO_ROOT / "skills" / "xas-reporting" / "SKILL.md").read_text(encoding="utf-8")
    for shape in ("/job_cards/8745", "/vehicles/11370", "/accounts/655dc47b9c098a054a0791c3"):
        assert shape in skill


# --------------------------------------------------------------------------
# The route comes from the phrasebook, not from a table in the sandbox.
# --------------------------------------------------------------------------


def test_the_bundled_phrasebook_carries_the_route_for_every_classification_with_a_page():
    """`link.py --route` is fed from this column, so the column is the route map.
    Checked against the BUNDLED bytes for the same reason the normalizer is: a
    table built from a different split is the failure nobody would see."""
    import setup_agent

    table = dict(setup_agent.reporting_bundle())["xas-reporting/phrasebook.tsv"]
    rows = [tuple(line.split("\t")) for line in table.decode().splitlines()[1:] if line]
    cols = {name: i for i, name in enumerate(phrasebook.COLUMNS)}
    routes = {
        (row[cols["entity"]], row[cols["code"]]): row[cols["route"]]
        for row in rows
        if row[cols["kind"]] == "classification"
    }

    assert routes[("JobCard", "VRV")] == "/vehicle_planning"
    assert routes[("JobCard", "Service")] == "/job_cards"
    assert routes[("JobCard", "Reservation")] == "/contracts"
    assert routes[("Vehicle", "Vehicle")] == "/vehicles"
    assert routes[("Account", "customer")] == "/accounts"
    # Every job card lists somewhere: the app's own fallback is `/job_cards`.
    assert all(route for (entity, _), route in routes.items() if entity == "JobCard")


def test_naming_the_tool_is_naming_the_page():
    """Everything `get_vehicle_list` returns lists on one page, so the page is a
    fact about the call rather than something to look up. This is the whole reason
    `--tool` exists: a vehicle question resolves to a STATUS row, which carries no
    route, so the rule "take the route from the phrasebook" had nothing to take and
    a session typed `/vehicles` from memory instead."""
    sent = {"status.code": "03"}
    for tool, route in link.TOOL_ROUTES.items():
        assert link.list_url(route, sent) == link.list_url(link.TOOL_ROUTES[tool], sent)
    assert link.TOOL_ROUTES["get_vehicle_list"] == "/vehicles"
    assert link.TOOL_ROUTES["get_account_list"] == "/accounts"


def test_the_tool_routes_are_the_entity_routes():
    """`link.py` cannot import the builder, so these two are the same fact written
    twice and this is the joint that holds them together."""
    assert set(link.TOOL_ROUTES.values()) == set(phrasebook.ENTITY_ROUTES.values())


def test_a_job_card_tool_has_no_single_page(monkeypatch):
    """Job cards list on three pages by classification, so the tool name cannot fix
    one. The refusal names `--route` rather than picking a page."""
    assert "get_job_list" not in link.TOOL_ROUTES
    monkeypatch.setattr(link.sys, "argv", ["link.py", "--tool", "get_job_list", "--filter", "{}"])
    with pytest.raises(SystemExit) as exit_info:
        link.main()
    assert "--route" in str(exit_info.value)


def test_an_entity_with_no_read_tool_gets_no_route():
    """Activities and Items are in the taxonomy but behind no tool, so the agent
    can never have counted anything on such a page. An empty route makes that a
    refusal rather than a plausible link to a page it did not query."""
    assert phrasebook.route_for("classification", "Item", "SpareParts") == ""
    assert phrasebook.route_for("classification", "Activity", "Task") == ""


def test_only_classification_rows_carry_a_route():
    """A status or a branch is a filter VALUE, not a page — giving those a route
    invites a link built from the wrong row."""
    assert phrasebook.route_for("status", "JobCard", "Open") == ""
    assert phrasebook.route_for("branch", "", "") == ""


def test_the_three_way_split_matches_the_app_and_is_disjoint():
    """Transcribed from the app's enums; a code in both sets would make the route
    depend on which branch ran first."""
    assert not (phrasebook.VEHICLE_PLANNING & phrasebook.CONTRACTS)
    assert len(phrasebook.VEHICLE_PLANNING) == 14
    assert len(phrasebook.CONTRACTS) == 8


# --------------------------------------------------------------------------
# The skill file must not hold a second copy of anything the repo owns.
# --------------------------------------------------------------------------


def test_link_py_stands_alone_in_the_sandbox():
    """It ships without the repo, so it may import only the standard library —
    the same constraint `resolve.py` and `dates.py` are under."""
    source = (REPO_ROOT / "skills" / "xas-reporting" / "link.py").read_text(encoding="utf-8")
    assert "import phrasebook" not in source
    assert "xas_allocation" not in source


def test_link_py_holds_no_route_table_of_its_own():
    """The route is an argument, resolved from the phrasebook by the agent. A copy
    here is a second source that drifts from the taxonomy the moment either moves."""
    source = (REPO_ROOT / "skills" / "xas-reporting" / "link.py").read_text(encoding="utf-8")
    for code in ("VRV", "VPO", "BlanketAgreement", "LeaseContract"):
        assert code not in source
