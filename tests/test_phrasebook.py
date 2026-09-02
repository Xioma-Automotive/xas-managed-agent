"""The taxonomy->lookup-table hop, and the query side that reads what it built.

Two modules, split along where they RUN: `phrasebook` (repo root) parses the
taxonomy at deploy time and never reaches the sandbox; `resolve` (in the skill)
is what the agent runs against the shipped table. They share ONE `normalize`,
which `phrasebook` imports from `resolve` — the test at the bottom pins that the
column the table was built with really is that function's output.

The original point stands for both:

The point of it is that term -> system code stays deterministic code instead of
being re-derived by the model each turn. These tests pin what that buys: Hebrew
typed without niqqud matches the stored form; the index's mixed quoting is parsed
correctly (booleans are unquoted and a naive regex drops them); the header legend
is not mistaken for data; every alias becomes its own searchable row; and the same
index yields a byte-identical phrasebook.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
INDEX = REPO_ROOT / "skills" / "xas-reporting" / "index.md"

import phrasebook  # repo root, host-side builder

_spec = importlib.util.spec_from_file_location(
    "resolve", REPO_ROOT / "skills" / "xas-reporting" / "resolve.py"
)
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)


@pytest.fixture(scope="module")
def rows():
    return phrasebook.build(INDEX)


def _cols(row):
    return dict(zip(phrasebook.COLUMNS, row))


def test_normalize_strips_niqqud_so_plain_hebrew_matches():
    """The miss that motivated the phrasebook: users do not type vowel points."""
    assert phrasebook.normalize("חֲלָפִים") == "חלפים"


def test_normalize_casefolds_and_collapses_space():
    assert phrasebook.normalize("  Vehicle   Purchase ORDER ") == "vehicle purchase order"


def test_stored_hebrew_alias_is_findable_as_typed(rows):
    hits = [_cols(r) for r in rows if r[0] == "חלפים"]
    assert [h["code"] for h in hits] == ["SpareParts"]
    assert hits[0]["surface"] == "חֲלָפִים"


def test_unquoted_booleans_survive_parsing(rows):
    """`closed=true` is unquoted; a (\\w+)="([^"]*)" regex would drop it."""
    closed = {
        _cols(r)["name"]
        for r in rows
        if _cols(r)["classification"] == "Service"
        and _cols(r)["kind"] == "status"
        and _cols(r)["closed"] == "true"
    }
    assert closed == {"Closed", "Canceled"}


def test_header_legend_is_not_parsed_as_data(rows):
    """The legend documents the format with the same key=<placeholder> syntax."""
    assert not [r for r in rows if "<" in r[1] or ">" in r[1]]


def test_every_alias_becomes_its_own_row(rows):
    """One row per surface string is what makes a single anchored grep enough."""
    vpo = {_cols(r)["surface"]: _cols(r)["role"] for r in rows if _cols(r)["code"] == "VPO"}
    assert vpo == {
        "VPO": "code",
        "Vehicle Purchase Order": "name",
        "הזמנת רכש רכב": "alias",
    }


def test_code_and_name_diverge_and_both_resolve(rows):
    """code="Evaluation" is named "Service Lead" — the trap the agent must not infer
    around. `Service` used to be the example, named "Distinct_name" until 2026-08-27,
    when that turned out to be dev junk rather than a tenant rename."""
    by_surface = {
        _cols(r)["surface"]: _cols(r) for r in rows if _cols(r)["kind"] == "classification"
    }
    assert by_surface["Evaluation"]["name"] == "Service Lead"
    assert by_surface["Service Lead"]["code"] == "Evaluation"
    assert by_surface["Service"]["name"] == "Vehicle Service Order"


def test_ambiguous_alias_yields_both_candidates(rows):
    """קריאת שירות belongs to two classifications; the agent must ask, not guess."""
    codes = sorted(_cols(r)["code"] for r in rows if r[0] == phrasebook.normalize("קריאת שירות"))
    assert codes == ["Service", "ServiceCall"]


def test_status_rows_carry_the_id_filtering_needs(rows):
    closed = [
        _cols(r)
        for r in rows
        if _cols(r)["kind"] == "status"
        and _cols(r)["classification"] == "Service"
        and _cols(r)["surface"] == "Closed"
    ]
    assert closed and closed[0]["id"] == "6530d9a89c098a05a65b6764"
    assert closed[0]["state"] == "Closed"


def test_branch_resolves_to_the_id_a_filter_takes(rows):
    """A branch has no code: `{"Branch": ["Main"]}` returns 0 with no error, so
    the ObjectId in `id` is the only thing a Branch filter can be built from."""
    main = [_cols(r) for r in rows if _cols(r)["kind"] == "branch" and r[0] == "main"]
    assert main and main[0]["id"] == "69f07fdaf930e4ee6d524dc1"
    assert main[0]["code"] == ""


def test_every_branch_is_one_row(rows):
    branches = {_cols(r)["name"]: _cols(r)["id"] for r in rows if _cols(r)["kind"] == "branch"}
    assert len(branches) == 7
    assert branches["Service Branch"] == "69f209400e50752cea08ce26"


def test_a_branch_name_can_collide_with_a_classification(rows):
    """ "Potain" is a branch AND the display name of the Warranty classification.
    Both rows must come back so the agent reads `kind` instead of taking the first."""
    kinds = sorted(_cols(r)["kind"] for r in rows if r[0] == "potain")
    assert kinds == ["branch", "classification"]


def test_build_is_deterministic():
    assert phrasebook.build(INDEX) == phrasebook.build(INDEX)


# --------------------------------------------------------------------------
# suggest(): the typo rung of the ladder. Exact and substring search both need
# the letters to be right; a misspelling defeats them, and no amount of synonym
# guessing recovers "sapre parts". These are CANDIDATES for the user to confirm.
# --------------------------------------------------------------------------


def test_suggest_recovers_a_misspelling(rows):
    codes = {_cols(r)["code"] for r in resolve.suggest("sapre parts", rows)}
    assert "SpareParts" in codes


def test_suggest_recovers_a_hebrew_misspelling(rows):
    """חלפם is חלפים with a letter dropped — normalization alone cannot bridge it."""
    codes = {_cols(r)["code"] for r in resolve.suggest("חלפם", rows)}
    assert "SpareParts" in codes


def test_suggest_returns_nothing_for_a_term_the_tenant_lacks(rows):
    """The point of the ladder's last rung: an honest empty, so the agent asks
    instead of dressing up the nearest row as an answer."""
    assert resolve.suggest("zzqqxx wobble", rows) == []


def test_suggest_is_deterministic_and_bounded(rows):
    first = resolve.suggest("srvice", rows)
    assert first == resolve.suggest("srvice", rows)
    assert 0 < len(first) <= resolve.SUGGEST_LIMIT


def test_suggest_returns_one_row_per_candidate_wording(rows):
    """Deduped on `normalized`: five spellings of the same code is not five
    candidates, and a user asked to choose needs distinct options."""
    normalized = [r[0] for r in resolve.suggest("srvice", rows)]
    assert len(normalized) == len(set(normalized))


# --------------------------------------------------------------------------
# --lookup: the whole ladder, over every wording, in one call. It used to be four
# greps the agent typed itself, in an order only the prose held: run the loose one
# first and the single right row for `service` is one of nineteen. Order is code's
# job now; proposing the WORDINGS stays the agent's. What order does NOT do any
# more is suppress: a rung reached by one wording used to end the whole call.
# --------------------------------------------------------------------------


def _only(matches):
    """The single block a one-term lookup returns."""
    assert len(matches) == 1, matches
    return matches[0]


def test_lookup_takes_the_exact_row_over_the_substring_haul(rows):
    """`service` is one stored surface and nineteen substrings. The rung order is
    the whole reason this moved out of the skill's prose."""
    term, rung, shown, found = _only(resolve.lookup(["service"], rows))
    assert (term, rung, found) == ("service", "exact", 1)
    assert _cols(shown[0])["name"] == "Vehicle Service Order"


def test_lookup_leads_with_an_exact_row_whichever_wording_found_it(rows):
    """Rung before term, in the ORDER the blocks are read: the agent proposes
    wordings in the order they occur to it, and the best MATCH is not usually the
    first guess. `parts inventory` finds rows by word; `spare parts` is a row."""
    matches = resolve.lookup(["parts inventory", "spare parts"], rows)
    assert (matches[0][0], matches[0][1]) == ("spare parts", "exact")


def test_lookup_answers_every_wording_not_only_the_best_one(rows):
    """The regression this call exists to prevent. The agent is told to send every
    wording it would have tried; a call that returns the first rung ANY of them
    reaches makes the extra wordings HARM the answer. Here `in stock` is an exact
    row and `inventory` is a substring haul holding the two sibling
    classifications the user's question was actually about -- and a lookup that
    stopped at `in stock` hid them."""
    matches = resolve.lookup(["inventory", "in stock"], rows)
    answered = {term: rung for term, rung, _, _ in matches}
    assert answered == {"in stock": "exact", "inventory": "partial"}
    named = {_cols(r)["name"] for _, _, shown, _ in matches for r in shown}
    assert {"In Stock", "Inventory Vehicles", "Inventory Vehicles (Truck)"} <= named


def test_a_wording_that_matches_nothing_is_reported_beside_the_ones_that_did(rows):
    """Silence would read as "this wording was not tried"."""
    text = resolve.report(resolve.lookup(["spare parts", "zzqqxx wobble"], rows))
    assert "matched 'spare parts' — exact" in text
    assert "no match for 'zzqqxx wobble'" in text


def test_the_typo_rung_stays_a_whole_call_fallback(rows):
    """Per term it would fire on every hedge word the agent invented, and the
    caller is INSTRUCTED to act on a `CONFIRM` line. `status` is nobody's term
    here and its nearest neighbour is `Task`; offering it beside a real match is
    an invitation to answer about tasks."""
    text = resolve.report(resolve.lookup(["spare parts", "status"], rows))
    assert "CONFIRM" not in text
    assert "no match for 'status'" in text


def test_the_typo_rung_still_fires_when_no_wording_reached_a_rung(rows):
    """`sapre parts` has one real word in it, and the rows `parts` alone pulls are
    every Parts status -- noise, without the `Spare Parts` the user meant. A word
    that matches nothing drops the whole term to the typo rung."""
    _, rung, shown, _ = _only(resolve.lookup(["sapre parts"], rows))
    assert rung == "near"
    assert "SpareParts" in {_cols(r)["code"] for r in shown}


def test_lookup_reads_the_table_backwards_from_an_id(rows):
    """A `JobState` arrives as a bare ObjectId, which is nobody's surface string.
    Without this rung the skill had to send the agent back to a raw grep."""
    _, rung, shown, _ = _only(resolve.lookup(["6530d9a89c098a15dc784be6"], rows))
    assert rung == "code or id"
    assert {_cols(r)["name"] for r in shown} == {"Closed"}


def test_lookup_caps_a_broad_rung_and_says_what_it_held_back():
    """Every row printed is re-read on every later turn of the session, so a word
    matching half the table returns a page of it, not the table."""
    many = [(f"widget {n}", f"Widget {n}") + ("",) * 10 for n in range(30)]
    matches = resolve.lookup(["widget"], many)
    _, rung, shown, found = _only(matches)
    assert (rung, len(shown), found) == ("partial", resolve.LOOSE_LIMIT, 30)
    assert "showing 20 of 30" in resolve.report(matches)


def test_a_row_two_wordings_both_found_is_printed_once():
    """Overlap between wordings is the NORMAL case -- the agent sends synonyms --
    so paying for the same row twice is what would make hedging expensive."""
    many = [(f"widget {n}", f"Widget {n}") + ("",) * 10 for n in range(5)]
    text = resolve.report(resolve.lookup(["widget", "widget 1"], many))
    assert text.count("Widget 1") == 1
    assert "already above" in text


def test_the_whole_call_has_a_row_ceiling_on_top_of_the_per_term_cap():
    """Eight broad wordings at the per-term cap would be 160 rows in a
    conversation that re-reads them every later turn. Blocks are emitted
    best-rung-first, so a ceiling only ever cuts the loosest end."""
    many = [
        (f"w{group} {n}", f"W{group} {n}") + ("",) * 10 for group in range(6) for n in range(30)
    ]
    text = resolve.report(resolve.lookup([f"w{group}" for group in range(6)], many))
    printed = sum(1 for line in text.splitlines() if line.startswith("w"))
    assert printed == resolve.TOTAL_LIMIT
    assert "held back, the call is full" in text


def test_lookup_that_matches_nothing_says_to_ask(rows):
    """The dead-end rung, in the one line the agent acts on: no row, no candidate,
    no code to improvise with."""
    matches = resolve.lookup(["zzqqxx wobble"], rows)
    _, rung, shown, _ = _only(matches)
    assert (rung, shown) == ("", [])
    assert resolve.report(matches) == (
        "no match for 'zzqqxx wobble' — ask the user what they meant"
    )


def test_report_leads_with_the_column_legend(rows):
    """The rows are tab-separated columns the agent has to read by name, so the
    legend comes before the first block rather than inside each one."""
    lines = resolve.report(resolve.lookup(["spare parts"], rows)).splitlines()
    assert lines[0] == "\t".join(resolve.COLUMNS)
    assert lines[1].startswith("matched 'spare parts' — exact")


def test_lookup_finds_hebrew_typed_without_niqqud(rows):
    """The miss the phrasebook exists for, end to end through the one command."""
    _, rung, shown, _ = _only(resolve.lookup(["חלפים"], rows))
    assert rung == "exact"
    assert {_cols(r)["code"] for r in shown} == {"SpareParts"}


# --------------------------------------------------------------------------
# --list: the bucket list a breakdown loops over. `--lookup` answers what a word
# MEANS; this answers what the values ARE. Without it a live session invented
# status names, looked them up one guess at a time, and still never reached
# `99 Disabled` -- three round trips to not have the list.
# --------------------------------------------------------------------------


def test_list_enumerates_every_bucket_a_guess_would_have_to_reach(rows):
    """The failure this verb exists for, on the exact question that hit it: a
    vehicle-status breakdown is thirteen buckets and `99 Disabled` is the one a
    session counting 01..12 never sees."""
    found = resolve.buckets({"kind": "status", "entity": "Vehicle"}, rows)
    codes = [_cols(row)["code"] for row in found]
    assert len(found) == 13
    assert codes == sorted(codes), "a loop is written in code order"
    assert ("99", "Disabled") in {(_cols(r)["code"], _cols(r)["name"]) for r in found}


def test_list_keeps_two_names_under_one_code_as_two_buckets(rows):
    """Vehicle `02` is both `On The Way` and `Available For Sale`. Collapsing by
    code would send ONE call and report their sum as one bucket."""
    found = resolve.buckets({"kind": "status", "entity": "Vehicle"}, rows)
    assert [_cols(r)["name"].strip() for r in found if _cols(r)["code"] == "02"] == [
        "Available For Sale",
        "On The Way",
    ]


def test_list_collapses_the_many_surfaces_of_one_record(rows):
    """The table is one row per surface string -- a code, a name and four aliases
    are six rows -- and JobCard status `01 New` is carried by eleven
    classifications. A loop wants one call for it, not sixty."""
    every_row = [
        r for r in rows if _cols(r)["kind"] == "status" and _cols(r)["entity"] == "JobCard"
    ]
    found = resolve.buckets({"kind": "status", "entity": "JobCard"}, rows)
    assert len(every_row) > 100 and len(found) == 21
    assert [_cols(r)["name"] for r in found].count("New") == 1


def test_list_shows_the_printable_row_of_a_record_not_an_alias(rows):
    """`Inventory Vehicles (Truck)` is reachable as `1212`, `333` and `Truck`.
    The bucket has to print as the name a planner would recognise."""
    found = resolve.buckets({"kind": "classification", "entity": "Vehicle"}, rows)
    truck = next(r for r in found if _cols(r)["code"] == "Truck")
    assert _cols(truck)["surface"] == _cols(truck)["name"] == "Inventory Vehicles (Truck)"


def test_list_rejects_a_column_the_table_does_not_have(rows):
    """A filter on a column that does not exist would match nothing, and nothing
    reads exactly like a tenant that has none of these."""
    with pytest.raises(SystemExit) as raised:
        resolve.parse_filters(["nonsense=x"])
    assert "column one of" in str(raised.value)


def test_list_says_plainly_when_a_real_filter_matches_nothing(rows):
    filters = {"kind": "status", "entity": "Account"}
    assert resolve.bucket_report(filters, resolve.buckets(filters, rows)) == (
        "no rows for kind=status entity=Account"
    )


def test_list_is_bounded_and_says_so():
    """A tenant with hundreds of classifications should not put all of them in
    the conversation, and a loop that long is not one anybody wants either."""
    many = [
        ("", f"C{n}", "name", "classification", "JobCard", "", f"C{n}", "", f"C{n}", "", "", "")
        for n in range(resolve.BUCKET_LIMIT + 40)
    ]
    filters = {"kind": "classification"}
    text = resolve.bucket_report(filters, resolve.buckets(filters, many))
    assert text.startswith(f"{resolve.BUCKET_LIMIT + 40} buckets — kind=classification (showing ")
    assert len(text.splitlines()) == resolve.BUCKET_LIMIT + 2


def test_state_ids_resolve_to_printable_names(rows):
    """A card carries `JobState` as a bare ObjectId — no Code, no Label, unlike
    `JobStatus` — so without these rows the only way to print it was the `states`
    block riding on each response. Added 2026-08-27 as an id -> name dictionary.

    A dictionary is ALL it is: state does not follow from status. Sampled the same
    day, Service/`Open` cards came back 47 In Process to 3 New, so the `state=`
    attribute on a STATUS row is the typical value, not the card's."""
    states = {c["id"]: c["name"] for c in map(_cols, rows) if c["kind"] == "state"}
    assert states == {
        "6530d9a89c098a33be3e0c78": "New",
        "6530d9a89c098a05a65b6766": "Pending",
        "6530d9a89c098a37eb4562db": "In Process",
        "6530d9a89c098a37e96ff5c8": "Has Alert",
        "6530d9a89c098a15dc784be6": "Closed",
    }


def test_a_shared_surface_is_split_by_kind(rows):
    """`Closed` is a status name AND a state name; `1` is a status code AND a state
    code. Both are legitimate, so the phrasebook keeps them and `kind` is what tells
    them apart — the skill says to read it before acting on a row."""
    kinds = {c["kind"] for c in map(_cols, rows) if c["normalized"] == "closed"}
    assert kinds == {"status", "state"}


# --------------------------------------------------------------------------
# The shipped table. The index does NOT reach the sandbox any more, so the
# render -> read_rows hop is the only path back to rows: if it is lossy,
# `--suggest` quietly degrades there and nowhere else.
# --------------------------------------------------------------------------


def test_render_round_trips_through_read_rows(rows, tmp_path):
    table = tmp_path / "phrasebook.tsv"
    table.write_text(phrasebook.render(rows), encoding="utf-8")
    assert resolve.read_rows(table) == rows


def test_render_leads_with_the_column_legend(rows):
    first = phrasebook.render(rows).splitlines()[0]
    assert first.split("\t") == list(phrasebook.COLUMNS)


def test_read_rows_does_not_return_the_legend_as_data(rows, tmp_path):
    """Parse the header as a row and `--suggest` starts proposing "normalized"."""
    table = tmp_path / "phrasebook.tsv"
    table.write_text(phrasebook.render(rows), encoding="utf-8")
    assert all(row[0] != "normalized" for row in resolve.read_rows(table))


def test_suggest_works_off_the_shipped_table(rows, tmp_path):
    """The sandbox path: a typo is recovered with no index anywhere near it."""
    table = tmp_path / "phrasebook.tsv"
    table.write_text(phrasebook.render(rows), encoding="utf-8")
    codes = {_cols(r)["code"] for r in resolve.suggest("sapre parts", resolve.read_rows(table))}
    assert "SpareParts" in codes


# --------------------------------------------------------------------------
# The one property that makes normalizer drift impossible to ship unnoticed.
# --------------------------------------------------------------------------


def test_every_shipped_row_normalizes_to_its_own_surface():
    """The table's first column IS `normalize(surface)`, checked against the
    BUNDLED bytes with the SHIPPED normalizer.

    If those two ever disagree — a changed `normalize`, a table built elsewhere,
    a stale artifact — an anchored grep misses and the term reads to the planner
    as "not in this dealership's vocabulary". Silently, which is the whole
    failure the phrasebook exists to prevent. This catches all three without
    caring which one happened.
    """
    import setup_agent

    table = dict(setup_agent.reporting_bundle())["xas-reporting/phrasebook.tsv"]
    rows = [tuple(line.split("\t")) for line in table.decode().splitlines()[1:] if line]
    assert rows, "the bundled table must not be empty"
    for normalized, surface, *_rest in rows:
        assert normalized == resolve.normalize(surface)


def test_the_builder_borrows_the_skill_normalizer_rather_than_defining_one():
    """One definition, and it lives on the side that must stand alone in the
    sandbox. A second copy here is how the two quietly diverge."""
    # Identity would only compare two module objects; what matters is that the
    # builder's normalizer is COMPILED FROM the skill's file and that the builder
    # defines none of its own.
    skill_file = REPO_ROOT / "skills" / "xas-reporting" / "resolve.py"
    assert Path(phrasebook.normalize.__code__.co_filename) == skill_file
    assert phrasebook.COLUMNS == resolve.COLUMNS

    builder_source = (REPO_ROOT / "phrasebook.py").read_text(encoding="utf-8")
    assert "def normalize" not in builder_source
    assert "COLUMNS = (" not in builder_source


def test_the_inactive_classifications_that_hold_cards_survive_into_the_bundle():
    """`VGR` and `LeaseContract` are flagged inactive in the tenant config and so
    are NOT emitted by `dump_taxonomy` — they are hand-maintained in `index.md`,
    and the next regeneration drops them exactly like the BRANCH block.

    They are there because inactive is a config flag, not an empty set: between
    them they own 39 live job cards. Without these rows a breakdown by type files
    those 39 under "a type I could not identify" — observed 2026-08-30 on a chart
    of one customer's jobs. That is a wrong answer that looks like a careful one,
    so the loss has to fail here rather than in front of a planner.
    """
    import setup_agent

    table = dict(setup_agent.reporting_bundle())["xas-reporting/phrasebook.tsv"]
    rows = [tuple(line.split("\t")) for line in table.decode().splitlines()[1:] if line]

    for code, name, route in (
        ("VGR", "Vehicle Goods Receipt", "/vehicle_planning"),
        ("LeaseContract", "Lease Contract", "/contracts"),
    ):
        hits = [r for r in rows if r[3] == "classification" and r[6] == code]
        assert hits, f"{code} is missing from the bundled phrasebook — regenerated over?"
        assert {r[8] for r in hits} == {name}
        # A route is what makes the classification linkable; an empty one reads as
        # "nothing to link" and would hide the loss rather than report it.
        assert {r[11] for r in hits} == {route}
