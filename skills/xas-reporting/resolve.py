"""Resolve a term against this tenant's phrasebook. Runs IN THE SANDBOX.

One verb, and it is all the agent ever runs:

    python resolve.py --lookup "חלפים" "spare parts" "parts"

The rungs that are pure lookup live HERE — exact surface, code or id, substring,
word-by-word, then nearest spelling — tried in that order and stopped at the
first that returns rows. Typing them out one grep at a time is what the skill
used to ask for, and a ladder written by hand is a ladder that can be climbed in
the wrong order: the loose search run first returns nineteen rows for `service`
where the anchored one returns the single right row.

Proposing the alternative WORDINGS stays the agent's judgment — this takes
several terms in one call so its proposals cost one round trip, not three — and
so does what to say about the row that comes back.

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
    # The app page a classification's records live on, for `link.py --route`.
    # Appended rather than inserted: the recipes in SKILL.md address columns by
    # position (`$4`, `$11`), so a new column goes on the end or they all shift.
    "route",
)

CODE_COLUMN = COLUMNS.index("code")
ID_COLUMN = COLUMNS.index("id")


def normalize(text: str) -> str:
    """Casefold, strip combining marks (Hebrew niqqud, Latin diacritics), collapse space."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(bare.casefold().split())


def read_rows(path: Path) -> list[tuple[str, ...]]:
    """The shipped table, back as rows. The header line is a legend, not data —
    parse it as data and the nearest-spelling rung starts proposing "normalized"."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [tuple(line.split("\t")) for line in lines[1:] if line]


# A misspelling is not a synonym problem: the letters are wrong, so neither the
# anchored match nor a substring search can reach the row. difflib closes that
# gap deterministically and offline. 0.6 is difflib's own default cutoff, kept so
# the suggestions stay tight — a loose cutoff turns "did you mean" into noise.
SUGGEST_CUTOFF = 0.6
SUGGEST_LIMIT = 5

# A substring rung is as broad as the word it is given: `service` matches nineteen
# rows in this tenant's table and a production tenant's runs to megabytes. Every
# row printed stays in the conversation for the session, so the loose rungs stop
# at 20 and say how many they held back. The exact rungs are never capped — a
# handful of rows there IS the answer.
LOOSE_LIMIT = 20


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


def _exact(term: str, rows: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Rows whose surface IS the term. One surface can sit under two kinds."""
    wanted = normalize(term)
    return [row for row in rows if row[0] == wanted]


def _by_identifier(term: str, rows: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Rows a code or an id points at — the phrasebook read backwards.

    A `JobState` arrives from the live system as a bare ObjectId, which is in the
    table's `id` column and is nobody's surface string, so the forward rungs
    cannot reach it.
    """
    wanted = normalize(term)
    if not wanted:
        return []
    return [
        row
        for row in rows
        if len(row) > ID_COLUMN
        and wanted in (normalize(row[CODE_COLUMN]), normalize(row[ID_COLUMN]))
    ]


# The columns a user's own words can land in. The rest — `classification`,
# `entity`, `state`, `id` — are provenance, and searching them is what made a
# substring rung useless: `Parts` sits in the classification column of every
# status row, so one common word pulled fifty rows of statuses.
SEARCHED_COLUMNS = (COLUMNS.index("normalized"), COLUMNS.index("surface"), COLUMNS.index("name"))


def _haystack(row: tuple[str, ...]) -> str:
    return normalize("\t".join(row[i] for i in SEARCHED_COLUMNS if i < len(row)))


def _containing(term: str, rows: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Rows whose wording holds the term — the substring rung, normalized so that
    Hebrew typed without niqqud still matches the stored form."""
    wanted = normalize(term)
    return [row for row in rows if wanted in _haystack(row)]


def _by_word(term: str, rows: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """A multi-word term one word at a time, most words matched first.

    A phrase nobody stored whole still has words that are stored, and the rows
    holding the most of them come first.

    A word that matches NOTHING sends the whole term down to the nearest-spelling
    rung instead: in `sapre parts` only `parts` is a real word, and the rows it
    alone pulls are every status of the Parts classification — noise, with the
    `Spare Parts` the user meant nowhere among them.
    """
    words = normalize(term).split()
    if len(words) < 2:
        return []
    haystacks = [_haystack(row) for row in rows]
    hits_by_word = [sum(word in hay for hay in haystacks) for word in words]
    if not all(hits_by_word):
        return []
    scored = []
    for position, (row, haystack) in enumerate(zip(rows, haystacks)):
        hits = sum(word in haystack for word in words)
        if hits:
            scored.append((-hits, position, row))
    return [row for _, _, row in sorted(scored)]


# Each rung is (label, finder, capped). Order is the whole point: the exact rungs
# run before anything broad, so the single right row is never buried in thirteen.
RUNGS = (
    ("exact", _exact, False),
    ("code or id", _by_identifier, False),
    ("partial", _containing, True),
    ("words", _by_word, True),
)


def lookup(
    terms: list[str], rows: list[tuple[str, ...]]
) -> tuple[str, str, list[tuple[str, ...]], int]:
    """Work the ladder over ``terms``; stop at the first rung any of them hits.

    Rung before term, not term before rung: an exact match on the third wording
    the agent proposed beats a substring match on the first.

    Returns (term, rung, rows to show, rows found). ``rung`` is "near" for the
    nearest-spelling candidates, which are the caller's to CONFIRM and never to
    use, and "" when nothing matched at all.
    """
    for label, finder, capped in RUNGS:
        for term in terms:
            found = finder(term, rows)
            if found:
                shown = found[:LOOSE_LIMIT] if capped else found
                return term, label, shown, len(found)

    for term in terms:
        near = suggest(term, rows)
        if near:
            return term, "near", near, len(near)

    return terms[0], "", [], 0


def report(term: str, rung: str, shown: list[tuple[str, ...]], found: int) -> str:
    """What the agent reads: which term matched and how, then the rows."""
    if not rung:
        return f"no match for {term!r} — ask the user what they meant"

    if rung == "near":
        head = f"no match for {term!r} — nearest entries, CONFIRM before using one:"
    else:
        head = f"matched {term!r} — {rung}"
    if found > len(shown):
        head += f" (showing {len(shown)} of {found}; narrow the term for the rest)"

    lines = [head, "\t".join(COLUMNS)]
    lines += ["\t".join(row) for row in shown]
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--lookup":
        if not PHRASEBOOK_PATH.is_file():
            sys.exit(f"No phrasebook at {PHRASEBOOK_PATH} (it ships in this skill)")
        print(report(*lookup(sys.argv[2:], read_rows(PHRASEBOOK_PATH))))
        return

    sys.exit('usage: resolve.py --lookup "<term>" ["<other wording>" …]')


if __name__ == "__main__":
    main()
