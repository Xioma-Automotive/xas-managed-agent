# Managed Agents: what's left on the table

Capability audit for this repo's Managed Agent. Concepts the platform offers that we aren't
using, triaged by whether they work at all on a **self-hosted** environment. That filter decides
more than anything else here.

| | |
| --- | --- |
| Environment | `self_hosted` |
| Agent | `claude-opus-4-8`, effort medium |
| Beta | `managed-agents-2026-04-01` |

---

## The credential mechanism we were planning is unavailable

The plan was to give the agent data access "by proper authorization." The platform's first-class
answer is a vault `environment_variable` credential: the sandbox sees an opaque placeholder, and
Anthropic substitutes the real secret at egress, so agent code can never read it. Exactly the
right primitive — but:

> Environment variable credentials (`environment_variable`) are not yet supported with
> self-hosted sandboxes.

Because egress is ours, there is nowhere for Anthropic to substitute the value.

**Consequence:** on self-hosted, the credentialed custom tool isn't the fallback — it's the only
path. Vault *MCP* credentials (`mcp_oauth`, `static_bearer`) do still work, because those inject
on Anthropic's side.

---

## Adopt — worth taking now

All verified available on self-hosted.

### Outcomes (`user.define_outcome`)

A task plus a gradeable rubric. A separate grader scores each iteration and feeds gaps back; the
agent revises until it passes, exhausts `max_iterations`, or is interrupted.

Our system prompt already encodes a rubric in prose — verify every number, single standalone
HTML, label axes. This makes it **enforced rather than hoped for**.

### Permission policies (`always_ask`)

Per-tool approval gate. The session goes idle on the call and waits for allow or deny, with a
deny message the model reads.

The direct answer to raw `bash`. Gate bash, leave read/grep/write automatic. **Does not cover
custom tools** — approval there lives in our own code.

### Task budgets (beta, min 20k tokens)

A token ceiling the agent can see and pace against, distinct from `max_tokens`, which it cannot.

Principled replacement for `MAX_TOOL_CALLS`, which is client-side and stops applying the moment
the browser disconnects. The agent **wraps up instead of being cut off**.

### Scheduled deployments

Cron schedule plus initial events; each firing creates a session on its own. Per-firing run
records with typed errors, and pause / unpause / archive.

A nightly dashboard with **no scheduler of our own**. A manual run endpoint lets us test the
schedule immediately rather than waiting for it.

### Live previews (`event_deltas[]`)

Opts a stream into incremental text events so assistant output renders as it generates.

Our UI waits for the whole buffered message today. Note the delta type is `content_delta`, **not**
the Messages-API shape — accumulator code doesn't port over.

### Session metadata

Arbitrary key-value pairs carried on the session and readable by our worker.

The documented self-hosted staging path: put an S3 path or commit SHA in metadata, fetch the
session in the poller, stage files before the container starts. **The only way to pass
per-session inputs** — session `resources` are rejected outright (see Blocked).

### Webhooks

Signed POSTs on session, agent, vault, and deployment state changes. Thin payload — fetch the
resource on receipt.

Notify on completion without holding a stream open, and **wake the worker on demand** instead of
polling continuously. Not durable: three attempts, then dropped, and no ordering guarantee.

### Stream reconnect

On every reconnect, fetch event history and dedupe by ID before tailing the live stream.

The stream has no replay. Drop it while a tool call is pending and **the session deadlocks** —
client gone, session idle, nothing to resolve it. We have no reconnect at all.

### Initial events

Up to 50 events at session create, starting the loop in the same call.

Collapses our create-then-send into one round trip. The session is created directly in `running`
and **never passes through idle** — worth knowing before gating on that transition.

### Agent version pinning

Every update mints an immutable version; a session can pin to one instead of taking latest.

Setup updates in place and the web client always takes latest, so a prompt edit **silently
changes behavior for in-flight work**. Pin for reproducibility, roll back on regression.

### Mid-session system messages

Appends system-level context between turns without touching the agent's stored prompt.

Inject tenant, date range, or client context per turn — **no new agent version, no cache
invalidation**. Gated to Opus 5 / 4.8 / Sonnet 5 / Fable 5.

---

## Smaller gaps in what we run

- **Console trace URL** — the per-session live trace view. The CLI client printed it; the web
  client never did, and we deleted the CLI. Cheapest debugging win available.
- **Session lifecycle** — we create sessions and never archive or delete them. There's also a
  documented race: the stream reports idle slightly before the status is queryable, so an
  immediate delete intermittently fails.
- **Terminal stop reasons** — we treat everything except `requires_action` as done, so
  `retries_exhausted` (a terminal failure) currently renders as a normal finish.
- **Compaction event** — fires when session history is summarized. Long dashboard sessions will
  hit it and we'd have no idea.
- **Large tool outputs** — anything past roughly 100k characters offloads to a file with a
  preview. Relevant the first time the agent dumps a big query result.

---

## Later — real, but not yet

**Multiagent coordinator with per-subagent threads.** One agent per data source, fanning into a
synthesizer. A big jump from where we are: one level of delegation only, capped at 25 concurrent
threads.

**Reaching our internal MCP servers.** Two routes for `xas-mcp`, `xas-logs`, `xas-code`: a tunnel
inward, or — the better fit for self-hosted — wrapping each server as custom tools so our worker
is the MCP client and the server needs no inbound connectivity. Tools are declared, not
discovered: the worker lists them once at startup and can't add any to a running session.

**Fast mode.** Roughly 2.5× output speed on Opus 5 and 4.8 at premium pricing. Works on Managed
Agents; not on Bedrock, Google Cloud, or Foundry.

---

## Blocked — don't plan around these

Unavailable on self-hosted environments.

| Concept | Status |
| --- | --- |
| Vault environment-variable credentials | Not supported. The finding above — and the reason the custom tool is the only path to gated data access. |
| Session resources (`file`, `github_repository`) | A session carrying **any** resource on a self-hosted environment is rejected outright. Use session metadata and stage files in the poller instead. |
| Memory stores | Not supported. No cross-session persistent memory for the agent. |
| Files API session outputs | Doesn't apply — self-hosted omits the outputs-directory instruction from the system prompt entirely. Our per-session bind mount is the substitute, which is why it was built that way. |

---

## If we pick three

1. **Permission policy on bash.** Smallest change; addresses the exposure still open after the
   sandbox split. Roughly a one-line agent config change plus a confirm handler in the web client.
2. **The credentialed custom tool.** Now confirmed as the only path rather than a preference.
3. **Outcomes.** Our prompt is already a rubric written as prose. This makes it checkable.

---

Sourced from the Managed Agents documentation, filtered against our self-hosted environment.
Every "blocked" row is an explicit statement in the docs, not an inference. Availability under
`self_hosted` differs enough from cloud that it's worth re-checking before committing to anything
here — several of these limits are marked "not yet."
