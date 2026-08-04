"""Determinism invariant (§11.7) — the crack the whole design guards against.

    plan = pure_function(data_snapshot, skill, ledger)

These tests prove:
  1. the synthetic pull regenerates byte-identically from its seed;
  2. a solve is deterministic given (snapshot, override);
  3. the headline invariant: DISCARD the sandbox (all in-memory state), reload the
     ledger from disk, regenerate the pull from the seed, replay -> the SAME plan,
     byte-for-byte;
  4. TTL entries drop out of replay once the current week passes their expiry;
  5. the residual (LLM-judgment) compatibility call is cached and inherited on a
     second run instead of being re-judged — the one place determinism can leak.

Runnable two ways:
    PYTHONPATH=. python tests/test_invariant.py     # plain-assert runner
    python -m pytest tests/test_invariant.py         # if pytest is installed
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xas_allocation.ledger import Ledger, LedgerEntry
from xas_allocation.session import run_cycle
from xas_allocation.solver import NOW_WEEK, solve
from xas_allocation.spec_match import ResidualCache, resolve_compatibility
from xas_allocation.synth_data import generate_snapshot

SEED = 20
GEN = dict(seed=SEED, spare_ratio=0.2, delay_weeks=2)

# A representative steering override (defer + boost + λ), fixed timestamp so the
# ledger serializes identically on every run.
STEER = LedgerEntry(
    turn=1,
    author="Olga",
    override={
        "pins": [{"order": 4000, "action": "defer", "not_before": "2026-W38"}],
        "boosts": [{"customer": "CUST-001", "weight_mult": 3.0}],
        "lambda": 25,
        "ttl": None,
    },
    timestamp="2026-08-04T09:15:00Z",
    ttl=None,
)


def _plan_json(plan: dict[int, int]) -> str:
    """Canonical serialization for byte-comparison."""
    return json.dumps({str(k): v for k, v in sorted(plan.items())}, sort_keys=True)


def test_snapshot_reproducible() -> None:
    a = generate_snapshot(**GEN).as_dict()
    b = generate_snapshot(**GEN).as_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_solve_deterministic() -> None:
    snap = generate_snapshot(**GEN)
    override = STEER.override
    r1 = solve(snap, override, ResidualCache.load(None), lam=25)
    r2 = solve(snap, override, ResidualCache.load(None), lam=25)
    assert _plan_json(r1.plan) == _plan_json(r2.plan)
    assert r1.self_check["ok"], r1.self_check["violations"]


def test_replay_invariant_across_sandbox_discard() -> None:
    with tempfile.TemporaryDirectory() as d:
        ledger_path = Path(d) / "ledger.json"

        def frontier(cyc) -> list[tuple]:
            return [(p.lam, p.n_changes, p.weighted_late_days, p.unfilled) for p in cyc.sweep]

        # --- Run A: build ledger, solve. Sandbox = these in-memory objects. ---
        ledgerA = Ledger.load(ledger_path)
        ledgerA.append(STEER)  # persists to disk
        snapA = generate_snapshot(**GEN)
        cycA = run_cycle(snapA, ledgerA, ResidualCache.load(None), current_week=NOW_WEEK)
        planA, frontierA = _plan_json(cycA.chosen.plan), frontier(cycA)

        # --- DISCARD the sandbox: drop every in-memory object. Reload the
        #     ledger from disk, regenerate the pull from the seed, replay. ---
        del ledgerA, snapA, cycA
        ledgerB = Ledger.load(ledger_path)          # reloaded, not remembered
        snapB = generate_snapshot(**GEN)            # regenerated, not remembered
        cycB = run_cycle(snapB, ledgerB, ResidualCache.load(None), current_week=NOW_WEEK)
        planB, frontierB = _plan_json(cycB.chosen.plan), frontier(cycB)

        assert planA == planB, "plan changed after sandbox discard — state leaked"
        assert frontierA == frontierB, "λ frontier changed after sandbox discard"


def test_ttl_entry_drops_from_replay() -> None:
    ledger = Ledger.load(None)
    ledger.append(
        LedgerEntry(
            turn=1,
            author="Olga",
            override={"boosts": [{"customer": "CUST-001", "weight_mult": 3.0}]},
            timestamp="2026-08-04T09:15:00Z",
            ttl="2026-W33",  # expires after week 33
        )
    )
    active = ledger.replay(current_week=32)     # within TTL
    expired = ledger.replay(current_week=40)    # past TTL
    assert active["boosts"] == [{"customer": "CUST-001", "weight_mult": 3.0}]
    assert expired["boosts"] == [], "TTL-expired entry still contributed on replay"


def test_residual_decision_is_cached_not_rejudged() -> None:
    # Order requires AWD; unit leaves drivetrain unknown -> ambiguous (residual).
    order_spec = {"model": "SUV", "drivetrain": "AWD", "trim": "Sport", "color": "Blue"}
    unit_spec = {"model": "SUV", "drivetrain": None, "trim": "Sport", "color": "Blue"}

    cache = ResidualCache.load(None)
    first = resolve_compatibility(unit_spec, order_spec, cache)  # fallback: refuse
    assert first is False
    assert len(cache.decisions) == 1  # the judgment was written back

    # A DIFFERENT resolver that would say True must NOT be consulted — the cached
    # decision is inherited, so the second run matches the first (determinism).
    second = resolve_compatibility(unit_spec, order_spec, cache, resolver=lambda u, o: True)
    assert second == first, "residual was re-judged instead of read from cache"


ALL_TESTS = [
    test_snapshot_reproducible,
    test_solve_deterministic,
    test_replay_invariant_across_sandbox_discard,
    test_ttl_entry_drops_from_replay,
    test_residual_decision_is_cached_not_rejudged,
]


def _main() -> int:
    failed = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(ALL_TESTS) - failed}/{len(ALL_TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
