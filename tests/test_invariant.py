"""Determinism invariant (§11.7) — the crack the whole design guards against.

    plan = pure_function(data_snapshot, skill, ledger)

These tests prove:
  1. the fabricated pull regenerates byte-identically (engine seed + flatten);
  2. a solve is deterministic given (snapshot, override);
  3. the headline invariant: DISCARD the sandbox (all in-memory state), reload the
     ledger from disk, re-pull the dataset from disk and re-flatten, replay ->
     the SAME plan, byte-for-byte;
  4. TTL entries drop out of replay once the pull date passes their expiry.

Eligibility is now a hard `sales_model` equality — there is no LLM residual
left to cache, so the old residual-cache leak test is gone with it.

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

from scenario_engine.generate import generate
from xas_allocation.flatten import flatten
from xas_allocation.ledger import Ledger, LedgerEntry
from xas_allocation.session import run_cycle
from xas_allocation.solver import solve

SEED = 20

# A representative steering override (defer + boost + λ), fixed timestamp so the
# ledger serializes identically on every run. Dates now, not week labels.
STEER = LedgerEntry(
    turn=1,
    author="Olga",
    override={
        "pins": [{"order": "SO-4000", "action": "defer", "not_before": "2026-09-21"}],
        "boosts": [{"customer": "CUST-001", "weight_mult": 3.0}],
        "lambda": 25,
        "ttl": None,
    },
    timestamp="2026-08-04T09:15:00Z",
    ttl=None,
)


def _pull_from_disk(path: Path) -> dict:
    """Fabricate the dataset to disk, then read it back — the pull the agent sees."""
    path.write_text(json.dumps(generate(seed=SEED)["pull"], sort_keys=True))
    return json.loads(path.read_text())


def _plan_json(plan: dict[str, str]) -> str:
    """Canonical serialization for byte-comparison."""
    return json.dumps({str(k): v for k, v in sorted(plan.items())}, sort_keys=True)


def test_snapshot_reproducible() -> None:
    a = flatten(generate(seed=SEED)["pull"]).as_dict()
    b = flatten(generate(seed=SEED)["pull"]).as_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_solve_deterministic() -> None:
    snap = flatten(generate(seed=SEED)["pull"])
    override = STEER.override
    r1 = solve(snap, override, lam=25)
    r2 = solve(snap, override, lam=25)
    assert _plan_json(r1.plan) == _plan_json(r2.plan)
    assert r1.self_check["ok"], r1.self_check["violations"]


def test_replay_invariant_across_sandbox_discard() -> None:
    with tempfile.TemporaryDirectory() as d:
        ledger_path = Path(d) / "ledger.json"
        data_path = Path(d) / "pull.json"

        def frontier(cyc) -> list[tuple]:
            return [(p.lam, p.n_changes, p.weighted_late_days, p.unfilled) for p in cyc.sweep]

        # --- Run A: build ledger, pull+flatten, solve. Sandbox = in-memory. ---
        ledgerA = Ledger.load(ledger_path)
        ledgerA.append(STEER)  # persists to disk
        snapA = flatten(_pull_from_disk(data_path))
        cycA = run_cycle(snapA, ledgerA)
        planA, frontierA = _plan_json(cycA.chosen.plan), frontier(cycA)

        # --- DISCARD the sandbox: drop every in-memory object. Reload the ledger
        #     from disk, re-pull the dataset from disk, re-flatten, replay. ---
        del ledgerA, snapA, cycA
        ledgerB = Ledger.load(ledger_path)  # reloaded, not remembered
        snapB = flatten(_pull_from_disk(data_path))  # re-pulled, not remembered
        cycB = run_cycle(snapB, ledgerB)
        planB, frontierB = _plan_json(cycB.chosen.plan), frontier(cycB)

        assert planA == planB, "plan changed after sandbox discard — state leaked"
        assert frontierA == frontierB, "λ frontier changed after sandbox discard"


def test_ttl_entry_drops_from_replay() -> None:
    from datetime import date

    ledger = Ledger.load(None)
    ledger.append(
        LedgerEntry(
            turn=1,
            author="Olga",
            override={"boosts": [{"customer": "CUST-001", "weight_mult": 3.0}]},
            timestamp="2026-08-04T09:15:00Z",
            ttl="2026-08-17",  # expires after this date
        )
    )
    active = ledger.replay(current_date=date(2026, 8, 10))  # within TTL
    expired = ledger.replay(current_date=date(2026, 9, 1))  # past TTL
    assert active["boosts"] == [{"customer": "CUST-001", "weight_mult": 3.0}]
    assert expired["boosts"] == [], "TTL-expired entry still contributed on replay"


ALL_TESTS = [
    test_snapshot_reproducible,
    test_solve_deterministic,
    test_replay_invariant_across_sandbox_discard,
    test_ttl_entry_drops_from_replay,
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
