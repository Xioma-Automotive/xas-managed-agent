"""Resolve a term against this tenant's phrasebook. Runs IN THE SANDBOX.

Everything here is the QUERY side, and it is all the agent ever runs:

    python resolve.py --normalize "חֲלָפִים"    # normalize a term before the anchored grep
    python resolve.py --suggest "sapre parts"  # closest real entries to a term that missed

The table it reads, `phrasebook.tsv`, ships built beside this file — see the
repo-root `phrasebook.py`, which builds it at deploy time and imports `normalize`
and `COLUMNS` from HERE. That import direction is forced: this file must stand
alone in a sandbox that cannot see the repo, so anything both sides need lives
here and the builder borrows it. One definition, so the column the table was
BUILT with and the form a query is normalized INTO cannot drift apart.
"""

from __future__ import annotations

import difflib
import sys
import unicodedata
from pathlib import Path

# The table ships beside this file, so it is found relative to __file__ — the
# only thing that knows where the platform unpacked the skill.
PHRASEBOOK_PATH = Path(__file__).resolve().parent / "phrasebook.tsv"

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


def read_rows(path: Path) -> list[tuple[str, ...]]:
    """The shipped table, back as rows. The header line is a legend, not data —
    parse it as data and `--suggest` starts proposing the word "normalized"."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [tuple(line.split("\t")) for line in lines[1:] if line]


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
        if not PHRASEBOOK_PATH.is_file():
            sys.exit(f"No phrasebook at {PHRASEBOOK_PATH} (it ships in this skill)")
        rows = suggest(sys.argv[2], read_rows(PHRASEBOOK_PATH))
        if not rows:
            print(f"no near match for {sys.argv[2]!r} — ask the user what they meant")
            return
        print("\t".join(COLUMNS))
        for row in rows:
            print("\t".join(row))
        return

    sys.exit('usage: resolve.py --normalize "<term>" | --suggest "<term>"')


if __name__ == "__main__":
    main()
