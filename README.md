# Billing Dashboard Agent

A conversational Claude **Managed Agent** that reads billing data from an
internal HTTP API, analyzes it (trends, anomalies, top movers), and produces
self-contained HTML dashboards. You chat with it to describe the report you want.

It uses the Managed Agents REST surface via the Python `anthropic` SDK, model
`claude-opus-5`. Anthropic runs the agent loop and hosts a per-session sandbox
where the agent's tools (bash, file ops, web fetch, etc.) execute; your code
just drives the session and collects the results.

## Setup-once vs. runtime-per-conversation

Managed Agents split cleanly into two planes, and so does this repo:

- **Control plane — run once.** The agent, environment, and vault are
  persistent, versioned resources. You create them a single time with
  `setup_agent.py`, then reference them by ID forever after. Re-creating them on
  every run accumulates orphaned resources and pays create latency for nothing.
- **Data plane — run every conversation.** `billing_agent.py` opens a fresh
  session against the pre-created agent, streams the conversation, and downloads
  outputs. It never calls `agents.create()` / `environments.create()` /
  `vaults.create()` — it only references the IDs from `.env`.

## Files

| File               | Plane        | What it does |
| ------------------ | ------------ | ------------ |
| `.gitignore`       | —            | Ignores `.env`, generated `*.html`, and Python cruft. |
| `.env.example`     | —            | Committed template for the values below. Copy to `.env`. |
| `requirements.txt` | —            | `anthropic` + `python-dotenv`. |
| `setup_agent.py`   | Control (once) | Creates a **cloud** environment (limited networking, scoped to the billing host), the **agent** (`claude-opus-5`, prebuilt toolset, billing-analyst system prompt), and a **vault** with an `environment_variable` credential for the billing token. Prints `AGENT_ID` / `ENV_ID` / `VAULT_ID`. |
| `billing_agent.py` | Data (per run) | Loads the three IDs, opens a session, runs a smoke-test turn then your real request, and downloads the HTML dashboards the agent produced. |
| `README.md`        | —            | This file. |

## Run order

```bash
pip install -r requirements.txt
cp .env.example .env
#   fill in ANTHROPIC_API_KEY, BILLING_API_TOKEN, BILLING_HOST
python setup_agent.py
#   paste the printed AGENT_ID / ENV_ID / VAULT_ID into .env
python billing_agent.py
```

`billing_agent.py` prints a Console trace URL for each session so you can watch
tool calls and messages stream live. Dashboards the agent writes are downloaded
into the working directory as `*.html` (gitignored).

## How auth works

The billing token is stored in an Anthropic-managed **vault** as an
`environment_variable` credential, scoped to the billing host. The sandbox only
ever sees an opaque placeholder; the real token is substituted into the outbound
`Authorization` header **at egress**, so agent code can't read or exfiltrate it.
The smoke-test turn exists because credential/network failures surface on first
use, not when the session is created — it does one authenticated GET and reports
the status before any real work.

## Things to confirm

- **`BILLING_HOST` in `.env.example` is a placeholder** (`api.xiomautomotive.com`).
  Set it to the real internal billing API host before running setup — it scopes
  both the environment's `allowed_hosts` and the credential's egress allow-list,
  so a wrong value means the agent can't reach the API.
- **The token is injected as `Authorization: Bearer <token>`** (header injection).
  If the real billing API expects a different auth scheme — a custom header like
  `X-API-Key`, a query param, or a non-Bearer scheme — the header-injected Bearer
  token won't authenticate. Adjust the credential's `injection_location` and the
  system prompt's auth guidance accordingly (note: secrets can only be injected
  into request headers or bodies, never the URL path).
