"""`dates.py` is the period-word convention as code, for the reporting lane.

It exists because the model was re-deriving it every turn: two bash calls and
~20s of week-boundary arithmetic to conclude "last week = 17-23 Aug", at UTC
midnight, for a dealership whose day starts three hours earlier. These tests pin
the three things that were being decided per turn — the tenant's clock, where the
week starts, and which end of the range is open — plus the refusal, which is the
only alternative to a plausible wrong range.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATES_PY = REPO_ROOT / "skills" / "xas-reporting" / "dates.py"

_spec = importlib.util.spec_from_file_location("dates", DATES_PY)
dates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dates)

# A Sunday, which is the day the trace was taken and the one that makes the
# Monday-start rule visible: it belongs to the week that OPENED six days earlier.
SUNDAY = date(2026, 8, 30)


def test_last_week_is_the_previous_monday_to_sunday():
    start, end, label = dates.resolve("last week", SUNDAY)
    assert (start, end) == (date(2026, 8, 17), date(2026, 8, 24))
    assert label == "last week"


def test_a_sunday_belongs_to_the_week_that_opened_on_monday():
    """Monday start: 30 Aug is a Sunday, so "this week" opened on the 24th, not
    on the 30th. Get this backwards and "last week" slides a day."""
    start, _end, _label = dates.resolve("this week", SUNDAY)
    assert start == date(2026, 8, 24)


def test_bounds_are_local_midnight_expressed_in_utc():
    """The dealership runs UTC+3 and the filter compares in UTC, so a day opens
    at 21:00Z the evening before. Bound it at UTC midnight and three hours of
    cards land in the wrong period."""
    assert dates.as_utc(date(2026, 8, 17)) == "2026-08-16T21:00:00Z"


def test_the_range_is_half_open():
    """Verified against the live system: a card stamped exactly `end` is
    excluded, one stamped exactly `start` is included. So consecutive periods
    share a boundary instant and nothing is counted twice."""
    _s1, e1, _ = dates.resolve("last week", SUNDAY)
    s2, _e2, _ = dates.resolve("this week", SUNDAY)
    assert e1 == s2
    assert dates.as_utc(e1) == dates.as_utc(s2)


def test_last_month_spans_the_whole_previous_month():
    start, end, _ = dates.resolve("last month", SUNDAY)
    assert (start, end) == (date(2026, 7, 1), date(2026, 8, 1))


def test_last_month_crosses_a_year_boundary():
    start, end, _ = dates.resolve("last month", date(2026, 1, 14))
    assert (start, end) == (date(2025, 12, 1), date(2026, 1, 1))


def test_last_n_days_counts_today_as_one_of_them():
    """ "Last 7 days" spans seven days, not eight."""
    start, end, label = dates.resolve("last 7 days", SUNDAY)
    assert (end - start).days == 7
    assert end == date(2026, 8, 31)
    assert label == "the last 7 days"


def test_today_and_yesterday_are_single_days():
    assert dates.resolve("today", SUNDAY)[:2] == (SUNDAY, date(2026, 8, 31))
    assert dates.resolve("yesterday", SUNDAY)[:2] == (date(2026, 8, 29), SUNDAY)


def test_wording_is_normalized_not_matched_literally():
    assert dates.resolve("  LAST   Week ", SUNDAY) == dates.resolve("last week", SUNDAY)


def test_a_phrase_it_cannot_read_resolves_to_nothing():
    """The whole point: no fallback range. A plausible wrong period returns a
    real-looking number the planner cannot tell is wrong."""
    for phrase in ("next tuesday", "since the launch", "Q3", "last 0 days", ""):
        assert dates.resolve(phrase, SUNDAY) is None


def test_cli_prints_the_filter_then_the_span_in_words():
    """Two lines: the first is pasted into the filter, the second is what the
    planner is told the count covers. The VALUES move with the clock and are
    pinned above through `resolve`; this pins the shape the skill relies on."""
    out = subprocess.run(
        [sys.executable, str(DATES_PY), "last week"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    bounds = json.loads(out[0])
    assert sorted(bounds) == ["end", "start"]
    assert all(value.endswith("21:00:00Z") for value in bounds.values()), (
        "local midnight in a UTC+3 tenant, not UTC midnight"
    )
    assert out[1].startswith("last week = ")
    assert out[1].endswith(", dealership time")


def test_cli_refuses_a_phrase_it_cannot_read():
    done = subprocess.run(
        [sys.executable, str(DATES_PY), "whenever"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "ask which dates they mean" in done.stderr
