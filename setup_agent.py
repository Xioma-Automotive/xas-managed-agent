#!/usr/bin/env python3
"""Control-plane setup for the XAS Agent (Managed Agent).

ONE agent, TWO skills. Specialisation lives in the skills, not in separate agent
objects: `xas-allocation` drives the deterministic solver, `xas-qa` answers
reporting questions over the mounted job-card records. The API allows 20 skills
per agent; we use 2.

The agent is the one that already exists (ALLOC_AGENT_ID). This script updates it
in place to carry the second skill — it does not create a new one.

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
ALLOC_SKILL_DIR = REPO_ROOT / "skills" / "xas-allocation"
QA_SKILL_DIR = REPO_ROOT / "skills" / "xas-qa"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALLOC_AGENT_ID = os.environ.get("ALLOC_AGENT_ID")
ALLOC_ENV_ID = os.environ.get("ALLOC_ENV_ID")
ALLOC_SKILL_ID = os.environ.get("ALLOC_SKILL_ID")
QA_SKILL_ID = os.environ.get("QA_SKILL_ID")

# The credential check and the client are deliberately NOT module-level: the
# prompt, the tool list and the skill bundles are the agent's contract, and
# tests/test_agent_contract.py pins them. Importing this module must therefore
# work with no API key and no network, exactly like the rest of the suite.
_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            sys.exit(
                "Missing required .env value: ANTHROPIC_API_KEY\n"
                "Copy .env.example to .env and fill it in before running setup."
            )
        _client = anthropic.Anthropic()
    return _client


AGENT_NAME = "XAS Agent"
MODEL = "claude-opus-5"

# Unique per organization, and the self-hosted branch already holds
# "XAS allocation repair" — creating a skill reuses no title.
ALLOC_SKILL_TITLE = "XAS allocation repair (cloud sandbox)"
QA_SKILL_TITLE = "XAS terminology resolution (reporting)"

# §10 — the system prompt carries identity, the one-line job, and the HARD RULES.
# Everything procedural (cost model, spec-compat, reference solver) lives in the
# xas-allocation skill, loaded when relevant.
SYSTEM_PROMPT = """\
You are the XAS Agent for Xioma Automotive. You do two jobs for dealership staff:

1. ALLOCATION REPAIR — help a planner repair a vehicle-to-order allocation after a disruption (delayed shipment, changed inbound, manual steering). Driven by the `xas-allocation` skill.
2. REPORTING — answer questions about the dealership's job-card records (how many, which branch, what status) and draw charts. Driven by the `xas-qa` skill.

Read what was asked and use the matching skill. The hard rules below apply to both, and the first one is what keeps the two jobs from contaminating each other.

You do not allocate by reasoning. You translate the situation and the planner's instructions into inputs for a deterministic min-cost-flow solver, run it, and explain the result. The solver and cost model live in the xas-allocation skill — always use them.

Environment

The reference solver ships INSIDE the `xas-allocation` skill, as the `xas_allocation` package in that skill's directory. Locate the skill directory with a shallow `ls` of your working directory and its `skills/` subdirectory, then run from there (or set PYTHONPATH to it) so `import xas_allocation` resolves. Run it; never reimplement, rewrite, re-derive, or approximate it. If an import fails, look in the skill directory — do NOT search the filesystem. `find /` exceeds the 120s bash timeout and kills your shell.
Run `pip install ortools` once per session; the solver needs it.
Call pull_allocation_snapshot to get data. It returns a summary plus a `flatten` command — run that command verbatim to write snapshot.json into your sandbox. `flatten` maps the rich pull (VSO jobcards + a vehicle pool of real/future vehicles) into the solver's orders/units/incumbent arrays; it is pure code (`xas_allocation.flatten`), not something to reason out by hand. Then read the file from your solver code, never into this conversation.
Your data is mounted as files. There is NO network:
  /workspace/pull.json                the allocation snapshot. Reached through the pull_allocation_snapshot tool and the `flatten` command — never read by hand.
  /workspace/reports/jobcards.json    the job-card records REPORTING answers over.
The tenant's taxonomy is NOT mounted — `index.md` ships inside the `xas-qa` skill directory, beside `phrasebook.py`. It lists every live entity, classification and status with the multi-language names users actually say, and is the ONLY authority for turning business words into system codes.
No network access — everything is local.

Determinism (the core invariant)
plan = pure_function(data_snapshot, skill, override). You hold no plan state in memory. Steering is ONE combined override object (weights / pins / forbid / lambda / scope / bump) — accumulate every instruction into it, show it back each turn, and carry it forward. There is no ledger, no replay: re-applying the same override to the same snapshot reproduces the plan exactly. If the sandbox is reclaimed, recover the override from the last one you showed the planner. (Durable cross-session persistence is deferred — DECIDE-5.) Consequences:

The same bundled dataset backs every turn of a repair cycle — re-applying the same combined override is the only thing that reproduces a turn, and the same override against different data is not the same turn.
Flattening the pull into the snapshot is pure code (eligibility is a hard sales_model equality — no model judgment, no residual). Never re-shape the data by reasoning.

Hard rules (never violate)

The plan comes from the solver, not from you. Every claim about allocation — which order is late, which vehicle an order gets, what a repair costs, who would be bumped — comes from running the solver through the xas-allocation skill's helpers. NEVER from reading /workspace/reports/jobcards.json or any other file. Those records answer reporting questions only; they are a different snapshot of the business and are not guaranteed to agree with the pull. If you cannot answer an allocation question by running the solver, say so — do not substitute a number you read.
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

Reporting (the other job)

For a question about the records — counts, breakdowns, statuses, branches, charts — use the xas-qa skill. It holds the procedure: build the phrasebook once, resolve the user's words against it exact-first, then compute the answer with real code over the records. Never eyeball the records and never invent a number. Resolve every business term through the taxonomy rather than guessing a code, translate codes back to human names before answering, and if a term matches more than one classification ask ONE short question instead of picking. Write charts as a file and tell the planner the filename.

Charts: write a SELF-CONTAINED .html file into /mnt/session/outputs/ — that directory is the ONLY one the planner's screen can reach, and a chart written anywhere else is invisible to them. Self-contained means the SVG is inlined in the page: never link a CDN or an external stylesheet. The skill has the exact recipe. Use a descriptive filename, say the filename in your reply, then STOP: do not read the chart back with the read tool. You already know what you plotted, the planner sees it rendered, and reading it back costs tens of thousands of tokens for nothing. Label axes and legends with human names, never raw codes.

Reporting is read-only. It never changes an allocation, and its numbers never become the basis for an allocation claim.

Reply in the language the person wrote in — this dealership works in Hebrew and English, and a Hebrew question gets a Hebrew answer. That applies to both jobs, and to chart labels: use the human names people recognise, never a raw code or an ObjectId.

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


def skill_files(skill_dir: Path, package: Path | None = None) -> list[tuple[str, bytes]]:
    """One skill bundle: the skill directory, plus an optional Python package.

    The API requires one top-level directory with SKILL.md at its root, so
    everything is mapped under ``<skill_dir.name>/``. Shipping the solver inside
    the allocation skill is what gets it into an Anthropic-hosted sandbox at all
    — there is no host-side workdir to copy it into, and having the model retype
    it from a prompt is the determinism leak this design exists to prevent.

    Sources stay where they are: this synthesizes the bundle at upload time
    rather than duplicating files, so the tests and the skill run against the
    same source. The pull and the job-card records are never bundled — they are
    mounted per session (see web.py), so regenerating either needs no redeploy.
    Changing this code does, and so does editing the taxonomy the QA bundle now
    carries (DECIDE-16).
    """
    files: list[tuple[str, bytes]] = []
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files.append((str(path.relative_to(skill_dir.parent)), path.read_bytes()))
    if not any(name.endswith("/SKILL.md") for name, _ in files):
        sys.exit(f"No SKILL.md found in {skill_dir}")

    if package is not None:
        for path in sorted(package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files.append((f"{skill_dir.name}/{path.relative_to(REPO_ROOT)}", path.read_bytes()))
    return files


def alloc_bundle() -> list[tuple[str, bytes]]:
    return skill_files(ALLOC_SKILL_DIR, REPO_ROOT / "xas_allocation")


def qa_bundle() -> list[tuple[str, bytes]]:
    """SKILL.md + phrasebook.py + index.md. No package: grep over a flattened
    table is the matcher.

    TODO (DECIDE-16): index.md — the tenant taxonomy — rides along in this bundle
    because there is exactly one tenant. It is the one piece of DATA in a skill,
    and the cost is that the caller can no longer choose a dealership per session
    and a taxonomy edit needs a redeploy. Second tenant = move it back to a
    per-session mount (`datasource.get_taxonomy` + `/workspace/reports/index.md`,
    reverted from this commit); do NOT fix it by bundling every tenant's
    taxonomy, which shows each session all the others.
    """
    return skill_files(QA_SKILL_DIR)


def create_environment() -> str:
    environment = client().beta.environments.create(
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


def create_skill(files: list[tuple[str, bytes]], title: str) -> str:
    skill = client().beta.skills.create(files=files, display_title=title)
    print(f"Created skill:       {skill.id}  ({title})")
    return skill.id


def update_skill(skill_id: str, files: list[tuple[str, bytes]], title: str) -> None:
    version = client().beta.skills.versions.create(skill_id, files=files)
    print(f"Updated skill:       {skill_id} -> version {version.version}  ({title})")


def _skills(alloc_skill_id: str, qa_skill_id: str) -> list[dict]:
    """Both entries, every time — agents.update() PRESERVES omitted array fields,
    so a skills list that is not sent is a skills list that does not change."""
    return [
        {"type": "custom", "skill_id": alloc_skill_id},
        {"type": "custom", "skill_id": qa_skill_id},
    ]


def create_agent(alloc_skill_id: str, qa_skill_id: str) -> str:
    agent = client().beta.agents.create(
        name=AGENT_NAME,
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        skills=_skills(alloc_skill_id, qa_skill_id),
    )
    print(f"Created agent:       {agent.id}  (version {agent.version})")
    return agent.id


def update_agent(agent_id: str, alloc_skill_id: str, qa_skill_id: str) -> None:
    agent = client().beta.agents.update(
        agent_id,
        # Sent on update too: the agent predates the merge and would otherwise
        # keep the console label "XAS Allocation Agent" while doing two jobs.
        name=AGENT_NAME,
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        skills=_skills(alloc_skill_id, qa_skill_id),
    )
    print(f"Updated agent:       {agent.id}  (version {agent.version}, 2 skills)")


def check_environment_type(environment_id: str) -> None:
    """This branch builds a cloud agent; .env may still hold self-hosted IDs.

    Updating across that boundary produces an agent whose environment nothing
    serves — the sessions would queue forever waiting for a worker that is not
    coming. Cheaper to refuse than to debug.
    """
    kind = client().beta.environments.retrieve(environment_id).config.type
    if kind != "cloud":
        sys.exit(
            f"ALLOC_ENV_ID={environment_id} is a {kind!r} environment, but this branch\n"
            "builds an Anthropic-hosted (cloud) agent. Clear ALLOC_AGENT_ID / ALLOC_ENV_ID /\n"
            "ALLOC_SKILL_ID from .env and re-run to create a fresh cloud set — the two\n"
            "sandbox types need separate resources."
        )


def main() -> None:
    """Three paths, because the allocation agent already exists.

    The common one after the merge is the MIDDLE case: agent, environment and
    allocation skill are live, the QA skill is not. That path creates one skill
    and updates the agent to carry both — it never creates a second agent.
    """
    if ALLOC_ENV_ID:
        check_environment_type(ALLOC_ENV_ID)

    # Everything exists — refresh both bundles and the agent.
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_SKILL_ID and QA_SKILL_ID:
        print("All resources exist — updating in place.\n")
        update_skill(ALLOC_SKILL_ID, alloc_bundle(), ALLOC_SKILL_TITLE)
        update_skill(QA_SKILL_ID, qa_bundle(), QA_SKILL_TITLE)
        update_agent(ALLOC_AGENT_ID, ALLOC_SKILL_ID, QA_SKILL_ID)
        print("\nDone. The IDs in .env are unchanged.")
        return

    # The migration path: add the QA skill to the agent that already exists.
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_SKILL_ID and not QA_SKILL_ID:
        print("Adding the reporting skill to the existing agent.\n")
        update_skill(ALLOC_SKILL_ID, alloc_bundle(), ALLOC_SKILL_TITLE)
        qa_skill_id = create_skill(qa_bundle(), QA_SKILL_TITLE)
        update_agent(ALLOC_AGENT_ID, ALLOC_SKILL_ID, qa_skill_id)
        print("\n" + "=" * 60)
        print("Add this ONE line to your .env (the others are unchanged):\n")
        print(f"QA_SKILL_ID={qa_skill_id}")
        print("=" * 60)
        return

    # Cold start.
    environment_id = ALLOC_ENV_ID or create_environment()
    check_environment_type(environment_id)
    alloc_skill_id = ALLOC_SKILL_ID or create_skill(alloc_bundle(), ALLOC_SKILL_TITLE)
    qa_skill_id = QA_SKILL_ID or create_skill(qa_bundle(), QA_SKILL_TITLE)
    agent_id = ALLOC_AGENT_ID or create_agent(alloc_skill_id, qa_skill_id)

    print("\n" + "=" * 60)
    print("Setup complete. Paste these into your .env:\n")
    print(f"ALLOC_AGENT_ID={agent_id}")
    print(f"ALLOC_ENV_ID={environment_id}")
    print(f"ALLOC_SKILL_ID={alloc_skill_id}")
    print(f"QA_SKILL_ID={qa_skill_id}")
    print("=" * 60)
    print(
        "\nThe environment is Anthropic-hosted — there is no worker to start and no\n"
        "environment key to generate. Run `uv run uvicorn web:app --port 8000`."
    )


if __name__ == "__main__":
    main()
