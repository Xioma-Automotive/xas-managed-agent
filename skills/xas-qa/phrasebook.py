"""Flatten the mounted taxonomy index into a normalized, greppable phrasebook.

One row per SURFACE STRING — every code, name and alias becomes its own line —
so that a term the user typed can be found with a single grep, and multi-word
terms with a chain of greps in any order. The `normalized` column is casefolded
and stripped of combining marks, which is what makes Hebrew typed without niqqud
(חלפים) match the index's stored form (חֲלָפִים).

Pure code, no judgement: same index in, byte-identical phrasebook out. Run once
per session, then grep the result.

    python phrasebook.py                     # /workspace/index.md -> /workspace/phrasebook.tsv
    python phrasebook.py IN.md OUT.tsv
    python phrasebook.py --normalize "חֲלָפִים"   # normalize a query the same way
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

# The host mounts the taxonomy at /workspace/reports/index.md, but the platform
# may materialize a mounted resource under /mnt/session/uploads. Resolve rather
# than assume: guessing wrong means no phrasebook and a silent fallback to
# grepping a file that isn't there.
INDEX_CANDIDATES = (
    Path("/workspace/reports/index.md"),
    Path("/mnt/session/uploads/workspace/reports/index.md"),
)
DEFAULT_OUT = Path("/workspace/phrasebook.tsv")


def default_index() -> Path | None:
    """The first taxonomy that actually exists, or None if none do."""
    return next((c for c in INDEX_CANDIDATES if c.is_file()), None)


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


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--normalize":
        print(normalize(sys.argv[2]))
        return

    index_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_index()
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    if index_path is None:
        sys.exit(f"No taxonomy index found in {[str(c) for c in INDEX_CANDIDATES]}")
    if not index_path.is_file():
        sys.exit(f"No taxonomy index at {index_path}")

    rows = build(index_path)
    body = "\n".join("\t".join(row) for row in rows)
    out_path.write_text("\t".join(COLUMNS) + "\n" + body + "\n", encoding="utf-8")
    print(f"{out_path}: {len(rows)} surface strings from {index_path}")


if __name__ == "__main__":
    main()
