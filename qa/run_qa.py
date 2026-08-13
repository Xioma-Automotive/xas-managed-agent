"""Run plane (Milestone 0): drive one XAS Q&A session against mounted data.

    uv run python qa/run_qa.py "how many service cards are in each status? draw a bar chart"
    uv run python qa/run_qa.py --he "כמה כרטיסי שירות סגורים יש?"

What it does: uploads the slim terminology index + the job-cards sample, mounts
them into a fresh sandbox session, sends the question, streams the agent's turn to
the terminal, and downloads any file the agent wrote (the chart) into ./qa-outputs/.

No network, no credentials — the sandbox only sees the two mounted files. Milestone 1
replaces the mounted jobcards file with the live xas-app-mcp via a per-user vault
(Design A); the index can stay mounted or also move behind the read path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

HERE = pathlib.Path(__file__).parent
load_dotenv(HERE / ".env")

BETA = "managed-agents-2026-04-01"
client = AsyncAnthropic(default_headers={"anthropic-beta": BETA})

ENV_ID = os.environ.get("XAS_QA_ENV_ID", "")
AGENT_ID = os.environ.get("XAS_QA_AGENT_ID", "")
MODEL = os.environ.get("XAS_QA_MODEL", "claude-sonnet-4-5")

INDEX_FILE = HERE / "data" / "sample_index.json"
JOBCARDS_FILE = HERE / "data" / "sample_jobcards.json"
INPUT_NAMES = {"index.json", "jobcards.json"}
OUT_DIR = HERE.parent / "qa-outputs"


async def _mount(name: str, path: pathlib.Path) -> str:
    meta = await client.beta.files.upload(
        file=(name, path.read_bytes(), "application/json"), betas=[BETA]
    )
    return meta.id


async def run(question: str) -> None:
    if not (ENV_ID and AGENT_ID):
        raise SystemExit("Set XAS_QA_ENV_ID and XAS_QA_AGENT_ID in qa/.env (run setup_xas_qa.py).")

    index_id = await _mount("index.json", INDEX_FILE)
    jobcards_id = await _mount("jobcards.json", JOBCARDS_FILE)

    session = await client.beta.sessions.create(
        agent={"type": "agent_with_overrides", "id": AGENT_ID, "model": MODEL},
        environment_id=ENV_ID,
        title="xas-qa milestone 0",
        resources=[
            {"type": "file", "file_id": index_id, "mount_path": "/workspace/index.json"},
            {"type": "file", "file_id": jobcards_id, "mount_path": "/workspace/jobcards.json"},
        ],
    )
    print(f"session {session.id}\n")

    await client.beta.sessions.events.send(
        session_id=session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": question}]}],
    )

    # Stream the turn to the terminal until the session goes idle/terminated.
    async with await client.beta.sessions.events.stream(session.id) as stream:
        async for event in stream:
            kind = getattr(event, "type", "")
            if kind == "agent.message":
                text = "".join(b.text for b in event.content if getattr(b, "type", None) == "text")
                if text:
                    print(text)
            elif kind == "agent.tool_use":
                detail = ""
                if isinstance(getattr(event, "input", None), dict):
                    detail = event.input.get("command") or event.input.get("file_path") or ""
                print(f"  · {event.name}: {str(detail)[:120]}")
            elif kind in ("session.status_idle", "session.status_terminated"):
                break

    # Download whatever the agent wrote (the chart), skipping our mounted inputs.
    OUT_DIR.mkdir(exist_ok=True)
    listing = await client.beta.files.list(scope_id=session.id, betas=[BETA])
    saved = []
    for f in listing.data:
        if f.filename in INPUT_NAMES:
            continue
        content = await client.beta.files.download(f.id)
        target = OUT_DIR / f.filename
        await content.write_to_file(str(target))
        saved.append(str(target))
    print("\nsaved:", ", ".join(saved) if saved else "(no output files)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--he", action="store_true", help="(no-op flag; ask in Hebrew directly)")
    args = ap.parse_args()
    asyncio.run(run(args.question))


if __name__ == "__main__":
    main()
