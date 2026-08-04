#!/usr/bin/env python3
"""Data-plane driver for the XAS Allocation Agent Managed Agent.

RUN EVERY CONVERSATION. Loads the IDs produced by setup_allocation_agent.py,
opens a session against the pre-created agent, and drives the §8 loop:

  1. Materialize the pinned reference solver into the sandbox and run the
     determinism invariant test as a SMOKE TEST — the billing agent's
     "surface failures on first use" philosophy, here proving the exact
     reference solver runs deterministically in this sandbox before any real
     work. Determinism is the reference solver's, never the model re-deriving it.
  2. A real steering turn: the planner describes the disruption + instruction in
     natural language; the agent compiles it to a typed override object, shows it
     back, appends it to the append-only ledger, runs the λ sweep, self-checks,
     and returns a reason-coded change list.
  3. Download whatever the agent wrote to /mnt/session/outputs/ (ledger, plan,
     change list).

This script NEVER creates agents/environments/vaults — setup does that once.

Prototype notes / open decisions:
- DECIDE-7: the real XAS pull/write-back MCP does not exist. The reference solver
  ships with a synthetic generator; the agent runs that in-sandbox.
- DECIDE-5: session persistence is not assumed as a platform primitive — the
  ledger is a JSON artifact in /mnt/session/outputs, so replay is provable.
"""

import os
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

ALLOC_AGENT_ID = os.environ.get("ALLOC_AGENT_ID")
ALLOC_ENV_ID = os.environ.get("ALLOC_ENV_ID")
ALLOC_VAULT_ID = os.environ.get("ALLOC_VAULT_ID")

missing = [
    name
    for name, value in (
        ("ALLOC_AGENT_ID", ALLOC_AGENT_ID),
        ("ALLOC_ENV_ID", ALLOC_ENV_ID),
        ("ALLOC_VAULT_ID", ALLOC_VAULT_ID),
    )
    if not value
]
if missing:
    sys.exit(
        "Missing required .env values: "
        + ", ".join(missing)
        + "\nRun `python setup_allocation_agent.py` and paste the printed IDs into .env first."
    )

client = anthropic.Anthropic()
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"

REPO_ROOT = Path(__file__).resolve().parent
# The exact reference solver + tests to push into the sandbox. Paths are
# reproduced verbatim under /mnt/session so imports resolve unchanged.
PACKAGE_FILES = [
    "xas_allocation/__init__.py",
    "xas_allocation/decisions.py",
    "xas_allocation/synth_data.py",
    "xas_allocation/spec_match.py",
    "xas_allocation/solver.py",
    "xas_allocation/ledger.py",
    "xas_allocation/session.py",
    "xas_allocation/overrides_schema.json",
    "tests/test_invariant.py",
]


def _materialize_message() -> str:
    """Bundle the reference solver into one setup message the agent writes to
    disk. Uses only the send-message surface — no speculative upload endpoint."""
    parts = [
        "Set up the pinned XAS allocation reference solver in this sandbox, then "
        "prove it is deterministic. Do EXACTLY this:\n",
        "1. Write each file below to the given path under /mnt/session/ "
        "(create directories as needed). Copy the contents byte-for-byte.\n",
        "2. `pip install ortools` (the environment allows package managers).\n",
        "3. Run: `cd /mnt/session && PYTHONPATH=. python tests/test_invariant.py`\n",
        "4. Report ONLY the test summary line (e.g. '5/5 passed') and, if anything "
        "failed, the failing assertion. Do not build a plan yet.\n",
        "\n--- FILES ---\n",
    ]
    for rel in PACKAGE_FILES:
        content = (REPO_ROOT / rel).read_text()
        lang = "json" if rel.endswith(".json") else "python"
        parts.append(f"\n### path: {rel}\n```{lang}\n{content}\n```\n")
    return "".join(parts)


STEERING_TURN = (
    "Now run one repair cycle on the synthetic pull (seed=20, spare_ratio=0.2, "
    "delay_weeks=2), then apply this planner steering:\n\n"
    '  "For the delayed shipment, defer order 4000 to no earlier than 2026-W38, '
    'and prefer Colmobil this cycle. Keep churn moderate."\n\n'
    "Follow the skill procedure: compile the instruction to a typed override "
    "object and SHOW IT BACK first; append it to an append-only ledger at "
    "/mnt/session/outputs/ledger.json; replay the ledger; run the λ sweep; "
    "self-check the hard constraints; and return the λ frontier plus a "
    "reason-coded change list. Write the ledger and the chosen plan (as JSON) and "
    "the change list (as text) to /mnt/session/outputs/. Do NOT write back to XAS "
    "— this is a proposal for the planner to approve."
)


def drain(stream) -> None:
    for event in stream:
        if event.type == "agent.message":
            for block in event.content:
                if block.type == "text":
                    print(block.text, end="", flush=True)
        elif event.type == "session.status_terminated":
            print()
            return
        elif event.type == "session.status_idle":
            if event.stop_reason.type != "requires_action":
                print()
                return
        elif event.type == "session.error":
            message = getattr(getattr(event, "error", None), "message", "unknown error")
            print(f"\n[session error] {message}", flush=True)


def send_and_drain(session_id: str, text: str) -> None:
    with client.beta.sessions.events.stream(session_id=session_id) as stream:
        client.beta.sessions.events.send(
            session_id=session_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
        )
        drain(stream)


def download_outputs(session_id: str, out_dir: str = "./out") -> None:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    files = []
    for attempt in range(3):
        listing = client.beta.files.list(scope_id=session_id, betas=[MANAGED_AGENTS_BETA])
        files = list(listing.data)
        if files:
            break
        if attempt < 2:
            time.sleep(2)
    if not files:
        print("No output files found for this session.")
        return
    for f in files:
        content = client.beta.files.download(f.id)
        safe_name = os.path.basename(f.filename) or f.id
        content.write_to_file(os.path.join(out_dir, safe_name))
        print(f"Saved: {os.path.join(out_dir, safe_name)} ({f.size_bytes} bytes)")


def main() -> None:
    session = client.beta.sessions.create(
        agent=ALLOC_AGENT_ID,
        environment_id=ALLOC_ENV_ID,
        vault_ids=[ALLOC_VAULT_ID],
        title="XAS allocation repair session",
    )
    print(f"Session:  {session.id}")
    print(
        "Console trace: "
        f"https://platform.claude.com/workspaces/default/sessions/{session.id}\n"
    )

    # 1. Smoke test = determinism proof of the exact reference solver in-sandbox.
    print("--- Materializing reference solver + running the invariant test ---")
    send_and_drain(session.id, _materialize_message())

    # 2. Real turn — the §8 loop with one steering instruction. Swap in a live
    #    planner prompt via input() for interactive use.
    print("\n--- Repair cycle + steering ---")
    try:
        request = input("planner instruction (blank = use the default demo)> ").strip()
    except EOFError:
        request = ""
    send_and_drain(session.id, request or STEERING_TURN)

    # 3. Pull down the ledger, plan, and change list.
    print("\n--- Downloading session outputs ---")
    download_outputs(session.id)


if __name__ == "__main__":
    main()
