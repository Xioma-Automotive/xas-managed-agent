"""Call the dev app MCP by hand — `tools/list` and `tools/call`, nothing else.

    uv run python -m appmcp --list                     # every tool, params summarised
    uv run python -m appmcp --list get_job_list        # one tool, full input schema
    uv run python -m appmcp get_job_list '{"paging": {"count": 1}}'
    uv run python -m appmcp get_job_list '{...}' --raw # no trimming at all

Exists because the surface changes under us: the six tools were renamed on
2026-08-27, `fields` cannot reach inside a field, and camelCase filter keys return
0 with no error. Probe it, do not trust a doc — including `docs/appmcp-connect.md`.

Read-only. Auth is `appmcp_auth`: the login in `.env` yields the 30-minute inner
token, and the JWE is minted around it here. `.env` is loaded BEFORE the auth
module reads its config, which is the whole reason that module reads it per call.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

import appmcp_auth  # must be imported AFTER load_dotenv(); see the module docstring

# Rides on every job-card response, identical every time, and says nothing: five
# lifecycle buckets with Count: 0. Trimmed unless --raw, so it stops drowning the
# thing you are actually looking at.
NOISE = ("states",)


async def rpc(method: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=120) as http:
        jwe = appmcp_auth.mint(await appmcp_auth._fetch_user_token(http))
        r = await http.post(
            appmcp_auth.APPMCP_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {jwe}",
            },
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        )
    body = r.text
    if "data:" in body[:200]:  # streamable HTTP answers as SSE
        body = "".join(l[6:] for l in body.splitlines() if l.startswith("data: "))
    payload = json.loads(body)
    if "error" in payload:
        sys.exit(f"JSON-RPC error: {json.dumps(payload['error'])}")
    return payload.get("result", {})


def summarise(tool: dict) -> str:
    props = tool["inputSchema"].get("properties", {})
    enum = (props.get("fields") or {}).get("items", {}).get("enum") or []
    fields = f", fields[{len(enum)}]" if enum else ""
    return f"{tool['name']:22} params: {', '.join(sorted(props))}{fields}"


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    raw = "--raw" in args
    args = [a for a in args if a != "--raw"]

    if args[0] == "--list":
        tools = (await rpc("tools/list", {}))["tools"]
        if len(args) > 1:
            for t in tools:
                if t["name"] == args[1]:
                    print(t["description"])
                    print("\n--- inputSchema:")
                    print(json.dumps(t["inputSchema"], indent=2))
                    return
            sys.exit(f"no such tool: {args[1]}  (have: {', '.join(t['name'] for t in tools)})")
        for t in tools:
            print(summarise(t))
        return

    name = args[0]
    arguments = json.loads(args[1]) if len(args) > 1 else {}
    result = await rpc("tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        print(json.dumps(result.get("content"), indent=2, ensure_ascii=False))
        sys.exit(1)
    text = result.get("content", [{}])[0].get("text", "{}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(text)
        return
    if not raw and isinstance(data, dict):
        for key in NOISE:
            data.pop(key, None)
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
