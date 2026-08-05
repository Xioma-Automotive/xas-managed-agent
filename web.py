#!/usr/bin/env python3
"""Web interface for the XAS Allocation Agent (Anthropic-hosted sandbox).

Thin FastAPI server between the browser and the Managed Agents session API.

  uv run uvicorn web:app --reload --port 8000

The ONLY process. The sandbox is Anthropic's, so nothing here executes the
agent's bash / file tools and there is no worker to run alongside.

What this process still owes the session is the one **custom** tool. A custom
tool is answered by the API client wherever the sandbox lives: the agent emits
``agent.custom_tool_use`` and the session idles on ``requires_action`` until a
``user.custom_tool_result`` arrives. So each session gets a background
``tool_runner`` task here that answers ``pull_allocation_snapshot`` and leaves
every other tool name alone for the cloud sandbox to handle.

That task must not depend on a browser being attached — a session parked on a
pending tool call never times out, so a page close would hang it indefinitely.
It is owned by the session lifecycle, not by the event stream.

One session is active at a time. With a cloud sandbox that is a product choice
rather than a constraint (nothing queues), kept because the ledger and the
planner's attention are both singular.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from anthropic import APIError, AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import alloc_tools

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("web")

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
ALLOC_AGENT_ID = os.environ.get("ALLOC_AGENT_ID")
ALLOC_ENV_ID = os.environ.get("ALLOC_ENV_ID")
DOWNLOAD_DIR = Path(
    os.environ.get("ALLOC_DOWNLOAD_DIR") or Path.home() / "xas-alloc-outputs"
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

# The one active session, and the task answering its custom tool calls.
_active: str | None = None
_answering: asyncio.Task | None = None
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


def _digest(call) -> str:
    """One line describing what the pull returned, for the server log."""
    blocks = getattr(getattr(call, "result", None), "content", None) or []
    body = "".join(
        b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") for b in blocks
    )
    if not body:
        return "no result body"
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        return f"{len(body)} chars"
    disruption = d.get("disruption") or {}
    return (
        f"now={d.get('now')} orders={d.get('orders')} vehicles={d.get('vehicles')} "
        f"disruption={disruption.get('pdn')}+{disruption.get('delay_days')}d "
        f"freed={d.get('disrupted_orders')}"
    )


async def _answer_custom_tools(session_id: str) -> None:
    """Answer this session's custom tool calls for as long as it lives.

    Registers exactly one tool. A tool name the runner does not own is left
    unanswered, which is what lets the cloud sandbox keep serving bash and the
    file tools while we serve the data pull over the same session.

    Runs as a background task owned by the session, not by the browser: the
    session idles on ``requires_action`` while a custom call is pending and never
    times out, so an unanswered call is a hang rather than an error.
    """
    runner = client.beta.sessions.events.tool_runner(
        session_id, tools=[alloc_tools.pull_allocation_snapshot]
    )
    try:
        async for call in runner:
            # Log the arguments and a digest of the answer, not just the name.
            # This is the one tool we own, and it runs out of sight of both the
            # sandbox and the transcript — the name alone tells you nothing about
            # which seed the agent chose or what it got back.
            log.info(
                "%s(%s) -> %s",
                call.event.name,
                json.dumps(getattr(call.event, "input", {}), sort_keys=True),
                _digest(call),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        # If this dies the session wedges on the next pull with no visible error,
        # so it is worth a loud log rather than a silent task exception.
        log.exception("tool runner for %s stopped", session_id)


async def _interrupt(session_id: str) -> None:
    await client.beta.sessions.events.send(
        session_id=session_id, events=[{"type": "user.interrupt"}]
    )


async def _detach(session_id: str) -> None:
    """Pause a session without ending it: interrupt its current run and drop our
    tool-answering task, but do NOT archive it — so it stays continuable and the
    planner can switch back to it from the sidebar. Only the active session's
    pulls are answered; that is the one-at-a-time design, unchanged.
    """
    global _answering
    try:
        await _interrupt(session_id)
    except APIError as e:  # already terminated, or idle with nothing to interrupt
        log.info("interrupt on %s: %s", session_id, e)
    if _answering:
        _answering.cancel()
        await asyncio.gather(_answering, return_exceptions=True)
        _answering = None


async def _stop(session_id: str) -> None:
    """Interrupt the agent, end (archive) the session, drop its tool-answering task.

    Both API calls are allowed to fail: a session that already terminated on its
    own rejects them, and that is the ordinary case rather than an error.
    """
    await _detach(session_id)
    try:
        await client.beta.sessions.archive(session_id)
    except APIError as e:
        log.info("archive on %s: %s", session_id, e)


def _render(event) -> dict | None:
    """One session event -> what the browser shows, or None to drop it.

    Builtin tool results are dropped — they are sandbox chatter, and the agent's
    own reply already says what came of them. The *custom* tool's result is
    kept: it is answered by this process, so the transcript is the only place a
    planner can see what the pull actually returned.
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
        return {"type": "tool", "name": event.name, "detail": json.dumps(event.input)}
    if kind == "user.custom_tool_result":
        body = "".join(b.text for b in event.content if getattr(b, "type", None) == "text")
        if getattr(event, "is_error", False):
            return {"type": "error", "text": f"pull failed: {body[:400]}"}
        return {"type": "tool_result", "text": body}
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

    global _answering
    async with _lock:
        previous = None
        if _active:
            # Detach, don't archive — the previous session stays in the sidebar
            # and the planner can switch back to it.
            previous, _active = _active, None
            await _detach(previous)

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
        # Start answering before the planner can send anything: a pull that
        # arrives with no runner attached parks the session indefinitely.
        _answering = asyncio.create_task(_answer_custom_tools(session.id))

    log.info("session %s started (%s)", session.id, MODELS[body.model]["id"])
    return {"id": session.id, "model": body.model, "stopped": previous}


@app.post("/session/{session_id}/activate")
async def activate(session_id: str) -> dict:
    """Switch the active session — the one whose pulls we answer and messages reach.

    Detaches the current session's tool runner (without archiving it) and attaches
    one to the selected session, so the planner can pick an earlier conversation
    back up from the sidebar. Idempotent when it is already active.
    """
    _require_config()
    global _active, _answering
    async with _lock:
        if _active == session_id:
            return {"active": _active}
        if _active:
            await _detach(_active)
        _active = session_id
        _answering = asyncio.create_task(_answer_custom_tools(session_id))
    log.info("activated session %s", session_id)
    return {"active": _active}


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
        session_id, _active = _active, None
        await _stop(session_id)
    return {"stopped": session_id}


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


MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"


@app.get("/session/{session_id}/files")
async def files(session_id: str) -> dict:
    """What the agent wrote — the ledger, the plan, the change list.

    With an Anthropic-hosted sandbox these live in the session's file store
    rather than on our disk, so this replaces the archive directory the
    self-hosted build could just list.
    """
    _require_config()
    listing = await client.beta.files.list(scope_id=session_id, betas=[MANAGED_AGENTS_BETA])
    return {
        "files": [
            {"id": f.id, "filename": f.filename, "size_bytes": f.size_bytes} for f in listing.data
        ]
    }


@app.post("/session/{session_id}/files/download")
async def download_files(session_id: str) -> dict:
    """Pull the session's outputs onto this host, under ALLOC_DOWNLOAD_DIR."""
    _require_config()
    listing = await client.beta.files.list(scope_id=session_id, betas=[MANAGED_AGENTS_BETA])
    destination = DOWNLOAD_DIR / session_id
    destination.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in listing.data:
        content = await client.beta.files.download(f.id)
        # basename: the filename comes from the sandbox, so treat it as untrusted
        # input rather than a path we are willing to follow.
        target = destination / os.path.basename(f.filename or f.id)
        await content.write_to_file(target)
        saved.append(str(target))
    log.info("downloaded %d file(s) for %s", len(saved), session_id)
    return {"saved": saved, "directory": str(destination)}
