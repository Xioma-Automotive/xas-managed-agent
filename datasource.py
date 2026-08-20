"""Where the allocation pull comes from — a callable source, resolved HOST-SIDE.

This is DECIDE-7 made concrete. The rich pull the agent repairs used to ship
frozen inside the skill bundle (`data/pull.json`); now the host fetches it from a
**data source** at session start and mounts it into the sandbox as a file (see
`web.py`). The source is chosen by config:

  - ``ScenarioEngineSource`` — the fabricated dataset (the FAKE). Default, offline,
    byte-for-byte with the tests. This is what keeps the whole suite runnable with
    no network until a real XAS exists.
  - ``XASApiSource`` — the real, credentialed HTTP pull. Stubbed, because the XAS
    endpoint does not exist yet; the response→contract mapping is documented so it
    is a small, testable change the day a sample lands.

**This module runs on our host, never in the sandbox** — same rule that keeps
`scenario_engine/`'s code out of the agent. HTTP and credentials live here, where
the sandbox can't see them; the agent only ever receives an already-fetched file.

The contract every source returns is the rich pull shape `flatten()` and
`alloc_tools.summarize()` already consume::

    {meta, vsos, vehicles, disruption}

Select with the ``XAS_DATA_SOURCE`` env var (``scenario`` | ``xas``).

This serves the ALLOCATION lane only. The REPORTING lane used to mount a
fabricated ``jobcards.json`` from here as well; it reads the live system through
the `xas-app-mcp` tools now, so nothing host-side fetches records.

The tenant TAXONOMY used to be mounted from here too. It now ships inside the
`xas-qa` skill bundle instead (see `setup_agent.qa_bundle`) — one tenant, so a
static file beats a per-session upload. That collapses the caller's choice of
dealership: DECIDE-16 in `xas_allocation.decisions` is the note to undo this the
day a second tenant exists.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
DATASET_PATH = DATA_DIR / "pull.json"


@runtime_checkable
class DataSource(Protocol):
    """A callable pull. ``scope`` is a future fetch-filter (which customers /
    month / POs to fetch) — accepted now, ignored until the pull grows a
    parameter; see the plan's non-goals."""

    def pull(self, scope: dict | None = None) -> dict: ...


@dataclass
class ScenarioEngineSource:
    """The fake: the fabricated dataset. Reads the committed ``data/pull.json`` by
    default (stable + offline), or regenerates it from the scenario engine when
    ``regenerate`` is set (varies the starting conditions)."""

    dataset_path: Path = DATASET_PATH
    regenerate: bool = False
    seed: int = 20

    def pull(self, scope: dict | None = None) -> dict:
        if self.regenerate:
            from scenario_engine.generate import generate

            return generate(seed=self.seed)["pull"]
        return json.loads(self.dataset_path.read_text())


@dataclass
class XASApiSource:
    """The real pull — a credentialed HTTP GET against XAS, mapped into the rich
    contract. STUBBED: the endpoint does not exist yet (DECIDE-7).

    When it does, ``pull`` should:
      1. ``httpx.get(f"{base_url}/allocation/pull", headers={"Authorization":
         f"Bearer {token}"}, params=_fetch_params(scope), timeout=…)``;
      2. hand the JSON to ``_map_response`` — a PURE function that shapes the XAS
         response into ``{meta, vsos, vehicles, disruption}`` (unit-testable
         against a captured sample, no network).

    The credential lives here on the host and is NEVER shipped to the sandbox.
    """

    base_url: str
    token: str
    timeout: float = 30.0

    def pull(self, scope: dict | None = None) -> dict:
        raise NotImplementedError(
            "The real XAS pull endpoint does not exist yet (DECIDE-7). Set "
            "XAS_DATA_SOURCE=scenario to use the fabricated dataset. When XAS "
            "exists, implement the GET + _map_response mapping documented on "
            "XASApiSource; the rest of the pipeline is unchanged."
        )

    @staticmethod
    def _map_response(xas_json: dict) -> dict:  # pragma: no cover - stub
        """Map an XAS pull response into the rich contract {meta, vsos,
        vehicles, disruption}. Pure; fill in once a sample response exists."""
        raise NotImplementedError


def get_source() -> DataSource:
    """Pick the data source from the environment. Defaults to the fabricated
    dataset so dev and CI run with no network and no credentials."""
    kind = (os.environ.get("XAS_DATA_SOURCE") or "scenario").strip().lower()
    if kind == "xas":
        base = os.environ.get("XAS_API_BASE")
        token = os.environ.get("XAS_API_TOKEN")
        if not (base and token):
            raise RuntimeError(
                "XAS_DATA_SOURCE=xas needs XAS_API_BASE and XAS_API_TOKEN in the "
                "environment (host-side credentials, never shipped to the sandbox)."
            )
        return XASApiSource(base_url=base, token=token)
    if kind == "scenario":
        return ScenarioEngineSource()
    raise RuntimeError(f"unknown XAS_DATA_SOURCE {kind!r} (expected 'scenario' or 'xas')")
