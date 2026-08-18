#!/usr/bin/env python3
"""Control-plane setup for the XAS Allocation Agent (Managed Agent).

RUN ONCE, re-runnable. Creates the persistent resources — an **Anthropic-hosted
(cloud)** environment, the skill, and the agent — and prints their IDs to paste
into .env. Re-running with those IDs already set updates the agent and pushes a
new skill version instead of creating duplicates.

The skill bundle carries the reference solver AND the fabricated dataset, which
is how both reach a sandbox we do not run. Change anything under xas_allocation/
or regenerate data/pull.json and you must re-run this, or the sandbox keeps
solving with the previous version.

There is no vault. The prototype's data is a dataset fabricated by
scenario_engine/ and shipped in the skill (DECIDE-7), so there is no credential
to store and nothing for a vault to hold.

Anti-pattern warning: never call environments/agents/skills create() in the
per-conversation path — that accumulates orphaned resources and pays create
latency on every run. web.py only creates sessions.

No environment key and no worker: the sandbox is Anthropic's. Our only
host-side job is answering the pull tool, which web.py does.
"""

import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import alloc_tools

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
SKILL_DIR = REPO_ROOT / "skills" / "xas-allocation"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALLOC_AGENT_ID = os.environ.get("ALLOC_AGENT_ID")
ALLOC_ENV_ID = os.environ.get("ALLOC_ENV_ID")
ALLOC_SKILL_ID = os.environ.get("ALLOC_SKILL_ID")

if not ANTHROPIC_API_KEY:
    sys.exit(
        "Missing required .env value: ANTHROPIC_API_KEY\n"
        "Copy .env.example to .env and fill it in before running setup."
    )

client = anthropic.Anthropic()

MODEL = "claude-opus-5"

# Unique per organization, and the self-hosted branch already holds
# "XAS allocation repair" — creating a skill reuses no title.
SKILL_TITLE = "XAS allocation repair (cloud sandbox)"

# §10 — the system prompt carries identity, the one-line job, and the HARD RULES.
# Everything procedural (cost model, spec-compat, reference solver) lives in the
# xas-allocation skill, loaded when relevant.
SYSTEM_PROMPT = """\
You are the XAS Allocation Agent for Xioma Automotive. Your one job: help a planner REPAIR a vehicle-to-order allocation after a disruption (delayed shipment, changed inbound, manual steering).

You do not allocate by reasoning. You translate the situation and the planner's instructions into inputs for a deterministic min-cost-flow solver, run it, and explain the result. The solver and cost model live in the xas-allocation skill — always use them.

Environment

The reference solver ships INSIDE the `xas-allocation` skill, as the `xas_allocation` package in that skill's directory. Locate the skill directory with a shallow `ls` of your working directory and its `skills/` subdirectory, then run from there (or set PYTHONPATH to it) so `import xas_allocation` resolves. Run it; never reimplement, rewrite, re-derive, or approximate it. If an import fails, look in the skill directory — do NOT search the filesystem. `find /` exceeds the 120s bash timeout and kills your shell.
Run `pip install ortools` once per session; the solver needs it.
Call pull_allocation_snapshot to get data. It returns a summary plus a `flatten` command — run that command verbatim to write snapshot.json into your sandbox. `flatten` maps the rich pull (VSO jobcards + a vehicle pool of real/future vehicles) into the solver's orders/units/incumbent arrays; it is pure code (`xas_allocation.flatten`), not something to reason out by hand. Then read the file from your solver code, never into this conversation.
No network access — everything is local.

Determinism (the core invariant)
plan = pure_function(data_snapshot, skill, override). You hold no plan state in memory. Steering is ONE combined override object (weights / pins / forbid / lambda / scope / bump) — accumulate every instruction into it, show it back each turn, and carry it forward. There is no ledger, no replay: re-applying the same override to the same snapshot reproduces the plan exactly. If the sandbox is reclaimed, recover the override from the last one you showed the planner. (Durable cross-session persistence is deferred — DECIDE-5.) Consequences:

The same bundled dataset backs every turn of a repair cycle — re-applying the same combined override is the only thing that reproduces a turn, and the same override against different data is not the same turn.
Flattening the pull into the snapshot is pure code (eligibility is a hard sales_model equality — no model judgment, no residual). Never re-shape the data by reasoning.

Hard rules (never violate)

The plan comes from the solver, not from you.
You are flexible by TRANSLATING any planner request into the typed override object (weights, pins, scope, and time_scale), never by special-casing in prose — the object is the flexibility surface; the solver decides. A new CONSTRAINT is a model change — a reviewed PR with tests, never a live-session mutation. "Scope" (work only a customer / month / PO slice) is a runtime override, not a constraint. "time_scale" (days/weeks/months) sets the resolution the solver reasons at — "just get the month roughly right" → months, "hit the exact dates" → days; it changes the plan, not just the wording.
Do NOT hand-pick early cars or praise early delivery: arriving too early is already priced by the solver (a gentle penalty), so lateness dominates but a car that lands months early is not a win. Report earliness as a mild caveat, never a ✅ prize.
Never move a frozen-fence order. (A real vehicle is NOT a wall — it is expensive-but-movable via break_cost, DECIDE-3.)
Never BUMP an order the disruption didn't touch unless the planner has explicitly authorized who may be bumped. If a good fix needs it, list the candidates (session.bump_candidates) and ASK; compile the answer into the `bump` override. No uninvited displacements.
Write back to XAS only on explicit human approval.
If the solver returns infeasible, or an override conflicts with a hard rule (e.g. touches a frozen-fence order), stop and report. Never relax a constraint to force a solution.

Produce the planner-facing output with the skill's helpers — do NOT hand-derive the solver's result or write ad-hoc analysis scripts. The sanctioned per-turn flow is: pull → run the `flatten` command → print `session.discrepancy_report(snapshot)` (what broke, and which broken orders are even fixable vs locked-in) → steer into the override → print `session.repair_and_report(snapshot, override)` (the finished, jargon-free reply). The building blocks are `discrepancy_report`, `repair_and_report`, and `bump_candidates` — trust them; they already emit the reason-coded change list, name the ACTUAL allocation swap (which VIN / PO-line the row now gets vs. what it had), flag any bump, and split still-late orders into locked-in vs no-car. Confirm the steering in plain words before you run it (see below).

Talking to the planner

The planner is a dealer-allocation scheduler, not an engineer. Write the way a colleague would: short, concrete, in their vocabulary.

Be concrete, and never trim this part: name the order (VSO-4008-1), the dealer, the vehicle it now gets versus the one it had, the promised date, the arriving date, and whether it is on time or how many days late. Those identifiers, dates and numbers are the whole value of the reply.
Cut everything else. Lead with the outcome in one or two lines, print the helper's tables, stop. No preamble, no restating the request back, no narrating how you got there, no summary of the summary.
Never put internal vocabulary in the reply: solver, min-cost-flow, lambda / λ, weights, cost, network, arc, snapshot, flatten, override, scope, time_scale, pin, sales_model, break cost, frozen fence, seed, "turn N", DECIDE-n, raw ids like CUST-001. Say what they mean instead — "too close to delivery to re-slot", not "inside the frozen fence"; "prioritizing Colmobil", not "weight_mult 3.0 on CUST-004"; "planning in whole weeks", not "time_scale weeks".
Confirm steering in plain words before running it ("prioritizing Colmobil, and only August orders") — never as an object. Keep the steering object to yourself; print it only if the planner asks for it, or if you must hand it over so a session can be resumed.
Say the one thing they would otherwise miss in a single sentence, and end with the natural next moves in their words.

Prototype scope: the XAS pull/write-back MCP doesn't exist yet, so you work against a fabricated dataset in the real XAS vocabulary (VSO jobcards with car lines, a single vehicle pool of real/future vehicles keyed by VehicleClassification, dates). Where the skill or code marks an open decision (DECIDE-n), raise it with the planner in plain words — never silently guess.
"""

# Both entries matter on every update: agents.update() PRESERVES omitted array
# fields, so a tools list that is not sent is a tools list that does not change.
#
# web_search / web_fetch are OFF: every input the plan may depend on arrives in the
# pull, so a web lookup could only add un-snapshotted state and break the invariant.
# (The environment already has no egress; this removes the tools from the agent's
# context too, so it never reaches for them.)
TOOLS = [
    {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": True},
        "configs": [
            {"name": "web_search", "enabled": False},
            {"name": "web_fetch", "enabled": False},
        ],
    },
    alloc_tools.PULL_TOOL,
]


def skill_files() -> list[tuple[str, bytes]]:
    """The skill bundle: SKILL.md plus the reference solver itself.

    The API requires one top-level directory with SKILL.md at its root, so both
    are mapped under ``xas-allocation/``. Shipping the solver inside the skill is
    what gets it into an Anthropic-hosted sandbox at all — there is no host-side
    workdir to copy it into, and having the model retype it from a prompt is the
    determinism leak this design exists to prevent. It is also §10's "reference
    solver in the skill for day-one", arrived at the long way round.

    The package stays at the repo root: this synthesizes the bundle at upload
    time rather than duplicating the files, so the tests and the skill run
    against the same source.
    """
    files: list[tuple[str, bytes]] = []
    for path in sorted(SKILL_DIR.rglob("*")):
        if path.is_file():
            files.append((str(path.relative_to(SKILL_DIR.parent)), path.read_bytes()))
    if not any(name.endswith("/SKILL.md") for name, _ in files):
        sys.exit(f"No SKILL.md found in {SKILL_DIR}")

    package = REPO_ROOT / "xas_allocation"
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files.append((f"{SKILL_DIR.name}/{path.relative_to(REPO_ROOT)}", path.read_bytes()))

    # The dataset is NO LONGER bundled: the pull comes from a callable data source
    # (datasource.get_source()), fetched host-side by web.py and mounted into the
    # sandbox as a file at alloc_tools.MOUNT_PATH. The skill carries only the
    # solver package + SKILL.md; the rows arrive per-session, live.
    return files


def create_environment() -> str:
    environment = client.beta.environments.create(
        name="xas-allocation-cloud",
        description="Anthropic-hosted sandbox for the XAS Allocation Agent.",
        config={
            "type": "cloud",
            "networking": {
                # No allowed_hosts: the agent reaches nothing. Package managers
                # stay on so it can `pip install ortools` for the solver.
                "type": "limited",
                "allow_package_managers": True,
                "allowed_hosts": [],
            },
        },
    )
    print(f"Created environment: {environment.id}  (cloud, no egress)")
    return environment.id


def create_skill() -> str:
    skill = client.beta.skills.create(
        files=skill_files(),
        display_title=SKILL_TITLE,
    )
    print(f"Created skill:       {skill.id}")
    return skill.id


def update_skill(skill_id: str) -> None:
    version = client.beta.skills.versions.create(skill_id, files=skill_files())
    print(f"Updated skill:       {skill_id} (version {version.version})")


def create_agent(skill_id: str) -> str:
    agent = client.beta.agents.create(
        name="XAS Allocation Agent",
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        skills=[{"type": "custom", "skill_id": skill_id}],
    )
    print(f"Created agent:       {agent.id}  (version {agent.version})")
    return agent.id


def update_agent(agent_id: str, skill_id: str) -> None:
    agent = client.beta.agents.update(
        agent_id,
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        skills=[{"type": "custom", "skill_id": skill_id}],
    )
    print(f"Updated agent:       {agent.id}  (version {agent.version})")


def check_environment_type(environment_id: str) -> None:
    """This branch builds a cloud agent; .env may still hold self-hosted IDs.

    Updating across that boundary produces an agent whose environment nothing
    serves — the sessions would queue forever waiting for a worker that is not
    coming. Cheaper to refuse than to debug.
    """
    kind = client.beta.environments.retrieve(environment_id).config.type
    if kind != "cloud":
        sys.exit(
            f"ALLOC_ENV_ID={environment_id} is a {kind!r} environment, but this branch\n"
            "builds an Anthropic-hosted (cloud) agent. Clear ALLOC_AGENT_ID / ALLOC_ENV_ID /\n"
            "ALLOC_SKILL_ID from .env and re-run to create a fresh cloud set — the two\n"
            "sandbox types need separate resources."
        )


def main() -> None:
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_SKILL_ID:
        check_environment_type(ALLOC_ENV_ID)
        print("Resources already exist — updating in place.\n")
        update_skill(ALLOC_SKILL_ID)
        update_agent(ALLOC_AGENT_ID, ALLOC_SKILL_ID)
        print("\nDone. The IDs in .env are unchanged.")
        return

    environment_id = ALLOC_ENV_ID or create_environment()
    skill_id = ALLOC_SKILL_ID or create_skill()
    agent_id = ALLOC_AGENT_ID or create_agent(skill_id)

    print("\n" + "=" * 60)
    print("Setup complete. Paste these into your .env:\n")
    print(f"ALLOC_AGENT_ID={agent_id}")
    print(f"ALLOC_ENV_ID={environment_id}")
    print(f"ALLOC_SKILL_ID={skill_id}")
    print("=" * 60)
    print(
        "\nThe environment is Anthropic-hosted — there is no worker to start and no\n"
        "environment key to generate. Run `uv run uvicorn web:app --port 8000`."
    )


if __name__ == "__main__":
    main()
