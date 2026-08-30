"""Turn the filter a read tool just ran into a link to the same page. Runs IN THE SANDBOX.

The planner reads a number and then wants the rows behind it. Printing them costs
~400 output tokens for twenty cards, puts a table in front of someone who has the
real one a click away, and leaves those rows in the conversation to be re-read on
every later turn. A link costs ~70 and carries the whole set, sorted, paged and
filterable.

    python link.py --route /vehicle_planning --filter '<the filter you sent>'
    python link.py --card 6813          # one job card, whatever its classification
    python link.py --vehicle 11370
    python link.py --account 6a9144209004759d555d03f1

This works at all because a list page's result set is a pure function of its query
string: the page parses `filter` / `paging` / `sort` out of the URL and sends them
to the same endpoint the read tools call. So the link is not a second query that
happens to agree — it IS the query, and the two cannot drift.

**Build it from what the tool ECHOED, never from memory of what you asked.** Every
list response carries a `source` block with the filter it actually ran; that is the
input here. A filter retyped from recollection is a different question wearing the
answer's clothes.

Two dialects, because two pages parse the URL differently:

- **Job cards** (`/job_cards`, `/vehicle_planning`, `/contracts`) forward the filter
  verbatim. Copied through untouched.
- **Vehicles and accounts** run it through an adapter first, which lowercases the
  first letter of every dotted segment and re-wraps bare values according to the
  tenant's own field types — `$like` for a string, `$in` for a select. Which of
  those you get is not knowable from here, and a `$like` where an `$in` was meant
  is a substring match reported as an exact one. The adapter has one branch that
  does no guessing: a value that is ALREADY an all-`$` operator object passes
  through with only the lowercasing applied. So this file meets it there — ucFirst
  every segment, wrap every bare value in `{"$in": [v]}` — and the round trip is
  exact by construction rather than by luck.

The trap that makes hand-writing these unsafe: **a raw `$` in the query string
returns an empty page**. Not an error, not a warning — "No results", on a filter
that matches five cars. Verified back to back: `{"%24in": [...]}` returned all
five, `{"$in": [...]}` returned none. Every vehicle and account link needs `$in`
to hit the passthrough branch above, so every one of them is one unescaped
character away from silently telling the planner there is nothing there.
"""

from __future__ import annotations

import json
import sys
import urllib.parse

# One tenant, one agent, so the host it points at is a constant here for the same
# reason the taxonomy is (DECIDE-16). A second tenant makes this a per-session
# value and moves it out of the bundle, exactly as it does for the phrasebook.
APP_BASE_URL = "https://dev.app.automotivecloud.net"

# What the planner should land on: page one, and enough rows to be a list rather
# than a keyhole. Deliberately NOT the paging the tool ran — a count question asks
# for `count: 1` because it only reads the total, and handing a human a one-row
# page would be a link that contradicts the number printed above it.
LINK_PAGE_SIZE = 20

# The pages whose URL filter reaches the backend untouched. Everything else goes
# through the adapter described in the module docstring.
VERBATIM_ROUTES = ("/job_cards", "/vehicle_planning", "/contracts")

# Detail pages. The job-card one takes `DMSJCEntry` whatever the classification —
# there is no `/vehicle_planning/<id>`. The account one takes the account's `Id`,
# NOT its `Code`: the two both look like identifiers and only one routes.
CARD_DETAIL = "/job_cards"
VEHICLE_DETAIL = "/vehicles"
ACCOUNT_DETAIL = "/accounts"

# Filters that mean "whoever is asking". The agent asks as the integration login;
# the person clicking is someone else, so these two silently re-scope the page to
# a different set than the one that produced the number. There is no encoding fix
# for that — the value has to be resolved to explicit ids before it can be linked.
VIEWER_RELATIVE_KEYS = ("MyJobCards", "Branch")


def is_operator_object(value: object) -> bool:
    """A dict whose every key starts with `$` — the adapter's passthrough test.

    Mirrors `isMongoOperatorObject` in the app: a non-empty plain object, all of
    whose keys are operators. An empty dict is NOT one, and neither is a list.
    """
    if not isinstance(value, dict) or not value:
        return False
    return all(key.startswith("$") for key in value)


def uc_first(text: str) -> str:
    """Upper-case the first character, leave the rest alone.

    The inverse of the adapter's own `lcFirst`, applied per dotted segment, so
    `make.code` and `Make.Code` are the same field either side of the hop. Not
    `.title()` or `.capitalize()`, both of which would also lower-case the tail
    and turn `DMSJCEntry` into `Dmsjcentry`.
    """
    return text[:1].upper() + text[1:]


def adapt_for_core(filter_obj: dict) -> dict:
    """A vehicles/accounts filter in the form the PAGE takes.

    ucFirst each dotted segment and wrap every bare value in `{"$in": [...]}` so
    the adapter's passthrough branch fires and hands the backend exactly what was
    passed in here. `$and` / `$or` keep their own name and their contents are left
    ALONE — the adapter never descends into them, so what is written inside is
    what the backend receives, camelCase and all.
    """
    adapted: dict = {}
    for key, value in filter_obj.items():
        if key.startswith("$"):
            adapted[key] = value
            continue
        name = ".".join(uc_first(part) for part in key.split("."))
        adapted[name] = value if is_operator_object(value) else {"$in": _as_list(value)}
    return adapted


def _as_list(value: object) -> list:
    return list(value) if isinstance(value, list) else [value]


def viewer_relative(filter_obj: dict) -> str | None:
    """The first key that would re-scope the page to whoever opens it, if any.

    `{"Branch": true}` is the live one: it resolves to the branch of the logged-in
    user, which for the agent is the integration account and for the planner is
    theirs. A branch named by its id is fine and stays fine — only the boolean is
    a problem, so this checks the VALUE and not just the key.
    """
    for key in VIEWER_RELATIVE_KEYS:
        if filter_obj.get(key) is True:
            return key
    return None


def compact(obj: object) -> str:
    """JSON the way the app writes it: no spaces, so the URL stays readable."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def list_url(route: str, filter_obj: dict, sort: dict | None = None) -> str:
    """A link to the list page showing exactly `filter_obj`.

    `paging` is this file's own, not the caller's (see LINK_PAGE_SIZE). `kpi` is
    always empty: a KPI id makes the page a saved view, which is a different
    question than the one just answered. The four parameter names are the only
    ones the page reads — `page` and `pageSize` as separate params are parsed by
    nothing, so a link built with those silently falls back to the defaults.
    """
    payload = filter_obj if route in VERBATIM_ROUTES else adapt_for_core(filter_obj)
    query = urllib.parse.urlencode(
        {
            "paging": compact({"page": 1, "count": LINK_PAGE_SIZE}),
            "filter": compact(payload),
            "sort": compact(sort or {}),
            "kpi": "",
        },
        # quote_via, not the default quote_plus: a space must become %20 and not
        # `+`, which the page would read back as a literal plus. `safe=""` is what
        # escapes `$`, and that one is the difference between the right rows and
        # an empty page.
        quote_via=urllib.parse.quote,
        safe="",
    )
    return f"{APP_BASE_URL}{route}?{query}"


def detail_url(page: str, key: str) -> str:
    return f"{APP_BASE_URL}{page}/{urllib.parse.quote(str(key), safe='')}"


def parse_url(url: str) -> dict:
    """The filter a link carries, back as an object. Used by the tests to prove the
    round trip, and by anyone checking a link by hand rather than by clicking it."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query, keep_blank_values=True)
    return json.loads(query["filter"][0])


USAGE = """usage:
  link.py --route /job_cards --filter '<json>' [--sort '<json>']
  link.py --card <DMSJCEntry> | --vehicle <VehicleCode> | --account <Id>

--route comes from the phrasebook's `route` column for the classification in play;
this file holds no table of its own. --filter is the `source.filter` the read tool
echoed back, not a filter retyped from memory."""


def main() -> None:
    args = sys.argv[1:]
    flags = dict(zip(args[::2], args[1::2]))
    if len(args) % 2 or not flags:
        sys.exit(USAGE)

    for flag, page in (
        ("--card", CARD_DETAIL),
        ("--vehicle", VEHICLE_DETAIL),
        ("--account", ACCOUNT_DETAIL),
    ):
        if flag in flags:
            print(detail_url(page, flags[flag]))
            return

    if "--route" not in flags or "--filter" not in flags:
        sys.exit(USAGE)

    route = flags["--route"]
    if not route.startswith("/"):
        sys.exit(f"--route must be a page path such as /job_cards, not {route!r}")

    try:
        filter_obj = json.loads(flags["--filter"])
        sort = json.loads(flags.get("--sort", "{}"))
    except json.JSONDecodeError as err:
        sys.exit(f"--filter/--sort must be JSON: {err}")
    if not isinstance(filter_obj, dict):
        sys.exit("--filter must be a JSON object")

    relative = viewer_relative(filter_obj)
    if relative:
        sys.exit(
            f"cannot link a filter containing {relative!r}: it resolves to whoever is "
            "logged in, so the page would show the planner a different set than the "
            "one counted. Re-run the query with explicit ids, then link that."
        )

    print(list_url(route, filter_obj, sort))


if __name__ == "__main__":
    main()
