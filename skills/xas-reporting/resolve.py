"""Resolve a term against this tenant's phrasebook. Runs IN THE SANDBOX.

One verb, and it is all the agent ever runs:

    python resolve.py --lookup "חלפים" "spare parts" "parts"

The rungs that are pure lookup live HERE — exact surface, code or id, substring,
word-by-word, then nearest spelling. EVERY term gets its own best rung and every
one of them is reported: the rungs ORDER the answer, they no longer suppress it.
Typing them out one grep at a time is what the skill used to ask for, and a
ladder written by hand is a ladder that can be climbed in the wrong order: the
loose search run first returns nineteen rows for `service` where the anchored one
returns the single right row. Ordering keeps that; answering every term keeps the
other seven wordings the agent was told to send.

Proposing the alternative WORDINGS stays the agent's judgment — this takes
several terms in one call so its proposals cost one round trip, not three — and
so does what to say about the rows that come back.

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
ROLE_COLUMN = COLUMNS.index("role")
NAME_COLUMN = COLUMNS.index("name")
SURFACE_COLUMN = COLUMNS.index("surface")


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


# Each rung is (label, finder, capped). Order is presentation, not suppression:
# every term is answered, and the exact rungs are printed first so the single
# right row is never read after thirteen loose ones.
RUNGS = (
    ("exact", _exact, False),
    ("code or id", _by_identifier, False),
    ("partial", _containing, True),
    ("words", _by_word, True),
)

RUNG_ORDER = {label: position for position, (label, _, _) in enumerate(RUNGS)}

# The whole call's row budget, on top of the per-term cap. Eight broad wordings
# at LOOSE_LIMIT each would put 160 rows in a conversation that re-reads them
# every later turn; the blocks are emitted best-rung-first, so what a ceiling
# cuts is always the loosest end.
TOTAL_LIMIT = 60

Match = tuple[str, str, list[tuple[str, ...]], int]


def best_rung(term: str, rows: list[tuple[str, ...]]) -> Match:
    """The highest rung this ONE term reaches: (term, rung, rows to show, rows found)."""
    for label, finder, capped in RUNGS:
        found = finder(term, rows)
        if found:
            return term, label, (found[:LOOSE_LIMIT] if capped else found), len(found)
    return term, "", [], 0


def lookup(terms: list[str], rows: list[tuple[str, ...]]) -> list[Match]:
    """Answer EVERY term, best rung first.

    The agent is told to send every wording it would have tried in one call, so a
    call that answers one of them and drops the rest makes extra wordings HARM the
    answer: a hedge that happens to hit a higher rung hides the wording that meant
    what the user asked. This used to stop at the first rung any term reached —
    right for synonyms of one thing, wrong for hedges across different things, and
    the caller cannot tell which it is doing at the time it calls. So the rungs
    rank the blocks and nothing is discarded.

    The nearest-spelling rung stays a WHOLE-CALL fallback rather than running per
    term: a hedge word that is nobody\'s term ("status") has near neighbours that
    are nobody\'s answer ("Task"), and the caller is instructed to act on a
    "CONFIRM" line. It fires only when no term reached a real rung.

    ``rung`` is "near" for those candidates, which are the caller\'s to CONFIRM and
    never to use, and "" for a term that matched nothing.
    """
    matches = [best_rung(term, rows) for term in terms]
    if any(rung for _, rung, _, _ in matches):
        # Stable by the order the agent proposed them, so equal rungs read as typed.
        ranked = sorted(
            enumerate(matches),
            key=lambda pair: (RUNG_ORDER.get(pair[1][1], len(RUNGS)), pair[0]),
        )
        return [match for _, match in ranked]

    for term in terms:
        near = suggest(term, rows)
        if near:
            return [(term, "near", near, len(near))]

    return [(terms[0], "", [], 0)]


def _headline(term: str, rung: str, shown: list[tuple[str, ...]], found: int) -> str:
    if rung == "near":
        return f"no match for {term!r} — nearest entries, CONFIRM before using one:"
    head = f"matched {term!r} — {rung}"
    if found > len(shown):
        head += f" (showing {len(shown)} of {found}; narrow the term for the rest)"
    return head


def report(matches: list[Match]) -> str:
    """What the agent reads: the column legend, then one block per wording.

    A row already printed under an earlier wording is not printed twice — the
    block says so instead, because two wordings finding the same row is the
    normal case and paying for it twice is what makes a hedge expensive.
    """
    if len(matches) == 1 and not matches[0][1]:
        return f"no match for {matches[0][0]!r} — ask the user what they meant"

    lines = ["\t".join(COLUMNS)]
    seen: set[tuple[str, ...]] = set()
    budget = TOTAL_LIMIT
    for term, rung, shown, found in matches:
        if not rung:
            lines.append(f"no match for {term!r}")
            continue
        head = _headline(term, rung, shown, found)
        fresh = [row for row in shown if row not in seen]
        repeated = len(shown) - len(fresh)
        held = max(0, len(fresh) - budget)
        fresh = fresh[: len(fresh) - held]
        seen.update(fresh)
        budget -= len(fresh)
        if repeated:
            head += f" — {repeated} already above"
        if held:
            head += f" — {held} held back, the call is full"
        lines.append(head)
        lines += ["\t".join(row) for row in fresh]
    return "\n".join(lines)


# --list: the bucket list a breakdown loops over. `--lookup` answers "what does
# this word mean"; nothing answered "what are all the values", so a session
# invented status names one at a time and looked each one up — three round trips
# that still missed `99 Disabled`.
#
# A tenant with hundreds of classifications should not put all of them in the
# conversation at once, and a list that long is not a loop anybody wants either.
BUCKET_LIMIT = 100

# Which surface stands for a record when several point at it: the printable name
# first, then the code. A loop filters on the code and prints the name, and an
# alias row (`1212`) is neither.
ROLE_RANK = {"name": 0, "code": 1}


def _cell(row: tuple[str, ...], index: int) -> str:
    return row[index] if index < len(row) else ""


def buckets(filters: dict[str, str], rows: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Every distinct RECORD matching `column=value`, one row each.

    The table is one row per surface string, so a record with a code, a name and
    four aliases is six rows and a loop wants one. Distinctness is
    (code, name, id): the eleven JobCard classifications that share status
    `01 New` collapse to ONE bucket, while vehicle `02` stays TWO, because
    `On The Way` and `Available For Sale` are two names under one code and a loop
    that sent that code once would silently sum them.
    """
    wanted = {COLUMNS.index(column): normalize(value) for column, value in filters.items()}
    best: dict[tuple[str, ...], tuple[int, tuple[str, ...]]] = {}
    for row in rows:
        if any(normalize(_cell(row, index)) != value for index, value in wanted.items()):
            continue
        identity = (
            _cell(row, CODE_COLUMN),
            _cell(row, NAME_COLUMN),
            _cell(row, ID_COLUMN),
        )
        # An ENTITY row carries none of the three, so it is its own bucket.
        key = identity if any(identity) else (_cell(row, SURFACE_COLUMN),)
        rank = ROLE_RANK.get(_cell(row, ROLE_COLUMN), len(ROLE_RANK))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, row)
    return sorted(
        (row for _, row in best.values()),
        key=lambda row: (_cell(row, CODE_COLUMN), _cell(row, NAME_COLUMN)),
    )


def bucket_report(filters: dict[str, str], found: list[tuple[str, ...]]) -> str:
    """What the agent reads: how many buckets there are, then one row each."""
    asked = " ".join(f"{column}={value}" for column, value in filters.items())
    if not found:
        return f"no rows for {asked}"
    shown = found[:BUCKET_LIMIT]
    head = f"{len(found)} buckets — {asked}"
    if len(shown) < len(found):
        head += f" (showing {len(shown)}; add another column=value to narrow)"
    return "\n".join([head, "\t".join(COLUMNS)] + ["\t".join(row) for row in shown])


def parse_filters(pairs: list[str]) -> dict[str, str]:
    """`kind=status entity=Vehicle` -> {"kind": "status", "entity": "Vehicle"}.

    An unknown column is an error naming the real ones rather than an empty list:
    a filter that silently matches nothing reads exactly like a tenant that has
    none of these.
    """
    filters: dict[str, str] = {}
    for pair in pairs:
        column, sep, value = pair.partition("=")
        if not sep or column not in COLUMNS:
            sys.exit(f"{pair!r}: expected <column>=<value>, column one of {', '.join(COLUMNS)}")
        filters[column] = value
    return filters


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--lookup":
        if not PHRASEBOOK_PATH.is_file():
            sys.exit(f"No phrasebook at {PHRASEBOOK_PATH} (it ships in this skill)")
        print(report(lookup(sys.argv[2:], read_rows(PHRASEBOOK_PATH))))
        return

    if len(sys.argv) > 2 and sys.argv[1] == "--list":
        if not PHRASEBOOK_PATH.is_file():
            sys.exit(f"No phrasebook at {PHRASEBOOK_PATH} (it ships in this skill)")
        filters = parse_filters(sys.argv[2:])
        print(bucket_report(filters, buckets(filters, read_rows(PHRASEBOOK_PATH))))
        return

    sys.exit(
        'usage: resolve.py --lookup "<term>" ["<other wording>" …]\n'
        "       resolve.py --list <column>=<value> [<column>=<value> …]"
    )


if __name__ == "__main__":
    main()
