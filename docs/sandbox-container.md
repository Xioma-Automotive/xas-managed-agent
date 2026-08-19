# Containerising the self-hosted sandbox

`CLAUDE.md` names this as the open gap and the intended fix:

> tools run as the host process, so `bash` inherits the host network. This is a
> local prototype, not an isolation boundary. A container + proxy allow-list is
> the later adaptation.

This file explains what that adaptation is, what it has to satisfy, and where
this repo's design constrains it. It is an explanation, not a build — nothing
here is wired up.

---

## What a self-hosted sandbox actually is

Four parts, and only the third is the "sandbox" in the sense that matters:

| Part | Where it lives | Notes |
| --- | --- | --- |
| The environment record | Anthropic | `config: {"type": "self_hosted"}` — the entire config. No `networking`, no `packages`. |
| The environment key | Console → the environment → *Generate environment key* | `sk-ant-oat01-…`, scoped to one environment's work queue. This is `.env.worker`. |
| The execution container | **You** | Anthropic hardens nothing here. This is the subject of this document. |
| The worker process | **You** | `worker.py`. Long-polls the work queue, executes tool calls, force-stops the item. |

The agent loop, the model, and the orchestration stay on Anthropic's side.
Self-hosting moves **tool execution**, not inference. Connectivity is
outbound-only: the worker dials out, nothing dials in.

Today parts three and four are collapsed — `worker.py` *is* the sandbox, running
as your uid on your machine. Containerising separates them.

---

## What the platform requires of the image

These are hard constraints from the SDK, not preferences.

- **`/bin/bash` at that exact path.** The SDK's bash tool resolves it literally
  and ignores `PATH`. Debian slim has it; alpine (busybox `ash`) does not. This
  is the single most common way a first image fails.
- **A writable workdir.** Tool calls are confined to it. Passed as
  `EnvironmentWorker(workdir=…)`; here that is `ALLOC_SANDBOX_ROOT`.
- **Skills land in `{workdir}/skills/<name>/`**, downloaded per session by the
  worker. Nothing to bake in, but the workdir must be writable and sized for it.
  (This repo's skill is uploaded to the platform by
  `setup_allocation_agent.py`, so the agent receives it through the session
  rather than as a downloaded skill directory.)
- **The TypeScript worker additionally needs `unzip`, `tar`, and Node 22+.** Not
  relevant here — `worker.py` is Python — but it is the same class of literal
  path/binary dependency as `/bin/bash`.

---

## What this repo's design requires of the container

These are the ones that will bite, and they are specific to how `web.py` and
`worker.py` already agree with each other.

### The sandbox root must be a bind mount, not tmpfs

`web.py` ends a session by **moving** `~/xas-alloc-sandbox` into
`~/xas-alloc-sessions/<session_id>/`. That archive is what the session list
points at. If the container writes to an internal tmpfs, the files die with the
container and `web.py` archives an empty directory.

So the host directory has to be mounted in, and both processes have to be
pointed at the same place under their own names:

| Process | `ALLOC_SANDBOX_ROOT` | Same directory? |
| --- | --- | --- |
| `web.py` (host) | `~/xas-alloc-sandbox` | yes |
| `worker.py` (container) | `/sandbox` | yes — bind-mounted |

Each reads its own environment, so the paths differing is fine. Them pointing at
different directories is not.

### The container must write as your uid

Follows directly from the above: `web.py` runs as you and has to be able to move
those files. A container user of `uid 10001` leaves them owned by someone else
and the archive fails on permissions. Run with `--user "$(id -u):$(id -g)"` and
set `HOME` explicitly, since that uid has no `passwd` entry inside the image.

This does not weaken the boundary. The isolation being bought is *filesystem and
network namespace* — the agent cannot see `.env`, cannot reach the host
filesystem, cannot reach the host network — not uid separation. Same uid inside
a namespace still sees only what is mounted.

### The image must carry the solver

`worker.py`'s `provision()` copies `xas_allocation/` and
`tests/test_invariant.py` from `REPO_ROOT` into the workdir at the start of
every session, because the prompt promises the solver is importable there. In a
container `REPO_ROOT` is `/app`, so both have to be in the image. If they are
not, the agent goes hunting, runs `find /`, blows the bash tool's 120s timeout,
and kills its own shell — the failure `CLAUDE.md` already records.

Install from `requirements.txt`, not `pyproject.toml`: fastapi/uvicorn/
sse-starlette are `web.py`'s dependencies and have no business in the process
that executes agent tool calls.

### `.env` must never enter the build context

The build context has to be the repo root (the image needs `xas_allocation/`),
and the repo root is where `.env` lives. A `.dockerignore` excluding `.env` and
`.env.*` is load-bearing, not hygiene — an explicit `COPY` list protects you
only until someone adds `COPY . .`.

`.env.worker` should not be baked in either. Pass `ALLOC_ENV_ID` and
`ANTHROPIC_ENVIRONMENT_KEY` as run-time environment. `worker.py`'s preflight
still refuses to start if it sees an `ANTHROPIC_API_KEY`; containerising turns
that from the only defence into the second one.

### Nothing about concurrency changes

`EnvironmentWorker.run()` stays. One container, one long-lived worker, sessions
served sequentially — which is exactly the rule `web.py` presents as "new session
stops the old". The two agree by construction, and `CLAUDE.md` warns against
adding concurrency to one side only.

A per-session container (`run_one()` behind the mid-level work poller) is the
other valid shape, and is what you would want if sessions should be genuinely
disposable. It is a **different design**, not a tuning knob: it breaks the
sequential agreement with `web.py` and needs an orchestrator process to launch
containers per work item. Don't drift into it by accident.

---

## Reference shape

Illustrative, to make the constraints above concrete.

```dockerfile
FROM python:3.12-slim          # NOT alpine — needs /bin/bash

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py alloc_tools.py ./
COPY xas_allocation/ ./xas_allocation/
COPY tests/test_invariant.py ./tests/test_invariant.py

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ALLOC_SANDBOX_ROOT=/sandbox
ENTRYPOINT ["python", "/app/worker.py"]
```

Built from the repo root: `docker build -f sandbox/Dockerfile -t xas-alloc-sandbox .`

```sh
docker run --rm -it --init \
    --user "$(id -u):$(id -g)" \
    --env-file .env.worker \
    -e HOME=/tmp -e ALLOC_SANDBOX_ROOT=/sandbox \
    -v "$HOME/xas-alloc-sandbox:/sandbox" \
    --network agent-egress \
    --read-only --tmpfs /tmp:rw,nosuid,size=256m \
    --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 512 --memory 4g --cpus 2 \
    xas-alloc-sandbox
```

Why each flag is there:

| Flag | Reason |
| --- | --- |
| `--init` | The bash tool spawns subprocesses and PID 1 does not reap them. Without this, zombies accumulate over a long session. |
| `--user` | Keeps mounted files owned by you, so `web.py` can archive them. |
| `--env-file .env.worker` | The credential arrives at run time, never in a layer. |
| `--read-only` + `--tmpfs /tmp` | Rootfs immutable; only the mounted sandbox and a small tmpfs are writable. |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | Nothing in the toolset needs a capability. |
| `--pids-limit`, `--memory`, `--cpus` | A fork bomb or runaway solver run stops being a host-level event. |

`worker.py` installs SIGINT/SIGTERM handlers that cancel the in-flight tool call
and force-stop the work item rather than leaving its lease to expire on TTL.
Those survive containerisation — `docker stop` sends SIGTERM to PID 1 — so a
clean shutdown still releases the work item.

---

## The egress problem, stated honestly

`--read-only` means the agent cannot `pip install`. Everything it needs must be
in the image; `ortools` already is.

Networking is the part a container does **not** solve on its own. The worker
itself needs outbound access to `api.anthropic.com` to poll the work queue — so
`--network none` is not available, and whatever egress the worker gets, the
agent's `bash` gets too. They share a network namespace.

The mitigation is the "proxy allow-list" half of `CLAUDE.md`'s sentence: put the
container on a network whose egress is restricted to `api.anthropic.com`, via an
egress proxy or firewall rules on a dedicated Docker network. Docker's default
bridge is unrestricted outbound, so leaving `--network bridge` in place means
the container is a filesystem boundary only.

Worth keeping in proportion: `beta_agent_toolset_20260401` is
`bash, read, write, edit, glob, grep` — no fetch, no search — and nothing in the
toolset is credentialed. The exposure is `bash` specifically, and today it is
`bash` on your host network with your uid.

---

## What this closes, and what it does not

**Closes:** `bash` reading the host filesystem (`.env`, `~/.aws`, `~/.ssh`,
credential files generally) — the failure that motivated the cloud branch in the
first place. Also host-level blast radius from resource exhaustion.

**Closes only with an egress allow-list:** `bash` reaching arbitrary network
destinations. The container alone does not do this.

**Does not close:** the pull tool is still registered in the worker process
alongside the builtin tools, so anything in the worker's environment remains
readable from a shell the agent controls — now bounded to the container, but
that includes `ANTHROPIC_ENVIRONMENT_KEY`. `CLAUDE.md` is explicit that when a
credentialed XAS API arrives (DECIDE-7) the credential goes in a separate
host-side process answering the tool call over the session, **not** here.
Containerising does not change that and should not be read as permission to
relax it.

**Not available on self-hosted at all**, per `docs/managed-agents-adoption.md`:
vault `environment_variable` credentials (there is no Anthropic-side egress to
substitute at), session resources, memory stores.

---

## Stale claims if this is ever built

Two statements would become false and are the reason this is a document rather
than a directory of files:

- `README.md`: the trust-level table's third row reads *"there is no fourth: no
  broker, no vault, no container"*, and the section *"The sandbox lives outside
  the repo"* justifies that placement by `bash` not being confined. With a
  container the sandbox root is a mount and the `cd ..` argument no longer
  carries the weight.
- `CLAUDE.md`: *"A container + proxy allow-list is the later adaptation"* would
  need to describe the current state instead of the plan.

Both are design statements in the repo's own voice, not incidental prose. They
should be rewritten deliberately, not as a side effect.
