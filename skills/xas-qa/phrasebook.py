"""Flatten this skill's taxonomy index into a normalized, greppable phrasebook.

One row per SURFACE STRING — every code, name and alias becomes its own line —
so that a term the user typed can be found with a single grep, and multi-word
terms with a chain of greps in any order. The `normalized` column is casefolded
and stripped of combining marks, which is what makes Hebrew typed without niqqud
(חלפים) match the index's stored form (חֲלָפִים).

Pure code, no judgement: same index in, byte-identical phrasebook out. Run once
per session, then grep the result.

    python phrasebook.py                     # index.md (beside this file) -> /workspace/phrasebook.tsv
    python phrasebook.py IN.md OUT.tsv
    python phrasebook.py --normalize "חֲלָפִים"   # normalize a query the same way
    python phrasebook.py --suggest "sapre parts" # closest real entries to a term that missed
"""

from __future__ import annotations

import difflib
import re
import sys
import unicodedata
from pathlib import Path

# The taxonomy ships in this skill bundle, right beside this file, so it is found
# relative to __file__ — the only thing that knows where the platform unpacked
# the skill. It used to be a per-session mount at /workspace/reports/index.md,
# which meant guessing between that path and /mnt/session/uploads/...; shipping
# the two together removes the guess. One tenant only: see DECIDE-16.
INDEX_PATH = Path(__file__).resolve().parent / "index.md"
DEFAULT_OUT = Path("/workspace/phrasebook.tsv")


def default_index() -> Path | None:
    """The taxonomy shipped beside this file, or None if it is missing."""
    return INDEX_PATH if INDEX_PATH.is_file() else None


# Only real records; the header legend documents the format with the same
# `key=<placeholder>` syntax and must not be parsed as data.
RECORD = re.compile(r"^(ENTITY|CLASSIFICATION|STATUS)\s+(.*)$")

# Strings are quoted, booleans and counts are not — match both forms.
FIELD = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')

COLUMNS = (
    "normalized",
    "surface",
    "role",
    "kind",
    "entity",
    "classification",
    "code",
    "id",
    "name",
    "state",
    "closed",
)


def normalize(text: str) -> str:
    """Casefold, strip combining marks (Hebrew niqqud, Latin diacritics), collapse space."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(bare.casefold().split())


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
        found.append((fields.get("code", ""), "code"))
        found.append((fields.get("name", ""), "name"))
        for alias in fields.get("aliases", "").split("|"):
            found.append((alias.strip(), "alias"))
    return [(text, role) for text, role in found if text]


def build(index_path: Path) -> list[tuple[str, ...]]:
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


# A misspelling is not a synonym problem: the letters are wrong, so neither the
# anchored match nor a substring search can reach the row. difflib closes that
# gap deterministically and offline. 0.6 is difflib's own default cutoff, kept so
# the suggestions stay tight — a loose cutoff turns "did you mean" into noise.
SUGGEST_CUTOFF = 0.6
SUGGEST_LIMIT = 5


def suggest(
    term: str, rows: list[tuple[str, ...]], limit: int = SUGGEST_LIMIT
) -> list[tuple[str, ...]]:
    """Rows whose normalized surface most nearly spells ``term``.

    For a term that matched NOTHING — a typo, a truncation. The caller shows
    these as candidates for the user to confirm; picking one automatically is
    how a plausible wrong code becomes a confident wrong number.
    """
    by_normalized: dict[str, tuple[str, ...]] = {}
    for row in rows:
        by_normalized.setdefault(row[0], row)
    close = difflib.get_close_matches(
        normalize(term), sorted(by_normalized), n=limit, cutoff=SUGGEST_CUTOFF
    )
    return [by_normalized[hit] for hit in close]


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--normalize":
        print(normalize(sys.argv[2]))
        return

    if len(sys.argv) > 2 and sys.argv[1] == "--suggest":
        index_path = default_index()
        if index_path is None:
            sys.exit(f"No taxonomy index at {INDEX_PATH} (it ships in this skill)")
        rows = suggest(sys.argv[2], build(index_path))
        if not rows:
            print(f"no near match for {sys.argv[2]!r} — ask the user what they meant")
            return
        print("\t".join(COLUMNS))
        for row in rows:
            print("\t".join(row))
        return

    index_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_index()
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    if index_path is None:
        sys.exit(f"No taxonomy index at {INDEX_PATH} (it ships in this skill)")
    if not index_path.is_file():
        sys.exit(f"No taxonomy index at {index_path}")

    rows = build(index_path)
    body = "\n".join("\t".join(row) for row in rows)
    out_path.write_text("\t".join(COLUMNS) + "\n" + body + "\n", encoding="utf-8")
    print(f"{out_path}: {len(rows)} surface strings from {index_path}")


if __name__ == "__main__":
    main()
