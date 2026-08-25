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
rather than a constraint (nothing queues), kept because the steering override
and the planner's attention are both singular.
"""

import asyncio
import json
import logging
import mimetypes
import os
from pathlib import Path

from anthropic import APIError, AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import alloc_tools
import appmcp_auth
import datasource

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
# setup_agent.py gave it; picking a model here never mutates it.
MODELS = {
    "opus48": {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    "opus5": {"id": "claude-opus-5", "label": "Opus 5"},
    "sonnet": {"id": "claude-sonnet-5", "label": "Sonnet 5"},
    "haiku": {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
}
DEFAULT_MODEL = "opus48"

# Hard ceiling on one session's spend, priced at Anthropic's LIST rates (model
# tokens + web search + $0.08/hour of session runtime), in cents as an integer
# string. $4 sits well above a full repair cycle — the heaviest single turn
# observed was 87c — so it is a runaway backstop, not a working limit.
#
# CREATE-ONLY, and that is the whole reason it is here: a session started
# without a budget can never be given one. At the cap the session goes idle with
# `stop_reason: budget_reached` (the SSE relay already forwards that) and keeps
# its container and history; only raising or removing the budget resumes it, and
# removal is one-way.
SESSION_BUDGET = {"type": "limit", "max_list_cost": {"amount": "400", "currency": "USD"}}

app = FastAPI(title="XAS Allocation Agent")
client = AsyncAnthropic()

# The one active session, and the task answering its custom tool calls.
_active: str | None = None
_answering: asyncio.Task | None = None
# The task keeping the app-MCP bearer fresh in its vault. Session-owned like the
# tool answerer, and for the same reason: the inner user token expires in 30
# minutes, so a session outlives its own credential unless something re-mints.
_rotating: asyncio.Task | None = None
_lock = asyncio.Lock()

# The rich pull we fetched and mounted for each session, so the tool answerer can
# summarize it without re-reading. A convenience cache: the mounted file is the
# durable copy, rebuilt from it after a restart (see _pull_for).
_pull_by_session: dict[str, dict] = {}
MOUNTED_PULL_FILENAME = "pull.json"

# The pull is the ONLY mount. The reporting lane used to get a second one — a
# fabricated jobcards.json under /workspace/reports/ — and the prompt's hard rule
# forbade that path. Reporting now reads the live system through `xas-app-mcp`
# instead, so the fence is toolset-shaped and there is no records file to mount.
#
# Mounted inputs come back from files.list(scope_id=...) alongside whatever the
# agent wrote, so both the listing and the download filter them out — otherwise a
# planner asking for "the outputs" gets their own inputs handed back.
MOUNTED_INPUT_FILENAMES = frozenset({MOUNTED_PULL_FILENAME})


class NewSession(BaseModel):
    model: str = DEFAULT_MODEL
    title: str | None = None


class Message(BaseModel):
    text: str


def _require_config() -> None:
    if not (ALLOC_AGENT_ID and ALLOC_ENV_ID):
        raise HTTPException(
            500,
            "ALLOC_AGENT_ID / ALLOC_ENV_ID are not set. Run setup_agent.py "
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
        f"now={d.get('now')} rows={d.get('orders')} supply={d.get('supply')} "
        f"disruption={disruption.get('delayed_model')}+{disruption.get('delay_days')}d "
        f"on {disruption.get('delayed_vehicles')} vehicles freed={d.get('disrupted_orders')}"
    )


async def _upload(filename: str, blob: bytes, media_type: str):
    """Upload the pull for mounting into a session's sandbox."""
    return await client.beta.files.upload(
        file=(filename, blob, media_type), betas=[MANAGED_AGENTS_BETA]
    )


async def _download_pull(session_id: str) -> dict:
    """Rebuild a session's rich pull from the file we mounted into its sandbox.

    The mounted ``pull.json`` is the durable copy; used when ``_pull_by_session``
    is cold (this process restarted while the session lived on)."""
    listing = await client.beta.files.list(scope_id=session_id, betas=[MANAGED_AGENTS_BETA])
    for f in listing.data:
        if os.path.basename(f.filename or "") == MOUNTED_PULL_FILENAME:
            content = await client.beta.files.download(f.id)
            return json.loads(await content.read())
    raise RuntimeError(f"no mounted {MOUNTED_PULL_FILENAME} for session {session_id}")


async def _pull_for(session_id: str) -> dict:
    """The rich pull for this session: the in-process cache, or the mounted file
    after a restart. This is what the tool answerer summarizes."""
    cached = _pull_by_session.get(session_id)
    if cached is not None:
        return cached
    rich = await _download_pull(session_id)
    _pull_by_session[session_id] = rich
    return rich


async def _answer_custom_tools(session_id: str) -> None:
    """Answer this session's custom tool calls for as long as it lives.

    Registers exactly one tool, built over this session's fetched-and-mounted
    pull. A tool name the runner does not own is left unanswered, which is what
    lets the cloud sandbox keep serving bash and the file tools while we serve the
    data pull over the same session.

    Runs as a background task owned by the session, not by the browser: the
    session idles on ``requires_action`` while a custom call is pending and never
    times out, so an unanswered call is a hang rather than an error.
    """
    tool = alloc_tools.make_pull_tool(lambda: _pull_for(session_id))
    runner = client.beta.sessions.events.tool_runner(session_id, tools=[tool])
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


async def _refresh_appmcp_credential() -> None:
    """Re-mint the app-MCP bearer into its vault, tolerating failure.

    The reporting lane loses its live tools if this fails; the allocation lane
    does not care, because the pull is mounted and needs no credential. So this
    never blocks a session from starting — but it logs loudly, because the
    symptom on the agent's side is a 401 or "chat session has expired" from a
    tool call, which reads like an MCP outage rather than a stale credential.
    """
    if not appmcp_auth.configured():
        # Loudly, because the symptom is otherwise a confusing MCP-side error:
        # no vault attached means the tool call goes out with no credential.
        log.warning(
            "app-MCP not configured (%s) — its tools will fail this session",
            ", ".join(n for n in appmcp_auth.REQUIRED_ENV if not os.environ.get(n)),
        )
        return
    try:
        await appmcp_auth.rotate_once(client)
    except Exception:
        log.exception("app-MCP credential not refreshed — its tools will fail this session")


def _start_rotating() -> asyncio.Task | None:
    return (
        asyncio.create_task(appmcp_auth.rotate_forever(client))
        if appmcp_auth.configured()
        else None
    )


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
    global _answering, _rotating
    try:
        await _interrupt(session_id)
    except APIError as e:  # already terminated, or idle with nothing to interrupt
        log.info("interrupt on %s: %s", session_id, e)
    for task in (_answering, _rotating):
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    _answering = _rotating = None


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
        # check_files: the agent may have written a chart. Outputs are indexed
        # ~1-3s after idle, so the browser polls rather than asking once.
        return {
            "type": "status",
            "status": "idle",
            "stop_reason": stop_reason,
            "check_files": stop_reason != "requires_action",
        }
    if kind == "session.status_terminated":
        return {"type": "status", "status": "terminated"}
    if kind == "session.error":
        message = getattr(getattr(event, "error", None), "message", "unknown error")
        return {"type": "error", "text": message}
    return None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(REPO_ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


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

    global _answering, _rotating
    async with _lock:
        previous = None
        if _active:
            # Detach, don't archive — the previous session stays in the sidebar
            # and the planner can switch back to it.
            previous, _active = _active, None
            await _detach(previous)

        # Fetch the pull HERE, on the host, from the configured data source (the
        # scenario fake or real XAS), then mount it into the sandbox as a file.
        # The sandbox never calls the source and never sees a credential; it only
        # finds the rows waiting as a file the flatten command reads. One pull
        # backs the whole repair cycle — the invariant "same snapshot every turn".
        # `pull()` is sync and the real source makes two blocking HTTP calls, so
        # it goes to a thread: on the event loop it would stall every other
        # session's tool answers for the duration of the fetch.
        rich = await asyncio.to_thread(datasource.get_source().pull)
        pull_meta = await _upload(
            MOUNTED_PULL_FILENAME, json.dumps(rich).encode(), "application/json"
        )
        # Mint the app-MCP bearer into its vault before the session exists, so
        # the agent's first reporting call cannot land on a stale one. Failing
        # here costs the MCP, not the session: allocation needs no credential,
        # so log it and carry on rather than refusing to start.
        await _refresh_appmcp_credential()
        session = await client.beta.sessions.create(
            agent={
                "type": "agent_with_overrides",
                "id": ALLOC_AGENT_ID,
                "model": MODELS[body.model]["id"],
            },
            environment_id=ALLOC_ENV_ID,
            title=body.title or "XAS session",
            # `budget` via extra_body: the API takes it, anthropic 0.120.2 does
            # not model it yet. Drop the wrapper once the SDK grows the field.
            extra_body={"budget": SESSION_BUDGET},
            resources=[
                {"type": "file", "file_id": pull_meta.id, "mount_path": alloc_tools.MOUNT_PATH},
            ],
            # Create-only: `vault_ids` is rejected on session update, so a vault
            # not attached here can never be attached to this session.
            **({"vault_ids": [appmcp_auth.vault_id()]} if appmcp_auth.configured() else {}),
        )
        _active = session.id
        _pull_by_session[session.id] = rich
        # Start answering before the planner can send anything: a pull that
        # arrives with no runner attached parks the session indefinitely.
        _answering = asyncio.create_task(_answer_custom_tools(session.id))
        _rotating = _start_rotating()

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
    global _active, _answering, _rotating
    async with _lock:
        if _active == session_id:
            return {"active": _active}
        if _active:
            await _detach(_active)
        _active = session_id
        # Same as a fresh session: the credential is 30 minutes old at most, and
        # a session resumed from the sidebar was very likely idle for longer.
        await _refresh_appmcp_credential()
        _answering = asyncio.create_task(_answer_custom_tools(session_id))
        _rotating = _start_rotating()
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
    try:
        await client.beta.sessions.events.send(
            session_id=_active,
            events=[{"type": "user.message", "content": [{"type": "text", "text": body.text}]}],
        )
    except APIError as e:
        # A session paused at its budget takes only events that settle work
        # already in progress, so a new message is a 400 that would otherwise
        # reach the browser as an opaque 500.
        log.warning("message rejected for %s: %s", _active, e)
        raise HTTPException(409, f"session will not accept a new message: {e}") from e
    return {"sent": _active}


MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"


def _render_mode(filename: str | None) -> str:
    """How the browser should present this output.

    ``frame`` — a self-contained HTML chart (inline SVG). This is the format the
    agent is told to produce: it scales, its labels are selectable text, it opens
    in a new tab natively, and for the charts measured here it is SMALLER than
    the equivalent PNG (47KB vs 55KB).
    ``image`` — a raster/vector image the agent produced anyway.
    ``link``  — anything else; offer it, don't try to draw it.
    """
    media_type = _media_type(filename)
    if media_type == "text/html":
        return "frame"
    if media_type.startswith("image/"):
        return "image"
    return "link"


def _media_type(filename: str | None) -> str:
    """What the browser should treat this file as.

    Charts arrive as .png/.svg and render inline; .html renders in a frame. The
    agent picks the renderer (the skill does not prescribe one), so infer from
    the name rather than assuming matplotlib.
    """
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


@app.get("/session/{session_id}/files")
async def files(session_id: str) -> dict:
    """What the agent WROTE — the steering override, the plan, a chart.

    Mounted inputs are excluded: they come back from the same listing, and
    handing a planner their own pull back as an "output" is noise.

    With an Anthropic-hosted sandbox these live in the session's file store
    rather than on our disk, so this replaces the archive directory the
    self-hosted build could just list.
    """
    _require_config()
    listing = await client.beta.files.list(scope_id=session_id, betas=[MANAGED_AGENTS_BETA])
    return {
        "files": [
            {
                "id": f.id,
                "filename": f.filename,
                "size_bytes": f.size_bytes,
                "media_type": _media_type(f.filename),
                "render": _render_mode(f.filename),
            }
            for f in listing.data
            if os.path.basename(f.filename or "") not in MOUNTED_INPUT_FILENAMES
        ]
    }


@app.get("/session/{session_id}/files/{file_id}/content")
async def file_content(session_id: str, file_id: str) -> Response:
    """Serve one output file to the browser so a chart can be shown, not just saved.

    The file_id is checked against THIS session's listing before anything is
    fetched: the id comes from the URL, and without the check one session could
    be used to read another's outputs.
    """
    _require_config()
    listing = await client.beta.files.list(scope_id=session_id, betas=[MANAGED_AGENTS_BETA])
    match = next((f for f in listing.data if f.id == file_id), None)
    if match is None:
        raise HTTPException(404, f"no file {file_id} in session {session_id}")
    name = os.path.basename(match.filename or file_id)
    if name in MOUNTED_INPUT_FILENAMES:
        raise HTTPException(404, "that file is a mounted input, not an output")

    downloaded = await client.beta.files.download(file_id)
    blob = await downloaded.read()
    return Response(
        content=blob,
        media_type=_media_type(name),
        # inline: the point is to SHOW it. Browsers download octet-stream anyway.
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@app.post("/session/{session_id}/files/download")
async def download_files(session_id: str) -> dict:
    """Pull the session's outputs onto this host, under ALLOC_DOWNLOAD_DIR."""
    _require_config()
    listing = await client.beta.files.list(scope_id=session_id, betas=[MANAGED_AGENTS_BETA])
    destination = DOWNLOAD_DIR / session_id
    destination.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in listing.data:
        # basename first: the filter must compare the same untrusted string the
        # write below uses, or a crafted "outputs/pull.json" would slip past it.
        name = os.path.basename(f.filename or "")
        if name in MOUNTED_INPUT_FILENAMES:
            continue
        content = await client.beta.files.download(f.id)
        # basename: the filename comes from the sandbox, so treat it as untrusted
        # input rather than a path we are willing to follow.
        target = destination / os.path.basename(f.filename or f.id)
        await content.write_to_file(target)
        saved.append(str(target))
    log.info("downloaded %d file(s) for %s", len(saved), session_id)
    return {"saved": saved, "directory": str(destination)}
