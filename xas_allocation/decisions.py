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
            "W(o) = priority · (1+alpha·n_prior_delays) + beta·days_backordered. "
            "Additive keeps an old back-order from compounding with high priority into a "
            "runaway weight. Flip AGING_MODE to 'multiplicative' to make age and priority "
            "reinforce."
        ),
    ),
    Decision(
        key="DECIDE-2",
        title="Time-fence boundaries (days from the pull date)",
        default="frozen <= 14d, slushy 15-42d, liquid > 42d",
        rationale=(
            "Classic MPS split, restated in days now the model is date-based. Frozen = "
            "hard pin, slushy = movable only at high lambda, liquid = free. Constants "
            "FROZEN_MAX_DAYS / SLUSHY_MAX_DAYS."
        ),
    ),
    Decision(
        key="DECIDE-3",
        title="Break cost: soft vs hard allocation (real vs future vehicle)",
        default="hard (VehicleClassification 'Vehicle') = expensive-but-movable; soft ('Future') = free; BREAK_COST",
        rationale=(
            "A VSO row bound to a REAL vehicle (VehicleClassification 'Vehicle', a VIN) is a "
            "HARD allocation; one bound to a FUTURE vehicle (VehicleClassification 'Future', "
            "not yet built) is SOFT. Hard is NOT a wall — the repair loop may bump it 'for the "
            "sake of another' order, it just pays a large finite BREAK_COST['hard'] to do so; "
            "soft costs BREAK_COST['soft'] (0 by default). The cost applies only to displacing "
            "an ON-TIME binding (a kept promise); an already-LATE binding protects nothing, so "
            "re-allocating a disrupted order is free — the break prices the bump VICTIM, not "
            "the disrupted order being rescued. There is NO location gradient: the real "
            "inventory-vehicle API carries no usable position (all location fields null), so "
            "hardness is the binary real-vs-future, keyed on Unit.vehicle_classification. "
            "BREAK_COST['hard'] is the tunable ratio 'how many weighted late-days is breaking "
            "one hard allocation worth'; it is a solver parameter the planner can override "
            "per session. (Supersedes the retired committed-vehicle hard wall.)"
        ),
    ),
    Decision(
        key="DECIDE-4",
        title="Pin mechanism: pre-commit-arc vs infinite-cost",
        default="inf_cost (finite large penalty) for instruction pins; pre-commit (exclude) for data pins",
        rationale=(
            "Instruction pins use a large finite penalty so the lambda sweep can re-run "
            "without rebuilding the network and so conflicts surface as cost (DECIDE-8). "
            "The frozen-fence data pin is pre-committed out of the graph entirely "
            "(a real vehicle is NOT — it is priced via BREAK_COST, DECIDE-3)."
        ),
    ),
    Decision(
        key="DECIDE-5",
        title="Managed Agents session-persistence + mid-session-steering API",
        default=(
            "steering is a single combined OVERRIDE object the agent carries forward and "
            "confirms each turn in plain words (the object itself only on request); NO durable, "
            "cross-session persistence is assumed as a platform primitive in this prototype"
        ),
        rationale=(
            "The invariant is plan = f(snapshot, skill, override): same snapshot + same "
            "override => byte-identical plan, so the override is the only state that must "
            "survive. In this prototype it lives only in the conversation — after a sandbox "
            "reclaim the agent restates it from the conversation, printing the object when a "
            "handover needs it; there is no ledger. A "
            "durable host-side store (web.py keyed by session id, shipped in via the pull) is "
            "the real fix and stays DEFERRED. Verify the current Managed Agents persistence + "
            "mid-session-steering surface against Anthropic docs before wiring it."
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
        default=(
            "pull comes from a callable DataSource resolved HOST-SIDE (datasource.py): the "
            "scenario-engine fake by default, the real XAS endpoint by config. Either returns "
            "the rich {meta, pos, sos, supply, disruption} contract; field shapes per "
            "docs/xasdatamodel.md"
        ),
        rationale=(
            "The real XAS API does not exist yet, so ScenarioEngineSource fabricates the "
            "contract and XASApiSource is a documented stub. web.py calls the source host-side "
            "at session start and mounts the result into the sandbox as a file (the agent never "
            "calls XAS, never holds a credential). Flip XAS_DATA_SOURCE=xas and implement the "
            "response->contract mapping when a sample lands; nothing downstream changes."
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
    Decision(
        key="DECIDE-10",
        title="reserved_for_customer eligibility",
        default="ignored (a reserved vehicle is eligible for anyone) — DEFERRED, not in the minimal build",
        rationale=(
            "docs/xasdatamodel + the data-model diagram flag a reserved_for_customer field: a "
            "vehicle earmarked for a dealer should be eligible only for that dealer's orders. "
            "Not modelled in the minimal 30-customer build; when added, it becomes an extra "
            "term in the sparse-arc eligibility rule."
        ),
    ),
    Decision(
        key="DECIDE-13",
        title="Bumping an untouched order requires explicit planner authorization",
        default="never bump an untouched row unless the planner names who may be bumped (override 'bump')",
        rationale=(
            "By default the repair frees only disrupted rows, so an untouched order is never "
            "displaced. When a good fix requires bumping one, the agent must ASK the planner "
            "which orders/customers/POs may be bumped and compile the answer into the 'bump' "
            "filter; the solver then displaces one only if it lowers total cost (low-priority, "
            "not-already-rescheduled targets first). No uninvited bumps."
        ),
    ),
    Decision(
        key="DECIDE-12",
        title="Future vehicle = soft supply",
        default="a Future vehicle (VehicleClassification 'Future') is SOFT, free to re-allocate",
        rationale=(
            "Supply is ONE vehicle pool of real ∪ future vehicles. A Future vehicle is a "
            "not-yet-built car, so it is freely re-allocatable — a SOFT binding, "
            "BREAK_COST['soft']=0 (DECIDE-3). Once shipping info arrives it becomes a concrete "
            "vehicle (VehicleClassification 'Vehicle'), a REAL/HARD binding. There is no "
            "separate slot/qty-expansion step and no 'committed' flag — the classification is "
            "the whole distinction."
        ),
    ),
    Decision(
        key="DECIDE-14",
        title="Time-scale granularity: the resolution the solver reasons at",
        default="planner knob time_scale (days|weeks|months); round day-deltas UP; default days",
        rationale=(
            "A planner works at different horizons. time_scale sets the unit the solver "
            "measures every gap in: day-deltas are rounded UP to whole units (ceil) before "
            "costing, so differences finer than a unit collapse and coarser scales stop "
            "fussing over a few days. Round-up is strict — any lateness is at least one unit, "
            "so a coarse view never under-states lateness. The hard time fence (DECIDE-2) "
            "stays in real days — it is physical, not a reasoning lens. "
            "month = 30 days nominal (delta rounding, not calendar months). Default 'days' = "
            "today's behaviour exactly. SCALE_DAYS / DEFAULT_TIME_SCALE."
        ),
    ),
    Decision(
        key="DECIDE-15",
        title="Earliness penalty: how hard to discourage arriving too early",
        default="EARLY_WEIGHT = 0.15, linear; extreme earliness may lose to slight lateness (uncapped)",
        rationale=(
            "Only lateness used to be priced, so the solver grabbed wildly-early cars and sold "
            "them as wins. A linear, small early-side term (EARLY_WEIGHT · W(o) · early_units) "
            "makes a little early cheap and a lot early costly, while the convex lateness term "
            "always dominates for comparable magnitudes. Uncapped: a car months early CAN cost "
            "more than one a day late (tying a car up for months is real waste) — a documented "
            "crossover, not a bug. Earliness only; lateness stays strict (no late-side grace)."
        ),
    ),
    Decision(
        key="DECIDE-11",
        title="Reschedule fairness: how hard to protect an already-bumped order",
        default="GAMMA = 0.75 escalation on W(o) per times_rescheduled",
        rationale=(
            "times_rescheduled counts reschedules OUR repair loop caused (distinct from "
            "supply-side n_prior_delays). Folding it into the weight escalation makes a "
            "repeatedly-bumped order heavier, so the solver protects it from being delayed "
            "again and spreads the pain instead of hitting the same dealer every cycle. GAMMA "
            "tunes the strength; 0 disables it. In production the field is incremented on "
            "approved write-back and re-pulled, never mutated mid-session (the invariant)."
        ),
    ),
    Decision(
        key="DECIDE-16",
        title="Where the tenant taxonomy comes from: bundled in the skill vs mounted per session",
        default="bundled — index.md ships inside the xas-qa skill",
        rationale=(
            "There is exactly one tenant, so the taxonomy is static config and shipping it "
            "beside phrasebook.py beats uploading it on every session start. The price: the "
            "caller can no longer pick a dealership, and editing the taxonomy needs a "
            "setup_agent.py redeploy. A SECOND TENANT flips this back to a host-side mount at "
            "/workspace/reports/index.md — bundling every tenant's taxonomy is not the fix, "
            "since that shows each session all the others. See setup_agent.qa_bundle."
        ),
    ),
]


# --- Load-bearing defaults referenced by the solver / session -----------------
# These are the concrete values behind the decisions above. Change them HERE.

# Customer priority letter -> multiplicative weight on W(o) (§2).
PRIORITY_WEIGHT: dict[str, float] = {"A": 3.0, "B": 2.0, "C": 1.0}

# DECIDE-1
AGING_MODE = "additive"  # "additive" | "multiplicative"

# DECIDE-2  (tardiness measured in whole days from the promised date)
FROZEN_MAX_DAYS = 14
SLUSHY_MAX_DAYS = 42

# DECIDE-3  break cost: what it costs to move an allocation OFF its current
# binding, keyed on the binding's flavor. A real vehicle is HARD (expensive but
# movable); a future vehicle is SOFT (free to reshuffle). Two levels, no
# gradient — hardness derives from Unit.vehicle_classification, not location.
# BREAK_COST["hard"] is the load-bearing ratio (cost of breaking one hard
# allocation, in the same scaled units as weighted lateness); the planner can
# override it per session. STUB DEFAULT — the real "days-late worth one hard
# break" ratio needs sign-off.
BREAK_COST: dict[str, float] = {"hard": 200.0, "soft": 0.0}

# DECIDE-4 / DECIDE-8
# Large finite penalty used for soft instruction pins/forbids/defers. Big enough
# to dominate any legitimate lateness cost, small enough that the min-cost-flow
# stays well inside int range (no overflow) so a violated pin surfaces as a
# visible, comparable cost line instead of an outright infeasibility.
SOFT_PIN_COST = 1_000_000

# DECIDE-9
SOLVER_VERSION = "0.2.0-prototype"

# Cost-model coefficients (§2). Fixed formula, tunable coefficients.
CONVEX_EXPONENT = 1.5  # >1 so lateness never dumps entirely on one order
ALPHA = 0.5  # supply-side prior-delay escalation: (1 + ALPHA * n_prior_delays)
GAMMA = 0.75  # DECIDE-11 reschedule-fairness escalation: (+ GAMMA * times_rescheduled)
BETA = 0.1  # back-order aging per day (see AGING_MODE)

# DECIDE-15  earliness: price early arrivals so the solver stops grabbing
# needlessly-early cars. LINEAR and small, so the convex lateness term always
# dominates (a late car is never chosen over an on-time one to avoid earliness).
# A little early costs almost nothing; a lot early costs real money.
EARLY_WEIGHT = 0.15

# DECIDE-14  time-scale granularity: the resolution the solver reasons at. Day
# gaps (early/late) are rounded UP to whole units before costing, so differences
# finer than a unit collapse. Nominal days-per-unit — month = 30d nominal, NOT
# calendar months (we round day-deltas, never snap to a calendar boundary).
SCALE_DAYS: dict[str, int] = {"days": 1, "weeks": 7, "months": 30}
DEFAULT_TIME_SCALE = "days"  # no behaviour change unless the planner asks

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
