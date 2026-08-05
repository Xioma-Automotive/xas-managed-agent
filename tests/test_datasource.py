"""The pull comes from a callable source, host-side (DECIDE-7).

Guards the seam:
  1. the scenario-engine fake returns the rich contract and it flattens;
  2. the source is selected by env, defaulting to the offline fake;
  3. the real XAS source is a clean stub (raises, doesn't silently return junk)
     and refuses to construct without its credentials.
"""

import json

import pytest

import datasource
from xas_allocation.flatten import flatten

CONTRACT_KEYS = {"meta", "pos", "sos", "supply", "disruption"}


def test_scenario_source_returns_the_rich_contract():
    rich = datasource.ScenarioEngineSource().pull()
    assert CONTRACT_KEYS <= set(rich), "scenario pull is missing contract keys"
    # and it is actually flatten-able into a non-empty snapshot
    snap = flatten(rich)
    assert snap.orders and snap.units and snap.incumbent


def test_scenario_source_matches_committed_dataset():
    """The default fake reads the committed dataset — stable and offline, so the
    determinism suite and this source agree byte-for-byte."""
    rich = datasource.ScenarioEngineSource().pull()
    committed = json.loads(datasource.DATASET_PATH.read_text())
    assert rich == committed


def test_scenario_source_can_regenerate():
    rich = datasource.ScenarioEngineSource(regenerate=True, seed=20).pull()
    assert CONTRACT_KEYS <= set(rich)


def test_get_source_defaults_to_scenario(monkeypatch):
    monkeypatch.delenv("XAS_DATA_SOURCE", raising=False)
    assert isinstance(datasource.get_source(), datasource.ScenarioEngineSource)


def test_get_source_selects_xas(monkeypatch):
    monkeypatch.setenv("XAS_DATA_SOURCE", "xas")
    monkeypatch.setenv("XAS_API_BASE", "https://xas.example/api")
    monkeypatch.setenv("XAS_API_TOKEN", "secret")
    src = datasource.get_source()
    assert isinstance(src, datasource.XASApiSource)
    # the endpoint does not exist yet — it must fail loudly, not fabricate
    with pytest.raises(NotImplementedError):
        src.pull()


def test_xas_source_needs_credentials(monkeypatch):
    monkeypatch.setenv("XAS_DATA_SOURCE", "xas")
    monkeypatch.delenv("XAS_API_BASE", raising=False)
    monkeypatch.delenv("XAS_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="XAS_API_BASE"):
        datasource.get_source()


def test_get_source_rejects_unknown(monkeypatch):
    monkeypatch.setenv("XAS_DATA_SOURCE", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="unknown"):
        datasource.get_source()
