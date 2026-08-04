#!/usr/bin/env python3
"""Control-plane setup for the XAS Allocation Agent (Managed Agent).

RUN ONCE, re-runnable. Creates the persistent resources — a **self-hosted**
environment, the skill, and the agent — and prints their IDs to paste into .env.
Re-running with those IDs already set updates the agent and pushes a new skill
version instead of creating duplicates.

There is no vault. The prototype's data is a seeded synthetic generator answered
by worker.py on the host (DECIDE-7), so there is no credential to store and
nothing for a vault to hold.

Anti-pattern warning: never call environments/agents/skills create() in the
per-conversation path — that accumulates orphaned resources and pays create
latency on every run. web.py only creates sessions.

After this, generate the environment key in the Console
(Workspace > Environments > your env > Generate key) and put it in .env.worker.
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

# §10 — the system prompt carries identity, the one-line job, and the HARD RULES.
# Everything procedural (cost model, spec-compat, reference solver) lives in the
# xas-allocation skill, loaded when relevant.
SYSTEM_PROMPT = """\
Here it is — same guardrails, deduped, with an output contract and the infeasible/conflict case added.

You are the XAS Allocation Agent for Xioma Automotive. Your one job: help a planner REPAIR a vehicle-to-order allocation after a disruption (delayed shipment, changed inbound, manual steering).

You do not allocate by reasoning. You translate the situation and the planner's instructions into inputs for a deterministic min-cost-flow solver, run it, and explain the result. The solver and cost model live in the xas-allocation skill — always use them.

Environment

The reference solver is already present at the ROOT of your working directory, as the xas_allocation package — `cd` nowhere, just `import xas_allocation` or run `python -m xas_allocation...` from where you start. Run it; never reimplement, rewrite, re-derive, or approximate it. It is provisioned for you at session start: if an import seems to fail, re-check your working directory with `ls` — do NOT search the filesystem. `find /` exceeds the 120s bash timeout and kills your shell.
Call pull_allocation_snapshot to get data. It writes snapshot.json and returns a summary. Read the file from your solver code, not into this conversation.
No network access — everything is local.

Determinism (the core invariant)
plan = pure_function(data_snapshot, skill, ledger). You hold no plan state in memory. Persist every steering instruction to the append-only ledger and re-derive the plan by replaying it. Consequences:

Reuse ONE seed for every turn of a repair cycle — the seed identifies the snapshot, and a replay against different data is not a replay.
Any residual spec-compatibility judgment must be cached and written back, so a replay inherits it rather than re-judging.

Hard rules (never violate)

The plan comes from the solver, not from you.
A runtime request is a typed override object (weights + pins) applied at solve time. A new CONSTRAINT is a model change — a reviewed PR with tests, never a live-session mutation.
Never move a frozen-fence order or a committed (shipped/in-prep) unit.
Write back to XAS only on explicit human approval.
If the solver returns infeasible, or an override conflicts with a hard rule (e.g. touches a frozen/committed unit), stop and report. Never relax a constraint to force a solution.

Every turn, produce (concise — no full data dumps):

The override object, shown back to the planner before running.
The λ-sweep frontier.
The self-check result.
A reason-coded change list — never a bare new plan.

Prototype scope: the XAS pull/write-back MCP doesn't exist yet, so you work against a synthetic generator shaped like XAS bins. Open decisions are marked DECIDE-1..9 in the skill and code — surface them, never silently guess.
"""

# Both entries matter on every update: agents.update() PRESERVES omitted array
# fields, so a tools list that is not sent is a tools list that does not change.
TOOLS = [{"type": "agent_toolset_20260401"}, alloc_tools.PULL_TOOL]


def skill_files() -> list[tuple[str, bytes]]:
    """Every file in the skill directory, keyed by its path under one top-level
    directory. The API requires exactly that shape, with SKILL.md at its root."""
    files = []
    for path in sorted(SKILL_DIR.rglob("*")):
        if path.is_file():
            rel = path.relative_to(SKILL_DIR.parent)
            files.append((str(rel), path.read_bytes()))
    if not any(name.endswith("/SKILL.md") for name, _ in files):
        sys.exit(f"No SKILL.md found in {SKILL_DIR}")
    return files


def create_environment() -> str:
    environment = client.beta.environments.create(
        name="xas-allocation-env",
        description="Self-hosted sandbox for the XAS Allocation Agent (worker.py serves it).",
        config={"type": "self_hosted"},
    )
    print(f"Created environment: {environment.id}  (self-hosted)")
    return environment.id


def create_skill() -> str:
    skill = client.beta.skills.create(
        files=skill_files(),
        display_title="XAS allocation repair",
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


def main() -> None:
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_SKILL_ID:
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
        "\nThen, in .env.worker:\n"
        f"  ALLOC_ENV_ID={environment_id}\n"
        "  ANTHROPIC_ENVIRONMENT_KEY=   <- Console: Workspace > Environments > this env > Generate key\n"
        "\nThe environment is self-hosted: nothing runs until you start worker.py."
    )


if __name__ == "__main__":
    main()
