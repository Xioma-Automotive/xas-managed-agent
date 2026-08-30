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


def build(index_path: Path = INDEX_PATH) -> list[tuple[str, ...]]:
    rows: set[tuple[str, ...]] = set()
    for line in index_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_line(line)
        if not parsed:
            continue
        kind, fields = parsed
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
                )
            )
    return sorted(rows)


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
