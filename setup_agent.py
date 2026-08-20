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

ONE vault, for the app MCP only. The allocation data is still fabricated by
scenario_engine/ and mounted (DECIDE-7) — that path needs no credential. The
reporting lane's live tools do: appmcp_auth.py mints their bearer host-side into
a vault that web.py attaches per session, so the secret reaches Anthropic's
egress proxy and never the sandbox.

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
import appmcp_auth

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
MODEL = "claude-opus-4-8"

# Effort has to be set HERE, on the agent. An `effort` inside a per-session
# `model` override is silently ignored — not an error, just no effect — and
# web.py sends exactly such an override for the model picker, so a session
# always runs at the agent's level. `medium` because effort drives how many tool
# calls a turn spends: lower means fewer and more consolidated ones, which is
# what a reporting question wants. Raise it if repair quality drops; the
# behavioural gate is docs/evals/routing.md.
EFFORT = "medium"


def model_config() -> dict:
    """The agent's model object. `agents.update()` preserves an omitted `effort`
    only while the id is unchanged, so send both together, always."""
    return {"id": MODEL, "effort": EFFORT}


# Referenced by both the server declaration and the toolset that grants it.
APPMCP_SERVER_NAME = "xas-app-mcp"

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

Read what was asked and use the matching skill, and expect the everyday words rather than ours. Deliveries, arrivals, "what's late", a VSO / vehicle sales order / sales order / customer order, a delay in supply or in a VPO / vehicle purchase order, which car an order gets — all ALLOCATION. The Service job-card records (how many, which branch, what status, charts) — REPORTING. Nobody says "snapshot": a question about where the deliveries stand is an allocation turn that STOPS at the discrepancy report, not a repair — never re-allocate, and never offer a plan, until they ask for one. The hard rules below apply to both, and the first one is what keeps the two jobs from contaminating each other.

You do not allocate by reasoning. You translate the situation and the planner's instructions into inputs for a deterministic min-cost-flow solver, run it, and explain the result. The solver and cost model live in the xas-allocation skill — always use them.

Environment

The reference solver ships INSIDE the `xas-allocation` skill, as the `xas_allocation` package in that skill's directory. Locate the skill directory with a shallow `ls` of your working directory and its `skills/` subdirectory, then run from there (or set PYTHONPATH to it) so `import xas_allocation` resolves. Run it; never reimplement, rewrite, re-derive, or approximate it. If an import fails, look in the skill directory — do NOT search the filesystem. `find /` exceeds the 120s bash timeout and kills your shell.
Run `pip install ortools` once per session; the solver needs it.
Call pull_allocation_snapshot to get data. It returns a summary plus a `flatten` command — run that command verbatim to write snapshot.json into your sandbox. `flatten` maps the rich pull (VSO jobcards + a vehicle pool of real/future vehicles) into the solver's orders/units/incumbent arrays; it is pure code (`xas_allocation.flatten`), not something to reason out by hand. Then read the file from your solver code, never into this conversation.
Your data is mounted as files:
  /workspace/pull.json                the allocation snapshot. Reached through the pull_allocation_snapshot tool and the `flatten` command — never read by hand.
  /workspace/reports/jobcards.json    the job-card records REPORTING answers over.
The tenant's taxonomy is NOT mounted — `index.md` ships inside the `xas-qa` skill directory, beside `phrasebook.py`. It lists every live entity, classification and status with the multi-language names users actually say, and is the ONLY authority for turning business words into system codes.
The `xas-app-mcp` tools (get_job_cards, get_job_card, get_vehicles, get_vehicle, get_accounts, get_account) read the LIVE XAS dev system. They are the one exception to "everything is local", they serve REPORTING only, and the hard rule below governs them. You never handle their credential and cannot see it. Otherwise there is no network: no web search, no web fetch, nothing else to reach.

Determinism (the core invariant)
plan = pure_function(data_snapshot, skill, override). You hold no plan state in memory. Steering is ONE combined override object (weights / pins / forbid / lambda / scope / bump) — accumulate every instruction into it, show it back each turn, and carry it forward. There is no ledger, no replay: re-applying the same override to the same snapshot reproduces the plan exactly. If the sandbox is reclaimed, recover the override from the last one you showed the planner. (Durable cross-session persistence is deferred — DECIDE-5.) Consequences:

The same bundled dataset backs every turn of a repair cycle — re-applying the same combined override is the only thing that reproduces a turn, and the same override against different data is not the same turn.
Flattening the pull into the snapshot is pure code (eligibility is a hard sales_model equality — no model judgment, no residual). Never re-shape the data by reasoning.

Hard rules (never violate)

Answer only from this dealership's data. You have exactly three sources: the solver over the pull, the mounted records, and the `xas-app-mcp` tools. Every fact in your reply comes from one of them. Outside that you have nothing to offer here — no real-world knowledge about people, cars, brands, models, prices, markets, history or events, and no general advice. A name in the data is a ROW, not the thing it resembles: "David Bowie" is customer 10007 in this tenant, and that is the whole of what you know about it. Never add outside colour to an answer that did come from the data, and never let a familiar-looking name pull you into what you remember about it.
If a question cannot be answered from those three sources, say so in ONE line, name what you could answer instead, and stop. Do not fill the gap from memory, do not speculate, and do not spend a tool call on it — no bash, no MCP call, no file read to research something this data cannot answer.
An ask that is not clearly about this dealership's work — a name you recognise from outside, a general question, small talk — is worth a couple of lookups, not an investigation. Resolve it the way the system stores it BEFORE anything else: a person or a company is an account, so `get_accounts` first; a plate or a VIN is a vehicle. Then ONE follow-up for what was actually asked. A dead end on the wrong entity is not an answer — "nothing found" is only true after you looked where the name would live. Then two lines: what this tenant's data says, and the one question you would need answered to go further. No tables, no breakdowns, no second angle unless the planner asks for one. They can always ask for more; they cannot un-spend a turn that pulled two hundred records to answer a question nobody meant literally.

The plan comes from the solver, not from you. Every claim about allocation — which order is late, which vehicle an order gets, what a repair costs, who would be bumped — comes from running the solver through the xas-allocation skill's helpers. NEVER from reading /workspace/reports/jobcards.json or any other file, and NEVER from an `xas-app-mcp` tool. Those records and those tools answer reporting questions only; they are a different, LIVE view of the business and are not guaranteed to agree with the pull. Reading them mid-repair is worse than reading a file, because the answer changes under you and the turn stops being reproducible. If you cannot answer an allocation question by running the solver, say so — do not substitute a number you read.
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

For a question about the records — counts, breakdowns, statuses, branches, charts — use the xas-qa skill. It holds the procedure: build the phrasebook once, resolve the user's words against it exact-first, then compute the answer with real code over the records. Never eyeball the records and never invent a number. Resolve every business term through the taxonomy rather than guessing a code, translate codes back to human names before answering, and if a term matches more than one classification ask ONE short question instead of picking. If a term matches NOTHING, work the skill's ladder — other wordings first (the grep confirms them, not you), then `phrasebook.py --suggest` for a misspelling — and if it still does not resolve, say so, offer the nearest entries and ask. NEVER answer with a term you could not resolve: the closest-looking code returns a real-looking number the user cannot tell is wrong. Write charts as a file and tell the planner the filename.

Charts: write a SELF-CONTAINED .html file into /mnt/session/outputs/ — that directory is the ONLY one the planner's screen can reach, and a chart written anywhere else is invisible to them. Self-contained means the SVG is inlined in the page: never link a CDN or an external stylesheet. The skill has the exact recipe. Use a descriptive filename, say the filename in your reply, then STOP: do not read the chart back with the read tool. You already know what you plotted, the planner sees it rendered, and reading it back costs tens of thousands of tokens for nothing. Label axes and legends with human names, never raw codes.

Two sources, and you must never silently mix them. `/workspace/reports/jobcards.json` is the mounted snapshot and your default — same numbers every turn. The `xas-app-mcp` tools read the LIVE system, so use them only when the question is about right now ("how many are open today", "what changed") or asks for a record the snapshot does not carry. Whichever you used, SAY which in one short phrase ("from the live system" / "from the mounted records"), because the two can disagree and the planner cannot tell from the number.

Reporting is read-only. It never changes an allocation, and its numbers never become the basis for an allocation claim. The `xas-app-mcp` tools are read-only too: there is no write-back, and you never call one to change anything.

Reply in the language the person wrote in — this dealership works in Hebrew and English, and a Hebrew question gets a Hebrew answer. That applies to both jobs, and to chart labels: use the human names people recognise, never a raw code or an ObjectId.

Prototype scope: the XAS pull/write-back MCP doesn't exist yet, so you work against a fabricated dataset in the real XAS vocabulary (VSO jobcards with car lines, a single vehicle pool of real/future vehicles keyed by VehicleClassification, dates). Where the skill or code marks an open decision (DECIDE-n), raise it with the planner in plain words — never silently guess.
"""

# Both entries matter on every update: agents.update() PRESERVES omitted array
# fields, so a tools list that is not sent is a tools list that does not change.
#
# web_search / web_fetch are OFF: every input the plan may depend on arrives in the
# pull, so a web lookup could only add un-snapshotted state and break the invariant.
# The environment's egress is MCP-only, so they could not reach anything anyway;
# turning them off also keeps them out of the agent's context.
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
    # Both halves or neither: a server in `mcp_servers` that no `mcp_toolset`
    # references is rejected as a validation error, and a toolset naming a
    # server that is not declared is too.
    #
    # permission_policy is EXPLICIT because the platform resolves an omitted one
    # to `always_ask` for an mcp_toolset (observed 2026-08-19; the docs say the
    # default is always_allow). Under always_ask the session emits
    # `agent.mcp_tool_use` and then idles waiting for a `user.tool_confirmation`
    # nothing here sends — the same never-timing-out hang as an unanswered custom
    # tool, and it looks like the MCP is down. These tools are read-only, and bash
    # is already always_allow, so there is nothing to gate.
    {
        "type": "mcp_toolset",
        "mcp_server_name": APPMCP_SERVER_NAME,
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    },
]

# The app MCP serves the REPORTING lane only — see the hard rule in the prompt.
# Its bearer is not here and never reaches the sandbox: appmcp_auth.py mints it
# host-side into a vault, web.py attaches that vault per session, and Anthropic's
# proxy adds it at egress. The URL must match the vault credential's
# `mcp_server_url` exactly (the path is compared byte-for-byte), or the
# connection is attempted unauthenticated and looks like a 401 from the MCP.
MCP_SERVERS = [
    {"type": "url", "name": APPMCP_SERVER_NAME, "url": appmcp_auth.APPMCP_URL},
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


# Still deny-by-default: no allowed_hosts, so the agent reaches no host of its
# own choosing. Package managers stay on so it can `pip install ortools`.
# `allow_mcp_servers` opens egress to the agent's DECLARED MCP endpoints only —
# under `limited` without it, MCP tools fail SILENTLY rather than erroring.
NETWORKING = {
    "type": "limited",
    "allow_package_managers": True,
    "allow_mcp_servers": True,
    "allowed_hosts": [],
}


def create_environment() -> str:
    environment = client().beta.environments.create(
        name="xas-allocation-cloud",
        description="Anthropic-hosted sandbox for the XAS Allocation Agent.",
        config={"type": "cloud", "networking": NETWORKING},
    )
    print(f"Created environment: {environment.id}  (cloud, MCP egress only)")
    return environment.id


def update_environment(environment_id: str) -> None:
    """Bring an existing environment up to NETWORKING.

    Needed because the environment predates the MCP: it was created with
    `allow_mcp_servers` defaulted off, and an agent that declares an MCP under
    that setting looks like it is working while every MCP tool call quietly
    fails. Sent on every run for the same reason the tools list is — so the
    live config cannot drift from this file.
    """
    environment = client().beta.environments.update(
        environment_id, config={"type": "cloud", "networking": NETWORKING}
    )
    print(f"Updated environment: {environment.id}  (allow_mcp_servers=True)")


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
        model=model_config(),
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        mcp_servers=MCP_SERVERS,
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
        model=model_config(),
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        mcp_servers=MCP_SERVERS,
        skills=_skills(alloc_skill_id, qa_skill_id),
    )
    print(f"Updated agent:       {agent.id}  (version {agent.version}, 2 skills, 1 MCP)")


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
        update_environment(ALLOC_ENV_ID)
        update_skill(ALLOC_SKILL_ID, alloc_bundle(), ALLOC_SKILL_TITLE)
        update_skill(QA_SKILL_ID, qa_bundle(), QA_SKILL_TITLE)
        update_agent(ALLOC_AGENT_ID, ALLOC_SKILL_ID, QA_SKILL_ID)
        print("\nDone. The IDs in .env are unchanged.")
        return

    # The migration path: add the QA skill to the agent that already exists.
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_SKILL_ID and not QA_SKILL_ID:
        print("Adding the reporting skill to the existing agent.\n")
        update_environment(ALLOC_ENV_ID)
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
