"""Turn the filter a read tool just ran into a link to the same page. Runs IN THE SANDBOX.

The planner reads a number and then wants the rows behind it. Printing them costs
~400 output tokens for twenty cards, puts a table in front of someone who has the
real one a click away, and leaves those rows in the conversation to be re-read on
every later turn. A link costs ~70 and carries the whole set, sorted, paged and
filterable.

    python link.py --tool get_vehicle_list --filter '<the filter you are sending>'
    python link.py --route /vehicle_planning --filter '<the filter you are sending>'

A DETAIL page — one card, one car, one account — is a path and an id with no filter
to encode, so the agent writes those inline and this file is not involved.

This works at all because a list page's result set is a pure function of its query
string: the page parses `filter` / `paging` / `sort` out of the URL and sends them
to the same endpoint the read tools call. So the link is not a second query that
happens to agree — it IS the query, and the two cannot drift.

**Write the filter ONCE, into the tool call and this command together.** That is
what makes the two agree — not care in copying them. Both arguments are things you
hold before the call returns, so the pair goes out in one block and the link costs
no round trip of its own. (If you ever do build one after the fact, take the filter
from the `source` block the response echoed, never from memory of what you asked.)

**The page is a fact about the call, not a lookup.** Two of the three list tools
have exactly one page — `--tool` names it. Job cards are the one lane where the
page depends on the classification, and that mapping lives in the phrasebook,
never here.

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

# Every link is RELATIVE: the planner reads the answer inside the app, so a path
# resolves against whatever host they are already on — dev, staging or a tenant's
# own — and there is no host here to go stale or to leak one tenant's into
# another's answer. It also matches the links the agent writes by hand for a single
# record, which have no host to know. Set this to an origin only if the answers are
# ever rendered somewhere other than the app itself.
APP_BASE_URL = ""

# What the planner should land on: page one, and enough rows to be a list rather
# than a keyhole. Deliberately NOT the paging the tool ran — a count question asks
# for `count: 1` because it only reads the total, and handing a human a one-row
# page would be a link that contradicts the number printed above it.
LINK_PAGE_SIZE = 20

# The pages whose URL filter reaches the backend untouched. Everything else goes
# through the adapter described in the module docstring.
VERBATIM_ROUTES = ("/job_cards", "/vehicle_planning", "/contracts")

# The read tools whose records all list on ONE page, so the page is a fact about
# the call and not something to look up. `get_job_list` is deliberately absent:
# job cards list on three pages depending on classification, which is what the
# phrasebook's `route` column is for. Confirmed 2026-08-31 against the endpoints
# the tools echo — `get_account_list` reads `/api/coreApi/customers`, so this is
# transcribed from live responses rather than derived from the tool's name.
TOOL_ROUTES = {"get_vehicle_list": "/vehicles", "get_account_list": "/accounts"}

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


def parse_url(url: str) -> dict:
    """The filter a link carries, back as an object. Used by the tests to prove the
    round trip, and by anyone checking a link by hand rather than by clicking it."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query, keep_blank_values=True)
    return json.loads(query["filter"][0])


USAGE = """usage:
  link.py --tool get_vehicle_list --filter '<json>'
  link.py --route /job_cards --filter '<json>' [--sort '<json>']

Set links only. A DETAIL page is a path and an id (`/job_cards/<DMSJCEntry>`,
`/vehicles/<VehicleCode>`, `/accounts/<Id>`) with no filter to encode, so the
agent writes those inline and this file is not involved.

--tool is the read tool you are calling, for the tools whose records all list on
one page. --route is for job cards, which do not: it comes from the phrasebook's
`route` column for the classification in play, and this file holds no table of
its own. --filter is the filter you are sending, written once into both."""


def main() -> None:
    args = sys.argv[1:]
    flags = dict(zip(args[::2], args[1::2]))
    if len(args) % 2 or not flags:
        sys.exit(USAGE)

    if ("--route" in flags) == ("--tool" in flags) or "--filter" not in flags:
        sys.exit(USAGE)

    if "--tool" in flags:
        tool = flags["--tool"]
        if tool not in TOOL_ROUTES:
            sys.exit(
                f"--tool does not fix a page for {tool!r}. Job cards list on three "
                "different pages depending on classification, so pass --route from the "
                "phrasebook's `route` column instead."
            )
        route = TOOL_ROUTES[tool]
    else:
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
