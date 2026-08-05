"""The override ledger — an application-level pattern (§7), NOT an API primitive.

DECIDE-5: the *statefulness* (persisted session state, mid-session steering) is a
Managed Agents platform concern; the ledger — schema, append-only discipline,
replay loop, TTL, attribution — is what we build on top. This prototype persists
it to a plain JSON file so replay is provable without depending on any beta
session-state surface. Verify the platform persistence API before wiring these
together.

The ledger IS the session: an ordered, timestamped, append-only list of override
entries. Plan state is never mutated in place — every turn appends one entry and
re-derives the plan by replaying the whole ledger top-to-bottom. Discard the
sandbox, replay the ledger against a fresh pull, and you get the same plan. If
you don't, state has leaked and that's the bug.

Determinism note: timestamps are attribution metadata only and never affect
replay logic; callers supply them (no wall-clock here) so replays are stable.
TTL is evaluated against a *current date* (the pull's ``now``), not real time,
for the same reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .snapshot import parse_date


@dataclass
class LedgerEntry:
    turn: int
    author: str
    override: dict  # §6 typed override object
    timestamp: str | None = None
    ttl: str | None = None  # date label, e.g. "2026-09-30"; None = permanent

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "timestamp": self.timestamp,
            "author": self.author,
            "override": self.override,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, d: dict) -> LedgerEntry:
        return cls(
            turn=d["turn"],
            author=d["author"],
            override=d.get("override", {}),
            timestamp=d.get("timestamp"),
            ttl=d.get("ttl"),
        )


def _entry_active(entry: LedgerEntry, current_date: date) -> bool:
    """A TTL'd entry is skipped once the current date passes its expiry, so a
    one-off nudge can't silently distort a later cycle (§7)."""
    if entry.ttl is None:
        return True
    return current_date <= parse_date(entry.ttl)


@dataclass
class Ledger:
    path: Path | None = None
    entries: list[LedgerEntry] = field(default_factory=list)

    # --- persistence ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None) -> Ledger:
        if path is None:
            return cls(path=None, entries=[])
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            return cls(path=p, entries=[LedgerEntry.from_dict(e) for e in data])
        return cls(path=p, entries=[])

    def save(self) -> None:
        if self.path is not None:
            self.path.write_text(json.dumps([e.to_dict() for e in self.entries], indent=2))

    # --- append-only discipline ---------------------------------------------
    def append(self, entry: LedgerEntry) -> LedgerEntry:
        """The ONLY mutation. Never edits or deletes an existing entry."""
        self.entries.append(entry)
        self.save()
        return entry

    def next_turn(self) -> int:
        return (self.entries[-1].turn + 1) if self.entries else 1

    # --- replay --------------------------------------------------------------
    def replay(self, current_date: date) -> dict:
        """Fold the ledger (skipping TTL-expired entries) into one combined
        override. Top-to-bottom; later λ wins, everything else accumulates."""
        combined: dict = {"pins": [], "boosts": [], "forbid": [], "lambda": None}
        for e in self.entries:
            if not _entry_active(e, current_date):
                continue
            ov = e.override or {}
            combined["pins"] += ov.get("pins", [])
            combined["boosts"] += ov.get("boosts", [])
            combined["forbid"] += ov.get("forbid", [])
            if ov.get("lambda") is not None:
                combined["lambda"] = ov["lambda"]
        return combined

    # --- attribution (§7) ----------------------------------------------------
    def who_touched(self, order_id: str, current_date: date) -> list[str]:
        """Attribution trail: which entries acted on a given order, so
        '4471 moved because Olga pinned it turn 5' is distinguishable from a
        solver-driven move."""
        trail: list[str] = []
        for e in self.entries:
            if not _entry_active(e, current_date):
                continue
            ov = e.override or {}
            hits = [p for p in ov.get("pins", []) if str(p.get("order")) == order_id]
            hits += [f for f in ov.get("forbid", []) if str(f.get("order")) == order_id]
            for h in hits:
                trail.append(f"turn {e.turn} ({e.author}): {h.get('action', 'pin')} on {order_id}")
        return trail
