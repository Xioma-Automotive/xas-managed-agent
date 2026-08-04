"""XAS Allocation Agent — prototype reference solver (see build spec).

Core invariant:  plan = pure_function(data_snapshot, skill, ledger)

Modules:
  decisions   — every <<DECIDE>> stub, surfaced at runtime.
  synth_data  — synthetic XAS-shaped pull (orders, units, incumbent, disruption).
  spec_match  — rule-driven is_compatible() + residual resolution with caching.
  solver      — OR-Tools min-cost-flow repair, cost model §2, pins §5, λ sweep.
  ledger      — append-only override store, replay-with-TTL fold.
  session     — the §8 per-turn loop; reason-coded change list.
"""

from .decisions import SOLVER_VERSION

__all__ = ["SOLVER_VERSION"]
__version__ = SOLVER_VERSION
