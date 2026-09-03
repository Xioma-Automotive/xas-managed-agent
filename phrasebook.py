"""Build the reporting lane's phrasebook: `index.md` -> `phrasebook.tsv`.

HOST-SIDE ONLY. `setup_agent.reporting_bundle()` calls this at deploy time and
ships the rendered table; the taxonomy source never reaches the sandbox and
neither does this parser. Structurally the same hop as the allocation lane's
`flatten.py` — one format in, one derived artifact out, pure code, no judgement:
same index in, byte-identical table out.

One row per SURFACE STRING — every code, name and alias becomes its own line — so
that a term the user typed is found with a single grep, and multi-word terms with
a chain of greps in any order.

`normalize` and `COLUMNS` are imported from the SKILL's `resolve.py` rather than
defined here. The skill file has to stand alone in a sandbox that cannot see this
repo, so it owns them and this borrows; one definition means the `normalized`
column a row is BUILT with and the form a query is normalized INTO cannot drift.
Pair that with rendering at bundle time and never committing the table, and a
skill version physically cannot hold a table built by a different normalizer.

    uv run python -m phrasebook                    # rebuild in place (rarely needed)
    uv run python -m phrasebook IN.md OUT.tsv
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
REPORTING_SKILL_DIR = REPO_ROOT / "skills" / "xas-reporting"
INDEX_PATH = REPORTING_SKILL_DIR / "index.md"
TABLE_PATH = REPORTING_SKILL_DIR / "phrasebook.tsv"


def _resolve_module():
    """`skills/` is not a package, so the query side is loaded by path."""
    spec = importlib.util.spec_from_file_location("resolve", REPORTING_SKILL_DIR / "resolve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_resolve = _resolve_module()
normalize = _resolve.normalize
COLUMNS = _resolve.COLUMNS


# Which app page a classification's records are listed on, so that a link to the
# filter the agent just ran can be built without the sandbox holding a route table
# of its own (`skills/xas-reporting/link.py`).
#
# The app splits JobCard classifications three ways by a pair of hardcoded enums
# (`app/src/types/tenant/classifications.ts`), NOT by anything the taxonomy
# carries, which is why the split is transcribed here rather than derived. The
# fallback matches the app's own: a classification in neither set lists on
# `/job_cards`.
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

# Entities with a list page of their own. An entity absent from here gets an empty
# route, and `link.py` refuses rather than guessing — Activities and Items have no
# read tool behind them, so a link would point at a page the agent cannot have
# counted anything on.
ENTITY_ROUTES = {"Vehicle": "/vehicles", "Account": "/accounts"}


def route_for(kind: str, entity: str, code: str) -> str:
    """The page this record's classification lists on, or "" if it has none."""
    if kind != "classification":
        return ""
    if entity == "JobCard":
        if code in VEHICLE_PLANNING:
            return "/vehicle_planning"
        return "/contracts" if code in CONTRACTS else "/job_cards"
    return ENTITY_ROUTES.get(entity, "")


# Only real records; the header legend documents the format with the same
# `key=<placeholder>` syntax and must not be parsed as data.
RECORD = re.compile(r"^(ENTITY|CLASSIFICATION|STATUS|STATE|BRANCH)\s+(.*)$")

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


def surfaces(kind: str, fields: dict[str, str]) -> list[tuple[str, str]]:
    """Every string a user might say for this record, tagged with where it came from."""
    found: list[tuple[str, str]] = []
    if kind == "entity":
        found.append((fields.get("entity", ""), "entity"))
        found.append((fields.get("businessType", ""), "businessType"))
    else:
        # A BRANCH has only a name — no code, no aliases — because the value a
        # Branch filter takes is its ObjectId, which rides in the `id` column. A
        # STATE is the same shape plus a code: it exists so that the bare ObjectId
        # in a card's `JobState` can be reverse-looked-up to a printable name.
        found.append((fields.get("code", ""), "code"))
        found.append((fields.get("name", ""), "name"))
        for alias in fields.get("aliases", "").split("|"):
            found.append((alias.strip(), "alias"))
    return [(text, role) for text, role in found if text]


def build(
    index_path: Path = INDEX_PATH, include_classifications: bool = True
) -> list[tuple[str, ...]]:
    """Every surface string in the index, as table rows.

    `include_classifications=False` drops the type rows: a prompt that carries
    the type list inline (see `classification_block`) makes them a second copy in
    front of the same model, and the shipped table is then statuses, branches,
    states and entities only. The default keeps them, because a prompt WITHOUT
    that list has nowhere else to resolve a type from.
    """
    rows: set[tuple[str, ...]] = set()
    for line in index_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_line(line)
        if not parsed:
            continue
        kind, fields = parsed
        if kind == "classification" and not include_classifications:
            continue
        entity = fields.get("entity", "")
        # An ENTITY's own code IS its entity name; a CLASSIFICATION owns itself.
        classification = fields.get("classification") or (
            fields.get("code", "") if kind == "classification" else ""
        )
        for surface, role in surfaces(kind, fields):
            rows.add(
                (
                    normalize(surface),
                    surface,
                    role,
                    kind,
                    entity,
                    classification,
                    fields.get("code", ""),
                    fields.get("id", ""),
                    fields.get("name", ""),
                    fields.get("state", ""),
                    fields.get("closed", ""),
                    route_for(kind, entity, fields.get("code", "")),
                )
            )
    return sorted(rows)


# The three entities a read tool can filter on, with the filter key each one
# takes. Activities, Items and Models have classifications too, but no tool
# behind them, so putting those in a prompt buys weight the agent can never use.
# `type` on an account also accepts customer / supplier / lid directly.
FILTERABLE = {
    "JobCard": "Job cards — filter `JobClassification`; the page is what its set link opens",
    "Vehicle": "Vehicles — filter `vehicleClassification`",
    "Account": "Accounts — filter `type`",
}


def classification_block(index_path: Path = INDEX_PATH) -> str:
    """This tenant's card / vehicle / account types, rendered for a prompt.

    Built from the same index as the table and substituted at deploy time, so a
    prompt carrying the list cannot drift from the taxonomy the way a hand-typed
    one would. Statuses, branches and states stay OUT — there are 245 of them,
    and they are what `resolve.py` is for.
    """
    groups: dict[str, list[str]] = {entity: [] for entity in FILTERABLE}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_line(line)
        if not parsed:
            continue
        kind, fields = parsed
        entity = fields.get("entity", "")
        if kind != "classification" or entity not in FILTERABLE:
            continue
        code = fields.get("code", "")
        entry = f"- `{code}` {fields.get('name', '')}"
        if entity == "JobCard":
            entry += f" — {route_for(kind, entity, code)}"
        aliases = [alias.strip() for alias in fields.get("aliases", "").split("|") if alias.strip()]
        if aliases:
            entry += "  (also: " + ", ".join(aliases) + ")"
        groups[entity].append(entry)

    blocks = []
    for entity, heading in FILTERABLE.items():
        if groups[entity]:
            blocks.append(heading + ":\n" + "\n".join(groups[entity]))
    return "\n\n".join(blocks)


def render(rows: list[tuple[str, ...]]) -> str:
    """The table as it ships: a header line, then one row per surface string."""
    body = "\n".join("\t".join(row) for row in rows)
    return "\t".join(COLUMNS) + "\n" + body + "\n"


def main() -> None:
    index_path = Path(sys.argv[1]) if len(sys.argv) > 1 else INDEX_PATH
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else TABLE_PATH
    if not index_path.is_file():
        sys.exit(f"No taxonomy index at {index_path}")
    rows = build(index_path)
    out_path.write_text(render(rows), encoding="utf-8")
    print(f"{out_path}: {len(rows)} surface strings from {index_path}")


if __name__ == "__main__":
    main()
