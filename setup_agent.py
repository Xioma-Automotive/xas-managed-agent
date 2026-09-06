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

The skill bundle carries the reference solver, which is how it reaches a sandbox
we do not run. Change anything under xas_allocation/ and you must re-run this, or
the sandbox keeps solving with the previous version. The DATA is never bundled —
it is mounted per session, so re-carving a scenario needs no redeploy.

ONE vault, for the app MCP only. The allocation data is read from a scenario
directory of the real export and mounted (DECIDE-7) — that path needs no
credential. The reporting lane's live tools do: appmcp_auth.py mints their bearer
host-side into a vault that web.py attaches per session, so the secret reaches
Anthropic's egress proxy and never the sandbox.

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
import phrasebook

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
ALLOC_SKILL_DIR = REPO_ROOT / "skills" / "xas-allocation"
REPORTING_SKILL_DIR = REPO_ROOT / "skills" / "xas-reporting"

# A whole prompt+skill pair, swapped by one env var: `XAS_VARIANT=full uv run
# python setup_agent.py` deploys `variants/full/` instead of the prompt in this
# file and `skills/xas-reporting/SKILL.md`. Unset = the pair that ships, which
# lives in those two default slots. Only those two files are swapped: the helpers, the taxonomy and
# the allocation skill ship the same either way.
VARIANT = os.environ.get("XAS_VARIANT", "")
VARIANT_DIR = REPO_ROOT / "variants" / VARIANT if VARIANT else None

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
# always runs at the agent's level. It is sent explicitly rather than omitted
# because an omitted `effort` is only preserved while the model id is unchanged.
# Effort drives how many tool calls a turn spends, so this is a cost knob as much
# as a quality one; no test reaches what changing it does to a turn.
EFFORT = "low"


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

# §10 — the system prompt says what each component is FOR and leaves the reasoning
# to the model. It carries identity, the pieces the agent has, what rides in the
# first block, the two link kinds and the never-show-the-kitchen rule — and no
# procedure at all: which call to send, how to bound a page and how to present a
# figure are the reporting skill's, and routing to `xas-allocation` is that skill's
# own description. The tenant's VOCABULARY moved into the skill on 2026-09-06 with
# the lookup's deletion: it is only ever wanted once you are already answering a
# reporting question, and by then the skill has landed in the same block. `dates.py`
# stays here because it fires in that block, beside the read, and so cannot live in
# the file it would have to wait for.
SYSTEM_PROMPT = """\
You are the XAS Agent for Xioma Automotive. You answer questions over this dealership's own records — counts, breakdowns, lists, charts. Read the skill that fits before you act.

The pieces

- `xas-app-mcp` read tools — the live system, read-only: the only source of a number you report.
- The `xas-reporting` skill — the procedure, AND this dealership's whole vocabulary: every card, vehicle and account type, every status, state and branch, with the value each one is filtered by. It is complete, so nothing is ever looked up; a word that is not in it is not this dealership's word, and you ask rather than pick the nearest.
- Dates — a named period, both halves: the filter to send and the span in words to tell the planner. Never work one out yourself:
  `python /workspace/skills/xas-reporting/dates.py "last week"`

The first block

Everything before your first `xas-app-mcp` call goes in ONE block, never a round trip each: read the `xas-reporting` skill, and in that same block turn every named period into a date range. That is the whole of it — two things, one block, and nothing else stands between a question and its answer.

A word about WHEN is a date, not a status: "opened", "created", "raised", "closed last week" all mean `CreateDateTime` over a span. `Open` is a status; "opened" is a date; reading one as the other answers a different question.

Links

- Every record carries its own `Url` and every list a `ListUrl`. Those are your links: use them, never build, guess or edit one, and name a record that came back without one in plain text.
- Make the name itself the link, wherever it appears — never the id where a name belongs: `[Delek Motors](/accounts/6a9144209004759d555d03f1)`.
- Close a count or a set with the `ListUrl` of the call you counted.
- TEN named records is the ceiling; past ten the set link is the list, so print ten and say how many more there are.
- A link is a name made clickable, never a bare address.

Never show the kitchen

The reply is the answer, in the planner's own words. No file path or filename, no tool, field or column name, no code or id where a name belongs, no account of what you ran or checked. Trouble in business terms ("the live system returned nothing for July"). The links above are the one exception.
"""


def variant_file(name: str) -> Path:
    """One file of the selected variant, or exit naming what is missing."""
    path = VARIANT_DIR / name
    if not path.is_file():
        sys.exit(f"XAS_VARIANT={VARIANT}: no {path}")
    return path


# The reporting SKILL carries the tenant's vocabulary inline instead of shipping a
# table and a matcher to search it. Both blocks are SUBSTITUTED at deploy time from
# index.md — a hand-typed list beside a generated one is a second copy of the
# taxonomy, free to drift — and a variant SKILL.md that carries no marker simply
# keeps its own text.
CLASSIFICATIONS_MARKER = "{{CLASSIFICATIONS}}"
VOCABULARY_MARKER = "{{VOCABULARY}}"

if VARIANT_DIR is not None:
    SYSTEM_PROMPT = variant_file("system-prompt.md").read_text()


def render_vocabulary(skill_md: str) -> str:
    """Both taxonomy blocks substituted into the reporting SKILL.md."""
    return skill_md.replace(CLASSIFICATIONS_MARKER, phrasebook.classification_block()).replace(
        VOCABULARY_MARKER, phrasebook.vocabulary_block()
    )


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
    web.py), so re-carving a scenario needs no redeploy.
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
    """SKILL.md — with the taxonomy rendered INTO it — plus charts.md and dates.py.

    There is no table and no matcher any more (2026-09-06). Everything a lookup
    could have answered is 46 records, which cost 1,051 tokens written out against
    489 for one `--lookup` call and 1,070 for the one `--list` call a breakdown
    needed; the tool cost more than the data and spent a round trip doing it. So
    the vocabulary is rendered into the skill the agent already reads, and a
    classification cannot be looked up because nothing can.

    index.md stays in the repo as the SOURCE and does not ship: `dump_taxonomy`
    regenerates it, and re-rendering is part of this deploy.

    TODO (DECIDE-16): the tenant taxonomy rides along in this bundle because
    there is exactly one tenant. It is the one piece of DATA in a skill, and the
    cost is that the caller can no longer choose a dealership per session and a
    taxonomy edit needs a redeploy. Second tenant = move it back to a per-session
    mount (`datasource.get_taxonomy` + /workspace/reports/); do NOT fix it by
    bundling every tenant's taxonomy, which shows each session all the others.
    """
    files = [
        (name, blob)
        for name, blob in skill_files(REPORTING_SKILL_DIR)
        if not name.endswith("/index.md")
    ]
    if VARIANT_DIR is not None:
        variant_md = variant_file("xas-reporting.SKILL.md").read_bytes()
        files = [(name, variant_md if name.endswith("/SKILL.md") else blob) for name, blob in files]
    return sorted(
        (name, render_vocabulary(blob.decode()).encode() if name.endswith("/SKILL.md") else blob)
        for name, blob in files
    )


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
    # Which pair is going out is not something to infer from the diff afterwards.
    print(f"Prompt + reporting skill: {VARIANT_DIR if VARIANT_DIR else 'the shipped pair'}\n")

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
