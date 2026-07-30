#!/usr/bin/env python3
"""Control-plane setup for the billing dashboard Managed Agent.

Two modes, decided by what's already in .env:

  FRESH SETUP (no AGENT_ID/ENV_ID/VAULT_ID yet)
    Creates the environment, the agent, and a vault. If BILLING_DATA_TOKEN is
    set, also attaches the billing credential; otherwise it's skipped and you
    can add it later. Prints the three IDs to paste into .env.

  ATTACH CREDENTIAL (IDs already in .env, BILLING_DATA_TOKEN now set)
    Re-run to attach the billing credential to the existing vault — no
    duplicate resources are created (environment names must be unique, so a
    blind re-create would fail).

Anti-pattern warning: never call environments/agents/vaults create() in the
per-conversation request path — that accumulates orphaned resources and pays
create latency on every run. This is the one-time setup step; the data-plane
script only references the IDs it prints.
"""

import os
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BILLING_DATA_TOKEN = os.environ.get("BILLING_DATA_TOKEN")
BILLING_HOST = os.environ.get("BILLING_HOST")
AGENT_ID = os.environ.get("AGENT_ID")
ENV_ID = os.environ.get("ENV_ID")
VAULT_ID = os.environ.get("VAULT_ID")

# ANTHROPIC_API_KEY and BILLING_HOST are always required. BILLING_DATA_TOKEN is
# optional — you can stand everything up now and attach the credential later.
missing = [
    name
    for name, value in (
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ("BILLING_HOST", BILLING_HOST),
    )
    if not value
]
if missing:
    sys.exit(
        "Missing required .env values: "
        + ", ".join(missing)
        + "\nCopy .env.example to .env and fill them in before running setup."
    )

# The Anthropic() client picks up ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic()

SYSTEM_PROMPT = """\
You are a billing analytics agent for Xioma Automotive. You read billing data \
from an internal HTTP API and turn it into clear, self-contained HTML dashboards.

Data access
- The billing API base host is provided via the environment. Make authenticated
  HTTPS requests to it. Your credential is injected automatically at egress into
  the Authorization header (Bearer) — you only ever see a placeholder, so do NOT
  print, log, echo, or try to read the token, and do not hardcode it.
- Only the billing API host is reachable. Package managers are available for
  installing helper libraries.

Analysis
- Surface concrete, decision-useful insights: trends over time, anomalies and
  outliers, top movers (largest increases/decreases), and notable
  concentrations. Quantify everything — percentages, deltas, and absolute
  figures — and say what the numbers mean.
- Verify every number you report against the source data. Never invent, estimate,
  or extrapolate a figure you did not compute from the API response. If the data
  is missing or ambiguous, say so rather than guessing.

Deliverable
- Write self-contained HTML dashboards to /mnt/session/outputs/. Each file must
  be a single standalone .html document: all CSS inline, all chart rendering done
  with inline SVG or inline <script> (no external stylesheets, fonts, images, CDN
  scripts, or network requests). It must render correctly opened directly from
  disk with no internet connection.
- Make dashboards readable: a clear title, a short summary of the key findings at
  the top, then the charts and supporting tables. Label axes and units.

Communication
- Keep chat replies concise. Report what you built, where you wrote it, and the
  headline insights — do not paste the full HTML or long data dumps into chat.
"""


def add_credential(vault_id: str) -> None:
    """Attach the billing token to a vault as an environment-variable credential.

    The token is stored by Anthropic and substituted into the outbound
    Authorization header at egress — the sandbox only ever sees an opaque
    placeholder.
    """
    client.beta.vaults.credentials.create(
        vault_id,
        display_name="Billing API token",
        auth={
            "type": "environment_variable",
            "secret_name": "BILLING_DATA_TOKEN",
            "secret_value": BILLING_DATA_TOKEN,
            "networking": {
                "type": "limited",
                "allowed_hosts": [BILLING_HOST],
            },
            # Substitute the secret into the Authorization header only, never
            # into request bodies.
            "injection_location": {"header": True},
        },
    )
    print("Added credential:    BILLING_DATA_TOKEN (header injection)")


def attach_only() -> None:
    """IDs already exist — just attach the credential to the existing vault."""
    if not BILLING_DATA_TOKEN:
        print(
            "Resources already exist (AGENT_ID/ENV_ID/VAULT_ID are set), but "
            "BILLING_DATA_TOKEN is empty.\nSet it in .env and re-run to attach "
            "the billing credential."
        )
        return
    add_credential(VAULT_ID)
    print("\nCredential attached to existing vault. The agent can now reach the billing API.")


def fresh_setup() -> None:
    # 1. Environment — cloud, limited networking scoped to the billing host.
    environment = client.beta.environments.create(
        name="billing-dashboard-env",
        config={
            "type": "cloud",
            "networking": {
                "type": "limited",
                "allow_package_managers": True,
                "allowed_hosts": [BILLING_HOST],
            },
        },
    )
    print(f"Created environment: {environment.id}")

    # 2. Agent — model, system prompt, and the full prebuilt toolset live here.
    agent = client.beta.agents.create(
        name="Billing Dashboard Agent",
        model="claude-opus-5",
        system=SYSTEM_PROMPT,
        tools=[{"type": "agent_toolset_20260401"}],
    )
    print(f"Created agent:       {agent.id}  (version {agent.version})")

    # 3. Vault — the credential store. Created empty; the credential is added
    #    now if we have the token, or later via a re-run.
    vault = client.beta.vaults.create(
        display_name="Billing API vault",
        metadata={"purpose": "billing-dashboard-agent"},
    )
    print(f"Created vault:       {vault.id}")

    if BILLING_DATA_TOKEN:
        add_credential(vault.id)
        cred_note = "Credential attached — the agent can reach the billing API."
    else:
        cred_note = (
            "Credential SKIPPED (BILLING_DATA_TOKEN was empty). Add the token to "
            ".env and re-run this script to attach it before the agent can reach "
            "the billing API."
        )

    print("\n" + "=" * 60)
    print("Setup complete. Paste these into your .env:\n")
    print(f"AGENT_ID={agent.id}")
    print(f"ENV_ID={environment.id}")
    print(f"VAULT_ID={vault.id}")
    print("=" * 60)
    print(f"\n{cred_note}")


def main() -> None:
    if AGENT_ID and ENV_ID and VAULT_ID:
        attach_only()
    else:
        fresh_setup()


if __name__ == "__main__":
    main()
