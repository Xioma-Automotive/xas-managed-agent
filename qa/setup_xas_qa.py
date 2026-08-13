"""Control plane (run once, re-runnable): create the cloud environment + agent for
the XAS dealership Q&A managed agent — Milestone 0 (mounted-data spike).

Milestone 0 has NO MCP and NO custom tools: the agent gets the terminology index
and the job-card records as *mounted files* (see run_qa.py) and answers by reading
them with its built-in bash/file tools, then draws a chart as a file. Milestone 1
swaps the mounted job-cards file for the live xas-app-mcp via a per-user vault
(Design A) — that only touches run_qa.py + the agent's tools/mcp_servers, not this
file's shape.

Usage:
    uv run python qa/setup_xas_qa.py
    # paste the printed XAS_QA_AGENT_ID / XAS_QA_ENV_ID into qa/.env

If all IDs are already in the env it updates in place instead of creating.
`agents.update()` PRESERVES omitted array fields, so we always send `tools`.
"""
from __future__ import annotations

import os
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BETA = "managed-agents-2026-04-01"
client = anthropic.Anthropic(default_headers={"anthropic-beta": BETA})

MODEL = os.environ.get("XAS_QA_MODEL", "claude-sonnet-4-5")

# Milestone 0: only the built-in toolset (bash + file tools). No custom tools,
# no MCP. Data arrives as mounted files; the agent reads them and writes a chart.
TOOLS = [{"type": "agent_toolset_20260401"}]

SYSTEM_PROMPT = """\
You are the XAS assistant for car-dealership staff (Xioma Automotive). You answer
questions about job cards (service jobs) and draw charts when asked.

Your data is mounted as files in your sandbox — there is NO network:
  /workspace/index.json     terminology index: maps human business terms (any
                            language, incl. Hebrew) to the SYSTEM values used in
                            the records. Match a term against classification /
                            status / field name + aliases (case-insensitive). A
                            status has a `name` (what people say) and a `system_id`
                            / `code` (what the record stores) — they can diverge, so
                            always resolve through the index, never guess a code.
  /workspace/jobcards.json  the job-card records to answer over.

Procedure every turn:
  1. Read /workspace/index.json to resolve the human terms in the question to the
     system codes / ids / field names you will filter and group on.
  2. Read /workspace/jobcards.json and compute the answer over it with real code
     (filter, count, group) — never eyeball it, never invent numbers.
  3. If the user wants a chart (or the answer is a breakdown that clearly wants
     one), WRITE A CHART FILE into /workspace — a PNG via matplotlib is fine
     (`pip install matplotlib` once if needed), or a self-contained .html/.svg.
     Use human-facing names on the chart (e.g. status "Closed", not code "97" or
     the ObjectId). Tell the user the exact filename you wrote.

Answer briefly, in the user's language, like a knowledgeable colleague. Never show
raw system codes, ObjectIds, or internal field names to the user — translate them
back to their human names via the index. If a lookup is ambiguous, ask one short
clarifying question instead of guessing. If the data has nothing, say so plainly.
"""


def create_environment() -> str:
    env = client.beta.environments.create(
        name="xas-qa-cloud",
        config={
            "type": "cloud",
            # Milestone 0 needs no egress. Package managers stay on so the agent
            # can `pip install matplotlib` to draw. Milestone 1 will add the MCP
            # host to allowed_hosts (or allow_mcp_servers) for Design A.
            "networking": {"type": "limited", "allow_package_managers": True, "allowed_hosts": []},
        },
    )
    return env.id


def check_cloud(environment_id: str) -> None:
    kind = client.beta.environments.retrieve(environment_id).config.type
    if kind != "cloud":
        sys.exit(f"Environment {environment_id} is {kind!r}, not 'cloud'. Refusing to cross-wire.")


def create_agent() -> str:
    agent = client.beta.agents.create(
        name="xas-qa", model=MODEL, system=SYSTEM_PROMPT, tools=TOOLS,
    )
    return agent.id


def update_agent(agent_id: str) -> None:
    client.beta.agents.update(agent_id, model=MODEL, system=SYSTEM_PROMPT, tools=TOOLS)


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY (org key) in qa/.env first.")

    env_id = os.environ.get("XAS_QA_ENV_ID")
    agent_id = os.environ.get("XAS_QA_AGENT_ID")

    if env_id and agent_id:
        check_cloud(env_id)
        update_agent(agent_id)
        print(f"Updated agent {agent_id} on environment {env_id} (model {MODEL}).")
        return

    env_id = env_id or create_environment()
    check_cloud(env_id)
    agent_id = create_agent()
    print("Created. Paste these into qa/.env:\n")
    print(f"XAS_QA_ENV_ID={env_id}")
    print(f"XAS_QA_AGENT_ID={agent_id}")


if __name__ == "__main__":
    main()
