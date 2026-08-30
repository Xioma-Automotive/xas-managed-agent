#!/usr/bin/env python3
"""Host-side credential minting for the XAS app MCP (dev).

The sandbox never holds this credential. Anthropic does: the bearer lives in a
**vault**, and MCP tool calls are routed through an Anthropic-side proxy that
fetches it and adds it to the outbound request. So the agent can call the app
MCP without anything secret ever entering its context or its filesystem — the
same boundary the pull tool already keeps for the data snapshot.

Why a rotator instead of `mcp_oauth` auto-refresh
-------------------------------------------------
The bearer is TWO nested credentials:

  outer   an AES-256-GCM JWE we encrypt with MCP_TOKEN_ENC_KEY   (7 days)
  inner   a `__DMS_app_token` the gateway issues at login        (30 minutes)

Anthropic can auto-refresh an `mcp_oauth` credential, but only by POSTing a
standard OAuth `refresh_token` grant to a `token_endpoint` — and our outer token
is not obtainable that way; it has to be *encrypted here*, with a key that never
leaves this host. So the credential is a `static_bearer` and we re-mint it
ourselves, well inside the inner token's 30 minutes.

The two expiries fail differently, which is the thing worth remembering: a stale
outer token is a flat `401` from the MCP, while a stale INNER token still passes
auth and comes back `200` with `isError: true` and "chat session has expired".
`tools/list` never reaches the gateway, so it keeps working either way and proves
nothing about the inner token.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("web.appmcp")

# The MCP whose credential this mints. Vault matching normalizes scheme, host
# case, default ports and a trailing slash — but NOT the path, so this must stay
# byte-identical to the URL declared in setup_agent.MCP_SERVERS. A URL that does
# not match means the connection is attempted UNAUTHENTICATED, which surfaces as
# a 401 from the MCP rather than as a configuration error.
APPMCP_URL = "https://dev-appmcp.app.automotivecloud.net/mcp"

# The login endpoint fronting the SAME gateway the MCP talks to (internally
# `dev_proxy`). A token minted against a different stack authenticates the JWE
# fine and still fails every tool call with "chat session has expired".
GATEWAY_LOGIN = "https://dev.proxy.automotivecloud.net/api/account/login"
USER_TOKEN_COOKIE = "__DMS_app_token"

# Claims the MCP requires. `typ` is the one that is easy to forget; without it
# the server answers a bare 401 with no indication which param was wrong.
ISS = "http://dev_aibot:5050"
CLAIM_TYPE = "appmcp"
SCOPE = "jobcards.read accounts.read vehicles.read"

OUTER_TTL_SECONDS = 7 * 24 * 60 * 60  # a week, per Olga's call
ROTATE_EVERY_SECONDS = 20 * 60  # 20 min — comfortably inside the inner 30
RETRY_AFTER_SECONDS = 60  # a failed rotation retries sooner than the full period

# Read at CALL time, never at import. web.py imports this module before it calls
# load_dotenv(), so module-level `os.environ.get` would capture None for every
# one of these — configured() would be False, web.py would skip `vault_ids`
# silently, and the agent's first MCP call would fail with "no credential is
# stored for this server URL".
REQUIRED_ENV = (
    "MCP_TOKEN_ENC_KEY",
    "APPMCP_VAULT_ID",
    "APPMCP_CREDENTIAL_ID",
    "APPMCP_COMPANY_DB",
    "APPMCP_LOGIN_EMAIL",
    "APPMCP_LOGIN_PASSWORD",
)


def configured() -> bool:
    """Whether this host can mint and store the credential.

    Everything is required together: minting needs the key and the login, and
    storing needs the vault. Half a configuration would create an agent that
    declares the MCP and calls it unauthenticated, so web.py attaches the vault
    only when this is true.
    """
    return all(os.environ.get(name) for name in REQUIRED_ENV)


def vault_id() -> str | None:
    """The vault web.py attaches to a session, once the environment is loaded."""
    return os.environ.get("APPMCP_VAULT_ID")


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# The three vars the gateway login itself needs — a subset of REQUIRED_ENV, kept
# separate because the login stands on its own: it is checked and reported apart
# from the vault and encryption config that only the MCP bearer uses.
LOGIN_ENV = ("APPMCP_COMPANY_DB", "APPMCP_LOGIN_EMAIL", "APPMCP_LOGIN_PASSWORD")


def login_request() -> tuple[str, dict]:
    """The gateway login call — its URL and JSON body. ONE definition of the
    credential, kept apart from `_fetch_user_token` (its only caller now) so the
    call can be read and reproduced by hand — `docs/appmcp-connect.md` walks it.
    The allocation pull used to be a second caller; it reads CSVs since
    2026-08-27. Read per call, never at import — see `configured()`.

    `forceLogin` invalidates any other session for this user, so every call here
    kicks a browser logged in as the same account.
    """
    return GATEWAY_LOGIN, {
        "companyId": os.environ["APPMCP_COMPANY_DB"],
        "email": os.environ["APPMCP_LOGIN_EMAIL"],
        "password": os.environ["APPMCP_LOGIN_PASSWORD"],
        "forceLogin": True,
    }


async def _fetch_user_token(http: httpx.AsyncClient) -> str:
    """Log in and take the `__DMS_app_token` off the Set-Cookie header.

    The response body is `{}` — the token is cookie-only. Self-forging one does
    not work: the gateway validates the session server-side, not just the
    signature.
    """
    url, body = login_request()
    response = await http.post(url, json=body)
    response.raise_for_status()
    token = response.cookies.get(USER_TOKEN_COOKIE)
    if not token:
        raise RuntimeError(
            f"login returned {response.status_code} without a {USER_TOKEN_COOKIE} cookie"
        )
    return token


def mint(user_token: str, now: int | None = None) -> str:
    """Wrap a user token in the compact JWE the MCP accepts.

    `dir` + `A256GCM` over sha256(MCP_TOKEN_ENC_KEY). Hand-built rather than via
    a JOSE library because that is the whole algorithm for direct encryption:
    the protected header doubles as the AAD and there is no encrypted key, which
    is why the second segment of the five is empty.
    """
    enc_key = os.environ.get("MCP_TOKEN_ENC_KEY")
    if not enc_key:
        raise RuntimeError("MCP_TOKEN_ENC_KEY is not set")
    issued = int(time.time()) if now is None else now
    claims = {
        "typ": CLAIM_TYPE,
        "userToken": user_token,
        "userId": os.environ.get("APPMCP_LOGIN_EMAIL"),
        "companyDB": os.environ.get("APPMCP_COMPANY_DB"),
        "scope": SCOPE,
        "iss": ISS,
        "aud": APPMCP_URL,
        "iat": issued,
        "exp": issued + OUTER_TTL_SECONDS,
    }
    protected = _b64u(json.dumps({"alg": "dir", "enc": "A256GCM"}, separators=(",", ":")).encode())
    iv = os.urandom(12)
    sealed = AESGCM(hashlib.sha256(enc_key.encode()).digest()).encrypt(
        iv, json.dumps(claims, separators=(",", ":")).encode(), protected.encode("ascii")
    )
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return ".".join([protected, "", _b64u(iv), _b64u(ciphertext), _b64u(tag)])


async def rotate(client, http: httpx.AsyncClient) -> None:
    """Log in, mint, and overwrite the stored bearer.

    `mcp_server_url` is immutable and preserved across the update; only the
    token changes. A running session picks the new value up on its next MCP
    call, because the proxy reads the credential per request.
    """
    token = mint(await _fetch_user_token(http))
    await client.beta.vaults.credentials.update(
        credential_id=os.environ["APPMCP_CREDENTIAL_ID"],
        vault_id=os.environ["APPMCP_VAULT_ID"],
        auth={"type": "static_bearer", "token": token},
    )


async def rotate_once(client) -> None:
    """One login + mint + store. Awaited at session start, so the session never
    begins against a credential whose inner token has already expired."""
    async with httpx.AsyncClient(timeout=30) as http:
        await rotate(client, http)
    log.info("app-MCP bearer re-minted into %s", os.environ["APPMCP_CREDENTIAL_ID"])


async def rotate_forever(client) -> None:
    """Keep the credential fresh for as long as the session lives.

    Sleeps FIRST: the caller has just rotated synchronously, and rotating again
    immediately would spend a login for nothing. A failed rotation is logged and
    retried sooner rather than ending the loop — the stored credential is still
    good for the rest of its 30 minutes, so one transient login failure is not
    worth losing the rotator over.
    """
    while True:
        await asyncio.sleep(ROTATE_EVERY_SECONDS)
        try:
            await rotate_once(client)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("app-MCP credential rotation failed (%s); retrying in 60s", e)
            await asyncio.sleep(RETRY_AFTER_SECONDS)
        except Exception:
            log.exception("app-MCP credential rotation failed; retrying in 60s")
            await asyncio.sleep(RETRY_AFTER_SECONDS)
