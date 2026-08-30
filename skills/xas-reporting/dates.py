"""Resolve the period words a planner says into a CreateDateTime filter.

"Last week" is not a fact about the clock, it is a convention — and re-deriving
that convention costs a turn of week-boundary arithmetic every time it is asked
for. This is the convention as code: same phrase in, same bounds out.

    python dates.py "last week"
    python dates.py "last 30 days"

Three things it settles, each of which was being decided turn by turn:

- **The dealership's clock is UTC+3 and the filter compares in UTC.** A day runs
  from LOCAL midnight, so "last week" opens at 21:00Z the evening before. Bound it
  at UTC midnight instead and three hours of cards land in the wrong week.
- **The week starts MONDAY.**
- **The range is half-open, `[start, end)`.** Verified against the live system: a
  card stamped exactly `end` is excluded, one stamped exactly `start` is included.

A phrase it cannot read is an error, never a guess — a plausible wrong range
returns a real-looking number nobody can tell is wrong.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

# The dealership's own clock. Job cards come back stamped +03:00; every bound
# below is built as local midnight and converted, never taken as UTC midnight.
TENANT_TZ = timezone(timedelta(hours=3))

# Monday. `date.weekday()` is already Monday=0, so this is the offset subtracted
# to reach the start of the week a given day falls in.
WEEK_START = 0

LAST_N_DAYS = re.compile(r"^last (\d+) days?$")


def today(now: datetime | None = None) -> date:
    """The dealership's date, which near midnight is not UTC's."""
    return (now or datetime.now(TENANT_TZ)).astimezone(TENANT_TZ).date()


def week_start(day: date) -> date:
    return day - timedelta(days=(day.weekday() - WEEK_START) % 7)


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(day: date) -> date:
    """The 1st of the following month. 32 days clears every month length."""
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def resolve(phrase: str, day: date) -> tuple[date, date, str] | None:
    """Half-open `[start, end)` in LOCAL dates, with what to call the span.

    None means the phrase is not one we resolve — the caller must ask, not guess.
    """
    text = " ".join(phrase.lower().split())
    if text == "today":
        return day, day + timedelta(days=1), "today"
    if text == "yesterday":
        return day - timedelta(days=1), day, "yesterday"
    if text == "this week":
        start = week_start(day)
        return start, start + timedelta(days=7), "this week"
    if text == "last week":
        start = week_start(day) - timedelta(days=7)
        return start, start + timedelta(days=7), "last week"
    if text == "this month":
        start = month_start(day)
        return start, next_month(start), "this month"
    if text == "last month":
        end = month_start(day)
        return month_start(end - timedelta(days=1)), end, "last month"
    if text == "this year":
        return date(day.year, 1, 1), date(day.year + 1, 1, 1), "this year"
    if text == "last year":
        return date(day.year - 1, 1, 1), date(day.year, 1, 1), "last year"
    match = LAST_N_DAYS.match(text)
    if match:
        # N days ENDING TODAY, today included — so "last 7 days" spans 7 days,
        # not 8. The window a planner means when they say it out loud.
        count = int(match.group(1))
        if count < 1:
            return None
        return day - timedelta(days=count - 1), day + timedelta(days=1), f"the last {count} days"
    return None


def as_utc(day: date) -> str:
    """Local midnight on `day`, written the way the filter wants it."""
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=TENANT_TZ)
    return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit('usage: dates.py "last week"')
    resolved = resolve(sys.argv[1], today())
    if resolved is None:
        sys.exit(
            f"cannot read {sys.argv[1]!r} as a period — ask which dates they mean; do not guess"
        )
    start, end, label = resolved
    last = end - timedelta(days=1)
    span = f"{start:%a %d %b %Y}" if start == last else f"{start:%a %d %b %Y} to {last:%a %d %b %Y}"
    print(json.dumps({"start": as_utc(start), "end": as_utc(end)}))
    print(f"{label} = {span}, dealership time")


if __name__ == "__main__":
    main()
