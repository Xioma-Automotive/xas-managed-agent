"""Central registry of every `<<DECIDE>>` from the build spec.

The spec is emphatic: do NOT guess the open decisions. Each one below is
stubbed with a clearly-labelled DEFAULT and a one-line rationale, and every
default is surfaced at runtime via ``format_decisions()`` (printed at the top of
a ``session.py`` run) so a human reviewer sees exactly what was assumed and can
flip it before this prototype touches real dealer data.

Nothing here is a settled answer. These are load-bearing stubs, kept in ONE
place so the decisions are auditable rather than scattered through the solver.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    key: str
    title: str
    default: str
    rationale: str
    status: str = "STUB — needs human sign-off"


DECISIONS: list[Decision] = [
    Decision(
        key="DECIDE-1",
        title="Aging term: additive vs multiplicative on the effective weight W(o)",
        default="additive",
        rationale=(
            "W(o) = w_base·priority·(1+alpha·n_prior_delays) + beta·days_backordered. "
            "Additive keeps an old back-order from compounding with high priority into a "
            "runaway weight. Flip AGING_MODE to 'multiplicative' to make age and priority "
            "reinforce."
        ),
    ),
    Decision(
        key="DECIDE-2",
        title="Time-fence boundaries (weeks)",
        default="frozen <= 2 wks, slushy 3-6 wks, liquid > 6 wks",
        rationale=(
            "Classic MPS split. Frozen = hard pin, slushy = movable only at high lambda, "
            "liquid = free. Constants FROZEN_MAX_WEEKS / SLUSHY_MAX_WEEKS."
        ),
    ),
    Decision(
        key="DECIDE-3",
        title="Commit-point unit states that force a recall/hard pin",
        default="{'shipped', 'in_prep'}",
        rationale=(
            "A unit in these states is physically committed and cannot be reassigned. "
            "See COMMIT_POINT_STATES."
        ),
    ),
    Decision(
        key="DECIDE-4",
        title="Pin mechanism: pre-commit-arc vs infinite-cost",
        default="inf_cost (finite large penalty) for instruction pins; pre-commit (exclude) for data pins",
        rationale=(
            "Instruction pins use a large finite penalty so the lambda sweep can re-run "
            "without rebuilding the network and so conflicts surface as cost (DECIDE-8). "
            "Data pins (frozen/committed) are pre-committed out of the graph entirely."
        ),
    ),
    Decision(
        key="DECIDE-5",
        title="Managed Agents session-persistence + mid-session-steering API",
        default=(
            "ledger is a LOCAL application-level artifact (ledger.py); session persistence "
            "is NOT assumed as a platform primitive in this prototype"
        ),
        rationale=(
            "Spec §7 flags the ledger schema/replay/TTL as an application pattern, not an "
            "API primitive. The prototype persists the ledger to a JSON file so replay is "
            "provable without depending on a beta session-state API. Verify the current "
            "Managed Agents persistence + mid-session-steering surface against Anthropic "
            "docs before wiring the ledger to platform session state."
        ),
    ),
    Decision(
        key="DECIDE-6",
        title="xas-code MCP liveness-check pattern",
        default="single directory_tree call at session start (prototype: skipped, synthetic data)",
        rationale="Match Olga's standing xas-code liveness pattern once MCP is wired.",
    ),
    Decision(
        key="DECIDE-7",
        title="XAS API data contract (fields + endpoints)",
        default="synthetic generator; invented field schema in synth_data.py stands in",
        rationale=(
            "The real XAS API does not exist yet. synth_data.py defines the field shapes "
            "(orders + inbound units) this prototype assumes; treat them as the proposed "
            "contract, not a confirmed one."
        ),
    ),
    Decision(
        key="DECIDE-8",
        title="Infeasibility strategy",
        default="high-cost soft pins (option 1)",
        rationale=(
            "Instruction pins run as large finite costs so the solver always returns "
            "something; a violated pin shows up as a large cost line to surface ('honouring "
            "this pin costs N extra changes'). CP-SAT assumption-literal minimal conflict "
            "sets (option 2) are the more-honest upgrade, deferred with the CP-SAT escape "
            "hatch."
        ),
    ),
    Decision(
        key="DECIDE-9",
        title="Solver repo location + versioning",
        default="reference solver lives in-repo under xas_allocation/; skill pins SOLVER_VERSION",
        rationale=(
            "Spec §10: the reference copy lives in the skill for day-one. Canonical version "
            "moves to a tested repo before real dealer data; the skill then pins a version. "
            "SOLVER_VERSION is that pin point."
        ),
    ),
]


# --- Load-bearing defaults referenced by the solver / session -----------------
# These are the concrete values behind the decisions above. Change them HERE.

# DECIDE-1
AGING_MODE = "additive"  # "additive" | "multiplicative"

# DECIDE-2  (tardiness measured in whole weeks from the promised week)
FROZEN_MAX_WEEKS = 2
SLUSHY_MAX_WEEKS = 6

# DECIDE-3
COMMIT_POINT_STATES = frozenset({"shipped", "in_prep"})

# DECIDE-4 / DECIDE-8
# Large finite penalty used for soft instruction pins/forbids/defers. Big enough
# to dominate any legitimate lateness cost, small enough that the min-cost-flow
# stays well inside int range (no overflow) so a violated pin surfaces as a
# visible, comparable cost line instead of an outright infeasibility.
SOFT_PIN_COST = 1_000_000

# DECIDE-9
SOLVER_VERSION = "0.1.0-prototype"

# Cost-model coefficients (§2). Fixed formula, tunable coefficients.
CONVEX_EXPONENT = 1.5  # >1 so lateness never dumps entirely on one order
ALPHA = 0.5            # prior-delay escalation:  (1 + ALPHA * n_prior_delays)
BETA = 0.1            # back-order aging per day (see AGING_MODE)

# The lambda sweep (§2, "highest-value output").
LAMBDA_SWEEP = (0, 5, 10, 25, 50, 100)


def format_decisions() -> str:
    """Human-readable dump of every open decision + its stubbed default.

    Printed at the top of a session run so no assumption is silent.
    """
    lines = ["OPEN DECISIONS (stubbed defaults — NOT settled answers):"]
    for d in DECISIONS:
        lines.append(f"  [{d.key}] {d.title}")
        lines.append(f"      default : {d.default}")
        lines.append(f"      why     : {d.rationale}")
        lines.append(f"      status  : {d.status}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_decisions())
