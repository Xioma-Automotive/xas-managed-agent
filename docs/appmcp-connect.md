# Connecting to xas-app-mcp (dev) — mint & call

How to authenticate to the deployed app MCP and call its tools directly (no agent).

## The easy path: `appmcp.py`

```bash
uv run python -m appmcp --list                     # every tool, params summarised
uv run python -m appmcp --list get_job_list        # one tool, full input schema
uv run python -m appmcp get_job_list '{"paging": {"count": 1}}'
uv run python -m appmcp get_job_list '{...}' --raw # keep the states block too
```

It mints through `appmcp_auth` (so `.env` is the only setup) and trims the `states`
block, which rides on every job-card response and never says anything. Use it to
CHECK this file rather than trusting it — the surface has changed under us twice.

Everything below is the manual route, for when you need to mint a bearer yourself.

## Endpoint
`POST https://dev-appmcp.app.automotivecloud.net/mcp` (JSON-RPC 2.0, streamable HTTP)

Headers on every request:
```
Content-Type: application/json
Accept: application/json, text/event-stream
Authorization: Bearer <JWE>
```
There is **one** auth header now. The old `X-Xioma-User-Token` + static shared-key
scheme is gone; the user token now rides *inside* the encrypted bearer.

## The bearer is an AES-256-GCM JWE

Crypto (mirrors `xas-app-mcp/src/auth/tokenCrypto.ts` — all params must match or you get a
generic `401 Unauthorized` with no detail):

| Param | Value |
|-------|-------|
| Key management | `dir` (direct) |
| Content encryption | `A256GCM` |
| Key | `sha256(MCP_TOKEN_ENC_KEY)` → 32 bytes |
| `iss` | `http://dev_aibot:5050` |
| `aud` | `https://dev-appmcp.app.automotivecloud.net/mcp` |
| Clock tolerance | 30s |

Required claims (server rejects if any missing/wrong):
- `typ`: `"appmcp"` ← easy to forget; missing it => 401
- `userToken`: a `__DMS_app_token` JWT (the logged-in user's Xioma app token)
- `userId`, `companyDB`, `scope` (space-separated; server intersects with its own vocab)
- `iat`, `exp` — keep short (e.g. 30m)

## Prerequisites
- `MCP_TOKEN_ENC_KEY` in this project's `.env` (do not hardcode; it's a secret).
- `jose` (npm, v5/v6) available. `node -e` with jose is simplest.
- A `__DMS_app_token` for the `userToken` claim — see caveat below.

## Mint (Node + jose) — `mint.mjs`
```js
import { EncryptJWT } from 'jose';
import { createHash } from 'node:crypto';

const KEY = createHash('sha256').update(process.env.MCP_TOKEN_ENC_KEY).digest();
const userToken = process.env.USER_APP_TOKEN;   // a __DMS_app_token

const jwe = await new EncryptJWT({
  typ: 'appmcp',
  userToken,
  userId: 'manager',
  companyDB: '6530d4f8d5c9e5001d6e319e',
  scope: 'jobcards.read',
})
  .setProtectedHeader({ alg: 'dir', enc: 'A256GCM' })
  .setIssuer('http://dev_aibot:5050')
  .setAudience('https://dev-appmcp.app.automotivecloud.net/mcp')
  .setIssuedAt()
  .setExpirationTime('30m')
  .encrypt(KEY);

console.log(jwe);
```

## Call
```bash
# load the secret from this project's .env
export $(grep MCP_TOKEN_ENC_KEY .env)
export USER_APP_TOKEN='<a __DMS_app_token>'
JWE=$(node mint.mjs)

MCP=https://dev-appmcp.app.automotivecloud.net/mcp
curl -s -X POST "$MCP" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $JWE" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'

curl -s -X POST "$MCP" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $JWE" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_job_list","arguments":{"filter":{"JobClassification":"Service"},"fields":["DMSJCEntry"],"paging":{"page":1,"count":2}}},"id":2}'
```

## Tools available
`get_job_list`, `get_job_details`, `get_vehicle_list`, `get_vehicle_details`,
`get_account_list`, `get_account_details`. **Renamed 2026-08-27** — the old
`get_job_cards` / `get_vehicles` / `get_accounts` names now return
`-32602 Tool not found`, so re-probe with `tools/list` before trusting any name
written down here.

All six take `fields` (a subset of what the tool already returns — it cannot
widen, and a name outside that set is dropped silently). The `*_details` tools
take `include` for sub-resources; `get_job_details` returns job items by default,
which is the only place the `ModelItem` car lines live.

## Verifying / debugging
- `tools/list` → **200 + tool list** means the JWE auth is correct.
- **401** (`{"code":-32001,"message":"Unauthorized"}`) = a crypto/claim mismatch. Deliberately
  opaque — no hint which. Recheck, in order: `typ:'appmcp'`, key derivation (`sha256`), `alg/enc`,
  `iss`, `aud`, `exp`. Round-trip your own token with `jwtDecrypt(jwe, KEY, {issuer, audience})`
  to confirm it's internally consistent before blaming the server.
- `tools/call` returns **200** at the transport layer but the JSON-RPC result has
  `isError:true`. `"chat session has expired"` here means the **inner `userToken` was rejected
  by the gateway** — auth passed, the app token is stale/wrong-realm. Different layer than 401.

## The `userToken` caveat (important)
The MCP forwards `userToken` to *its* gateway (`GATEWAY_BASE_URL`, internally `http://dev_proxy`),
which validates the session server-side. A token minted against a **different** gateway (e.g. a
local dev stack login) authenticates the JWE fine but fails the tool call with
`"chat session has expired"`. Mint the `userToken` from the login endpoint that fronts the SAME
gateway this MCP talks to. Self-forging a `__DMS_app_token` does not work — sessions are validated,
not just signature-checked.

## Design note
This scheme was intentionally built so the MCP is DECRYPT-ONLY (no `EncryptJWT` in the server) —
the canonical minter is `xas-ai-bot` (`src/auth/mcpCredential.ts`). Minting your own JWE works for
testing but is outside the intended flow. If any crypto param drifts, the source of truth chain is:
`xioma-mcp-server/src/oauth/tokenCrypto.ts` → `xas-app-mcp/src/auth/tokenCrypto.ts` →
`xas-ai-bot/src/auth/mcpCredential.ts` — all three must agree.
