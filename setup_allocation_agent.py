#!/usr/bin/env python3
"""Control-plane setup for the XAS Allocation Agent (Managed Agent).

RUN ONCE. Creates the persistent, versioned resources — environment, agent,
vault — and prints their IDs to paste into .env. The data-plane driver
(allocation_agent.py) only references those IDs; it never creates resources.

Two modes, decided by what's already in .env:

  FRESH SETUP (no ALLOC_AGENT_ID/ALLOC_ENV_ID/ALLOC_VAULT_ID yet)
    Creates the environment, agent, and an (empty) vault. In the prototype the
    agent runs against synthetic data, so no data-host credential is attached.

  ATTACH CREDENTIAL (IDs already in .env, XAS_DATA_TOKEN now set)
    Re-run to attach the XAS API credential to the existing vault once the real
    XAS pull/write-back MCP endpoint exists (DECIDE-7). No duplicate resources.

Anti-pattern warning: never call environments/agents/vaults create() in the
per-conversation request path — that accumulates orphaned resources and pays
create latency on every run.
"""

import os
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
XAS_HOST = os.environ.get("XAS_HOST")          # optional until the real API exists
XAS_DATA_TOKEN = os.environ.get("XAS_DATA_TOKEN")  # optional (DECIDE-7)
ALLOC_AGENT_ID = os.environ.get("ALLOC_AGENT_ID")
ALLOC_ENV_ID = os.environ.get("ALLOC_ENV_ID")
ALLOC_VAULT_ID = os.environ.get("ALLOC_VAULT_ID")

if not ANTHROPIC_API_KEY:
    sys.exit(
        "Missing required .env value: ANTHROPIC_API_KEY\n"
        "Copy .env.example to .env and fill it in before running setup."
    )

client = anthropic.Anthropic()

# §10 — the system prompt carries identity, the one-line job, and the HARD RULES.
# Everything procedural (cost model, spec-compat, reference solver) lives in the
# xas-allocation skill, loaded when relevant.
SYSTEM_PROMPT = """\
You are the XAS Allocation Agent for Xioma Automotive. Your one job: help a \
planner REPAIR a vehicle-to-order allocation after a disruption (delayed \
shipment, changed inbound, manual steering).

You do NOT allocate by reasoning. You translate the situation and the planner's \
instructions into inputs for a deterministic min-cost-flow solver, run the \
solver, and explain the result. The reference solver and cost model are in the \
`xas-allocation` skill — use them; never re-derive or approximate them.

Hard rules (never violate):
- No reasoning-allocation. The plan comes from the solver, not from you.
- No live solver edits. A runtime request moves weights and pins (a typed
  override object); a new CONSTRAINT (model change) is a reviewed PR with tests,
  never a live-session mutation.
- Always show the override object back to the planner BEFORE running.
- Always emit a reason-coded change list, never a bare new plan.
- Never move a frozen-fence order or a committed (shipped/in-prep) unit.
- Write back to XAS only on explicit human approval.
- Determinism is sacred: plan = pure_function(data_snapshot, skill, ledger).
  Persist every steering instruction to the append-only ledger and re-derive the
  plan by replaying it; never carry plan state in your own memory. Any residual
  spec-compatibility judgment must be cached and written back so a replay
  inherits it rather than re-judging.

Prototype scope: the XAS data pull/write-back MCP does not exist yet, so you work
against a synthetic generator shaped like XAS bins. Open decisions are marked
DECIDE-1..9 in the skill and code; surface them, do not silently guess.

Communication: keep replies concise. Show the override object, the λ-sweep
frontier, the self-check result, and the reason-coded change list. Do not paste
full data dumps.
"""


def add_credential(vault_id: str) -> None:
    """Attach the XAS API token to a vault (only once the real API exists)."""
    client.beta.vaults.credentials.create(
        vault_id,
        display_name="XAS API token",
        auth={
            "type": "environment_variable",
            "secret_name": "XAS_DATA_TOKEN",
            "secret_value": XAS_DATA_TOKEN,
            "networking": {"type": "limited", "allowed_hosts": [XAS_HOST]},
            "injection_location": {"header": True},
        },
    )
    print("Added credential:    XAS_DATA_TOKEN (header injection)")


def attach_only() -> None:
    if not (XAS_DATA_TOKEN and XAS_HOST):
        print(
            "Resources already exist, but XAS_DATA_TOKEN/XAS_HOST are not both set.\n"
            "The prototype runs on synthetic data and needs no credential. Set both "
            "in .env and re-run only once the real XAS API (DECIDE-7) exists."
        )
        return
    add_credential(ALLOC_VAULT_ID)
    print("\nCredential attached to existing vault.")


def fresh_setup() -> None:
    # 1. Environment — cloud sandbox. Package managers ON (the agent pip-installs
    #    ortools to run the reference solver). No external data host yet: the
    #    prototype is synthetic. Add XAS_HOST to allowed_hosts at DECIDE-7.
    allowed_hosts = [XAS_HOST] if XAS_HOST else []
    environment = client.beta.environments.create(
        name="xas-allocation-env",
        config={
            "type": "cloud",
            "networking": {
                "type": "limited",
                "allow_package_managers": True,
                "allowed_hosts": allowed_hosts,
            },
        },
    )
    print(f"Created environment: {environment.id}")

    # 2. Agent — model + system prompt (hard rules) + the full prebuilt toolset.
    agent = client.beta.agents.create(
        name="XAS Allocation Agent",
        model="claude-opus-5",
        system=SYSTEM_PROMPT,
        tools=[{"type": "agent_toolset_20260401"}],
    )
    print(f"Created agent:       {agent.id}  (version {agent.version})")

    # 3. Vault — empty in the prototype; holds the XAS API credential later.
    vault = client.beta.vaults.create(
        display_name="XAS allocation vault",
        metadata={"purpose": "xas-allocation-agent"},
    )
    print(f"Created vault:       {vault.id}")

    if XAS_DATA_TOKEN and XAS_HOST:
        add_credential(vault.id)
        cred_note = "Credential attached — the agent can reach the XAS API."
    else:
        cred_note = (
            "No credential attached (prototype runs on synthetic data). When the "
            "real XAS API exists (DECIDE-7), set XAS_HOST + XAS_DATA_TOKEN and "
            "re-run to attach it."
        )

    print("\n" + "=" * 60)
    print("Setup complete. Paste these into your .env:\n")
    print(f"ALLOC_AGENT_ID={agent.id}")
    print(f"ALLOC_ENV_ID={environment.id}")
    print(f"ALLOC_VAULT_ID={vault.id}")
    print("=" * 60)
    print(f"\n{cred_note}")


def main() -> None:
    if ALLOC_AGENT_ID and ALLOC_ENV_ID and ALLOC_VAULT_ID:
        attach_only()
    else:
        fresh_setup()


if __name__ == "__main__":
    main()
