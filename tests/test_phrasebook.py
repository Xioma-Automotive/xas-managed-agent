"""The taxonomy -> skill-vocabulary hop.

ONE module now, host-side: `phrasebook` (repo root) parses the taxonomy at deploy
time and renders the two blocks that are substituted into the reporting SKILL.md.
It never reaches the sandbox.

There is no lookup table and no matcher any more (2026-09-06). Everything a
lookup could ever have answered is 46 records — 21 job-card statuses, 13 vehicle
statuses, 5 lifecycle states, 7 branches — which is 1,051 tokens written out in
full, against 489 for ONE `--lookup` call answering three wordings and 1,070 for
the single `--list` call a breakdown needed. The tool cost more than the data on
every call and spent a round trip doing it, so the vocabulary is simply IN the
skill and `resolve.py` / `phrasebook.tsv` are gone. Git history holds them and
the 40-odd tests that pinned their rungs.

What these pin is what that hop still buys: the index's mixed quoting is parsed
correctly (booleans are unquoted and a naive regex drops them); the header legend
is not mistaken for data; every filterable type and every filterable value
reaches the agent; job-card statuses carry the id the APP filters by; and the
same index renders byte-identical blocks.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import phrasebook  # repo root, host-side renderer


def _index_lines():
    return phrasebook.INDEX_PATH.read_text(encoding="utf-8").splitlines()


def _classifications():
    return {
        (fields["entity"], fields["code"])
        for kind, fields in phrasebook.records()
        if kind == "classification" and fields["entity"] in phrasebook.FILTERABLE
    }


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


def test_unquoted_booleans_survive_parsing():
    """`closed=true` is unquoted; a (\\w+)="([^"]*)" regex would drop it."""
    closed = {
        fields["name"]
        for kind, fields in phrasebook.records()
        if kind == "status"
        and fields.get("classification") == "Service"
        and fields.get("closed") == "true"
    }
    assert closed == {"Closed", "Canceled"}


def test_header_legend_is_not_parsed_as_data():
    """The legend documents the format with the same key=<placeholder> syntax."""
    assert not [f for _, f in phrasebook.records() if any("<" in v for v in f.values())]


def test_a_record_is_one_entry_however_many_lines_carry_it():
    """The index lists a status once per classification that carries it — 96 lines
    for 21 JobCard statuses. Identity is (code, id, name), which is also what keeps
    vehicle `02` as TWO records: `On The Way` and `Available For Sale ` share a
    code, and collapsing them would hide the split behind one number."""
    parsed = phrasebook.records()
    assert len(phrasebook.distinct(parsed, "status", "JobCard")) == 21
    assert len(phrasebook.distinct(parsed, "status", "Vehicle")) == 13
    assert len(phrasebook.distinct(parsed, "state")) == 5
    assert len(phrasebook.distinct(parsed, "branch")) == 7

    codes = [f["code"] for f in phrasebook.distinct(parsed, "status", "Vehicle")]
    assert codes.count("02") == 2


# --------------------------------------------------------------------------
# The type list
# --------------------------------------------------------------------------


def test_classification_block_carries_every_filterable_type():
    """The skill lists the tenant's types inline instead of making the agent look
    them up, and `{{CLASSIFICATIONS}}` is substituted at deploy time from index.md.

    Hand-typing that list would put a second copy of the taxonomy in the repo,
    free to drift from the first. So what is pinned is that the generated block
    holds EVERY classification of the three entities a read tool can filter (job
    cards, vehicles, accounts) and nothing from the entities it cannot (Item,
    Activity, Model), and that the job-card types are grouped under the index's
    GROUP names — which is what a planner asking for a whole area by name
    ("vehicle sales", "sales cards") is answered from.
    """
    block = phrasebook.classification_block()

    expected = _classifications()
    assert len(expected) == 31, "the taxonomy changed — is the skill still the right size?"
    for _, code in expected:
        assert f"`{code}`" in block, f"{code} is missing from the type list"

    assert "SpareParts" not in block and "TestDrive" not in block

    # Declaration order: the reader meets the areas as the taxonomy names them.
    assert (
        block.index("\nVehicle service:")
        < block.index("\nVehicle sales:")
        < block.index("\nContracts:")
    )
    assert "`Service` Vehicle Service Order  (also:" in block
    assert "`VPO` Vehicle Purchase Order  (also: הזמנת רכש רכב)" in block
    # No page anywhere: the route groups the types, it no longer targets a link.
    assert "/vehicles" not in block and "/vehicle_planning" not in block


def test_the_type_list_says_the_entity_word_takes_no_filter():
    """The list is types, and the word for the WHOLE set is not one of them.

    A live session asked "how many job cards were opened this month" and spent a
    round trip resolving `job card`: every word in this block was a type, and
    nothing said what the word naming the entity does. It takes no filter at all.
    The words are generated from the index's ENTITY line, so they cannot drift.
    """
    block = phrasebook.classification_block()

    assert (
        'Job cards — "job cards", "jobs" means ALL of them, whatever their type: '
        "NO `JobClassification` filter at all." in block
    )
    for word, key in (("vehicles", "vehicleClassification"), ("accounts", "type")):
        assert f'"{word}" means ALL of them' in block
        assert f"NO `{key}` filter at all" in block

    business = {
        f["entity"]: f["businessType"] for kind, f in phrasebook.records() if kind == "entity"
    }
    assert business["JobCard"] == "jobs"


# --------------------------------------------------------------------------
# The vocabulary — what the lookup used to answer
# --------------------------------------------------------------------------


def test_vocabulary_block_holds_every_value_a_filter_can_take():
    """46 records, and the agent never asks for one: this block IS the answer to
    every question `--lookup` used to be asked."""
    block = phrasebook.vocabulary_block()
    parsed = phrasebook.records()

    for kind, entity in (
        ("status", "JobCard"),
        ("status", "Vehicle"),
        ("state", None),
        ("branch", None),
    ):
        for fields in phrasebook.distinct(parsed, kind, entity):
            if not fields.get("name"):
                continue
            assert fields["name"].strip() in block, f"{kind} {fields['name']!r} is missing"


def test_job_card_statuses_carry_the_id_the_app_itself_filters_by():
    """`JobStatus.Code` is honoured by the server and would save 21 ObjectIds
    (~290 tokens), and it is NOT what we send.

    Probed live 2026-09-06: nine cards store an ID that contradicts their own Code
    and Label — card 8629 displays `Canceled` and carries New's id — so the two
    filters disagree (Canceled 219 by id, 228 by code). The app's own job-card
    filter control emits `JobStatus.ID`
    (`app/src/components/Filters/Controls/JobStatus/index.tsx`), so the page a
    `ListUrl` opens is the ID-filtered one. Counting by code would print a number
    above a link that shows a different number, which is the one thing the link
    design exists to prevent.
    """
    block = phrasebook.vocabulary_block()
    assert "filter `JobStatus.ID` with the id" in block
    assert "- Open 6530d9a89c098a33be3e0c73 [In Process]" in block
    assert "- Canceled 6530d9a89c098a05a65b6765 [Closed] CLOSED" in block
    assert "JobStatus.Code" not in block


def test_two_vehicle_statuses_under_one_code_stay_two_lines():
    """Vehicle `02` is both `On The Way` and `Available For Sale `, so filtering
    the code returns their SUM. Two lines is the warning; the prose beside it says
    what to do instead."""
    block = phrasebook.vocabulary_block()
    assert "- On The Way `02`" in block
    assert "- Available For Sale `02`" in block
    assert "`status.name` with `$like`" in block


def test_a_status_no_dictionary_names_is_reported_not_dropped():
    """Four JobCard statuses are `unresolved=true`: carried by cards, missing from
    the dictionary, so they have a code and neither name nor id. They cannot be
    filtered and must not be guessed at, so the block names the codes and says so
    — silently omitting them is how those cards end up under a status they are
    not in."""
    block = phrasebook.vocabulary_block()
    assert "Codes 6, 8, 9, 11 are carried by cards but missing from the dictionary" in block
    assert "never guess which one it was" in block


def test_the_inactive_classifications_are_excluded_everywhere():
    """`VGR` and `LeaseContract` are inactive in the tenant config and were
    hand-maintained in `index.md` until 2026-09-03, when the list went ACTIVE
    ONLY at the user's call.

    They hold 39 live job cards between them (24 + 15, counted 2026-08-30), so
    the accepted cost is that a breakdown by type reports those 39 under no type
    at all — the earlier version of this test pinned the opposite, and git history
    holds it. What is pinned now is that nothing brings them back by halves.
    """
    import setup_agent

    for code in ("VGR", "LeaseContract"):
        assert code not in phrasebook.classification_block()
        assert code not in phrasebook.vocabulary_block()

    skill = dict(setup_agent.reporting_bundle())["xas-reporting/SKILL.md"].decode()
    assert "VGR" not in skill and "LeaseContract" not in skill


# --------------------------------------------------------------------------
# What ships
# --------------------------------------------------------------------------


def test_the_shipped_skill_leaves_no_marker_unsubstituted():
    """A SKILL.md that still says `{{VOCABULARY}}` when it reaches the agent is a
    skill with a hole in it, and the deploy would not complain."""
    import setup_agent

    skill = dict(setup_agent.reporting_bundle())["xas-reporting/SKILL.md"].decode()
    assert setup_agent.CLASSIFICATIONS_MARKER not in skill
    assert setup_agent.VOCABULARY_MARKER not in skill
    assert "{{" not in skill
    assert "\nVehicle service:" in skill
    assert "filter `Branch` with the id" in skill


def test_nothing_ships_that_could_look_a_word_up():
    """The deletion is the feature: with no table and no matcher in the sandbox,
    a classification cannot be resolved by a tool because no tool exists. The
    parser stays host-side, as it always did."""
    import setup_agent

    bundle = dict(setup_agent.reporting_bundle())
    assert sorted(bundle) == [
        "xas-reporting/SKILL.md",
        "xas-reporting/charts.md",
        "xas-reporting/dates.py",
    ]
    for name, blob in bundle.items():
        text = blob.decode()
        assert "resolve.py" not in text, name
        assert "phrasebook" not in text, name


def test_the_blocks_are_deterministic():
    """Same index in, byte-identical blocks out — the property that lets a deploy
    be re-run without changing what the agent reads."""
    assert phrasebook.classification_block() == phrasebook.classification_block()
    assert phrasebook.vocabulary_block() == phrasebook.vocabulary_block()


# --------------------------------------------------------------------------
# The route column groups a job card's types into the business areas a planner
# asks for by name ("vehicle sales", "sales cards"), which is what the skill
# renders as headings. Moved here whole from the deleted `tests/test_link.py`.
# --------------------------------------------------------------------------


def test_every_job_card_type_belongs_to_an_area():
    """The group map: it puts a job card's types under the area a planner asks for
    by name, which is what the block renders as headings."""
    routes = {
        code: phrasebook.route_for("classification", "JobCard", code)
        for entity, code in _classifications()
        if entity == "JobCard"
    }
    assert routes["VRV"] == "/vehicle_planning"
    assert routes["Service"] == "/job_cards"
    assert routes["Reservation"] == "/contracts"
    # Every job card belongs to an area: the app's own fallback is `/job_cards`.
    assert all(routes.values())


def test_only_a_job_card_type_carries_an_area():
    """Vehicles and accounts have one list each and no areas to split into, and a
    status or a branch is a filter VALUE rather than a type — giving either a group
    invites an area enumerated from the wrong rows."""
    assert phrasebook.route_for("classification", "Vehicle", "Truck") == ""
    assert phrasebook.route_for("classification", "Item", "SpareParts") == ""
    assert phrasebook.route_for("status", "JobCard", "Open") == ""
    assert phrasebook.route_for("branch", "", "") == ""


def test_the_three_way_split_matches_the_app_and_is_disjoint():
    """Transcribed from the app's enums; a code in both sets would make the group
    depend on which branch ran first."""
    assert not (phrasebook.VEHICLE_PLANNING & phrasebook.CONTRACTS)
    assert len(phrasebook.VEHICLE_PLANNING) == 14
    assert len(phrasebook.CONTRACTS) == 8
