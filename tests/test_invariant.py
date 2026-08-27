"""Determinism invariant (§11.7) — the crack the whole design guards against.

    plan = pure_function(data_snapshot, skill, override)

Steering is a single combined **override** object the agent carries forward —
there is no ledger, no append-only log, no replay, no TTL. These tests prove:
  1. the pull re-reads byte-identically (scenario CSVs -> translate -> flatten);
  2. a solve is deterministic given (snapshot, override);
  3. the headline invariant: DISCARD the sandbox (all in-memory state), re-read
     the mounted payloads from disk, re-flatten, re-apply the SAME override ->
     the SAME plan, byte-for-byte. The override is the only state that crosses
     the discard; there is nothing else to remember.

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
from xas_allocation.flatten import flatten, flatten_paths
from xas_allocation.session import run_cycle
from xas_allocation.solver import solve

# The scenario every case here runs over: both disturbances at once, so the plan
# has unallocated demand AND late arrivals to weigh against each other.
SCENARIO = "scenario-mixed"

# A representative steering override — all three keys, naming real rows of that
# scenario. Just a dict; the whole point is that the agent carries this object,
# not a log of how it was built.
STEER = {
    "priority": [{"order": "502387", "step": "urgent"}],
    "may_move": {"only": {"models": ["T71506JGVMH0009"]}, "never": ["503756"]},
    "churn_price": 25,
}


def _pull() -> dict:
    """The pull as the host produces it: read the scenario's CSVs, translate."""
    return datasource.get_source(SCENARIO).pull()


def _snapshot() -> object:
    """Translate + flatten, in memory — the two halves of the data path."""
    pull = _pull()
    return flatten(datasource.orders_payload(pull), datasource.vehicles_payload(pull))


def _mount(directory: Path) -> tuple[Path, Path]:
    """Write the two payloads the host mounts, then hand back their paths — the
    files the sandbox actually reads."""
    pull = _pull()
    orders, vehicles = directory / "orders.json", directory / "vehicles.json"
    orders.write_text(json.dumps(datasource.orders_payload(pull), sort_keys=True))
    vehicles.write_text(json.dumps(datasource.vehicles_payload(pull), sort_keys=True))
    return orders, vehicles


def _plan_json(plan: dict[str, str]) -> str:
    """Canonical serialization for byte-comparison."""
    return json.dumps({str(k): v for k, v in sorted(plan.items())}, sort_keys=True)


def test_snapshot_reproducible() -> None:
    a, b = _snapshot().as_dict(), _snapshot().as_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_solve_deterministic() -> None:
    snap = _snapshot()
    r1 = solve(snap, STEER, churn_price=25)
    r2 = solve(snap, STEER, churn_price=25)
    assert _plan_json(r1.plan) == _plan_json(r2.plan)
    assert r1.self_check["ok"], r1.self_check["violations"]


def test_override_invariant_across_sandbox_discard() -> None:
    """The headline invariant, ledger-free: the ONLY state that survives a sandbox
    discard is the override dict. Re-pull, re-flatten, re-apply it -> same plan."""
    with tempfile.TemporaryDirectory() as d:
        orders_path, vehicles_path = _mount(Path(d))

        def frontier(cyc) -> list[tuple]:
            return [
                (p.churn_price, p.n_changes, p.weighted_late_days, p.unfilled) for p in cyc.sweep
            ]

        # --- Run A: read the mounted payloads, flatten, solve under the override. ---
        snapA = flatten_paths(orders_path, vehicles_path)
        cycA = run_cycle(snapA, STEER)
        planA, frontierA = _plan_json(cycA.chosen.plan), frontier(cycA)

        # --- DISCARD the sandbox: drop every in-memory object. Re-read the same
        #     mounted files, re-flatten, re-apply the SAME override (carried, not
        #     remembered — it's just the dict we already had). ---
        del snapA, cycA
        snapB = flatten_paths(orders_path, vehicles_path)  # re-read, not remembered
        cycB = run_cycle(snapB, STEER)
        planB, frontierB = _plan_json(cycB.chosen.plan), frontier(cycB)

        assert planA == planB, "plan changed after sandbox discard — state leaked"
        assert frontierA == frontierB, "the churn-price frontier changed after sandbox discard"


def test_override_is_order_independent() -> None:
    """The override is a set of accumulated instructions, not an ordered log: the
    same instructions in a different arrangement produce the same plan. (What the
    ledger's replay order used to guarantee, now trivially true — there is no
    order to get wrong.)"""
    snap = _snapshot()
    a = {
        "priority": [{"order": "502387", "step": "urgent"}],
        "may_move": {"never": ["503756"], "only": {"models": ["T71506JGVMH0009"]}},
        "churn_price": 25,
    }
    b = {
        "churn_price": 25,
        "may_move": {"only": {"models": ["T71506JGVMH0009"]}, "never": ["503756"]},
        "priority": [{"order": "502387", "step": "urgent"}],
    }
    assert _plan_json(solve(snap, a, churn_price=25).plan) == _plan_json(
        solve(snap, b, churn_price=25).plan
    )


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
