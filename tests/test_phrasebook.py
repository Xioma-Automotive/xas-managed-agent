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
# --suggest: the typo rung of the ladder. Exact and substring search both need
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
