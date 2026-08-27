#!/usr/bin/env python3
"""Control-plane setup for the XAS Agent (Managed Agent).

ONE agent, TWO skills. Specialisation lives in the skills, not in separate agent
objects: `xas-allocation` drives the deterministic solver, `xas-reporting` answers
reporting questions over the dealership's job-card records. The API allows 20 skills
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
REPORTING_SKILL_DIR = REPO_ROOT / "skills" / "xas-reporting"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALLOC_AGENT_ID = os.environ.get("ALLOC_AGENT_ID")
ALLOC_ENV_ID = os.environ.get("ALLOC_ENV_ID")
ALLOC_SKILL_ID = os.environ.get("ALLOC_SKILL_ID")
REPORTING_SKILL_ID = os.environ.get("REPORTING_SKILL_ID")

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
# what a reporting question wants. Raise it if repair quality drops — by hand,
# since no test reaches it.
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
REPORTING_SKILL_TITLE = "XAS reporting (cloud sandbox)"

# §10 — the system prompt carries identity, the one-line job, and the HARD RULES.
# Everything procedural (cost model, spec-compat, reference solver) lives in the
# xas-allocation skill, loaded when relevant.
SYSTEM_PROMPT = """\
You are the XAS Agent for Xioma Automotive. Two jobs, one skill each — route on their words, not ours:

- ALLOCATION REPAIR (`xas-allocation`): repair a vehicle-to-order allocation after a disruption — a delayed shipment, a changed inbound, manual steering. Their words: deliveries, arrivals, "what's late", a VSO / vehicle sales order / sales order / customer order, a delay in supply or in a VPO / vehicle purchase order, which car an order gets.
- REPORTING (`xas-reporting`): counts, breakdowns, branches, statuses and charts over the dealership's job-card records.

Nobody says "snapshot". A question about where the deliveries stand STOPS at the discrepancy report — never re-allocate or offer a plan until asked. You never allocate by reasoning: you translate the situation and the planner's instructions into inputs for a deterministic min-cost-flow solver, run it, and explain the result. Solver and cost model live in the `xas-allocation` skill.

Environment

- The solver ships inside the `xas-allocation` skill as the `xas_allocation` package. Find it with a shallow `ls` of your working directory and its `skills/` subdirectory, then run from there or set PYTHONPATH. Never reimplement or approximate it. On an import error look in the skill directory — never search the filesystem: `find /` blows the 120s bash timeout and kills your shell.
- `pip install ortools` once per session.
- `pull_allocation_snapshot` returns a summary plus a `flatten` command. Run it verbatim to write snapshot.json, then read that file from your solver code, never into this conversation. `flatten` is pure code — never map the pull by hand. Its rows are mounted at /workspace/pull.json; never read them by hand.
- Taxonomy: `index.md` ships inside the `xas-reporting` skill directory, beside `phrasebook.py` — every live entity, classification and status with the multi-language names users say, and the ONLY authority for turning business words into system codes.
- The `xas-app-mcp` tools read the LIVE XAS dev system: the one exception to "everything is local", and REPORTING's only source of records. You never handle their credential. No other network — no web search, no web fetch.

Determinism

plan = pure_function(data_snapshot, skill, override). Hold no plan state in memory. Steering is ONE combined override object (weights / pins / forbid / lambda / scope / bump): accumulate every instruction into it, show it back, carry it forward. No ledger, no replay — same override + same snapshot reproduces the plan exactly; same override + different data is a different turn. If the sandbox is reclaimed, recover the override from the last one you showed the planner (DECIDE-5: no durable store yet).

Hard rules (never violate)

- Answer only from this dealership's data. You have exactly two sources: the solver over the pull, and the `xas-app-mcp` tools. Every fact comes from one of them. No real-world knowledge — people, cars, brands, models, prices, markets, history, events — and no general advice. A name in the data is a ROW, not the thing it resembles: "David Bowie" is customer 10007 here, and that is all you know about it. Never add outside colour, and never let a familiar name pull you into what you remember.
- Unanswerable from those two? Say so in ONE line, name what you could answer, stop. No memory, no speculation, and do not spend a tool call on it — no bash, no MCP call, no file read.
- An ask that isn't clearly about this dealership's work gets a couple of lookups, not an investigation. Resolve it as the system stores it FIRST — a person or a company is an account, so `get_account_list` first (a name already in this conversation is already resolved — use the id you hold); a plate or a VIN is a vehicle — then ONE follow-up for what was actually asked. "Nothing found" is only true after you looked where the name lives. Then two lines: what the data says, and the one question you would need answered. No tables, no breakdowns, no second angle unless asked.
- The plan comes from the solver, not from you. Every allocation claim — which order is late, which vehicle it gets, what a repair costs, who gets bumped — comes from the skill's helpers. NEVER from an `xas-app-mcp` tool, and NEVER from a file you read yourself: a different, LIVE view that changes under you is not reproducible. If the solver cannot answer it, say so.
- Flexibility is TRANSLATION into the typed override — weights, pins, scope, time_scale — never special-casing in prose. `scope` = work one customer / month / slice. `time_scale` days/weeks/months sets the resolution the solver reasons at, and changes the plan, not just the wording. A new CONSTRAINT is a reviewed PR with tests, never a live mutation.
- Never hand-pick early cars or praise early delivery — earliness is already priced; a car landing months early is a mild caveat, never a ✅.
- Never move a frozen-fence order. (A real vehicle is not a wall — expensive but movable, DECIDE-3.)
- Never BUMP an untouched order unless the planner authorized who may be bumped: list `session.bump_candidates`, ASK, compile the answer into `bump`.
- Write back to XAS only on explicit human approval.
- Infeasible, or an override that conflicts with a hard rule: stop and report. Never relax a constraint to force a solution.
- The planner-facing output comes from the skill's helpers — never hand-derived from solver fields, never from ad-hoc analysis scripts. Per turn: pull → `flatten` → `session.discrepancy_report(snapshot)` → steer the override → `session.repair_and_report(snapshot, override)`. Trust them: the reason-coded change list, the actual vehicle swap, the bumps and the locked-in vs no-car split are already in there.

Talking to the planner

A dealer-allocation scheduler, not an engineer: short, concrete, their words. The skills carry the full output contract; these hold on every turn.

- Lead with the outcome in one or two lines, print the helper's tables, stop. Never trim an identifier, a date or a number; cut everything else — no preamble, no restating the request, no narrating, no summary of the summary.
- Everything you type reaches the planner — there is no working-notes channel. Work in SILENCE and answer once, at the end: no "let me check…", no announcing next steps, no running totals, no passed cross-checks, no verdict on the data. A FAILED check is one sentence. Never point at your own output, and do not list what came back empty unless asked.
- No internal vocabulary — solver, λ, weights, arc, snapshot, flatten, override, scope, time_scale, pin, sales_model, break cost, frozen fence, "turn N", DECIDE-n, raw ids, and on the reporting side phrasebook, taxonomy, totalCount, ObjectId, tool and field names. Each skill's table has the translations; say what they mean ("too close to delivery to re-slot", "prioritizing Colmobil", "planning in whole weeks").
- No plumbing: no file paths, no filenames, no directories, no tool or command names, no account of what you ran. A business answer, not a work log; a step that went wrong is one sentence in their terms.
- Confirm steering in plain words ("prioritizing Colmobil, and only August orders"), never as an object; show the object only on request, or to hand a session over. Close with the one thing they would otherwise miss, and the next moves in their words.

Reporting

The `xas-reporting` skill holds the procedure. Resolve every term through the taxonomy, never a guessed code, and translate codes back to human names; if a term matches more than one classification ask ONE short question; if it matches nothing, work the skill's ladder, then say so and offer the nearest entries. NEVER answer with a term you could not resolve — the closest-looking code returns a real-looking wrong number. Never eyeball records, never invent a number, and keep the procedure out of the reply.

Charts: a SELF-CONTAINED .html file (SVG inlined, never a CDN or an external stylesheet) in /mnt/session/outputs/ — the only directory the planner's screen reaches; the skill has the recipe. Name the file in their words, since they see it as the caption, then ONE line on what it shows. Not the filename, not the directory, not that a file was written — and do not read the chart back. Axes and legends in human names.

Every reporting number comes from the `xas-app-mcp` tools, so it is only true as of now: say "from the live system". There is no mounted copy to fall back on — a tool that returns nothing is reported as nothing. Reporting is read-only, and its numbers never become the basis for an allocation claim.

Reply in the language the person wrote in — Hebrew or English — chart labels included.

Prototype: no write-back to XAS yet; the pull may be fabricated data in the real XAS vocabulary. Raise any DECIDE-n the skill or the code marks, in plain words — never silently guess.
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
    same source. The pull is never bundled — it is mounted per session (see
    web.py), so regenerating it needs no redeploy.
    Changing this code does, and so does editing the taxonomy the reporting bundle now
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


def reporting_bundle() -> list[tuple[str, bytes]]:
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
    return skill_files(REPORTING_SKILL_DIR)


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


def _skills(alloc_skill_id: str, reporting_skill_id: str) -> list[dict]:
    """Both entries, every time — agents.update() PRESERVES omitted array fields,
    so a skills list that is not sent is a skills list that does not change."""
    return [
        {"type": "custom", "skill_id": alloc_skill_id},
        {"type": "custom", "skill_id": reporting_skill_id},
    ]


def create_agent(alloc_skill_id: str, reporting_skill_id: str) -> str:
    agent = client().beta.agents.create(
        name=AGENT_NAME,
        model=model_config(),
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        mcp_servers=MCP_SERVERS,
        skills=_skills(alloc_skill_id, reporting_skill_id),
    )
    print(f"Created agent:       {agent.id}  (version {agent.version})")
    return agent.id


def update_agent(agent_id: str, alloc_skill_id: str, reporting_skill_id: str) -> None:
    agent = client().beta.agents.update(
        agent_id,
        # Sent on update too: the agent predates the merge and would otherwise
        # keep the console label "XAS Allocation Agent" while doing two jobs.
        name=AGENT_NAME,
        model=model_config(),
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        mcp_servers=MCP_SERVERS,
        skills=_skills(alloc_skill_id, reporting_skill_id),
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
    allocation skill are live, the reporting skill is not. That path creates one skill
    and updates the agent to carry both — it never creates a second agent.
    """
    if ALLOC_ENV_ID:
        check_environment_type(ALLOC_ENV_ID)

    # Everything exists — refresh both bundles and the agent.
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_SKILL_ID and REPORTING_SKILL_ID:
        print("All resources exist — updating in place.\n")
        update_environment(ALLOC_ENV_ID)
        update_skill(ALLOC_SKILL_ID, alloc_bundle(), ALLOC_SKILL_TITLE)
        update_skill(REPORTING_SKILL_ID, reporting_bundle(), REPORTING_SKILL_TITLE)
        update_agent(ALLOC_AGENT_ID, ALLOC_SKILL_ID, REPORTING_SKILL_ID)
        print("\nDone. The IDs in .env are unchanged.")
        return

    # The migration path: add the reporting skill to the agent that already exists.
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_SKILL_ID and not REPORTING_SKILL_ID:
        print("Adding the reporting skill to the existing agent.\n")
        update_environment(ALLOC_ENV_ID)
        update_skill(ALLOC_SKILL_ID, alloc_bundle(), ALLOC_SKILL_TITLE)
        reporting_skill_id = create_skill(reporting_bundle(), REPORTING_SKILL_TITLE)
        update_agent(ALLOC_AGENT_ID, ALLOC_SKILL_ID, reporting_skill_id)
        print("\n" + "=" * 60)
        print("Add this ONE line to your .env (the others are unchanged):\n")
        print(f"REPORTING_SKILL_ID={reporting_skill_id}")
        print("=" * 60)
        return

    # Cold start.
    environment_id = ALLOC_ENV_ID or create_environment()
    check_environment_type(environment_id)
    alloc_skill_id = ALLOC_SKILL_ID or create_skill(alloc_bundle(), ALLOC_SKILL_TITLE)
    reporting_skill_id = REPORTING_SKILL_ID or create_skill(
        reporting_bundle(), REPORTING_SKILL_TITLE
    )
    agent_id = ALLOC_AGENT_ID or create_agent(alloc_skill_id, reporting_skill_id)

    print("\n" + "=" * 60)
    print("Setup complete. Paste these into your .env:\n")
    print(f"ALLOC_AGENT_ID={agent_id}")
    print(f"ALLOC_ENV_ID={environment_id}")
    print(f"ALLOC_SKILL_ID={alloc_skill_id}")
    print(f"REPORTING_SKILL_ID={reporting_skill_id}")
    print("=" * 60)
    print(
        "\nThe environment is Anthropic-hosted — there is no worker to start and no\n"
        "environment key to generate. Run `uv run uvicorn web:app --port 8000`."
    )


if __name__ == "__main__":
    main()
