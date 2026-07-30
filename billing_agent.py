#!/usr/bin/env python3
"""Data-plane driver for the billing dashboard Managed Agent.

RUN EVERY CONVERSATION. Loads the three IDs produced by setup_agent.py, opens a
session against the pre-created agent, runs a cheap smoke-test turn (so
credential/network failures surface on first use rather than silently), then a
real turn where the user describes the dashboard they want. Afterwards it
downloads whatever the agent wrote to /mnt/session/outputs/.

This script NEVER creates agents/environments/vaults — those are set up once by
setup_agent.py and referenced here by ID only.
"""

import os
import sys
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

AGENT_ID = os.environ.get("AGENT_ID")
ENV_ID = os.environ.get("ENV_ID")
VAULT_ID = os.environ.get("VAULT_ID")

missing = [
    name
    for name, value in (("AGENT_ID", AGENT_ID), ("ENV_ID", ENV_ID), ("VAULT_ID", VAULT_ID))
    if not value
]
if missing:
    sys.exit(
        "Missing required .env values: "
        + ", ".join(missing)
        + "\nRun `python setup_agent.py` and paste the printed IDs into .env first."
    )

client = anthropic.Anthropic()

# The managed-agents beta header is set automatically by the SDK on
# client.beta.{agents,sessions,environments,vaults}.* calls. The Files API
# needs it passed explicitly for session-scoped listing (see download step).
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"

SMOKE_TEST = (
    "Do exactly one authenticated GET request against the billing API to confirm "
    "connectivity and auth. Report the HTTP status code and a one-line summary of "
    "the response. Do NOT build a dashboard or do any analysis yet."
)


def drain(stream) -> None:
    """Print agent.message text from an open event stream and return when the
    turn is truly done.

    Returns on session.status_terminated, or on session.status_idle whose
    stop_reason.type is anything other than `requires_action` (i.e. end_turn or
    retries_exhausted). A bare idle is transient — the agent may go idle between
    parallel tool calls — so we do not break on idle alone.
    """
    for event in stream:
        if event.type == "agent.message":
            for block in event.content:
                if block.type == "text":
                    print(block.text, end="", flush=True)
        elif event.type == "session.status_terminated":
            print()  # newline after streamed text
            return
        elif event.type == "session.status_idle":
            if event.stop_reason.type != "requires_action":
                print()
                return
        elif event.type == "session.error":
            message = getattr(getattr(event, "error", None), "message", "unknown error")
            print(f"\n[session error] {message}", flush=True)


def send_and_drain(session_id: str, text: str) -> None:
    """Stream-first: open the event stream BEFORE sending the message, so we
    never miss early events, then drain to completion."""
    with client.beta.sessions.events.stream(session_id=session_id) as stream:
        client.beta.sessions.events.send(
            session_id=session_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
        )
        drain(stream)


def download_outputs(session_id: str, out_dir: str = ".") -> None:
    """Download files the agent wrote to /mnt/session/outputs/ and save to disk.

    There's a brief (~1-3s) indexing lag between idle and files appearing, so
    retry the listing a couple of times before giving up.
    """
    files = []
    for attempt in range(3):
        listing = client.beta.files.list(
            scope_id=session_id,
            betas=[MANAGED_AGENTS_BETA],
        )
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
        # basename guards against any path components in the filename
        safe_name = os.path.basename(f.filename) or f.id
        out_path = os.path.join(out_dir, safe_name)
        content.write_to_file(out_path)
        print(f"Saved: {out_path} ({f.size_bytes} bytes)")


def main() -> None:
    session = client.beta.sessions.create(
        agent=AGENT_ID,
        environment_id=ENV_ID,
        vault_ids=[VAULT_ID],
        title="Billing dashboard session",
    )
    # Swap 'default' for your workspace ID if the API key is not in the
    # Default workspace (the session response does not carry the workspace).
    print(f"Session:  {session.id}")
    print(
        "Console trace: "
        f"https://platform.claude.com/workspaces/default/sessions/{session.id}\n"
    )

    # 1. Cheap smoke test — surfaces credential/network failures on first use,
    #    which don't appear at session-create time.
    print("--- Smoke test: verifying billing API connectivity ---")
    send_and_drain(session.id, SMOKE_TEST)

    # 2. Real turn — the user describes the dashboard they want.
    print("\n--- Describe the dashboard you'd like ---")
    try:
        request = input("> ").strip()
    except EOFError:
        request = ""
    if not request:
        request = (
            "Build an executive billing overview dashboard: revenue trend over the "
            "most recent period available, top movers by customer or product, and "
            "any anomalies worth flagging."
        )
        print(f"(no input — using default request)\n{request}")

    print("\n--- Building dashboard ---")
    send_and_drain(session.id, request)

    # 3. Pull down whatever the agent wrote to /mnt/session/outputs/.
    print("\n--- Downloading session outputs ---")
    download_outputs(session.id)


if __name__ == "__main__":
    main()
