# XAS Q&A — Managed Agent (Milestone 0)

The simplest runnable slice of porting `xas-ai-agent` onto Managed Agents:
**an agent that answers a dealership question over data and draws a chart**, with
the data handed in as **mounted files** (no MCP, no network, no credentials yet).

See `../docs/xas-ai-agent-migration-plan.md` for the full plan. This directory is
Milestone 0 of that plan's Phase 0/2.

## What runs where

| | Holds | Runs |
| --- | --- | --- |
| `run_qa.py` (your host) | org `ANTHROPIC_API_KEY` | uploads the two data files, opens one session, streams it, downloads the chart |
| Anthropic's sandbox | nothing of yours | the agent loop + bash/file tools; reads the mounted files, writes a chart file |

- `setup_xas_qa.py` — **run once.** Creates the cloud environment + agent (system
  prompt, model, built-in bash/file toolset). Re-run to update in place.
- `run_qa.py` — **per question.** Mounts `data/sample_index.json` +
  `data/sample_jobcards.json`, sends the question, prints the turn, saves any file
  the agent wrote into `../qa-outputs/`.
- `data/` — a **slim** terminology index (Service classification + statuses +
  branches + a few Hebrew aliases; the full prod index is ~2 MB, so we mount a
  trimmed one) and a faithful 24-card job-card sample (from the mock generator).

## Run it

```bash
cp qa/.env.example qa/.env          # set ANTHROPIC_API_KEY
uv run python qa/setup_xas_qa.py    # paste printed XAS_QA_ENV_ID / XAS_QA_AGENT_ID into qa/.env
uv run python qa/run_qa.py "how many service cards are in each status? draw a bar chart"
uv run python qa/run_qa.py "כמה כרטיסי שירות סגורים יש בכל סניף? צייר תרשים"   # Hebrew
```

The chart lands in `qa-outputs/`. The agent decides how to draw it (matplotlib PNG
or self-contained HTML/SVG) — Milestone 0 doesn't prescribe the renderer.

## How "return an object" works

The agent doesn't emit a binary in the message stream — it **writes a file** in its
sandbox (`chart.png` etc.), and `run_qa.py` pulls the bytes with the Files API
(`files.list(scope_id=session_id)` + `files.download`). Any object works this way:
PNG, SVG, HTML, PDF, CSV.

## Next: Milestone 1 (Design A — live MCP)

Swap the mounted `jobcards.json` for the live `xas-app-mcp` reached **directly by
the sandbox** via a per-user vault credential (`mcp_oauth`/`static_bearer`), i.e.
`sessions.create(..., vault_ids=[user_vault])` + an `mcp_toolset` tool. Only
`run_qa.py` (mounting → MCP) and the agent's `tools`/`mcp_servers` change; the
system prompt, chart handling, and the index mount stay. Prerequisites: expose
`xas-app-mcp` to Anthropic's network + a test-user token in a vault. See the plan's
"MCP auth: today → managed agents" and D-1/D-1a.
