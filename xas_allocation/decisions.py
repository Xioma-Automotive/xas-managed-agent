"""Central registry of every `<<DECIDE>>` from the build spec.

Each one carries a DEFAULT, a rationale, and a STATUS, and all three are
surfaced at runtime via ``format_decisions()`` (printed at the top of a
``session.py`` run) so nothing is silently assumed.

Reviewed 2026-08-26. The statuses are not decoration — read them:

* ``SETTLED`` — decided. The mechanism is the answer, not a placeholder.
* ``SETTLED (mechanism) — VALUE tuned but unvalidated`` — the shape is decided,
  the NUMBER is not. No planner has ever seen these values (DECIDE-3, -15); they
  are tuned against the fabricated dataset alone and are reviewed at first real
  dealer data.
* ``RETIRED`` — the mechanism was built, reviewed and REMOVED. Kept in the
  register with what went wrong, because a decision that reads as merely absent
  invites someone to make it again.
* ``DEFERRED`` — recorded, deliberately not built.
* ``OPEN`` — still needs human sign-off. One left: DECIDE-5.

This file holds the DECISIONS, never the numbers. Every parameter the solver
prices with lives in ``solver_config.yaml``; a value quoted in a status line
below is a note about that config, not the thing the code reads.

Keeping them in ONE place is what makes them auditable rather than scattered
through the solver.
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
        status=(
            "RETIRED 2026-08-26 — the whole aging term is deleted, so there is nothing left to be additive or multiplicative. Not parked: rebuilt only if a real column ever carries an order's age."
        ),
        title="Aging term: additive vs multiplicative on the effective weight W(o)",
        default="deleted — W(o) is the planner's priority step and nothing else",
        rationale=(
            "W(o) used to read three escalation fields: n_prior_delays (supply-side "
            "history), days_backordered (queue fairness) and times_rescheduled "
            "(DECIDE-11). Every one is zero on every row of real data. The intended "
            "derivation of days_backordered — the order's age since it was entered — was "
            "never wired up, and only the fabricated generator ever set any of them, at "
            "random. A switch (additive vs multiplicative) on a term that is always zero "
            "is a knob on nothing, so the term, the switch and the three snapshot fields "
            "all went together."
        ),
    ),
    Decision(
        key="DECIDE-2",
        status=(
            "RETIRED 2026-08-26 — the fence is deleted outright and nothing replaces it. Removing it is what makes bump authorisation work."
        ),
        title="Time-fence boundaries (days from the pull date)",
        default="deleted — an order is protected by not being in the free set, not by a wall",
        rationale=(
            "The fence froze any order promised within 14 days whose car was on time, with "
            "a 15-42d middle band. Two faults. It fired BEFORE the authorisation check in "
            "partition, so it silently cancelled displacements a planner had explicitly "
            "authorised — three authorised bumps once no-oped for exactly this reason, the "
            "freed cars sitting idle while the rescue targets stayed fence-locked. And what "
            "it was believed to protect, a settled on-time order, is already protected by "
            "the free-set rule: such an order is never freed, so there is nothing to churn. "
            "The middle band also quietly gated the churn price, which is half of why that "
            "came back flat (DECIDE-11's sibling fault). Removing the fence leaves the "
            "no-bump default exactly as it was."
        ),
    ),
    Decision(
        key="DECIDE-3",
        status=(
            "MECHANISM RETIRED 2026-08-27 (the hard/soft split is gone) — ONE break_cost survives, VALUE tuned but unvalidated: no planner has seen it. Review at first real dealer data. (break_cost=200.0 in solver_config.yaml — the 'days-late worth one broken promise' ratio is the number a planner must own)"
        ),
        title="Break cost: what disturbing a kept promise costs",
        default="one break_cost in solver_config.yaml, charged only on an ON-TIME allocation",
        rationale=(
            "The COST stands and the SPLIT is gone. It used to be two numbers keyed on a "
            "real-vs-future binding read off the vehicle's status name (hard 200 for a car on "
            "the lot, soft 0 for one still coming). The export the pull now reads carries no "
            "such distinction: a car's status IS its allocation state (Available For Sale / "
            "Dealer Order Confirmation / Dealer Reservation), and the physical stage beside it "
            "(Sea Transit, Bonded, PDI, Future Vehicle) says where a shipment is, not what a "
            "promise costs to disturb. Timing is already carried by the arrival date, so the "
            "binding priced nothing the model did not already know. What remains: one "
            "break_cost, charged ONLY when the displaced order's car was arriving ON TIME. An "
            "already-late allocation protects nothing, so re-allocating a disrupted order is "
            "free — the break prices the bump VICTIM, not the disrupted order being rescued. "
            "It is NOT a wall: the repair loop may bump a kept promise 'for the sake of "
            "another' order, it just pays for it. And it is CONFIG, not steering — it left the "
            "override object on 2026-08-26 because it is a constant somebody exposed per "
            "session, not a sentence a planner says. (Supersedes the retired committed-vehicle "
            "hard wall and the location gradient the real API could never support.)"
        ),
    ),
    Decision(
        key="DECIDE-4",
        status=(
            "RETIRED 2026-08-26 — there are no instruction pins left to choose a mechanism for. What remains is pre-commit, and only pre-commit."
        ),
        title="Pin mechanism: pre-commit-arc vs infinite-cost",
        default="pre-commit — an order that may not move is excluded from the graph",
        rationale=(
            "Both mechanisms this decision chose between are gone. The finite-penalty side "
            "priced the soft instruction pin ('do not deliver this before 14 September'), "
            "cut on 2026-08-26: it was the only price on an INSTRUCTION rather than an "
            "outcome, and deferring an order never moved its promised date, so a deferred "
            "order paid a lateness charge and a pin charge at once. Pushing an order back "
            "is a NEW PROMISED DATE, priced correctly by the lateness and earliness terms "
            "with no extra machinery. The pre-commit side priced the frozen fence "
            "(DECIDE-2), also gone. What is left needs no decision: `may_move.never` and "
            "any order outside the free set are simply not in the graph."
        ),
    ),
    Decision(
        key="DECIDE-5",
        status=(
            "OPEN — reaffirmed open 2026-08-25. The override still lives only in the conversation; a host-side store in web.py remains the candidate fix, undecided. Verify the Managed Agents persistence + mid-session-steering surface against current docs before wiring anything."
        ),
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
        status=(
            "SETTLED 2026-08-25 — NOT APPLICABLE, no liveness check. The allocation pull happens host-side before the session exists, so a session-start call from the agent proves nothing about it; a fetch failure surfaces at mount time instead. The reporting lane's MCP liveness shows up as a failed tool call. The no-op step is removed from the skill."
        ),
        title="xas-code MCP liveness-check pattern",
        default="single directory_tree call at session start (prototype: skipped, synthetic data)",
        rationale="Match Olga's standing xas-code liveness pattern once MCP is wired.",
    ),
    Decision(
        key="DECIDE-7",
        status=(
            "SETTLED 2026-08-27 — the source is the EXPORT's two CSVs, not the app MCP. Nothing is blocked on anyone else any more."
        ),
        title="Where the allocation data comes from",
        default=(
            "a scenario directory of the real export: orders.csv + vehicles.csv, read and "
            "translated host-side by datasource.translate into two mounted JSON payloads"
        ),
        rationale=(
            "The app MCP was the source for a week and is not one now. It PROJECTED — an "
            "allowlisted subset of each record — and the fields the solver needs were not on "
            "the list: no `jobitems` (the grain itself), so every dev job card dropped for "
            "no_car_line and the live pull came back EMPTY, and it asked for DueDate where XAS "
            "stores DueDateTime. Rather than wait on a widened projection, the pull reads the "
            "export we already have: `scenario_engine/real_*.py` carves a scenario out of it, "
            "and one mapping (`datasource.translate`) turns the two row streams into the two "
            "payloads `flatten` reads in the sandbox. What the mapping encodes: one ORDER ROW "
            "is one order for one car, keyed by its own OrderId (no cards, no lines, no "
            "Quantity); the promise is the ORDER's etaDealer and the arrival is the CAR's "
            "availableBy; eligibility is SalesModel equality, never modelId.code; free supply "
            "is the car's own status, stripped ('Available For Sale ' carries a real trailing "
            "space); the disruption is DERIVED (nothing records a delay manifest); a car "
            "claimed by two orders yields no allocation for either. "
            "`python -m datasource --census` prints the funnel. Still host-side, still mounted "
            "as files: the agent never reads a CSV and never holds a credential. The MCP tools "
            "the agent holds are the REPORTING lane's, and that has not changed."
        ),
    ),
    Decision(
        key="DECIDE-8",
        status=("SETTLED 2026-08-26 — restated: the only large finite cost left is no_car_cost."),
        title="Infeasibility strategy",
        default="large finite costs, never hard walls — the solver always returns a plan",
        rationale=(
            "Nothing in the cost model is infinite, so the flow is always feasible and a "
            "book with more orders than cars comes back as a plan naming who is unplaceable "
            "rather than as a crash. Since the instruction pins went (DECIDE-4), the one "
            "cost carrying this is `no_car_cost` (10,000,000 in solver_config.yaml): high "
            "enough that a late car always beats no car, finite so 'no car' stays a "
            "reportable outcome. CP-SAT assumption-literal minimal conflict sets are the "
            "more-honest upgrade, deferred with the CP-SAT escape hatch."
        ),
    ),
    Decision(
        key="DECIDE-9",
        status=(
            "SETTLED 2026-08-25 — stays in-repo under xas_allocation/, shipped in the skill bundle. Extraction to a version-pinned repo is triggered by the FIRST NON-DEV TENANT, not by a date; SOLVER_VERSION is the pin point."
        ),
        title="Solver repo location + versioning",
        default="reference solver lives in-repo under xas_allocation/; solver_config.yaml pins the version",
        rationale=(
            "Spec §10: the reference copy lives in the skill for day-one. Canonical version "
            "moves to a tested repo before real dealer data; the skill then pins a version. "
            "The config's `version` is that pin point."
        ),
    ),
    Decision(
        key="DECIDE-10",
        status=(
            "DEFERRED 2026-08-25 — record corrected, nothing built. Earmarked supply (a Reserved-for-X car eligible only for X) stays out of the minimal build."
        ),
        title="reserved_for_customer eligibility",
        default="a reserved vehicle is out of the pool entirely — eligible for NO ONE",
        rationale=(
            "Corrected 2026-08-25: this used to read 'ignored — eligible for anyone', which "
            "described behaviour the mapper does not have. `Reserved-*` is one of the statuses "
            "datasource.py drops as out_of_scope_status (DECIDE-3), so an earmarked car is not "
            "supply for anybody, including the dealer it is earmarked for. XAS carries the "
            "reserved-for-customer notion as that status, NOT the `IsReserved` flag "
            "(`docs/real-source-investigation.md` §2). Modelling it as EARMARKED SUPPLY — a "
            "Reserved-for-X vehicle eligible only for X's orders — is the deferred upgrade: it "
            "widens supply and needs the reserved-to-account link to resolve, and it becomes an "
            "extra term in the sparse-arc eligibility rule."
        ),
    ),
    Decision(
        key="DECIDE-13",
        status=("SETTLED 2026-08-25"),
        title="Bumping an untouched order requires explicit planner authorization",
        default="never bump an untouched row unless the planner names who may be bumped (override 'may_move.also')",
        rationale=(
            "By default the repair frees only rows that need help — late, or with no car — "
            "so an untouched order is never displaced. When a good fix requires bumping one, "
            "the agent must ASK the planner who may be bumped and compile the answer into "
            "`may_move.also`; the solver then displaces one only if it lowers total cost, "
            "paying break_cost for the disturbed promise. No uninvited bumps. Renamed from "
            "'bump' on 2026-08-26 when the three who-may-move keys merged into one: it is "
            "still the ONE place permission to displace is granted, and it is now the only "
            "key that can widen the set at all."
        ),
    ),
    Decision(
        key="DECIDE-12",
        status=("SETTLED 2026-08-25"),
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
        status=(
            "RETIRED 2026-08-26 — cut unbuilt-for: no planner ever asked for it. Confirmed with Olga before removal."
        ),
        title="Time-scale granularity: the resolution the solver reasons at",
        default="deleted — the solver measures in exact days, and the report speaks days",
        rationale=(
            "A planner works at different horizons. time_scale sets the unit the solver "
            "measures every gap in: day-deltas are rounded UP to whole units (ceil) before "
            "costing, so differences finer than a unit collapse and coarser scales stop "
            "fussing over a few days. Round-up is strict — any lateness is at least one unit, "
            "so a coarse view never under-states lateness. The hard time fence (DECIDE-2) "
            "stays in real days — it is physical, not a reasoning lens. "
            "month = 30 days nominal (delta rounding, not calendar months). What it cost to "
            "keep: a rounding helper, a resolver, an extra argument threaded through the "
            "cost function, duration phrasing in every report and a 110-line test file — to "
            "stop the solver distinguishing three days from six. Rebuild it the day a "
            "planner asks to think in whole weeks."
        ),
    ),
    Decision(
        key="DECIDE-15",
        status=(
            "SETTLED 2026-08-25 (mechanism) — VALUE tuned but unvalidated: no planner has seen it. Review at first real dealer data. (early_weight=0.15 in solver_config.yaml)"
        ),
        title="Earliness penalty: how hard to discourage arriving too early",
        default="early_weight = 0.15, linear; extreme earliness may lose to slight lateness (uncapped)",
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
        status=(
            "RETIRED 2026-08-26 — deleted with the rest of the weight escalation (DECIDE-1). It never fired on anything real."
        ),
        title="Reschedule fairness: how hard to protect an already-bumped order",
        default="deleted — no term tracks how often an order has been rescheduled",
        rationale=(
            "The intent was sound: an order this system has already rescheduled gets "
            "heavier, so the next slip lands on someone else. The mechanism never ran. "
            "times_rescheduled was only ever written by a random draw in the fabricated "
            "generator — the repair loop does not record a reschedule, and there is no "
            "approved write-back to increment it — so the term was zero on every real row "
            "from the day it was written. Rebuilding it needs the write-back first; the "
            "field is the last step, not the first."
        ),
    ),
    Decision(
        key="DECIDE-16",
        status=(
            "SETTLED 2026-08-25 — bundled. A SECOND TENANT is the trigger that flips it back to a host-side mount."
        ),
        title="Where the tenant taxonomy comes from: bundled in the skill vs mounted per session",
        default="bundled — index.md ships inside the xas-reporting skill",
        rationale=(
            "There is exactly one tenant, so the taxonomy is static config and shipping it "
            "beside phrasebook.py beats uploading it on every session start. The price: the "
            "caller can no longer pick a dealership, and editing the taxonomy needs a "
            "setup_agent.py redeploy. A SECOND TENANT flips this back to a host-side mount at "
            "/workspace/reports/index.md — bundling every tenant's taxonomy is not the fix, "
            "since that shows each session all the others. See setup_agent.reporting_bundle."
        ),
    ),
]


# The numbers are NOT here. Every parameter the solver prices with lives in
# `solver_config.yaml` beside this file and is read by `solver.py` alone. This
# module is the register of what was decided and why; putting a default back
# here would give the same number two homes and one of them would go stale.


def format_decisions() -> str:
    """Human-readable dump of every decision, its default and its status.

    Printed at the top of a session run so no assumption is silent. OPEN,
    value-unvalidated and RETIRED entries are counted in the header — a reader
    who stops at the first line still learns how much is genuinely decided, and
    how much was built and taken back out.
    """
    open_keys = [d.key for d in DECISIONS if d.status.startswith("OPEN")]
    unvalidated = [d.key for d in DECISIONS if "VALUE tuned but unvalidated" in d.status]
    retired = [d.key for d in DECISIONS if d.status.startswith("RETIRED")]
    lines = [
        (
            f"DECISION REGISTER ({len(DECISIONS)} entries): "
            f"{len(open_keys)} OPEN ({', '.join(open_keys) or 'none'}); "
            f"{len(unvalidated)} settled in shape but carrying an UNVALIDATED VALUE "
            f"({', '.join(unvalidated) or 'none'}); "
            f"{len(retired)} RETIRED — built, then removed "
            f"({', '.join(retired) or 'none'})."
        )
    ]
    for d in DECISIONS:
        lines.append(f"  [{d.key}] {d.title}")
        lines.append(f"      default : {d.default}")
        lines.append(f"      why     : {d.rationale}")
        lines.append(f"      status  : {d.status}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_decisions())
