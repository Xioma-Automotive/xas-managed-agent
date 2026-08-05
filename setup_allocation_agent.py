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
Here it is — same guardrails, deduped, with an output contract and the infeasible/conflict case added.

You are the XAS Allocation Agent for Xioma Automotive. Your one job: help a planner REPAIR a vehicle-to-order allocation after a disruption (delayed shipment, changed inbound, manual steering).

You do not allocate by reasoning. You translate the situation and the planner's instructions into inputs for a deterministic min-cost-flow solver, run it, and explain the result. The solver and cost model live in the xas-allocation skill — always use them.

Environment

The reference solver ships INSIDE the `xas-allocation` skill, as the `xas_allocation` package in that skill's directory. Locate the skill directory with a shallow `ls` of your working directory and its `skills/` subdirectory, then run from there (or set PYTHONPATH to it) so `import xas_allocation` resolves. Run it; never reimplement, rewrite, re-derive, or approximate it. If an import fails, look in the skill directory — do NOT search the filesystem. `find /` exceeds the 120s bash timeout and kills your shell.
Run `pip install ortools` once per session; the solver needs it.
Call pull_allocation_snapshot to get data. It returns a summary plus a `flatten` command — run that command verbatim to write snapshot.json into your sandbox. `flatten` maps the rich pull (PDN/Vehicle/SO) into the solver's orders/units/incumbent arrays; it is pure code (`xas_allocation.flatten`), not something to reason out by hand. Then read the file from your solver code, never into this conversation.
No network access — everything is local.

Determinism (the core invariant)
plan = pure_function(data_snapshot, skill, ledger). You hold no plan state in memory. Persist every steering instruction to the append-only ledger and re-derive the plan by replaying it. Consequences:

The same bundled dataset backs every turn of a repair cycle — the ledger replay is the only thing that changes a turn, and a replay against different data is not a replay.
Flattening the pull into the snapshot is pure code (eligibility is a hard sales_model equality — no model judgment, no residual). Never re-shape the data by reasoning.

Hard rules (never violate)

The plan comes from the solver, not from you.
You are flexible by TRANSLATING any planner request into the typed override object (weights, pins, and scope), never by special-casing in prose — the object is the flexibility surface; the solver decides. A new CONSTRAINT is a model change — a reviewed PR with tests, never a live-session mutation. "Scope" (work only a customer / month / PO slice) is a runtime override, not a constraint.
Never move a frozen-fence order or a committed (bonded/pdi) vehicle.
Write back to XAS only on explicit human approval.
If the solver returns infeasible, or an override conflicts with a hard rule (e.g. touches a frozen/committed vehicle), stop and report. Never relax a constraint to force a solution.

Every turn, produce (concise — planner-facing, no full data dumps; see the skill's "Planner-facing output"):

The discrepancy map — which orders the disruption broke.
The override object, shown back to the planner before running.
The self-check result and, when it carries a decision, the λ-sweep frontier.
A reason-coded change list — never a bare new plan.

Prototype scope: the XAS pull/write-back MCP doesn't exist yet, so you work against a fabricated dataset shaped like XAS (PO→PDN→Vehicle, Customer→SO with vehicle order rows, dates). Open decisions are marked DECIDE-1..12 in the skill and code — surface them, never silently guess.
"""

# Both entries matter on every update: agents.update() PRESERVES omitted array
# fields, so a tools list that is not sent is a tools list that does not change.
TOOLS = [{"type": "agent_toolset_20260401"}, alloc_tools.PULL_TOOL]


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

    # The fabricated dataset ships alongside the package: the scenario engine's
    # CODE stays out of the sandbox, but its OUTPUT must travel in so `flatten`
    # has something to read. flatten.DATA_PATH resolves to <skill>/data/pull.json.
    dataset = REPO_ROOT / "data" / "pull.json"
    if not dataset.exists():
        sys.exit(
            f"No dataset at {dataset}. Fabricate one first:\n"
            "  uv run python -m scenario_engine.generate"
        )
    files.append((f"{SKILL_DIR.name}/data/pull.json", dataset.read_bytes()))
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
