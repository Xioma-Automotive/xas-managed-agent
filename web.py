#!/usr/bin/env python3
"""Web interface for the XAS Allocation Agent.

Thin FastAPI server between the browser and the Managed Agents session API.

  uv run uvicorn web:app --reload --port 8000

One session is active at a time. That is not a UI convention imposed on the
backend — worker.py serves sessions sequentially, so a second live session would
simply queue behind the first with nothing to show for it. Starting a new session
therefore stops the current one and archives its sandbox directory, which is what
gives the session list something to point at.

This process holds the organization API key: it creates and stops sessions and
reads their event streams. It runs no tool calls. worker.py, which does run them,
holds only the environment key and refuses to start if it finds an org key.
"""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from anthropic import APIError, AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("web")

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
ALLOC_AGENT_ID = os.environ.get("ALLOC_AGENT_ID")
ALLOC_ENV_ID = os.environ.get("ALLOC_ENV_ID")
# Both outside the repo, matching worker.py: bash is not confined to the workdir,
# and a sandbox inside the repo puts .env one `cd ..` away.
SANDBOX_ROOT = Path(
    os.environ.get("ALLOC_SANDBOX_ROOT") or Path.home() / "xas-alloc-sandbox"
).expanduser()
SESSIONS_ROOT = Path(
    os.environ.get("ALLOC_SESSIONS_ROOT") or Path.home() / "xas-alloc-sessions"
).expanduser()

# Per-session model overrides. The agent resource keeps whatever
# setup_allocation_agent.py gave it; picking a model here never mutates it.
MODELS = {
    "opus": {"id": "claude-opus-5", "label": "Opus 5"},
    "sonnet": {"id": "claude-sonnet-5", "label": "Sonnet 5"},
    "haiku": {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
}
DEFAULT_MODEL = "opus"

app = FastAPI(title="XAS Allocation Agent")
client = AsyncAnthropic()

# The one active session. A single slot, deliberately: see the module docstring.
_active: str | None = None
_lock = asyncio.Lock()


class NewSession(BaseModel):
    model: str = DEFAULT_MODEL
    title: str | None = None


class Message(BaseModel):
    text: str


def _require_config() -> None:
    if not (ALLOC_AGENT_ID and ALLOC_ENV_ID):
        raise HTTPException(
            500,
            "ALLOC_AGENT_ID / ALLOC_ENV_ID are not set. Run setup_allocation_agent.py "
            "and paste the printed IDs into .env.",
        )


def _archive_sandbox(session_id: str) -> str | None:
    """Move the finished session's working files out of the shared sandbox.

    The sandbox directory is flat and reused, so this is what keeps one session's
    ledger, snapshot, and plan from being read as the next one's.
    """
    if not SANDBOX_ROOT.exists() or not any(SANDBOX_ROOT.iterdir()):
        return None
    destination = SESSIONS_ROOT / session_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.move(str(SANDBOX_ROOT), str(destination))
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    log.info("archived sandbox for %s -> %s", session_id, destination)
    return str(destination)


async def _interrupt(session_id: str) -> None:
    await client.beta.sessions.events.send(
        session_id=session_id, events=[{"type": "user.interrupt"}]
    )


async def _stop(session_id: str) -> str | None:
    """Interrupt the agent, archive the session, archive its files.

    Both API calls are allowed to fail: a session that already terminated on its
    own rejects them, and that is the ordinary case rather than an error. What
    must not be skipped is the sandbox archive below — leave it out and the next
    session inherits this one's ledger.
    """
    try:
        await _interrupt(session_id)
    except APIError as e:  # already terminated, or never started
        log.info("interrupt on %s: %s", session_id, e)
    try:
        await client.beta.sessions.archive(session_id)
    except APIError as e:
        log.info("archive on %s: %s", session_id, e)
    return _archive_sandbox(session_id)


def _render(event) -> dict | None:
    """One session event -> what the browser shows, or None to drop it.

    Tool results are dropped: they are the snapshot summary and solver output,
    which belong in the sandbox and the agent's own reply, not in the transcript.
    """
    kind = event.type
    if kind == "agent.message":
        text = "".join(b.text for b in event.content if b.type == "text")
        return {"type": "agent", "text": text}
    if kind == "user.message":
        text = "".join(b.text for b in event.content if getattr(b, "type", None) == "text")
        return {"type": "user", "text": text}
    if kind == "agent.thinking":
        return {"type": "thinking"}
    if kind == "agent.tool_use":
        detail = event.input.get("command") or event.input.get("file_path") or ""
        return {"type": "tool", "name": event.name, "detail": str(detail)[:200]}
    if kind == "agent.custom_tool_use":
        return {"type": "tool", "name": event.name, "detail": json.dumps(event.input)[:200]}
    if kind == "session.status_running":
        return {"type": "status", "status": "running"}
    if kind == "session.status_idle":
        stop_reason = getattr(getattr(event, "stop_reason", None), "type", "")
        return {"type": "status", "status": "idle", "stop_reason": stop_reason}
    if kind == "session.status_terminated":
        return {"type": "status", "status": "terminated"}
    if kind == "session.error":
        message = getattr(getattr(event, "error", None), "message", "unknown error")
        return {"type": "error", "text": message}
    return None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(REPO_ROOT / "static" / "index.html")


@app.get("/models")
async def models() -> dict:
    return {"models": MODELS, "default": DEFAULT_MODEL}


@app.get("/sessions")
async def sessions() -> dict:
    """The session list is the API's, not a local database."""
    _require_config()
    listing = await client.beta.sessions.list(
        agent_id=ALLOC_AGENT_ID, limit=50, order="desc", include_archived=True
    )
    return {
        "active": _active,
        "sessions": [
            {
                "id": s.id,
                "title": s.title,
                "status": s.status,
                "model": s.agent.model.id if s.agent and s.agent.model else None,
                "created_at": s.created_at.isoformat(),
                "archived": s.archived_at is not None,
                "files": str(SESSIONS_ROOT / s.id) if (SESSIONS_ROOT / s.id).exists() else None,
            }
            for s in listing.data
        ],
    }


@app.post("/session")
async def new_session(body: NewSession) -> dict:
    """Stop whatever is running, then start fresh."""
    _require_config()
    global _active

    if body.model not in MODELS:
        raise HTTPException(400, f"unknown model {body.model!r}")

    async with _lock:
        previous, archived = None, None
        if _active:
            previous, archived = _active, await _stop(_active)
            _active = None

        session = await client.beta.sessions.create(
            agent={
                "type": "agent_with_overrides",
                "id": ALLOC_AGENT_ID,
                "model": MODELS[body.model]["id"],
            },
            environment_id=ALLOC_ENV_ID,
            title=body.title or "XAS allocation repair",
        )
        _active = session.id

    log.info("session %s started (%s)", session.id, MODELS[body.model]["id"])
    return {"id": session.id, "model": body.model, "stopped": previous, "archived": archived}


@app.post("/session/interrupt")
async def interrupt() -> dict:
    """Stop the agent mid-run without ending the session."""
    if not _active:
        raise HTTPException(409, "no active session")
    await _interrupt(_active)
    return {"interrupted": _active}


@app.post("/session/stop")
async def stop() -> dict:
    """Stop the agent and end the session."""
    global _active
    if not _active:
        raise HTTPException(409, "no active session")
    async with _lock:
        session_id, archived = _active, await _stop(_active)
        _active = None
    return {"stopped": session_id, "archived": archived}


@app.get("/session/{session_id}/events")
async def events(session_id: str) -> EventSourceResponse:
    """Replay what the session has already produced, then follow it live.

    The replay is what makes a browser reload work: the stream carries no
    history, so a page that reconnects mid-run would otherwise show an empty
    transcript for a session that is busy working.
    """
    _require_config()

    async def relay():
        seen: set[str] = set()
        history = await client.beta.sessions.events.list(session_id, order="asc", limit=200)
        for event in history.data:
            seen.add(event.id)
            rendered = _render(event)
            if rendered:
                yield {"data": json.dumps(rendered)}

        async with await client.beta.sessions.events.stream(session_id) as stream:
            async for event in stream:
                if getattr(event, "id", None) in seen:
                    continue
                rendered = _render(event)
                if rendered:
                    yield {"data": json.dumps(rendered)}

    return EventSourceResponse(relay())


@app.post("/message")
async def message(body: Message) -> dict:
    if not _active:
        raise HTTPException(409, "no active session")
    await client.beta.sessions.events.send(
        session_id=_active,
        events=[{"type": "user.message", "content": [{"type": "text", "text": body.text}]}],
    )
    return {"sent": _active}
