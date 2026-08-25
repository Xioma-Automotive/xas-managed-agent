"""Determinism invariant (§11.7) — the crack the whole design guards against.

    plan = pure_function(data_snapshot, skill, override)

Steering is a single combined **override** object the agent carries forward —
there is no ledger, no append-only log, no replay, no TTL. These tests prove:
  1. the fabricated pull regenerates byte-identically (engine seed + flatten);
  2. a solve is deterministic given (snapshot, override);
  3. the headline invariant: DISCARD the sandbox (all in-memory state), re-pull
     the dataset from disk, re-flatten, re-apply the SAME override -> the SAME
     plan, byte-for-byte. The override is the only state that crosses the
     discard; there is nothing else to remember.

Eligibility is a hard `sales_model` equality — there is no LLM residual left to
cache, so the old residual-cache leak test is gone with it. Durable, cross-
session persistence of the override is a platform concern, deferred (DECIDE-5);
here it is simply a dict, and the invariant holds because it is re-applied
verbatim, not remembered by the sandbox.

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

import datasource
from scenario_engine.generate import generate
from xas_allocation.flatten import flatten
from xas_allocation.session import run_cycle
from xas_allocation.solver import solve

SEED = 20

# A representative steering override (defer + boost + λ). Just a dict — the whole
# point is that the agent carries this object, not a log of how it was built.
STEER = {
    "pins": [{"order": "VSO-4000-1", "action": "defer", "not_before": "2026-09-21"}],
    "boosts": [{"customer": "CUST-001", "weight_mult": 3.0}],
    "lambda": 25,
}


def _pull_from_disk(path: Path) -> dict:
    """Fabricate the dataset to disk, then read it back — the pull the agent sees."""
    rich = datasource.map_world(generate(seed=SEED)["pull"])
    path.write_text(json.dumps(rich, sort_keys=True))
    return json.loads(path.read_text())


def _plan_json(plan: dict[str, str]) -> str:
    """Canonical serialization for byte-comparison."""
    return json.dumps({str(k): v for k, v in sorted(plan.items())}, sort_keys=True)


def test_snapshot_reproducible() -> None:
    a = flatten(datasource.map_world(generate(seed=SEED)["pull"])).as_dict()
    b = flatten(datasource.map_world(generate(seed=SEED)["pull"])).as_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_solve_deterministic() -> None:
    snap = flatten(datasource.map_world(generate(seed=SEED)["pull"]))
    r1 = solve(snap, STEER, lam=25)
    r2 = solve(snap, STEER, lam=25)
    assert _plan_json(r1.plan) == _plan_json(r2.plan)
    assert r1.self_check["ok"], r1.self_check["violations"]


def test_override_invariant_across_sandbox_discard() -> None:
    """The headline invariant, ledger-free: the ONLY state that survives a sandbox
    discard is the override dict. Re-pull, re-flatten, re-apply it -> same plan."""
    with tempfile.TemporaryDirectory() as d:
        data_path = Path(d) / "pull.json"

        def frontier(cyc) -> list[tuple]:
            return [(p.lam, p.n_changes, p.weighted_late_days, p.unfilled) for p in cyc.sweep]

        # --- Run A: pull+flatten from disk, solve under the override. ---
        snapA = flatten(_pull_from_disk(data_path))
        cycA = run_cycle(snapA, STEER)
        planA, frontierA = _plan_json(cycA.chosen.plan), frontier(cycA)

        # --- DISCARD the sandbox: drop every in-memory object. Re-pull the dataset
        #     from disk, re-flatten, re-apply the SAME override (carried, not
        #     remembered — it's just the dict we already had). ---
        del snapA, cycA
        snapB = flatten(_pull_from_disk(data_path))  # re-pulled, not remembered
        cycB = run_cycle(snapB, STEER)
        planB, frontierB = _plan_json(cycB.chosen.plan), frontier(cycB)

        assert planA == planB, "plan changed after sandbox discard — state leaked"
        assert frontierA == frontierB, "λ frontier changed after sandbox discard"


def test_override_is_order_independent() -> None:
    """The override is a set of accumulated instructions, not an ordered log: the
    same instructions in a different arrangement produce the same plan. (What the
    ledger's replay order used to guarantee, now trivially true — there is no
    order to get wrong.)"""
    snap = flatten(datasource.map_world(generate(seed=SEED)["pull"]))
    a = {
        "boosts": [{"customer": "CUST-001", "weight_mult": 3.0}],
        "pins": [{"order": "VSO-4000-1", "action": "defer", "not_before": "2026-09-21"}],
        "lambda": 25,
    }
    b = {
        "lambda": 25,
        "pins": [{"order": "VSO-4000-1", "action": "defer", "not_before": "2026-09-21"}],
        "boosts": [{"customer": "CUST-001", "weight_mult": 3.0}],
    }
    assert _plan_json(solve(snap, a, lam=25).plan) == _plan_json(solve(snap, b, lam=25).plan)


ALL_TESTS = [
    test_snapshot_reproducible,
    test_solve_deterministic,
    test_override_invariant_across_sandbox_discard,
    test_override_is_order_independent,
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
