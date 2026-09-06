"""Render the reporting lane's vocabulary: `index.md` -> the blocks the skill ships.

HOST-SIDE ONLY. `setup_agent.reporting_bundle()` calls this at deploy time and
substitutes the blocks into the reporting SKILL.md; the taxonomy source never
reaches the sandbox and neither does this parser. Structurally the same hop as
the allocation lane's `flatten.py` — one format in, one derived artifact out,
pure code, no judgement: same index in, byte-identical blocks out.

There is NO lookup any more (2026-09-06). Everything a lookup could ever have
answered is 46 records — 21 job-card statuses, 13 vehicle statuses, 5 lifecycle
states, 7 branches — and that is 1,051 tokens written out in full, against 489
for ONE `--lookup` call answering three wordings and 1,070 for the single
`--list` call a breakdown needed. The tool cost more than the data every time it
ran, and it ran on a round trip of its own. So the vocabulary is simply IN the
skill, read where the procedure is already read, and a classification cannot be
looked up because nothing can look anything up.

    uv run python -m phrasebook            # both blocks, to eyeball a change
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
REPORTING_SKILL_DIR = REPO_ROOT / "skills" / "xas-reporting"
INDEX_PATH = REPORTING_SKILL_DIR / "index.md"


# The app splits JobCard classifications three ways by a pair of hardcoded enums
# (`app/src/types/tenant/classifications.ts`), NOT by anything the taxonomy
# carries, which is why the split is transcribed here rather than derived. It
# groups the type list under the business areas a planner asks for by NAME
# ("vehicle sales", "sales cards"); the app PAGE it used to target went with link
# building on 2026-09-03. The fallback matches the app's own.
VEHICLE_PLANNING = frozenset(
    (
        "VPR",
        "VPO",
        "VGR",
        "VSR",
        "VST",
        "VRV",
        "VAD",
        "VRT",
        "VRS",
        "VSO",
        "VDN",
        "VSI",
        "VDR",
        "VIC",
    )
)
CONTRACTS = frozenset(
    (
        "Contract",
        "BlanketAgreement",
        "LeaseContract",
        "RentContract",
        "MaintenanceContract",
        "RentalContractQuote",
        "LeaseContractQuote",
        "Reservation",
    )
)


def route_for(kind: str, entity: str, code: str) -> str:
    """Which business area a job-card type belongs to, or "" if it has none."""
    if kind != "classification" or entity != "JobCard":
        return ""
    if code in VEHICLE_PLANNING:
        return "/vehicle_planning"
    return "/contracts" if code in CONTRACTS else "/job_cards"


# Only real records; the header legend documents the format with the same
# `key=<placeholder>` syntax and must not be parsed as data.
RECORD = re.compile(r"^(ENTITY|CLASSIFICATION|STATUS|STATE|BRANCH|GROUP)\s+(.*)$")

# Strings are quoted, booleans and counts are not — match both forms.
FIELD = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')


def parse_line(line: str) -> tuple[str, dict[str, str]] | None:
    match = RECORD.match(line)
    if not match:
        return None
    kind, rest = match.group(1), match.group(2)
    fields: dict[str, str] = {}
    for field in FIELD.finditer(rest):
        # group(2) is None when the value was unquoted; "" when it was `key=""`.
        quoted, bare = field.group(2), field.group(3)
        fields[field.group(1)] = quoted if quoted is not None else bare
    return kind.lower(), fields


def records(index_path: Path = INDEX_PATH) -> list[tuple[str, dict[str, str]]]:
    lines = index_path.read_text(encoding="utf-8").splitlines()
    return [parsed for parsed in map(parse_line, lines) if parsed]


def distinct(
    parsed: list[tuple[str, dict[str, str]]], kind: str, entity: str | None = None
) -> list[dict[str, str]]:
    """One entry per RECORD, in declaration order.

    The index lists a status once per classification that carries it, so the 96
    JobCard status lines are 21 records. Identity is (code, id, name), which is
    also what keeps vehicle `02` as TWO records: `On The Way` and
    `Available For Sale ` share a code, and collapsing them would hide the split
    behind one number.
    """
    seen: dict[tuple[str, str, str], dict[str, str]] = {}
    for record_kind, fields in parsed:
        if record_kind != kind or (entity is not None and fields.get("entity") != entity):
            continue
        key = (fields.get("code", ""), fields.get("id", ""), fields.get("name", ""))
        seen.setdefault(key, fields)
    return list(seen.values())


# The three entities a read tool can filter on, as (what to call them, the filter
# key each one takes). Activities, Items and Models have classifications too, but
# no tool behind them, so putting those in front of the agent buys weight it can
# never use. `type` on an account also accepts customer / supplier / lid directly.
FILTERABLE = {
    "JobCard": ("Job cards", "JobClassification"),
    "Vehicle": ("Vehicles", "vehicleClassification"),
    "Account": ("Accounts", "type"),
}


def entity_heading(label: str, filter_key: str, business_type: str) -> str:
    """The line above one entity's type list.

    It says the thing the list itself cannot: the entity's OWN words are not a
    type. A session asked "how many job cards were opened this month" and spent a
    round trip resolving `job card`, because every word in the list was a type and
    nothing said what the word naming the whole set does. The words come from the
    index's ENTITY line (`entity=` / `businessType=`), generated like the rest.
    """
    words = ", ".join(f'"{word}"' for word in dict.fromkeys((label.lower(), business_type)) if word)
    return (
        f"{label} — {words} means ALL of them, whatever their type: NO `{filter_key}` "
        f"filter at all. Only a TYPE below goes in that filter:"
    )


def classification_block(index_path: Path = INDEX_PATH) -> str:
    """This tenant's card / vehicle / account TYPES.

    Generated from the same index as everything else, so the list the agent reads
    cannot drift from the taxonomy the way a hand-typed one would. Job-card types
    are grouped under the index's GROUP names (vehicle service / vehicle sales /
    contracts): a planner asks for a whole area by its name, and the heading is
    what says that means every type beneath it.
    """
    parsed = records(index_path)
    group_names = {fields["route"]: fields["name"] for kind, fields in parsed if kind == "group"}
    business_types = {
        fields.get("entity", ""): fields.get("businessType", "")
        for kind, fields in parsed
        if kind == "entity"
    }

    entries: dict[str, dict[str, list[str]]] = {entity: {} for entity in FILTERABLE}
    for kind, fields in parsed:
        entity = fields.get("entity", "")
        if kind != "classification" or entity not in FILTERABLE:
            continue
        code = fields.get("code", "")
        entry = f"- `{code}` {fields.get('name', '')}"
        aliases = [alias.strip() for alias in fields.get("aliases", "").split("|") if alias.strip()]
        if aliases:
            entry += "  (also: " + ", ".join(aliases) + ")"
        # One unnamed group per entity where the index declares none; job cards
        # are the only entity whose types split by area.
        entries[entity].setdefault(route_for(kind, entity, code), []).append(entry)

    blocks = []
    for entity, (label, filter_key) in FILTERABLE.items():
        heading = entity_heading(label, filter_key, business_types.get(entity, ""))
        # Declaration order, so the reader meets the areas in the order the
        # taxonomy names them rather than the order the dump happens to list.
        routes = [route for route in group_names if route in entries[entity]]
        routes += [route for route in entries[entity] if route not in group_names]
        if len(routes) == 1 and not group_names.get(routes[0]):
            blocks.append(heading + "\n" + "\n".join(entries[entity][routes[0]]))
            continue
        block = [heading]
        for route in routes:
            block.append(f"\n{group_names[route]}:")
            block.append("\n".join(entries[entity][route]))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _status_sort(code: str) -> tuple[str, str]:
    """`01` and `1` are DIFFERENT statuses here, so they sort adjacent on purpose:
    seeing New and Open next to each other is what makes the collision visible."""
    return code.zfill(2), code


def vocabulary_block(index_path: Path = INDEX_PATH) -> str:
    """Everything else this tenant names its own way: statuses, states, branches.

    This is the whole of what `resolve.py --lookup` used to answer, and writing it
    out costs less than one call did. Every line carries the value its filter
    takes and the name to print — job-card statuses by ObjectId because the app's
    own list filter is `JobStatus.ID` (`app/src/components/Filters/Controls/
    JobStatus/index.tsx`), so a count built on anything else would disagree with
    the page its `ListUrl` opens.
    """
    parsed = records(index_path)
    out = [
        (
            "Job-card statuses — filter `JobStatus.ID` with the id, always in an array. "
            "`[bucket]` is the lifecycle state it rolls up to; CLOSED marks the two that "
            "count as closed:"
        )
    ]
    unresolved = []
    for fields in sorted(
        distinct(parsed, "status", "JobCard"), key=lambda f: _status_sort(f["code"])
    ):
        if not fields.get("name") or not fields.get("id"):
            unresolved.append(fields["code"])
            continue
        closed = " CLOSED" if fields.get("closed") == "true" else ""
        out.append(f"- {fields['name']} {fields['id']} [{fields.get('state', '')}]{closed}")
    if unresolved:
        out.append(
            f"Codes {', '.join(sorted(unresolved, key=_status_sort))} are carried by cards but "
            "missing from the "
            "dictionary: such a card has no status name and no id to filter on. Call it an "
            "unknown status, count it separately, never guess which one it was."
        )

    out += [
        "",
        (
            "Vehicle statuses — filter `status.code`. Two names can share one code, and "
            "filtering the code returns their SUM: to tell them apart filter "
            "`status.name` with `$like`:"
        ),
    ]
    for fields in sorted(
        distinct(parsed, "status", "Vehicle"), key=lambda f: _status_sort(f["code"])
    ):
        out.append(f"- {fields['name']} `{fields['code']}`")

    out += ["", "Lifecycle states — a card's `JobState` arrives as one of these ids:"]
    for fields in distinct(parsed, "state"):
        out.append(f"- {fields['name']} {fields['id']}")

    out += ["", "Branches — filter `Branch` with the id, never the name:"]
    for fields in distinct(parsed, "branch"):
        out.append(f"- {fields['name']} {fields['id']}")
    return "\n".join(out)


def main() -> None:
    index = Path(sys.argv[1]) if len(sys.argv) > 1 else INDEX_PATH
    print(classification_block(index))
    print()
    print(vocabulary_block(index))


if __name__ == "__main__":
    main()
