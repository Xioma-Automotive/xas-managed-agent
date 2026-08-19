"""`phrasebook` is the pure taxonomy->lookup-table hop for the XAS Q&A agent.

The point of it is that term -> system code stays deterministic code instead of
being re-derived by the model each turn. These tests pin what that buys: Hebrew
typed without niqqud matches the stored form; the index's mixed quoting is parsed
correctly (booleans are unquoted and a naive regex drops them); the header legend
is not mistaken for data; every alias becomes its own searchable row; and the same
index yields a byte-identical phrasebook.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "skills" / "xas-qa" / "index.md"

_spec = importlib.util.spec_from_file_location(
    "phrasebook", REPO_ROOT / "skills" / "xas-qa" / "phrasebook.py"
)
phrasebook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phrasebook)


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
    """code="Service" is named "Distinct_name" — the trap the agent must not infer around."""
    by_surface = {
        _cols(r)["surface"]: _cols(r) for r in rows if _cols(r)["kind"] == "classification"
    }
    assert by_surface["Service"]["name"] == "Distinct_name"
    assert by_surface["Distinct_name"]["code"] == "Service"


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


def test_build_is_deterministic():
    assert phrasebook.build(INDEX) == phrasebook.build(INDEX)
