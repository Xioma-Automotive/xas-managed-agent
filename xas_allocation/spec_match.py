"""Spec compatibility — the one place LLM judgment can leak into the plan.

``is_compatible(unit_spec, order_spec)`` is pure rule logic: an order can be
filled by a unit only if every *co-specified* field agrees. Where a field is
unknown on either side (half-populated configs, §8.3) the rule treats it as a
wildcard — EXCEPT for fields in ``CRITICAL_FIELDS``, where an unknown is genuine
ambiguity the rules can't settle and must be escalated to the residual resolver.

The determinism soft spot (§11): any residual resolved by judgment MUST be
written back to a cache so a second run inherits the first run's call instead of
re-judging. ``ResidualCache`` + ``resolve_compatibility`` implement exactly that.
The live-LLM resolver is a clearly-marked TODO hook; the prototype fallback is
deterministic so the invariant test is stable, and it still writes back through
the cache so the caching path itself is exercised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Customer priority letter -> multiplicative weight on W(o) (§2).
PRIORITY_WEIGHT: dict[str, float] = {"A": 3.0, "B": 2.0, "C": 1.0}

# Spec fields compared by the rule. "model" is the bin key (always populated).
SPEC_FIELDS = ("model", "drivetrain", "trim", "color")

# Fields where "unknown on either side" is real ambiguity, not a free wildcard:
# you cannot safely fill an AWD order with a unit of unknown drivetrain.
CRITICAL_FIELDS = frozenset({"drivetrain"})


@dataclass(frozen=True)
class MatchResult:
    compatible: bool
    ambiguous: bool          # relied on a wildcard over a CRITICAL field
    reason: str
    critical_wildcards: tuple[str, ...] = ()


def is_compatible(unit_spec: dict, order_spec: dict) -> MatchResult:
    """Pure rule-driven compatibility. No judgment, no I/O, deterministic."""
    critical_wild: list[str] = []
    for f in SPEC_FIELDS:
        u = unit_spec.get(f)
        o = order_spec.get(f)
        if u is not None and o is not None:
            if u != o:
                return MatchResult(False, False, f"{f} mismatch: unit={u} order={o}")
        else:
            # One or both unspecified -> wildcard at the rule level.
            if f in CRITICAL_FIELDS:
                critical_wild.append(f)
    if critical_wild:
        return MatchResult(
            compatible=True,
            ambiguous=True,
            reason=f"rule-compatible but relies on wildcard over critical field(s): "
            f"{', '.join(critical_wild)}",
            critical_wildcards=tuple(critical_wild),
        )
    return MatchResult(True, False, "all co-specified fields agree")


# --- Residual resolution + cache ---------------------------------------------

def _signature(unit_spec: dict, order_spec: dict) -> str:
    """Stable key for a residual decision. Keyed on the SPECS, not the ids, so
    the cached judgment generalizes across snapshots with the same spec pair."""
    u = tuple(unit_spec.get(f) for f in SPEC_FIELDS)
    o = tuple(order_spec.get(f) for f in SPEC_FIELDS)
    return json.dumps({"unit": u, "order": o}, sort_keys=True)


@dataclass
class ResidualCache:
    """Append-only-ish store of resolved residual decisions.

    Persisted to JSON so a discarded sandbox, replayed, inherits prior judgments
    rather than re-deciding them (the §11 determinism requirement)."""
    path: Optional[Path] = None
    decisions: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[str | Path]) -> "ResidualCache":
        if path is None:
            return cls(path=None)
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            return cls(path=p, decisions=dict(data))
        return cls(path=p, decisions={})

    def save(self) -> None:
        if self.path is not None:
            self.path.write_text(json.dumps(self.decisions, indent=2, sort_keys=True))

    def get(self, sig: str) -> Optional[bool]:
        return self.decisions.get(sig)

    def put(self, sig: str, value: bool) -> None:
        self.decisions[sig] = value


# TODO(LLM residual): the real resolver asks Claude to judge a genuinely
# ambiguous spec pair (e.g. "AWD order, unit drivetrain unspecified — fill it?").
# Signature: (unit_spec, order_spec) -> bool. Whatever it returns is cached below
# and NEVER re-asked, so determinism holds. Until it's wired, the prototype uses
# a deterministic conservative fallback.
LLMResolver = Callable[[dict, dict], bool]


def _conservative_fallback(unit_spec: dict, order_spec: dict) -> bool:
    """Deterministic stand-in for the LLM resolver.

    Conservative rule: if the ORDER specifies a critical field the unit leaves
    unknown, refuse (don't gamble a spec'd requirement on an unknown). If the
    order itself left it unknown, allow. This is a placeholder policy, not a
    settled answer — the real resolver replaces it."""
    for f in CRITICAL_FIELDS:
        if order_spec.get(f) is not None and unit_spec.get(f) is None:
            return False
    return True


def resolve_compatibility(
    unit_spec: dict,
    order_spec: dict,
    cache: ResidualCache,
    resolver: Optional[LLMResolver] = None,
) -> bool:
    """Full compatibility incl. residual resolution with write-back caching.

    Rule-clear cases (compatible/incompatible with no critical wildcard) never
    touch the cache. Ambiguous cases are resolved once — from cache if present,
    else via ``resolver`` (or the deterministic fallback) — and the result is
    written back so it's inherited on replay."""
    rule = is_compatible(unit_spec, order_spec)
    if not rule.compatible:
        return False
    if not rule.ambiguous:
        return True

    sig = _signature(unit_spec, order_spec)
    cached = cache.get(sig)
    if cached is not None:
        return cached

    decide = resolver or _conservative_fallback
    verdict = bool(decide(unit_spec, order_spec))
    cache.put(sig, verdict)  # write-back: never re-judged on replay
    return verdict


if __name__ == "__main__":
    a = {"model": "SUV", "drivetrain": "AWD", "trim": "Sport", "color": "Blue"}
    b = {"model": "SUV", "drivetrain": None, "trim": "Sport", "color": None}
    print("rule:", is_compatible(a, b))
    cache = ResidualCache.load(None)
    print("resolved (order AWD, unit unknown):", resolve_compatibility(a, b, cache))
