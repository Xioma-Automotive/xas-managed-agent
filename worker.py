#!/usr/bin/env python3
"""The self-hosted sandbox for the XAS Allocation Agent.

ONE process serves every session. ``EnvironmentWorker.run()`` claims a work item,
serves that session's tool calls to completion, force-stops the item, and loops —
sessions are handled **sequentially**, which is the same one-active-session rule
``web.py`` presents in the UI. The two agree by construction rather than by
coordination between them.

  uv run python worker.py

Requires ``.env.worker`` (ALLOC_ENV_ID, ANTHROPIC_ENVIRONMENT_KEY). That file is
separate from ``.env`` on purpose: ``.env`` holds the organization API key that
``web.py`` and ``setup_allocation_agent.py`` need, and this process — the one that
actually executes agent tool calls — must never hold it. The preflight refuses to
start if it finds one.

Two things this worker deliberately does NOT have:

- **No broker.** The data tool is registered in this process alongside the
  builtin tools, which is only safe because the data is a seeded generator with
  nothing to protect. The ``bash`` tool is a subprocess under the same uid, so
  anything in this process's environment is readable from a shell the agent
  controls. When a credentialed XAS API arrives (DECIDE-7), that credential
  belongs in a separate host process answering the tool call over the session —
  not here.
- **No container.** Tools run as this process, in ``ALLOC_SANDBOX_ROOT``. The
  file tools confine themselves to the workdir, but ``bash`` has this host's
  network and whatever filesystem access this user has. See README.
"""

import asyncio
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.environments import EnvironmentWorker
from anthropic.lib.tools.agent_toolset import AgentToolContext, beta_agent_toolset_20260401
from dotenv import load_dotenv

import alloc_tools

REPO_ROOT = Path(__file__).resolve().parent
WORKER_ENV_FILE = REPO_ROOT / ".env.worker"
load_dotenv(WORKER_ENV_FILE)

log = logging.getLogger("worker")

ENV_ID = os.environ.get("ALLOC_ENV_ID")
ENVIRONMENT_KEY = os.environ.get("ANTHROPIC_ENVIRONMENT_KEY")

# Outside the repo, deliberately. The file tools confine themselves to the
# workdir, but ``bash`` does not — and a sandbox sited inside the repo puts .env,
# which holds the organization API key, one `cd ..` away from a shell the agent
# controls.
SANDBOX_ROOT = Path(
    os.environ.get("ALLOC_SANDBOX_ROOT") or Path.home() / "xas-alloc-sandbox"
).expanduser()

# Copied into the workdir at the start of every session. The system prompt tells
# the agent the solver is importable at the root of its working directory; this
# is what makes that true. Without it the agent goes hunting — and `find /` takes
# longer than the bash tool's 120s timeout, which kills its shell.
PROVISIONED = ("xas_allocation", "tests/test_invariant.py")


def provision(workdir: Path) -> None:
    """Put the reference solver where the prompt says it is.

    Copied, not symlinked: the file tools resolve symlinks and reject any that
    land outside the workdir, so a link to the repo would read as an escape.
    Re-copied per session because web.py archives the whole sandbox directory
    away when a session ends.
    """
    for rel in PROVISIONED:
        source = REPO_ROOT / rel
        destination = workdir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        else:
            shutil.copy2(source, destination)
    log.info("provisioned %s into %s", ", ".join(PROVISIONED), workdir)


def tools_for(env: AgentToolContext):
    """The builtin toolset plus the one data tool.

    Called once per claimed session, which is also the hook for provisioning the
    workdir — the agent's first tool call must find the solver already there.

    ``beta_agent_toolset_20260401`` is bash / read / write / edit / glob / grep —
    there is no fetch or search tool in it, so no tool in this list offers
    network egress. ``bash`` still can; that is the gap named in the README.
    """
    workdir = Path(env.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    provision(workdir)
    return [*beta_agent_toolset_20260401(env), alloc_tools.make_pull_tool(env.workdir)]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    assert ENV_ID and ENVIRONMENT_KEY

    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)

    async with AsyncAnthropic(auth_token=ENVIRONMENT_KEY) as client:
        worker = EnvironmentWorker(
            client,
            environment_id=ENV_ID,
            environment_key=ENVIRONMENT_KEY,
            workdir=SANDBOX_ROOT,
            tools=tools_for,
        )
        task = asyncio.create_task(worker.run())

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Cancel rather than exit: the worker's cleanup — cancelling the
            # in-flight tool call, tearing down bash, force-stopping the work
            # item so its lease is not left to expire on TTL — only runs if the
            # coroutine is allowed to unwind.
            loop.add_signal_handler(sig, task.cancel)

        log.info("polling environment %s", ENV_ID)
        log.info("sandbox workdir: %s", SANDBOX_ROOT)
        log.info("serving sessions one at a time")

        try:
            await task
        except asyncio.CancelledError:
            log.warning("interrupted: released the in-flight work item")

    log.info("worker stopped")


def check_config() -> None:
    missing = [
        name
        for name, value in (
            ("ALLOC_ENV_ID", ENV_ID),
            ("ANTHROPIC_ENVIRONMENT_KEY", ENVIRONMENT_KEY),
        )
        if not value
    ]
    if missing:
        sys.exit(
            f"Missing required values in {WORKER_ENV_FILE}: "
            + ", ".join(missing)
            + "\nCopy the .env.worker block out of .env.example and fill it in. Generate the"
            "\nenvironment key in the Console (Workspace > Environments > your env > Generate key),"
            "\nand run setup_allocation_agent.py first if you have no ALLOC_ENV_ID yet."
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is set in this process's environment. It is an\n"
            "organization-scoped credential and must not reach the process that runs\n"
            "agent tool calls — unset it before starting the worker. (Its home is .env,\n"
            "read by web.py and setup_allocation_agent.py; this process reads .env.worker.)"
        )


if __name__ == "__main__":
    check_config()
    asyncio.run(main())
